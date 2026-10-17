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

  /* ---- Contact book row (Contacts modal) ---- */
  function contactRow(p) {
    const where = [p.country, p.port].filter(Boolean).join(" · ");
    return `<div class="flex items-center justify-between gap-2 py-1.5 border-b border-slate-200/70 last:border-0">
      <div class="min-w-0"><div class="text-xs font-semibold truncate">${esc(p.name)}</div>
        <div class="text-[11px] text-slate-500 mono truncate">${esc(p.email)}${where ? " · " + esc(where) : ""}</div></div>
      <button type="button" class="btn btn-red !py-1 !px-2" data-delete-contact="${esc(p.id)}" title="Remove ${esc(p.name)}"><i data-lucide="trash-2" class="w-3 h-3"></i></button>
    </div>`;
  }
  function contactList(list, role) {
    return list.length ? list.map(contactRow).join("") : `<p class="text-[11px] text-slate-500 py-2">No saved ${role}s yet.</p>`;
  }

  /* Who this campaign is negotiating with (inspector header). */
  function partiesLine(c) {
    const party = (label, name, email) => email ? `<span>${label}: <b class="font-semibold text-slate-600">${esc(name || "–")}</b> <span class="mono">&lt;${esc(email)}&gt;</span></span>` : "";
    const parts = [party("Buyer", c.target_buyer_name, c.target_buyer_email), party("Supplier", c.target_supplier_name, c.target_supplier_email)].filter(Boolean);
    return parts.length ? `<div class="text-[11px] text-slate-400 mt-0.5 flex flex-wrap gap-x-3">${parts.join("")}</div>` : "";
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
        <div class="text-[11px] text-slate-400 mt-0.5">${esc(c.commodity)} · round ${esc(c.negotiation_round ?? 0)}/${esc(c.max_negotiation_rounds ?? "–")}</div>${partiesLine(c)}</div>
        <div class="w-full sm:w-80">${pipeline(c)}</div></div>
      ${approvalBanner(c)}
      <div class="inspector-split"><div class="space-y-3 min-w-0">${threadPanel(c, threadFilter)}${simulator(c)}</div>
      <div class="space-y-3 min-w-0">${pricingPanel(c)}${auditTimeline(c)}</div></div></div>`;
  }

  window.UI = { esc, usd, pct, statusInfo, statusBadge, marginTone, campaignRow, contactList, inspector, emptyInspector, emptyFeed, approvalSummary };
})();

/* ---------------------------------------------------------------------------
 * 3D layer (Three.js r128): spectrum particle backdrop, frosted-glass trade globes (landing showcase + live desk),
 * margin cube, tilt-on-hover cards. Everything degrades to a no-op when THREE / WebGL is unavailable.
 * ------------------------------------------------------------------------- */
(function () {
  const VIOLET = 0x8B5CF6, FUCHSIA = 0xD946EF, ORANGE = 0xF97316, RED = 0xEF4444, SLATE = 0x94A3B8;
  // Readout colours (need to read on white glass) and cube fluid tints per margin tone
  const TONE = { green: 0x059669, amber: 0xD97706, red: 0xDC2626, blue: 0x4B5563, gray: 0x4B5563 };
  const TINT = { green: VIOLET, amber: VIOLET, red: RED, blue: VIOLET, gray: VIOLET };   // violet fluid; red below the floor
  const PORTS = {
    "Karachi": [24.86, 67.0, "origin"], "Mundra": [22.84, 69.7, "origin"],
    "Laem Chabang": [13.08, 100.88, "origin"], "Cat Lai": [10.77, 106.77, "origin"],
    "Jebel Ali": [25.0, 55.06, "dest"], "Dammam": [26.43, 50.1, "dest"],
  };
  // Landing-page showcase lanes (the desk globe draws live campaigns instead)
  const SHOWCASE = [
    { o: "Karachi", d: "Jebel Ali", sel: true }, { o: "Laem Chabang", d: "Dammam" }, { o: "Cat Lai", d: "Jebel Ali" },
    { o: "Mundra", d: "Dammam" }, { o: "Laem Chabang", d: "Jebel Ali" },
  ];
  const views = [];   // { renderer, scene, camera, el, visible, tick(t, dt) }
  const globes = [];  // live-data globes (desk); showcase globes are drawn once at init
  let cube = null;

  /* Canvas sprite textures: soft round dot, and a violet→orange halo for the globe aura. */
  const texCache = {};
  function radialTexture(key, stops) {
    if (texCache[key]) return texCache[key];
    const s = 128, cv = document.createElement("canvas"); cv.width = cv.height = s;
    const ctx = cv.getContext("2d"), g = ctx.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
    stops.forEach(([o, c]) => g.addColorStop(o, c));
    ctx.fillStyle = g; ctx.fillRect(0, 0, s, s);
    return (texCache[key] = new THREE.CanvasTexture(cv));
  }
  const dotTexture = () => radialTexture("dot", [[0, "rgba(255,255,255,1)"], [0.45, "rgba(255,255,255,0.9)"], [1, "rgba(255,255,255,0)"]]);
  const auraTexture = () => radialTexture("aura", [[0, "rgba(139,92,246,0.34)"], [0.42, "rgba(217,70,239,0.18)"], [0.7, "rgba(249,115,22,0.12)"], [1, "rgba(249,115,22,0)"]]);

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
    // Also pauses views inside the hidden route (display:none never intersects)
    if (window.IntersectionObserver) new IntersectionObserver((es) => { v.visible = es[es.length - 1].isIntersecting; }).observe(el);
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

  /* ---------------- Globe ----------------
   * Porcelain Earth: a real water mask + normal map (three.js r128 example textures) re-tinted in a custom shader,
   * so oceans read as pale porcelain and continents as lavender-taupe relief. Until the textures arrive (or if the
   * CDN is unreachable) the sphere renders as plain porcelain and everything else still works. */
  const EARTH_TEX = "https://cdn.jsdelivr.net/gh/mrdoob/three.js@r128/examples/textures/planets/";
  let earthUniforms = null;   // shared by every globe so the textures load once
  function earthMaterial() {
    if (!earthUniforms) {
      earthUniforms = {
        waterMap: { value: null }, reliefMap: { value: null }, ready: { value: 0 },
        ocean: { value: new THREE.Color(0xF4F3F8) }, land: { value: new THREE.Color(0xD5CEE0) }, landDeep: { value: new THREE.Color(0xBFB4D1) },
        rimA: { value: new THREE.Color(VIOLET) }, rimB: { value: new THREE.Color(ORANGE) },
        lightDir: { value: new THREE.Vector3(-0.45, 0.55, 0.7).normalize() },
      };
      const loader = new THREE.TextureLoader(); loader.setCrossOrigin("anonymous");
      let n = 0; const done = () => { if (++n === 2) earthUniforms.ready.value = 1; };
      loader.load(EARTH_TEX + "earth_specular_2048.jpg", (t) => { earthUniforms.waterMap.value = t; done(); });
      loader.load(EARTH_TEX + "earth_normal_2048.jpg", (t) => { earthUniforms.reliefMap.value = t; done(); });
    }
    return new THREE.ShaderMaterial({
      uniforms: earthUniforms,
      vertexShader: `
        varying vec2 vUv; varying vec3 vN; varying vec3 vT; varying vec3 vB;
        void main() {
          vUv = uv;
          vec3 n = normalize(normal);
          vec3 t = normalize(cross(vec3(0.0, 1.0, 0.0), n) + vec3(1e-4, 0.0, 0.0));
          vN = normalize(normalMatrix * n); vT = normalize(normalMatrix * t); vB = normalize(normalMatrix * cross(n, t));
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }`,
      fragmentShader: `
        uniform sampler2D waterMap; uniform sampler2D reliefMap; uniform float ready;
        uniform vec3 ocean; uniform vec3 land; uniform vec3 landDeep; uniform vec3 rimA; uniform vec3 rimB; uniform vec3 lightDir;
        varying vec2 vUv; varying vec3 vN; varying vec3 vT; varying vec3 vB;
        void main() {
          float water = ready > 0.5 ? smoothstep(0.25, 0.6, texture2D(waterMap, vUv).r) : 1.0;
          vec3 rel = ready > 0.5 ? texture2D(reliefMap, vUv).xyz * 2.0 - 1.0 : vec3(0.0, 0.0, 1.0);
          float k = (1.0 - water) * 1.6;                       // relief only on land
          vec3 N = normalize(vN + (rel.x * vT + rel.y * vB) * k);
          float lit = max(dot(normalize(vN), lightDir), 0.0);
          float relief = dot(N, lightDir) - dot(normalize(vN), lightDir);   // relief adds contrast, never overall brightness
          float ridge = clamp(length(rel.xy) * 2.2, 0.0, 1.0);             // steeper terrain reads deeper lavender
          vec3 landCol = mix(land, landDeep, ridge * 0.7) * (0.84 + 0.2 * lit) * (1.0 + relief * 0.9);
          vec3 seaCol = ocean * (0.93 + 0.09 * lit);
          vec3 col = mix(landCol, seaCol, water);
          col += vec3(1.0) * pow(max(dot(reflect(-lightDir, N), vec3(0.0, 0.0, 1.0)), 0.0), 24.0) * 0.12 * water;
          float fres = pow(1.0 - max(dot(normalize(vN), vec3(0.0, 0.0, 1.0)), 0.0), 2.6);
          vec3 rim = mix(rimB, rimA, smoothstep(-0.7, 0.7, vN.y + vN.x * 0.35));
          col = mix(col, rim, fres * 0.55);
          gl_FragColor = vec4(col, 1.0);
        }`,
    });
  }

  /* Fresnel atmosphere: back-face shell whose alpha rises toward the limb, violet up top fading to orange below. */
  function atmosphereMaterial() {
    return new THREE.ShaderMaterial({
      uniforms: { a: { value: new THREE.Color(VIOLET) }, b: { value: new THREE.Color(ORANGE) }, strength: { value: 1 } },
      vertexShader: `varying vec3 vN; void main() { vN = normalize(normalMatrix * normal); gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
      fragmentShader: `
        uniform vec3 a; uniform vec3 b; uniform float strength; varying vec3 vN;
        void main() {
          float i = pow(smoothstep(0.0, 0.52, -vN.z), 1.6);   // 0 at the shell edge, full at the planet limb
          vec3 c = mix(b, a, smoothstep(-0.6, 0.6, vN.y + vN.x * 0.35));
          gl_FragColor = vec4(c, i * 0.85 * strength);
        }`,
      side: THREE.BackSide, transparent: true, depthWrite: false,
    });
  }

  function initGlobe(el, opts = {}) {
    const v = makeView(el, opts.fov || 40, opts.camZ || 5.2); if (!v) return null;
    const R = 1.5, group = new THREE.Group(); v.scene.add(group);
    // Soft spectrum glow behind the planet
    const aura = new THREE.Sprite(new THREE.SpriteMaterial({ map: auraTexture(), transparent: true, depthWrite: false, opacity: 0.7 }));
    aura.scale.setScalar(R * 3.6); aura.position.z = -0.8; v.scene.add(aura);
    const atmo = new THREE.Mesh(new THREE.SphereGeometry(R * 1.16, 64, 48), atmosphereMaterial());
    v.scene.add(atmo);
    const earth = new THREE.Mesh(new THREE.SphereGeometry(R, 96, 64), earthMaterial());
    group.add(earth);
    // Port nodes: core + pulsing halo, violet export / orange destination
    const pulses = [];
    Object.values(PORTS).forEach(([lat, lon, kind], i) => {
      const c = kind === "dest" ? ORANGE : VIOLET;
      const m = new THREE.Mesh(new THREE.SphereGeometry(kind === "dest" ? 0.032 : 0.026, 16, 16), new THREE.MeshBasicMaterial({ color: c }));
      m.position.copy(ll(lat, lon, R * 1.004)); group.add(m);
      const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: dotTexture(), color: c, transparent: true, depthWrite: false }));
      halo.position.copy(ll(lat, lon, R * 1.012)); halo.renderOrder = 2; group.add(halo);
      pulses.push({ halo, ph: i * 1.1 });
    });
    // Subtle particle shell drifting around the globe
    const P = 220, pp = new Float32Array(P * 3), pc = new Float32Array(P * 3), tmp = new THREE.Color();
    for (let i = 0; i < P; i++) {
      const u = Math.random() * 2 - 1, a = Math.random() * 6.283, rr = R * (1.3 + Math.random() * 0.9), s = Math.sqrt(1 - u * u);
      pp.set([Math.cos(a) * s * rr, u * rr, Math.sin(a) * s * rr], i * 3);
      tmp.set([VIOLET, ORANGE, FUCHSIA, SLATE][i % 4]); pc.set([tmp.r, tmp.g, tmp.b], i * 3);
    }
    const pg = new THREE.BufferGeometry();
    pg.setAttribute("position", new THREE.BufferAttribute(pp, 3)); pg.setAttribute("color", new THREE.BufferAttribute(pc, 3));
    const shell = new THREE.Points(pg, new THREE.PointsMaterial({ size: 0.028, map: dotTexture(), vertexColors: true, transparent: true, opacity: 0.55, depthWrite: false }));
    v.scene.add(shell);

    const routes = new THREE.Group(); group.add(routes);
    // Auto-rotation is a slow sway around a "home" heading over the Arabian Sea, so the lanes never spin out of view.
    // Dragging moves the home heading; the sway resumes from wherever the user leaves it.
    const p = ll(18, 72, 1);
    const g = { v, group, routes, arcs: [], key: "", drag: 0, homeY: -Math.atan2(p.x, p.z), sway: 0 };
    group.rotation.set(0.32, g.homeY, 0);
    draggable(el, (dx, dy) => { g.homeY += dx * 0.008; group.rotation.x = Math.max(-1, Math.min(1, group.rotation.x + dy * 0.006)); g.drag = 1.2; });
    v.tick = (t, dt) => {
      g.drag = Math.max(0, g.drag - dt);
      if (!g.drag) g.sway += dt;                               // sway clock pauses while dragging
      group.rotation.y = g.homeY + Math.sin(g.sway * 0.18) * 0.55;
      shell.rotation.y = -t * 0.03; shell.rotation.x = Math.sin(t * 0.1) * 0.1;
      atmo.material.uniforms.strength.value = 0.9 + Math.sin(t * 1.1) * 0.1;
      pulses.forEach((q) => { const k = (Math.sin(t * 2.4 + q.ph) + 1) / 2; q.halo.scale.setScalar(0.1 + k * 0.12); q.halo.material.opacity = 0.65 - k * 0.45; });
      g.arcs.forEach((a) => {
        a.dot.position.copy(a.curve.getPoint(((t * a.speed) + a.offset) % 1));
        a.tube.material.opacity = a.base * (0.8 + 0.2 * Math.sin(t * 2 + a.offset * 6.28));
      });
    };
    if (opts.showcase) drawRoutes(g, opts.showcase); else globes.push(g);
    return g;
  }

  function originOf(c) {
    const n = String(c.commodity || "").toLowerCase();
    if (/thai|jasmine/.test(n)) return "Laem Chabang";
    if (/viet/.test(n)) return "Cat Lai";
    return "Karachi";
  }
  function destOf(c) { return /dammam|saudi/i.test((c.buyer_terms && c.buyer_terms.port) || c.destination_port || "") ? "Dammam" : "Jebel Ali"; }

  /* Rebuild a globe's arcs: tubes shaded violet (export) → orange (destination); the selected lane is thicker and brighter. */
  function drawRoutes(g, specs) {
    const R = 1.5, cA = new THREE.Color(VIOLET), cB = new THREE.Color(ORANGE), tmp = new THREE.Color();
    g.arcs.forEach((a) => { a.tube.geometry.dispose(); a.tube.material.dispose(); a.dot.material.dispose(); });   // sprite geometry is shared: don't dispose it
    g.routes.clear(); g.arcs = [];
    const anySel = specs.some((s) => s.sel);
    specs.forEach((s, i) => {
      const a = ll(PORTS[s.o][0], PORTS[s.o][1], R), b = ll(PORTS[s.d][0], PORTS[s.d][1], R);
      const mid = a.clone().add(b).multiplyScalar(0.5).normalize().multiplyScalar(R * (1.16 + a.distanceTo(b) * 0.45));
      const curve = new THREE.QuadraticBezierCurve3(a, mid, b);
      const TS = 64, RS = 8, geo = new THREE.TubeGeometry(curve, TS, s.sel ? 0.014 : 0.009, RS, false);
      const col = new Float32Array(geo.attributes.position.count * 3);
      for (let j = 0; j <= TS; j++) {                         // TubeGeometry lays vertices out ring by ring along the path
        tmp.copy(cA).lerp(cB, j / TS);
        for (let k = 0; k <= RS; k++) col.set([tmp.r, tmp.g, tmp.b], (j * (RS + 1) + k) * 3);
      }
      geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
      const base = s.sel || !anySel ? 0.95 : 0.5;
      const tube = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, opacity: base, depthWrite: false }));
      tube.renderOrder = 3;
      const dot = new THREE.Sprite(new THREE.SpriteMaterial({ map: dotTexture(), color: s.sel ? RED : FUCHSIA, transparent: true, depthWrite: false }));
      dot.scale.setScalar(s.sel ? 0.13 : 0.1); dot.renderOrder = 4;
      g.routes.add(tube, dot);
      g.arcs.push({ curve, tube, dot, base, speed: 0.12 + (i % 3) * 0.03, offset: (i * 0.37) % 1 });
    });
  }

  function updateGlobe(list, selected) {
    const live = list.filter((c) => c.deal_status !== "rejected" && c.deal_status !== "supplier_lock_failed");
    const specs = live.map((c) => ({ o: originOf(c), d: destOf(c), sel: c.campaign_id === selected }));
    const key = JSON.stringify(specs);
    globes.forEach((g) => { if (key !== g.key) { g.key = key; drawRoutes(g, specs); } });
    const legend = document.getElementById("globeLegend");
    if (legend) legend.textContent = (specs.length ? specs.length + " active route" + (specs.length > 1 ? "s" : "") : "no active routes") + " · drag to rotate";
  }

  /* ---------------- Margin cube ---------------- */
  function initCube(el) {
    const v = makeView(el, 38, 7); if (!v) return;
    v.scene.add(new THREE.AmbientLight(0xffffff, 0.75));
    const light = new THREE.DirectionalLight(0xffffff, 0.8); light.position.set(4, 6, 6); v.scene.add(light);
    const warm = new THREE.DirectionalLight(0xFFD7B5, 0.4); warm.position.set(-5, 2, 3); v.scene.add(warm);
    const group = new THREE.Group(); v.scene.add(group);
    group.rotation.x = 0.28;
    const grid = new THREE.GridHelper(5, 10, 0xC4B5FD, 0xE5E7EB); grid.position.y = -1.5; group.add(grid);
    // Frosted glass block, violet frame and fluid level, red floor / green target planes
    const glass = new THREE.Mesh(new THREE.BoxGeometry(1.7, 3.6, 1.7), new THREE.MeshPhongMaterial({ color: 0xffffff, specular: 0xffffff, shininess: 140, transparent: true, opacity: 0.4, depthWrite: false }));
    glass.position.y = -1.5 + 1.8; group.add(glass);
    const frame = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(1.7, 3.6, 1.7)), new THREE.LineBasicMaterial({ color: VIOLET, transparent: true, opacity: 0.6 }));
    frame.position.y = -1.5 + 1.8; group.add(frame);
    const mat = new THREE.MeshPhongMaterial({ color: TINT.gray, emissive: TINT.gray, emissiveIntensity: 0.25, specular: 0xffffff, shininess: 120, transparent: true, opacity: 0.78 });
    const bar = new THREE.Mesh(new THREE.BoxGeometry(1.4, 1, 1.4), mat); group.add(bar);
    bar.add(new THREE.LineSegments(new THREE.EdgesGeometry(bar.geometry), new THREE.LineBasicMaterial({ color: 0x6D28D9 })));
    const mk = (color) => { const m = new THREE.Mesh(new THREE.BoxGeometry(2.2, 0.02, 2.2), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.4, depthWrite: false })); group.add(m); return m; };
    const c = { v, group, bar, mat, hardPlane: mk(RED), softPlane: mk(0x10B981), h: 0.05, target: 0.05, drag: 0 };
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

  /* ---------------- Spectrum particle field ----------------
   * Transparent canvas over the CSS aura blobs: a sparse field of tiny violet, fuchsia, orange and slate
   * particles drifting slowly, with gentle mouse parallax. Normal blending so it reads on a white canvas. */
  function initBackground() {
    const canvas = document.getElementById("bgCanvas");
    if (!canvas || !window.THREE) return;
    let renderer;
    try { renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: true }); } catch (e) { return; }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    renderer.setClearColor(0x000000, 0);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 400);
    camera.position.set(0, 0, 30);

    const palette = [VIOLET, VIOLET, FUCHSIA, ORANGE, RED, SLATE].map((c) => new THREE.Color(c));
    const layer = (n, spread, depth, size, opacity) => {
      const pos = new Float32Array(n * 3), col = new Float32Array(n * 3);
      for (let i = 0; i < n; i++) {
        pos.set([(Math.random() - 0.5) * spread, (Math.random() - 0.5) * spread * 0.62, -Math.random() * depth], i * 3);
        const c = palette[(Math.random() * palette.length) | 0]; col.set([c.r, c.g, c.b], i * 3);
      }
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(pos, 3)); g.setAttribute("color", new THREE.BufferAttribute(col, 3));
      const pts = new THREE.Points(g, new THREE.PointsMaterial({ size, map: dotTexture(), vertexColors: true, transparent: true, opacity, depthWrite: false, sizeAttenuation: true }));
      scene.add(pts);
      return pts;
    };
    const far = layer(700, 220, 160, 0.6, 0.32), near = layer(160, 120, 50, 0.32, 0.5);

    let mx = 0, my = 0;
    window.addEventListener("pointermove", (e) => { mx = e.clientX / window.innerWidth - 0.5; my = e.clientY / window.innerHeight - 0.5; });
    const resize = () => { renderer.setSize(window.innerWidth, window.innerHeight, false); camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix(); };
    window.addEventListener("resize", resize); resize();

    views.unshift({
      renderer, scene, camera, visible: true,
      tick(t) {
        far.rotation.z = t * 0.004; far.rotation.y = Math.sin(t * 0.02) * 0.05;
        near.rotation.z = -t * 0.007; near.position.y = Math.sin(t * 0.05) * 1.5;
        camera.position.x += (mx * 3 - camera.position.x) * 0.03;
        camera.position.y += (-my * 2 - camera.position.y) * 0.03;
        camera.lookAt(0, 0, -40);
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
      const hero = document.getElementById("heroGlobeBox");
      if (hero) initGlobe(hero, { showcase: SHOWCASE, camZ: 6.2 });
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
