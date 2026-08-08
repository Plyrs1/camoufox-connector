"""
Camoufox Connector - WebSocket Bridge for Multi-Language Playwright Access

Connect to Camoufox anti-detect browser from any programming language
via Playwright's remote protocol.
"""

import subprocess
from datetime import datetime, timezone

__version__ = "1.0.3"
__author__ = "Scrappey"


def get_display_version() -> str:
    """Return a user-facing version string.

    Resolves the git tag on the current commit; falls back to ``__version__``
    plus the build date when git metadata is unavailable (e.g. inside a
    Docker image without the ``.git`` directory).
    """
    # 1) exact git tag on current commit
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--exact-match"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if tag:
            return tag
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        pass

    # 2) build date from last commit, or static fallback
    try:
        ts = subprocess.check_output(
            ["git", "log", "-1", "--format=%ci"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        build_date = datetime.fromisoformat(ts).strftime("%Y-%m-%d")
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        build_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return f"{__version__} (built {build_date})"


from .config import Settings
from .pool import BrowserPool, BrowserInstance
from .health import create_health_app
from .server import main

__all__ = [
    "Settings",
    "BrowserPool",
    "BrowserInstance",
    "create_health_app",
    "main",
    "__version__",
    "get_display_version",
]
