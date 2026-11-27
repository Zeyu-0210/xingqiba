// 星期八 窗口界面（前端）。数据都从本机的 ui_server.py 拿，口令在打开窗口时带过来。
"use strict";

const TOKEN = (() => {
  const url = new URL(location.href);
  let t = url.searchParams.get("t");
  try {
    if (t) sessionStorage.setItem("t", t);
    else t = sessionStorage.getItem("t");
  } catch (e) { /* 存不了也没关系 */ }
  if (url.searchParams.has("t")) history.replaceState(null, "", "/");  // 地址栏里不留口令
  return t || "";
})();

const $ = (sel) => document.querySelector(sel);
const S = { state: null, chat: [], pending: null, group: null, typing: {}, page: "home", unread: 0, level: 0,
            openMember: null, connected: false, openSteps: new Set(), settingsTab: "models" };
let groupDraft = null;

// ---------------------------------------------------------------- 和后台通信
const REFRESH_TIMEOUT_MS = 5000;
const refreshRequests = new Map();
const refreshFailures = new Set();
let syncDictation = false;
let pendingGroupMessages = null;

async function refreshSection(path, label, apply) {
  refreshRequests.get(path)?.abort();
  const controller = new AbortController();
  refreshRequests.set(path, controller);
  const timer = setTimeout(() => controller.abort(), REFRESH_TIMEOUT_MS);
  try {
    const r = await fetch(path, { headers: { "X-Token": TOKEN }, signal: controller.signal });
    if (!r.ok) throw new Error(r.status === 403 ? "请从托盘图标或快捷方式重新打开" : `请求出错（${r.status}）`);
    const data = await r.json();
    if (refreshRequests.get(path) !== controller) return;
    apply(data);
    refreshFailures.delete(path);
  } catch (e) {
    if (refreshRequests.get(path) !== controller) return;
    if (!refreshFailures.has(path)) {
      const reason = controller.signal.aborted ? "超时，稍后重试" : `失败，${e.message}`;
      toast(`${label}刷新${reason}`, true);
    }
    refreshFailures.add(path);
  } finally {
    clearTimeout(timer);
    if (refreshRequests.get(path) === controller) refreshRequests.delete(path);
  }
}
async function act(name, data = {}, quiet = false) {
  try {
    const r = await fetch(`/api/action/${name}`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Token": TOKEN }, body: JSON.stringify(data),
    });
    const j = await r.json();
    if (name === "group_send" && r.ok && j.saved === undefined) {
      // 旧后台收到非空群消息时，先写历史再返回 HTTP 200。
      j.saved = true;
    }
    if (j.message && (!quiet || !j.ok)) toast(j.message, !j.ok);
    return j;
  } catch (e) {
    if (name === "group_send") return { ok: false, connectionError: true };
    toast("连不上星期八（它可能已经退出了）", true);
    return { ok: false };
  }
}

function connect() {
  const es = new EventSource(`/api/events?t=${encodeURIComponent(TOKEN)}`);
  es.onopen = () => { S.connected = true; refreshAll(); };
  es.onerror = () => {
    S.connected = false;
    $("#side-state").textContent = "连接断开，重连中…";
  };
  es.onmessage = (e) => onEvent(JSON.parse(e.data));
}

function refreshAll() {
  syncDictation = true;
  refreshState();
  refreshChat();
  refreshGroup();
}
function refreshState() {
  return refreshSection("/api/state", "状态", (state) => {
    S.state = state;
    renderStatus();
    if (S.chat.length || S.pending) renderChips();
    else renderHome();
    renderSettings();
    if (syncDictation) {
      syncDictation = false;
      if (state.dictation) applyDictation(state.dictation);
    }
  });
}
function refreshChat() {
  return refreshSection("/api/chat", "对话", (chat) => {
    S.chat = chat;
    renderHome();
  });
}
function refreshGroup() {
  const messages = [];
  pendingGroupMessages = messages;
  return refreshSection("/api/group", "群聊", (group) => {
    // 请求期间从实时推送收到的消息可能比接口返回的历史更新。
    const ids = new Set(group.history.map((message) => message.id));
    for (const message of messages) {
      if (ids.has(message.id)) continue;
      group.history.push(message);
      ids.add(message.id);
    }
    S.group = group;
    renderGroup();
    renderSettings();
  }).finally(() => {
    if (pendingGroupMessages === messages) pendingGroupMessages = null;
  });
}

function onEvent(ev) {
  if (ev.type === "status") {
    if (!S.state) return;
    S.state.status = { state: ev.state, detail: ev.detail, since: ev.since, style: ev.style };
    if (["待命中", "已暂停", "在听"].includes(ev.state)) S.state.paused = ev.state === "已暂停";
    renderStatus();
  } else if (ev.type === "level") {
    S.level = ev.value;
  } else if (ev.type === "dictation") {
    applyDictation(ev, ev.auto);
  } else if (ev.type === "turn_start") {
    S.pending = { you: ev.you, source: ev.source };
    renderHome(true);
  } else if (ev.type === "turn") {
    S.pending = null;
    S.chat.push({ time: new Date().toISOString(), you: ev.you, reply: ev.reply, actions: ev.actions, source: ev.source });
    renderHome(true);
  } else if (ev.type === "group_message") {
    confirmGroupDraft(ev.message.id);
    pendingGroupMessages?.push(ev.message);
    if (!S.group || S.group.history.some((message) => message.id === ev.message.id)) return;
    S.group.history.push(ev.message);
    if (S.page !== "group" && ev.message.role === "ai") S.unread += 1;
    renderGroupMessages(true); renderBadge();
  } else if (ev.type === "group_typing") {
    if (ev.on) S.typing[ev.member_id] = { name: ev.name, doing: ev.doing || "" }; else delete S.typing[ev.member_id];
    renderTyping();
  } else if (ev.type === "group_members") {
    refreshGroup();
  } else if (ev.type === "group_cleared" || ev.type === "group_memory" || ev.type === "group_tasks") {
    refreshGroup();
  } else if (ev.type === "group_project") {
    // 换了项目：聊天记录、记忆、任务卡整个换成那个项目的
    S.memoryDirty = false; S.openSteps?.clear?.();
    refreshGroup().then(() => { if (!S.group) return; renderMemory(true); renderProjects(true); renderGroupMessages(true); });
  } else if (ev.type === "login_status") {
    if (ev.provider === "claude") refreshState();
    refreshGroup();
  } else if (ev.type === "settings") {
    // 设置变了（可能是你用语音换了大脑、换了声音）：马上刷新界面（几个连着改的合并成一次）
    clearTimeout(onEvent.settingsTimer);
    onEvent.settingsTimer = setTimeout(refreshState, 250);
  }
}

