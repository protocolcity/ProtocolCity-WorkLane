"""wl-302: ntfy notify_done — dry-run paths + mocked dispatch (no network in CI)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worklane.notify import load_ntfy_config, notify_done


class TestNotifyDryRun(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_notify_")
        self._config_dir = Path(self._tmp.name)
        # Tests run under the conftest WL_NTFY_DISABLE=1 fixture; remove it
        # locally so we can test the non-kill-switch paths.
        self._saved = os.environ.pop("WL_NTFY_DISABLE", None)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        if self._saved is not None:
            os.environ["WL_NTFY_DISABLE"] = self._saved
        else:
            os.environ.pop("WL_NTFY_DISABLE", None)

    def _patch_config(self, path: Path):
        return mock.patch("worklane.notify._config_path", return_value=path)

    # ── dry-run paths ─────────────────────────────────────────────────────────

    def test_no_config_file_returns_true(self) -> None:
        missing = self._config_dir / "ntfy.json"
        with self._patch_config(missing):
            with mock.patch("urllib.request.urlopen") as m:
                result = notify_done("wl-1", "some task")
                m.assert_not_called()
        self.assertTrue(result)

    def test_kill_switch_skips_network(self) -> None:
        os.environ["WL_NTFY_DISABLE"] = "1"
        config_path = self._config_dir / "ntfy.json"
        config_path.write_text(json.dumps({"enabled": True, "topic": "live-topic"}))
        with self._patch_config(config_path):
            with mock.patch("urllib.request.urlopen") as m:
                result = notify_done("wl-2", "task")
                m.assert_not_called()
        self.assertTrue(result)

    def test_enabled_false_skips_network(self) -> None:
        config_path = self._config_dir / "ntfy.json"
        config_path.write_text(json.dumps({"enabled": False, "topic": "t"}))
        with self._patch_config(config_path):
            with mock.patch("urllib.request.urlopen") as m:
                result = notify_done("wl-3", "disabled")
                m.assert_not_called()
        self.assertTrue(result)

    def test_missing_topic_skips_network(self) -> None:
        config_path = self._config_dir / "ntfy.json"
        config_path.write_text(json.dumps({"enabled": True}))
        with self._patch_config(config_path):
            with mock.patch("urllib.request.urlopen") as m:
                result = notify_done("wl-4", "no topic")
                m.assert_not_called()
        self.assertTrue(result)

    # ── mocked dispatch ───────────────────────────────────────────────────────

    def _mock_resp(self, status: int = 200):
        resp = mock.MagicMock()
        resp.status = status
        resp.__enter__ = lambda s: s
        resp.__exit__ = mock.MagicMock(return_value=False)
        return resp

    def test_dispatch_sends_correct_url_and_body(self) -> None:
        config_path = self._config_dir / "ntfy.json"
        config_path.write_text(
            json.dumps({"enabled": True, "topic": "my-topic", "server": "https://ntfy.sh"})
        )
        with self._patch_config(config_path):
            with mock.patch("urllib.request.urlopen", return_value=self._mock_resp()) as m:
                result = notify_done("wl-5", "my task title")
        self.assertTrue(result)
        m.assert_called_once()
        req = m.call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.sh/my-topic")
        self.assertIn(b"wl-5 done", req.data)
        self.assertIn(b"my task title", req.data)

    def test_dispatch_empty_title(self) -> None:
        config_path = self._config_dir / "ntfy.json"
        config_path.write_text(json.dumps({"enabled": True, "topic": "t"}))
        with self._patch_config(config_path):
            with mock.patch("urllib.request.urlopen", return_value=self._mock_resp()) as m:
                result = notify_done("wl-6", "")
        self.assertTrue(result)
        req = m.call_args[0][0]
        self.assertEqual(req.data, b"wl-6 done")

    def test_non_200_returns_false(self) -> None:
        config_path = self._config_dir / "ntfy.json"
        config_path.write_text(json.dumps({"enabled": True, "topic": "t"}))
        with self._patch_config(config_path):
            with mock.patch("urllib.request.urlopen", return_value=self._mock_resp(429)):
                result = notify_done("wl-7", "throttled")
        self.assertFalse(result)

    def test_network_error_returns_false(self) -> None:
        config_path = self._config_dir / "ntfy.json"
        config_path.write_text(json.dumps({"enabled": True, "topic": "t"}))
        with self._patch_config(config_path):
            with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
                result = notify_done("wl-8", "fail task")
        self.assertFalse(result)

    # ── load_ntfy_config ──────────────────────────────────────────────────────

    def test_load_config_missing_returns_empty(self) -> None:
        missing = self._config_dir / "ntfy.json"
        with self._patch_config(missing):
            cfg = load_ntfy_config()
        self.assertEqual(cfg, {})

    def test_load_config_malformed_returns_empty(self) -> None:
        config_path = self._config_dir / "ntfy.json"
        config_path.write_text("not json {{{")
        with self._patch_config(config_path):
            cfg = load_ntfy_config()
        self.assertEqual(cfg, {})
