"""Rendering helpers for the WL admin UI.

Vendored from core.web.utils.common (tradeOS) to break the cross-repo
Python import dependency — ADR-025 cord-cut Phase 1a, ticket #406.

Only the subset of CSS that the WL admin UI actually needs is included.
tradeOS-specific component CSS (nav, cards, modals, etc.) is NOT copied.
WL-specific ``ops-*`` CSS stays inline in task_server.py.

When the Phase 2 repo split happens, update the import path in task_server.py
from ``worklane.rendering`` to the WL-native module in its own repo.
"""

from __future__ import annotations

import re
from typing import List


# ── HTML helpers ─────────────────────────────────────────────────────────────


def _esc(s: str) -> str:
    """HTML-escape a string."""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ── Markdown renderer (wl-27) ────────────────────────────────────────────────
#
# Dependency-light by design (PROTOCOL.md's docs surface must not add a pip
# dependency just to render the repo's own truth files) — a small line-based
# block parser plus a regex inline pass. Not CommonMark-complete: nested list
# indentation is flattened to one level, no images, no reference-style links.
# Sized to what PROTOCOL.md/TRUTH.md/README.md/CLAUDE.md actually use.

_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_HR_RE = re.compile(r"^(-{3,}|\*{3,})\s*$")
_MD_FENCE_RE = re.compile(r"^```")
_MD_UL_RE = re.compile(r"^[-*]\s+(.*)$")
_MD_OL_RE = re.compile(r"^\d+\.\s+(.*)$")
_MD_QUOTE_RE = re.compile(r"^>\s?(.*)$")


