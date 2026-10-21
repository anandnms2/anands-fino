"use strict";
/* Anands Fino dashboard. Plain JS, no build step. Every dynamic string goes through esc(). */
const $app = document.getElementById("app");
let timer = null, busy = false;

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 2) => (v == null || isNaN(v) ? "–" : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
const money = (v) => (v == null ? "–" : (v < 0 ? "-" : "") + "$" + num(Math.abs(v), 2));
const signed = (v, d = 1) => (v == null ? "–" : (v > 0 ? "+" : "") + num(v, d));
const hm = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "–");
const ago = (m) => (m == null ? "never" : m < 60 ? Math.round(m) + "m ago" : Math.round(m / 60) + "h ago");

async function api(path, method = "GET", body) {
  const opt = { method, headers: { "X-Requested-With": "fino" } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* non-JSON */ }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || r.statusText);
  return data;
}
function toast(msg, err) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, 4000);
}
async function act(path, body, ok) {
  try { await api(path, "POST", body); if (ok) toast(ok); } catch (e) { toast(e.message, true); }
  render();
}
window.act = act;
window.saveRisk = () => { const b = {}; ["risk_per_trade_pct", "daily_loss_cap_pct", "max_open", "max_loss_streak", "min_rr"].forEach((k) => { const el = document.getElementById("rk_" + k); if (el) b[k] = Number(el.value); }); act("/api/risk", b, "Risk limits saved"); };
window.askAct = (msg, path, ok) => { if (confirm(msg)) act(path, {}, ok); };

const VLABEL = { paper: "Paper", demo: "Demo", live: "Live" };
const VDESC = { paper: "Simulated account on real prices. No broker needed.", demo: "cTrader demo account with demo funds.", live: "cTrader live account. Real money." };
const vPill = (v) => v ? `<span class="pill ${v === "live" ? "warn" : "acc"}">${esc(VLABEL[v] || v)}</span>` : "";
const sw = (on, label, path, body) => `<button class="btn sm ${on ? "pri" : ""}" role="switch" aria-checked="${on}" aria-label="${esc(label)}" onclick="act('${path}',${body},'${esc(label)} ${on ? "off" : "on"}')">${on ? "ON" : "OFF"}</button>`;
const STATUS = {
  executed: ["Executed", "buy"], pending_approval: ["Needs approval", "acc"], claude_no: ["Claude: NO", "none"], gate_blocked: ["Blocked by gate", "warn"],
  no_trade: ["No trade", "none"], alerted: ["Alert only", "acc"], error: ["Error", "red"], rejected: ["Rejected", "none"], expired: ["Expired", "none"],
};
const stPill = (s) => { const [l, c] = STATUS[s] || [s, "none"]; return `<span class="pill ${c}">${esc(l)}</span>`; };
const biasPill = (b) => `<span class="pill ${b === "BUY" ? "buy" : b === "SELL" ? "sell" : "none"}">${esc(b || "NONE")}</span>`;

function scoreBar(score) {
  const s = Math.max(-100, Math.min(100, score || 0));
  const col = s >= 0 ? "var(--buy)" : "var(--sell)";
  const left = s >= 0 ? 50 : 50 + s / 2, w = Math.abs(s) / 2;
  return `<div class="track" aria-hidden="true"><b></b><i style="left:${left}%;width:${w}%;background:${col}"></i></div>`;
}


/* ------------------------------------------------------------ live state (terminal) */
const $bar = document.getElementById("bar");
const S = { an: {}, venue: localStorage.getItem("fv") || "paper", sym: localStorage.getItem("fs") || "", tf: 0, tab: "pos", px: {}, last: {}, dir: {}, live: {}, lat: -1,
  st: null, latest: [], posd: { open: [], closed: [] }, appr: [], actv: [], det: null, detId: 0, candles: [], candleKey: "", mounted: false, drawQ: false };
const dig = (s) => (s === "XAUUSD" ? 2 : s === "XAGUSD" ? 3 : /JPY$/.test(s) ? 3 : 5);
const pxs = (v, s) => (v == null ? "–" : Number(v).toFixed(dig(s)));
const pageName = () => { const h = location.hash; return h.startsWith("#/signal") ? "signal" : h === "#/reports" ? "reports" : h === "#/specs" ? "specs" : h === "#/settings" ? "settings" : "terminal"; };
const $ = (id) => document.getElementById(id);

function startStream() {
  if (S.es || !window.EventSource) return;
  const es = new EventSource("/api/stream" + (S.sym ? "?sym=" + S.sym : ""));
  S.es = es; S.esSym = S.sym;
  es.onmessage = (e) => {
    let d; try { d = JSON.parse(e.data); } catch (_) { return; }
    S.lat = Math.max(0, Date.now() - d.ts);
    for (const v in d.px) for (const sy in d.px[v]) {
      const o = (S.last[v] || {})[sy], n = d.px[v][sy][0];
      if (o != null && n !== o) { (S.dir[v] = S.dir[v] || {})[sy] = n > o ? 1 : -1; }
      (S.last[v] = S.last[v] || {})[sy] = n;
    }
    S.px = d.px; S.live = {};
    for (const p of d.pos) S.live[p.venue + ":" + p.ticket] = p;
    onTick();
  };
  es.onerror = () => { S.lat = -1; setLat(); };
}

function syncStream() { if (S.es && S.esSym !== S.sym && S.sym) { S.es.close(); S.es = null; startStream(); } }
function onTick() {
  barLive();
  if (pageName() !== "terminal" || !S.mounted || !S.st) return;
  updateWatch(); updateHead(); updateLiveCandle(); updateLivePos(); queueDraw();
}

/* ------------------------------------------------------------ top bar */
function setLat() {
  const el = $("bar-lat"), dot = $("bar-dot");
  if (!el) return;
  el.textContent = S.lat < 0 ? "stream reconnecting" : "stream " + S.lat + " ms";
  dot.style.background = S.lat < 0 ? "var(--sell)" : S.lat > 1500 ? "var(--warn)" : "var(--buy)";
}
function liveFloat() {
  let any = false, f = 0;
  for (const k in S.live) if (k.startsWith(S.venue + ":")) { any = true; f += S.live[k].floating; }
  return any ? f : null;
}
function barLive() {
  setLat();
  const st = S.st; if (!st) return;
  const v = st.venues[S.venue] || {}, a = v.account, lf = liveFloat(), fl = lf != null ? lf : (v.floating || 0);
  const fe = $("bar-fl"), ee = $("bar-eq");
  if (fe) { fe.textContent = (fl < 0 ? "-$" : "+$") + num(Math.abs(fl), 2); fe.className = "mono b " + (fl < 0 ? "bad" : "ok"); }
  if (ee && a) ee.textContent = "$" + num(lf != null ? a.balance + lf : a.equity, 2);
}
function renderBar() {
  const st = S.st; if (!st) return;
  const V = st.venues, v = V[S.venue] || {}, a = v.account, r = st.risk, pg = pageName();
  const nav = [["terminal", "#/", "Terminal"], ["reports", "#/reports", "Reports"], ["specs", "#/specs", "Contracts"], ["settings", "#/settings", "Settings"]];
  $bar.innerHTML = `<div class="bar-in"><div class="bar-r1"><a class="brand" href="#/"><img src="/static/logo.png" alt=""><div><b>Anands Fino</b><span>FOREX &amp; METALS · AI + RULES</span></div></a>
    <nav class="bnav" aria-label="Pages">${nav.map(([k, h, l]) => `<a href="${h}" class="${pg === k || (pg === "signal" && k === "terminal") ? "on" : ""}">${l}</a>`).join("")}</nav>
    <div class="vtabs" role="tablist" aria-label="Venue">${Object.values(V).map((x) => `<button role="tab" aria-selected="${x.name === S.venue}" class="${x.name === S.venue ? "on" : ""}" onclick="pickVenue('${x.name}')">${esc(x.label)} <span class="mono">${x.locked ? "LOCKED" : x.on ? "ON" : "OFF"}</span></button>`).join("")}</div></div>
    <div class="bar-r2"><div class="metrics">
      <div><span>BALANCE</span><b class="mono">${a ? "$" + num(a.balance, 2) : "–"}</b></div>
      <div><span>EQUITY</span><b class="mono" id="bar-eq">${a ? "$" + num(a.equity, 2) : "–"}</b></div>
      <div><span>FLOATING P/L</span><b class="mono b" id="bar-fl">–</b></div>
      <div><span>OPEN RISK</span><b class="mono">${num(v.open_risk_pct || 0, 2)}% <i>/ ${num(r.open_risk_cap_pct, 1)}%</i></b></div>
      <div><span>DAY P/L</span><b class="mono ${a && a.day_pnl < 0 ? "bad" : ""}">${a ? signed(a.day_pnl_pct, 2) + "%" : "–"} <i>/ -${r.daily_loss_cap_pct}%</i></b></div></div>
    <div class="chips"><span class="chip"><span id="bar-dot" class="dot"></span><span class="mono" id="bar-lat">stream …</span></span>
      <span class="chip">Profile <b class="acc">${st.profile === "fast" ? "FAST" : "NORMAL"}</b></span>
      <span class="chip">Trailing <b class="${st.trail ? "ok" : "mute"}">${st.trail ? "ON" : "OFF"}</b></span>
      <span class="chip">Claude veto <b class="${st.claude_veto ? "ok" : "mute"}">${st.claude_veto ? "ON" : "OFF"}</b></span>
      ${st.halted && st.halted.on ? `<button class="halt on" onclick="act('/api/resume',{},'Resumed')">RESUME</button>` : `<button class="halt" onclick="askAct('Halt all trading on every venue?','/api/halt','Halted')">HALT ALL</button>`}</div></div></div>`;
  document.documentElement.style.setProperty("--barh", $bar.offsetHeight + "px");
  barLive();
}
window.pickVenue = (v) => {
  S.venue = v; localStorage.setItem("fv", v); S.tf = 0; S.candleKey = "";
  renderBar();
  if (pageName() === "terminal") { renderWatch(); renderHead(); renderBottom(); loadCandles(); renderSide(); }
};

