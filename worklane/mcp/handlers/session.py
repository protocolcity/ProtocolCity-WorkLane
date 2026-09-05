"""TPHandlers session: connect-time identity and store resolution."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from worklane.mcp.handlers.errors import ToolError, _DEFAULT_PROJECT
from worklane.mcp.handlers.read import ReadMixin
from worklane.mcp.handlers.write import WriteMixin
from worklane.products import (
    default_product_slug,
    empty_runtime_override_warning,
    emit_empty_runtime_override_warning,
    get_product,
    known_prefix_slug,
    prefixed_task_id,
    product_slugs,
    product_tracker,
    resolve_write_task_id,
    split_task_id,
    unknown_product_message,
)
from worklane.trackers.protocol import Task
from worklane.trackers.sqlite import SQLiteTracker


class TPHandlers(ReadMixin, WriteMixin):
    """Stateful tool surface bound to a single agent identity."""

    def __init__(self, author: str, default_product: Optional[str] = None) -> None:
        author = (author or "").strip()
        if not author:
            raise ValueError(
                "author identity is required at connect time "
                "(--author / WL_AGENT_ID or WL_AGENT_ID) — PROTOCOL.md §3.8"
            )
        self.author = author
        self.default_product = (
            (
                default_product
                or os.environ.get("WL_PROJECT")
                or os.environ.get("WL_PRODUCT")
                or os.environ.get("WL_PROJECT")
                or os.environ.get("WL_PRODUCT")
                or default_product_slug()
                or _DEFAULT_PROJECT
            )
            .strip()
            .lower()
        )
        # wl-374: one-shot empty RUNTIME_DIR override hint for tool clients.
        # Startup log is emitted by mcp.server.main / server.main; this flag
        # lets the first tool result also carry the path when miswired.
        self._empty_override_hint: Optional[str] = empty_runtime_override_warning()
        self._empty_override_hint_sent = False

    def _consume_empty_override_hint(self) -> Optional[str]:
        """Return the one-time empty-override tool hint, then clear it."""
        if self._empty_override_hint_sent or not self._empty_override_hint:
            return None
        self._empty_override_hint_sent = True
        emit_empty_runtime_override_warning()
        return self._empty_override_hint

    # ── resolution helpers ───────────────────────────────────────────

    def _resolve_product(self, product: Optional[str]) -> str:
        slug = (
            product or self.default_product or default_product_slug() or _DEFAULT_PROJECT
        ).strip().lower()
        if not slug:
            slug = default_product_slug() or _DEFAULT_PROJECT
        # "all" is list/ready only; other tools need a concrete store.
        if slug == "all":
            return "all"
        # tradeos is always present (env overrides / lazy create) regardless
        # of the configured default product — see the _DEFAULT_PRODUCT note
        # above. Other products must already be discovered on disk —
        # product_tracker() maps unknown slugs to the tradeos default, which
        # is a footgun.
        if slug != "tradeos" and get_product(slug) is None:
            known = product_slugs() or ["tradeos"]
            raise ToolError(unknown_product_message(slug, known))
        return slug

    def _tracker(self, product: str) -> Tuple[str, SQLiteTracker]:
        slug = self._resolve_product(product)
        if slug == "all":
            raise ToolError("product='all' is only valid for wl_list / wl_ready")
        tr = product_tracker(slug)
        if not isinstance(tr, SQLiteTracker):
            # product_tracker always returns SQLiteTracker today
            raise ToolError(f"no tracker for product {slug!r}")
        return slug, tr

    def _resolve_task(
        self,
        task_id: str,
        product: Optional[str] = None,
        *,
        write: bool = False,
    ) -> Tuple[str, str, SQLiteTracker, Task]:
        """Return (product_slug, raw_id, tracker, task).

        When ``write=True`` (wl-344): bare ids without an explicit
        ``project``/``product`` are rejected — never silent default-store
        writes. Reads keep the legacy default-product fallback.
        """
        tid = (task_id or "").strip()
        if not tid:
            raise ToolError("task_id is required")

        if write:
            try:
                slug_from_id, raw = resolve_write_task_id(tid, product)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            slug, tr = self._tracker(slug_from_id)
            raw_id = raw
        elif known_prefix_slug(tid) is not None:
            # Composite id wins over product param when prefix is known.
            slug_from_id, raw = split_task_id(tid)
            if product:
                want = self._resolve_product(product)
                if want not in ("all", slug_from_id):
                    raise ToolError(
                        f"task_id {tid!r} belongs to product {slug_from_id!r}, "
                        f"not {want!r}"
                    )
            slug, tr = self._tracker(slug_from_id)
            raw_id = raw
        else:
            slug, tr = self._tracker(product)
            raw_id = tid

        task = tr.get_task(raw_id)
        if task is None:
            raise ToolError(f"task not found: {tid}")
        return slug, str(task.id), tr, task

    def _public_id(self, slug: str, raw_id: Any) -> str:
        return prefixed_task_id(slug, raw_id)

    def _task_dict(
        self, slug: str, task: Task, *, include_description: bool = True
    ) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self._public_id(slug, task.id),
            "raw_id": str(task.id),
            "product": slug,
            "title": task.title,
            "status": task.status,
            "priority": task.priority,
            "labels": list(task.labels or []),
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
        if task.ext_id:
            d["ext_id"] = task.ext_id
        if task.gate_type:
            d["gate_type"] = task.gate_type
            d["gate_until"] = task.gate_until
            d["gate_note"] = task.gate_note
        if include_description:
            d["description"] = task.description or ""
        return d
