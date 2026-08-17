from __future__ import annotations

from aviapars.telegram.handlers.admin import _parse_cookie_payload


def test_parses_cookie_editor_json_array() -> None:
    payload = '[{"name": "cfidsw-s7-ibe", "value": "abc123", "domain": "ibe.s7.ru"}, {"name": "gsscw-s7-ibe", "value": "def456"}]'
    assert _parse_cookie_payload(payload) == {"cfidsw-s7-ibe": "abc123", "gsscw-s7-ibe": "def456"}


def test_parses_plain_json_object() -> None:
    payload = '{"cfidsw-s7-ibe": "abc123", "gsscw-s7-ibe": "def456"}'
    assert _parse_cookie_payload(payload) == {"cfidsw-s7-ibe": "abc123", "gsscw-s7-ibe": "def456"}


def test_parses_raw_cookie_header_string() -> None:
    payload = "cfidsw-s7-ibe=abc123; gsscw-s7-ibe=def456"
    assert _parse_cookie_payload(payload) == {"cfidsw-s7-ibe": "abc123", "gsscw-s7-ibe": "def456"}


def test_ignores_cookie_editor_entries_missing_name_or_value() -> None:
    payload = '[{"name": "ok", "value": "1"}, {"domain": "no-name-or-value.example"}]'
    assert _parse_cookie_payload(payload) == {"ok": "1"}


def test_garbage_input_returns_empty_dict() -> None:
    assert _parse_cookie_payload("not cookies at all, no equals signs") == {}
