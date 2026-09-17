/* Chaser decision inbox: vanilla JS, polls /api/state every 3 s (every 2 s while the close runs). */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const POLL_MS = 3000;
  const POLL_FAST_MS = 2000;
  let state = null;
  let wasRunning = false;
  let runningSince = null;
  let pollTimer = null;
  let refreshing = false;
  const drafts = {}; // decision id -> edited fields kept across polls

  const money = (n) => "$" + Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");
  const TIER_LABEL = { 0: "hold", 1: "gentle", 2: "firm", 3: "final", 4: "escalate" };

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    if (!res.ok) throw new Error((await res.text()) || res.statusText);
    return res.json();
  }

  function banner(msg, isError) {
    const el = $("#banner");
    if (!msg) { el.classList.add("hidden"); return; }
    el.textContent = msg;
    el.classList.toggle("error", !!isError);
    el.classList.remove("hidden");
  }

  // ------------------------------------------------------------------ approvals
  function tierBadge(d) {
    if (d.kind === "review") return '<span class="badge badge-review">review deposit</span>';
    const text = (d.tool_input.subject || "") + " " + (d.tool_input.body || "");
    if (d.tool_name === "send_client_email" && /resend/i.test(text)) return '<span class="badge badge-resend">resend invoice</span>';
    if (d.tool_name === "offer_payment_plan") return '<span class="badge tier-4">payment plan</span>';
    if (d.tool_name === "propose_write_off") return '<span class="badge tier-4">write-off</span>';
    const t = d.tier ?? 0;
    return `<span class="badge tier-${t}">tier ${t} · ${TIER_LABEL[t] || "reminder"}</span>`;
  }

  function invoiceFacts(d) {
    const inv = d.invoice;
    if (!inv) return "";
    return `<div class="facts">
      <span><b>${esc(inv.id)}</b> ${esc(inv.description || "")}</span>
      <span>outstanding <b>${money(inv.outstanding)}</b></span>
      <span>due <b>${esc(inv.due)}</b></span>
      <span><b>${inv.days_overdue}</b> days overdue</span>
      ${inv.payment_link ? `<a href="${esc(inv.payment_link)}" target="_blank" rel="noopener">payment link</a>` : ""}
    </div>`;
  }

  function approvalCard(d) {
    const ed = drafts[d.id] || {};
    const ti = d.tool_input;
    let body = "";
    if (d.tool_name === "send_client_email") {
      body = `<div class="email-meta">
        <span class="muted">To</span><input data-field="to" value="${esc(ed.to ?? ti.to)}" />
        <span class="muted">Subject</span><input data-field="subject" value="${esc(ed.subject ?? ti.subject)}" />
      </div>
      <textarea class="email-preview" data-field="body">${esc(ed.body ?? ti.body)}</textarea>`;
    } else if (d.tool_name === "offer_payment_plan") {
      body = `<div class="email-meta"><span class="muted">Terms</span><input data-field="terms" value="${esc(ed.terms ?? ti.terms)}" /></div>`;
    } else if (d.tool_name === "propose_write_off") {
      body = `<div class="card-reason">Reason: ${esc(ti.reason)}</div>`;
    }
    return `<div class="card" data-id="${d.id}">
      <div class="card-head">${tierBadge(d)}<span class="card-title">${esc(d.summary)}</span><span class="muted small">by ${esc(d.agent)}</span></div>
      ${invoiceFacts(d)}
      ${body}
      <div class="card-actions">
        <button class="btn btn-approve btn-sm" data-act="approve">Approve</button>
        <button class="btn btn-sm" data-act="approve-edits">Approve with edits</button>
        <span class="spacer"></span>
        <button class="btn btn-ghost btn-sm" data-act="skip">Skip this week</button>
      </div>
    </div>`;
  }

  function reviewCard(d) {
    const ti = d.tool_input;
    const options = (d.open_invoices || []).map((i) => `<option value="${esc(i.id)}">${esc(i.id)} · ${esc(i.client)} · ${money(i.outstanding)}</option>`).join("");
    return `<div class="card" data-id="${d.id}">
      <div class="card-head">${tierBadge(d)}<span class="card-title">${esc(d.summary)}</span><span class="muted small">by ${esc(d.agent)}</span></div>
      <div class="facts"><span>deposit <b>${esc(ti.deposit_id)}</b></span><span><b>${money(ti.amount)}</b> on ${esc(ti.date)}</span><span>memo <b>${esc(ti.memo)}</b></span></div>
      <div class="card-reason">${esc(ti.reason)}</div>
      <div class="card-actions">
        <select data-field="invoice_id"><option value="">Match to invoice…</option>${options}</select>
        <button class="btn btn-approve btn-sm" data-act="match">Match</button>
        <button class="btn btn-sm" data-act="other-income">Mark as other income</button>
        <span class="spacer"></span>
        <button class="btn btn-ghost btn-sm" data-act="skip">Skip this week</button>
      </div>
    </div>`;
  }

  function renderApprovals() {
    const root = $("#approvals");
    const pending = state.pending || [];
    $("#pending-count").textContent = pending.length;
    if (!pending.length) {
      root.innerHTML = `<div class="empty">Nothing waiting on you. ${state.last_sweep_at ? "The last close needed no decisions that are still open." : "Run the weekly close to see proposals."}</div>`;
      return;
    }
    const groups = {};
    pending.forEach((d) => { const k = d.client || (d.kind === "review" ? "Unexplained deposits" : "Other"); (groups[k] = groups[k] || []).push(d); });
    root.innerHTML = Object.keys(groups).sort().map((client) => `<div class="client-group"><h3>${esc(client)}</h3>${groups[client].map((d) => (d.kind === "review" ? reviewCard(d) : approvalCard(d))).join("")}</div>`).join("");

    root.querySelectorAll("[data-field]").forEach((el) => {
      el.addEventListener("input", () => {
        const id = el.closest(".card").dataset.id;
        drafts[id] = drafts[id] || {};
        drafts[id][el.dataset.field] = el.value;
      });
    });
    root.querySelectorAll("button[data-act]").forEach((btn) => btn.addEventListener("click", onDecision));
  }

  async function onDecision(ev) {
    const btn = ev.currentTarget;
    const card = btn.closest(".card");
    const id = card.dataset.id;
    const act = btn.dataset.act;
    const edits = {};
    let response = "yes";
    if (act === "skip") response = "no";
    if (act === "approve-edits") Object.assign(edits, drafts[id] || {});
    if (act === "match") {
      const sel = card.querySelector('select[data-field="invoice_id"]');
      if (!sel.value) { banner("Pick an invoice to match first.", true); return; }
      edits.invoice_id = sel.value;
    }
    if (act === "other-income") response = "other income";
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      const out = await api(`/api/decisions/${id}`, { method: "POST", body: JSON.stringify({ response, edits }) });
      delete drafts[id];
      banner(out.ok ? `Done: ${out.decision.summary} → ${out.decision.status}` : `Failed: ${out.error || "unknown error"}`, !out.ok);
      setTimeout(() => banner(""), 4000);
      await refresh();
    } catch (e) {
      banner(e.message, true);
      card.querySelectorAll("button").forEach((b) => (b.disabled = false));
    }
  }

  // ------------------------------------------------------------------ aging
  function renderAging() {
    const a = state.aging;
    const total = Object.values(a).reduce((s, b) => s + b.total, 0) || 1;
    const labels = { current: "Current (not yet due)", d1_30: "1-30 days", d31_60: "31-60 days", d61_plus: "61+ days" };
    $("#cash-week").textContent = `collected this week ${money(state.cash_collected_this_week)}`;
    $("#aging").innerHTML = Object.keys(labels).map((k) => {
      const b = a[k];
      return `<div class="bucket">
        <div class="bucket-row"><span>${labels[k]} <span class="muted">(${b.count})</span></span><b>${money(b.total)}</b></div>
        <div class="bar ${k}"><span style="width:${Math.max(2, (b.total / total) * 100)}%"></span></div>
        <div class="chips">${b.invoices.map((i) => `<span class="chip">${esc(i.client)} <b>${esc(i.id)}</b> ${money(i.outstanding)}${i.status === "partial" ? " · partial" : ""}</span>`).join("")}</div>
      </div>`;
    }).join("") + `<div class="bucket-row" style="margin-top:8px;border-top:1px solid var(--line);padding-top:8px"><span>Total outstanding</span><b>${money(total === 1 ? 0 : total)}</b></div>`;
  }

  // ------------------------------------------------------------------ report
  function renderReport() {
    const r = state.report;
    const root = $("#report");
    if (!r) { root.innerHTML = '<div class="empty">No close has run yet.</div>'; $("#report-meta").textContent = ""; return; }
    $("#report-meta").textContent = `${r.period_start} to ${r.period_end} · generated ${fmtTime(r._created_at)}`;
    const o = r.outstanding || {};
    const rec = r.reconciliation || {};
    const b = r.books || {};
    root.innerHTML = `
      <div class="kpis">
        <div class="kpi"><div class="label">Cash collected</div><div class="value">${money(r.cash_collected_this_week)}</div></div>
        <div class="kpi"><div class="label">Outstanding 1-30</div><div class="value">${money(o.d1_30)}</div></div>
        <div class="kpi"><div class="label">31-60</div><div class="value">${money(o.d31_60)}</div></div>
        <div class="kpi"><div class="label">61+</div><div class="value">${money(o.d61_plus)}</div></div>
      </div>
      <div class="narrative">${esc(r.narrative)}</div>
      ${(r.top_things_to_know || []).length ? `<h4>Top things to know</h4><ul>${r.top_things_to_know.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
      <h4>Reconciliation</h4><ul>
        <li>${rec.deposits_matched ?? 0} deposits matched, ${rec.partial_payments_recorded ?? 0} partial payment(s), ${rec.deposits_flagged_for_review ?? 0} flagged for review</li>
        ${(rec.notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}
      </ul>
      <h4>Books</h4><ul><li>${b.expenses_categorized ?? 0} expenses categorized, ${b.receipts_matched ?? 0} receipts matched, ${b.expenses_missing_receipts ?? 0} still missing receipts, ${b.todos_created ?? 0} to-do(s) created</li></ul>
      ${(r.drafts_awaiting_approval || []).length ? `<h4>Awaiting approval</h4><ul>${r.drafts_awaiting_approval.map((d) => `<li>${esc(d.client)} · ${esc(d.invoice_id)} · tier ${d.tier} · ${money(d.amount)} · ${esc(d.action)}</li>`).join("")}</ul>` : ""}
      <div class="pipeline"><span class="muted">Graph run:</span>${(r.execution_order || []).map((n, i) => `${i ? '<span class="arrow">→</span>' : ""}<span class="agent-tag ${n}">${n}</span>`).join("")}</div>`;
  }

  // ------------------------------------------------------------------ books
  function renderBooks() {
    const todos = state.todos || [];
    const missing = state.missing_receipts || [];
    const uncategorized = state.uncategorized_expenses || [];
    const unmatched = state.unmatched_deposits || [];
    $("#books").innerHTML = `
      <div class="books-section"><h4>To-dos (${todos.length})</h4>${todos.length ? todos.map((t) => `<div class="todo"><span>${esc(t.title)}</span><span class="due">${esc(t.due_date || "")}</span></div>`).join("") : '<div class="muted small">None open.</div>'}</div>
      <div class="books-section"><h4>Missing receipts (${missing.length})</h4>${missing.length ? missing.map((e) => `<div class="todo"><span>${esc(e.merchant)} ${money(Math.abs(e.amount))}</span><span class="due">${e.age_days}d</span></div>`).join("") : '<div class="muted small">All receipts attached.</div>'}</div>
      <div class="books-section"><h4>Uncategorized expenses (${uncategorized.length})</h4>${uncategorized.length ? `<div class="chips">${uncategorized.map((e) => `<span class="chip">${esc(e.merchant)} <b>${money(Math.abs(e.amount))}</b></span>`).join("")}</div>` : '<div class="muted small">Everything categorized.</div>'}</div>
      <div class="books-section"><h4>Unmatched deposits (${unmatched.length})</h4>${unmatched.length ? `<div class="chips">${unmatched.map((d) => `<span class="chip">${esc(d.memo)} <b>${money(d.amount)}</b></span>`).join("")}</div>` : '<div class="muted small">All deposits explained.</div>'}</div>`;
  }

  // ------------------------------------------------------------------ activity + resolved
  function renderActivity() {
    const items = (state.activity || []).filter((a) => a.kind !== "read" || false);
    const list = $("#activity");
    if (!items.length) { list.innerHTML = '<li class="empty" style="display:block">No activity yet.</li>'; return; }
    list.innerHTML = items.map((a) => `<li class="${a.kind}"><span class="agent-tag ${esc(a.agent)}">${esc(a.agent)}</span><span>${esc(a.summary || a.tool_name)}</span><span class="status ${esc(a.status)}">${esc(a.status)} · ${fmtTime(a.created_at)}</span></li>`).join("");
  }

  function renderResolved() {
    const items = state.resolved || [];
    $("#resolved").innerHTML = items.length
      ? items.map((d) => `<li><span class="pill ${esc(d.status)}">${esc(d.status)}</span><span>${esc(d.summary)}</span></li>`).join("")
      : '<li class="muted small">Nothing decided yet.</li>';
  }

  // ------------------------------------------------------------------ header
  function renderHeader() {
    const btn = $("#run-btn");
    const running = state.sweep_running;
    btn.disabled = running;
    btn.innerHTML = running ? '<span class="spinner"></span>Running weekly close…' : "Run weekly close";
    $("#last-run").textContent = state.last_sweep_at ? `Last close ${fmtTime(state.last_sweep_at)} · ${state.last_sweep_status}` : "No close run yet";
    if (state.last_error) banner(`Last sweep error: ${state.last_error}`, true);
    if (wasRunning && !running) { banner("Weekly close finished."); setTimeout(() => banner(""), 3500); }
    wasRunning = running;
    runningSince = running ? Date.now() - (state.running_for_seconds || 0) * 1000 : null;
  }

  // ------------------------------------------------------------------ live strip
  const AGENT_LABEL = { reconciler: "Reconciler", collector: "Collector", bookkeeper: "Bookkeeper", reporter: "Reporter" };

  function renderLive() {
    const el = $("#live");
    const running = !!state.sweep_running;
    el.classList.toggle("hidden", !running);
    if (!running) return;
    const lines = (state.progress || []).slice(-5);
    const last = lines[lines.length - 1];
    $("#live-title").textContent = last
      ? `Weekly close running: ${AGENT_LABEL[last.agent] || last.agent} is working`
      : "Weekly close running: starting the four-agent graph";
    $("#live-lines").innerHTML = lines
      .filter((p, i) => p.kind !== "thinking" || i === lines.length - 1)
      .map((p) => {
        const text = p.text.length > 180 ? p.text.slice(0, 177) + "..." : p.text;
        return `<li class="${esc(p.kind)}"><span class="who">${esc(AGENT_LABEL[p.agent] || p.agent)}</span>${esc(text)}</li>`;
      })
      .join("");
    tickElapsed();
  }

  function tickElapsed() {
    const el = $("#live-elapsed");
    if (!el) return;
    if (runningSince == null) { el.textContent = ""; return; }
    const s = Math.max(0, Math.round((Date.now() - runningSince) / 1000));
    el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  async function refresh() {
    if (refreshing) return; // a slow /api/state (the runtime is busy) must not stack up requests
    refreshing = true;
    try {
      state = await api("/api/state");
      renderHeader(); renderLive(); renderApprovals(); renderAging(); renderReport(); renderBooks(); renderActivity(); renderResolved();
    } catch (e) {
      banner(`Cannot reach the API: ${e.message}`, true);
    } finally {
      refreshing = false;
      clearTimeout(pollTimer);
      pollTimer = setTimeout(refresh, state && state.sweep_running ? POLL_FAST_MS : POLL_MS);
    }
  }

  $("#run-btn").addEventListener("click", async () => {
    try {
      const out = await api("/api/sweep", { method: "POST" });
      if (!out.started) banner(out.reason || "Sweep not started", true);
      wasRunning = true;
      await refresh();
    } catch (e) { banner(e.message, true); }
  });

  $("#ask-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const q = $("#ask-input").value.trim();
    if (!q) return;
    const out = $("#ask-answer");
    out.innerHTML = '<span class="spinner"></span>Thinking…';
    try {
      const res = await api("/api/ask", { method: "POST", body: JSON.stringify({ prompt: q }) });
      out.textContent = res.answer || res.error || "(no answer)";
    } catch (e) { out.textContent = e.message; }
  });

  refresh();
  setInterval(tickElapsed, 1000);
})();
