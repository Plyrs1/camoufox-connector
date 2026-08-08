"""Embedded Streamable HTTP MCP browser-control server."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import secrets
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .pool import BrowserInstance, BrowserPool


@dataclass
class BrowserSession:
    id: str
    lease_id: str
    instance: BrowserInstance
    browser: Browser
    context: BrowserContext
    page: Page
    last_activity: float
    tab_ids: dict[int, str] = field(default_factory=dict)


class BrowserSessionManager:
    """Own persistent browser sessions and their pool leases."""

    def __init__(
        self,
        pool: BrowserPool,
        timeout: float = 1800,
        state_dir: str | Path = ".camoufox-connector/mcp-state",
    ) -> None:
        self.pool = pool
        self.timeout = timeout
        self.state_dir = Path(state_dir)
        self.max_backup_size = 10 * 1024 * 1024
        self.sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._sweeper: asyncio.Task[None] | None = None

    async def start(self) -> None:
        async with self._lock:
            if self._sweeper is None:
                self._sweeper = asyncio.create_task(self._sweep())

    async def _sweep(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(max(self.timeout / 4, 0.1), 60))
                async with self._lock:
                    expired = [
                        session
                        for session in self.sessions.values()
                        if time.monotonic() - session.last_activity >= self.timeout
                    ]
                    for session in expired:
                        await self._close_locked(session)
        except asyncio.CancelledError:
            return

    async def create(self) -> BrowserSession:
        acquired = await self.pool.acquire_lease(timeout=self.timeout)
        if acquired is None:
            raise RuntimeError("No healthy browser instance is available")
        lease_id, instance = acquired
        if not instance.ws_endpoint:
            # Defensive: the pool should never hand out an endpoint-less instance.
            await self.pool.release_lease(lease_id)
            raise RuntimeError("No healthy browser instance is available")

        async with self._lock:
            browser = context = page = None
            try:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                browser = await self._playwright.firefox.connect(instance.ws_endpoint)
                context = await browser.new_context()
                page = await context.new_page()
                session = BrowserSession(
                    uuid.uuid4().hex,
                    lease_id,
                    instance,
                    browser,
                    context,
                    page,
                    time.monotonic(),
                )
                session.tab_ids[id(page)] = uuid.uuid4().hex
                self.sessions[session.id] = session
                return session
            except Exception:
                await self._close_resources(browser, context)
                await self.pool.release_lease(lease_id)
                raise

    async def get(self, session_id: str) -> BrowserSession:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        async with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise ValueError("Unknown browser session id")
            if not session.instance.is_healthy:
                await self._close_locked(session)
                raise RuntimeError("The leased browser instance is no longer healthy")
            session.last_activity = time.monotonic()
            return session

    async def close(self, session_id: str) -> bool:
        async with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                return False
            await self._close_locked(session)
            return True

    async def _close_resources(self, browser: Any, context: Any) -> None:
        for resource in (context, browser):
            if resource is not None:
                try:
                    await resource.close()
                except Exception:
                    pass

    async def _close_locked(self, session: BrowserSession) -> None:
        self.sessions.pop(session.id, None)
        await self._close_resources(session.browser, session.context)
        try:
            await self.pool.release_lease(session.lease_id)
        except Exception:
            pass

    async def keyboard_press(self, session_id: str, key: str, delay_ms: int = 0) -> None:
        if not key.strip() or delay_ms < 0 or delay_ms > 60000:
            raise ValueError("key must be non-empty and delay_ms must be between 0 and 60000")
        await (await self.get(session_id)).page.keyboard.press(key, delay=delay_ms)

    async def keyboard_type(self, session_id: str, text: str, delay_ms: int = 0) -> None:
        if not text or len(text) > 100000 or delay_ms < 0 or delay_ms > 60000:
            raise ValueError("text must be non-empty and delay_ms must be between 0 and 60000")
        await (await self.get(session_id)).page.keyboard.type(text, delay=delay_ms)

    async def mouse_move(self, session_id: str, x: float, y: float, steps: int = 1) -> None:
        if not all(math.isfinite(v) for v in (x, y)) or steps < 1 or steps > 1000:
            raise ValueError("coordinates must be finite and steps must be between 1 and 1000")
        await (await self.get(session_id)).page.mouse.move(x, y, steps=steps)

    async def mouse_click(
        self,
        session_id: str,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int = 0,
    ) -> None:
        if (
            not all(math.isfinite(v) for v in (x, y))
            or button not in {"left", "middle", "right"}
            or click_count < 1
            or click_count > 10
            or delay_ms < 0
            or delay_ms > 60000
        ):
            raise ValueError("invalid mouse click arguments")
        await (await self.get(session_id)).page.mouse.click(
            x, y, button=button, click_count=click_count, delay=delay_ms
        )

    async def mouse_wheel(self, session_id: str, delta_x: float, delta_y: float) -> None:
        if not all(math.isfinite(v) for v in (delta_x, delta_y)):
            raise ValueError("wheel deltas must be finite")
        await (await self.get(session_id)).page.mouse.wheel(delta_x, delta_y)

    def _backup_path(self, backup_id: str) -> Path:
        if not backup_id or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", backup_id):
            raise ValueError("Invalid backup id")
        return self.state_dir / f"{backup_id}.json"

    async def backup_state(self, session_id: str, name: str | None = None) -> dict[str, Any]:
        session = await self.get(session_id)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        state = await session.context.storage_state()
        storage: dict[str, dict[str, dict[str, str]]] = {}
        for page in session.context.pages:
            origin = await page.evaluate("location.origin")
            if origin.startswith(("http://", "https://")):
                storage.setdefault(origin, {})["session_storage"] = await page.evaluate(
                    "() => Object.fromEntries(Object.entries(sessionStorage))"
                )
                storage[origin]["local_storage"] = await page.evaluate(
                    "() => Object.fromEntries(Object.entries(localStorage))"
                )
        backup_id = secrets.token_urlsafe(32).rstrip("=")
        payload = {
            "version": 1,
            "created_at": time.time(),
            "name": name.strip()[:200] if name and name.strip() else None,
            "cookies": state.get("cookies", []),
            "origins": state.get("origins", []),
            "session_storage": storage,
        }
        path = self._backup_path(backup_id)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.max_backup_size:
            raise ValueError("Backup exceeds maximum size")
        fd, temp = tempfile.mkstemp(dir=self.state_dir, prefix=".backup-", text=False)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fchmod(stream.fileno(), 0o600)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return {
            "backup_id": backup_id,
            "name": payload["name"],
            "created_at": payload["created_at"],
        }

    def list_state_backups(self) -> list[dict[str, Any]]:
        if not self.state_dir.is_dir():
            return []
        result = []
        for path in self.state_dir.glob("*.json"):
            if path.is_symlink() or path.stat().st_size > self.max_backup_size:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != 1:
                    continue
                if not (isinstance(data.get("name"), (str, type(None))) and isinstance(data.get("created_at"), (int, float))):
                    continue
                result.append(
                    {
                        "backup_id": path.stem,
                        "name": data.get("name"),
                        "created_at": data.get("created_at"),
                    }
                )
            except (OSError, ValueError):
                continue
        return result

    def delete_state_backup(self, backup_id: str) -> bool:
        path = self._backup_path(backup_id)
        if path.is_symlink():
            raise ValueError("Invalid backup path")
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    async def restore_state(self, session_id: str, backup_id: str) -> dict[str, Any]:
        session = await self.get(session_id)
        path = self._backup_path(backup_id)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > self.max_backup_size:
            raise ValueError("Invalid or oversized backup")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Malformed backup JSON") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Unsupported backup version")
        cookies = data.get("cookies")
        storage_data = data.get("session_storage")
        if not isinstance(cookies, list) or not isinstance(storage_data, dict):
            raise ValueError("Invalid backup schema")
        await session.context.add_cookies(cookies)
        failures = []
        active = session.page
        original_pages = set(session.context.pages)
        for origin, values in storage_data.items():
            if not isinstance(origin, str) or not isinstance(values, dict):
                raise ValueError("Invalid backup schema")
            if urlparse(origin).scheme not in {"http", "https"} or not urlparse(origin).netloc:
                raise ValueError("Invalid backup origin")
            if not all(isinstance(values.get(key, {}), dict) for key in ("local_storage", "session_storage")):
                raise ValueError("Invalid backup schema")
            try:
                page = None
                for candidate in session.context.pages:
                    if await candidate.evaluate("location.origin") == origin:
                        page = candidate
                        break
                if page is None:
                    page = await session.context.new_page()
                    await page.goto(origin)
                await page.evaluate(
                    "(v) => { for (const [k,x] of Object.entries(v)) localStorage.setItem(k,x) }",
                    dict(values.get("local_storage", {})),
                )
                await page.evaluate(
                    "(v) => { for (const [k,x] of Object.entries(v)) sessionStorage.setItem(k,x) }",
                    dict(values.get("session_storage", {})),
                )
            except Exception as exc:
                failures.append({"origin": origin, "error": str(exc)})
        for page in set(session.context.pages) - original_pages:
            try:
                await page.close()
            except Exception:
                pass
        session.page = active
        return {
            "backup_id": backup_id,
            "restored_origins": len(data.get("session_storage", {})),
            "failures": failures,
        }

    async def cleanup(self) -> None:
        async with self._lock:
            sweeper = self._sweeper
            self._sweeper = None
            if sweeper is not None:
                sweeper.cancel()
            for session in list(self.sessions.values()):
                await self._close_locked(session)
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
        if sweeper is not None:
            await asyncio.gather(sweeper, return_exceptions=True)


def _validate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url.strip()
    if url.strip() == "about:blank":
        return url.strip()
    raise ValueError("url must be an absolute http(s) URL or about:blank")


def _mcp_security_hosts(host: str | None, api_port: int) -> tuple[list[str], list[str]]:
    """Build strict MCP Host/Origin allowlists while retaining local access."""
    allowed_hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"]
    allowed_origins = [
        "http://localhost", "http://localhost:*",
        "http://127.0.0.1", "http://127.0.0.1:*",
        "http://[::1]", "http://[::1]:*",
    ]
    if host:
        value = host.strip()
        if not value or any(c.isspace() for c in value) or "://" in value:
            raise ValueError("mcp_host must be a hostname or IP with an optional port")
        if value.endswith(":*"):
            base = value[:-2]
            wildcard = True
        else:
            base, wildcard = value, False
        parsed = urlsplit(f"//{base}")
        if "*" in base or not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("mcp_host must be a hostname or IP with an optional port")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("mcp_host has an invalid port") from exc
        if port is None and not wildcard:
            value = f"{value}:{api_port}"
        allowed_hosts.append(value)
        origin = f"http://{value}"
        allowed_origins.append(origin)
    return allowed_hosts, allowed_origins


def create_mcp(
    pool: BrowserPool, timeout: float, state_dir: str | Path = ".camoufox-connector/mcp-state",
    host: str | None = None,
) -> tuple[FastMCP, BrowserSessionManager]:
    manager = BrowserSessionManager(pool, timeout, state_dir)
    api_port = getattr(getattr(pool, "settings", None), "api_port", 8080)
    allowed_hosts, allowed_origins = _mcp_security_hosts(host, api_port)
    transport_security = TransportSecuritySettings(
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    mcp = FastMCP(
        "camoufox-connector", streamable_http_path="/", transport_security=transport_security
    )

    async def session(session_id: str) -> BrowserSession:
        return await manager.get(session_id)

    @mcp.tool()
    async def create_session() -> dict[str, Any]:
        """Create a session pinned to one healthy browser instance."""
        await manager.start()
        created = await manager.create()
        return {
            "session_id": created.id,
            "instance": created.instance.index,
            "url": created.page.url,
        }

    @mcp.tool()
    async def close_session(session_id: str) -> dict[str, bool]:
        """Close a session and release its browser lease."""
        return {"closed": await manager.close(session_id)}

    @mcp.tool()
    async def navigate(session_id: str, url: str) -> dict[str, str]:
        """Navigate the active tab to an absolute URL."""
        target = _validate_url(url)
        current = await session(session_id)
        await current.page.goto(target)
        return {"url": current.page.url}

    @mcp.tool()
    async def snapshot(
        session_id: str, max_text: int = 10000, max_content: int = 20000
    ) -> dict[str, Any]:
        """Return bounded title, URL, visible text, and HTML."""
        if max_text < 0 or max_content < 0:
            raise ValueError("bounds must be nonnegative")
        current = await session(session_id)
        text = await current.page.locator("body").inner_text()
        content = await current.page.content()
        return {
            "url": current.page.url,
            "title": await current.page.title(),
            "text": text[:max_text],
            "content": content[:max_content],
            "text_truncated": len(text) > max_text,
            "content_truncated": len(content) > max_content,
        }

    @mcp.tool()
    async def click(session_id: str, selector: str) -> dict[str, bool]:
        """Click a CSS selector on the active tab."""
        if not selector.strip():
            raise ValueError("selector must not be empty")
        await (await session(session_id)).page.locator(selector).click()
        return {"clicked": True}

    @mcp.tool()
    async def fill(session_id: str, selector: str, value: str) -> dict[str, bool]:
        """Fill a CSS selector on the active tab."""
        if not selector.strip():
            raise ValueError("selector must not be empty")
        await (await session(session_id)).page.locator(selector).fill(value)
        return {"filled": True}

    @mcp.tool()
    async def evaluate(session_id: str, expression: str) -> Any:
        """Evaluate JavaScript in the active tab."""
        if not expression.strip():
            raise ValueError("expression must not be empty")
        return await (await session(session_id)).page.evaluate(expression)

    @mcp.tool()
    async def screenshot(session_id: str) -> dict[str, str]:
        """Capture the active tab as base64 PNG."""
        data = await (await session(session_id)).page.screenshot()
        return {"mime_type": "image/png", "data": base64.b64encode(data).decode()}

    @mcp.tool()
    async def tabs(session_id: str) -> list[dict[str, Any]]:
        """List tabs with stable session-local identifiers."""
        current = await session(session_id)
        result = []
        for page in current.context.pages:
            tab_id = current.tab_ids.setdefault(id(page), uuid.uuid4().hex)
            result.append(
                {
                    "tab_id": tab_id,
                    "url": page.url,
                    "title": await page.title(),
                    "active": page is current.page,
                }
            )
        return result

    @mcp.tool()
    async def new_tab(session_id: str, url: str = "about:blank") -> dict[str, str]:
        """Create and select a tab."""
        current = await session(session_id)
        page = await current.context.new_page()
        current.tab_ids[id(page)] = uuid.uuid4().hex
        current.page = page
        await page.goto(_validate_url(url))
        return {"tab_id": current.tab_ids[id(page)], "url": page.url}

    @mcp.tool()
    async def select_tab(session_id: str, tab_id: str) -> dict[str, str]:
        """Select a tab by its session-local identifier."""
        current = await session(session_id)
        for page in current.context.pages:
            if current.tab_ids.get(id(page)) == tab_id:
                current.page = page
                return {"tab_id": tab_id, "url": page.url}
        raise ValueError("Unknown tab id")

    @mcp.tool()
    async def close_tab(session_id: str, tab_id: str) -> dict[str, bool]:
        """Close a tab and keep a valid active tab."""
        current = await session(session_id)
        for page in list(current.context.pages):
            if current.tab_ids.get(id(page)) == tab_id:
                await page.close()
                current.tab_ids.pop(id(page), None)
                if not current.context.pages:
                    current.page = await current.context.new_page()
                    current.tab_ids[id(current.page)] = uuid.uuid4().hex
                elif current.page is page:
                    current.page = current.context.pages[0]
                return {"closed": True}
        raise ValueError("Unknown tab id")

    @mcp.tool()
    async def keyboard_press(session_id: str, key: str, delay_ms: int = 0) -> dict[str, bool]:
        await manager.keyboard_press(session_id, key, delay_ms)
        return {"pressed": True}

    @mcp.tool()
    async def keyboard_type(session_id: str, text: str, delay_ms: int = 0) -> dict[str, bool]:
        await manager.keyboard_type(session_id, text, delay_ms)
        return {"typed": True}

    @mcp.tool()
    async def mouse_move(session_id: str, x: float, y: float, steps: int = 1) -> dict[str, bool]:
        await manager.mouse_move(session_id, x, y, steps)
        return {"moved": True}

    @mcp.tool()
    async def mouse_click(
        session_id: str,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int = 0,
    ) -> dict[str, bool]:
        await manager.mouse_click(session_id, x, y, button, click_count, delay_ms)
        return {"clicked": True}

    @mcp.tool()
    async def mouse_wheel(session_id: str, delta_x: float, delta_y: float) -> dict[str, bool]:
        await manager.mouse_wheel(session_id, delta_x, delta_y)
        return {"scrolled": True}

    @mcp.tool()
    async def backup_state(session_id: str, name: str | None = None) -> dict[str, Any]:
        return await manager.backup_state(session_id, name)

    @mcp.tool()
    async def list_state_backups() -> list[dict[str, Any]]:
        return manager.list_state_backups()

    @mcp.tool()
    async def restore_state(session_id: str, backup_id: str) -> dict[str, Any]:
        return await manager.restore_state(session_id, backup_id)

    @mcp.tool()
    async def delete_state_backup(backup_id: str) -> dict[str, bool]:
        return {"deleted": manager.delete_state_backup(backup_id)}

    return mcp, manager