// ---------------------------------------------------------------- 小工具
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
// 很小的 Markdown：代码块、行内代码、粗体、列表、标题（先转义，所以不会执行任何 HTML）
function md(text) {
  const blocks = [];
  let s = esc(text).replace(/```[^\n]*\n?([\s\S]*?)```/g, (_, code) => { blocks.push(code); return `\u0000${blocks.length - 1}\u0000`; });
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  const out = []; let list = null;
  for (const line of s.split("\n")) {
    const m = line.match(/^\s*(?:[-*•]|(\d+)[.、)])\s+(.*)$/);
    if (m) {
      const kind = m[1] ? "ol" : "ul";
      if (list !== kind) { if (list) out.push(`</${list}>`); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${m[2]}</li>`); continue;
    }
    if (list) { out.push(`</${list}>`); list = null; }
    const h = line.match(/^#{1,4}\s+(.*)$/);
    out.push(h ? `<b>${h[1]}</b><br>` : `${line}<br>`);
  }
  if (list) out.push(`</${list}>`);
  return out.join("").replace(/(<br>)+$/, "").replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre><code>${blocks[+i]}</code></pre>`);
}
function timeText(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const today = new Date().toDateString() === d.toDateString();
  const hm = d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  return today ? hm : `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}
function toast(text, bad = false) {
  const t = $("#toast");
  t.textContent = text; t.classList.toggle("bad", bad); t.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove("show"), 3200);
}
function confirmBox(text, yes = "确定") {
  return new Promise((resolve) => {
    const d = $("#confirm");
    $("#confirm-text").textContent = text; $("#confirm-yes").textContent = yes;
    d.onclose = () => resolve(d.returnValue === "yes");
    d.showModal();
  });
}
function atBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 80; }
function initial(name) { return (name || "?").trim().charAt(0).toUpperCase(); }
function autoGrow(ta) { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; }

// ---------------------------------------------------------------- 状态圆球（和右下角状态窗同一套动画）
function drawOrb(canvas, t, big) {
  const st = S.state?.status;
  const [rgb, anim] = st?.style || [[90, 160, 200], "spin"];
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
  const c = canvas.getContext("2d");
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.3;
  const col = (a) => `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`;
  const lw = big ? 5 : 2.5;
  // 光晕
  const g = c.createRadialGradient(cx, cy, R * 0.2, cx, cy, R * 1.6);
  g.addColorStop(0, col(big ? 0.22 : 0.18)); g.addColorStop(1, col(0));
  c.fillStyle = g; c.beginPath(); c.arc(cx, cy, R * 1.6, 0, Math.PI * 2); c.fill();
  c.lineCap = "round";
  if (anim === "breathe") {
    const k = 0.55 + 0.45 * Math.sin(t * 2.2);
    c.strokeStyle = col(0.35 + 0.65 * k); c.lineWidth = lw;
    c.beginPath(); c.arc(cx, cy, R * (0.92 + 0.08 * k), 0, Math.PI * 2); c.stroke();
  } else if (anim === "bars") {
    S.smooth = 0.7 * (S.smooth || 0) + 0.3 * Math.min(1, S.level * 12);
    c.strokeStyle = col(1); c.lineWidth = big ? 9 : 3.5;
    for (let i = 0; i < 5; i++) {
      const bh = R * (0.25 + 1.5 * S.smooth * (0.6 + 0.4 * Math.sin(t * 9 + i * 1.3)) + 0.06 * Math.random());
      const x = cx - R + i * (R / 2);
      c.beginPath(); c.moveTo(x, cy - bh / 2); c.lineTo(x, cy + bh / 2); c.stroke();
    }
  } else if (anim === "spin") {
    c.lineWidth = lw;
    c.strokeStyle = col(0.22); c.beginPath(); c.arc(cx, cy, R, 0, Math.PI * 2); c.stroke();
    const a = (t * 2 * Math.PI) % (Math.PI * 2);
    c.strokeStyle = col(1); c.beginPath(); c.arc(cx, cy, R, a, a + 1.75); c.stroke();
  } else if (anim === "wave") {
    c.lineWidth = big ? 4 : 2;
    for (let i = 0; i < 3; i++) {
      const p = (t * 0.9 + i / 3) % 1;
      c.strokeStyle = col(1 - p); c.beginPath(); c.arc(cx, cy, R * (0.4 + 0.8 * p), 0, Math.PI * 2); c.stroke();
    }
  } else {
    c.strokeStyle = col(1); c.lineWidth = lw; c.beginPath(); c.arc(cx, cy, R, 0, Math.PI * 2); c.stroke();
  }
  if (big) {  // 中间的“八”
    c.fillStyle = col(0.95); c.font = `700 ${Math.round(R * 0.62)}px "Microsoft YaHei UI", sans-serif`;
    c.textAlign = "center"; c.textBaseline = "middle";
    if (anim !== "bars") c.fillText("八", cx, cy + 1);
  }
}
function animate() {
  const t = performance.now() / 1000;
  drawOrb($("#mini-orb"), t, false);
  if (S.page === "home") drawOrb($("#orb"), t, true);
  if (S.state?.status && S.page === "home") {
    const st = S.state.status;
    const secs = Date.now() / 1000 - st.since;
    const idle = ["待命中", "已暂停", "启动中"].includes(st.state);
    $("#hero-state").textContent = idle || secs < 0.8 ? st.state : `${st.state} · ${secs.toFixed(1)} 秒`;
  }
  requestAnimationFrame(animate);
}

// ---------------------------------------------------------------- 左边栏、状态
function renderStatus() {
  const st = S.state?.status;
  if (!st) return;
  $("#side-state").textContent = st.state;
  document.body.dataset.micState = S.state.paused ? "paused" : "ready";
  $("#hero-mode").textContent = slotInfo("voice", S.state.voice_mode)?.label || "语音对话";
  $("#barge-control").hidden = S.state.voice_mode === "minicpm";
  renderModelStrip();
  $("#hero-detail").textContent = st.detail || (st.state === "待命中" ? `说“${S.state.wake_words.join("、")}”叫醒我，或者在下面打字` : "");
  $("#pause-toggle").checked = S.state.paused;
  const hint = S.state.voice_mode === "minicpm" ? "MiniCPM-o 本地通话" : `说“${S.state.wake_words[0]}”唤醒`;
  $("#side-hint").textContent = S.state.paused ? "麦克风已关闭" : hint;
}
function renderBadge() {
  const b = $("#group-badge");
  b.hidden = S.unread === 0; b.textContent = S.unread > 99 ? "99+" : S.unread;
}
function showPage(page) {
  S.page = page;
  document.querySelectorAll(".nav-item").forEach((n) => {
    const active = n.dataset.page === page;
    n.classList.toggle("active", active);
    if (active) n.setAttribute("aria-current", "page");
    else n.removeAttribute("aria-current");
  });
  document.querySelectorAll(".page").forEach((p) => { p.hidden = p.id !== `page-${page}`; });
  if (page === "group") { S.unread = 0; renderBadge(); const m = $("#group-messages"); m.scrollTop = m.scrollHeight; $("#group-input").focus(); }
  if (page === "home") { const m = $("#home-messages"); m.scrollTop = m.scrollHeight; }
  if (page === "settings") { renderSettings(); refreshState(); }
}

// ---------------------------------------------------------------- 星期八（对话）
function renderChips() {
  if (!S.state) return;
  const sel = $("#brain-select");
  sel.innerHTML = S.state.brains.map((b) => `<option value="${esc(b.name)}" ${b.name === S.state.brain ? "selected" : ""}>${esc(b.name)}${b.note ? "（不可用）" : ""}</option>`).join("");
  const chip = $("#barge-chip");
  chip.checked = S.state.barge_in && S.state.barge_in_available;
  chip.disabled = !S.state.barge_in_available;
}
function renderHome(scroll) {
  renderChips();
  const box = $("#home-messages");
  const stick = scroll || atBottom(box);
  if (!S.chat.length && !S.pending) {
    if (S.state?.voice_mode === "minicpm") {
      box.innerHTML = '<div class="empty"><b>可以直接说话</b>等待状态显示「在听」，或在下方输入消息。</div>';
      return;
    }
    box.innerHTML = `<div class="empty"><b>还没有对话</b>说“${esc(S.state?.wake_words?.[0] || "星期八")}”叫醒我，或者在下面打字。<br>今天的对话会显示在这里（只在你电脑上）。</div>`;
    return;
  }
  const parts = [];
  for (const r of S.chat) {
    const src = r.source === "text" ? "打字" : r.source === "voice" ? "语音" : "";
    parts.push(`<div class="row me"><div class="bubble">${md(r.you)}<div class="meta">${src ? src + " · " : ""}${timeText(r.time)}</div></div></div>`);
    const did = (r.actions || []).length ? `<span class="tag" title="${esc(r.actions.join("、"))}">做了 ${r.actions.length} 个操作</span>` : "";
    parts.push(`<div class="row"><div class="avatar" style="background:#0aa5c8">八</div><div class="bubble">${md(r.reply || "（没说话）")}<div class="meta">${did}${timeText(r.time)}</div></div></div>`);
  }
  if (S.pending) {
    parts.push(`<div class="row me"><div class="bubble">${md(S.pending.you)}</div></div>`);
    parts.push(`<div class="row"><div class="avatar" style="background:#0aa5c8">八</div><div class="bubble thinking"><span class="dots">正在想</span></div></div>`);
  }
  box.innerHTML = parts.join("");
  if (stick) box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------- AI 群聊
function memberById(id) { return S.group?.members.find((m) => m.id === id); }
function renderGroup() {
  if (!S.group) return;
  if (groupDraft && S.group.history.some((msg) => msg.id === groupDraft.id)) {
    confirmGroupDraft(groupDraft.id);
  }
  const inGroup = S.group.members.filter((m) => m.enabled);
  const ready = inGroup.filter((m) => m.ready);
  const hands = S.group.members.find((m) => m.hands);
  $("#group-sub").textContent = inGroup.length
    ? `${inGroup.map((m) => m.name).join("、")}`
      + `${ready.length < inGroup.length ? `（${inGroup.length - ready.length} 个还不能说话）` : ""}`
    : "群里还没有 AI，点右上角“成员”拉几个进来";
  // 执行权（能改东西的权限）：同一时间只给一个，只有你能给、能收回
  const give = inGroup.filter((m) => m.can_exec && !m.hands)
    .map((m) => `<button class="btn small" type="button" data-hands="${esc(m.id)}">交给 ${esc(m.name)}</button>`).join("");
  $("#hands-bar").innerHTML = !inGroup.length ? "" : hands
    ? `<span class="hands-text on">🖐 执行权在 <b style="color:${esc(hands.color)}">${esc(hands.name)}</b> 手里：你说的话它会真的去做，其他 AI 只能看、不能改</span>${give}<button class="btn small ghost" type="button" data-hands="">收回</button>`
    : `<span class="hands-text">🔒 现在没人有执行权：大家都能看文件、出主意，但不会改东西</span>${give}`;
  $("#mention-bar").innerHTML = inGroup.map((m) =>
    `<button class="mention" type="button" data-name="${esc(m.name)}" title="只让 ${esc(m.name)} 回答"><span class="dot" style="background:${esc(m.color)}">${esc(initial(m.name))}</span>@${esc(m.name)}</button>`).join("");
  renderGroupMessages(false);
  renderTyping();
  renderMembers();
  renderMemory(false);
  renderTasks();
  renderProjects(false);
}
// ---- 项目：每个项目各自的聊天记录、记忆、任务卡
function renderProjects(force) {
  const p = S.group?.projects;
  if (!p) return;
  const sel = $("#project-select");
  if (force || document.activeElement !== sel) {
    sel.innerHTML = p.list.map((x) => `<option value="${esc(x.id)}" ${x.current ? "selected" : ""}>${esc(x.name)}</option>`).join("");
    sel.value = p.current;
  }
  const cur = p.list.find((x) => x.current);
  const name = $("#project-name"), folders = $("#project-folders");
  const editing = [name, folders].includes(document.activeElement);
  if (cur && (force || !editing)) {
    name.value = cur.name;
    folders.value = (cur.folders || []).join("\n");
  }
}
// ---- 任务卡
const TASK_STATUS = { "进行中": "run", "待拍板": "ask", "已完成": "done" };
function taskByMsg(msgId) { return (S.group?.tasks || []).find((t) => t.msg_id === msgId); }
function renderTasks() {
  const tasks = S.group?.tasks || [];
  const open = tasks.filter((t) => t.status !== "已完成");
  const ask = open.filter((t) => t.status === "待拍板").length;
  const c = $("#tasks-count");
  c.hidden = !open.length; c.textContent = open.length; c.classList.toggle("ask", ask > 0);
  c.title = ask ? `${ask} 件等你拍板` : "";
  if (S.taskEditing && document.activeElement?.closest("#task-list input")) return;  // 正在改标题时不重画
  const list = (items) => items.map((x) => `<li>${esc(x)}</li>`).join("");
  $("#task-list").innerHTML = tasks.map((t) => `
    <div class="task ${TASK_STATUS[t.status] || ""} ${t.focus ? "focus" : ""}" data-id="${esc(t.id)}">
      <div class="task-top">
        <span class="task-status">${esc(t.status)}</span>
        <b class="task-title" data-act="rename" title="点一下改标题">${esc(t.title)}</b>
      </div>
      <div class="task-owners">${(t.owners || []).map((o) => `<span class="dot" style="background:${esc(o.color)}" title="${esc(o.name)}">${esc(initial(o.name))}</span>${esc(o.name)}`).join(" ")}
        <span class="fine-inline">· ${esc(timeText(t.updated))}${t.running ? " · 正在整理…" : ""}${t.focus ? " · 当前任务" : ""}</span></div>
      <details class="task-origin"><summary>原话（${esc(t.origin_by || "")}）</summary><div>${md(t.origin || "")}</div></details>
      <div class="task-sec"><span>进展</span>${t.progress ? md(t.progress) : `<i class="fine-inline">${t.running ? "正在整理…" : "还没整理（点“更新摘要”）"}</i>`}</div>
      ${t.next ? `<div class="task-sec"><span>下一步</span>${esc(t.next)}</div>` : ""}
      ${t.disagreements?.length ? `<div class="task-sec warn"><span>分歧</span><ul>${list(t.disagreements)}</ul></div>` : ""}
      ${t.decisions?.length ? `<div class="task-sec ask"><span>等你拍板</span><ul>${list(t.decisions)}</ul></div>` : ""}
      ${t.error ? `<div class="task-sec bad">${esc(t.error)}</div>` : ""}
      <div class="member-actions">
        <button class="btn small ghost" data-act="delete" type="button">删除</button>
        <button class="btn small ghost" data-act="done" type="button">${t.status === "已完成" ? "重新打开" : "标为完成"}</button>
        <button class="btn small ghost" data-act="refresh" type="button" ${t.running ? "disabled" : ""}>更新摘要</button>
        <button class="btn small primary" data-act="continue" type="button">继续任务</button>
      </div>
    </div>`).join("") || `<p class="fine">还没有任务卡。</p>`;
}
function showDrawer(id) {
  for (const d of ["members-drawer", "memory-drawer", "tasks-drawer", "projects-drawer"]) {
    const el = document.getElementById(d);
    el.hidden = d === id ? !el.hidden : true;
  }
}
function renderMemory(force) {
  if (!S.group) return;
  const info = S.group.memory_info || {};
  $("#memory-info").textContent = (info.running ? "正在整理……" : `还有 ${info.pending ?? 0} 条新消息没整理（攒够 ${info.every ?? 20} 条自动整理）`)
    + (info.updated ? ` · 上次整理：${String(info.updated).replace("T", " ").slice(5, 16)}` : "")
    + (info.error ? ` · ${info.error}` : "");
  const box = $("#memory-text");
  if (force || (!S.memoryDirty && document.activeElement !== box)) box.value = S.group.memory || "";
}
function renderGroupMessages(scroll) {
  const box = $("#group-messages");
  const stick = scroll || atBottom(box);
  const list = S.group.history;
  if (!list.length) {
    box.innerHTML = `<div class="empty"><b>AI 群聊</b>发一句“你好”，群里的 AI 会轮流回一句。<br>用 @名字 只问某一个；它们互相 @ 时会接着回；点下面的“交给某某”给它执行权，它才会真的改东西。</div>`;
    return;
  }
  const taskBtn = (m) => {
    const t = taskByMsg(m.id);
    return t ? `<button class="task-link" type="button" data-open-task="${esc(t.id)}" title="打开任务卡">📌 ${esc(t.title)}</button>`
      : `<button class="task-make" type="button" data-make-task="${esc(m.id)}" title="把这条消息转为任务卡">📌 转为任务</button>`;
  };
  box.innerHTML = list.map((m) => {
    if (m.role === "system") return `<div class="note">${esc(m.text)}</div>`;
    if (m.role === "user") return `<div class="row me"><div class="bubble">${md(m.text)}<div class="meta">${m.task_id ? "" : taskBtn(m)}${timeText(m.time)}</div></div></div>`;
    const color = memberById(m.member_id)?.color || m.color || "#888";
    if (m.role === "step") {
      const icon = { cmd: "🖐", file: "✏️", think: "💭", read: "👀" }[m.kind] || (/^(看|想看)/.test(m.text) ? "👀" : "🖐");
      const line = `<span class="step-hand">${icon}</span><b style="color:${esc(color)}">${esc(m.name)}</b><span class="step-text ${m.kind === "think" ? "think" : ""}">${esc(m.text)}</span>`;
      return m.detail
        ? `<details class="step" data-id="${esc(m.id)}" ${S.openSteps.has(m.id) ? "open" : ""}><summary>${line}</summary><pre>${esc(m.detail)}</pre></details>`
        : `<div class="step">${line}</div>`;
    }
    const secs = m.seconds ? ` · 用时 ${m.seconds} 秒` : "";
    const tag = m.exec ? `<span class="tag exec">动手结果${m.steps ? ` · ${m.steps} 步` : ""}</span>`
      : m.review ? `<span class="tag exec">检查结果</span>` : "";
    return `<div class="row"><div class="avatar" style="background:${esc(color)}">${esc(initial(m.name))}</div>
      <div class="bubble ${m.exec ? "exec" : ""}"><div class="who" style="color:${esc(color)}">${esc(m.name)}</div>${md(m.text)}<div class="meta">${taskBtn(m)}${tag}${timeText(m.time)}${secs}</div></div></div>`;
  }).join("");
  if (stick) box.scrollTop = box.scrollHeight;
}
function renderTyping() {
  const all = Object.values(S.typing);
  const doing = all.filter((t) => t.doing === "do" || t.doing === true).map((t) => t.name);
  const looking = all.filter((t) => t.doing === "look").map((t) => t.name);
  const reviewing = all.filter((t) => t.doing === "review").map((t) => t.name);
  const typing = all.filter((t) => !t.doing).map((t) => t.name);
  const parts = [];
  if (doing.length) parts.push(`🖐 ${doing.join("、")} 正在动手（点群里的每一步能看命令和输出）`);
  if (looking.length) parts.push(`👀 ${looking.join("、")} 正在看项目`);
  if (reviewing.length) parts.push(`🔍 ${reviewing.join("、")} 正在检查`);
  if (typing.length) parts.push(`${typing.join("、")} 正在输入`);
  $("#group-typing").innerHTML = parts.length ? `<span class="dots">${esc(parts.join("；"))}</span>` : "";
}
function renderMembers() {
  if (!S.group) return;
  const keys = Object.fromEntries((S.group.keys || []).map((k) => [k.name, k]));
  const claudeModels = (S.state?.claude_models || []).filter((c) => c.id !== "auto");
  $("#member-list").innerHTML = S.group.members.map((m) => {
    const open = S.openMember === m.id;
    const sub = !m.ready ? `<div class="member-sub bad">${esc(m.why)}</div>` : `<div class="member-sub">${esc(m.kind === "claude" ? (claudeModels.find((c) => c.id === m.model)?.label || m.model) : (m.model || (m.kind === "codex" ? "ChatGPT 默认模型" : "")))}${m.role ? ` · 职责：${esc(m.role)}` : ""}</div>`;
    let edit = "";
    if (open) {
      const modelField = m.kind === "claude"
        ? `<label class="field">模型<select data-f="model">${claudeModels.map((c) => `<option value="${c.id}" ${c.id === m.model ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</select></label>`
        : `<label class="field">模型名<input data-f="model" value="${esc(m.model)}" placeholder="${m.kind === "codex" ? "留空 = 用 ChatGPT 默认模型" : "比如 deepseek-chat"}"></label>`;
      const urlField = m.kind === "openai" ? `<label class="field">服务地址（兼容 OpenAI 接口）<input data-f="base_url" value="${esc(m.base_url || "")}" placeholder="https://…/v1"></label>` : "";
      const k = m.key ? keys[m.key] : null;
      const keyField = m.kind === "openai" || m.kind === "claude"
        ? `<label class="field">${m.kind === "claude" ? "Anthropic 密钥（可不填：不填就用你登录的 Claude 账号）" : "密钥"}
             <input type="password" data-key="${esc(m.kind === "claude" ? "anthropic" : m.key)}" placeholder="${k?.set ? "已填写 " + esc(k.hint) + "（留空不改）" : "粘贴密钥"}" autocomplete="off"></label>` : "";
      edit = `<div class="member-edit">
        <label class="field">名字<input data-f="name" value="${esc(m.name)}"></label>
        <label class="field">职责（像公司里的分工，它每次回答都会记着）<textarea data-f="role" rows="2" placeholder="比如：写代码 / 定调和把关 / 查资料">${esc(m.role || "")}</textarea></label>
        ${modelField}${urlField}${keyField}
        ${m.about ? `<div class="member-sub" style="white-space:normal">${esc(m.about)}</div>` : ""}
        <div class="member-actions">
          <button class="btn small ghost" data-act="remove" type="button">删除成员</button>
          <button class="btn small primary" data-act="save" type="button">保存</button>
        </div></div>`;
    }
    const handBtn = m.hands
      ? `<button class="btn small hand on" data-act="hands" type="button" title="收回执行权，大家都只能看">🖐 有执行权</button>`
      : `<button class="btn small hand" data-act="hands" type="button" ${m.can_exec ? "" : "disabled"}
           title="${m.can_exec ? "把执行权交给它：你说的话它会真的去做（危险操作先问你），其他 AI 只能看" : esc("现在不能动手：" + m.exec_why)}">🖐 交给它</button>`;
    return `<div class="member ${m.enabled ? "" : "off"} ${m.hands ? "has-hands" : ""}" data-id="${m.id}">
      <div class="member-top">
        <div class="avatar" style="background:${esc(m.color)}">${esc(initial(m.name))}</div>
        <div class="member-info"><div class="member-name">${esc(m.name)}${m.typing ? " · 正在输入" : ""}</div>${sub}</div>
        ${handBtn}
        <button class="btn small ghost" data-act="edit" type="button">${open ? "收起" : "设置"}</button>
        <input type="checkbox" class="switch" data-act="toggle" ${m.enabled ? "checked" : ""} title="${m.enabled ? "在群里（点一下移出群）" : "不在群里（点一下拉进群）"}" aria-label="${esc(m.name)} 在不在群里">
      </div>${edit}</div>`;
  }).join("") || `<p class="fine">还没有成员。</p>`;
  $("#preset-grid").innerHTML = (S.group.presets || []).map((p) =>
    `<button class="preset" type="button" data-preset="${p.id}" title="${esc(p.about)}"><span class="dot" style="background:${esc(p.color)}">${esc(initial(p.name))}</span>${esc(p.name)}</button>`).join("");
}

