import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const STYLE_ID = "comfyui-omniscience-style";
const PANEL_ID = "comfyui-omniscience-panel";

const state = {
  tasks: [],
  task: null,
  messages: [],
  globalMessages: [],
  globalChatActive: false,
  globalBusy: false,
  context: [],
  contextStats: null,
  settings: null,
  mcp: [],
  skills: [],
  selectedSkillIds: [],
  skillMenuOpen: false,
  skillQuery: "",
  skillMenuIndex: 0,
  editingSkillId: "",
  cursor: -1,
  view: "chat",
  busy: false,
  cancelling: false,
  error: "",
  polling: false,
  projectFolderRequest: null,
  currentWorkflow: null,
  currentWorkflowSignature: "",
  currentWorkflowTaskId: "",
};

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function button(label, className = "") {
  const node = el("button", className, label);
  node.type = "button";
  return node;
}

async function request(path, options = {}) {
  const response = await api.fetchApi(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  try { data = await response.json(); } catch { /* status is enough below */ }
  if (!response.ok) throw new Error(data?.error || `请求失败 (${response.status})`);
  return data;
}

function ensureStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const link = document.createElement("link");
  link.id = STYLE_ID;
  link.rel = "stylesheet";
  link.href = new URL("./omniscience.css", import.meta.url).href;
  document.head.appendChild(link);
}

function lines(value) {
  const fragment = document.createDocumentFragment();
  for (const line of String(value ?? "").split("\n")) fragment.appendChild(el("div", "cline-line", line));
  return fragment;
}

function currentTaskId() { return state.task?.id || ""; }

function displayedMessages() { return state.globalChatActive ? state.globalMessages : state.messages; }

function displayedBusy() { return state.globalChatActive ? state.globalBusy : state.busy; }

function serializeCurrentCanvasWorkflow() {
  if (!app?.graph || typeof app.graph.serialize !== "function") {
    throw new Error("ComfyUI 当前画布接口不可用，请确认 Comfyui-omniscience 已作为 ComfyUI 前端扩展加载。");
  }
  const workflow = app.graph.serialize();
  if (!workflow || typeof workflow !== "object" || Array.isArray(workflow)) {
    throw new Error("ComfyUI 当前画布没有返回有效的工作流对象。");
  }
  return workflow;
}

async function syncCurrentWorkflow({ force = false, silent = true } = {}) {
  if (!currentTaskId()) return null;
  try {
    const workflow = serializeCurrentCanvasWorkflow();
    const signature = JSON.stringify(workflow);
    if (!force && state.currentWorkflowTaskId === currentTaskId() && state.currentWorkflowSignature === signature) {
      return state.currentWorkflow;
    }
    const result = await request(`/cline/tasks/${encodeURIComponent(currentTaskId())}/current-workflow`, {
      method: "POST",
      body: JSON.stringify({ workflow, source: "comfyui_canvas" }),
    });
    state.currentWorkflow = result.current || null;
    state.currentWorkflowSignature = signature;
    state.currentWorkflowTaskId = currentTaskId();
    return state.currentWorkflow;
  } catch (error) {
    if (!silent) throw error;
    console.warn("[Comfyui-omniscience] current workflow sync failed", error);
    return null;
  }
}

function requestProjectFolder() {
  if (state.projectFolderRequest) {
    state.projectFolderRequest.input.focus();
    return state.projectFolderRequest.promise;
  }

  const panel = document.getElementById(PANEL_ID);
  if (!panel) return Promise.resolve(null);

  const backdrop = el("div", "cline-project-dialog-backdrop");
  const dialog = el("form", "cline-project-dialog");
  const title = el("h3", "", "新建 ComfyUI 节点项目");
  const description = el("p", "cline-hint", "请输入项目文件夹名称。项目会创建在设置的项目父目录下。");
  const label = el("label", "cline-field");
  label.appendChild(el("span", "", "项目文件夹名称"));
  const input = document.createElement("input");
  input.type = "text";
  input.name = "folder";
  input.placeholder = "例如：my-comfy-node";
  input.autocomplete = "off";
  label.appendChild(input);
  const error = el("p", "cline-error");
  error.hidden = true;
  const actions = el("div", "cline-dialog-actions");
  const cancel = button("取消", "cline-secondary");
  const confirm = button("创建项目", "cline-primary");
  confirm.type = "submit";
  actions.append(cancel, confirm);
  dialog.append(title, description, label, error, actions);
  backdrop.appendChild(dialog);
  panel.appendChild(backdrop);

  let finish;
  const promise = new Promise((resolve) => { finish = resolve; });
  const close = (value) => {
    if (!state.projectFolderRequest) return;
    state.projectFolderRequest = null;
    backdrop.remove();
    finish(value);
  };
  const submit = (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) {
      error.textContent = "项目文件夹名称不能为空。";
      error.hidden = false;
      input.focus();
      return;
    }
    close(value);
  };
  dialog.onsubmit = submit;
  cancel.onclick = () => close(null);
  backdrop.onclick = (event) => {
    if (event.target === backdrop) close(null);
  };
  input.onkeydown = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close(null);
    }
  };
  state.projectFolderRequest = { input, promise };
  window.setTimeout(() => input.focus(), 0);
  return promise;
}

function statusText(status) {
  return { idle: "空闲", running: "运行中", waiting_approval: "运行中", cancelled: "已中断，可继续" }[status] || status || "空闲";
}

function statusClass(status) {
  return { idle: "idle", running: "running", waiting_approval: "running", cancelled: "idle" }[status] || "idle";
}

function taskDate(task) {
  const timestamp = Number(task.updated_at || task.created_at);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "";
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp * 1000));
}

function promptLengthText(stats) {
  if (!stats) return currentTaskId() ? "提示词：读取中…" : "提示词：—";
  const tokens = Number(stats.prompt_tokens);
  const chars = Number(stats.prompt_chars);
  const format = (value) => Number.isFinite(value) ? value.toLocaleString() : "—";
  return `提示词：约 ${format(tokens)} Token · ${format(chars)} 字符`;
}