/* ------------------------------------------------------------ terminal */
function mountTerminal() {
  $app.className = "full";
  $app.innerHTML = `<div id="banners"></div><div class="term">
    <aside class="card wl"><div class="wl-h"><span class="lab">Watchlist</span><span class="mute" style="font-size:11px" id="wlc"></span></div>
      <div class="wl-r head"><span>SYMBOL</span><span>BID</span><span>ASK</span><span>SC</span></div><div id="wl"></div></aside>
    <section class="card chartc"><div class="chead" id="chead"></div><div class="cv" id="cvw"><canvas id="cv" role="img" aria-label="Live candle chart"></canvas></div><div class="cfoot" id="cfoot"></div></section>
    <aside class="side" id="side"></aside>
    <section class="card bottom"><div class="tabs" id="tabs"></div><div id="btab"></div></section></div>`;
  S.mounted = true;
}
async function terminal() {
  if (!S.mounted || !$app.querySelector(".term")) mountTerminal();
  const [st, latest, pos, appr, actv] = await Promise.all([api("/api/state"), api("/api/signals/latest"), api("/api/positions"), api("/api/approvals"), api("/api/activity?limit=30")]);
  Object.assign(S, { st, latest, posd: pos, appr, actv });
  if (pageName() !== "terminal" || !$app.querySelector(".term")) return;
  if (!st.symbols.includes(S.sym)) S.sym = st.symbols[0] || "";
  syncStream();
  if (!S.tf) S.tf = (st.venues[S.venue] || {}).tf || 1;
  renderBar(); renderBanners(); renderWatch(); renderHead(); renderBottom(); renderSide(); loadCandles();
}
function renderBanners() {
  const st = S.st; let h = "";
  if (st.halted && st.halted.on) h += `<div class="banner" role="alert"><strong>Trading halted.</strong><span>${esc(st.halted.reason)}</span><button class="btn sm" style="margin-left:auto" onclick="act('/api/resume',{},'Resumed')">Resume</button></div>`;
  for (const v of Object.values(st.venues)) {
    if (v.halt && v.halt.on) h += `<div class="banner" role="alert"><strong>${esc(v.label)} venue paused.</strong><span>${esc(v.halt.reason)}</span><button class="btn sm" style="margin-left:auto" onclick="act('/api/resume',{},'Resumed')">Resume</button></div>`;
    if (v.on && v.connected === false) h += `<div class="banner warn"><strong>${esc(v.label)} not connected.</strong><span class="mute">${esc(v.error)}</span><a class="btn sm" style="margin-left:auto" href="/api/diagnostics?venue=${v.name}">Diagnostics</a></div>`;
  }
  $("banners").innerHTML = h;
}

/* watchlist */
function latestFor(sym) { return S.latest.find((r) => r.symbol === sym); }
function renderWatch() {
  const st = S.st, list = st.symbols;
  $("wlc").textContent = list.length + " symbols";
  $("wl").innerHTML = list.map((s) => {
    const r = latestFor(s), sc = r ? r.score : null;
    return `<div class="wl-r ${s === S.sym ? "sel" : ""}" role="button" tabindex="0" onclick="pickSym('${s}')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();pickSym('${s}')}" aria-pressed="${s === S.sym}">
      <span><b>${esc(s)}</b><i class="mute">${esc(r ? r.timeframe : "")}</i></span><span class="mono" id="wb-${s}">–</span><span class="mono" id="wa-${s}">–</span>
      <span class="mono sc ${sc == null || Math.abs(sc) < (st.thresholds.score || 20) ? "mute" : sc > 0 ? "ok" : "bad"}">${sc == null ? "–" : (sc > 0 ? "+" : "") + Math.round(sc)}</span></div>`;
  }).join("");
  updateWatch();
}
function updateWatch() {
  const row = S.px[S.venue] || {}, dr = S.dir[S.venue] || {};
  for (const s of S.st.symbols) {
    const p = row[s], b = $("wb-" + s), a = $("wa-" + s);
    if (!p || !b) continue;
    b.textContent = pxs(p[0], s); a.textContent = pxs(p[1], s);
    const c = dr[s] > 0 ? "ok" : dr[s] < 0 ? "bad" : "";
    b.className = a.className = "mono " + c;
  }
}
window.pickSym = (s) => { S.sym = s; syncStream(); localStorage.setItem("fs", s); S.candleKey = ""; renderWatch(); renderHead(); renderSide(); renderBottom(); loadCandles(); };

/* chart header */
function renderHead() {
  const st = S.st, v = st.venues[S.venue] || {};
  $("chead").innerHTML = `<div class="ch-l"><b class="sym">${esc(S.sym)}</b><span class="mono big" id="hb">–</span><span class="mono mute" id="hs"></span></div>
    <div class="tfs" role="group" aria-label="Chart candle size">${st.timeframes.map((m) => `<button aria-pressed="${m === S.tf}" onclick="pickTf(${m})">${m === 60 ? "1h" : m + "m"}</button>`).join("")}</div>`;
  updateHead();
}
window.openTrade = (venue, sym, tf) => {   // click an open trade: show its chart on the candle size the trade was taken on
  const was = S.venue; S.venue = venue; localStorage.setItem("fv", venue);
  S.sym = sym; localStorage.setItem("fs", sym); syncStream();
  S.tf = tf || (S.st.venues[venue] || {}).tf || S.tf; S.candleKey = "";
  if (pageName() !== "terminal" || !S.mounted) { location.hash = "#/"; return; }   // the router draws the terminal with these choices
  if (was !== venue) { renderBar(); renderBottom(); }
  renderWatch(); renderHead(); renderSide(); loadCandles();
};
window.pickTf = (m) => { S.tf = m; S.candleKey = ""; renderHead(); loadCandles(); };
function updateHead() {
  const p = (S.px[S.venue] || {})[S.sym]; if (!p) return;
  const dr = ((S.dir[S.venue] || {})[S.sym]) || 0, b = $("hb"), s = $("hs");
  if (!b) return;
  b.textContent = pxs(p[0], S.sym); b.className = "mono big " + (dr > 0 ? "ok" : dr < 0 ? "bad" : "");
  s.textContent = "ask " + pxs(p[1], S.sym) + " · spread " + pxs(p[1] - p[0], S.sym);
}

