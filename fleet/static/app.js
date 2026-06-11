/**
 * Fleet dashboard — SSE connection manager + diff expand toggle.
 * Vanilla JS, no build step. < 200 lines.
 */

(function () {
  'use strict';

  var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── SSE Manager ─────────────────────────────────────────────────────────────

  var sseBanner = document.getElementById('sse-banner');
  var activeSource = null;
  var reconnectTimer = null;
  var lastEventId = null;
  var sseScope = null;

  function showBanner(text) {
    if (!sseBanner) return;
    sseBanner.textContent = text || 'reconnecting…';
    sseBanner.classList.add('visible');
  }

  function hideBanner() {
    if (!sseBanner) return;
    sseBanner.classList.remove('visible');
  }

  function buildStreamUrl(scope, afterId) {
    var url = '/api/events/stream?scope=' + encodeURIComponent(scope);
    if (afterId) url += '&after_id=' + encodeURIComponent(afterId);
    return url;
  }

  function appendEventRow(event) {
    // Conversation view: append to #message-thread
    var thread = document.getElementById('message-thread');
    if (!thread) return;

    var row = document.createElement('div');
    row.className = 'message-row' + (prefersReducedMotion ? '' : ' sse-new');
    row.style.padding = '4px 8px';
    row.style.display = 'flex';
    row.style.alignItems = 'center';
    row.style.gap = '8px';

    var typeSpan = document.createElement('span');
    typeSpan.className = 'mono text-muted';
    typeSpan.textContent = event.type || '';
    var summarySpan = document.createElement('span');
    summarySpan.textContent = event.summary || '';
    row.appendChild(typeSpan);
    row.appendChild(summarySpan);

    // Remove empty-state if present
    var emptyState = thread.querySelector('.empty-state');
    if (emptyState) emptyState.remove();

    thread.appendChild(row);
    thread.scrollTop = thread.scrollHeight;

    // Re-run Lucide in case icons were added
    if (typeof lucide !== 'undefined') lucide.createIcons();
  }

  function appendTimelineRow(event) {
    // Timeline view: prepend to #timeline-tbody
    var tbody = document.getElementById('timeline-tbody');
    if (!tbody) return;

    // Remove empty-state row if present
    var emptyRow = tbody.querySelector('.empty-state');
    if (emptyRow) {
      var parentRow = emptyRow.closest('tr');
      if (parentRow) parentRow.remove();
    }

    var tr = document.createElement('tr');
    tr.className = 'event-row' + (prefersReducedMotion ? '' : ' sse-new');
    tr.setAttribute('data-event-id', String(event.id || ''));
    tr.setAttribute('data-event-type', event.type || '');

    var ts = event.ts || '';
    var agentId = event.agent_id || '—';
    var type = event.type || '';
    var summary = event.summary || '';

    tr.innerHTML = [
      '<td><span class="mono text-muted">' + escapeHtml(ts) + '</span></td>',
      '<td><span class="text-secondary">' + escapeHtml(agentId) + '</span></td>',
      '<td><span class="mono text-muted" style="font-size:11px">' + escapeHtml(type) + '</span></td>',
      '<td>' + escapeHtml(summary) + '</td>',
      '<td></td>',
    ].join('');

    tbody.insertBefore(tr, tbody.firstChild);
  }

  function handleEvent(rawEvent) {
    var event;
    try {
      event = JSON.parse(rawEvent.data);
    } catch (e) {
      return;
    }

    if (rawEvent.lastEventId) {
      lastEventId = rawEvent.lastEventId;
    }

    // Route to whichever view is active
    appendEventRow(event);
    appendTimelineRow(event);
  }

  function connectSSE(scope, afterId) {
    if (activeSource) { activeSource.close(); activeSource = null; }
    var url = buildStreamUrl(scope, afterId);
    var source = new EventSource(url);
    activeSource = source;

    source.onopen = function () {
      hideBanner();
    };

    source.onmessage = function (ev) {
      handleEvent(ev);
    };

    source.onerror = function () {
      source.close();
      activeSource = null;
      showBanner('reconnecting…');

      // Exponential back-off capped at 10s
      var delay = Math.min(10000, 1000 + Math.random() * 2000);
      reconnectTimer = setTimeout(function () {
        connectSSE(sseScope, lastEventId);
      }, delay);
    };
  }

  function initSSE() {
    // Conversation page sets window.__SSE_SCOPE
    var scope = window.__SSE_SCOPE;
    if (!scope) return;

    sseScope = scope;

    var thread = document.getElementById('message-thread');
    if (thread) {
      lastEventId = thread.getAttribute('data-last-event-id') || null;
    }

    connectSSE(scope, lastEventId);
  }

  // ── Diff expand toggle ───────────────────────────────────────────────────────

  function initDiffExpand() {
    // Click on a diff-stat row toggles the adjacent <details> block.
    document.addEventListener('click', function (e) {
      var target = e.target;
      if (!target) return;
      var row = target.closest('[data-diff-toggle]');
      if (!row) return;
      var targetId = row.getAttribute('data-diff-toggle');
      var details = document.getElementById(targetId);
      if (details && details.tagName === 'DETAILS') {
        details.open = !details.open;
      }
    });
  }

  // ── Utility ──────────────────────────────────────────────────────────────────

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ── Init ─────────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    initSSE();
    initDiffExpand();
  });

  // Clean up on page unload
  window.addEventListener('beforeunload', function () {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (activeSource) activeSource.close();
  });

}());