def _md_inline(text: str) -> str:
    """Render inline markdown (code/bold/italic/links) from already-raw text."""
    escaped = _esc(text)
    codes: List[str] = []

    def _stash_code(m: "re.Match[str]") -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    escaped = re.sub(r"`([^`]+)`", _stash_code, escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        escaped,
    )
    # Non-greedy `.+?` (not a `[^*]+` class) so bold spans that wrap a nested
    # italic (`**WL *is* right now:**`, seen in README.md/CLAUDE.md) still
    # find their real closing `**` instead of failing on the inner `*`.
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    # `_` emphasis follows CommonMark's intraword rule (word char must not sit
    # against the delimiter) so identifiers/filenames like HOST_PROFILE_TEMPLATE
    # or WORKLANE_DB — common in these docs, never backtick-quoted —
    # aren't torn apart as false emphasis.
    escaped = re.sub(r"(?<![\w_])__(.+?)__(?![\w_])", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(?<![\w_])_(.+?)_(?![\w_])", r"<em>\1</em>", escaped)
    escaped = re.sub(
        r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", escaped
    )
    return escaped


def _md_table(lines: List[str]) -> str:
    def _split_row(row: str) -> List[str]:
        return [c.strip() for c in row.strip().strip("|").split("|")]

    header = _split_row(lines[0])
    body_lines = lines[2:] if len(lines) > 1 else []
    thead = "".join(f"<th>{_md_inline(c)}</th>" for c in header)
    tbody = "".join(
        "<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in _split_row(row)) + "</tr>"
        for row in body_lines
        if row.strip()
    )
    return f"<table class='tos-table'><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>"


def render_markdown(text: str) -> str:
    """Render a markdown document to HTML (headers, fences, lists, tables, ...)."""
    lines = text.split("\n")
    n = len(lines)
    out: List[str] = []
    para: List[str] = []

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_md_inline(' '.join(para))}</p>")
            para.clear()

    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if _MD_FENCE_RE.match(stripped):
            flush_para()
            i += 1
            code_lines: List[str] = []
            while i < n and not _MD_FENCE_RE.match(lines[i].strip()):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            out.append(f"<pre><code>{_esc(chr(10).join(code_lines))}</code></pre>")
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        m = _MD_HEADER_RE.match(stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            out.append(f"<h{level}>{_md_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        if _MD_HR_RE.match(stripped):
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        if stripped.startswith("|") and stripped.count("|") >= 2:
            flush_para()
            table_lines = []
            while i < n and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            out.append(_md_table(table_lines))
            continue

        m = _MD_QUOTE_RE.match(stripped)
        if m:
            flush_para()
            quote_parts = [m.group(1)]
            i += 1
            while i < n:
                qm = _MD_QUOTE_RE.match(lines[i].strip())
                if not qm:
                    break
                quote_parts.append(qm.group(1))
                i += 1
            out.append(f"<blockquote><p>{_md_inline(' '.join(quote_parts))}</p></blockquote>")
            continue

        m = _MD_UL_RE.match(stripped)
        if m:
            flush_para()
            items = [m.group(1)]
            i += 1
            while i < n:
                im = _MD_UL_RE.match(lines[i].strip())
                if not im:
                    break
                items.append(im.group(1))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_md_inline(x)}</li>" for x in items) + "</ul>")
            continue

        m = _MD_OL_RE.match(stripped)
        if m:
            flush_para()
            items = [m.group(1)]
            i += 1
            while i < n:
                im = _MD_OL_RE.match(lines[i].strip())
                if not im:
                    break
                items.append(im.group(1))
                i += 1
            out.append("<ol>" + "".join(f"<li>{_md_inline(x)}</li>" for x in items) + "</ol>")
            continue

        para.append(stripped)
        i += 1

    flush_para()
    return "\n".join(out)


# ── Badge renderer ────────────────────────────────────────────────────────────

_BADGE_TIERS = {
    "critical", "warning", "info", "positive", "neutral", "long", "short",
}


def _badge(text: str, tier: str = "neutral") -> str:
    """Render a semantic status badge.

    `tier` must be one of: critical, warning, info, positive, neutral, long, short.
    """
    t = (tier or "neutral").strip().lower()
    if t not in _BADGE_TIERS:
        t = "neutral"
    return f"<span class='badge tier-{t}'>{_esc(str(text))}</span>"


# ── Design-system CSS (WL subset) ────────────────────────────────────────────

def _css() -> str:
    """Return the WL admin UI stylesheet.

    Includes: design tokens (dark + light theme), base reset, typography,
    .page, .dim, .btn variants, .badge + .tier-*, .chip, table defaults.

    Does NOT include: tradeOS nav, cards, modals, or any tos-* components.
    """
    return """
    /* ── Dark theme — Dispatch "ink board" (wl-34/wl-35) ── */
    :root, [data-theme="dark"] {
      --bg: #17140f;
      --bg2: #211d16;
      --bg3: #2b261e;
      --fg: #f4f2ea;
      --muted: rgba(244,242,234,.70);
      --dim: rgba(244,242,234,.42);
      --neon: #e8622c;
      --mag: #b58a5a;
      --green: #4caf7d;
      --yellow: #d9a441;
      --red: #e05c3a;
      --border: rgba(244,242,234,.14);
      --shadow: rgba(0,0,0,.45);
      --hover-tint: color-mix(in srgb, var(--neon) 4%, transparent);
      --code-bg: rgba(244,242,234,.06);
      --surface: #211d16;
      --surface-alt: #2b261e;
      --text: #f4f2ea;
      --text-bright: #faf8f0;
      --text-muted: rgba(244,242,234,.70);
      --bright: #faf8f0;
      --accent: #e8622c;
      --blue: #7a8fa3;
      --purple: #9c8468;
      --magenta: #b58a5a;
      --orange: #c98a3d;
      /* Type */
      --sans: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --font-sans: var(--sans);
      --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --font-mono: var(--mono);
      /* Type scale */
      --fs-xs: 10px;
      --fs-sm: 11px;
      --fs-base: 13px;
      --fs-md: 14px;
      --fs-lg: 16px;
      --fs-xl: 20px;
      --fs-2xl: 24px;
      --text-page-title: 24px;
      --text-section:    16px;
      --text-body:       14px;
      --text-secondary:  12px;
      --text-badge:      11px;
      --type-page:    var(--text-page-title);
      --type-section: var(--text-section);
      --type-body:    var(--text-body);
      --type-small:   var(--text-secondary);
      --type-micro:   var(--text-badge);
      /* Semantic color tokens */
      --clr-positive:    #4caf7d;
      --clr-negative:    #e05c3a;
      --clr-warning:     #d9a441;
      --clr-interactive: #e8622c;
      --clr-neutral:     rgba(244,242,234,.40);
      --clr-long:        #7a8fa3;
      --clr-short:       #c98a3d;
      --clr-positive-bg:    color-mix(in srgb, var(--clr-positive) 12%, transparent);
      --clr-negative-bg:    color-mix(in srgb, var(--clr-negative) 12%, transparent);
      --clr-warning-bg:     color-mix(in srgb, var(--clr-warning) 12%, transparent);
      --clr-interactive-bg: color-mix(in srgb, var(--neon) 8%, transparent);
      /* Spacing */
      --sp-xs: 4px;
      --sp-sm: 8px;
      --sp-md: 12px;
      --sp-lg: 16px;
      --sp-xl: 24px;
      /* Radius */
      --r-sm: 4px;
      --r-md: 6px;
      --r-lg: 8px;
      --r-xl: 12px;
      --r-pill: 999px;
    }

    /* ── Light theme — Dispatch "paper" (wl-34/wl-35) ── */
    [data-theme="light"] {
      --bg: #f1efe8;
      --bg2: #faf9f4;
      --bg3: #e6e3d8;
      --fg: #211d17;
      --muted: rgba(33,29,23,.68);
      --dim: rgba(33,29,23,.45);
      --neon: #d94f1e;
      --mag: #7a6248;
      --green: #2c7a4b;
      --yellow: #a06b13;
      --red: #b3341f;
      --border: rgba(33,29,23,.16);
      --shadow: rgba(33,29,23,.08);
      --hover-tint: color-mix(in srgb, var(--neon) 5%, transparent);
      --code-bg: rgba(33,29,23,.05);
      --surface: #faf9f4;
      --surface-alt: #f1efe8;
      --text: #211d17;
      --text-bright: #17140f;
      --text-muted: rgba(33,29,23,.68);
      --bright: #17140f;
      --accent: #d94f1e;
      --blue: #52667a;
      --purple: #7a6248;
      --magenta: #8a6a4c;
      --orange: #995f1a;
      --clr-positive:    #2c7a4b;
      --clr-negative:    #b3341f;
      --clr-warning:     #a06b13;
      --clr-interactive: #d94f1e;
      --clr-neutral:     rgba(33,29,23,.45);
      --clr-long:        #52667a;
      --clr-short:       #995f1a;
      --clr-positive-bg:    color-mix(in srgb, var(--clr-positive) 10%, transparent);
      --clr-negative-bg:    color-mix(in srgb, var(--clr-negative) 10%, transparent);
      --clr-warning-bg:     color-mix(in srgb, var(--clr-warning) 10%, transparent);
      --clr-interactive-bg: color-mix(in srgb, var(--neon) 8%, transparent);
      --mode-color: var(--neon);
      --mode-color-dim: color-mix(in srgb, var(--neon) 32%, transparent);
      --mode-color-bg: color-mix(in srgb, var(--neon) 7%, transparent);
    }

    /* ── Mode ambient color ── */
    html {
      --mode-color: var(--neon);
      --mode-color-dim: color-mix(in srgb, var(--neon) 28%, transparent);
      --mode-color-bg: color-mix(in srgb, var(--neon) 7%, transparent);
    }

    /* ── Base reset ── */
    * { box-sizing: border-box; touch-action: manipulation; }
    body {
      background: var(--bg); color: var(--fg);
      font-family: var(--font-sans, -apple-system, sans-serif);
      font-size: var(--fs-base); line-height: 1.5;
      margin: 0; padding: 0;
    }

    /* ── Layout ── */
    .page { max-width: 1200px; margin: 0 auto; padding: var(--sp-xl) var(--sp-xl); }
    .page.page-full { max-width: none; padding-left: var(--sp-lg); padding-right: var(--sp-lg); }

    /* ── Typography ── */
    h1 { font-size: var(--fs-xl); margin: 10px 0 6px; }
    h2 { font-size: var(--fs-lg); margin: 18px 0 8px; color: var(--fg); }
    h3 { font-size: var(--type-section); margin: 14px 0 6px; color: var(--fg); font-weight: 600; letter-spacing: 0; }
    .muted { color: var(--muted); }
    .dim { color: var(--dim); }
    a { color: var(--accent); }
    .hr { height: 1px; background: var(--border); margin: var(--sp-lg) 0; }

    /* ── Buttons ── */
    .btn {
      display: inline-block; padding: var(--sp-sm) 18px;
      border-radius: var(--r-lg); border: 1px solid var(--mode-color);
      background: transparent; color: var(--mode-color);
      font-size: var(--fs-base); font-weight: 500; cursor: pointer;
      text-decoration: none; transition: all .15s;
    }
    .btn:hover, .btn:active { background: var(--mode-color-bg); }
    .btn.primary { background: var(--mode-color-bg); }
    .btn.danger { border-color: var(--red); color: var(--red); }
    .btn.danger:hover, .btn.danger:active { background: color-mix(in srgb, var(--red) 10%, transparent); }
    .btn.warn { border-color: var(--yellow); color: var(--yellow); }
    .btn.warn:hover, .btn.warn:active { background: color-mix(in srgb, var(--yellow) 10%, transparent); }
    .btn.go { border-color: var(--green); color: var(--green); }
    .btn.go:hover, .btn.go:active { background: color-mix(in srgb, var(--green) 10%, transparent); }
    .btn:disabled { opacity: .4; cursor: not-allowed; }
    .btn-sm { font-size: var(--fs-sm); padding: 4px 12px; }
    .btn-xs { font-size: var(--fs-xs); padding: 2px 10px; }

    /* ── Badges ── */
    .badge {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 2px 8px; border-radius: var(--r-pill);
      font-size: var(--text-badge); font-weight: 500;
      line-height: 1.4; border: 1px solid transparent;
      white-space: nowrap;
    }
    .badge::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
    .badge.tier-critical { color: var(--clr-negative); border-color: color-mix(in srgb, var(--clr-negative) 55%, transparent); background: color-mix(in srgb, var(--clr-negative) 10%, transparent); }
    .badge.tier-warning  { color: var(--clr-warning);  border-color: color-mix(in srgb, var(--clr-warning)  55%, transparent); background: color-mix(in srgb, var(--clr-warning)  10%, transparent); }
    .badge.tier-info     { color: var(--clr-interactive); border-color: color-mix(in srgb, var(--clr-interactive) 45%, transparent); background: color-mix(in srgb, var(--clr-interactive) 8%, transparent); }
    .badge.tier-info::before { display: none; }
    .badge.tier-positive { color: var(--clr-positive); border-color: color-mix(in srgb, var(--clr-positive) 50%, transparent); background: color-mix(in srgb, var(--clr-positive) 10%, transparent); }
    .badge.tier-neutral  { color: var(--muted); border-color: var(--border); background: var(--code-bg); }
    .badge.tier-neutral::before { display: none; }
    .badge.tier-long     { color: var(--clr-long);  border-color: color-mix(in srgb, var(--clr-long)  50%, transparent); background: color-mix(in srgb, var(--clr-long)  10%, transparent); }
    .badge.tier-short    { color: var(--clr-short); border-color: color-mix(in srgb, var(--clr-short) 55%, transparent); background: color-mix(in srgb, var(--clr-short) 10%, transparent); }

    /* ── Tables ── */
    table { border-collapse: collapse; width: 100%; }
    th, td { border-bottom: 1px solid var(--border); padding: 10px var(--sp-sm); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: var(--fs-sm); text-transform: uppercase; letter-spacing: .08em; }
    td { font-size: var(--fs-base); }
    tr:hover { background: var(--hover-tint); }

    /* ── Docs surface (wl-27) ── */
    .ts-doc-body { max-width: 860px; }
    .ts-doc-body p { margin: 8px 0; }
    .ts-doc-body code {
      font-family: var(--font-mono); font-size: 0.92em;
      background: var(--code-bg); border-radius: var(--r-sm);
      padding: 1px 5px;
    }
    .ts-doc-body pre {
      background: var(--code-bg); border: 1px solid var(--border);
      border-radius: var(--r-md); padding: var(--sp-md);
      overflow-x: auto; margin: 10px 0;
    }
    .ts-doc-body pre code { background: none; padding: 0; }
    .ts-doc-body blockquote {
      margin: 10px 0; padding: 4px 14px;
      border-left: 3px solid var(--mode-color); color: var(--muted);
    }
    .ts-doc-body ul, .ts-doc-body ol { margin: 8px 0; padding-left: 22px; }
    .ts-doc-body li { margin: 2px 0; }
    .ts-doc-body table { margin: 10px 0; }

    /* ── Chips ── */
    .chip { display:inline-block; padding:2px 8px; border-radius:var(--r-pill); border:1px solid var(--border); font-size: var(--fs-sm); }
    .chip.ok  { border-color: color-mix(in srgb, var(--green) 50%, transparent);  color: var(--green); }
    .chip.warn { border-color: color-mix(in srgb, var(--yellow) 55%, transparent); color: var(--yellow); }
    .chip.bad  { border-color: color-mix(in srgb, var(--red) 55%, transparent);    color: var(--red); }
    .chip.off  { border-color: var(--border); color: var(--dim); }
    .chip.neon { border-color: color-mix(in srgb, var(--neon) 50%, transparent); color: var(--neon); }
    """