// ---------------------------------------------------------------- 设置
const SETTINGS_TABS = [
  ["models", "模型与语音"],
  ["group", "AI 群聊"],
  ["system", "账户与系统"],
];

function slotInfo(group, value) {
  return S.state?.model_slots?.[group]?.find((item) => item.id === value);
}

function renderModelStrip() {
  const s = S.state;
  const items = [
    ["实时对话", slotInfo("voice", s.voice_mode)?.label || (s.voice_mode === "minicpm" ? "MiniCPM-o 4.5" : "唤醒词对话")],
    ["语音转写", slotInfo("transcription", s.transcription_model)?.label || "Whisper"],
    ["本地推理", slotInfo("reasoning", s.reasoning_model)?.label || "Gemma 4 12B"],
  ];
  $("#home-models").innerHTML = items.map(([name, model]) =>
    `<div><span>${name}</span><strong>${esc(model)}</strong></div>`).join("");
  $("#asr-settings").textContent = `本机转写 · ${items[1][1]}`;
}

function modelField(title, key, items, selected) {
  const current = items.find((item) => item.id === selected);
  const options = items.map((item) =>
    `<option value="${esc(item.id)}" ${item.id === selected ? "selected" : ""} ${item.available === false ? "disabled" : ""}>${esc(item.label)}${item.available === false ? `（${esc(item.unavailable || "未下载")}）` : ""}</option>`
  ).join("");
  return `<label class="model-field"><span>${title}</span><select data-set="${key}">${options}</select>
    <small>${esc(current?.detail || "")}</small></label>`;
}