/* candles */
async function loadCandles() {
  const key = [S.venue, S.sym, S.tf].join("|");
  if (!S.sym || key === S.candleKey) return;
  S.candleKey = key;
  try {
    const r = await api(`/api/candles?symbol=${S.sym}&tf=${S.tf}&venue=${S.venue}&n=400`);
    if (key !== S.candleKey) return;
    S.candles = r.candles; $("cfoot").textContent = "";
  } catch (e) { S.candles = []; $("cfoot").textContent = "No candles: " + e.message; }
  queueDraw();
}
function updateLiveCandle() {
  const p = (S.px[S.venue] || {})[S.sym], c = S.candles; if (!p || !c.length) return;
  const mid = (p[0] + p[1]) / 2, last = c[c.length - 1], now = Date.now() / 1000;
  if (S.venue !== "paper" && now >= last[0] + S.tf * 60) c.push([Math.floor(now / (S.tf * 60)) * S.tf * 60, mid, mid, mid, mid]);
  else { last[4] = mid; last[2] = Math.max(last[2], mid); last[3] = Math.min(last[3], mid); }
  if (c.length > 400) c.shift();
}
function queueDraw() { if (S.drawQ) return; S.drawQ = true; requestAnimationFrame(() => { S.drawQ = false; drawChart(); }); }
function openPos(sym) { return S.posd.open.find((p) => p.venue === S.venue && p.symbol === sym); }
function drawChart() {
  const cv = $("cv"); if (!cv) return;
  const box = cv.parentElement, w = box.clientWidth, h = box.clientHeight, dpr = window.devicePixelRatio || 1;
  if (!w || !h) return;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); cv.style.width = w + "px"; cv.style.height = h + "px"; }
  const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  const PAD = 76, slot = 8, n = Math.max(10, Math.floor((w - PAD - 12) / slot)), view = S.candles.slice(-n);
  const emaOf = (k) => { const a = 2 / (k + 1), out = []; let e = null; for (const c of S.candles) { e = e == null ? c[4] : e + a * (c[4] - e); out.push(e); } return out.slice(-view.length); };
  const EMAS = S.candles.length > 60 ? [[20, "#F58A14"], [50, "#5AA9E6"], [200, "#B48CF2"]].map(([k, col]) => [k, col, emaOf(k)]) : [];
  if (view.length < 2) { g.fillStyle = "#9C9EA6"; g.font = "13px Inter,sans-serif"; g.fillText("Loading candles…", 16, 28); return; }
  const P = openPos(S.sym), d = dig(S.sym);
  let lo = Math.min(...view.map((c) => c[3])), hi = Math.max(...view.map((c) => c[2]));
  if (P) { lo = Math.min(lo, P.tp, P.sl, P.entry); hi = Math.max(hi, P.tp, P.sl, P.entry, P.sl0 || P.sl); }
  const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
  const Y = (v) => 8 + (1 - (v - lo) / (hi - lo)) * (h - 20), x0 = w - PAD - view.length * slot;
  g.font = "11px 'JetBrains Mono',monospace"; g.textBaseline = "middle";
  for (let i = 0; i <= 5; i++) { const v = lo + ((hi - lo) * i) / 5, y = Y(v); g.strokeStyle = "#1a1a1f"; g.setLineDash([3, 4]); g.beginPath(); g.moveTo(0, y); g.lineTo(w - PAD, y); g.stroke(); g.setLineDash([]); g.fillStyle = "#6c6e75"; g.fillText(v.toFixed(d), w - PAD + 6, y); }
  view.forEach((c, i) => {
    const x = x0 + i * slot + slot / 2, up = c[4] >= c[1], col = up ? "#3DC48F" : "#F06D6D";
    g.strokeStyle = col; g.fillStyle = col; g.beginPath(); g.moveTo(x, Y(c[2])); g.lineTo(x, Y(c[3])); g.stroke();
    const t = Y(Math.max(c[1], c[4])), hh = Math.max(1, Math.abs(Y(c[1]) - Y(c[4]))); g.fillRect(x - 2.5, t, 5, hh);
  });
  EMAS.forEach(([k, col, arr]) => { g.strokeStyle = col; g.lineWidth = 1.2; g.beginPath(); arr.forEach((v, i) => { const x = x0 + i * slot + slot / 2; i ? g.lineTo(x, Y(v)) : g.moveTo(x, Y(v)); }); g.stroke(); g.lineWidth = 1; });
  EMAS.forEach(([k, col], i) => { g.fillStyle = col; g.textAlign = "left"; g.fillText("EMA " + k, 10 + i * 70, 16); });
  const tag = (v, col, bg, txt, dash) => { const y = Y(v); g.strokeStyle = col; g.setLineDash(dash || []); g.beginPath(); g.moveTo(0, y); g.lineTo(w - PAD, y); g.stroke(); g.setLineDash([]);
    const t = txt + " " + v.toFixed(d), tw = g.measureText(t).width + 10; g.fillStyle = bg; g.fillRect(w - PAD - tw - 4, y - 9, tw, 18); g.fillStyle = col; g.textAlign = "left"; g.fillText(t, w - PAD - tw + 1, y); };
  if (P) {
    tag(P.tp, "#3DC48F", "#12281c", "TP", [5, 4]); tag(P.entry, "#EDEDEE", "#26262b", "ENTRY");
    tag(P.sl, "#F06D6D", "#2a1414", P.sl !== P.sl0 ? "SL · trailed" : "SL", [5, 4]);
    if (P.sl !== P.sl0) { const y = Y(P.sl0); g.strokeStyle = "#5a3333"; g.setLineDash([2, 4]); g.beginPath(); g.moveTo(0, y); g.lineTo(w - PAD, y); g.stroke(); g.setLineDash([]); g.fillStyle = "#8a6a6a"; g.fillText("first stop " + P.sl0.toFixed(d), 8, y - 8); }
  }
  const last = view[view.length - 1][4], yl = Y(last);
  g.strokeStyle = "#F58A14"; g.beginPath(); g.moveTo(0, yl); g.lineTo(w - PAD, yl); g.stroke();
  g.fillStyle = "#F58A14"; g.fillRect(w - PAD, yl - 10, PAD - 2, 20); g.fillStyle = "#16181B"; g.font = "600 12px 'JetBrains Mono',monospace"; g.fillText(last.toFixed(d), w - PAD + 5, yl);
}
window.addEventListener("resize", queueDraw);

