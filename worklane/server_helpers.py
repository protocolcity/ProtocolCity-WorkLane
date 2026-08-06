"""Shared helpers extracted from task_server (wl-225).

Functions used by both the board/UI routes (task_server.py) and the task
CRUD / dev API routes (api/tasks.py).  No host imports; no core.* imports.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from worklane import archival
from worklane.board import (
    TASK_ID_PREFIX_OPS,
    _claim_stale_minutes,
    _parse_iso_ts,
    get_ops_ticket_tracker,
    list_tasks_for_scope_multi,
    list_tasks_for_wq_multi,
    parse_wq_product,
)
from worklane.devqueue import WorkQueue
from worklane.products import (
    ProductSpec,
    live_feed_product_slug,
    product_tracker,
    product_trackers,
    resolve_write_task_id,
    split_task_id,
    wl_data_dir,
    get_product,
)
from worklane.trackers import Task, TaskComment, TaskStatus


# ── Optional tradeOS HTTP-feed plugin (wl-222/wl-223) ──────────────────────
try:
    from worklane.plugins.host_feed import (  # noqa: PLC0415
        _tradeos_api_base,
        _fetch_tradeos_json,
        _tradeos_tickets_use_http_feed,
        _request_tradeos_json,
        _task_from_tradeos_api_row,
        _tradeos_preview_map_from_api_tasks,
        _fetch_tradeos_tasks_via_http,
        _list_tasks_for_wq_multi_resolved,
        _fetch_tradeos_ops_snapshot,
    )
except ImportError:
    def _tradeos_api_base() -> str:  # type: ignore[misc]
        return ""

    def _fetch_tradeos_json(  # type: ignore[misc]
        path: str, timeout: float = 1.75
    ) -> Optional[Dict[str, Any]]:
        return None

    def _tradeos_tickets_use_http_feed() -> bool:  # type: ignore[misc]
        return False

    def _request_tradeos_json(  # type: ignore[misc]
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        timeout: float = 12.0,
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        return -1, None

    def _task_from_tradeos_api_row(row: Dict[str, Any]) -> Any:  # type: ignore[misc]
        raise NotImplementedError("host_feed not available")

    def _tradeos_preview_map_from_api_tasks(  # type: ignore[misc]
        rows: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, str]]:
        return {}

    def _fetch_tradeos_tasks_via_http(  # type: ignore[misc]
        *,
        status: Optional[str],
        label: Optional[str],
        priority: Optional[int],
        limit: int,
        with_preview: bool,
    ) -> Tuple[List[Task], Dict[str, Dict[str, str]]]:
        return [], {}

    def _list_tasks_for_wq_multi_resolved(  # type: ignore[misc]
        products: List[Tuple[ProductSpec, Any]],
        *,
        status: Optional[str],
        label: Optional[str],
        priority: Optional[int],
        product: str,
        limit: int,
        with_preview: bool,
        gate_type: Optional[str] = None,
    ) -> Tuple[List[Task], Dict[str, Dict[str, str]]]:
        p = (product or "").strip().lower()
        return list_tasks_for_wq_multi(
            products,
            status=status,
            label=label,
            priority=priority,
            product=p,
            gate_type=gate_type,
            limit=limit,
        ), {}

    def _fetch_tradeos_ops_snapshot() -> Dict[str, Optional[Dict[str, Any]]]:  # type: ignore[misc]
        return {}


# ── Scope helpers ───────────────────────────────────────────────────────────

def _scoped_product_trackers(scope: str = "") -> List[Tuple[ProductSpec, Any]]:
    """Registered (spec, tracker) pairs, narrowed to one project store when a
    scope slug is given ("" = every store) — wl-85: every page declares a
    scope and everything on it honors that scope.
    """
    pairs = product_trackers()
    if not scope:
        return pairs
    return [(s, t) for s, t in pairs if s.slug == scope]


def _merged_ready_count(scope: str = "") -> int:
    """Ready-to-dispatch count aggregated across the in-scope product
    trackers (wl-40; "" = all) — each product's WorkQueue resolves its own
    blockers independently (blocker ids don't cross product boundaries), so
    this sums per-tracker ready() counts rather than building one
    cross-product queue. Local SQLite trackers only, same as
    list_tasks_for_scope_multi's non-HTTP-feed branch below — the tradeOS
    live-HTTP-feed source is wl-48 slice c's separate concern.
    """
    return sum(
        len(WorkQueue(tracker).ready())
        for _spec, tracker in _scoped_product_trackers(scope)
    )


def _merged_in_flight_tasks(scope: str = "") -> List[Task]:
    """in_progress + in_review tasks aggregated across the in-scope product
    trackers (wl-40; "" = all), sorted newest-updated first. See
    _merged_ready_count for the local-SQLite-only scope note.

    wl-353: status-filtered + slim rows (no description). Avoids WorkQueue's
    default limit=500 full-table materialization per store.
    """
    out: List[Task] = []
    for spec, tracker in _scoped_product_trackers(scope):
        for status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
            try:
                batch = tracker.list_tasks(
                    status=status, include_description=False,
                )
            except TypeError:
                # Non-SQLite adapters that don't accept include_description.
                batch = tracker.list_tasks(status=status)
            out.extend(
                # Composite ids, same convention as list_tasks_for_scope_multi
                # (wl-144): bare store-local ids fall back to the DEFAULT store
                # in split_task_id, mis-attributing every non-default store's
                # in-flight work downstream (attention feed, in-flight API).
                replace(t, id=f"{spec.prefix}-{t.id}")
                for t in batch
            )
    out.sort(key=lambda t: t.updated_at or "", reverse=True)
    return out


def _merged_scope_tasks_for_filters(product: str) -> List[Task]:
    """All tasks for label chips / buckets (respects HTTP vs local live-feed source)."""
    p = parse_wq_product(product)
    products = product_trackers()
    if not _tradeos_tickets_use_http_feed():
        return list_tasks_for_scope_multi(products, p, limit=None)
    feed_slug = live_feed_product_slug()
    merged: List[Task] = []
    if p in ("", feed_slug):
        ta, _ = _fetch_tradeos_tasks_via_http(
            status=None,
            label=None,
            priority=None,
            limit=5000,
            with_preview=False,
        )
        merged.extend(ta)
    if p != feed_slug:
        non_feed = [(s, t) for s, t in products if s.slug != feed_slug]
        merged.extend(list_tasks_for_scope_multi(non_feed, p, limit=None))
    merged.sort(key=lambda x: x.updated_at or "", reverse=True)
    return merged


# ── Date / time helpers ─────────────────────────────────────────────────────

def _parse_task_date_utc(raw: Optional[str]) -> Optional[datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        # Common case: ISO timestamp, optionally with Z suffix.
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        pass
    # Fallbacks for SQLite-ish formats.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _pulse_relative_time(iso_ts: str, *, now: Optional[datetime] = None) -> str:
    """Return a compact relative time string like '3m', '2h', '1d'."""
    if not iso_ts:
        return "—"
    try:
        s = iso_ts.replace("Z", "+00:00")
        ts = datetime.fromisoformat(s)
    except Exception:
        return "—"
    if now is None:
        now = datetime.now(ts.tzinfo)
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return "now"
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h"
    days = hrs // 24
    return f"{days}d"


def _activity_ts_sort_key(raw: object) -> float:
    """Parse mixed ISO/SQLite timestamps so merged feed sorts newest-first reliably."""
    if raw is None:
        return 0.0
    s = str(raw).strip()
    if not s:
        return 0.0
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ── Allocation helpers ──────────────────────────────────────────────────────

_LANE_LABEL_PREFIX = "lane:"


def _tracker_db_path(tracker: Any) -> Optional[Path]:
    """Hot SQLite path for a product tracker, if it is file-backed."""
    p = getattr(tracker, "_db_path", None)
    if p is None:
        return None
    return Path(p)


def _allocation_lane_rows(
    all_tasks: List[Task], since: datetime, *, prefix: str = _LANE_LABEL_PREFIX
) -> List[Dict[str, Any]]:
    """Filed-vs-closed per lane:* label within the window.

    Filed = created_at in window (any status); closed = status==done and
    updated_at in window — same created/updated proxy _render_flow_panel
    uses (no closed_at column). Unlabeled tasks collect into a synthetic
    'unlabeled' row, same convention as _lane_lens_rows.
    """
    buckets: Dict[str, Dict[str, int]] = {}

    def _bucket(name: str) -> Dict[str, int]:
        return buckets.setdefault(name, {"filed": 0, "closed": 0})

    for t in all_tasks:
        lanes = [lbl[len(prefix):] for lbl in (t.labels or []) if lbl.startswith(prefix)] or ["unlabeled"]
        created = _parse_iso_ts(t.created_at)
        if created is not None and created >= since:
            for lane in lanes:
                _bucket(lane)["filed"] += 1
        if t.status == TaskStatus.DONE:
            closed_at = _parse_iso_ts(t.updated_at)
            if closed_at is not None and closed_at >= since:
                for lane in lanes:
                    _bucket(lane)["closed"] += 1

    rows = [
        {"lane": lane, **counts}
        for lane, counts in buckets.items()
        if counts["filed"] or counts["closed"]
    ]
    rows.sort(key=lambda r: (r["lane"] != "unlabeled", -(r["filed"] + r["closed"]), r["lane"]))
    return rows


def _allocation_author_rows(scope: str, since: datetime) -> List[Dict[str, Any]]:
    """Filed-vs-closed per comment author within the window.

    Same signed-comment derivation as _author_tally: filed = 'Intake: filed
    by%' comment (PROTOCOL.md §5 intake marker), closed = 'Completed:%'
    closeout comment — windowed on the comment's own created_at.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    since_s = since.isoformat()
    for _spec, tracker in _scoped_product_trackers(scope):
        db_path = _tracker_db_path(tracker)
        if db_path is None or not Path(db_path).exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    """
                    SELECT author,
                           COUNT(DISTINCT CASE WHEN body LIKE 'Intake: filed by%'
                                               AND created_at >= ? THEN task_id END) AS filed,
                           COUNT(DISTINCT CASE WHEN body LIKE 'Completed:%'
                                               AND created_at >= ? THEN task_id END) AS closed
                    FROM task_comments
                    WHERE author != ''
                    GROUP BY author
                    """,
                    (since_s, since_s),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            continue
        for author, filed, closed in rows:
            agg = merged.setdefault(author, {"author": author, "filed": 0, "closed": 0})
            agg["filed"] += int(filed or 0)
            agg["closed"] += int(closed or 0)
    out = [a for a in merged.values() if a["filed"] or a["closed"]]
    out.sort(key=lambda a: (-(a["filed"] + a["closed"]), a["author"]))
    return out


# ── Task / tracker resolution ───────────────────────────────────────────────

def _resolve_product_tracker(
    task_id: str,
    project: Optional[str] = None,
    *,
    write: bool = False,
) -> Tuple[str, str, Any]:
    """Composite task id → (product slug, raw store id, tracker).

    ``o-`` ids still resolve to the retired ops store so legacy links
    keep working; everything else routes through the product registry.

    When ``write=True`` (wl-344): bare ids require an explicit ``project``
    (query/body); never fall through to the configured default store.
    Raises ``ValueError`` with a caller-safe message on addressing errors.
    """
    if write:
        slug, raw = resolve_write_task_id(task_id, project)
    else:
        slug, raw = split_task_id(task_id)
        explicit = str(project or "").strip().lower() or None
        if explicit and explicit not in ("all", slug):
            # Read-path mismatch is still a hard error when both are given.
            raise ValueError(
                f"task_id {task_id!r} belongs to product {slug!r}, "
                f"not {explicit!r}"
            )
    if slug == "ops":
        return slug, raw, get_ops_ticket_tracker()
    return slug, raw, product_tracker(slug)


def _public_prefix_for_surface(surf: str) -> str:
    spec = get_product(surf) if surf not in ("ops", "op") else None
    return TASK_ID_PREFIX_OPS if surf in ("ops", "op") else (spec.prefix if spec else "t")


def _task_relations_dicts(
    surf: str, raw_id: str, tracker: Any
) -> List[Dict[str, Any]]:
    """Structured relations for a ticket (empty when store is not local SQLite)."""
    from worklane import relations as relmod  # noqa: PLC0415

    try:
        db_path = _tracker_db_path(tracker)
    except Exception:
        return []
    if db_path is None:
        return []
    try:
        rels = relmod.list_relations(db_path, task_id=raw_id)
    except Exception:
        return []
    prefix = _public_prefix_for_surface(surf)

    def _pub(tid: str) -> str:
        return f"{prefix}-{tid}"

    out: List[Dict[str, Any]] = []
    for r in rels:
        d = r.to_dict()
        d["from_id"] = _pub(r.from_id)
        d["to_id"] = _pub(r.to_id)
        out.append(d)
    return out


def _archive_tracker_for_hot_db(hot_db: Path) -> Optional[Any]:
    """Open the sibling archive store for ``hot_db`` (None if missing)."""
    from worklane.trackers.sqlite import SQLiteTracker  # noqa: PLC0415

    archive_path = archival.archive_db_path_for(hot_db)
    if not archive_path.exists():
        return None
    return SQLiteTracker(db_path=archive_path, product_default="")


def _get_task_hot_or_archive(
    tracker: Any, raw_id: str
) -> Tuple[Optional[Task], List[TaskComment], bool]:
    """Hot-store first; fall through to sibling archive DB (read-only).

    Returns ``(task, comments, archived)``. ``archived=True`` means the
    row lives only in cold storage — mutations must refuse.
    """
    task = tracker.get_task(raw_id)
    if task is not None:
        comments: List[TaskComment] = (
            tracker.list_comments(raw_id)
            if hasattr(tracker, "list_comments")
            else []
        )
        return task, comments, False

    hot = _tracker_db_path(tracker)
    if hot is None:
        return None, [], False
    archive_tr = _archive_tracker_for_hot_db(hot)
    if archive_tr is None:
        return None, [], False
    task = archive_tr.get_task(raw_id)
    if task is None:
        return None, [], False
    comments = (
        archive_tr.list_comments(raw_id)
        if hasattr(archive_tr, "list_comments")
        else []
    )
    return task, comments, True


# ── Attention / founder gate feed ───────────────────────────────────────────

def _stale_inflight() -> timedelta:
    return timedelta(minutes=_claim_stale_minutes())


_FOUNDER_DECISION_LABELS = {"needs:founder-decision", "founder-decision"}


def _is_inbox_report(labels: Optional[Any]) -> bool:
    """True when any label is ``inbox-report`` or ``inbox-report:*``.

    Canonical stamp from workspace ``report_to_for_you.py`` (wl-331 research).
    Used to split For You **Read** from act-now **Decide** without a new
    ``gate_type`` — both stay ``gate_type=human`` / ``kind=human_gate``.
    """
    for raw in labels or []:
        lab = str(raw or "")
        if lab == "inbox-report" or lab.startswith("inbox-report:"):
            return True
    return False


def _derive_attention_face(kind: str, labels: Optional[Any] = None) -> str:
    """Citizen face for Map/Overview: ``decide`` | ``read`` | ``watch`` (wl-405).

    Additive only — engine ``kind`` strings stay stable for snooze scopes and
    desk stamps. Rules (docs/research/attention-kinds-decide-read-note-wl-331.md):

    - **read** — any ``inbox-report`` / ``inbox-report:*`` label (wins even when
      kind is human_gate)
    - **watch** — kind in {stalled, embargo}
    - **decide** — default for human_gate / founder_decision gold
    """
    if _is_inbox_report(labels):
        return "read"
    if kind in ("stalled", "embargo"):
        return "watch"
    return "decide"


def _attention_band_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """Tally visible attention rows by face for suite KPI SoT (wl-405).

    ``act_now_count`` = decide face (Map You-pill “decisions waiting”).
    Sum of the three bands equals ``len(items)`` when every item carries a face.
    """
    act_now = 0
    read = 0
    watch = 0
    for it in items:
        face = it.get("face")
        if not face:
            face = _derive_attention_face(str(it.get("kind") or ""), None)
        if face == "read":
            read += 1
        elif face == "watch":
            watch += 1
        else:
            act_now += 1
    return {
        "act_now_count": act_now,
        "read_count": read,
        "watch_count": watch,
    }


def _attention_item(
    t: Task, product: str, kind: str, note: str,
    since: Optional[datetime], now: datetime,
) -> Dict[str, Any]:
    return {
        "id": t.id,
        "product": product,
        "title": t.title,
        "priority": int(t.priority or 3),
        "kind": kind,
        "face": _derive_attention_face(kind, t.labels),
        "note": note,
        "waiting_since": since.isoformat() if since else None,
        "age_minutes": int((now - since).total_seconds() // 60) if since else None,
        "gate_until": None,
        "url": f"/admin/desk?open={t.id}",
    }


def _human_gate_is_parked(gate_note: Optional[str]) -> bool:
    """True when human gate withholds ready but is NOT a "You act now" signal.

    City practice (tradeOS north-star deferrals, epic umbrellas): gate_type=human
    keeps agents off the ticket, but For You / Map gold must not paint deferred
    tracks as urgent. Convention (PROCESS §3.9): gate_note starts with or
    contains parked markers — not a blank human gate that still needs a call.
    """
    n = (gate_note or "").strip().lower()
    if not n:
        return False
    if n.startswith("deferred:") or n.startswith("umbrella"):
        return True
    markers = (
        "post-northstar",
        "not claimable",
        "withheld from ready",
        "thaw when",
        "parked:",
        "far future",
    )
    return any(m in n for m in markers)


def _attention_seed_tasks_for_store(spec: ProductSpec, tracker: Any) -> List[Task]:
    """Per-store seed rows for founder attention (wl-353).

    Prefers SQLite's slim SQL (no description blobs, only candidate statuses/
    gates/labels). Falls back to a full list + Python filter for adapters that
    lack ``list_attention_seed_tasks``.
    """
    if hasattr(tracker, "list_attention_seed_tasks"):
        seeds = tracker.list_attention_seed_tasks()
    else:
        try:
            all_rows = tracker.list_tasks(include_description=False)
        except TypeError:
            all_rows = tracker.list_tasks()
        seeds = []
        for t in all_rows:
            if t.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
                seeds.append(t)
                continue
            if t.status in (TaskStatus.DONE, TaskStatus.CANCELED):
                continue
            labels = set(t.labels or [])
            if labels & _FOUNDER_DECISION_LABELS:
                seeds.append(t)
            elif t.gate_type in ("human", "timer"):
                seeds.append(t)
    return [replace(t, id=f"{spec.prefix}-{t.id}") for t in seeds]


def _collect_founder_attention_items(*, now: datetime) -> List[Dict[str, Any]]:
    """Everything blocked on the founder *now*, all stores (wl-135 / wl-257):

    needs:founder-decision labels, gate_type=human that still needs a concrete
    You action, stalled in-flight (§4, >90m), and gate_type=timer embargoes.
    Sorted oldest-first.

    **Not** in the list (2026-07-30 founder — file=decided / scarce For You):
    - bare ``in_review`` — that status is soft-lock / reserve / bundle park
      (PROCESS §4), **not** “work finished, wait for You to accept.” Ordinary
      implement WOs close ``in_progress`` → ``done``. Use ``gate_type=human``
      only when You must act now.
    - parked human gates (deferred:/umbrella/post-northstar etc.) — they still
      withhold ready, but must not gold-paint For You.

    Each open task counts once. Code order (preserved): stalled in-flight first,
    then founder_decision / human_gate / timer on remaining open seeds.

    wl-353: walks slim per-store attention seeds instead of loading every
    ticket (and multi-MB description blobs) via ``_merged_scope_tasks_for_filters``.
    """
    items: List[Dict[str, Any]] = []
    counted: set = set()
    seeds: List[Task] = []
    for spec, tracker in product_trackers():
        seeds.extend(_attention_seed_tasks_for_store(spec, tracker))

    for t in seeds:
        if t.status not in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
            continue
        prod_slug, _ = split_task_id(t.id)
        # in_review is no longer auto-gold (was misread as founder accept).
        # Still surface stalled in_progress/in_review after 90m silence.
        since = _parse_task_date_utc(t.updated_at)
        if since is not None and (now - since) >= _stale_inflight():
            items.append(_attention_item(
                t, prod_slug, "stalled",
                f"no update {_pulse_relative_time(t.updated_at, now=now)}", since, now,
            ))
            counted.add(t.id)
    for t in seeds:
        if t.id in counted or t.status in (TaskStatus.DONE, TaskStatus.CANCELED):
            continue
        prod_slug, _ = split_task_id(t.id)
        since = _parse_task_date_utc(t.updated_at) or _parse_task_date_utc(t.created_at)
        labels = set(t.labels or [])
        if labels & _FOUNDER_DECISION_LABELS:
            items.append(_attention_item(t, prod_slug, "founder_decision", "founder decision needed", since, now))
            counted.add(t.id)
        elif t.gate_type == "deferred":
            # Deferred gate: withholds ready but never enters For You (wl-261).
            # Seed query already skips pure-deferred; keep for fallback path.
            continue
        elif t.gate_type == "human":
            if _human_gate_is_parked(t.gate_note):
                # Ready still withheld; For You / Map gold skip (scarce signal law).
                continue
            items.append(_attention_item(t, prod_slug, "human_gate", t.gate_note or "human gate", since, now))
            counted.add(t.id)
        elif t.gate_type == "timer" and t.gate_until:
            note = f"gated until {t.gate_until[:10]}" if t.gate_until else (t.gate_note or "embargoed")
            item = _attention_item(t, prod_slug, "embargo", note, since, now)
            item["gate_until"] = t.gate_until
            items.append(item)
            counted.add(t.id)

    items.sort(key=lambda it: it["waiting_since"] or "")
    return items


# ── Attention prefs / snooze ────────────────────────────────────────────────

_ATTENTION_PREFS_NAME = "attention_prefs.json"


def _attention_prefs_path() -> Path:
    return wl_data_dir() / _ATTENTION_PREFS_NAME


def _load_attention_prefs() -> Dict[str, Any]:
    path = _attention_prefs_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, ValueError, TypeError):
        pass
    return {"snoozes": []}


def _save_attention_prefs(prefs: Dict[str, Any]) -> None:
    path = _attention_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_until_iso(raw: str, *, now: datetime) -> datetime:
    """Accept ISO datetime, YYYY-MM-DD, or duration tokens: today|1d|3d|1w|eod."""
    s = (raw or "").strip().lower()
    if not s:
        raise ValueError("until is required")
    # end of local calendar day in UTC-ish: use now's date + 1 day 00:00 UTC as "tomorrow morning"
    if s in ("today", "eod", "end-of-day"):
        # snooze until next local midnight approximated as now+ (24h - hour) or simply +12h max day remainder
        end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if end <= now:
            end = now + timedelta(hours=12)
        return end
    if s in ("1d", "day", "24h"):
        return now + timedelta(days=1)
    if s in ("3d", "3day"):
        return now + timedelta(days=3)
    if s in ("1w", "week", "7d"):
        return now + timedelta(days=7)
    if s in ("1h", "hour"):
        return now + timedelta(hours=1)
    # date only
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return d
        except ValueError as exc:
            raise ValueError(f"bad until date: {raw!r}") from exc
    # ISO
    try:
        iso = raw.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError(
            "until must be ISO datetime, YYYY-MM-DD, or today|1d|3d|1w"
        ) from exc


def _active_attention_snoozes(*, now: datetime) -> List[Dict[str, Any]]:
    prefs = _load_attention_prefs()
    raw = prefs.get("snoozes") if isinstance(prefs.get("snoozes"), list) else []
    active: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    changed = False
    for s in raw:
        if not isinstance(s, dict):
            changed = True
            continue
        until_s = s.get("until") or ""
        try:
            until = _parse_until_iso(str(until_s), now=now) if until_s else None
        except ValueError:
            changed = True
            continue
        if until is not None and until <= now:
            changed = True
            continue
        kept.append(s)
        active.append({
            "scope": s.get("scope") or "product",
            "product": (s.get("product") or "").strip().lower(),
            "kind": (s.get("kind") or "").strip().lower(),
            "task_id": (s.get("task_id") or "").strip().lower(),
            "until": until.isoformat() if until else None,
            "reason": s.get("reason") or "",
        })
    if changed:
        prefs["snoozes"] = kept
        _save_attention_prefs(prefs)
    return active


def _item_is_snoozed(it: Dict[str, Any], snoozes: List[Dict[str, Any]]) -> bool:
    prod = (it.get("product") or "").strip().lower()
    kind = (it.get("kind") or "").strip().lower()
    task_id = (it.get("id") or "").strip().lower()
    for s in snoozes:
        scope = s.get("scope") or "product"
        if scope == "product" and s.get("product") and s["product"] == prod:
            return True
        if scope == "kind" and s.get("kind") and s["kind"] == kind:
            return True
        if scope == "task" and s.get("task_id") and s["task_id"] == task_id:
            return True
        if scope == "all":
            return True
    return False


def _partition_attention_items(
    items: List[Dict[str, Any]], *, now: datetime
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Returns (visible, snoozed_items, active_snoozes)."""
    snoozes = _active_attention_snoozes(now=now)
    vis: List[Dict[str, Any]] = []
    hid: List[Dict[str, Any]] = []
    for it in items:
        if _item_is_snoozed(it, snoozes):
            hid.append(it)
        else:
            vis.append(it)
    return vis, hid, snoozes


# ── Identity config (wl-148) — shared by task_server board routes + api/tasks ─

_FOUNDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


def _identity_config_path() -> Path:
    return wl_data_dir() / "identity.json"


def _identity_config() -> Dict[str, str]:
    cfg: Dict[str, str] = {"founder_id": "founder", "founder_alias": ""}
    try:
        raw = json.loads(_identity_config_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k in cfg:
                if isinstance(raw.get(k), str):
                    cfg[k] = raw[k]
    except (OSError, ValueError):
        pass
    return cfg
