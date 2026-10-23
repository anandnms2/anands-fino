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

const VLABEL = { demo: "Demo", live: "Live" };
const VDESC = { demo: "cTrader demo account with demo funds.", live: "cTrader live account. Real money." };
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
const S = { an: {}, venue: (["demo", "live"].includes(localStorage.getItem("fv")) ? localStorage.getItem("fv") : "demo"), sym: localStorage.getItem("fs") || "", tf: 0, tab: "pos", px: {}, last: {}, dir: {}, live: {}, lat: -1,
  ind: (() => { try { return Object.assign({ ema: true, bb: false, fvg: false, rsi: false, macd: false }, JSON.parse(localStorage.getItem("fi") || "{}")); } catch (e) { return { ema: true, bb: false, fvg: false, rsi: false, macd: false }; } })(), cross: null, st: null, latest: [], posd: { open: [], closed: [] }, appr: [], actv: [], det: null, detId: 0, candles: [], candleKey: "", mounted: false, drawQ: false };
const dig = (s) => (s === "XAUUSD" ? 2 : s === "XAGUSD" ? 3 : /JPY$/.test(s) ? 3 : 5);
const pxs = (v, s) => (v == null ? "–" : Number(v).toFixed(dig(s)));
const pageName = () => { const h = location.hash; return h.startsWith("#/signal") ? "signal" : h === "#/reports" ? "reports" : h === "#/specs" ? "specs" : h === "#/strategies" ? "strategies" : h === "#/matrix" ? "matrix" : h === "#/settings" ? "settings" : "terminal"; };
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
  barLive(); if (T) tkCalc();
  if (pageName() !== "terminal" || !S.mounted || !S.st) return;
  updateWatch(); updateHead(); updateLiveCandle(); updateLivePos(); queueDraw(); tkCalc();
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
  if (fe) { fe.textContent = (fl < 0 ? "-$" : "+$") + num(Math.abs(fl), 2); fe.className = "mono b " + (fl < 0 ? "bad" : fl > 0 ? "ok" : ""); }
  if (ee && a) ee.textContent = "$" + num(lf != null ? a.balance + lf : a.equity, 2);
}
const gr = (label, used, cap, text) => {
  const p = cap ? Math.max(0, Math.min(100, (used / cap) * 100)) : 0, c = p >= 80 ? "var(--sell)" : p >= 50 ? "var(--accent)" : "var(--buy)";
  return `<div class="gr"><span>${label}</span><div class="rb" role="img" aria-label="${esc(label)} ${Math.round(p)}% of its limit"><i style="width:${p}%;background:${c}"></i></div><span class="mono">${text}</span></div>`;
};
function renderBar() {
  const st = S.st; if (!st) return;
  const V = st.venues, v = V[S.venue] || {}, a = v.account, r = st.risk, pg = pageName();
  const nav = [["terminal", "#/", "Terminal"], ["matrix", "#/matrix", "Signal Matrix"], ["strategies", "#/strategies", "Strategy Lab"], ["reports", "#/reports", "Reports"], ["specs", "#/specs", "Contracts"], ["settings", "#/settings", "Settings"]];
  const dpl = a ? a.day_pnl : null, dcap = r.daily_loss_cap_pct, lossUsed = a ? Math.max(0, -a.day_pnl_pct) : 0;
  const kpi = (label, val, sub, id, cls) => `<div class="kpi"><span>${label}</span><b class="mono ${cls || ""}" ${id ? `id="${id}"` : ""}>${val}</b><i>${sub}</i></div>`;
  $bar.innerHTML = `<div class="bar-in"><div class="bar-r1"><a class="brand" href="#/"><img src="/static/logo.png" alt=""><div><b>Anands Fino</b><span>FOREX &amp; METALS · AI + RULES</span></div></a>
    <nav class="bnav" aria-label="Pages">${nav.map(([k, h, l]) => `<a href="${h}" class="${pg === k || (pg === "signal" && k === "terminal") ? "on" : ""}">${l}</a>`).join("")}</nav>
    <div class="vtabs" role="tablist" aria-label="Venue">${Object.values(V).map((x) => `<button role="tab" aria-selected="${x.name === S.venue}" class="${x.name === S.venue ? "on" : ""}" onclick="pickVenue('${x.name}')"><span class="vd ${x.locked ? "lk" : x.on ? "on" : ""}"></span>${esc(x.label)} <span class="mono">${x.locked ? "LOCKED" : x.on ? "ON" : "OFF"}</span></button>`).join("")}</div>
    <div class="chips"><span class="chip"><span id="bar-dot" class="dot"></span><span class="mono" id="bar-lat">stream …</span></span>
      <span class="chip">Profile <b class="acc">${st.profile === "fast" ? "FAST" : "NORMAL"}</b></span>
      <button class="chip chipbtn" title="Click to switch: ON = a symbol with no proven strategy does not trade" onclick="act('/api/proven-only',{on:${!st.proven_only}},'Proven only ${st.proven_only ? "OFF" : "ON"}')">Proven only <b class="${st.proven_only ? "ok" : "mute"}">${st.proven_only ? "ON" : "OFF"}</b></button>
      <span class="chip">Trailing <b class="${st.trail ? "ok" : "mute"}">${st.trail ? "ON" : "OFF"}</b></span>
      <span class="chip">Claude veto <b class="${st.claude_veto ? "ok" : "mute"}">${st.claude_veto ? "ON" : "OFF"}</b></span>
      ${st.halted && st.halted.on ? `<button class="halt on" onclick="act('/api/resume',{},'Resumed')">RESUME</button>` : `<button class="halt" onclick="askAct('Halt all trading on every venue?','/api/halt','Halted')">HALT ALL</button>`}
      <button class="halt flat" title="Close every open trade on every venue and halt trading" onclick="askAct('FLATTEN ALL: close every open trade on EVERY venue (including Live) at market and halt trading?','/api/flatten','Flattened: all trades closed, trading halted')">FLATTEN ALL</button></div></div>
    <div class="strip2" aria-label="Account">
      ${kpi("BALANCE", a ? "$" + num(a.balance, 2) : "–", a ? (a.demo ? "cTrader demo" : "cTrader live") : "not connected")}
      ${kpi("EQUITY", a ? "$" + num(a.equity, 2) : "–", a ? signed(a.day_pnl_pct, 2) + "% today" : "", "bar-eq")}
      ${kpi("FLOATING P/L", "–", (v.open_count || 0) + " open", "bar-fl", "b")}
      ${kpi("OPEN RISK", num(v.open_risk_pct || 0, 2) + "%", "limit " + num(r.open_risk_cap_pct, 1) + "%")}
      ${kpi("DAY P/L", dpl == null ? "–" : (dpl < 0 ? "-$" : "+$") + num(Math.abs(dpl), 2), "limit -" + dcap + "%", "", dpl < 0 ? "bad" : dpl > 0 ? "ok" : "")}
      <div class="kpi guard"><span>GUARD RAILS · THIS VENUE</span>
        ${gr("Daily loss", lossUsed, dcap, num(lossUsed, 1) + " / " + dcap + "%")}${gr("Open risk", v.open_risk_pct || 0, r.open_risk_cap_pct, num(v.open_risk_pct || 0, 1) + " / " + num(r.open_risk_cap_pct, 1) + "%")}${gr("Loss streak", v.loss_streak || 0, r.max_loss_streak, (v.loss_streak || 0) + " / " + r.max_loss_streak)}</div></div></div>`;
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
    <section class="card chartc"><div class="chead" id="chead"></div><div class="cv" id="cvw"><canvas id="cv" role="img" aria-label="Live candle chart"></canvas></div><div class="cfoot" id="cfoot"></div><div class="why" id="why"></div></section>
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

/* strategy status badge: PROVEN (with the strategy and candle), TESTING while a backtest runs, UNPROVEN when nothing has passed yet */
function stratBadge(sym) {
  const st = S.st;
  if (!st.strategy_mode) return `<i class="sb off" title="Strategy mode is off: trading on the score rules">SCORE</i>`;
  const x = (st.strategies || {})[sym];
  if (x) return `<i class="sb ok" title="Proven on unseen data: ${esc(x.strategy)}, ${x.tf}m candles, stop ${x.stop_k} ATR, target ${x.rr}x. Unseen: ${x.test.n} trades, ${num(x.test.exp, 2)}R each, PF ${num(x.test.pf, 2)}">PROVEN · ${esc(x.strategy)} · ${tfName(x.tf).toUpperCase()}</i>`;
  if (st.bt_state === "running") return `<i class="sb test" title="A backtest is running">TESTING</i>`;
  return st.proven_only ? `<i class="sb un" title="No setup has passed the backtest. Proven only is ON, so this symbol does not trade.">UNPROVEN · NO TRADE</i>` : `<i class="sb un" title="No setup has passed the backtest yet. It trades on the normal score rules, with the same risk gate.">UNPROVEN · SCORE RULES</i>`;
}
/* watchlist */
function latestFor(sym) { return S.latest.find((r) => r.symbol === sym); }
function renderWatch() {
  const st = S.st, pv = (x) => ((st.strategies || {})[x] ? 1 : 0), sa = (x) => Math.abs((latestFor(x) || {}).score || 0);
  const list = st.symbols.slice().sort((a, b) => pv(b) - pv(a) || sa(b) - sa(a));   // proven setups first, then the strongest score
  $("wlc").textContent = list.length + " symbols · sorted by edge";
  $("wl").innerHTML = list.map((s) => {
    const r = latestFor(s), sc = r ? r.score : null;
    return `<div class="wl-r ${s === S.sym ? "sel" : ""}" role="button" tabindex="0" onclick="pickSym('${s}')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();pickSym('${s}')}" aria-pressed="${s === S.sym}">
      <span><b>${esc(s)}</b><i class="mute">${esc(r ? r.timeframe : "")}</i>${stratBadge(s)}</span><span class="mono" id="wb-${s}">–</span><span class="mono" id="wa-${s}">–</span>
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
  $("chead").innerHTML = `<div class="ch-l"><b class="sym">${esc(S.sym)}</b>${stratBadge(S.sym)}<span class="mono big" id="hb">–</span><span class="mono mute" id="hs"></span></div>
    <div class="buysell"><button class="bs buy" onclick="openTicket('BUY')">BUY</button><button class="bs sell" onclick="openTicket('SELL')">SELL</button></div>
    <div class="tfs inds" role="group" aria-label="Chart indicators">${[["ema", "EMA"], ["bb", "Bollinger"], ["fvg", "FVG"], ["rsi", "RSI"], ["macd", "MACD"]].map(([k, l]) => `<button aria-pressed="${!!S.ind[k]}" onclick="togInd('${k}')">${l}</button>`).join("")}</div>
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
window.togInd = (k) => { S.ind[k] = !S.ind[k]; try { localStorage.setItem("fi", JSON.stringify(S.ind)); } catch (e) { /* private mode */ } renderHead(); queueDraw(); };
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
  if (now >= last[0] + S.tf * 60) c.push([Math.floor(now / (S.tf * 60)) * S.tf * 60, mid, mid, mid, mid]);
  else { last[4] = mid; last[2] = Math.max(last[2], mid); last[3] = Math.min(last[3], mid); }
  if (c.length > 400) c.shift();
}
const closesOf = (C) => C.map((c) => c[4]);
function emaArr(v, k) { const a = 2 / (k + 1); let e = null; return v.map((x) => (e = e == null ? x : e + a * (x - e))); }
function bbArr(C) { const c = closesOf(C); return c.map((_, i) => { if (i < 19) return null; const w = c.slice(i - 19, i + 1), m = w.reduce((a, b) => a + b, 0) / 20, sd = Math.sqrt(w.reduce((a, b) => a + (b - m) * (b - m), 0) / 20); return [m - 2 * sd, m, m + 2 * sd]; }); }
function rsiArr(C, n = 14) { const c = closesOf(C), out = c.map(() => null); let ag = 0, al = 0; for (let i = 1; i < c.length; i++) { const d = c[i] - c[i - 1], g = Math.max(d, 0), l = Math.max(-d, 0); if (i <= n) { ag += g / n; al += l / n; } else { ag = (ag * (n - 1) + g) / n; al = (al * (n - 1) + l) / n; } if (i >= n) out[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al); } return out; }
function macdArr(C) { const c = closesOf(C), m = emaArr(c, 12).map((x, i) => x - emaArr(c, 26)[i]); const sg = emaArr(m, 9); return m.map((x, i) => [x, sg[i], x - sg[i]]); }
function fvgZones(C) {   // unfilled 3-candle gaps: [first index, bottom, top, bull?]
  const out = [];
  for (let i = 2; i < C.length; i++) {
    const a = C.slice(Math.max(0, i - 14), i + 1).reduce((s, c) => s + (c[2] - c[3]), 0) / Math.min(15, i + 1);
    let z = null;
    if (C[i][3] > C[i - 2][2] && C[i][3] - C[i - 2][2] >= 0.5 * a) z = [i - 2, C[i - 2][2], C[i][3], true];
    else if (C[i][2] < C[i - 2][3] && C[i - 2][3] - C[i][2] >= 0.5 * a) z = [i - 2, C[i][2], C[i - 2][3], false];
    if (!z) continue;
    let filled = false; for (let j = i + 1; j < C.length; j++) { if (z[3] ? C[j][4] < z[1] : C[j][4] > z[2]) { filled = true; break; } }
    if (!filled) out.push(z);
  }
  return out.slice(-6);
}
function queueDraw() { if (S.drawQ) return; S.drawQ = true; requestAnimationFrame(() => { S.drawQ = false; drawChart(); }); }
function openPos(sym) { return S.posd.open.find((p) => p.venue === S.venue && p.symbol === sym); }
function drawChart() {
  const cv = $("cv"); if (!cv) return;
  const box = cv.parentElement, w = box.clientWidth, h = box.clientHeight, dpr = window.devicePixelRatio || 1;
  if (!w || !h) return;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); cv.style.width = w + "px"; cv.style.height = h + "px"; }
  const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  const np = (S.ind.rsi ? 1 : 0) + (S.ind.macd ? 1 : 0), PH = 78, hm = h - np * PH;
  const PAD = 76, slot = 8, n = Math.max(10, Math.floor((w - PAD - 12) / slot)), view = S.candles.slice(-n);
  const emaOf = (k) => { const a = 2 / (k + 1), out = []; let e = null; for (const c of S.candles) { e = e == null ? c[4] : e + a * (c[4] - e); out.push(e); } return out.slice(-view.length); };
  const EMAS = S.ind.ema && S.candles.length > 60 ? [[20, "#F58A14"], [50, "#5AA9E6"], [200, "#B48CF2"]].map(([k, col]) => [k, col, emaOf(k)]) : [];
  if (view.length < 2) { g.fillStyle = "#9C9EA6"; g.font = "13px Inter,sans-serif"; g.fillText("Loading candles…", 16, 28); return; }
  const P0 = openPos(S.sym), d = dig(S.sym), P = P0 && S.drag && S.drag.ticket === P0.ticket ? { ...P0, [S.drag.k]: S.drag.v } : P0;
  cv.style.touchAction = P0 ? "none" : "";
  let lo = Math.min(...view.map((c) => c[3])), hi = Math.max(...view.map((c) => c[2]));
  if (P) { lo = Math.min(lo, P.tp, P.sl, P.entry); hi = Math.max(hi, P.tp, P.sl, P.entry, P.sl0 || P.sl); }
  const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
  if (S.drag) { lo = S.drag.lo; hi = S.drag.hi; }   // the scale stays put while a line is being dragged
  S.geo = { lo, hi, h: hm };
  const Y = (v) => 8 + (1 - (v - lo) / (hi - lo)) * (hm - 20), x0 = w - PAD - view.length * slot;
  g.font = "11px 'JetBrains Mono',monospace"; g.textBaseline = "middle";
  for (let i = 0; i <= 5; i++) { const v = lo + ((hi - lo) * i) / 5, y = Y(v); g.strokeStyle = "#1a1a1f"; g.setLineDash([3, 4]); g.beginPath(); g.moveTo(0, y); g.lineTo(w - PAD, y); g.stroke(); g.setLineDash([]); g.fillStyle = "#6c6e75"; g.fillText(v.toFixed(d), w - PAD + 6, y); }
  view.forEach((c, i) => {
    const x = x0 + i * slot + slot / 2, up = c[4] >= c[1], col = up ? "#3DC48F" : "#F06D6D";
    g.strokeStyle = col; g.fillStyle = col; g.beginPath(); g.moveTo(x, Y(c[2])); g.lineTo(x, Y(c[3])); g.stroke();
    const t = Y(Math.max(c[1], c[4])), hh = Math.max(1, Math.abs(Y(c[1]) - Y(c[4]))); g.fillRect(x - 2.5, t, 5, hh);
  });
  EMAS.forEach(([k, col, arr]) => { g.strokeStyle = col; g.lineWidth = 1.2; g.beginPath(); arr.forEach((v, i) => { const x = x0 + i * slot + slot / 2; i ? g.lineTo(x, Y(v)) : g.moveTo(x, Y(v)); }); g.stroke(); g.lineWidth = 1; });
  EMAS.forEach(([k, col], i) => { g.fillStyle = col; g.textAlign = "left"; g.fillText("EMA " + k, 10 + i * 70, 16); });
  const off = S.candles.length - view.length, X = (i) => x0 + i * slot + slot / 2;
  if (S.ind.fvg) fvgZones(S.candles).forEach(([i0, bo, to, bull]) => { const xs = Math.max(0, X(i0 - off) - slot / 2); if (X(i0 - off) < 0 && i0 < off) return; g.fillStyle = bull ? "rgba(61,196,143,.16)" : "rgba(240,109,109,.16)"; g.fillRect(xs, Y(to), w - PAD - xs, Math.max(2, Y(bo) - Y(to))); g.fillStyle = bull ? "#3DC48F" : "#F06D6D"; g.fillText("FVG", xs + 3, Y(to) + 10); });
  if (S.ind.bb) { const B = bbArr(S.candles).slice(-view.length); [[0, "#6c6e75"], [1, "#6c6e75"], [2, "#6c6e75"]].forEach(([k, col]) => { g.strokeStyle = col; g.lineWidth = 1; g.setLineDash(k === 1 ? [3, 3] : []); g.beginPath(); let st = false; B.forEach((b, i) => { if (!b) return; st ? g.lineTo(X(i), Y(b[k])) : g.moveTo(X(i), Y(b[k])); st = true; }); g.stroke(); g.setLineDash([]); }); g.fillStyle = "#9C9EA6"; g.fillText("BB 20,2", 10 + EMAS.length * 70, 16); }
  const tag = (v, col, bg, txt, dash) => { const y = Y(v); g.strokeStyle = col; g.setLineDash(dash || []); g.beginPath(); g.moveTo(0, y); g.lineTo(w - PAD, y); g.stroke(); g.setLineDash([]);
    const t = txt + " " + v.toFixed(d), tw = g.measureText(t).width + 10; g.fillStyle = bg; g.fillRect(w - PAD - tw - 4, y - 9, tw, 18); g.fillStyle = col; g.textAlign = "left"; g.fillText(t, w - PAD - tw + 1, y); };
  if (P) {
    tag(P.tp, "#3DC48F", "#12281c", "TP ⇅", [5, 4]); tag(P.entry, "#EDEDEE", "#26262b", "ENTRY");
    tag(P.sl, "#F06D6D", "#2a1414", (P.sl !== P.sl0 ? "SL · trailed" : "SL") + " ⇅", [5, 4]);
    if (P.sl !== P.sl0) { const y = Y(P.sl0); g.strokeStyle = "#5a3333"; g.setLineDash([2, 4]); g.beginPath(); g.moveTo(0, y); g.lineTo(w - PAD, y); g.stroke(); g.setLineDash([]); g.fillStyle = "#8a6a6a"; g.fillText("first stop " + P.sl0.toFixed(d), 8, y - 8); }
  }
  let top = hm;
  const pane = (title, draw) => { g.fillStyle = "#0b0b0d"; g.fillRect(0, top, w, PH); g.strokeStyle = "#1d1d22"; g.beginPath(); g.moveTo(0, top + .5); g.lineTo(w, top + .5); g.stroke(); g.fillStyle = "#9C9EA6"; g.textAlign = "left"; g.fillText(title, 10, top + 12); draw(top + 6, PH - 12); top += PH; };
  if (S.ind.rsi) { const R = rsiArr(S.candles).slice(-view.length); pane("RSI 14", (t0, ph) => { const y = (v) => t0 + (1 - v / 100) * ph; [30, 70].forEach((lv) => { g.strokeStyle = "#2b2b30"; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(0, y(lv)); g.lineTo(w - PAD, y(lv)); g.stroke(); g.setLineDash([]); g.fillStyle = "#6c6e75"; g.fillText(lv, w - PAD + 6, y(lv)); }); g.strokeStyle = "#5AA9E6"; g.beginPath(); let st = false; R.forEach((v, i) => { if (v == null) return; st ? g.lineTo(X(i), y(v)) : g.moveTo(X(i), y(v)); st = true; }); g.stroke(); const lv = R[R.length - 1]; if (lv != null) { g.fillStyle = "#5AA9E6"; g.fillText(lv.toFixed(1), w - PAD + 6, y(lv)); } }); }
  if (S.ind.macd) { const M = macdArr(S.candles).slice(-view.length), mx = Math.max(1e-12, ...M.map((m) => Math.abs(m[2])), ...M.map((m) => Math.abs(m[0]))); pane("MACD 12,26,9", (t0, ph) => { const y = (v) => t0 + ph / 2 - (v / mx) * (ph / 2); g.strokeStyle = "#2b2b30"; g.beginPath(); g.moveTo(0, y(0)); g.lineTo(w - PAD, y(0)); g.stroke(); M.forEach((m, i) => { g.fillStyle = m[2] >= 0 ? "rgba(61,196,143,.7)" : "rgba(240,109,109,.7)"; g.fillRect(X(i) - 2, Math.min(y(0), y(m[2])), 4, Math.max(1, Math.abs(y(m[2]) - y(0)))); }); [[0, "#F58A14"], [1, "#B48CF2"]].forEach(([k, col]) => { g.strokeStyle = col; g.beginPath(); M.forEach((m, i) => (i ? g.lineTo(X(i), y(m[k])) : g.moveTo(X(i), y(m[k])))); g.stroke(); }); }); }
  S.geo.x0 = x0; S.geo.slot = slot; S.geo.n = view.length; S.geo.view = view;
  const last = view[view.length - 1][4], yl = Y(last);
  g.strokeStyle = "#F58A14"; g.beginPath(); g.moveTo(0, yl); g.lineTo(w - PAD, yl); g.stroke();
  g.fillStyle = "#F58A14"; g.fillRect(w - PAD, yl - 10, PAD - 2, 20); g.fillStyle = "#16181B"; g.font = "600 12px 'JetBrains Mono',monospace"; g.fillText(last.toFixed(d), w - PAD + 5, yl);
  const X0 = S.cross;
  if (X0 && !S.drag && X0.x < w - PAD) {
    const k = Math.floor((X0.x - x0) / slot), c = view[k];
    g.strokeStyle = "#6c6e75"; g.setLineDash([2, 3]); g.beginPath(); g.moveTo(X0.x, 0); g.lineTo(X0.x, h); if (X0.y < hm) { g.moveTo(0, X0.y); g.lineTo(w - PAD, X0.y); } g.stroke(); g.setLineDash([]);
    if (X0.y < hm) { const pv = lo + (1 - (X0.y - 8) / (hm - 20)) * (hi - lo); g.fillStyle = "#26262b"; g.fillRect(w - PAD, X0.y - 9, PAD - 2, 18); g.fillStyle = "#EDEDEE"; g.font = "11px 'JetBrains Mono',monospace"; g.textAlign = "left"; g.fillText(pv.toFixed(d), w - PAD + 5, X0.y); }
    if (c) { const t = new Date(c[0] * 1000), txt = `${t.toLocaleDateString([], { month: "short", day: "numeric" })} ${t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}  O ${c[1].toFixed(d)}  H ${c[2].toFixed(d)}  L ${c[3].toFixed(d)}  C ${c[4].toFixed(d)}`; g.font = "11px 'JetBrains Mono',monospace"; const tw = g.measureText(txt).width + 14; g.fillStyle = "rgba(14,14,17,.92)"; g.fillRect(8, hm - 28, tw, 20); g.fillStyle = c[4] >= c[1] ? "#3DC48F" : "#F06D6D"; g.textAlign = "left"; g.fillText(txt, 14, hm - 18); }
  }
}
window.addEventListener("resize", queueDraw);
document.addEventListener("pointerout", (e) => { if (e.target && e.target.id === "cv" && S.cross) { S.cross = null; queueDraw(); } });
/* drag the stop or target line on the chart to move it; on release the new level is sent to the broker */
const gy = (v) => 8 + (1 - (v - S.geo.lo) / (S.geo.hi - S.geo.lo)) * (S.geo.h - 20);
const gp = (y) => Math.min(S.geo.hi, Math.max(S.geo.lo, S.geo.lo + (1 - (y - 8) / (S.geo.h - 20)) * (S.geo.hi - S.geo.lo)));
const hitLine = (e) => { const P = openPos(S.sym); if (!P || !S.geo) return null; const y = e.clientY - e.target.getBoundingClientRect().top; return Math.abs(gy(P.sl) - y) <= 8 ? "sl" : Math.abs(gy(P.tp) - y) <= 8 ? "tp" : null; };
document.addEventListener("pointerdown", (e) => {
  if (!e.target || e.target.id !== "cv") return;
  const k = hitLine(e), P = openPos(S.sym); if (!k) return;
  S.drag = { k, v: P[k], lo: S.geo.lo, hi: S.geo.hi, ticket: P.ticket, venue: P.venue, y0: e.clientY, moved: false };
  e.target.setPointerCapture(e.pointerId); e.preventDefault();
});
document.addEventListener("pointermove", (e) => {
  if (!e.target || e.target.id !== "cv") return;
  if (!S.drag) { const r = e.target.getBoundingClientRect(); S.cross = { x: e.clientX - r.left, y: e.clientY - r.top }; e.target.style.cursor = hitLine(e) ? "ns-resize" : "crosshair"; queueDraw(); return; }
  if (Math.abs(e.clientY - S.drag.y0) > 3) S.drag.moved = true;
  S.drag.v = gp(e.clientY - e.target.getBoundingClientRect().top); queueDraw();
});
document.addEventListener("pointerup", async (e) => {
  const dr = S.drag; if (!dr) return; S.drag = null;
  const P = openPos(S.sym); if (!dr.moved || !P || P.ticket !== dr.ticket) { queueDraw(); return; }
  const v = +dr.v.toFixed(dig(P.symbol)), sl = dr.k === "sl" ? v : P.sl, tp = dr.k === "tp" ? v : P.tp;
  if (!confirm(`Move the ${dr.k === "sl" ? "STOP" : "TARGET"} of ${P.symbol} ${P.side} to ${v}?` + (dr.venue === "live" ? "\n\nLIVE account." : ""))) { queueDraw(); return; }
  try { await api(`/api/position/${dr.venue}/${dr.ticket}/levels`, "POST", { sl, tp }); toast("Stop and target moved"); } catch (err) { toast(err.message, true); }
  render();
});

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
/* win-chance as a range (95% interval from the unseen trades), with the win rate that breaks even at this target */
function wilson(win, n) {
  if (!n) return [0, 0];
  const p = win / 100, z = 1.96, d = 1 + (z * z) / n, c = (p + (z * z) / (2 * n)) / d, h = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / d;
  return [Math.max(0, c - h) * 100, Math.min(1, c + h) * 100];
}
function rangeBar(t, rr) {
  const [lo, hi] = wilson(t.win, t.n), be = 100 / (1 + rr), thin = lo < be;
  return `<div class="rng" role="img" aria-label="Win chance ${num(lo, 0)} to ${num(hi, 0)} percent, break-even ${num(be, 0)} percent"><div class="rt"><i class="rr" style="left:${lo}%;width:${Math.max(1, hi - lo)}%"></i><i class="rbe" style="left:${be}%"></i><i class="rw" style="left:${t.win}%"></i></div>
    <div class="rl"><span class="mono">${num(lo, 0)}%</span><span class="mono">${num(t.win, 1)}% wins</span><span class="mono">${num(hi, 0)}%</span></div></div>
    <div class="mute" style="font-size:12px;line-height:1.5">Break-even needs ${num(be, 0)}% at ${rr}:1 (before spread). ${thin ? "The low end of the range sits below it, so this setup is thin: size it small." : "Even the low end clears it."}</div>`;
}
function decisionCard(s) {
  const pend = S.appr.find((a) => a.venue === S.venue && a.symbol === S.sym);
  const bc = s.bias === "BUY" ? "ok" : s.bias === "SELL" ? "bad" : "mute";
  return `<section class="card dec"><div class="hd"><h2>Decision · ${esc(S.sym)}</h2><span class="mono mute">${hm(s.ts)}</span></div>
    <div class="pad"><div class="dline"><b class="big3 ${bc}">${esc(s.bias || "NONE")}</b>${s.lots != null ? `<span class="mono">${num(s.lots, 2)} lots</span>` : ""}${stPill(s.status)}</div>
    <p>${esc(s.reason || "")}</p>${pend ? `<div class="dact"><button class="btn pri" onclick="act('/api/approve/${pend.id}',{},'Approved')">Approve</button><button class="btn" onclick="act('/api/reject/${pend.id}',{},'Skipped')">Skip</button></div>` : ""}</div></section>`;
}
function trustCard() {
  const x = (S.st.strategies || {})[S.sym], f = S.st.bt_finished;
  if (!x) return `<section class="card"><div class="hd"><h2>Why you can trust it</h2><span class="pill ${S.st.proven_only ? "none" : "warn"}">no proof</span></div><div class="pad mute">${esc(S.sym)} has no setup that earned on unseen data. ${S.st.proven_only ? "Proven only is ON, so it will not trade." : "It trades on the score rules and is marked UNPROVEN."} <a href="#/strategies">Open the Strategy Lab</a></div></section>`;
  const t = x.test;
  return `<section class="card trust"><div class="hd"><h2>Why you can trust it · unseen data</h2><span class="pill buy">proven</span></div><div class="pad">
    <div class="tstat"><div><b class="mono ${t.exp >= 0 ? "ok" : "bad"}">${signed(t.exp, 2)}R</b><i>per trade</i></div><div><b class="mono">${t.n}</b><i>trades</i></div><div><b class="mono">${num(t.pf, 2)}</b><i>profit factor</i></div></div>
    ${rangeBar(t, x.rr)}
    <div class="mute" style="font-size:11px;margin-top:8px">${esc(x.strategy)} · ${tfName(x.tf)} · luck score t = ${num(t.t, 2)}${f ? " · re-tested " + hm(f) : ""} · <a href="#/strategies">open in Strategy Lab</a></div></div></section>`;
}
function renderWhy() {
  const el = $("why"); if (!el) return;
  const st = S.st, x = (st.strategies || {})[S.sym], c = S.an[S.venue + ":" + S.sym], d = c && c.d, s = S.det, p = (S.px[S.venue] || {})[S.sym];
  const ev = (st.events || []).filter((e) => e.impact === "High" && e.minutes > -30)[0];
  const card = (l, v, sub, cls) => `<div class="wy"><span>${l}</span><b class="${cls || ""}">${v}</b><i>${sub}</i></div>`;
  el.innerHTML = card("SETUP", x ? esc(x.strategy) : "Score rules", x ? `${tfName(x.tf)} · stop ${x.stop_k} ATR · target ${x.rr}×` : (st.proven_only ? "unproven: no trade while Proven only is ON" : "unproven: trades on the score"), x ? "ok" : "")
    + card("REGIME", d ? esc(d.ind.htf || "no higher-timeframe read") : "reading…", d ? `RSI ${d.ind.rsi.toFixed(0)} · ATR ${pxs(d.ind.atr, S.sym)}` : "")
    + card("NEWS", ev ? esc(ev.title) : "Clear", ev ? `${esc(ev.currency)} in ${ev.minutes} min` : "no high-impact event in 36 h", ev && ev.minutes <= 60 ? "warn" : ev ? "" : "ok")
    + card("SIZE", s && s.lots != null ? num(s.lots, 2) + " lots" : "no order", `risk ${st.risk.risk_per_trade_pct}% per trade${p ? " · spread " + pxs(p[1] - p[0], S.sym) : ""}`);
}
async function renderSide() { await renderSideInner(); renderWhy(); }
async function renderSideInner() {
  const el = $("side"); if (!el) return;
  S.an = S.an || {}; loadAn();
  const top = watchCard();
  const r = latestFor(S.sym);
  if (!r) { el.innerHTML = top + trustCard() + `<section class="card"><div class="hd"><h2>Decision · ${esc(S.sym)}</h2></div><div class="pad mute">No cycle has analysed this symbol yet.</div></section>` + indCard(); return; }
  const vs = (r.venues || []).find((x) => x.venue === S.venue), id = vs ? vs.id : r.id;
  if (id !== S.detId) { try { S.det = await api("/api/signal/" + id); S.detId = id; } catch (e) { S.det = null; } }
  const s = S.det; if (!s || !$("side")) { if ($("side")) $("side").innerHTML = top + trustCard() + indCard(); return; }
  const c = s.claude || {}, g = s.gate || {}, l = s.levels || {}, th = S.st.thresholds, ck = g.checks || [];
  $("side").innerHTML = top + decisionCard(s) + trustCard() + `<section class="card"><div class="hd"><h2>Risk gate · code</h2><span class="pill ${g.ok ? "buy" : g.checks ? "warn" : "none"}">${ck.length ? ck.filter((k) => k.ok).length + " of " + ck.length : "not run"}</span></div>
      ${ck.map((k) => `<div class="row sm"><span><i class="mk ${k.ok ? "g" : "r"}">${k.ok ? "✓" : "✕"}</i>${esc(k.name)}</span><span class="mono mute" style="text-align:right">${esc(k.detail)}</span></div>`).join("") || '<div class="pad mute">The gate has not run for this signal.</div>'}</section>
    <section class="card claude"><div class="hd"><h2>Claude · final say</h2><span class="pill ${c.decision === "YES" ? "buy" : "none"}">${esc(c.decision || "not asked")}${c.confidence != null ? " " + num(c.confidence, 2) : ""}</span></div>
      ${c.thesis || c.against ? `<div class="pad"><p>${esc(c.thesis)}</p>${c.against ? `<div class="lab">Against</div><p>${esc(c.against)}</p>` : ""}${c.main_risk ? `<div class="lab">Main risk</div><p>${esc(c.main_risk)}</p>` : ""}</div>` : `<div class="pad mute">${esc(c.error || s.reason || "Claude was not asked.")}</div>`}</section>
    <section class="card"><div class="hd"><h2>Rules score ${biasPill(s.bias)}</h2><b class="mono big2 ${s.score >= 0 ? "ok" : "bad"}">${signed(s.score, 1)}</b></div>
      <div class="pad"><div class="mute" style="font-size:12px;margin:0 0 8px">${s.agree}/${s.available} factors agree · needs ${th.agree_pct || 0}% and ±${th.score}</div>
      ${Object.entries(s.factors || {}).map(([k, f]) => `<div class="frow"><span style="text-transform:capitalize">${esc(k)}</span>${f.score == null ? '<span class="mute" style="font-size:12px">n/a</span>' : scoreBar(f.score)}<span class="mono ${f.score > 0 ? "ok" : f.score < 0 ? "bad" : "mute"}">${f.score == null ? "–" : signed(f.score, 0)}</span></div>`).join("")}</div></section>
    <section class="card"><div class="hd"><h2>Order</h2><a href="#/signal/${s.id}">full detail</a></div>
      <div class="row sm"><span>Entry</span><span class="mono">${esc(l.entry)}</span></div><div class="row sm"><span>Stop</span><span class="mono bad">${esc(l.sl)}</span></div>
      <div class="row sm"><span>Target</span><span class="mono ok">${esc(l.tp)}</span></div><div class="row sm"><span>R:R · lots</span><span class="mono">${esc(l.rr)} · ${s.lots != null ? num(s.lots, 2) : "–"}</span></div></section>` + indCard();
}

/* bottom tabs */
window.saveLv = (venue, ticket) => {
  const sl = Number($("sl-" + ticket).value), tp = Number($("tp-" + ticket).value);
  if (!sl || !tp) { toast("Enter a stop and a target", true); return; }
  S.edit = 0; act(`/api/position/${venue}/${ticket}/levels`, { sl, tp }, "Stop and target moved");
};
function renderBottom() {
  if (S.edit && document.activeElement && document.activeElement.classList.contains("lvin")) return;   // do not redraw under a box being edited
  const mine = S.posd.open.filter((p) => p.venue === S.venue), pend = S.appr.filter((a) => a.venue === S.venue);
  const tabs = [["pos", `Positions (${mine.length})`], ["pend", `Pending approval (${pend.length})`], ["hist", "History"], ["log", "Journal"]];
  $("tabs").innerHTML = tabs.map(([k, l]) => `<button class="${S.tab === k ? "on" : ""}" onclick="pickTab('${k}')">${l}</button>`).join("") + `<span class="mute tnote">positions re-priced on every tick · edit the stop or target boxes, or drag the lines on the chart</span>`;
  let h = "";
  if (S.tab === "pos") {
    h = `<div class="tw"><div class="ptable"><div class="prow head"><span>SYMBOL</span><span>SIDE</span><span>LOTS</span><span>ENTRY</span><span>LIVE</span><span>STOP</span><span>TARGET</span><span>STOP ◂ PRICE ▸ TARGET</span><span class="r">P/L</span><span class="r">R</span><span></span></div>
      ${mine.map((p) => `<div class="prow" data-k="${p.venue}:${p.ticket}" data-e="${p.entry}" data-sl="${p.sl}" data-tp="${p.tp}" data-side="${p.side}">
        <span><a href="#/" class="symlink" title="Open ${esc(p.symbol)} chart" onclick="openTrade('${p.venue}','${esc(p.symbol)}',${p.tf || 0});return false"><b>${esc(p.symbol)}</b>${stratBadge(p.symbol)}${p.tf ? ` <i class="mute tfm">${p.tf >= 60 ? p.tf / 60 + "h" : p.tf + "m"}</i>` : ""}</a> ${p.sl !== p.sl0 ? '<i class="trail">TRAILING</i>' : ""}${p.watch ? `<i class="trail ${p.watch.status === "reversing" || p.watch.status === "reversal" ? "rev" : ""}" title="Re-scored every 30 s on all 7 factors">${p.watch.status === "reversal" ? "REVERSAL" : p.watch.status === "reversing" ? "TURNING" : "WATCH"} ${signed(p.watch.signed, 0)}</i>` : ""}</span><span class="${p.side === "BUY" ? "ok" : "bad"}"><b>${esc(p.side)}</b></span><span class="mono">${num(p.lots, 2)}</span>
        <span class="mono mute">${pxs(p.entry, p.symbol)}</span><span class="mono lv"><b>${pxs(p.last_price, p.symbol)}</b></span><span><input class="lvin bad" id="sl-${p.ticket}" type="number" step="any" value="${p.sl}" aria-label="Stop loss for ${esc(p.symbol)}" onfocus="S.edit=1" onblur="S.edit=0" onchange="saveLv('${p.venue}',${p.ticket})" onkeydown="if(event.key==='Enter')this.blur()"></span><span><input class="lvin ok" id="tp-${p.ticket}" type="number" step="any" value="${p.tp}" aria-label="Target for ${esc(p.symbol)}" onfocus="S.edit=1" onblur="S.edit=0" onchange="saveLv('${p.venue}',${p.ticket})" onkeydown="if(event.key==='Enter')this.blur()"></span>
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
    const pr = row.querySelector(".pr"); const pp = ((L.live - (+row.dataset.e)) * (row.dataset.side === "BUY" ? 1 : -1)) / Math.pow(10, -(dig(sym) - 1));
    pr.textContent = (L.r >= 0 ? "+" : "") + L.r.toFixed(2) + "R · " + (pp >= 0 ? "+" : "") + pp.toFixed(1) + " pips"; pr.className = "mono r pr " + (L.r < 0 ? "bad" : "ok");
    const e = +row.dataset.e, sl = +row.dataset.sl, tp = +row.dataset.tp, a = Math.min(sl, tp), b = Math.max(sl, tp), pc = (v) => Math.min(100, Math.max(0, ((v - a) / (b - a)) * 100));
    const pe = pc(e), pn = pc(L.live), f = row.querySelector(".pf"), n = row.querySelector(".pn");
    f.style.left = Math.min(pe, pn) + "%"; f.style.width = Math.abs(pn - pe) + "%"; f.style.background = (row.dataset.side === "BUY" ? L.live >= e : L.live <= e) ? "var(--buy)" : "var(--sell)"; n.style.left = "calc(" + pn + "% - 1px)";
  });
}


