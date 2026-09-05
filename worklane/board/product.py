"""Product / priority parsing for work-queue and tickets-app paths."""
from __future__ import annotations

from typing import Optional

from worklane.board.constants import (
    PRODUCT_LABEL_OPS,
    PRODUCT_LABEL_TRADEOS,
    TICKETS_APP_ALL,
)
from worklane.products import product_slugs

def _embed_product_query_param(list_path: str) -> bool:
    return not (list_path or "").startswith("/admin/tickets/")


def wq_product_sql_label(product: str) -> Optional[str]:
    if product == "tradeos":
        return PRODUCT_LABEL_TRADEOS
    if product == "ops":
        return PRODUCT_LABEL_OPS
    return None


def parse_wq_priority(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        v = int(s)
    except ValueError:
        return None
    return v if v in (1, 2, 3, 4) else None


# Product aliases (wl-207 cutover: store is worklane; old slug still resolves).
# Unknown explicit slugs must NOT expand to multi (wl-219 fail-closed).
_PRODUCT_ALIASES = {
    "trade_os": "tradeos",
    "ops": "ops",
    "op": "ops",
    # wl-207: store slug is worklane; legacy slug keeps resolving
    "worklane": "worklane",
    "wl": "worklane",  # rare: slug passed as prefix name
    # davi-9 B / wl-492: folder+store comms; davinci slug keeps resolving
    "davinci": "comms",
}


def parse_wq_product(raw: Optional[str]) -> str:
    """Resolve product scope. Empty string = multi/all.

    Known aliases map (worklane→worklane). **Unknown explicit
    slugs also return \"\" for back-compat** — prefer
    :func:`resolve_wq_product` on new list paths so callers can fail closed
    (wl-219).
    """
    prod, _ok = resolve_wq_product(raw)
    return prod or ""


def resolve_wq_product(raw: Optional[str]) -> tuple:
    """Return ``(product_or_empty, ok)``.

    * ``(\"\", True)`` — omitted / all (multi-store).
    * ``(\"tradeos\", True)`` — known store or alias.
    * ``(\"\", False)`` — client **explicitly** passed an unknown slug
      (must not silently become multi — wl-219).
    """
    if raw is None:
        return "", True
    s = str(raw).strip().lower()
    if s in ("", "all"):
        return "", True
    s = _PRODUCT_ALIASES.get(s, s)
    if s in product_slugs() or s == "ops":
        return s, True
    return "", False


def product_scope_from_list_path(list_path: str) -> str:
    p = (list_path or "").rstrip("/")
    if p.endswith("/ops"):
        return "ops"
    tail = p.rsplit("/", 1)[-1]
    if tail in product_slugs():
        return tail
    return ""


def tickets_app_path(slug: str) -> str:
    """Canonical Pool path for a product surface (``all`` for no scope)."""
    s = (slug or "").strip().lower()
    if s and s in product_slugs():
        return f"/admin/tickets/{s}"
    return TICKETS_APP_ALL
