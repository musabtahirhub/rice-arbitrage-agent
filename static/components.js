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
    return `<div class="price-card tilt${accent ? " accent-" + accent : ""}"><div class="label">${esc(label)}</div><div class="value">${value}</div>${hint ? `<div class="hint">${esc(hint)}</div>` : ""}</div>`;
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
    const row = (k, v) => `<div class="flex justify-between py-1.5 border-b border-[var(--line)] text-xs"><span class="text-slate-400">${k}</span><span class="mono">${v}</span></div>`;
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

/* ---------------------------------------------------------------------------
 * 3D layer (Three.js r128): dark-studio grain backdrop, trade-route globe, margin cube, tilt-on-hover cards.
 * Everything degrades to a no-op when THREE / WebGL is unavailable.
 * ------------------------------------------------------------------------- */
(function () {
  const INK = 0x2A2723, TAUPE = 0x8a7f70, GOLD = 0xD4A359, FLUID = 0xE5B842, EMERALD = 0x2E7D32;
  // Readout colours (need to read on dark glass) and cube fluid tints per margin tone
  const TONE = { green: 0x2E7D32, amber: 0xA96F08, red: 0xbf4d3c, blue: TAUPE, gray: TAUPE };
  const TINT = { green: FLUID, amber: FLUID, red: 0xc9743a, blue: FLUID, gray: FLUID };   // warm gold fluid; rust below the floor
  const PORTS = {
    "Karachi": [24.86, 67.0, "origin"], "Mundra": [22.84, 69.7, "origin"],
    "Laem Chabang": [13.08, 100.88, "origin"], "Cat Lai": [10.77, 106.77, "origin"],
    "Jebel Ali": [25.0, 55.06, "dest"], "Dammam": [26.43, 50.1, "dest"],
  };
  const views = [];   // { renderer, scene, camera, el, visible, tick(t, dt) }
  let globe = null, cube = null;

  function makeView(el, fov, camZ) {
    if (!window.THREE) { el.innerHTML = '<div class="viz-fallback">3D view unavailable (Three.js failed to load).</div>'; return null; }
    let renderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true }); }
    catch (e) { el.innerHTML = '<div class="viz-fallback">3D view unavailable (WebGL is disabled in this browser).</div>'; return null; }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    el.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(fov, 1, 0.1, 100);
    camera.position.set(0, 0, camZ);
    const v = { renderer, scene, camera, el, visible: true, tick() {} };
    const resize = () => {
      const w = el.clientWidth || 1, h = el.clientHeight || 1;
      renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
    };
    if (window.ResizeObserver) new ResizeObserver(resize).observe(el); else window.addEventListener("resize", resize);
    if (window.IntersectionObserver) new IntersectionObserver((es) => { v.visible = es[0].isIntersecting; }).observe(el);
    resize();
    views.push(v);
    return v;
  }

  /* Pointer drag -> rotation offset on a group. */
  function draggable(el, onDrag) {
    let last = null;
    el.addEventListener("pointerdown", (e) => { last = e; if (el.setPointerCapture) el.setPointerCapture(e.pointerId); });
    el.addEventListener("pointermove", (e) => { if (last) { onDrag(e.clientX - last.clientX, e.clientY - last.clientY); last = e; } });
    const end = () => { last = null; };
    el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end);
  }

  const ll = (lat, lon, r) => {
    const phi = (90 - lat) * Math.PI / 180, th = (lon + 180) * Math.PI / 180;
    return new THREE.Vector3(-r * Math.sin(phi) * Math.cos(th), r * Math.cos(phi), r * Math.sin(phi) * Math.sin(th));
  };

  /* ---------------- Globe ---------------- */
  function initGlobe(el) {
    const v = makeView(el, 40, 4.3); if (!v) return;
    const R = 1.5, group = new THREE.Group(); v.scene.add(group);
    v.scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const sun = new THREE.DirectionalLight(0xfff0dc, 0.9); sun.position.set(3, 3, 4); v.scene.add(sun);
    // Frosted white-glass body, white/taupe wire graticule, faint gold halo
    group.add(new THREE.Mesh(new THREE.SphereGeometry(R * 0.995, 48, 32), new THREE.MeshPhongMaterial({ color: 0xf6f1e8, specular: 0xffffff, shininess: 90, transparent: true, opacity: 0.66 })));
    group.add(new THREE.Mesh(new THREE.SphereGeometry(R, 24, 16), new THREE.MeshBasicMaterial({ color: 0x8f8068, wireframe: true, transparent: true, opacity: 0.4 })));
    v.scene.add(new THREE.Mesh(new THREE.SphereGeometry(R * 1.07, 32, 24), new THREE.MeshBasicMaterial({ color: 0xffd9a0, transparent: true, opacity: 0.18, side: THREE.BackSide })));
    Object.values(PORTS).forEach(([lat, lon, kind]) => {
      const m = new THREE.Mesh(new THREE.SphereGeometry(kind === "dest" ? 0.05 : 0.038, 12, 12), new THREE.MeshBasicMaterial({ color: kind === "dest" ? GOLD : INK }));
      m.position.copy(ll(lat, lon, R)); group.add(m);
    });
    const routes = new THREE.Group(); group.add(routes);
    const g = { v, group, routes, arcs: [], key: "", spin: 0.1, drag: 0 };
    // Face the Arabian Sea / Indian subcontinent first.
    const p = ll(24, 62, 1); group.rotation.y = -Math.atan2(p.x, p.z); group.rotation.x = 0.35;
    draggable(el, (dx, dy) => { group.rotation.y += dx * 0.008; group.rotation.x = Math.max(-1, Math.min(1, group.rotation.x + dy * 0.006)); g.drag = 1.5; });
    v.tick = (t, dt) => {
      g.drag = Math.max(0, g.drag - dt);
      if (!g.drag) group.rotation.y += g.spin * dt;
      g.arcs.forEach((a) => { a.dot.position.copy(a.curve.getPoint(((t * a.speed) + a.offset) % 1)); });
    };
    globe = g;
  }

  function originOf(c) {
    const n = String(c.commodity || "").toLowerCase();
    if (/thai|jasmine/.test(n)) return "Laem Chabang";
    if (/viet/.test(n)) return "Cat Lai";
    return "Karachi";
  }
  function destOf(c) { return /dammam|saudi/i.test((c.buyer_terms && c.buyer_terms.port) || c.destination_port || "") ? "Dammam" : "Jebel Ali"; }

  function updateGlobe(list, selected) {
    if (!globe) return;
    const live = list.filter((c) => c.deal_status !== "rejected" && c.deal_status !== "supplier_lock_failed");
    const specs = live.map((c) => ({ o: originOf(c), d: destOf(c), sel: c.campaign_id === selected }));
    const key = JSON.stringify(specs);
    if (key === globe.key) return;
    globe.key = key;
    const R = 1.5;
    globe.arcs.forEach((a) => { a.line.geometry.dispose(); a.line.material.dispose(); a.dot.geometry.dispose(); a.dot.material.dispose(); });
    globe.routes.clear(); globe.arcs = [];
    specs.forEach((s, i) => {
      const a = ll(PORTS[s.o][0], PORTS[s.o][1], R), b = ll(PORTS[s.d][0], PORTS[s.d][1], R);
      const mid = a.clone().add(b).multiplyScalar(0.5).normalize().multiplyScalar(R * (1.3 + a.distanceTo(b) * 0.25));
      const curve = new THREE.QuadraticBezierCurve3(a, mid, b);
            const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(48)),
        new THREE.LineBasicMaterial({ color: s.sel ? GOLD : INK, transparent: true, opacity: s.sel ? 1 : 0.55 }));
      const dot = new THREE.Mesh(new THREE.SphereGeometry(s.sel ? 0.06 : 0.04, 10, 10), new THREE.MeshBasicMaterial({ color: s.sel ? 0xfff0c8 : 0xe9dcc0 }));
      globe.routes.add(line, dot);
      globe.arcs.push({ curve, line, dot, speed: 0.12 + (i % 3) * 0.03, offset: (i * 0.37) % 1 });
    });
    const legend = document.getElementById("globeLegend");
    if (legend) legend.textContent = (specs.length ? specs.length + " active route" + (specs.length > 1 ? "s" : "") : "no active routes") + " · drag to rotate";
  }

  /* ---------------- Margin cube ---------------- */
  function initCube(el) {
    const v = makeView(el, 38, 7); if (!v) return;
    v.scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    const light = new THREE.DirectionalLight(0xfff0dc, 1.0); light.position.set(4, 6, 6); v.scene.add(light);
    const group = new THREE.Group(); v.scene.add(group);
    group.rotation.x = 0.28;
    const grid = new THREE.GridHelper(5, 10, 0xb39a6a, 0xd8cfc0); grid.position.y = -1.5; group.add(grid);
    // Translucent glass block, warm gold fluid level, green floor boundary
    const glass = new THREE.Mesh(new THREE.BoxGeometry(1.7, 3.6, 1.7), new THREE.MeshPhongMaterial({ color: 0xffffff, specular: 0xffffff, shininess: 140, transparent: true, opacity: 0.35, depthWrite: false }));
    glass.position.y = -1.5 + 1.8; group.add(glass);
    const frame = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(1.7, 3.6, 1.7)), new THREE.LineBasicMaterial({ color: GOLD, transparent: true, opacity: 0.6 }));
    frame.position.y = -1.5 + 1.8; group.add(frame);
    const mat = new THREE.MeshPhongMaterial({ color: TINT.gray, emissive: TINT.gray, emissiveIntensity: 0.3, specular: 0xffffff, shininess: 120, transparent: true, opacity: 0.8 });
    const bar = new THREE.Mesh(new THREE.BoxGeometry(1.4, 1, 1.4), mat); group.add(bar);
    bar.add(new THREE.LineSegments(new THREE.EdgesGeometry(bar.geometry), new THREE.LineBasicMaterial({ color: 0x9a6d12 })));
    const mk = (color) => { const m = new THREE.Mesh(new THREE.BoxGeometry(2.2, 0.02, 2.2), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.5 })); group.add(m); return m; };
    const c = { v, group, bar, mat, hardPlane: mk(EMERALD), softPlane: mk(GOLD), h: 0.05, target: 0.05, drag: 0 };
    draggable(el, (dx) => { group.rotation.y += dx * 0.01; c.drag = 1.5; });
    v.tick = (t, dt) => {
      c.h += (c.target - c.h) * Math.min(1, dt * 4);
      bar.scale.y = Math.max(c.h, 0.02);
      bar.position.y = -1.5 + c.h / 2;
      group.position.y = Math.sin(t * 1.2) * 0.08;
      c.drag = Math.max(0, c.drag - dt);
      if (!c.drag) group.rotation.y += dt * 0.25;
    };
    cube = c;
  }

  function updateCube(c) {
    const readout = document.getElementById("cubeReadout"), label = document.getElementById("cubeCampaign");
    const U = window.UI;
    if (!c) {
      if (label) label.textContent = "–";
      if (readout) readout.innerHTML = '<div class="sub">Select a campaign</div>';
      if (cube) cube.target = 0.05;
      return;
    }
    const spread = typeof c.net_spread_usd === "number" ? c.net_spread_usd : 0;
    const hard = c.min_profit_per_mt_hard || 0, soft = c.min_profit_per_mt_soft || 0;
    const tone = U.marginTone(c), hex = TONE[tone];
    if (label) label.textContent = c.campaign_id;
    if (readout) readout.innerHTML = `<div class="big" style="color:#${hex.toString(16).padStart(6, "0")}">${U.usd(spread)}<span class="sub">/MT</span></div><div class="sub">margin ${U.pct(c.net_margin_pct)} · floor ${U.usd(hard, 0)} · target ${U.usd(soft, 0)}</div>`;
    if (!cube) return;
    const max = Math.max(soft * 1.4, spread * 1.1, 1), H = 3.6;   // full scale = 3.6 units tall
    cube.target = Math.max(0.05, Math.min(1.3, spread / max) * H);
    cube.mat.color.setHex(TINT[tone]); cube.mat.emissive.setHex(TINT[tone]);
    cube.hardPlane.position.y = -1.5 + (hard / max) * H;
    cube.softPlane.position.y = -1.5 + (soft / max) * H;
  }

  /* ---------------- Tilt cards ---------------- */
  function initTilt() {
    if (window.matchMedia && (matchMedia("(prefers-reduced-motion: reduce)").matches || matchMedia("(hover: none)").matches)) return;
    let active = null;
    const reset = (el) => { if (el) { el.style.setProperty("--rx", "0deg"); el.style.setProperty("--ry", "0deg"); el.classList.remove("tilting"); } };
    document.addEventListener("pointermove", (e) => {
      const el = e.target.closest && e.target.closest(".tilt");
      if (active && active !== el) reset(active);
      active = el;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width - 0.5, py = (e.clientY - r.top) / r.height - 0.5;
      el.style.setProperty("--ry", (px * 10).toFixed(2) + "deg");
      el.style.setProperty("--rx", (-py * 10).toFixed(2) + "deg");
      el.style.setProperty("--gx", ((px + 0.5) * 100).toFixed(1) + "%");
      el.style.setProperty("--gy", ((py + 0.5) * 100).toFixed(1) + "%");
      el.classList.add("tilting");
    });
    document.addEventListener("pointerleave", () => reset(active));
  }

  /* ---------------- Sandstone backdrop ----------------
   * Transparent canvas over the CSS ivory gradient: a sparse cloud of thin, slender rice grains
   * (instanced, 160 meshes) drifting under a slowly shifting warm sun. */
  function initBackground() {
    const canvas = document.getElementById("bgCanvas");
    if (!canvas || !window.THREE) return;
    let renderer;
    try { renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: true }); } catch (e) { return; }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    renderer.setClearColor(0x000000, 0);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 200);
    camera.position.set(0, 8, 17);
    scene.add(new THREE.AmbientLight(0xfff0dc, 0.85));
    const sun = new THREE.DirectionalLight(0xffe8c8, 1.0); scene.add(sun);

    // Grain: unit sphere stretched along z (about 0.06 x 0.06 x 0.30) = thin long-grain Basmati profile
    const N = 160, grains = new THREE.InstancedMesh(new THREE.SphereGeometry(1, 10, 8),
      new THREE.MeshPhongMaterial({ color: 0xffffff, specular: 0xffffff, shininess: 80, transparent: true, opacity: 0.35, depthWrite: false }), N);
    const palette = [0xFFFFFF, 0xF1E6CF, 0xD9C9A5, 0x8f7f6f].map((c) => new THREE.Color(c));
    const seed = [];
    for (let i = 0; i < N; i++) {
      seed.push({
        x: (Math.random() - 0.5) * 44, y: 0.5 + Math.random() * 13, z: -14 + Math.random() * 22,
        ph: Math.random() * 6.28, sp: 0.12 + Math.random() * 0.3,
        w: 0.05 + Math.random() * 0.02, l: 0.26 + Math.random() * 0.08,
        rx: Math.random() * 6.28, ry: Math.random() * 6.28,
      });
      grains.setColorAt(i, palette[i % palette.length]);
    }
    grains.instanceColor.needsUpdate = true; grains.frustumCulled = false; scene.add(grains);
    const dummy = new THREE.Object3D();

    let mx = 0, my = 0;
    window.addEventListener("pointermove", (e) => { mx = e.clientX / window.innerWidth - 0.5; my = e.clientY / window.innerHeight - 0.5; });
    const resize = () => { renderer.setSize(window.innerWidth, window.innerHeight, false); camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix(); };
    window.addEventListener("resize", resize); resize();

    const L = new THREE.Vector3(), gold = new THREE.Color(0xffe8c8), rose = new THREE.Color(0xffd2b4);
    views.unshift({
      renderer, scene, camera, visible: true,
      tick(t) {
        const a = t * 0.12;
        L.set(Math.cos(a), 0.5 + 0.2 * Math.sin(a * 0.7), 0.45 * Math.sin(a) + 0.6);
        sun.position.copy(L).multiplyScalar(20); sun.color.copy(gold).lerp(rose, (Math.sin(a * 0.5) + 1) / 2);
        camera.position.x += (mx * 2.2 - camera.position.x) * 0.04;
        camera.position.y += (8 + my * 0.8 - camera.position.y) * 0.04;
        camera.lookAt(0, 0.5, -4);
        for (let i = 0; i < N; i++) {
          const g = seed[i];
          const fall = (g.y - t * g.sp * 0.4) % 13; const y = 0.5 + (fall < 0 ? fall + 13 : fall);
          dummy.position.set(g.x + Math.sin(t * 0.4 + g.ph) * 0.8, y, g.z + Math.cos(t * 0.3 + g.ph) * 0.5);
          dummy.rotation.set(g.rx + t * 0.36 * g.sp, g.ry + t * 0.1, 0);
          dummy.scale.set(g.w, g.w, g.l);
          dummy.updateMatrix(); grains.setMatrixAt(i, dummy.matrix);
        }
        grains.instanceMatrix.needsUpdate = true;
      },
    });
  }

  /* ---------------- Loop ---------------- */
  function loop() {
    let prev = performance.now();
    const frame = (now) => {
      const dt = Math.min(0.05, (now - prev) / 1000); prev = now;
      if (!document.hidden) views.forEach((v) => { if (v.visible) { v.tick(now / 1000, dt); v.renderer.render(v.scene, v.camera); } });
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  window.UI3D = {
    init() {
      initTilt();
      initBackground();
      const g = document.getElementById("globeBox"), c = document.getElementById("cubeBox");
      if (g) initGlobe(g);
      if (c) initCube(c);
      if (views.length) loop();
    },
    update(list, selectedId) {
      updateGlobe(list, selectedId);
      updateCube(list.find((x) => x.campaign_id === selectedId) || null);
    },
  };
})();
