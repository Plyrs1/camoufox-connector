"""Tests for camoufox_connector.launcher."""

import json
import signal
import sys
from unittest.mock import MagicMock, patch

import pytest

from camoufox_connector.launcher import main


class TestLauncherMain:
    """Tests for launcher.main() entry point."""

    def test_missing_argument_exits(self, capsys):
        """Should exit with code 1 when no JSON argument is passed."""
        with patch.object(sys, "argv", ["launcher"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Usage:" in err

    def test_invalid_json_exits(self, capsys):
        """Should exit with code 2 when JSON is malformed."""
        with patch.object(sys, "argv", ["launcher", "not-json{{"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "Invalid JSON" in err

    def test_none_values_filtered_in_config(self):
        """None values must be stripped before the config is sent to Node.js."""
        test_kwargs = {"headless": True, "port": 9222, "proxy": None}
        node_path = "/opt/playwright/driver/node"

        with patch.object(sys, "argv", ["launcher", json.dumps(test_kwargs)]):
            with patch(
                "camoufox_connector.launcher.launch_options", return_value=test_kwargs
            ) as mock_launch:
                with patch(
                    "camoufox_connector.launcher.compute_driver_executable",
                    return_value=(node_path, "cli.js"),
                ):
                    with patch("subprocess.Popen") as mock_popen:
                        mock_proc = MagicMock()
                        mock_proc.pid = 1234
                        mock_proc.returncode = 1
                        mock_proc.stdin = MagicMock()
                        mock_proc.wait = MagicMock(return_value=1)
                        mock_popen.return_value = mock_proc

                        with patch("os.getpgid", return_value=1234):
                            with patch("os.killpg"):
                                with pytest.raises(RuntimeError, match="terminated unexpectedly"):
                                    main()

        # launch_options was called with original kwargs (including None)
        mock_launch.assert_called_once_with(**test_kwargs)

        # Node must receive the Playwright driver package as argv[2]
        # (required by camoufox 0.5.x launchServer.js).
        mock_popen.assert_called_once()
        popen_args = mock_popen.call_args[0][0]
        assert popen_args[0] == node_path
        assert str(popen_args[1]).endswith("launchServer.js")
        assert str(popen_args[2]) == "/opt/playwright/driver/package"
        assert mock_popen.call_args.kwargs.get("cwd") is not None or (
            len(mock_popen.call_args) > 1 and mock_popen.call_args[1].get("cwd") is not None
        )

    def test_signal_handlers_registered(self):
        """SIGTERM and SIGINT handlers should be registered on launch."""
        test_kwargs = {"headless": True}

        with patch.object(sys, "argv", ["launcher", json.dumps(test_kwargs)]):
            with patch("camoufox_connector.launcher.launch_options", return_value={}):
                with patch(
                    "camoufox_connector.launcher.compute_driver_executable",
                    return_value=("/opt/playwright/driver/node", "cli.js"),
                ):
                    with patch("subprocess.Popen") as mock_popen:
                        mock_proc = MagicMock()
                        mock_proc.pid = 5678
                        mock_proc.returncode = 1
                        mock_proc.stdin = MagicMock()
                        mock_proc.wait = MagicMock(return_value=1)
                        mock_popen.return_value = mock_proc

                        with patch("signal.signal") as mock_signal:
                            with patch("os.getpgid", return_value=5678):
                                with patch("os.killpg"):
                                    with pytest.raises(
                                        RuntimeError, match="terminated unexpectedly"
                                    ):
                                        main()

        # signal.signal should have been called for SIGTERM and SIGINT
        calls = [c[0] for c in mock_signal.call_args_list]
        assert any(c[0] == signal.SIGTERM for c in calls)
        assert any(c[0] == signal.SIGINT for c in calls)
