"""
Configuration handling for Camoufox Connector.

Supports configuration via:
- Command line arguments
- Environment variables
- JSON configuration files
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_SUPPORTED_PROXY_SCHEMES = ("http", "https", "socks5")


def parse_proxy_url(url: str) -> dict:
    """Parse a proxy URL string into a mapping accepted by ``launch_options``.

    Camoufox >= 0.5.x expects ``proxy`` to be a mapping (``{'server': ...,
    'username': ..., 'password': ...}``) instead of a raw URL string; passing a
    string raises a TypeError.  This helper produces that mapping.

    - ``server`` keeps the scheme/hostname/port (credentials stripped)
    - ``username``/``password`` are percent-decoded via ``urllib.parse.unquote``
    - invalid or malformed URLs are rejected with a descriptive ``ValueError``

    Args:
        url: A proxy URL such as ``http://user:pass@host:8080``.

    Returns:
        A mapping consumed by ``camoufox.launch_options(proxy=...)``.

    Raises:
        ValueError: If the URL is malformed, uses an unsupported scheme, has no
            hostname, has incomplete credentials, or has an invalid port.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Proxy URL must not be empty")
    url = url.strip()

    if "://" not in url:
        raise ValueError(
            f"Malformed proxy URL '{url}': missing scheme "
            "(must be http://, https://, or socks5://)"
        )

    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ValueError(f"Malformed proxy URL '{url}': {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in _SUPPORTED_PROXY_SCHEMES:
        raise ValueError(
            f"Unsupported proxy scheme '{parts.scheme}'; "
            "must be http://, https://, or socks5://"
        )
    if not parts.hostname:
        raise ValueError(f"Proxy URL '{url}' is missing a hostname")

    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"Proxy URL '{url}' has an invalid port: {exc}") from exc

    host = parts.hostname
    if ":" in host:  # IPv6 literals must be re-bracketed for the server field
        host = f"[{host}]"
    server = f"{scheme}://{host}"
    if port is not None:
        server += f":{port}"

    mapping = {"server": server}

    username = parts.username
    password = parts.password
    if username is None and password is None:
        return mapping
    if not username or not password:  # None or empty after urlsplit decoding
        raise ValueError(
            f"Proxy URL '{url}' credentials must include both username and password"
        )
    if "," in username or "," in password:
        raise ValueError(
            "Commas inside proxy credentials are not supported; "
            "percent-encode the comma (e.g. %2C) or omit the credential"
        )
    mapping["username"] = urllib.parse.unquote(username)
    mapping["password"] = urllib.parse.unquote(password)
    return mapping


def parse_proxy_list(value: Optional[str]) -> list:
    """Parse one or more comma-separated proxy URLs into launch mappings."""
    if value is None or value == "":
        return []
    return [parse_proxy_url(item) for item in value.split(",")]


class ServerMode(str, Enum):
    """Operating mode for the connector server."""

    SINGLE = "single"
    POOL = "pool"


