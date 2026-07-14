#!/bin/bash
set -e

if [[ "${CAMOUFOX_HEADLESS}" != "true" ]]; then
    # Start Xvfb
    echo "Starting Xvfb..."
    Xvfb :99 -screen 0 1920x1080x24 &
    XVFB_PID=$!

    # Wait for Xvfb to be ready
    sleep 2

    # Start x11vnc
    echo "Starting x11vnc..."
    x11vnc -display :99 -localhost -nopw -no6 -xkb -forever -shared -rfbport 5901 &
    X11VNC_PID=$!

    # Wait for x11vnc to be ready
    sleep 2

    # Start noVNC
    echo "Starting noVNC..."
    /usr/share/novnc/utils/novnc_proxy --vnc localhost:5901 --listen ${NOVNC_PORT:-6080} &
    NOVNC_PID=$!

    # Wait for noVNC to be ready
    sleep 2

    echo "VNC services started. Access via http://0.0.0.0:${NOVNC_PORT:-6080}/vnc.html"
fi

# # Start the main application
# echo "Starting enowX AI..."
# exec /root/.local/bin/enowxai "$@"
exec python -m camoufox_connector.server