function row(title, desc, control) {
  return `<div class="set-row"><div class="set-text"><div class="set-title">${title}</div>${desc ? `<div class="set-desc">${desc}</div>` : ""}</div>${control}</div>`;
}
function switchHtml(id, on, disabled = false) {
  return `<input type="checkbox" class="switch" data-set="${id}" ${on ? "checked" : ""} ${disabled ? "disabled" : ""}>`;
}
function renderSettings() {
  const s = S.state;
  if (!s) return;
  if (S.page === "settings" && document.activeElement?.closest("#settings input[type=password], #settings input[type=text]")) return;  // 正在输入密钥时不刷新
  const minicpm = s.voice_mode === "minicpm";
  const voices = s.voices.map((v) => `<option value="${esc(v.id)}" ${v.id === s.voice ? "selected" : ""}>${esc(v.name)}（${esc(v.about)}）</option>`).join("");
  const brains = s.brains.map((b) => `<label class="radio"><input type="radio" name="brain" value="${esc(b.name)}" ${b.name === s.brain ? "checked" : ""} ${b.available === false ? "disabled" : ""}>
      <span><b>${esc(b.name)}</b><br><span class="set-desc">${esc(b.about)}</span>${b.note ? `<br><span class="warn">${esc(b.note)}</span>` : ""}</span></label>`).join("");
  const models = s.claude_models.map((c) => `<option value="${esc(c.id)}" ${c.id === s.claude_model ? "selected" : ""}>${esc(c.label)}</option>`).join("");
  const g = S.group;
  const effName = (e) => `${g.effort_names?.[e] || e}（${e}）`;
  const groupModels = (g?.members || []).filter((m) => m.kind === "claude" || m.kind === "codex").map((m) => {
    let models, efforts;
    if (m.kind === "claude") {
      models = g.claude_models.map((c) => `<option value="${esc(c.id)}" ${c.id === m.model ? "selected" : ""}>${esc(c.label)}</option>`).join("");
      efforts = g.claude_efforts;
    } else {
      const chosen = g.codex_models.find((x) => x.id === m.model);
      models = `<option value="" ${!m.model ? "selected" : ""}>默认（跟 ChatGPT 一样）</option>`
        + g.codex_models.map((c) => `<option value="${esc(c.id)}" ${c.id === m.model ? "selected" : ""}>${esc(c.name)}</option>`).join("");
      efforts = chosen ? chosen.efforts : [...new Set(g.codex_models.flatMap((x) => x.efforts))];
    }
    const effortOpts = `<option value="" ${!m.effort ? "selected" : ""}>自动</option>`
      + efforts.map((e) => `<option value="${esc(e)}" ${e === m.effort ? "selected" : ""}>${esc(effName(e))}</option>`).join("");
    return row(`${esc(m.name)}${m.hands ? " 🖐" : ""}`, m.kind === "claude" ? "用你的 Claude 账号" : "用你的 ChatGPT 账号（Codex）",
      `<select data-member-model="${esc(m.id)}">${models}</select> <select data-member-effort="${esc(m.id)}" title="思考深度：越高越聪明，也越慢、越费额度">${effortOpts}</select>`);
  }).join("");
  const keys = (g?.keys || []).map((k) => `<div class="key-row"><div>${esc(k.label)}<div class="state ${k.set ? "set" : ""}">${k.set ? "已填写 " + esc(k.hint) : "没填"}</div></div>
      <input type="password" data-key="${esc(k.name)}" placeholder="${k.set ? "留空不改；要换就粘贴新的" : "粘贴密钥"}" autocomplete="off">
      <button class="btn small" data-save-key="${esc(k.name)}" type="button">保存</button></div>`).join("");
  const r = s.research || {};
  const research = r.summary
    ? `<div class="set-desc" style="padding:10px 0 4px">${esc(timeText(r.time))} 研究了 ${esc((r.projects || []).join("、"))}：${esc(r.summary)}</div>${(r.top || []).length ? `<ol class="research-top">${r.top.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : ""}`
    : `<div class="set-desc" style="padding:10px 0 4px">还没有研究报告。每天凌晨 3 点左右，额度够的话会自动研究一次。</div>`;
  const slots = s.model_slots;
  const tabs = SETTINGS_TABS.map(([id, label]) =>
    `<button class="settings-tab" type="button" data-tab="${id}" aria-pressed="${S.settingsTab === id}">${label}</button>`
  ).join("");
  const show = (id) => S.settingsTab === id ? "" : "hidden";
  const claudeLogin = s.claude_login ? "已登录（用会员额度，不用填密钥）"
    : s.claude_login_pending ? "正在检查登录状态"
    : s.mac ? "还没登录，请在终端运行 claude auth login" : "还没登录，点右边按钮后按弹出窗口里的提示登录";
  $("#settings").innerHTML = `
    <header class="workspace-head settings-head"><h1>设置</h1></header>
    <nav class="settings-tabs" aria-label="设置分区">${tabs}</nav>
    <div ${show("models")}>
      <section class="card model-card"><h2>本地模型</h2>
        ${slots ? `<div class="model-fields">
          ${modelField("实时对话", "voice_mode", slots.voice, s.voice_mode)}
          ${modelField("语音转写", "transcription_model", slots.transcription, s.transcription_model)}
          ${modelField("本地推理", "reasoning_model", slots.reasoning, s.reasoning_model)}
        </div>` : row("实时对话", "重启星期八后可选择语音转写和本地推理模型", `<select data-set="voice_mode" aria-label="实时对话"><option value="wakeword" ${minicpm ? "" : "selected"}>唤醒词对话</option><option value="minicpm" ${minicpm ? "selected" : ""}>MiniCPM-o 4.5</option></select>`)}
      </section>
      <section class="card"><h2>语音设置</h2>
      ${row("语音转给调度员", "把明显的多项任务或点名分派交给群聊", switchHtml("voice_dispatch", s.voice_dispatch))}
      ${minicpm ? "" : `
      ${row("声音", "换了会用新声音打个招呼", `<select data-set="voice">${voices}</select>`)}
      ${row("说话时可以打断它", s.barge_in_available ? "它说话时你一开口它就停下来听你说；只是“嗯”一声它会接着说。打游戏开着外放、队友语音多时可以关掉" : "回声消除组件没装好，暂时不能用", switchHtml("barge_in", s.barge_in, !s.barge_in_available))}
      ${row("回声参考", s.mac ? "Mac 使用程序声音作为回声参考。" : "系统实际回放包含系统音量变化。设备不支持时自动使用程序声音。", `<select data-set="echo_reference" aria-label="回声参考" ${s.mac ? "disabled" : ""}><option value="program" ${s.echo_reference === "program" ? "selected" : ""}>程序声音</option><option value="system" ${s.echo_reference === "system" ? "selected" : ""}>系统实际回放</option></select>`)}
      ${row("说话慢一点也等我", "开口前、句中停顿和接着说时都多等一会儿", switchHtml("slow_speech", s.slow_speech))}
      ${row("回答完再听多久", `这段时间里接着说不用再喊名字。${s.slow_speech ? "慢速设置会延长所选时长。" : ""}它在问你问题时会多等一会儿。随时说“静默”“退下吧”马上回去待命`,
            `<select data-set="follow_up">${s.follow_up_choices.map((c) => `<option value="${c.value}" ${c.value === s.follow_up ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</select>`)}`}
      ${row("暂停监听", "关掉麦克风", switchHtml("pause", s.paused))}
      ${row("右下角状态窗", s.mac ? "Mac 上暂不可用" : "", `<select data-set="overlay" ${s.mac ? "disabled" : ""}><option value="auto" ${s.overlay_mode === "auto" ? "selected" : ""}>自动（唤醒时显示，待命 3 秒后隐藏）</option><option value="always" ${s.overlay_mode === "always" ? "selected" : ""}>一直显示</option><option value="off" ${s.overlay_mode === "off" ? "selected" : ""}>不显示</option></select>`)}
    </section>
    <div class="card"><h2>回答者</h2>
      <div class="radio-list">${brains}</div>
      ${row("语音大脑用哪个 Claude 模型", esc(s.usage_today), `<select data-set="claude_model">${models}</select>`)}
      ${s.mac ? "" : row("本地做不成时，自动请 Claude 接手", "会用你的 Claude 额度", switchHtml("auto_help", s.auto_help))}
      ${row("Claude 账号", claudeLogin, `<button class="btn small ${s.claude_login ? "" : "primary"}" data-do="login_claude" type="button">${s.claude_login ? "重新登录" : "登录 Claude 账号"}</button>`)}
    </div>
    </div>
    <div ${show("group")}>
    <div class="card"><h2>群成员的模型与思考深度</h2>
      ${g ? groupModels || `<div class="set-desc" style="padding:10px 0">群里还没有 Claude 或 GPT。</div>` : `<p class="fine">群聊设置暂未加载。</p>`}
      ${g ? row("动手后让另一个 AI 检查一遍", "一个做完，另一个当检查员看结果；发现问题，动手的那个按意见再改一次", switchHtml("group_review", g.review)) : ""}    </div>
    </div>
    <div ${show("system")}>
    <div class="card"><h2>本机保存的 API 密钥</h2>${g ? keys : `<p class="fine">密钥状态暂未加载。</p>`}<div style="height:8px"></div></div>
    <div class="card"><h2>夜间研究</h2>${research}
      <div class="btn-row"><button class="btn small" data-folder="research" type="button">打开研究日报文件夹</button></div></div>
    <div class="card"><h2>其他</h2>
      ${row("开机自动启动", s.mac ? "Mac 版通过启动脚本手动打开" : "", switchHtml("autostart", s.autostart, s.mac))}
      <div class="btn-row">
        <button class="btn small" data-folder="tasks" type="button">打开 AI 任务文件夹</button>
        <button class="btn small" data-folder="memory" type="button">打开记忆文件夹</button>
        <button class="btn small" data-folder="logs" type="button">打开日志文件夹</button>
      </div>
      ${row("退出星期八", "彻底关闭（先复盘、卸载模型）。之后可以从桌面快捷方式再打开", `<button class="btn small danger" data-do="quit" type="button">退出</button>`)}
    </div>
    </div>`;
}

// ---------------------------------------------------------------- 事件绑定
function bindComposer(formSel, inputSel, send) {
  const form = $(formSel), input = $(inputSel);
  input.addEventListener("input", () => autoGrow(input));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (text) await send(text, input);
  });
}

