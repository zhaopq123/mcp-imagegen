"""管理面板页面（单文件 SPA：HTML + CSS + JS 全内联，零外部依赖）。

为什么不用框架 / 不打 CDN
-------------------------
这个面板要能在**纯内网甚至离线**的机器上打开（典型部署是家里的
一台小主机或手机上的容器），任何外链 JS/CSS 都可能直接白屏。内联 + 原生
JS 意味着：装完依赖就有界面，升级不需要重新拉前端资源。

``{{PREFIX}}`` 会在返回响应时被替换成实际的管理路径前缀（支持反代子路径部署）。
"""
from __future__ import annotations

_PAGE = """<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>生成服务控制台</title>
<style>
:root {
  --bg: #0b1020; --bg-soft: #101730; --card: #141b31; --card-2: #1a2340;
  --border: #242c48; --border-soft: #1d2440; --text: #e7ebf5; --muted: #8f9bb8;
  --brand: #6366f1; --brand-2: #22d3ee; --ok: #34d399; --warn: #fbbf24;
  --bad: #f87171; --shadow: 0 8px 28px rgba(0,0,0,.35);
  --radius: 14px; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
[data-theme="light"] {
  --bg: #f4f6fb; --bg-soft: #eef1f8; --card: #ffffff; --card-2: #f7f9fd;
  --border: #e2e7f2; --border-soft: #eaeef6; --text: #17203a; --muted: #66718c;
  --shadow: 0 8px 24px rgba(23,32,58,.08);
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; }
body {
  background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.55;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: inherit; text-decoration: none; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 8px; }
::-webkit-scrollbar-track { background: transparent; }

/* ---------- shell ---------- */
.shell { display: flex; min-height: 100vh; }
.side {
  width: 236px; flex: 0 0 236px; padding: 18px 14px; background: var(--bg-soft);
  border-right: 1px solid var(--border-soft); display: flex; flex-direction: column;
  position: sticky; top: 0; height: 100vh;
}
.brand { display: flex; align-items: center; gap: 10px; padding: 6px 8px 18px; }
.brand-mark {
  width: 34px; height: 34px; border-radius: 10px; flex: 0 0 34px;
  background: linear-gradient(135deg, var(--brand), var(--brand-2));
  display: grid; place-items: center; color: #fff; font-weight: 700; font-size: 15px;
  box-shadow: 0 6px 18px rgba(99,102,241,.35);
}
.brand-text b { display: block; font-size: 14.5px; letter-spacing: .2px; }
.brand-text span { font-size: 11.5px; color: var(--muted); }
.nav { display: flex; flex-direction: column; gap: 3px; }
.nav-item {
  display: flex; align-items: center; gap: 10px; padding: 9px 11px; border-radius: 10px;
  color: var(--muted); cursor: pointer; border: 1px solid transparent;
  transition: background .15s, color .15s;
}
.nav-item:hover { background: var(--card); color: var(--text); }
.nav-item.active { background: var(--card-2); color: var(--text); border-color: var(--border); }
.nav-item svg { width: 17px; height: 17px; opacity: .9; }
.nav-sep { height: 1px; background: var(--border-soft); margin: 14px 6px 8px; }
.side-foot { margin-top: auto; padding: 10px 8px 0; font-size: 11.5px; color: var(--muted); }

.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.topbar {
  height: 62px; padding: 0 24px; display: flex; align-items: center; gap: 14px;
  border-bottom: 1px solid var(--border-soft); background: color-mix(in srgb, var(--bg) 88%, transparent);
  position: sticky; top: 0; z-index: 20; backdrop-filter: blur(8px);
}
.topbar h1 { font-size: 17px; margin: 0; font-weight: 650; }
.topbar .sub { font-size: 12px; color: var(--muted); }
.spacer { flex: 1; }
.content { padding: 22px 24px 40px; max-width: 1280px; width: 100%; }

/* ---------- primitives ---------- */
.btn {
  display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border);
  background: var(--card); color: var(--text); padding: 7px 13px; border-radius: 9px;
  cursor: pointer; font-size: 13px; transition: .15s; white-space: nowrap;
}
.btn:hover { border-color: var(--brand); transform: translateY(-1px); }
.btn:active { transform: none; }
.btn.primary { background: linear-gradient(135deg, var(--brand), #7c3aed); border-color: transparent; color: #fff; }
.btn.ghost { background: transparent; }
.btn.danger { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 40%, var(--border)); }
.btn.sm { padding: 4px 9px; font-size: 12px; border-radius: 7px; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.input, .textarea, .select {
  width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text);
  border-radius: 9px; padding: 8px 11px; font-size: 13px; font-family: inherit; outline: none;
}
.input:focus, .textarea:focus, .select:focus { border-color: var(--brand); box-shadow: 0 0 0 3px color-mix(in srgb, var(--brand) 18%, transparent); }
.textarea { font-family: var(--mono); font-size: 12.5px; line-height: 1.5; resize: vertical; }
label.field { display: block; margin-bottom: 13px; }
label.field > span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 5px; }
.hint { font-size: 11.5px; color: var(--muted); margin-top: 4px; }
.card {
  background: var(--card); border: 1px solid var(--border-soft); border-radius: var(--radius);
  padding: 16px; box-shadow: var(--shadow);
}
.card > header {
  display: flex; align-items: center; gap: 10px; margin: -2px 0 14px;
}
.card > header h3 { margin: 0; font-size: 14px; font-weight: 620; }
.grid { display: grid; gap: 16px; }
.grid.stats { grid-template-columns: repeat(auto-fit, minmax(196px, 1fr)); }
.grid.two { grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }
.stat { position: relative; overflow: hidden; }
.stat .label { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.stat .value { font-size: 26px; font-weight: 660; margin-top: 6px; letter-spacing: .5px; }
.stat .foot { font-size: 11.5px; color: var(--muted); margin-top: 2px; }
.stat .accent { position: absolute; right: -18px; top: -18px; width: 68px; height: 68px; border-radius: 50%; background: color-mix(in srgb, var(--brand) 16%, transparent); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; font-weight: 560; color: var(--muted); font-size: 11.5px;
  text-transform: uppercase; letter-spacing: .5px; padding: 8px 10px; border-bottom: 1px solid var(--border-soft);
}
td { padding: 10px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
tbody tr:hover { background: color-mix(in srgb, var(--brand) 5%, transparent); }
.badge {
  display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border-radius: 999px;
  font-size: 11.5px; border: 1px solid var(--border); background: var(--card-2); color: var(--muted);
}
.badge.ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 35%, transparent); }
.badge.bad { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 35%, transparent); }
.badge.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 35%, transparent); }
.badge.brand { color: var(--brand-2); border-color: color-mix(in srgb, var(--brand-2) 35%, transparent); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
.dot.ok { background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 22%, transparent); }
.dot.bad { background: var(--bad); box-shadow: 0 0 0 3px color-mix(in srgb, var(--bad) 22%, transparent); }
.dot.warn { background: var(--warn); box-shadow: 0 0 0 3px color-mix(in srgb, var(--warn) 22%, transparent); }
.mono { font-family: var(--mono); font-size: 12px; }
.muted { color: var(--muted); }
.row-actions { display: flex; gap: 6px; justify-content: flex-end; }
.switch { position: relative; width: 38px; height: 21px; display: inline-block; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; inset: 0; background: var(--border); border-radius: 999px; cursor: pointer; transition: .18s; }
.slider:before { content: ""; position: absolute; width: 15px; height: 15px; left: 3px; top: 3px; background: #fff; border-radius: 50%; transition: .18s; }
.switch input:checked + .slider { background: var(--ok); }
.switch input:checked + .slider:before { transform: translateX(17px); }
.bar { height: 6px; border-radius: 999px; background: var(--border); overflow: hidden; }
.bar > i { display: block; height: 100%; background: linear-gradient(90deg, var(--brand), var(--brand-2)); }
.empty { padding: 34px 16px; text-align: center; color: var(--muted); }
.empty svg { width: 40px; height: 40px; opacity: .45; margin-bottom: 8px; }
.progress-line { display: flex; gap: 8px; flex-wrap: wrap; font-size: 11.5px; color: var(--muted); }
kbd { font-family: var(--mono); background: var(--card-2); border: 1px solid var(--border); border-bottom-width: 2px; border-radius: 5px; padding: 1px 5px; font-size: 11px; }

/* ---------- overlay ---------- */
.mask { position: fixed; inset: 0; background: rgba(6,9,20,.62); backdrop-filter: blur(3px); display: grid; place-items: center; z-index: 60; padding: 20px; }
.modal {
  background: var(--card); border: 1px solid var(--border); border-radius: 16px;
  box-shadow: var(--shadow); width: 100%; max-width: 720px; max-height: 88vh; display: flex; flex-direction: column;
}
.modal.small { max-width: 420px; }
.modal header { padding: 15px 18px; border-bottom: 1px solid var(--border-soft); display: flex; align-items: center; gap: 10px; }
.modal header h3 { margin: 0; font-size: 15px; }
.modal .body { padding: 18px; overflow: auto; }
.modal footer { padding: 13px 18px; border-top: 1px solid var(--border-soft); display: flex; gap: 8px; justify-content: flex-end; }
.tabs { display: flex; gap: 4px; padding: 10px 18px 0; border-bottom: 1px solid var(--border-soft); flex-wrap: wrap; }
.tab { padding: 7px 12px; font-size: 13px; color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent; }
.tab.active { color: var(--text); border-bottom-color: var(--brand); }
.toast-wrap { position: fixed; right: 20px; bottom: 20px; display: flex; flex-direction: column; gap: 8px; z-index: 90; }
.toast { background: var(--card); border: 1px solid var(--border); border-left: 3px solid var(--brand); padding: 10px 14px; border-radius: 10px; box-shadow: var(--shadow); min-width: 220px; max-width: 420px; font-size: 13px; animation: in .2s ease; }
.toast.ok { border-left-color: var(--ok); }
.toast.bad { border-left-color: var(--bad); }
@keyframes in { from { opacity: 0; transform: translateY(8px); } }
.spinner { width: 15px; height: 15px; border: 2px solid var(--border); border-top-color: var(--brand); border-radius: 50%; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.center-screen { min-height: 100vh; display: grid; place-items: center; padding: 20px; }
.auth-card { width: 100%; max-width: 380px; }
.skeleton { height: 13px; border-radius: 6px; background: linear-gradient(90deg, var(--card-2), var(--border), var(--card-2)); background-size: 200% 100%; animation: sh 1.2s infinite; }
@keyframes sh { to { background-position: -200% 0; } }
@media (max-width: 860px) {
  .side { position: fixed; left: -240px; z-index: 50; transition: left .2s; }
  .side.open { left: 0; }
  .content { padding: 16px; }
}
</style>
</head>
<body>
<div id="root"></div>
<div class="toast-wrap" id="toasts"></div>
<script>
const PREFIX = "{{PREFIX}}";
const API = PREFIX + "/api";
const WRITE_HEADERS = { "X-Admin-Request": "1", "Content-Type": "application/json" };
const store = {
  page: "dashboard", state: null, theme: localStorage.getItem("mi.theme") || "dark",
  data: {}, tab: "basic", autoRefresh: true, timer: null, expanded: {},
};

/* ---------------- helpers ---------------- */
function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function el(html) { const d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstElementChild; }
function fmtNum(n) { return (n || 0).toLocaleString("zh-CN"); }
function fmtSec(s) {
  if (s === null || s === undefined) return "-";
  const v = Number(s); if (v < 60) return v.toFixed(1) + "s";
  const m = Math.floor(v / 60), h = Math.floor(m / 60);
  return h ? h + "h" + (m % 60) + "m" : m + "m" + Math.round(v % 60) + "s";
}
function fmtTime(ts) { if (!ts) return "-"; return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }); }
function fmtClock(ts) { if (!ts) return "-"; return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }); }
async function api(path, opts) {
  opts = opts || {};
  const method = (opts.method || "GET").toUpperCase();
  const headers = Object.assign({}, opts.headers || {});
  if (method !== "GET") Object.assign(headers, WRITE_HEADERS);
  const res = await fetch(API + path, { method, headers, body: opts.body, credentials: "same-origin" });
  let payload = null;
  try { payload = await res.json(); } catch (e) { payload = { ok: false, error: "bad_json", message: "响应不是 JSON" }; }
  if (res.status === 401) { store.state = (store.state || {}); store.state.authenticated = false; render(); }
  return Object.assign({ _status: res.status }, payload);
}
function toast(message, kind) {
  const node = el('<div class="toast ' + (kind || "") + '">' + esc(message) + "</div>");
  document.getElementById("toasts").appendChild(node);
  setTimeout(function () { node.style.opacity = "0"; node.style.transition = ".3s"; setTimeout(function () { node.remove(); }, 300); }, 3200);
}
function confirmBox(title, message, onYes) {
  const mask = el('<div class="mask"><div class="modal small"><header><h3>' + esc(title) + '</h3></header>'
    + '<div class="body"><p class="muted" style="margin:0">' + esc(message) + '</p></div>'
    + '<footer><button class="btn ghost" data-no>取消</button><button class="btn danger" data-yes>确定</button></footer></div></div>');
  document.body.appendChild(mask);
  mask.querySelector("[data-no]").onclick = function () { mask.remove(); };
  mask.querySelector("[data-yes]").onclick = function () { mask.remove(); onYes(); };
  mask.onclick = function (e) { if (e.target === mask) mask.remove(); };
}
function closeModal() { const m = document.querySelector(".mask"); if (m) m.remove(); }

/* ---------------- icons ---------------- */
const ICON = {
  dashboard: '<path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"/>',
  provider: '<path d="M12 2 2 7l10 5 10-5-10-5zm0 7.5L4.2 5.6 12 9.6l7.8-4-7.8 4.9zM2 17l10 5 10-5M2 12l10 5 10-5"/>',
  token: '<path d="M14 2a6 6 0 0 0-5.7 7.9L2 16.2V22h5.8l6.3-6.3A6 6 0 1 0 14 2zm3.5 6.5a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z"/>',
  tasks: '<path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h10v2H4z"/>',
  settings: '<path d="M12 15.5A3.5 3.5 0 1 1 15.5 12 3.5 3.5 0 0 1 12 15.5zm7.4-2.6.1-.9-.1-.9 2-1.5-2-3.4-2.3 1a7.6 7.6 0 0 0-1.6-.9L15 4.4H9l-.5 2.4c-.6.2-1.1.5-1.6.9l-2.3-1-2 3.4L4.6 11l-.1.9.1.9-2 1.5 2 3.4 2.3-1c.5.4 1 .7 1.6.9l.5 2.4h6l.5-2.4c.6-.2 1.1-.5 1.6-.9l2.3 1 2-3.4z"/>',
  logs: '<path d="M4 4h16v2H4zm0 4h16v2H4zm0 4h12v2H4zm0 4h16v2H4z"/>',
  plus: '<path d="M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6z"/>',
  refresh: '<path d="M12 4a8 8 0 0 1 7.4 5H17a6 6 0 0 0-11.3 2.2L4 11V5h6L7.8 7.2A8 8 0 0 1 12 4zm8 6v6h-6l2.2-2.2A6 6 0 0 1 18 14z"/>',
  sun: '<path d="M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zm0-14 1.6 2.4h-3.2L12 3zm0 18 1.6-2.4h-3.2L12 21zM3 12l2.4-1.6v3.2L3 12zm18 0-2.4 1.6v-3.2L21 12z"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/>',
  logout: '<path d="M10 17l1.4-1.4L9.8 14H16v-2H9.8l1.6-1.6L10 9l-4 4zm8-13H4v18h14V4z"/>',
  copy: '<path d="M16 1H4v14h2V3h10V1zm3 4H8v14h11V5zm-2 12H10V7h7v10z"/>',
};
function icon(name, size) {
  return '<svg viewBox="0 0 24 24" width="' + (size || 17) + '" height="' + (size || 17) + '" fill="currentColor">' + (ICON[name] || "") + "</svg>";
}

/* ---------------- shell ---------------- */
function render() {
  const root = document.getElementById("root");
  document.documentElement.setAttribute("data-theme", store.theme);
  localStorage.setItem("mi.theme", store.theme);
  if (!store.state) { root.innerHTML = '<div class="center-screen"><div class="skeleton" style="width:300px"></div></div>'; return; }
  if (!store.state.initialized) { root.innerHTML = renderSetup(); bindSetup(); return; }
  if (!store.state.authenticated) { root.innerHTML = renderLogin(); bindLogin(); return; }
  root.innerHTML = '<div class="shell">' + renderSide() + '<div class="main">' + renderTop() + '<div class="content" id="content"></div></div></div>';
  bindShell();
  route();
}
function renderSide() {
  const items = [
    ["dashboard", "总览", "dashboard"], ["providers", "服务提供方", "provider"],
    ["tokens", "访问令牌", "token"], ["tasks", "视频任务", "tasks"],
    ["settings", "运行参数", "settings"], ["logs", "服务日志", "logs"],
  ];
  const nav = items.map(function (it) {
    return '<div class="nav-item' + (store.page === it[0] ? " active" : "") + '" data-page="' + it[0] + '">' + icon(it[2]) + "<span>" + it[1] + "</span></div>";
  }).join("");
  return '<aside class="side" id="side">'
    + '<div class="brand"><div class="brand-mark">M</div><div class="brand-text"><b>生成服务控制台</b><span>v' + esc(store.state.version) + "</span></div></div>"
    + '<nav class="nav">' + nav + "</nav>"
    + '<div class="side-foot"><div class="progress-line"><span class="dot ok"></span> 服务运行中</div>'
    + '<div style="margin-top:6px">图片 / 视频 MCP</div></div></aside>';
}
function renderTop() {
  const titles = { dashboard: ["总览", "实时健康与吞吐"], providers: ["服务提供方", "自定义任意图片 / 视频 API"],
    tokens: ["访问令牌", "多会话与多工作空间隔离"], tasks: ["视频任务", "后台作业与异步结果"],
    settings: ["运行参数", "并发、预算与配额"], logs: ["服务日志", "最近的服务端输出"] };
  const meta = titles[store.page] || ["", ""];
  return '<div class="topbar">'
    + '<button class="btn ghost sm" id="menu-btn" style="display:none">' + icon("tasks") + "</button>"
    + '<div><h1>' + meta[0] + '</h1><div class="sub">' + meta[1] + "</div></div>"
    + '<div class="spacer"></div>'
    + '<label class="switch" title="自动刷新"><input type="checkbox" id="auto-refresh" ' + (store.autoRefresh ? "checked" : "") + '><span class="slider"></span></label>'
    + '<span class="muted" style="font-size:12px">自动刷新</span>'
    + '<button class="btn sm" id="theme-btn">' + icon(store.theme === "dark" ? "sun" : "moon") + "</button>"
    + '<button class="btn sm" id="refresh-btn">' + icon("refresh") + "</button>"
    + '<button class="btn sm danger" id="logout-btn">' + icon("logout") + "</button></div>";
}
function bindShell() {
  document.querySelectorAll(".nav-item").forEach(function (node) {
    node.onclick = function () { store.page = node.dataset.page; location.hash = "#/" + store.page; route(); };
  });
  document.getElementById("logout-btn").onclick = async function () {
    await api("/auth/logout", { method: "POST" });
    store.state = { initialized: true, authenticated: false, version: store.state.version };
    render();
  };
  document.getElementById("refresh-btn").onclick = function () { loadPage(true); toast("已刷新"); };
  document.getElementById("theme-btn").onclick = function () {
    store.theme = store.theme === "dark" ? "light" : "dark"; render();
  };
  document.getElementById("auto-refresh").onchange = function (e) {
    store.autoRefresh = e.target.checked; setupTimer();
  };
  setupTimer();
}
function setupTimer() {
  if (store.timer) { clearInterval(store.timer); store.timer = null; }
  if (store.autoRefresh) store.timer = setInterval(function () {
    if (["dashboard", "tasks", "logs", "providers"].indexOf(store.page) >= 0) loadPage(true);
  }, 5000);
}

/* ---------------- auth screens ---------------- */
function renderSetup() {
  return '<div class="center-screen"><div class="card auth-card">'
    + '<div class="brand" style="padding:0 0 14px"><div class="brand-mark">M</div><div class="brand-text"><b>初始化控制台</b><span>设置管理员密码</span></div></div>'
    + '<label class="field"><span>管理员密码（至少 8 位）</span><input class="input" type="password" id="pw" placeholder="••••••••"></label>'
    + '<label class="field"><span>确认密码</span><input class="input" type="password" id="pw2"></label>'
    + '<button class="btn primary" id="setup-btn" style="width:100%;justify-content:center">完成初始化</button>'
    + '<div class="hint" style="margin-top:10px">密码用 PBKDF2 加盐哈希后写入配置文件，服务端不存明文、也没有任何出厂默认口令。</div>'
    + "</div></div>";
}
function bindSetup() {
  const go = async function () {
    const a = document.getElementById("pw").value, b = document.getElementById("pw2").value;
    if (a.length < 8) return toast("密码至少 8 位", "bad");
    if (a !== b) return toast("两次输入不一致", "bad");
    const res = await api("/setup", { method: "POST", body: JSON.stringify({ password: a }) });
    if (!res.ok) return toast(res.message || res.error, "bad");
    toast("初始化完成，请登录", "ok");
    await bootstrap();
  };
  document.getElementById("setup-btn").onclick = go;
}
function renderLogin() {
  return '<div class="center-screen"><div class="card auth-card">'
    + '<div class="brand" style="padding:0 0 14px"><div class="brand-mark">M</div><div class="brand-text"><b>生成服务控制台</b><span>请输入管理员密码</span></div></div>'
    + '<label class="field"><span>管理员密码</span><input class="input" type="password" id="pw" autofocus></label>'
    + '<button class="btn primary" id="login-btn" style="width:100%;justify-content:center">登录</button>'
    + "</div></div>";
}
function bindLogin() {
  const go = async function () {
    const res = await api("/auth/login", { method: "POST", body: JSON.stringify({ password: document.getElementById("pw").value }) });
    if (!res.ok) return toast(res.message || res.error, "bad");
    store.state.authenticated = true; render(); route();
  };
  document.getElementById("login-btn").onclick = go;
  document.getElementById("pw").onkeydown = function (e) { if (e.key === "Enter") go(); };
}

/* ---------------- router ---------------- */
function route() {
  const hash = (location.hash || "#/dashboard").replace("#/", "");
  if (hash && hash !== store.page) store.page = hash;
  const content = document.getElementById("content");
  if (!content) return;
  content.innerHTML = '<div class="grid"><div class="card"><div class="skeleton" style="width:40%"></div><div class="skeleton" style="width:70%;margin-top:8px"></div></div></div>';
  document.querySelectorAll(".nav-item").forEach(function (n) { n.classList.toggle("active", n.dataset.page === store.page); });
  loadPage(false);
}
async function loadPage(silent) {
  const content = document.getElementById("content");
  if (!content) return;
  const view = { dashboard: viewDashboard, providers: viewProviders, tokens: viewTokens, tasks: viewTasks, settings: viewSettings, logs: viewLogs }[store.page] || viewDashboard;
  try { await view(content, silent); } catch (e) { content.innerHTML = '<div class="card"><div class="empty">加载失败：' + esc(e.message || e) + "</div></div>"; }
}

/* ---------------- dashboard ---------------- */
async function viewDashboard(node, silent) {
  const res = await api("/dashboard");
  if (!res.ok) throw new Error(res.message || res.error);
  const d = res;
  const m = d.metrics || {};
  const lat = m.latency_ms || {};
  const cards = [
    ["请求总数", fmtNum(m.total), "成功 " + fmtNum(m.ok) + " · 失败 " + fmtNum(m.failed)],
    ["成功率", (m.success_rate === null ? "-" : m.success_rate + "%"), "近窗 " + fmtNum((m.latency_ms || {}).window) + " 条采样"],
    ["吞吐 QPS", m.qps_60s, "最近 60 秒"],
    ["P95 延迟", lat.p95 + " ms", "P50 " + lat.p50 + " / P99 " + lat.p99 + " ms"],
    ["服务提供方", d.counts.providers_enabled + " / " + d.counts.providers, "已启用 / 已配置"],
    ["视频作业", d.counts.video_jobs, "内存中跟踪"],
    ["令牌", d.counts.tokens_enabled + " / " + d.counts.tokens, "已启用 / 已签发"],
    ["运行时长", fmtSec(m.uptime_sec), "启动于 " + fmtClock(m.started_at)],
  ];
  const statCards = cards.map(function (c) {
    return '<div class="card stat"><span class="accent"></span><div class="label">' + esc(c[0]) + '</div><div class="value">' + esc(c[1]) + '</div><div class="foot">' + esc(c[2]) + "</div></div>";
  }).join("");

  const laneRows = Object.keys(d.lanes || {}).map(function (name) {
    const lane = d.lanes[name];
    const pct = Math.min(100, Math.round((lane.in_use / Math.max(lane.size, 1)) * 100));
    return "<tr><td><b>" + esc(name) + '</b></td><td>' + lane.in_use + " / " + lane.size + '</td><td>' + lane.waiting + " / " + (lane.max_queued || "∞")
      + '</td><td style="width:130px"><div class="bar"><i style="width:' + pct + '%"></i></div></td></tr>';
  }).join("");

  const entries = Object.keys(d.providers || {});
  const providerRows = entries.length ? entries.map(function (pid) {
    const p = d.providers[pid];
    const state = p.circuit_open ? '<span class="badge bad">熔断</span>' : '<span class="badge ok">正常</span>';
    const caps = (p.capabilities || []).map(function (c) { return '<span class="badge brand">' + esc(c) + "</span>"; }).join(" ");
    return "<tr><td><b>" + esc(pid) + '</b><div class="muted mono" style="font-size:11px">' + esc(p.base_url || "(未配置地址)") + "</div></td>"
      + "<td>" + caps + "</td><td>" + p.priority + "</td><td>" + state + "</td>"
      + "<td>" + fmtNum(p.calls) + ' <span class="muted">/ ' + fmtNum(p.fail) + " 失败</span></td>"
      + "<td>" + fmtNum(p.last_latency_ms) + " ms</td></tr>";
  }).join("") : '<tr><td colspan="6"><div class="empty">还没有配置服务提供方，去「服务提供方」新建一个</div></td></tr>';

  const recent = (m.recent || []).slice(-12).reverse().map(function (r) {
    const kind = r.result && r.result.indexOf("ok") === 0 ? "ok" : (r.result && r.result.indexOf("error") === 0 ? "bad" : "warn");
    return "<tr><td class='mono'>" + fmtTime(r.ts) + "</td><td>" + esc(r.kind) + "</td><td>" + esc(r.provider || "-") + "</td>"
      + "<td><span class='badge " + kind + "'>" + esc(r.result) + "</span></td><td>" + esc(r.ms) + " ms</td>"
      + "<td class='muted' style='max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>" + esc(r.detail || "") + "</td></tr>";
  }).join("");

  node.innerHTML = '<div class="grid stats">' + statCards + "</div>"
    + '<div class="grid two" style="margin-top:16px">'
    + '<div class="card"><header><h3>并发泳道</h3><span class="spacer"></span><span class="muted" style="font-size:12px">占用 / 配额</span></header>'
    + '<table><thead><tr><th>泳道</th><th>占用</th><th>排队</th><th>水位</th></tr></thead><tbody>' + (laneRows || '<tr><td colspan="4"><div class="empty">无</div></td></tr>') + "</tbody></table></div>"
    + '<div class="card"><header><h3>能力就绪度</h3></header>'
    + Object.keys(d.capabilities || {}).map(function (k) {
      const ok = d.capabilities[k];
      return '<div style="display:flex;align-items:center;gap:10px;padding:9px 2px;border-bottom:1px solid var(--border-soft)">'
        + '<span class="dot ' + (ok ? "ok" : "bad") + '"></span><b>' + esc(k) + '</b><span class="spacer"></span>'
        + '<span class="badge ' + (ok ? "ok" : "warn") + '">' + (ok ? "已就绪" : "缺提供方") + "</span></div>";
    }).join("")
    + '<div style="margin-top:12px" class="muted">磁盘水位：' + (d.disk_ratio === null ? "不可得" : Math.round(d.disk_ratio * 100) + "%") + "</div></div>"
    + "</div>"
    + '<div class="card" style="margin-top:16px"><header><h3>服务提供方健康</h3></header>'
    + '<table><thead><tr><th>名称 / 地址</th><th>能力</th><th>优先级</th><th>状态</th><th>调用</th><th>最近耗时</th></tr></thead><tbody>' + providerRows + "</tbody></table></div>"
    + '<div class="card" style="margin-top:16px"><header><h3>最近请求</h3><span class="spacer"></span><span class="muted" style="font-size:12px">最近 5 分钟</span></header>'
    + '<table><thead><tr><th>时间</th><th>类型</th><th>提供方</th><th>结果</th><th>耗时</th><th>摘要</th></tr></thead><tbody>' + (recent || '<tr><td colspan="6"><div class="empty">暂无请求</div></td></tr>') + "</tbody></table></div>";
}

/* ---------------- providers ---------------- */
async function viewProviders(node) {
  const res = await api("/providers");
  if (!res.ok) throw new Error(res.message || res.error);
  const rows = (res.items || []).map(function (p) {
    const caps = Object.keys(p.capabilities || {}).map(function (c) {
      const on = p.capabilities[c].enabled;
      return '<span class="badge ' + (on ? "brand" : "") + '">' + esc(c) + (on ? "" : " (关)") + "</span>";
    }).join(" ");
    return '<tr><td><div style="display:flex;align-items:center;gap:8px">'
      + '<span class="dot ' + (p.enabled ? "ok" : "") + '"></span><div><b>' + esc(p.name) + '</b>'
      + '<div class="muted mono" style="font-size:11px">' + esc(p.id) + " · " + esc(p.preset) + "</div></div></div></td>"
      + "<td>" + caps + "</td><td>" + p.priority + "</td>"
      + '<td><span class="badge ' + (p.auth.token_set ? "ok" : "bad") + '">' + (p.auth.token_set ? esc(p.auth.token_preview) : "未配置") + "</span></td>"
      + "<td>" + fmtNum((p.health || {}).calls) + ' <span class="muted">/ ' + fmtNum((p.health || {}).fail) + "</span></td>"
      + '<td class="row-actions">'
      + '<button class="btn sm" data-edit="' + esc(p.id) + '">编辑</button>'
      + '<button class="btn sm" data-test="' + esc(p.id) + '">测试</button>'
      + '<label class="switch" style="margin-left:4px"><input type="checkbox" data-toggle="' + esc(p.id) + '" ' + (p.enabled ? "checked" : "") + '><span class="slider"></span></label>'
      + '<button class="btn sm danger" data-del="' + esc(p.id) + '">删除</button></td></tr>';
  }).join("");
  node.innerHTML = '<div class="card"><header><h3>服务提供方</h3><span class="spacer"></span>'
    + '<button class="btn sm" id="export-btn">导出</button>'
    + '<button class="btn sm" id="import-btn">导入</button>'
    + '<button class="btn sm primary" id="new-btn">' + icon("plus", 14) + "新建</button></header>"
    + '<table><thead><tr><th>名称</th><th>能力</th><th>优先级</th><th>密钥</th><th>调用</th><th></th></tr></thead><tbody>'
    + (rows || '<tr><td colspan="6"><div class="empty">还没有服务提供方。<br>点「新建」，把你的图片 / 视频 API 填进来即可。</div></td></tr>')
    + "</tbody></table>"
    + '<div class="hint">同一能力可以配多个提供方：按优先级排序，熔断中的会被自动跳过，健康的按轮转分摊——某个上游挂掉不影响整体可用。</div></div>';

  node.querySelectorAll("[data-toggle]").forEach(function (n) {
    n.onchange = async function () {
      const res = await api("/providers/" + encodeURIComponent(n.dataset.toggle) + "/toggle", { method: "POST", body: "{}" });
      toast(res.ok ? "已更新启用状态" : (res.message || res.error), res.ok ? "ok" : "bad");
      if (!res.ok) n.checked = !n.checked;
    };
  });
  node.querySelectorAll("[data-edit]").forEach(function (n) { n.onclick = function () { openProviderEditor(n.dataset.edit); }; });
  node.querySelectorAll("[data-del]").forEach(function (n) {
    n.onclick = function () {
      const pid = n.dataset.del;
      confirmBox("删除服务提供方", "删除 " + pid + " 后，绑定它的令牌会退回全局路由。此操作不可撤销。", async function () {
        const res = await api("/providers/" + encodeURIComponent(pid), { method: "DELETE" });
        toast(res.ok ? "已删除" : (res.message || res.error), res.ok ? "ok" : "bad");
        loadPage(true);
      });
    };
  });
  node.querySelectorAll("[data-test]").forEach(function (n) {
    n.onclick = async function () {
      const pid = n.dataset.test, orig = n.textContent;
      n.disabled = true; n.innerHTML = '<span class="spinner" style="width:12px;height:12px"></span>';
      const res = await api("/providers/" + encodeURIComponent(pid) + "/test", { method: "POST", body: "{}" });
      n.disabled = false; n.textContent = orig;
      openResult(("联通性自检 · " + pid), res);
    };
  });
  document.getElementById("new-btn").onclick = function () { openProviderEditor(null); };
  document.getElementById("export-btn").onclick = async function () {
    const res = await api("/providers/export");
    download("providers.json", JSON.stringify({ providers: res.providers || [] }, null, 2));
  };
  document.getElementById("import-btn").onclick = openImport;
}
function download(name, text) {
  const blob = new Blob([text], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
}
function openProviderEditor(pid) {
  const mask = el('<div class="mask"><div class="modal"><header><h3>服务提供方</h3><span class="spacer"></span>'
    + '<button class="btn ghost sm" id="json-mode">JSON</button><button class="btn ghost sm" id="close-x">✕</button></header>'
    + '<div class="tabs" id="tabs"></div><div class="body" id="modal-body"></div>'
    + '<footer><button class="btn ghost" id="cancel">取消</button><button class="btn primary" id="save">保存并生效</button></footer></div></div>');
  document.body.appendChild(mask);
  mask.querySelector("#close-x").onclick = closeModal;
  mask.querySelector("#cancel").onclick = closeModal;
  const state = { pid: pid, item: null, tab: "basic", json: false };
  api("/providers" + (pid ? "/" + encodeURIComponent(pid) : "/stub?preset=custom&id=" + encodeURIComponent("provider" + Math.random().toString(36).slice(2, 6))))
    .then(function (res) {
      state.item = res.item || { id: "", name: "", preset: "custom", enabled: true, base_url: "", auth: { kind: "bearer", token: "", name: "Authorization", prefix: "Bearer " }, limits: { rps: 1, burst: 2, cooldown_sec: 15 }, pool: {}, priority: 100, capabilities: {} };
      if (pid) { api("/providers/" + encodeURIComponent(pid)).then(function (r) { if (r.ok) state.item = r.item; drawTabs(); }); }
      else drawTabs();
    });
  function drawTabs() {
    const tabs = [["basic", "基础"], ["auth", "鉴权"], ["image", "生图"], ["video", "视频"], ["limits", "限速与连接"], ["json", "JSON"]];
    mask.querySelector("#tabs").innerHTML = tabs.map(function (t) {
      return '<div class="tab' + (state.tab === t[0] ? " active" : "") + '" data-tab="' + t[0] + '">' + t[1] + "</div>";
    }).join("");
    mask.querySelectorAll("[data-tab]").forEach(function (n) { n.onclick = function () { state.tab = n.dataset.tab; drawTabs(); }; });
    drawBody();
  }
  function f(name, value, hint, ph) {
    return '<label class="field"><span>' + name + '</span><input class="input" data-f="' + value + '" value="' + esc(ph === undefined ? "" : ph) + '">'
      + (hint ? '<div class="hint">' + hint + "</div>" : "") + "</label>";
  }
  function ensureCap(kind) {
    state.item.capabilities = state.item.capabilities || {};
    if (!state.item.capabilities[kind]) {
      state.item.capabilities[kind] = { enabled: true, model: "", async_poll: true,
        request: { method: "POST", path: kind === "image" ? "/v1/images/generations" : "/v1/videos", headers: {}, query: {}, body: {}, body_encoding: "json" },
        extract: { urls: "", task_id: "", status: "", url: "", b64: false, status_map: {}, error_message: "message" } };
    }
    return state.item.capabilities[kind];
  }
  function drawBody() {
    const it = state.item, body = mask.querySelector("#modal-body"), t = state.tab;
    if (t === "json") {
      body.innerHTML = '<label class="field"><span>完整配置（可直接粘贴导入格式）</span><textarea class="textarea" id="json-text" rows="20">' + esc(JSON.stringify(it, null, 2)) + "</textarea></label>";
      return;
    }
    if (t === "basic") {
      body.innerHTML = f("显示名称", "name", "", it.name)
        + f("唯一 ID", "id", "字母/数字/下划线/连字符，保存后不建议改", it.id)
        + f("基础地址 Base URL", "base_url", "以 http:// 或 https:// 开头，末尾斜杠会自动去掉", it.base_url)
        + '<label class="field"><span>预设 Preset</span><select class="select" data-f="preset">'
        + ["custom", "openai"].map(function (p) { return '<option value="' + p + '"' + (it.preset === p ? " selected" : "") + ">" + p + "</option>"; }).join("")
        + '</select><div class="hint">只影响表单默认值，实际行为由下面的请求模板决定</div></label>'
        + f("优先级 Priority", "priority", "数字越小越优先", it.priority)
        + '<div class="hint">提示：把 <code>{prompt}</code> 这类占位符写进下面的请求模板即可注入变量（<code>{{images}}</code> 是原样注入数组）。</div>';
    }
    if (t === "auth") {
      const a = it.auth || {};
      body.innerHTML = '<label class="field"><span>鉴权方式</span><select class="select" data-f="auth.kind">'
        + ["bearer", "header", "query", "none"].map(function (k) { return '<option value="' + k + '"' + (a.kind === k ? " selected" : "") + ">" + k + "</option>"; }).join("")
        + "</select></label>"
        + f("密钥 Token", "auth.token", "留空表示「不修改」；写成 <code>env:变量名</code> 可从环境变量读取（推荐，密钥不落配置文件）", a.token ? "（已保存，留空则不修改）" : "")
        + f("名称 Name", "auth.name", "header / query 模式下是键名", a.name)
        + f("前缀 Prefix", "auth.prefix", "bearer 模式默认 “Bearer&nbsp;”", a.prefix);
      return;
    }
    if (t === "image" || t === "video") {
      const isVideo = t === "video";
      const cap = ensureCap(t);
      const req = cap.request || {}, ex = cap.extract || {};
      body.innerHTML = '<label class="field" style="display:flex;align-items:center;gap:8px"><span style="margin:0">启用该能力</span>'
        + '<label class="switch"><input type="checkbox" data-f="cap.enabled" ' + (cap.enabled ? "checked" : "") + '><span class="slider"></span></label></label>'
        + f("模型名 Model", "cap.model", "会替换请求模板里的 <code>{model}</code>；留空则该键不注入", cap.model)
        + f("请求方法与路径", "cap.request.method", "", req.method) + ""
        + f("路径 Path", "cap.request.path", "支持占位符，如 <code>/v1/videos/{task_id}</code>", req.path)
        + '<label class="field"><span>请求体模板（JSON）</span><textarea class="textarea" data-area="cap.request.body" rows="10">'
        + esc(typeof req.body === "string" ? req.body : JSON.stringify(req.body || {}, null, 2)) + "</textarea></label>"
        + (isVideo ? '<label class="field"><span>轮询端点 Path</span><input class="input" data-f="cap.poll.path" value="' + esc(((cap.poll || {}).path) || "") + '"><div class="hint">留空表示「提交即出结果」，不做轮询</div></label>'
          + '<label class="field"><span>轮询查询参数（JSON 对象）</span><textarea class="textarea" data-area="cap.poll.query" rows="4">' + esc(JSON.stringify(((cap.poll || {}).query) || {}, null, 2)) + "</textarea></label>"
          : "")
        + '<label class="field"><span>' + (isVideo ? "任务号抽取路径" : "图片地址抽取路径") + '</span>'
        + '<input class="input" data-f="cap.extract.' + (isVideo ? "task_id" : "urls") + '" placeholder="' + (isVideo ? "留空=自动猜测 id/video_id/task_id" : "留空=自动猜测 data[].url / images[].url") + '" value="' + esc(isVideo ? (ex.task_id || "") : (ex.urls || "")) + '"></label>'
        + (isVideo ? '<label class="field"><span>状态抽取路径</span><input class="input" data-f="cap.extract.status" value="' + esc(ex.status || "") + '"><div class="hint">留空=自动猜测 status/state</div></label>'
          + '<label class="field"><span>结果地址抽取路径</span><input class="input" data-f="cap.extract.url" value="' + esc(ex.url || "") + '"></label>'
          + '<label class="field"><span>状态映射（JSON）</span><textarea class="textarea" data-area="cap.extract.status_map" rows="3">' + esc(JSON.stringify(ex.status_map || {}, null, 2)) + '</textarea><div class="hint">把上游的状态字符串映射成 running / completed / failed</div></label>'
          : '<label class="field" style="display:flex;align-items:center;gap:8px"><span style="margin:0">返回值是 base64</span>'
          + '<label class="switch"><input type="checkbox" data-f="cap.extract.b64" ' + (ex.b64 ? "checked" : "") + '><span class="slider"></span></label></label>');
      return;
    }
    if (t === "limits") {
      const l = it.limits || {}, p = it.pool || {};
      body.innerHTML = '<div class="grid two">'
        + '<div>' + f("每秒令牌数 rps", "limits.rps", "本地令牌桶，超出发前先排队", l.rps)
        + f("突发容量 burst", "limits.burst", "", l.burst)
        + f("过载冻结秒数", "limits.cooldown_sec", "收到 429/5xx 后冻结窗口", l.cooldown_sec) + "</div>"
        + '<div>' + f("最大连接数", "pool.max_connections", "0 = 用全局值", p.max_connections || 0)
        + f("保活连接数", "pool.max_keepalive_connections", "0 = 用全局值", p.max_keepalive_connections || 0)
        + f("读超时（秒）", "pool.read_sec", "0 = 用全局分档", p.read_sec || 0) + "</div></div>";
      return;
    }
  }
  function collect() {
    const it = state.item;
    if (state.tab === "json" || state.json) {
      try { return JSON.parse(mask.querySelector("#json-text").value); } catch (e) { toast("JSON 语法有误：" + e.message, "bad"); throw e; }
    }
    mask.querySelectorAll("[data-f]").forEach(function (n) {
      const path = n.dataset.f.split(".");
      let target = state.item, i;
      for (i = 0; i < path.length - 1; i++) {
        const key = path[i];
        // 能力字段作用域：cap.* 落在当前编辑的能力上
        if (key === "cap") { if (path[i + 1] === "enabled" || path[i + 1] === "model") { break; } }
        if (target[key] === undefined || target[key] === null) target[key] = {};
        target = target[key];
      }
      const lastKey = path[path.length - 1];
      if (path[0] === "cap" && path.length === 2) {
        const cap = ensureCap(state.tab); cap[lastKey === "enabled" ? "enabled" : "model"] = lastKey === "enabled" ? n.checked : n.value; return;
      }
      if (path[0] === "cap" && path[1] === "enabled") { ensureCap(state.tab).enabled = n.checked; return; }
      if (path[0] === "cap" && path[1] === "model") { ensureCap(state.tab).model = n.value; return; }
      if (path[0] === "cap" && path[1] === "extract" && path[2] === "b64") { ensureCap(state.tab).extract.b64 = n.checked; return; }
      if (path[0] === "cap" && path[1] === "poll") { ensureCap(state.tab).poll = ensureCap(state.tab).poll || {}; ensureCap(state.tab).poll[path[2]] = n.value; return; }
      if (path[0] === "cap" && path[1] === "request") { ensureCap(state.tab).request[path[2]] = n.value; return; }
      if (path[0] === "cap" && path[1] === "extract") { ensureCap(state.tab).extract[path[2]] = n.value; return; }
      const numeric = ["priority", "limits", "pool"];
      let value = n.type === "checkbox" ? n.checked : n.value;
      if (numeric.indexOf(path[0]) >= 0 && typeof value === "string" && value.trim() !== "" && !isNaN(Number(value))) value = Number(value);
      target[lastKey] = value;
    });
    mask.querySelectorAll("[data-area]").forEach(function (n) {
      const path = n.dataset.area.split(".");
      let value;
      try { value = n.value.trim() ? JSON.parse(n.value) : (path[path.length - 1] === "body" ? null : {}); }
      catch (e) { toast("模板 JSON 语法有误：" + e.message, "bad"); throw e; }
      if (path[0] === "cap") {
        const cap = ensureCap(state.tab);
        if (path[1] === "request") cap.request[path[2]] = value;
        if (path[1] === "poll") { cap.poll = cap.poll || {}; cap.poll[path[2]] = value; }
        if (path[1] === "extract") cap.extract[path[2]] = value;
        return;
      }
      state.item[path[0]] = value;
    });
    return state.item;
  }
  mask.querySelector("#save").onclick = async function () {
    let payload; try { payload = collect(); } catch (e) { return; }
    const id = state.pid || payload.id;
    const res = await api("/providers" + (state.pid ? "/" + encodeURIComponent(state.pid) : ""), { method: state.pid ? "PUT" : "POST", body: JSON.stringify(payload) });
    if (!res.ok) return toast(res.message || res.error, "bad");
    toast("已保存并热加载（无需重启）", "ok"); closeModal(); loadPage(true);
  };
}
function openResult(title, res) {
  const mask = el('<div class="mask"><div class="modal small"><header><h3>' + esc(title) + '</h3></header><div class="body">'
    + '<pre class="mono" style="margin:0;white-space:pre-wrap">' + esc(JSON.stringify(res, null, 2)) + "</pre></div>"
    + '<footer><button class="btn" data-close>关闭</button></footer></div></div>');
  document.body.appendChild(mask);
  mask.querySelector("[data-close]").onclick = closeModal;
  mask.onclick = function (e) { if (e.target === mask) mask.remove(); };
}
function openImport() {
  const mask = el('<div class="mask"><div class="modal small"><header><h3>导入服务提供方</h3></header><div class="body">'
    + '<label class="field"><span>粘贴导出内容（JSON）</span><textarea class="textarea" id="imp" rows="14" placeholder=\'{"providers": [...]}\'></textarea></label></div>'
    + '<footer><button class="btn ghost" data-no>取消</button><button class="btn primary" data-yes>导入</button></footer></div></div>');
  document.body.appendChild(mask);
  mask.querySelector("[data-no]").onclick = closeModal;
  mask.querySelector("[data-yes]").onclick = async function () {
    let parsed; try { parsed = JSON.parse(mask.querySelector("#imp").value); } catch (e) { return toast("JSON 语法有误", "bad"); }
    const res = await api("/providers/import", { method: "POST", body: JSON.stringify(parsed) });
    if (!res.ok) return toast(res.message || res.error, "bad");
    toast("导入完成：新增 " + res.added + " / 更新 " + res.updated, "ok"); closeModal(); loadPage(true);
  };
}

/* ---------------- tokens ---------------- */
async function viewTokens(node) {
  const res = await api("/tokens");
  if (!res.ok) throw new Error(res.message || res.error);
  const rows = (res.items || []).map(function (t) {
    return '<tr><td><b>' + esc(t.name) + '</b><div class="muted mono" style="font-size:11px">' + esc(t.id) + "</div></td>"
      + '<td class="mono">' + esc((t.token || "").slice(0, 8) + "…") + "</td>"
      + "<td>" + (t.allowed_capabilities || []).map(function (c) { return '<span class="badge brand">' + esc(c) + "</span>"; }).join(" ") + "</td>"
      + "<td>" + esc(t.default_provider || "-") + "</td>"
      + "<td>" + (t.max_concurrency || "不限") + "</td>"
      + "<td>" + fmtNum(t.use_count) + '<div class="muted" style="font-size:11px">' + fmtClock(t.last_used_at) + "</div></td>"
      + '<td class="row-actions"><button class="btn sm" data-view="' + esc(t.id) + '">查看密钥</button>'
      + '<button class="btn sm" data-edit="' + esc(t.id) + '">编辑</button>'
      + '<button class="btn sm" data-rotate="' + esc(t.id) + '">轮换</button>'
      + '<button class="btn sm danger" data-del="' + esc(t.id) + '">删除</button></td></tr>';
  }).join("");
  node.innerHTML = '<div class="card"><header><h3>访问令牌</h3><span class="spacer"></span>'
    + '<button class="btn sm primary" id="new-tok">' + icon("plus", 14) + "签发令牌</button></header>"
    + '<table><thead><tr><th>名称</th><th>令牌</th><th>允许能力</th><th>默认提供方</th><th>并发上限</th><th>用量</th><th></th></tr></thead><tbody>'
    + (rows || '<tr><td colspan="7"><div class="empty">还没有令牌</div></td></tr>') + "</tbody></table>"
    + '<div class="hint">一个令牌 = 一个客户端 / 一个工作空间。给它设并发上限，就能保证某个会话再忙也不会把别人的额度挤干。</div></div>';
  document.getElementById("new-tok").onclick = function () { openTokenEditor(null); };
  node.querySelectorAll("[data-view]").forEach(function (n) { n.onclick = async function () {
    const r = await api("/tokens/" + encodeURIComponent(n.dataset.view)); openResult("令牌 " + n.dataset.view, r.item || r); }; });
  node.querySelectorAll("[data-edit]").forEach(function (n) { n.onclick = function () { openTokenEditor(n.dataset.edit); }; });
  node.querySelectorAll("[data-rotate]").forEach(function (n) { n.onclick = async function () {
    const id = n.dataset.rotate;
    const r = await api("/tokens/" + encodeURIComponent(id) + "/rotate", { method: "POST", body: "{}" });
    if (r.ok) { openResult("新令牌（旧令牌立即失效）", r.item); loadPage(true); } else toast(r.message || r.error, "bad"); }; });
  node.querySelectorAll("[data-del]").forEach(function (n) { n.onclick = function () {
    const id = n.dataset.del;
    confirmBox("删除令牌", "使用该令牌的客户端会立即无法连接（401）。", async function () {
      const r = await api("/tokens/" + encodeURIComponent(id), { method: "DELETE" });
      toast(r.ok ? "已删除" : (r.message || r.error), r.ok ? "ok" : "bad"); loadPage(true); }); }; });
}
function openTokenEditor(tid) {
  const mask = el('<div class="mask"><div class="modal small"><header><h3>访问令牌</h3></header><div class="body" id="tok-body"></div>'
    + '<footer><button class="btn ghost" data-no>取消</button><button class="btn primary" data-yes>保存</button></footer></div></div>');
  document.body.appendChild(mask);
  mask.querySelector("[data-no]").onclick = closeModal;
  const body = mask.querySelector("#tok-body");
  if (tid) {
    api("/tokens/" + encodeURIComponent(tid)).then(function (r) { const t = r.item || {};
      body.innerHTML = '<label class="field"><span>名称</span><input class="input" data-k="name" value="' + esc(t.name) + '"></label>'
        + '<label class="field"><span>绑定默认提供方</span><input class="input" data-k="default_provider" value="' + esc(t.default_provider || "") + '"><div class="hint">留空=按全局优先级路由</div></label>'
        + '<label class="field"><span>并发上限（0=不限）</span><input class="input" data-k="max_concurrency" value="' + esc(t.max_concurrency || 0) + '"></label>'
        + '<label class="field"><span>备注</span><input class="input" data-k="note" value="' + esc(t.note || "") + '"></label>';
      mask.querySelector("[data-yes]").onclick = async function () {
        const payload = {}; body.querySelectorAll("[data-k]").forEach(function (n) { payload[n.dataset.k] = n.value; });
        if (payload.max_concurrency !== undefined) payload.max_concurrency = Number(payload.max_concurrency) || 0;
        const res = await api("/tokens/" + encodeURIComponent(tid), { method: "PUT", body: JSON.stringify(payload) });
        if (!res.ok) return toast(res.message || res.error, "bad");
        toast("已保存", "ok"); closeModal(); loadPage(true); };
    });
  } else {
    body.innerHTML = '<label class="field"><span>令牌 ID（用于识别）</span><input class="input" data-k="id" placeholder="例如 laptop / agent-a"></label>'
      + '<label class="field"><span>名称</span><input class="input" data-k="name" placeholder="可选"></label>'
      + '<label class="field"><span>绑定默认提供方</span><input class="input" data-k="default_provider" placeholder="留空=全局路由"></label>'
      + '<label class="field"><span>并发上限（0=不限）</span><input class="input" data-k="max_concurrency" value="0"></label>';
    mask.querySelector("[data-yes]").onclick = async function () {
      const payload = {}; body.querySelectorAll("[data-k]").forEach(function (n) { payload[n.dataset.k] = n.value; });
      const res = await api("/tokens", { method: "POST", body: JSON.stringify(payload) });
      if (!res.ok) return toast(res.message || res.error, "bad");
      openResult("新建成功 · 令牌只显示一次", res.item); closeModal(); loadPage(true); };
  }
}

/* ---------------- tasks ---------------- */
async function viewTasks(node) {
  const res = await api("/tasks?limit=100");
  if (!res.ok) throw new Error(res.message || res.error);
  const rows = (res.items || []).map(function (t) {
    const kind = t.status === "completed" ? "ok" : (t.status === "failed" ? "bad" : "warn");
    return "<tr><td class='mono'>" + esc(t.task_id) + "</td><td><span class='badge " + kind + "'>" + esc(t.status) + "</span></td>"
      + "<td>" + esc(t.mode || "-") + "</td><td class='mono'>" + esc(t.upstream_id || "-") + "</td>"
      + "<td>" + esc(t.elapsed) + "s</td><td>" + esc(t.submit_attempts || "-") + "</td>"
      + "<td class='muted' style='max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>" + esc(t.url || "-") + "</td></tr>";
  }).join("");
  node.innerHTML = '<div class="card"><header><h3>视频任务</h3><span class="spacer"></span><span class="muted" style="font-size:12px">每 5 秒自动刷新</span></header>'
    + '<table><thead><tr><th>任务号</th><th>状态</th><th>模式</th><th>上游任务号</th><th>已耗时</th><th>提交尝试</th><th>结果地址</th></tr></thead><tbody>'
    + (rows || '<tr><td colspan="7"><div class="empty">暂无视频任务</div></td></tr>') + "</tbody></table></div>";
}

/* ---------------- settings ---------------- */
async function viewSettings(node) {
  const res = await api("/settings");
  if (!res.ok) throw new Error(res.message || res.error);
  const hot = res.hot || {}, ro = res.readonly || {};
  const groups = [
    ["生图并发", [["image_max_concurrency", "同时进行的生图请求数"], ["image_queue_wait_sec", "拿不到槽时最多等多久"], ["image_queue_max", "排队深度上限（超出即快速失败）"], ["image_batch_concurrency", "单请求内 batch 拆分的并发度"]]],
    ["视频并发", [["video_max_concurrency", "视频提交并发"], ["video_queue_wait_sec", "视频排队等待"], ["video_queue_max", "视频排队深度"], ["video_poll_interval_sec", "后台轮询间隔"], ["video_job_max", "同时跟踪的后台作业数"], ["video_job_concurrency", "后台轮询并发"], ["video_job_max_wait_sec", "后台作业最长跟踪时间"]]],
    ["上游排队", [["video_submit_queue_wait_sec", "上游队列满时的代排队窗口"], ["video_submit_retry_min_sec", "重试最小退避"], ["video_submit_retry_max_sec", "重试最大退避"]]],
    ["参考图与上传", [["reference_max_mb", "单张参考图上限(MB)"], ["image_reference_max", "生图最多几张参考图"], ["video_reference_max", "视频最多几张参考图"], ["upload_max_mb", "单次上传上限(MB)"]]],
  ];
  const form = groups.map(function (g) {
    const rows = g[1].map(function (row) {
      return '<label class="field"><span>' + esc(row[1]) + '</span><input class="input" data-k="' + row[0] + '" value="' + esc(hot[row[0]]) + '"></label>';
    }).join("");
    return '<div class="card"><header><h3>' + esc(g[0]) + "</h3></header>" + rows + "</div>";
  }).join("");
  node.innerHTML = '<div class="grid two">' + form + "</div>"
    + '<div class="card" style="margin-top:16px"><header><h3>只读参数（需改环境变量后重启）</h3></header>'
    + '<table><tbody>' + Object.keys(ro).map(function (k) { return "<tr><th style='border:0;padding-left:0'>" + esc(k) + "</th><td class='mono'>" + esc(ro[k]) + "</td></tr>"; }).join("") + "</tbody></table></div>"
    + '<div style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end"><button class="btn primary" id="save-settings">保存（立即生效）</button></div>';
  document.getElementById("save-settings").onclick = async function () {
    const patch = {};
    node.querySelectorAll("[data-k]").forEach(function (n) {
      const v = n.value.trim();
      patch[n.dataset.k] = v === "" ? null : (isNaN(Number(v)) ? v : Number(v));
    });
    const res = await api("/settings", { method: "PUT", body: JSON.stringify(patch) });
    toast(res.ok ? "已保存并生效" : (res.message || res.error), res.ok ? "ok" : "bad");
    if (res.ok) loadPage(true);
  };
}

/* ---------------- logs ---------------- */
async function viewLogs(node) {
  const level = store.logLevel || "";
  const res = await api("/logs?limit=200" + (level ? "&level=" + level : ""));
  if (!res.ok) throw new Error(res.message || res.error);
  const rows = (res.items || []).reverse().map(function (r) {
    const kind = r.level === "ERROR" || r.level === "CRITICAL" ? "bad" : (r.level === "WARNING" ? "warn" : "");
    return "<tr><td class='mono' style='width:90px'>" + fmtTime(r.ts) + "</td>"
      + "<td style='width:86px'><span class='badge " + kind + "'>" + esc(r.level) + "</span></td>"
      + "<td class='muted' style='width:150px'>" + esc(r.logger.replace("mcp_imagegen.", "")) + "</td>"
      + "<td class='mono' style='white-space:pre-wrap;word-break:break-word'>" + esc(r.message) + "</td></tr>";
  }).join("");
  node.innerHTML = '<div class="card"><header><h3>服务日志</h3><span class="spacer"></span>'
    + '<select class="select" id="level" style="width:130px">' + ["", "INFO", "WARNING", "ERROR"].map(function (l) {
      return '<option value="' + l + '"' + (level === l ? " selected" : "") + ">" + (l || "全部级别") + "</option>"; }).join("")
    + "</select></header>"
    + '<table><thead><tr><th>时间</th><th>级别</th><th>来源</th><th>内容</th></tr></thead><tbody>'
    + (rows || '<tr><td colspan="4"><div class="empty">暂无日志</div></td></tr>') + "</tbody></table>"
    + '<div class="hint">仅保留最近 300 条（内存环形缓冲），完整日志看 journald / 标准输出。</div></div>';
  document.getElementById("level").onchange = function (e) { store.logLevel = e.target.value; loadPage(true); };
}

/* ---------------- boot ---------------- */
async function bootstrap() {
  try {
    const res = await fetch(API + "/state", { credentials: "same-origin" });
    const data = await res.json();
    if (data.ok !== true && !data.initialized) throw new Error(data.message || "unreachable");
    store.state = data;
    render();
    const hash = (location.hash || "").replace("#/", "");
    if (hash) { store.page = hash; }
  } catch (e) {
    document.getElementById("root").innerHTML = '<div class="center-screen"><div class="card"><div class="empty">无法连接管理服务：' + esc(e.message || e) + "</div></div></div>";
  }
}
window.addEventListener("hashchange", route);
bootstrap();
</script>
</body>
</html>
"""

PAGE_HTML = _PAGE
