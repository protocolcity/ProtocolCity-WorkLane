"""Product-store management routes (list / create / update / compact)."""
from __future__ import annotations

import re
from typing import Any, Dict

from fastapi import Request
from fastapi.responses import JSONResponse

from worklane import archival
from worklane.api.tasks._router import router
from worklane.api.tasks.helpers import _tracker_db_path
from worklane.products import (
    all_taken_prefixes,
    discover_products,
    get_product,
    live_feed_product_slug,
    product_tracker,
    register_product_meta,
    wl_data_dir,
)


@router.get("/api/admin/products")
def api_list_products() -> JSONResponse:
    """List all registered product stores (wl-253): slug, display, prefix, db_path."""
    specs = discover_products()
    return JSONResponse({
        "ok": True,
        "products": [
            {
                "slug": s.slug,
                "display": s.display,
                "prefix": s.prefix,
                "db_path": str(s.db_path),
            }
            for s in specs
        ],
    })


@router.post("/api/admin/products")
async def api_create_product(request: Request) -> JSONResponse:
    """Bootstrap a new product store (wl-12): creates ``<slug>.db`` and
    returns its surface. Deliberate by design — no implicit creation from a
    typo'd ``surface=`` on ``/api/admin/tasks``; this is the only door in.
    """
    from worklane.trackers.sqlite import SQLiteTracker  # noqa: PLC0415
    from worklane.task_server import _city_neighborhood_slugs  # noqa: PLC0415

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    slug = str(payload.get("slug") or "").strip().lower()
    if not slug:
        return JSONResponse({"ok": False, "error": "slug is required"}, status_code=400)
    if not re.match(r"^[a-z][a-z0-9_-]{0,39}$", slug):
        return JSONResponse(
            {
                "ok": False,
                "error": "slug must start with a letter and contain only lowercase "
                "letters, digits, '-' or '_' (max 40 chars)",
            },
            status_code=400,
        )
    if slug in ("all", "ops", "op"):
        return JSONResponse(
            {"ok": False, "error": f"{slug!r} is a reserved surface name"},
            status_code=400,
        )

    existing = get_product(slug)
    if existing is not None and (existing.db_path.exists() or slug == live_feed_product_slug()):
        return JSONResponse(
            {"ok": False, "error": f"project {slug!r} already exists"},
            status_code=409,
        )

    # wl-427 / pc-1186 (successai incident): when a city root is known, refuse
    # creating a product store that has no matching neighborhood folder
    # (AGENTS.md). Soft warn (wl-155) was not enough — GH fixture force-adopt
    # still POSTed empty stores onto production desk. Escape hatch:
    # ``allow_orphan: true`` (founder deliberate; rare). Host-neutral installs
    # (no city root) keep free create.
    allow_orphan = bool(payload.get("allow_orphan"))
    hoods = _city_neighborhood_slugs()
    if hoods is not None and slug not in hoods and not allow_orphan:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"no neighborhood folder for slug {slug!r} at the city root "
                    "(need a top-level folder with AGENTS.md whose slug matches; "
                    "slug = folder name lowercased, whitespace → hyphens). "
                    "Refusing create so fixture/foreign adopts cannot pollute "
                    "the live desk (wl-427 · successai). For deliberate orphan "
                    "stores only: pass allow_orphan=true."
                ),
                "code": "neighborhood-required",
            },
            status_code=400,
        )

    display = str(payload.get("display") or "").strip() or None
    prefix = str(payload.get("prefix") or "").strip().lower() or None
    if prefix is not None:
        if not re.match(r"^[a-z][a-z0-9]{1,7}$", prefix):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "prefix must be 2-8 lowercase letters/digits, starting with a letter",
                },
                status_code=400,
            )
        taken = all_taken_prefixes()
        if prefix in taken:
            return JSONResponse(
                {"ok": False, "error": f"prefix {prefix!r} is already used by another project"},
                status_code=400,
            )

    db_path = wl_data_dir() / f"{slug}.db"
    tracker = SQLiteTracker(db_path=db_path, product_default=f"product:{slug}")
    tracker.list_tasks(limit=1)  # forces _connect(), materializing the file + schema

    if display or prefix:
        register_product_meta(slug, display=display, prefix=prefix)

    spec = get_product(slug)
    if spec is None:
        return JSONResponse(
            {"ok": False, "error": "project store created but not discoverable — check runtime dir"},
            status_code=500,
        )
    warning = None
    if allow_orphan and hoods is not None and slug not in hoods:
        warning = (
            f"orphan store {slug!r} created (allow_orphan=true) — no matching "
            "neighborhood folder; Map will not show a building until one exists"
        )
    return JSONResponse(
        {
            "ok": True,
            "warning": warning,
            "product": {
                "slug": spec.slug,
                "display": spec.display,
                "prefix": spec.prefix,
                "db_path": str(spec.db_path),
            },
        }
    )


