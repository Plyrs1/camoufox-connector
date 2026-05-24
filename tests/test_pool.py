"""Tests for camoufox_connector.pool port tracking utilities."""

import asyncio
import os
import signal
from unittest.mock import patch

def _fake_file(content: str):
    """Helper returning a file-like object from a string."""
    from io import StringIO
    return StringIO(content)

import pytest

from camoufox_connector.pool import (
    _find_pids_for_inodes,
    _get_listening_inodes_for_port,
    BrowserPool,
)


class TestIsPortFree:
    """Tests for BrowserPool._is_port_free."""

    def test_high_random_port_is_free(self):
        """A high ephemeral port should normally be free."""
        assert BrowserPool._is_port_free(59999) is True

    def test_localhost_binding_only(self):
        """Only check 127.0.0.1 by default."""
        # Port 0 is a sentinel; connect_ex(0) returns an error quickly.
        assert BrowserPool._is_port_free(0) is True


class TestFindPidsBindingPort:
    """Tests for BrowserPool._find_pids_binding_port (pure-Python /proc scan)."""

    @patch(
        "camoufox_connector.pool._get_listening_inodes_for_port",
        return_value={12345, 67890},
    )
    @patch(
        "camoufox_connector.pool._find_pids_for_inodes",
        return_value=[100, 200],
    )
    def test_returns_pids(self, _mock_find, _mock_inodes):
        pids = BrowserPool._find_pids_binding_port(8080)
        assert sorted(pids) == [100, 200]

    @patch(
        "camoufox_connector.pool._get_listening_inodes_for_port",
        return_value=set(),
    )
    def test_empty_when_no_inodes(self, _mock):
        pids = BrowserPool._find_pids_binding_port(8080)
        assert pids == []


class TestKillProcessTree:
    """Tests for BrowserPool._kill_process_tree."""

    @pytest.mark.asyncio
    async def test_sigterm_then_sigkill(self):
        """Should send SIGTERM, wait, then SIGKILL if process still alive."""
        with patch("os.kill") as mock_kill:
            # First call (SIGTERM) succeeds, second call (kill 0 check) succeeds,
            # third call (SIGKILL) succeeds.
            mock_kill.side_effect = [None, None, None]
            await BrowserPool._kill_process_tree(12345, timeout=0.1)

        assert mock_kill.call_count == 3
        assert mock_kill.call_args_list[0][0] == (12345, signal.SIGTERM)
        assert mock_kill.call_args_list[1][0] == (12345, 0)
        assert mock_kill.call_args_list[2][0] == (12345, signal.SIGKILL)

    @pytest.mark.asyncio
    async def test_already_dead_on_sigterm(self):
        """Should stop immediately if ProcessLookupError on SIGTERM."""
        with patch("os.kill", side_effect=ProcessLookupError()):
            # Should not raise
            await BrowserPool._kill_process_tree(12345, timeout=0.1)

    @pytest.mark.asyncio
    async def test_permission_denied(self):
        """Should handle PermissionError gracefully."""
        with patch("os.kill", side_effect=PermissionError()):
            await BrowserPool._kill_process_tree(12345, timeout=0.1)


class TestGetListeningInodes:
    """Tests for _get_listening_inodes_for_port."""

    @patch(
        "builtins.open",
        side_effect=[
            # /proc/net/tcp
            _fake_file(
                "  sl  local_address rem_address   st tx_queue:rx_queue tr:tm->when retrnsmt   uid  timeout inode\n"
                "   0: 0100007F:23F0 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345\n"
                "   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 67890\n"
            ),
            # /proc/net/tcp6 — not present
            FileNotFoundError(),
        ],
    )
    def test_parses_tcp_listening(self, _mock_open):
        # Port 0x23F0 = 9200, 0x1F90 = 8080
        inodes = _get_listening_inodes_for_port(8080)
        assert inodes == {67890}

    @patch("builtins.open", side_effect=FileNotFoundError())
    def test_returns_empty_on_missing_proc(self, _mock):
        inodes = _get_listening_inodes_for_port(8080)
        assert inodes == set()


class TestFindPidsForInodes:
    """Tests for _find_pids_for_inodes."""

    @patch("os.listdir", side_effect=[
        ["1", "2", "self"],           # /proc entries
        ["0", "1", "2", "255"],       # pid 1 fd entries
        ["0", "1"],                   # pid 2 fd entries (no match)
    ])
    @patch("os.readlink", side_effect=[
        "socket:[12345]",
        "pipe:[99999]",
        "socket:[67890]",
        "anon_inode:[eventfd]",
    ])
    def test_finds_matching_pids(self, _mock_readlink, _mock_listdir):
        pids = _find_pids_for_inodes({12345})
        assert pids == [1]

    @patch("os.listdir", side_effect=OSError())
    def test_returns_empty_on_error(self, _mock):
        pids = _find_pids_for_inodes({12345})
        assert pids == []


def _fake_file(content: str):
    """Helper returning a file-like object from a string."""
    from io import StringIO

    return StringIO(content)
