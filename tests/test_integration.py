"""Integration tests for the Camoufox Connector browser pool.

These tests exercise the full lifecycle:
  1. Start a BrowserPool (spawns launcher → Node.js → Camoufox)
  2. Confirm a healthy WebSocket endpoint is reported
  3. Stop the pool gracefully (or forcefully on timeout)
  4. Verify no orphan process remains on the configured port

Requirements:
  - Camoufox binaries downloaded (run ``camoufox fetch`` first)
  - Xvfb for headless mode on Linux (Docker image already installs it)
  - A few hundred MB of disk space
  - Playwright version compatible with camoufox (browserServerImpl.js must exist)

All tests use generous timeouts because first browser startup can take 10-30s.
"""

import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from playwright._impl._driver import compute_driver_executable

from camoufox_connector.config import Settings, ServerMode
from camoufox_connector.pool import BrowserPool


# Use a high ephemeral port range so we don't collide with any existing services.
_INTEG_WS_PORT_START = 59_000


def _browser_server_impl_exists() -> bool:
    """Check whether the undocumented Playwright internal exists.

    Camoufox's launchServer.js depends on ``lib/browserServerImpl.js`` inside
    the Playwright driver package.  This file was removed/renamed in some
    Playwright releases.  When it is missing, ``launch_server`` will fail with
    ``MODULE_NOT_FOUND``.
    """
    _nodejs = compute_driver_executable()[0]
    nodejs = _nodejs[0] if isinstance(_nodejs, tuple) else _nodejs
    pkg_dir = Path(nodejs).parent / "package"
    impl = pkg_dir / "lib" / "browserServerImpl.js"
    return impl.exists()


def _browser_can_run() -> bool:
    """Check whether the Camoufox binary has all required shared libraries.

    A missing ``libgtk-3.so.0`` (common in minimal headless containers)
    produces an immediate ``XPCOMGlueLoad error`` on launch.
    """
    try:
        result = subprocess.run(
            ["ldd", "/home/py/.cache/camoufox/libmozgtk.so"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "not found" not in result.stdout
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def _check_environment():
    """Fail fast with a clear message if the environment cannot run integration tests."""
    if not _browser_server_impl_exists():
        pytest.skip(
            "Playwright internal 'browserServerImpl.js' not found. "
            "Integration tests requiring a real browser launch are skipped.",
            allow_module_level=True,
        )
    if not _browser_can_run():
        pytest.skip(
            "Camoufox browser runtime dependencies (e.g. libgtk-3-0) not installed. "
            "Integration tests requiring a real browser launch are skipped.",
            allow_module_level=True,
        )


@pytest_asyncio.fixture
async def pool():
    """Yield a started BrowserPool; always stop it on teardown."""
    settings = Settings(
        mode=ServerMode.SINGLE,
        headless=True,
        geoip=False,
        humanize=False,
        ws_port_start=_INTEG_WS_PORT_START,
        debug=False,
    )
    browser_pool = BrowserPool(settings=settings)

    try:
        await asyncio.wait_for(browser_pool.start(), timeout=120)
    except asyncio.TimeoutError:
        pytest.fail("BrowserPool.start() timed out (>120s)")

    yield browser_pool

    # Teardown — try graceful stop first, then force-kill orphans if needed
    try:
        await asyncio.wait_for(browser_pool.stop(), timeout=30)
    except asyncio.TimeoutError:
        # If graceful stop times out, try to clean up any remaining stale pids
        for inst in browser_pool.instances:
            pids = BrowserPool._find_pids_binding_port(inst.port)
            for pid in pids:
                try:
                    # fast sync kill so we don't block pytest exit
                    import os
                    import signal
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        pytest.fail("BrowserPool.stop() timed out (>30s) — leaked processes may remain")


@pytest.mark.asyncio
async def test_pool_starts_and_reports_ws_endpoint(pool):
    """Pool should start, create a healthy instance, and expose a ws:// URL."""
    assert len(pool.instances) == 1

    inst = pool.instances[0]
    assert inst.is_healthy is True
    assert inst.ws_endpoint is not None
    assert inst.ws_endpoint.startswith("ws://")
    assert str(inst.port) in inst.ws_endpoint

    stats = pool.get_stats()
    assert stats["healthy_instances"] == 1
    assert stats["total_instances"] == 1


@pytest.mark.asyncio
async def test_restart_instance_reclaims_port(pool):
    """Restarting an instance should not fail with 'port already in use'."""
    inst = pool.instances[0]
    old_endpoint = inst.ws_endpoint
    assert old_endpoint is not None

    # Restart — this exercises the port-reclaim logic in _start_instance
    ok = await asyncio.wait_for(pool.restart_instance(inst.index), timeout=120)
    assert ok is True

    assert inst.is_healthy is True
    # Endpoint may differ (new browser process = new ws path)
    assert inst.ws_endpoint is not None
    assert inst.ws_endpoint.startswith("ws://")


@pytest.mark.asyncio
async def test_stop_releases_port_no_orphans(pool):
    """After stop(), the configured port should be free (no orphan processes)."""
    inst = pool.instances[0]
    port = inst.port

    await asyncio.wait_for(pool.stop(), timeout=30)

    # Verify the port is no longer occupied
    assert BrowserPool._is_port_free(port) is True

    # Extra safety: verify no PIDs are still bound to the port
    pids = BrowserPool._find_pids_binding_port(port)
    assert pids == [], f"Orphan process(es) still bound to port {port}: {pids}"


@pytest.mark.asyncio
async def test_round_robin_endpoint_distribution(pool):
    """Multiple calls to get_next_endpoint should distribute across instances."""
    # In single mode there's only one browser, so all requests go to it
    ep1 = await pool.get_next_endpoint()
    ep2 = await pool.get_next_endpoint()

    assert ep1 is not None
    assert ep2 is not None

    stats = pool.get_stats()
    assert stats["total_connections"] == 0
    assert stats["active_connections"] == 0

    # Endpoint selection does not count as an active connection; the WebSocket
    # proxy increments counters only when a client actually connects.