function renderMessages() {
  const log = document.querySelector(`#${PANEL_ID} .cline-messages`);
  if (!log) return;
  const messages = displayedMessages();
  log.replaceChildren();
  if (!messages.length) {
    const welcome = el("div", "cline-welcome");
    welcome.append(el("strong", "", state.globalChatActive ? "全局对话" : "Comfyui-omniscience for ComfyUI"));
    welcome.append(el("p", "", state.globalChatActive
      ? "这是独立于所有任务的全局对话。你可以讨论想法、提问，或提供公开 GitHub Skill 链接并明确要求安装；不会读取或修改项目文件。"
      : "创建或选择一个任务，然后描述要检查、规划或修改的内容。Plan 模式只允许读取或浏览；Act 模式会直接执行允许的写入、命令及外部工具调用。"));
    log.appendChild(welcome);
  }
  for (const message of messages) {
    const role = message.role || "assistant";
    const article = el("article", `cline-message ${role}`);
    article.appendChild(el("div", "cline-message-role", role === "user" ? "你" : role === "assistant" ? "Omniscience" : "工具结果"));
    const content = el("div", "cline-message-content");
    content.appendChild(lines(message.content || ""));
    article.appendChild(content);
    if (message.tool_calls?.length) {
      article.appendChild(el("div", "cline-tool-calls", `工具调用：${message.tool_calls.map((call) => call.function?.name || "未知工具").join("、")}`));
    }
    log.appendChild(article);
  }
  if (state.error) log.appendChild(el("div", "cline-error", state.error));
  if (displayedBusy() || (!state.globalChatActive && state.task?.status === "running")) log.appendChild(el("div", "cline-status", "Omniscience 正在思考或执行…"));
  log.scrollTop = log.scrollHeight;
}

function renderTasks() {
  const list = document.querySelector(`#${PANEL_ID} .cline-task-list`);
  if (!list) return;
  list.replaceChildren();
  if (!state.tasks.length) {
    list.appendChild(el("p", "cline-task-empty cline-hint", "还没有任务节点。"));
    return;
  }
  for (const task of state.tasks) {
    const active = !state.globalChatActive && task.id === currentTaskId();
    const row = button("", `cline-task-node${active ? " active" : ""}`);
    const port = el("span", `cline-task-node-port ${statusClass(task.status)}`);
    port.setAttribute("aria-hidden", "true");
    const header = el("span", "cline-task-node-header");
    header.append(
      el("strong", "cline-task-title", task.folder || task.title || "未命名项目"),
      el("span", `cline-task-node-status ${statusClass(task.status)}`, statusText(task.status)),
    );
    const description = el("span", "cline-task-node-description", task.title || "未命名任务");
    const footer = el("span", "cline-task-node-footer");
    footer.append(
      el("span", "cline-task-node-mode", task.mode === "plan" ? "Plan" : "Act"),
      ...(active ? [el("span", "cline-task-prompt-length", promptLengthText(state.contextStats).replace("提示词：", ""))] : []),
      el("span", "cline-task-node-date", taskDate(task)),
    );
    row.append(port, header, description, footer);
    row.title = task.id;
    row.setAttribute("aria-current", active ? "true" : "false");
    row.onclick = () => loadTask(task.id);
    const item = el("div", "cline-task-history-item");
    item.appendChild(row);
    list.appendChild(item);
  }
}

function renderHeader() {
  const title = document.querySelector(`#${PANEL_ID} .cline-current-task`);
  const promptStats = document.querySelector(`#${PANEL_ID} .cline-prompt-stats`);
  const status = document.querySelector(`#${PANEL_ID} .cline-task-status`);
  if (title) title.textContent = state.globalChatActive
    ? "全局对话（独立于任务）"
    : state.task ? `${state.task.folder || state.task.title} · ${state.task.title || "未命名任务"}` : "未选择任务";
  if (promptStats) {
    promptStats.textContent = state.globalChatActive ? "提示词：—" : promptLengthText(state.contextStats);
    promptStats.title = state.globalChatActive ? "全局对话不包含任务上下文或工具定义。" : "当前模型提示词估算，包括系统指令、工具定义和任务对话。";
  }
  if (status) status.textContent = state.globalChatActive ? (state.globalBusy ? "思考中" : "") : state.task ? statusText(state.task.status) : "";
  const globalChat = document.querySelector(`#${PANEL_ID} .cline-global-chat-toggle`);
  if (globalChat) {
    globalChat.textContent = state.globalChatActive ? "返回任务对话" : "全局对话";
    globalChat.classList.toggle("active", state.globalChatActive);
    globalChat.title = state.globalChatActive ? "返回当前任务对话" : "打开独立于任务的全局对话";
  }
  const cancel = document.querySelector(`#${PANEL_ID} .cline-cancel-task`);
  if (cancel) {
    const cancellable = !state.globalChatActive && ["running", "waiting_approval"].includes(state.task?.status);
    cancel.hidden = !cancellable;
    cancel.disabled = state.cancelling;
    cancel.textContent = state.cancelling ? "正在取消…" : "强制取消";
  }
  const composerInput = document.querySelector(`#${PANEL_ID} textarea[data-composer]`);
  const send = document.querySelector(`#${PANEL_ID} .cline-send-message`);
  // Cancellation stops only the current run. The task remains resumable, so
  // the composer always stays available for the next turn in this node.
  if (composerInput) {
    composerInput.disabled = false;
    composerInput.placeholder = state.globalChatActive ? "输入想讨论的问题…" : "描述你要完成的编程任务…";
  }
  if (send) send.disabled = displayedBusy();
  const clear = document.querySelector(`#${PANEL_ID} .cline-clear-history`);
  if (clear) {
    clear.textContent = state.globalChatActive ? "清理全局对话" : "清理历史对话";
    clear.disabled = state.globalChatActive ? state.globalBusy : !state.task || state.busy || ["running", "waiting_approval"].includes(state.task?.status);
  }
  for (const mode of ["plan", "act"]) {
    const control = document.querySelector(`#${PANEL_ID} [data-mode="${mode}"]`);
    if (control) {
      control.classList.toggle("active", state.task?.mode === mode);
      control.disabled = state.globalChatActive || !state.task || state.busy;
    }
  }
}

function renderContext() {
  const contextArea = document.querySelector(`#${PANEL_ID} .cline-context-label`);
  if (contextArea) contextArea.hidden = state.globalChatActive;
  if (state.globalChatActive) return;
  const label = contextArea;
  if (label) label.textContent = state.context.length ? `已选择 ${state.context.length} 个文件上下文` : "点击文件可加入下一条消息的上下文";
}

