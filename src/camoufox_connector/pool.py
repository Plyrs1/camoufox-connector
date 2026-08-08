"""
Browser pool management for Camoufox Connector.

Manages multiple Camoufox browser instances with round-robin load balancing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from collections.abc import Awaitable, Callable

from .config import Settings

logger = logging.getLogger(__name__)


@dataclass
class BrowserInstance:
    """Represents a single Camoufox browser instance."""

    index: int
    port: int
    ws_endpoint: Optional[str] = None
    proxy_token: Optional[str] = None
    proxy_endpoint: Optional[str] = None
    process: Optional[asyncio.subprocess.Process] = None
    started_at: Optional[float] = None
    connections: int = 0
    total_connections: int = 0
    is_healthy: bool = False
    leased: bool = False
    owner: Optional[str] = None
    restarting: bool = False
    last_health_check: Optional[float] = None
    # Lifecycle state: reservation timeout and idle-stop coordination.
    reserved_at: Optional[float] = None
    connection_deadline: Optional[float] = None
    intentionally_stopped: bool = False

    @property
    def status(self) -> str:
        """Get the browser instance status for API responses."""
        if self.process is None or self.ws_endpoint is None:
            return "inactive"
        if not self.is_healthy:
            return "error"
        if self.connections > 0:
            return "busy"
        return "idle"

    @property
    def uptime(self) -> float:
        """Get uptime in seconds."""
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "index": self.index,
            "port": self.port,
            "ws_endpoint": self.ws_endpoint,
            "proxy_endpoint": self.proxy_endpoint,
            "status": self.status,
            "uptime": round(self.uptime, 2),
            "connections": self.connections,
            "total_connections": self.total_connections,
            "is_healthy": self.is_healthy,
            "leased": self.leased,
            "owner": self.owner,
            "intentionally_stopped": self.intentionally_stopped,
        }


@dataclass
class Lease:
    """A pool reservation keyed by the instance's stable proxy token.

    ``lease_id`` IS ``instance.proxy_token`` — there is no opaque token and
    no expiry field. A reservation's lifetime is governed by
    ``connection_timeout`` while it has zero WebSocket connections; the first
    successful connection clears the deadline, after which it never expires
    by timeout. Enforcement is lazy via
    :meth:`BrowserPool._purge_stale_reservations_locked`.
    """

    lease_id: str  # == instance.proxy_token
    instance: BrowserInstance


@dataclass
class BrowserPool:
    """
    Manages a pool of Camoufox browser instances.

    Provides round-robin load balancing across browser instances,
    each with its own unique fingerprint.

    The pool owns the reservation registry. Reservations are keyed by each
    instance's stable ``proxy_token`` — the token returned by /next and used
    by /ws/{token} is the reservation key, so no opaque ids are needed,
    multiple clients may share one token, and explicit release by proxy
    token works. A reserved instance is excluded from round-robin allocation
    until its reservation is released, expires, or is invalidated by a
    stop/restart.

    Lifecycle: a fresh reservation holds a ``connection_timeout`` deadline
    that applies only while it has zero WebSocket connections; the first
    successful connection clears the deadline so a connected reservation
    never expires by timeout. When the last WebSocket connection drops,
    ``browser_grace_period`` (0 = immediate) triggers a guarded idle stop
    (single task per instance) which a reconnection cancels. MCP-owned
    instances (``owner == "mcp"``) are never idle-stopped. Intentionally
    stopped instances stay registered and are restarted on demand by /next.

    # TODO: Process-level grouping and port tracking are Linux/Unix-only.
    # For Windows support, evaluate:
    #   - subprocess.CREATE_NEW_PROCESS_GROUP for process groups
    #   - os.kill(pid, signal.CTRL_BREAK_EVENT) for signaling
    #   - netstat -ano or GetExtendedTcpTable for port-to-PID mapping
    """

    settings: Settings
    instances: list[BrowserInstance] = field(default_factory=list)
    _current_index: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _running: bool = False
    _restart_tasks: dict[int, asyncio.Task[bool]] = field(default_factory=dict)
    _idle_stop_tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    _leases: dict[str, Lease] = field(default_factory=dict)

    async def start(self) -> None:
        """Start all browser instances in the pool."""
        if self._running:
            logger.warning("Pool is already running")
            return

        self._running = True
        pool_size = 1 if self.settings.mode.value == "single" else self.settings.pool_size

        logger.info(f"Starting browser pool with {pool_size} instance(s)")

        # Create and start instances concurrently
        tasks = []
        for i in range(pool_size):
            instance = BrowserInstance(
                index=i,
                port=self.settings.get_ws_port(i),
            )
            self.instances.append(instance)
            tasks.append(self._start_instance(instance))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Check for failures
        failed = sum(1 for r in results if isinstance(r, Exception))
        if failed > 0:
            logger.error(f"{failed}/{pool_size} browser instances failed to start")

        healthy = sum(1 for inst in self.instances if inst.is_healthy)
        logger.info(f"Browser pool started: {healthy}/{pool_size} healthy instances")

    async def _start_instance(self, instance: BrowserInstance) -> None:
        """Start a single browser instance."""
        try:
            logger.info(f"Starting browser instance {instance.index} on port {instance.port}")

            kwargs = self.settings.to_camoufox_kwargs(
                port=instance.port, index=instance.index
            )

            # Reclaim port if a stale orphan process is still holding it.
            if not self._is_port_free(instance.port):
                logger.warning(f"Port {instance.port} occupied — reclaiming stale process")
                for pid in self._find_pids_binding_port(instance.port):
                    await self._kill_process_tree(pid)
                await asyncio.sleep(0.2)  # Allow OS to release the port

            # Start the launcher as a dedicated module (replaces generated script).
            instance.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "camoufox_connector.launcher",
                json.dumps(kwargs),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            instance.started_at = time.time()

            # Wait for the WebSocket endpoint to be printed
            ws_endpoint = await self._wait_for_endpoint(instance)

            if ws_endpoint:
                instance.ws_endpoint = ws_endpoint
                instance.proxy_token = secrets.token_urlsafe(32)
                instance.proxy_endpoint = self._build_proxy_endpoint(instance.proxy_token)
                instance.is_healthy = True
                logger.info(
                    f"Browser instance {instance.index} ready at {instance.proxy_endpoint}"
                )
            else:
                raise RuntimeError("Failed to get WebSocket endpoint")

        except Exception as e:
            logger.error(f"Failed to start browser instance {instance.index}: {e}")
            # Endpoint startup can fail after the launcher has spawned Node. Clean
            # that process and any port-owning descendants before the next retry.
            await self._stop_instance(instance)
            raise

    # ------------------------------------------------------------------
    # Port tracking (pure-Python, no external tools like lsof)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_port_free(port: int, host: str = "127.0.0.1") -> bool:
        """Check whether a TCP port is currently available."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            return sock.connect_ex((host, port)) != 0

    @staticmethod
    def _find_pids_binding_port(port: int) -> list[int]:
        """List PIDs that have a socket listening on *port*.

        This is a pure-Python implementation that scans ``/proc/*/fd/`` to find
        inodes open as sockets and matches them against entries in
        ``/proc/net/tcp`` (and ``/proc/net/tcp6``).

        Works on Linux only.  Falls back to an empty list on any error.
        """
        try:
            inodes = _get_listening_inodes_for_port(port)
            if not inodes:
                return []
            return _find_pids_for_inodes(inodes)
        except Exception:
            return []

    @staticmethod
    async def _kill_process_tree(pid: int, timeout: float = 5.0) -> None:
        """Send SIGTERM, wait briefly, then SIGKILL if the process still lives."""
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return  # Already gone
        except PermissionError:
            logger.warning(f"Permission denied killing PID {pid}")
            return

        # Short grace period before SIGKILL
        await asyncio.sleep(min(timeout, 0.5))

        try:
            os.kill(pid, 0)  # Check if still alive (raises if dead)
        except ProcessLookupError:
            return

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # ------------------------------------------------------------------

    async def _wait_for_endpoint(
        self,
        instance: BrowserInstance,
        timeout: float = 120.0,  # Increased timeout for first startup
    ) -> Optional[str]:
        """Wait for the browser to print its WebSocket endpoint."""
        if instance.process is None:
            return None

        # Pattern to match WebSocket endpoints
        # Matches various formats:
        # - ws://127.0.0.1:9222/abc123
        # - ws://localhost:9222/abc123
        # - ws://0.0.0.0:9222/abc123
        # - ws://[::1]:9222/abc123
        ws_pattern = re.compile(r"ws://[^\s\)\"']+")

        start_time = time.time()

        # Read from both stdout and stderr concurrently
        while time.time() - start_time < timeout:
            # Check if process died
            if instance.process.returncode is not None:
                # Read remaining stderr for error info
                if instance.process.stderr:
                    try:
                        remaining = await instance.process.stderr.read()
                        error_text = remaining.decode("utf-8", errors="replace")
                        if error_text:
                            logger.error(f"Browser process exited with code {instance.process.returncode}")
                            logger.error(f"Stderr: {error_text}")
                    except Exception:
                        pass
                return None

            # Try to read from both streams
            if instance.process.stdout:
                try:
                    line = await asyncio.wait_for(
                        instance.process.stdout.readline(),
                        timeout=0.5,
                    )
                    if line:
                        text = line.decode("utf-8", errors="replace").strip()
                        # Always log in debug mode, or if it contains 'ws://'
                        if self.settings.debug or 'ws://' in text.lower():
                            logger.debug(f"[Browser {instance.index}] stdout: {text}")

                        match = ws_pattern.search(text)
                        if match:
                            endpoint = match.group(0).rstrip('.,;:!?')  # Clean up trailing punctuation
                            logger.info(f"Found endpoint in stdout: {endpoint}")
                            return endpoint
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    if self.settings.debug:
                        logger.debug(f"Error reading stdout: {e}")

            if instance.process.stderr:
                try:
                    line = await asyncio.wait_for(
                        instance.process.stderr.readline(),
                        timeout=0.5,
                    )
                    if line:
                        text = line.decode("utf-8", errors="replace").strip()
                        # Always log in debug mode, or if it contains 'ws://'
                        if self.settings.debug or 'ws://' in text.lower():
                            logger.debug(f"[Browser {instance.index}] stderr: {text}")

                        match = ws_pattern.search(text)
                        if match:
                            endpoint = match.group(0).rstrip('.,;:!?')  # Clean up trailing punctuation
                            logger.info(f"Found endpoint in stderr: {endpoint}")
                            return endpoint
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    if self.settings.debug:
                        logger.debug(f"Error reading stderr: {e}")

            # Small sleep to avoid busy waiting
            await asyncio.sleep(0.1)

        # Before giving up, try to read any remaining output for debugging
        logger.error(f"Timeout waiting for browser {instance.index} endpoint after {timeout}s")
        
        if instance.process.stdout:
            try:
                remaining = await asyncio.wait_for(instance.process.stdout.read(), timeout=1.0)
                if remaining:
                    output = remaining.decode("utf-8", errors="replace")
                    logger.error(f"Remaining stdout from browser {instance.index}:\n{output}")
            except Exception:
                pass
        
        if instance.process.stderr:
            try:
                remaining = await asyncio.wait_for(instance.process.stderr.read(), timeout=1.0)
                if remaining:
                    output = remaining.decode("utf-8", errors="replace")
                    logger.error(f"Remaining stderr from browser {instance.index}:\n{output}")
            except Exception:
                pass
        
        return None

    async def stop(self) -> None:
        """Stop all browser instances."""
        if not self._running:
            return

        logger.info("Stopping browser pool...")
        self._running = False

        tasks = [self._stop_instance(inst) for inst in self.instances]
        await asyncio.gather(*tasks, return_exceptions=True)

        self.instances.clear()
        self._current_index = 0
        self._leases.clear()
        for task in self._idle_stop_tasks.values():
            task.cancel()
        self._idle_stop_tasks.clear()
        logger.info("Browser pool stopped")

    async def _stop_instance(
        self, instance: BrowserInstance, intentionally_stopped: bool = False
    ) -> None:
        """Stop a single browser instance with orphan cleanup.

        ``intentionally_stopped=True`` marks the slot as an idle stop that
        /next restarts on demand; ``False`` for restart/full-shutdown paths.
        Stopping always invalidates the instance's token — any reservation
        keyed on it is dropped so the stale proxy endpoint can never reopen.
        """
        port = instance.port
        process = instance.process

        try:
            if process is not None:
                # Step 1: terminate launcher process if still alive
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"Force killing browser instance {instance.index}")
                        process.kill()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            logger.warning(f"Cannot kill browser instance {instance.index}")
                else:
                    logger.warning(
                        "Launcher for browser instance %s already exited with code %s",
                        instance.index,
                        process.returncode,
                    )

            # Step 2: always reclaim any remaining process that still owns the instance port
            if not self._is_port_free(port):
                logger.warning(f"Port {port} still occupied after stop — hunting orphans")
                launcher_pid = process.pid if process is not None else None
                for pid in self._find_pids_binding_port(port):
                    if launcher_pid is not None and pid == launcher_pid:
                        continue
                    await self._kill_process_tree(pid)

        except Exception as e:
            logger.error(f"Error stopping browser instance {instance.index}: {e}")
        finally:
            async with self._lock:
                self._drop_reservations_for_instance_locked(instance)
            instance.process = None
            instance.is_healthy = False
            instance.ws_endpoint = None
            instance.proxy_token = None
            instance.proxy_endpoint = None
            instance.reserved_at = None
            instance.connection_deadline = None
            instance.intentionally_stopped = intentionally_stopped

    def _build_proxy_endpoint(self, token: str) -> str:
        """Build the public proxy WebSocket endpoint for a token."""
        return f"{self.settings.get_public_ws_base_url()}/ws/{token}"

    async def get_next_instance(self) -> Optional[BrowserInstance]:
        """
        Get the next available browser instance using round-robin.

        Returns:
            Browser instance or None if no healthy instances are available.
        """
        async with self._lock:
            return await self._get_next_instance_locked()

    # ------------------------------------------------------------------
    # Reservations — keyed by the instance's stable proxy token.
    # ------------------------------------------------------------------

    def _clear_reservation_flags_locked(self, instance: BrowserInstance) -> None:
        """Reset an instance's reservation fields. Call with the lock held."""
        instance.leased = False
        instance.owner = None
        instance.reserved_at = None
        instance.connection_deadline = None

    def _mark_reserved_locked(self, instance: BrowserInstance, owner: str) -> None:
        """Reserve a healthy instance under its stable proxy token."""
        token = instance.proxy_token
        if not token:
            raise RuntimeError(f"Cannot reserve endpoint-less instance {instance.index}")
        instance.leased = True
        instance.owner = owner
        instance.reserved_at = time.monotonic()
        if owner == "mcp":
            # MCP sessions manage their own idle expiry; the pool must never
            # reap or grace-stop an MCP-owned instance (its connections bypass
            # the proxy connection counter).
            instance.connection_deadline = None
        else:
            instance.connection_deadline = time.monotonic() + max(
                self.settings.connection_timeout, 0.0
            )
        self._leases[token] = Lease(token, instance)

    def _drop_reservations_for_instance_locked(self, instance: BrowserInstance) -> None:
        """Drop every reservation held on an instance (token invalidation).

        Must be called while holding ``self._lock``.
        """
        for token, lease in list(self._leases.items()):
            if lease.instance is instance:
                self._leases.pop(token, None)
        self._clear_reservation_flags_locked(instance)

    def _purge_stale_reservations_locked(self) -> None:
        """Drop unconnected reservations that outlived ``connection_timeout``.

        A reservation only expires while it has zero WebSocket connections and
        has not been pinned by a successful connection (its
        ``connection_deadline`` is then cleared). Must be held under ``_lock``.
        """
        now = time.monotonic()
        for token, lease in list(self._leases.items()):
            instance = lease.instance
            if (
                instance.connections == 0
                and instance.connection_deadline is not None
                and instance.connection_deadline <= now
            ):
                self._leases.pop(token, None)
                self._clear_reservation_flags_locked(instance)
                logger.debug(
                    f"Reservation {token} expired; browser instance {instance.index} is available again"
                )

    async def reserve_next_instance(self) -> Optional[BrowserInstance]:
        """
        Atomically reserve the next available healthy browser instance.

        Reservations are stored under the instance's stable ``proxy_token``,
        so ``/next`` hands out the token as the reservation key and multiple
        clients may share it. If no healthy unreserved instance is available,
        an intentionally stopped slot's single guarded restart is awaited
        before returning (concurrent callers are serialized per instance).
        """
        while True:
            async with self._lock:
                self._purge_stale_reservations_locked()
                instance = await self._get_next_instance_locked()
                if instance is not None:
                    self._mark_reserved_locked(instance, owner="next")
                    self._cancel_idle_stop(instance)
                    return instance
                stopped = next(
                    (
                        inst
                        for inst in self.instances
                        if inst.intentionally_stopped and not inst.restarting
                    ),
                    None,
                )
            if stopped is None:
                return None
            task = self._schedule_restart(stopped)
            if not await task:
                return None

    async def acquire_lease(
        self, timeout: Optional[float] = None, owner: str = "next"
    ) -> Optional[tuple[str, BrowserInstance]]:
        """
        Reserve the next available healthy instance, keyed by its proxy token.

        ``timeout`` is accepted for backward compatibility and ignored: a
        reservation's lifetime is governed by ``settings.connection_timeout``
        (and cleared entirely once the first WebSocket connects). Service
        integrations owning the browser directly (MCP) must pass
        ``owner="mcp"`` so the instance is never grace-stopped on idle and is
        never reaped by the connection timeout.

        Returns:
            A ``(proxy_token, instance)`` pair, or ``None`` when no healthy,
            unreserved instance is available.
        """
        async with self._lock:
            self._purge_stale_reservations_locked()
            instance = await self._get_next_instance_locked()
            if instance is None:
                return None
            token = instance.proxy_token
            if token is None:
                return None
            self._mark_reserved_locked(instance, owner=owner)
            return token, instance

    async def release_lease(self, token: str) -> bool:
        """
        Atomically release a reservation by its proxy token.

        Returns:
            ``True`` if the token was a live reservation and the instance was
            released; ``False`` for unknown tokens.
        """
        async with self._lock:
            lease = self._leases.pop(token, None)
            if lease is None:
                return False
            self._clear_reservation_flags_locked(lease.instance)
            logger.debug(f"Reservation {token} released from browser instance {lease.instance.index}")
            return True

    async def lease_next_instance(self, owner: str = "next") -> Optional[BrowserInstance]:
        """Compatibility wrapper: reserve an instance and return only the instance."""
        acquired = await self.acquire_lease(owner=owner)
        return acquired[1] if acquired is not None else None

    async def release_instance(self, instance: BrowserInstance) -> None:
        """Compatibility wrapper: release whichever reservation the instance holds."""
        async with self._lock:
            for token, lease in list(self._leases.items()):
                if lease.instance is instance:
                    self._leases.pop(token, None)
                    self._clear_reservation_flags_locked(instance)
                    break

    async def _get_next_instance_locked(self) -> Optional[BrowserInstance]:
        if not self.instances:
            return None
        for _ in range(len(self.instances)):
            instance = self.instances[self._current_index]
            self._current_index = (self._current_index + 1) % len(self.instances)
            if (
                instance.is_healthy
                and not instance.leased
                and not instance.restarting
                and instance.ws_endpoint
                and instance.proxy_endpoint
            ):
                return instance
        return None

    async def get_next_endpoint(self) -> Optional[str]:
        """
        Get the next available proxied WebSocket endpoint using round-robin.

        The returned endpoint retains its instance's proxy token (the
        reservation key), so explicit release is possible by token.

        Returns:
            Proxied WebSocket endpoint URL or None if no healthy instances are available.
        """
        instance = await self.reserve_next_instance()
        return instance.proxy_endpoint if instance else None

    # ------------------------------------------------------------------
    # WebSocket connection lifecycle (used by the /ws/{token} proxy)
    # ------------------------------------------------------------------

    def _can_idle_stop(self, instance: BrowserInstance) -> bool:
        """Whether an instance may be idle-stopped once all connections drop.

        MCP browser connections attach directly to the internal browser
        endpoint and bypass the proxy connection counter, so a zero count
        does not mean the instance is unused.
        """
        return instance.owner != "mcp"

    def _cancel_idle_stop(self, instance: BrowserInstance) -> None:
        """Cancel a pending guarded idle stop (e.g. on reconnection)."""
        task = self._idle_stop_tasks.pop(instance.index, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_idle_stop(self, instance: BrowserInstance, delay: float) -> None:
        """(Re)schedule a single guarded idle stop for an instance.

        Concurrent disconnects share the per-instance task; a reconnection
        cancels it via :meth:`_cancel_idle_stop`.
        """
        existing = self._idle_stop_tasks.get(instance.index)
        if existing is not None and not existing.done():
            return
        self._cancel_idle_stop(instance)  # drop any finished entry

        async def _runner() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                # Re-check: a reconnection may have arrived while we waited.
                async with self._lock:
                    if instance.connections > 0 or not self._can_idle_stop(instance):
                        return
                logger.info(
                    "Idle stop of browser instance %s after %ss grace",
                    instance.index,
                    delay,
                )
                # Rare race: a reconnection can land between the re-check and
                # the stop; the reconnecting client simply re-runs /next.
                await self._stop_instance(instance, intentionally_stopped=True)
            finally:
                self._idle_stop_tasks.pop(instance.index, None)

        self._idle_stop_tasks[instance.index] = asyncio.create_task(_runner())

    async def on_websocket_connected(self, token: str) -> None:
        """Register a successful proxied WebSocket connection for ``token``.

        The first successful connection pins the reservation: its connection
        deadline is cleared (never expires by timeout) and any pending
        grace-period stop is cancelled.
        """
        async with self._lock:
            instance = self.get_instance_by_proxy_token(token)
            if instance is None:
                return
            instance.connections += 1
            instance.total_connections += 1
            instance.connection_deadline = None
        self._cancel_idle_stop(instance)

    async def on_websocket_disconnected(self, token: str) -> None:
        """Handle a proxied WebSocket disconnect for ``token``.

        On the last disconnect: ``browser_grace_period`` 0 stops the instance
        immediately; a positive grace schedules a guarded stop that a
        reconnection cancels. MCP-owned instances are never idle-stopped.
        """
        async with self._lock:
            instance = self.get_instance_by_proxy_token(token)
            if instance is None:
                return
            if instance.connections > 0:
                instance.connections -= 1
            if instance.connections > 0 or not self._can_idle_stop(instance):
                return
            delay = self.settings.browser_grace_period
        self._schedule_idle_stop(instance, delay)

    def get_instance_by_proxy_token(self, token: str) -> Optional[BrowserInstance]:
        """Get a browser instance by its stable proxy token."""
        for instance in self.instances:
            if instance.proxy_token == token:
                return instance
        return None

    def get_all_endpoints(self) -> list[dict]:
        """Get all healthy proxied WebSocket endpoints with status metadata."""
        return [
            {
                "index": inst.index,
                "endpoint": inst.proxy_endpoint,
                "proxy_endpoint": inst.proxy_endpoint,
                "status": inst.status,
                "healthy": inst.is_healthy,
                "connections": inst.connections,
                "total_connections": inst.total_connections,
            }
            for inst in self.instances
            if inst.is_healthy and inst.proxy_endpoint
        ]

    def get_stats(self) -> dict:
        """Get pool statistics."""
        healthy = sum(1 for inst in self.instances if inst.is_healthy)
        total_connections = sum(inst.total_connections for inst in self.instances)
        active_connections = sum(inst.connections for inst in self.instances)

        return {
            "mode": self.settings.mode.value,
            "total_instances": len(self.instances),
            "healthy_instances": healthy,
            "active_connections": active_connections,
            "total_connections": total_connections,
            "instances": [inst.to_dict() for inst in self.instances],
        }

    async def restart_instance(self, index: int) -> bool:
        """Restart a specific browser instance."""
        if index < 0 or index >= len(self.instances):
            return False

        instance = self.instances[index]
        async with self._lock:
            # Stale (expired) reservations are cleaned before the leased check
            # so an expired token never blocks (or leaks) a restart attempt.
            self._purge_stale_reservations_locked()
            if instance.leased:
                return False
            task = self._schedule_restart(instance)
        return await task

    async def _restart_instance_work(self, instance: BrowserInstance) -> bool:
        self._cancel_idle_stop(instance)
        instance.intentionally_stopped = False
        await self._stop_instance(instance)

        # Reset instance state
        instance.ws_endpoint = None
        instance.proxy_token = None
        instance.proxy_endpoint = None
        instance.started_at = None
        instance.connections = 0
        instance.total_connections = 0
        instance.is_healthy = False

        try:
            await self._start_instance(instance)
            return True
        except Exception as e:
            logger.error(f"Failed to restart instance {instance.index}: {e}")
            return False

    def _schedule_restart(self, instance: BrowserInstance) -> asyncio.Task[bool]:
        existing = self._restart_tasks.get(instance.index)
        if existing is not None and not existing.done():
            return existing

        instance.restarting = True

        async def _runner() -> bool:
            try:
                return await self._restart_instance_work(instance)
            finally:
                instance.restarting = False
                current = self._restart_tasks.get(instance.index)
                if current is asyncio.current_task():
                    self._restart_tasks.pop(instance.index, None)

        task = asyncio.create_task(_runner())
        self._restart_tasks[instance.index] = task
        return task

    async def health_check(self) -> dict:
        """Perform health check on all instances."""
        results = {
            "healthy": True,
            "instances": [],
        }

        restart_tasks: list[Awaitable[bool]] = []

        # Clean stale (expired, unconnected) reservations first so an expired
        # token never leaves its instance reserved (blocking allocation and
        # restart decisions).
        async with self._lock:
            self._purge_stale_reservations_locked()

        for instance in self.instances:
            instance.last_health_check = time.time()

            # Check if process is still running
            is_alive = (
                instance.process is not None
                and instance.process.returncode is None
            )

            if not is_alive and instance.is_healthy:
                logger.warning(f"Browser instance {instance.index} died unexpectedly")
                instance.is_healthy = False
                # An unexpected death invalidates any reservation on this
                # instance: the token it held can never reopen the endpoint.
                # Drop it under the same lock used by allocation so /next
                # cannot pick up a dead instance in between, then restart the
                # dead slot (single guarded task).
                async with self._lock:
                    self._drop_reservations_for_instance_locked(instance)
                    task = self._schedule_restart(instance)
                restart_tasks.append(task)

            results["instances"].append({
                "index": instance.index,
                "healthy": instance.is_healthy,
                "endpoint": instance.proxy_endpoint,
                "proxy_endpoint": instance.proxy_endpoint,
                "status": instance.status,
            })

        if restart_tasks:
            await asyncio.gather(*restart_tasks, return_exceptions=True)

        results["healthy"] = any(inst.is_healthy for inst in self.instances)
        return results


# ------------------------------------------------------------------------------
# Pure-Python port-to-PID helpers (Linux-only)
# ------------------------------------------------------------------------------


def _get_listening_inodes_for_port(port: int) -> set[int]:
    """Parse /proc/net/tcp{,6} and return inodes for sockets listening on *port*."""
    inodes: set[int] = set()
    hex_port = f"{port:04X}"

    for net_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(net_file) as fh:
                next(fh)  # skip header line
                for line in fh:
                    parts = line.strip().split()
                    if len(parts) < 10:
                        continue
                    # parts[1] = "local_address:port"
                    local = parts[1]
                    if not local.endswith(f":{hex_port}"):
                        continue
                    state = parts[3]
                    # 0x0A = TCP_LISTEN
                    if state == "0A":
                        inode_str = parts[9]
                        if inode_str != "0":
                            inodes.add(int(inode_str))
        except FileNotFoundError:
            continue

    return inodes


def _find_pids_for_inodes(inodes: set[int]) -> list[int]:
    """Scan /proc/*/fd/* to find PIDs that own any of the given socket inodes."""
    pids: set[int] = set()
    proc_dir = "/proc"

    try:
        entries = os.listdir(proc_dir)
    except OSError:
        return []

    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = os.path.join(proc_dir, entry, "fd")
        try:
            for fd in os.listdir(fd_dir):
                try:
                    link = os.readlink(os.path.join(fd_dir, fd))
                    # Socket links look like: "socket:[12345]"
                    if link.startswith("socket:[") and link.endswith("]"):
                        inode = int(link[8:-1])
                        if inode in inodes:
                            pids.add(pid)
                            break  # No need to check other fds for this PID
                except (OSError, ValueError):
                    continue
        except OSError:
            continue

    return list(pids)
