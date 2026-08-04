"""Retired CLI entry-point shims (wl-384).

``wl`` used to share the real CLI main. That was a silent alias of ``wl`` and
kept retired vocabulary discoverable on PATH. The shim prints the ``wl``
equivalent and exits nonzero so nothing retired still executes silently.

Do not re-add a ``tk`` console_script (wl-327 ruling B / wl-342) — not even as
a shim. Fresh installs simply omit it.
"""
from __future__ import annotations

import sys
from typing import List, Optional


def _retired_message(invoked: str, argv: List[str]) -> str:
    rest = " ".join(argv).strip()
    if rest:
        equivalent = "wl " + rest
    else:
        equivalent = "wl"
    return (
        f"error: '{invoked}' is retired — use '{equivalent}' "
        f"(long form: worklane). See MARKETING.md name registry."
    )


def main(argv: Optional[List[str]] = None) -> None:
    """Entry for the retired ``wl`` console_script (and any future shims)."""
    if argv is None:
        argv = sys.argv[1:]
    invoked = "wl"
    # When installed as a console script, sys.argv[0] is the binary name.
    if sys.argv and sys.argv[0]:
        base = sys.argv[0].rsplit("/", 1)[-1]
        if base in ("wl", "tk"):
            invoked = base
    print(_retired_message(invoked, list(argv)), file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