function renderSkillSelector() {
  const area = document.querySelector(`#${PANEL_ID} .cline-skill-selector`);
  if (!area) return;
  area.hidden = false;
  area.replaceChildren();
  const heading = el("div", "cline-skill-selector-heading");
  heading.append(el("strong", "", "当前会话 Skill"));
  const manage = button("管理 Skill", "cline-secondary");
  manage.onclick = () => setView("skills");
  heading.appendChild(manage);
  area.appendChild(heading);
  const enabled = state.skills.filter((skill) => skill.enabled);
  if (!enabled.length) {
    const hint = el("p", "cline-hint", "还没有启用的 Skill。点击“管理 Skill”创建一个。");
    area.appendChild(hint);
    return;
  }
  const selected = el("div", "cline-selected-skills");
  for (const skill of enabled.filter((item) => state.selectedSkillIds.includes(item.id))) {
    const chip = el("span", "cline-skill-chip");
    const remove = button("×", "cline-skill-chip-remove");
    remove.title = `移除 ${skill.name}`;
    remove.onclick = () => {
      state.selectedSkillIds = state.selectedSkillIds.filter((id) => id !== skill.id);
      if (!state.globalChatActive) loadContextStats().catch(() => {});
      renderPanel();
    };
    chip.append(el("span", "", skill.name), remove);
    selected.appendChild(chip);
  }
  if (selected.childNodes.length) area.appendChild(selected);
  area.appendChild(el("p", "cline-hint", "输入框键入 / 呼起全局 Skill 列表；选中的 Skill 会用于后续消息，不属于任何任务。"));
  renderSkillMenu(area, enabled);
}

function skillCommand(value) {
  const match = String(value || "").match(/(?:^|\s)\/([^\s]*)$/);
  if (!match) return null;
  const raw = match[1] || "";
  return {
    query: raw.toLocaleLowerCase() === "skill" ? "" : raw,
    start: match.index + match[0].lastIndexOf("/"),
    end: match.index + match[0].lastIndexOf("/") + raw.length + 1,
  };
}

function matchingSkills() {
  const query = state.skillQuery.trim().toLocaleLowerCase();
  return state.skills.filter((skill) => skill.enabled && (!query || `${skill.name} ${skill.description || ""}`.toLocaleLowerCase().includes(query)));
}

function renderSkillMenu(area, enabled = state.skills.filter((skill) => skill.enabled)) {
  if (!state.skillMenuOpen) return;
  const menu = el("div", "cline-skill-command-menu");
  const matches = matchingSkills();
  if (!matches.length) {
    menu.appendChild(el("p", "cline-hint", "没有匹配的启用 Skill。"));
  } else {
    const list = el("div", "cline-skill-command-list");
    matches.forEach((skill, index) => {
      const item = button("", `cline-skill-command-item${index === state.skillMenuIndex ? " active" : ""}`);
      item.append(el("strong", "", skill.name), el("span", "cline-skill-command-description", skill.description || "无描述"));
      item.title = skill.description || skill.name;
      item.onclick = () => selectSkillFromMenu(skill.id);
      list.appendChild(item);
    });
    menu.appendChild(list);
  }
  area.appendChild(menu);
}

function selectSkillFromMenu(skillId) {
  const input = document.querySelector(`#${PANEL_ID} textarea[data-composer]`);
  const command = skillCommand(input?.value || "");
  if (!state.selectedSkillIds.includes(skillId)) state.selectedSkillIds.push(skillId);
  if (input && command) {
    input.value = `${input.value.slice(0, command.start)}${input.value.slice(command.end)}`.replace(/\s+$/, "");
    input.dispatchEvent(new Event("input"));
  }
  state.skillMenuOpen = false;
  state.skillQuery = "";
  state.skillMenuIndex = 0;
  renderPanel();
  input?.focus();
}

function renderPanel() {
  renderTasks();
  renderHeader();
  renderMessages();
  renderContext();
  renderSkillSelector();
}

async function loadTasks() {
  const result = await request("/cline/tasks");
  state.tasks = result.tasks || [];
  renderTasks();
}

async function loadGlobalChat() {
  const result = await request("/cline/global-chat");
  state.globalMessages = result.messages || [];
  return state.globalMessages;
}

async function toggleGlobalChat() {
  if (state.globalChatActive) {
    state.globalChatActive = false;
    state.globalBusy = false;
    state.error = "";
    setView("chat");
    renderPanel();
    return;
  }
  state.error = "";
  try {
    await loadGlobalChat();
    state.globalChatActive = true;
    state.skillMenuOpen = false;
    state.skillQuery = "";
    state.skillMenuIndex = 0;
    setView("chat");
    renderPanel();
  } catch (error) {
    showError(error);
  }
}

function applyEvents(events) {
  for (const event of events) {
    state.cursor = Math.max(state.cursor, Number(event.seq ?? -1));
    const payload = event.payload || {};
    if (event.kind === "task_started") state.busy = true;
    if (event.kind === "task_finished" || event.kind === "task_failed" || event.kind === "task_cancelled") state.busy = false;
    if (event.kind === "task_failed") state.error = payload.error || "任务执行失败。";
    if (event.kind === "task_cancelled") {
      state.error = "本次运行已被强制取消，可以继续提问。";
    }
  }
}

async function loadTask(taskId, { resetSkills = true } = {}) {
  state.globalChatActive = false;
  state.globalBusy = false;
  state.error = "";
  state.cursor = -1;
  state.context = [];
  state.contextStats = null;
  if (resetSkills) {
    state.selectedSkillIds = [];
    state.skillMenuOpen = false;
    state.skillQuery = "";
    state.skillMenuIndex = 0;
  }
  state.currentWorkflow = null;
  state.currentWorkflowSignature = "";
  state.currentWorkflowTaskId = "";
  try {
    const result = await request(`/cline/tasks/${encodeURIComponent(taskId)}`);
    state.task = result.task;
    state.messages = result.messages || [];
    await loadSkills();
    const liveWorkflow = await request(`/cline/tasks/${encodeURIComponent(taskId)}/current-workflow`);
    state.currentWorkflow = liveWorkflow.current || null;
    const events = await request(`/cline/tasks/${encodeURIComponent(taskId)}/events?after=-1`);
    applyEvents(events.events || []);
    state.busy = result.task.status === "running";
  } catch (error) {
    state.error = error.message;
  }
  try {
    await loadContextStats();
  } catch (error) {
    state.contextStats = null;
    console.warn("[Comfyui-omniscience] prompt length refresh failed", error);
  }
  renderPanel();
  await syncCurrentWorkflow({ silent: true });
  if (state.view === "settings") loadSettings().then(renderSettings).catch(showError);
  if (state.view === "checkpoints") renderCheckpoints().catch(showError);
  if (state.view === "skills") loadSkills().then(renderSkills).catch(showError);
}