/* decision panel */
const WSTAT = { aligned: ["Aligned with the trade", "ok"], holding: ["Holding, view neutral", "mute"], reversing: ["View turning against the trade", "warn"], reversal: ["Reversal building", "bad"] };
function loadAn() {
  const k = S.venue + ":" + S.sym, c = S.an[k];
  if (c && Date.now() - c.t < 15000) return;
  if (c && c.busy) return;
  S.an[k] = { ...(c || {}), busy: true, t: c ? c.t : 0 };
  api("/api/analysis?symbol=" + S.sym + "&venue=" + S.venue).then((d) => { S.an[k] = { t: Date.now(), d }; if (pageName() === "terminal") renderSide(); })
    .catch((e) => { S.an[k] = { t: Date.now(), err: String(e.message || e), d: (S.an[k] || {}).d }; if (pageName() === "terminal") renderSide(); });
}
function indCard() {
  const c = S.an[S.venue + ":" + S.sym]; if (!c) return `<section class="card"><div class="hd"><h2>Indicators · ${esc(S.sym)}</h2></div><div class="pad mute">Reading the market...</div></section>`;
  if (!c.d) return `<section class="card"><div class="hd"><h2>Indicators · ${esc(S.sym)}</h2></div><div class="pad mute">${esc(c.err || "No data")}</div></section>`;
  const d = c.d, i = d.ind, p = (v) => pxs(v, S.sym), up = (a, b) => (a > b ? "ok" : "bad");
  const rsiC = i.rsi > 70 ? "bad" : i.rsi < 30 ? "ok" : "";
  const row = (a, b, cls) => `<div class="row sm"><span>${a}</span><span class="mono ${cls || ""}">${b}</span></div>`;
  return `<section class="card"><div class="hd"><h2>Indicators · ${esc(d.tf_label)}</h2><span class="mono mute">live</span></div>
    ${row("EMA 20", p(i.ema20), up(i.close, i.ema20))}${row("EMA 50", p(i.ema50), up(i.close, i.ema50))}${row("EMA 200", p(i.ema200), up(i.close, i.ema200))}
    ${row("Higher timeframe", esc(i.htf || "n/a"), i.htf ? (/up/.test(i.htf) ? "ok" : "bad") : "mute")}
    ${row("RSI 14", i.rsi.toFixed(1), rsiC)}${row("MACD histogram", (i.macd_hist >= 0 ? "+" : "") + i.macd_hist + (i.macd_hist > i.macd_hist_prev ? " ▲" : " ▼"), i.macd_hist >= 0 ? "ok" : "bad")}
    ${row("ATR 14", p(i.atr))}${row("Candle pattern", esc(i.pattern || "none"), i.pattern ? (/bull/.test(i.pattern) ? "ok" : "bad") : "mute")}
    ${row("Swing highs", (i.swing_highs || []).slice(-2).map(p).join(" · ") || "–")}${row("Swing lows", (i.swing_lows || []).slice(-2).map(p).join(" · ") || "–")}
    <div class="pad" style="padding-top:6px">${Object.entries(d.factors).map(([k, f]) => `<div class="fread"><b style="text-transform:capitalize">${esc(k)}</b> <span class="mono ${f.score > 0 ? "ok" : f.score < 0 ? "bad" : "mute"}">${f.score == null ? "n/a" : signed(f.score, 0)}</span><div class="mute" style="font-size:11.5px">${esc(f.reading)}</div></div>`).join("")}</div></section>`;
}
function watchCard() {
  const p = openPos(S.sym); if (!p) return "";
  const w = p.watch, on = S.st.watch;
  if (!w) return `<section class="card"><div class="hd"><h2>Reversal watch</h2><span class="pill none">${on ? "starting" : "off"}</span></div><div class="pad mute">${on ? "The first re-score of this trade lands within 30 seconds." : "Switched off in Settings."}</div></section>`;
  const [t, cls] = WSTAT[w.status] || [w.status, "mute"];
  return `<section class="card"><div class="hd"><h2>Reversal watch · ${esc(p.side)}</h2><span class="mono mute">${hm(w.ts)}</span></div>
    <div class="pad"><div style="display:flex;justify-content:space-between;align-items:baseline"><span class="${cls}"><b>${esc(t)}</b></span><b class="mono big2 ${w.signed >= 0 ? "ok" : "bad"}">${signed(w.signed, 0)}</b></div>
    <div class="mute" style="font-size:12px;margin:2px 0 8px">Score seen from this trade's side. Negative means the market now favours the other way. ${w.against.length ? "Against: " + esc(w.against.join(", ")) + "." : ""}${w.streak ? " Confirmed " + w.streak + " check(s) in a row." : ""}</div>
      ${Object.entries(w.factors).map(([k, f]) => { const v = f.score == null ? null : f.score * (p.side === "BUY" ? 1 : -1); return `<div class="frow"><span style="text-transform:capitalize">${esc(k)}</span>${v == null ? '<span class="mute" style="font-size:12px">n/a</span>' : scoreBar(v)}<span class="mono ${v > 0 ? "ok" : v < 0 ? "bad" : "mute"}">${v == null ? "–" : signed(v, 0)}</span></div>`; }).join("")}
      ${w.action ? `<div class="lab">Last action</div><p>${esc(w.action)}</p>` : ""}</div></section>`;
}
async function renderSide() {
  const el = $("side"); if (!el) return;
  S.an = S.an || {}; loadAn();
  const top = watchCard() + indCard();
  const r = latestFor(S.sym);
  if (!r) { el.innerHTML = top + `<section class="card"><div class="hd"><h2>Decision · ${esc(S.sym)}</h2></div><div class="pad mute">No cycle has analysed this symbol yet.</div></section>`; return; }
  const vs = (r.venues || []).find((x) => x.venue === S.venue), id = vs ? vs.id : r.id;
  if (id !== S.detId) { try { S.det = await api("/api/signal/" + id); S.detId = id; } catch (e) { S.det = null; } }
  const s = S.det; if (!s || !$("side")) { if ($("side")) $("side").innerHTML = top; return; }
  const c = s.claude || {}, g = s.gate || {}, l = s.levels || {}, th = S.st.thresholds;
  $("side").innerHTML = top + `<section class="card"><div class="hd"><h2>Decision · ${esc(S.sym)}</h2><span class="mono mute">${hm(s.ts)}</span></div>
      <div class="pad"><div style="display:flex;justify-content:space-between;align-items:baseline"><span>Rules score ${biasPill(s.bias)}</span><b class="mono big2 ${s.score >= 0 ? "ok" : "bad"}">${signed(s.score, 1)}</b></div>
      <div class="mute" style="font-size:12px;margin:2px 0 8px">${s.agree}/${s.available} factors agree · needs ${th.agree_pct || 0}% and ±${th.score}</div>
      ${Object.entries(s.factors || {}).map(([k, f]) => `<div class="frow"><span style="text-transform:capitalize">${esc(k)}</span>${f.score == null ? '<span class="mute" style="font-size:12px">n/a</span>' : scoreBar(f.score)}<span class="mono ${f.score > 0 ? "ok" : f.score < 0 ? "bad" : "mute"}">${f.score == null ? "–" : signed(f.score, 0)}</span></div>`).join("")}</div></section>
    <section class="card"><div class="hd"><h2>Risk gate · code</h2><span class="pill ${g.ok ? "buy" : g.checks ? "warn" : "none"}">${g.ok ? "passed" : g.checks ? "blocked" : "not run"}</span></div>
      ${(g.checks || []).map((k) => `<div class="row sm"><span><span class="dot ${k.ok ? "g" : "r"}"></span>${esc(k.name)}</span><span class="mono mute" style="text-align:right">${esc(k.detail)}</span></div>`).join("") || '<div class="pad mute">The gate has not run for this signal.</div>'}</section>
    <section class="card claude"><div class="hd"><h2>Claude · final say</h2><span class="pill ${c.decision === "YES" ? "buy" : "none"}">${esc(c.decision || "not asked")}${c.confidence != null ? " " + num(c.confidence, 2) : ""}</span></div>
      ${c.thesis || c.against ? `<div class="pad"><p>${esc(c.thesis)}</p>${c.against ? `<div class="lab">Against</div><p>${esc(c.against)}</p>` : ""}${c.main_risk ? `<div class="lab">Main risk</div><p>${esc(c.main_risk)}</p>` : ""}</div>` : `<div class="pad mute">${esc(c.error || s.reason || "Claude was not asked.")}</div>`}</section>
    <section class="card"><div class="hd"><h2>Order</h2><a href="#/signal/${s.id}">full detail</a></div>
      <div class="row sm"><span>Entry</span><span class="mono">${esc(l.entry)}</span></div><div class="row sm"><span>Stop</span><span class="mono bad">${esc(l.sl)}</span></div>
      <div class="row sm"><span>Target</span><span class="mono ok">${esc(l.tp)}</span></div><div class="row sm"><span>R:R · lots</span><span class="mono">${esc(l.rr)} · ${s.lots != null ? num(s.lots, 2) : "–"}</span></div>
      <div class="row sm"><span>Status</span><span>${stPill(s.status)}</span></div></section>`;
}

