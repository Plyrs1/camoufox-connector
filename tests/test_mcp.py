"""Tests for MCP session lifecycle, input forwarding, and state backups."""

import asyncio
import json
import math
import os
from types import SimpleNamespace
from typing import Any, get_type_hints
from urllib.parse import urlparse

import pytest
from starlette.routing import Mount
from starlette.testclient import TestClient

from camoufox_connector.config import Settings
from camoufox_connector.health import create_health_app
from camoufox_connector.mcp import BrowserSessionManager, _mcp_security_hosts, create_mcp
from camoufox_connector.pool import BrowserInstance, BrowserPool


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.calls = []
        self.closed = False
        self.local = {"local": "value"}
        self.session = {"session": "value"}
        self.keyboard = SimpleNamespace(press=self.press, type=self.type)
        self.mouse = SimpleNamespace(move=self.move, click=self.click, wheel=self.wheel)

    async def close(self):
        self.closed = True

    async def goto(self, url):
        self.url = url

    async def evaluate(self, expression, value=None):
        if expression == "location.origin":
            return urlparse(self.url).scheme + "://" + urlparse(self.url).netloc if self.url.startswith(("http://", "https://")) else "null"
        if "Object.fromEntries" in expression:
            return self.session if "sessionStorage" in expression else self.local
        if "localStorage.setItem" in expression:
            self.local = value
        if "sessionStorage.setItem" in expression:
            self.session = value

    async def press(self, key, **kwargs):
        self.calls.append(("press", key, kwargs))

    async def type(self, text, **kwargs):
        self.calls.append(("type", text, kwargs))

    async def move(self, x, y, **kwargs):
        self.calls.append(("move", x, y, kwargs))

    async def click(self, x, y, **kwargs):
        self.calls.append(("click", x, y, kwargs))

    async def wheel(self, x, y):
        self.calls.append(("wheel", x, y))


class FakeContext:
    def __init__(self):
        self.pages = []
        self.cookies = [{"name": "sid", "value": "one"}]
        self.added_cookies = []

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    async def storage_state(self):
        return {"cookies": self.cookies, "origins": [{"origin": "https://example.test"}]}

    async def add_cookies(self, cookies):
        self.added_cookies = cookies

    async def close(self):
        for page in self.pages:
            page.closed = True


class FakeBrowser:
    async def new_context(self):
        context = FakeContext()
        await context.new_page()
        return context

    async def close(self):
        pass


class FakePlaywright:
    def __init__(self):
        self.firefox = self
        self.stopped = False

    async def connect(self, endpoint):
        return FakeBrowser()

    async def stop(self):
        self.stopped = True


class AsyncPlaywrightFactory:
    def __init__(self, playwright):
        self.playwright = playwright

    async def start(self):
        return self.playwright


class FakePool:
    def __init__(self, count=1):
        self.instances = [
            BrowserInstance(i, 9000 + i, "ws://browser", f"token-{i}", "ws://public", is_healthy=True)
            for i in range(count)
        ]

    async def lease_next_instance(self):
        for instance in self.instances:
            if not instance.leased and instance.is_healthy:
                instance.leased = True
                return instance
        return None

    async def release_instance(self, instance):
        instance.leased = False

    async def restart_instance(self, index):
        if self.instances[index].leased:
            raise RuntimeError("Cannot restart a leased browser instance")


@pytest.fixture
async def stack(monkeypatch, tmp_path):
    playwright = FakePlaywright()
    monkeypatch.setattr(
        "camoufox_connector.mcp.async_playwright",
        lambda: AsyncPlaywrightFactory(playwright),
    )
    pool = FakePool()
    manager = BrowserSessionManager(pool, timeout=1, state_dir=tmp_path)
    session = await manager.create()
    session.page.url = "https://example.test/page"
    yield manager, session, pool, tmp_path
    await manager.cleanup()


