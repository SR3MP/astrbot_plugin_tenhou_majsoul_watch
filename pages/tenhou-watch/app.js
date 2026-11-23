// 天凤/雀魂云观战 - 管理页前端
const bridge = window.AstrBotPluginPage || window.AstrBotPluginView;

function parsePluginEndpoint(url) {
  const [rawPath, rawQuery = ""] = String(url).split("?", 2);
  const endpoint = rawPath.replace(/^\/?api\/?/, "").replace(/^\/+|\/+$/g, "");
  const params = {};
  for (const [k, v] of new URLSearchParams(rawQuery)) params[k] = v;
  return { endpoint, params };
}
const API = {
  async get(url) {
    const { endpoint, params } = parsePluginEndpoint(url);
    return bridge.apiGet(endpoint, Object.keys(params).length ? params : undefined);
  },
  async post(url, data) {
    const { endpoint } = parsePluginEndpoint(url);
    return bridge.apiPost(endpoint, data || {});
  },
};

// ---------- 通用工具 ----------
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
// 生成可直接嵌进双引号 HTML 属性的 JS 字面量：
// 先 JSON.stringify 得到合法 JS 字符串，再把 " & < > 转义成 HTML 实体，
// 否则 onclick="fn("名字")" 会被属性引号截断，点击直接失效。
function jsArg(s) {
  return JSON.stringify(String(s == null ? "" : s))
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function toast(msg, type) {
  const e = document.createElement("div");
  e.className = "toast" + (type === "error" ? " toast-error" : "");
  e.textContent = msg;
  document.body.appendChild(e);
  setTimeout(() => e.remove(), 3200);
}
function loading(c) { c.innerHTML = '<div class="page-loading"><span class="mdi mdi-loading mdi-spin"></span><p>加载中...</p></div>'; }

let dialogResolve = null, dialogMode = "confirm";
function showDialog({ title, label = "", mode = "confirm", value = "" }) {
  document.getElementById("page-dialog-title").textContent = title || "";
  document.getElementById("page-dialog-label").textContent = label || "";
  const wrap = document.getElementById("page-dialog-input-wrap");
  const input = document.getElementById("page-dialog-input");
  wrap.style.display = mode === "prompt" ? "block" : "none";
  input.value = value || "";
  dialogMode = mode;
  document.getElementById("page-dialog").classList.add("show");
  if (mode === "prompt") setTimeout(() => input.focus(), 0);
  return new Promise(res => { dialogResolve = res; });
}
function promptBox(title, label, value) { return showDialog({ title, label, mode: "prompt", value }); }
function confirmBox(title) { return showDialog({ title, mode: "confirm" }); }
window.resolvePageDialog = ok => {
  document.getElementById("page-dialog").classList.remove("show");
  if (!dialogResolve) return;
  const res = dialogResolve, input = document.getElementById("page-dialog-input");
  dialogResolve = null;
  res(ok ? (dialogMode === "prompt" ? input.value.trim() : true) : null);
};

// 当前选中的群 session（订阅/定制文案页共享）
let currentSession = "";
function setSessionOptions(sessions, labelOf) {
  const sel = document.getElementById("group-select");
  if (!sel) return;
  if (!sessions.length) {
    sel.innerHTML = '<option value="">（暂无订阅群）</option>';
    currentSession = "";
    return;
  }
  sel.innerHTML = sessions.map(s =>
    `<option value="${esc(s)}">${esc(labelOf ? labelOf(s) : s)}</option>`).join("");
  if (!sessions.includes(currentSession)) currentSession = sessions[0];
  sel.value = currentSession;
}
function shortLabel(s) {
  const p = String(s || "").split(":");
  return p.length ? p[p.length - 1] : String(s);
}

// ---------- 导航 ----------
document.querySelectorAll(".nav-item").forEach(it => {
  it.addEventListener("click", () => navigateTo(it.dataset.page));
});
async function navigateTo(page) {
  document.querySelectorAll(".nav-item").forEach(i => i.classList.toggle("active", i.dataset.page === page));
  const c = document.getElementById("content");
  loading(c);
  const pages = {
    overview: renderOverview, subs: renderSubs, custom: renderCustom,
    report: renderReport, config: renderConfig,
  };
  try { await (pages[page] || renderOverview)(); }
  catch (e) {
    c.innerHTML = `<div class="card"><div class="empty">加载失败：${esc(e.message || e)}<br><br><button class="btn btn-primary" onclick="navigateTo(${jsArg(page)})">重试</button></div></div>`;
  }
}

// ---------- 状态总览 ----------
async function renderOverview() {
  const d = await API.get("/api/overview");
  const c = document.getElementById("content");
  const msOk = d.ms_login === "已登录";
  c.innerHTML = `
    <div class="page-title">状态总览</div>
    <div class="page-sub">监控运行状态与订阅规模一览</div>
    <div class="stats-grid">
      <div class="stat-card ${d.th_watching ? "ok" : "warn"}"><div class="stat-value">${d.th_watching ? "运行中" : "停止"}</div><div class="stat-label">天凤监控</div></div>
      <div class="stat-card ${d.ms_watching ? "ok" : "warn"}"><div class="stat-value">${d.ms_watching ? "运行中" : "停止"}</div><div class="stat-label">雀魂监控</div></div>
      <div class="stat-card ${msOk ? "ok" : "warn"}"><div class="stat-value" style="font-size:${msOk ? 28 : 16}px">${esc(d.ms_login)}</div><div class="stat-label">雀魂账号</div></div>
      <div class="stat-card"><div class="stat-value">${d.sessions}</div><div class="stat-label">订阅群</div></div>
      <div class="stat-card"><div class="stat-value">${d.th_players}</div><div class="stat-label">天凤玩家</div></div>
      <div class="stat-card"><div class="stat-value">${d.ms_accounts}</div><div class="stat-label">雀魂账号</div></div>
      <div class="stat-card"><div class="stat-value">${d.contests}</div><div class="stat-label">比赛场</div></div>
      <div class="stat-card"><div class="stat-value">${d.custom}</div><div class="stat-label">定制文案</div></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="mdi mdi-eye"></span>当前跟踪中的对局</div>
      <div class="stats-grid" style="margin-bottom:0">
        <div class="stat-card"><div class="stat-value">${d.th_live}</div><div class="stat-label">天凤 · 手动 wg</div></div>
        <div class="stat-card"><div class="stat-value">${d.th_auto}</div><div class="stat-label">天凤 · 订阅自动</div></div>
        <div class="stat-card"><div class="stat-value">${d.ms_live}</div><div class="stat-label">雀魂 · 手动 wg</div></div>
        <div class="stat-card"><div class="stat-value">${d.ms_auto}</div><div class="stat-label">雀魂 · 订阅自动</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="mdi mdi-chart-line"></span>PT 报告</div>
      <div class="row-between">
        <div>${d.report_enabled
          ? `已启用，每天 ${String(d.report_hour).padStart(2, "0")}:${String(d.report_minute).padStart(2, "0")} 推送`
          : "未启用（可在「PT 报告」页开启）"}</div>
        <button class="btn btn-sm" onclick="navigateTo('report')">前往设置</button>
      </div>
    </div>`;
}

// ---------- 订阅管理 ----------
let subsData = null;
async function renderSubs() {
  const d = await API.get("/api/subscriptions");
  subsData = d;
  const c = document.getElementById("content");
  const groups = d.groups || [];
  const labelOf = s => shortLabel(s);
  c.innerHTML = `
    <div class="page-title">订阅管理</div>
    <div class="page-sub">按群管理天凤玩家、雀魂账号、比赛场与筛选</div>
    <div class="group-bar">
      <label>选择群：</label>
      <select id="group-select" onchange="onSubGroupChange()"></select>
      <span class="muted" id="group-hint"></span>
      <button class="btn btn-sm" onclick="refreshSubs()">刷新</button>
    </div>
    <div id="subs-body"></div>`;
  setSessionOptions(groups.map(g => g.session), labelOf);
  renderSubsBody();
}
function onSubGroupChange() {
  currentSession = document.getElementById("group-select").value;
  renderSubsBody();
}
async function refreshSubs() { await renderSubs(); }
function currentGroup() {
  return (subsData?.groups || []).find(g => g.session === currentSession) || null;
}
function renderSubsBody() {
  const body = document.getElementById("subs-body");
  const g = currentGroup();
  const hint = document.getElementById("group-hint");
  if (!g) {
    body.innerHTML = '<div class="card"><div class="empty">没有订阅数据。请先在群里用 thadd / msadd 指令添加，再来这里管理。</div></div>';
    if (hint) hint.textContent = "";
    return;
  }
  if (hint) hint.textContent = `session: ${g.session}`;
  const thTags = g.th.players.length
    ? g.th.players.map(p => `<span class="tag">${esc(p)} <a href="javascript:void(0)" onclick="thDel(${jsArg(p)})" title="删除" style="color:var(--accent-red);text-decoration:none">✕</a></span>`).join("")
    : '<span class="muted">无</span>';
  const msRows = g.ms.accounts.length
    ? g.ms.accounts.map(a => `<tr><td class="mono">${a.aid}</td><td>${esc(a.name)}</td><td><button class="btn btn-sm btn-danger" onclick="msDel(${a.aid})">删除</button></td></tr>`).join("")
    : `<tr><td colspan="3" class="empty">无</td></tr>`;
  const contestTags = g.ms.contests.length
    ? g.ms.contests.map(c => `<span class="tag">${c} <a href="javascript:void(0)" onclick="contestDel(${c})" style="color:var(--accent-red);text-decoration:none">✕</a></span>`).join("")
    : '<span class="muted">无</span>';

  body.innerHTML = `
    <div class="card">
      <div class="card-title row-between"><span><span class="mdi mdi-silverware-fork-knife"></span>天凤订阅</span>
        <label class="switch" title="天凤推送开关"><input type="checkbox" ${g.th.enabled ? "checked" : ""} onchange="thToggle(this.checked)"><span class="slider"></span></label></div>
      <div style="margin-bottom:12px">${thTags}</div>
      <div class="form-row">
        <div class="form-group"><label class="form-label">添加天凤玩家名</label><input id="th-name" class="form-input" placeholder="玩家名"></div>
        <button class="btn btn-primary" onclick="thAdd()">添加</button>
      </div>
      <div class="form-group" style="margin-top:14px"><label class="form-label">场筛选（空格分隔，留空=不限，all=清空）</label>
        <div class="form-row"><div class="form-group"><input id="th-filter" class="form-input" value="${esc(g.th.filters.join(" "))}" placeholder="如: 东风 玉"></div><button class="btn" onclick="thFilter()">保存筛选</button></div></div>
    </div>

    <div class="card">
      <div class="card-title row-between"><span><span class="mdi mdi-gamepad-variant"></span>雀魂订阅</span>
        <label class="switch" title="雀魂推送开关"><input type="checkbox" ${g.ms.enabled ? "checked" : ""} onchange="msToggle(this.checked)"><span class="slider"></span></label></div>
      <table><thead><tr><th>AID</th><th>昵称</th><th>操作</th></tr></thead><tbody>${msRows}</tbody></table>
      <div class="form-row" style="margin-top:14px">
        <div class="form-group"><label class="form-label">添加雀魂 AID（纯数字）</label><input id="ms-aid" class="form-input" placeholder="账号 AID"></div>
        <button class="btn btn-primary" onclick="msAdd()">添加</button>
      </div>
      <div class="form-group" style="margin-top:14px"><label class="form-label">场筛选（空格分隔，留空/all=不限）</label>
        <div class="form-row"><div class="form-group"><input id="ms-filter" class="form-input" value="${esc(g.ms.filters.join(" "))}" placeholder="如: 金 玉 王座"></div><button class="btn" onclick="msFilter()">保存筛选</button></div></div>
    </div>

    <div class="card">
      <div class="card-title"><span class="mdi mdi-trophy"></span>比赛场登记</div>
      <div style="margin-bottom:12px">${contestTags}</div>
      <div class="form-row">
        <div class="form-group"><label class="form-label">比赛号（数字）</label><input id="contest-id" class="form-input" placeholder="比赛号"></div>
        <button class="btn btn-primary" onclick="contestAdd()">登记</button>
      </div>
    </div>`;
}

async function thAdd() {
  const v = document.getElementById("th-name").value.trim();
  if (!v) return toast("请输入玩家名", "error");
  try { await API.post("/api/th/add", { session: currentSession, name: v }); toast("已添加"); await refreshSubs(); }
  catch (e) { toast(e.message, "error"); }
}
async function thDel(name) {
  if (!await confirmBox(`删除天凤订阅「${name}」？`)) return;
  try { await API.post("/api/th/del", { session: currentSession, name }); toast("已删除"); await refreshSubs(); }
  catch (e) { toast(e.message, "error"); }
}
async function thFilter() {
  const v = document.getElementById("th-filter").value.trim();
  try { await API.post("/api/th/filter", { session: currentSession, filters: v }); toast("筛选已保存"); await refreshSubs(); }
  catch (e) { toast(e.message, "error"); }
}
async function thToggle(enabled) {
  try { await API.post("/api/th/toggle", { session: currentSession, enabled }); toast(enabled ? "天凤推送已开启" : "天凤推送已关闭"); }
  catch (e) { toast(e.message, "error"); }
}
async function msAdd() {
  const v = document.getElementById("ms-aid").value.trim();
  if (!/^\d+$/.test(v)) return toast("请输入纯数字 AID", "error");
  try { await API.post("/api/ms/add", { session: currentSession, aid: v }); toast("已添加"); await refreshSubs(); }
  catch (e) { toast(e.message, "error"); }
}
async function msDel(aid) {
  if (!await confirmBox(`删除雀魂订阅 ${aid}？`)) return;
  try { await API.post("/api/ms/del", { session: currentSession, aid: String(aid) }); toast("已删除"); await refreshSubs(); }
  catch (e) { toast(e.message, "error"); }
}
async function msFilter() {
  const v = document.getElementById("ms-filter").value.trim();
  try { await API.post("/api/ms/filter", { session: currentSession, filters: v }); toast("筛选已保存"); await refreshSubs(); }
  catch (e) { toast(e.message, "error"); }
}
async function msToggle(enabled) {
  try { await API.post("/api/ms/toggle", { session: currentSession, enabled }); toast(enabled ? "雀魂推送已开启" : "雀魂推送已关闭"); }
  catch (e) { toast(e.message, "error"); }
}
async function contestAdd() {
  const v = document.getElementById("contest-id").value.trim();
  if (!/^\d+$/.test(v)) return toast("请输入数字比赛号", "error");
  try { await API.post("/api/ms/contest/add", { session: currentSession, contest: v }); toast("已登记"); await refreshSubs(); }
  catch (e) { toast(e.message, "error"); }
}
async function contestDel(c) {
  if (!await confirmBox(`取消登记比赛 ${c}？`)) return;
  try { await API.post("/api/ms/contest/del", { session: currentSession, contest: String(c) }); toast("已取消"); await refreshSubs(); }
  catch (e) { toast(e.message, "error"); }
}

// ---------- 定制文案 ----------
const CT_NODES = [
  { key: "start", label: "对局开始", ph: "{name} {player} {who} {game} {mid} {time} {lead} {head} {seats} {body} {raw}" },
  { key: "round", label: "小局/流局", ph: "{name} {mid} {time} {head} {hule} {detail} {scores} {body} {raw}" },
  { key: "big",   label: "大牌通报", ph: "{name} {mid} {time} {kind} {gain} {desc} {mood} {hule} {head} {raw}" },
  { key: "finish",label: "对局完成", ph: "{name} {place} {total} {score} {pt} {heads} {head} {scores} {body} {raw}" },
];
const nodeLabel = k => (CT_NODES.find(n => n.key === k) || {}).label || k;

// 占位符详细说明：按「适用节点」分组，node 为空表示 4 个节点通用。
// 每项 = [占位符, 同义写法, 含义, 真实示例]
const PH_GROUPS = [
  {
    node: "", title: "通用 · 4 个节点都能用",
    items: [
      ["{name}", "{player}", "本次播报命中的玩家名，就是你上面填的那个玩家；按播报原文取，原文怎么写就怎么取。", "虎皮猫"],
      ["{mid}", "", "对局 ID，取标题行 # 后面那一串；不带 ID 的播报（如牌谱回放）为空。", "mc361"],
      ["{time}", "", "播报时间 HH:MM；不带时间的节点为空字符串。", "12:06"],
      ["{head}", "", "标题行 = 整条播报的第一行（含 #ID 和【节点名】）。", "#mc361【大牌通报】12:06"],
      ["{body}", "", "正文 = 除标题行以外的全部内容，保留原有换行。", "东4局 0本場 虎皮猫 自摸 26000 8番20符，笑嘻了。"],
      ["{raw}", "", "原始整条播报，含标题行与换行、不做任何拆分；整条只写 {raw} 等于不修改。", "#mc361【大牌通报】12:06↵东4局 …"],
    ],
  },
  {
    node: "big", title: "大牌通报 · 和出 / 点出分档",
    items: [
      ["{kind}", "", "本条属于哪一类：和出 = 你自己胡牌，点出 = 你自己放铳。", "和出"],
      ["{gain}", "", "本局点数收支，取绝对值、纯数字、不带「点」：和出是你赢的点数，点出是你输的点数。分档的上下限就是按这个数比。", "26000"],
      ["{desc}", "", "和牌描述行，已去掉结尾的心情短语；含 局名 + 本場 + 和牌家 + 自摸/荣和 + 点数 + 番符。", "东4局 0本場 虎皮猫 自摸 26000 8番20符"],
      ["{mood}", "", "结尾心情短语，来自「大牌通报」点数规则里配的那句文案（如 笑嘻了 / 急哭了）；规则只配了纯描述时为空。", "笑嘻了"],
      ["{hule}", "", "和牌行：大牌节点里与 {desc} 相同；小局节点里是本局第一条和牌行或流局行。", "东4局 0本場 虎皮猫 自摸 26000 8番20符"],
    ],
  },
  {
    node: "round", title: "小局 / 流局 · 每局结算",
    items: [
      ["{hule}", "", "本局第一条和牌行；流局时是流局行。", "东1局 0本場 虎皮猫 荣和 Miaonana7 12000"],
      ["{detail}", "", "和牌细节的缩进行（手牌 / 副露 / 役与番符）；流局时是听牌名单。", "听牌: 虎皮猫 Miaonana7"],
      ["{scores}", "", "本局结束后的逐家点数行，一行一人。", "虎皮猫 33000 -> 45000"],
    ],
  },
  {
    node: "start", title: "对局开始 · 开局播报",
    items: [
      ["{game}", "", "场次名（规则 + 场数 + 赤牌等），取【对局开始】后的那段。", "四般南喰赤"],
      ["{who}", "", "同 {name}，本次播报的主角。", "虎皮猫"],
      ["{lead}", "", "整条播报的首行原文（订阅抬头那类行），不做任何加工。", "雀魂订阅 虎皮猫:"],
      ["{seats}", "", "座位表区块，含上下分隔线，原样多行。", "────↵东 虎皮猫 25000↵────"],
    ],
  },
  {
    node: "finish", title: "对局完成 · 终局总结",
    items: [
      ["{place}", "{rank} {total}", "最终名次，1~4；{rank}、{total} 是同一个值的别名。", "2"],
      ["{score}", "{point}", "终局点数，原文就带千分位逗号，不做数字加工。", "32,400"],
      ["{pt}", "{delta}", "本场 PT 增减，正数带 +、可能带小数。", "+12.4"],
      ["{heads}", "", "【对局完成】标记之前的那几行，即各家名次点评行，原样多行。", "虎皮猫 2位(+12.4)"],
      ["{scores}", "", "终局逐家点数行（含名次括号），一行一人。", "虎皮猫 32400(+12.4)"],
    ],
  },
];

function renderPhHelp(node) {
  const rows = [];
  for (const g of PH_GROUPS) {
    const hit = !!g.node && g.node === node;
    rows.push(`<tr class="ph-group${hit ? " hit" : ""}" data-nodes="${g.node || ""}"><td colspan="3">${esc(g.title)}${hit ? ' <span class="tag on">当前节点</span>' : ""}</td></tr>`);
    for (const it of g.items) {
      const alias = it[1] ? ` <span class="ph-alias">同 ${esc(it[1])}</span>` : "";
      rows.push(`<tr class="ph-row${hit ? " hit" : ""}" data-nodes="${g.node || ""}">
        <td class="ph-key mono">${esc(it[0])}${alias}</td>
        <td class="ph-desc">${esc(it[2])}</td>
        <td class="ph-eg mono">${esc(it[3])}</td></tr>`);
    }
  }
  return `<div class="ph-help" id="ct-ph-help">
    <div class="ph-help-head">占位符详细说明 <span class="muted">（写进模板后会在播报时替换成实际内容；认不出的占位符原样输出，不会报错）</span></div>
    <table class="ph-table"><thead><tr><th style="width:200px">占位符</th><th>含义</th><th style="width:250px">实际例子</th></tr></thead>
    <tbody>${rows.join("")}</tbody></table>
    <div class="ph-help-foot muted">
      模板留空 = 该节点不套用定制（用回默认播报）；模板里可以换行，播报按原样换行输出。
      大牌通报 / 对局完成的分档编辑器里，命中的那一档模板就是整条播报内容。
    </div>
  </div>`;
}

// 当前节点对应的分组加「当前节点」标记 + 高亮，其余保持可读（不强隐）
function markPhHelp(node) {
  document.querySelectorAll("#ct-ph-help tr[data-nodes]").forEach(tr => {
    const n = tr.getAttribute("data-nodes") || "";
    tr.classList.toggle("hit", !!n && n === node);
  });
  document.querySelectorAll("#ct-ph-help .tag.on").forEach(t => t.remove());
  const head = document.querySelector(`#ct-ph-help tr.ph-group[data-nodes="${node}"] td`);
  if (head) head.insertAdjacentHTML("beforeend", ' <span class="tag on">当前节点</span>');
}
let customData = null;
let ctEditing = null; // {name, node} 正在编辑的条目

function normalizeEntry(v) {
  if (v && typeof v === "object" && !Array.isArray(v)) return v;
  return v ? { finish: String(v) } : {};
}

// ---- 大牌通报：和出/点出 × 点数分档 规则编辑器 ----
const BIG_KINDS = [{ key: "和出", label: "和出（自己胡牌）" },
                   { key: "点出", label: "点出（自己放铳）" }];
function bigRuleRow(kind, r, i) {
  r = r || {};
  return `<div class="big-rule-row" data-kind="${kind}" data-i="${i}">
    <div class="form-row" style="align-items:flex-end">
      <div class="form-group" style="max-width:150px"><label class="form-label">下限 ≥</label>
        <input class="form-input br-min" value="${esc(r.min || "")}" placeholder="留空不限"></div>
      <div class="form-group" style="max-width:150px"><label class="form-label">上限 &lt;</label>
        <input class="form-input br-max" value="${esc(r.max || "")}" placeholder="留空不限"></div>
      <div class="form-group" style="flex:1"><label class="form-label">文案模板</label>
        <input class="form-input br-text" value="${esc(r.text || "")}" placeholder="{name}自摸{gain}点，太狠了"></div>
      <button class="btn btn-sm btn-danger" onclick="bigDelRow('${kind}',${i})" title="删除该档">✕</button>
    </div>
  </div>`;
}
function bigRenderEditor(value) {
  // value 可能是结构化 {和出:[...],点出:[...]}、旧字符串、或空
  const data = { "和出": [], "点出": [] };
  if (value && typeof value === "object" && !Array.isArray(value)) {
    data["和出"] = Array.isArray(value["和出"]) ? value["和出"] : [];
    data["点出"] = Array.isArray(value["点出"]) ? value["点出"] : [];
    if (!data["和出"].length && !data["点出"].length && Array.isArray(value["两者"])) {
      // 旧「两者」规则：同时放进和出/点出
      data["和出"] = value["两者"].slice(); data["点出"] = value["两者"].slice();
    }
  } else if (typeof value === "string" && value.trim()) {
    data["和出"] = [{ min: "", max: "", text: value }];
    data["点出"] = [{ min: "", max: "", text: value }];
  }
  const box = document.getElementById("ct-big-editor");
  if (!box) return;
  box.innerHTML = BIG_KINDS.map(k => {
    const rules = data[k.key];
    const rows = rules.length
      ? rules.map((r, i) => bigRuleRow(k.key, r, i)).join("")
      : `<div class="muted" style="padding:6px 0">暂无档位，点「+ 添加一档」新建</div>`;
    return `<div class="big-kind-block">
      <div class="big-kind-title">${k.label}
        <button class="btn btn-sm" onclick="bigAddRow('${k.key}')">+ 添加一档</button>
      </div>
      <div class="big-kind-rows" data-kind="${k.key}">${rows}</div>
    </div>`;
  }).join("");
}
function bigAddRow(kind) {
  // 先把当前 DOM 里的行读回内存，再加一行（避免覆盖已输入内容）
  const snapshot = bigCollect();
  if (!snapshot[kind]) snapshot[kind] = [];
  snapshot[kind].push({ min: "", max: "", text: "" });
  bigRenderEditor(snapshot);
}
function bigDelRow(kind, i) {
  const snapshot = bigCollect();
  if (snapshot[kind]) snapshot[kind].splice(i, 1);
  bigRenderEditor(snapshot);
}
function bigCollect() {
  const out = { "和出": [], "点出": [] };
  document.querySelectorAll("#ct-big-editor .big-rule-row").forEach(row => {
    const kind = row.getAttribute("data-kind");
    const min = row.querySelector(".br-min").value.trim();
    const max = row.querySelector(".br-max").value.trim();
    const text = row.querySelector(".br-text").value.trim();
    if (text) out[kind].push({ min, max, text });
  });
  return out;
}
function bigSummary(value) {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "";
  const lines = [];
  for (const k of BIG_KINDS) {
    const arr = Array.isArray(value[k.key]) ? value[k.key] : [];
    if (!arr.length) continue;
    lines.push(`【${k.key}】${arr.length} 档`);
    arr.forEach(r => {
      const lo = (r.min === "" || r.min == null) ? "−∞" : r.min;
      const hi = (r.max === "" || r.max == null) ? "+∞" : r.max;
      lines.push(`  [${lo}, ${hi})  ${r.text}`);
    });
  }
  if (!lines.length && Array.isArray(value["两者"]) && value["两者"].length) {
    lines.push(`【两者通用】${value["两者"].length} 档`);
    value["两者"].forEach(r => lines.push(`  [${r.min || "−∞"}, ${r.max || "+∞"})  ${r.text}`));
  }
  return lines.join("\n") || "(空规则)";
}
// ---- 对局完成：按名次(1/2/3/4/通用)分档 编辑器 ----
const FINISH_RANKS = [
  { key: "1", label: "1位" },
  { key: "2", label: "2位" },
  { key: "3", label: "3位" },
  { key: "4", label: "4位" },
  { key: "*", label: "通用（未单独配置的名次回退到这里）" },
];
function finishRenderEditor(value) {
  // value 可能是 {1:...,"*":...}、旧字符串（视为通用）、或空
  const data = {};
  if (typeof value === "string" && value.trim()) data["*"] = value;
  else if (value && typeof value === "object") {
    for (const r of FINISH_RANKS) {
      const v = value[r.key];
      if (typeof v === "string" && v.trim()) data[r.key] = v;
    }
  }
  const box = document.getElementById("ct-finish-editor");
  if (!box) return;
  box.innerHTML = FINISH_RANKS.map(r => `<div class="finish-rank-block">
      <label class="form-label">${esc(r.label)}</label>
      <textarea class="form-input fr-text" data-rank="${r.key}" rows="2" placeholder="${r.key === "*" ? "任意名次没单独配时回退到这条" : "留空则该名次回退到通用"}">${esc(data[r.key] || "")}</textarea>
    </div>`).join("");
}
function finishCollect() {
  const out = {};
  document.querySelectorAll("#ct-finish-editor .fr-text").forEach(t => {
    const v = t.value.trim();
    if (v) out[t.getAttribute("data-rank")] = v;
  });
  return out;
}
function finishSummary(value) {
  if (typeof value === "string") return `通用: ${value}`;
  if (!value || typeof value !== "object") return "";
  const lab = { "1": "1位", "2": "2位", "3": "3位", "4": "4位", "*": "通用" };
  const lines = [];
  for (const k of ["1", "2", "3", "4", "*"]) {
    const v = value[k];
    if (typeof v === "string" && v.trim()) lines.push(`${lab[k]}: ${v}`);
  }
  return lines.join("\n") || "(空)";
}
// ---- 玩家名下拉（当前群订阅玩家）+ 选中后自动加载已有配置 ----
function updateCtPlayers() {
  const dl = document.getElementById("ct-players");
  if (!dl) return;
  const g = (subsData?.groups || []).find(x => x.session === currentSession);
  const names = new Set();
  if (g) {
    (g.th?.players || []).forEach(n => n && names.add(n));
    (g.ms?.accounts || []).forEach(a => a?.name && names.add(a.name));
  }
  dl.innerHTML = [...names].sort().map(n => `<option value="${esc(n)}"></option>`).join("");
}
function ctStored(node) {
  const nameEl = document.getElementById("ct-name");
  const name = nameEl ? nameEl.value.trim() : "";
  if (!name) return null;
  const entry = normalizeEntry((customData[currentSession] || {})[name] || {});
  const v = entry[node];
  return v === undefined ? null : v;
}
function ctLoadCurrent(mode) {
  // mode: "node" 切换节点 → 回填该节点配置（没配也渲染空编辑器）；
  //       "name" 切换玩家 → 仅有已存配置时回填，避免打断正在输入的内容；
  //       "force" 点「改」→ 强制回填。
  if (ctEditing && mode !== "force") return;
  const node = document.getElementById("ct-node")?.value;
  if (!node) return;
  const cur = ctStored(node);
  if (node === "big") {
    if (cur || mode !== "name") bigRenderEditor(cur);
  } else if (node === "finish") {
    if (cur || mode !== "name") finishRenderEditor(cur);
  } else {
    const t = document.getElementById("ct-text");
    if (!t) return;
    if (cur) t.value = typeof cur === "string" ? cur : "";
    else if (mode !== "name") t.value = "";
  }
}
function onCtNameChange() { ctLoadCurrent("name"); }
async function renderCustom() {
  // 群列表不能只依赖 subsData（只有打开过「订阅管理」页它才有值）。
  // 每次进本页或点刷新都主动拉一次订阅列表，保证直接进来也能选到群。
  const [d, sub] = await Promise.all([
    API.get("/api/custom/list"),
    API.get("/api/subscriptions").catch(() => subsData || {}),
  ]);
  customData = d.custom || {};
  subsData = sub;
  const c = document.getElementById("content");
  const groups = sub?.groups || [];
  const sessions = Object.keys(customData).concat(groups.map(g => g.session));
  const uniq = [...new Set(sessions)];
  c.innerHTML = `
    <div class="page-title">定制文案</div>
    <div class="page-sub">按群 + 玩家 + 播报节点定制文案，用模板整条重写该节点播报，写入即生效</div>
    <div class="group-bar">
      <label>选择群：</label>
      <select id="group-select" onchange="onCustomGroupChange()"></select>
      <span id="ct-session-full" class="muted mono"></span>
      <button class="btn btn-sm" onclick="renderCustom()">刷新</button>
    </div>
    <div class="card">
      <div class="card-title"><span class="mdi mdi-format-text"></span>新增 / 修改文案</div>
      <div class="form-row">
        <div class="form-group" style="max-width:220px"><label class="form-label">玩家名（下拉选订阅玩家 / 可手输）</label><input id="ct-name" class="form-input" list="ct-players" placeholder="点开下拉选订阅玩家" onchange="onCtNameChange()"><datalist id="ct-players"></datalist></div>
        <div class="form-group" style="max-width:180px"><label class="form-label">播报节点</label>
          <select id="ct-node" class="form-input" onchange="onCtNodeChange()">${CT_NODES.map(n => `<option value="${n.key}">${n.label}</option>`).join("")}</select>
        </div>
      </div>
      <div id="ct-simple-wrap">
        <div class="form-group" style="margin-top:12px">
          <label class="form-label">模板（整条重写）</label>
          <textarea id="ct-text" class="form-input" rows="3" placeholder="留空占位符表示删除该节点；{raw} 代表原始整条播报"></textarea>
        </div>
      </div>
      <div id="ct-finish-editor" class="finish-editor" style="margin-top:12px;display:none"></div>
      <div id="ct-big-editor" class="big-editor" style="margin-top:12px;display:none"></div>
      <div id="ct-editing-tip" class="ct-editing-tip" style="display:none"></div>
      <div class="ct-actions">
        <button class="btn btn-primary" id="ct-save-btn" onclick="ctSet()">保存</button>
        <button class="btn btn-sm" onclick="ctReset()">清空</button>
      </div>
      <div class="muted" style="margin-top:10px">本节点常用占位符：<span id="ct-ph" class="mono"></span></div>
      <div id="ct-ph-note" class="muted" style="margin-top:4px">{raw} = 原始整条播报；{head} = 标题行；{body} = 除标题外的正文。终局节点若模板不含块占位符，则只替换顺位点评句（兼容旧用法）。</div>
      ${renderPhHelp((CT_NODES[0] || {}).key)}
    </div>
    <div class="card">
      <div class="card-title"><span class="mdi mdi-format-list-bulleted"></span>本群定制列表（玩家 × 节点）</div>
      <div id="ct-list"></div>
    </div>`;
  setSessionOptions(uniq, shortLabel);
  updateCtSessionFull();
  updateCtPlayers();
  onCtNodeChange();
  renderCustomList();
}
function updateCtSessionFull() {
  const el = document.getElementById("ct-session-full");
  if (el) el.textContent = currentSession ? `完整 session: ${currentSession}` : "";
}
// 只切 UI（节点说明、编辑器显隐），不碰内容
function syncCtNodeUI() {
  const el = document.getElementById("ct-node");
  if (!el) return;
  const node = el.value;
  const ph = document.getElementById("ct-ph");
  if (ph) ph.textContent = (CT_NODES.find(n => n.key === node) || {}).ph || "";
  const simple = document.getElementById("ct-simple-wrap");
  const finEd = document.getElementById("ct-finish-editor");
  const bigEd = document.getElementById("ct-big-editor");
  const note = document.getElementById("ct-ph-note");
  const isBig = node === "big";
  const isFin = node === "finish";
  // 注意：这里必须写死 "block"，不能用 ""。"" 只是清掉行内样式，
  // 会退回样式表里的 .big-editor{display:none} —— 编辑器渲染好了却整块不可见。
  if (simple) simple.style.display = (isBig || isFin) ? "none" : "block";
  if (finEd) finEd.style.display = isFin ? "block" : "none";
  if (bigEd) bigEd.style.display = isBig ? "block" : "none";
  if (note) note.style.display = (isBig || isFin) ? "none" : "block";
  markPhHelp(node);
}
function onCtNodeChange() {
  syncCtNodeUI();
  // 切换节点一定重绘：没配过也要渲染出空规则编辑器，
  // 否则区块一片空白、连「+ 添加一档」都没有（曾经的“大牌通报是空的”就是这个）。
  ctLoadCurrent("node");
}
function onCustomGroupChange() {
  currentSession = document.getElementById("group-select").value;
  ctEditing = null;
  updateCtSessionFull();
  updateCtPlayers();
  renderCustomList();
  ctReset();
}
function ctReset() {
  ctEditing = null;
  const n = document.getElementById("ct-name"); if (n) { n.value = ""; n.disabled = false; }
  const t = document.getElementById("ct-text"); if (t) t.value = "";
  const tip = document.getElementById("ct-editing-tip"); if (tip) { tip.style.display = "none"; tip.innerHTML = ""; }
  const saveBtn = document.getElementById("ct-save-btn"); if (saveBtn) saveBtn.textContent = "保存";
  finishRenderEditor(null);
  bigRenderEditor(null);
}
// 列表里的预览：不做截断，完整展示（保留换行）
function ctPreviewText(nodeKey, tpl) {
  if (nodeKey === "big") return bigSummary(tpl);
  if (nodeKey === "finish") return finishSummary(tpl);
  return typeof tpl === "string" ? tpl : String(tpl || "");
}
function ctHasContent(nodeKey, tpl) {
  if (tpl === undefined || tpl === null) return false;
  if (typeof tpl === "string") return !!tpl.trim();
  if (typeof tpl === "object") return Object.keys(tpl).length > 0;
  return true;
}
function renderCustomList() {
  const box = document.getElementById("ct-list");
  if (!box) return;
  const map = customData[currentSession] || {};
  const names = Object.keys(map).sort();
  if (!names.length) { box.innerHTML = '<div class="empty">本群暂无定制文案</div>'; return; }
  const rows = [];
  for (const n of names) {
    const entry = normalizeEntry(map[n]);
    for (const node of CT_NODES) {
      const tpl = entry[node.key];
      if (!ctHasContent(node.key, tpl)) continue;
      const preview = ctPreviewText(node.key, tpl);
      rows.push(`<tr>
        <td class="ct-td-name">${esc(n)}</td>
        <td class="ct-td-node"><span class="tag">${esc(node.label)}</span></td>
        <td class="ct-td-tpl"><div class="ct-tpl mono">${esc(preview || "(空)")}</div></td>
        <td class="ct-td-act">
          <button class="btn btn-sm" onclick="ctEdit(${jsArg(n)}, ${jsArg(node.key)})">改</button>
          <button class="btn btn-sm btn-danger" onclick="ctDel(${jsArg(n)}, ${jsArg(node.key)})">删</button>
        </td></tr>`);
    }
  }
  box.innerHTML = rows.length
    ? `<table class="ct-table"><colgroup><col style="width:150px"><col style="width:96px"><col><col style="width:118px"></colgroup>
       <thead><tr><th>玩家</th><th>节点</th><th>模板（完整）</th><th>操作</th></tr></thead><tbody>${rows.join("")}</tbody></table>`
    : '<div class="empty">本群暂无定制文案</div>';
}
async function ctSet() {
  const name = document.getElementById("ct-name").value.trim();
  const node = document.getElementById("ct-node").value;
  let text;
  if (node === "big") {
    const rules = bigCollect();
    const has = (rules["和出"].length || rules["点出"].length);
    if (!has) return toast("和出/点出至少填一档文案", "error");
    text = rules;
  } else if (node === "finish") {
    const fm = finishCollect();
    if (!Object.keys(fm).length) return toast("至少填一个名次的文案", "error");
    text = fm;
  } else {
    text = document.getElementById("ct-text").value.trim();
    if (!text) return toast("玩家名和模板都要填", "error");
  }
  if (!name) return toast("玩家名要填", "error");
  if (!currentSession) return toast("请先选择群", "error");
  try {
    await API.post("/api/custom/set", { session: currentSession, name, node, text });
    toast("已保存"); ctReset(); await renderCustom();
  } catch (e) { toast(e.message, "error"); }
}
function ctEdit(name, node) {
  if (!CT_NODES.some(n => n.key === node)) return toast("未知节点：" + node, "error");
  ctEditing = { name, node };
  const nameEl = document.getElementById("ct-name");
  const nodeEl = document.getElementById("ct-node");
  nameEl.value = name;
  nameEl.disabled = true;
  nodeEl.value = node;
  syncCtNodeUI();
  ctLoadCurrent("force");
  const tip = document.getElementById("ct-editing-tip");
  if (tip) {
    tip.style.display = "";
    tip.innerHTML = `正在修改：<b>${esc(name)}</b> · ${esc(nodeLabel(node))}（改完点「保存并覆盖」，或点「清空」退出编辑）`;
  }
  const saveBtn = document.getElementById("ct-save-btn");
  if (saveBtn) saveBtn.textContent = "保存并覆盖";
  const anchor = document.getElementById(node === "big" ? "ct-big-editor"
    : node === "finish" ? "ct-finish-editor" : "ct-text");
  if (anchor) anchor.scrollIntoView({ behavior: "smooth", block: "center" });
}
async function ctDel(name, node) {
  if (!await confirmBox(`删除「${name}」的「${nodeLabel(node)}」文案？`)) return;
  try { await API.post("/api/custom/del", { session: currentSession, name, node }); toast("已删除"); ctReset(); await renderCustom(); }
  catch (e) { toast(e.message, "error"); }
}

// ---------- PT 报告 ----------
async function renderReport() {
  const [cfg, sub] = await Promise.all([
    API.get("/api/report/config"),
    API.get("/api/subscriptions").catch(() => subsData || {}),
  ]);
  subsData = sub;
  const groups = sub?.groups || [];
  const sessions = groups.map(g => g.session).filter(Boolean);
  const c = document.getElementById("content");
  c.innerHTML = `
    <div class="page-title">PT 报告</div>
    <div class="page-sub">每天定时播报订阅玩家的 PT 变动榜（仅雀魂），按群隔离</div>
    <div class="group-bar">
      <label>选择群：</label>
      <select id="group-select" onchange="onReportGroupChange()"></select>
      <span id="rp-session-full" class="muted mono"></span>
      <button class="btn btn-sm" onclick="renderReport()">刷新</button>
    </div>
    <div class="card">
      <div class="card-title"><span class="mdi mdi-cog"></span>定时设置</div>
      <div class="form-row">
        <div class="form-group" style="max-width:160px">
          <label class="form-label">启用</label>
          <label class="switch"><input id="rp-enabled" type="checkbox" ${cfg.enabled ? "checked" : ""}><span class="slider"></span></label>
        </div>
        <div class="form-group" style="max-width:140px"><label class="form-label">小时 (0-23)</label><input id="rp-hour" class="form-input" type="number" min="0" max="23" value="${cfg.hour}"></div>
        <div class="form-group" style="max-width:140px"><label class="form-label">分钟 (0-59)</label><input id="rp-minute" class="form-input" type="number" min="0" max="59" value="${cfg.minute}"></div>
        <button class="btn btn-primary" onclick="reportSave()">保存</button>
      </div>
      <div class="muted" style="margin-top:10px">按容器本地时间。日报每天发，周报每周一、月报每月 1 号随日报一起发。QQ 官方群刚重启后需先有发言才能主动推送。</div>
    </div>
    <div class="card">
      <div class="card-title row-between"><span><span class="mdi mdi-chart-line"></span>立即生成一次（预览，不推群，仅所选群）</span>
        <button class="btn" onclick="reportRun()">生成报告</button></div>
      <div class="report-text" id="report-output">点击「生成报告」查看当前群的日/周/月榜。</div>
    </div>`;
  setSessionOptions(sessions, shortLabel);
  updateReportSessionFull();
}
function onReportGroupChange() {
  currentSession = document.getElementById("group-select").value;
  updateReportSessionFull();
}
function updateReportSessionFull() {
  const el = document.getElementById("rp-session-full");
  if (el) el.textContent = currentSession ? `完整 session: ${currentSession}` : "";
}
async function reportSave() {
  const enabled = document.getElementById("rp-enabled").checked;
  const hour = document.getElementById("rp-hour").value;
  const minute = document.getElementById("rp-minute").value;
  try { await API.post("/api/report/config", { enabled, hour: Number(hour), minute: Number(minute) }); toast("报告设置已保存"); }
  catch (e) { toast(e.message, "error"); }
}
async function reportRun() {
  const out = document.getElementById("report-output");
  out.textContent = "生成中...";
  try {
    const d = await API.post("/api/report/run", { session: currentSession });
    if (d.image) out.innerHTML = `<img class="report-img" src="${d.image}" alt="PT 变动榜">`;
    else out.textContent = d.text || "（报告为空：尚无历史快照，启用定时或稍后再试）";
  } catch (e) { out.textContent = "生成失败：" + (e.message || e); toast(e.message, "error"); }
}

// ---------- 插件配置 ----------
async function renderConfig() {
  const d = await API.get("/api/config");
  const c = document.getElementById("content");
  const cfg = d.config || {};
  const ma = cfg.majsoul || {}, ko = cfg.koromo || {}, rp = cfg.report || {};
  const dis = cfg.display || {};
  c.innerHTML = `
    <div class="page-title">插件配置</div>
    <div class="page-sub">全部可编辑；敏感项留空或保持打码则不修改。保存后部分项需重载插件生效。</div>
    <div class="card">
      <div class="card-title"><span class="mdi mdi-account-key"></span>雀魂账号</div>
      <div class="form-row">
        <div class="form-group"><label class="form-label">邮箱</label><input id="cfg-ms-email" class="form-input" value="${esc(ma.email || "")}"></div>
        <div class="form-group"><label class="form-label">密码（打码则不变）</label><input id="cfg-ms-password" class="form-input" type="password" value="${esc(ma.password || "")}"></div>
      </div>
      <div class="form-row" style="margin-top:12px">
        <div class="form-group"><label class="form-label">Access Token（打码则不变）</label><input id="cfg-ms-token" class="form-input" type="password" value="${esc(ma.token || "")}"></div>
        <div class="form-group" style="max-width:200px"><label class="form-label">轮询间隔(秒)</label><input id="cfg-ms-poll" class="form-input" type="number" value="${esc(ma.poll_interval || 30)}"></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="mdi mdi-key"></span>牌谱屋凭证</div>
      <div class="form-group" style="max-width:420px"><label class="form-label">Token（留空不变；留空也昵称/统计照常用）</label><input id="cfg-ko-token" class="form-input" type="password" value="${esc(ko.token || "")}"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="mdi mdi-palette"></span>播报显示</div>
      <div class="form-row">
        <div class="form-group" style="max-width:200px"><label class="form-label">数据来源</label>
          <select id="cfg-dis-src"><option value="majsoul" ${dis.rate_source !== "sapk" ? "selected" : ""}>majsoul</option><option value="sapk" ${dis.rate_source === "sapk" ? "selected" : ""}>sapk（牌谱屋）</option></select></div>
        <div class="form-group"><label class="form-label">开关</label><div style="display:flex;gap:16px;align-items:center;height:34px">
          <label><input id="cfg-dis-tierlabels" type="checkbox" ${dis.tier_labels ? "checked" : ""}> 安定标签</label>
          <label><input id="cfg-dis-sanhaoyi" type="checkbox" ${dis.sanhaoyi_image ? "checked" : ""}> 雀豪彩蛋</label>
          <label><input id="cfg-dis-pointalert" type="checkbox" ${dis.point_alert ? "checked" : ""}> 大牌文案</label>
        </div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="mdi mdi-chart-line"></span>PT 报告</div>
      <div class="form-row">
        <div class="form-group" style="max-width:160px"><label class="form-label">启用</label><label class="switch"><input id="cfg-rp-enabled" type="checkbox" ${rp.enabled ? "checked" : ""}><span class="slider"></span></label></div>
        <div class="form-group" style="max-width:140px"><label class="form-label">小时</label><input id="cfg-rp-hour" class="form-input" type="number" value="${esc(rp.hour || 12)}"></div>
        <div class="form-group" style="max-width:140px"><label class="form-label">分钟</label><input id="cfg-rp-minute" class="form-input" type="number" value="${esc(rp.minute || 0)}"></div>
      </div>
    </div>
    <div style="margin-bottom:24px"><button class="btn btn-primary" onclick="configSave()">保存全部配置</button></div>`;
}
async function configSave() {
  const payload = {
    majsoul: {
      email: document.getElementById("cfg-ms-email").value,
      password: document.getElementById("cfg-ms-password").value,
      token: document.getElementById("cfg-ms-token").value,
      poll_interval: Number(document.getElementById("cfg-ms-poll").value) || 30,
    },
    koromo: { token: document.getElementById("cfg-ko-token").value },
    display: {
      rate_source: document.getElementById("cfg-dis-src").value,
      tier_labels: document.getElementById("cfg-dis-tierlabels").checked,
      sanhaoyi_image: document.getElementById("cfg-dis-sanhaoyi").checked,
      point_alert: document.getElementById("cfg-dis-pointalert").checked,
    },
    report: {
      enabled: document.getElementById("cfg-rp-enabled").checked,
      hour: Number(document.getElementById("cfg-rp-hour").value),
      minute: Number(document.getElementById("cfg-rp-minute").value),
    },
  };
  try {
    await API.post("/api/config", payload);
    toast("配置已保存。账号/令牌等改动需重载插件后生效");
    await renderConfig();
  } catch (e) { toast(e.message, "error"); }
}

// ---------- 启动 ----------
navigateTo("overview");
