"""Close-path evidence checks registry (wl-339 / pc-961).

Projects may register deterministic checks in a machine-readable file.
When registered, implement-class close-outs must **cite** those checks in
the ``Verification:`` section (command/token + a result signal). The engine
does not run the commands — agents still execute them; this is the cheap
mechanical floor so "hand says done" becomes "check is named with a result."

Projects with no registration behave exactly as today (no extra close guard).

Registration surface (runtime overlay, same root as products.json)::

    local/config/closeout_checks.json

Shape::

    {
      "<product_slug>": {
        "checks": [
          {
            "id": "pytest",
            "tokens": ["pytest"],
            "description": "python3 -m pytest tests/ -q"
          }
        ],
        "exempt_labels": ["docs", "research", "notes", "teaching"]
      }
    }

``tokens`` are optional; when omitted the check ``id`` is the only match key.
Default exempt labels (docs / research / notes / teaching / umbrella / epic)
apply when ``exempt_labels`` is absent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from worklane.products import wl_data_dir

# Section body after "Verification:" until Links: / Follow-ups: / end.
_VERIFICATION_SECTION_RE = re.compile(
    r"(?im)^\s*Verification\s*:\s*(.*?)(?=^\s*Links\s*:|^\s*Follow-ups\s*:|\Z)",
    re.DOTALL | re.MULTILINE,
)

_CLOSEOUT_START_RE = re.compile(r"^\s*Completed\s*:", re.IGNORECASE)

# Result-ish signals agents commonly put next to a green check run.
_RESULT_SIGNAL_RE = re.compile(
    r"(?i)\b("
    r"green|pass(?:ed)?|ok|clean|"
    r"0\s+failed|exit\s*0|"
    r"\d+\s+passed|"
    r"all\s+passed|"
    r"no\s+failures?"
    r")\b"
)

# Built-in exempt classes: docs/notes and coordination wrappers.
_DEFAULT_EXEMPT_LABELS = frozenset(
    {
        "docs",
        "research",
        "notes",
        "teaching",
        "umbrella",
        "epic",
    }
)

CHECKS_HINT_PREFIX = (
    "Verification: must cite registered close-out check(s) with a result "
    "(wl-339) — "
)


@dataclass(frozen=True)
class CloseoutCheck:
    """One registered deterministic check for a product."""

    id: str
    tokens: Tuple[str, ...]
    description: str = ""

    def match_keys(self) -> Tuple[str, ...]:
        keys = [self.id]
        for t in self.tokens:
            if t and t.lower() not in {k.lower() for k in keys}:
                keys.append(t)
        return tuple(keys)


@dataclass(frozen=True)
class ProductCloseoutChecks:
    """Per-product close-out check registry entry."""

    product: str
    checks: Tuple[CloseoutCheck, ...]
    exempt_labels: frozenset


def closeout_checks_config_path() -> Path:
    """Operator overlay: ``local/config/closeout_checks.json``."""
    return wl_data_dir().parent / "config" / "closeout_checks.json"


def _parse_check_entry(raw: Any) -> Optional[CloseoutCheck]:
    if not isinstance(raw, dict):
        return None
    cid = str(raw.get("id") or "").strip()
    if not cid:
        return None
    tokens_raw = raw.get("tokens")
    tokens: List[str] = []
    if isinstance(tokens_raw, list):
        for t in tokens_raw:
            s = str(t or "").strip()
            if s:
                tokens.append(s)
    desc = str(raw.get("description") or "").strip()
    return CloseoutCheck(id=cid, tokens=tuple(tokens), description=desc)


def _parse_product_entry(
    product: str, raw: Any
) -> Optional[ProductCloseoutChecks]:
    if not isinstance(raw, dict):
        return None
    checks_raw = raw.get("checks")
    if not isinstance(checks_raw, list) or not checks_raw:
        return None
    checks: List[CloseoutCheck] = []
    for item in checks_raw:
        c = _parse_check_entry(item)
        if c is not None:
            checks.append(c)
    if not checks:
        return None
    exempt_raw = raw.get("exempt_labels")
    if isinstance(exempt_raw, list):
        exempt = frozenset(
            str(x).strip().lower()
            for x in exempt_raw
            if str(x or "").strip()
        )
    else:
        exempt = _DEFAULT_EXEMPT_LABELS
    return ProductCloseoutChecks(
        product=product.strip().lower(),
        checks=tuple(checks),
        exempt_labels=exempt,
    )


def load_closeout_checks_registry(
    path: Optional[Path] = None,
) -> Dict[str, ProductCloseoutChecks]:
    """Load per-product checks from the overlay file (empty if absent/bad)."""
    cfg = path if path is not None else closeout_checks_config_path()
    try:
        if not cfg.is_file():
            return {}
        raw = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, ProductCloseoutChecks] = {}
    for key, val in raw.items():
        slug = str(key or "").strip().lower()
        if not slug or slug in ("default", "live_feed_product"):
            continue
        entry = _parse_product_entry(slug, val)
        if entry is not None:
            out[slug] = entry
    return out


def get_product_closeout_checks(
    product: str,
    registry: Optional[Dict[str, ProductCloseoutChecks]] = None,
) -> Optional[ProductCloseoutChecks]:
    """Return registered checks for *product*, or None when unregistered."""
    slug = (product or "").strip().lower()
    if not slug:
        return None
    reg = registry if registry is not None else load_closeout_checks_registry()
    return reg.get(slug)


def extract_verification_section(body: str) -> str:
    """Return the Verification: section body from a §5 close-out, or ''."""
    m = _VERIFICATION_SECTION_RE.search(body or "")
    if not m:
        return ""
    return (m.group(1) or "").strip()


def _labels_set(labels: Optional[Iterable[str]]) -> frozenset:
    if not labels:
        return frozenset()
    return frozenset(str(x).strip().lower() for x in labels if str(x or "").strip())


def is_exempt_from_checks(
    labels: Optional[Iterable[str]],
    entry: ProductCloseoutChecks,
) -> bool:
    """True when any task label is in the product's exempt set."""
    lab = _labels_set(labels)
    if not lab:
        return False
    return bool(lab & entry.exempt_labels)