@router.patch("/api/admin/products/{slug}")
async def api_update_product(slug: str, request: Request) -> JSONResponse:
    """Rename a product's display name / id prefix (wl-17): writes the
    ``local/config/products.json`` overlay via :func:`register_product_meta`.
    Editing only — the store itself is untouched, and ids already stored
    with the old prefix keep rendering under the new one (composite ids are
    computed at render time, never rewritten).
    """
    spec = get_product(slug)
    if spec is None:
        return JSONResponse({"ok": False, "error": f"unknown project {slug!r}"}, status_code=404)

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    display = payload.get("display")
    display = str(display).strip() if display is not None else None
    prefix = payload.get("prefix")
    prefix = str(prefix).strip().lower() if prefix is not None else None

    if display is None and prefix is None:
        return JSONResponse(
            {"ok": False, "error": "at least one of display/prefix is required"},
            status_code=400,
        )
    if display is not None and not display:
        return JSONResponse({"ok": False, "error": "display cannot be blank"}, status_code=400)
    if prefix is not None:
        if not re.match(r"^[a-z][a-z0-9]{1,7}$", prefix):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "prefix must be 2-8 lowercase letters/digits, starting with a letter",
                },
                status_code=400,
            )
        if prefix == "o":
            return JSONResponse(
                {"ok": False, "error": "prefix 'o' is reserved (legacy ops store)"},
                status_code=400,
            )
        taken = all_taken_prefixes(exclude_slug=slug)
        if prefix in taken:
            return JSONResponse(
                {"ok": False, "error": f"prefix {prefix!r} is already used by another project"},
                status_code=400,
            )

    # A real prefix rename retires the old prefix into legacy_prefixes (wl-152)
    # so every composite id already written under it — comments, close-out
    # Links:, commit messages, bookmarks — keeps resolving forever.
    old_prefix = spec.prefix
    retiring_prefix = old_prefix if (prefix is not None and prefix != old_prefix) else None
    register_product_meta(
        slug, display=display, prefix=prefix, add_legacy_prefix=retiring_prefix
    )
    updated = get_product(slug)
    if updated is None:
        return JSONResponse(
            {"ok": False, "error": "project updated but no longer discoverable"},
            status_code=500,
        )
    return JSONResponse(
        {
            "ok": True,
            "product": {
                "slug": updated.slug,
                "display": updated.display,
                "prefix": updated.prefix,
                "db_path": str(updated.db_path),
            },
        }
    )


@router.post("/api/admin/products/{slug}/compact")
async def api_compact_product(slug: str, request: Request) -> JSONResponse:
    """Move cold done/canceled tickets into the sibling archive DB (wl-23).

    Archival is move-not-delete and reversible. Default age is 90 days.
    Body (optional JSON): ``{"older_than_days": 90}``.
    """
    s = (slug or "").strip().lower()
    spec = get_product(s)
    if spec is None:
        return JSONResponse({"ok": False, "error": f"unknown project {s!r}"}, status_code=404)

    older_than_days = archival.DEFAULT_ARCHIVE_AGE_DAYS
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict) and "older_than_days" in payload:
        try:
            older_than_days = int(payload["older_than_days"])
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "older_than_days must be an integer"},
                status_code=400,
            )
        if older_than_days < 1:
            return JSONResponse(
                {"ok": False, "error": "older_than_days must be >= 1"},
                status_code=400,
            )

    tracker = product_tracker(spec)
    hot = _tracker_db_path(tracker)
    result = archival.archive_cold_tickets(hot, older_than_days=older_than_days)
    archive_path = archival.archive_db_path_for(hot)
    return JSONResponse(
        {
            "ok": True,
            "product": s,
            "tickets": result.tickets,
            "comments": result.comments,
            "relations": result.relations,
            "older_than_days": older_than_days,
            "source_path": result.source_path,
            "archive_path": result.archive_path,
            "archive_count": archival.archive_counts(archive_path),
        }
    )