/* bottom tabs */
function renderBottom() {
  const mine = S.posd.open.filter((p) => p.venue === S.venue), pend = S.appr.filter((a) => a.venue === S.venue);
  const tabs = [["pos", `Positions (${mine.length})`], ["pend", `Pending approval (${pend.length})`], ["hist", "History"], ["log", "Journal"]];
  $("tabs").innerHTML = tabs.map(([k, l]) => `<button class="${S.tab === k ? "on" : ""}" onclick="pickTab('${k}')">${l}</button>`).join("") + `<span class="mute tnote">positions re-priced on every tick · stops checked every 5 s</span>`;
  let h = "";
  if (S.tab === "pos") {
    h = `<div class="tw"><div class="ptable"><div class="prow head"><span>SYMBOL</span><span>SIDE</span><span>LOTS</span><span>ENTRY</span><span>LIVE</span><span>STOP</span><span>TARGET</span><span>STOP ◂ PRICE ▸ TARGET</span><span class="r">P/L</span><span class="r">R</span><span></span></div>
      ${mine.map((p) => `<div class="prow" data-k="${p.venue}:${p.ticket}" data-e="${p.entry}" data-sl="${p.sl}" data-tp="${p.tp}" data-side="${p.side}">
        <span><a href="#/" class="symlink" title="Open ${esc(p.symbol)} chart" onclick="openTrade('${p.venue}','${esc(p.symbol)}',${p.tf || 0});return false"><b>${esc(p.symbol)}</b>${p.tf ? ` <i class="mute tfm">${p.tf >= 60 ? p.tf / 60 + "h" : p.tf + "m"}</i>` : ""}</a> ${p.sl !== p.sl0 ? '<i class="trail">TRAILING</i>' : ""}${p.watch ? `<i class="trail ${p.watch.status === "reversing" || p.watch.status === "reversal" ? "rev" : ""}" title="Re-scored every 30 s on all 7 factors">${p.watch.status === "reversal" ? "REVERSAL" : p.watch.status === "reversing" ? "TURNING" : "WATCH"} ${signed(p.watch.signed, 0)}</i>` : ""}</span><span class="${p.side === "BUY" ? "ok" : "bad"}"><b>${esc(p.side)}</b></span><span class="mono">${num(p.lots, 2)}</span>
        <span class="mono mute">${pxs(p.entry, p.symbol)}</span><span class="mono lv"><b>${pxs(p.last_price, p.symbol)}</b></span><span class="mono bad">${pxs(p.sl, p.symbol)}</span><span class="mono ok">${pxs(p.tp, p.symbol)}</span>
        <span class="pbar"><i class="pf"></i><i class="pn"></i></span><span class="mono r pl ${p.floating < 0 ? "bad" : "ok"}"><b>${money(p.floating)}</b></span><span class="mono r pr">–</span>
        <button class="btn dng sm" onclick="askAct('Close ${esc(p.symbol)} at market?','/api/close/${p.venue}/${p.ticket}','Closed')">Close</button></div>`).join("") || '<div class="pad mute" style="padding-top:16px">Nothing open on this venue.</div>'}</div></div>`;
  } else if (S.tab === "pend") {
    h = pend.length ? pend.map((s) => { const l = s.levels || {}; return `<div class="appr"><div class="what"><strong>${esc(s.symbol)}</strong> ${biasPill(s.bias)} <span class="mono mute">${num(s.lots, 2)} lots · entry ${esc(l.entry)} · SL ${esc(l.sl)} · TP ${esc(l.tp)}</span>
        <div class="mono mute">expires ${hm(s.expires_at)} · <a href="#/signal/${s.id}">details</a></div></div><div><button class="btn pri sm" onclick="act('/api/approve/${s.id}',{},'Approved')">Approve</button> <button class="btn sm" onclick="act('/api/reject/${s.id}',{},'Rejected')">Reject</button></div></div>`; }).join("")
      : `<div class="pad mute" style="padding-top:16px">Nothing waiting. ${(S.st.venues[S.venue] || {}).auto ? "Auto-approve is on, so cleared signals go straight to the broker." : ""}</div>`;
  } else if (S.tab === "hist") {
    const rows = S.posd.closed.filter((p) => p.venue === S.venue);
    h = `<div class="tw"><table style="min-width:520px"><tbody>${rows.map((p) => `<tr><td><b>${esc(p.symbol)}</b> <span class="pill ${p.side === "BUY" ? "buy" : "sell"}">${esc(p.side)}</span></td><td class="mono">${num(p.lots, 2)} lots</td><td class="mono">${esc(p.entry)} → ${esc(p.close_price)}</td><td class="mono ${p.profit < 0 ? "bad" : "ok"}">${money(p.profit)}</td><td class="mono mute">${hm(p.closed_at)}</td></tr>`).join("") || '<tr><td class="mute">Nothing closed yet on this venue.</td></tr>'}</tbody></table></div>`;
  } else {
    h = S.actv.map((x) => `<div class="feed"><span class="t">${hm(x.ts)}</span><span class="${x.level === "error" ? "bad" : x.level === "warn" ? "warn" : ""}">${x.symbol ? "<strong>" + esc(x.symbol) + "</strong> " : ""}${esc(x.text)}${x.signal_id ? ` <a href="#/signal/${x.signal_id}">view</a>` : ""}</span></div>`).join("") || '<div class="pad mute">No activity yet.</div>';
  }
  $("btab").innerHTML = h;
  updateLivePos();
}
window.pickTab = (k) => { S.tab = k; renderBottom(); };
function updateLivePos() {
  document.querySelectorAll(".prow[data-k]").forEach((row) => {
    const L = S.live[row.dataset.k]; if (!L) return;
    const sym = (S.posd.open.find((p) => p.venue + ":" + p.ticket === row.dataset.k) || {}).symbol || S.sym;
    row.querySelector(".lv b").textContent = pxs(L.live, sym);
    const pl = row.querySelector(".pl"); pl.className = "mono r pl " + (L.floating < 0 ? "bad" : "ok"); pl.firstChild.textContent = money(L.floating);
    const pr = row.querySelector(".pr"); pr.textContent = (L.r >= 0 ? "+" : "") + L.r.toFixed(2) + "R"; pr.className = "mono r pr " + (L.r < 0 ? "bad" : "ok");
    const e = +row.dataset.e, sl = +row.dataset.sl, tp = +row.dataset.tp, a = Math.min(sl, tp), b = Math.max(sl, tp), pc = (v) => Math.min(100, Math.max(0, ((v - a) / (b - a)) * 100));
    const pe = pc(e), pn = pc(L.live), f = row.querySelector(".pf"), n = row.querySelector(".pn");
    f.style.left = Math.min(pe, pn) + "%"; f.style.width = Math.abs(pn - pe) + "%"; f.style.background = (row.dataset.side === "BUY" ? L.live >= e : L.live <= e) ? "var(--buy)" : "var(--sell)"; n.style.left = "calc(" + pn + "% - 1px)";
  });
}

/* ------------------------------------------------------------ settings */
const swBtn = (on, label, path, body, confirmMsg) => `<button class="tg ${on ? "on" : ""}" role="switch" aria-checked="${on}" aria-label="${esc(label)}" onclick="${confirmMsg ? `if(!confirm('${confirmMsg}'))return;` : ""}act('${path}',${body},'${esc(label)} ${on ? "off" : "on"}')"><i></i></button>`;

