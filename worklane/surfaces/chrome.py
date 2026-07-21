"""Brand constants for WorkLane / ProtocolCity mode (wl-222).

Extracted from task_server so surfaces and api modules can import without
pulling in the full server module.
"""

from __future__ import annotations

import os

# WL_BRAND=city (suite install) or standalone (public WorkLane install).
# Internal checkout defaults to "city"; public export flips to "standalone"
# (wl-134).
_BRAND_MODE: str = os.environ.get("WL_BRAND", "city")

# Sixth naming amendment (2026-07-15): city D0 mast is "[Folder] Desk".
_BRAND_NAME: str = (
    "ProtocolCity — Desk · Tickets" if _BRAND_MODE == "city" else "WorkLane — Tickets"
)

_BRAND_SUBTITLE: str = (
    "ProtocolCity · powered by WorkLane" if _BRAND_MODE == "city"
    else "the work-order desk"
)

_BRAND_HEADER_HTML: str = (
    "<span class='brand-room'>DESK</span>" if _BRAND_MODE == "city"
    else "WORKLANE — <span class='brand-room'>TICKETS</span>"
)
