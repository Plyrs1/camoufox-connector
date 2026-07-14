"""Tests for health API proxy endpoint behavior."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from camoufox_connector.config import Settings
from camoufox_connector.health import create_health_app
from camoufox_connector.pool import BrowserInstance, BrowserPool


def _pool_with_instance() -> BrowserPool:
    settings = Settings(geoip=False, public_ws_url="ws://public.example:8080")
    pool = BrowserPool(settings)
    instance = BrowserInstance(index=0, port=9222)
    instance.ws_endpoint = "ws://127.0.0.1:9222/browser"
    instance.proxy_token = "stable-token"
    instance.proxy_endpoint = "ws://public.example:8080/ws/stable-token"
    instance.process = AsyncMock()
    instance.process.returncode = None
    instance.is_healthy = True
    pool.instances.append(instance)
    return pool


def test_next_returns_proxy_endpoint_and_browser_status():
    pool = _pool_with_instance()
    app = create_health_app(pool)

    with TestClient(app) as client:
        response = client.get("/next")

    assert response.status_code == 200
    data = response.json()
    assert data["endpoint"] == "ws://public.example:8080/ws/stable-token"
    assert data["proxy_endpoint"] == "ws://public.example:8080/ws/stable-token"
    assert data["browser"] == {
        "index": 0,
        "status": "idle",
        "healthy": True,
        "connections": 0,
        "total_connections": 0,
    }


def test_endpoints_returns_proxy_endpoint_objects():
    pool = _pool_with_instance()
    app = create_health_app(pool)

    with TestClient(app) as client:
        response = client.get("/endpoints")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["endpoints"][0]["endpoint"] == "ws://public.example:8080/ws/stable-token"
    assert data["endpoints"][0]["status"] == "idle"


def test_unknown_proxy_token_is_rejected():
    pool = _pool_with_instance()
    app = create_health_app(pool)

    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/missing-token"):
                pass


def test_websocket_proxy_forwards_text_frames():
    pool = _pool_with_instance()
    app = create_health_app(pool)

    class FakeUpstream:
        def __init__(self):
            self.sent = []
            self.closed = False

        async def send(self, message):
            self.sent.append(message)

        async def close(self):
            self.closed = True

        def __aiter__(self):
            self._messages = iter(["from-browser"])
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration:
                await asyncio.sleep(0.05)
                raise StopAsyncIteration

    fake_upstream = FakeUpstream()

    async def fake_connect(_url):
        return fake_upstream

    with patch("camoufox_connector.health.websockets.connect", side_effect=fake_connect):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/stable-token") as websocket:
                websocket.send_text("from-client")
                assert websocket.receive_text() == "from-browser"

    assert fake_upstream.sent == ["from-client"]
    assert fake_upstream.closed is True
    assert pool.instances[0].connections == 0
    assert pool.instances[0].total_connections == 1
