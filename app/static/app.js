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

/* ------------------------------------------------------------ desk */
async function desk() {
  const [st, latest, pos, appr, act_] = await Promise.all([
    api("/api/state"), api("/api/signals/latest"), api("/api/positions"), api("/api/approvals"), api("/api/activity?limit=25")]);
  const r = st.risk, u = st.usage, V = st.venues;
  const h = st.halted || {};
  let html = `<header class="top">
    <div class="brand"><img src="/static/logo.png" alt=""><div><h1>Anands Fino</h1><div class="lab">Forex &amp; metals desk</div></div></div>
    <div class="tools">
      ${st.mock ? '<span class="pill warn">MOCK broker</span>' : ""}
      ${Object.values(V).filter((v) => v.on).map((v) => `<span class="pill ${v.connected === false ? "red" : v.name === "live" ? "warn" : "buy"}">${esc(v.label)} ${esc(v.tf_label)} ${v.auto ? "auto" : "manual"}</span>`).join("") || '<span class="pill none">No venue on: signals only</span>'}
      <span class="pill ${st.market_open ? "buy" : "none"}">Market ${st.market_open ? "open" : "closed"}</span>
      <a class="btn" href="#/reports">Reports</a>
      <button class="btn pri" onclick="act('/api/cycle?force=true',{},'Cycle started')">Run cycle now</button>
      <button class="btn" onclick="act('/api/refresh',{},'Refreshing feeds')">Refresh feeds</button>
    </div></header>`;

  if (h.on) html += `<div class="banner" role="alert"><strong>Trading halted.</strong><span>${esc(h.reason)}</span>
    <button class="btn sm" style="margin-left:auto" onclick="act('/api/resume',{},'Resumed')">Resume</button></div>`;
  for (const v of Object.values(V)) {
    if (v.halt && v.halt.on) html += `<div class="banner" role="alert"><strong>${esc(v.label)} venue paused.</strong><span>${esc(v.halt.reason)}</span>
      <button class="btn sm" style="margin-left:auto" onclick="act('/api/resume',{},'Resumed')">Resume</button></div>`;
    if (v.connected === false) {
      const needAuth = st.broker !== "mt5" && v.name !== "paper" && /authoris|ctrader\/connect/i.test(v.error || "");
      html += `<div class="banner warn"><strong>${esc(v.label)} not connected.</strong><span class="mute">${esc(v.error)}</span>
        ${needAuth ? '<a class="btn pri sm" style="margin-left:auto" href="/ctrader/connect">Connect cTrader</a>' : ""}
        <a class="btn sm" ${needAuth ? "" : 'style="margin-left:auto"'} href="/api/diagnostics?venue=${v.name}">Diagnostics</a></div>`;
    }
  }
  if (!st.feeds.claude_key) html += `<div class="banner warn"><strong>ANTHROPIC_API_KEY is not set.</strong><span class="mute">Claude cannot say YES, so no trade will pass.</span></div>`;

  html += `<section class="card strip">${Object.values(V).map((v) => {
    const a = v.account;
    return `<div><div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><strong>${esc(v.label)}</strong>
        ${a ? `<span class="pill ${a.demo ? "acc" : "warn"}">${a.demo ? "DEMO" : "REAL"} ${esc(a.login)}</span>` : v.locked ? '<span class="pill none">locked</span>' : ""}</div>
      <div class="mute" style="font-size:12px;margin:2px 0 8px">${esc(VDESC[v.name])}</div>
      <div class="v">${a ? money(a.equity) : "–"}</div>
      <div class="s">${a ? `today <span class="${a.day_pnl < 0 ? "bad" : "ok"}">${money(a.day_pnl)}</span> (${signed(a.day_pnl_pct, 2)}%) · ` : ""}${v.open_count}/${r.max_open} open${v.floating ? " · floating " + money(v.floating) : ""}</div>
      <div style="display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;align-items:center">
        <span class="lab">Trade ${v.locked ? `<button class="btn sm" onclick="if(confirm('Unlock LIVE trading? Real money on a real account. Live stays OFF until you switch it on.'))act('/api/allow-live',{on:true},'Live unlocked')">Unlock</button>` : sw(v.on, v.label + " venue", "/api/venue/" + v.name, `{on:${!v.on}}` ).replace("onclick=\"", v.name === "live" && !v.on ? "onclick=\"if(!confirm('Switch LIVE trading on? Real money on a real account.'))return;" : "onclick=\"")}</span>
        <span class="lab">Auto-approve ${v.locked ? "" : sw(v.auto, v.label + " auto-approve", "/api/venue/" + v.name, `{auto:${!v.auto}}`).replace("onclick=\"", v.name === "live" && !v.auto ? "onclick=\"if(!confirm('Auto-approve LIVE trades? Orders will go out without your click.'))return;" : "onclick=\"")}</span>
        <span class="lab">Candles <select aria-label="${esc(v.label)} chart timeframe" onchange="act('/api/venue/${v.name}',{tf:Number(this.value)},'${esc(v.label)} timeframe set')" style="background:var(--panel2);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:5px 8px;font:12px var(--font-mono)">${st.timeframes.map((m) => `<option value="${m}" ${m === v.tf ? "selected" : ""}>${m === 60 ? "1 hour" : m + " min"}</option>`).join("")}</select></span>
        ${v.name === "paper" ? `<button class="btn sm" onclick="askAct('Reset the paper account? Balance and history are cleared.','/api/paper/reset','Paper account reset')">Reset</button>` : ""}
      </div></div>`;
  }).join("")}</section>`;

  if (appr.length) {
    html += `<section class="card"><div class="hd"><h2>Waiting for your approval</h2><span class="mute">Claude said YES, the gate passed</span></div>`;
    for (const s of appr) {
      const l = s.levels || {};
      html += `<div class="appr"><div class="what">${vPill(s.venue)} <strong>${esc(s.symbol)}</strong> ${biasPill(s.bias)}
        <span class="mono mute"> ${num(s.lots, 2)} lots · entry ${esc(l.entry)} · SL ${esc(l.sl)} · TP ${esc(l.tp)}</span>
        <div class="mute">${esc((s.claude || {}).thesis)}</div>
        <div class="mute mono">expires ${hm(s.expires_at)} · <a href="#/signal/${s.id}">details</a></div></div>
        <div><button class="btn pri sm" onclick="act('/api/approve/${s.id}',{},'Approved')">Approve</button>
        <button class="btn sm" onclick="act('/api/reject/${s.id}',{},'Rejected')">Reject</button></div></div>`;
    }
    html += `</section>`;
  }

  const rows = latest.map((s) => `<tr>
    <td><a href="#/signal/${s.id}"><strong>${esc(s.symbol)}</strong></a></td>
    <td>${biasPill(s.bias)}</td>
    <td style="min-width:150px">${scoreBar(s.score)}<div class="mono mute" style="font-size:12px">${signed(s.score, 1)} · ${s.agree}/${s.available} agree</div></td>
    <td class="mono">${s.decision ? esc(s.decision) + (s.confidence != null ? " " + num(s.confidence, 2) : "") : "–"}</td>
    <td>${(s.venues || []).length ? s.venues.map((x) => `<a href="#/signal/${x.id}">${vPill(x.venue)} ${stPill(x.status)}</a>`).join(" ") : stPill(s.status)}<div class="mute" style="font-size:12px;max-width:340px">${esc((s.venues || []).length ? "" : s.reason)}</div></td>
    <td class="mono mute">${hm(s.ts)}</td></tr>`).join("");
  html += `<div class="grid"><div class="col-main">
    <section class="card"><div class="hd"><h2>Latest cycle</h2><span class="mute mono">threshold ±${st.thresholds.score} · ${st.thresholds.agree_pct ? st.thresholds.agree_pct + "% of factors" : st.thresholds.agree + "+ factors"} agree</span></div>
      <div class="tw"><table><thead><tr><th>Symbol</th><th>Bias</th><th>Score</th><th>Claude</th><th>Outcome</th><th>At</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="mute">No cycle has run yet. Press “Run cycle now”.</td></tr>'}</tbody></table></div></section>`;

  const prow = pos.open.map((p) => `<tr><td>${vPill(p.venue)} <strong>${esc(p.symbol)}</strong> <span class="pill ${p.side === "BUY" ? "buy" : "sell"}">${esc(p.side)}</span></td>
    <td class="mono">${num(p.lots, 2)}</td><td class="mono">${esc(p.entry)}</td><td class="mono">${esc(p.sl)} / ${esc(p.tp)}</td>
    <td class="mono ${p.floating < 0 ? "bad" : "ok"}">${money(p.floating)}</td><td class="mono">${money(p.risk_usd)}</td>
    <td class="r"><button class="btn dng sm" onclick="askAct('Close ${esc(p.symbol)} at market?','/api/close/${p.venue}/${p.ticket}','Closed')">Close</button></td></tr>`).join("");
  html += `<section class="card"><div class="hd"><h2>Open positions</h2></div><div class="tw"><table><thead><tr><th>Position</th><th>Lots</th><th>Entry</th><th>SL / TP</th><th>Floating</th><th>Risk</th><th></th></tr></thead>
    <tbody>${prow || '<tr><td colspan="7" class="mute">Nothing open.</td></tr>'}</tbody></table></div></section>`;

  const crow = pos.closed.map((p) => `<tr><td>${vPill(p.venue)} ${esc(p.symbol)} <span class="pill ${p.side === "BUY" ? "buy" : "sell"}">${esc(p.side)}</span></td>
    <td class="mono">${esc(p.entry)} → ${esc(p.close_price)}</td><td class="mono ${p.profit < 0 ? "bad" : "ok"}">${money(p.profit)}</td><td class="mono mute">${hm(p.closed_at)}</td></tr>`).join("");
  if (crow) html += `<section class="card"><div class="hd"><h2>Recently closed</h2></div><div class="tw"><table style="min-width:420px"><tbody>${crow}</tbody></table></div></section>`;

  html += `<section class="card"><div class="hd"><h2>Activity</h2></div>${act_.map((x) => `<div class="feed"><span class="t">${hm(x.ts)}</span>
    <span class="${x.level === "error" ? "bad" : x.level === "warn" ? "warn" : ""}">${x.symbol ? "<strong>" + esc(x.symbol) + "</strong> " : ""}${esc(x.text)}${x.signal_id ? ` <a href="#/signal/${x.signal_id}">view</a>` : ""}</span></div>`).join("") || '<div class="pad mute">No activity yet.</div>'}</section>
    </div><div class="col-side">`;

  html += `<section class="card"><div class="hd"><h2>Symbols</h2><span class="mono mute">${st.symbols.length} traded${st.symbols_custom ? " · set here" : " · from environment"}</span></div><div class="pad">
    <div class="mono" style="font-size:12px;margin-bottom:8px;word-break:break-word">${st.symbols.map(esc).join(" · ")}</div>
    <textarea id="symIn" rows="3" style="width:100%;background:var(--panel2);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px;font:12px var(--font-mono)">${esc(st.symbols_default.join(","))}</textarea>
    <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap"><button class="btn sm pri" onclick="act('/api/symbols',{symbols:document.getElementById('symIn').value},'Symbols saved')">Save list</button>
    ${st.symbols_custom ? `<button class="btn sm" onclick="act('/api/symbols',{reset:true},'Back to the environment list')">Use environment list</button>` : ""}</div>
    <div class="mute" style="font-size:12px;margin-top:6px">Takes effect on the next cycle. The textarea starts from the built-in list.</div></div></section>`;

  html += `<section class="card"><div class="hd"><h2>Kill switch</h2></div><div class="pad">
    <div class="mute" style="margin-bottom:10px">Stops every venue. A venue also pauses by itself at ${r.max_loss_streak} losses in a row or a ${r.daily_loss_cap_pct}% daily loss.</div>
    ${h.on ? "" : `<button class="btn dng" onclick="act('/api/halt',{reason:'halted from the dashboard'},'Halted')">Halt trading</button>`}
    ${st.allow_live ? `<button class="btn sm" style="margin-left:8px" onclick="act('/api/allow-live',{on:false},'Live locked')">Lock Live</button>` : ""}</div></section>`;

  const meter = (label, val, cap, txt, hot) => `<div class="row" style="display:block"><div style="display:flex;justify-content:space-between"><span>${label}</span><span class="mono mute">${txt}</span></div>
    <div class="meter ${hot ? "hot" : ""}"><i style="width:${Math.min(100, cap ? (val / cap) * 100 : 0)}%"></i></div></div>`;
  html += `<section class="card"><div class="hd"><h2>Risk limits</h2><span class="mono mute">per venue</span></div>
    ${Object.values(V).filter((v) => v.on || v.open_count).map((v) => {
      const dl = v.account ? Math.max(0, -v.account.day_pnl_pct) : 0, orp = v.open_risk_pct || 0;
      return `<div class="row" style="border-top:1px solid var(--line)"><strong>${esc(v.label)}</strong></div>
        ${meter("Daily loss", dl, r.daily_loss_cap_pct, `${num(dl, 2)}% / ${r.daily_loss_cap_pct}%`, dl > r.daily_loss_cap_pct * 0.7)}
        ${meter("Open risk", orp, r.open_risk_cap_pct, `${num(orp, 2)}% / ${num(r.open_risk_cap_pct, 1)}%`, orp > r.open_risk_cap_pct * 0.7)}
        ${meter("Loss streak", v.loss_streak, r.max_loss_streak, `${v.loss_streak} / ${r.max_loss_streak}`, v.loss_streak >= r.max_loss_streak - 1)}`;
    }).join("") || '<div class="pad mute">No venue is on.</div>'}
    <div class="row"><span>Risk per trade</span><span class="mono">${r.risk_per_trade_pct}%</span></div></section>`;

  html += `<section class="card"><div class="hd"><h2>Events (36h)</h2></div>${st.events.map((e) => `<div class="row"><span><span class="pill ${e.impact === "High" ? "warn" : "none"}">${esc(e.currency)}</span> ${esc(e.title)}</span>
    <span class="mono mute">${e.minutes >= 0 ? "in " + Math.floor(e.minutes / 60) + "h " + (e.minutes % 60) + "m" : "now"}</span></div>`).join("") || '<div class="pad mute">No high or medium events soon, or the calendar has not loaded.</div>'}</section>`;

  const sys = (n, ok, extra) => `<div class="row"><span><span class="dot ${ok ? "g" : "r"}"></span>${n}</span><span class="mono mute">${extra}</span></div>`;
  const ag = st.feeds.ages_min;
  html += `<section class="card"><div class="hd"><h2>Systems</h2></div>
    ${sys("Claude (final say)", st.feeds.claude_key, `${u.claude}/${u.claude_cap} today`)}
    ${sys("FreeLLMAPI (tagging)", st.feeds.freellm_url, `${u.freellm}/${u.freellm_cap} today`)}
    ${sys("FRED macro", st.feeds.fred_key, ago(ag.macro))}
    ${sys("CFTC positioning", ag.cot != null, ago(ag.cot))}
    ${sys("Calendar", ag.calendar != null, ago(ag.calendar))}
    ${sys("News", ag.news_run != null, ago(ag.news_run))}
    <div class="pad" style="padding-top:12px"><a href="/api/diagnostics">Open diagnostics</a></div></section>
    </div></div>`;
  $app.innerHTML = html;
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
  let html = `<header class="top"><div class="brand"><img src="/static/logo.png" alt=""><div><h1>${esc(s.symbol)} ${biasPill(s.bias)}</h1>
    <div class="lab">Signal #${s.id} · ${esc(s.candle_time)} · ${esc(VLABEL[s.venue] || "analysis")}${s.mode ? " · " + esc(s.mode.split(":")[1]) : ""}</div></div></div>
    <div class="tools">${stPill(s.status)}<a class="btn" href="#/">Back to desk</a></div></header>
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
async function reports() {
  const r = await api("/api/report?days=" + repDays);
  let html = `<header class="top"><div class="brand"><img src="/static/logo.png" alt=""><div><h1>Trading reports</h1><div class="lab">Paper · Demo · Live, closed trades after commission and swap</div></div></div>
    <div class="tools"><span class="seg" style="padding:0">${[[0, "All time"], [1, "Today"], [7, "7 days"], [30, "30 days"]].map(([d, l]) => `<button aria-pressed="${repDays === d}" onclick="repDays=${d};render()">${l}</button>`).join("")}</span>
    <a class="btn" href="#/">Back to desk</a></div></header>`;
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

/* ------------------------------------------------------------ router */
async function render() {
  if (busy) return;
  busy = true;
  try {
    const m = location.hash.match(/^#\/signal\/(\d+)/);
    await (m ? detail(m[1]) : location.hash === "#/reports" ? reports() : desk());
  } catch (e) {
    if (!$app.querySelector("header")) $app.innerHTML = `<p class="bad" style="padding:40px 0">Could not load: ${esc(e.message)}</p>`;
    else toast(e.message, true);
  } finally { busy = false; }
}
window.addEventListener("hashchange", () => { window.scrollTo(0, 0); render(); });
render();
timer = setInterval(() => { if (!document.hidden && !location.hash.startsWith("#/signal") && location.hash !== "#/reports") render(); }, 10000);
