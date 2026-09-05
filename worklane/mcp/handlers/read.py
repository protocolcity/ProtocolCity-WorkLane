"""Read-side MCP tools: list, ready, show, mine, counts."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from worklane.devqueue.queue import WorkQueue
from worklane.products import discover_products, product_tracker
from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


class ReadMixin:
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
