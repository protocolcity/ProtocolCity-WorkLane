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
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TextIO, Tuple


# Slugs with curated display names / short id prefixes. Anything else
# discovered on disk falls back to (title-cased slug, slug) so a new
# product needs zero code here.
_KNOWN_PRODUCT_META: Dict[str, Tuple[str, str]] = {
    "tradeos": ("tradeOS", "t"),
    # wl-207: canonical host store is worklane (wl-); wl- resolves via legacy_prefixes
    "worklane": ("WorkLane", "wl"),
    "worklane": ("WorkLane", "wl"),  # legacy slug only if old .db still present
}

# Legacy stores that are not product surfaces. ``ops_tickets`` is the
# retired Ops Cockpit store (empty; surface removed from the UI).
# ``register`` is the pre-cutover OneSeoPOS store (2026-08-03): live surface
# is ``oneseo-pos`` / ``osp-`` with ``legacy_prefixes: ["regi"]``. Empty
# register.db must not reappear as a Map/doctor project row.
# Stems ending in ``_archive`` are cold companion DBs (wl-23 archival) —
# never product surfaces themselves.
_IGNORED_DB_STEMS = {"ops_tickets", "register"}

# Backup/scratch artifacts that land in the data dir (a pre-write sqlite
# backup, a dry-run decoy) are not product surfaces (wl-78 incident:
# tradeos.pre-tp7-backfill.<ts>.db was discovered as a phantom product).
# A slug matching one of these globs is still discovered if an operator
# has explicitly registered it in the products.json config overlay.
_SCRATCH_DB_GLOBS = ("*.pre-*", "*.backup*", "*bak*", "zzz*")

# Same grammar as POST /api/admin/products (wl-12). Discovery must refuse
# anything outside it — sync/copy collision suffixes like
# ``protocolcity 992.db`` / ``worklane 348.db`` (space + digits) otherwise
# register as bogus products (wl-377).
_PRODUCT_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


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
    # wl-377: refuse collision-suffixed / non-slug stems (whitespace, dots,
    # leading digits, over-long names). Explicit products.json registration
    # cannot override this — a stem outside slug grammar is never a product.
    if not _PRODUCT_SLUG_RE.match(s):
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
    legacy_prefixes: Tuple[str, ...] = ()  # retired prefixes that still resolve here (wl-152)


def _is_source_checkout() -> bool:
    """True when this package is running from a git checkout (repo root has
    a ``.git``) rather than an installed package (e.g. site-packages after
    ``pip install``)."""
    return (Path(__file__).resolve().parents[1] / ".git").exists()


def runtime_dir_override() -> str:
    """Absolute override path from WORKLANE_RUNTIME_DIR or WORKLANE_RUNTIME_DIR.

    Empty string when neither env is set — callers treat that as "use
    package/checkout defaults" rather than an operator pin.
    """
    return (
        os.environ.get("WORKLANE_RUNTIME_DIR")
        or os.environ.get("WORKLANE_RUNTIME_DIR")
        or ""
    ).strip()


def wl_data_dir() -> Path:
    """Runtime data dir (honors WORKLANE_RUNTIME_DIR; or WORKLANE_RUNTIME_DIR).

    Source checkouts keep the existing in-repo default so hosts already
    running from a checkout see no change. An installed package (wl-124:
    no ``.git`` at the repo root, e.g. a ``pip install`` of the exported
    package) falls back to a user-level directory instead of writing
    inside site-packages, where it would be wiped on reinstall/upgrade.
    """
    override = runtime_dir_override()
    if override:
        return Path(override) / "data"
    if _is_source_checkout():
        return Path(__file__).parent / "local" / "data"
    return Path.home() / ".worklane" / "data"


def runtime_root() -> Path:
    """Resolved runtime root (parent of the data dir).

    When WORKLANE_RUNTIME_DIR / WORKLANE_RUNTIME_DIR is set this
    is that path; otherwise it is the parent of the package/user data dir.
    Surfaced in unknown-product errors so a miswired override is
    self-diagnosing (wl-374).
    """
    return wl_data_dir().parent


