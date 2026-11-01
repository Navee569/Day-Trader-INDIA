"""Validated, local UI settings. Never accepts or stores broker credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .ai_committee import load_config as load_ai_config
from .market_engine import _settings as validate_market_settings
from .paper_lifecycle import load_config as load_paper_config


FILES = {"ai": "ai_committee.json", "paper": "paper_lifecycle.json",
         "market": "market_engine.json", "notifications": "notifications.json"}
DEFAULTS = {"ai": Path("examples/ai_committee_config.json"),
            "paper": Path("examples/paper_lifecycle_config.json")}
NOTIFICATION_DEFAULT = {"browser_events": False, "show_feed_warnings": True}


class SettingsError(ValueError):
    pass


def setting_path(root, kind):
    if kind not in FILES:
        raise SettingsError("unknown settings section")
    return Path(root) / "trader" / "settings" / FILES[kind]


def effective_path(root, kind):
    custom = setting_path(root, kind)
    if custom.is_file():
        return custom
    if kind in DEFAULTS:
        return Path(root) / DEFAULTS[kind]
    return None


def _read(path, max_bytes=64_000):
    if not path or not path.is_file() or path.stat().st_size > max_bytes:
        raise SettingsError("settings file missing or too large")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise SettingsError("settings JSON invalid") from exc
    if not isinstance(result, dict):
        raise SettingsError("settings must be a JSON object")
    return result


def load_settings(root, kind):
    if kind == "ai":
        return load_ai_config(effective_path(root, kind))
    if kind == "paper":
        source = effective_path(root, kind)
        load_paper_config(source)
        return _read(source)
    custom = effective_path(root, kind)
    if kind == "market":
        return validate_market_settings(**_read(custom)) if custom else validate_market_settings()
    if kind == "notifications":
        return _read(custom) if custom else dict(NOTIFICATION_DEFAULT)
    raise SettingsError("unknown settings section")


def _validate(kind, payload, temporary):
    if not isinstance(payload, dict):
        raise SettingsError("settings must be a JSON object")
    if kind == "ai":
        load_ai_config(temporary)
    elif kind == "paper":
        load_paper_config(temporary)
    elif kind == "market":
        if set(payload) != set(validate_market_settings()):
            raise SettingsError("all market settings fields are required")
        validate_market_settings(**payload)
    elif kind == "notifications":
        if set(payload) != set(NOTIFICATION_DEFAULT) or \
                any(type(value) is not bool for value in payload.values()):
            raise SettingsError("invalid notification preferences")
    else:
        raise SettingsError("unknown settings section")


def save_settings(root, kind, payload):
    target = setting_path(root, kind)
    if kind == "paper" and target.parent.parent.joinpath("state", "paper-lifecycle.json").is_file():
        current = _read(target.parent.parent / "state" / "paper-lifecycle.json", 5_000_000)
        if current.get("initial_capital_inr") != payload.get("initial_capital_inr"):
            raise SettingsError("starting capital is locked once the paper ledger exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    try:
        raw = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if len(raw) > 64_000:
            raise SettingsError("settings too large")
        temporary.write_text(raw, encoding="utf-8")
        _validate(kind, payload, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"section": kind, "saved": True, "restart_workers":
            kind in ("ai", "paper", "market"), "settings": payload}
