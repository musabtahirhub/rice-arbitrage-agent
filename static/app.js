/* Dashboard controller: state, polling, routing and API calls. Rendering lives in components.js (window.UI). */
(function () {
  const POLL_MS = 5000;
  const STORE_KEY = "arb_desk_campaigns";
  const $ = (id) => document.getElementById(id);

  const state = {
    ids: [],              // campaign ids this browser knows about (the backend exposes no list route)
    data: {},             // id -> latest GET /api/campaigns/{id} payload
    selected: null,
    feedFilter: "all",
    threadFilter: "all",
    listSupported: true,  // flips off if GET /api/campaigns is not served
    lastRender: "",
  };

  /* ---------- helpers ---------- */
  const store = {
    load() { try { return JSON.parse(localStorage.getItem(STORE_KEY)) || []; } catch { return []; } },
    save() { try { localStorage.setItem(STORE_KEY, JSON.stringify(state.ids)); } catch { /* storage unavailable */ } },
  };
  function toast(msg, kind = "ok") {
    const el = document.createElement("div");
    el.className = "toast " + kind; el.textContent = msg;
    $("toasts").appendChild(el); setTimeout(() => el.remove(), 4500);
  }
  async function api(path, opts) {
    const res = await fetch(path, opts);
    let body = null;
    try { body = await res.json(); } catch { /* non-JSON body */ }
    if (!res.ok) { const e = new Error(body?.detail || res.statusText || "Request failed"); e.status = res.status; throw e; }
    return body;
  }
  const post = (path, payload) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const icons = () => window.lucide && window.lucide.createIcons();
  function remember(id) { if (id && !state.ids.includes(id)) { state.ids.unshift(id); store.save(); } }

  /* ---------- data loading ---------- */
  async function discoverIds() {
    if (!state.listSupported) return;
    try {
      const body = await api("/api/campaigns");
      const rows = Array.isArray(body) ? body : body?.campaigns;
      (rows || []).forEach((r) => remember(typeof r === "string" ? r : r.campaign_id || r.id));
    } catch (e) {
      if (e.status === 404 || e.status === 405) state.listSupported = false; // fall back to locally tracked ids
    }
  }
  async function loadCampaign(id) {
    try { state.data[id] = await api("/api/campaigns/" + encodeURIComponent(id)); }
    catch (e) {
      if (e.status === 404) { state.ids = state.ids.filter((x) => x !== id); delete state.data[id]; store.save(); }
    }
  }
  async function checkHealth() {
    const badge = $("healthBadge");
    let ok = false;
    try { await api("/api/health"); ok = true; }
    catch (e) { if (e.status === 404 || e.status === 405) { try { await api("/api/directory"); ok = true; } catch { /* offline */ } } }
    badge.className = "badge " + (ok ? "badge-green" : "badge-red");
    badge.textContent = ok ? "Desk live" : "API offline";
  }
  async function refresh() {
    await Promise.all([checkHealth(), discoverIds()]);
    await Promise.all(state.ids.map(loadCampaign));
    if (state.selected && !state.data[state.selected]) state.selected = null;
    if (!state.selected && state.ids.length) state.selected = state.ids.find((i) => state.data[i]) || null;
    $("lastSync").textContent = "synced " + new Date().toLocaleTimeString();
    render();
  }

  /* ---------- rendering ---------- */
  const campaigns = () => state.ids.map((i) => state.data[i]).filter(Boolean);
  const isActive = (c) => !["closed", "rejected", "supplier_lock_failed"].includes(c.deal_status);

  function renderKpis() {
    const all = campaigns();
    const closed = all.filter((c) => c.deal_status === "closed");
    const vol = closed.reduce((s, c) => s + (c.buyer_terms?.quantity_mt || 0), 0);
    const margins = all.filter((c) => c.buyer_terms || c.deal_status === "closed")
      .map((c) => c.final_net_margin_pct ?? c.net_margin_pct).filter((m) => typeof m === "number");
    const avg = margins.length ? margins.reduce((a, b) => a + b, 0) / margins.length : null;
    const pending = all.filter((c) => c.deal_status === "pending_approval").length;
    $("kpiActive").textContent = all.filter(isActive).length;
    $("kpiVolume").textContent = vol.toLocaleString();
    $("kpiMargin").textContent = avg === null ? "–" : avg.toFixed(2) + "%";
    $("kpiMargin").className = "kpi-value " + (avg === null ? "" : avg > 0 ? "text-green-400" : "text-red-400");
    $("kpiPending").textContent = pending;
    $("kpiPending").className = "kpi-value " + (pending ? "text-amber-400" : "");
  }
  function renderFeed() {
    let list = campaigns();
    if (state.feedFilter === "pending") list = list.filter((c) => c.deal_status === "pending_approval");
    if (state.feedFilter === "locked") list = list.filter((c) => c.deal_status === "closed");
    $("campaignList").innerHTML = list.length ? list.map((c) => UI.campaignRow(c, c.campaign_id === state.selected)).join("") : UI.emptyFeed();
    document.querySelectorAll("#feedFilters .tab").forEach((t) => t.classList.toggle("active", t.dataset.filter === state.feedFilter));
  }
  function renderInspector() {
    const c = state.data[state.selected];
    const box = $("inspector");
    const sig = JSON.stringify([c, state.threadFilter]);
    if (sig === state.lastRender) return; // nothing changed: keep scroll position and unsent drafts
    state.lastRender = sig;
    if (!c) { box.innerHTML = UI.emptyInspector(); return; }
    const draft = $("emailInput")?.value || "";
    const role = document.querySelector('input[name="senderRole"]:checked')?.value;
    const open = box.querySelector("details")?.open;
    const scroll = $("threadScroll");
    const atBottom = !scroll || scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 30;
    box.innerHTML = UI.inspector(c, state.threadFilter);
    if ($("emailInput")) { $("emailInput").value = draft; if (draft) box.querySelector("details").open = true; }
    if (open) box.querySelector("details").open = true;
    if (role) { const r = document.querySelector(`input[name="senderRole"][value="${role}"]`); if (r) r.checked = true; }
    const ns = $("threadScroll"); if (ns && atBottom) ns.scrollTop = ns.scrollHeight;
  }
  function render() { renderKpis(); renderFeed(); renderInspector(); icons(); }

  function select(id) { state.selected = id; state.lastRender = ""; render(); }

  /* ---------- modals ---------- */
  const open = (id) => { $(id).classList.remove("hidden"); icons(); };
  const close = (id) => $(id).classList.add("hidden");
  document.querySelectorAll(".modal-backdrop").forEach((m) => {
    m.addEventListener("click", (e) => { if (e.target === m || e.target.closest("[data-close]")) m.classList.add("hidden"); });
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelectorAll(".modal-backdrop").forEach((m) => m.classList.add("hidden")); });

  /* ---------- actions ---------- */
  async function launchCampaign(e) {
    e.preventDefault();
    const auto = $("fAuto").checked;
    const payload = {
      commodity: $("fCommodity").value,
      target_volume_mt: parseFloat($("fVolume").value),
      target_margin_pct: 10.0,
      max_variance_from_benchmark_pct: 5.0,
      destination_port: $("fPort").value,
      min_profit_per_mt_hard: parseFloat($("fHard").value),
      min_profit_per_mt_soft: parseFloat($("fSoft").value),
      max_negotiation_rounds: parseInt($("fRounds").value, 10),
      auto_run: auto,
    };
    const btn = $("launchBtn"); btn.disabled = true; btn.textContent = auto ? "Running…" : "Launching…";
    try {
      const data = await post("/api/campaigns", payload);
      remember(data.campaign_id); close("campaignModal");
      state.selected = data.campaign_id; state.lastRender = "";
      toast("Campaign " + data.campaign_id + " launched");
      await refresh();
    } catch (err) { toast("Launch failed: " + err.message, "error"); }
    finally { btn.disabled = false; btn.textContent = "Launch"; }
  }

  async function submitEmail(e) {
    e.preventDefault();
    const raw = $("emailInput").value.trim();
    if (!raw) return toast("Enter email text first", "error");
    const role = document.querySelector('input[name="senderRole"]:checked').value;
    try {
      await post("/api/negotiate", { campaign_id: state.selected, sender_role: role, raw_email: raw });
      $("emailInput").value = "";
      toast("Evaluation complete"); await refresh();
    } catch (err) { toast(err.message, "error"); await refresh(); }
  }

  async function decide(approved) {
    const c = state.data[state.selected]; if (!c) return;
    const ov = parseFloat($("aOverride").value);
    const payload = { approved, override_cif_price: isNaN(ov) ? null : ov, reviewer_notes: $("aNotes").value.trim() || null };
    const btns = [$("approveBtn"), $("rejectBtn")]; btns.forEach((b) => (b.disabled = true));
    try {
      await post(`/api/campaigns/${encodeURIComponent(c.campaign_id)}/approve`, payload);
      close("approvalModal"); toast(approved ? "Deal approved — supplier locked, buyer confirmed" : "Deal rejected");
      state.lastRender = ""; await refresh();
    } catch (err) { toast(err.message, "error"); await refresh(); }
    finally { btns.forEach((b) => (b.disabled = false)); }
  }

  /* ---------- events ---------- */
  document.addEventListener("click", (e) => {
    const row = e.target.closest("[data-campaign]"); if (row) return select(row.dataset.campaign);
    const f = e.target.closest("#feedFilters [data-filter]"); if (f) { state.feedFilter = f.dataset.filter; return renderFeed(); }
    const t = e.target.closest("[data-thread]"); if (t) { state.threadFilter = t.dataset.thread; return render(); }
    if (e.target.closest('[data-action="open-approval"]')) {
      const c = state.data[state.selected]; if (!c) return;
      $("approvalSummary").innerHTML = UI.approvalSummary(c); $("aOverride").value = ""; $("aNotes").value = ""; open("approvalModal");
    }
  });
  document.addEventListener("submit", (e) => { if (e.target.id === "negotiateForm") submitEmail(e); });
  $("campaignForm").addEventListener("submit", launchCampaign);
  $("newCampaignBtn").addEventListener("click", () => open("campaignModal"));
  $("refreshBtn").addEventListener("click", refresh);
  $("approveBtn").addEventListener("click", () => decide(true));
  $("rejectBtn").addEventListener("click", () => decide(false));

  /* ---------- boot ---------- */
  state.ids = store.load();
  render();
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
})();
