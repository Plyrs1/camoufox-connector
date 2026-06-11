# Camoufox Connector — Agent Guide

> **TL;DR:** WebSocket bridge exposing [Camoufox](https://github.com/daijro/camoufox) anti-detect browser to any Playwright client (Node.js, Go, Python, Java, .NET, etc.). Manages a pool of browser instances with round-robin load balancing and health monitoring via an HTTP API.

---

## 1. What This Project Does

Camoufox is a Firefox-based anti-detect browser with a **Python-only** API. This project makes it accessible from **any language** via Playwright's remote protocol:

- Spawns one or more Camoufox browser instances
- Each instance gets a unique fingerprint and a WebSocket endpoint (`ws://host:port/...`)
- Exposes an HTTP API for load balancing, health checks, and management
- Clients connect via Playwright's `firefox.connect(endpoint)` method

### Use Cases
- High-volume web scraping with fingerprint rotation (pool mode)
- Session persistence via a fixed endpoint (single mode)
- Multi-language browser automation from a single Python backend

---

## 2. Architecture Overview

```
Client (Node.js/Go/Python/Java/etc.)
      │  HTTP GET /next
      ▼
  ┌─────────────────────────────────────┐
  │  HTTP API (Starlette/Uvicorn)       │
  │  Port: API_PORT (default 8080)      │
  │  Endpoints: /, /health, /next,      │
  │  /endpoints, /stats, /restart/{n}   │
  └────────────────┬────────────────────┘
                   │ creates & starts
                   ▼
  ┌─────────────────────────────────────┐
  │  BrowserPool (pool.py)              │
  │  - Round-robin load balancer        │
  │  - Manages BrowserInstance objects  │
  │  - Port reclamation & orphan cleanup│
  └────────────────┬────────────────────┘
                   │ python -m camoufox_connector.launcher
                   ▼
  ┌─────────────────────────────────────┐
  │  Launcher (launcher.py)             │
  │  - Subprocess per browser           │
  │  - Process groups + signal forward  │
  │  - Calls camoufox.launch_options()  │
  └────────────────┬────────────────────┘
                   │ stdin: base64-encoded config
                   ▼
  ┌─────────────────────────────────────┐
  │  Node.js + Camoufox browser         │
  │  - launchServer.js from camoufox    │
  │  - Prints ws:// endpoint to stdout  │
  └─────────────────────────────────────┘
```

---

## 3. Directory Structure

```
/home/py/projects/camoufox-connector/
├── src/camoufox_connector/        # Main Python package
│   ├── __init__.py                # Version, exports Settings/BrowserPool/main
│   ├── config.py                  # Pydantic Settings, env var parsing, validation
│   ├── server.py                  # CLI entry point, signal handling, orchestration
│   ├── pool.py                    # BrowserPool, BrowserInstance, port tracking
│   ├── launcher.py                # Subprocess launcher, Node.js process group mgmt
│   └── health.py                  # Starlette HTTP API endpoints
├── tests/                         # Test suite (pytest)
│   ├── test_integration.py        # Full lifecycle integration tests (requires browser)
│   ├── test_launcher.py           # Launcher unit tests
│   └── test_pool.py               # Port tracking & pool unit tests
├── examples/                      # Client examples in 11 languages
│   ├── nodejs/, typescript/       # Full Playwright examples
│   ├── python/, go/, java/        # Full Playwright examples
│   ├── csharp/, kotlin/           # Full Playwright examples
│   ├── ruby/, php/, rust/         # API-only examples (no native Playwright)
│   └── curl/                      # Shell/cURL examples
├── pyproject.toml                 # Package metadata, deps, entry points, tool config
├── requirements.txt               # Runtime dependencies
├── Dockerfile                     # Multi-stage build, pre-downloads camoufox
├── docker-compose.yml             # Three service profiles: single, pool, proxy
├── .env.example                   # Example environment variables (see §6)
├── README.md                      # User-facing comprehensive docs
└── .github/workflows/docker.yml   # CI: build & push Docker image on v* tags
```

---

## 4. Tech Stack

| Category | Technology |
|----------|-----------|
| Language | Python 3.9+ |
| Browser Engine | Camoufox (Firefox-based anti-detect) |
| Browser Automation | Playwright (>=1.40.0) |
| Web Framework | Starlette (>=0.35.0) |
| HTTP Server | Uvicorn (>=0.25.0) |
| Configuration | Pydantic (>=2.0.0) + pydantic-settings |
| JSON Serialization | orjson (used in launcher.py for speed) |
| HTTP Client | httpx (>=0.26.0) |
| Dev Tools | pytest, pytest-asyncio, black, ruff |

---

## 5. Key Components

### 5.1 `config.py` — Configuration

- **`ServerMode`** enum: `SINGLE` (1 browser) or `POOL` (multiple browsers)
- **`Settings`** (Pydantic BaseSettings):
  - All fields configurable via `CAMOUFOX_*` environment variables
  - Supports `.env` file, CLI args, and JSON config file (`--config`)
  - Validates proxy URL format
  - Auto-disables `geoip` if no proxy is configured (with warning)
  - `to_camoufox_kwargs()` converts settings to camoufox launch parameters

### 5.2 `pool.py` — Browser Pool

- **`BrowserInstance`** dataclass: tracks index, port, ws_endpoint, process, connections, health
- **`BrowserPool`**:
  - `start()`: spawns all instances concurrently
  - `stop()`: graceful shutdown with SIGTERM → SIGKILL cascade
  - `get_next_endpoint()`: round-robin across healthy instances (async-safe with lock)
  - `restart_instance(index)`: stop + start a specific instance
  - `health_check()`: checks process liveness
  - Port reclamation: scans `/proc/net/tcp` + `/proc/*/fd/` to find stale PIDs holding ports

> **Important:** Port tracking is **Linux-only**. Windows support would need `netstat` or `GetExtendedTcpTable`.

### 5.3 `launcher.py` — Subprocess Launcher

- Called as: `python -m camoufox_connector.launcher <json_config>`
- Loads config via `camoufox.utils.launch_options()`
- Filters out `None` values (workaround for camoufox 0.4.11 null-proxy bug)
- Spawns Node.js with `preexec_fn=os.setsid()` to create process group
- Forwards `SIGTERM`/`SIGINT` to entire process group via `os.killpg()`

### 5.4 `health.py` — HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Server info (name, version, mode, config) |
| `/health` | GET | Health check. 200 if >=1 healthy instance, 503 otherwise |
| `/next` | GET | Round-robin browser WebSocket endpoint |
| `/endpoints` | GET | List all healthy endpoints |
| `/stats` | GET | Pool stats: connections, uptime, per-instance details |
| `/restart/{n}` | POST | Restart browser instance N |

- Runs on `api_host:api_port` (default `0.0.0.0:8080`)

### 5.5 `server.py` — Main Entry Point

- Parses CLI args via `argparse`
- Builds `Settings` from CLI + env + JSON config
- Creates `BrowserPool`, starts it, prints startup banner
- Runs health server (blocks until shutdown)
- Handles `SIGTERM`/`SIGINT` for graceful shutdown
- CLI command: `camoufox-connector`

---

## 6. Configuration

### 6.1 Environment Variables

All config uses `CAMOUFOX_` prefix. See `.env.example` for a template.

| Variable | Default | Description |
|----------|---------|-------------|
| `CAMOUFOX_MODE` | `single` | `single` or `pool` |
| `CAMOUFOX_POOL_SIZE` | `3` | Browser instances in pool mode (1–20) |
| `CAMOUFOX_API_PORT` | `8080` | HTTP API port (1024–65535) |
| `CAMOUFOX_API_HOST` | `0.0.0.0` | HTTP API bind host |
| `CAMOUFOX_WS_PORT_START` | `9222` | Starting WebSocket port (1024–65500) |
| `CAMOUFOX_HEADLESS` | `true` | Headless mode |
| `CAMOUFOX_GEOIP` | `true` | GeoIP spoofing (requires proxy) |
| `CAMOUFOX_HUMANIZE` | `true` | Humanization features |
| `CAMOUFOX_BLOCK_IMAGES` | `false` | Block images for faster loads |
| `CAMOUFOX_PROXY` | *(none)* | Proxy URL (`http://user:pass@host:port`) |
| `CAMOUFOX_DEBUG` | `false` | Enable debug logging |

### 6.2 CLI Arguments

All env vars have CLI equivalents (`--mode`, `--pool-size`, `--api-port`, etc.).
Boolean flags use `--flag` / `--no-flag` pattern (e.g. `--headless` / `--no-headless`).

```bash
camoufox-connector --mode pool --pool-size 5 --proxy http://user:pass@host:port
```

### 6.3 JSON Config File

```bash
camoufox-connector --config config.json
```

```json
{
  "mode": "pool",
  "pool_size": 5,
  "headless": true,
  "geoip": true,
  "humanize": true,
  "proxy": "http://user:pass@host:port"
}
```

### 6.4 Precedence

JSON config file < Environment variables < CLI arguments

---

## 7. Entry Points

### 7.1 CLI

```bash
# Install
cd /home/py/projects/camoufox-connector
pip install -e .

# Single mode (default)
camoufox-connector

# Pool mode with 5 browsers
camoufox-connector --mode pool --pool-size 5

# With debug logging
camoufox-connector --debug
```

### 7.2 Programmatic

```python
from camoufox_connector import Settings, BrowserPool, ServerMode
from camoufox_connector.server import Server

settings = Settings(mode=ServerMode.POOL, pool_size=5)
server = Server(settings)
await server.start()  # Blocks until shutdown
```

### 7.3 Docker

```bash
# Single mode
docker compose up

# Pool mode (Linux host network for dynamic ports)
docker compose --profile pool up

# Proxy mode
docker compose --profile proxy up
```

---

## 8. Testing

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Unit tests (port tracking, launcher)
pytest tests/test_pool.py tests/test_launcher.py

# Integration tests (requires camoufox binaries, Playwright, Xvfb)
# Auto-skips if browser dependencies are missing
camoufox fetch  # Pre-download browser binaries
pytest tests/test_integration.py

# All tests
pytest
```

Integration tests use port range 59000+ to avoid collisions and have 120s timeouts for first browser startup.

---

## 9. Docker / Deployment

### Dockerfile
- Based on `python:3.11-slim`
- Installs browser system deps (fonts, GTK, NSS, Xvfb, etc.)
- Pre-downloads Camoufox binaries with `camoufox fetch`
- Exposes port 8080 (API) and 9222–9230 (WebSocket)
- Health check hits `/health`
- Entry point: `python -m camoufox_connector.server`

### Docker Compose Profiles
- **`camoufox`** (default): Single browser, ports `8080:8080` + `9222:9222`
- **`camoufox-pool`** (`--profile pool`): Pool mode, `network_mode: host` (Linux only)
- **`camoufox-proxy`** (`--profile proxy`): Pool mode with proxy, ports `8080:8080` + `9222-9230:9222-9230`

> Pool mode requires `network_mode: host` on Linux because Camoufox assigns WebSocket ports dynamically.

### CI/CD
- `.github/workflows/docker.yml`: Builds and pushes to `ghcr.io` on `v*` tags

---

## 10. Common Issues & Gotchas

| Issue | Cause / Fix |
|-------|-------------|
| `InvalidDatabaseError` | GeoIP DB missing. Install with `pip install camoufox[geoip]` or use `--no-geoip` |
| `geoip=True` warning at startup | GeoIP requires a proxy. Set `PROXY` or use `--no-geoip` |
| Port already in use on restart | Pool's port reclamation should handle this automatically (Linux `/proc` scan) |
| Integration tests skipped | Missing `browserServerImpl.js` (Playwright internal) or missing `libgtk-3.so.0` |
| Pool mode in Docker on Windows/Mac | `network_mode: host` only works on Linux. Run natively or use a Linux VM |

---

## 11. Version & Metadata

- **Package:** `camoufox-connector`
- **Version:** `1.0.3` (defined in `src/camoufox_connector/__init__.py` and `pyproject.toml`)
- **Author:** Scrappey
- **License:** MIT
- **Python:** >=3.9

---

## 12. Agent Rules (MUST FOLLOW)

When working on this project, follow these rules:

### 12.1 Keep AGENTS.md Self-Updating

Whenever you add, modify, or refactor code that changes the project's behavior, **update this AGENTS.md file** to reflect the changes. This includes:
- New configuration options or changed defaults
- New API endpoints or changes to existing ones
- New dependencies or version bumps
- Changes to component interaction flows
- New files or renamed modules

### 12.2 Suggest Conventional Commit Messages

When you complete a task, always suggest a simplified commit message that follows [Conventional Commits](https://www.conventionalcommits.org/) rules. Format it inside a code block so the user can easily copy it. **Do not commit yourself.** Let the user review the changes first; they will commit with the message you provide.

Example:
```
feat(pool): add connection limit per browser instance

- Add `max_connections` field to BrowserInstance
- Enforce limit in get_next_endpoint() with automatic restart
- Update tests and documentation

Refs: #42
```

### 12.3 No Speculation — Verify or Ask

If you are in doubt or have uncertainty about anything, **do not speculate** and **do not try to solve it blindly**. Use available tools (context7, Tavily, web search) to figure it out. You can also **ask the user** — asking the user is highly encouraged when clarification is needed.

### 12.4 Prioritize Built-in Tools for File Operations

Always use built-in tools (`read`, `edit`, `glob`, `grep`) for file listing, string search, finding files, and any file operations. If you can't use tools for some reason, ask the user for direction or ask for permission to use shell commands.

### 12.5 Use `.env.example` for Configuration Examples

If you need to attach configuration from the environment for explanation in this file, **do not use an existing `.env` file**. Always use `.env.example`. Keep `.env.example` updated to include all possible configuration options, and **always include the default value** for each option.

---

## 13. External Resources

- [Camoufox GitHub](https://github.com/daijro/camoufox)
- [Camoufox Documentation](https://camoufox.com/)
- [Playwright Documentation](https://playwright.dev/docs/intro)
- [PyPI Package](https://pypi.org/project/camoufox-connector/)
- [GitHub Repository](https://github.com/pim97/camoufox-connector)
