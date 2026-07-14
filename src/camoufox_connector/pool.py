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
    last_health_check: Optional[float] = None

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
        }


@dataclass
class BrowserPool:
    """
    Manages a pool of Camoufox browser instances.

    Provides round-robin load balancing across browser instances,
    each with its own unique fingerprint.

    # TODO(Windows): Process group signaling and port tracking are Linux/Unix-only.
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

            kwargs = self.settings.to_camoufox_kwargs(port=instance.port)

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
            instance.is_healthy = False
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
        logger.info("Browser pool stopped")

    async def _stop_instance(self, instance: BrowserInstance) -> None:
        """Stop a single browser instance with orphan cleanup."""
        if instance.process is None:
            return

        port = instance.port
        process = instance.process

        try:
            # Step 1: graceful termination — launcher forwards SIGTERM to the group
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # Step 2: force kill launcher directly
                logger.warning(f"Force killing browser instance {instance.index}")
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(f"Cannot kill browser instance {instance.index}")

            # Step 3: port-based orphan cleanup if the port is still occupied
            if not self._is_port_free(port):
                logger.warning(f"Port {port} still occupied after stop — hunting orphans")
                for pid in self._find_pids_binding_port(port):
                    await self._kill_process_tree(pid)

        except Exception as e:
            logger.error(f"Error stopping browser instance {instance.index}: {e}")
        finally:
            instance.is_healthy = False
            instance.ws_endpoint = None
            instance.proxy_token = None
            instance.proxy_endpoint = None

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
            if not self.instances:
                return None

            attempts = 0
            while attempts < len(self.instances):
                instance = self.instances[self._current_index]
                self._current_index = (self._current_index + 1) % len(self.instances)

                if instance.is_healthy and instance.ws_endpoint and instance.proxy_endpoint:
                    return instance

                attempts += 1

            return None

    async def get_next_endpoint(self) -> Optional[str]:
        """
        Get the next available proxied WebSocket endpoint using round-robin.

        Returns:
            Proxied WebSocket endpoint URL or None if no healthy instances are available.
        """
        instance = await self.get_next_instance()
        return instance.proxy_endpoint if instance else None

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
        await self._stop_instance(instance)

        # Reset instance state
        instance.ws_endpoint = None
        instance.proxy_token = None
        instance.proxy_endpoint = None
        instance.started_at = None
        instance.connections = 0
        instance.is_healthy = False

        try:
            await self._start_instance(instance)
            return True
        except Exception as e:
            logger.error(f"Failed to restart instance {index}: {e}")
            return False

    async def health_check(self) -> dict:
        """Perform health check on all instances."""
        results = {
            "healthy": True,
            "instances": [],
        }

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

            results["instances"].append({
                "index": instance.index,
                "healthy": instance.is_healthy,
                "endpoint": instance.proxy_endpoint,
                "proxy_endpoint": instance.proxy_endpoint,
                "status": instance.status,
            })

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
