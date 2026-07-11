"""Product registry — one product, one SQLite ticket store.

WL treats every ``<slug>.db`` file under the runtime data dir as an
independent product ticket store. Products stay separate by construction:
each has its own DB file, and the Pool UI renders one surface tab per
discovered product plus a merged read-only "All" view. Dropping a new
``<slug>.db`` into the data dir (or creating a ticket via the API with
``surface=<slug>``) is all it takes for a product to appear.

Composite task ids namespace tickets across stores in merged views:
``<prefix>-<rowid>`` (e.g. ``t-1095`` for tradeos, ``wl-3`` for
worklane). Bare numeric ids resolve to the configured default
product (see :func:`default_product_slug`) for backward compatibility.
"""

from __future__ import annotations

import fnmatch
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
# Stems ending in ``_archive`` are cold companion DBs (wl-23 archival) —
# never product surfaces themselves.
_IGNORED_DB_STEMS = {"ops_tickets"}

# Backup/scratch artifacts that land in the data dir (a pre-write sqlite
# backup, a dry-run decoy) are not product surfaces (wl-78 incident:
# tradeos.pre-tp7-backfill.<ts>.db was discovered as a phantom product).
# A slug matching one of these globs is still discovered if an operator
# has explicitly registered it in the products.json config overlay.
_SCRATCH_DB_GLOBS = ("*.pre-*", "*.backup*", "*bak*", "zzz*")


def _is_scratch_db_stem(stem: str) -> bool:
    """True when ``stem`` looks like a backup/scratch artifact, not a
    live product store."""
    return any(fnmatch.fnmatch(stem, pat) for pat in _SCRATCH_DB_GLOBS)