@pytest.mark.asyncio
async def test_session_affinity_and_atomic_lease_exclusion(monkeypatch):
    playwright = FakePlaywright()
    monkeypatch.setattr("camoufox_connector.mcp.async_playwright", lambda: AsyncPlaywrightFactory(playwright))
    manager = BrowserSessionManager(FakePool(2))
    first = await manager.create()
    second = await manager.create()
    assert first.instance is not second.instance
    assert await manager.close(first.id)
    assert not await manager.close("missing")
    await manager.cleanup()


@pytest.mark.asyncio
async def test_create_failure_releases_lease(monkeypatch):
    class BrokenBrowser:
        async def new_context(self):
            raise RuntimeError("broken")

    class BrokenPW(FakePlaywright):
        async def connect(self, endpoint):
            return BrokenBrowser()

    monkeypatch.setattr("camoufox_connector.mcp.async_playwright", lambda: AsyncPlaywrightFactory(BrokenPW()))
    pool = FakePool()
    manager = BrowserSessionManager(pool)
    with pytest.raises(RuntimeError):
        await manager.create()
    assert not pool.instances[0].leased
    await manager.cleanup()


@pytest.mark.asyncio
async def test_manager_cleanup_unhealthy_release_and_idle_sweep(stack):
    manager, session, pool, _ = stack
    session.last_activity -= 10
    await manager.start()
    await asyncio.sleep(0.35)
    assert session.id not in manager.sessions
    session = await manager.create()
    pool.instances[0].is_healthy = False
    with pytest.raises(RuntimeError):
        await manager.get(session.id)
    assert not pool.instances[0].leased


@pytest.mark.asyncio
async def test_keyboard_and_mouse_forwarding_and_boundaries(stack):
    manager, session, _, _ = stack
    await manager.keyboard_press(session.id, "A", 2)
    await manager.keyboard_type(session.id, "hello", 3)
    await manager.mouse_move(session.id, 1, 2, 4)
    await manager.mouse_click(session.id, 3, 4, "right", 2, 5)
    await manager.mouse_wheel(session.id, 6, -7)
    assert session.page.calls == [
        ("press", "A", {"delay": 2}),
        ("type", "hello", {"delay": 3}),
        ("move", 1, 2, {"steps": 4}),
        ("click", 3, 4, {"button": "right", "click_count": 2, "delay": 5}),
        ("wheel", 6, -7),
    ]
    invalid = [
        (manager.keyboard_press, (session.id, "", 0)),
        (manager.keyboard_press, (session.id, "x", 60001)),
        (manager.keyboard_type, (session.id, "", 0)),
        (manager.mouse_move, (session.id, math.inf, 1, 1)),
        (manager.mouse_move, (session.id, 1, 1, 0)),
        (manager.mouse_click, (session.id, 1, 1, "bad", 1, 0)),
        (manager.mouse_click, (session.id, 1, 1, "left", 0, 0)),
        (manager.mouse_click, (session.id, 1, 1, "left", 1, -1)),
        (manager.mouse_wheel, (session.id, math.nan, 1)),
    ]
    for function, args in invalid:
        with pytest.raises(ValueError):
            await function(*args)


@pytest.mark.asyncio
async def test_backup_metadata_restore_persistence_and_delete(stack):
    manager, session, _, state_dir = stack
    blank = await session.context.new_page()
    backup = await manager.backup_state(session.id, " backup ")
    path = state_dir / f"{backup['backup_id']}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    data = json.loads(path.read_text())
    assert data["cookies"] and data["session_storage"]["https://example.test"]
    assert "null" not in data["session_storage"]
    assert set(manager.list_state_backups()[0]) == {"backup_id", "name", "created_at"}
    recreated = BrowserSessionManager(manager.pool, state_dir=state_dir)
    assert recreated.list_state_backups()
    original_page, count = session.page, len(session.context.pages)
    result = await manager.restore_state(session.id, backup["backup_id"])
    assert result["restored_origins"] == 1
    assert session.page is original_page and len(session.context.pages) == count
    assert session.context.added_cookies
    assert not blank.closed
    assert manager.delete_state_backup(backup["backup_id"])
    with pytest.raises(ValueError):
        manager.delete_state_backup("../escape")