async function refreshTask() {
  if (!currentTaskId()) return;
  const result = await request(`/cline/tasks/${encodeURIComponent(currentTaskId())}`);
  if (result.task.id !== currentTaskId()) return;
  state.task = result.task;
  state.messages = result.messages || [];
  try { await loadSkills(); } catch (error) { console.warn("[Comfyui-omniscience] skill refresh failed", error); }
  state.busy = result.task.status === "running";
  try {
    await loadContextStats();
  } catch (error) {
    state.contextStats = null;
    console.warn("[Comfyui-omniscience] prompt length refresh failed", error);
  }
  renderPanel();
  if (state.view === "settings") {
    try {
      await loadContextStats();
      renderSettings();
    } catch (error) { console.warn("[Comfyui-omniscience] context stats refresh failed", error); }
  }
}

async function loadSkills() {
  const result = await request("/cline/skills");
  state.skills = result.skills || [];
  const available = new Set(state.skills.filter((skill) => skill.enabled).map((skill) => skill.id));
  state.selectedSkillIds = state.selectedSkillIds.filter((id) => available.has(id));
  renderSkillSelector();
  return state.skills;
}

async function pollEvents() {
  if (state.polling || state.globalChatActive || !currentTaskId()) return;
  state.polling = true;
  try {
    await syncCurrentWorkflow();
    const result = await request(`/cline/tasks/${encodeURIComponent(currentTaskId())}/events?after=${state.cursor}`);
    if ((result.events || []).length) {
      applyEvents(result.events);
      await refreshTask();
      await loadTasks();
    }
  } catch (error) {
    console.warn("[Comfyui-omniscience] event polling failed", error);
  } finally {
    state.polling = false;
  }
}

async function createTask() {
  state.error = "";
  const folderName = await requestProjectFolder();
  if (!folderName) return;
  try {
    const task = await request("/cline/tasks", { method: "POST", body: JSON.stringify({ title: folderName, folder: folderName, mode: "act" }) });
    await loadTasks();
    await loadTask(task.id);
    setView("chat");
  } catch (error) {
    state.error = error.message;
    renderPanel();
  }
}

async function clearHistory() {
  if (state.globalChatActive) {
    if (state.globalBusy) return;
    if (!window.confirm("确定清理全局对话吗？这不会影响任何任务的历史记录。")) return;
    try {
      await request("/cline/global-chat/history", { method: "DELETE" });
      state.globalMessages = [];
      state.error = "全局对话已清理。";
      renderPanel();
    } catch (error) {
      state.error = error.message;
      renderPanel();
    }
    return;
  }
  const taskId = currentTaskId();
  if (!taskId || state.busy) return;
  const folder = state.task?.folder || state.task?.title || "当前项目";
  if (!window.confirm(`确定清理“${folder}”的历史对话吗？\n项目文件夹和文件不会被删除。`)) return;
  try {
    await request(`/cline/tasks/${encodeURIComponent(taskId)}/history`, { method: "DELETE" });
    state.messages = [];
    state.cursor = -1;
    state.error = "当前项目的历史对话已清理。";
    try {
      await loadContextStats();
    } catch (error) {
      state.contextStats = null;
      console.warn("[Comfyui-omniscience] prompt length refresh failed", error);
    }
    renderPanel();
    await loadTasks();
  } catch (error) {
    state.error = error.message;
    renderPanel();
  }
}

async function setMode(mode) {
  if (!currentTaskId() || state.busy || state.task.mode === mode) return;
  try {
    state.task = await request(`/cline/tasks/${encodeURIComponent(currentTaskId())}/mode`, { method: "POST", body: JSON.stringify({ mode }) });
    await loadContextStats();
    await loadTasks();
    renderPanel();
  } catch (error) { showError(error); }
}

