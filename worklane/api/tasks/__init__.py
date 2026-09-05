"""Task CRUD and dev API routes extracted from task_server (wl-225).

Package peel of the former ``worklane/api/tasks.py`` monolith. Existing
imports stay stable: ``from worklane.api.tasks import router`` and
``import worklane.api.tasks as _tasks_api`` keep working.
"""
from __future__ import annotations

from worklane.api.tasks._router import router
from worklane.api.tasks.helpers import (  # noqa: F401
    DEFAULT_AGENT_ID,
    _project_from_request,
    _resolve_write_tracker,
    _tracker_db_path,
    _workforce_products_for_workers,
    _workforce_roster_path,
    _workforce_workers_for_product,
)
from worklane.api.tasks.ops import (  # noqa: F401
    _invalidate_attention_cache,
    _build_attention_payload,
)
from worklane.api.tasks.crud import (  # noqa: F401
    _comment_process_violation,
    _misattributed_owner,
)

# Side-effect imports register routes on the shared router.
from worklane.api.tasks import products as _products  # noqa: F401,E402
from worklane.api.tasks import crud as _crud  # noqa: F401,E402
from worklane.api.tasks import ops as _ops  # noqa: F401,E402

__all__ = [
    "DEFAULT_AGENT_ID",
    "router",
    "_invalidate_attention_cache",
    "_workforce_workers_for_product",
]