# One-shot empty-override boot warning (wl-374). Process-global so server
# + MCP + first tool path share a single loud line rather than spamming.
_empty_override_warned: bool = False


def on_disk_product_db_stems() -> List[str]:
    """Sorted stems of real product ``.db`` files under the data dir.

    Scratch/ignored stems are filtered the same way as discovery — a
    backup artifact does not count as a live store.
    """
    data = wl_data_dir()
    if not data.is_dir():
        return []
    return sorted(
        p.stem.strip().lower()
        for p in data.glob("*.db")
        if _is_product_db_stem(p.stem)
    )


def is_empty_runtime_override() -> bool:
    """True when an explicit RUNTIME_DIR override is set but the store is empty.

    Miswire pattern (wl-374): city-generated ``.mcp.json`` pins
    WORKLANE_RUNTIME_DIR / WORKLANE_RUNTIME_DIR at an empty
    directory while the live multi-product store lives in the source
    checkout. Without a config/products.json and without any product
    ``.db`` on disk, discovery still surfaces the built-in default
    (tradeos) — callers then see ``unknown product 'X'; known: ['tradeos']``
    with no hint the override is wrong.

    Returns False when no override is set (package/checkout defaults are
    not "miswired"), when products.json exists, or when at least one
    product store file is on disk.
    """
    if not runtime_dir_override():
        return False
    if products_config_path().is_file():
        return False
    return not on_disk_product_db_stems()


def empty_runtime_override_warning() -> Optional[str]:
    """Human-readable warning when :func:`is_empty_runtime_override`, else None."""
    if not is_empty_runtime_override():
        return None
    root = runtime_root()
    data = wl_data_dir()
    override = runtime_dir_override()
    return (
        f"WARNING: empty RUNTIME_DIR override at {root} "
        f"(env pin: {override}). No config/products.json and no product "
        f".db files under {data}. Registry will only serve the built-in "
        f"default (tradeos) — other slugs fail with 'unknown product'. "
        f"Point WORKLANE_RUNTIME_DIR / WORKLANE_RUNTIME_DIR at "
        f"the live checkout runtime root (…/worklane/local) or "
        f"bootstrap stores into {data}."
    )


def emit_empty_runtime_override_warning(
    stream: Optional[TextIO] = None,
) -> bool:
    """Print the empty-override warning once per process. Returns True if emitted."""
    global _empty_override_warned
    msg = empty_runtime_override_warning()
    if not msg or _empty_override_warned:
        return False
    _empty_override_warned = True
    print(msg, file=stream if stream is not None else sys.stderr)
    return True


def reset_empty_runtime_override_warning_for_tests() -> None:
    """Test helper: allow the one-shot warning to fire again."""
    global _empty_override_warned
    _empty_override_warned = False


def unknown_product_message(
    slug: str, known: Optional[List[str]] = None
) -> str:
    """Self-diagnosing unknown-product / unknown-surface error (wl-374).

    Always names the resolved runtime root so a miswired override is
    obvious. When the empty-override pattern is active, appends an
    explicit bootstrap/miswire hint.
    """
    known_list = list(known) if known is not None else (product_slugs() or ["tradeos"])
    base = (
        f"unknown product {slug!r}; known: {known_list}; "
        f"runtime_dir={runtime_root()}"
    )
    if is_empty_runtime_override():
        base += (
            f" (empty RUNTIME_DIR override — no products.json / product "
            f".dbs under {wl_data_dir()}; check WORKLANE_RUNTIME_DIR / "
            f"WORKLANE_RUNTIME_DIR)"
        )
    return base


