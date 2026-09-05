"""Board CSS and client JS (extracted verbatim from board.py)."""
from __future__ import annotations

def _board_styles() -> str:
    return """
<style>
.tb-toolbar { display:flex; align-items:center; gap:12px; margin-bottom:12px;
              flex-wrap:wrap; }
.tb-toolbar .tb-quick-add { margin-left:auto; }

/* Flex board (wl-36): real columns share space, empty ones collapse to rails. */
.tb-board { display:flex; gap:12px; align-items:stretch; }
.tb-col { flex:1 1 0; min-width:220px;
          border:1px solid var(--border); border-radius:var(--r-lg);
          background:var(--bg2); display:flex; flex-direction:column;
          height:calc(100vh - 180px); min-height:200px;
          transition:border-color .15s, background .15s, flex-basis .2s; }
.tb-col.drag-over { border-color:var(--neon);
                    background:var(--clr-interactive-bg); }
.tb-col-head { display:flex; align-items:center; justify-content:space-between;
               padding:4px 10px; border-bottom:1px solid var(--border);
               position:sticky; top:0; background:var(--bg2); z-index:1; }
.tb-col-head h3 { margin:0; font-size:var(--fs-sm); font-weight:600;
                  color:var(--fg); text-transform:uppercase;
                  letter-spacing:.06em; }
.tb-col-count { font-size:10px; color:var(--dim);
                border:1px solid var(--border); border-radius:999px;
                padding:0 8px; font-variant-numeric:tabular-nums; }
.tb-col-cap-note { font-size:10px; color:var(--dim); font-style:italic;
                   margin-left:auto; padding-right:8px; white-space:nowrap;
                   font-variant-numeric:tabular-nums; }
.tb-col-cap-note[hidden] { display:none; }
.tb-col-body { flex:1 1 auto; min-height:0; overflow-y:auto; padding:4px 6px 6px; }
.tb-col-body > * + * { margin-top:6px; }
.tb-col-empty { padding:10px 0; font-size:var(--fs-xs); text-align:center; }

/* Empty-column rail (wl-36): slim vertical strip; header/body hide, label shows. */
.tb-rail-label { display:none; }
.tb-col--rail { flex:0 0 34px; min-width:34px; border-style:dashed; }
.tb-col--rail .tb-col-head, .tb-col--rail .tb-col-body { display:none; }
.tb-col--rail .tb-rail-label {
  display:flex; flex-direction:column; align-items:center; gap:10px;
  padding:12px 0; flex:1 1 auto; }
.tb-rail-name { writing-mode:vertical-rl; font-size:10px; font-weight:600;
                text-transform:uppercase; letter-spacing:.14em;
                color:var(--dim); }
.tb-rail-count { font-size:10px; color:var(--dim);
                 font-variant-numeric:tabular-nums; }

.tb-card { display:block; border:1px solid var(--border);
           border-radius:var(--r-md); background:var(--bg);
           padding:7px 9px; cursor:grab; transition:all .12s;
           font-size:var(--fs-sm); line-height:1.45; position:relative; }
.tb-card:hover { border-color:var(--neon); transform:translateY(-1px); }
.tb-card.dragging { opacity:.5; cursor:grabbing; }

/* Priority stripe (wl-36): systematic left-edge encoding — signal for
   High/Urgent, border-tone for Normal, none for Low. Inset box-shadow so it
   survives the border-color change on hover. */
.tb-card.tb-prio-1 { box-shadow: inset 3px 0 0 var(--red);
                     background: color-mix(in srgb, var(--red) 5%, var(--bg)); }
.tb-card.tb-prio-2 { box-shadow: inset 3px 0 0 var(--neon); }
.tb-card.tb-prio-3 { box-shadow: inset 3px 0 0 var(--border); }
.tb-card.tb-prio-4 { opacity:.85; }
.tb-card-head { display:flex; align-items:center; justify-content:space-between;
                gap:6px; margin-bottom:3px; }
.tb-card-ext { font-family:var(--font-mono); font-size:10px; color:var(--dim);
               font-weight:600; letter-spacing:.05em; }
.tb-card-id { font-family:var(--font-mono); font-size:11px; color:var(--neon);
              font-weight:700; letter-spacing:.03em; opacity:.85; }
.tb-card-priority { flex:0 0 auto; }
.tb-card-title { display:-webkit-box; -webkit-line-clamp:2;
                 -webkit-box-orient:vertical; overflow:hidden;
                 color:var(--fg); text-decoration:none;
                 font-weight:500; word-break:break-word;
                 font-size:var(--fs-xs); line-height:1.4; }
.tb-card-title:hover { color:var(--neon); }
/* Byline (wl-54): responsible/worked-by identity, shown on every column. */
.tb-card-owner { display:flex; align-items:center; gap:4px; margin-top:4px;
                  font-family:var(--font-mono); font-size:10px;
                  color:var(--muted); letter-spacing:.03em; }
/* Claim age (wl-104): dimmer than the identity it trails. */
.tb-card-claim-age { color:var(--dim); }
/* Staleness hint (wl-104): claimed, no comment since, past threshold —
   a stale claim signal, not a process-state claim. */
.tb-card-stale { color:#f59e0b; border:1px solid #f59e0b; border-radius:3px;
                 padding:0 4px; font-size:9px; text-transform:uppercase;
                 letter-spacing:.04em; }
/* Gate chip (wl-21): shown while gate_type withholds the ticket from ready. */
.tb-card-gate { margin-top:4px; }
/* Meta row (wl-36): labels + age share one line; age right-aligned. */
.tb-card-meta { margin-top:5px; display:flex; flex-wrap:wrap; gap:3px;
                align-items:center; }
.tb-card-meta .badge { font-size:9px; padding:1px 5px; }
.tb-card-meta-ago { margin-left:auto; font-size:10px; color:var(--dim);
                    white-space:nowrap; }
.tb-card--has-detail { cursor:pointer; }
.tb-card-more { font-size:10px; }
/* Collapsible detail section — hidden by default, shown on click */
.tb-card-detail { display:none; margin-top:8px; padding-top:8px;
                  border-top:1px solid var(--border); }
.tb-card.expanded .tb-card-detail { display:block; }
.tb-card-preview { font-size:var(--fs-xs); color:var(--muted);
                   line-height:1.5; max-height:120px; overflow-y:auto; }
.tb-card-preview-author { font-family:var(--font-mono);
                          font-size:10px; color:var(--dim);
                          margin-bottom:4px; display:block; }
@media (max-width:1100px) {
  .tb-board { flex-wrap:wrap; }
  .tb-col { flex:1 1 45%; height:auto; }
  /* Wrapped layout has no fixed column height, so clamp the body instead —
     with the 50-card cap a full column would otherwise be several
     thousand px tall. */
  .tb-col-body { max-height:70vh; }
  .tb-col--rail { flex:1 1 100%; min-height:34px; }
  .tb-col--rail .tb-rail-label { flex-direction:row; padding:0 12px; }
  .tb-col--rail .tb-rail-name { writing-mode:horizontal-tb; }
}

/* ── In-progress pulse animation ─────────────────────────────────── */
@keyframes tb-ip-pulse {
  0%, 100% { box-shadow: inset 3px 0 0 var(--neon), 0 0 6px color-mix(in srgb, var(--neon) 14%, transparent); }
  50%       { box-shadow: inset 3px 0 0 var(--neon), 0 0 20px color-mix(in srgb, var(--neon) 40%, transparent); }
}
.tb-card[data-status="in_progress"] {
  animation: tb-ip-pulse 2.8s ease-in-out infinite;
  border-color: color-mix(in srgb, var(--neon) 38%, transparent);
}
.tb-card[data-status="in_progress"]:hover {
  animation-play-state: paused;
}

/* ── Activity strip ───────────────────────────────────────────────── */
#admin-activity-strip {
  display:flex; align-items:center; gap:0; flex-wrap:nowrap;
  overflow-x:auto; overflow-y:hidden;
  background:color-mix(in srgb, var(--neon) 4%, transparent);
  border:1px solid color-mix(in srgb, var(--neon) 20%, transparent);
  border-radius:var(--r-md);
  padding:6px 12px;
  margin-bottom:10px;
  min-height:32px;
  scrollbar-width:none;
}
#admin-activity-strip:empty::before {
  content: 'No tickets in progress';
  font-size:var(--fs-xs);
  color:var(--dim);
}
.tb-strip-item {
  display:flex; align-items:center; gap:6px;
  flex-shrink:0;
  font-size:var(--fs-xs);
  color:var(--muted);
  text-decoration:none;
  padding:2px 10px 2px 0;
  border-right:1px solid color-mix(in srgb, var(--neon) 18%, transparent);
  margin-right:10px;
  white-space:nowrap;
  overflow:hidden;
  max-width:320px;
}
.tb-strip-item:last-child { border-right:none; margin-right:0; }
.tb-strip-item:hover { color:var(--fg); }
.tb-strip-dot {
  display:inline-block;
  width:7px; height:7px;
  border-radius:50%;
  background:var(--neon);
  flex-shrink:0;
  animation:tb-ip-pulse 2s ease-in-out infinite;
}
.tb-strip-id { font-family:var(--font-mono); color:var(--neon);
               font-weight:700; font-size:10px; flex-shrink:0; }
.tb-strip-title { font-weight:500; color:var(--fg);
                  overflow:hidden; text-overflow:ellipsis; max-width:140px; }
.tb-strip-comment { color:var(--dim); overflow:hidden;
                    text-overflow:ellipsis; max-width:140px; }

/* ── Compact inline filter toolbar ───────────────────────────────── */
.tb-toolbar { display:flex; align-items:center; gap:8px; margin-bottom:10px;
              flex-wrap:wrap; }
.tb-toolbar-filter { display:contents; }
.tb-toolbar-filter select,
.tb-toolbar-filter input[type=text] {
  padding:4px 8px; font-size:var(--fs-xs);
  background:var(--bg2); border:1px solid var(--border);
  border-radius:var(--r-sm); color:var(--fg);
  height:28px; line-height:1;
}
.tb-toolbar-filter input[type=text] { min-width:130px; }
.tb-toolbar-filter .btn { padding:4px 10px; font-size:var(--fs-xs); height:28px; }
.tb-toolbar .tb-quick-add { margin-left:auto; }

/* Gate class filter chips (wl-265): Ready · For You · Deferred */
.wq-gate-row { gap:6px; padding-bottom:4px; border-bottom:1px solid
               color-mix(in srgb, var(--border) 50%, transparent); margin-bottom:4px; }
.wq-gate-chip { display:inline-flex; align-items:center; gap:5px;
                padding:3px 10px; font-size:var(--fs-xs);
                border:1px solid var(--border); border-radius:999px;
                color:var(--muted); background:var(--bg2);
                text-decoration:none; transition:all .12s; white-space:nowrap; }
.wq-gate-chip:hover { color:var(--fg); border-color:var(--neon); }
.wq-gate-chip--active { color:var(--neon); border-color:var(--neon);
                         background:color-mix(in srgb, var(--neon) 10%, transparent);
                         font-weight:600; }
.wq-gate-chip-count { font-size:10px; opacity:.7;
                       font-variant-numeric:tabular-nums; }

/* Quick-add modal (floats over board/table views). */
.tb-qa-overlay { position:fixed; inset:0; background:rgba(0,0,0,.55);
                 z-index:1000; display:flex; align-items:flex-start;
                 justify-content:center; padding-top:12vh; }
.tb-qa-overlay[hidden] { display:none; }
.tb-qa-modal { width:min(520px, 92vw); background:var(--bg2);
               border:1px solid var(--neon); border-radius:var(--r-lg);
               box-shadow:0 12px 48px rgba(0,0,0,.45);
               padding:18px 20px; }
.tb-qa-modal header { display:flex; align-items:center; justify-content:space-between;
                      margin-bottom:12px; }
.tb-qa-modal header h3 { margin:0; font-size:var(--fs-md); color:var(--neon);
                         letter-spacing:.04em; text-transform:uppercase; }
.tb-qa-modal header button { background:none; border:0; color:var(--dim);
                             font-size:20px; cursor:pointer; padding:0 4px; }
.tb-qa-modal header button:hover { color:var(--fg); }
.tb-qa-modal form { display:flex; flex-direction:column; gap:10px; }
.tb-qa-modal input, .tb-qa-modal select { width:100%; }
.tb-qa-row { display:grid; grid-template-columns:140px 1fr; gap:10px; }
.tb-qa-actions { display:flex; gap:10px; align-items:center;
                 justify-content:flex-end; margin-top:4px; }
.tb-qa-actions #admin-qa-status { margin-right:auto; font-size:var(--fs-xs); }
</style>
"""