def _is_product_db_stem(stem: str) -> bool:
    """True when ``stem`` is eligible as a product store name."""
    s = (stem or "").strip().lower()
    if not s or s in _IGNORED_DB_STEMS:
        return False
    if s.endswith("_archive"):
        return False
    if _is_scratch_db_stem(s) and s not in _config_overrides():
        return False
    return True


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

    Shape: ``{"<slug>": {"display": "...", "prefix": "..."}, "default": "<slug>"}``.
    Per-slug entries win over the shipped ``_KNOWN_PRODUCT_META`` defaults;
    absent keys fall through. The top-level ``"default"`` string key is the
    host's bootstrap-default product (see :func:`default_product_slug`).
    Surfaced (and eventually edited) via /admin/settings.
    """
    return wl_data_dir().parent / "config" / "products.json"


def _raw_products_config() -> Dict[str, Any]:
    cfg = products_config_path()
    try:
        if cfg.exists():
            raw = json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass  # malformed overlay never takes the board down
    return {}


def _config_overrides() -> Dict[str, Dict[str, str]]:
    return {
        str(k).strip().lower(): v
        for k, v in _raw_products_config().items()
        if isinstance(v, dict)
    }


def default_product_slug_with_source() -> Tuple[str, str]:
    """Resolve the host's bootstrap-default product slug and where it came from.

    Order: ``WL_DEFAULT_PRODUCT`` env (explicit, always wins), the
    ``"default"`` key in the ``products.json`` config overlay (an operator's
    persisted choice), ``WL_PRODUCT`` env (the client-scoping convention,
    used here only as a fresh-install fallback when no config overlay has
    picked a default yet), then the first product discovered on disk.

    The config overlay outranks ``WL_PRODUCT`` deliberately: ``WL_PRODUCT``
    is a *client* identity var (which store a given CLI/MCP session talks
    to), not server config. A lane exporting it and then restarting the
    server (wl-68, 2026-07-11) must not silently override an operator's
    configured default — that flips routing for every other client. Fresh
    installs with no ``products.json`` yet still get a sane default via the
    ``WL_PRODUCT`` fallback, and ``WL_DEFAULT_PRODUCT`` remains available as
    an explicit, intentional override for either case.

    Empty slug only on a fresh install with nothing configured and nothing
    on disk yet — callers treat that as "no default product" rather than
    substituting a literal.
    """
    val = (os.environ.get("WL_DEFAULT_PRODUCT") or "").strip().lower()
    if val:
        return val, "env:WL_DEFAULT_PRODUCT"
    configured = _raw_products_config().get("default")
    if isinstance(configured, str) and configured.strip():
        return configured.strip().lower(), "config:products.json"
    val = (os.environ.get("WL_PRODUCT") or "").strip().lower()
    if val:
        return val, "env:WL_PRODUCT (fresh-install fallback)"
    data = wl_data_dir()
    if data.is_dir():
        found = sorted(
            p.stem.strip().lower()
            for p in data.glob("*.db")
            if _is_product_db_stem(p.stem)
        )
        if found:
            return found[0], "disk:first-discovered"
    return "", "none"


def default_product_slug() -> str:
    """Resolve the host's bootstrap-default product slug — no code literal.

    See :func:`default_product_slug_with_source` for the resolution order
    and rationale; this is the slug-only convenience most callers want.
    """
    return default_product_slug_with_source()[0]


def live_feed_product_slug() -> str:
    """Slug of the product allowed to serve tickets from a live HTTP feed
    instead of its local SQLite store (see
    ``task_server._tradeos_tickets_use_http_feed``).

    This is a documented host-specific integration point (wl-59), not a
    generic upstream-feed abstraction — generalize only if a second host
    ever needs an equivalent feed. Configurable via the
    ``WL_LIVE_FEED_PRODUCT`` env var or the ``live_feed_product`` key in
    the ``products.json`` config overlay; defaults to ``"tradeos"`` (the
    only host that has ever used this feature) so an unconfigured install
    behaves exactly as before.
    """
    override = (os.environ.get("WL_LIVE_FEED_PRODUCT") or "").strip().lower()
    if override:
        return override
    configured = _raw_products_config().get("live_feed_product")
    if isinstance(configured, str) and configured.strip():
        return configured.strip().lower()
    return "tradeos"


def register_product_meta(
    slug: str, display: Optional[str] = None, prefix: Optional[str] = None
) -> None:
    """Persist a display/prefix override for ``slug`` into the config overlay.

    Merges into the existing ``local/config/products.json`` (creating it if
    absent) rather than replacing it, so unrelated overrides — including the
    top-level ``"default"`` key — survive.
    """
    cfg = products_config_path()
    raw = _raw_products_config()
    entry = dict(raw.get(slug) or {}) if isinstance(raw.get(slug), dict) else {}
    if display:
        entry["display"] = display
    if prefix:
        entry["prefix"] = prefix
    if not entry:
        return
    raw[slug] = entry
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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

    Ordering: the configured default product first (see
    :func:`default_product_slug`), then alphabetical. Fresh from disk on
    every call — the server picks up a newly installed product DB without
    a restart. The default's spec is present even before its DB file
    exists on disk (its tracker honors env path overrides, e.g. tradeOS's
    ``WORKLANE_DB`` / ``TRADEOS_TRACKER_DB``).
    """
    default = default_product_slug()
    data = wl_data_dir()
    slugs: Dict[str, Path] = {}
    if default:
        slugs[default] = data / f"{default}.db"
    if data.is_dir():
        for p in sorted(data.glob("*.db")):
            stem = p.stem.strip().lower()
            if not _is_product_db_stem(stem):
                continue
            slugs.setdefault(stem, p)
    always_present = (default,) if default else ()
    ordered = [s for s in always_present if s in slugs] + sorted(
        s for s in slugs if s not in always_present
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

    The ``tradeos`` product specifically goes through
    :func:`get_default_tracker` so its adapter selection and DB-path env
    overrides (``TRADEOS_TRACKER`` / ``TRADEOS_TRACKER_DB``) keep working;
    every other product — including one that happens to be the
    *configured* default via :func:`default_product_slug` — binds SQLite
    directly to its own file. Comparing against the configured default
    instead of the literal ``"tradeos"`` would make any product whose
    slug matches that default silently collide with tradeos.db, since
    :func:`get_default_tracker` ignores ``spec.db_path`` entirely.
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
    """``"wl-3"`` → ``("worklane", "3")``; bare ids → the default product.

    Unknown prefixes fall back to the configured default product (see
    :func:`default_product_slug`) with the id untouched, matching the
    legacy behavior of ``parse_surface_task_id``.
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
    return default_product_slug(), s
