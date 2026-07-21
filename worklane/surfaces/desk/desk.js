<script>
  /* tp-9: clickable on-card label/priority filters lived only on the JS
     card renderer (poll path). Cards are server-rendered now; filter via
     the command-bar chips instead. */

  /* ── Elapsed time helper ─────────────────────────────────────────── */
  function afFormatElapsed(isoStr) {
    if (!isoStr) return '';
    var then = Date.parse(isoStr);
    if (isNaN(then)) return '';
    var secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
    var h = Math.floor(secs / 3600);
    var m = Math.floor((secs % 3600) / 60);
    if (h > 24) {
      var d = Math.floor(h / 24);
      return d + 'd ' + (h % 24) + 'h';
    }
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
  }

  /* ── Last-updated indicator ──────────────────────────────────────── */
  var _tsLastPollAt = 0;
  function tsUpdateLastUpdated() {
    var el = document.getElementById('ts-last-updated');
    if (!el || !_tsLastPollAt) return;
    var secs = Math.floor((Date.now() - _tsLastPollAt) / 1000);
    el.textContent = secs < 5 ? 'Updated just now' : 'Updated ' + secs + 's ago';
  }
  setInterval(tsUpdateLastUpdated, 5000);

  /* ── Track previous task statuses for transition animation ───────── */
  var _tsPrevStatuses = {};

  /* After every board rebuild, inject elapsed time badges, animate
     cards that changed columns, and refresh the activity feed. */
  var _origAdminBoardRebuild = (typeof adminBoardRebuild === 'function') ? adminBoardRebuild : null;
  adminBoardRebuild = function(tasks, columnCounts) {
    /* Snapshot current statuses before rebuild */
    var newStatuses = {};
    for (var i = 0; i < tasks.length; i++) {
      newStatuses[tasks[i].id] = tasks[i].status;
    }

    /* Forward every arg — the wrapped fn takes (tasks, columnCounts) and
       dropping the counts clobbers the tp-47 header totals. */
    if (_origAdminBoardRebuild) _origAdminBoardRebuild(tasks, columnCounts);

    /* Animate cards that moved columns */
    for (var tid in newStatuses) {
      if (_tsPrevStatuses[tid] && _tsPrevStatuses[tid] !== newStatuses[tid]) {
        var card = document.querySelector(".tb-card[data-task-id='" + tid + "']");
        if (card) {
          card.classList.add('ts-card-entering');
          card.addEventListener('animationend', function() {
            this.classList.remove('ts-card-entering');
          }, {once: true});
        }
      }
    }
    _tsPrevStatuses = newStatuses;

    /* Update last-poll timestamp */
    _tsLastPollAt = Date.now();
    tsUpdateLastUpdated();

    /* Inject elapsed time into in-progress card footers */
    var ipCards = document.querySelectorAll(".tb-card[data-status='in_progress']");
    for (var i = 0; i < ipCards.length; i++) {
      var card = ipCards[i];
      var agoSpan = card.querySelector('.tb-card-ago[data-iso]');
      if (!agoSpan) continue;
      var iso = agoSpan.getAttribute('data-iso');
      var existing = card.querySelector('.tb-elapsed');
      var elapsed = afFormatElapsed(iso);
      if (!elapsed) continue;
      if (existing) {
        existing.textContent = '\u23F1 ' + elapsed;
      } else {
        var foot = card.querySelector('.tb-card-meta');
        if (foot) {
          var badge = document.createElement('span');
          badge.className = 'tb-elapsed';
          badge.textContent = '\u23F1 ' + elapsed;
          foot.appendChild(badge);
        }
      }
    }
    tsFetchBoardSummary();
    tsFetchAttentionSummary();
    tsFetchScopeNavCounts();
  };

  /* Tick elapsed timers every 30s */
  setInterval(function() {
    var ipCards = document.querySelectorAll(".tb-card[data-status='in_progress']");
    for (var i = 0; i < ipCards.length; i++) {
      var card = ipCards[i];
      var agoSpan = card.querySelector('.tb-card-ago[data-iso]');
      if (!agoSpan) continue;
      var iso = agoSpan.getAttribute('data-iso');
      var el = card.querySelector('.tb-elapsed');
      if (el) el.textContent = '\u23F1 ' + afFormatElapsed(iso);
    }
  }, 30000);

  /* ── Header pills: ready / in flight / stalled (tp-28) ─────────────
     tp-85: pills honor the page's declared scope (body[data-ops-scope])
     and their click-throughs land on the same scope's Board. */
  async function tsFetchBoardSummary() {
    try {
      var scope = document.body.getAttribute('data-ops-scope') || '';
      var poolPath = '/admin/tickets/' + (scope || 'all');
      var resp = await fetch('/api/dev/board-summary?scope=' + encodeURIComponent(scope), {
        headers: { 'Accept': 'application/json' }
      });
      var j = await resp.json();
      var readyEl = document.getElementById('ts-ready-badge');
      var inflightEl = document.getElementById('ts-inflight-badge');
      var stalledEl = document.getElementById('ts-stalled-badge');
      if (readyEl) {
        if (j.ready_count > 0) {
          readyEl.textContent = j.ready_count + ' ready';
          readyEl.hidden = false;
          readyEl.onclick = function() { window.location.href = poolPath + '?view=table&status=backlog'; };
        } else {
          readyEl.hidden = true;
        }
      }
      if (inflightEl) {
        if (j.in_flight_count > 0) {
          inflightEl.textContent = j.in_flight_count + ' in flight';
          inflightEl.hidden = false;
          inflightEl.onclick = function() { window.location.href = poolPath + '?view=board'; };
        } else {
          inflightEl.hidden = true;
        }
      }
      if (stalledEl) {
        if ((j.stalled_count || 0) > 0) {
          stalledEl.textContent = j.stalled_count + ' stalled';
          stalledEl.hidden = false;
          stalledEl.onclick = function() {
            window.location.href = poolPath + '?view=board';
          };
        } else {
          stalledEl.hidden = true;
        }
      }
      _tsLastPollAt = Date.now();
      tsUpdateLastUpdated();
    } catch (e) { /* silent */ }
  }

  /* tp-120: per-scope ready/stalled badges in the scope switcher pills
     ("All" + each discovered store, plus overflow "More" rows) — one batch
     request populates every data-scope-badge element on the page. */
  async function tsFetchScopeNavCounts() {
    var badges = document.querySelectorAll('[data-scope-badge]');
    if (!badges.length) return;
    try {
      var resp = await fetch('/api/dev/board-summary/all-scopes', {
        headers: { 'Accept': 'application/json' }
      });
      var j = await resp.json();
      var scopes = j.scopes || {};
      for (var i = 0; i < badges.length; i++) {
        var el = badges[i];
        var slug = el.getAttribute('data-scope-badge');
        var s = scopes[slug];
        if (!s) { el.hidden = true; continue; }
        var parts = [];
        if (s.ready_count > 0) {
          parts.push('<span class="ts-seg-count ts-seg-count--ready" title="' +
            s.ready_count + ' ready">' + s.ready_count + '</span>');
        }
        if (s.stalled_count > 0) {
          parts.push('<span class="ts-seg-count ts-seg-count--stalled" title="' +
            s.stalled_count + ' stalled">' + s.stalled_count + '</span>');
        }
        if (parts.length) {
          el.innerHTML = parts.join('');
          el.hidden = false;
        } else {
          el.hidden = true;
        }
      }
    } catch (e) { /* silent */ }
  }

  /* tp-135: founder-attention chip — always all-store, unlike the
     scope-aware pills above (no ?scope= — same "all stores" convention as
     board-summary's scope=all). */
  async function tsFetchAttentionSummary() {
    try {
      var resp = await fetch('/api/dev/attention', { headers: { 'Accept': 'application/json' } });
      var j = await resp.json();
      var el = document.getElementById('ts-attention-badge');
      if (!el) return;
      var n = j.count || 0;
      if (n > 0) {
        /* Persona law: human is "You" on shipped surfaces (pc-198). */
        el.textContent = n + ' for You';
        el.hidden = false;
        el.title = n + ' waiting on You (human gates · review · decisions) · open attention';
        el.onclick = function() { window.location.href = '/admin/attention'; };
        /* Optional browser notify — same pref key as Office/Roster (pc_you_notify). */
        try {
          var prev = window._tsYouAttnCount;
          window._tsYouAttnCount = n;
          var pref = localStorage.getItem('pc_you_notify') || '';
          if (prev != null && n > prev && pref !== 'off'
              && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
            var top = (j.items && j.items[0]) || null;
            var body = top
              ? ((top.id || '') + ' · ' + String(top.title || top.note || '').slice(0, 90))
              : (n + ' waiting on You');
            var note = new Notification(n + ' for You · Protocol City', {
              body: body, tag: 'pc-you-attention', renotify: true
            });
            note.onclick = function() {
              window.location.href = '/admin/attention';
              note.close();
            };
          }
        } catch (e2) { /* ignore */ }
      } else {
        el.hidden = true;
        window._tsYouAttnCount = 0;
      }
    } catch (e) { /* silent */ }
  }

  /* ── Init: load everything on page ready ─────────────────────────── */
  function tsInit() {
    _tsLastPollAt = Date.now();
    tsUpdateLastUpdated();
    tsFetchBoardSummary();
    tsFetchAttentionSummary();
    tsFetchScopeNavCounts();
    /* Snapshot initial statuses from server-rendered cards */
    var cards = document.querySelectorAll('.tb-card[data-task-id]');
    for (var i = 0; i < cards.length; i++) {
      _tsPrevStatuses[cards[i].getAttribute('data-task-id')] =
        cards[i].getAttribute('data-status');
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tsInit);
  } else {
    tsInit();
  }
</script>