function confirmGroupDraft(id) {
  if (!groupDraft || groupDraft.id !== id) return;
  const input = $("#group-input");
  if (input.value.trim() === groupDraft.text) {
    input.value = "";
    autoGrow(input);
  }
  groupDraft = null;
}

async function sendGroupDraft(text, input) {
  const button = input.form.querySelector(".send");
  if (button.disabled) return;

  const draft = groupDraft?.text === text
    ? groupDraft
    : { id: crypto.randomUUID().replaceAll("-", ""), text };
  groupDraft = draft;
  button.disabled = true;
  try {
    const result = await act("group_send", { text, request_id: draft.id }, true);
    if (result.saved === true) confirmGroupDraft(draft.id);
    else if (result.connectionError && groupDraft === draft) toast("发送结果未确认，草稿已保留", true);
  } finally {
    button.disabled = false;
  }
}

// 转写预览独立于草稿，定稿后追加一次。
let micSession = 0;
let micFinished = 0;
let micStarted = 0;

function appendDictation(text) {
  const input = $("#group-input");
  const space = input.value && !/\s$/.test(input.value) ? " " : "";
  input.value += space + text;
  autoGrow(input);
  if (S.page === "group") input.focus();
}

function updateMicClock() {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - micStarted));
  $("#dictation-time").textContent = micStarted
    ? `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}` : "";
}

