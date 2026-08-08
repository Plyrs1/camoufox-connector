"""Tests for health API proxy endpoint behavior."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from camoufox_connector.config import Settings
from camoufox_connector.health import create_health_app
from camoufox_connector.pool import BrowserInstance, BrowserPool


def _pool_with_instances(
    count: int = 1,
    connection_timeout: float = 300.0,
    browser_grace_period: float = 0.0,
) -> BrowserPool:
    settings = Settings(
        geoip=False,
        public_ws_url="ws://public.example:8080",
        connection_timeout=connection_timeout,
        browser_grace_period=browser_grace_period,
    )
    pool = BrowserPool(settings)
    for i in range(count):
        instance = BrowserInstance(index=i, port=9222 + i)
        instance.ws_endpoint = f"ws://127.0.0.1:{9222 + i}/browser"
        instance.proxy_token = f"stable-token-{i}"
        instance.proxy_endpoint = f"ws://public.example:8080/ws/stable-token-{i}"
        instance.process = AsyncMock()
        instance.process.returncode = None
        instance.is_healthy = True
        pool.instances.append(instance)
    return pool


def _pool_with_instance(**kwargs) -> BrowserPool:
    return _pool_with_instances(1, **kwargs)


def test_next_returns_proxy_endpoint_and_browser_status():
    pool = _pool_with_instance()
    app = create_health_app(pool)

    with TestClient(app) as client:
        response = client.get("/next")

    assert response.status_code == 200
    data = response.json()
    assert data["endpoint"] == "ws://public.example:8080/ws/stable-token-0"
    assert data["proxy_endpoint"] == "ws://public.example:8080/ws/stable-token-0"
    # The reservation key is the instance's stable proxy token.
    assert data["lease_id"] == "stable-token-0"
    assert data["browser"] == {
        "index": 0,
        "status": "idle",
        "healthy": True,
        "connections": 0,
        "total_connections": 0,
    }


def test_next_reserves_second_call_503_until_release():
    pool = _pool_with_instance()
    app = create_health_app(pool)

    with TestClient(app) as client:
        first = client.get("/next")
        assert first.status_code == 200
        token = first.json()["lease_id"]
        assert token == "stable-token-0"

        # The single instance is reserved; the next allocation must fail.
        second = client.get("/next")
        assert second.status_code == 503

        # Releasing the token permits reuse.
        release = client.post(f"/release/{token}")
        assert release.status_code == 200
        assert release.json() == {"status": "released", "lease_id": token}

        third = client.get("/next")
        assert third.status_code == 200


def test_release_invalid_token_returns_404():
    pool = _pool_with_instance()
    app = create_health_app(pool)

    with TestClient(app) as client:
        assert client.post("/release/missing-token").status_code == 404


def test_stale_unconnected_reservation_is_reaped():
    pool = _pool_with_instances(count=2, connection_timeout=0.05)
    app = create_health_app(pool)

    with TestClient(app) as client:
        first = client.get("/next")
        assert first.status_code == 200
        token_a = first.json()["lease_id"]

        time.sleep(0.1)  # the unconnected reservation expires while browsing

        # The expired reservation frees the instance: the next /next moves on.
        second = client.get("/next")
        assert second.status_code == 200
        token_b = second.json()["lease_id"]
        assert token_b != token_a
        assert second.json()["browser"]["index"] != first.json()["browser"]["index"]

        # The expired token is now invalid.
        assert client.post(f"/release/{token_a}").status_code == 404


def test_endpoints_returns_proxy_endpoint_objects():
    pool = _pool_with_instance()
    app = create_health_app(pool)

    with TestClient(app) as client:
        response = client.get("/endpoints")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["endpoints"][0]["endpoint"] == "ws://public.example:8080/ws/stable-token-0"
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
            with client.websocket_connect("/ws/stable-token-0") as websocket:
                websocket.send_text("from-client")
                assert websocket.receive_text() == "from-browser"

    assert fake_upstream.sent == ["from-client"]
    assert fake_upstream.closed is True
    assert pool.instances[0].connections == 0
    assert pool.instances[0].total_connections == 1


def test_websocket_finally_invokes_pool_disconnect_and_grace_stop():
    """The /ws/{token} finally must drive the pool's disconnect lifecycle."""
    pool = _pool_with_instance()  # browser_grace_period == 0 -> immediate stop
    app = create_health_app(pool)

    class FakeUpstream:
        def __init__(self):
            self.closed = False

        async def send(self, message):
            pass

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0.05)
            raise StopAsyncIteration

    fake_upstream = FakeUpstream()

    async def fake_connect(_url):
        return fake_upstream

    with patch("camoufox_connector.health.websockets.connect", side_effect=fake_connect):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/stable-token-0") as websocket:
                websocket.send_text("hello")

            # The websocket handler's finally already invoked
            # pool.on_websocket_disconnected: with grace 0 the last disconnect
            # stops the idle browser (task runs on the TestClient's loop).
            instance = pool.instances[0]
            deadline = time.time() + 5.0
            while time.time() < deadline and not instance.intentionally_stopped:
                time.sleep(0.01)

            assert instance.intentionally_stopped is True
            assert instance.is_healthy is False
            assert instance.proxy_token is None
            assert instance.connections == 0
            assert "stable-token-0" not in pool._leases

    assert fake_upstream.closed is True