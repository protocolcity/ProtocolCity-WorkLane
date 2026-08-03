"""Pure tool handlers for the WL MCP server.

No MCP transport dependency — unit-tested directly. Resolves products via
the registry, signs every write with the connect-time author identity.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from worklane.devqueue.queue import WorkQueue
from worklane.products import (
    default_product_slug,
    discover_products,
    get_product,
    known_prefix_slug,
    prefixed_task_id,
    product_slugs,
    product_tracker,
    resolve_write_task_id,
    split_task_id,
)
from worklane.trackers.protocol import Task, TaskStatus
from worklane.trackers.sqlite import SQLiteTracker

# Terminal safety-net when a tool omits ``project``, no WL_PROJECT/
# WL_DEFAULT_PROJECT env or products.json default is set, and nothing is
# discovered on disk yet. Deliberately the literal "tradeos", not config-
# driven: trackers/sqlite.py's legacy SQLiteTracker always resolves *some*
# tradeos.db store (see its DEFAULT_DB_PATH fallback chain), and
# products.product_tracker() routes slug == "tradeos" to that guaranteed
# store for the same reason — see its docstring for why comparing against
# the *configured* default here instead would risk a silent collision.
_DEFAULT_PROJECT = "tradeos"
_DEFAULT_PRODUCT = _DEFAULT_PROJECT  # back-compat alias for external callers


class ToolError(Exception):
    """Raised for tool-level failures that should surface to the MCP client."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TPHandlers:
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
            raise ToolError(f"unknown product {slug!r}; known: {known}")
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

    # ── tools ────────────────────────────────────────────────────────

    def wl_list(
        self,
        product: Optional[str] = None,
        status: Optional[str] = None,
        label: Optional[str] = None,
        priority: Optional[int] = None,
        gate_type: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """List tickets for a product (or all products when product='all')."""
        slug = self._resolve_product(product)
        limit = max(1, min(int(limit or 50), 500))
        status_f = (status or "").strip() or None
        label_f = (label or "").strip() or None
        prio = int(priority) if priority is not None else None
        gate_f: Optional[str] = gate_type  # None = no filter; '' = ungated; 'deferred'/'human'/'timer'

        if slug == "all":
            items: List[Dict[str, Any]] = []
            for spec in discover_products():
                tr = product_tracker(spec.slug)
                for t in tr.list_tasks(
                    status=status_f, label=label_f, priority=prio,
                    gate_type=gate_f, limit=limit,
                ):
                    items.append(
                        self._task_dict(spec.slug, t, include_description=False)
                    )
            items.sort(key=lambda x: (x.get("priority") or 9, x.get("id") or ""))
            return {"product": "all", "count": len(items[:limit]), "tasks": items[:limit]}

        _, tr = self._tracker(slug)
        tasks = tr.list_tasks(
            status=status_f, label=label_f, priority=prio,
            gate_type=gate_f, limit=limit,
        )
        return {
            "product": slug,
            "count": len(tasks),
            "tasks": [
                self._task_dict(slug, t, include_description=False) for t in tasks
            ],
        }

    def wl_ready(
        self,
        product: Optional[str] = None,
        label: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Backlog tickets whose declared blockers are all done."""
        slug = self._resolve_product(product)
        limit = max(1, min(int(limit or 20), 200))
        labels: Optional[Sequence[str]] = None
        if label:
            labels = [label.strip()]

        if slug == "all":
            ready: List[Dict[str, Any]] = []
            for spec in discover_products():
                tr = product_tracker(spec.slug)
                wq = WorkQueue(tr)
                for t in wq.ready(labels=labels):
                    ready.append(
                        self._task_dict(spec.slug, t, include_description=False)
                    )
            ready.sort(key=lambda x: (x.get("priority") or 9, x.get("id") or ""))
            return {
                "product": "all",
                "count": len(ready[:limit]),
                "tasks": ready[:limit],
            }

        _, tr = self._tracker(slug)
        wq = WorkQueue(tr)
        tasks = wq.ready(labels=labels)[:limit]
        return {
            "product": slug,
            "count": len(tasks),
            "tasks": [
                self._task_dict(slug, t, include_description=False) for t in tasks
            ],
        }

    def wl_show(
        self, task_id: str, product: Optional[str] = None, comments_limit: int = 50
    ) -> Dict[str, Any]:
        """Full ticket detail including recent comments."""
        slug, raw_id, tr, task = self._resolve_task(task_id, product)
        comments_limit = max(1, min(int(comments_limit or 50), 200))
        comments = tr.list_comments(raw_id)
        # newest last in store; return full trail capped from the end
        tail = comments[-comments_limit:]
        out = self._task_dict(slug, task, include_description=True)
        out["comments"] = [
            {
                "id": c.id,
                "author": c.author,
                "body": c.body,
                "created_at": c.created_at,
            }
            for c in tail
        ]
        out["comment_count"] = len(comments)
        return out

    def wl_create(
        self,
        title: str,
        description: str,
        product: Optional[str] = None,
        priority: int = 3,
        labels: Optional[List[str]] = None,
        intake: Optional[str] = None,
    ) -> Dict[str, Any]:
        """File a ticket with signed intake (PROTOCOL.md §5 / wl-26)."""
        title = (title or "").strip()
        description = (description or "").strip()
        if not title:
            raise ToolError("title is required")
        if not description:
            raise ToolError(
                "description is required — state the problem and expected "
                "outcome (PROTOCOL.md §5 intake)"
            )
        prio = int(priority if priority is not None else 3)
        if prio not in (1, 2, 3, 4):
            raise ToolError("priority must be 1 (urgent) … 4 (low)")
        intake_val = str(intake or "").strip() or "mcp"

        from worklane.routing_labels import ensure_create_labels

        slug, tr = self._tracker(product)
        hired: list = []
        try:
            from worklane.api.tasks import _workforce_workers_for_product

            hired = _workforce_workers_for_product(slug)
        except Exception:
            hired = []
        labs, stamped_nr, route_err = ensure_create_labels(
            labels, hired_hands=hired, hard_when_hands=True
        )
        if route_err:
            raise ToolError(route_err)
        task = tr.create_task(
            title=title,
            description=description,
            priority=prio,
            labels=labs,
            actor=self.author,
            intake=intake_val,
        )
        tr.add_comment(
            str(task.id), f"Intake: filed by {self.author}", author=self.author
        )
        if stamped_nr:
            tr.add_comment(
                str(task.id),
                "Routing: no worker:<id> on create (pre-hire) — stamped "
                "needs:routing. After hands exist, create requires worker:* "
                "or worker:you (wl-274 B).",
                author=self.author,
            )
        # re-fetch for updated_at after intake comment
        fresh = tr.get_task(str(task.id)) or task
        out = {"ok": True, "task": self._task_dict(slug, fresh)}
        if stamped_nr:
            out["routing_warning"] = (
                "no worker:* and no hired hands yet — stamped needs:routing. "
                "After hire, pass worker:<persona> or worker:you on create."
            )
            if hired:
                out["hired_hands"] = hired
        return out

    def wl_claim(
        self,
        task_id: str,
        product: Optional[str] = None,
        plan: Optional[str] = None,
        workdir: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomic reserve+promote to in_progress with a signed Owner marker.

        PROTOCOL.md §2 splits reserve (→ in_review) from promote (→ in_progress).
        MCP claim collapses both: single-ticket agents start work immediately.
        Soft-lock-only reserve remains available via the host CLI status path.
        """
        slug, raw_id, tr, task = self._resolve_task(task_id, product, write=True)
        if task.status == TaskStatus.DONE:
            raise ToolError(f"{self._public_id(slug, raw_id)} is already done")
        if task.status == TaskStatus.CANCELED:
            raise ToolError(f"{self._public_id(slug, raw_id)} is canceled")

        # backlog → in_review → in_progress; in_review → in_progress;
        # already in_progress is idempotent re-claim (reposts marker).
        if task.status == TaskStatus.BACKLOG:
            tr.update_status(raw_id, TaskStatus.IN_REVIEW, actor=self.author)
            tr.update_status(raw_id, TaskStatus.IN_PROGRESS, actor=self.author)
        elif task.status == TaskStatus.IN_REVIEW:
            tr.update_status(raw_id, TaskStatus.IN_PROGRESS, actor=self.author)
        elif task.status != TaskStatus.IN_PROGRESS:
            raise ToolError(
                f"cannot claim from status {task.status!r}; "
                f"expected backlog/in_review/in_progress"
            )

        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            f"Owner: {self.author}",
        ]
        if workdir:
            lines.append(f"Workdir: {workdir.strip()}")
        if branch:
            lines.append(f"Branch: {branch.strip()}")
        lines.append(f"Start: {start}")
        lines.append("Plan:")
        plan_text = (plan or "").strip()
        if plan_text:
            for ln in plan_text.splitlines():
                bullet = ln.strip().lstrip("-•").strip()
                if bullet:
                    lines.append(f"- {bullet}")
        else:
            lines.append("- (claimed via MCP)")

        body = "\n".join(lines)
        tr.add_comment(raw_id, body, author=self.author)
        fresh = tr.get_task(raw_id)
        assert fresh is not None
        return {
            "ok": True,
            "task": self._task_dict(slug, fresh),
            "owner_comment": body,
        }

    def wl_comment(
        self, task_id: str, body: str, product: Optional[str] = None
    ) -> Dict[str, Any]:
        """Post a signed comment. Lifecycle auto-transitions still apply."""
        body = (body or "").strip()
        if not body:
            raise ToolError("body is required")
        # Mirror API guard: Completed: must carry Verification: + Links:
        first_line = next((ln.strip() for ln in body.split("\n") if ln.strip()), "")
        if first_line.startswith("Completed"):
            if "Verification:" not in body or "Links:" not in body:
                raise ToolError(
                    "close-out comments must carry literal 'Verification:' and "
                    "'Links:' sections (PROTOCOL.md §5) — prefer wl_close"
                )
        if first_line.startswith("Blocked") and "Next step:" not in body:
            raise ToolError(
                "Blocked comments must include a 'Next step:' line (PROTOCOL.md §5)"
            )

        slug, raw_id, tr, task = self._resolve_task(task_id, product, write=True)
        # wl-347: refuse Completed: on umbrella/epic with uncovered children.
        from worklane.epic_coverage import (  # noqa: PLC0415
            body_is_done_closeout,
            coverage_block_reason,
        )

        if body_is_done_closeout(body):
            db_path = getattr(tr, "_db_path", None)
            spec = get_product(slug)
            prefix = spec.prefix if spec is not None else None
            cov_err = coverage_block_reason(
                task, tr, db_path=db_path, product_prefix=prefix
            )
            if cov_err:
                raise ToolError(cov_err)

        comment = tr.add_comment(raw_id, body, author=self.author)
        fresh = tr.get_task(raw_id)
        return {
            "ok": True,
            "comment": {
                "id": comment.id,
                "task_id": self._public_id(slug, raw_id),
                "author": comment.author,
                "body": comment.body,
                "created_at": comment.created_at,
            },
            "task": self._task_dict(slug, fresh) if fresh else None,
        }

    def wl_close(
        self,
        task_id: str,
        completed: str,
        verification: str,
        links: str,
        follow_ups: str = "none",
        product: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Close with structured §5 sections — malformed close-outs impossible."""
        completed = (completed or "").strip()
        verification = (verification or "").strip()
        links = (links or "").strip()
        follow_ups = (follow_ups or "none").strip() or "none"
        if not completed:
            raise ToolError("completed is required (what changed)")
        if not verification:
            raise ToolError("verification is required (commands/tests + result)")
        if not links:
            raise ToolError(
                "links is required — at least one navigable reference "
                "(PR URL, commit SHA, or repo-relative path)"
            )

        slug, raw_id, tr, task = self._resolve_task(task_id, product, write=True)
        if task.status not in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
            raise ToolError(
                f"can only close from in_progress/in_review "
                f"(current: {task.status})"
            )

        # wl-347: umbrella/epic child-coverage before close-out lands.
        from worklane.epic_coverage import coverage_block_reason  # noqa: PLC0415

        db_path = getattr(tr, "_db_path", None)
        spec = get_product(slug)
        prefix = spec.prefix if spec is not None else None
        cov_err = coverage_block_reason(
            task, tr, db_path=db_path, product_prefix=prefix
        )
        if cov_err:
            raise ToolError(cov_err)

        body = (
            f"Completed:\n{completed}\n\n"
            f"Verification:\n{verification}\n\n"
            f"Links:\n{links}\n\n"
            f"Follow-ups:\n{follow_ups}"
        )
        comment = tr.add_comment(raw_id, body, author=self.author)
        fresh = tr.get_task(raw_id)
        return {
            "ok": True,
            "task": self._task_dict(slug, fresh) if fresh else None,
            "comment": {
                "id": comment.id,
                "author": comment.author,
                "body": comment.body,
                "created_at": comment.created_at,
            },
        }

    def wl_release(
        self,
        task_id: str,
        product: Optional[str] = None,
        reason: Optional[str] = None,
        next_step: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Release claim back to backlog.

        With reason + next_step: posts a ``Blocked:`` comment (auto-returns
        to backlog). Without: explicit status move + short release note.
        """
        slug, raw_id, tr, task = self._resolve_task(task_id, product, write=True)
        if task.status not in (
            TaskStatus.IN_PROGRESS,
            TaskStatus.IN_REVIEW,
            TaskStatus.BACKLOG,
        ):
            raise ToolError(
                f"cannot release from status {task.status!r}"
            )

        reason_s = (reason or "").strip()
        next_s = (next_step or "").strip()
        if reason_s:
            if not next_s:
                next_s = "return to pool for another agent"
            body = f"Blocked: {reason_s}\nNext step: {next_s}"
            comment = tr.add_comment(raw_id, body, author=self.author)
        else:
            body = f"Released by {self.author} — returning to backlog"
            comment = tr.add_comment(raw_id, body, author=self.author)
            if task.status != TaskStatus.BACKLOG:
                tr.update_status(raw_id, TaskStatus.BACKLOG, actor=self.author)

        fresh = tr.get_task(raw_id)
        return {
            "ok": True,
            "task": self._task_dict(slug, fresh) if fresh else None,
            "comment": {
                "id": comment.id,
                "author": comment.author,
                "body": comment.body,
                "created_at": comment.created_at,
            },
        }

    def wl_label(
        self,
        task_id: str,
        product: Optional[str] = None,
        add: Optional[List[str]] = None,
        remove: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Add and/or remove labels on an existing ticket (triage / lane routing)."""
        add_list = [str(x).strip() for x in (add or []) if str(x).strip()]
        remove_list = [str(x).strip() for x in (remove or []) if str(x).strip()]
        if not add_list and not remove_list:
            raise ToolError("at least one of 'add' or 'remove' is required")

        slug, raw_id, tr, _task = self._resolve_task(task_id, product, write=True)

        # wl-320: starve guard — label mutation must not bypass wl-315.
        from worklane.routing_labels import (  # noqa: PLC0415
            check_mutation_starve_guard,
        )
        try:
            from worklane.api.tasks import _workforce_workers_for_product
            _hired = _workforce_workers_for_product(slug)
        except Exception:
            _hired = []
        _starve_err = check_mutation_starve_guard(
            list(_task.labels or []),
            add=add_list,
            remove=remove_list,
            hired_hands=_hired,
        )
        if _starve_err:
            raise ToolError(_starve_err)

        updated = tr.update_labels(
            raw_id, add=add_list, remove=remove_list, actor=self.author
        )
        if updated is None:
            raise ToolError(f"task not found: {task_id}")
        return {"ok": True, "task": self._task_dict(slug, updated)}

    def wl_update(
        self,
        task_id: str,
        product: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[int] = None,
        gate_type: Optional[str] = None,
        gate_until: Optional[str] = None,
        gate_note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Edit title, description, priority, and/or gate (wl-21) on a ticket."""
        if (
            title is None
            and description is None
            and priority is None
            and gate_type is None
        ):
            raise ToolError(
                "at least one of title, description, priority, or gate_type is required"
            )
        title_s: Optional[str] = None
        if title is not None:
            title_s = str(title).strip()
            if not title_s:
                raise ToolError("title must be non-empty when provided")
        desc_s: Optional[str] = None
        if description is not None:
            desc_s = str(description).strip()
            if not desc_s:
                raise ToolError("description must be non-empty when provided")
        prio: Optional[int] = None
        if priority is not None:
            prio = int(priority)
            if prio not in (1, 2, 3, 4):
                raise ToolError("priority must be 1 (urgent) … 4 (low)")
        if gate_type is not None and gate_type not in ("", "human", "timer", "deferred"):
            raise ToolError(
                "gate_type must be '' (clear), 'human', 'timer', or 'deferred'"
            )
        if gate_type == "timer" and not gate_until:
            raise ToolError("gate_until is required when gate_type is 'timer'")

        slug, raw_id, tr, _task = self._resolve_task(task_id, product, write=True)
        updated = tr.update_task(
            raw_id,
            title=title_s,
            description=desc_s,
            priority=prio,
            gate_type=gate_type,
            gate_until=gate_until,
            gate_note=gate_note,
            actor=self.author,
        )
        if updated is None:
            raise ToolError(f"task not found: {task_id}")
        return {"ok": True, "task": self._task_dict(slug, updated)}

    def wl_cancel(
        self,
        task_id: str,
        reason: str,
        product: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancel a ticket (* → canceled) with a signed rationale."""
        reason_s = (reason or "").strip()
        if not reason_s:
            raise ToolError("reason is required — cancel needs a short rationale")

        slug, raw_id, tr, task = self._resolve_task(task_id, product, write=True)
        if task.status == TaskStatus.CANCELED:
            raise ToolError(f"{self._public_id(slug, raw_id)} is already canceled")
        if task.status == TaskStatus.DONE:
            raise ToolError(
                f"{self._public_id(slug, raw_id)} is done — use wl_reopen first "
                "if you need to revisit, or leave it closed"
            )

        body = f"Canceled: {reason_s}"
        comment = tr.add_comment(raw_id, body, author=self.author)
        tr.update_status(raw_id, TaskStatus.CANCELED, actor=self.author)
        fresh = tr.get_task(raw_id)
        return {
            "ok": True,
            "task": self._task_dict(slug, fresh) if fresh else None,
            "comment": {
                "id": comment.id,
                "author": comment.author,
                "body": comment.body,
                "created_at": comment.created_at,
            },
        }

    def wl_reopen(
        self,
        task_id: str,
        product: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reopen a closed ticket (done/canceled → backlog)."""
        slug, raw_id, tr, task = self._resolve_task(task_id, product, write=True)
        if task.status not in (TaskStatus.DONE, TaskStatus.CANCELED):
            raise ToolError(
                f"can only reopen from done/canceled "
                f"(current: {task.status})"
            )

        reason_s = (reason or "").strip()
        if reason_s:
            body = f"Reopened: {reason_s}"
        else:
            body = f"Reopened by {self.author} — returning to backlog"
        comment = tr.add_comment(raw_id, body, author=self.author)
        tr.update_status(raw_id, TaskStatus.BACKLOG, actor=self.author)
        fresh = tr.get_task(raw_id)
        return {
            "ok": True,
            "task": self._task_dict(slug, fresh) if fresh else None,
            "comment": {
                "id": comment.id,
                "author": comment.author,
                "body": comment.body,
                "created_at": comment.created_at,
            },
        }

    def wl_reserve(
        self,
        task_id: str,
        product: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Soft-lock a ticket to in_review without promoting to in_progress.

        PROTOCOL.md §2 step 2/3 — reserve while reading or park siblings in a
        bundle. Promote later with ``wl_claim``.
        """
        slug, raw_id, tr, task = self._resolve_task(task_id, product, write=True)
        if task.status == TaskStatus.IN_REVIEW:
            # Idempotent re-reserve: repost marker, stay in_review.
            pass
        elif task.status == TaskStatus.BACKLOG:
            tr.update_status(raw_id, TaskStatus.IN_REVIEW, actor=self.author)
        elif task.status == TaskStatus.IN_PROGRESS:
            raise ToolError(
                f"{self._public_id(slug, raw_id)} is in_progress — "
                "use wl_park to soft-lock without releasing, or wl_release "
                "to return it to the pool"
            )
        else:
            raise ToolError(
                f"cannot reserve from status {task.status!r}; "
                f"expected backlog/in_review"
            )

        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            f"Owner: {self.author}",
            f"Reserved: {start}",
        ]
        note_s = (note or "").strip()
        if note_s:
            lines.append(f"Note: {note_s}")
        body = "\n".join(lines)
        comment = tr.add_comment(raw_id, body, author=self.author)
        fresh = tr.get_task(raw_id)
        return {
            "ok": True,
            "task": self._task_dict(slug, fresh) if fresh else None,
            "comment": {
                "id": comment.id,
                "author": comment.author,
                "body": comment.body,
                "created_at": comment.created_at,
            },
        }

    def wl_park(
        self,
        task_id: str,
        product: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Park a live ticket (in_progress → in_review) for bundle rotate.

        PROTOCOL.md §2 step 5 — soft-lock the current live ticket so a sibling
        can be promoted via ``wl_claim`` without releasing the parked one
        back to the free pool.
        """
        slug, raw_id, tr, task = self._resolve_task(task_id, product, write=True)
        if task.status == TaskStatus.IN_REVIEW:
            # Already parked — idempotent note only.
            pass
        elif task.status == TaskStatus.IN_PROGRESS:
            tr.update_status(raw_id, TaskStatus.IN_REVIEW, actor=self.author)
        else:
            raise ToolError(
                f"can only park from in_progress/in_review "
                f"(current: {task.status})"
            )

        reason_s = (reason or "").strip()
        if reason_s:
            body = f"Parked: {reason_s}\nOwner: {self.author}"
        else:
            body = (
                f"Parked by {self.author} — soft-lock in_review "
                f"(bundle rotate / pause)"
            )
        comment = tr.add_comment(raw_id, body, author=self.author)
        fresh = tr.get_task(raw_id)
        return {
            "ok": True,
            "task": self._task_dict(slug, fresh) if fresh else None,
            "comment": {
                "id": comment.id,
                "author": comment.author,
                "body": comment.body,
                "created_at": comment.created_at,
            },
        }

    def _latest_owner(self, tr: SQLiteTracker, raw_id: str) -> Optional[str]:
        """Return the owner string from the most recent ``Owner:`` marker."""
        owner: Optional[str] = None
        for c in tr.list_comments(raw_id):
            body = c.body or ""
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("Owner:"):
                    owner = stripped.split(":", 1)[1].strip() or None
                    break
        return owner

    def wl_mine(
        self,
        product: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """List open tickets owned by this agent (latest Owner: marker).

        Scans ``in_progress`` and ``in_review`` only — backlog/done/canceled
        are never "mine" for session resume / ghost-audit.
        """
        slug = self._resolve_product(product)
        limit = max(1, min(int(limit or 50), 200))
        open_statuses = (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW)

        def _scan(product_slug: str, tr: SQLiteTracker) -> List[Dict[str, Any]]:
            found: List[Dict[str, Any]] = []
            for st in open_statuses:
                for t in tr.list_tasks(status=st, limit=500):
                    owner = self._latest_owner(tr, str(t.id))
                    if owner == self.author:
                        d = self._task_dict(
                            product_slug, t, include_description=False
                        )
                        d["owner"] = owner
                        found.append(d)
            return found

        if slug == "all":
            items: List[Dict[str, Any]] = []
            for spec in discover_products():
                items.extend(_scan(spec.slug, product_tracker(spec.slug)))
            items.sort(
                key=lambda x: (
                    0 if x.get("status") == TaskStatus.IN_PROGRESS else 1,
                    x.get("priority") or 9,
                    x.get("id") or "",
                )
            )
            return {
                "product": "all",
                "author": self.author,
                "count": len(items[:limit]),
                "tasks": items[:limit],
            }

        _, tr = self._tracker(slug)
        items = _scan(slug, tr)
        items.sort(
            key=lambda x: (
                0 if x.get("status") == TaskStatus.IN_PROGRESS else 1,
                x.get("priority") or 9,
                x.get("id") or "",
            )
        )
        return {
            "product": slug,
            "author": self.author,
            "count": len(items[:limit]),
            "tasks": items[:limit],
        }

    def wl_counts(self, product: Optional[str] = None) -> Dict[str, Any]:
        """Status histogram for a product (or all products). Counts only."""
        slug = self._resolve_product(product)

        def _hist(tr: SQLiteTracker) -> Dict[str, Any]:
            counts: Dict[str, int] = {s: 0 for s in TaskStatus.ALL}
            total = 0
            # No limit — histogram must see the full store.
            for t in tr.list_tasks():
                st = t.status if t.status in counts else None
                if st is None:
                    continue
                counts[st] = counts.get(st, 0) + 1
                total += 1
            # Drop zero buckets to keep the payload small.
            non_zero = {k: v for k, v in counts.items() if v > 0}
            return {"total": total, "counts": non_zero}

        if slug == "all":
            by_product: Dict[str, Any] = {}
            merged: Dict[str, int] = {}
            grand = 0
            for spec in discover_products():
                h = _hist(product_tracker(spec.slug))
                by_product[spec.slug] = h
                grand += int(h["total"])
                for k, v in h["counts"].items():
                    merged[k] = merged.get(k, 0) + int(v)
            return {
                "product": "all",
                "total": grand,
                "counts": merged,
                "by_product": by_product,
            }

        _, tr = self._tracker(slug)
        h = _hist(tr)
        return {"product": slug, "total": h["total"], "counts": h["counts"]}


def build_tool_definitions() -> List[Dict[str, Any]]:
    """MCP tools/list payload — schemas for work + triage tools."""
    project_prop = {
        "type": "string",
        "description": (
            "Project store slug (e.g. worklane, tradeos) — canonical "
            "name (wl-64). 'product' is a silent back-compat alias for this "
            "same field; passing both with different values is an error. "
            "On write tools with a bare task_id, project= is required (wl-344) — "
            "connect-time default alone is not enough. With a composite id, "
            "omit or pass the matching store. "
            "wl_list/wl_ready/wl_mine/wl_counts also accept 'all'."
        ),
    }
    product_prop = {
        "type": "string",
        "description": (
            "Back-compat alias for 'project' (PROTOCOL.md §5.2 — same field, "
            "same meaning). Prefer 'project' in new integrations."
        ),
    }
    task_id_prop = {
        "type": "string",
        "description": (
            "Ticket id — prefer composite (wl-328, ts-12). Bare numeric ids "
            "on write tools require an explicit project= (or product= alias); "
            "default-store fallback is refused to stop cross-store bleed (wl-344). "
            "Reads may still omit project when the connect-time default is intended."
        ),
    }

    return [
        {
            "name": "wl_list",
            "description": (
                "List WorkLane tickets. Filter by status, label, "
                "priority, or gate class. Returns composite ids."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": project_prop,
                    "product": product_prop,
                    "status": {
                        "type": "string",
                        "enum": list(TaskStatus.ALL),
                        "description": "Filter by status",
                    },
                    "label": {"type": "string", "description": "Filter by label"},
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 4,
                        "description": "1=urgent … 4=low",
                    },
                    "gate_type": {
                        "type": "string",
                        "enum": ["", "human", "timer", "deferred"],
                        "description": (
                            "Filter by gate class: 'deferred' = parked tickets; "
                            "'human' = act-now gates; 'timer' = embargoed; "
                            "'' = ungated (no active gate)"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "wl_ready",
            "description": (
                "List backlog tickets ready for dispatch (declared blockers "
                "all done). Prefer this over raw backlog scan."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": project_prop,
                    "product": product_prop,
                    "label": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 20,
                    },
                },
            },
        },
        {
            "name": "wl_show",
            "description": "Show full ticket detail including comment trail.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "comments_limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_create",
            "description": (
                "File a new ticket with signed intake. Description is "
                "required (problem + expected outcome)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "project": project_prop,
                    "product": product_prop,
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 4,
                        "default": 3,
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Prefer include worker:<hand-id> so scheduled hands "
                            "drain the ticket. Required when hands are hired for "
                            "this product — omit a seat and create is rejected "
                            "with valid seat options. Pre-hire: stamps needs:routing."
                        ),
                    },
                    "intake": {
                        "type": "string",
                        "description": (
                            "Entry channel — how the ticket entered the system. "
                            "Defaults to 'mcp' for MCP callers. "
                            "Values: mcp | cli | api | agent | import | unknown"
                        ),
                    },
                },
                "required": ["title", "description"],
            },
        },
        {
            "name": "wl_claim",
            "description": (
                "Claim a ticket: move to in_progress and post a signed "
                "Owner marker (PROTOCOL.md §2/§5)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "plan": {
                        "type": "string",
                        "description": "Plan bullets (newline-separated)",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Absolute working-copy path for Owner marker",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch name if not working on main",
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_comment",
            "description": (
                "Post a signed comment. For close-outs prefer wl_close; "
                "for blockers include 'Blocked:' + 'Next step:'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "body": {"type": "string"},
                    "project": project_prop,
                    "product": product_prop,
                },
                "required": ["task_id", "body"],
            },
        },
        {
            "name": "wl_close",
            "description": (
                "Close a ticket with structured PROTOCOL.md §5 sections. "
                "Malformed close-outs are rejected by construction."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "completed": {
                        "type": "string",
                        "description": "What changed (files/surfaces)",
                    },
                    "verification": {
                        "type": "string",
                        "description": "Commands/tests run + result",
                    },
                    "links": {
                        "type": "string",
                        "description": "PR URL, commit SHA, or repo-relative path",
                    },
                    "follow_ups": {
                        "type": "string",
                        "description": "Ticket refs or 'none'",
                        "default": "none",
                    },
                    "project": project_prop,
                    "product": product_prop,
                },
                "required": ["task_id", "completed", "verification", "links"],
            },
        },
        {
            "name": "wl_release",
            "description": (
                "Release a claim back to backlog. Optional reason+next_step "
                "posts a Blocked: comment."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "reason": {"type": "string"},
                    "next_step": {"type": "string"},
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_label",
            "description": (
                "Add and/or remove labels on an existing ticket (lane routing, "
                "area tags). At least one of add/remove required."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "add": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels to add",
                    },
                    "remove": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels to remove",
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_update",
            "description": (
                "Edit title, description, priority, and/or gate on an existing "
                "ticket (triage re-scoping). At least one field required. "
                "gate_type controls dispatch: '' clears the gate; 'human' withholds "
                "until manually cleared AND surfaces in For You (act-now); "
                "'timer' withholds until gate_until then auto-thaws; "
                "'deferred' parks the ticket — withholds from ready AND never enters "
                "For You / Map gold (PROCESS §3.9 Deferred class, wl-261). "
                "Use deferred when work is real but not yet actionable; use human "
                "only when founder action is needed now. To thaw a deferred ticket, "
                "call wl_update with gate_type='' (clears the gate)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 4,
                        "description": "1=urgent … 4=low",
                    },
                    "gate_type": {
                        "type": "string",
                        "enum": ["", "human", "timer", "deferred"],
                        "description": (
                            "'' clears the gate; 'human' = act-now (surfaces in For You); "
                            "'timer' = embargoed until gate_until; "
                            "'deferred' = parked (withholds ready, never enters For You)"
                        ),
                    },
                    "gate_until": {
                        "type": "string",
                        "description": "ISO timestamp; required when gate_type is 'timer'",
                    },
                    "gate_note": {
                        "type": "string",
                        "description": (
                            "Optional context for the gate. For human gates: "
                            "describe what decision or action is needed. "
                            "For deferred gates: describe what condition would thaw it."
                        ),
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_cancel",
            "description": (
                "Cancel a ticket (* → canceled) with a signed rationale. "
                "Does not apply to done tickets."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "reason": {
                        "type": "string",
                        "description": "Short rationale (required)",
                    },
                },
                "required": ["task_id", "reason"],
            },
        },
        {
            "name": "wl_reopen",
            "description": (
                "Reopen a closed ticket (done or canceled → backlog). "
                "Optional reason is recorded as a signed comment."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "reason": {
                        "type": "string",
                        "description": "Optional reopen note",
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_reserve",
            "description": (
                "Soft-lock a ticket to in_review without starting work "
                "(PROTOCOL.md §2 reserve / bundle). Promote later with wl_claim."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "note": {
                        "type": "string",
                        "description": "Optional reserve note (why soft-locked)",
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_park",
            "description": (
                "Park a live ticket (in_progress → in_review) for bundle rotate. "
                "Does not return it to the free pool — use wl_release for that."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "reason": {
                        "type": "string",
                        "description": "Optional park reason",
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_mine",
            "description": (
                "List open tickets owned by this agent (latest Owner: marker on "
                "in_progress/in_review). For session resume and ghost-audit."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": project_prop,
                    "product": product_prop,
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "wl_counts",
            "description": (
                "Status histogram for a product (or all). Counts only — cheap "
                "board pulse without listing tickets."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": project_prop,
                    "product": product_prop,
                },
            },
        },
    ]


def dispatch_tool(handlers: TPHandlers, name: str, arguments: Dict[str, Any]) -> Any:
    """Route a tools/call to the matching handler method.

    ``project`` (wl-64) is the canonical name for what every handler still
    takes as ``product`` internally; ``product`` remains a silent back-compat
    alias. Resolved here, once, rather than renaming the parameter on all 16
    handler methods — same field, same store lookup, lower surface area.
    Passing both with different values is rejected rather than silently
    picking one (PROTOCOL.md §5.2 alias-precedence rule, wl-64).
    """
    import inspect

    args = dict(arguments or {})
    if "project" in args:
        project_val = args.pop("project")
        product_val = args.get("product")
        if (
            project_val not in (None, "")
            and product_val not in (None, "")
            and str(project_val).strip().lower() != str(product_val).strip().lower()
        ):
            raise ToolError(
                f"conflicting project/product values: project={project_val!r} "
                f"product={product_val!r} — pass only one"
            )
        if project_val not in (None, ""):
            args["product"] = project_val
    table = {
        "wl_list": handlers.wl_list,
        "wl_ready": handlers.wl_ready,
        "wl_show": handlers.wl_show,
        "wl_create": handlers.wl_create,
        "wl_claim": handlers.wl_claim,
        "wl_comment": handlers.wl_comment,
        "wl_close": handlers.wl_close,
        "wl_release": handlers.wl_release,
        "wl_label": handlers.wl_label,
        "wl_update": handlers.wl_update,
        "wl_cancel": handlers.wl_cancel,
        "wl_reopen": handlers.wl_reopen,
        "wl_reserve": handlers.wl_reserve,
        "wl_park": handlers.wl_park,
        "wl_mine": handlers.wl_mine,
        "wl_counts": handlers.wl_counts,
    }
    fn = table.get(name)
    if fn is None:
        raise ToolError(f"unknown tool: {name}")
    # Drop unknown keys so clients sending extra fields don't TypeError.
    sig = inspect.signature(fn)
    accepted = {k: v for k, v in args.items() if k in sig.parameters}
    return fn(**accepted)