/* every rule that can be changed by hand: an input per rule, saved together per card */
function tuneRows(group) {
  return S.st.tune.filter((t) => t.group === group).map((t) => `<div class="krow trow ${t.custom ? "mine" : ""}"><span>${esc(t.label)}${t.custom ? ` <em class="mute" title="Profile value">(profile ${num(t.base, t.step < 0.1 ? 2 : 1)})</em>` : ""}</span>
    <span class="tin"><input id="tn_${t.key}" data-g="${group}" data-k="${t.key}" class="mono" type="number" min="${t.min}" max="${t.max}" step="${t.step}" value="${t.value}" aria-label="${esc(t.label)}">${t.custom ? `<button class="btn sm" title="Back to the profile value" aria-label="Reset ${esc(t.label)}" onclick="act('/api/tune',{clear:['${t.key}']},'Back to the profile value')">↺</button>` : ""}</span></div>`).join("");
}
const tuneBar = (group) => `<div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:12px"><button class="btn pri sm" onclick="saveTune('${group}')">Save</button>${S.st.tune.some((t) => t.group === group && t.custom) ? `<button class="btn sm" onclick="act('/api/tune',{clear:${JSON.stringify(S.st.tune.filter((t) => t.group === group && t.custom).map((t) => t.key))}},'Back to the profile values')">Reset these</button>` : ""}</div>`;
window.saveTune = (group) => { const b = {}; document.querySelectorAll(`input[data-g="${group}"]`).forEach((el) => { if (el.value !== "") b[el.dataset.k] = Number(el.value); }); act("/api/tune", b, "Saved"); };
async function settings() {
  $app.className = "wrap"; S.mounted = false;
  const st = await api("/api/state"); S.st = st; renderBar();
  const r = st.risk, th = st.thresholds, V = Object.values(st.venues), u = st.usage, ag = st.feeds.ages_min, fast = st.profile === "fast";
  const rail = [["venues", "Venues"], ["profile", "Trading profile"], ["risk", "Risk limits"], ["ai", "Claude and rules"], ["trailing", "Trailing stop"], ["symbols", "Symbols"], ["feeds", "Feeds and keys"], ["danger", "Danger zone"]];
  const prof = [["Score needed", "±20", "±35"], ["Factors that must agree", "50%", "60%"], ["Candle must move (spreads)", "1.5", "2"], ["Stop no wider than (ATR)", "2.5", "3.5"], ["Default stop / target (ATR)", "1.0 / 1.5", "1.5 / 3.0"], ["Auto chart climbs below (spreads)", "2.5", "3"], ["Claude re-asked after NO", "5 min", "15 min"], ["Claude confidence needed", "0.55", "0.60"], ["Max open trades", "5", "3"], ["Minimum reward : risk", "1.1", "1.5"]];
  const all = Array.from(new Set(st.symbols_default.concat(st.symbols)));
  const kv = (a, b) => `<div class="kv"><span>${a}</span><span class="mono">${b}</span></div>`;
  const feed = (n, ok, extra) => `<div class="kv"><span><span class="dot ${ok ? "g" : "r"}"></span>${n}</span><span class="mono mute">${extra}</span></div>`;
  $app.innerHTML = `<div class="set"><nav class="rail" aria-label="Settings sections"><span class="lab">SETTINGS</span>${rail.map(([id, l]) => `<a href="#/settings" onclick="document.getElementById('s-${id}').scrollIntoView({behavior:'smooth'});return false">${l}</a>`).join("")}</nav><div class="setc">
    <section class="card sec" id="s-venues"><h2>Trading venues</h2><p class="mute">Three venues run side by side, each on its own candle size. Every switch takes effect on the next cycle.</p><div class="vgrid">${V.map((v) => `<div class="vcard"><div class="vh"><b>${esc(v.label)}</b><span class="pill ${v.name === "live" ? "warn" : v.connected === false ? "red" : "none"}">${v.locked ? "LOCKED" : v.connected === false ? "NOT CONNECTED" : v.name === "paper" ? "SIMULATED" : v.account && v.account.demo ? "DEMO" : "CONNECTED"}</span></div>
      <div class="mute" style="font-size:12px">${esc(VDESC[v.name])}</div>
      <div class="krow"><span>Trade</span>${v.locked ? `<button class="btn sm" onclick="if(confirm('Unlock LIVE trading? Real money on a real account. Live stays OFF until you switch it on.'))act('/api/allow-live',{on:true},'Live unlocked')">Unlock</button>` : swBtn(v.on, v.label + " venue", "/api/venue/" + v.name, `{on:${!v.on}}`, v.name === "live" && !v.on ? "Switch LIVE trading on? Real money on a real account." : "")}</div>
      <div class="krow"><span>Auto-approve</span>${v.locked ? "" : swBtn(v.auto, v.label + " auto-approve", "/api/venue/" + v.name, `{auto:${!v.auto}}`, v.name === "live" && !v.auto ? "Auto-approve LIVE trades? Orders will go out without your click." : "")}</div>
      <div class="krow"><span>Chart candles</span><select aria-label="${esc(v.label)} candle size" onchange="act('/api/venue/${v.name}',{tf:Number(this.value)},'${esc(v.label)} candles set')">${st.timeframes.map((m) => `<option value="${m}" ${m === v.tf ? "selected" : ""}>${m === 60 ? "1 hour" : m + " min"}</option>`).join("")}</select></div>
      ${v.name === "paper" ? `<div class="krow"><span>Balance (USD)</span><span><input id="pbal" class="mono" type="number" min="100" step="100" value="${v.account ? Math.round(v.account.balance * 100) / 100 : 10000}" aria-label="Paper balance" style="width:120px;text-align:right"> <button class="btn sm pri" onclick="act('/api/paper/balance',{balance:Number($('pbal').value)},'Paper balance set')">Set</button></span></div><button class="btn sm" onclick="askAct('Reset the paper account? Balance returns to your set amount and history is cleared.','/api/paper/reset','Paper account reset')">Reset account…</button>` : v.name !== "paper" && v.connected === false ? `<a class="btn sm" href="/ctrader/connect">Connect cTrader</a>` : ""}</div>`).join("")}</div></section>
    <section class="card sec" id="s-profile"><h2>Trading profile and entry rules</h2><p class="mute">Fast takes more trades with looser filters; Normal is stricter. Pick a profile, then change any number below by hand. Your values stay when you switch profile, and each shows the profile value beside it so you can go back (↺).</p>
      <div class="seg0"><button class="btn ${fast ? "pri" : ""}" onclick="act('/api/profile',{profile:'fast'},'Fast profile')">Fast</button> <button class="btn ${fast ? "" : "pri"}" onclick="act('/api/profile',{profile:'normal'},'Normal profile')">Normal</button></div>
      <h3 class="sub">Entry rules</h3>${tuneRows("entry")}${tuneBar("entry")}
      <h3 class="sub">Stops and targets</h3>${tuneRows("stops")}${tuneBar("stops")}</section>
    <section class="card sec" id="s-risk"><h2>Risk limits</h2><p class="mute">Enforced in code on every venue. A venue pauses itself when a loss limit is hit. Changes apply from the next check.</p>
      ${tuneRows("risk")}${kv("Total open risk limit", num(r.open_risk_cap_pct, 1) + "%")}${tuneBar("risk")}</section>
    <section class="card sec" id="s-ai"><h2>Claude and the rules</h2><p class="mute">The rules score a setup in code. Claude gives the final YES or NO on every trade that clears the gate.</p>
      <div class="krow big"><div><b>Claude veto</b><div class="mute" style="font-size:12px">On: Claude can say NO. Off: trades follow the score and the gate alone.</div></div>${swBtn(st.claude_veto, "Claude veto", "/api/claude-veto", `{on:${!st.claude_veto}}`, st.claude_veto ? "Turn Claude's veto OFF? Trades will then follow the score and the risk gate alone." : "")}</div>
      ${kv("Claude calls today", `${u.claude} / ${u.claude_cap}`)}${tuneRows("claude")}${tuneBar("claude")}
      <h3 class="sub">How much each factor counts in the score</h3><p class="mute" style="font-size:12px;margin:0 0 6px">Weights are relative: the score is the weighted average of the factors that have data.</p>${tuneRows("weights")}${tuneBar("weights")}</section>
    <section class="card sec" id="s-trailing"><h2>Trailing stop</h2><p class="mute">R is the first stop distance. Locks profit as the trade moves your way.</p>
      <div class="krow big"><div><b>Trailing stop</b></div>${swBtn(st.trail, "Trailing stop", "/api/trail", `{on:${!st.trail}}`)}</div>
      <div class="krow big"><div><b>Reversal watch</b><div class="mute" style="font-size:12px">Re-scores every open trade on all 7 factors (news sentiment included) every 30 s. When the view turns against the trade the stop is tightened to lock profit; when the flip is confirmed twice with 3+ factors against, the trade is closed.</div></div>${swBtn(st.watch, "Reversal watch", "/api/watch", `{on:${!st.watch}}`)}</div>
      <h3 class="sub">Trailing steps</h3>${tuneRows("trail")}${tuneBar("trail")}
      <h3 class="sub">Reversal watch</h3>${tuneRows("watch")}${tuneBar("watch")}
      <div class="note">R is the first stop distance. Once the trade reaches the first step the stop moves to break-even plus the locked profit. From the second step it follows the price at the set gap and the target moves ahead of it. The stop never loosens.</div></section>
    <section class="card sec" id="s-symbols"><h2>Symbols</h2><p class="mute">${st.symbols.length} traded. Toggle a symbol to include it from the next cycle.</p><div class="chips2">${all.map((s) => { const on = st.symbols.includes(s); const list = on ? st.symbols.filter((x) => x !== s) : st.symbols.concat([s]); return `<button class="sc ${on ? "on" : ""}" aria-pressed="${on}" onclick="act('/api/symbols',{symbols:'${list.join(",")}'},'Symbols saved')" ${on && st.symbols.length <= 1 ? "disabled" : ""}>${esc(s)}</button>`; }).join("")}</div>
      ${st.symbols_custom ? `<button class="btn sm" style="margin-top:12px" onclick="act('/api/symbols',{reset:true},'Back to the environment list')">Use environment list</button>` : ""}</section>
    <section class="card sec" id="s-feeds"><h2>Data feeds and keys</h2>${feed("Claude API", st.feeds.claude_key, `${u.claude}/${u.claude_cap} today`)}${feed("FreeLLMAPI (news tagging)", st.feeds.freellm_url, `${u.freellm}/${u.freellm_cap} today`)}${feed("FRED macro", st.feeds.fred_key, ago(ag.macro))}${feed("CFTC positioning", ag.cot != null, ago(ag.cot))}${feed("Economic calendar", ag.calendar != null, ago(ag.calendar))}${feed("News", ag.news_run != null, ago(ag.news_run))}
      <div style="margin-top:10px"><a href="/api/diagnostics">Open diagnostics</a></div></section>
    <section class="card sec danger" id="s-danger"><h2>Danger zone</h2><p class="mute">Live trading uses real money and stays locked until you unlock it.</p><div style="display:flex;flex-wrap:wrap;gap:10px">
      ${st.halted && st.halted.on ? `<button class="btn" onclick="act('/api/resume',{},'Resumed')">Resume trading</button>` : `<button class="btn dng" onclick="askAct('Halt all trading on every venue?','/api/halt','Halted')">Halt all trading</button>`}
      ${st.allow_live ? `<button class="btn" onclick="act('/api/allow-live',{on:false},'Live locked')">Lock Live</button>` : `<button class="btn" onclick="if(confirm('Unlock LIVE trading? Real money on a real account.'))act('/api/allow-live',{on:true},'Live unlocked')">Unlock Live…</button>`}
      <button class="btn" onclick="if(confirm('Drop every value you set by hand and return to the profile values?'))act('/api/tune',{reset:true},'All rules back to the profile')">Reset all rules to profile…</button> <button class="btn" onclick="askAct('Reset the paper account? Balance and history are cleared.','/api/paper/reset','Paper account reset')">Reset Paper account…</button></div></section>
  </div></div>`;
}