def _token_in_text(token: str, text: str) -> bool:
    t = (token or "").strip().lower()
    if not t:
        return False
    # Word-ish boundary for short ids; substring for multi-word tokens.
    if re.search(r"^[a-z0-9_.:-]+$", t):
        return re.search(r"(?i)\b" + re.escape(t) + r"\b", text or "") is not None
    return t in (text or "").lower()


def missing_check_cites(
    verification_text: str,
    checks: Sequence[CloseoutCheck],
) -> List[str]:
    """Return check ids not cited in *verification_text*."""
    text = verification_text or ""
    missing: List[str] = []
    for c in checks:
        if not any(_token_in_text(k, text) for k in c.match_keys()):
            missing.append(c.id)
    return missing


def verification_missing_result_signal(verification_text: str) -> bool:
    """True when Verification has no pass/fail/green-style result token."""
    return _RESULT_SIGNAL_RE.search(verification_text or "") is None


def verification_checks_violation(
    verification_text: str,
    product: str,
    labels: Optional[Iterable[str]] = None,
    registry: Optional[Dict[str, ProductCloseoutChecks]] = None,
) -> Optional[str]:
    """Return error when Verification fails registered-check cite rules.

    No registration for the product → None (today's behavior).
    Exempt labels → None.
    """
    entry = get_product_closeout_checks(product, registry=registry)
    if entry is None or not entry.checks:
        return None
    if is_exempt_from_checks(labels, entry):
        return None

    missing = missing_check_cites(verification_text, entry.checks)
    no_result = verification_missing_result_signal(verification_text)
    if not missing and not no_result:
        return None

    parts: List[str] = []
    if missing:
        parts.append(
            "cite each registered check in Verification: "
            + ", ".join(missing)
        )
    if no_result:
        parts.append(
            "include a result signal (e.g. green, passed, 0 failed, exit 0)"
        )
    registered = ", ".join(c.id for c in entry.checks)
    return (
        CHECKS_HINT_PREFIX
        + "; ".join(parts)
        + f" (product={entry.product}; registered: {registered}). "
        "Register/clear checks in local/config/closeout_checks.json."
    )


def closeout_checks_violation(
    body: str,
    product: str,
    labels: Optional[Iterable[str]] = None,
    registry: Optional[Dict[str, ProductCloseoutChecks]] = None,
) -> Optional[str]:
    """If *body* is a Completed: close-out, enforce registered checks.

    Non-close-out comments return None. Missing Verification: is handled by
    the existing §5 section guard — this only runs when the section is present
    or when callers pass a structured Verification field via
    :func:`verification_checks_violation`.
    """
    text = body or ""
    first_line = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
    if not _CLOSEOUT_START_RE.match(first_line):
        return None
    # Defer to section guard when Verification: is absent entirely.
    if "Verification:" not in text and "verification:" not in text.lower():
        return None
    return verification_checks_violation(
        extract_verification_section(text),
        product,
        labels=labels,
        registry=registry,
    )
