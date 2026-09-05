"""Write-side MCP tools: create through park."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from worklane.mcp.handlers.errors import ToolError
from worklane.products import get_product
from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


class WriteMixin:
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
        # wl-359: claimable worker:<hand> create → WorkForce wake nudge.
        try:
            from worklane.wake_nudge import gate_of, maybe_wake_hand

            pub = self._public_id(slug, str(fresh.id))
            maybe_wake_hand(
                list(fresh.labels or labs),
                status=fresh.status,
                gate_type=gate_of(fresh),
                only_on_seat_change=True,
                previous_hand=None,
                task_id=pub,
            )
        except Exception:
            pass
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
            # wl-396: Links must cite a landing commit SHA (cheap presence).
            from worklane.closeout_links import (  # noqa: PLC0415
                closeout_links_violation,
            )

            sha_err = closeout_links_violation(body)
            if sha_err:
                raise ToolError(sha_err)
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
            # wl-339: registered close-out checks must be cited in Verification.
            from worklane.closeout_checks import (  # noqa: PLC0415
                closeout_checks_violation,
            )

            chk_err = closeout_checks_violation(
                body, product=slug, labels=getattr(task, "labels", None)
            )
            if chk_err:
                raise ToolError(chk_err)

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
        # wl-396: reject path-only Links (landing SHA required).
        from worklane.closeout_links import links_missing_landing_sha  # noqa: PLC0415

        sha_err = links_missing_landing_sha(links)
        if sha_err:
            raise ToolError(sha_err)

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

        # wl-339: when product registers checks, Verification must cite them.
        from worklane.closeout_checks import (  # noqa: PLC0415
            verification_checks_violation,
        )

        chk_err = verification_checks_violation(
            verification,
            product=slug,
            labels=getattr(task, "labels", None),
        )
        if chk_err:
            raise ToolError(chk_err)

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
        # wl-359: release → ready on a seated hand → wake.
        if fresh is not None:
            try:
                from worklane.wake_nudge import gate_of, maybe_wake_hand

                maybe_wake_hand(
                    list(fresh.labels or []),
                    status=fresh.status,
                    gate_type=gate_of(fresh),
                    only_on_seat_change=False,
                    task_id=self._public_id(slug, raw_id),
                )
            except Exception:
                pass
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
        # wl-372: foreign-seat guard — reject worker:<hand> not hired here.
        from worklane.routing_labels import (  # noqa: PLC0415
            check_mutation_foreign_seat,
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
        _foreign_err = check_mutation_foreign_seat(
            list(_task.labels or []),
            add=add_list,
            remove=remove_list,
            hired_hands=_hired,
        )
        if _foreign_err:
            raise ToolError(_foreign_err)

        try:
            from worklane.wake_nudge import previous_hand_from_labels

            _prev_hand = previous_hand_from_labels(list(_task.labels or []))
        except Exception:
            _prev_hand = None

        updated = tr.update_labels(
            raw_id, add=add_list, remove=remove_list, actor=self.author
        )
        if updated is None:
            raise ToolError(f"task not found: {task_id}")
        # wl-359: seat gain / re-route on claimable ticket → wake.
        try:
            from worklane.wake_nudge import gate_of, maybe_wake_hand

            maybe_wake_hand(
                list(updated.labels or []),
                status=updated.status,
                gate_type=gate_of(updated),
                only_on_seat_change=True,
                previous_hand=_prev_hand,
                task_id=self._public_id(slug, raw_id),
            )
        except Exception:
            pass
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
        if gate_type is not None and gate_type not in (
            "",
            "human",
            "timer",
            "deferred",
            "tracking",
        ):
            raise ToolError(
                "gate_type must be '' (clear), 'human', 'timer', 'deferred', or 'tracking'"
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
        # wl-359: gate-clear on seated ready ticket → wake.
        if gate_type is not None and str(gate_type).strip() == "":
            try:
                from worklane.wake_nudge import gate_of, maybe_wake_hand

                maybe_wake_hand(
                    list(updated.labels or []),
                    status=updated.status,
                    gate_type=gate_of(updated),
                    only_on_seat_change=False,
                    task_id=self._public_id(slug, raw_id),
                )
            except Exception:
                pass
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
        # wl-359: reopen → backlog on a seated hand → wake.
        if fresh is not None:
            try:
                from worklane.wake_nudge import gate_of, maybe_wake_hand

                maybe_wake_hand(
                    list(fresh.labels or []),
                    status=fresh.status,
                    gate_type=gate_of(fresh),
                    only_on_seat_change=False,
                    task_id=self._public_id(slug, raw_id),
                )
            except Exception:
                pass
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