class Settings(BaseSettings):
    """
    Configuration settings for Camoufox Connector.

    Settings can be configured via environment variables (prefixed with CAMOUFOX_)
    or directly passed as arguments.
    """

    model_config = SettingsConfigDict(
        env_prefix="CAMOUFOX_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server mode
    mode: ServerMode = Field(
        default=ServerMode.SINGLE,
        description="Operating mode: 'single' for one browser, 'pool' for multiple",
    )

    # Pool configuration
    pool_size: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Number of browser instances in pool mode",
    )

    # Network configuration
    api_port: int = Field(
        default=8080,
        ge=1024,
        le=65535,
        description="HTTP API port for health checks and management",
    )

    ws_port_start: int = Field(
        default=9222,
        ge=1024,
        le=65500,
        description="Starting port for browser WebSocket endpoints",
    )

    api_host: str = Field(
        default="0.0.0.0",
        description="Host to bind the HTTP API to",
    )

    public_ws_url: Optional[str] = Field(
        default=None,
        description="Public WebSocket base URL for proxied browser endpoints",
    )

    # Browser lifecycle configuration
    connection_timeout: float = Field(
        default=300.0,
        ge=0.0,
        description=(
            "Seconds after a /next reservation with zero WebSocket connections it "
            "expires and the instance becomes allocatable again; cleared by the "
            "first successful WebSocket connection (0 = expire immediately)"
        ),
    )
    browser_grace_period: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Grace period in seconds before an instance is stopped after its last "
            "WebSocket disconnect (0 = stop immediately); pending stops are "
            "cancelled by a reconnection"
        ),
    )

    # Browser configuration
    headless: bool = Field(
        default=True,
        description="Run browsers in headless mode",
    )

    geoip: bool = Field(
        default=True,
        description="Enable GeoIP-based locale/timezone spoofing",
    )

    humanize: bool = Field(
        default=True,
        description="Enable humanization features",
    )

    block_images: bool = Field(
        default=False,
        description="Block image loading for faster page loads",
    )

    # Proxy configuration
    proxy: Optional[str] = Field(
        default=None,
        description=(
            "One or more comma-separated proxy URLs "
            "(http://, https://, or socks5://user:pass@host:port). "
            "Commas inside credentials are not supported. Each configured "
            "proxy is assigned to browser instances deterministically by "
            "instance index (cycling when the pool is larger than the proxy "
            "list); a single proxy applies to all instances."
        ),
    )

    # MCP configuration
    mcp_enabled: bool = Field(default=False, description="Enable the embedded MCP server")
    mcp_path: str = Field(default="/mcp", description="ASGI path for Streamable HTTP MCP")
    mcp_host: Optional[str] = Field(
        default=None,
        description="Externally visible MCP Host header (hostname or IP, optionally with port)",
    )
    mcp_session_timeout: float = Field(
        default=1800.0, ge=1.0, description="Idle MCP browser session timeout in seconds"
    )
    mcp_state_dir: Path = Field(
        default=Path(".camoufox-connector/mcp-state"),
        description="Directory for opaque MCP browser-state backups",
    )

    # Debug settings
    debug: bool = Field(
        default=False,
        description="Enable debug logging",
    )

    @field_validator("proxy")
    @classmethod
    def validate_proxy(cls, v: Optional[str]) -> Optional[str]:
        """Validate one or more comma-separated proxy URLs."""
        if v is None or v == "":
            return None
        # Raises ValueError with a descriptive message for invalid entries.
        parse_proxy_list(v)
        return v

    @field_validator("public_ws_url")
    @classmethod
    def validate_public_ws_url(cls, v: Optional[str]) -> Optional[str]:
        """Validate public WebSocket URL format."""
        if v is None or v == "":
            return None
        if not v.startswith(("ws://", "wss://")):
            raise ValueError("Public WebSocket URL must start with ws:// or wss://")
        return v.rstrip("/")

    @model_validator(mode='after')
    def validate_geoip_requires_proxy(self) -> 'Settings':
        """Warn and disable geoip if no proxy is configured."""
        if self.geoip and not self.proxy:
            logger.warning(
                "geoip=True requires a proxy to be configured. "
                "Automatically disabling geoip. Set a proxy or use --no-geoip to silence this warning."
            )
            self.geoip = False
        return self

    def get_public_ws_base_url(self) -> str:
        """Get the public WebSocket base URL for proxied endpoints."""
        return self.public_ws_url or f"ws://localhost:{self.api_port}"

    @classmethod
    def from_json(cls, path: str | Path) -> Settings:
        """Load settings from a JSON configuration file."""
        with open(path) as f:
            data = json.load(f)
        return cls(**data)

    @classmethod
    def from_cli_args(cls, args) -> Settings:
        """Create settings from parsed CLI arguments."""
        # Convert argparse namespace to dict, filtering None values
        data = {k: v for k, v in vars(args).items() if v is not None}

        # Handle config file if specified
        if "config" in data and data["config"]:
            config_path = data.pop("config")
            base_settings = cls.from_json(config_path)
            # Merge CLI args on top of config file
            return base_settings.model_copy(update=data)

        return cls(**data)

    def get_ws_port(self, index: int = 0) -> int:
        """Get WebSocket port for a given browser instance index."""
        return self.ws_port_start + index

    def proxy_mappings(self) -> list:
        """Launch mappings for every configured proxy URL, in order.

        Returns an empty list when no proxy is configured.
        """
        return parse_proxy_list(self.proxy)

    def proxy_mapping(self, index: int = 0) -> Optional[dict]:
        """Get the proxy mapping assigned to the browser instance *index*.

        Proxies are assigned deterministically by instance index, cycling
        when the pool size exceeds the number of configured proxies.  A
        single proxy applies to every instance.

        Returns:
            The launch mapping for the instance, or ``None`` when no proxy is
            configured.
        """
        mappings = self.proxy_mappings()
        if not mappings:
            return None
        return mappings[index % len(mappings)]

    def to_camoufox_kwargs(self, port: Optional[int] = None, index: int = 0) -> dict:
        """Convert settings to kwargs for camoufox launch_server.

        ``proxy`` is passed as the mapping expected by Camoufox 0.5.x
        (``{'server': ..., 'username': ..., 'password': ...}``); the instance
        ``index`` selects which configured proxy applies (cycling).
        """
        kwargs: dict = {
            "headless": self.headless,
            "geoip": self.geoip,
            "humanize": self.humanize,
            "block_images": self.block_images,
        }

        proxy = self.proxy_mapping(index)
        if proxy is not None:
            kwargs["proxy"] = proxy

        if port is not None:
            kwargs["port"] = port

        return kwargs