def products_config_path() -> Path:
    """Operator overlay for product metadata: ``local/config/products.json``.

    Shape: ``{"<slug>": {"display": "...", "prefix": "...", "legacy_prefixes":
    [...]}, "default": "<slug>"}``. Per-slug entries win over the shipped
    ``_KNOWN_PRODUCT_META`` defaults; absent keys fall through. ``legacy_prefixes``
    is a list of retired id prefixes that still resolve to this slug forever
    (wl-152) — see :func:`_legacy_prefix_map` and :func:`split_task_id`. The
    top-level ``"default"`` string key is the host's bootstrap-default product
    (see :func:`default_product_slug`). Surfaced (and eventually edited) via
    /admin/settings.
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

    Order: ``WL_DEFAULT_PROJECT`` env (preferred canonical, always wins),
    ``WL_DEFAULT_PRODUCT`` (silent back-compat alias for the same),
    ``WL_DEFAULT_PROJECT`` / ``WL_DEFAULT_PRODUCT`` (legacy aliases, still read),
    the ``"default"`` key in the ``products.json`` config overlay (an operator's
    persisted choice), ``WL_PROJECT`` / ``WL_PRODUCT`` / ``WL_PROJECT`` /
    ``WL_PRODUCT`` env (the client-scoping convention, used here only as a
    fresh-install fallback when no config overlay has picked a default yet),
    then the first product discovered on disk.

    The config overlay outranks ``WL_PROJECT`` / ``WL_PROJECT`` deliberately:
    those are *client* identity vars (which store a given CLI/MCP session talks
    to), not server config. A lane exporting them and then restarting the
    server (wl-68, 2026-07-11) must not silently override an operator's
    configured default — that flips routing for every other client. Fresh
    installs with no ``products.json`` yet still get a sane default via the
    ``WL_PROJECT`` / ``WL_PROJECT`` fallback, and ``WL_DEFAULT_PROJECT`` /
    ``WL_DEFAULT_PROJECT`` remain available as explicit, intentional overrides.

    Empty slug only on a fresh install with nothing configured and nothing
    on disk yet — callers treat that as "no default product" rather than
    substituting a literal.
    """
    val = (
        os.environ.get("WL_DEFAULT_PROJECT")
        or os.environ.get("WL_DEFAULT_PRODUCT")
        or os.environ.get("WL_DEFAULT_PROJECT")
        or os.environ.get("WL_DEFAULT_PRODUCT")
        or ""
    ).strip().lower()
    if val:
        if os.environ.get("WL_DEFAULT_PROJECT"):
            src = "WL_DEFAULT_PROJECT"
        elif os.environ.get("WL_DEFAULT_PRODUCT"):
            src = "WL_DEFAULT_PRODUCT"
        elif os.environ.get("WL_DEFAULT_PROJECT"):
            src = "WL_DEFAULT_PROJECT"
        else:
            src = "WL_DEFAULT_PRODUCT"
        return val, f"env:{src}"
    configured = _raw_products_config().get("default")
    if isinstance(configured, str) and configured.strip():
        return configured.strip().lower(), "config:products.json"
    val = (
        os.environ.get("WL_PROJECT")
        or os.environ.get("WL_PRODUCT")
        or os.environ.get("WL_PROJECT")
        or os.environ.get("WL_PRODUCT")
        or ""
    ).strip().lower()
    if val:
        if os.environ.get("WL_PROJECT"):
            src = "WL_PROJECT"
        elif os.environ.get("WL_PRODUCT"):
            src = "WL_PRODUCT"
        elif os.environ.get("WL_PROJECT"):
            src = "WL_PROJECT"
        else:
            src = "WL_PRODUCT"
        return val, f"env:{src} (fresh-install fallback)"
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
    ``WL_LIVE_FEED_PRODUCT`` env var (or ``WL_LIVE_FEED_PRODUCT``) or the ``live_feed_product`` key in
    the ``products.json`` config overlay; defaults to ``"tradeos"`` (the
    only host that has ever used this feature) so an unconfigured install
    behaves exactly as before.
    """
    override = (os.environ.get("WL_LIVE_FEED_PRODUCT") or os.environ.get("WL_LIVE_FEED_PRODUCT") or "").strip().lower()
    if override:
        return override
    configured = _raw_products_config().get("live_feed_product")
    if isinstance(configured, str) and configured.strip():
        return configured.strip().lower()
    return "tradeos"


def register_product_meta(
    slug: str,
    display: Optional[str] = None,
    prefix: Optional[str] = None,
    add_legacy_prefix: Optional[str] = None,
) -> None:
    """Persist a display/prefix override for ``slug`` into the config overlay.

    Merges into the existing ``local/config/products.json`` (creating it if
    absent) rather than replacing it, so unrelated overrides — including the
    top-level ``"default"`` key — survive. ``add_legacy_prefix``, when given,
    appends (dedup, sorted) to ``slug``'s ``legacy_prefixes`` list instead of
    replacing it — the PATCH prefix-rename endpoint uses this to keep a
    retired prefix resolving forever (wl-152).
    """
    cfg = products_config_path()
    raw = _raw_products_config()
    entry = dict(raw.get(slug) or {}) if isinstance(raw.get(slug), dict) else {}
    if display:
        entry["display"] = display
    if prefix:
        entry["prefix"] = prefix
    if add_legacy_prefix:
        legacy = str(add_legacy_prefix).strip().lower()
        if legacy:
            existing = entry.get("legacy_prefixes")
            current = set(existing) if isinstance(existing, list) else set()
            current.add(legacy)
            entry["legacy_prefixes"] = sorted(current)
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
    raw_legacy = over.get("legacy_prefixes")
    legacy_prefixes: Tuple[str, ...] = ()
    if isinstance(raw_legacy, list):
        legacy_prefixes = tuple(
            sorted({str(p).strip().lower() for p in raw_legacy if str(p).strip()})
        )
    return ProductSpec(
        slug=slug,
        display=display,
        prefix=prefix,
        db_path=db_path,
        legacy_prefixes=legacy_prefixes,
    )


def _legacy_prefix_map() -> Dict[str, str]:
    """Legacy id prefix -> target slug, live product or not.

    Seeded with the shipped ``"o"`` -> ``"ops"`` default (the retired Ops
    Cockpit store has no on-disk ``.db`` and so never appears in
    :func:`discover_products`), then merged with every ``legacy_prefixes``
    list declared in the config overlay — including overlay entries for
    slugs with no live store, so a product can carry retired aliases even
    after its own DB is gone.
    """
    mapping: Dict[str, str] = {"o": "ops"}
    for slug, entry in _config_overrides().items():
        raw = entry.get("legacy_prefixes")
        if not isinstance(raw, list):
            continue
        for p in raw:
            p = str(p).strip().lower()
            if p:
                mapping[p] = slug
    return mapping


def prefix_collisions() -> List[Dict[str, Any]]:
    """Overlay-declared prefix collisions, for operator visibility (wl-151).

    :func:`discover_products` already RESOLVES collisions deterministically
    (the later store falls back to its slug as prefix), so nothing
    mis-routes — but the operator who hand-edited ``products.json`` never
    learns the overlay is bad. This reports what discovery had to fix:
    one entry per contested prefix, naming the declared owner set and the
    fallback in effect. Empty list = healthy registry.
    """
    default = default_product_slug()
    data = wl_data_dir()
    slugs: Dict[str, Path] = {}
    if default:
        slugs[default] = data / f"{default}.db"
    if data.is_dir():
        for p in sorted(data.glob("*.db")):
            stem = p.stem.strip().lower()
            if _is_product_db_stem(stem):
                slugs.setdefault(stem, p)
    declared: Dict[str, List[str]] = {}
    for s in sorted(slugs):
        declared.setdefault(_spec_for_slug(s, slugs[s]).prefix, []).append(s)
    legacy = _legacy_prefix_map()
    out: List[Dict[str, Any]] = []
    for prefix, owners in sorted(declared.items()):
        legacy_owner = legacy.get(prefix)
        contested = len(owners) > 1 or (
            legacy_owner is not None and any(s != legacy_owner for s in owners)
        )
        if not contested:
            continue
        resolved = {s.slug: s.prefix for s in discover_products() if s.slug in owners}
        out.append({
            "prefix": prefix,
            "slugs": owners,
            "legacy_owner": legacy_owner,
            "resolved": resolved,
        })
    return out


def all_taken_prefixes(exclude_slug: Optional[str] = None) -> Set[str]:
    """Every prefix — live or legacy — already claimed by a product other
    than ``exclude_slug``. Used by the product create/rename endpoints so a
    new live prefix can't shadow another store's retired alias, and a
    retired alias can't shadow another store's live prefix (wl-151/wl-152
    collision guard)."""
    taken: Set[str] = set()
    for spec in discover_products():
        if spec.slug == exclude_slug:
            continue
        taken.add(spec.prefix)
        taken.update(spec.legacy_prefixes)
    for prefix, target_slug in _legacy_prefix_map().items():
        if target_slug != exclude_slug:
            taken.add(prefix)
    return taken


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
    taken_prefixes = set(_legacy_prefix_map().keys())  # reserved: "o" + declared legacy aliases
    for s in ordered:
        spec = _spec_for_slug(s, slugs[s])
        if spec.prefix in taken_prefixes:
            # Collision (bad overlay or clashing slug): fall back to the
            # slug itself, which is unique by construction.
            spec = ProductSpec(
                slug=spec.slug, display=spec.display,
                prefix=spec.slug, db_path=spec.db_path,
                legacy_prefixes=spec.legacy_prefixes,
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


def known_prefix_slug(task_id: str) -> Optional[str]:
    """Return the product slug when ``task_id`` has a known live/legacy prefix.

    ``"wl-328"`` → ``"worklane"``; ``"wl-3"`` → worklane (or worklane
    if that .db still exists) via legacy map; bare ``"328"`` → ``None``.
    Unknown hyphenated tokens (``"zz-9"``) also return ``None`` — they are
    not addressable composites.
    """
    s = str(task_id or "").strip()
    if "-" not in s:
        return None
    prefix, rest = s.split("-", 1)
    if not rest:
        return None
    for spec in discover_products():
        if spec.prefix == prefix:
            return spec.slug
    return _legacy_prefix_map().get(prefix)


def split_task_id(task_id: str) -> Tuple[str, str]:
    """``"wl-3"`` → ``("worklane", "3")``; bare ids → the default product.

    Resolves live prefixes first, then legacy aliases (see
    :func:`_legacy_prefix_map` — includes the shipped ``"o"`` -> ``"ops"``
    default plus any per-slug ``legacy_prefixes`` from the config overlay,
    wl-152), so a retired prefix keeps resolving to its store forever.
    Unknown prefixes fall back to the configured default product (see
    :func:`default_product_slug`) with the id untouched, matching the
    legacy behavior of ``parse_surface_task_id``.
    """
    s = str(task_id or "").strip()
    slug = known_prefix_slug(s)
    if slug is not None:
        _prefix, rest = s.split("-", 1)
        return slug, rest
    return default_product_slug(), s


def resolve_write_task_id(
    task_id: str, project: Optional[str] = None
) -> Tuple[str, str]:
    """Resolve ``(product_slug, raw_id)`` for **write** operations (wl-344).

    Hard guarantee against default-store bleed:

    * known composite id (``wl-328``) → store from prefix; optional
      ``project`` must match when given
    * bare / unknown id → requires explicit ``project`` (never the
      configured default alone)
    * composite prefix + mismatched ``project`` → ``ValueError``

    Raises ``ValueError`` with a caller-safe message on violation.
    """
    tid = str(task_id or "").strip()
    if not tid:
        raise ValueError("task_id is required")
    explicit = str(project or "").strip().lower() or None
    if explicit == "all":
        raise ValueError(
            "project='all' is not valid for write ops — pass a concrete store "
            "or a composite task id"
        )
    prefix_slug = known_prefix_slug(tid)
    if prefix_slug is not None:
        _prefix, rest = tid.split("-", 1)
        if explicit and explicit != prefix_slug:
            raise ValueError(
                f"task_id {tid!r} belongs to product {prefix_slug!r}, "
                f"not {explicit!r}"
            )
        return prefix_slug, rest
    if not explicit:
        raise ValueError(
            f"task_id {tid!r} is not a composite id and no project= was "
            "passed — pass a composite id (e.g. wl-328) or project=<slug> "
            "to prevent default-store bleed (wl-344)"
        )
    return explicit, tid