def _client_js() -> str:
    return r"""
<script>
  async function adminTaskStatusChange(sel) {
    var taskId = sel.getAttribute('data-task-id');
    var status = sel.value;
    sel.disabled = true;
    try {
      var resp = await fetch('/api/admin/tasks/' + encodeURIComponent(taskId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: status })
      });
      var j = await resp.json();
      if (!j.ok) { showToast('Update failed: ' + (j.error || resp.status), 'error'); return; }
      showToast('Status updated', 'success');
    } catch (e) {
      showToast('Network error', 'error');
    } finally {
      sel.disabled = false;
    }
  }

  /* ──────────────────────────────────────────────────────────────────
     Kanban board (SEO-205)
     ────────────────────────────────────────────────────────────────── */

  var __ADMIN_BOARD_POLL_MS = 10000;
  var __ADMIN_BOARD_COLUMNS = ['backlog', 'in_review', 'in_progress', 'done'];
  var __ADMIN_BOARD_COLUMN_CAP = 50; // mirror of Python _BOARD_COLUMN_CAP (wl-11)
  var __ADMIN_BOARD_LABELS = {
    'backlog': 'Backlog',
    'in_progress': 'In Progress',
    'in_review': 'In Review',
    'done': 'Done',
    'canceled': 'Canceled'
  };
  var __ADMIN_BOARD_POLL_HANDLE = null;

  function adminBoardFmtAgo(iso) {
    if (!iso) return '';
    var then = Date.parse(iso);
    if (isNaN(then)) return iso.slice(0, 19);
    var diff = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (diff < 45)        return 'just now';
    if (diff < 90)        return 'a minute ago';
    if (diff < 3600)      return Math.floor(diff / 60) + 'm ago';
    if (diff < 5400)      return 'an hour ago';
    if (diff < 86400)     return Math.floor(diff / 3600) + 'h ago';
    if (diff < 172800)    return 'yesterday';
    if (diff < 2592000)   return Math.floor(diff / 86400) + 'd ago';
    if (diff < 5184000)   return 'a month ago';
    if (diff < 31536000)  return Math.floor(diff / 2592000) + 'mo ago';
    return Math.floor(diff / 31536000) + 'y ago';
  }

  function adminBoardEscape(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* wl-76: Jump-# box. Composite ids (wl-503) navigate directly; a bare
     number resolves against the current product scope when the command
     bar is scoped to one, otherwise (All view) it's ambiguous across
     stores and /api/admin/tasks/resolve is asked to disambiguate. */
  async function adminBoardJumpSubmit(form) {
    var box = document.getElementById('wq-jump-ambiguous');
    if (box) { box.hidden = true; box.innerHTML = ''; }
    var v = (form.elements['id'].value || '').trim().replace(/^#/, '');
    if (!v) return false;
    if (/^[A-Za-z]+-\d+$/.test(v)) {
      window.location.href = '/admin/desk?open=' + encodeURIComponent(v);
      return false;
    }
    if (!/^\d+$/.test(v)) {
      if (typeof showToast === 'function') showToast('Use a number or an id like wl-503', 'error');
      return false;
    }
    var prefix = form.getAttribute('data-scope-prefix') || '';
    if (prefix) {
      window.location.href = '/admin/desk?open=' + encodeURIComponent(prefix + '-' + v);
      return false;
    }
    try {
      var resp = await fetch('/api/admin/tasks/resolve?id=' + encodeURIComponent(v));
      var j = await resp.json();
      if (j.ok && j.match) {
        window.location.href = '/admin/desk?open=' + encodeURIComponent(j.match);
      } else if (j.ok && j.candidates && j.candidates.length && box) {
        box.innerHTML = j.candidates.map(function (c) {
          return "<a href='/admin/desk?open=" + encodeURIComponent(c.id) + "'>" +
            adminBoardEscape(c.id) + ' — ' + adminBoardEscape(c.title) + '</a>';
        }).join('');
        box.hidden = false;
      } else if (typeof showToast === 'function') {
        showToast('No work order #' + v + ' found', 'error');
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('Network error', 'error');
    }
    return false;
  }

  // wl-9: card markup is server-rendered only (_render_task_card). The poll
  // returns task.card_html; JS never reimplements badge/chip/worker anatomy.

  function adminActivityStripRebuild(tasks) {
    var strip = document.getElementById('admin-activity-strip');
    if (!strip) return;
    var inProgress = (tasks || []).filter(function(t) { return t.status === 'in_progress'; });
    if (!inProgress.length) {
      strip.innerHTML = '';
      return;
    }
    strip.innerHTML = inProgress.map(function(t) {
      var idStr = '#' + adminBoardEscape(t.id);
      var title = adminBoardEscape((t.title || '').slice(0, 60));
      var comment = '';
      if (t.last_comment_preview) {
        comment = ' — ' + adminBoardEscape((t.last_comment_preview || '').slice(0, 70));
      }
      return "<a class='tb-strip-item' href='/admin/desk?open=" + encodeURIComponent(t.id) + "'"
           + " title='" + adminBoardEscape(t.title) + "'>"
           + "<span class='tb-strip-dot'></span>"
           + "<span class='tb-strip-id'>" + idStr + "</span>"
           + "<span class='tb-strip-title'>" + title + "</span>"
           + (comment ? "<span class='tb-strip-comment'>" + comment + "</span>" : "")
           + "</a>";
    }).join('');
  }

  function adminSummaryRebuild(tasks) {
    var bar = document.querySelector('.ts-summary-bar');
    if (!bar) return;
    var inFlight = 0, inReview = 0, done = 0, waiting = 0, backlog = 0;
    (tasks || []).forEach(function(t) {
      if (t.status === 'in_progress') inFlight++;
      else if (t.status === 'in_review') inReview++;
      else if (t.status === 'done') done++;
      else if (t.status === 'backlog') backlog++;
      if ((t.labels || []).indexOf('needs:decision') >= 0
          && t.status !== 'done' && t.status !== 'canceled') waiting++;
    });
    var parts = [
      "<span class='ts-sum-chip ts-sum-flight'>" + inFlight + " in flight</span>"
    ];
    if (inReview) parts.push("<span class='ts-sum-chip ts-sum-review'>" + inReview + " in review</span>");
    parts.push("<span class='ts-sum-chip ts-sum-done'>" + done + " done</span>");
    if (waiting) parts.push("<span class='ts-sum-chip ts-sum-waiting'>" + waiting + " waiting on you</span>");
    parts.push("<span class='ts-sum-chip ts-sum-backlog'>" + backlog + " backlog</span>");
    bar.innerHTML = parts.join(' &middot; ');
  }

  function adminBoardRebuild(tasks, columnCounts) {
    adminActivityStripRebuild(tasks);
    adminSummaryRebuild(tasks);
    var board = document.getElementById('admin-task-board');
    if (!board) return;
    var bucket = {};
    __ADMIN_BOARD_COLUMNS.forEach(function(c) { bucket[c] = []; });
    (tasks || []).forEach(function(t) {
      if (bucket.hasOwnProperty(t.status)) bucket[t.status].push(t);
    });
    var cols = board.querySelectorAll('.tb-col');
    cols.forEach(function(col) {
      var status = col.getAttribute('data-status');
      var list = bucket[status] || [];
      // Mirror of the SSR backlog sort in _render_task_board (wl-8):
      // urgency first, stable so recency breaks ties.
      if (status === 'backlog') {
        list = list.slice().sort(function(a, b) {
          return (parseInt(a.priority, 10) || 3) - (parseInt(b.priority, 10) || 3);
        });
      }
      var body = col.querySelector('.tb-col-body');
      // wl-47: header shows the true (uncapped) scope count for the current
      // filters; the cap note says how much of it the capped fetch holds.
      var scopeN = list.length;
      if (columnCounts && typeof columnCounts === 'object') {
        var n = Number(columnCounts[status] || 0);
        if (isFinite(n) && n > scopeN) scopeN = n;
      }
      var countEls = col.querySelectorAll('[data-count]');
      countEls.forEach(function(el) { el.textContent = String(scopeN); });
      var capNote = col.querySelector('[data-cap-note]');
      if (capNote) {
        var truncated = list.length > 0 && scopeN > list.length;
        capNote.hidden = !truncated;
        capNote.textContent = truncated
          ? 'most recent ' + list.length + ' shown' : '';
      }
      // Empty columns collapse to rails (wl-36); a rail inflating back into
      // a column is itself the signal that work entered that state.
      col.classList.toggle('tb-col--rail', !list.length);
      if (!body) return;
      if (!list.length) {
        body.innerHTML = "<div class='tb-col-empty dim'>No tickets in this column.</div>";
        return;
      }
      var visible = list.slice(0, __ADMIN_BOARD_COLUMN_CAP);
      var hidden = list.slice(__ADMIN_BOARD_COLUMN_CAP);
      // wl-9: swap pre-rendered card HTML (same bytes as SSR) by task id.
      var html = visible.map(function(t) { return t.card_html || ''; }).join('');
      if (hidden.length) {
        var moreId = 'tb-col-more-' + status;
        var hiddenId = 'tb-col-hidden-' + status;
        html += "<div class='tb-col-more' id='" + moreId + "'>"
              + "<button class='btn' onclick='document.getElementById(\"" + hiddenId + "\").hidden=false;this.parentElement.hidden=true;'>Show all " + list.length + "</button></div>"
              + "<div id='" + hiddenId + "' hidden>"
              + hidden.map(function(t) { return t.card_html || ''; }).join('')
              + "</div>";
      }
      body.innerHTML = html;
    });
    adminBoardTouchRelativeTime();
  }

  function adminBoardTouchRelativeTime() {
    var spans = document.querySelectorAll('.tb-card-ago[data-iso]');
    for (var i = 0; i < spans.length; i++) {
      var iso = spans[i].getAttribute('data-iso');
      if (iso) spans[i].textContent = adminBoardFmtAgo(iso);
    }
  }

  // wl-10: advanced-filters chip groups collapse overflow/one-off labels
  // behind a per-group toggle; the search box expands + filters live.
  function adminBoardToggleChipGroup(btn) {
    var group = btn.closest('.wq-chip-group');
    if (!group) return;
    var collapsed = group.getAttribute('data-collapsed') === '1';
    group.setAttribute('data-collapsed', collapsed ? '0' : '1');
  }

  function adminBoardFilterChipSearch(query) {
    var q = (query || '').trim().toLowerCase();
    var container = document.querySelector('.wq-filter-chips');
    if (!container) return;
    container.classList.toggle('wq-chips-searching', !!q);
    var groups = container.querySelectorAll('.wq-chip-group');
    groups.forEach(function(g) {
      var chips = g.querySelectorAll('.notif-filter-chip');
      var anyVisible = false;
      chips.forEach(function(c) {
        var match = !q || (c.textContent || '').toLowerCase().indexOf(q) !== -1;
        c.classList.toggle('wq-chip-search-hidden', !match);
        if (match) anyVisible = true;
      });
      if (q) {
        g.setAttribute('data-collapsed', '0');
        g.style.display = anyVisible ? '' : 'none';
      } else {
        g.setAttribute('data-collapsed', g.getAttribute('data-default-collapsed') || '1');
        g.style.display = '';
      }
    });
  }

  function adminBoardUpdateFilterBuckets(scopeCounts, scopeTotal) {
    if (!scopeCounts || typeof scopeCounts !== 'object') return;
    var total = Number(scopeTotal);
    if (!isFinite(total)) {
      total = 0;
      Object.keys(scopeCounts).forEach(function(k) {
        total += Number(scopeCounts[k] || 0);
      });
    }
    var allEl = document.querySelector("[data-wq-bucket-count='__all__']");
    if (allEl) allEl.textContent = String(total);
    Object.keys(scopeCounts).forEach(function(status) {
      var el = document.querySelector("[data-wq-bucket-count='" + status + "']");
      if (el) el.textContent = String(Number(scopeCounts[status] || 0));
    });
  }

  function adminWorkQueueApiQuery() {
    var g = window.__WQ_POLL_PARAMS || {};
    var q = new URLSearchParams();
    q.set('with_preview', '1');
    q.set('limit', '500');
    if (g.status) q.set('status', g.status);
    if (g.label) q.set('label', g.label);
    if (g.priority) q.set('priority', g.priority);
    if (g.product) q.set('product', g.product);
    if (g.gate) q.set('gate', g.gate);
    return q.toString();
  }

  async function adminBoardFetch() {
    try {
      var resp = await fetch('/api/admin/tasks?' + adminWorkQueueApiQuery(), {
        headers: { 'Accept': 'application/json' }
      });
      var j = await resp.json();
      if (!j || !j.ok) return;
      adminBoardRebuild(j.tasks || [], j.column_counts || null);
      adminBoardUpdateFilterBuckets(j.scope_counts || {}, j.scope_total);
    } catch (e) {
      /* Silent — a transient 500 or network blip shouldn't nuke the UI. */
    }
  }

  function adminBoardDragStart(ev) {
    var card = ev.currentTarget;
    card.classList.add('dragging');
    var taskId = card.getAttribute('data-task-id');
    ev.dataTransfer.setData('text/plain', taskId);
    ev.dataTransfer.effectAllowed = 'move';
  }

  function adminBoardDragEnd(ev) {
    ev.currentTarget.classList.remove('dragging');
    document.querySelectorAll('.tb-col.drag-over').forEach(function(c) {
      c.classList.remove('drag-over');
    });
  }

  function adminBoardDragOver(ev) {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    ev.currentTarget.classList.add('drag-over');
  }

  function adminBoardDragLeave(ev) {
    if (ev.currentTarget === ev.target) {
      ev.currentTarget.classList.remove('drag-over');
    }
  }

  async function adminBoardDrop(ev) {
    ev.preventDefault();
    var col = ev.currentTarget;
    col.classList.remove('drag-over');
    var taskId = ev.dataTransfer.getData('text/plain');
    if (!taskId) return;
    var newStatus = col.getAttribute('data-status');
    var card = document.querySelector(".tb-card[data-task-id='" + CSS.escape(taskId) + "']");
    if (!card) return;
    if (card.getAttribute('data-status') === newStatus) return;
    var prevParent = card.parentNode;
    var body = col.querySelector('.tb-col-body');
    if (body) body.insertBefore(card, body.firstChild);
    card.setAttribute('data-status', newStatus);
    try {
      var resp = await fetch('/api/admin/tasks/' + encodeURIComponent(taskId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: newStatus })
      });
      var j = await resp.json();
      if (!j.ok) throw new Error(j.error || ('HTTP ' + resp.status));
      if (typeof showToast === 'function') {
        showToast('Moved to ' + (__ADMIN_BOARD_LABELS[newStatus] || newStatus), 'success');
      }
      adminBoardFetch();
    } catch (e) {
      if (prevParent) prevParent.appendChild(card);
      if (typeof showToast === 'function') {
        showToast('Move failed: ' + e.message, 'error');
      }
    }
  }

  function adminBoardInit() {
    var hasBoard = !!document.getElementById('admin-task-board');
    var hasStrip = !!document.getElementById('admin-activity-strip');
    // wl-38: Table view has no live poll target, but its Age column reuses
    // the same .tb-card-ago[data-iso] relative-time hook as Board cards.
    var hasTable = !!document.querySelector('.ts-timetable-table');
    if (!hasBoard && !hasStrip && !hasTable) return;
    adminBoardTouchRelativeTime();
    if (!hasBoard && !hasStrip) return;
    adminBoardFetch();
    if (__ADMIN_BOARD_POLL_HANDLE) clearInterval(__ADMIN_BOARD_POLL_HANDLE);
    __ADMIN_BOARD_POLL_HANDLE = setInterval(function() {
      if (document.visibilityState === 'hidden') return;
      adminBoardFetch();
    }, __ADMIN_BOARD_POLL_MS);
    document.addEventListener('visibilitychange', function() {
      if (document.visibilityState === 'visible') adminBoardFetch();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', adminBoardInit);
  } else {
    adminBoardInit();
  }
</script>
"""

