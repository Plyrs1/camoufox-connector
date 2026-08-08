"""Tests for camoufox_connector.pool port tracking utilities."""

import asyncio
import os
import signal
import time
from unittest.mock import patch

def _fake_file(content: str):
    """Helper returning a file-like object from a string."""
    from io import StringIO
    return StringIO(content)

import pytest
from types import SimpleNamespace

from camoufox_connector.config import Settings
from camoufox_connector.pool import (
    _find_pids_for_inodes,
    _get_listening_inodes_for_port,
    BrowserInstance,
    BrowserPool,
    Lease,
)


class TestBrowserInstanceStatus:
    """Tests for BrowserInstance.status."""

    def test_status_inactive_without_process_or_endpoint(self):
        instance = BrowserInstance(index=0, port=9222)

        assert instance.status == "inactive"

    def test_status_error_when_unhealthy_with_endpoint(self):
        instance = BrowserInstance(index=0, port=9222)
        instance.process = object()
        instance.ws_endpoint = "ws://127.0.0.1:9222/browser"
        instance.is_healthy = False

        assert instance.status == "error"

    def test_status_idle_when_healthy_without_connections(self):
        instance = BrowserInstance(index=0, port=9222)
        instance.process = object()
        instance.ws_endpoint = "ws://127.0.0.1:9222/browser"
        instance.is_healthy = True

        assert instance.status == "idle"

    def test_status_busy_when_healthy_with_connections(self):
        instance = BrowserInstance(index=0, port=9222)
        instance.process = object()
        instance.ws_endpoint = "ws://127.0.0.1:9222/browser"
        instance.is_healthy = True
        instance.connections = 1

        assert instance.status == "busy"


class TestProxyEndpoints:
    """Tests for BrowserPool proxy endpoint helpers."""

    def test_build_proxy_endpoint_uses_public_ws_url(self):
        pool = BrowserPool(Settings(geoip=False, public_ws_url="wss://browser.example.com"))

        assert pool._build_proxy_endpoint("abc") == "wss://browser.example.com/ws/abc"

    def test_get_instance_by_proxy_token(self):
        pool = BrowserPool(Settings(geoip=False))
        instance = BrowserInstance(index=0, port=9222, proxy_token="abc")
        pool.instances.append(instance)

        assert pool.get_instance_by_proxy_token("abc") is instance
        assert pool.get_instance_by_proxy_token("missing") is None


class TestIsPortFree:
    """Tests for BrowserPool._is_port_free."""

    def test_high_random_port_is_free(self):
        """A high ephemeral port should normally be free."""
        assert BrowserPool._is_port_free(59999) is True

    def test_localhost_binding_only(self):
        """Only check 127.0.0.1 by default."""
        # Port 0 is a sentinel; connect_ex(0) returns an error quickly.
        assert BrowserPool._is_port_free(0) is True


class TestFindPidsBindingPort:
    """Tests for BrowserPool._find_pids_binding_port (pure-Python /proc scan)."""

    @patch(
        "camoufox_connector.pool._get_listening_inodes_for_port",
        return_value={12345, 67890},
    )
    @patch(
        "camoufox_connector.pool._find_pids_for_inodes",
        return_value=[100, 200],
    )
    def test_returns_pids(self, _mock_find, _mock_inodes):
        pids = BrowserPool._find_pids_binding_port(8080)
        assert sorted(pids) == [100, 200]

    @patch(
        "camoufox_connector.pool._get_listening_inodes_for_port",
        return_value=set(),
    )
    def test_empty_when_no_inodes(self, _mock):
        pids = BrowserPool._find_pids_binding_port(8080)
        assert pids == []