async function sendMessage() {
  const input = document.querySelector(`#${PANEL_ID} textarea[data-composer]`);
  const content = input?.value.trim();
  if (!content || displayedBusy()) return;
  state.error = "";
  try {
    if (state.globalChatActive) {
      const previousSkillIds = new Set(state.skills.map((skill) => skill.id));
      input.value = "";
      state.skillMenuOpen = false;
      state.skillQuery = "";
      state.skillMenuIndex = 0;
      state.globalBusy = true;
      renderPanel();
      const result = await request("/cline/global-chat/messages", {
        method: "POST",
        body: JSON.stringify({ content, skill_ids: [...state.selectedSkillIds] }),
      });
      state.globalMessages = result.messages || [...state.globalMessages, result.message].filter(Boolean);
      try {
        await loadSkills();
        const newlyInstalled = state.skills
          .filter((skill) => skill.enabled && !previousSkillIds.has(skill.id))
          .map((skill) => skill.id);
        state.selectedSkillIds = [...new Set([...state.selectedSkillIds, ...newlyInstalled])];
      } catch (error) {
        console.warn("[Comfyui-omniscience] Skill list refresh failed", error);
      }
      return;
    }
    if (!currentTaskId()) {
      const folderName = await requestProjectFolder();
      if (!folderName) return;
      const task = await request("/cline/tasks", { method: "POST", body: JSON.stringify({ title: content.slice(0, 48), folder: folderName, mode: "act" }) });
      await loadTasks();
      await loadTask(task.id, { resetSkills: false });
    }
    // Canvas synchronization is best effort for chat. An unavailable or very
    // large canvas must not prevent the user from asking the Agent a question.
    await syncCurrentWorkflow({ force: true, silent: true });
    const context = state.context.join("\n\n");
    state.context = [];
    state.skillMenuOpen = false;
    state.skillQuery = "";
    state.skillMenuIndex = 0;
    input.value = "";
    state.busy = true;
    renderPanel();
    const result = await request(`/cline/tasks/${encodeURIComponent(currentTaskId())}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, context, skill_ids: [...state.selectedSkillIds] }),
    });
    await refreshTask();
    await loadTasks();
  } catch (error) {
    state.error = error.message;
  } finally {
    if (state.globalChatActive) state.globalBusy = false;
    else state.busy = false;
    renderPanel();
  }
}

async function cancelTask() {
  const taskId = currentTaskId();
  if (!taskId || state.cancelling || !["running", "waiting_approval"].includes(state.task?.status)) return;
  if (!window.confirm("确定要取消当前运行吗？正在运行的命令会被终止，之后仍可在这个节点继续提问。")) return;
  state.cancelling = true;
  renderPanel();
  try {
    state.task = await request(`/cline/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
    state.busy = false;
    state.error = "本次运行已被强制取消，可以继续提问。";
    await loadTasks();
  } catch (error) {
    state.error = error.message;
  } finally {
    state.cancelling = false;
    renderPanel();
  }
}

function showError(error) {
  state.error = error.message || String(error);
  renderPanel();
}

function setView(view) {
  state.view = view;
  const views = { chat: ".cline-chat-view", checkpoints: ".cline-checkpoints-area", files: ".cline-files-area", workflow: ".cline-workflow-area", mcp: ".cline-mcp-area", settings: ".cline-settings-area", skills: ".cline-skills-area" };
  for (const [name, selector] of Object.entries(views)) {
    document.querySelector(`#${PANEL_ID} ${selector}`)?.classList.toggle("cline-hidden", name !== view);
    document.querySelector(`#${PANEL_ID} [data-view="${name}"]`)?.classList.toggle("active", name === view);
  }
  if (view === "files") renderFiles();
  if (view === "workflow") renderWorkflow();
  if (view === "settings") loadSettings().then(renderSettings).catch(showError);
  if (view === "mcp") loadMcp().then(renderMcp).catch(showError);
  if (view === "checkpoints") renderCheckpoints().catch(showError);
  if (view === "skills") loadSkills().then(renderSkills).catch(showError);
}

async function addFileContext(path) {
  try {
    const result = await request(`/cline/file?task_id=${encodeURIComponent(currentTaskId())}&path=${encodeURIComponent(path)}`);
    state.context.push(`文件：${result.path}\n${result.content}`);
    renderContext();
  } catch (error) { window.alert(`读取文件失败：${error.message}`); }
}

async function renderFiles() {
  const area = document.querySelector(`#${PANEL_ID} .cline-files-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "项目文件"));
  if (!currentTaskId()) {
    area.appendChild(el("p", "cline-hint", "先创建或选择一个项目文件夹。"));
    return;
  }
  try {
    const result = await request(`/cline/files?task_id=${encodeURIComponent(currentTaskId())}&recursive=true`);
    area.appendChild(el("div", "cline-workspace-label", result.workspace));
    const list = el("div", "cline-file-list");
    for (const entry of result.entries || []) {
      if (entry.type !== "file") continue;
      const row = button(entry.path, "cline-file-row");
      row.title = "点击加入下一条消息的上下文";
      row.onclick = () => addFileContext(entry.path);
      list.appendChild(row);
    }
    area.appendChild(list);
  } catch (error) { area.appendChild(el("p", "cline-error", error.message)); }
}

async function renderWorkflow() {
  const area = document.querySelector(`#${PANEL_ID} .cline-workflow-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "ComfyUI 工作流"));
  if (!currentTaskId()) {
    area.appendChild(el("p", "cline-hint", "先创建或选择一个项目文件夹。"));
    return;
  }
  try {
    const result = await request(`/cline/tasks/${encodeURIComponent(currentTaskId())}/workflow`);
    state.currentWorkflow = result.current || null;
    area.appendChild(el("div", "cline-workspace-label", result.path));
    const liveTitle = el("h4", "cline-workflow-section-title", "当前页面实时画布（Agent 可读取）");
    const liveHint = el("p", "cline-hint", state.currentWorkflow
      ? `最近同步：${new Date(state.currentWorkflow.updated_at * 1000).toLocaleString()}。未保存的画布修改也会同步，不会覆盖 workflow.json。`
      : "尚未同步当前画布。点击“读取当前画布”或发送消息时会自动同步。");
    const liveEditor = document.createElement("textarea");
    liveEditor.className = "cline-json-editor cline-workflow-editor";
    liveEditor.readOnly = true;
    liveEditor.value = state.currentWorkflow ? JSON.stringify(state.currentWorkflow.workflow, null, 2) : "";
    const liveActions = el("div", "cline-inline-actions");
    const sync = button("读取当前画布", "cline-primary");
    const liveCopy = button("复制实时 JSON", "cline-secondary");
    const liveOutput = el("span", "cline-hint");
    sync.onclick = async () => {
      sync.disabled = true;
      try {
        await syncCurrentWorkflow({ force: true, silent: false });
        await renderWorkflow();
      } catch (error) { liveOutput.textContent = `读取失败：${error.message}`; }
      finally { sync.disabled = false; }
    };
    liveCopy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(liveEditor.value);
        liveOutput.textContent = "已复制实时工作流。";
      } catch (error) { liveOutput.textContent = `复制失败：${error.message}`; }
    };
    liveActions.append(sync, liveCopy, liveOutput);
    area.append(liveTitle, liveHint, liveEditor, liveActions);

    area.appendChild(el("h4", "cline-workflow-section-title", "项目 workflow.json（不会被实时画布覆盖）"));
    area.appendChild(el("p", "cline-hint", "这是一个可导入 ComfyUI 的基础测试工作流。已有 workflow.json 不会被 Comfyui-omniscience 覆盖。"));
    const editor = document.createElement("textarea");
    editor.className = "cline-json-editor cline-workflow-editor";
    editor.readOnly = true;
    editor.value = result.content || "";
    const actions = el("div", "cline-inline-actions");
    const copy = button("复制 JSON", "cline-primary");
    const download = button("下载 workflow.json", "cline-secondary");
    const output = el("span", "cline-hint");
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(editor.value);
        output.textContent = "已复制。";
      } catch (error) { output.textContent = `复制失败：${error.message}`; }
    };
    download.onclick = () => {
      const blob = new Blob([editor.value], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "workflow.json";
      link.click();
      URL.revokeObjectURL(url);
    };
    actions.append(copy, download, output);
    area.append(editor, actions);
  } catch (error) { area.appendChild(el("p", "cline-error", error.message)); }
}

