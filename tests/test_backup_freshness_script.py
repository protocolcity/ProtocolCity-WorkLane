"""Regression tests for the backup freshness monitor shell script."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "check_backup_freshness.sh"


class BackupFreshnessScriptTest(unittest.TestCase):
    def test_existing_alert_is_counted_from_tasks_payload(self) -> None:
        """A stale backup must not create a duplicate open alert."""
        with tempfile.TemporaryDirectory(prefix="wl_backup_freshness_") as raw_tmp:
            tmp = Path(raw_tmp)
            backup_dir = tmp / "backups" / "worklane"
            backup_dir.mkdir(parents=True)
            old_backup = backup_dir / "worklane-old.db"
            old_backup.write_bytes(b"")
            old_mtime = time.time() - (48 * 60 * 60)
            os.utime(old_backup, (old_mtime, old_mtime))

            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            post_marker = tmp / "post-called"
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    args="$*"
                    if [[ "$args" == *"/admin/overview"* ]]; then
                      printf '200'
                    elif [[ "$args" == *"status=backlog"* ]]; then
                      printf '%s' '{"ok":true,"tasks":[{"id":"wl-existing"}],"column_counts":{"backlog":1}}'
                    elif [[ "$args" == *"status=in_review"* || "$args" == *"status=in_progress"* ]]; then
                      printf '%s' '{"ok":true,"tasks":[],"column_counts":{}}'
                    elif [[ "$args" == *"-X POST"* ]]; then
                      : > "$CURL_POST_MARKER"
                      printf '%s' '{"ok":true,"task":{"id":"wl-duplicate"}}'
                    else
                      printf '%s' '{"ok":true,"tasks":[]}'
                    fi
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "WL_BACKUP_DIR": str(tmp / "backups"),
                    "WL_BACKUP_MONITOR_THRESHOLD_H": "26",
                    "WL_BOARD_URL": "http://desk.invalid",
                    "WL_BACKUP_MONITOR_PRODUCT": "worklane",
                    "CURL_POST_MARKER": str(post_marker),
                }
            )
            result = subprocess.run(
                ["bash", str(_SCRIPT)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
            self.assertIn("open alert ticket exists", result.stdout)
            self.assertFalse(post_marker.exists(), msg="monitor filed a duplicate alert")


if __name__ == "__main__":
    unittest.main()