class TestKillProcessTree:
    """Tests for BrowserPool._kill_process_tree."""

    @pytest.mark.asyncio
    async def test_sigterm_then_sigkill(self):
        """Should send SIGTERM, wait, then SIGKILL if process still alive."""
        with patch("os.kill") as mock_kill:
            # First call (SIGTERM) succeeds, second call (kill 0 check) succeeds,
            # third call (SIGKILL) succeeds.
            mock_kill.side_effect = [None, None, None]
            await BrowserPool._kill_process_tree(12345, timeout=0.1)

        assert mock_kill.call_count == 3
        assert mock_kill.call_args_list[0][0] == (12345, signal.SIGTERM)
        assert mock_kill.call_args_list[1][0] == (12345, 0)
        assert mock_kill.call_args_list[2][0] == (12345, signal.SIGKILL)

    @pytest.mark.asyncio
    async def test_already_dead_on_sigterm(self):
        """Should stop immediately if ProcessLookupError on SIGTERM."""
        with patch("os.kill", side_effect=ProcessLookupError()):
            # Should not raise
            await BrowserPool._kill_process_tree(12345, timeout=0.1)

    @pytest.mark.asyncio
    async def test_permission_denied(self):
        """Should handle PermissionError gracefully."""
        with patch("os.kill", side_effect=PermissionError()):
            await BrowserPool._kill_process_tree(12345, timeout=0.1)


class TestGetListeningInodes:
    """Tests for _get_listening_inodes_for_port."""

    @patch(
        "builtins.open",
        side_effect=[
            # /proc/net/tcp
            _fake_file(
                "  sl  local_address rem_address   st tx_queue:rx_queue tr:tm->when retrnsmt   uid  timeout inode\n"
                "   0: 0100007F:23F0 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345\n"
                "   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 67890\n"
            ),
            # /proc/net/tcp6 — not present
            FileNotFoundError(),
        ],
    )
    def test_parses_tcp_listening(self, _mock_open):
        # Port 0x23F0 = 9200, 0x1F90 = 8080
        inodes = _get_listening_inodes_for_port(8080)
        assert inodes == {67890}

    @patch("builtins.open", side_effect=FileNotFoundError())
    def test_returns_empty_on_missing_proc(self, _mock):
        inodes = _get_listening_inodes_for_port(8080)
        assert inodes == set()


class TestHealthCheckAutoRestart:
    @pytest.mark.asyncio
    async def test_restarts_dead_unleased_instance(self):
        pool = BrowserPool(Settings(geoip=False))
        instance = BrowserInstance(index=0, port=9222, is_healthy=True, leased=False)
        instance.process = SimpleNamespace(returncode=1, pid=1234)
        instance.ws_endpoint = "ws://127.0.0.1:9222/test"
        instance.proxy_token = "token"
        instance.proxy_endpoint = "ws://localhost:8080/ws/token"
        pool.instances = [instance]

        with patch.object(pool, "_restart_instance_work", return_value=True) as mock_restart:
            result = await pool.health_check()

        assert result["healthy"] is False
        mock_restart.assert_called_once_with(instance)

    @pytest.mark.asyncio
    async def test_restarts_dead_reserved_instance_and_invalidates_token(self):
        pool = BrowserPool(Settings(geoip=False))
        instance = BrowserInstance(index=0, port=9222, is_healthy=True, leased=True)
        instance.owner = "next"
        instance.process = SimpleNamespace(returncode=1, pid=1234)
        instance.ws_endpoint = "ws://127.0.0.1:9222/test"
        instance.proxy_token = "token"
        instance.proxy_endpoint = "ws://localhost:8080/ws/token"
        pool.instances = [instance]
        pool._leases["token"] = Lease("token", instance)

        with patch.object(pool, "_restart_instance_work", return_value=True) as mock_restart:
            result = await pool.health_check()

        assert result["healthy"] is False
        # The stale token was invalidated: it can never reopen the dead browser.
        assert "token" not in pool._leases
        assert instance.leased is False
        mock_restart.assert_called_once_with(instance)

    @pytest.mark.asyncio
    async def test_restart_schedule_guard_avoids_duplicate(self):
        pool = BrowserPool(Settings(geoip=False))
        instance = BrowserInstance(index=0, port=9222)

        blocker = asyncio.Future()

        async def slow_restart(_instance):
            await blocker
            return True

        with patch.object(pool, "_restart_instance_work", side_effect=slow_restart):
            first = pool._schedule_restart(instance)
            second = pool._schedule_restart(instance)
            assert first is not None
            assert second is first
            blocker.set_result(True)
            assert await first is True

    @pytest.mark.asyncio
    async def test_restart_rejects_instance_with_live_reservation(self):
        pool = BrowserPool(Settings(geoip=False))
        instance = BrowserInstance(index=0, port=9222, is_healthy=True)
        instance.ws_endpoint = "ws://127.0.0.1:9222/test"
        instance.proxy_token = "token"
        instance.proxy_endpoint = "ws://localhost:8080/ws/token"
        pool.instances = [instance]
        lease_id, _ = await pool.acquire_lease()
        assert lease_id == "token"

        # A live reservation blocks the restart.
        assert await pool.restart_instance(0) is False

        # Explicit release by proxy token permits the restart.
        assert await pool.release_lease("token")
        with patch.object(pool, "_restart_instance_work", return_value=True) as mock_restart:
            assert await pool.restart_instance(0) is True
        mock_restart.assert_called_once_with(instance)

    @pytest.mark.asyncio
    async def test_restart_allowed_after_connection_deadline_expired(self):
        pool = BrowserPool(Settings(geoip=False, connection_timeout=0.05))
        instance = BrowserInstance(index=0, port=9222, is_healthy=True)
        instance.ws_endpoint = "ws://127.0.0.1:9222/test"
        instance.proxy_token = "token"
        instance.proxy_endpoint = "ws://localhost:8080/ws/token"
        pool.instances = [instance]
        lease_id, _ = await pool.acquire_lease()
        assert lease_id == "token"

        await asyncio.sleep(0.1)  # unconnected reservation expires

        with patch.object(pool, "_restart_instance_work", return_value=True) as mock_restart:
            assert await pool.restart_instance(0) is True
        mock_restart.assert_called_once_with(instance)


