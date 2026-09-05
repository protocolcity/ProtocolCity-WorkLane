"""Board rendering helpers for WorkLane (extracted from host, ADR-025 Phase 1b).

Package peel of the former ``worklane/board.py`` monolith. Existing
``from worklane.board import …`` callers keep working.
"""
from __future__ import annotations

from worklane.board.assets import _board_styles, _client_js
from worklane.board.badges import (
    _label_tier,
    _render_labels,
    _render_priority_badge,
    _render_status_badge,
)
from worklane.board.constants import (
    OWNER_BYLINE_ICON,
    PRODUCT_LABEL_OPS,
    PRODUCT_LABEL_TRADEOS,
    TASK_ID_PREFIX_OPS,
    TASK_ID_PREFIX_TRADEOS,
    TICKETS_APP_ALL,
    TICKETS_APP_OPS,
    TICKETS_APP_TRADEOS,
    _BOARD_COLUMNS,
    _CHIP_FACET_PREFIXES,
    _CHIP_TOP_N,
    _PRIORITY_LABELS,
    _PRIORITY_TIERS,
    _STATUS_LABELS,
    _STATUS_TIERS,
    _WORK_QUEUE_PATH,
)
from worklane.board.filters import (
    _render_work_queue_filters,
    _render_wq_quick_buckets,
    _wq_query_for_view,
)
from worklane.board.ops import (
    get_ops_ticket_tracker,
    ops_tickets_db_path,
    parse_surface_task_id,
)
from worklane.board.product import (
    _PRODUCT_ALIASES,
    _embed_product_query_param,
    parse_wq_priority,
    parse_wq_product,
    product_scope_from_list_path,
    resolve_wq_product,
    tickets_app_path,
    wq_product_sql_label,
)
from worklane.board.queries import (
    _OWNER_LINE_RE,
    _extract_owner,
    _extract_owner_claim,
    _load_preview_comments_multi,
    _parse_gate_filter,
    _search_terms_for_store,
    _task_id_matches_q,
    _tracker_status_counts,
    _wq_column_counts,
    _wq_gate_counts,
    _wq_status_counts,
    column_counts_for_scope_multi,
    list_tasks_for_product_scope,
    list_tasks_for_scope_multi,
    list_tasks_for_wq_multi,
    status_counts_for_scope_multi,
)
from worklane.board.render import (
    _BOARD_COLUMN_CAP,
    _INFLIGHT_STATUSES,
    _claim_stale_minutes,
    _detect_owner,
    _owner_claim_html,
    _parse_iso_ts,
    _render_column_body,
    _render_comments,
    _render_task_board,
    _render_task_card,
    _scoped_labels,
)

__all__ = [
    "OWNER_BYLINE_ICON",
    "PRODUCT_LABEL_OPS",
    "PRODUCT_LABEL_TRADEOS",
    "TASK_ID_PREFIX_OPS",
    "TASK_ID_PREFIX_TRADEOS",
    "TICKETS_APP_ALL",
    "TICKETS_APP_OPS",
    "TICKETS_APP_TRADEOS",
    "_PRODUCT_ALIASES",
    "_OWNER_LINE_RE",
    "_STATUS_LABELS",
    "_WORK_QUEUE_PATH",
    "_board_styles",
    "_claim_stale_minutes",
    "_client_js",
    "_load_preview_comments_multi",
    "_owner_claim_html",
    "_parse_gate_filter",
    "_parse_iso_ts",
    "_render_comments",
    "_render_labels",
    "_render_priority_badge",
    "_render_status_badge",
    "_render_task_board",
    "_render_task_card",
    "_render_work_queue_filters",
    "_scoped_labels",
    "_wq_column_counts",
    "_wq_query_for_view",
    "column_counts_for_scope_multi",
    "get_ops_ticket_tracker",
    "list_tasks_for_scope_multi",
    "list_tasks_for_wq_multi",
    "ops_tickets_db_path",
    "parse_wq_priority",
    "parse_wq_product",
    "product_scope_from_list_path",
    "resolve_wq_product",
    "status_counts_for_scope_multi",
    "tickets_app_path",
]
