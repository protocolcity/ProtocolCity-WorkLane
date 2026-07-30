"""Work-order completion notifications via ntfy (wl-302).

Fires one push when a WO transitions to done. Never raises — notification
failure must not affect ticket state. Dry-runs when unconfigured.

Config (git-ignored, ``worklane/local/config/ntfy.json``)::

    { "enabled": true, "topic": "…", "server": "https://ntfy.sh" }

Kill switch: ``WL_NTFY_DISABLE=1`` forces dry-run even when a live config
exists — set by the test suite (``tests/conftest.py``) so pytest can never
page the founder's phone with synthetic events.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import request as urllib_request

_LOG = logging.getLogger("worklane.notify")


def _config_path() -> Path:
    from worklane.products import wl_data_dir

    return wl_data_dir().parent / "config" / "ntfy.json"


def load_ntfy_config() -> Dict[str, Any]:
    """Return ntfy config dict; empty dict when missing or unreadable."""
    path = _config_path()
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except Exception as exc:
        _LOG.warning("notify.config_unreadable path=%s error=%s", path, exc)
        return {}


def _ntfy_url(cfg: Dict[str, Any]) -> Optional[str]:
    topic = str(cfg.get("topic") or "").strip()
    if not topic:
        return None
    server = str(cfg.get("server") or "https://ntfy.sh").rstrip("/")
    return f"{server}/{topic}"


def notify_done(task_id: str, title: str) -> bool:
    """Push a WO-done notification. Returns True on success or dry-run, False on dispatch error."""
    if os.environ.get("WL_NTFY_DISABLE") == "1":
        _LOG.info("notify.dry_run(kill_switch) task=%s", task_id)
        return True

    cfg = load_ntfy_config()
    if not cfg.get("enabled", True):
        _LOG.info("notify.dry_run(disabled) task=%s", task_id)
        return True

    url = _ntfy_url(cfg)
    if url is None:
        _LOG.info("notify.dry_run(no_topic) task=%s", task_id)
        return True

    message = f"{task_id} done · {title}" if title else f"{task_id} done"

    def _ascii(s: str) -> str:
        return s.encode("ascii", "ignore").decode("ascii")

    headers = {
        "Title": _ascii(message[:255]),
        "Priority": "default",
        "Tags": "white_check_mark",
    }

    try:
        req = urllib_request.Request(
            url, data=message.encode("utf-8"), headers=headers, method="POST"
        )
        with urllib_request.urlopen(req, timeout=8) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                _LOG.warning(
                    "notify.dispatch_non_ok status=%s task=%s", resp.status, task_id
                )
            return ok
    except Exception as exc:
        _LOG.warning("notify.dispatch_failed task=%s error=%s", task_id, exc)
        return False
