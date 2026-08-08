"""WorkLane public verb aliases (wl-176 / wl-143 / wl-384) — CLI + MCP.

CLI surface: canonical short `wl` + long form `worklane` share one main.
`tk` fully retired 2026-08-03 (wl-327 ruling B / wl-342). `wl` retired as a
silent CLI alias 2026-08-04 (wl-384) — deprecation shim only, exits nonzero.
MCP: `wl_*` aliases of `wl_*` on private checkouts (tool dual-catalog stays).

The WorkLane public export rewrites `wl`/`wl_*` → `wl`/`wl_*` wholesale, so
dual-prefix MCP assertions only apply when the private catalog is present.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover — py39/py310
    tomllib = None  # type: ignore

from worklane.cli import legacy_cli_shim
from worklane.cli import wl as wl_cli
from worklane.mcp.handlers import (
    TPHandlers,
    build_tool_definitions,
    dispatch_tool,
)
from worklane.mcp.server import MCPServer
from worklane.mcp.tool_aliases import (
    canonical_tool_name,
    with_wl_tool_aliases,
)
from worklane.trackers.sqlite import SQLiteTracker

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _make_env(tmp: Path) -> None:
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    os.environ["WORKLANE_RUNTIME_DIR"] = str(tmp)
    os.environ["WORKLANE_DB"] = str(tmp / "data" / "tradeos.db")
    os.environ.pop("TRADEOS_TRACKER_DB", None)
    os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"


def _core_is_internal() -> bool:
    # Concat so export branding cannot rewrite this detector to wl_.
    internal = "t" + "p_"
    return any(
        (t.get("name") or "").startswith(internal)
        for t in build_tool_definitions()
    )


class CliEntrypointAliasTest(unittest.TestCase):
    """console_scripts: live CLI mains; retired names non-silent / absent."""

    def test_pyproject_declares_worklane_and_wl_scripts(self) -> None:
        raw = _PYPROJECT.read_text(encoding="utf-8")
        # Concat so export branding cannot rewrite retired-name checks.
        retired_cli = "t" + "p"
        # Dual-window package name (wl-280); export rewrites bare
        # "worklane" → "worklane", so keep the token split.
        dual_window = "ticketing" + "protocol"
        if tomllib is not None:
            data = tomllib.loads(raw)
            scripts = data["project"]["scripts"]
            # wl-342: tk must stay absent (founder ruling B on wl-327).
            self.assertNotIn(
                "tk",
                scripts,
                msg="tk console_script was retired 2026-08-03 (wl-342)",
            )
            # wl-414: dual-window package-name console aliases retired post-0.1.7.
            self.assertNotIn(
                dual_window,
                scripts,
                msg="dual-window package console_script retired wl-414",
            )
            self.assertNotIn(
                dual_window + "-mcp",
                scripts,
                msg="dual-window package-mcp console_script retired wl-414",
            )
            # Live ticket CLI = paths under .cli. (public: only wl;
            # private: wl + long-form worklane; worklane server is not .cli.).
            cli_entries = {
                k: v for k, v in scripts.items() if ".cli." in v
            }
            self.assertIn("wl", cli_entries)
            self.assertTrue(
                cli_entries["wl"].endswith(":main"),
                msg=f"wl must dispatch a CLI main, got {cli_entries!r}",
            )
            self.assertNotIn(
                "legacy_cli_shim",
                cli_entries["wl"],
                msg="wl must not point at the deprecation shim",
            )
            if "worklane" in cli_entries:
                self.assertEqual(
                    cli_entries["worklane"],
                    cli_entries["wl"],
                    msg="private long-form CLI must share wl's main",
                )
            # wl-384: retired name may ship only as a non-silent shim.
            if retired_cli in scripts:
                self.assertIn(
                    "legacy_cli_shim",
                    scripts[retired_cli],
                    msg="retired CLI must not silently dispatch the real main",
                )
                self.assertNotEqual(
                    scripts[retired_cli],
                    cli_entries["wl"],
                    msg="retired CLI must not be a silent alias of wl",
                )
        else:
            # py39 fallback without tomllib.
            self.assertRegex(raw, r'(?m)^wl\s*=\s*".*\.cli\.\w+:main"')
            self.assertIsNone(
                re.search(r"^tk\s*=", raw, re.M),
                msg="tk console_script must not reappear in pyproject",
            )
            self.assertIsNone(
                re.search(
                    r"^" + re.escape(dual_window) + r"(?:-mcp)?\s*=",
                    raw,
                    re.M,
                ),
                msg="dual-window package console_scripts retired wl-414",
            )
            retired_line = re.search(
                r"(?m)^" + re.escape(retired_cli) + r'\s*=\s*"([^"]+)"',
                raw,
            )
            if retired_line is not None:
                self.assertIn(
                    "legacy_cli_shim",
                    retired_line.group(1),
                    msg="retired CLI must not silently dispatch the real main",
                )

    def test_argparse_prog_is_wl(self) -> None:
        """wl-384: help/errors brand as the taught verb, never the retired one."""
        parser = wl_cli._build_parser()
        self.assertEqual(parser.prog, "wl")
        help_text = parser.format_help()
        self.assertIn("usage: wl", help_text)
        # Concat so export branding cannot rewrite the retired prog token.
        retired_prog = "t" + "p"
        self.assertNotIn("usage: " + retired_prog, help_text)

    def test_tp_shim_exits_nonzero_and_points_at_wl(self) -> None:
        """wl-384: retired CLI binary must not execute ticket commands silently."""
        # Skip on public export if the shim module was not shipped (public
        # pyproject rebuilds scripts without a retired binary).
        if not hasattr(legacy_cli_shim, "main"):
            self.skipTest("legacy shim not present in this checkout")
        retired = "t" + "p"
        buf = io.StringIO()
        with mock.patch("sys.argv", [retired, "list", "--status", "backlog"]):
            with mock.patch("sys.stderr", buf):
                with self.assertRaises(SystemExit) as cm:
                    legacy_cli_shim.main(["list", "--status", "backlog"])
        self.assertEqual(cm.exception.code, 2)
        err = buf.getvalue()
        self.assertIn("retired", err.lower())
        self.assertIn("wl list --status backlog", err)

    def test_main_dispatch_identical_under_alias_prog_names(self) -> None:
        """argv tokens drive routing — prog name does not fork behavior."""
        args_a = wl_cli.parse_cli_args(["list", "--status", "backlog"])
        args_b = wl_cli.parse_cli_args(["list", "--status", "backlog"])
        self.assertEqual(args_a.command, args_b.command)
        self.assertEqual(args_a.status, args_b.status)

    @mock.patch("urllib.request.urlopen")
    def test_list_via_main_works_for_alias_path(self, mock_urlopen) -> None:
        body = json.dumps(
            {
                "ok": True,
                "tasks": [
                    {
                        "id": "wl-1",
                        "title": "x",
                        "status": "backlog",
                        "priority": 2,
                        "labels": [],
                    }
                ],
            }
        ).encode("utf-8")

        def _open(_req, timeout=None):  # noqa: ARG001 — mirror urlopen
            cm = mock.MagicMock()
            cm.__enter__.return_value = io.BytesIO(body)
            cm.__exit__.return_value = False
            return cm

        mock_urlopen.side_effect = _open

        # Canonical short (wl) and long form (worklane) both dispatch.
        for prog in ("wl", "worklane"):
            mock_urlopen.reset_mock()
            mock_urlopen.side_effect = _open
            with mock.patch("sys.argv", [prog, "list", "--status", "backlog"]):
                wl_cli.main()
            sent = mock_urlopen.call_args[0][0]
            self.assertEqual(sent.get_method(), "GET")
            self.assertIn("status=backlog", sent.full_url)


class McpToolAliasUnitTest(unittest.TestCase):
    def test_canonical_wl_to_tp_when_internal(self) -> None:
        internal = "t" + "p_"
        public = "w" + "l_"
        self.assertEqual(
            canonical_tool_name(public + "list", internal_catalog=True),
            internal + "list",
        )
        self.assertEqual(
            canonical_tool_name(public + "close", internal_catalog=True),
            internal + "close",
        )
        self.assertEqual(
            canonical_tool_name(internal + "list", internal_catalog=True),
            internal + "list",
        )
        # Public catalog: wl_* is already the handler key.
        self.assertEqual(
            canonical_tool_name(public + "list", internal_catalog=False),
            public + "list",
        )

    def test_with_wl_aliases_doubles_internal_catalog_only(self) -> None:
        internal = "t" + "p_"
        public = "w" + "l_"
        core = build_tool_definitions()
        expanded = with_wl_tool_aliases(core)
        if not _core_is_internal():
            # Export already public-named — expansion is a no-op.
            self.assertEqual(len(expanded), len(core))
            return
        core_names = {t["name"] for t in core}
        exp_names = {t["name"] for t in expanded}
        self.assertEqual(len(core), 16)
        self.assertEqual(len(expanded), 32)
        self.assertTrue(core_names <= exp_names)
        for name in core_names:
            self.assertTrue(name.startswith(internal))
            alias = public + name[len(internal) :]
            self.assertIn(alias, exp_names)
        by = {t["name"]: t for t in expanded}
        for name in core_names:
            alias = public + name[len(internal) :]
            self.assertEqual(by[name]["inputSchema"], by[alias]["inputSchema"])


class McpToolAliasDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_PROJECT",
                "WL_PRODUCT",
            )
        }
        _make_env(self.root)
        SQLiteTracker(
            db_path=self.root / "data" / "worklane.db"
        ).create_task(
            title="seed",
            description="bootstrap worklane.db for discovery",
        )
        self.h = TPHandlers(author="tess", default_product="tradeos")

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_dispatch_via_resolved_wl_name(self) -> None:
        if not _core_is_internal():
            self.skipTest("public catalog — wl_* is already canonical")
        public = "w" + "l_"
        created = dispatch_tool(
            self.h,
            canonical_tool_name(public + "create", internal_catalog=True),
            {
                "title": "alias create",
                "description": "Problem: need alias path. Expected: ticket.",
                "priority": 3,
            },
        )
        self.assertTrue(created["ok"])
        tid = created["task"]["id"]
        listed = dispatch_tool(
            self.h,
            canonical_tool_name(public + "list", internal_catalog=True),
            {"status": "backlog"},
        )
        self.assertIn(tid, {t["id"] for t in listed["tasks"]})


class McpServerAliasSessionTest(unittest.TestCase):
    """End-to-end: tools/list exposes wl_*; tools/call routes them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_PROJECT",
                "WL_PRODUCT",
            )
        }
        _make_env(self.root)
        SQLiteTracker(
            db_path=self.root / "data" / "worklane.db"
        ).create_task(
            title="seed",
            description="bootstrap worklane.db for discovery",
        )
        self.h = TPHandlers(author="tess", default_product="tradeos")

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _session(self, messages):
        stdin = io.StringIO(
            "\n".join(json.dumps(m) for m in messages) + "\n"
        )
        stdout = io.StringIO()
        server = MCPServer(self.h, stdin=stdin, stdout=stdout)
        server.serve_forever()
        lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
        return [json.loads(ln) for ln in lines]

    def test_tools_list_includes_wl_aliases(self) -> None:
        replies = self._session(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )
        tools = replies[1]["result"]["tools"]
        names = {t["name"] for t in tools}
        internal = "t" + "p_"
        public = "w" + "l_"
        if _core_is_internal():
            self.assertEqual(len(tools), 32)
            self.assertIn(internal + "create", names)
            self.assertIn(public + "create", names)
            self.assertIn(public + "ready", names)
            self.assertIn(public + "close", names)
        else:
            self.assertEqual(len(tools), 16)
            self.assertTrue(any(n.startswith(public) for n in names))

    def test_tools_call_wl_create(self) -> None:
        if not _core_is_internal():
            self.skipTest("public catalog — use native wl_create path")
        public = "w" + "l_"
        replies = self._session(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": public + "create",
                        "arguments": {
                            "title": "Via wl alias",
                            "description": (
                                "Problem: public verb path. "
                                "Expected: ticket exists."
                            ),
                            "priority": 3,
                        },
                    },
                },
            ]
        )
        self.assertFalse(replies[1]["result"]["isError"])
        payload = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertIn("id", payload["task"])


if __name__ == "__main__":
    unittest.main()
