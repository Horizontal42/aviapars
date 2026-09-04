from __future__ import annotations

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from aviapars.systemd_notify import watchdog_loop

from aviapars.systemd_notify import notify


def test_noop_when_notify_socket_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notify("READY=1")  # must not raise


def test_never_raises_on_unreachable_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/path/that/does/not/exist.sock")
    notify("READY=1")  # startup must survive even if systemd's socket is unreachable


def test_notify_sends_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    with patch("socket.socket") as mock_socket:
        mock_sock_instance = MagicMock()
        mock_socket.return_value.__enter__.return_value = mock_sock_instance

        notify("READY=1")

        mock_socket.assert_called_once()
        mock_sock_instance.connect.assert_called_once_with("/run/systemd/notify")
        mock_sock_instance.sendall.assert_called_once_with(b"READY=1")

def test_notify_handles_abstract_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", "@/org/freedesktop/systemd1/notify")
    with patch("socket.socket") as mock_socket:
        mock_sock_instance = MagicMock()
        mock_socket.return_value.__enter__.return_value = mock_sock_instance

        notify("RELOADING=1")

        mock_sock_instance.connect.assert_called_once_with("\0/org/freedesktop/systemd1/notify")
        mock_sock_instance.sendall.assert_called_once_with(b"RELOADING=1")

def test_notify_handles_attribute_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    with patch("socket.socket", side_effect=AttributeError("module 'socket' has no attribute 'AF_UNIX'")):
        notify("READY=1") # must not raise


@pytest.mark.asyncio
async def test_watchdog_loop_calls_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")

    with patch("socket.socket") as mock_socket:
        mock_sock_instance = MagicMock()
        mock_socket.return_value.__enter__.return_value = mock_sock_instance

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]

            with pytest.raises(asyncio.CancelledError):
                await watchdog_loop(42.0)

            assert mock_sleep.call_count == 2
            mock_sleep.assert_any_call(42.0)

            mock_socket.assert_called_once()
            mock_sock_instance.connect.assert_called_once_with("/run/systemd/notify")
            mock_sock_instance.sendall.assert_called_once_with(b"WATCHDOG=1")
