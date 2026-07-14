# Camoufox Connector - Multi-stage Docker build
# Base image with Python and system dependencies

FROM python:3.11-slim-trixie AS base

# Install system dependencies for browsers
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    xvfb \
    x11vnc novnc \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /uvx /bin/

RUN uv venv /opt/venv
# Use the virtual environment automatically
ENV VIRTUAL_ENV=/opt/venv
# Place entry points in the environment at the front of the path
ENV PATH="/opt/venv/bin:$PATH"
# Set up VNC environment
ENV DISPLAY=:99
ENV VNC_PORT=5900
ENV NOVNC_PORT=6080

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --no-cache-dir -r requirements.txt

# Pre-download camoufox browser binaries to avoid runtime downloads
# This prevents multiple pool instances from downloading simultaneously
RUN --mount=type=cache,target=/root/.cache/uv \
    camoufox fetch

# Install the application
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --no-cache-dir -e .
RUN chmod +x start.sh

# Expose ports
# 8080: HTTP API
# 9222-9230: WebSocket endpoints for browsers
EXPOSE 8080
EXPOSE 9222-9230

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Default environment variables
ENV CAMOUFOX_MODE=single \
    CAMOUFOX_POOL_SIZE=3 \
    CAMOUFOX_API_PORT=8080 \
    CAMOUFOX_API_HOST=0.0.0.0 \
    CAMOUFOX_WS_PORT_START=9222 \
    CAMOUFOX_HEADLESS=true \
    CAMOUFOX_GEOIP=true \
    CAMOUFOX_HUMANIZE=true \
    CAMOUFOX_BLOCK_IMAGES=false

# Run with xvfb for headless support
ENTRYPOINT ["/app/start.sh"]
