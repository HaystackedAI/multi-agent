/* Live graph trace: opens an SSE stream to /api/sweep/stream and renders each frame as it
   arrives. No polling, no pausing — the run streams while it happens (the agent runtime cannot
   be interrupted; the human-in-the-loop interrupt is the approval cards after the run). */
(function () {
  "use strict";
  var btn = document.getElementById("trace-btn");
  var panel = document.getElementById("trace-panel");
  var list = document.getElementById("trace");
  var status = document.getElementById("trace-status");
  var source = null;
  var t0 = 0;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  }

  function tag(node) {
    return node ? '<span class="agent-tag ' + esc(node) + '">' + esc(node) + "</span>" : "";
  }

  function body(f) {
    switch (f.kind) {
      case "node_enter":
        return tag(f.node) + " <b>enters</b>";
      case "thinking":
        return tag(f.node) + ' <span class="muted">thinking…</span>';
      case "said":
        return tag(f.node) + " " + esc(String(f.text).slice(0, 300));
      case "tool_call":
        return (
          tag(f.node) +
          " → <code>" + esc(f.tool) + "</code> " +
          '<span class="muted small">' + esc(f.at || "") + "</span>" +
          '<pre class="tio">' + esc(JSON.stringify(f.input)) + "</pre>"
        );
      case "tool_result":
        return (
          tag(f.node) + " ✓ <code>" + esc(f.tool) + "</code> " +
          '<span class="badge ' + (f.status === "gated" ? "gated" : "") + '">' + esc(f.status) + "</span> " +
          '<span class="muted small">' + esc(f.output || "") + "</span>"
        );
      case "edge":
        return (
          '<span class="edge">edge ' + esc(f.name) + "</span>: <b>" + esc(String(f.value)) + "</b> " +
          '<span class="muted small">' + esc(f.detail || "") + "</span>"
        );
      case "done":
        if (f.skipped)
          return '<span class="muted">nothing to trace — ' + esc(f.note || "a close is already running") + "</span>";
        return f.ok
          ? "<b>done</b> · report saved · " + ((f.pending || []).length) + " pending decisions"
          : '<b class="err">failed</b>: ' + esc(f.error || "");
      default:
        return esc(JSON.stringify(f));
    }
  }

  function render(f) {
    var li = document.createElement("li");
    li.className = "trace-row trace-" + esc(f.kind);
    var dt = t0 ? "+" + ((Date.now() - t0) / 1000).toFixed(1) + "s" : "";
    li.innerHTML =
      '<span class="seq">' + esc(f.seq || "") + "</span>" +
      '<span class="dt muted small">' + dt + "</span> " +
      body(f);
    list.appendChild(li);
    // Append only; never move the viewport — the reader controls scrolling.
  }

  function stop(label) {
    if (source) { source.close(); source = null; }
    if (btn) btn.disabled = false;
    if (status) status.textContent = label;
  }

  function start() {
    if (source) stop("");
    if (panel) panel.classList.remove("hidden");
    if (list) list.innerHTML = "";
    if (status) status.textContent = "running…";
    if (btn) btn.disabled = true;
    t0 = Date.now();
    source = new EventSource("/api/sweep/stream");
    source.onmessage = function (e) {
      var f;
      try { f = JSON.parse(e.data); } catch (err) { return; }
      render(f);
      if (f.kind === "done") stop(f.skipped ? "a close is already running" : f.ok ? "finished" : "failed");
    };
    source.onerror = function () { stop("stream closed"); };
  }

  if (btn) btn.addEventListener("click", start);
})();
