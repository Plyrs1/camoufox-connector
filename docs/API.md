# Camoufox Connector API Usage

Camoufox Connector exposes Camoufox browser instances through proxied WebSocket endpoints that any Playwright client can connect to. The HTTP API is used to discover available browser proxy endpoints, monitor health, inspect pool statistics, and restart browser instances.

Default API base URL:

```text
http://localhost:8080
```

Default browser WebSocket ports start at `9222`. Client-facing proxy WebSocket URLs default to `ws://localhost:8080` and can be configured with `CAMOUFOX_PUBLIC_WS_URL`.

## Basic Workflow

1. Start the connector.
2. Request a browser WebSocket endpoint from the HTTP API.
3. Connect to that endpoint with Playwright Firefox.
4. Create pages and automate normally.
5. Close the Playwright browser connection when finished.

Example flow:

```bash
camoufox-connector --mode pool --pool-size 3
```

```bash
curl http://localhost:8080/next
```

```json
{
  "endpoint": "ws://localhost:8080/ws/stable-token"
}
```

Use the returned `endpoint` with Playwright's Firefox remote connection API.

## Starting The Connector

### Install From PyPI

```bash
pip install camoufox-connector
```

### Install From Source

```bash
git clone https://github.com/pim97/camoufox-connector.git
cd camoufox-connector
pip install -e .
```

### Single Mode

Single mode starts one browser instance. Use it when you want session persistence or a fixed fingerprint.

```bash
camoufox-connector --mode single
```

Single mode is the default, so this is equivalent:

```bash
camoufox-connector
```

### Pool Mode

Pool mode starts multiple browser instances and returns endpoints in round-robin order from `GET /next`.

```bash
camoufox-connector --mode pool --pool-size 5
```

Use pool mode when you want fingerprint rotation across browser instances.

### With Proxy

```bash
camoufox-connector --proxy http://user:pass@host:port
```

Supported proxy URL prefixes are:

- `http://`
- `https://`
- `socks5://`

GeoIP spoofing requires a proxy. If `geoip` is enabled but no proxy is configured, the connector disables GeoIP automatically at startup.

### Environment Variables

All environment variables use the `CAMOUFOX_` prefix.

```bash
export CAMOUFOX_MODE=pool
export CAMOUFOX_POOL_SIZE=5
export CAMOUFOX_API_HOST=0.0.0.0
export CAMOUFOX_API_PORT=8080
export CAMOUFOX_WS_PORT_START=9222
export CAMOUFOX_PUBLIC_WS_URL=ws://localhost:8080
export CAMOUFOX_HEADLESS=true
export CAMOUFOX_GEOIP=true
export CAMOUFOX_HUMANIZE=true
export CAMOUFOX_BLOCK_IMAGES=false
export CAMOUFOX_PROXY=http://user:pass@host:port
export CAMOUFOX_DEBUG=false

camoufox-connector
```

### JSON Configuration

```bash
camoufox-connector --config config.json
```

```json
{
  "mode": "pool",
  "pool_size": 5,
  "api_host": "0.0.0.0",
  "api_port": 8080,
  "ws_port_start": 9222,
  "public_ws_url": "ws://localhost:8080",
  "headless": true,
  "geoip": true,
  "humanize": true,
  "block_images": false,
  "proxy": "http://user:pass@host:port",
  "debug": false
}
```

Configuration precedence:

```text
JSON config file < Environment variables < CLI arguments
```

## Client Examples

### Node.js

```javascript
import { firefox } from 'playwright';

const response = await fetch('http://localhost:8080/next');

if (!response.ok) {
  throw new Error(`Connector returned ${response.status}: ${await response.text()}`);
}

const { endpoint } = await response.json();
const browser = await firefox.connect(endpoint);

try {
  const page = await browser.newPage();
  await page.goto('https://example.com');
  console.log(await page.title());
} finally {
  await browser.close();
}
```

### Python

