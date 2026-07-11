"""Product registry — one product, one SQLite ticket store.

WL treats every ``<slug>.db`` file under the runtime data dir as an
independent product ticket store. Products stay separate by construction:
each has its own DB file, and the Pool UI renders one surface tab per
discovered product plus a merged read-only "All" view. Dropping a new
``<slug>.db`` into the data dir (or creating a ticket via the API with
``surface=<slug>``) is all it takes for a product to appear.

Composite task ids namespace tickets across stores in merged views:
``<prefix>-<rowid>`` (e.g. ``t-1095`` for tradeos, ``wl-3`` for
worklane). Bare numeric ids resolve to tradeos for backward
compatibility.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Slugs with curated display names / short id prefixes. Anything else
# discovered on disk falls back to (title-cased slug, slug) so a new
# product needs zero code here.
_KNOWN_PRODUCT_META: Dict[str, Tuple[str, str]] = {
    "tradeos": ("tradeOS", "t"),
    "worklane": ("WorkLane", "wl"),
}

# Legacy stores that are not product surfaces. ``ops_tickets`` is the
# retired Ops Cockpit store (empty; surface removed from the UI).
_IGNORED_DB_STEMS = {"ops_tickets"}

# tradeos honors env overrides handled inside SQLiteTracker (WORKLANE_DB
# / TRADEOS_TRACKER_DB + legacy path fallbacks), so its spec is always present
# even before the DB file exists on disk.
_ALWAYS_PRESENT = ("tradeos",)


@dataclass(frozen=True)
class ProductSpec:
    slug: str        # path segment + API surface value, e.g. "tradeos"
    display: str     # tab label, e.g. "tradeOS"
    prefix: str      # composite task-id prefix, e.g. "t" in "t-1095"
    db_path: Path    # SQLite store for this product


def wl_data_dir() -> Path:
    """Runtime data dir (honors WORKLANE_RUNTIME_DIR)."""
    override = (os.environ.get("WORKLANE_RUNTIME_DIR") or "").strip()
    if override:
        return Path(override) / "data"
    return Path(__file__).parent / "local" / "data"


def products_config_path() -> Path:
    """Operator overlay for product metadata: ``local/config/products.json``.

    Shape: ``{"<slug>": {"display": "...", "prefix": "..."}}``. Entries win
    over the shipped ``_KNOWN_PRODUCT_META`` defaults; absent keys fall
    through. Surfaced (and eventually edited) via /admin/settings.
    """
    return wl_data_dir().parent / "config" / "products.json"


def _config_overrides() -> Dict[str, Dict[str, str]]:
    cfg = products_config_path()
    try:
        if cfg.exists():
            raw = json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {
                    str(k).strip().lower(): v
                    for k, v in raw.items()
                    if isinstance(v, dict)
                }
    except Exception:
        pass  # malformed overlay never takes the board down
    return {}


def register_product_meta(
    slug: str, display: Optional[str] = None, prefix: Optional[str] = None
) -> None:
    """Persist a display/prefix override for ``slug`` into the config overlay.

    Merges into the existing ``local/config/products.json`` (creating it if
    absent) rather than replacing it, so unrelated overrides survive.
    """
    cfg = products_config_path()
    overrides = _config_overrides()
    entry = dict(overrides.get(slug) or {})
    if display:
        entry["display"] = display
    if prefix:
        entry["prefix"] = prefix
    if not entry:
        return
    overrides[slug] = entry
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(overrides, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _spec_for_slug(slug: str, db_path: Path) -> ProductSpec:
    display, prefix = _KNOWN_PRODUCT_META.get(
        slug, (slug.replace("-", " ").replace("_", " ").title(), slug)
    )
    over = _config_overrides().get(slug) or {}
    display = str(over.get("display") or display)
    prefix = str(over.get("prefix") or prefix).strip().lower() or prefix
    return ProductSpec(slug=slug, display=display, prefix=prefix, db_path=db_path)


def discover_products() -> List[ProductSpec]:
    """Product specs for every ticket store in the data dir.

    Ordering: tradeos first (primary host), then alphabetical. Fresh from
    disk on every call — the server picks up a newly installed product DB
    without a restart.
    """
    data = wl_data_dir()
    slugs: Dict[str, Path] = {}
    for name in _ALWAYS_PRESENT:
        slugs[name] = data / f"{name}.db"
    if data.is_dir():
        for p in sorted(data.glob("*.db")):
            stem = p.stem.strip().lower()
            if not stem or stem in _IGNORED_DB_STEMS:
                continue
            slugs.setdefault(stem, p)
    ordered = [s for s in _ALWAYS_PRESENT if s in slugs] + sorted(
        s for s in slugs if s not in _ALWAYS_PRESENT
    )
    specs: List[ProductSpec] = []
    taken_prefixes = {"o"}  # reserved: legacy ops store ids
    for s in ordered:
        spec = _spec_for_slug(s, slugs[s])
        if spec.prefix in taken_prefixes:
            # Collision (bad overlay or clashing slug): fall back to the
            # slug itself, which is unique by construction.
            spec = ProductSpec(
                slug=spec.slug, display=spec.display,
                prefix=spec.slug, db_path=spec.db_path,
            )
        taken_prefixes.add(spec.prefix)
        specs.append(spec)
    return specs


def get_product(slug: str) -> Optional[ProductSpec]:
    s = (slug or "").strip().lower()
    for spec in discover_products():
        if spec.slug == s:
            return spec
    return None


def product_slugs() -> List[str]:
    return [spec.slug for spec in discover_products()]


def product_tracker(spec_or_slug: Any) -> Any:
    """Fresh tracker bound to the product's store.

    tradeos goes through :func:`get_default_tracker` so the
    ``TRADEOS_TRACKER`` adapter selection and DB-path env overrides keep
    working; every other product binds SQLite directly to its file.
    """
    from worklane.trackers import get_default_tracker
    from worklane.trackers.sqlite import SQLiteTracker

    spec = (
        spec_or_slug
        if isinstance(spec_or_slug, ProductSpec)
        else get_product(str(spec_or_slug))
    )
    if spec is None or spec.slug == "tradeos":
        return get_default_tracker()
    return SQLiteTracker(
        db_path=spec.db_path,
        product_default=f"product:{spec.slug}",
    )


def product_trackers() -> List[Tuple[ProductSpec, Any]]:
    """(spec, tracker) for every discovered product, in display order."""
    return [(spec, product_tracker(spec)) for spec in discover_products()]


def prefixed_task_id(slug: str, raw_id: Any) -> str:
    spec = get_product(slug)
    prefix = spec.prefix if spec else str(slug)
    return f"{prefix}-{raw_id}"


def split_task_id(task_id: str) -> Tuple[str, str]:
    """``"wl-3"`` → ``("worklane", "3")``; bare ids → tradeos.

    Unknown prefixes fall back to tradeos with the id untouched, matching
    the legacy behavior of ``parse_surface_task_id``.
    """
    s = str(task_id or "").strip()
    if "-" in s:
        prefix, rest = s.split("-", 1)
        if rest:
            if prefix == "o":  # legacy retired ops store
                return "ops", rest
            for spec in discover_products():
                if spec.prefix == prefix:
                    return spec.slug, rest
    return "tradeos", s