async function renderCheckpoints() {
  const area = document.querySelector(`#${PANEL_ID} .cline-checkpoints-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "检查点"));
  if (!currentTaskId()) {
    area.appendChild(el("p", "cline-hint", "先选择一个任务。"));
    return;
  }
  const result = await request(`/cline/tasks/${encodeURIComponent(currentTaskId())}/checkpoints`);
  if (!(result.checkpoints || []).length) {
    area.appendChild(el("p", "cline-hint", "工具完成后会自动创建检查点。"));
    return;
  }
  for (const checkpoint of result.checkpoints) {
    const card = el("article", "cline-checkpoint");
    card.append(el("strong", "", checkpoint.label || "检查点"), el("span", "cline-checkpoint-date", new Date(checkpoint.created_at * 1000).toLocaleString()));
    const actions = el("div", "cline-inline-actions");
    const diff = button("查看差异", "cline-secondary");
    const restore = button("恢复文件", "cline-secondary");
    const output = el("pre", "cline-action-details cline-hidden");
    diff.onclick = async () => {
      try {
        const value = await request(`/cline/checkpoints/${encodeURIComponent(checkpoint.id)}/diff`);
        output.textContent = value.diff || "（没有差异）";
        output.classList.toggle("cline-hidden");
      } catch (error) { output.textContent = error.message; }
    };
    restore.onclick = async () => {
      if (!window.confirm("恢复该检查点的文件？当前未保存的文件修改可能丢失。")) return;
      try {
        await request(`/cline/checkpoints/${encodeURIComponent(checkpoint.id)}/restore`, { method: "POST", body: JSON.stringify({ restore_files: true, restore_task: false }) });
        state.error = "检查点文件已恢复。";
      } catch (error) { state.error = error.message; }
      renderPanel();
    };
    actions.append(diff, restore);
    card.append(actions, output);
    area.appendChild(card);
  }
}

async function loadSettings() {
  state.settings = await request("/cline/settings");
  await loadContextStats();
  return state.settings;
}

async function loadContextStats() {
  if (!currentTaskId()) {
    state.contextStats = null;
    return null;
  }
  const skillQuery = encodeURIComponent(JSON.stringify(state.selectedSkillIds));
  state.contextStats = await request(`/cline/tasks/${encodeURIComponent(currentTaskId())}/context?skill_ids=${skillQuery}`);
  return state.contextStats;
}

function contextSummaryText(stats) {
  if (!stats) return currentTaskId() ? "正在读取当前提示词长度…" : "选择或创建任务后，这里会显示当前提示词长度。";
  const status = stats.above_compaction_threshold
    ? "已超过自动压缩阈值。"
    : `达到 ${stats.compaction_threshold_percent}% 时自动压缩。`;
  return `当前提示词：约 ${stats.prompt_tokens.toLocaleString()} Token，${stats.prompt_chars.toLocaleString()} 字符，占上下文上限 ${stats.prompt_percent.toFixed(1)}%（消息 ${stats.message_tokens.toLocaleString()}，系统 ${stats.system_tokens.toLocaleString()}，工具 ${stats.tools_tokens.toLocaleString()}）。${status}`;
}

function renderSettings() {
  const area = document.querySelector(`#${PANEL_ID} .cline-settings-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "模型与工作区设置"));
  const settings = state.settings || {};
  for (const [label, name, value] of [
    ["API 地址", "api_base_url", settings.api_base_url || "https://api.openai.com/v1"],
    ["模型", "model", settings.model || ""],
    ["最大上下文 Token 数", "max_context_tokens", settings.max_context_tokens || "32000"],
    ["自动压缩阈值（上下文百分比）", "compaction_threshold_percent", settings.compaction_threshold_percent || "80"],
    ["API Key（留空保持不变）", "api_key", ""],
    ["项目父目录", "workspace", settings.workspace || ""],
    ["ComfyUI 目录（可选）", "comfyui_directory", settings.comfyui_directory || ""],
  ]) {
    const field = el("label", "cline-field");
    field.appendChild(el("span", "", label));
    const input = document.createElement("input");
    input.name = name;
    input.value = value;
    input.type = ["max_context_tokens", "compaction_threshold_percent"].includes(name) ? "number" : name === "api_key" ? "password" : "text";
    if (name === "max_context_tokens") {
      input.min = "1024";
      input.max = "1000000";
      input.step = "1024";
    }
    if (name === "compaction_threshold_percent") {
      input.min = "1";
      input.max = "99";
      input.step = "1";
    }
    field.appendChild(input);
    area.appendChild(field);
  }
  const stats = state.contextStats;
  const contextLabel = el("p", stats?.above_compaction_threshold ? "cline-hint cline-error" : "cline-hint", contextSummaryText(stats));
  const hint = el("p", "cline-hint cline-settings-status", settings.api_key_configured
    ? "API Key 已配置，不会在这里显示。填写 ComfyUI 目录后，新建项目会自动链接到该目录的 custom_nodes。"
    : "尚未配置 API Key；本地 Ollama 等服务可留空。填写 ComfyUI 目录后，新建项目会自动链接到该目录的 custom_nodes。");
  const save = button("保存设置", "cline-primary");
  save.onclick = async () => {
    save.disabled = true;
    try {
      const values = Object.fromEntries([...area.querySelectorAll("input")].map((input) => [input.name, input.value]));
      state.settings = await request("/cline/settings", { method: "POST", body: JSON.stringify(values) });
      await loadContextStats();
      renderSettings();
      area.querySelector(".cline-settings-status").textContent = "已保存。";
    } catch (error) { hint.textContent = `保存失败：${error.message}`; }
    finally { save.disabled = false; }
  };
  area.append(contextLabel, hint, save);
}

function skillEditorField(label, name, value, multiline = false) {
  const field = el("label", "cline-field");
  field.appendChild(el("span", "", label));
  const input = multiline ? document.createElement("textarea") : document.createElement("input");
  input.name = name;
  input.value = value || "";
  if (multiline) input.className = "cline-skill-editor";
  field.appendChild(input);
  return field;
}

function resetSkillEditor() {
  state.editingSkillId = "";
  renderSkills();
}

function renderSkills() {
  const area = document.querySelector(`#${PANEL_ID} .cline-skills-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "Skill 管理"));
  area.appendChild(el("p", "cline-hint", "Skill 是全局可复用的提示词指令。启用后，在对话输入框键入 / 即可搜索并选择；选中的 Skill 会用于后续消息，不绑定任何任务。"));

  const editing = state.skills.find((skill) => skill.id === state.editingSkillId) || null;
  const editor = el("div", "cline-skill-form");
  editor.appendChild(el("h4", "cline-workflow-section-title", editing ? "编辑 Skill" : "新建 Skill"));
  editor.append(
    skillEditorField("名称", "name", editing?.name || ""),
    skillEditorField("描述（可选）", "description", editing?.description || ""),
    skillEditorField("提示词内容", "content", editing?.content || "", true),
  );
  const enabledField = el("label", "cline-skill-check");
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.name = "enabled";
  enabled.checked = editing ? !!editing.enabled : true;
  enabledField.append(enabled, el("span", "", "启用此 Skill"));
  editor.appendChild(enabledField);
  const editorActions = el("div", "cline-inline-actions");
  const save = button(editing ? "保存修改" : "创建 Skill", "cline-primary");
  const cancel = button("清空", "cline-secondary");
  const output = el("span", "cline-hint cline-skill-status");
  save.onclick = async () => {
    save.disabled = true;
    try {
      const values = Object.fromEntries([...editor.querySelectorAll("input[name], textarea[name]")].map((input) => [input.name, input.type === "checkbox" ? input.checked : input.value]));
      if (editing) {
        await request(`/cline/skills/${encodeURIComponent(editing.id)}`, { method: "PATCH", body: JSON.stringify(values) });
        output.textContent = "已保存。";
      } else {
        await request("/cline/skills", { method: "POST", body: JSON.stringify(values) });
        output.textContent = "已创建。";
      }
      await loadSkills();
      await loadContextStats().catch(() => {});
      state.editingSkillId = "";
      renderSkills();
      renderPanel();
    } catch (error) {
      output.textContent = `保存失败：${error.message}`;
    } finally { save.disabled = false; }
  };
  cancel.onclick = resetSkillEditor;
  editorActions.append(save, cancel, output);
  editor.appendChild(editorActions);
  area.appendChild(editor);

  const actions = el("div", "cline-inline-actions cline-skill-toolbar");
  const importButton = button("导入 JSON", "cline-secondary");
  const exportButton = button("导出 JSON", "cline-secondary");
  const importInput = document.createElement("input");
  importInput.type = "file";
  importInput.accept = "application/json,.json";
  importInput.hidden = true;
  importInput.onchange = async () => {
    const file = importInput.files?.[0];
    if (!file) return;
    try {
      const imported = JSON.parse(await file.text());
      await request("/cline/skills/import", { method: "POST", body: JSON.stringify(imported) });
      await loadSkills();
      await loadContextStats().catch(() => {});
      renderSkills();
      renderPanel();
    } catch (error) { window.alert(`导入失败：${error.message}`); }
    finally { importInput.value = ""; }
  };
  importButton.onclick = () => importInput.click();
  exportButton.onclick = () => {
    const blob = new Blob([JSON.stringify({ version: 1, skills: state.skills.map(({ name, description, content, enabled }) => ({ name, description, content, enabled })) }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "comfyui-omniscience-skills.json";
    link.click();
    URL.revokeObjectURL(url);
  };
  actions.append(importButton, exportButton, importInput);
  area.appendChild(actions);

  const list = el("div", "cline-skill-list");
  if (!state.skills.length) {
    list.appendChild(el("p", "cline-hint", "还没有 Skill。"));
  }
  for (const skill of state.skills) {
    const card = el("article", `cline-skill-card${skill.enabled ? "" : " disabled"}`);
    const heading = el("div", "cline-skill-card-heading");
    heading.append(el("strong", "", skill.name), el("span", "cline-skill-state", skill.enabled ? "已启用" : "已停用"));
    const description = el("p", "cline-hint", skill.description || "无描述");
    const preview = el("pre", "cline-action-details cline-skill-preview", skill.content);
    const cardActions = el("div", "cline-inline-actions");
    const toggle = button(skill.enabled ? "停用" : "启用", "cline-secondary");
    const edit = button("编辑", "cline-secondary");
    const remove = button("删除", "cline-secondary");
    toggle.onclick = async () => {
      try { await request(`/cline/skills/${encodeURIComponent(skill.id)}`, { method: "PATCH", body: JSON.stringify({ enabled: !skill.enabled }) }); await loadSkills(); await loadContextStats().catch(() => {}); renderSkills(); renderPanel(); }
      catch (error) { window.alert(`更新失败：${error.message}`); }
    };
    edit.onclick = () => { state.editingSkillId = skill.id; renderSkills(); };
    remove.onclick = async () => {
       if (!window.confirm(`确定删除全局 Skill“${skill.name}”？删除后，历史消息仍保留但不会再注入提示词。`)) return;
       try { await request(`/cline/skills/${encodeURIComponent(skill.id)}`, { method: "DELETE" }); await loadSkills(); await loadContextStats().catch(() => {}); renderSkills(); renderPanel(); }
      catch (error) { window.alert(`删除失败：${error.message}`); }
    };
    cardActions.append(toggle, edit, remove);
    card.append(heading, description, preview, cardActions);
    list.appendChild(card);
  }
  area.appendChild(list);
}

async function loadMcp() {
  const result = await request("/cline/mcp");
  state.mcp = result.servers || [];
  return state.mcp;
}

function mcpConfig() {
  const servers = {};
  for (const server of state.mcp) {
    servers[server.name] = server.transport === "stdio"
      ? { command: server.command, args: server.args || [], disabled: !!server.disabled, autoApprove: server.autoApprove || [] }
      : { type: server.transport, url: server.url, disabled: !!server.disabled, autoApprove: server.autoApprove || [] };
  }
  return { mcpServers: servers };
}

function renderMcp() {
  const area = document.querySelector(`#${PANEL_ID} .cline-mcp-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "MCP 工具"), el("p", "cline-hint", "stdio 命令和 HTTP URL 会在 ComfyUI 服务端执行，请只配置可信服务器。"));
  const editor = document.createElement("textarea");
  editor.className = "cline-json-editor";
  editor.value = JSON.stringify(mcpConfig(), null, 2);
  const actions = el("div", "cline-inline-actions");
  const save = button("保存 MCP 配置", "cline-primary");
  const discover = button("发现工具", "cline-secondary");
  const output = el("pre", "cline-action-details");
  save.onclick = async () => {
    try {
      const result = await request("/cline/mcp", { method: "POST", body: JSON.stringify(JSON.parse(editor.value)) });
      state.mcp = result.servers || [];
      editor.value = JSON.stringify(mcpConfig(), null, 2);
      output.textContent = "已保存。";
    } catch (error) { output.textContent = `保存失败：${error.message}`; }
  };
  discover.onclick = async () => {
    discover.disabled = true;
    try { output.textContent = JSON.stringify((await request("/cline/mcp/tools")).groups || [], null, 2); }
    catch (error) { output.textContent = `发现失败：${error.message}`; }
    finally { discover.disabled = false; }
  };
  actions.append(save, discover);
  area.append(editor, actions, output);
}

function buildPanel() {
  if (document.getElementById(PANEL_ID)) return;
  const panel = el("aside", "cline-panel");
  panel.id = PANEL_ID;
  const header = el("header", "cline-header");
  const heading = el("div", "cline-header-title");
  heading.append(
    el("strong", "", "Comfyui-omniscience"),
    el("span", "cline-current-task", "未选择任务"),
    el("span", "cline-prompt-stats", "提示词：—"),
  );
  const globalChat = button("全局对话", "cline-secondary cline-global-chat-toggle");
  globalChat.onclick = toggleGlobalChat;
  const close = button("×", "cline-close");
  close.onclick = () => panel.classList.remove("open");
  const headerActions = el("div", "cline-header-actions");
  headerActions.append(globalChat, close);
  header.append(heading, headerActions);

  const layout = el("div", "cline-task-layout");
  const sidebar = el("aside", "cline-task-sidebar");
  const create = button("＋ 新任务", "cline-primary cline-new-task");
  create.onclick = createTask;
  sidebar.append(create, el("h3", "", "任务节点"), el("div", "cline-task-list"));

  const main = el("main", "cline-main");
  const toolbar = el("div", "cline-toolbar");
  const modes = el("div", "cline-mode-controls");
  for (const mode of ["plan", "act"]) {
    const control = button(mode === "plan" ? "Plan" : "Act", "cline-mode");
    control.dataset.mode = mode;
    control.onclick = () => setMode(mode);
    modes.appendChild(control);
  }
  const status = el("span", "cline-task-status");
  const cancel = button("强制取消", "cline-secondary cline-cancel-task");
  const clearHistoryButton = button("清理历史对话", "cline-secondary cline-clear-history");
  cancel.hidden = true;
  cancel.onclick = cancelTask;
  clearHistoryButton.onclick = clearHistory;
  toolbar.append(modes, status, clearHistoryButton, cancel);
  const tabs = el("nav", "cline-tabs");
  for (const [view, label] of [["chat", "对话"], ["checkpoints", "检查点"], ["files", "文件"], ["workflow", "工作流"], ["mcp", "MCP"], ["skills", "Skill"], ["settings", "设置"]]) {
    const tab = button(label, view === "chat" ? "active" : "");
    tab.dataset.view = view;
    tab.onclick = () => setView(view);
    tabs.appendChild(tab);
  }
  const views = el("div", "cline-views");
  const chat = el("section", "cline-chat-view");
  const log = el("div", "cline-messages");
  const context = el("div", "cline-context-label", "点击文件可加入下一条消息的上下文");
  const skillSelector = el("div", "cline-skill-selector");
  const composer = el("div", "cline-composer");
  const textarea = document.createElement("textarea");
  textarea.dataset.composer = "true";
  textarea.placeholder = "描述你要完成的编程任务…";
  textarea.rows = 3;
  textarea.onkeydown = (event) => {
    if (state.skillMenuOpen) {
      const matches = matchingSkills();
      if (event.key === "ArrowDown" && matches.length) {
        event.preventDefault();
        state.skillMenuIndex = (state.skillMenuIndex + 1) % matches.length;
        renderSkillSelector();
        return;
      }
      if (event.key === "ArrowUp" && matches.length) {
        event.preventDefault();
        state.skillMenuIndex = (state.skillMenuIndex - 1 + matches.length) % matches.length;
        renderSkillSelector();
        return;
      }
      if (event.key === "Enter" && !event.metaKey && !event.ctrlKey && matches.length) {
        event.preventDefault();
        selectSkillFromMenu(matches[state.skillMenuIndex]?.id || matches[0].id);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        state.skillMenuOpen = false;
        state.skillQuery = "";
        state.skillMenuIndex = 0;
        renderSkillSelector();
        return;
      }
    }
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); sendMessage(); }
  };
  textarea.oninput = () => {
    const command = skillCommand(textarea.value);
    state.skillMenuOpen = !!command;
    state.skillQuery = command?.query || "";
    const matches = matchingSkills();
    state.skillMenuIndex = Math.min(state.skillMenuIndex, Math.max(0, matches.length - 1));
    renderSkillSelector();
  };
  const send = button("发送", "cline-primary cline-send-message");
  send.onclick = sendMessage;
  composer.append(textarea, send);
  chat.append(log, context, skillSelector, composer);
  views.append(chat, el("section", "cline-checkpoints-area cline-hidden"), el("section", "cline-files-area cline-hidden"), el("section", "cline-workflow-area cline-hidden"), el("section", "cline-mcp-area cline-hidden"), el("section", "cline-settings-area cline-hidden"), el("section", "cline-skills-area cline-hidden"));
  main.append(toolbar, tabs, views);
  layout.append(sidebar, main);
  panel.append(header, layout);
  document.body.appendChild(panel);
  renderPanel();
}

function installLauncher() {
  if (document.getElementById("comfyui-omniscience-launcher")) return;
  const launcher = button("Omniscience", "cline-launcher");
  launcher.id = "comfyui-omniscience-launcher";
  launcher.onclick = () => { buildPanel(); document.getElementById(PANEL_ID).classList.add("open"); };
  document.body.appendChild(launcher);
}

app.registerExtension({
  name: "ComfyUI.Omniscience",
  async setup() {
    ensureStyle();
    installLauncher();
    buildPanel();
    try {
      await loadTasks();
      await loadSkills();
      if (state.tasks.length) await loadTask(state.tasks[0].id);
    } catch (error) { showError(error); }
    window.setInterval(pollEvents, 1500);
  },
});