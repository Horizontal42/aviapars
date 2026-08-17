from __future__ import annotations

import pytest

from aviapars.systemd_notify import notify


def test_noop_when_notify_socket_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notify("READY=1")  # must not raise


def test_never_raises_on_unreachable_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/path/that/does/not/exist.sock")
    notify("READY=1")  # startup must survive even if systemd's socket is unreachable