```python
import httpx
from playwright.async_api import async_playwright

async def main():
    async with httpx.AsyncClient() as client:
        response = await client.get("http://localhost:8080/next")
        response.raise_for_status()
        endpoint = response.json()["endpoint"]

    async with async_playwright() as p:
        browser = await p.firefox.connect(endpoint)
        try:
            page = await browser.new_page()
            await page.goto("https://example.com")
            print(await page.title())
        finally:
            await browser.close()
```

### cURL

```bash
curl http://localhost:8080/
curl http://localhost:8080/health
curl http://localhost:8080/next
curl http://localhost:8080/endpoints
curl http://localhost:8080/stats
curl -X POST http://localhost:8080/restart/0
```

## HTTP API Reference

The API accepts and returns JSON. Existing endpoints do not require request bodies.

### GET `/`

Returns server metadata and active browser configuration.

Request body: none.

Query parameters: none.

Success response: `200 OK`

```json
{
  "name": "camoufox-connector",
  "version": "1.0.3",
  "mode": "pool",
  "pool_size": 3,
  "config": {
    "headless": true,
    "geoip": false,
    "humanize": true,
    "block_images": false,
    "proxy": null,
    "public_ws_url": "ws://localhost:8080"
  }
}
```

Response fields:

| Field | Type | Description |
|---|---|---|
| `name` | string | Service name. |
| `version` | string | Package version. |
| `mode` | string | Active mode, either `single` or `pool`. |
| `pool_size` | integer | Number of browser instances currently tracked by the pool. |
| `config.headless` | boolean | Whether browsers run headless. |
| `config.geoip` | boolean | Whether GeoIP spoofing is enabled. |
| `config.humanize` | boolean | Whether humanization is enabled. |
| `config.block_images` | boolean | Whether image loading is blocked. |
| `config.proxy` | string or null | Returns `configured` when a proxy is set, otherwise `null`. The actual proxy URL is not exposed. |
| `config.public_ws_url` | string | Public WebSocket base URL used for proxied browser endpoints. |

Example:

```bash
curl http://localhost:8080/
```

### GET `/health`

Returns health for all browser instances. The endpoint returns `200` when at least one instance is healthy and `503` when no instances are healthy.

Request body: none.

Query parameters: none.

Success response: `200 OK`

```json
{
  "status": "healthy",
  "mode": "pool",
  "instances": [
    {
      "index": 0,
      "healthy": true,
      "endpoint": "ws://localhost:8080/ws/stable-token-a",
      "proxy_endpoint": "ws://localhost:8080/ws/stable-token-a",
      "status": "idle"
    },
    {
      "index": 1,
      "healthy": true,
      "endpoint": "ws://localhost:8080/ws/stable-token-b",
      "proxy_endpoint": "ws://localhost:8080/ws/stable-token-b",
      "status": "busy"
    }
  ]
}
```

Unhealthy response: `503 Service Unavailable`

```json
{
  "status": "unhealthy",
  "mode": "pool",
  "instances": [
    {
      "index": 0,
      "healthy": false,
      "endpoint": null
    }
  ]
}
```

Response fields:

| Field | Type | Description |
|---|---|---|
| `status` | string | `healthy` if at least one browser is healthy, otherwise `unhealthy`. |
| `mode` | string | Active mode, either `single` or `pool`. |
| `instances` | array | Health entries for each browser instance. |
| `instances[].index` | integer | Zero-based browser instance index. |
| `instances[].healthy` | boolean | Whether the instance is currently healthy. |
| `instances[].endpoint` | string or null | Browser WebSocket endpoint, or `null` if unavailable. |

Example:

```bash
curl -i http://localhost:8080/health
```

### GET `/next`

Returns the next healthy proxied browser WebSocket endpoint using round-robin selection. This is the primary endpoint clients should call before connecting with Playwright.

Request body: none.

Query parameters: none.

Success response: `200 OK`

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

Error response: `503 Service Unavailable`

```json
{
  "error": "No healthy browser instances available"
}
```

Response fields:

| Field | Type | Description |
|---|---|---|
| `endpoint` | string | Proxied WebSocket URL to pass to Playwright Firefox's remote connect method. |
| `proxy_endpoint` | string | Same value as `endpoint`; included to make proxy behavior explicit. |
| `browser` | object | Selected browser instance metadata. |
| `browser.index` | integer | Zero-based browser instance index. |
| `browser.status` | string | Browser status: `idle`, `busy`, `error`, or `inactive`. |
| `browser.healthy` | boolean | Whether the selected instance is healthy. |
| `browser.connections` | integer | Current active proxied WebSocket connections. |
| `browser.total_connections` | integer | Lifetime proxied WebSocket connections. |
| `error` | string | Error message returned when no healthy browser instance exists. |

Example:

```bash
curl http://localhost:8080/next
```

Playwright usage:

```javascript
const { endpoint } = await fetch('http://localhost:8080/next').then((res) => res.json());
const browser = await firefox.connect(endpoint);
```

### GET `/endpoints`

Returns all currently healthy proxied browser WebSocket endpoints with browser status metadata.

Request body: none.

Query parameters: none.

Success response: `200 OK`

```json
{
  "endpoints": [
    {
      "index": 0,
      "endpoint": "ws://localhost:8080/ws/stable-token-a",
      "proxy_endpoint": "ws://localhost:8080/ws/stable-token-a",
      "status": "idle",
      "healthy": true,
      "connections": 0,
      "total_connections": 12
    },
    {
      "index": 1,
      "endpoint": "ws://localhost:8080/ws/stable-token-b",
      "proxy_endpoint": "ws://localhost:8080/ws/stable-token-b",
      "status": "busy",
      "healthy": true,
      "connections": 1,
      "total_connections": 8
    }
  ],
  "count": 2
}
```

If no healthy endpoints are available, the endpoint still returns `200 OK` with an empty list:

```json
{
  "endpoints": [],
  "count": 0
}
```

Response fields:

| Field | Type | Description |
|---|---|---|
| `endpoints` | array of objects | Healthy proxied browser WebSocket endpoints. |
| `endpoints[].index` | integer | Zero-based browser instance index. |
| `endpoints[].endpoint` | string | Proxied WebSocket endpoint for the browser instance. |
| `endpoints[].proxy_endpoint` | string | Same value as `endpoint`; included to make proxy behavior explicit. |
| `endpoints[].status` | string | Browser status: `idle`, `busy`, `error`, or `inactive`. |
| `endpoints[].healthy` | boolean | Whether the instance is healthy. |
| `endpoints[].connections` | integer | Current active proxied WebSocket connections. |
| `endpoints[].total_connections` | integer | Lifetime proxied WebSocket connections. |
| `count` | integer | Number of endpoints in the response. |

Example:

```bash
curl http://localhost:8080/endpoints
```

Use this endpoint when you need to pin work to a specific browser instance, for example to preserve a login session.

### GET `/stats`

Returns pool statistics and per-instance details.

Request body: none.

Query parameters: none.

Success response: `200 OK`

```json
{
  "mode": "pool",
  "total_instances": 3,
  "healthy_instances": 3,
  "active_connections": 5,
  "total_connections": 142,
  "instances": [
    {
      "index": 0,
      "port": 9222,
      "ws_endpoint": "ws://127.0.0.1:9222/abc123",
      "proxy_endpoint": "ws://localhost:8080/ws/stable-token-a",
      "status": "busy",
      "uptime": 3600.5,
      "connections": 2,
      "total_connections": 48,
      "is_healthy": true
    },
    {
      "index": 1,
      "port": 9223,
      "ws_endpoint": "ws://127.0.0.1:9223/def456",
      "proxy_endpoint": "ws://localhost:8080/ws/stable-token-b",
      "status": "busy",
      "uptime": 3600.3,
      "connections": 2,
      "total_connections": 47,
      "is_healthy": true
    }
  ]
}
```

Response fields:

