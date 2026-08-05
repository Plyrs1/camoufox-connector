"""
Standalone launcher for Camoufox browser instances.

Called as: python -m camoufox_connector.launcher <json_config>

This module replaces the previously generated inline script in pool.py.
It runs in its own process, sets up a process group for the Node.js child,
and forwards signals (SIGTERM/SIGINT) to prevent orphan processes.

# TODO(Windows): Process group setup and port tracking are Linux/Unix-only.
# For Windows support, evaluate:
#   - subprocess.CREATE_NEW_PROCESS_GROUP for process groups
#   - signal.CTRL_BREAK_EVENT for cross-process signaling
#   - netstat -ano or GetExtendedTcpTable for port-to-PID mapping
"""

import base64
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import orjson
from camoufox.pkgman import LOCAL_DATA
from camoufox.server import to_camel_case_dict
from camoufox.utils import launch_options
from playwright._impl._driver import compute_driver_executable


def _setup_process_group() -> None:
    """Run in the Node.js child process before exec().

    Creates a new process group so that signals forwarded from the
    launcher (e.g. SIGTERM on pool shutdown) can reach the entire
    browser tree.
    """
    os.setsid()  # type: ignore[attr-defined]


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m camoufox_connector.launcher <json_config>", file=sys.stderr)
        sys.exit(1)

    raw = sys.argv[1]
    try:
        kwargs = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON config: {exc}", file=sys.stderr)
        sys.exit(2)

    # Build config.  launch_options() is synchronous and may perform I/O
    # (downloading fonts/addons on first run).  We are already in a
    # dedicated child process so blocking here is acceptable.
    config = launch_options(**kwargs)

    # Filter out None values — workaround for camoufox 0.4.11 where
    # proxy=None gets serialised as null and breaks the Node.js server.
    config = {k: v for k, v in config.items() if v is not None}

    # Resolve paths.  camoufox 0.5.x launchServer.js requires the Playwright
    # driver package directory as argv[2] (see camoufox.server.launch_server).
    launch_script = LOCAL_DATA / "launchServer.js"
    _nodejs = compute_driver_executable()[0]
    nodejs = _nodejs[0] if isinstance(_nodejs, tuple) else _nodejs
    driver_package = Path(nodejs).parent / "package"

    data = orjson.dumps(to_camel_case_dict(config))

    # Spawn Node.js in a new process group.
    # preexec_fn runs in the child before exec() and is POSIX-only.
    process = subprocess.Popen(
        [nodejs, str(launch_script), str(driver_package)],
        cwd=driver_package,
        stdin=subprocess.PIPE,
        text=True,
        preexec_fn=_setup_process_group,
    )

    if process.stdin:
        process.stdin.write(base64.b64encode(data).decode())
        process.stdin.close()

    def _forward_signal(signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
        """Forward signal to the Node.js process group, then exit."""
        try:
            os.killpg(os.getpgid(process.pid), signum)  # type: ignore[attr-defined]
        except (ProcessLookupError, PermissionError):
            pass
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _forward_signal)
    signal.signal(signal.SIGINT, _forward_signal)

    try:
        process.wait()
    except KeyboardInterrupt:
        _forward_signal(signal.SIGINT, None)

    exit_code = int(process.returncode) if process.returncode is not None else 1
    if exit_code != 0:
        print(
            f"Camoufox Node server exited unexpectedly (node_exit_code={exit_code})",
            file=sys.stderr,
        )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