/* ------------------------------------------------------------ manual order ticket */
let T = null;
window.openTicket = (side) => { T = { side, sym: S.sym, venue: S.venue, busy: true, err: "", live: S.venue === "live" }; renderTicket(); loadTicket(); };
window.closeTicket = () => { T = null; const el = $("tkt"); if (el) el.remove(); };
async function loadTicket() {
  if (!T) return; const mine = T; mine.busy = true; mine.err = ""; renderTicket();
  try { const d = await api(`/api/trade/ticket?venue=${mine.venue}&symbol=${mine.sym}&side=${mine.side}`); if (T !== mine) return; Object.assign(mine, d, { lotsIn: d.lots, slIn: d.sl, tpIn: d.tp }); }
  catch (e) { if (T === mine) mine.err = e.message; }
  mine.busy = false; renderTicket();
}
window.tkSide = (side) => { if (!T || T.side === side) return; T.side = side; loadTicket(); };
window.tkSym = (sym) => { if (!T) return; T.sym = sym; loadTicket(); };
window.tkAuto = () => { if (!T) return; T.lotsIn = T.lots; T.slIn = T.sl; T.tpIn = T.tp; renderTicket(); };
function tkVals() { const g = (id) => Number(($(id) || {}).value); return { lots: g("tk-lots"), sl: g("tk-sl"), tp: g("tk-tp") }; }
function tkCalc() {
  if (!T || T.busy || !T.pv) return;
  const px = (S.px[T.venue] || {})[T.sym], buy = T.side === "BUY", entry = px ? (buy ? px[1] : px[0]) : (buy ? T.ask : T.bid), v = tkVals();
  const stop = Math.abs(entry - v.sl), risk = v.lots * stop * T.pv, rr = stop && v.tp ? Math.abs(v.tp - entry) / stop : 0, pct = T.balance ? (risk / T.balance) * 100 : 0;
  const okSide = buy ? v.sl < entry && (!v.tp || v.tp > entry) : v.sl > entry && (!v.tp || v.tp < entry), over = pct > T.day_cap_pct;
  const f = (n, d) => (isFinite(n) ? n.toFixed(d) : "–");
  $("tk-entry").textContent = f(entry, T.digits);
  $("tk-calc").innerHTML = `<span>Risk <b class="mono ${over ? "bad" : ""}">${money(risk)} (${f(pct, 2)}%)</b></span><span>Reward : risk <b class="mono">${f(rr, 2)}</b></span><span>Stop <b class="mono">${f(stop, T.digits)}</b></span>`;
  $("tk-warn").textContent = !okSide ? "Stop and target must be on opposite sides of the price: stop " + (buy ? "below" : "above") + ", target " + (buy ? "above" : "below") + "." : over ? "This risks more than the daily loss limit (" + T.day_cap_pct + "%). The order will be refused." : v.lots < T.min || v.lots > T.max ? "Lots must be between " + T.min + " and " + T.max + "." : "";
  $("tk-go").disabled = !!$("tk-warn").textContent || T.sending;
}
function renderTicket() {
  if (!T) return;
  let el = $("tkt"); if (!el) { el = document.createElement("div"); el.id = "tkt"; el.className = "tk-ov"; el.onclick = (e) => { if (e.target === el) closeTicket(); }; document.body.appendChild(el); }
  const buy = T.side === "BUY", dg = T.digits || 5, st = S.st;
  el.innerHTML = `<div class="tk" role="dialog" aria-modal="true" aria-label="New trade"><div class="tk-h"><b>New trade</b><span class="pill ${T.venue === "live" ? "warn" : "acc"}">${esc(VLABEL[T.venue])}</span><button class="btn sm" onclick="closeTicket()" aria-label="Close">✕</button></div>
    <div class="tk-b"><label>Instrument<select id="tk-sym" onchange="tkSym(this.value)">${st.symbols.map((x) => `<option ${x === T.sym ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></label>
    <div class="tk-side"><button class="bs buy ${buy ? "on" : ""}" onclick="tkSide('BUY')">BUY</button><button class="bs sell ${buy ? "" : "on"}" onclick="tkSide('SELL')">SELL</button></div>
    ${T.err ? `<div class="banner warn" style="margin:8px 0"><span>${esc(T.err)}</span></div>` : ""}
    ${T.busy ? '<div class="mute" style="padding:18px 0">Reading the market…</div>' : T.pv ? `
    <div class="tk-px">Fills at <b class="mono" id="tk-entry">–</b> <span class="mute">· ${esc(T.tf ? tfName(T.tf) : "")} chart · ATR ${esc(T.atr)}</span></div>
    <div class="tk-g"><label>Lots<input id="tk-lots" type="number" step="${T.step}" min="${T.min}" max="${T.max}" value="${T.lotsIn}" oninput="tkCalc()"></label>
      <label>Stop loss<input id="tk-sl" type="number" step="any" value="${T.slIn}" oninput="tkCalc()"></label>
      <label>Target<input id="tk-tp" type="number" step="any" value="${T.tpIn}" oninput="tkCalc()"></label></div>
    <div class="tk-calc" id="tk-calc"></div><div class="tk-warn" id="tk-warn" role="alert"></div>
    <div class="mute" style="font-size:12px">Start values come from the current chart and your risk per trade (${T.risk_pct}%). Change any of them. A manual trade skips the score and Claude, but not the halt switches, the Live lock or the daily loss limit. Trailing stop and reversal watch manage it afterwards like any other trade.</div>
    ${T.venue === "live" ? '<label class="tk-chk"><input id="tk-live" type="checkbox" onchange="tkCalc()"> This is a REAL-money order on the Live account</label>' : ""}` : ""}</div>
    <div class="tk-f"><button class="btn sm" onclick="tkAuto()" ${T.busy || !T.pv ? "disabled" : ""}>Reset to suggested</button><button class="btn pri" id="tk-go" onclick="tkSend()" ${T.busy || !T.pv ? "disabled" : ""}>${buy ? "Buy" : "Sell"} ${esc(T.sym)}</button></div></div>`;
  tkCalc();
}
const tfName = (m) => (m >= 60 ? m / 60 + "h" : m + "m");
window.tkSend = async () => {
  if (!T || T.sending) return; const v = tkVals(), live = T.venue === "live";
  if (live && !($("tk-live") || {}).checked) { $("tk-warn").textContent = "Tick the box to confirm a real-money order."; return; }
  if (!confirm(`${T.side} ${v.lots} lots ${T.sym} on ${VLABEL[T.venue]}\nstop ${v.sl}, target ${v.tp || "none"}${live ? "\n\nREAL MONEY." : ""}`)) return;
  T.sending = true; $("tk-go").disabled = true;
  try { const r = await api("/api/trade", "POST", { venue: T.venue, symbol: T.sym, side: T.side, lots: v.lots, sl: v.sl, tp: v.tp, confirm_live: live }); toast(`Opened ${T.sym} ${T.side} ${r.lots} lots at ${r.price}`); closeTicket(); render(); }
  catch (e) { if (T) { T.sending = false; $("tk-warn").textContent = e.message; $("tk-go").disabled = false; } }
};
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && T) closeTicket(); });

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
    <section class="card sec" id="s-venues"><h2>Trading venues</h2><p class="mute">Two venues, Demo and Live, run side by side, each on its own candle size. Every switch takes effect on the next cycle.</p><div class="vgrid">${V.map((v) => `<div class="vcard"><div class="vh"><b>${esc(v.label)}</b><span class="pill ${v.name === "live" ? "warn" : v.connected === false ? "red" : "none"}">${v.locked ? "LOCKED" : v.connected === false ? "NOT CONNECTED" : v.account && v.account.demo ? "DEMO" : "CONNECTED"}</span></div>
      <div class="mute" style="font-size:12px">${esc(VDESC[v.name])}</div>
      <div class="krow"><span>Trade</span>${v.locked ? `<button class="btn sm" onclick="if(confirm('Unlock LIVE trading? Real money on a real account. Live stays OFF until you switch it on.'))act('/api/allow-live',{on:true},'Live unlocked')">Unlock</button>` : swBtn(v.on, v.label + " venue", "/api/venue/" + v.name, `{on:${!v.on}}`, v.name === "live" && !v.on ? "Switch LIVE trading on? Real money on a real account." : "")}</div>
      <div class="krow"><span>Auto-approve</span>${v.locked ? "" : swBtn(v.auto, v.label + " auto-approve", "/api/venue/" + v.name, `{auto:${!v.auto}}`, v.name === "live" && !v.auto ? "Auto-approve LIVE trades? Orders will go out without your click." : "")}</div>
      <div class="krow"><span>Chart candles</span><select aria-label="${esc(v.label)} candle size" onchange="act('/api/venue/${v.name}',{tf:Number(this.value)},'${esc(v.label)} candles set')">${st.timeframes.map((m) => `<option value="${m}" ${m === v.tf ? "selected" : ""}>${m === 60 ? "1 hour" : m + " min"}</option>`).join("")}</select></div>
      ${v.connected === false ? `<a class="btn sm" href="/ctrader/connect">Connect cTrader</a>` : ""}</div>`).join("")}</div></section>
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
      <button class="btn" onclick="if(confirm('Drop every value you set by hand and return to the profile values?'))act('/api/tune',{reset:true},'All rules back to the profile')">Reset all rules to profile…</button></div></section>
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
  let html = pageHead("Trading reports", "Demo · Live, closed trades after commission and swap", `<span class="seg" style="padding:0">${[[0, "All time"], [1, "Today"], [7, "7 days"], [30, "30 days"]].map(([d, l]) => `<button aria-pressed="${repDays === d}" onclick="repDays=${d};render()">${l}</button>`).join("")}</span>`);
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
let specVenue = "demo";
async function specs() {
  curPath = "/api/specs?venue=" + specVenue;
  const r = await cget(curPath);
  let html = pageHead("Contract specifications", "Straight from the cTrader account. Read-only: nothing here can be edited", `<span class="seg" style="padding:0">${["demo", "live"].map((v) => `<button aria-pressed="${specVenue === v}" onclick="specVenue='${v}';render()">${VLABEL[v]}</button>`).join("")}</span>`);
  html += `<div class="note" style="margin:0"><span class="pill acc">READ-ONLY</span> ${r.account ? `Account <b class="mono">${esc(r.account.login)}</b> on <b>${esc(r.account.server)}</b> · ` : ""}contract size, digits, lot limits, swaps, commission and the live spread are the broker's own values${r.fetched ? ", read " + hm(r.fetched) : ""}. Lots, costs and stop distances in the app are sized from these numbers, so they cannot be changed here.</div>`;
  if (r.error) html += `<div class="banner warn"><strong>${esc(VLABEL[specVenue])} not reachable.</strong><span class="mute">${esc(r.error)}</span></div>`;
  const dash = (v) => (v == null || v === "" ? "–" : esc(v));
  const swap = (v) => (v == null ? "–" : num(v, 2));
  html += `<section class="card"><div class="tw"><table style="min-width:1240px"><thead><tr><th>Symbol</th><th>Digits</th><th>Units per lot</th><th>Lot min / step / max</th><th>P/L in</th><th>Spread</th><th>Points</th><th>Cost per lot</th><th>Swap long / short</th><th>Commission (as reported)</th><th>Min stop dist.</th><th>Bid / Ask</th></tr></thead><tbody>`;
  for (const x of r.rows) {
    if (!x.ok) { html += `<tr><td><strong>${esc(x.symbol)}</strong></td><td colspan="11" class="mute">${esc(x.error || "no data")}</td></tr>`; continue; }
    html += `<tr><td><strong>${esc(x.symbol)}</strong> <span class="mute mono" style="font-size:11px">${esc(x.broker_symbol)}</span></td><td class="mono">${x.digits}</td><td class="mono">${num(x.contract, 0)}</td>
      <td class="mono">${x.volume_min} / ${x.volume_step} / ${x.volume_max}</td><td class="mono">${dash(x.profit_ccy)}</td><td class="mono">${esc(x.spread)}</td><td class="mono">${x.spread_points ?? "–"}</td>
      <td class="mono">${num(x.cost_per_lot, 2)} ${esc(x.profit_ccy)}</td><td class="mono">${swap(x.swap_long)} / ${swap(x.swap_short)}${x.swap_3days != null ? ` <i class="mute" style="font-size:10px;font-style:normal">3-day: ${esc(x.swap_3days)}</i>` : ""}</td>
      <td class="mono">${x.commission ? esc(x.commission) + (x.commission_type != null ? ` <i class="mute" style="font-size:10px;font-style:normal">type ${esc(x.commission_type)}</i>` : "") : "none"}</td><td class="mono">${dash(x.min_stop_distance)}</td><td class="mono mute">${esc(x.bid)} / ${esc(x.ask)}</td></tr>`;
  }
  html += `</tbody></table></div></section>`;
  $app.innerHTML = html;
}


/* ------------------------------------------------------------ signal matrix */
let MX = { sort: "edge", filter: "all" };
const FACT_LBL = { trend: "Trend", momentum: "Momentum", structure: "Structure", macro: "Macro", sentiment: "News", positioning: "COT", calendar: "Events", volatility: "Volatility", technical: "Technical", news: "News", cot: "COT" };
window.mxSet = (k, v) => { MX[k] = v; render(); };
window.mxTrade = (sym, side) => { S.sym = sym; localStorage.setItem("fs", sym); S.candleKey = ""; openTicket(side); };
async function matrixPage() {
  const d = await api("/api/matrix"), st = S.st, th = d.threshold || 20;
  let rows = d.rows.slice();
  const keys = []; rows.forEach((r) => Object.keys(r.factors || {}).forEach((k) => { if (!keys.includes(k)) keys.push(k); }));
  const sa = (r) => Math.abs(r.score || 0);
  rows.sort((a, b) => MX.sort === "name" ? a.symbol.localeCompare(b.symbol) : (b.proven ? 1 : 0) - (a.proven ? 1 : 0) || sa(b) - sa(a));
  if (MX.filter === "proven") rows = rows.filter((r) => r.proven); else if (MX.filter === "signal") rows = rows.filter((r) => sa(r) >= th);
  const heat = (v) => v == null ? "#1c1c20" : Math.abs(v) < 8 ? "#26262b" : v > 0 ? `rgba(42,143,102,${0.3 + Math.min(1, v / 80) * 0.7})` : `rgba(168,67,63,${0.3 + Math.min(1, -v / 80) * 0.7})`;
  const seg = (k, opts) => `<span class="seg" style="padding:0">${opts.map(([v, l]) => `<button aria-pressed="${MX[k] === v}" onclick="mxSet('${k}','${v}')">${l}</button>`).join("")}</span>`;
  let h = pageHead("Signal Matrix", `Every symbol against every factor, from the latest analysis. Green leans buy, red leans sell. Needs ±${th} and ${d.agree_pct || 0}% of factors agreeing.`,
    seg("filter", [["all", "All"], ["signal", "Signals only"], ["proven", "Proven only"]]) + seg("sort", [["edge", "By edge"], ["name", "A–Z"]]));
  h += `<section class="card mxc"><div class="tw"><table class="mx"><thead><tr><th>Symbol</th><th>Price</th><th>Verdict</th><th class="r">Score</th><th>Agree</th>${keys.map((k) => `<th class="c">${esc(FACT_LBL[k] || k)}</th>`).join("")}<th>Proof</th><th>Status</th><th class="r">Trade</th></tr></thead><tbody>`;
  for (const r of rows) {
    const px = ((S.px[S.venue] || {})[r.symbol]) || null, sc = r.score, strong = sc != null && Math.abs(sc) >= th;
    const why = r.proven ? `${esc(r.proven.strategy)} ${tfName(r.proven.tf)}` : "";
    h += `<tr><td><a href="#/" onclick="pickSym('${r.symbol}');return true"><b>${esc(r.symbol)}</b></a>${r.timeframe ? ` <i class="mute" style="font-size:10px;font-style:normal">${esc(r.timeframe)}</i>` : ""}</td>
        <td class="mono">${px ? pxs(px[0], r.symbol) : "–"}</td><td>${biasPill(strong ? r.bias : "NONE")}</td>
        <td class="mono r ${sc == null ? "mute" : sc > 0 ? "ok" : sc < 0 ? "bad" : "mute"}"><b>${sc == null ? "–" : signed(sc, 0)}</b></td><td class="mono mute">${r.available ? r.agree + "/" + r.available : "–"}</td>
        ${keys.map((k) => { const f = (r.factors || {})[k], v = f ? f.score : null; return `<td class="c"><span class="mxc2" style="background:${heat(v)}" title="${esc(f ? f.reading : "no data")}">${v == null ? "–" : signed(v, 0)}</span></td>`; }).join("")}
        <td>${r.proven ? `<span class="sb ok" style="margin:0" title="Unseen: ${r.proven.test.n} trades, ${num(r.proven.test.exp, 2)}R each, PF ${num(r.proven.test.pf, 2)}">PROVEN · ${why}</span>` : `<span class="sb un" style="margin:0">UNPROVEN</span>`}</td>
        <td>${r.status ? stPill(r.status) : '<span class="mute">–</span>'}</td>
        <td class="r"><span class="mxt"><button class="bs buy" onclick="mxTrade('${r.symbol}','BUY')">BUY</button><button class="bs sell" onclick="mxTrade('${r.symbol}','SELL')">SELL</button></span></td></tr>`;
  }
  h += `</tbody></table></div></section><div class="note" style="margin:0">A manual trade skips the score and Claude but not the halt switches, the Live lock or the daily loss limit. <b>PROVEN</b> means the setup earned on data it never saw in the backtest; <b>UNPROVEN</b> has no such evidence, so size it small. Hover a cell for the reading behind it. Prices are live from ${esc(VLABEL[S.venue])}.</div>`;
  $app.innerHTML = h;
}

/* ------------------------------------------------------------ strategy lab */
let btTimer = null, LAB = null;
window.labPick = (sym, tf) => { LAB = [sym, tf]; render(); };
async function strategiesPage() {
  const d = await api("/api/backtest"), st = S.st, rows = d.rows || [], R = d.rules || {}, run = d.state === "running", live = d.live_set || {};
  const by = {}; rows.forEach((r) => { (by[r.symbol] = by[r.symbol] || {})[r.tf] = r; });
  const tfs = R.timeframes || [1, 3, 5, 15, 30, 60], syms = st.symbols.filter((x) => by[x]).concat(Object.keys(by).filter((x) => !st.symbols.includes(x)));
  const nProven = rows.filter((r) => r.ok).length, days = rows.length ? Math.max(...rows.map((r) => r.days || 0)) : 0, nCt = rows.filter((r) => r.source === "ctrader").length;
  const fin = d.finished ? new Date(d.finished) : null, nextH = fin ? Math.round((fin.getTime() + 2 * 86400000 - Date.now()) / 3600000) : null;
  const sel = (LAB && by[LAB[0]] && by[LAB[0]][LAB[1]]) || rows.find((r) => r.ok) || rows[0];
  const tot = rows.length, kp = (l, v, sub, c) => `<div class="lkpi"><span>${l}</span><b class="mono ${c || ""}">${v}</b><i>${sub}</i></div>`;
  let h = pageHead("Strategy Lab", fin ? `Last full test ${hm(d.finished)} · next in ${nextH != null && nextH > 0 ? nextH + " h" : "due"} · ${R.n_strategies || 10} strategies × ${syms.length} symbols × ${tfs.length} candle sizes` : "No test has finished yet",
    `<button class="btn pri" ${run ? "disabled" : ""} onclick="act('/api/backtest/run',{},'Backtest started')">${run ? "Running " + d.done + "/" + d.total : d.state === "interrupted" ? "Restart backtest" : "Run backtest now"}</button>`);
  if (run && d.current) h += `<div class="note" style="margin:0">Now: <b class="mono">${esc(d.current)}</b> · ${esc(d.stage || "")} · ${d.done} of ${d.total} done${d.beat ? " · updated " + hm(d.beat) : ""}</div>`;
  if (d.state === "interrupted") h += `<div class="banner warn"><strong>The last run stopped at ${d.done} of ${d.total}.</strong><span class="mute">The server restarted or the worker died. It restarts by itself within 10 minutes, or press Restart backtest.</span></div>`;
  h += `<div class="lkpis">${kp("PROVEN SETUPS", nProven, "of " + tot + " tests", nProven ? "ok" : "")}${kp("STRATEGIES TESTED", R.n_strategies || 10, "plus the old score entries for comparison")}${kp("HISTORY", days + " days", nCt ? nCt + " of " + tot + " tests on cTrader candles" : "Yahoo candles")}${kp("STATE", esc(d.state), d.error ? esc(d.error) : (d.errors && d.errors.length ? d.errors.length + " tests skipped" : "re-tests every 2 days"), run ? "acc" : "")}</div>`;
  const cell = (r) => {
    if (!r) return `<div class="hc na">–</div>`;
    if (r.error) return `<div class="hc na" title="${esc(r.error)}">–</div>`;
    const v = r.best ? r.best.test.exp : null;
    if (v == null) return `<div class="hc na" title="nothing earned on the first 60%">–</div>`;
    const a = Math.min(1, Math.abs(v) / 0.25), bg = Math.abs(v) < 0.03 ? "#2a2a2f" : v > 0 ? `rgba(42,143,102,${0.35 + a * 0.65})` : `rgba(168,67,63,${0.3 + a * 0.7})`;
    const on = sel && sel.symbol === r.symbol && sel.tf === r.tf;
    return `<button class="hc ${r.ok ? "pv" : ""} ${on ? "sel" : ""}" style="background:${bg}" onclick="labPick('${r.symbol}',${r.tf})" title="${esc(r.symbol)} ${tfName(r.tf)}: ${esc(r.best.strategy)}">${signed(v, 2)}</button>`;
  };
  h += `<div class="lgrid"><section class="card lmap"><div class="lh"><b>Edge map · average R per trade on unseen data</b><span class="lleg"><i style="background:#a8433f"></i>loses<i style="background:#2a2a2f"></i>flat<i style="background:#2a8f66"></i>earns<i class="ring"></i>proven</span></div>
    <div class="hrow hhead"><span></span>${tfs.map((t) => `<span class="mono">${tfName(t)}</span>`).join("")}</div>
    ${syms.map((sy) => `<div class="hrow"><b>${esc(sy)}</b>${tfs.map((t) => cell((by[sy] || {})[t])).join("")}</div>`).join("") || '<p class="mute">No results yet. Press Run backtest now: it runs on the server and takes several minutes.</p>'}
    <p class="mute" style="font-size:11px;margin:10px 0 0">Each cell is the best setup chosen on the first 60% of ${R.history_days ? "about " + R.history_days + " days of" : ""} candles and judged on the last 40% it never saw. Spread is paid on every trade. Click a cell for its proof.</p></section>`;
  let side = "";
  if (sel && !sel.error) {
    const b = sel.best, t = b && b.test, need = sel.source === "ctrader" ? R.min_days_ctrader : (R.min_days_yahoo || {})[sel.tf] || 180, mt = sel.tf >= 60 ? R.min_t : R.min_t_fast;
    const ck = (ok, label, val) => `<div class="pk"><i class="mk ${ok ? "g" : "r"}">${ok ? "✓" : "✕"}</i><span>${label}</span><b class="mono">${val}</b></div>`;
    side += `<section class="card"><div class="lh"><b>${esc(sel.symbol)} · ${b ? esc(b.strategy) : "no setup"} · ${tfName(sel.tf)}</b><span class="pill ${sel.ok ? "buy" : "none"}">${sel.ok ? "PROVEN" : "NOT PROVEN"}</span></div>
      ${b ? `<div class="mute" style="font-size:12px">stop ${b.stop_k} ATR · target ${b.rr}× · ${b.session === "all" ? "all hours" : "London/NY only"} · ${esc(sel.source || "")} candles</div>
      <div class="lab" style="margin-top:10px">PROOF CHECKLIST</div>
      ${ck(sel.days >= need, `History covers ${need}+ days`, sel.days + " d")}${ck(t.n >= R.min_test, `Unseen trades (need ${R.min_test})`, t.n)}${ck(t.exp >= R.min_exp, `Average per trade (need ${R.min_exp}R)`, signed(t.exp, 2) + "R")}
      ${ck(t.pf >= R.min_pf, `Profit factor (need ${R.min_pf})`, num(t.pf, 2))}${ck((t.t || 0) >= mt, `Luck score t (need ${mt})`, num(t.t, 2))}${ck(sel.share_positive_test >= R.min_robust, `Neighbouring settings also earn (need ${Math.round(R.min_robust * 100)}%)`, Math.round(sel.share_positive_test * 100) + "%")}` : '<p class="mute">Nothing earned on the first 60% of the data, so there is nothing to prove.</p>'}</section>`;
    const rp = sel.report;
    if (b && rp && rp.curve) {
      const cv = [0].concat(rp.curve), lo = Math.min(...cv), hi = Math.max(...cv), rg = hi - lo || 1, W = 320, H = 110, pt = cv.map((v, i) => `${(i / (cv.length - 1)) * W},${H - 6 - ((v - lo) / rg) * (H - 12)}`).join(" "), z = H - 6 - ((0 - lo) / rg) * (H - 12);
      side += `<section class="card"><div class="lh"><b>Unseen-trade record</b></div>
        <div class="tstat"><div><b class="mono ${rp.net_r >= 0 ? "ok" : "bad"}">${signed(rp.net_r, 1)}R</b><i>net, ≈ ${signed(rp.net_r * (st.risk.risk_per_trade_pct || 0.5), 1)}% of equity</i></div><div><b class="mono bad">-${num(rp.max_dd_r, 1)}R</b><i>max drawdown</i></div><div><b class="mono">${num(rp.sharpe, 2)}</b><i>Sharpe (annualised)</i></div></div>
        <svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Equity curve of the unseen trades in R"><line x1="0" x2="${W}" y1="${z}" y2="${z}" stroke="#4A4A52" stroke-dasharray="4 4"/><polyline points="${pt}" fill="none" stroke="${rp.net_r >= 0 ? "#3DC48F" : "#F06D6D"}" stroke-width="1.8"/></svg>
        <details style="margin-top:8px"><summary class="mute" style="cursor:pointer;font-size:12px">Last ${rp.trades.length} trades</summary><div class="tlog">${rp.trades.slice().reverse().map(([t, dd, x]) => `<div class="mono"><span class="mute">${new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" })} ${new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span><span class="${dd > 0 ? "ok" : "bad"}">${dd > 0 ? "BUY" : "SELL"}</span><span class="${x >= 0 ? "ok" : "bad"}">${signed(x, 2)}R</span></div>`).join("")}</div></details></section>`;
    }
    if (b) side += `<section class="card"><div class="lh"><b>Win-chance range</b></div><div class="mute" style="font-size:11px;margin-bottom:8px">From ${t.n} unseen trades. A range, not one number.</div>${rangeBar(t, b.rr)}</section>`;
  }
  h += `<div class="lside">${side}</div></div>`;
  h += `<section class="card"><div class="pad"><div class="krow big"><div><b>Proven only</b><div class="mute" style="font-size:12px">On: only symbols with a proven setup trade; every other symbol stays out. Off: symbols with no proven setup still trade on the score rules, marked UNPROVEN.</div></div>${swBtn(d.proven_only, "Proven only", "/api/proven-only", `{on:${!d.proven_only}}`)}</div></div></section>`;
  h += `<section class="card"><div class="pad"><div class="krow big"><div><b>Use proven setups where they exist</b><div class="mute" style="font-size:12px">On: a symbol with a proven setup trades only that setup, on its candle size. Off: score rules only.</div></div>${swBtn(d.strategy_mode, "Strategy mode", "/api/strategy-mode", `{on:${!d.strategy_mode}}`, d.strategy_mode ? "Turn strategy mode OFF? The old score-only entries (which lost money) trade again." : "")}</div>
    <div class="note">Live now: <b class="acc">${Object.keys(live).length}</b> symbol(s) cleared to trade${Object.keys(live).length ? ": " + Object.entries(live).map(([k, v]) => `${esc(k)} ${esc(v.strategy)} ${tfName(v.tf)}`).join(", ") : ". Nothing is proven yet, so with Proven only ON nothing trades"}.</div>
    <p class="mute" style="font-size:12px;margin:8px 0 0">A backtest is evidence, not a promise: markets change, and testing many setups always turns up some that look good by luck. That is why a setup must also earn on the unseen 40%, with enough trades and a luck score above the bar.</p></div></section>`;
  h += `<details class="card ltab"><summary>All results (${rows.length})</summary><div class="tw"><table style="min-width:980px"><thead><tr><th>Symbol</th><th>Candles</th><th>Best setup (picked on the first 60%)</th><th>First 60%</th><th>Unseen 40%</th><th>Old entries, unseen</th><th class="r">Verdict</th></tr></thead><tbody>
    ${rows.slice().sort((a, b) => a.symbol.localeCompare(b.symbol) || a.tf - b.tf).map((r) => { const b = r.best, pf = (t) => (t ? `${t.n} trades · ${num(t.win, 0)}% win · ${t.exp >= 0 ? "+" : ""}${num(t.exp, 2)}R · PF ${num(t.pf, 2)}${t.t != null ? " · t " + num(t.t, 1) : ""}` : "–");
      return `<tr><td><b>${esc(r.symbol)}</b></td><td class="mono">${tfName(r.tf)}</td><td>${b ? `${esc(b.strategy)} · stop ${b.stop_k} ATR · target ${b.rr}× · ${b.session === "all" ? "all hours" : "London/NY"}` : '<span class="mute">nothing earned on the first 60%</span>'}</td>
      <td class="mono">${b ? pf(b.train) : "–"}</td><td class="mono ${b && b.test.exp > 0 ? "ok" : "bad"}">${b ? pf(b.test) : "–"}</td><td class="mono mute">${pf(r.baseline_test)}</td>
      <td class="r">${r.ok ? '<span class="pill buy">PROVEN</span>' : '<span class="pill none">not proven</span>'}</td></tr>`; }).join("")}</tbody></table></div>
    ${d.errors && d.errors.length ? `<div class="pad mute" style="font-size:12px">Skipped: ${d.errors.slice(0, 12).map(esc).join(" · ")}${d.errors.length > 12 ? " · …" : ""}</div>` : ""}</details>`;
  $app.innerHTML = h;
  clearTimeout(btTimer);
  if (run) btTimer = setTimeout(() => { if (pageName() === "strategies") render(); }, 4000);
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
    else if (pg === "matrix") { await ensureSt(); await matrixPage(); }
    else if (pg === "strategies") { await ensureSt(); await strategiesPage(); }
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