| Field | Type | Description |
|---|---|---|
| `mode` | string | Active mode, either `single` or `pool`. |
| `total_instances` | integer | Number of browser instances tracked by the pool. |
| `healthy_instances` | integer | Number of currently healthy browser instances. |
| `active_connections` | integer | Sum of current connection counters across instances. |
| `total_connections` | integer | Sum of lifetime proxied WebSocket connections across instances. |
| `instances` | array | Detailed instance stats. |
| `instances[].index` | integer | Zero-based browser instance index. |
| `instances[].port` | integer | Configured WebSocket port for the instance. |
| `instances[].ws_endpoint` | string or null | Internal browser WebSocket endpoint used by the proxy. |
| `instances[].proxy_endpoint` | string or null | Public proxied WebSocket endpoint returned to clients. |
| `instances[].status` | string | Browser status: `idle`, `busy`, `error`, or `inactive`. |
| `instances[].uptime` | number | Seconds since the instance was started, rounded to two decimals. |
| `instances[].connections` | integer | Current active proxied WebSocket connections for the instance. |
| `instances[].total_connections` | integer | Lifetime proxied WebSocket connections for the instance. |
| `instances[].is_healthy` | boolean | Whether the instance is currently healthy. |

Example:

```bash
curl http://localhost:8080/stats
```

Connection counter note: `GET /next` only selects an instance and does not increment counters. `connections` tracks active proxied WebSocket sessions, and `total_connections` increments when a client connects to a `/ws/{token}` proxy endpoint.

### POST `/restart/{index}`

Restarts one browser instance by zero-based index.

Request body: none.

Path parameters:

| Parameter | Type | Description |
|---|---|---|
| `index` | integer | Zero-based browser instance index to restart. |

Success response: `200 OK`

```json
{
  "status": "restarted",
  "index": 0
}
```

Failure response: `500 Internal Server Error`

```json
{
  "error": "Failed to restart instance 99"
}
```

The failure response is returned when the index is outside the current pool range or the browser fails to restart.

Example:

```bash
curl -X POST http://localhost:8080/restart/0
```

## Endpoint Summary

| Endpoint | Method | Request Body | Success | Error |
|---|---|---|---|---|
| `/` | GET | None | `200` server info | Not expected during normal operation |
| `/health` | GET | None | `200` healthy status | `503` no healthy instances |
| `/next` | GET | None | `200` next endpoint | `503` no healthy instances |
| `/endpoints` | GET | None | `200` endpoint list | Not expected during normal operation |
| `/stats` | GET | None | `200` pool stats | Not expected during normal operation |
| `/restart/{index}` | POST | None | `200` restarted | `500` invalid index or restart failure |

## Docker Notes

Single mode can expose fixed API and WebSocket ports:

```bash
docker run -p 8080:8080 -p 9222:9222 --shm-size=2gb camoufox-connector
```

Pool mode starts multiple browser instances. On Linux, use host networking so dynamically assigned WebSocket ports are reachable from the host:

```bash
docker run --network host \
  -e CAMOUFOX_MODE=pool \
  -e CAMOUFOX_POOL_SIZE=5 \
  --shm-size=4gb \
  camoufox-connector
```

Docker Compose profiles:

```bash
docker compose up
docker compose --profile pool up
docker compose --profile proxy up
```

## Common Issues

| Issue | Cause or Fix |
|---|---|
| `GET /next` returns `503` | No browser instance is healthy yet, startup failed, or all browsers exited. Check connector logs and `GET /health`. |
| `geoip=True` warning | GeoIP requires a proxy. Set `CAMOUFOX_PROXY` or disable GeoIP with `CAMOUFOX_GEOIP=false` or `--no-geoip`. |
| Cannot connect to WebSocket in Docker pool mode | Use Linux host networking or expose the required WebSocket port range. |
| Proxy validation error | Proxy must start with `http://`, `https://`, or `socks5://`. |
| Session is not preserved | Use single mode or reuse the same endpoint from `GET /endpoints` instead of calling `GET /next` for every task. |
