/* Pure render helpers: take plain data, return HTML strings. All user/email text goes through esc(). */
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);
  const usd = (v, d = 2) => (num(v) === null ? "–" : "$" + v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
  const pct = (v) => (num(v) === null ? "–" : v.toFixed(2) + "%");
  const icon = (name, cls = "w-3.5 h-3.5") => `<i data-lucide="${name}" class="${cls}"></i>`;

  /* ---- Status model ---- */
  const STATUS = {
    initiating: { label: "Outreach", tone: "blue" },
    prospecting: { label: "Outreach", tone: "blue" },
    counter_sent: { label: "Negotiating", tone: "blue" },
    negotiating: { label: "Negotiating", tone: "blue" },
    pending_approval: { label: "Pending Approval", tone: "amber", pulse: true },
    closed: { label: "Locked", tone: "green" },
    rejected: { label: "Rejected", tone: "red" },
    supplier_lock_failed: { label: "Lock Failed", tone: "red" },
  };
  const statusInfo = (s) => STATUS[s] || { label: s ? String(s).replace(/_/g, " ") : "Unknown", tone: "gray" };

  function statusBadge(status) {
    const i = statusInfo(status);
    return `<span class="badge badge-${i.tone}${i.pulse ? " pulse" : ""}">${esc(i.label)}</span>`;
  }

  /* Margin health: green = at/above soft target, amber = above hard floor, red = below floor. */
  function marginTone(c) {
    const spread = num(c.net_spread_usd);
    if (c.deal_status === "rejected" || c.deal_status === "supplier_lock_failed") return "red";
    if (spread === null || (spread === 0 && !c.buyer_terms)) return "gray";
    if (spread < (c.min_profit_per_mt_hard || 0)) return "red";
    if (c.deal_status === "pending_approval" || spread < (c.min_profit_per_mt_soft || 0)) return "amber";
    return "green";
  }
  function marginBadge(c) {
    const tone = marginTone(c);
    const label = { green: "Profitable", amber: "Review", red: "Below floor", gray: "No quote" }[tone];
    return `<span class="badge badge-${tone}">${label}</span>`;
  }

  /* ---- Campaign feed row ---- */
  function campaignRow(c, active) {
    const vol = c.buyer_terms?.quantity_mt ?? c.target_volume_mt;
    return `<button class="camp-row${active ? " active" : ""}" data-campaign="${esc(c.campaign_id)}">
      <div class="flex items-center justify-between gap-2">
        <span class="mono text-xs font-semibold">${esc(c.campaign_id)}</span>${statusBadge(c.deal_status)}
      </div>
      <div class="text-[11px] text-slate-400 mt-1 truncate">${esc(c.commodity)}${num(vol) ? " · " + vol.toLocaleString() + " MT" : ""}</div>
      <div class="flex items-center justify-between mt-1.5 text-[11px]">
        <span class="text-slate-500">Rd ${esc(c.negotiation_round ?? 0)}/${esc(c.max_negotiation_rounds ?? "–")}</span>
        <span class="mono text-slate-300">${usd(c.net_spread_usd)}/MT ${marginBadge(c)}</span>
      </div></button>`;
  }

  /* ---- Pricing cards ---- */
  function priceCard(label, value, hint, accent) {
    return `<div class="price-card${accent ? " accent-" + accent : ""}"><div class="label">${esc(label)}</div><div class="value">${value}</div>${hint ? `<div class="hint">${esc(hint)}</div>` : ""}</div>`;
  }

  function spreadGauge(c) {
    const spread = num(c.net_spread_usd) ?? 0;
    const hard = c.min_profit_per_mt_hard || 0, soft = c.min_profit_per_mt_soft || 0;
    const max = Math.max(soft * 1.4, spread * 1.1, 1);
    const w = Math.max(0, Math.min(100, (spread / max) * 100));
    const color = { green: "var(--green)", amber: "var(--amber)", red: "var(--red)", gray: "#475569" }[marginTone(c)];
    return `<div class="mt-3"><div class="flex justify-between text-[10px] text-slate-400 mb-1.5"><span>Net spread</span><span class="mono">${usd(spread)} / soft ${usd(soft, 0)}</span></div>
      <div class="gauge"><div class="gauge-fill" style="width:${w}%;background:${color}"></div>
      <div class="gauge-mark" style="left:${(hard / max) * 100}%" title="Hard floor"></div>
      <div class="gauge-mark" style="left:${(soft / max) * 100}%;background:var(--green)" title="Soft target"></div></div>
      <div class="flex justify-between text-[10px] mt-1.5"><span class="text-red-400">Hard ${usd(hard, 0)}</span><span class="text-green-400">Soft ${usd(soft, 0)}</span></div></div>`;
  }

  function pricingPanel(c) {
    const tone = marginTone(c);
    const accent = tone === "gray" ? "blue" : tone;
    const buyerBid = c.buyer_terms?.price_usd_per_mt, supplierAsk = c.supplier_terms?.price_usd_per_mt;
    const landed = num(supplierAsk) !== null && num(c.freight_cost_usd) !== null ? supplierAsk + c.freight_cost_usd : null;
    return `<div class="panel"><div class="panel-head"><span class="panel-title">CIF pricing &amp; margin</span>${marginBadge(c)}</div>
      <div class="p-3"><div class="grid grid-cols-2 gap-2">
        ${priceCard("Opening anchor", usd(c.anchor_cif_usd), "CIF $/MT cold SCO", "blue")}
        ${priceCard("CIF floor", usd(c.dynamic_cif_floor), "min viable quote", "red")}
        ${priceCard("FOB ceiling", usd(c.dynamic_fob_ceiling), "max acquisition", "amber")}
        ${priceCard("Freight", usd(c.freight_cost_usd), "per MT", null)}
        ${priceCard("Buyer bid (CIF)", usd(buyerBid), c.buyer_terms?.port || "", "green")}
        ${priceCard("Supplier ask (FOB)", usd(supplierAsk), landed === null ? "" : "landed " + usd(landed), null)}
      </div>
      <div class="grid grid-cols-2 gap-2 mt-2">
        ${priceCard("Net spread", usd(c.net_spread_usd) + "<span class='text-xs text-slate-500'>/MT</span>", null, accent)}
        ${priceCard("Net margin", pct(c.net_margin_pct), c.benchmark_fob_usd ? "benchmark FOB " + usd(c.benchmark_fob_usd, 0) : "", accent)}
      </div>${spreadGauge(c)}
      ${c.evaluation_reason ? `<p class="mt-3 text-[11px] text-slate-400 leading-relaxed">${icon("info", "w-3 h-3 inline mr-1")}${esc(c.evaluation_reason)}</p>` : ""}
      </div></div>`;
  }

  /* ---- Email bubbles ---- */
  function bubbleKind(e) {
    const role = String(e.role || "").toLowerCase();
    const inbound = String(e.action || "").toUpperCase() === "INBOUND";
    if (inbound) return role === "supplier" ? "supplier" : "buyer";
    return "desk";
  }
  /* Which counterparty thread an entry belongs to. */
  function threadOf(e) {
    const role = String(e.role || "").toLowerCase();
    if (role === "supplier") return "supplier";
    if (role === "buyer") return "buyer";
    const t = `${e.recipient || ""} ${e.subject || ""} ${e.message || ""}`.toLowerCase();
    return /rfq|supplier|volume lock|allocation|fob ceiling|mill/.test(t) && !/sco|soft corporate/.test(t) ? "supplier" : "buyer";
  }
  function emailBubble(e) {
    const kind = bubbleKind(e);
    const who = { buyer: "Buyer", supplier: "Supplier", desk: "Desk" }[kind];
    let body = String(e.message || "");
    let subject = e.subject || "";
    const m = body.match(/^subject:\s*(.*)\r?\n/i);
    if (m) { subject = subject || m[1]; body = body.slice(m[0].length).trim(); }
    const to = kind === "desk" ? ` → ${threadOf(e) === "supplier" ? "Supplier" : "Buyer"}` : "";
    return `<div class="bubble-row ${kind === "desk" ? "right" : ""}"><div class="bubble ${kind}">
      <div class="bubble-meta"><span class="bubble-who">${who}${to}</span><span class="text-slate-500 mono">#${esc(e.turn ?? "")}</span></div>
      ${subject ? `<div class="bubble-subject">${esc(subject)}</div>` : ""}<div class="bubble-body">${esc(body)}</div></div></div>`;
  }
  function threadPanel(c, filter) {
    const all = c.audit_transcript || [];
    const list = filter === "all" ? all : all.filter((e) => threadOf(e) === filter);
    const tabs = ["all", "buyer", "supplier"].map((t) => `<button class="tab${t === filter ? " active" : ""}" data-thread="${t}">${t[0].toUpperCase() + t.slice(1)}</button>`).join("");
    return `<div class="panel flex flex-col"><div class="panel-head"><span class="panel-title">Negotiation threads</span><div class="flex gap-1">${tabs}</div></div>
      <div id="threadScroll" class="scroll p-3 max-h-[560px] min-h-[200px]">${list.length ? list.map(emailBubble).join("") : `<p class="text-xs text-slate-500 text-center py-10">No messages in this thread yet.</p>`}</div></div>`;
  }

  /* ---- Audit timeline ---- */
  function auditTimeline(c) {
    const items = [];
    const t = c.audit_transcript || [];
    t.forEach((e) => {
      const kind = bubbleKind(e);
      const tone = { buyer: "green", supplier: "blue", desk: "amber" }[kind];
      const sub = e.subject || String(e.message || "").split("\n")[0].replace(/^subject:\s*/i, "");
      items.push(`<div class="tl-item"><span class="tl-dot ${tone}"></span><div class="text-[11px]"><b>${kind === "desk" ? "Desk sent" : (kind === "buyer" ? "Buyer replied" : "Supplier replied")}</b> <span class="text-slate-500 mono">#${esc(e.turn ?? "")}</span></div><div class="text-[11px] text-slate-400 truncate">${esc(sub.slice(0, 90))}</div></div>`);
    });
    const info = statusInfo(c.deal_status);
    items.push(`<div class="tl-item"><span class="tl-dot ${info.tone}"></span><div class="text-[11px]"><b>Now: ${esc(info.label)}</b>${c.action ? ` <span class="text-slate-500">· ${esc(c.action)}</span>` : ""}</div></div>`);
    return `<div class="panel"><div class="panel-head"><span class="panel-title">Audit log</span><span class="text-[10px] text-slate-500">${t.length} events</span></div>
      <div class="p-3 scroll max-h-[320px]"><div class="timeline">${items.join("")}</div></div></div>`;
  }

  /* ---- Pipeline stepper ---- */
  function pipeline(c) {
    const labels = ["Outreach", "Buyer terms", "Supplier RFQ", "Close"];
    const step = c.deal_status === "closed" ? 5 : (c.pipeline_step || 1);
    return `<div class="flex gap-1">${labels.map((l, i) => `<div class="step ${i + 1 < step ? "done" : i + 1 === step ? "now" : ""}"><div class="n">${i + 1 < step ? "✓" : i + 1}</div>${l}</div>`).join("")}</div>`;
  }

  /* ---- Approval action center ---- */
  function approvalBanner(c) {
    if (c.deal_status !== "pending_approval") return "";
    return `<div class="panel border-amber-500/40 bg-amber-500/5 p-3 flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-2 text-xs text-amber-200">${icon("shield-alert", "w-4 h-4 text-amber-400")}<span><b>Human approval required.</b> Margin ${pct(c.net_margin_pct)} · spread ${usd(c.net_spread_usd)}/MT</span></div>
      <button class="btn btn-green" data-action="open-approval">${icon("gavel")}Review &amp; decide</button></div>`;
  }
  function approvalSummary(c) {
    const vol = c.buyer_terms?.quantity_mt, bid = c.buyer_terms?.price_usd_per_mt, ask = c.supplier_terms?.price_usd_per_mt;
    const row = (k, v) => `<div class="flex justify-between py-1.5 border-b border-[#1f2733] text-xs"><span class="text-slate-400">${k}</span><span class="mono">${v}</span></div>`;
    return `<div class="price-card accent-amber">${row("Campaign", esc(c.campaign_id))}${row("Commodity", esc(c.commodity))}${row("Volume", num(vol) ? vol.toLocaleString() + " MT" : "–")}
      ${row("Buyer bid CIF", usd(bid))}${row("Supplier ask FOB", usd(ask))}${row("Freight", usd(c.freight_cost_usd))}${row("Net spread", usd(c.net_spread_usd) + "/MT")}${row("Net margin", pct(c.net_margin_pct))}
      ${num(vol) && num(c.net_spread_usd) !== null ? row("Total profit", usd(vol * c.net_spread_usd, 0)) : ""}</div>`;
  }

  /* ---- Manual email feed (maps to POST /api/negotiate) ---- */
  function simulator(c) {
    const locked = c.deal_status === "pending_approval" || c.deal_status === "closed" || c.deal_status === "rejected";
    return `<details class="panel"><summary class="panel-head cursor-pointer select-none"><span class="panel-title">Inject inbound email</span><span class="text-[10px] text-slate-500">${locked ? "unavailable in this state" : "simulate buyer / supplier reply"}</span></summary>
      <form id="negotiateForm" class="p-3 space-y-2">
        <div class="flex gap-4 text-xs"><label class="flex items-center gap-1.5"><input type="radio" name="senderRole" value="buyer" checked /> Buyer</label><label class="flex items-center gap-1.5"><input type="radio" name="senderRole" value="supplier" /> Supplier</label></div>
        <textarea id="emailInput" rows="4" class="input mono" placeholder="Subject: …&#10;&#10;We offer USD 1085.00/MT CIF Jebel Ali for 500 MT."></textarea>
        <button class="btn btn-blue" ${locked ? "disabled" : ""}>${icon("send")}Run evaluation</button></form></details>`;
  }

  function emptyInspector() {
    return `<div class="panel flex flex-col items-center justify-center text-center py-24 px-6">${icon("layout-dashboard", "w-8 h-8 text-slate-600 mb-3")}
      <p class="text-sm font-semibold">No campaign selected</p><p class="text-xs text-slate-500 mt-1">Pick one from the feed or launch a new campaign.</p></div>`;
  }
  function emptyFeed() {
    return `<p class="text-xs text-slate-500 text-center py-10 px-4">No campaigns yet. Launch one to start negotiating.</p>`;
  }

  function inspector(c, threadFilter) {
    return `<div class="space-y-3">
      <div class="panel p-3 flex flex-wrap items-center justify-between gap-3">
        <div class="min-w-0"><div class="flex items-center gap-2"><span class="mono text-sm font-bold">${esc(c.campaign_id)}</span>${statusBadge(c.deal_status)}</div>
        <div class="text-[11px] text-slate-400 mt-0.5">${esc(c.commodity)} · round ${esc(c.negotiation_round ?? 0)}/${esc(c.max_negotiation_rounds ?? "–")}</div></div>
        <div class="w-full sm:w-80">${pipeline(c)}</div></div>
      ${approvalBanner(c)}
      <div class="inspector-split"><div class="space-y-3 min-w-0">${threadPanel(c, threadFilter)}${simulator(c)}</div>
      <div class="space-y-3 min-w-0">${pricingPanel(c)}${auditTimeline(c)}</div></div></div>`;
  }

  window.UI = { esc, usd, pct, statusInfo, statusBadge, marginTone, campaignRow, inspector, emptyInspector, emptyFeed, approvalSummary };
})();
