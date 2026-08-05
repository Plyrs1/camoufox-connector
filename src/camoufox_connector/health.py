"""
HTTP Health Check API for Camoufox Connector.

Provides endpoints for health monitoring and browser pool management.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket

if TYPE_CHECKING:
    from .pool import BrowserPool

logger = logging.getLogger(__name__)


def create_health_app(pool: BrowserPool) -> Starlette:
    """
    Create a Starlette application for health checks and management.

    Args:
        pool: Browser pool instance to monitor

    Returns:
        Starlette application instance
    """

    async def health(request: Request) -> Response:
        """
        Health check endpoint.

        Returns 200 if at least one browser is healthy, 503 otherwise.
        """
        health_status = await pool.health_check()

        status_code = 200 if health_status["healthy"] else 503

        return JSONResponse(
            {
                "status": "healthy" if health_status["healthy"] else "unhealthy",
                "mode": pool.settings.mode.value,
                "instances": health_status["instances"],
            },
            status_code=status_code,
        )

    async def endpoints(request: Request) -> Response:
        """
        Get available WebSocket endpoints.

        Returns a list of all healthy browser endpoints.
        """
        all_endpoints = pool.get_all_endpoints()

        return JSONResponse({
            "endpoints": all_endpoints,
            "count": len(all_endpoints),
        })

    async def next_endpoint(request: Request) -> Response:
        """
        Get the next available proxied endpoint using round-robin.

        This is the primary endpoint for clients to get a browser.
        """
        instance = await pool.get_next_instance()

        if instance is None or instance.proxy_endpoint is None:
            return JSONResponse(
                {"error": "No healthy browser instances available"},
                status_code=503,
            )

        return JSONResponse({
            "endpoint": instance.proxy_endpoint,
            "proxy_endpoint": instance.proxy_endpoint,
            "browser": {
                "index": instance.index,
                "status": instance.status,
                "healthy": instance.is_healthy,
                "connections": instance.connections,
                "total_connections": instance.total_connections,
            },
        })

    async def stats(request: Request) -> Response:
        """
        Get detailed pool statistics.

        Returns connection counts, uptime, and instance details.
        """
        return JSONResponse(pool.get_stats())

    async def restart_instance(request: Request) -> Response:
        """
        Restart a specific browser instance.

        POST /restart/{index}
        """
        try:
            index = int(request.path_params["index"])
        except (KeyError, ValueError):
            return JSONResponse(
                {"error": "Invalid instance index"},
                status_code=400,
            )

        success = await pool.restart_instance(index)

        if success:
            return JSONResponse({
                "status": "restarted",
                "index": index,
            })
        else:
            return JSONResponse(
                {"error": f"Failed to restart instance {index}"},
                status_code=500,
            )

    async def info(request: Request) -> Response:
        """
        Get server information and configuration.
        """
        from . import __version__

        return JSONResponse({
            "name": "camoufox-connector",
            "version": __version__,
            "mode": pool.settings.mode.value,
            "pool_size": len(pool.instances),
            "config": {
                "headless": pool.settings.headless,
                "geoip": pool.settings.geoip,
                "humanize": pool.settings.humanize,
                "block_images": pool.settings.block_images,
                "proxy": "configured" if pool.settings.proxy else None,
                "public_ws_url": pool.settings.get_public_ws_base_url(),
            },
        })

    async def websocket_proxy(websocket: WebSocket) -> None:
        """Proxy a client WebSocket connection to a browser instance."""
        token = websocket.path_params.get("token")
        instance = pool.get_instance_by_proxy_token(token) if token else None

        if (
            instance is None
            or not instance.is_healthy
            or instance.ws_endpoint is None
            or instance.process is None
            or instance.process.returncode is not None
        ):
            await websocket.close(code=1008)
            return

        upstream = None
        counted_connection = False
        try:
            upstream = await websockets.connect(instance.ws_endpoint)
            await websocket.accept()
            instance.connections += 1
            instance.total_connections += 1
            counted_connection = True

            async def client_to_browser() -> None:
                while True:
                    message = await websocket.receive()
                    message_type = message.get("type")
                    if message_type == "websocket.disconnect":
                        break
                    if "text" in message:
                        await upstream.send(message["text"])
                    elif "bytes" in message:
                        await upstream.send(message["bytes"])

            async def browser_to_client() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = [
                asyncio.create_task(client_to_browser()),
                asyncio.create_task(browser_to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except Exception as exc:
            if pool.settings.debug:
                logger.debug("WebSocket proxy error for browser %s: %s", instance.index, exc)
            try:
                await websocket.close(code=1011)
            except RuntimeError:
                pass
        finally:
            if counted_connection and instance.connections > 0:
                instance.connections -= 1
            if upstream is not None:
                await upstream.close()

    routes = [
        Route("/", info, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/endpoints", endpoints, methods=["GET"]),
        Route("/next", next_endpoint, methods=["GET"]),
        WebSocketRoute("/ws/{token}", websocket_proxy),
        Route("/stats", stats, methods=["GET"]),
        Route("/restart/{index:int}", restart_instance, methods=["POST"]),
    ]

    if pool.settings.mcp_enabled:
        from .mcp import create_mcp
        mcp, manager = create_mcp(
            pool,
            pool.settings.mcp_session_timeout,
            pool.settings.mcp_state_dir,
            pool.settings.mcp_host,
        )
        routes.append(Mount(pool.settings.mcp_path, app=mcp.streamable_http_app()))

        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def lifespan(app):
            try:
                async with mcp.session_manager.run():
                    await manager.start()
                    yield
            finally:
                await manager.cleanup()
    else:
        lifespan = None

    app = Starlette(debug=pool.settings.debug, routes=routes, lifespan=lifespan)
    return app


async def run_health_server(pool: BrowserPool) -> None:
    """
    Run the health check HTTP server.

    Args:
        pool: Browser pool instance to monitor
    """
    import uvicorn

    app = create_health_app(pool)

    config = uvicorn.Config(
        app,
        host=pool.settings.api_host,
        port=pool.settings.api_port,
        log_level="info" if pool.settings.debug else "warning",
        access_log=pool.settings.debug,
    )

    server = uvicorn.Server(config)
    await server.serve()
