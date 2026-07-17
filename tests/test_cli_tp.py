"""Tests for worklane.cli.wl (wl-13) — the host-neutral HTTP CLI.

This CLI only speaks
HTTP, so these tests mock urllib.request.urlopen and assert on the request
that would have gone out (method, URL, JSON body) rather than hitting a
live server.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

from worklane.cli import wl as wl_cli


def _http_error(status: int, error_body: dict):
    import urllib.error

    raw = json.dumps(error_body).encode("utf-8")
    return urllib.error.HTTPError(
        url="http://x", code=status, msg="err", hdrs=None, fp=io.BytesIO(raw)
    )


def _ok_response(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    cm = mock.MagicMock()
    cm.__enter__.return_value = io.BytesIO(body)
    cm.__exit__.return_value = False
    return cm


class RequestBuildingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env_before = {
            k: os.environ.get(k) for k in ("WL_BASE_URL", "WL_AGENT_ID", "WL_PROJECT", "WL_PRODUCT")
        }
        os.environ.pop("WL_BASE_URL", None)
        os.environ.pop("WL_AGENT_ID", None)
        os.environ.pop("WL_PROJECT", None)
        os.environ.pop("WL_PRODUCT", None)

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_base_url(self) -> None:
        self.assertEqual(wl_cli._base_url(), "http://127.0.0.1:8799")

    def test_base_url_override(self) -> None:
        os.environ["WL_BASE_URL"] = "http://example.internal:9000/"
        self.assertEqual(wl_cli._base_url(), "http://example.internal:9000")

    @mock.patch("urllib.request.urlopen")
    def test_list_builds_query_string_and_parses_tasks(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "tasks": [{"id": "wl-1", "title": "x", "status": "backlog",
                                     "priority": 2, "labels": []}]}
        )
        parser = wl_cli._build_parser()
        args = parser.parse_args(["list", "--status", "backlog", "--product", "worklane"])
        wl_cli.cmd_list(args)

        sent_req = mock_urlopen.call_args[0][0]
        self.assertEqual(sent_req.get_method(), "GET")
        self.assertIn("/api/admin/tasks?", sent_req.full_url)
        self.assertIn("status=backlog", sent_req.full_url)
        # wl-196: wire key is now "project=" (back-compat: server also accepts "product=")
        self.assertIn("project=worklane", sent_req.full_url)

    @mock.patch("urllib.request.urlopen")
    def test_show_quotes_task_id(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "task": {"id": "wl-13", "title": "x", "status": "backlog",
                                   "priority": 2, "labels": [], "comments": []}}
        )
        parser = wl_cli._build_parser()
        args = parser.parse_args(["show", "wl-13", "--json"])
        wl_cli.cmd_show(args)
        sent_req = mock_urlopen.call_args[0][0]
        self.assertTrue(sent_req.full_url.endswith("/api/admin/tasks/wl-13"))

    def test_create_requires_title(self) -> None:
        parser = wl_cli._build_parser()
        args = parser.parse_args(
            ["create", "--description", "d", "--product", "p", "--author", "wl-pool"]
        )
        with self.assertRaises(SystemExit) as ctx:
            wl_cli.cmd_create(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_create_requires_description(self) -> None:
        parser = wl_cli._build_parser()
        args = parser.parse_args(
            ["create", "--title", "t", "--product", "p", "--author", "wl-pool"]
        )
        with self.assertRaises(SystemExit) as ctx:
            wl_cli.cmd_create(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_create_requires_signed_author(self) -> None:
        parser = wl_cli._build_parser()
        args = parser.parse_args(
            ["create", "--title", "t", "--description", "d", "--product", "p"]
        )
        with self.assertRaises(SystemExit) as ctx:
            wl_cli.cmd_create(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_create_requires_product(self) -> None:
        parser = wl_cli._build_parser()
        args = parser.parse_args(
            ["create", "--title", "t", "--description", "d", "--author", "wl-pool"]
        )
        with self.assertRaises(SystemExit) as ctx:
            wl_cli.cmd_create(args)
        self.assertEqual(ctx.exception.code, 1)

    @mock.patch("urllib.request.urlopen")
    def test_create_sends_expected_body(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "task": {"id": "wl-99", "title": "New ticket"}}
        )
        parser = wl_cli._build_parser()
        args = parser.parse_args(
            [
                "create",
                "--title", "New ticket",
                "--description", "Problem + expected outcome",
                "--product", "worklane",
                "--priority", "2",
                "--label", "area:install",
                "--label", "size:S",
                "--author", "wl-pool",
            ]
        )
        wl_cli.cmd_create(args)

        sent_req = mock_urlopen.call_args[0][0]
        self.assertEqual(sent_req.get_method(), "POST")
        self.assertTrue(sent_req.full_url.endswith("/api/admin/tasks"))
        sent_body = json.loads(sent_req.data.decode("utf-8"))
        self.assertEqual(
            sent_body,
            {
                "title": "New ticket",
                "description": "Problem + expected outcome",
                "author": "wl-pool",
                "surface": "worklane",
                "priority": 2,
                "labels": ["area:install", "size:S"],
            },
        )

    @mock.patch("urllib.request.urlopen")
    def test_create_falls_back_to_tp_product_env(self, mock_urlopen) -> None:
        os.environ["WL_PRODUCT"] = "worklane"
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "task": {"id": "wl-99", "title": "t"}}
        )
        parser = wl_cli._build_parser()
        args = parser.parse_args(
            ["create", "--title", "t", "--description", "d", "--author", "wl-pool"]
        )
        wl_cli.cmd_create(args)
        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent_body["surface"], "worklane")

    @mock.patch("urllib.request.urlopen")
    def test_create_via_project_flag(self, mock_urlopen) -> None:
        # wl-64: --project is the canonical flag, resolves the same as --product.
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "task": {"id": "wl-99", "title": "t"}}
        )
        parser = wl_cli._build_parser()
        args = parser.parse_args(
            [
                "create",
                "--title", "t",
                "--description", "d",
                "--project", "worklane",
                "--author", "wl-pool",
            ]
        )
        wl_cli.cmd_create(args)
        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent_body["surface"], "worklane")

    def test_create_project_product_conflict_exits(self) -> None:
        parser = wl_cli._build_parser()
        args = parser.parse_args(
            [
                "create",
                "--title", "t",
                "--description", "d",
                "--project", "worklane",
                "--product", "tradeos",
                "--author", "wl-pool",
            ]
        )
        with self.assertRaises(SystemExit) as ctx:
            wl_cli.cmd_create(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_create_project_product_agree_is_fine(self) -> None:
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _ok_response(
                {"ok": True, "task": {"id": "wl-99", "title": "t"}}
            )
            parser = wl_cli._build_parser()
            args = parser.parse_args(
                [
                    "create",
                    "--title", "t",
                    "--description", "d",
                    "--project", "worklane",
                    "--product", "worklane",
                    "--author", "wl-pool",
                ]
            )
            wl_cli.cmd_create(args)  # no SystemExit — equal values are not a conflict

    def test_comment_requires_signed_author(self) -> None:
        parser = wl_cli._build_parser()
        args = parser.parse_args(["comment", "wl-13", "body text"])
        with self.assertRaises(SystemExit) as ctx:
            wl_cli.cmd_comment(args)
        self.assertEqual(ctx.exception.code, 1)

    @mock.patch("urllib.request.urlopen")
    def test_comment_sends_signed_body(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "comment": {"id": "1", "body": "hi", "author": "wl-pool",
                                      "created_at": "now"}}
        )
        parser = wl_cli._build_parser()
        args = parser.parse_args(["comment", "wl-13", "hi", "--author", "wl-pool"])
        wl_cli.cmd_comment(args)

        sent_req = mock_urlopen.call_args[0][0]
        self.assertEqual(sent_req.get_method(), "POST")
        sent_body = json.loads(sent_req.data.decode("utf-8"))
        self.assertEqual(sent_body, {"body": "hi", "author": "wl-pool"})

    @mock.patch("urllib.request.urlopen")
    def test_comment_falls_back_to_tp_agent_id_env(self, mock_urlopen) -> None:
        os.environ["WL_AGENT_ID"] = "grok"
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "comment": {"id": "1", "body": "hi", "author": "grok",
                                      "created_at": "now"}}
        )
        parser = wl_cli._build_parser()
        args = parser.parse_args(["comment", "wl-13", "hi"])
        wl_cli.cmd_comment(args)
        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent_body["author"], "grok")

    # --- wl-97: positional body must survive flag interleave + --body alias ---

    def test_parse_comment_positional_body_before_author(self) -> None:
        args = wl_cli.parse_cli_args(
            ["comment", "wl-13", "hello world", "--author", "wl-pool"]
        )
        self.assertEqual(args.id, "wl-13")
        self.assertEqual(args.body, "hello world")
        self.assertEqual(args.author, "wl-pool")
        self.assertFalse(args.stdin)

    def test_parse_comment_positional_body_after_author(self) -> None:
        """Founder repro: `wl comment id --author x \"body\"` was exit 2."""
        args = wl_cli.parse_cli_args(
            ["comment", "wl-13", "--author", "wl-pool", "hello world"]
        )
        self.assertEqual(args.body, "hello world")
        self.assertEqual(args.author, "wl-pool")

    def test_parse_comment_author_before_id_and_body(self) -> None:
        args = wl_cli.parse_cli_args(
            ["comment", "--author", "wl-pool", "wl-13", "hello world"]
        )
        self.assertEqual(args.id, "wl-13")
        self.assertEqual(args.body, "hello world")
        self.assertEqual(args.author, "wl-pool")

    def test_parse_comment_body_flag_after_author(self) -> None:
        args = wl_cli.parse_cli_args(
            ["comment", "wl-13", "--author", "wl-pool", "--body", "via flag"]
        )
        self.assertEqual(args.body, "via flag")
        self.assertEqual(args.author, "wl-pool")

    def test_parse_comment_body_flag_before_author(self) -> None:
        args = wl_cli.parse_cli_args(
            ["comment", "wl-13", "--body", "via flag", "--author", "wl-pool"]
        )
        self.assertEqual(args.body, "via flag")

    def test_parse_comment_stdin_flag_unchanged(self) -> None:
        args = wl_cli.parse_cli_args(
            ["comment", "wl-13", "--stdin", "--author", "wl-pool"]
        )
        self.assertTrue(args.stdin)
        self.assertEqual(args.body, "")
        self.assertEqual(args.author, "wl-pool")

    def test_parse_comment_rejects_double_body_sources(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            wl_cli.parse_cli_args(
                ["comment", "wl-13", "positional", "--body", "flag", "--author", "x"]
            )
        self.assertEqual(ctx.exception.code, 2)

    @mock.patch("urllib.request.urlopen")
    def test_comment_interleaved_author_posts_body(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "comment": {"id": "9", "body": "close-out text",
                                      "author": "grok", "created_at": "now"}}
        )
        args = wl_cli.parse_cli_args(
            ["comment", "wl-13", "--author", "grok", "close-out text"]
        )
        wl_cli.cmd_comment(args)
        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent_body, {"body": "close-out text", "author": "grok"})

    @mock.patch("urllib.request.urlopen")
    def test_comment_body_flag_posts_body(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "comment": {"id": "10", "body": "flag body",
                                      "author": "grok", "created_at": "now"}}
        )
        args = wl_cli.parse_cli_args(
            ["comment", "wl-13", "--author", "grok", "--body", "flag body"]
        )
        wl_cli.cmd_comment(args)
        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent_body, {"body": "flag body", "author": "grok"})

    @mock.patch("urllib.request.urlopen")
    def test_comment_stdin_unchanged_end_to_end(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "comment": {"id": "11", "body": "from stdin\n",
                                      "author": "grok", "created_at": "now"}}
        )
        args = wl_cli.parse_cli_args(
            ["comment", "wl-13", "--stdin", "--author", "grok"]
        )
        with mock.patch("sys.stdin", io.StringIO("from stdin\n")):
            wl_cli.cmd_comment(args)
        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent_body, {"body": "from stdin\n", "author": "grok"})

    @mock.patch("urllib.request.urlopen")
    def test_status_sends_patch(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "task": {"id": "wl-13", "status": "in_review"}}
        )
        parser = wl_cli._build_parser()
        args = parser.parse_args(["status", "wl-13", "in_review"])
        wl_cli.cmd_status(args)
        sent_req = mock_urlopen.call_args[0][0]
        self.assertEqual(sent_req.get_method(), "PATCH")
        self.assertEqual(json.loads(sent_req.data.decode("utf-8")), {"status": "in_review"})

    def test_label_requires_add_or_remove(self) -> None:
        parser = wl_cli._build_parser()
        args = parser.parse_args(["label", "wl-13"])
        with self.assertRaises(SystemExit) as ctx:
            wl_cli.cmd_label(args)
        self.assertEqual(ctx.exception.code, 1)

    @mock.patch("urllib.request.urlopen")
    def test_label_sends_add_and_remove(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "task": {"id": "wl-13", "labels": ["area:install"]}}
        )
        parser = wl_cli._build_parser()
        args = parser.parse_args(["label", "wl-13", "--add", "area:install", "--remove", "area:board"])
        wl_cli.cmd_label(args)
        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent_body, {"add": ["area:install"], "remove": ["area:board"]})

    @mock.patch("urllib.request.urlopen")
    def test_api_error_response_raises_apierror(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = _http_error(404, {"error": "task not found"})
        parser = wl_cli._build_parser()
        args = parser.parse_args(["show", "wl-999"])
        with self.assertRaises(wl_cli.ApiError) as ctx:
            wl_cli.cmd_show(args)
        self.assertIn("task not found", str(ctx.exception))

    @mock.patch("urllib.request.urlopen")
    def test_unreachable_server_raises_apierror(self, mock_urlopen) -> None:
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        parser = wl_cli._build_parser()
        args = parser.parse_args(["show", "wl-1"])
        with self.assertRaises(wl_cli.ApiError):
            wl_cli.cmd_show(args)


class EnvVarProjectAliasTest(unittest.TestCase):
    """wl-196: WL_PROJECT canonical; WL_PRODUCT back-compat for CLI."""

    def setUp(self) -> None:
        self._env_before = {
            k: os.environ.get(k) for k in ("WL_PROJECT", "WL_PRODUCT", "WL_BASE_URL", "WL_AGENT_ID")
        }
        for k in ("WL_PROJECT", "WL_PRODUCT", "WL_BASE_URL", "WL_AGENT_ID"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @mock.patch("urllib.request.urlopen")
    def test_create_falls_back_to_tp_project_env(self, mock_urlopen) -> None:
        os.environ["WL_PROJECT"] = "worklane"
        mock_urlopen.return_value = _ok_response(
            {"ok": True, "task": {"id": "wl-99", "title": "t"}}
        )
        parser = wl_cli._build_parser()
        args = parser.parse_args(
            ["create", "--title", "t", "--description", "d", "--author", "wl-pool"]
        )
        wl_cli.cmd_create(args)
        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent_body["surface"], "worklane")

    @mock.patch("urllib.request.urlopen")
    def test_list_sends_project_wire_key(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _ok_response({"ok": True, "tasks": []})
        parser = wl_cli._build_parser()
        args = parser.parse_args(["list", "--project", "worklane"])
        wl_cli.cmd_list(args)
        sent_req = mock_urlopen.call_args[0][0]
        self.assertIn("project=worklane", sent_req.full_url)

    @mock.patch("urllib.request.urlopen")
    def test_list_tp_project_env_resolves(self, mock_urlopen) -> None:
        os.environ["WL_PROJECT"] = "worklane"
        mock_urlopen.return_value = _ok_response({"ok": True, "tasks": []})
        parser = wl_cli._build_parser()
        args = parser.parse_args(["list"])
        wl_cli.cmd_list(args)
        sent_req = mock_urlopen.call_args[0][0]
        self.assertIn("project=worklane", sent_req.full_url)


if __name__ == "__main__":
    unittest.main()
