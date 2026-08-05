# Camoufox Connector

[![PyPI version](https://badge.fury.io/py/camoufox-connector.svg)](https://pypi.org/project/camoufox-connector/)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**WebSocket bridge for multi-language Playwright access to Camoufox anti-detect browser**

Connect to [Camoufox](https://github.com/daijro/camoufox) from **any programming language** that has Playwright bindings - Node.js, Go, Java, .NET, Python, and more.

## Architecture

```mermaid
flowchart TB
    subgraph clients["Client Applications"]
        NodeJS["Node.js<br/>Playwright"]
        Go["Go<br/>Playwright"]
        Java["Java<br/>Playwright"]
        Python["Python<br/>Playwright"]
        Other["Other Languages<br/>Playwright"]
    end
    
    subgraph connector["Camoufox Connector"]
        API["HTTP API<br/>:8080<br/>GET /next"]
        LB["Round-Robin<br/>Load Balancer"]
        
        subgraph pool["Browser Pool"]
            B1["Camoufox 1<br/>WS :9222<br/>Fingerprint A"]
            B2["Camoufox 2<br/>WS :9223<br/>Fingerprint B"]
            B3["Camoufox N<br/>WS :922X<br/>Fingerprint N"]
        end
        note1["Note: WebSocket ports<br/>are dynamically assigned"]
    end
    
    NodeJS -->|"WebSocket"| API
    Go -->|"WebSocket"| API
    Java -->|"WebSocket"| API
    Python -->|"WebSocket"| API
    Other -->|"WebSocket"| API
    
    API -->|"Round-Robin"| LB
    LB -->|"Distribute"| B1
    LB -->|"Distribute"| B2
    LB -->|"Distribute"| B3
    
    style clients fill:#e1f5ff
    style connector fill:#fff4e1
    style pool fill:#e8f5e9
    style API fill:#ffebee
    style LB fill:#f3e5f5
```

**How it works:**
1. **Clients** (Node.js, Go, Python, etc.) connect via Playwright
2. **HTTP API** provides proxied endpoints via `GET /next` (round-robin)
3. **Load Balancer** distributes connections across browser instances
4. **Browser Pool** maintains multiple Camoufox instances with unique fingerprints
5. Each client gets a connector-hosted **WebSocket proxy endpoint**
6. Internal browser WebSocket ports stay behind the connector proxy

---

## Sponsored by [Scrappey](https://scrappey.com/)

**Tired of getting blocked while scraping the web?**

Rotating proxies, Anti-Bot technology and headless browsers to CAPTCHAs. It's never been this easy using our simple-to-use API.

👉 **[Try Scrappey for free](https://scrappey.com/)**

---

## Why Camoufox Connector?

Camoufox is a powerful anti-detect browser based on Firefox, but its Python-only interface limits accessibility. Camoufox Connector solves this by:

- **Exposing proxied WebSocket endpoints** that any Playwright client can connect to
- **Managing browser pools** for high-volume scraping with fingerprint rotation
- **Providing health monitoring** via HTTP API
- **Simplifying deployment** with Docker support

## Features

- **Multi-language support** - Connect from Node.js, Go, Python, Java, .NET, or any Playwright-compatible language
- **Single & Pool modes** - One persistent browser or multiple rotating browsers
- **Round-robin load balancing** - Distribute connections across browser instances
- **Fingerprint rotation** - Each browser instance has a unique fingerprint
- **Health check API** - Monitor browser health and statistics
- **Docker ready** - Production-ready containerization
- **High performance** - Async architecture optimized for concurrent connections

## Quick Start

### Installation

```bash
# Install from PyPI
pip install camoufox-connector
```

Or install from source:

```bash
# Clone the repository
git clone https://github.com/pim97/camoufox-connector.git
cd camoufox-connector

# Install with pip
pip install -e .

# Or install from PyPI (when published)
pip install camoufox-connector
```

### Start the Server

```bash
# Single browser mode (default)
camoufox-connector

# Pool mode with 5 browsers
camoufox-connector --mode pool --pool-size 5

# With proxy
camoufox-connector --proxy http://user:pass@host:port
```

### Language Examples

Full working examples are available for many programming languages:

| Language | Directory | Playwright Support |
|----------|-----------|-------------------|
| **Node.js** | [`examples/nodejs/`](examples/nodejs/) | Full |
| **TypeScript** | [`examples/typescript/`](examples/typescript/) | Full |
| **Python** | [`examples/python/`](examples/python/) | Full |
| **Go** | [`examples/go/`](examples/go/) | Full |
| **Java** | [`examples/java/`](examples/java/) | Full |
| **Kotlin** | [`examples/kotlin/`](examples/kotlin/) | Full |
| **C# (.NET)** | [`examples/csharp/`](examples/csharp/) | Full |
| **Ruby** | [`examples/ruby/`](examples/ruby/) | API only |
| **PHP** | [`examples/php/`](examples/php/) | API only |
| **Rust** | [`examples/rust/`](examples/rust/) | API only |
| **cURL/Shell** | [`examples/curl/`](examples/curl/) | API only |

### Connect from Node.js

```javascript
import { firefox } from 'playwright';

// Get endpoint from the connector API
const response = await fetch('http://localhost:8080/next');
const { endpoint } = await response.json();

// Connect to Camoufox
const browser = await firefox.connect(endpoint);
const page = await browser.newPage();

await page.goto('https://example.com');
console.log(await page.title());

await browser.close();
```

### Connect from Go

```go
package main

import (
    "github.com/playwright-community/playwright-go"
)

func main() {
    pw, _ := playwright.Run()
    defer pw.Stop()
    
    // Get endpoint from connector API
    // endpoint := getEndpointFromAPI()
    endpoint := "ws://localhost:8080/ws/stable-token"
    
    browser, _ := pw.Firefox.Connect(endpoint)
    page, _ := browser.NewPage()
    
    page.Goto("https://example.com")
    title, _ := page.Title()
    println(title)
    
    browser.Close()
}
```

### Connect from Python

```python
import httpx
from playwright.async_api import async_playwright

async def main():
    # Get endpoint from connector API
    async with httpx.AsyncClient() as client:
        response = await client.get("http://localhost:8080/next")
        endpoint = response.json()["endpoint"]
    
    async with async_playwright() as p:
        browser = await p.firefox.connect(endpoint)
        page = await browser.new_page()
        
        await page.goto("https://example.com")
        print(await page.title())
        
        await browser.close()
```

## Operating Modes

### Single Mode (Default)

One browser instance with a consistent fingerprint. Ideal for:
- Maintaining logged-in sessions
- Sequential scraping tasks
- Development and testing

```bash
camoufox-connector --mode single
```

### Pool Mode

Multiple browser instances with different fingerprints, distributed via round-robin. Ideal for:
- High-volume scraping
- Avoiding detection through fingerprint rotation
- Parallel processing

```bash
camoufox-connector --mode pool --pool-size 5
```

> **Note:** Since each browser instance maintains its own fingerprint, use pool mode when you need fingerprint rotation between requests. Use single mode when you need session persistence.

## HTTP API

The connector exposes an HTTP API for health monitoring and browser management. For full usage instructions, request/response schemas, status codes, and client examples, see [`docs/API.md`](docs/API.md).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Server info and version |
| `/health` | GET | Health check (returns 200/503) |
| `/next` | GET | Get next browser endpoint (round-robin) |
| `/endpoints` | GET | List all available endpoints |
| `/stats` | GET | Pool statistics and connection counts |
| `/restart/{n}` | POST | Restart browser instance N |

### Example API Responses

**GET /next**
```json
{
  "endpoint": "ws://localhost:8080/ws/stable-token",
  "proxy_endpoint": "ws://localhost:8080/ws/stable-token",
  "browser": {
    "index": 0,
    "status": "idle",
    "healthy": true,
    "connections": 0,
    "total_connections": 12
  }
}
```

**GET /health**
```json
{
  "status": "healthy",
  "mode": "pool",
  "instances": [
    {"index": 0, "healthy": true, "endpoint": "ws://localhost:8080/ws/token-a", "status": "idle"},
    {"index": 1, "healthy": true, "endpoint": "ws://localhost:8080/ws/token-b", "status": "busy"},
    {"index": 2, "healthy": true, "endpoint": "ws://localhost:8080/ws/token-c", "status": "idle"}
  ]
}
```

**GET /stats**
```json
{
  "mode": "pool",
  "total_instances": 3,
  "healthy_instances": 3,
  "active_connections": 5,
  "total_connections": 142,
  "instances": [
    {"index": 0, "uptime": 3600.5, "connections": 2, "total_connections": 48},
    {"index": 1, "uptime": 3600.3, "connections": 2, "total_connections": 47},
    {"index": 2, "uptime": 3600.1, "connections": 1, "total_connections": 47}
  ]
}
```

## Configuration

### Command Line Options

```
Usage: camoufox-connector [OPTIONS]

Options:
  --mode {single,pool}   Operating mode (default: single)
  --pool-size N          Number of browser instances in pool mode (default: 3)
  --api-port PORT        HTTP API port (default: 8080)
  --api-host HOST        HTTP API host (default: 0.0.0.0)
  --ws-port-start PORT   Starting port for internal browser WebSocket endpoints (default: 9222)
  --public-ws-url URL    Public WebSocket base URL for proxy endpoints
  --headless             Run browsers in headless mode (default)
  --no-headless          Run browsers in headed mode
  --geoip                Enable GeoIP spoofing (default)
  --no-geoip             Disable GeoIP spoofing
  --humanize             Enable humanization (default)
  --no-humanize          Disable humanization
  --block-images         Block image loading
  --proxy URL            Proxy URL (http://user:pass@host:port)
  --config FILE          Load configuration from JSON file
  --debug                Enable debug logging
```

### Environment Variables

All options can be set via `CAMOUFOX_` prefixed environment variables:

```bash
export CAMOUFOX_MODE=pool
export CAMOUFOX_POOL_SIZE=5
export CAMOUFOX_PUBLIC_WS_URL=ws://localhost:8080
export CAMOUFOX_HEADLESS=true
export CAMOUFOX_PROXY=http://user:pass@host:port

camoufox-connector
```

### JSON Configuration

```json
{
  "mode": "pool",
  "pool_size": 5,
  "public_ws_url": "ws://localhost:8080",
  "headless": true,
  "geoip": true,
  "humanize": true,
  "proxy": "http://user:pass@host:port"
}
```

```bash
camoufox-connector --config config.json
```

## MCP Browser Control API

When enabled with `CAMOUFOX_MCP_ENABLED=true`, the connector exposes the MCP Streamable HTTP transport at `http://localhost:8080/mcp` (the MCP transport endpoint is not `/mcp/mcp`). By default only localhost Host headers are accepted. A configured host is added to those local defaults. For a remote client, set `CAMOUFOX_MCP_HOST=10.10.0.11:53000` (or a hostname with its port); this configures the SDK DNS-rebinding allowlist without weakening protection. A host without a port uses the API port; append `:*` only when deliberately allowing any port. Wildcards are not enabled by default. Configure an MCP client with:

```json
{"url":"http://localhost:8080/mcp"}
```

Create an explicit browser session first, pass its returned `session_id` to every subsequent call, then close it:

```text
create_session -> navigate/snapshot/click/fill/evaluate/screenshot/tabs/new_tab/select_tab/close_tab -> close_session
```

Available tools are `create_session`, `close_session`, `navigate`, `snapshot`, `click`, `fill`, `evaluate`, `screenshot`, `tabs`, `new_tab`, `select_tab`, and `close_tab`. Sessions preserve the same browser, context, page, and pool instance. They expire after 1800 seconds of inactivity by default, releasing the lease automatically. In pool mode, leased instances are excluded from new sessions and cannot be restarted until released; pool exhaustion returns an error.

| Variable | Default | Description |
|---|---:|---|
| `CAMOUFOX_MCP_ENABLED` | `false` | Opt-in embedded MCP server |
| `CAMOUFOX_MCP_PATH` | `/mcp` | Streamable HTTP mount path |
| `CAMOUFOX_MCP_HOST` | *(unset; localhost only)* | External MCP Host hostname/IP, optionally with port |
| `CAMOUFOX_MCP_SESSION_TIMEOUT` | `1800` | Idle explicit browser-session timeout in seconds |
| `CAMOUFOX_MCP_STATE_DIR` | `.camoufox-connector/mcp-state` | Server-side persistent state backup directory |

MCP also provides keyboard and mouse input tools plus opaque-ID browser-state backups (`backup_state`, `list_state_backups`, `restore_state`, and `delete_state_backup`). Backups contain cookies and web storage, never session IDs or host paths. In Docker, mount persistent storage at `/var/lib/camoufox-connector/mcp-state`.

## Docker

### Quick Start with Docker

```bash
# Build the image
docker build -t camoufox-connector .

# Run in single mode
docker run -p 8080:8080 -p 9222:9222 \
  --shm-size=2gb \
  camoufox-connector

# Run in pool mode (Linux: use host network for dynamic ports)
docker run --network host \
  -e CAMOUFOX_MODE=pool \
  -e CAMOUFOX_POOL_SIZE=5 \
  --shm-size=4gb \
  camoufox-connector
```

> **Note:** Pool mode uses `--network host` because camoufox assigns WebSocket ports dynamically. On Windows/Mac, run natively or use a Linux VM.
>
> Do **not** mount an empty volume over `/root/.cache/camoufox`. That path already contains browsers downloaded at image build time (`camoufox fetch`). An empty mount hides them and causes `CamoufoxNotInstalled`.

### Docker Compose

```bash
# Single mode
docker compose up

# Pool mode
docker compose --profile pool up
```

### Custom docker-compose.yml

```yaml
services:
  camoufox-single:
    build: .
    ports:
      - "8080:8080"
      - "9222:9222"
    environment:
      - CAMOUFOX_MODE=single
      - CAMOUFOX_HEADLESS=true
    shm_size: 2gb
    restart: unless-stopped

  camoufox-pool:
    build: .
    # Use host network for dynamic WebSocket port access
    network_mode: host
    environment:
      - CAMOUFOX_MODE=pool
      - CAMOUFOX_POOL_SIZE=5
      - CAMOUFOX_HEADLESS=true
    shm_size: 4gb
    restart: unless-stopped
```

> **Note:** Browser binaries are baked into the image via `camoufox fetch` during the Docker build. Avoid mounting a volume over `/root/.cache/camoufox` unless it already contains a valid Camoufox install. Pool mode requires `network_mode: host` on Linux to support dynamically assigned WebSocket ports.


## Use Cases

### High-Volume Web Scraping

```javascript
// Distribute scraping across multiple fingerprints
async function scrapeUrls(urls) {
  const results = await Promise.all(urls.map(async (url) => {
    // Each request gets a different browser/fingerprint
    const { endpoint } = await fetch('http://localhost:8080/next').then(r => r.json());
    const browser = await firefox.connect(endpoint);
    
    try {
      const page = await browser.newPage();
      await page.goto(url);
      return await page.content();
    } finally {
      await browser.close();
    }
  }));
  
  return results;
}
```

### Session Persistence

```javascript
// Use a specific endpoint for session persistence
const { endpoints } = await fetch('http://localhost:8080/endpoints').then(r => r.json());
const sessionEndpoint = endpoints[0].endpoint;  // Always use the same browser

// Login once
let browser = await firefox.connect(sessionEndpoint);
let page = await browser.newPage();
await page.goto('https://example.com/login');
// ... perform login
await browser.close();

// Subsequent requests use the same session
browser = await firefox.connect(sessionEndpoint);
page = await browser.newPage();
await page.goto('https://example.com/dashboard');  // Already logged in
```

### Load Balancing with Health Checks

```javascript
async function getHealthyEndpoint() {
  const health = await fetch('http://localhost:8080/health').then(r => r.json());
  
  if (health.status !== 'healthy') {
    throw new Error('No healthy browsers available');
  }
  
  const { endpoint } = await fetch('http://localhost:8080/next').then(r => r.json());
  return endpoint;
}
```

## Performance Tips

1. **Use pool mode for parallel tasks** - Each browser instance can handle multiple pages concurrently
2. **Set appropriate pool size** - Rule of thumb: 1-2 browsers per CPU core
3. **Enable `--block-images`** - Significantly speeds up page loads for text-based scraping
4. **Use `--headless`** - Reduces memory and CPU usage
5. **Monitor with `/stats`** - Watch connection distribution and adjust pool size accordingly

## Troubleshooting

### Browser fails to start

```bash
# Check if Camoufox is installed with GeoIP support
python -c "from camoufox.sync_api import Camoufox; print('OK')"

# Install with GeoIP support (required for --geoip flag)
pip install camoufox[geoip]
python -m playwright install firefox
```

### GeoIP database error

If you see `InvalidDatabaseError: Error opening database file`, install camoufox with GeoIP support:

```bash
pip install camoufox[geoip]
```

Or disable GeoIP if you don't need it:

```bash
camoufox-connector --no-geoip
```

### Connection refused

```bash
# Check if server is running
curl http://localhost:8080/health

# Check the current public WebSocket proxy endpoint
curl http://localhost:8080/next
```

### Out of memory in Docker

```bash
# Increase shared memory (required for browsers)
docker run --shm-size=2gb camoufox-connector
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT License - see [LICENSE](LICENSE) for details.

## Credits

- [Camoufox](https://github.com/daijro/camoufox) - The anti-detect browser this project wraps
- [Playwright](https://playwright.dev/) - Browser automation framework
- [node-camoufox](https://github.com/DemonMartin/node-camoufox) - Inspiration for this project

## Links

- [PyPI Package](https://pypi.org/project/camoufox-connector/)
- [GitHub Repository](https://github.com/pim97/camoufox-connector)
- [Camoufox Documentation](https://camoufox.com/)
- [Playwright Documentation](https://playwright.dev/docs/intro)