function setMic(phase, info = {}) {
  const mic = $("#group-mic");
  const recording = phase === "recording";
  const busy = phase === "busy" || phase === "starting";
  mic.classList.toggle("recording", recording);
  mic.classList.toggle("busy", busy);
  mic.disabled = busy;
  mic.dataset.state = phase;
  mic.setAttribute("aria-pressed", String(recording));
  mic.setAttribute("aria-label", recording ? "停止录音并转写" : busy ? "处理中" : "语音输入");
  $("#dictation-panel").hidden = phase === "idle";
  $("#dictation-panel").dataset.phase = phase;
  $("#dictation-cancel").hidden = !recording;
  if (info.started) micStarted = info.started;
  if (phase === "idle") micStarted = 0;
  updateMicClock();
  $("#dictation-title").textContent = recording ? "正在录音" : phase === "starting" ? "正在打开麦克风" : "正在转写";
  const preview = $("#dictation-preview");
  if (phase === "idle" || phase === "starting") preview.textContent = "";
  if (info.partial) preview.textContent = info.partial;
  if (recording && !preview.textContent) preview.textContent = "说完再点一次麦克风，转写会加入草稿。";
}

function applyDictation(info, toastResult = false) {
  const id = info.session || 0;
  if (id && (id < micSession || id === micFinished)) return;
  if (id) micSession = id;
  if (info.recording || info.processing) {
    setMic(info.processing ? "busy" : "recording", info);
    return;
  }
  if (id && typeof info.ok === "boolean") micFinished = id;
  setMic("idle");
  if (info.ok && info.text) appendDictation(info.text);
  if (info.message && (toastResult || info.ok)) toast(info.message, true);
  else if (info.auto && info.ok) toast("已到录音时限，转写已放进草稿");
}