/* ------------------------------------------------------------ signal detail */
function chart(closes, l, bias) {
  if (!closes || closes.length < 2) return '<div class="pad mute">No price context stored.</div>';
  const W = 760, H = 240, P = 8;
  const lv = [l && l.entry, l && l.sl, l && l.tp].filter((x) => x != null);
  const all = closes.concat(lv), lo = Math.min(...all), hi = Math.max(...all), rg = hi - lo || 1;
  const x = (i) => P + (i / (closes.length - 1)) * (W - 2 * P - 60), y = (v) => H - P - ((v - lo) / rg) * (H - 2 * P);
  const pts = closes.map((c, i) => `${x(i).toFixed(1)},${y(c).toFixed(1)}`).join(" ");
  const line = (v, col, lab) => v == null ? "" : `<line x1="${P}" x2="${W - 56}" y1="${y(v)}" y2="${y(v)}" stroke="${col}" stroke-dasharray="4 4" stroke-width="1"/><text x="${W - 52}" y="${y(v) + 4}" fill="${col}" font-size="11">${lab} ${esc(v)}</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Last ${closes.length} closes with entry, stop and target"><polyline points="${pts}" fill="none" stroke="#EDEDEE" stroke-width="1.6"/>
    ${l ? line(l.tp, "#3DC48F", "TP") + line(l.entry, "#F58A14", "ENTRY") + line(l.sl, "#F06D6D", "SL") : ""}</svg>`;
}

async function detail(id) {
  const s = await api("/api/signal/" + id);
  const l = s.levels || {}, c = s.claude || {}, g = s.gate || {}, p = s.position;
  let html = pageHead(`${esc(s.symbol)} ${biasPill(s.bias)}`, `Signal #${s.id} · ${esc(s.candle_time)} · ${esc(VLABEL[s.venue] || "analysis")}${s.mode ? " · " + esc(s.mode.split(":")[1]) : ""}`, `${stPill(s.status)}<a class="btn" href="#/">Back to terminal</a>`)
    + `
    <p class="mute" style="margin:0">${esc(s.reason)}</p>
    <div class="grid"><div class="col-main">
    <section class="card"><div class="hd"><h2>Price and levels</h2><span class="mono mute">last ${(s.context || {}).closes ? s.context.closes.length : 0} closed ${esc(s.timeframe || "H1")} candles</span></div><div class="pad">${chart((s.context || {}).closes, l, s.bias)}</div></section>
    <section class="card"><div class="hd"><h2>Why the score is ${signed(s.score, 1)}</h2><span class="mono mute">${s.agree}/${s.available} factors agree</span></div>`;
  for (const [k, f] of Object.entries(s.factors || {})) {
    html += `<div class="factor"><div><strong style="text-transform:capitalize">${esc(k)}</strong> <span class="mute mono">w${esc(f.weight)}</span></div>
      <div>${f.score == null ? '<span class="mute">not available</span>' : scoreBar(f.score)}</div><div class="mono r">${f.score == null ? "–" : signed(f.score, 0)}</div>
      <div class="hint">${esc(f.reading)}</div></div>`;
  }
  html += `</section></div><div class="col-side">
    <section class="card"><div class="hd"><h2>Claude's call</h2><span class="pill ${c.decision === "YES" ? "buy" : "none"}">${esc(c.decision || "not asked")}${c.confidence != null ? " " + num(c.confidence, 2) : ""}</span></div>
      ${c.thesis ? `<div class="pad"><div class="lab">Thesis</div><p>${esc(c.thesis)}</p><div class="lab">Strongest case against</div><p>${esc(c.against)}</p>
      <div class="lab">Invalidation</div><p>${esc(c.invalidation)}</p><div class="lab">Main risk</div><p>${esc(c.main_risk)}</p><div class="mono mute">${esc(c.model)}</div></div>`
      : `<div class="pad mute">${esc(c.error || "Claude was not asked: an earlier check stopped this signal.")}</div>`}</section>
    <section class="card"><div class="hd"><h2>Order</h2></div>
      <div class="row"><span>Entry</span><span class="mono">${esc(l.entry)}</span></div><div class="row"><span>Stop</span><span class="mono bad">${esc(l.sl)}</span></div>
      <div class="row"><span>Target</span><span class="mono ok">${esc(l.tp)}</span></div><div class="row"><span>R:R</span><span class="mono">${esc(l.rr)}</span></div>
      <div class="row"><span>Lots</span><span class="mono">${s.lots != null ? num(s.lots, 2) : "–"}</span></div>
      ${p ? `<div class="row"><span>Ticket ${esc(p.ticket)}</span><span class="mono ${p.profit != null ? (p.profit < 0 ? "bad" : "ok") : ""}">${p.status === "closed" ? money(p.profit) : money(p.floating)}</span></div>` : ""}</section>
    <section class="card"><div class="hd"><h2>Risk gate</h2><span class="pill ${g.ok ? "buy" : "warn"}">${g.ok ? "passed" : "blocked"}</span></div>
      ${(g.checks || []).map((k) => `<div class="row"><span><span class="dot ${k.ok ? "g" : "r"}"></span>${esc(k.name)}</span><span class="mono mute" style="text-align:right">${esc(k.detail)}</span></div>`).join("") || '<div class="pad mute">No gate result stored.</div>'}</section>
    </div></div>`;
  $app.innerHTML = html;
}