class TestReservations:
    """Tests for the token-keyed reservation registry and browser lifecycle."""

    def _pool(self, count=1, **settings_kwargs) -> BrowserPool:
        kwargs = {"geoip": False, "connection_timeout": 60.0}
        kwargs.update(settings_kwargs)
        pool = BrowserPool(Settings(**kwargs))
        for i in range(count):
            pool.instances.append(
                BrowserInstance(
                    index=i,
                    port=9222 + i,
                    ws_endpoint=f"ws://127.0.0.1:{9222 + i}/browser",
                    proxy_token=f"token-{i}",
                    proxy_endpoint=f"ws://localhost:8080/ws/token-{i}",
                    is_healthy=True,
                )
            )
        return pool

    @pytest.mark.asyncio
    async def test_acquire_and_release_using_proxy_token(self):
        pool = self._pool()
        acquired = await pool.acquire_lease()
        assert acquired is not None
        lease_id, instance = acquired
        # The reservation key IS the instance's stable proxy token.
        assert lease_id == "token-0" == instance.proxy_token
        assert instance is pool.instances[0]
        assert instance.leased is True
        assert instance.owner == "next"
        assert lease_id in pool._leases
        assert pool._leases[lease_id].instance is instance

        assert await pool.release_lease(lease_id) is True
        assert instance.leased is False
        assert instance.owner is None
        assert lease_id not in pool._leases
        # Releasing twice / unknown tokens fail
        assert await pool.release_lease(lease_id) is False
        assert await pool.release_lease("missing") is False

    @pytest.mark.asyncio
    async def test_acquire_returns_none_when_all_instances_reserved(self):
        pool = self._pool()
        # Everything healthy and unreserved, first acquire succeeds...
        assert await pool.acquire_lease() is not None
        # ...second acquire on a single-instance pool returns None
        assert await pool.acquire_lease() is None
        assert await pool.acquire_lease() is None

    @pytest.mark.asyncio
    async def test_release_permits_reuse(self):
        pool = self._pool()
        lease_id, instance = await pool.acquire_lease()
        assert await pool.release_lease(lease_id)
        acquired = await pool.acquire_lease()
        assert acquired is not None and acquired[1] is instance is not None
        assert acquired[0] == "token-0"

    @pytest.mark.asyncio
    async def test_connection_deadline_expiry_permits_reuse(self):
        pool = self._pool(connection_timeout=0.05)
        lease_id, instance = await pool.acquire_lease()
        assert lease_id == "token-0"
        assert instance.leased is True
        await asyncio.sleep(0.1)
        # A stale unconnected reservation is purged and does not block the
        # next allocation; the same instance is reserved again (its stable
        # proxy token is reused) with a fresh deadline.
        acquired = await pool.acquire_lease()
        assert acquired is not None and acquired[1] is instance
        assert acquired[0] == "token-0"
        assert instance.leased is True
        assert instance.connection_deadline is not None

    @pytest.mark.asyncio
    async def test_first_connection_pins_reservation_never_expires(self):
        pool = self._pool(connection_timeout=0.05, browser_grace_period=60.0)
        lease_id, instance = await pool.acquire_lease()
        assert instance.connection_deadline is not None
        await pool.on_websocket_connected(lease_id)
        assert instance.connections == 1
        assert instance.connection_deadline is None  # pinned forever
        await asyncio.sleep(0.1)
        # The deadline is gone: neither purge nor re-allocation happens.
        assert await pool.acquire_lease() is None
        await pool.on_websocket_disconnected(lease_id)
        task = pool._idle_stop_tasks.get(0)
        if task is not None:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_concurrent_acquire_allocates_each_instance_once(self):
        pool = self._pool(count=3)
        results = await asyncio.gather(*[pool.acquire_lease() for _ in range(5)])
        successes = [r for r in results if r is not None]
        assert len(successes) == 3
        assert {lease_id for lease_id, _ in successes} == {"token-0", "token-1", "token-2"}
        assert {inst.index for _, inst in successes} == {0, 1, 2}
        # No further leases while all instances are held
        assert await pool.acquire_lease() is None
        # Releasing everything makes all instances allocatable again
        for lease_id, _und in successes:
            assert await pool.release_lease(lease_id)
        assert await pool.acquire_lease() is not None

    @pytest.mark.asyncio
    async def test_wrapper_lease_next_instance_and_release_instance(self):
        pool = self._pool()
        instance = await pool.lease_next_instance()  # compatibility wrapper
        assert instance is not None
        assert instance is pool.instances[0]
        assert instance.leased is True
        assert len(pool._leases) == 1
        await pool.release_instance(instance)
        assert instance.leased is False
        assert len(pool._leases) == 0

    @pytest.mark.asyncio
    async def test_zero_connections_grace_stop_stops_immediately(self):
        pool = self._pool(browser_grace_period=0.0)
        lease_id, instance = await pool.acquire_lease()
        await pool.on_websocket_connected(lease_id)
        assert instance.connections == 1

        await pool.on_websocket_disconnected(lease_id)
        task = pool._idle_stop_tasks.get(0)
        assert task is not None
        await task

        # grace == 0: the last disconnect stops the browser and invalidates
        # its token (the reservation is dropped, a fresh /next restarts it).
        assert instance.intentionally_stopped is True
        assert instance.is_healthy is False
        assert instance.proxy_token is None
        assert "token-0" not in pool._leases

    @pytest.mark.asyncio
    async def test_grace_period_stop_cancelled_by_reconnect(self):
        pool = self._pool(browser_grace_period=1.0)
        lease_id, instance = await pool.acquire_lease()
        await pool.on_websocket_connected(lease_id)
        await pool.on_websocket_disconnected(lease_id)
        task = pool._idle_stop_tasks.get(0)
        assert task is not None and not task.done()

        # grace > 0: a reconnection cancels the pending stop.
        await pool.on_websocket_connected(lease_id)
        assert 0 not in pool._idle_stop_tasks
        assert instance.is_healthy is True

        # After the final disconnect the stop (re)schedules and completes.
        await pool.on_websocket_disconnected(lease_id)
        task = pool._idle_stop_tasks.get(0)
        assert task is not None
        await task
        assert instance.intentionally_stopped is True

    @pytest.mark.asyncio
    async def test_multiple_clients_share_token_and_stop_on_last_disconnect(self):
        pool = self._pool(browser_grace_period=0.0)
        lease_id, instance = await pool.acquire_lease()
        await pool.on_websocket_connected(lease_id)
        await pool.on_websocket_connected(lease_id)
        assert instance.connections == 2

        await pool.on_websocket_disconnected(lease_id)
        assert instance.connections == 1
        assert 0 not in pool._idle_stop_tasks  # still in use

        await pool.on_websocket_disconnected(lease_id)
        task = pool._idle_stop_tasks.get(0)
        assert task is not None
        await task
        assert instance.intentionally_stopped is True
        assert instance.connections == 0

    @pytest.mark.asyncio
    async def test_mcp_owned_instance_is_never_idle_stopped(self):
        pool = self._pool(browser_grace_period=0.0)
        lease_id, instance = await pool.acquire_lease(owner="mcp")
        assert instance.owner == "mcp"
        assert instance.connection_deadline is None  # pool never reaps MCP

        await pool.on_websocket_connected(lease_id)
        await pool.on_websocket_disconnected(lease_id)
        # The proxy connection count hit zero but MCP still owns the browser.
        assert 0 not in pool._idle_stop_tasks
        assert instance.is_healthy is True

        assert await pool.release_lease(lease_id) is True
        assert instance.owner is None
        assert instance.leased is False

    @pytest.mark.asyncio
    async def test_reserve_next_restarts_intentionally_stopped_slot(self):
        pool = BrowserPool(Settings(geoip=False, connection_timeout=60.0))
        instance = BrowserInstance(index=0, port=9222)
        instance.intentionally_stopped = True  # no process, not healthy
        pool.instances.append(instance)

        async def fake_restart(inst):
            assert inst is instance
            instance.is_healthy = True
            instance.ws_endpoint = "ws://127.0.0.1:9222/browser"
            instance.proxy_token = "fresh-token"
            instance.proxy_endpoint = "ws://localhost:8080/ws/fresh-token"
            return True

        with patch.object(pool, "_restart_instance_work", side_effect=fake_restart):
            acquired = await pool.reserve_next_instance()

        assert acquired is not None
        assert acquired is instance
        assert instance.proxy_token == "fresh-token"
        assert instance.leased is True
        assert "fresh-token" in pool._leases

    @pytest.mark.asyncio
    async def test_concurrent_reserve_serializes_single_guarded_restart(self):
        pool = BrowserPool(Settings(geoip=False, connection_timeout=60.0))
        instance = BrowserInstance(index=0, port=9222)
        instance.intentionally_stopped = True
        pool.instances.append(instance)

        restart_calls = []

        async def fake_restart(inst):
            restart_calls.append(inst)
            await asyncio.sleep(0.05)
            instance.is_healthy = True
            instance.ws_endpoint = "ws://127.0.0.1:9222/browser"
            instance.proxy_token = "fresh-token"
            instance.proxy_endpoint = "ws://localhost:8080/ws/fresh-token"
            return True

        with patch.object(pool, "_restart_instance_work", side_effect=fake_restart):
            results = await asyncio.gather(
                pool.reserve_next_instance(), pool.reserve_next_instance()
            )

        # Exactly one guarded restart ran; one caller won the slot, the other
        # caller found no available instance.
        assert len(restart_calls) == 1
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        assert winners[0] is instance


class TestFindPidsForInodes:
    """Tests for _find_pids_for_inodes."""

    @patch("os.listdir", side_effect=[
        ["1", "2", "self"],           # /proc entries
        ["0", "1", "2", "255"],       # pid 1 fd entries
        ["0", "1"],                   # pid 2 fd entries (no match)
    ])
    @patch("os.readlink", side_effect=[
        "socket:[12345]",
        "pipe:[99999]",
        "socket:[67890]",
        "anon_inode:[eventfd]",
    ])
    def test_finds_matching_pids(self, _mock_readlink, _mock_listdir):
        pids = _find_pids_for_inodes({12345})
        assert pids == [1]

    @patch("os.listdir", side_effect=OSError())
    def test_returns_empty_on_error(self, _mock):
        pids = _find_pids_for_inodes({12345})
        assert pids == []


def _fake_file(content: str):
    """Helper returning a file-like object from a string."""
    from io import StringIO

    return StringIO(content)