def _mcp_initialize(app_or_client, host="localhost:8080", origin=None):
    headers = {
        "host": host,
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": "2025-03-26",
    }
    if origin is not None:
        headers["origin"] = origin
    if not isinstance(app_or_client, TestClient):
        with TestClient(app_or_client) as client:
            return _mcp_initialize(client, host, origin)
    client = app_or_client
    return client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )


def _mcp_app(mcp_host=None):
    settings = Settings(mcp_enabled=True, mcp_path="/mcp", mcp_host=mcp_host)
    return create_health_app(BrowserPool(settings))


@pytest.mark.parametrize("host", ["localhost:8080", "127.0.0.1:8080"])
def test_mcp_transport_accepts_default_local_hosts(host):
    response = _mcp_initialize(_mcp_app(), host)
    assert response.status_code == 200


def test_mcp_transport_rejects_default_external_host():
    assert _mcp_initialize(_mcp_app(), "evil.example:8080").status_code == 421


def test_mcp_transport_configured_host_and_local_access():
    assert _mcp_initialize(_mcp_app("10.10.0.11:53000"), "10.10.0.11:53000").status_code == 200
    assert _mcp_initialize(_mcp_app("10.10.0.11:53000"), "localhost:8080").status_code == 200
    assert _mcp_initialize(_mcp_app("10.10.0.11:53000"), "10.10.0.11:53001").status_code == 421
    assert _mcp_initialize(_mcp_app("10.10.0.11:53000"), "10.10.0.12:53000").status_code == 421


def test_mcp_transport_origin_allowlist():
    app = _mcp_app("10.10.0.11:53000")
    with TestClient(app) as client:
        assert _mcp_initialize(client, "10.10.0.11:53000", "http://10.10.0.11:53000").status_code == 200
        assert _mcp_initialize(client, "10.10.0.11:53000", "https://evil.example").status_code == 403


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("example.test", "example.test:8080"),
        ("example.test:53000", "example.test:53000"),
        ("example.test:*", "example.test:*"),
    ],
)
def test_mcp_security_host_normalization(host, expected):
    allowed_hosts, allowed_origins = _mcp_security_hosts(host, 8080)
    assert expected in allowed_hosts
    assert f"http://{expected}" in allowed_origins


@pytest.mark.parametrize("host", ["http://example.test", "example.test/path", "example.test:65536", "example .test"])
def test_mcp_security_host_rejects_malformed_values(host):
    with pytest.raises(ValueError):
        _mcp_security_hosts(host, 8080)


def test_mount_config_and_complete_tools():
    settings = Settings(mcp_enabled=True, mcp_path="/mcp")
    app = create_health_app(BrowserPool(settings))
    mount = next(route for route in app.routes if isinstance(route, Mount))
    assert mount.path == "/mcp"
    assert Settings().mcp_enabled is False
    mcp, _ = create_mcp(FakePool(), 1800)
    expected = {"create_session", "close_session", "navigate", "snapshot", "click", "fill", "evaluate", "screenshot", "tabs", "new_tab", "select_tab", "close_tab", "keyboard_press", "keyboard_type", "mouse_move", "mouse_click", "mouse_wheel", "backup_state", "list_state_backups", "restore_state", "delete_state_backup"}
    assert expected <= set(mcp._tool_manager._tools)
    tabs_return = get_type_hints(mcp._tool_manager._tools["tabs"].fn)["return"]
    assert tabs_return == list[dict[str, Any]]


def test_invalid_backup_files_and_symlink(tmp_path):
    manager = BrowserSessionManager(FakePool(), state_dir=tmp_path)
    valid = "A" * 32
    (tmp_path / f"{valid}.json").write_bytes(b"\xff")
    assert manager.list_state_backups() == []
    with pytest.raises(ValueError):
        manager._backup_path("../bad")
    target = tmp_path / "target"
    target.write_text("{}")
    try:
        os.symlink(target, tmp_path / f"{'B' * 32}.json")
        assert manager.list_state_backups() == []
    except OSError:
        pass