/* ------------------------------------------------------------ reports */
let repDays = 0;
function curveSvg(c) {
  if (!c || c.length < 2) return '<div class="pad mute">Not enough closed trades for a curve yet.</div>';
  const W = 700, H = 150, P = 8, vals = [0].concat(c.map((x) => x.pl));
  const lo = Math.min(...vals), hi = Math.max(...vals), rg = hi - lo || 1;
  const x = (i) => P + (i / (vals.length - 1)) * (W - 2 * P), y = (v) => H - P - ((v - lo) / rg) * (H - 2 * P);
  const last = vals[vals.length - 1], col = last >= 0 ? "#3DC48F" : "#F06D6D";
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Cumulative profit and loss"><line x1="${P}" x2="${W - P}" y1="${y(0)}" y2="${y(0)}" stroke="#4A4A52" stroke-dasharray="4 4"/>
    <polyline points="${vals.map((v, i) => x(i).toFixed(1) + "," + y(v).toFixed(1)).join(" ")}" fill="none" stroke="${col}" stroke-width="1.8"/></svg>`;
}
const pageHead = (title, sub, tools) => `<div class="pagehead"><div><h1>${title}</h1><div class="lab">${sub}</div></div><div class="tools">${tools || ""}</div></div>`;
/* stale-while-revalidate: show the last copy of a page's data at once, refresh it behind the scenes */
const PC = {};
async function cget(path) {
  const hit = PC[path];
  const fresh = api(path).then((d) => { const changed = !hit || JSON.stringify(hit) !== JSON.stringify(d); PC[path] = d; if (hit && changed && curPath === path) render(); return d; });
  if (hit) { fresh.catch(() => {}); return hit; }
  return fresh;
}
let curPath = "";
async function reports() {
  curPath = "/api/report?days=" + repDays;
  const r = await cget(curPath);
  let html = pageHead("Trading reports", "Paper · Demo · Live, closed trades after commission and swap", `<span class="seg" style="padding:0">${[[0, "All time"], [1, "Today"], [7, "7 days"], [30, "30 days"]].map(([d, l]) => `<button aria-pressed="${repDays === d}" onclick="repDays=${d};render()">${l}</button>`).join("")}</span>`);
  const tot = Object.values(r.venues);
  html += `<section class="card strip">${Object.entries(r.venues).map(([v, s]) => `<div><div class="lab">${esc(VLABEL[v])} net P/L</div>
    <div class="v ${s.net < 0 ? "bad" : s.net > 0 ? "ok" : ""}">${money(s.net)}</div>
    <div class="s">${s.trades} trades · ${num(s.win_rate, 0)}% win · today ${money(s.today)}</div></div>`).join("")}</section>`;
  for (const [v, s] of Object.entries(r.venues)) {
    const kv = (a, b, cls) => `<div class="row"><span>${a}</span><span class="mono ${cls || ""}">${b}</span></div>`;
    html += `<div class="grid"><div class="col-main"><section class="card"><div class="hd"><h2>${esc(VLABEL[v])}</h2><span class="mono mute">cumulative P/L</span></div><div class="pad">${curveSvg(s.curve)}</div>
      <div class="tw"><table><thead><tr><th>Symbol</th><th>Trades</th><th>Win rate</th><th class="r">Net</th></tr></thead><tbody>${s.by_symbol.map((x) => `<tr><td><strong>${esc(x.symbol)}</strong></td><td class="mono">${x.trades}</td>
        <td class="mono">${num(x.win_rate, 0)}%</td><td class="mono r ${x.net < 0 ? "bad" : "ok"}">${money(x.net)}</td></tr>`).join("") || '<tr><td colspan="4" class="mute">No closed trades in this period.</td></tr>'}</tbody></table></div></section>
      <section class="card"><div class="hd"><h2>${esc(VLABEL[v])} recent trades</h2></div><div class="tw"><table style="min-width:520px"><tbody>${s.recent.map((p) => `<tr><td>${esc(p.symbol)} <span class="pill ${p.side === "BUY" ? "buy" : "sell"}">${esc(p.side)}</span></td>
        <td class="mono">${num(p.lots, 2)} lots</td><td class="mono">${esc(p.entry)} → ${esc(p.close_price)}</td><td class="mono ${p.profit < 0 ? "bad" : "ok"}">${money(p.profit)}</td><td class="mono mute">${hm(p.closed_at)}</td></tr>`).join("") || '<tr><td class="mute">Nothing closed yet.</td></tr>'}</tbody></table></div></section></div>
      <div class="col-side"><section class="card"><div class="hd"><h2>${esc(VLABEL[v])} statistics</h2></div>
      ${kv("Net P/L", money(s.net), s.net < 0 ? "bad" : "ok")}${kv("Today / 7 days", money(s.today) + " / " + money(s.week))}${kv("Open now", s.open_count + " · floating " + money(s.floating))}
      ${kv("Trades (won / lost)", s.trades + " (" + s.wins + " / " + s.losses + ")")}${kv("Win rate", num(s.win_rate, 1) + "%")}
      ${kv("Profit factor", s.profit_factor == null ? "–" : num(s.profit_factor, 2))}${kv("Average win / loss", money(s.avg_win) + " / " + money(s.avg_loss))}
      ${kv("Expectancy per trade", money(s.expectancy))}${kv("Best / worst", money(s.best) + " / " + money(s.worst))}${kv("Max drawdown", money(s.max_drawdown), s.max_drawdown ? "bad" : "")}</section></div></div>`;
  }
  $app.innerHTML = html;
}

/* ------------------------------------------------------------ contract specs */
let specVenue = "paper";
window.saveSpec = async (sym) => {
  const g = (k) => document.getElementById(k + "_" + sym).value;
  try { await api("/api/paper/spec/" + sym, "POST", { spread: g("sp"), contract: g("ct"), digits: g("dg"), volume_min: g("vm"), volume_step: g("vs") }); toast(sym + " saved"); } catch (e) { toast(e.message, true); }
  render();
};
async function specs() {
  curPath = "/api/specs?venue=" + specVenue;
  const r = await cget(curPath);
  let html = pageHead("Contract specifications", "What each venue's broker says a lot is, and what it costs to trade", `<span class="seg" style="padding:0">${["paper", "demo", "live"].map((v) => `<button aria-pressed="${specVenue === v}" onclick="specVenue='${v}';render()">${VLABEL[v]}</button>`).join("")}</span>`);
  html += `<p class="mute" style="margin:0">${r.editable ? "Paper is a simulation, so you set the spread and contract here. Demo and Live show exactly what your cTrader account reports, read-only." : "Read from the cTrader account itself: contract size, digits, lot limits and the live spread are the broker's own, so metals, yen pairs and any non-standard symbol are sized from these numbers."}</p>`;
  if (r.error) html += `<div class="banner warn"><strong>${esc(VLABEL[specVenue])} not reachable.</strong><span class="mute">${esc(r.error)}</span></div>`;
  const inp = (id, sym, v, w) => `<input id="${id}_${sym}" value="${esc(v)}" style="width:${w}px;background:var(--panel2);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 6px;font:12px var(--font-mono)">`;
  html += `<section class="card"><div class="tw"><table style="min-width:900px"><thead><tr><th>Symbol</th><th>Digits</th><th>Units per lot</th><th>Lot min / step</th><th>P/L in</th><th>Spread (price)</th><th>Points</th><th>Cost per lot</th><th>Bid / Ask</th>${r.editable ? "<th></th>" : ""}</tr></thead><tbody>`;
  for (const x of r.rows) {
    if (!x.ok) { html += `<tr><td><strong>${esc(x.symbol)}</strong></td><td colspan="9" class="mute">${esc(x.error || "no data")}</td></tr>`; continue; }
    if (r.editable) {
      html += `<tr><td><strong>${esc(x.symbol)}</strong>${x.custom ? ' <span class="pill acc">custom</span>' : ""}</td><td>${inp("dg", x.symbol, x.digits, 44)}</td><td>${inp("ct", x.symbol, x.contract, 90)}</td>
        <td>${inp("vm", x.symbol, x.volume_min, 56)} / ${inp("vs", x.symbol, x.volume_step, 56)}</td><td class="mono">${esc(x.profit_ccy)}</td><td>${inp("sp", x.symbol, x.spread, 80)}</td>
        <td class="mono">${x.spread_points ?? "–"}</td><td class="mono">${num(x.cost_per_lot, 2)} ${esc(x.profit_ccy)}</td><td class="mono mute">${esc(x.bid)} / ${esc(x.ask)}</td>
        <td class="r"><button class="btn sm pri" onclick="saveSpec('${x.symbol}')">Save</button>${x.custom ? ` <button class="btn sm" onclick="act('/api/paper/spec/${x.symbol}',{reset:true},'Reset')">Default</button>` : ""}</td></tr>`;
    } else {
      html += `<tr><td><strong>${esc(x.symbol)}</strong> <span class="mute mono" style="font-size:11px">${esc(x.broker_symbol)}</span></td><td class="mono">${x.digits}</td><td class="mono">${num(x.contract, 0)}</td>
        <td class="mono">${x.volume_min} / ${x.volume_step}</td><td class="mono">${esc(x.profit_ccy)}</td><td class="mono">${esc(x.spread)}</td><td class="mono">${x.spread_points ?? "–"}</td>
        <td class="mono">${num(x.cost_per_lot, 2)} ${esc(x.profit_ccy)}</td><td class="mono mute">${esc(x.bid)} / ${esc(x.ask)}</td></tr>`;
    }
  }
  html += `</tbody></table></div></section>`;
  $app.innerHTML = html;
}

/* ------------------------------------------------------------ router */
async function ensureSt() {
  if (S.st) { renderBar(); api("/api/state").then((d) => { S.st = d; renderBar(); }).catch(() => {}); return; }
  S.st = await api("/api/state"); renderBar();
}
let again = false;
async function render() {
  if (busy) { again = true; return; }   // a render is in flight: run once more when it ends, so a click on a menu item is never dropped
  busy = true;
  try {
    const pg = pageName();
    if (pg !== "terminal") { S.mounted = false; $app.className = "wrap"; }
    if (pg === "signal") { await ensureSt(); await detail(location.hash.match(/(\d+)/)[1]); }
    else if (pg === "reports") { await ensureSt(); await reports(); }
    else if (pg === "specs") { await ensureSt(); await specs(); }
    else if (pg === "settings") await settings();
    else await terminal();
  } catch (e) {
    if (!$app.innerHTML.trim() || $app.querySelector("p.mute")) $app.innerHTML = `<p class="bad" style="padding:40px 24px">Could not load: ${esc(e.message)}</p>`;
    else toast(e.message, true);
  } finally { busy = false; if (again) { again = false; render(); } }
}
window.addEventListener("hashchange", () => { window.scrollTo(0, 0); render(); });
startStream();
render();
timer = setInterval(() => { if (!document.hidden && pageName() === "terminal") render(); }, 4000);