async function toggleDictation() {
  const phase = $("#group-mic").dataset.state || "idle";
  if (phase === "idle") {
    setMic("starting");
    const r = await act("dictate_start", {}, true);
    if (r.ok) applyDictation(r);
    else setMic("idle");
  } else if (phase === "recording") {
    setMic("busy");
    applyDictation(await act("dictate_stop", {}, true));
  }
}

async function cancelDictation() {
  setMic("busy");
  await act("dictate_cancel", {}, true);
  setMic("idle");
}

function bind() {
  document.querySelectorAll(".nav-item").forEach((n) => n.addEventListener("click", () => showPage(n.dataset.page)));
  $("#pause-toggle").addEventListener("change", (e) => act("pause", { value: e.target.checked }, true).then(refreshState));
  $("#brain-select").addEventListener("change", (e) => act("brain", { value: e.target.value }).then(refreshState));
  $("#barge-chip").addEventListener("change", (e) => act("barge_in", { value: e.target.checked }, true).then(refreshState));

  bindComposer("#home-form", "#home-input", (text, input) => {
    input.value = "";
    autoGrow(input);
    return act("chat", { text }, true);
  });
  bindComposer("#group-form", "#group-input", sendGroupDraft);
  $("#group-mic").addEventListener("click", toggleDictation);
  $("#dictation-cancel").addEventListener("click", cancelDictation);
  for (const id of ["#home-settings", "#asr-settings"]) {
    $(id).addEventListener("click", () => { S.settingsTab = "models"; showPage("settings"); });
  }

  $("#mention-bar").addEventListener("click", (e) => {
    const b = e.target.closest(".mention");
    if (!b) return;
    const input = $("#group-input");
    input.value = `@${b.dataset.name} ` + input.value.replace(/^@\S+\s*/, "");
    input.focus(); autoGrow(input);
  });
  $("#group-stop")?.addEventListener("click", async (e) => {
    const button = e.currentTarget;
    button.disabled = true;
    try {
      await act("group_stop", {}, true);
    } finally {
      button.disabled = false;
    }
  });
  $("#group-continue").addEventListener("click", () => act("group_continue", {}, true));
  $("#hands-bar").addEventListener("click", (e) => {
    const b = e.target.closest("[data-hands]");
    if (b) act("group_hands", { id: b.dataset.hands });
  });
  // 点开某一步看细节：记住它开着（群里来了新消息、重画的时候不会自己收起来）
  $("#group-messages").addEventListener("toggle", (e) => {
    const d = e.target;
    if (!d.matches?.("details.step")) return;
    if (d.open) S.openSteps.add(d.dataset.id); else S.openSteps.delete(d.dataset.id);
  }, true);
  $("#group-clear").addEventListener("click", async () => {
    if (await confirmBox("清空群聊记录？旧记录会存档到 groupchat 文件夹，不会真的删掉。", "清空")) act("group_clear", {}, true);
  });
  $("#group-members-btn").addEventListener("click", () => showDrawer("members-drawer"));
  // 任务卡
  $("#group-tasks-btn").addEventListener("click", () => { showDrawer("tasks-drawer"); renderTasks(); });
  $("#tasks-close").addEventListener("click", () => { $("#tasks-drawer").hidden = true; });
  const openTask = (id) => {
    $("#tasks-drawer").hidden = false; $("#members-drawer").hidden = true; $("#memory-drawer").hidden = true;
    $("#projects-drawer").hidden = true;
    renderTasks();
    const el = $(`#task-list .task[data-id="${CSS.escape(id)}"]`);
    if (el) { el.scrollIntoView({ block: "nearest" }); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1200); }
  };
  $("#group-messages").addEventListener("click", async (e) => {
    const make = e.target.closest("[data-make-task]");
    if (make) {
      make.disabled = true;
      const r = await act("task_create", { msg_id: make.dataset.makeTask }, true);
      if (r.ok && r.task) { await refreshGroup(); openTask(r.task.id); }
      else make.disabled = false;
      return;
    }
    const link = e.target.closest("[data-open-task]");
    if (link) openTask(link.dataset.openTask);
  });
  $("#task-list").addEventListener("click", async (e) => {
    const card = e.target.closest(".task");
    const what = e.target.closest("[data-act]")?.dataset.act;
    if (!card || !what) return;
    const id = card.dataset.id;
    const t = (S.group?.tasks || []).find((x) => x.id === id);
    if (what === "continue") { act("task_continue", { id }, true); $("#tasks-drawer").hidden = true; }
    if (what === "refresh") act("task_refresh", { id });
    if (what === "done") act("task_done", { id, value: t?.status !== "已完成" });
    if (what === "delete" && await confirmBox(`删除任务卡「${t?.title}」？（群聊记录不动）`, "删除")) act("task_delete", { id }, true);
    if (what === "rename") {
      const title = e.target.closest(".task-title");
      const input = document.createElement("input");
      input.value = t?.title || ""; input.className = "task-title-input";
      title.replaceWith(input); input.focus(); input.select(); S.taskEditing = true;
      const done = async (save) => {
        S.taskEditing = false;
        if (save && input.value.trim() && input.value.trim() !== t?.title) await act("task_rename", { id, title: input.value }, true);
        refreshGroup();
      };
      input.addEventListener("keydown", (k) => { if (k.key === "Enter") input.blur(); if (k.key === "Escape") { input.dataset.cancel = "1"; input.blur(); } });
      input.addEventListener("blur", () => done(!input.dataset.cancel), { once: true });
    }
  });
  $("#drawer-close").addEventListener("click", () => { $("#members-drawer").hidden = true; });
  // 群聊的长期记忆：看、改、现在整理
  $("#group-memory-btn").addEventListener("click", () => {
    showDrawer("memory-drawer");
    S.memoryDirty = false; renderMemory(true);
  });
  $("#memory-close").addEventListener("click", () => { $("#memory-drawer").hidden = true; });
  $("#memory-text").addEventListener("input", () => { S.memoryDirty = true; });
  $("#memory-save").addEventListener("click", async () => {
    const r = await act("group_memory_save", { text: $("#memory-text").value }, true);
    if (r.ok) { S.memoryDirty = false; toast("记忆已保存"); }
  });
  $("#memory-update").addEventListener("click", () => act("group_memory_update", {}));
  // 项目：切换、新建、改名和关联文件夹
  $("#project-select").addEventListener("change", async (e) => {
    const r = await act("project_switch", { value: e.target.value });
    if (!r.ok) renderProjects(true);  // 切不过去（比如有 AI 正在回话）：下拉框改回当前项目
  });
  $("#group-projects-btn").addEventListener("click", () => { showDrawer("projects-drawer"); renderProjects(true); });
  $("#projects-close").addEventListener("click", () => { $("#projects-drawer").hidden = true; });
  $("#project-save").addEventListener("click", () => {
    const cur = S.group?.projects?.current;
    if (cur) act("project_update", { id: cur, name: $("#project-name").value, folders: $("#project-folders").value });
  });
  $("#project-create").addEventListener("click", async () => {
    const r = await act("project_create", { name: $("#project-new-name").value, folders: $("#project-new-folders").value });
    if (r.ok) { $("#project-new-name").value = ""; $("#project-new-folders").value = ""; }
  });

  $("#member-list").addEventListener("click", async (e) => {
    const card = e.target.closest(".member");
    const what = e.target.closest("[data-act]")?.dataset.act;
    if (!card || !what) return;
    const id = card.dataset.id;
    if (what === "edit") { S.openMember = S.openMember === id ? null : id; renderMembers(); }
    if (what === "hands") act("group_hands", { id: memberById(id)?.hands ? "" : id });
    if (what === "remove") {
      const m = memberById(id);
      if (await confirmBox(`把 ${m?.name} 从成员里删掉？（只删设置，聊天记录和密钥不动）`, "删除")) act("group_remove", { id }, true);
    }
    if (what === "save") {
      const changes = {};
      card.querySelectorAll("[data-f]").forEach((el) => { changes[el.dataset.f] = el.value; });
      for (const el of card.querySelectorAll("[data-key]")) {
        if (el.value.trim()) await act("set_key", { key: el.dataset.key, value: el.value.trim() }, true);
      }
      const r = await act("group_update", { id, changes }, true);
      if (r.ok) { S.openMember = null; toast("已保存"); refreshGroup(); }
    }
  });
  $("#member-list").addEventListener("change", (e) => {
    if (e.target.dataset.act !== "toggle") return;
    const id = e.target.closest(".member").dataset.id;
    act("group_update", { id, changes: { enabled: e.target.checked } }, true);
  });
  $("#preset-grid").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-preset]");
    if (!b) return;
    const r = await act("group_add", { value: b.dataset.preset });
    if (r.ok && r.member) { S.openMember = r.member.id; }
  });

  const settings = $("#settings");
  settings.addEventListener("change", async (e) => {
    const el = e.target;
    if (el.name === "brain") { await act("brain", { value: el.value }); return refreshState(); }
    if (el.dataset.memberModel) {
      await act("group_model", { id: el.dataset.memberModel, model: el.value, effort: "" }, true);
      toast("已换模型（思考深度改回“自动”）"); return refreshGroup();
    }
    if (el.dataset.memberEffort) {
      await act("group_model", { id: el.dataset.memberEffort, effort: el.value }, true);
      toast("已改思考深度"); return refreshGroup();
    }
    if (el.dataset.set === "group_review") { await act("group_review", { value: el.checked }); return refreshGroup(); }
    const key = el.dataset.set;
    if (!key) return;
    if (key === "voice") await act("voice", { value: el.value, preview: true });
    else if (["voice_mode", "transcription_model", "reasoning_model", "echo_reference"].includes(key)) {
      await act(key, { value: el.value });
    }
    else if (key === "claude_model") await act("claude_model", { value: el.value });
    else if (key === "overlay") await act("overlay", { value: el.value }, true);
    else if (key === "follow_up") await act("follow_up", { value: Number(el.value) });
    else await act(key, { value: el.checked }, true);
    refreshState();
  });
  settings.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.tab) { S.settingsTab = b.dataset.tab; renderSettings(); return; }
    if (b.dataset.folder) act("open_folder", { value: b.dataset.folder }, true);
    if (b.dataset.do === "login_claude") act("login_claude");
    if (b.dataset.do === "quit" && await confirmBox("彻底退出星期八？退出后语音和这个窗口都不能用了。", "退出")) {
      await act("quit", {}, true); toast("正在退出……");
    }
    if (b.dataset.saveKey) {
      const input = settings.querySelector(`input[data-key="${CSS.escape(b.dataset.saveKey)}"]`);
      const value = input.value.trim();
      if (!value && !(await confirmBox("输入框是空的：要把这个密钥清空吗？", "清空"))) return;
      const r = await act("set_key", { key: b.dataset.saveKey, value }, true);
      if (r.ok) { toast(value ? "密钥已保存" : "密钥已清空"); input.value = ""; input.blur(); refreshGroup(); refreshState(); }
    }
  });
}

bind();
connect();
requestAnimationFrame(animate);
setInterval(() => { if (micStarted) updateMicClock(); }, 500);
setInterval(() => {
  if (!S.connected) return;
  if (S.page !== "settings" || refreshFailures.has("/api/state")) refreshState();
  if (refreshFailures.has("/api/chat")) refreshChat();
  if (refreshFailures.has("/api/group")) refreshGroup();
}, 30000);
