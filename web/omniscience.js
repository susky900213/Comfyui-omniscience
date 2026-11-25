import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createTranslator, normalizeLocale } from "./i18n.js";

const STYLE_ID = "comfyui-omniscience-style";
const PANEL_ID = "comfyui-omniscience-panel";
const LAUNCHER_ID = "comfyui-omniscience-launcher";
const WORKFLOW_INSPECTION_VERSION = 6;
const TERMINAL_FAILURES = new Set(["failed", "cancelled"]);

const state = {
  locale: normalizeLocale(navigator.languages?.[0] || navigator.language),
  composerDraft: "",
  tasks: [],
  projects: [],
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
  toolMenuOpen: false,
  toolQuery: "",
  toolMenuIndex: 0,
  fileMenuOpen: false,
  fileQuery: "",
  fileMenuIndex: 0,
  fileList: [],
  fileListScope: "",
  globalNetworkEnabled: false,
  fileListLoading: false,
  uploadBusy: false,
  networkBusy: false,
  nodeInstallBusy: false,
  nodeScans: {},
  editingSkillId: "",
  cursor: -1,
  view: "chat",
  busy: false,
  lastTaskOutcome: "",
  lastGlobalOutcome: "",
  cancelling: false,
  error: "",
  polling: false,
  taskLoading: false,
  workflowRepairCards: [],
  workflowRepairPolling: false,
  workflowRepairGeneration: 0,
  projectFolderRequest: null,
  currentWorkflow: null,
  currentWorkflowSignature: "",
  currentWorkflowTaskId: "",
};

const { translate: t, localize } = createTranslator(() => state.locale);

function configuredLocale() {
  const stores = [
    app?.ui?.settings,
    app?.settingStore,
    app?.settings,
    window.comfyAPI?.app?.settings,
    window.comfyAPI?.settings,
  ];
  for (const store of stores) {
    try {
      const value =
        store?.getSettingValue?.("Comfy.Locale") ??
        store?.get?.("Comfy.Locale") ??
        store?.settingsValues?.["Comfy.Locale"] ??
        store?.value?.["Comfy.Locale"];
      if (value) return normalizeLocale(value);
    } catch { /* try the next compatible settings facade */ }
  }
  return "";
}

async function resolveConfiguredLocale() {
  const direct = configuredLocale();
  if (direct) return direct;
  try {
    const response = await api.fetchApi("/settings/Comfy.Locale");
    if (response.ok) {
      const value = await response.json();
      if (value) return normalizeLocale(value);
    }
  } catch { /* navigator language remains the fallback */ }
  return normalizeLocale(navigator.languages?.[0] || navigator.language);
}

let localeRequestPending = false;
let lastLocaleFetch = 0;

function applyLocale(locale) {
  const nextLocale = normalizeLocale(locale);
  if (nextLocale === state.locale) return;
  const draft = document.querySelector(`#${PANEL_ID} textarea[data-composer]`)?.value ?? state.composerDraft;
  state.locale = nextLocale;
  state.composerDraft = draft;
  localize(document.getElementById(PANEL_ID));
  localize(document.getElementById(LAUNCHER_ID));
  renderPanel();
  if (!["settings", "skills", "mcp"].includes(state.view)) setView(state.view);
}

async function syncLocale(force = false) {
  if (localeRequestPending || !force && !configuredLocale() && Date.now() - lastLocaleFetch < 10_000) return;
  localeRequestPending = true;
  try {
    applyLocale(await resolveConfiguredLocale());
  } finally {
    localeRequestPending = false;
    lastLocaleFetch = Date.now();
  }
}

function installLocaleListener() {
  const onLocaleChange = (event) => {
    const locale = event.detail?.value || configuredLocale();
    if (locale) applyLocale(locale);
    else void syncLocale(true);
  };
  app?.ui?.settings?.addEventListener?.("Comfy.Locale.change", onLocaleChange);
  window.addEventListener("focus", () => { void syncLocale(true); });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void syncLocale(true);
  });
}

function installTranslationObserver() {
  const panel = document.getElementById(PANEL_ID);
  if (!panel || panel.dataset.translationObserver) return;
  panel.dataset.translationObserver = "true";
  new MutationObserver(() => localize(panel)).observe(panel, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ["placeholder", "title", "aria-label", "alt"],
  });
}

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = t(text);
  return node;
}

function button(label, className = "") {
  const node = el("button", className, label);
  node.type = "button";
  return node;
}

function confirm(message) {
  return window.confirm(t(message));
}

function alert(message) {
  window.alert(t(message));
}

async function request(path, options = {}) {
  const response = await api.fetchApi(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  try { data = await response.json(); } catch { /* status is enough below */ }
  if (!response.ok) {
    const error = new Error(data?.error || t(`请求失败 (${response.status})`));
    error.status = response.status;
    error.payload = data;
    throw error;
  }
  return data;
}

const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

function formatModelDownloadSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "0 B";
  if (value < 1024) return `${Math.floor(value)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = units[0];
  for (let index = 1; size >= 1024 && index < units.length; index += 1) {
    size /= 1024;
    unit = units[index];
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${unit}`;
}

function workflowNodeInstallStatusText(node) {
  const instances = node?.instances || [];
  if (instances.some((instance) => instance.status === "ambiguous")) return "候选冲突";
  if (instances.some((instance) => !instance.source_selected)) return "待确认安装来源";
  if (node?.status === "installed") return "已安装";
  if (node?.source_selected || instances.length && instances.every((instance) => instance.source_selected)) return "已确认安装来源";
  return "待确认安装来源";
}

function workflowNodeInstallCandidate(node) {
  const instances = node?.instances || [];
  const selected = instances.find((instance) => instance.source_selected && instance.candidate)
    || instances.find((instance) => instance.candidate)
    || null;
  const candidate = selected?.candidate
    || node?.candidate
    || node?.manager
    || node?.source_candidates?.[0]
    || {};
  return { instance: selected, candidate };
}

function dedupeWorkflowNodeTypes(plan) {
  const byType = new Map();
  for (const node of plan?.nodes || []) {
    const classType = String(node.class_type || "");
    const existing = byType.get(classType);
    if (!existing) {
      byType.set(classType, { ...node, instances: [...(node.instances || [])] });
      continue;
    }
    if ((!existing.source && node.source) || (!existing.manager_id && node.manager_id)) {
      Object.assign(existing, { ...node, instances: existing.instances });
    }
    const ids = new Set((existing.instances || []).map((instance) => String(instance.id || "")));
    for (const instance of node.instances || []) {
      if (!ids.has(String(instance.id || ""))) {
        existing.instances.push(instance);
        ids.add(String(instance.id || ""));
      }
    }
  }
  return [...byType.values()];
}

function renderWorkflowNodeInstallDetails(container, plan) {
  container.replaceChildren();
  const nodes = dedupeWorkflowNodeTypes(plan);
  const heading = el("strong", "", "安装节点详情");
  container.appendChild(heading);
  if (!nodes.length) {
    container.appendChild(el("p", "omniscience-workflow-candidate-meta", "没有需要安装的缺失节点。"));
    return;
  }
  const instanceCount = nodes.reduce((total, node) => total + (node.instances || []).length, 0);
  container.appendChild(el(
    "p",
    "omniscience-workflow-candidate-meta",
    `共 ${nodes.length} 种节点类型，${plan.installable_missing_node_count ?? plan.node_instance_count ?? instanceCount} 个工作流实例。`,
  ));
  nodes.forEach((node) => {
    const item = el("div", "omniscience-workflow-node-install-detail");
    const title = el("div", "omniscience-workflow-node-install-detail-heading");
    title.appendChild(el("strong", "", node.class_type || "未知节点"));
    const status = workflowNodeInstallStatusText(node);
    title.appendChild(el("span", `omniscience-workflow-node-install-detail-status ${status === "已确认安装来源" ? "ready" : "pending"}`, status));
    item.appendChild(title);

    const { instance, candidate } = workflowNodeInstallCandidate(node);
    const instances = node.instances || [];
    const instanceText = instances.map((entry) => {
      const id = entry.id || "未知 ID";
      return `${id}${entry.aux_id ? ` · aux_id=${entry.aux_id}` : ""}`;
    }).join("、") || "未知实例";
    item.appendChild(el("p", "omniscience-workflow-node-install-detail-field", `实例：${instanceText}`));

    const packageName = node.package_name
      || node.title
      || candidate.title
      || node.manager_id
      || candidate.manager_id
      || "未确定插件";
    item.appendChild(el("p", "omniscience-workflow-node-install-detail-field", `插件：${packageName}`));

    const managerId = node.manager_id || candidate.manager_id || "";
    const installType = node.install_type || candidate.install_type || "";
    const version = node.version || candidate.version || "";
    const packageId = node.package_id || candidate.install_id || candidate.cnr_id || candidate.id || "";
    const managerText = managerId ? `ComfyUI-Manager：${managerId}` : "ComfyUI-Manager：未匹配";
    const installText = installType ? `安装方式：${installType}` : "安装方式：由插件来源决定";
    item.appendChild(el("p", "omniscience-workflow-node-install-detail-field", `${managerText}${packageId ? ` · 安装包 ID：${packageId}` : ""} · ${installText}${version ? ` · 版本：${version}` : ""}`));

    const source = node.source || candidate.repository || candidate.reference || "";
    const sourceText = source
      ? `来源：${source}`
      : managerId
        ? `来源：ComfyUI-Manager（${managerId}）`
        : "来源：尚未确定，安装前需要选择来源";
    item.appendChild(el(
      "p",
      `omniscience-workflow-node-install-detail-field ${source || managerId ? "" : "warning"}`,
      sourceText,
    ));
    if (instance?.status === "ambiguous" || node.source_ambiguous) {
      item.appendChild(el("p", "omniscience-workflow-node-install-detail-field warning", "该节点存在多个可能插件，必须先选择正确来源。"));
    }
    container.appendChild(item);
  });
}

function modelDownloadProgress() {
  const container = el("div", "omniscience-workflow-download-progress");
  const row = el("div", "omniscience-workflow-download-progress-row");
  const bar = el("progress", "omniscience-workflow-download-progress-bar");
  bar.max = 100;
  bar.value = 0;
  bar.setAttribute("aria-label", "模型下载进度");
  const action = button("…", "omniscience-workflow-download-control");
  action.disabled = true;
  action.setAttribute("aria-label", "正在准备下载");
  action.title = "正在准备下载";
  const label = el("span", "omniscience-workflow-download-progress-label", "正在连接下载服务器…");
  row.append(bar, action);
  container.append(row, label);
  return { container, bar, label, action };
}

function updateModelDownloadProgress(progress, result) {
  const downloaded = Number(result.downloaded_bytes) || 0;
  const total = Number(result.total_bytes);
  if (Number.isFinite(total) && total > 0) {
    const percentage = Math.min(100, Math.floor((downloaded / total) * 100));
    progress.bar.value = percentage;
    progress.label.textContent = `${percentage}% · ${formatModelDownloadSize(downloaded)} / ${formatModelDownloadSize(total)}`;
  } else {
    progress.bar.removeAttribute("value");
    progress.label.textContent = `已下载 ${formatModelDownloadSize(downloaded)} · 总大小未知`;
  }
}

function nodeInstallProgress() {
  const container = el("div", "omniscience-workflow-node-install-progress");
  const heading = el("strong", "omniscience-workflow-node-install-progress-heading", "安装过程");
  const bar = el("progress", "omniscience-workflow-node-install-progress-bar");
  bar.max = 100;
  bar.value = 0;
  bar.setAttribute("aria-label", "节点插件安装进度");
  const label = el("span", "omniscience-workflow-node-install-progress-label", "尚未开始安装插件");
  const current = el("div", "omniscience-workflow-node-install-progress-current");
  const completed = el("div", "omniscience-workflow-node-install-progress-completed");
  const events = el("ol", "omniscience-workflow-node-install-progress-events");
  container.append(heading, bar, label, current, completed, events);
  return { container, bar, label, current, completed, events };
}

function updateNodeInstallProgress(progress, result) {
  const current = Number(result?.completed) || 0;
  const total = Number(result?.total) || 0;
  if (total > 0) progress.bar.value = Math.min(100, Math.floor((current / total) * 100));
  const currentPlugin = result?.current_plugin || result?.current_node || "正在准备插件";
  const currentNode = result?.current_node && result?.current_plugin
    ? `节点：${result.current_node}`
    : "";
  const managerMessage = Array.isArray(result?.manager_messages)
    ? result.manager_messages.filter(Boolean).slice(-1)[0]
    : "";
  const phaseName = {
    starting: "准备安装",
    queued: "已加入 ComfyUI-Manager 队列",
    running: "ComfyUI-Manager 安装中",
    downloading: "正在下载插件",
    installing: "安装插件依赖",
    processing: "正在处理插件",
    resolving: "正在解析 GitHub 仓库",
    extracting: "正在解包节点文件",
    validating: "正在验证目标节点",
    security_review: "正在安全扫描节点",
    placing: "正在写入 custom_nodes",
    completed: "已完成",
    security_blocked: "安全复核阻止",
    interrupted: "服务重启中断",
    failed: "失败",
  }[result?.phase] || result?.phase || "安装中";
  const phase = result?.interrupted
    ? "安装已因服务重启中断"
    : result?.status === "security_blocked"
      ? "Manager 已安装，但安全复核未通过"
    : result?.status === "failed"
      ? "安装失败"
      : result?.status === "completed"
        ? "安装完成"
        : "正在安装";
  progress.label.textContent = total
    ? `${phase} · ${current}/${total}`
    : phase;
  progress.current.textContent = result?.status === "completed"
    ? "所有缺失节点插件均已处理。"
    : `${phaseName} · 当前插件：${currentPlugin}${currentNode ? ` · ${currentNode}` : ""}${managerMessage ? ` · ${managerMessage}` : ""}`;

  const completedPlugins = Array.isArray(result?.completed_plugins) ? result.completed_plugins : [];
  const completedInstallations = Array.isArray(result?.completed_installations) ? result.completed_installations : [];
  progress.completed.replaceChildren();
  if (completedInstallations.length) {
    progress.completed.appendChild(el("strong", "", `已完成插件（${completedPlugins.length}）：`));
    completedInstallations.forEach((item) => {
      const label = [item.plugin, item.path].filter(Boolean).join(" · ");
      progress.completed.appendChild(el("p", "omniscience-workflow-candidate-meta", label));
    });
  } else if (completedPlugins.length) {
    progress.completed.appendChild(el("strong", "", `已完成插件（${completedPlugins.length}）：`));
    progress.completed.appendChild(el("span", "", completedPlugins.join("、")));
  } else {
    progress.completed.textContent = "已完成插件：暂无";
  }

  const eventItems = Array.isArray(result?.events) ? result.events.slice(-20).reverse() : [];
  progress.events.replaceChildren();
  if (eventItems.length) {
    eventItems.forEach((event) => progress.events.appendChild(el("li", "", String(event))));
  } else {
    progress.events.appendChild(el("li", "", "等待安装任务开始…"));
  }
  progress.container.classList.toggle("error", result?.status === "failed");
  progress.container.classList.toggle("completed", result?.status === "completed");
}

async function waitForNodeInstall(jobId, onProgress, shouldContinue = () => true) {
  for (;;) {
    await sleep(1000);
    if (!shouldContinue()) return { status: "detached", jobId };
    const result = await request(
      `/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/nodes/status`,
    );
    onProgress?.(result.progress || result);
    if (result.status === "completed") return result;
    if (result.status === "security_blocked") {
      const error = new Error(result.progress?.error || "Manager 已安装节点，但安全复核未通过");
      error.status = 409;
      if (result.progress?.security) error.payload = { security: result.progress.security };
      throw error;
    }
    if (result.status === "failed") {
      const error = new Error(result.progress?.error || "节点插件安装失败");
      if (result.progress?.security) error.payload = { security: result.progress.security };
      throw error;
    }
  }
}

function renderLocalModelInventory(container, inventory) {
  container.replaceChildren();
  container.classList.toggle("warning", inventory?.status !== "ok");
  if (inventory?.status !== "ok") {
    container.appendChild(el("p", "", inventory?.message || "暂时无法读取本地模型。"));
    return;
  }
  const heading = el("strong", "", `已存在的本地模型：${inventory.count || 0} 个`);
  container.appendChild(heading);
  if (!inventory.models?.length) {
    container.appendChild(el("p", "omniscience-workflow-candidate-meta", "ComfyUI models 目录中暂未找到模型文件。"));
    return;
  }
  const list = el("div", "omniscience-workflow-local-inventory-list");
  list.textContent = inventory.models.map((model) => model.path).join("\n");
  container.appendChild(list);
}

function modelSourceLabel(source) {
  const type = source?.source_type === "huggingface_search" ? "搜索引擎" : source?.source_type === "huggingface_manual" ? "手动地址" : "Hugging Face API";
  const region = source?.region === "domestic" ? "国内" : source?.region === "international" ? "海外" : "";
  return [type, region, source?.repo_id, source?.filename].filter(Boolean).join(" · ");
}

function setModelDownloadControl(progress, state, handler) {
  const controls = {
    idle: ["↓", "开始下载", false],
    starting: ["…", "正在准备下载", true],
    downloading: ["⏸", "暂停下载", false],
    pausing: ["…", "正在暂停下载", true],
    paused: ["▶", "恢复下载", false],
    failed: ["↻", "重试下载", false],
  };
  const [icon, label, disabled] = controls[state] || controls.starting;
  progress.action.textContent = icon;
  progress.action.disabled = disabled;
  progress.action.title = label;
  progress.action.setAttribute("aria-label", label);
  progress.action.onclick = handler || null;
  progress.container.classList.toggle("paused", state === "paused");
  progress.container.classList.toggle("error", state === "failed");
}

function isResumableModelDownloadError(error) {
  return /timed out|timeout|超时|已保留可续传/i.test(String(error || ""));
}

function normalizeModelDownloadResult(result) {
  if (result?.status !== "failed" || !isResumableModelDownloadError(result.error)) return result;
  return { ...result, status: "paused" };
}

async function waitForModelDownload(jobId, name, onProgress, shouldContinue = () => true) {
  for (;;) {
    await sleep(1500);
    if (!shouldContinue()) return { status: "detached", jobId, name };
    const result = normalizeModelDownloadResult(await request(
      `/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/models/status?name=${encodeURIComponent(name)}`,
    ));
    onProgress?.(result);
    if (result.status === "completed") return result;
    if (result.status === "cancelled") return result;
    if (result.status === "paused") return result;
    if (result.status === "failed") throw new Error(result.error || `模型下载失败：${name}`);
    if (result.status === "idle") throw new Error(`模型下载任务已停止，可重新点击下载并从断点继续：${name}`);
  }
}

function createModelDownloadRunner({ jobId, name, progress, prepareSource, onSourceSelected, onCompleted, onFailure }) {
  let running = false;
  let sourceReady = !prepareSource;

  const updateStatus = (result) => {
    if (result.status === "cancelling") {
      progress.label.textContent = "正在暂停下载…";
      setModelDownloadControl(progress, "pausing");
      return;
    }
    updateModelDownloadProgress(progress, result);
  };

  const finish = async (result) => {
    if (result.status === "detached") return result;
    if (result.status === "cancelled" || result.status === "paused") {
      updateModelDownloadProgress(progress, result);
      const timedOut = isResumableModelDownloadError(result.error);
      progress.label.textContent = timedOut
        ? "连接超时，已保留下载进度，可点击 ▶ 继续下载"
        : "下载已暂停，可点击 ▶ 继续下载";
      setModelDownloadControl(progress, "paused", run);
      return result;
    }
    if (result.status === "completed") {
      progress.bar.value = 100;
      progress.label.textContent = "下载完成，正在准备模型…";
      await onCompleted(result);
      return result;
    }
    return result;
  };

  const follow = async () => finish(await waitForModelDownload(
    jobId, name, updateStatus, () => progress.container.isConnected,
  ));

  const run = async () => {
    if (running) return;
    running = true;
    progress.container.classList.remove("paused", "error");
    setModelDownloadControl(progress, "starting");
    try {
      if (!sourceReady) {
        const selected = await prepareSource();
        sourceReady = true;
        onSourceSelected?.(selected);
      }
      await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/models/confirm`, {
        method: "POST", body: JSON.stringify({ confirmed: true, model_names: [name] }),
      });
      setModelDownloadControl(progress, "downloading", pause);
      progress.label.textContent = "正在连接下载服务器…";
      await follow();
    } catch (error) {
      progress.label.textContent = `下载失败：${error.message}`;
      setModelDownloadControl(progress, "failed", run);
      onFailure?.(error, sourceReady);
    } finally {
      running = false;
    }
  };

  const pause = async () => {
    if (!running) {
      await run();
      return;
    }
    setModelDownloadControl(progress, "pausing");
    progress.label.textContent = "正在暂停下载…";
    try {
      await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/models/cancel`, {
        method: "POST", body: JSON.stringify({ name }),
      });
    } catch (error) {
      setModelDownloadControl(progress, "downloading", pause);
      progress.label.textContent = `暂停失败：${error.message}`;
      showError(error);
    }
  };

  const restore = async () => {
    if (running) return null;
    try {
      const result = normalizeModelDownloadResult(await request(
        `/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/models/status?name=${encodeURIComponent(name)}`,
      ));
      updateStatus(result);
      if (result.status === "completed") {
        await finish(result);
        return result;
      }
      if (["downloading", "cancelling"].includes(result.status)) {
        running = true;
        setModelDownloadControl(progress, result.status === "cancelling" ? "pausing" : "downloading", pause);
        try {
          const finished = await follow();
          return finished;
        } finally {
          running = false;
        }
      }
      if (result.status === "cancelled" || result.status === "paused") {
        await finish(result);
        return result;
      }
      if (result.status === "failed") {
        progress.label.textContent = `下载失败：${result.error || `模型下载失败：${name}`}`;
        setModelDownloadControl(progress, "failed", run);
        onFailure?.(new Error(result.error || `模型下载失败：${name}`), sourceReady);
      }
      if (result.status === "idle") {
        progress.label.textContent = "尚未开始下载";
        setModelDownloadControl(progress, "idle", run);
      }
      return result;
    } catch (error) {
      progress.label.textContent = `读取下载状态失败：${error.message}`;
      setModelDownloadControl(progress, "failed", run);
      onFailure?.(error, sourceReady);
      return null;
    }
  };

  progress.action.onclick = run;
  return { run, pause, restore };
}

function ensureStyle() {
  let link = document.getElementById(STYLE_ID);
  if (!link) {
    link = document.createElement("link");
    link.id = STYLE_ID;
    link.rel = "stylesheet";
  }
  const stylesheetUrl = new URL("./omniscience.css", import.meta.url);
  stylesheetUrl.searchParams.set("v", "launcher-layer-4");
  if (link.href !== stylesheetUrl.href) link.href = stylesheetUrl.href;
  if (!link.isConnected) document.head.appendChild(link);
}

function lines(value) {
  const fragment = document.createDocumentFragment();
  for (const line of String(value ?? "").split("\n")) {
    const row = el("div", "omniscience-line");
    row.dataset.omniscienceNoTranslate = "true";
    row.textContent = line;
    fragment.appendChild(row);
  }
  return fragment;
}

function currentTaskId() { return state.task?.id || ""; }

function displayedMessages() { return state.globalChatActive ? state.globalMessages : state.messages; }

function displayedBusy() { return state.globalChatActive ? state.globalBusy : state.busy; }

function workflowApiPrompt(workflow) {
  if (!workflow || typeof workflow !== "object" || Array.isArray(workflow)) return null;
  if (workflow.prompt && typeof workflow.prompt === "object" && !Array.isArray(workflow.prompt)) {
    return workflow.prompt;
  }
  const looksLikePrompt = Object.values(workflow).some((node) => (
    node && typeof node === "object" && !Array.isArray(node)
      && typeof node.class_type === "string"
      && node.inputs && typeof node.inputs === "object"
      && !Array.isArray(node.inputs)
  ));
  return looksLikePrompt ? workflow : null;
}

function taskCanvasWorkflow(result) {
  if (result.content) {
    try {
      const workflow = JSON.parse(result.content);
      if (!workflow || typeof workflow !== "object" || Array.isArray(workflow)) throw new Error();
      return workflow;
    } catch {
      throw new Error("该任务节点的 workflow.json 不是有效的工作流 JSON。");
    }
  }
  if (result.current?.workflow && typeof result.current.workflow === "object" && !Array.isArray(result.current.workflow)) {
    return result.current.workflow;
  }
  throw new Error("该任务节点还没有可打开的工作流。");
}

async function loadWorkflowToCanvas(workflow) {
  if (!workflow || typeof workflow !== "object" || Array.isArray(workflow)) {
    throw new Error("工作流内容无效，无法加载到当前画布。");
  }
  if (Array.isArray(workflow.nodes)) {
    if (typeof app.loadGraphData !== "function") throw new Error("当前 ComfyUI 不支持导入工作流画布。");
    await app.loadGraphData(workflow);
    return;
  }
  const prompt = workflowApiPrompt(workflow);
  if (prompt && typeof app.loadApiJson === "function") {
    await app.loadApiJson(prompt);
    return;
  }
  if (prompt) {
    throw new Error("当前 ComfyUI 不支持将 API 工作流转换为画布，请更新 ComfyUI 前端后重试。");
  }
  throw new Error("该工作流不是可识别的 ComfyUI 画布或 API 格式。");
}

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
    const result = await request(`/omniscience/tasks/${encodeURIComponent(currentTaskId())}/current-workflow`, {
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

  const backdrop = el("div", "omniscience-project-dialog-backdrop");
  const dialog = el("form", "omniscience-project-dialog");
  const title = el("h3", "", "新建 ComfyUI 节点项目");
  const description = el("p", "omniscience-hint", "请输入项目文件夹名称。项目会创建在设置的项目父目录下。");
  const label = el("label", "omniscience-field");
  label.appendChild(el("span", "", "项目文件夹名称"));
  const input = document.createElement("input");
  input.type = "text";
  input.name = "folder";
  input.placeholder = "例如：my-comfy-node";
  input.autocomplete = "off";
  label.appendChild(input);
  const error = el("p", "omniscience-error");
  error.hidden = true;
  const actions = el("div", "omniscience-dialog-actions");
  const cancel = button("取消", "omniscience-secondary");
  const confirm = button("创建项目", "omniscience-primary");
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

function workflowExecutionDiagnostic(errorOrJob) {
  const payload = errorOrJob?.payload || errorOrJob;
  return payload?.diagnostic
    || payload?.workflow?.metadata?.execution_diagnostic
    || payload?.metadata?.execution_diagnostic
    || null;
}

function workflowJobHasExecutionFailure(job) {
  const metadata = job?.metadata || {};
  return job?.state === "failed" && (
    !!job?.error
    || !!job?.diagnostic
    || !!job?.execution_diagnostic
    || !!metadata.execution_error
    || !!metadata.execution_diagnostic
  );
}

function workflowFailureBelongsToCandidate(job, candidate) {
  const candidateId = String(candidate?.id ?? "").trim();
  if (!candidateId) return false;
  const selectedId = String(job?.selected_candidate_id ?? "").trim();
  if (selectedId) return selectedId === candidateId;
  // Older jobs may not have selected_candidate_id, but select_workflow
  // persisted the selected candidate in metadata before execution.
  const persistedId = String(
    job?.metadata?.candidate?.id
      ?? job?.metadata?.candidate_id
      ?? job?.candidate?.id
      ?? "",
  ).trim();
  return !!persistedId && persistedId === candidateId;
}

function appendWorkflowExecutionFailure(card, errorOrJob) {
  const diagnostic = workflowExecutionDiagnostic(errorOrJob) || {};
  const payload = errorOrJob?.payload || errorOrJob || {};
  const metadata = payload?.metadata || payload?.workflow?.metadata || {};
  const message = diagnostic.message
    || payload.message
    || payload.error
    || metadata.execution_error
    || "工作流执行失败。";
  const phaseLabels = { preflight: "执行前检查", execution: "ComfyUI 执行" };
  const categoryLabels = {
    node: "节点缺失或未加载", model: "模型/权重", memory: "显存或内存",
    parameters: "工作流参数", connection: "ComfyUI 连接", timeout: "执行超时",
    environment: "运行环境", unknown: "未确定",
  };
  card.querySelector(".omniscience-workflow-execution-error")?.remove();
  card.classList.remove("completed");
  card.classList.add("failed");
  const box = el("div", "omniscience-workflow-execution-error");
  box.appendChild(el("strong", "", "工作流执行失败"));
  const phase = phaseLabels[diagnostic.phase] || diagnostic.phase;
  const category = categoryLabels[diagnostic.category] || diagnostic.category;
  if (phase || category) {
    box.appendChild(el("p", "omniscience-workflow-candidate-meta omniscience-workflow-diagnostic-label", [phase, category && `判断类别：${category}`].filter(Boolean).join(" · ")));
  }
  box.appendChild(el("p", "omniscience-workflow-candidate-meta warning", `ComfyUI 原始错误：${message}`));
  if (diagnostic.ai_status === "completed" && diagnostic.summary) {
    box.appendChild(el("p", "omniscience-workflow-candidate-meta omniscience-workflow-ai-summary", `AI 分析：${diagnostic.summary}`));
    if (diagnostic.cause) box.appendChild(el("p", "omniscience-workflow-candidate-meta", `可能原因：${diagnostic.cause}`));
    if (Array.isArray(diagnostic.actions) && diagnostic.actions.length) {
      const actions = el("div", "omniscience-workflow-diagnostic-actions");
      actions.appendChild(el("strong", "", "下一步："));
      const list = document.createElement("ol");
      diagnostic.actions.slice(0, 5).forEach((action) => list.appendChild(el("li", "", String(action))));
      actions.appendChild(list);
      box.appendChild(actions);
    }
    if (diagnostic.confidence) box.appendChild(el("p", "omniscience-workflow-candidate-meta", `AI 置信度：${diagnostic.confidence}`));
  } else if (diagnostic.category && diagnostic.actions?.length) {
    box.appendChild(el("p", "omniscience-workflow-candidate-meta", `本地判断：${diagnostic.actions[0]}`));
  }
  if (diagnostic.hint) box.appendChild(el("p", "omniscience-workflow-candidate-meta", `处理建议：${diagnostic.hint}`));
  if (diagnostic.recovery) box.appendChild(el("p", "omniscience-workflow-candidate-meta", `自动处理：${diagnostic.recovery}`));
  if (diagnostic.details) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "查看 ComfyUI 详细错误";
    const body = el("pre", "omniscience-workflow-execution-details");
    const text = typeof diagnostic.details === "string"
      ? diagnostic.details
      : JSON.stringify(diagnostic.details, null, 2);
    body.textContent = String(text || "无更多详细错误").slice(0, 12000);
    details.append(summary, body);
    box.appendChild(details);
  }
  const repairActions = Array.isArray(diagnostic.repair_actions) ? diagnostic.repair_actions : [];
  const jobId = card.dataset.workflowJobId
    || errorOrJob?.job_id
    || errorOrJob?.workflow?.id
    || errorOrJob?.id
    || "";
  const automaticActions = repairActions.filter((action) => action?.automatic && action?.id);
  if (automaticActions.length) {
    const repairs = el("div", "omniscience-workflow-repair-actions");
    repairs.appendChild(el("strong", "", "可自动处理："));
    for (const action of automaticActions.slice(0, 3)) {
      const control = button(action.label || "解决", "omniscience-primary omniscience-workflow-repair");
      control.onclick = () => repairWorkflowFailure(card, jobId, action, control);
      control.disabled = !jobId;
      repairs.appendChild(control);
    }
    box.appendChild(repairs);
  }
  if (diagnostic.repair_blocker && !automaticActions.length) {
    box.appendChild(el("p", "omniscience-workflow-candidate-meta warning", `当前不能自动解决：${diagnostic.repair_blocker}`));
  }
  if (jobId) {
    const aiRepairs = el("div", "omniscience-workflow-repair-actions omniscience-workflow-ai-repair-actions");
    aiRepairs.appendChild(el("strong", "", "代码级修复："));
    const aiRepair = button("调用 AI 修复", "omniscience-primary omniscience-workflow-ai-repair");
    aiRepair.onclick = () => createAIRepairTask(card, jobId, aiRepair);
    aiRepairs.appendChild(aiRepair);
    box.appendChild(aiRepairs);
    box.appendChild(el("p", "omniscience-workflow-candidate-meta", "这会创建一个隔离的 Act 任务，让 AI 检查工作区并尝试修改代码；节点、模型和 ComfyUI 服务不会被自动更改。"));
  } else {
    box.appendChild(el("p", "omniscience-workflow-candidate-meta warning", "当前失败卡片缺少工作流任务 ID，无法安全创建 AI 修复任务；请重新打开工作流卡片后重试。"));
  }
  const execute = card.querySelector(".omniscience-workflow-execute");
  if (execute) {
    execute.insertAdjacentElement("afterend", box);
  } else {
    card.appendChild(box);
  }
  const existingRepairTaskId = errorOrJob?.metadata?.ai_repair_task_id
    || diagnostic.ai_repair_task_id
    || card.dataset.workflowRepairTaskId
    || "";
  if (existingRepairTaskId) renderWorkflowRepairProgress(card, existingRepairTaskId);
}

async function repairWorkflowFailure(card, jobId, action, control) {
  if (!jobId || control.disabled) return;
  if (!confirm(`确定执行“${action.label || "自动处理"}”吗？服务只会执行该错误报告授权的安全动作。`)) return;
  control.disabled = true;
  const originalLabel = control.textContent;
  control.textContent = "处理中…";
  try {
    const result = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/repair`, {
      method: "POST", body: JSON.stringify({ action_id: action.id, confirmed: true }),
    });
    const box = card.querySelector(".omniscience-workflow-execution-error");
    const status = result.message || (result.status === "repaired" ? "已处理，请查看执行结果。" : "已完成处理。");
    if (box) box.appendChild(el("p", "omniscience-workflow-candidate-meta", `处理结果：${status}`));
    const execution = result.execution || result.result?.result;
    if (execution?.status === "completed") {
      card.classList.add("completed");
      card.classList.remove("failed");
      card.querySelector(".omniscience-workflow-execution-error")?.remove();
      card.appendChild(el("p", "omniscience-workflow-candidate-meta", "自动处理完成，工作流已重新执行。"));
    } else {
      control.textContent = "已处理";
    }
  } catch (error) {
    control.disabled = false;
    control.textContent = originalLabel;
    showError(error);
  }
}

async function createAIRepairTask(card, jobId, control) {
  if (!jobId || control.disabled) return;
  if (!confirm("确定调用 AI 修复吗？这会创建一个 Act 任务，让 AI 在隔离项目工作区检查并修改代码；不会自动安装节点、下载模型或伪造修复结果。")) return;
  control.disabled = true;
  const originalLabel = control.textContent;
  control.textContent = "正在启动 AI…";
  try {
    const result = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/repair-ai`, {
      method: "POST", body: JSON.stringify({ confirmed: true, locale: state.locale }),
    });
    const box = card.querySelector(".omniscience-workflow-execution-error");
    const taskId = result.task_id || result.task?.id || "";
    const label = result.status === "already_started" ? "AI 修复任务已存在" : "AI 修复任务已启动";
    control.textContent = taskId ? `${label}：${result.folder || taskId.slice(0, 10)}` : label;
    if (box) box.appendChild(el("p", "omniscience-workflow-candidate-meta", `${label}。AI 将在任务面板中报告检查、修改和验证过程。`));
    if (taskId) {
      card.dataset.workflowRepairTaskId = taskId;
      renderWorkflowRepairProgress(card, taskId, result.task || null);
      await loadTasks();
    }
  } catch (error) {
    control.disabled = false;
    control.textContent = originalLabel;
    showError(error);
  }
}

function workflowRepairEventText(event) {
  const payload = event?.payload || {};
  const compact = (value, limit = 240) => String(value || "").replace(/\s+/g, " ").trim().slice(0, limit);
  if (event?.kind === "task_started") return "AI 已开始检查隔离修复工作区。";
  if (event?.kind === "assistant_text") return `AI：${compact(payload.text) || "正在分析修复方案。"}`;
  if (event?.kind === "tool_result") {
    const tool = compact(payload.tool, 80) || "工具";
    const result = payload.result;
    if (result && typeof result === "object" && result.error) return `${tool}：${compact(result.error)}`;
    return `已完成：${tool}`;
  }
  if (event?.kind === "context_compacted") return "AI 正在整理任务上下文。";
  if (event?.kind === "task_finished") return "AI 修复任务已完成，请查看验证结果。";
  if (event?.kind === "task_failed") return `AI 修复任务失败：${compact(payload.error) || "未知错误"}`;
  if (event?.kind === "task_cancelled") return "AI 修复任务已取消。";
  return compact(event?.kind, 100) || "修复任务有新的进展。";
}

function workflowRepairStatus(record) {
  const terminal = record.events.find((event) => ["task_finished", "task_failed", "task_cancelled"].includes(event.kind));
  if (terminal) {
    if (terminal.kind === "task_finished") return { text: "已完成", className: "completed" };
    if (terminal.kind === "task_cancelled") return { text: "已取消", className: "cancelled" };
    return { text: "失败", className: "failed" };
  }
  if (record.running) return { text: "进行中", className: "running" };
  if (record.queued) return { text: "排队中", className: "queued" };
  if (record.events.length) return { text: "等待下一步", className: "queued" };
  return { text: "已提交，等待 AI 开始", className: "queued" };
}

function workflowRepairRecord(card, taskId, task = null) {
  const normalizedId = String(taskId || "").trim();
  if (!normalizedId || card?.dataset.workflowRepairGeneration !== String(state.workflowRepairGeneration)) return null;
  let record = state.workflowRepairCards.find((item) => item.card === card);
  if (!record || record.taskId !== normalizedId) {
    record = { card, taskId: normalizedId, cursor: -1, events: [], running: false, queued: false, task };
    state.workflowRepairCards = state.workflowRepairCards.filter((item) => item.card !== card);
    state.workflowRepairCards.push(record);
  } else if (task) {
    record.task = task;
  }
  card.dataset.workflowRepairTaskId = normalizedId;
  return record;
}

function renderWorkflowRepairProgress(card, taskId, task = null) {
  const record = workflowRepairRecord(card, taskId, task);
  if (!record) return;
  card.querySelector(".omniscience-workflow-repair-progress")?.remove();
  const progress = el("div", "omniscience-workflow-repair-progress");
  const heading = el("div", "omniscience-workflow-repair-progress-heading");
  heading.appendChild(el("strong", "", "AI 修复过程"));
  const status = workflowRepairStatus(record);
  heading.appendChild(el("span", `omniscience-workflow-repair-status ${status.className}`, status.text));
  progress.appendChild(heading);
  if (record.events.length) {
    const list = el("ol", "omniscience-workflow-repair-events");
    record.events.slice(-6).forEach((event) => list.appendChild(el("li", "", workflowRepairEventText(event))));
    progress.appendChild(list);
  } else {
    progress.appendChild(el("p", "omniscience-workflow-candidate-meta", status.text));
  }
  const open = button("打开 AI 修复任务", "omniscience-secondary omniscience-workflow-ai-repair-open");
  open.onclick = async () => {
    await loadTask(record.taskId);
    setView("chat");
  };
  progress.appendChild(open);
  card.querySelector(".omniscience-workflow-execution-error")?.appendChild(progress);
}

async function refreshWorkflowRepairProgress(record, generation = state.workflowRepairGeneration) {
  if (generation !== state.workflowRepairGeneration || !record?.card?.isConnected || record.terminal) return;
  try {
    const result = await request(`/omniscience/tasks/${encodeURIComponent(record.taskId)}/events?after=${record.cursor}`);
    // Clearing the global conversation rebuilds the message DOM while this
    // request may still be in flight.  Do not recreate a polling record for a
    // detached card when the response arrives afterwards.
    if (generation !== state.workflowRepairGeneration || !record.card.isConnected || !state.workflowRepairCards.includes(record)) return;
    const events = result.events || [];
    if (events.length) {
      record.events.push(...events);
      record.cursor = Math.max(record.cursor, ...events.map((event) => Number(event.seq ?? -1)));
      record.terminal = events.some((event) => ["task_finished", "task_failed", "task_cancelled"].includes(event.kind));
    }
    record.running = !!result.running;
    record.queued = !!result.queued;
    renderWorkflowRepairProgress(record.card, record.taskId, record.task);
  } catch (error) {
    if (error.status === 404) record.terminal = true;
    console.warn("[Comfyui-omniscience] workflow repair progress refresh failed", error);
  }
}

async function pollWorkflowRepairCards() {
  if (state.workflowRepairPolling) return;
  state.workflowRepairCards = state.workflowRepairCards.filter((record) => record.card?.isConnected);
  if (!state.workflowRepairCards.length) return;
  const generation = state.workflowRepairGeneration;
  state.workflowRepairPolling = true;
  try {
    await Promise.all(state.workflowRepairCards.filter((record) => !record.terminal).map((record) => refreshWorkflowRepairProgress(record, generation)));
  } finally {
    if (generation === state.workflowRepairGeneration) state.workflowRepairPolling = false;
  }
}

function workflowCandidateCard(jobId, candidate, options = {}) {
  const card = el("div", "omniscience-workflow-candidate");
  // Keep the persisted workflow job identity on the card from the moment it
  // is rendered.  A failure can happen before dependency preparation adds the
  // execute button, and the repair area must not depend on that button.
  card.dataset.workflowJobId = String(jobId || "").trim();
  card.dataset.workflowRepairGeneration = String(state.workflowRepairGeneration);
  card.appendChild(el("strong", "omniscience-workflow-candidate-title", `${candidate.title || "未命名工作流"} · ID: ${candidate.id || "未知"}`));
  if (candidate.description) card.appendChild(el("p", "omniscience-workflow-candidate-description", candidate.description));
  const meta = [
    candidate.author,
    candidate.region
      ? candidate.region === "local" ? "来源：本地上传" : `站点：${candidate.region === "domestic" ? "国内" : "国外"}`
      : "",
  ]
    .filter(Boolean).join(" · ");
  if (meta) card.appendChild(el("p", "omniscience-workflow-candidate-meta", meta));
  const resource = candidate.resource_assessment;
  if (resource) {
    const resourceText = resource.stale
      ? "评估已过期，仅供搜索参考"
      : resource.feasible === true ? "本地资源匹配" : resource.feasible === false ? "本地资源不足" : "本地资源未知";
    const remoteText = resource.recommend_remote ? "，优先推荐非本地模型/云端推理" : "";
    card.appendChild(el("p", `omniscience-workflow-candidate-meta ${resource.feasible === false && !resource.stale ? "warning" : ""}`, `资源评估：${resourceText}${remoteText}。${resource.recommendation || ""}`));
  }
  if (["remote", "cloud", "api", "hosted"].includes(String(candidate.execution_mode || "").toLowerCase()) || candidate.remote_endpoint) {
    card.appendChild(el("p", "omniscience-workflow-candidate-meta omniscience-workflow-remote", "非本地模型/云端推理工作流"));
  }
  const actionLabel = options.uploaded
    ? "检查并准备这个上传工作流"
    : `选择并准备（ID: ${candidate.id || "未知"}）`;
  const action = button(actionLabel, "omniscience-secondary omniscience-workflow-select");
  action.onclick = async () => {
    const restoring = action.dataset.restore === "true";
    delete action.dataset.restore;
    action.disabled = true;
    try {
      let inspection;
      if (restoring) {
        // A restored card should use the last persisted inspection first. It
        // must not depend on RunningHub being reachable again just because
        // the user switched task nodes or changed settings.
        const job = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}`);
        const metadata = job.metadata || {};
        // Render the persisted failure before trying to refresh inspection.
        // A failed workflow must still expose its repair controls when the
        // provider is temporarily unavailable or an older job has no cached
        // inspection report.
        if (workflowJobHasExecutionFailure(job) && workflowFailureBelongsToCandidate(job, candidate)) {
          appendWorkflowExecutionFailure(card, job);
        }
        const reusableInspection = metadata.inspection
          && metadata.workflow
          && !options.forceInspection
          && Number(metadata.inspection.inspection_version) === WORKFLOW_INSPECTION_VERSION;
        inspection = reusableInspection
          ? { inspection: metadata.inspection, workflow: metadata.workflow }
          : await request("/omniscience/global-chat/workflows/inspect", {
            method: "POST", body: JSON.stringify({ job_id: jobId }),
          });
      } else {
        await request("/omniscience/global-chat/workflows/select", {
          method: "POST",
          body: JSON.stringify({ job_id: jobId, candidate_id: candidate.id }),
        });
        inspection = await request("/omniscience/global-chat/workflows/inspect", {
          method: "POST", body: JSON.stringify({ job_id: jobId }),
        });
      }
      const report = inspection.inspection || {};
      card.classList.add("selected");
      const installedModels = (report.models || []).filter((model) => ["installed", "recognized"].includes(model.local_status) || model.loader_status === "recognized").length;
      const missingModels = (report.models || []).filter((model) => !["installed", "recognized"].includes(model.local_status) && model.loader_status !== "recognized").length;
      const matchedNodeCount = report.canvas_matched_node_count
        ?? report.matched_node_count
        ?? report.loaded_nodes_count
        ?? 0;
      const missingNodes = report.installable_missing_nodes || report.missing_nodes || [];
      const missingNodeCount = report.installable_missing_node_count
        ?? report.missing_node_count
        ?? missingNodes.length;
      const missingNodeTypeCount = report.installable_missing_type_count
        ?? missingNodes.length;
      const frontendOnlyNodes = report.frontend_only_nodes || [];
      const workflowNodeTypes = report.canvas_class_types || report.class_types || [];
      const summary = el(
        "p",
        "omniscience-workflow-candidate-meta",
        `格式：${report.format || "未知"} · 工作流节点：${report.node_count || 0} 个实例/${workflowNodeTypes.length} 种类型 · 已加载：${report.loaded_nodes_checked ? matchedNodeCount : "无法核对"} · 模型：${report.models?.length || 0}（已安装 ${installedModels}，缺失 ${missingModels}）`,
      );
      if (missingNodes.length) {
        summary.textContent += ` · 需要安装节点：${missingNodeTypeCount} 种类型/${missingNodeCount} 个实例（${missingNodes.join("、")}）`;
      }
      if (frontendOnlyNodes.length) {
        summary.textContent += ` · 前端节点（无需安装）：${frontendOnlyNodes.join("、")}`;
      } else if (!missingNodes.length && report.loaded_nodes_checked) {
        summary.textContent += " · 需要安装节点：0 种类型/0 个实例";
      }
      if (report.unverified_nodes?.length) {
        summary.textContent += ` · 未核对节点：${report.unverified_node_count ?? report.unverified_nodes.length} 个实例`;
      }
      card.appendChild(summary);
      const loadCanvas = button("加载到当前画布", "omniscience-secondary omniscience-workflow-load-canvas");
      loadCanvas.onclick = async () => {
        const originalLabel = loadCanvas.textContent;
        loadCanvas.disabled = true;
        try {
          const workflow = inspection.workflow;
          await loadWorkflowToCanvas(workflow);
          loadCanvas.textContent = "已加载到当前画布";
        } catch (error) {
          loadCanvas.disabled = false;
          loadCanvas.textContent = originalLabel;
          showError(error);
        }
      };
      card.appendChild(loadCanvas);
      if (options.autoLoadCanvas) {
        loadCanvas.textContent = "正在加载到当前画布…";
        void loadCanvas.onclick();
      } else if (!restoring && report.format === "ui" && inspection.workflow?.nodes && typeof app.loadGraphData === "function") {
        if (confirm("工作流检查完成。是否先加载到当前 ComfyUI 画布？\n\n当前画布中的未保存内容可能会被替换。")) {
          void loadCanvas.onclick();
        }
      }
      let plan = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies`);
      const planSummary = el(
        "p",
        "omniscience-workflow-candidate-meta",
        `工作流节点：${plan.workflow_node_count ?? report.node_count ?? 0} · 需要安装插件：${plan.installable_plugin_count ?? (plan.plugin_groups || []).length} 个 · 节点：${plan.installable_missing_type_count ?? plan.installable_missing_nodes?.length ?? dedupeWorkflowNodeTypes(plan).length} 种类型/${plan.installable_missing_node_count ?? plan.node_instance_count ?? dedupeWorkflowNodeTypes(plan).reduce((total, node) => total + (node.instances || []).length, 0)} 个实例 · 模型依赖：${(plan.models || []).length}`,
      );
      card.appendChild(planSummary);
      const runStatus = el("p", "omniscience-workflow-candidate-meta omniscience-workflow-run-status");
      const renderRunStatus = (currentPlan) => {
        const blockers = currentPlan.run_blockers || [];
        const canRun = currentPlan.can_run === true;
        runStatus.className = `omniscience-workflow-candidate-meta omniscience-workflow-run-status ${canRun ? "ready" : "blocked"}`;
        runStatus.textContent = canRun
          ? "依赖检查完成：可以运行。请确认后执行。"
          : `暂不能运行：${blockers.length ? blockers.join("；") : "依赖尚未准备完成。"}`;
        card.classList.toggle("ready", canRun);
      };
      const refreshRunStatus = async () => {
        try {
          plan = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies`);
          renderRunStatus(plan);
          return plan;
        } catch (error) {
          runStatus.className = "omniscience-workflow-candidate-meta omniscience-workflow-run-status blocked";
          runStatus.textContent = `暂不能运行：无法刷新依赖状态（${error.message}）`;
          return null;
        }
      };
      card.appendChild(runStatus);
      renderRunStatus(plan);
      const nodeInstallArea = el("div", "omniscience-workflow-node-install-area");
      card.appendChild(nodeInstallArea);
      const nodeInstallDetails = el("div", "omniscience-workflow-node-install-details");
      renderWorkflowNodeInstallDetails(nodeInstallDetails, plan);
      nodeInstallArea.appendChild(nodeInstallDetails);
      const installablePlugins = plan.installable_plugins || plan.plugin_groups || [];
      if (installablePlugins.length) {
        nodeInstallArea.appendChild(el(
          "p",
          "omniscience-workflow-candidate-meta",
          `需要安装：${installablePlugins.map((plugin) => plugin.package_name || plugin.title || plugin.manager_id || plugin.source).join("、")}`,
        ));
      }
      if ((plan.frontend_only_nodes || []).length) {
        nodeInstallArea.appendChild(el(
          "p",
          "omniscience-workflow-candidate-meta",
          `前端节点（无需安装）：${plan.frontend_only_nodes.join("、")}`,
        ));
      }
      const nodeAttributionArea = el("div", "omniscience-workflow-node-attribution");
      const pluginGroups = plan.installable_plugins || plan.plugin_groups || [];
      if (pluginGroups.length) {
        const heading = el("strong", "", "已确认的插件归属");
        nodeAttributionArea.appendChild(heading);
        pluginGroups.forEach((plugin) => {
          const names = (plugin.class_types || []).join("、") || "未命名节点";
          const packageName = plugin.package_name || plugin.title || plugin.manager_id || plugin.id;
          const source = plugin.source || plugin.manager_id || plugin.id;
          nodeAttributionArea.appendChild(el(
            "p",
            "omniscience-workflow-candidate-meta",
            `${packageName}（${names}） · ${source} · ${plugin.node_count || 0} 个实例`,
          ));
        });
      }
      (plan.nodes || []).forEach((node) => {
        const instances = node.instances || [];
        if (!instances.length) return;
        const details = el("p", "omniscience-workflow-candidate-meta");
        const unresolved = instances.filter((instance) => !instance.source_selected);
        const labels = instances.map((instance) => {
          const id = instance.id || "未知 ID";
          const aux = instance.aux_id ? `，aux_id=${instance.aux_id}` : "";
          const status = instance.status === "ambiguous" ? "候选冲突" : instance.source_selected ? "已归属" : "待确认";
          return `${id}${aux}（${status}）`;
        });
        details.textContent = `${node.class_type}：${labels.join("；")}`;
        nodeAttributionArea.appendChild(details);
        if (unresolved.length) {
          nodeAttributionArea.appendChild(el(
            "p",
            "omniscience-workflow-candidate-meta warning",
            `尚未确认 ${unresolved.length} 个实例的插件归属；不会猜测安装来源。`,
          ));
        }
      });
      if ((plan.unresolved_instances || []).length) {
        nodeAttributionArea.appendChild(el(
          "p",
          "omniscience-workflow-candidate-meta warning",
          `仍有 ${(plan.unresolved_instances || []).length} 个节点实例未解决，请按实例选择来源或手动确认仓库。`,
        ));
      }
      if (nodeAttributionArea.children.length) nodeInstallArea.appendChild(nodeAttributionArea);
      const modelSourceArea = el("div", "omniscience-workflow-node-sources omniscience-workflow-model-sources");
      let modelSearchUrls = {};
      const localInventoryArea = el("div", "omniscience-workflow-local-inventory");
      const localInventoryButton = button("查看/刷新本地模型", "omniscience-secondary omniscience-workflow-local-models");
      const refreshLocalInventory = async () => {
        localInventoryButton.disabled = true;
        localInventoryButton.textContent = "正在读取 ComfyUI models…";
        try {
          const inventory = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/models/local`);
          renderLocalModelInventory(localInventoryArea, inventory);
          localInventoryButton.textContent = "刷新本地模型列表";
        } catch (error) {
          renderLocalModelInventory(localInventoryArea, { status: "error", message: error.message });
          localInventoryButton.textContent = "重试读取本地模型";
        } finally {
          localInventoryButton.disabled = false;
        }
      };
      const renderModelSources = async (models, candidatesByName = {}, searchUrlsByName = {}, searchCompleted = false) => {
        modelSourceArea.replaceChildren();
        for (const model of models || []) {
          const similarLocalCandidates = model.similar_local_candidates || [];
          if (
            (["installed", "selected_local", "recognized"].includes(model.status) || model.loader_status === "recognized")
            && !similarLocalCandidates.length
          ) continue;
          const name = model.name;
          const group = el("div", "omniscience-workflow-node-source-group");
          group.appendChild(el("strong", "", `模型文件：${name}`));
          const candidates = candidatesByName[name] || model.source_candidates || (model.source ? [model.source] : []);
          if (similarLocalCandidates.length && !model.local_selection) {
            group.appendChild(el(
              "p",
              "omniscience-workflow-candidate-meta",
              "发现名称相似的本地量化模型；请选择后替换工作流中的模型文件名：",
            ));
            const localPicker = el("select", "omniscience-workflow-model-picker");
            const localPlaceholder = el("option", "", "选择相似的本地模型");
            localPlaceholder.value = "";
            localPicker.appendChild(localPlaceholder);
            similarLocalCandidates.forEach((path) => {
              const option = el("option", "", path);
              option.value = path;
              localPicker.appendChild(option);
            });
            localPicker.onchange = async () => {
              if (!localPicker.value) return;
              localPicker.disabled = true;
              try {
                await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/models/local`, {
                  method: "POST",
                  body: JSON.stringify({ name, path: localPicker.value }),
                });
                const refreshed = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies`);
                plan = refreshed;
                await renderModelSources(plan.models, candidatesByName, searchUrlsByName, searchCompleted);
                renderRunStatus(plan);
                maybeAppendExecute();
              } catch (error) {
                localPicker.disabled = false;
                localPicker.value = "";
                showError(error);
              }
            };
            group.appendChild(localPicker);
          }
          if (model.source) {
            group.appendChild(el("p", "omniscience-workflow-candidate-meta", `已选择：${modelSourceLabel(model.source)}`));
            if (model.source.title || model.source.snippet) {
              group.appendChild(el("p", "omniscience-workflow-candidate-meta", model.source.title || model.source.snippet));
            }
            const downloadGroup = el("div", "omniscience-workflow-model-download-group");
            const download = button(`下载 ${name}`, "omniscience-secondary omniscience-workflow-model-download");
            const progressUi = modelDownloadProgress();
            const runner = createModelDownloadRunner({
              jobId,
              name,
              progress: progressUi,
              onCompleted: async (progress) => {
                model.status = "installed";
                group.replaceChildren(
                  el("strong", "", `已下载：${name}`),
                  el("p", "omniscience-workflow-candidate-meta", `${modelSourceLabel(model.source)} · ${progress.model?.link?.path || model.source.filename}`),
                );
                modelSourceArea.dispatchEvent(new Event("workflow-model-prepared"));
              },
              onFailure: (error) => {
                showError(error);
              },
            });
            download.onclick = () => {
              download.disabled = true;
              download.textContent = `正在下载：${name}`;
              runner.run();
            };
            downloadGroup.appendChild(download);
            downloadGroup.appendChild(progressUi.container);
            group.appendChild(downloadGroup);
            void runner.restore().then((result) => {
              if (!result) return;
              if (["downloading", "cancelling"].includes(result.status)) {
                download.disabled = true;
                download.textContent = `正在下载：${name}`;
              } else if (["paused", "cancelled"].includes(result.status)) {
                download.disabled = false;
                download.textContent = `继续下载 ${name}`;
              } else if (result.status === "failed") {
                download.disabled = false;
                download.textContent = `重试下载 ${name}`;
              }
            });
          } else if (!candidates.length) {
            group.appendChild(el("p", "omniscience-workflow-candidate-meta", "没有搜索结果；请重试搜索或改用已有模型。不会按文件名猜仓库。"));
          }
          if (!candidates.length && !model.source && searchCompleted) {
            const retrySearch = button("重新搜索此模型", "omniscience-secondary omniscience-workflow-model-retry-search");
            retrySearch.onclick = async () => {
              retrySearch.disabled = true;
              retrySearch.textContent = "正在重新搜索…";
              try {
                const searched = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/models/search`, {
                  method: "POST", body: JSON.stringify({ name }),
                });
                if (Array.isArray(searched.models)) {
                  plan.models = searched.models;
                }
                modelSearchUrls = { ...modelSearchUrls, ...(searched.search_urls || {}) };
                await renderModelSources(plan.models || [], searched.results || {}, modelSearchUrls, true);
                void refreshRunStatus();
                for (const warning of Object.values(searched.warnings || {})) {
                  localInventoryArea.appendChild(el("p", "omniscience-workflow-candidate-meta warning", warning));
                }
              } catch (error) {
                retrySearch.disabled = false;
                retrySearch.textContent = "重新搜索此模型";
                showError(error);
              }
            };
            group.appendChild(retrySearch);
            group.appendChild(el("p", "omniscience-workflow-candidate-meta", "也可以手动填写 Hugging Face 文件地址："));
            const manualInput = el("input", "omniscience-workflow-model-manual-input");
            manualInput.type = "url";
            manualInput.placeholder = "https://huggingface.co/<用户名>/<仓库>/blob/<分支>/<模型文件相对路径>";
            manualInput.value = "";
            manualInput.setAttribute("aria-label", `手动填写 ${name} 的 Hugging Face 文件地址`);
            const manualActions = el("div", "omniscience-workflow-model-manual-actions");
            const manualSubmit = button("使用此地址", "omniscience-secondary omniscience-workflow-model-manual-submit");
            const bingUrl = searchUrlsByName[name];
            if (bingUrl) {
              const bingLink = document.createElement("a");
              bingLink.className = "omniscience-workflow-model-bing-link";
              bingLink.href = bingUrl;
              bingLink.target = "_blank";
              bingLink.rel = "noopener noreferrer";
              bingLink.textContent = "在 Bing 中搜索这个文件";
              manualActions.append(manualSubmit, bingLink);
            } else {
              manualActions.appendChild(manualSubmit);
            }
            const manualError = el("p", "omniscience-workflow-candidate-meta warning");
            const progressUi = modelDownloadProgress();
            const runner = createModelDownloadRunner({
              jobId,
              name,
              progress: progressUi,
              prepareSource: async () => {
                const url = manualInput.value.trim();
                if (!url) throw new Error("请填写 Hugging Face 文件地址。");
                manualSubmit.disabled = true;
                manualInput.disabled = true;
                manualError.textContent = "正在校验并准备模型…";
                return request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/models/source`, {
                  method: "POST", body: JSON.stringify({ name, source: { url } }),
                });
              },
              onSourceSelected: (selected) => {
                model.source = selected.source;
                manualError.textContent = "地址已验证，正在下载…";
              },
              onCompleted: async (progress) => {
                model.status = "installed";
                group.replaceChildren(
                  el("strong", "", `已下载：${name}`),
                  el("p", "omniscience-workflow-candidate-meta", `${modelSourceLabel(model.source)} · ${progress.model?.link?.path || model.source.filename}`),
                );
                modelSourceArea.dispatchEvent(new Event("workflow-model-prepared"));
              },
              onFailure: (error, sourceReady) => {
                if (!sourceReady) {
                  manualSubmit.disabled = false;
                  manualInput.disabled = false;
                }
                manualError.textContent = `${sourceReady ? "下载失败" : "地址无效或下载失败"}：${error.message}`;
                showError(error);
              },
            });
            manualSubmit.onclick = runner.run;
            group.append(manualInput, manualActions, manualError, progressUi.container);
          }
          if (candidates.length && !model.source) {
            const picker = el("select", "omniscience-workflow-model-picker");
            const placeholder = el("option", "", "选择包含此模型文件的 Hugging Face 仓库");
            placeholder.value = "";
            picker.appendChild(placeholder);
            candidates.forEach((source, index) => {
              const option = el("option", "", modelSourceLabel(source));
              option.value = String(index);
              picker.appendChild(option);
            });
            picker.onchange = async () => {
              if (!picker.value) return;
              const source = candidates[Number(picker.value)];
              picker.disabled = true;
              const progressUi = modelDownloadProgress();
              group.appendChild(progressUi.container);
              const runner = createModelDownloadRunner({
                jobId,
                name,
                progress: progressUi,
                prepareSource: async () => request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/models/source`, {
                  method: "POST", body: JSON.stringify({ name, source }),
                }),
                onSourceSelected: (selected) => { model.source = selected.source || source; },
                onCompleted: async (progress) => {
                  model.status = "installed";
                  group.replaceChildren(
                    el("strong", "", `已下载：${name}`),
                    el("p", "omniscience-workflow-candidate-meta", `${modelSourceLabel(model.source)} · ${progress.model?.link?.path || source.filename}`),
                  );
                  modelSourceArea.dispatchEvent(new Event("workflow-model-prepared"));
                },
                onFailure: (error, sourceReady) => {
                  if (!sourceReady) {
                    picker.disabled = false;
                    picker.value = "";
                  }
                  showError(error);
                },
              });
              runner.run();
            };
            group.appendChild(picker);
          }
          modelSourceArea.appendChild(group);
        }
      };
      await renderModelSources(plan.models || []);
      const nodeSourceReady = (node) => {
        const instances = node.instances || [];
        if (instances.length) return instances.every((instance) => instance.source_selected);
        return !!(node.source || node.manager_id || node.source_selected);
      };
      const nodeTypes = dedupeWorkflowNodeTypes(plan);
      const missingSources = nodeTypes.filter((node) => !nodeSourceReady(node));
      const sourceSearchArea = el("div", "omniscience-workflow-node-sources");
      const searchButton = button("解析缺失节点（ComfyUI-Manager）", "omniscience-secondary omniscience-workflow-node-search");
      const modelSearchButton = button("搜索缺失模型文件（Hugging Face）", "omniscience-secondary omniscience-workflow-model-search");
      // A quantized local variant is still actionable even when a legacy
      // inspection report marked the dependency as installed. Do not hide the
      // model source area until all similar-local candidates have been handled.
      const pendingModels = (plan.models || []).filter((model) => (
        !["installed", "selected_local"].includes(model.status)
        || (model.similar_local_candidates || []).length > 0
      ));
      card.append(localInventoryButton, localInventoryArea);
      void refreshLocalInventory();
      if (pendingModels.length) card.appendChild(modelSourceArea);
      if (pendingModels.some((model) => !model.source && !model.source_candidates?.length)) {
        modelSearchButton.onclick = async () => {
          modelSearchButton.disabled = true;
          modelSearchButton.textContent = "正在搜索模型文件…";
          try {
            const searched = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/models/search`, {
              method: "POST", body: "{}",
            });
            if (Array.isArray(searched.models)) {
              plan.models = searched.models;
            }
            modelSearchUrls = { ...modelSearchUrls, ...(searched.search_urls || {}) };
            await renderModelSources(plan.models || [], searched.results || {}, modelSearchUrls, true);
            void refreshRunStatus();
            if (searched.message) {
              localInventoryArea.appendChild(el("p", "omniscience-workflow-candidate-meta", searched.message));
            }
            for (const warning of Object.values(searched.warnings || {})) {
              localInventoryArea.appendChild(el("p", "omniscience-workflow-candidate-meta warning", warning));
            }
            modelSearchButton.textContent = "重新搜索缺失模型文件";
          } catch (error) {
            showError(error);
            modelSearchButton.textContent = "重试搜索缺失模型文件";
          } finally { modelSearchButton.disabled = false; }
        };
        card.appendChild(modelSearchButton);
      }
      if (missingSources.length) {
        searchButton.onclick = async () => {
          searchButton.disabled = true;
          searchButton.textContent = "正在读取 ComfyUI-Manager 映射…";
          try {
            let searched;
            try {
              searched = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/nodes/search`, {
                method: "POST", body: "{}",
              });
            } catch (error) {
              const classTypes = [...new Set(nodeTypes.filter((node) => !nodeSourceReady(node)).map((node) => node.class_type))];
              searched = {
                results: Object.fromEntries(classTypes.map((classType) => [classType, []])),
                manager_errors: Object.fromEntries(classTypes.map((classType) => [classType, error.message || "Fetch timeout"])),
              };
            }
            const managerErrors = { ...(searched.manager_errors || {}) };
            const needsWebSearch = Object.entries(searched.results || {})
              .filter(([, candidates]) => !candidates?.length)
              .map(([classType]) => classType);
            if (needsWebSearch.length) {
              searchButton.textContent = "Manager 无映射，正在搜索 GitHub…";
              try {
                const web = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/nodes/web-search`, {
                  method: "POST", body: JSON.stringify({ class_types: needsWebSearch }),
                });
                searched = {
                  ...searched,
                  results: { ...(searched.results || {}), ...(web.results || {}) },
                  search_errors: web.search_errors || {},
                  manager_errors: managerErrors,
                };
              } catch (error) {
                searched.search_errors = Object.fromEntries(needsWebSearch.map((classType) => [classType, error.message]));
                searched.manager_errors = managerErrors;
              }
            }
            sourceSearchArea.replaceChildren();
            const searchErrors = searched.search_errors || {};
            const renderManagerErrors = searched.manager_errors || {};
            const searchResults = searched.results || {};
            const searchUrls = searched.search_urls || {};
            const searchEngines = searched.search_engines || {};
            const unresolvedByType = new Map();
            for (const node of plan.nodes || []) {
              const classType = String(node.class_type || "");
              const existing = unresolvedByType.get(classType);
              if (!existing) {
                unresolvedByType.set(classType, { ...node, instances: [...(node.instances || [])] });
                continue;
              }
              const seenInstanceIds = new Set((existing.instances || []).map((instance) => String(instance.id || "")));
              for (const instance of node.instances || []) {
                const id = String(instance.id || "");
                if (!seenInstanceIds.has(id)) {
                  existing.instances.push(instance);
                  seenInstanceIds.add(id);
                }
              }
            }
            const unresolvedTypes = [...unresolvedByType.values()];
            for (const plannedNode of unresolvedTypes) {
              const classType = plannedNode.class_type;
              const candidates = searchResults[classType] || [];
              const group = el("div", "omniscience-workflow-node-source-group");
              const instanceIds = [...new Set((plannedNode.instances || []).map((instance) => String(instance.id || "").trim()).filter(Boolean))];
              group.appendChild(el("strong", "", `节点 ${classType}：`));
              group.appendChild(el("p", "omniscience-workflow-candidate-meta", instanceIds.length
                ? `使用实例 ID：${instanceIds.join("、")}`
                : "未提供实例 ID"));
              if (nodeSourceReady(plannedNode)) {
                const source = plannedNode.source || (plannedNode.instances || []).find((instance) => instance.source)?.source || "来源已确认";
                group.appendChild(el("p", "omniscience-workflow-candidate-meta", `已确认安装来源 · 尚未安装：${source}`));
                const installNow = button("现在安装此节点", "omniscience-secondary omniscience-workflow-node-source");
                const runSingleNodeInstall = async (acknowledgeSecurity = false) => {
                  installNow.disabled = true;
                  installNow.textContent = "正在启动安装…";
                  try {
                    const started = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/nodes/confirm`, {
                      method: "POST",
                      body: JSON.stringify({
                        confirmed: true,
                        class_types: [classType],
                        acknowledge_security_for: acknowledgeSecurity ? classType : "",
                      }),
                    });
                    updateNodeInstallProgress(nodeProgressUi, started.progress || {});
                    if (started.status === "installing") {
                      installNow.textContent = "正在安装此节点…";
                      await waitForNodeInstall(
                        jobId,
                        (progress) => updateNodeInstallProgress(nodeProgressUi, progress),
                        () => nodeProgressUi.container.isConnected,
                      );
                    }
                    installNow.textContent = "此节点已完成安装检查";
                    installNow.disabled = true;
                    void refreshRunStatus().then(() => maybeAppendExecute());
                  } catch (error) {
                    installNow.disabled = false;
                    installNow.textContent = "重试安装此节点";
                    if (error?.payload?.security) showNodeSecurityReview(error, {
                      inline: true,
                      retry: () => runSingleNodeInstall(true),
                    });
                    else group.appendChild(el("p", "omniscience-workflow-candidate-meta warning", `安装失败：${error.message}`));
                  }
                };
                installNow.onclick = () => runSingleNodeInstall(false);
                group.appendChild(installNow);
                sourceSearchArea.appendChild(group);
                continue;
              }
              if (renderManagerErrors[classType]) {
                group.appendChild(el("p", "omniscience-workflow-candidate-meta warning", `ComfyUI-Manager 查询失败，已继续网页搜索：${renderManagerErrors[classType]}`));
              }
              if (searchErrors[classType]) {
                group.appendChild(el("p", "omniscience-workflow-candidate-meta warning", `网页搜索失败：${searchErrors[classType]}`));
              }
              const searchUrl = searchUrls[classType] || candidates.find((repo) => repo.search_url)?.search_url || "";
              const searchEngine = searchEngines[classType] || candidates.find((repo) => repo.search_engine)?.search_engine || "";
              let parsedSearchUrl = null;
              try { parsedSearchUrl = searchUrl ? new URL(searchUrl) : null; } catch {}
              const validSearchUrl = parsedSearchUrl?.protocol === "https:"
                && ((searchEngine === "bing" && parsedSearchUrl.hostname === "cn.bing.com")
                  || (searchEngine === "google" && parsedSearchUrl.hostname === "www.google.com"));
              if (!candidates.length && validSearchUrl) {
                const searchLink = el("a", "omniscience-workflow-candidate-meta", `在 ${searchEngine === "bing" ? "Bing" : "Google"} 中手动搜索 ${classType}`);
                searchLink.href = parsedSearchUrl.href;
                searchLink.target = "_blank";
                searchLink.rel = "noopener noreferrer";
                group.appendChild(searchLink);
              }
              const unresolvedInstances = (plannedNode?.instances || []).filter((instance) => !instance.source_selected);
              const auxInstances = unresolvedInstances.filter((instance) => instance.aux_id);
              if (auxInstances.length) {
                group.appendChild(el("p", "omniscience-workflow-candidate-meta warning", "该节点存在 aux_id，候选必须按实例匹配；未匹配的实例不会自动归属到同名插件。"));
                auxInstances.forEach((instance) => {
                  const instanceGroup = el("div", "omniscience-workflow-node-source-instance");
                  instanceGroup.appendChild(el("strong", "", `实例 ${instance.id || "未知"} · aux_id=${instance.aux_id}`));
                  const matching = candidates.filter((repo) => repo.matches_aux_id === true || [repo.aux_id, repo.repository, repo.source, repo.manager_id, repo.cnr_id]
                    .map((value) => String(value || "").trim().toLowerCase().replace(/\/$/, ""))
                    .includes(String(instance.aux_id).trim().toLowerCase().replace(/\/$/, "")));
                  if (!matching.length) {
                    instanceGroup.appendChild(el("p", "omniscience-workflow-candidate-meta warning", "没有带有相同 aux_id 证据的候选；请手动确认仓库。"));
                    const manualInput = el("input", "omniscience-workflow-node-manual-input");
                    manualInput.type = "url";
                    manualInput.placeholder = "https://github.com/<OWNER>/<REPO>";
                    manualInput.setAttribute("aria-label", `手动输入 ${classType} 实例 ${instance.id || "未知"} 的 GitHub 仓库地址`);
                    const manualSubmit = button("为此实例使用手动来源", "omniscience-secondary omniscience-workflow-node-manual-submit");
                    manualSubmit.onclick = async () => {
                      const value = manualInput.value.trim();
                      if (!value) return;
                      manualSubmit.disabled = true;
                      try {
                        await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/nodes/source`, {
                          method: "POST",
                          body: JSON.stringify(managerSelectionBody(repo, instance)),
                        });
                        instance.source = value.replace(/\/$/, "");
                        instance.source_type = "manual";
                        instance.source_selected = true;
                        instance.status = "selected";
                        manualSubmit.textContent = "已选择手动来源";
                        renderWorkflowNodeInstallDetails(nodeInstallDetails, plan);
                        prepare.disabled = !nodeTypes.every(nodeSourceReady);
                      } catch (error) {
                        manualSubmit.disabled = false;
                        showError(error);
                      }
                    };
                    instanceGroup.append(manualInput, manualSubmit);
                  }
                  matching.forEach((repo) => {
                    const label = repo.title || repo.manager_id || repo.cnr_id || "未命名节点包";
                    const option = button(`为此实例选择：${label}`, "omniscience-secondary omniscience-workflow-node-source");
                    option.onclick = async () => {
                      option.disabled = true;
                      try {
                        await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/nodes/source`, {
                          method: "POST",
                          body: JSON.stringify(managerSelectionBody(repo, instance)),
                        });
                        instance.source = repo.repository || "";
                        instance.manager_id = repo.manager_id || "";
                        instance.source_selected = true;
                        instance.status = "selected";
                        option.textContent = `已选择：${label}`;
                        prepare.disabled = !nodeTypes.every(nodeSourceReady);
                      } catch (error) {
                        option.disabled = false;
                        showError(error);
                      }
                    };
                    instanceGroup.appendChild(option);
                  });
                  group.appendChild(instanceGroup);
                });
              }
              const regularInstances = unresolvedInstances.filter((instance) => !instance.aux_id);
              const webCandidates = (candidates || []).filter((repo) => ["web_search", "github_proxy"].includes(repo.provider));
              const targets = webCandidates.length
                ? [{ id: "", aux_id: "", web_search: true }]
                : regularInstances.length
                  ? regularInstances
                  : unresolvedInstances.length
                    ? []
                    : [{ id: "", aux_id: "" }];
              const candidateMatches = (repo, target) => {
                const expected = String(target?.aux_id || "").trim().toLowerCase().replace(/\/$/, "");
                if (!expected) return true;
                if (repo.matches_aux_id === true) return true;
                return [repo.aux_id, repo.repository, repo.source, repo.manager_id, repo.cnr_id]
                  .map((value) => String(value || "").trim().toLowerCase().replace(/\/$/, ""))
                  .includes(expected);
              };
              const managerSelectionBody = (repo, target) => {
                const body = {
                  class_type: classType,
                  source: repo.repository || repo.source || repo.reference || "",
                  manager_id: repo.manager_id || "",
                };
                if (target?.id) body.instance_id = target.id;
                if (target?.aux_id) body.aux_id = target.aux_id;
                for (const field of ["cnr_id", "install_id", "install_type", "reference", "pip"]) {
                  if (repo[field] !== undefined && repo[field] !== null && repo[field] !== "") body[field] = repo[field];
                }
                const version = repo.version || repo.selected_version;
                if (version) body.version = version;
                return body;
              };
              const applySelection = (target, selection) => {
                if (!plannedNode) return;
                const occurrence = target?.id
                  ? (plannedNode.instances || []).find((instance) => String(instance.id) === String(target.id))
                  : null;
                const update = occurrence || plannedNode;
                Object.assign(update, {
                  source: selection.source,
                  manager_id: selection.manager_id || "",
                  source_type: selection.source_type,
                  source_selected: true,
                  status: "selected",
                });
                if (!occurrence || (plannedNode.instances || []).every((instance) => instance.source_selected)) {
                  plannedNode.source = selection.source;
                  plannedNode.manager_id = selection.manager_id || "";
                  plannedNode.source_type = selection.source_type;
                  plannedNode.source_selected = true;
                  plannedNode.download_allowed = true;
                }
              };
              targets.forEach((target) => {
                const targetLabel = target.id || target.aux_id
                  ? `实例 ${target.id || "未知"}${target.aux_id ? ` · aux_id=${target.aux_id}` : ""}`
                  : "节点类型级来源";
                const targetGroup = el("div", "omniscience-workflow-node-source-instance");
                targetGroup.appendChild(el("p", "omniscience-workflow-candidate-meta", targetLabel));
                const targetCandidates = (candidates || []).filter((repo) => (
                  target.web_search
                    ? ["web_search", "github_proxy"].includes(repo.provider)
                    : candidateMatches(repo, target)
                ));
                if (!targetCandidates.length) {
                  targetGroup.appendChild(el("p", "omniscience-workflow-candidate-meta warning", target.aux_id
                    ? "没有带有相同 aux_id 证据的 Manager 候选；请手动确认真正的 GitHub 节点仓库。"
                    : "ComfyUI-Manager 没有精确映射，请手动输入真正的 GitHub 节点仓库。"));
                  const manualInput = el("input", "omniscience-workflow-node-manual-input");
                  manualInput.type = "url";
                  manualInput.placeholder = "https://github.com/<OWNER>/<REPO>";
                  manualInput.setAttribute("aria-label", `手动输入 ${classType} ${targetLabel} 的 GitHub 仓库地址`);
                  const manualSubmit = button("使用此 GitHub 地址", "omniscience-secondary omniscience-workflow-node-manual-submit");
                  manualSubmit.onclick = async () => {
                    const value = manualInput.value.trim();
                    if (!value) return;
                    manualSubmit.disabled = true;
                    try {
                      const body = { class_type: classType, source: value, manual: true };
                      if (target?.id) body.instance_id = target.id;
                      if (target?.aux_id) body.aux_id = target.aux_id;
                      await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/nodes/source`, {
                        method: "POST", body: JSON.stringify(body),
                      });
                      manualSubmit.textContent = "已选择手动来源";
                      applySelection(target, { source: value.replace(/\/$/, ""), source_type: "manual" });
                      renderWorkflowNodeInstallDetails(nodeInstallDetails, plan);
                      const allSourcesSelected = nodeTypes.every(nodeSourceReady);
                      prepare.disabled = !allSourcesSelected;
                    } catch (error) {
                      manualSubmit.disabled = false;
                      showError(error);
                    }
                  };
                  targetGroup.append(manualInput, manualSubmit);
                }
                targetCandidates.forEach((repo) => {
                  const label = repo.title || repo.manager_id || repo.cnr_id || repo.full_name || "未命名节点包";
                  const isWebCandidate = ["web_search", "github_proxy"].includes(repo.provider);
                  if (isWebCandidate) {
                    const picker = el("select", "omniscience-workflow-node-source-picker");
                    picker.setAttribute("aria-label", `选择 ${classType} 的 GitHub 节点仓库`);
                    const placeholderOption = el("option", "", "请选择 GitHub 节点仓库…");
                    placeholderOption.value = "";
                    placeholderOption.selected = true;
                    picker.appendChild(placeholderOption);
                    const candidateOption = el("option", "", `${label} · ${repo.source || repo.repository || ""}`);
                    candidateOption.value = repo.candidate_id || "candidate";
                    picker.appendChild(candidateOption);
                    targetGroup.appendChild(el("p", "omniscience-workflow-candidate-meta", `网页搜索 · ${repo.search_engine === "bing" ? "Bing" : "Google"} · 请从下拉列表选择候选仓库`));
                    targetGroup.appendChild(picker);
                    const installOption = button("选择并安装此节点", "omniscience-secondary omniscience-workflow-node-source");
                    installOption.disabled = true;
                    picker.onchange = () => { installOption.disabled = !picker.value; };
                    installOption.onclick = async () => {
                      if (!picker.value) return;
                      installOption.disabled = true;
                      try {
                        const body = {
                          class_type: classType,
                          source: repo.source || repo.repository,
                          candidate_id: repo.candidate_id,
                          provider: repo.provider,
                          region: repo.region,
                        };
                        await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/nodes/source`, {
                          method: "POST", body: JSON.stringify(body),
                        });
                        installOption.textContent = `已选择：${label}`;
                        installOption.classList.add("selected");
                        applySelection({ id: "", aux_id: "" }, { source: repo.source || repo.repository || "", source_type: repo.provider });
                        renderWorkflowNodeInstallDetails(nodeInstallDetails, plan);
                        prepare.disabled = !nodeTypes.every(nodeSourceReady);
                      } catch (error) { installOption.disabled = false; showError(error); }
                    };
                    targetGroup.appendChild(installOption);
                    return;
                  }
                  const option = button(`选择并安装：${label}`, "omniscience-secondary omniscience-workflow-node-source");
                  const details = ["ComfyUI-Manager 精确映射", `包 ID：${repo.manager_id || "未知"}`, repo.repository].filter(Boolean).join(" · ");
                  targetGroup.appendChild(el("p", "omniscience-workflow-candidate-meta", details));
                  option.onclick = async () => {
                    option.disabled = true;
                    try {
                      const body = managerSelectionBody(repo, target);
                      await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/nodes/source`, {
                        method: "POST", body: JSON.stringify(body),
                      });
                      option.textContent = `已选择：${label}`;
                      option.classList.add("selected");
                      applySelection(target, { source: repo.repository || "", manager_id: repo.manager_id, source_type: repo.provider });
                      renderWorkflowNodeInstallDetails(nodeInstallDetails, plan);
                      prepare.disabled = !nodeTypes.every(nodeSourceReady);
                    } catch (error) { option.disabled = false; showError(error); }
                  };
                  targetGroup.appendChild(option);
                });
                group.appendChild(targetGroup);
              });
              sourceSearchArea.appendChild(group);
            }
            if (searched.message) sourceSearchArea.appendChild(el("p", "omniscience-workflow-candidate-meta", searched.message));
            searchButton.textContent = "重新解析 ComfyUI-Manager 映射";
          } catch (error) {
            searchButton.textContent = "重试搜索缺失节点仓库";
            showError(error);
          } finally { searchButton.disabled = false; }
        };
        nodeInstallArea.append(searchButton, sourceSearchArea);
      }
      const hasNodes = nodeTypes.length > 0;
      const nodesReady = hasNodes && nodeTypes.every(nodeSourceReady);
      const hasModels = (plan.models || []).length > 0;
      let nodesPrepared = plan.node_dependencies_ready === true || !hasNodes;
      const maybeAppendExecute = () => {
        const modelsPrepared = (plan.models || []).every((model) => ["installed", "selected_local", "recognized"].includes(model.status));
        if (nodesPrepared && modelsPrepared && plan.can_run === true) appendWorkflowExecuteButton(card, jobId);
      };
      modelSourceArea.addEventListener("workflow-model-prepared", () => {
        void refreshRunStatus().then(() => maybeAppendExecute());
      });
      const prepare = button(hasNodes ? (nodesReady ? `安装并安全检查缺少节点（工作流 ID: ${candidate.id || "未知"}）` : "先选择缺少节点的安装来源") : "检查工作流准备状态", "omniscience-secondary omniscience-workflow-prepare");
      const nodeProgressUi = nodeInstallProgress();
      const runNodeInstallation = async (acknowledgeSecurityFor = "", classTypes = null) => {
        const started = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/nodes/confirm`, {
          method: "POST",
          body: JSON.stringify({
            confirmed: true,
            acknowledge_security_for: acknowledgeSecurityFor,
            ...(Array.isArray(classTypes) ? { class_types: classTypes } : {}),
          }),
        });
        updateNodeInstallProgress(nodeProgressUi, started.progress || {});
        if (started.status === "security_blocked") {
          const error = new Error(started.progress?.error || "Manager 已安装节点，但安全复核未通过");
          error.status = 409;
          if (started.progress?.security) error.payload = { security: started.progress.security };
          throw error;
        }
        if (started.status !== "installing") return started;
        const completed = await waitForNodeInstall(
          jobId,
          (progress) => updateNodeInstallProgress(nodeProgressUi, progress),
          () => nodeProgressUi.container.isConnected,
        );
        updateNodeInstallProgress(nodeProgressUi, completed.progress || {});
        return completed;
      };
      prepare.disabled = hasNodes && !nodesReady;
      const showNodeSecurityReview = (securityError, options = {}) => {
        const security = securityError?.payload?.security;
        if (!security) {
          if (options.inline) card.appendChild(el("p", "omniscience-workflow-candidate-meta warning", `安全检查失败：${securityError.message}`));
          else showError(securityError);
          return;
        }
        const report = securityReport(security);
        if (security.post_install || security.manager_managed) {
          card.append(
            report,
            el(
              "p",
              "omniscience-workflow-candidate-meta warning",
              securityError.message || "ComfyUI-Manager 已经把该节点放入 custom_nodes，但安全复核未通过。请在 Manager 中停用或移除该节点后再处理；Omniscience 不会通过手动 GitHub 兜底安装。",
            ),
          );
          return;
        }
        const retry = button("查看报告后，单独确认仍要安装此风险节点", "omniscience-secondary omniscience-workflow-node-risk-confirm");
        retry.onclick = async () => {
          const findings = (security.findings || []).map((finding) => `${finding.severity}: ${finding.title}`).join("\n");
          if (!confirm(`安全检测报告为“${securityVerdict(security)}”。\n\n${findings}\n\n继续会把风险代码放入 ComfyUI，并可能在重启时执行。仍要继续吗？`)) return;
          if (options.retry) {
            retry.disabled = true;
            try {
              await options.retry();
            } catch (error) {
              retry.disabled = false;
              if (error?.payload?.security) showNodeSecurityReview(error, options);
              else if (options.inline) card.appendChild(el("p", "omniscience-workflow-candidate-meta warning", `安装失败：${error.message}`));
              else showError(error);
            }
            return;
          }
          retry.disabled = true;
          prepare.disabled = true;
          const acknowledgeFor = security.class_type;
          runNodeInstallation(acknowledgeFor, options.classTypes || null).then(() => {
            nodesPrepared = true;
            prepare.disabled = false;
            prepare.textContent = "已完成逐节点安全复核";
            retry.disabled = true;
            maybeAppendExecute();
          }).catch((nextError) => {
            retry.disabled = false;
            prepare.disabled = false;
            if (nextError?.payload?.security) showNodeSecurityReview(nextError);
            else showError(nextError);
          });
        };
        card.append(report, retry);
      };
      prepare.onclick = async () => {
        prepare.disabled = true;
        try {
          if (hasNodes) {
            if (!confirm(`将通过 ComfyUI-Manager 安装已映射节点，并对手动 GitHub 来源执行安全扫描与 NODE_CLASS_MAPPINGS 校验：\n\n${nodeTypes.map((node) => `${node.class_type} ← ${node.manager_id ? `Manager:${node.manager_id}` : node.source}`).join("\n")}\n\n继续？`)) {
              prepare.disabled = false;
              return;
            }
            await runNodeInstallation();
            nodesPrepared = true;
            prepare.disabled = false;
            prepare.textContent = "节点安全检测通过并已安装";
          }
          maybeAppendExecute();
        } catch (error) {
          if (error?.status === 409 && error?.payload?.security) {
            prepare.disabled = false;
            prepare.textContent = "安全检测未通过：查看报告";
            showNodeSecurityReview(error);
            return;
          }
          prepare.disabled = false;
          showError(error);
        }
      };
      if (hasNodes) {
        nodeInstallArea.appendChild(prepare);
        nodeInstallArea.appendChild(nodeProgressUi.container);
        void request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/dependencies/nodes/status`)
          .then((status) => {
            updateNodeInstallProgress(nodeProgressUi, status.progress || {});
            if (status.status === "completed") {
              nodesPrepared = true;
              prepare.disabled = false;
              prepare.textContent = "节点安全检测通过并已安装";
              void refreshRunStatus().then(() => maybeAppendExecute());
            } else if (status.status === "installing") {
              prepare.disabled = true;
              prepare.textContent = "正在安装节点插件…";
              void waitForNodeInstall(
                jobId,
                (progress) => updateNodeInstallProgress(nodeProgressUi, progress),
                () => nodeProgressUi.container.isConnected,
              ).then(() => {
                nodesPrepared = true;
                prepare.disabled = false;
                prepare.textContent = "节点安全检测通过并已安装";
                void refreshRunStatus().then(() => maybeAppendExecute());
              }).catch((error) => {
                prepare.disabled = false;
                if (error?.payload?.security) showNodeSecurityReview(error);
                else showError(error);
              });
            } else if (status.status === "failed") {
              prepare.disabled = false;
              prepare.textContent = status.progress?.interrupted ? "安装被重启中断，重试" : "节点安装失败，重试";
              if (status.progress?.security) {
                const error = new Error(status.progress.error || "节点插件安装失败");
                error.payload = { security: status.progress.security };
                error.classType = status.progress.blocked_node || status.progress.security.class_type || "";
                showNodeSecurityReview(error, { classTypes: error.classType ? [error.classType] : null });
              }
            } else if (status.status === "security_review_required" || status.status === "security_blocked") {
              prepare.disabled = Boolean(status.progress?.manager_installed);
              prepare.textContent = status.progress?.manager_installed
                ? "Manager 已安装，但安全复核未通过"
                : "安全检测发现风险：查看报告并决定是否继续";
              const error = new Error(status.progress?.error || "节点安全检查发现风险，请查看报告并决定是否继续安装");
              error.status = 409;
              if (status.progress?.security) error.payload = { security: status.progress.security };
              const classType = status.progress?.blocked_node || status.progress?.security?.class_type || "";
              showNodeSecurityReview(error, { classTypes: classType ? [classType] : null });
            }
          })
          .catch(() => {});
      }
      card.appendChild(el("p", "omniscience-workflow-candidate-meta omniscience-workflow-next-step", "节点安装需单独确认；模型可优先选择本地相似量化文件，也可搜索并选择包含目标文件的仓库，选定远程来源后会立即下载该文件。只有依赖检查通过后才会显示运行按钮。"));
      void refreshRunStatus().then(() => maybeAppendExecute());
      maybeAppendExecute();
      } catch (error) {
      action.disabled = false;
      showError(error);
    }
  };
  card.appendChild(action);
  if (options.autoPrepare) {
    action.textContent = "正在检查工作流…";
    action.dataset.restore = "true";
    queueMicrotask(() => action.click());
  }
  // The conversation transcript contains the candidate, while selection,
  // source choice, and download progress live in the persisted workflow job.
  // Re-entering the conversation must therefore restore the selected card
  // instead of asking the user to select it again. Uploaded workflows start
  // this same preparation flow automatically after the safe copy.
  if (options.autoRestore === false) return card;
  void request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}`)
    .then((job) => {
      // A failed job must be rendered even when the selected candidate field
      // was not persisted, or when an older payload used a numeric candidate
      // id.  Render it even if the request completed before the card was
      // attached to the document; detached DOM nodes are attached by the
      // caller immediately after this function returns.
      if (workflowJobHasExecutionFailure(job) && workflowFailureBelongsToCandidate(job, candidate)) {
        appendWorkflowExecutionFailure(card, job);
      }
      if (!card.isConnected
        || String(job?.selected_candidate_id || "") !== String(candidate.id || "")
        || card.classList.contains("selected")) return;
      action.dataset.restore = "true";
      action.click();
    })
    .catch(() => {});
  return card;
}

function appendWorkflowExecuteButton(card, jobId) {
  card.dataset.workflowJobId = jobId;
  if (card.querySelector(".omniscience-workflow-execute")) return;
  const execute = button("确认执行工作流", "omniscience-secondary omniscience-workflow-execute");
  execute.onclick = async () => {
    execute.disabled = true;
    try {
      const result = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}/execute`, { method: "POST", body: JSON.stringify({ confirmed: true }) });
      execute.textContent = `执行完成（${Object.keys(result.outputs || {}).length} 个输出节点）`;
      card.classList.add("completed");
      card.classList.remove("failed");
      card.querySelector(".omniscience-workflow-execution-error")?.remove();
    } catch (error) {
      execute.disabled = false;
      execute.textContent = "重新确认执行工作流";
      appendWorkflowExecutionFailure(card, error);
      try {
        const job = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}`);
        if (workflowJobHasExecutionFailure(job) && card.isConnected) appendWorkflowExecutionFailure(card, job);
      } catch { /* the original execution error is already visible on the card */ }
      showError(error);
    }
  };
  // Keep the execute button at its normal insertion point.  If the failure
  // card was restored before this asynchronous button creation completed,
  // move only the failure card below the button.
  card.appendChild(execute);
  const failure = card.querySelector(".omniscience-workflow-execution-error");
  if (failure && execute.nextElementSibling !== failure) {
    execute.insertAdjacentElement("afterend", failure);
  }
  void request(`/omniscience/global-chat/workflows/${encodeURIComponent(jobId)}`)
    .then((job) => {
      if (workflowJobHasExecutionFailure(job) && card.isConnected) {
        appendWorkflowExecutionFailure(card, job);
        execute.textContent = "重新确认执行工作流";
      }
    })
    .catch(() => {});
}

function appendWorkflowCandidates(content, article) {
  let payload;
  try { payload = JSON.parse(String(content || "")); } catch { return; }
  if (!payload?.job_id) return;
  if (!Array.isArray(payload.candidates) || !payload.candidates.length) {
    const box = el("div", "omniscience-workflow-no-results");
    box.appendChild(el("p", "omniscience-hint", payload.message || "没有找到合适的工作流。"));
    const develop = button("创建开发任务", "omniscience-secondary omniscience-workflow-develop");
    develop.onclick = async () => {
      develop.disabled = true;
      try {
        const result = await request(`/omniscience/global-chat/workflows/${encodeURIComponent(payload.job_id)}/develop`, {
          method: "POST", body: JSON.stringify({ confirmed: true, locale: state.locale }),
        });
        develop.textContent = `已创建任务：${result.folder || result.task_id}`;
        if (result.task_id) await loadTask(result.task_id);
      } catch (error) {
        develop.disabled = false;
        showError(error);
      }
    };
    box.appendChild(develop);
    article.appendChild(box);
    return;
  }
  const list = el("div", "omniscience-workflow-candidates");
  list.appendChild(el("strong", "omniscience-workflow-candidates-title", "找到以下工作流，请选择一个："));
  payload.candidates.forEach((candidate) => list.appendChild(workflowCandidateCard(payload.job_id, candidate)));
  article.appendChild(list);
}

function parseToolResult(content) {
  try {
    const parsed = JSON.parse(String(content || ""));
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function workflowInstallCard(content) {
  const result = parseToolResult(content) || {};
  const failed = Boolean(result.error) || result.status === "failed";
  const card = el("div", `omniscience-workflow-install-card ${failed ? "failed" : "installed"}`);
  const heading = el("div", "omniscience-workflow-install-heading");
  heading.appendChild(el("strong", "", "工作流安装"));
  heading.appendChild(el("span", `omniscience-workflow-install-status ${failed ? "failed" : "installed"}`, failed ? "安装失败" : "安装成功"));
  card.appendChild(heading);

  if (failed) {
    card.appendChild(el("p", "omniscience-workflow-install-error", String(result.error || "工作流安装失败。")));
    card.appendChild(el("p", "omniscience-workflow-install-hint", "请修复问题或更换文件名后重试；原上传文件不会被删除。"));
    return card;
  }

  const source = String(result.source || "");
  const destination = String(result.destination || "");
  const fileName = source.split("/").filter(Boolean).pop() || destination.split(/[\\/]/).filter(Boolean).pop() || "未命名工作流";
  const details = el("div", "omniscience-workflow-install-details");
  const fields = [
    ["文件", fileName],
    ["格式", result.format || "未知"],
    ["节点", `${Number(result.node_count) || 0} 个`],
    ["模型引用", `${Number(result.model_count) || 0} 个`],
    ["目标", destination || "ComfyUI 用户工作流目录"],
  ];
  for (const [label, value] of fields) {
    const row = el("div", "omniscience-workflow-install-detail");
    row.appendChild(el("span", "omniscience-workflow-install-label", label));
    row.appendChild(el("span", "omniscience-workflow-install-value", String(value)));
    details.appendChild(row);
  }
  card.appendChild(details);

  const safety = el("div", "omniscience-workflow-install-safety");
  safety.appendChild(el("strong", "", "安全说明"));
  for (const text of ["未执行工作流", "未安装节点", "未下载模型", "未覆盖同名文件"]) {
    safety.appendChild(el("span", "omniscience-workflow-install-safe-item", `✓ ${text}`));
  }
  card.appendChild(safety);
  if (result.workflow_job_id && result.candidate_id && result.candidate) {
    const preparation = el("div", "omniscience-workflow-install-preparation");
    preparation.appendChild(el("strong", "omniscience-workflow-install-preparation-title", "工作流准备"));
    preparation.appendChild(el("p", "omniscience-workflow-install-next-step", "文件已安全复制，正在按普通工作流卡片流程检查节点和模型依赖；不会自动执行工作流。"));
    preparation.appendChild(workflowCandidateCard(
      result.workflow_job_id,
      result.candidate,
      { uploaded: true, autoRestore: false, autoPrepare: true, autoLoadCanvas: true, forceInspection: true },
    ));
    card.appendChild(preparation);
  }
  return card;
}

function toolResultView(content) {
  const text = String(content || "");
  const limit = 2400;
  if (text.length <= limit) return lines(text);
  const details = document.createElement("details");
  details.className = "omniscience-tool-result-details";
  const summary = document.createElement("summary");
  summary.textContent = `工具结果过长，已折叠（${text.length.toLocaleString()} 字符）`;
  details.appendChild(summary);
  const body = el("div", "omniscience-tool-result-body");
  body.appendChild(lines(text));
  details.appendChild(body);
  return details;
}

function completedAssistantMessageIndex(messages) {
  if (displayedBusy() || !messages.length) return -1;
  const lastIndex = messages.length - 1;
  const lastMessage = messages[lastIndex];
  if (lastMessage?.role !== "assistant" || lastMessage.tool_calls?.length) return -1;
  if (!state.globalChatActive && ["running", "waiting_approval"].includes(state.task?.status)) return -1;
  const outcome = state.globalChatActive ? state.lastGlobalOutcome : state.lastTaskOutcome;
  if (TERMINAL_FAILURES.has(outcome)) return -1;
  return outcome === "completed" ? lastIndex : -1;
}

function restoreCompletedTaskOutcome() {
  if (state.busy || ["running", "waiting_approval"].includes(state.task?.status)) return;
  if (TERMINAL_FAILURES.has(state.lastTaskOutcome)) return;
  const lastMessage = state.messages.at(-1);
  if (lastMessage?.role === "assistant" && !lastMessage.tool_calls?.length) {
    state.lastTaskOutcome = "completed";
  }
}

function renderMessages() {
  const log = document.querySelector(`#${PANEL_ID} .omniscience-messages`);
  if (!log) return;
  const messages = displayedMessages();
  const shouldFollow = log.scrollHeight - log.scrollTop - log.clientHeight <= 24;
  log.replaceChildren();
  if (!messages.length) {
    const welcome = el("div", "omniscience-welcome");
    welcome.append(el("strong", "", state.globalChatActive ? "全局对话" : "Comfyui-omniscience for ComfyUI"));
    welcome.append(el("p", "", state.globalChatActive
      ? "这是独立于所有任务的全局对话。你可以讨论想法、提问，或提供公开 GitHub Skill 链接并明确要求安装；不会读取或修改项目文件。"
      : "创建或选择一个任务，然后描述要检查、规划或修改的内容。Plan 模式只允许读取或浏览；Act 模式会直接执行允许的写入、命令及外部工具调用。"));
    log.appendChild(welcome);
  }
  const toolNames = new Map();
  const completedIndex = completedAssistantMessageIndex(messages);
  for (const message of messages) {
    for (const call of message.tool_calls || []) {
      const callId = String(call.id || "").trim();
      const name = String(call.function?.name || "").trim();
      if (callId && name) toolNames.set(callId, name);
    }
  }
  messages.forEach((message, index) => {
    const role = message.role || "assistant";
    const completed = index === completedIndex ? " completed" : "";
    const article = el("article", `omniscience-message ${role}${completed}`);
    article.appendChild(el("div", "omniscience-message-role", role === "user" ? "你" : role === "assistant" ? "Omniscience" : "工具结果"));
    const content = el("div", "omniscience-message-content");
    const toolName = role === "tool" ? toolNames.get(String(message.tool_call_id || "")) : "";
    const isWorkflowInstall = role === "tool" && (
      toolName === "install_uploaded_workflow"
      || message.tool_call_id === "workflow-install-fallback"
    );
    content.appendChild(isWorkflowInstall ? workflowInstallCard(message.content || "") : role === "tool" ? toolResultView(message.content || "") : lines(message.content || ""));
    article.appendChild(content);
    appendWorkflowCandidates(message.content, article);
    if (message.tool_calls?.length) {
      article.appendChild(el("div", "omniscience-tool-calls", `工具调用：${message.tool_calls.map((call) => call.function?.name || "未知工具").join("、")}`));
    }
    log.appendChild(article);
  });
  if (state.error) log.appendChild(el("div", "omniscience-error", state.error));
  if (displayedBusy() || (!state.globalChatActive && state.task?.status === "running")) log.appendChild(el("div", "omniscience-status", "Omniscience 正在思考或执行…"));
  if (shouldFollow) log.scrollTop = log.scrollHeight;
}

function nodeScanStateKey(kind, value) {
  return `${kind}:${value}`;
}

function nodeActions(control, onClick, scanKey) {
  const wrap = el("span", "omniscience-node-actions");
  const scanning = state.nodeScans[scanKey]?.status === "scanning";
  // The project rows are buttons, so the scan control has to stay a span.
  const scan = el("span", "omniscience-node-scan", scanning ? "检测中…" : "安全检测");
  scan.setAttribute("role", "button");
  scan.setAttribute("aria-busy", scanning ? "true" : "false");
  scan.setAttribute("aria-disabled", scanning ? "true" : "false");
  scan.title = "按设置里的检测方式扫描该项目源码（AI/混合模式会调用模型）";
  if (scanning) scan.classList.add("busy");
  const run = async () => {
    if (state.nodeScans[scanKey]?.status === "scanning") return;
    state.nodeScans[scanKey] = { status: "scanning", error: "" };
    renderTasks();
    try {
      await onClick();
      delete state.nodeScans[scanKey];
    } catch (error) {
      state.nodeScans[scanKey] = { status: "failed", error: error.message };
      setNodeScanStatus(`检测失败：${error.message}`);
    } finally {
      renderTasks();
    }
  };
  scan.onclick = (event) => {
    event.stopPropagation();
    run();
  };
  scan.onkeydown = (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.stopPropagation();
    event.preventDefault();
    run();
  };
  wrap.append(control, scan);
  return wrap;
}

function setNodeScanStatus(message) {
  const live = document.querySelector(`#${PANEL_ID} .omniscience-node-scan-status`);
  if (live) live.textContent = message;
}

async function scanProject(folder) {
  setNodeScanStatus(`正在检测“${folder}”…`);
  const result = await request("/omniscience/custom-nodes/scan", { method: "POST", body: JSON.stringify({ folder }) });
  await loadTasks();
  setNodeScanStatus(`“${folder}”${securityStatusText(result.security)}`);
}

async function scanTaskProject(taskId) {
  const task = state.tasks.find((item) => item.id === taskId);
  setNodeScanStatus(`正在检测“${task?.folder || "任务项目"}”…`);
  const result = await request(`/omniscience/tasks/${encodeURIComponent(taskId)}/scan`, { method: "POST" });
  await loadTasks();
  setNodeScanStatus(`“${task?.folder || "任务项目"}”${securityStatusText(result.security)}`);
}

async function scanAllProjects(control) {
  if (control) control.disabled = true;
  setNodeScanStatus("正在静态检测全部 custom_nodes 项目…");
  try {
    const result = await request("/omniscience/custom-nodes/scan-all", { method: "POST", body: "{}" });
    await loadTasks();
    const flagged = (result.results || []).filter((item) => item.security?.verdict && item.security.verdict !== "clean");
    setNodeScanStatus(
      `已静态检测 ${result.scanned || 0} 个项目，${flagged.length} 个需复核`
      + (result.remaining ? `；剩余 ${result.remaining} 个未检测，可再次点击。` : "。"),
    );
  } catch (error) {
    setNodeScanStatus(`检测失败：${error.message}`);
  } finally {
    if (control) control.disabled = false;
  }
}

async function installNodePayload(payload, label) {
  if (state.nodeInstallBusy) return;
  state.nodeInstallBusy = true;
  setNodeInstallButtons(true);
  try {
    for (const acknowledged of [false, true]) {
      nodeInstallStatus(`正在${acknowledged ? "重新" : ""}安装${label}（先做安全检测）…`);
      try {
        const result = await request("/omniscience/custom-nodes/install", {
          method: "POST",
          body: JSON.stringify(acknowledged ? { ...payload, acknowledge_security: true } : payload),
        });
        await loadTasks();
        const dependencies = (result.dependencies || []).length ? `；依赖需自行安装：${result.dependencies.join("、")}` : "";
        nodeInstallStatus(`已安装 ${result.folder}：${securityStatusText(result.security)}${dependencies}未安装依赖、未执行任何脚本，需重启 ComfyUI 才会加载。`);
        return;
      } catch (error) {
        // Only a blocked report (409 + security) may be acknowledged; name conflicts cannot.
        if (!acknowledged && error.status === 409 && error.payload?.security && securityConfirmation(error, "安装")) continue;
        nodeInstallStatus(`安装失败：${error.message}`);
        return;
      }
    }
  } finally {
    state.nodeInstallBusy = false;
    setNodeInstallButtons(false);
  }
}

function nodeInstallStatus(message) {
  const live = document.querySelector(`#${PANEL_ID} .omniscience-node-install-status`);
  if (live) live.textContent = message;
}

function setNodeInstallButtons(busy) {
  for (const selector of [".omniscience-install-node-github", ".omniscience-install-node-archive"]) {
    const control = document.querySelector(`#${PANEL_ID} ${selector}`);
    if (control) control.disabled = busy;
  }
}

async function installNodeArchive(file) {
  if (!file) return;
  let encoded = "";
  try {
    encoded = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || "").split(",").pop() || "");
      reader.onerror = () => reject(new Error(`读取 ${file.name} 失败。`));
      reader.readAsDataURL(file);
    });
  } catch (error) {
    nodeInstallStatus(`安装失败：${error.message}`);
    return;
  }
  await installNodePayload({ archive_name: file.name, archive_base64: encoded }, `节点 ${file.name}`);
}

function nodeInstallControls() {
  const wrap = el("div", "omniscience-node-install");
  wrap.appendChild(el("h3", "", "安装节点"));
  const url = document.createElement("input");
  url.type = "text";
  url.className = "omniscience-node-install-url";
  url.placeholder = "https://github.com/所有者/仓库（或 tree 文件夹链接）";
  const fromGithub = button("从 GitHub 安装", "omniscience-secondary omniscience-install-node-github");
  fromGithub.onclick = () => {
    const value = url.value.trim();
    if (!value) { nodeInstallStatus("请先填写 GitHub 仓库地址。"); return; }
    installNodePayload({ github_url: value }, `节点 ${value}`);
  };
  const picker = document.createElement("input");
  picker.type = "file";
  picker.accept = ".zip,.tar,.tar.gz,.tgz,.tar.bz2,.tbz2,.tar.xz,.txz,application/zip,application/gzip,application/x-tar";
  picker.hidden = true;
  picker.onchange = async () => {
    const file = picker.files?.[0];
    picker.value = "";
    if (!file) return;
    await installNodeArchive(file);
  };
  const fromArchive = button("上传压缩包安装", "omniscience-secondary omniscience-install-node-archive");
  fromArchive.onclick = () => { if (!state.nodeInstallBusy) picker.click(); };
  const actions = el("div", "omniscience-node-install-actions");
  actions.append(fromGithub, fromArchive, picker);
  wrap.appendChild(url);
  wrap.appendChild(actions);
  wrap.appendChild(el("p", "omniscience-node-install-status omniscience-hint"));
  wrap.appendChild(el("p", "omniscience-hint", "只下载并复制节点文件：不安装依赖、不执行包内脚本。接管前会先做安全检测，检测方式与 Skill 共用同一开关（AI 或静态+AI 时每次安装消耗一次模型调用）。"));
  return wrap;
}

async function deleteTask(task) {
  const folder = task.folder || task.title || "未命名项目";
  if (!confirm(`确定删除任务节点“${folder}”？\n\n只删除任务记录、对话历史和由任务创建的软链接，不删除项目文件夹或源码。`)) return;
  try {
    await request(`/omniscience/tasks/${encodeURIComponent(task.id)}`, { method: "DELETE" });
    if (state.task?.id === task.id) {
      state.task = null;
      state.messages = [];
      state.events = [];
      state.contextStats = null;
    }
    await loadTasks();
    renderPanel();
  } catch (error) {
    showError(error);
  }
}

function taskNodeRow(task) {
  const active = !state.globalChatActive && task.id === currentTaskId();
  const scanKey = nodeScanStateKey("task", task.id);
  const scanState = state.nodeScans[scanKey];
  const nodeStatus = scanState?.status === "scanning" ? "检测中" : scanState?.status === "failed" ? "检测失败" : statusText(task.status);
  const nodeStatusClass = scanState?.status === "scanning" ? "running" : scanState?.status === "failed" ? "failed" : statusClass(task.status);
  const row = button("", `omniscience-task-node${active ? " active" : ""}`);
  const port = el("span", `omniscience-task-node-port ${nodeStatusClass}`);
  port.setAttribute("aria-hidden", "true");
  const header = el("span", "omniscience-task-node-header");
  const status = el("span", `omniscience-task-node-status ${nodeStatusClass}`, nodeStatus);
  if (scanState?.status === "failed") status.title = scanState.error || "安全检测失败，可重新检测。";
  header.append(
    el("strong", "omniscience-task-title", task.folder || task.title || "未命名项目"),
    status,
  );
  const description = el("span", "omniscience-task-node-description", task.title || "未命名任务");
  const footer = el("span", "omniscience-task-node-footer");
  footer.append(
    el("span", "omniscience-task-node-mode", task.mode === "plan" ? "Plan" : "Act"),
    ...(active ? [el("span", "omniscience-task-prompt-length", promptLengthText(state.contextStats).replace("提示词：", ""))] : []),
    el("span", "omniscience-task-node-date", taskDate(task)),
  );
  row.append(port, header, description, footer);
  row.appendChild(nodeActions(skillSecurityControl(task.metadata?.security), () => scanTaskProject(task.id), scanKey));
  const open = el("span", "omniscience-node-open", "打开工作流");
  open.setAttribute("role", "button");
  open.tabIndex = 0;
  open.title = "在 ComfyUI 画布打开此任务的 workflow.json；没有文件时打开实时画布，也可以右键任务节点";
  const runOpen = (event) => {
    event.stopPropagation();
    if (!state.taskLoading) void openTaskWorkflow(task.id);
  };
  open.onclick = runOpen;
  open.onkeydown = (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    runOpen(event);
  };
  row.appendChild(open);
  const remove = el("span", "omniscience-node-delete", "删除");
  remove.setAttribute("role", "button");
  remove.tabIndex = 0;
  remove.title = "删除任务记录；项目文件夹和源码会保留";
  const runDelete = (event) => {
    event.stopPropagation();
    deleteTask(task);
  };
  remove.onclick = runDelete;
  remove.onkeydown = (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    runDelete(event);
  };
  row.appendChild(remove);
  row.title = task.id;
  row.setAttribute("aria-current", active ? "true" : "false");
  row.onclick = () => loadTask(task.id);
  row.oncontextmenu = (event) => {
    event.preventDefault();
    if (!state.taskLoading) void openTaskWorkflow(task.id);
  };
  return row;
}

function externalProjectRow(project) {
  const row = button("", "omniscience-task-node external");
  const port = el("span", "omniscience-task-node-port idle");
  port.setAttribute("aria-hidden", "true");
  const header = el("span", "omniscience-task-node-header");
  header.append(
    el("strong", "omniscience-task-title", project.folder),
    el("span", "omniscience-task-node-kind", "ComfyUI"),
  );
  const description = el("span", "omniscience-task-node-description", "custom_nodes 项目，点击接管为任务");
  const footer = el("span", "omniscience-task-node-footer");
  footer.append(
    el("span", "omniscience-task-node-mode", "待接管"),
    el("span", "omniscience-task-node-date", taskDate({ updated_at: project.modified_at })),
  );
  row.append(port, header, description, footer);
  row.appendChild(nodeActions(skillSecurityControl(project.security), () => scanProject(project.folder), nodeScanStateKey("project", project.folder)));
  row.title = project.path;
  row.onclick = () => adoptProject(project.folder);
  return row;
}

async function adoptProject(folder) {
  const body = (acknowledged) => JSON.stringify(acknowledged ? { folder, acknowledge_security: true } : { folder });
  try {
    const result = await request("/omniscience/custom-nodes/adopt", { method: "POST", body: body(false) });
    await loadTasks();
    if (result.task && result.task.id) await loadTask(result.task.id);
    return;
  } catch (error) {
    // Only a security gate (409 with a report) may be acknowledged; a folder name conflict cannot.
    if (error.status !== 409 || !error.payload?.security || !securityConfirmation(error, "接管")) {
      showError(error);
      return;
    }
  }
  try {
    const retry = await request("/omniscience/custom-nodes/adopt", { method: "POST", body: body(true) });
    await loadTasks();
    if (retry.task && retry.task.id) await loadTask(retry.task.id);
  } catch (retryError) {
    showError(retryError);
  }
}

function renderTasks() {
  const list = document.querySelector(`#${PANEL_ID} .omniscience-task-list`);
  if (!list) return;
  list.replaceChildren();
  const nodes = [
    ...state.tasks.map((task) => ({ when: Number(task.updated_at || task.created_at) || 0, row: taskNodeRow(task) })),
    ...state.projects.map((project) => ({ when: Number(project.modified_at) || 0, row: externalProjectRow(project) })),
  ].sort((left, right) => right.when - left.when);
  if (!nodes.length) {
    list.appendChild(el("p", "omniscience-task-empty omniscience-hint", "还没有任务节点。"));
    return;
  }
  for (const node of nodes) {
    const item = el("div", "omniscience-task-history-item");
    item.appendChild(node.row);
    list.appendChild(item);
  }
}

function renderHeader() {
  const title = document.querySelector(`#${PANEL_ID} .omniscience-current-task`);
  const promptStats = document.querySelector(`#${PANEL_ID} .omniscience-prompt-stats`);
  const status = document.querySelector(`#${PANEL_ID} .omniscience-task-status`);
  if (title) title.textContent = state.globalChatActive
    ? "全局对话（独立于任务）"
    : state.task ? `${state.task.folder || state.task.title} · ${state.task.title || "未命名任务"}` : "未选择任务";
  if (promptStats) {
    promptStats.textContent = state.globalChatActive ? "提示词：—" : promptLengthText(state.contextStats);
    promptStats.title = state.globalChatActive ? "全局对话不包含任务上下文或工具定义。" : "当前模型提示词估算，包括系统指令、工具定义和任务对话。";
  }
  if (status) status.textContent = state.globalChatActive ? (state.globalBusy ? "思考中" : "") : state.task ? statusText(state.task.status) : "";
  const globalChat = document.querySelector(`#${PANEL_ID} .omniscience-global-chat-toggle`);
  if (globalChat) {
    globalChat.textContent = state.globalChatActive ? "返回任务对话" : "全局对话";
    globalChat.classList.toggle("active", state.globalChatActive);
    globalChat.title = state.globalChatActive ? "返回当前任务对话" : "打开独立于任务的全局对话";
  }
  const cancel = document.querySelector(`#${PANEL_ID} .omniscience-cancel-task`);
  if (cancel) {
    const cancellable = !state.globalChatActive && ["running", "waiting_approval"].includes(state.task?.status);
    cancel.hidden = !cancellable;
    cancel.disabled = state.cancelling;
    cancel.textContent = state.cancelling ? "正在取消…" : "强制取消";
  }
  const composerInput = document.querySelector(`#${PANEL_ID} textarea[data-composer]`);
  const send = document.querySelector(`#${PANEL_ID} .omniscience-send-message`);
  // Cancellation stops only the current run. The task remains resumable, so
  // the composer always stays available for the next turn in this node.
  if (composerInput) {
    composerInput.disabled = false;
    composerInput.placeholder = state.globalChatActive ? "输入想讨论的问题…" : "描述你要完成的编程任务…";
  }
  if (send) send.disabled = displayedBusy();
  const clear = document.querySelector(`#${PANEL_ID} .omniscience-clear-history`);
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
  const contextArea = document.querySelector(`#${PANEL_ID} .omniscience-context-label`);
  if (contextArea) contextArea.hidden = state.globalChatActive;
  if (state.globalChatActive) return;
  const label = contextArea;
  if (label) label.textContent = state.context.length ? `已选择 ${state.context.length} 个文件上下文` : "点击文件可加入下一条消息的上下文";
}

function renderSkillSelector() {
  const area = document.querySelector(`#${PANEL_ID} .omniscience-skill-selector`);
  if (!area) return;
  area.hidden = false;
  area.replaceChildren();
  const heading = el("div", "omniscience-skill-selector-heading");
  heading.append(el("strong", "", "当前会话 Skill"));
  const manage = button("管理 Skill", "omniscience-secondary");
  manage.onclick = () => setView("skills");
  heading.appendChild(manage);
  area.appendChild(heading);
  const enabled = state.skills.filter((skill) => skill.enabled);
  if (!enabled.length) {
    const hint = el("p", "omniscience-hint", "还没有启用的 Skill。点击“管理 Skill”创建一个。");
    area.appendChild(hint);
    return;
  }
  const selected = el("div", "omniscience-selected-skills");
  for (const skill of enabled.filter((item) => state.selectedSkillIds.includes(item.id))) {
    const chip = el("span", "omniscience-skill-chip");
    const remove = button("×", "omniscience-skill-chip-remove");
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
  area.appendChild(el("p", "omniscience-hint", "输入框键入 / 呼起全局 Skill 列表；选中的 Skill 会用于后续消息，不属于任何任务。"));
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
  const menu = el("div", "omniscience-skill-command-menu");
  const matches = matchingSkills();
  if (!matches.length) {
    menu.appendChild(el("p", "omniscience-hint", "没有匹配的启用 Skill。"));
  } else {
    const list = el("div", "omniscience-skill-command-list");
    matches.forEach((skill, index) => {
      const item = button("", `omniscience-skill-command-item${index === state.skillMenuIndex ? " active" : ""}`);
      item.append(el("strong", "", skill.name), el("span", "omniscience-skill-command-description", skill.description || "无描述"));
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

function toolCommand(value) {
  if (!state.globalChatActive) return null;
  const match = String(value || "").match(/(?:^|\s)@([^\s]*)$/);
  if (!match) return null;
  const raw = match[1] || "";
  const query = raw.toLocaleLowerCase();
  if (query && !"workflow".startsWith(query) && !"node".startsWith(query)) return null;
  return {
    query,
    start: match.index + match[0].lastIndexOf("@"),
    end: match.index + match[0].lastIndexOf("@") + raw.length + 1,
  };
}

function matchingTools() {
  const query = state.toolQuery.trim().toLocaleLowerCase();
  return [
    { id: "workflow", label: "@workflow", description: "安装最近上传的 JSON 工作流" },
    { id: "node", label: "@node", description: "节点安装（后续支持）", disabled: true },
  ].filter((tool) => !query || tool.id.startsWith(query));
}

function resetToolMenu() {
  state.toolMenuOpen = false;
  state.toolQuery = "";
  state.toolMenuIndex = 0;
}

function renderToolMenu(area) {
  area.querySelector(".omniscience-tool-command-menu")?.remove();
  if (!state.toolMenuOpen) return;
  const menu = el("div", "omniscience-tool-command-menu");
  const matches = matchingTools();
  if (!matches.length) {
    menu.appendChild(el("p", "omniscience-hint", "没有匹配的工具。"));
  } else {
    const list = el("div", "omniscience-tool-command-list");
    matches.forEach((tool, index) => {
      const item = button("", `omniscience-tool-command-item${index === state.toolMenuIndex ? " active" : ""}`);
      item.disabled = !!tool.disabled;
      item.append(el("strong", "", tool.label), el("span", "omniscience-tool-command-description", tool.description));
      if (!tool.disabled) item.onclick = () => selectToolFromMenu(tool.id);
      list.appendChild(item);
    });
    menu.appendChild(list);
  }
  area.appendChild(menu);
}

function selectToolFromMenu(toolId) {
  if (toolId !== "workflow") return;
  const input = composerInput();
  const command = toolCommand(input?.value || "");
  if (input && command) input.value = `${input.value.slice(0, command.start)}@workflow `;
  resetToolMenu();
  resetFileMenu();
  input?.dispatchEvent(new Event("input"));
  input?.focus();
}

function fileCommand(value) {
  const match = String(value || "").match(/(?:^|\s)@([^\s]*)$/);
  if (!match) return null;
  const raw = match[1] || "";
  if (!raw || /^(?:workflow|node)$/i.test(raw)) return null;
  return { query: raw, start: match.index + match[0].lastIndexOf("@") };
}

function resetFileMenu() {
  state.fileMenuOpen = false;
  state.fileQuery = "";
  state.fileMenuIndex = 0;
  resetToolMenu();
}

function matchingFiles() {
  const query = state.fileQuery.trim().toLocaleLowerCase();
  return state.fileList
    .filter((entry) => entry.type === "file")
    .filter((entry) => !query || `${entry.path} ${entry.name}`.toLocaleLowerCase().includes(query))
    .slice(0, 30);
}

function composerScopeKey() {
  return state.globalChatActive ? "global" : currentTaskId();
}

function composerHasScope() {
  return state.globalChatActive || !!currentTaskId();
}

function globalNetworkEnabled() {
  return state.globalChatActive ? !!state.globalNetworkEnabled : !!((state.task?.metadata || {}).network_enabled);
}

async function loadFileList({ force = false } = {}) {
  const scope = composerScopeKey();
  if (!scope) return [];
  if (!force && state.fileListScope === scope) return state.fileList;
  if (state.fileListLoading) return state.fileList;
  state.fileListLoading = true;
  try {
    const query = state.globalChatActive ? "scope=global" : `task_id=${encodeURIComponent(scope)}`;
    const result = await request(`/omniscience/files?${query}&recursive=true`);
    state.fileList = result.entries || [];
    state.fileListScope = scope;
  } catch (error) {
    console.warn("[Comfyui-omniscience] file list load failed", error);
  } finally {
    state.fileListLoading = false;
  }
  return state.fileList;
}

function selectFileFromMenu(path) {
  const input = document.querySelector(`#${PANEL_ID} textarea[data-composer]`);
  const command = fileCommand(input?.value || "");
  if (input && command) {
    input.value = `${input.value.slice(0, command.start)}@${path} `;
  } else if (input) {
    input.value = `${input.value}${input.value && !/\s$/.test(input.value) ? " " : ""}@${path} `;
  }
  resetFileMenu();
  input?.dispatchEvent(new Event("input"));
  renderFileMenu();
  input?.focus();
}

function renderFileMenu() {
  const area = document.querySelector(`#${PANEL_ID} .omniscience-composer-actions`);
  if (!area) return;
  renderToolMenu(area);
  area.querySelector(".omniscience-file-command-menu")?.remove();
  const upload = area.querySelector(".omniscience-upload-file");
  const mention = area.querySelector(".omniscience-mention-file");
  const network = area.querySelector(".omniscience-network-toggle");
  const hasScope = composerHasScope();
  const networkOn = globalNetworkEnabled();
  for (const node of [upload, mention]) if (node) node.hidden = !hasScope;
  if (network) {
    network.hidden = !hasScope;
    network.classList.toggle("active", networkOn);
    network.textContent = networkOn ? "网络访问：已开启" : "网络访问：已关闭";
    network.disabled = state.networkBusy || displayedBusy();
  }
  if (upload) upload.disabled = state.uploadBusy || !hasScope;
  if (mention) mention.disabled = !hasScope;
  if (!hasScope || !state.fileMenuOpen) return;
  const menu = el("div", "omniscience-file-command-menu");
  if (state.fileListLoading) {
    menu.appendChild(el("p", "omniscience-hint", "正在读取文件列表…"));
  } else {
    const matches = matchingFiles();
    if (!matches.length) menu.appendChild(el("p", "omniscience-hint", state.globalChatActive ? "全局工作区还没有文件，可先上传。" : "没有匹配的文件。"));
    else {
      const list = el("div", "omniscience-file-command-list");
      matches.forEach((entry, index) => {
        const item = button("", `omniscience-file-command-item${index === state.fileMenuIndex ? " active" : ""}`);
        item.append(el("strong", "", entry.name), el("span", "omniscience-file-command-path", entry.path));
        item.title = entry.path;
        item.onclick = () => selectFileFromMenu(entry.path);
        list.appendChild(item);
      });
      menu.appendChild(list);
    }
  }
  area.appendChild(menu);
}

function composerInput() { return document.querySelector(`#${PANEL_ID} textarea[data-composer]`); }

function uploadEndpoint() {
  return state.globalChatActive
    ? "/omniscience/global/upload"
    : `/omniscience/tasks/${encodeURIComponent(currentTaskId())}/upload`;
}

function workflowSourceFromContent(content) {
  const match = String(content || "").match(/(?:^|\s)@?(uploads\/[^\s@]+\.json)(?=$|\s)/i);
  return match ? match[1].replace(/\\/g, "/") : "";
}

async function uploadFilesToTask(files) {
  if (!composerHasScope() || !files.length) return;
  state.uploadBusy = true;
  renderFileMenu();
  const input = composerInput();
  try {
    for (const file of files) {
      const encoded = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || "").split(",").pop() || "");
        reader.onerror = () => reject(new Error(`读取 ${file.name} 失败。`));
        reader.readAsDataURL(file);
      });
      const result = await request(uploadEndpoint(), {
        method: "POST",
        body: JSON.stringify({ name: file.name, content_base64: encoded }),
      });
      if (input) {
        const mention = `@${result.path} `;
        input.value = `${input.value.replace(/\s+$/, "")} ${mention}`.trimStart();
        input.dispatchEvent(new Event("input"));
      }
      await loadFileList({ force: true });
    }
  } catch (error) {
    showError(error);
  } finally {
    state.uploadBusy = false;
    renderFileMenu();
    input?.focus();
  }
}

async function toggleNetworkAccess() {
  if (!composerHasScope() || state.networkBusy) return;
  const enabled = !globalNetworkEnabled();
  state.networkBusy = true;
  renderFileMenu();
  try {
    if (state.globalChatActive) {
      const result = await request("/omniscience/global/network", { method: "POST", body: JSON.stringify({ enabled }) });
      state.globalNetworkEnabled = !!result.network_enabled;
    } else {
      state.task = await request(`/omniscience/tasks/${encodeURIComponent(currentTaskId())}/network`, {
        method: "POST",
        body: JSON.stringify({ enabled }),
      });
    }
  } catch (error) {
    showError(error);
  } finally {
    state.networkBusy = false;
    renderFileMenu();
  }
}

async function expandFileMentions(content) {
  if (!composerHasScope()) return "";
  const paths = [...new Set([...String(content || "").matchAll(/(?:^|\s)@([^\s@]+)/g)]
    .map((match) => match[1])
    .filter((path) => !/^(?:workflow|node)$/i.test(path)))];
  const chunks = [];
  const scopeQuery = state.globalChatActive ? "scope=global" : `task_id=${encodeURIComponent(currentTaskId())}`;
  for (const path of paths) {
    try {
      const result = await request(`/omniscience/file?${scopeQuery}&path=${encodeURIComponent(path)}`);
      const body = String(result.content || "");
      const trimmed = body.length > 20000 ? `${body.slice(0, 20000)}\n…[内容过长已截断]` : body;
      chunks.push(`引用文件：${result.path}\n${trimmed}`);
    } catch (error) {
      chunks.push(`引用文件：${path}\n[无法读取该文件：${error.message}]`);
    }
  }
  return chunks.join("\n\n");
}

function renderPanel() {
  renderTasks();
  renderHeader();
  renderMessages();
  renderContext();
  renderSkillSelector();
  renderFileMenu();
}

async function loadTasks() {
  const result = await request("/omniscience/tasks");
  state.tasks = result.tasks || [];
  try {
    const external = await request("/omniscience/custom-nodes");
    state.projects = external.projects || [];
  } catch (error) {
    state.projects = [];
  }
  renderTasks();
}

async function loadGlobalChat() {
  const result = await request("/omniscience/global-chat");
  state.globalMessages = result.messages || [];
  state.globalNetworkEnabled = !!result.network_enabled;
  const lastMessage = state.globalMessages.at(-1);
  state.lastGlobalOutcome = lastMessage?.role === "assistant" && !lastMessage.tool_calls?.length
    ? "completed"
    : "";
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
    resetFileMenu();
    setView("chat");
    renderPanel();
  } catch (error) {
    showError(error);
  }
}

function applyEvents(events, { allowCanvasOpen = true } = {}) {
  for (const event of events) {
    state.cursor = Math.max(state.cursor, Number(event.seq ?? -1));
    const payload = event.payload || {};
    if (event.kind === "task_started") {
      state.busy = true;
      state.lastTaskOutcome = "running";
    }
    if (event.kind === "task_finished") {
      state.busy = false;
      state.lastTaskOutcome = "completed";
    }
    if (event.kind === "task_failed") {
      state.busy = false;
      state.lastTaskOutcome = "failed";
      state.error = payload.error || "任务执行失败。";
    }
    if (event.kind === "task_cancelled") {
      state.busy = false;
      state.lastTaskOutcome = "cancelled";
      state.error = "本次运行已被强制取消，可以继续提问。";
    }
    if (
      allowCanvasOpen
      && !state.taskLoading
      && event.kind === "tool_result"
      && payload.task_id === currentTaskId()
      && payload.tool === "open_workflow_on_canvas"
      && payload.result?.output?.action === "open_workflow_on_canvas"
    ) {
      void openTaskCanvasWorkflow(currentTaskId()).catch(showError);
    }
  }
}

async function loadTask(taskId, { resetSkills = true, openWorkflow = false } = {}) {
  if (state.taskLoading) return;
  state.taskLoading = true;
  state.globalChatActive = false;
  state.globalBusy = false;
  state.lastTaskOutcome = "";
  state.error = "";
  state.cursor = -1;
  state.context = [];
  state.contextStats = null;
  if (resetSkills) {
    state.selectedSkillIds = [];
    state.skillMenuOpen = false;
    state.skillQuery = "";
    state.skillMenuIndex = 0;
    resetFileMenu();
  }
  state.currentWorkflow = null;
  state.currentWorkflowSignature = "";
  state.currentWorkflowTaskId = "";
  let loaded = false;
  try {
    const result = await request(`/omniscience/tasks/${encodeURIComponent(taskId)}`);
    state.task = result.task;
    state.messages = result.messages || [];
    await loadSkills();
    if (openWorkflow) {
      const workflowResult = await request(`/omniscience/tasks/${encodeURIComponent(taskId)}/workflow`);
      await loadWorkflowToCanvas(taskCanvasWorkflow(workflowResult));
    }
    const liveWorkflow = await request(`/omniscience/tasks/${encodeURIComponent(taskId)}/current-workflow`);
    state.currentWorkflow = liveWorkflow.current || null;
    const events = await request(`/omniscience/tasks/${encodeURIComponent(taskId)}/events?after=-1`);
    applyEvents(events.events || [], { allowCanvasOpen: false });
    state.busy = result.task.status === "running";
    restoreCompletedTaskOutcome();
    loaded = true;
  } catch (error) {
    state.error = error.message;
  } finally {
    state.taskLoading = false;
  }
  try {
    await loadContextStats();
  } catch (error) {
    state.contextStats = null;
    console.warn("[Comfyui-omniscience] prompt length refresh failed", error);
  }
  renderPanel();
  if (loaded) await syncCurrentWorkflow({ silent: true });
  if (state.view === "settings") loadSettings().then(renderSettings).catch(showError);
  if (state.view === "checkpoints") renderCheckpoints().catch(showError);
  if (state.view === "skills") loadSkills().then(renderSkills).catch(showError);
}

async function openTaskCanvasWorkflow(taskId) {
  const workflowResult = await request(`/omniscience/tasks/${encodeURIComponent(taskId)}/workflow`);
  await loadWorkflowToCanvas(taskCanvasWorkflow(workflowResult));
  if (currentTaskId() === taskId) await syncCurrentWorkflow({ force: true, silent: false });
}

async function openTaskWorkflow(taskId) {
  state.error = "";
  renderTasks();
  await loadTask(taskId, { openWorkflow: true });
}

async function refreshTask() {
  if (!currentTaskId()) return;
  const result = await request(`/omniscience/tasks/${encodeURIComponent(currentTaskId())}`);
  if (result.task.id !== currentTaskId()) return;
  state.task = result.task;
  state.messages = result.messages || [];
  try { await loadSkills(); } catch (error) { console.warn("[Comfyui-omniscience] skill refresh failed", error); }
  state.busy = result.task.status === "running";
  restoreCompletedTaskOutcome();
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
  const result = await request("/omniscience/skills");
  state.skills = result.skills || [];
  const available = new Set(state.skills.filter((skill) => skill.enabled).map((skill) => skill.id));
  state.selectedSkillIds = state.selectedSkillIds.filter((id) => available.has(id));
  renderSkillSelector();
  return state.skills;
}

async function pollEvents() {
  await syncLocale();
  await pollWorkflowRepairCards();
  if (state.polling || state.taskLoading || state.globalChatActive || !currentTaskId()) return;
  state.polling = true;
  try {
    await syncCurrentWorkflow();
    const result = await request(`/omniscience/tasks/${encodeURIComponent(currentTaskId())}/events?after=${state.cursor}`);
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
    const task = await request("/omniscience/tasks", { method: "POST", body: JSON.stringify({ title: folderName, folder: folderName, mode: "act" }) });
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
    if (!confirm("确定清理全局对话吗？这不会影响任何任务的历史记录。")) return;
    try {
      await request("/omniscience/global-chat/history", { method: "DELETE" });
      state.globalMessages = [];
      state.lastGlobalOutcome = "";
      // Workflow candidate cards and AI repair progress are rendered from the
      // transcript, but their polling records live outside that array.  Drop
      // both projections as part of the same clear operation so an in-flight
      // refresh cannot leave stale cards/state behind in this panel.
      state.workflowRepairCards = [];
      state.workflowRepairGeneration += 1;
      state.workflowRepairPolling = false;
      state.context = [];
      state.fileList = [];
      state.fileListScope = "";
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
  if (!confirm(`确定清理“${folder}”的历史对话吗？\n项目文件夹和文件不会被删除。`)) return;
  try {
    await request(`/omniscience/tasks/${encodeURIComponent(taskId)}/history`, { method: "DELETE" });
    state.messages = [];
    state.cursor = -1;
    state.lastTaskOutcome = "";
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
    state.task = await request(`/omniscience/tasks/${encodeURIComponent(currentTaskId())}/mode`, { method: "POST", body: JSON.stringify({ mode }) });
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
      const mentioned = await expandFileMentions(content);
      input.value = "";
      state.composerDraft = "";
      state.skillMenuOpen = false;
      state.skillQuery = "";
      state.skillMenuIndex = 0;
      resetFileMenu();
      state.globalBusy = true;
      state.lastGlobalOutcome = "running";
      renderPanel();
      const result = await request("/omniscience/global-chat/messages", {
        method: "POST",
        body: JSON.stringify({
          content,
          skill_ids: [...state.selectedSkillIds],
          context: mentioned,
          workflow_source: workflowSourceFromContent(content),
          locale: state.locale,
        }),
      });
      state.globalMessages = result.messages || [...state.globalMessages, result.message].filter(Boolean);
      state.lastGlobalOutcome = "completed";
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
      const task = await request("/omniscience/tasks", { method: "POST", body: JSON.stringify({ title: content.slice(0, 48), folder: folderName, mode: "act" }) });
      await loadTasks();
      await loadTask(task.id, { resetSkills: false });
    }
    // Canvas synchronization is best effort for chat. An unavailable or very
    // large canvas must not prevent the user from asking the Agent a question.
    await syncCurrentWorkflow({ force: true, silent: true });
    const mentioned = await expandFileMentions(content);
    const context = [state.context.join("\n\n"), mentioned].filter(Boolean).join("\n\n");
    state.context = [];
    state.skillMenuOpen = false;
    state.skillQuery = "";
    state.skillMenuIndex = 0;
    resetFileMenu();
    input.value = "";
    state.composerDraft = "";
    state.busy = true;
    state.lastTaskOutcome = "running";
    renderPanel();
    // POST /messages only accepts the turn (202); progress arrives via pollEvents.
    await request(`/omniscience/tasks/${encodeURIComponent(currentTaskId())}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, context, skill_ids: [...state.selectedSkillIds], locale: state.locale }),
    });
    await refreshTask();
    await loadTasks();
  } catch (error) {
    state.error = error.message;
    state.busy = false;
    if (state.globalChatActive) state.lastGlobalOutcome = "failed";
    else state.lastTaskOutcome = "failed";
  } finally {
    if (state.globalChatActive) state.globalBusy = false;
    renderPanel();
  }
}

async function cancelTask() {
  const taskId = currentTaskId();
  if (!taskId || state.cancelling || !["running", "waiting_approval"].includes(state.task?.status)) return;
  if (!confirm("确定要取消当前运行吗？正在运行的命令会被终止，之后仍可在这个节点继续提问。")) return;
  state.cancelling = true;
  renderPanel();
  try {
    state.task = await request(`/omniscience/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
    state.busy = false;
    state.lastTaskOutcome = "cancelled";
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
  const views = { chat: ".omniscience-chat-view", checkpoints: ".omniscience-checkpoints-area", files: ".omniscience-files-area", workflow: ".omniscience-workflow-area", mcp: ".omniscience-mcp-area", settings: ".omniscience-settings-area", skills: ".omniscience-skills-area" };
  for (const [name, selector] of Object.entries(views)) {
    document.querySelector(`#${PANEL_ID} ${selector}`)?.classList.toggle("omniscience-hidden", name !== view);
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
    const result = await request(`/omniscience/file?task_id=${encodeURIComponent(currentTaskId())}&path=${encodeURIComponent(path)}`);
    state.context.push(`文件：${result.path}\n${result.content}`);
    renderContext();
  } catch (error) { alert(`读取文件失败：${error.message}`); }
}

async function renderFiles() {
  const area = document.querySelector(`#${PANEL_ID} .omniscience-files-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "项目文件"));
  if (!currentTaskId()) {
    area.appendChild(el("p", "omniscience-hint", "先创建或选择一个项目文件夹。"));
    return;
  }
  try {
    const result = await request(`/omniscience/files?task_id=${encodeURIComponent(currentTaskId())}&recursive=true`);
    area.appendChild(el("div", "omniscience-workspace-label", result.workspace));
    const list = el("div", "omniscience-file-list");
    for (const entry of result.entries || []) {
      if (entry.type !== "file") continue;
      const row = button(entry.path, "omniscience-file-row");
      row.title = "点击加入下一条消息的上下文";
      row.onclick = () => addFileContext(entry.path);
      list.appendChild(row);
    }
    area.appendChild(list);
  } catch (error) { area.appendChild(el("p", "omniscience-error", error.message)); }
}

async function renderWorkflow() {
  const area = document.querySelector(`#${PANEL_ID} .omniscience-workflow-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "ComfyUI 工作流"));
  if (!currentTaskId()) {
    area.appendChild(el("p", "omniscience-hint", "先创建或选择一个项目文件夹。"));
    return;
  }
  try {
    const result = await request(`/omniscience/tasks/${encodeURIComponent(currentTaskId())}/workflow`);
    state.currentWorkflow = result.current || null;
    area.appendChild(el("div", "omniscience-workspace-label", result.path));
    const liveTitle = el("h4", "omniscience-workflow-section-title", "当前页面实时画布（Agent 可读取）");
    const liveHint = el("p", "omniscience-hint", state.currentWorkflow
      ? `最近同步：${new Date(state.currentWorkflow.updated_at * 1000).toLocaleString()}。未保存的画布修改也会同步，不会覆盖 workflow.json。`
      : "尚未同步当前画布。点击“读取当前画布”或发送消息时会自动同步。");
    const liveEditor = document.createElement("textarea");
    liveEditor.className = "omniscience-json-editor omniscience-workflow-editor";
    liveEditor.readOnly = true;
    liveEditor.value = state.currentWorkflow ? JSON.stringify(state.currentWorkflow.workflow, null, 2) : "";
    const liveActions = el("div", "omniscience-inline-actions");
    const sync = button("读取当前画布", "omniscience-primary");
    const liveCopy = button("复制实时 JSON", "omniscience-secondary");
    const liveOutput = el("span", "omniscience-hint");
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

    area.appendChild(el("h4", "omniscience-workflow-section-title", "项目 workflow.json（不会被实时画布覆盖）"));
    area.appendChild(el("p", "omniscience-hint", "这是一个可导入 ComfyUI 的基础测试工作流。已有 workflow.json 不会被 Comfyui-omniscience 覆盖。"));
    const editor = document.createElement("textarea");
    editor.className = "omniscience-json-editor omniscience-workflow-editor";
    editor.readOnly = true;
    editor.value = result.content || "";
    const actions = el("div", "omniscience-inline-actions");
    const copy = button("复制 JSON", "omniscience-primary");
    const download = button("下载 workflow.json", "omniscience-secondary");
    const output = el("span", "omniscience-hint");
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
  } catch (error) { area.appendChild(el("p", "omniscience-error", error.message)); }
}

async function renderCheckpoints() {
  const area = document.querySelector(`#${PANEL_ID} .omniscience-checkpoints-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "检查点"));
  if (!currentTaskId()) {
    area.appendChild(el("p", "omniscience-hint", "先选择一个任务。"));
    return;
  }
  const result = await request(`/omniscience/tasks/${encodeURIComponent(currentTaskId())}/checkpoints`);
  if (!(result.checkpoints || []).length) {
    area.appendChild(el("p", "omniscience-hint", "工具完成后会自动创建检查点。"));
    return;
  }
  for (const checkpoint of result.checkpoints) {
    const card = el("article", "omniscience-checkpoint");
    card.append(el("strong", "", checkpoint.label || "检查点"), el("span", "omniscience-checkpoint-date", new Date(checkpoint.created_at * 1000).toLocaleString()));
    const actions = el("div", "omniscience-inline-actions");
    const diff = button("查看差异", "omniscience-secondary");
    const restore = button("恢复文件", "omniscience-secondary");
    const output = el("pre", "omniscience-action-details omniscience-hidden");
    diff.onclick = async () => {
      try {
        const value = await request(`/omniscience/checkpoints/${encodeURIComponent(checkpoint.id)}/diff`);
        output.textContent = value.diff || "（没有差异）";
        output.classList.toggle("omniscience-hidden");
      } catch (error) { output.textContent = error.message; }
    };
    restore.onclick = async () => {
      if (!confirm("恢复该检查点的文件？当前未保存的文件修改可能丢失。")) return;
      try {
        await request(`/omniscience/checkpoints/${encodeURIComponent(checkpoint.id)}/restore`, { method: "POST", body: JSON.stringify({ restore_files: true, restore_task: false }) });
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
  state.settings = await request("/omniscience/settings");
  await loadContextStats();
  return state.settings;
}

async function loadContextStats() {
  if (!currentTaskId()) {
    state.contextStats = null;
    return null;
  }
  const skillQuery = encodeURIComponent(JSON.stringify(state.selectedSkillIds));
  state.contextStats = await request(`/omniscience/tasks/${encodeURIComponent(currentTaskId())}/context?skill_ids=${skillQuery}&locale=${encodeURIComponent(state.locale)}`);
  return state.contextStats;
}

function contextSummaryText(stats) {
  if (!stats) return currentTaskId() ? "正在读取当前提示词长度…" : "选择或创建任务后，这里会显示当前提示词长度。";
  const status = stats.above_compaction_threshold
    ? "已超过自动压缩阈值。"
    : `达到 ${stats.compaction_threshold_percent}% 时自动压缩。`;
  return `当前提示词：约 ${stats.prompt_tokens.toLocaleString()} Token，${stats.prompt_chars.toLocaleString()} 字符，占上下文上限 ${stats.prompt_percent.toFixed(1)}%（消息 ${stats.message_tokens.toLocaleString()}，系统 ${stats.system_tokens.toLocaleString()}，工具 ${stats.tools_tokens.toLocaleString()}）。${status}`;
}

function providerOptions() {
  return (state.settings && state.settings.providers) || [];
}

function applyProviderDefaults(providerId) {
  const provider = providerOptions().find((entry) => entry.id === providerId);
  if (!provider) return;
  const baseInput = document.querySelector(`#${PANEL_ID} .omniscience-settings-area input[name="api_base_url"]`);
  const modelInput = document.querySelector(`#${PANEL_ID} .omniscience-settings-area input[name="model"]`);
  if (provider.api_base_url && baseInput) baseInput.value = provider.api_base_url;
  if (provider.models && provider.models.length && modelInput) modelInput.value = provider.models[0];
  renderModelSuggestions(providerId);
}

function renderModelSuggestions(providerId) {
  const datalist = document.querySelector(`#${PANEL_ID} .omniscience-settings-area datalist#omniscience-model-options`);
  if (!datalist) return;
  const provider = providerOptions().find((entry) => entry.id === providerId) || { models: [] };
  datalist.replaceChildren(...(provider.models || []).map((model) => {
    const option = document.createElement("option");
    option.value = model;
    return option;
  }));
}

function customProviderItems() {
  return providerOptions().filter((entry) => String(entry.id).startsWith("custom:"));
}

async function saveCustomProviders(list, statusEl) {
  try {
    state.settings = await request("/omniscience/settings", { method: "POST", body: JSON.stringify({ custom_providers: list }) });
    renderSettings();
  } catch (error) {
    if (statusEl) statusEl.textContent = `保存失败：${error.message}`;
    else showError(error);
  }
}

function renderSettings() {
  const area = document.querySelector(`#${PANEL_ID} .omniscience-settings-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "模型与工作区设置"));
  const settings = state.settings || {};
  const providerField = el("label", "omniscience-field");
  providerField.appendChild(el("span", "", "模型 Provider"));
  const providerSelect = document.createElement("select");
  providerSelect.name = "provider";
  for (const provider of providerOptions()) {
    const option = document.createElement("option");
    option.value = provider.id;
    option.textContent = t(provider.label);
    if (provider.id === settings.provider) option.selected = true;
    providerSelect.appendChild(option);
  }
  providerSelect.onchange = () => applyProviderDefaults(providerSelect.value);
  providerField.appendChild(providerSelect);
  area.appendChild(providerField);
  for (const [label, name, value] of [
    ["API 地址", "api_base_url", settings.api_base_url || "https://api.openai.com/v1"],
    ["模型", "model", settings.model || ""],
    ["最大上下文 Token 数", "max_context_tokens", settings.max_context_tokens || "32000"],
    ["自动压缩阈值（上下文百分比）", "compaction_threshold_percent", settings.compaction_threshold_percent || "80"],
    ["API Key（留空保持不变）", "api_key", ""],
    ["项目父目录", "workspace", settings.workspace || ""],
    ["ComfyUI 目录（可选）", "comfyui_directory", settings.comfyui_directory || ""],
    ["浏览器接入地址（auto=复用已打开的浏览器）", "browser_cdp_url", settings.browser_cdp_url || "auto"],
  ]) {
    const field = el("label", "omniscience-field");
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
    if (name === "model") input.setAttribute("list", "omniscience-model-options");
    field.appendChild(input);
    area.appendChild(field);
  }
  const modelDatalist = el("datalist", "");
  modelDatalist.id = "omniscience-model-options";
  area.appendChild(modelDatalist);
  renderModelSuggestions(settings.provider);

  const customHeading = el("h4", "omniscience-workflow-section-title", "自定义 Provider");
  const customList = el("div", "omniscience-custom-provider-list");
  const customs = customProviderItems();
  if (!customs.length) customList.appendChild(el("p", "omniscience-hint", "暂无自定义 Provider。"));
  for (const item of customs) {
    const row = el("div", "omniscience-custom-provider-row");
    row.append(el("strong", "", item.label), el("span", "omniscience-hint", ` ${item.api_base_url}`));
    const remove = button("删除", "omniscience-secondary");
    remove.onclick = () => {
      const next = customs
        .filter((entry) => entry.id !== item.id)
        .map((entry) => ({ name: entry.name, api_base_url: entry.api_base_url, models: entry.models }));
      saveCustomProviders(next, row);
    };
    row.appendChild(remove);
    customList.appendChild(row);
  }
  const addRow = el("div", "omniscience-custom-provider-row");
  const customName = document.createElement("input");
  customName.placeholder = "名称，例如 vLLM 内网";
  const customUrl = document.createElement("input");
  customUrl.placeholder = "https://host/v1";
  const customModels = document.createElement("input");
  customModels.placeholder = "模型（逗号分隔，可选）";
  const addCustom = button("添加", "omniscience-secondary");
  addCustom.onclick = async () => {
    const models = customModels.value.split(/[,，]/).map((model) => model.trim()).filter(Boolean);
    const existing = customs.map((entry) => ({ name: entry.name, api_base_url: entry.api_base_url, models: entry.models }));
    existing.push({ name: customName.value.trim(), api_base_url: customUrl.value.trim(), models });
    await saveCustomProviders(existing, addRow);
  };
  addRow.append(customName, customUrl, customModels, addCustom);
  area.append(customHeading, customList, addRow);
  const workflowNetworkHeading = el("h4", "omniscience-workflow-section-title", "工作流搜索站点");
  area.appendChild(workflowNetworkHeading);
  area.appendChild(el("p", "omniscience-hint", "RunningHub 连接固定为官方国内站和国外站，不支持在设置中修改地址；这里只选择使用哪个站点。"));
  area.appendChild(el("p", "omniscience-settings-readonly", "国内站：https://www.runninghub.cn"));
  area.appendChild(el("p", "omniscience-settings-readonly", "国外站：https://www.runninghub.ai"));
  const workflowNetworkFields = [
    ["工作流搜索区域", "network_region", settings.network_region || "domestic"],
    ["Hugging Face 国内模型镜像地址", "huggingface_domestic_base_url", settings.huggingface_domestic_base_url || "https://hf-mirror.com"],
    ["Hugging Face 国外站地址", "huggingface_international_base_url", settings.huggingface_international_base_url || "https://huggingface.co"],
    ["公网 IP 查询地址（自动区域判断，可选）", "public_ip_url", settings.public_ip_url || ""],
    ["IP 地理服务地址（URL 中可用 {ip}，可选）", "ip_geolocation_url", settings.ip_geolocation_url || ""],
  ];
  for (const [label, name, value] of workflowNetworkFields) {
    const field = el("label", "omniscience-field");
    field.appendChild(el("span", "", label));
    const input = document.createElement("input");
    input.name = name;
    input.value = value;
    if (name === "network_region") {
      const select = document.createElement("select");
      select.name = name;
      for (const [region, regionLabel] of [["domestic", "国内站"], ["international", "国外站"], ["auto", "自动判断本机公网 IP"]]) {
        const option = document.createElement("option");
        option.value = region;
        option.textContent = regionLabel;
        if (region === value) option.selected = true;
        select.appendChild(option);
      }
      field.replaceChildren(field.firstChild, select);
    } else {
      field.appendChild(input);
    }
    area.appendChild(field);
  }
  area.appendChild(el("p", "omniscience-hint", "默认使用国内站。聊天中说“国内/国外”也可临时覆盖当前搜索请求。"));

  const securityHeading = el("h4", "omniscience-workflow-section-title", "安全检测（Skill 与节点项目）");
  area.appendChild(securityHeading);
  const securityModeField = el("label", "omniscience-field");
  securityModeField.appendChild(el("span", "", "检测方式"));
  const securityMode = document.createElement("select");
  securityMode.name = "skill_security_mode";
  for (const [value, label] of [
    ["static", "静态规则检测（不调用模型）"],
    ["ai", "AI 模型审核（每次保存/安装调用一次模型）"],
    ["hybrid", "静态 + AI（结论取更严格）"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    if (value === (settings.skill_security_mode || "static")) option.selected = true;
    securityMode.appendChild(option);
  }
  securityModeField.appendChild(securityMode);
  area.appendChild(securityModeField);
  const reviewModelField = el("label", "omniscience-field");
  reviewModelField.appendChild(el("span", "", "AI 审核模型（留空使用上方模型）"));
  const reviewModel = document.createElement("input");
  reviewModel.name = "skill_security_model";
  reviewModel.value = settings.skill_security_model || "";
  reviewModel.setAttribute("list", "omniscience-model-options");
  reviewModelField.appendChild(reviewModel);
  area.appendChild(reviewModelField);
  area.appendChild(el("p", "omniscience-hint", "同一个开关同时用于 Skill 与 custom_nodes 项目检测：AI 审核把内容当作不可信数据交给模型判断风险，不会执行其中的指令；模型不可用时自动回退静态检测。AI/静态+AI 每次保存、接管或单项目检测会产生一次模型调用，「全部检测（不消耗模型）」始终只用静态规则。改完记得点击页面底部的「保存设置」。"));

  area.appendChild(el("p", "omniscience-hint", "工作流搜索会使用这里的区域设置。选择国内站或国外站后，不需要依赖本机公网 IP 判断；选择自动时才会读取公网 IP 和地理服务地址。聊天中说“国内/国外”也只对当前搜索请求临时生效。"));

  const stats = state.contextStats;
  const contextLabel = el("p", stats?.above_compaction_threshold ? "omniscience-hint omniscience-error" : "omniscience-hint", contextSummaryText(stats));
  const hint = el("p", "omniscience-hint omniscience-settings-status", settings.api_key_configured
    ? "API Key 已配置，不会在这里显示。工作流缺失节点优先使用 ComfyUI-Manager 的精确映射和安装队列；没有映射时才允许手动输入 GitHub 地址。填写 ComfyUI 目录后，新建项目会自动链接到该目录的 custom_nodes。"
    : "尚未配置 API Key；本地 Ollama 等服务可留空。工作流缺失节点优先使用 ComfyUI-Manager 的精确映射和安装队列；没有映射时才允许手动输入 GitHub 地址。填写 ComfyUI 目录后，新建项目会自动链接到该目录的 custom_nodes。");
  const save = button("保存设置", "omniscience-primary");
  save.onclick = async () => {
    save.disabled = true;
    try {
      const values = Object.fromEntries([...area.querySelectorAll("input")].map((input) => [input.name, input.value]));
      const provider = area.querySelector("select[name=provider]");
      if (provider) values.provider = provider.value;
      const region = area.querySelector("select[name=network_region]");
      if (region) values.network_region = region.value;
      const securityMode = area.querySelector("select[name=skill_security_mode]");
      if (securityMode) values.skill_security_mode = securityMode.value;
      state.settings = await request("/omniscience/settings", { method: "POST", body: JSON.stringify(values) });
      await loadContextStats();
      renderSettings();
      area.querySelector(".omniscience-settings-status").textContent = "已保存。";
    } catch (error) { hint.textContent = `保存失败：${error.message}`; }
    finally { save.disabled = false; }
  };
  area.append(contextLabel, hint, save);
  appendDonationSection(area);
}

function appendDonationSection(area) {
  const section = el("section", "omniscience-donation");
  section.appendChild(el("h4", "omniscience-workflow-section-title", "支持项目"));
  section.appendChild(el("p", "omniscience-hint", "如果这个项目对你有帮助，可以自愿打赏支持后续开发。打赏不影响任何功能。"));
  const options = el("div", "omniscience-donation-options");
  section.appendChild(options);
  const regionSelect = area.querySelector("select[name=network_region]");
  const render = () => {
    options.replaceChildren();
    const region = regionSelect?.value || "domestic";
    if (region !== "international") {
      const alipay = el("div", "omniscience-donation-item");
      alipay.appendChild(el("strong", "", "国内用户 · 支付宝"));
      const image = document.createElement("img");
      image.className = "omniscience-donation-qr";
      image.src = new URL("./assets/donation/alipay-qr.jpg", import.meta.url).href;
      image.alt = "支付宝打赏收款码";
      image.loading = "lazy";
      alipay.appendChild(image);
      options.appendChild(alipay);
    }
    if (region !== "domestic") {
      const usdt = el("div", "omniscience-donation-item");
      usdt.appendChild(el("strong", "", "海外用户 · USDT"));
      usdt.appendChild(el("p", "omniscience-hint", "BNB Smart Chain (BEP-20)"));
      usdt.appendChild(el("code", "omniscience-donation-address", "0x31babbb634bb46df0298d0edb93c1f4eaac73f85"));
      usdt.appendChild(el("p", "omniscience-hint", "转账前请核对网络与地址，加密货币转账不可撤销。"));
      options.appendChild(usdt);
    }
  };
  regionSelect?.addEventListener("change", render);
  render();
  area.appendChild(section);
}

function skillEditorField(label, name, value, multiline = false) {
  const field = el("label", "omniscience-field");
  field.appendChild(el("span", "", label));
  const input = multiline ? document.createElement("textarea") : document.createElement("input");
  input.name = name;
  input.value = value || "";
  if (multiline) input.className = "omniscience-skill-editor";
  field.appendChild(input);
  return field;
}

function resetSkillEditor() {
  state.editingSkillId = "";
  renderSkills();
}

const SECURITY_LABELS = { clean: "安全", review: "需复核", blocked: "已阻止" };
const SEVERITY_LABELS = { critical: "严重", high: "高危", medium: "中危", low: "提示" };

function securityEngineText(security) {
  const engine = String(security?.engine || "");
  const model = security?.ai_model || security?.model || "";
  if (engine.includes("-review-ai") && engine.includes("-security")) return `检测方式：静态规则 + AI 模型${model ? `（${model}）` : ""}`;
  if (engine.includes("-review-ai")) return `检测方式：AI 模型${model ? `（${model}）` : ""}`;
  if (engine.includes("-security")) return "检测方式：静态规则";
  return "";
}

function securityVerdict(security) {
  return SECURITY_LABELS[security?.verdict] || "未检测";
}

function securityReport(security) {
  const box = el("div", "omniscience-security-report");
  const engine = securityEngineText(security);
  if (engine) box.appendChild(el("p", "omniscience-hint", engine));
  if (security?.fallback) box.appendChild(el("p", "omniscience-hint omniscience-error", security.fallback));
  if (security?.files_scanned !== undefined) {
    box.appendChild(el("p", "omniscience-hint", `覆盖：${security.files_scanned}/${security.files_total || 0} 个文件${security.truncated ? "（已截断）" : ""}`));
  }
  const domains = security?.domains || [];
  const findings = security?.findings || [];
  for (const finding of findings) {
    const item = el("div", "omniscience-security-finding");
    item.appendChild(el("span", `omniscience-severity ${finding.severity}`, SEVERITY_LABELS[finding.severity] || finding.severity));
    item.appendChild(el("strong", "", finding.title || finding.rule));
    if (finding.advice) item.appendChild(el("p", "", finding.advice));
    for (const evidence of finding.evidence || []) item.appendChild(el("code", "", evidence));
    box.appendChild(item);
  }
  if (domains.length) box.appendChild(el("p", "omniscience-hint", `外部域名：${domains.join("、")}`));
  if (!findings.length && !domains.length) box.appendChild(el("p", "omniscience-hint", "未发现风险项。"));
  return box;
}

function skillSecurityControl(security) {
  const wrap = el("span", "omniscience-security");
  const badge = el("span", `omniscience-security-badge ${security?.verdict || "unknown"}`, securityVerdict(security));
  wrap.appendChild(badge);
  if (!security?.verdict || security.verdict === "clean") return wrap;
  const details = securityReport(security);
  details.hidden = true;
  badge.title = "点击查看安全检测详情";
  badge.onclick = (event) => {
    event.stopPropagation();
    details.hidden = !details.hidden;
  };
  wrap.appendChild(details);
  return wrap;
}

function securityConfirmation(error, action = "保存") {
  const security = error?.payload?.security || {};
  const findings = (security.findings || []).filter((item) => item.severity !== "low");
  const list = findings.length ? findings.map((item) => `· ${item.title}`).join("\n") : error.message;
  const engine = securityEngineText(security);
  return confirm(`安全检测阻止${action}：\n${list}\n\n${engine ? `${engine}\n` : ""}确认来源可信并要求仍然${action}？`);
}

function securityStatusText(security) {
  const risky = (security?.findings || []).filter((item) => item.severity !== "low").length;
  return `检测完成：${securityVerdict(security)}${risky ? `，${risky} 项需要关注` : ""}。`;
}

async function rescanSkills(ids) {
  if (!ids.length) {
    const result = await request("/omniscience/skills/rescan", { method: "POST" });
    return result.skills || [];
  }
  const refreshed = [];
  for (const id of ids) {
    const result = await request(`/omniscience/skills/${encodeURIComponent(id)}/rescan`, { method: "POST" });
    refreshed.push(result.skill);
  }
  return refreshed;
}

function renderSkills() {
  const area = document.querySelector(`#${PANEL_ID} .omniscience-skills-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "Skill 管理"));
  area.appendChild(el("p", "omniscience-hint", "Skill 是全局可复用的提示词指令。启用后，在对话输入框键入 / 即可搜索并选择；选中的 Skill 会用于后续消息，不绑定任何任务。"));

  const editing = state.skills.find((skill) => skill.id === state.editingSkillId) || null;
  const editor = el("div", "omniscience-skill-form");
  editor.appendChild(el("h4", "omniscience-workflow-section-title", editing ? "编辑 Skill" : "新建 Skill"));
  editor.append(
    skillEditorField("名称", "name", editing?.name || ""),
    skillEditorField("描述（可选）", "description", editing?.description || ""),
    skillEditorField("提示词内容", "content", editing?.content || "", true),
  );
  const enabledField = el("label", "omniscience-skill-check");
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.name = "enabled";
  enabled.checked = editing ? !!editing.enabled : true;
  enabledField.append(enabled, el("span", "", "启用此 Skill"));
  editor.appendChild(enabledField);
  const editorActions = el("div", "omniscience-inline-actions");
  const save = button(editing ? "保存修改" : "创建 Skill", "omniscience-primary");
  const scanDraft = button("立即检测", "omniscience-secondary");
  const cancel = button("清空", "omniscience-secondary");
  const output = el("span", "omniscience-hint omniscience-skill-status");
  const values = () => Object.fromEntries([...editor.querySelectorAll("input[name], textarea[name]")].map((input) => [input.name, input.type === "checkbox" ? input.checked : input.value]));
  const send = async (payload) => editing
    ? request(`/omniscience/skills/${encodeURIComponent(editing.id)}`, { method: "PATCH", body: JSON.stringify(payload) })
    : request("/omniscience/skills", { method: "POST", body: JSON.stringify(payload) });
  scanDraft.onclick = async () => {
    scanDraft.disabled = true;
    output.textContent = "正在检测…";
    try {
      const result = await request("/omniscience/skills/scan", { method: "POST", body: JSON.stringify(values()) });
      output.replaceChildren(el("span", "", securityStatusText(result.security)), securityReport(result.security));
    } catch (error) {
      output.textContent = `检测失败：${error.message}`;
    } finally { scanDraft.disabled = false; }
  };
  save.onclick = async () => {
    save.disabled = true;
    try {
      await send(values());
      output.textContent = editing ? "已保存。" : "已创建。";
    } catch (error) {
      if (error.status !== 409) {
        output.textContent = `保存失败：${error.message}`;
        save.disabled = false;
        return;
      }
      if (!securityConfirmation(error)) {
        output.replaceChildren(el("span", "", "已取消保存。"), securityReport(error.payload?.security));
        save.disabled = false;
        return;
      }
      try {
        await send({ ...values(), acknowledge_security: true });
        output.textContent = "已保存，并标记为你人工确认可信。";
      } catch (retryError) {
        output.textContent = `保存失败：${retryError.message}`;
        save.disabled = false;
        return;
      }
    }
    await loadSkills();
    await loadContextStats().catch(() => {});
    state.editingSkillId = "";
    renderSkills();
    renderPanel();
  };
  cancel.onclick = resetSkillEditor;
  editorActions.append(save, scanDraft, cancel, output);
  editor.appendChild(editorActions);
  area.appendChild(editor);

  const actions = el("div", "omniscience-inline-actions omniscience-skill-toolbar");
  const importButton = button("导入 JSON", "omniscience-secondary");
  const exportButton = button("导出 JSON", "omniscience-secondary");
  const importInput = document.createElement("input");
  importInput.type = "file";
  importInput.accept = "application/json,.json";
  importInput.hidden = true;
  importInput.onchange = async () => {
    const file = importInput.files?.[0];
    if (!file) return;
    let imported = null;
    try {
      imported = JSON.parse(await file.text());
      await request("/omniscience/skills/import", { method: "POST", body: JSON.stringify(imported) });
    } catch (error) {
      const blocked = String(error.message).includes("安全检测阻止导入");
      if (!blocked || !confirm(`${error.message}\n\n确认全部来源可信并要求仍然导入？`)) {
        alert(`导入失败：${error.message}`);
        importInput.value = "";
        return;
      }
      try {
        await request("/omniscience/skills/import", { method: "POST", body: JSON.stringify({ ...imported, acknowledge_security: true }) });
      } catch (retryError) { alert(`导入失败：${retryError.message}`); }
    }
    await loadSkills();
    await loadContextStats().catch(() => {});
    renderSkills();
    renderPanel();
    importInput.value = "";
  };
  importButton.onclick = () => importInput.click();
  const scanAll = button("全部安全检测", "omniscience-secondary");
  const scanStatus = el("span", "omniscience-hint omniscience-scan-status");
  const showScanStatus = (message) => {
    const live = document.querySelector(`#${PANEL_ID} .omniscience-scan-status`);
    if (live) live.textContent = message;
    else scanStatus.textContent = message;
  };
  scanAll.onclick = async () => {
    scanAll.disabled = true;
    showScanStatus(state.skills.length ? "正在检测…" : "没有可检测的 Skill。");
    try {
      await rescanSkills(state.skills.map((skill) => skill.id));
      await loadSkills();
      renderSkills();
      renderPanel();
      const flagged = state.skills.filter((skill) => skill.security?.verdict && skill.security.verdict !== "clean");
      showScanStatus(flagged.length
        ? `已检测 ${state.skills.length} 个 Skill，${flagged.length} 个需要复核：${flagged.map((skill) => skill.name).join("、")}。`
        : `已检测 ${state.skills.length} 个 Skill，全部未发现风险。`);
    } catch (error) { showScanStatus(`检测失败：${error.message}`); }
    finally { scanAll.disabled = false; }
  };
  exportButton.onclick = () => {
    const blob = new Blob([JSON.stringify({ version: 1, skills: state.skills.map(({ name, description, content, enabled }) => ({ name, description, content, enabled })) }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "comfyui-omniscience-skills.json";
    link.click();
    URL.revokeObjectURL(url);
  };
  actions.append(importButton, exportButton, scanAll, scanStatus, importInput);
  area.appendChild(actions);

  const list = el("div", "omniscience-skill-list");
  if (!state.skills.length) {
    list.appendChild(el("p", "omniscience-hint", "还没有 Skill。"));
  }
  for (const skill of state.skills) {
    const card = el("article", `omniscience-skill-card${skill.enabled ? "" : " disabled"}`);
    const heading = el("div", "omniscience-skill-card-heading");
    heading.append(el("strong", "", skill.name), el("span", "omniscience-skill-state", skill.enabled ? "已启用" : "已停用"), skillSecurityControl(skill.security));
    const description = el("p", "omniscience-hint", skill.description || "无描述");
    const preview = el("pre", "omniscience-action-details omniscience-skill-preview", skill.content);
    const cardActions = el("div", "omniscience-inline-actions");
    const toggle = button(skill.enabled ? "停用" : "启用", "omniscience-secondary");
    const edit = button("编辑", "omniscience-secondary");
    const scan = button("安全检测", "omniscience-secondary");
    const remove = button("删除", "omniscience-secondary");
    scan.onclick = async () => {
      scan.disabled = true;
      showScanStatus(`正在检测“${skill.name}”…`);
      try {
        await rescanSkills([skill.id]);
        await loadSkills();
        renderSkills();
        renderPanel();
        showScanStatus(`“${skill.name}”${securityStatusText(state.skills.find((item) => item.id === skill.id)?.security)}`);
      } catch (error) { showScanStatus(`检测失败：${error.message}`); }
      finally { scan.disabled = false; }
    };
    toggle.onclick = async () => {
      try { await request(`/omniscience/skills/${encodeURIComponent(skill.id)}`, { method: "PATCH", body: JSON.stringify({ enabled: !skill.enabled }) }); await loadSkills(); await loadContextStats().catch(() => {}); renderSkills(); renderPanel(); }
      catch (error) { alert(`更新失败：${error.message}`); }
    };
    edit.onclick = () => { state.editingSkillId = skill.id; renderSkills(); };
    remove.onclick = async () => {
       if (!confirm(`确定删除全局 Skill“${skill.name}”？删除后，历史消息仍保留但不会再注入提示词。`)) return;
       try { await request(`/omniscience/skills/${encodeURIComponent(skill.id)}`, { method: "DELETE" }); await loadSkills(); await loadContextStats().catch(() => {}); renderSkills(); renderPanel(); }
      catch (error) { alert(`删除失败：${error.message}`); }
    };
    cardActions.append(toggle, edit, scan, remove);
    card.append(heading, description, preview, cardActions);
    list.appendChild(card);
  }
  area.appendChild(list);
}

async function loadMcp() {
  const result = await request("/omniscience/mcp");
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
  const area = document.querySelector(`#${PANEL_ID} .omniscience-mcp-area`);
  if (!area) return;
  area.replaceChildren(el("h3", "", "MCP 工具"), el("p", "omniscience-hint", "stdio 命令和 HTTP URL 会在 ComfyUI 服务端执行，请只配置可信服务器。"));
  const editor = document.createElement("textarea");
  editor.className = "omniscience-json-editor";
  editor.value = JSON.stringify(mcpConfig(), null, 2);
  const actions = el("div", "omniscience-inline-actions");
  const save = button("保存 MCP 配置", "omniscience-primary");
  const discover = button("发现工具", "omniscience-secondary");
  const output = el("pre", "omniscience-action-details");
  save.onclick = async () => {
    try {
      const result = await request("/omniscience/mcp", { method: "POST", body: JSON.stringify(JSON.parse(editor.value)) });
      state.mcp = result.servers || [];
      editor.value = JSON.stringify(mcpConfig(), null, 2);
      output.textContent = "已保存。";
    } catch (error) { output.textContent = `保存失败：${error.message}`; }
  };
  discover.onclick = async () => {
    discover.disabled = true;
    try { output.textContent = JSON.stringify((await request("/omniscience/mcp/tools")).groups || [], null, 2); }
    catch (error) { output.textContent = `发现失败：${error.message}`; }
    finally { discover.disabled = false; }
  };
  actions.append(save, discover);
  area.append(editor, actions, output);
}

function buildPanel() {
  if (document.getElementById(PANEL_ID)) return;
  const panel = el("aside", "omniscience-panel");
  panel.id = PANEL_ID;
  const header = el("header", "omniscience-header");
  const heading = el("div", "omniscience-header-title");
  heading.append(
    el("strong", "", "Comfyui-omniscience"),
    el("span", "omniscience-current-task", "未选择任务"),
    el("span", "omniscience-prompt-stats", "提示词：—"),
  );
  const globalChat = button("全局对话", "omniscience-secondary omniscience-global-chat-toggle");
  globalChat.onclick = toggleGlobalChat;
  const close = button("×", "omniscience-close");
  close.onclick = () => setPanelOpen(false);
  const headerActions = el("div", "omniscience-header-actions");
  headerActions.append(globalChat, close);
  header.append(heading, headerActions);

  const layout = el("div", "omniscience-task-layout");
  const sidebar = el("aside", "omniscience-task-sidebar");
  const create = button("＋ 新任务", "omniscience-primary omniscience-new-task");
  create.onclick = createTask;
  const scanNodes = button("全部检测（不消耗模型）", "omniscience-secondary omniscience-scan-nodes");
  scanNodes.title = "用静态规则扫描 custom_nodes 下的全部项目，不调用模型";
  scanNodes.onclick = () => scanAllProjects(scanNodes);
  const nodeScanStatus = el("p", "omniscience-node-scan-status omniscience-hint");
  sidebar.append(
    create,
    el("h3", "", "任务节点"),
    el("div", "omniscience-task-list"),
    nodeInstallControls(),
    scanNodes,
    nodeScanStatus,
  );

  const main = el("main", "omniscience-main");
  const toolbar = el("div", "omniscience-toolbar");
  const modes = el("div", "omniscience-mode-controls");
  for (const mode of ["plan", "act"]) {
    const control = button(mode === "plan" ? "Plan" : "Act", "omniscience-mode");
    control.dataset.mode = mode;
    control.onclick = () => setMode(mode);
    modes.appendChild(control);
  }
  const status = el("span", "omniscience-task-status");
  const cancel = button("强制取消", "omniscience-secondary omniscience-cancel-task");
  const clearHistoryButton = button("清理历史对话", "omniscience-secondary omniscience-clear-history");
  cancel.hidden = true;
  cancel.onclick = cancelTask;
  clearHistoryButton.onclick = clearHistory;
  toolbar.append(modes, status, clearHistoryButton, cancel);
  const tabs = el("nav", "omniscience-tabs");
  for (const [view, label] of [["chat", "对话"], ["checkpoints", "检查点"], ["files", "文件"], ["workflow", "工作流"], ["mcp", "MCP"], ["skills", "Skill"], ["settings", "设置"]]) {
    const tab = button(label, view === "chat" ? "active" : "");
    tab.dataset.view = view;
    tab.onclick = () => setView(view);
    tabs.appendChild(tab);
  }
  const views = el("div", "omniscience-views");
  const chat = el("section", "omniscience-chat-view");
  const log = el("div", "omniscience-messages");
  const context = el("div", "omniscience-context-label", "点击文件可加入下一条消息的上下文");
  const skillSelector = el("div", "omniscience-skill-selector");
  const composer = el("div", "omniscience-composer");
  const textarea = document.createElement("textarea");
  textarea.dataset.composer = "true";
  textarea.value = state.composerDraft;
  textarea.placeholder = "描述你要完成的编程任务…";
  textarea.rows = 3;
  textarea.onkeydown = (event) => {
    if (state.toolMenuOpen) {
      const matches = matchingTools();
      if (event.key === "ArrowDown" && matches.length) {
        event.preventDefault();
        state.toolMenuIndex = (state.toolMenuIndex + 1) % matches.length;
        renderFileMenu();
        return;
      }
      if (event.key === "ArrowUp" && matches.length) {
        event.preventDefault();
        state.toolMenuIndex = (state.toolMenuIndex - 1 + matches.length) % matches.length;
        renderFileMenu();
        return;
      }
      if (event.key === "Enter" && !event.metaKey && !event.ctrlKey && matches.length) {
        event.preventDefault();
        if (!matches[state.toolMenuIndex]?.disabled) selectToolFromMenu(matches[state.toolMenuIndex]?.id || matches[0].id);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        resetToolMenu();
        renderFileMenu();
        return;
      }
    }
    if (state.fileMenuOpen) {
      const matches = matchingFiles();
      if (event.key === "ArrowDown" && matches.length) {
        event.preventDefault();
        state.fileMenuIndex = (state.fileMenuIndex + 1) % matches.length;
        renderFileMenu();
        return;
      }
      if (event.key === "ArrowUp" && matches.length) {
        event.preventDefault();
        state.fileMenuIndex = (state.fileMenuIndex - 1 + matches.length) % matches.length;
        renderFileMenu();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        resetFileMenu();
        renderFileMenu();
        return;
      }
      if (event.key === "Enter" && !event.metaKey && !event.ctrlKey && matches.length) {
        event.preventDefault();
        selectFileFromMenu(matches[state.fileMenuIndex]?.path || matches[0].path);
        return;
      }
    }
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
    state.composerDraft = textarea.value;
    const command = skillCommand(textarea.value);
    const tool = command ? null : toolCommand(textarea.value);
    const mention = command || tool ? null : fileCommand(textarea.value);
    state.skillMenuOpen = !!command;
    state.skillQuery = command?.query || "";
    const matches = matchingSkills();
    state.skillMenuIndex = Math.min(state.skillMenuIndex, Math.max(0, matches.length - 1));
    state.toolMenuOpen = !!tool;
    state.toolQuery = tool?.query || "";
    const toolMatches = matchingTools();
    state.toolMenuIndex = Math.min(state.toolMenuIndex, Math.max(0, toolMatches.length - 1));
    state.fileMenuOpen = !!mention;
    state.fileQuery = mention?.query || "";
    state.fileMenuIndex = 0;
    renderSkillSelector();
    if (mention) loadFileList().then(() => renderFileMenu()).catch(() => {});
    else renderFileMenu();
  };
  const send = button("发送", "omniscience-primary omniscience-send-message");
  send.onclick = sendMessage;
  const actions = el("div", "omniscience-composer-actions");
  const uploadButton = button("上传文件", "omniscience-secondary omniscience-upload-file");
  uploadButton.title = "上传文件到任务工作区的 uploads/ 目录";
  const filePicker = document.createElement("input");
  filePicker.type = "file";
  filePicker.multiple = true;
  filePicker.hidden = true;
  filePicker.onchange = () => {
    const files = [...filePicker.files];
    filePicker.value = "";
    uploadFilesToTask(files);
  };
  uploadButton.onclick = () => filePicker.click();
  const mentionButton = button("@", "omniscience-secondary omniscience-mention-file");
  mentionButton.title = "引用工作区文件到当前消息";
  mentionButton.onclick = () => {
    const value = textarea.value;
    const needsSpace = value && !/\s$/.test(value);
    const command = fileCommand(value);
    if (command) {
      textarea.value = `${value.slice(0, command.start)}@`;
    } else {
      textarea.value = `${value}${needsSpace ? " " : ""}@`;
    }
    state.fileMenuOpen = true;
    resetToolMenu();
    state.fileQuery = "";
    state.fileMenuIndex = 0;
    textarea.dispatchEvent(new Event("input"));
    textarea.focus();
  };
  const networkButton = button("网络访问：已关闭", "omniscience-secondary omniscience-network-toggle");
  networkButton.title = "开启后本任务允许浏览器工具和联网命令";
  networkButton.onclick = toggleNetworkAccess;
  actions.append(uploadButton, filePicker, mentionButton, networkButton);
  composer.append(textarea, actions, send);
  chat.append(log, context, skillSelector, composer);
  views.append(chat, el("section", "omniscience-checkpoints-area omniscience-hidden"), el("section", "omniscience-files-area omniscience-hidden"), el("section", "omniscience-workflow-area omniscience-hidden"), el("section", "omniscience-mcp-area omniscience-hidden"), el("section", "omniscience-settings-area omniscience-hidden"), el("section", "omniscience-skills-area omniscience-hidden"));
  main.append(toolbar, tabs, views);
  layout.append(sidebar, main);
  panel.append(header, layout);
  document.body.appendChild(panel);
  installTranslationObserver();
  localize(panel);
  renderPanel();
}

function setPanelOpen(open) {
  document.getElementById(PANEL_ID)?.classList.toggle("open", open);
  const launcher = document.getElementById(LAUNCHER_ID);
  if (!launcher) return;
  launcher.classList.toggle("active", open);
  launcher.classList.toggle("side-bar-button-selected", open);
  launcher.setAttribute("aria-expanded", String(open));
  launcher.setAttribute("aria-pressed", String(open));
}

function installLauncher() {
  if (document.getElementById(LAUNCHER_ID)) return;
  const launcher = button("", "omniscience-launcher side-bar-button");
  launcher.id = LAUNCHER_ID;
  launcher.setAttribute("aria-controls", PANEL_ID);
  launcher.setAttribute("aria-label", t("AI 助手"));
  launcher.title = t("AI 助手");
  launcher.setAttribute("aria-expanded", "false");
  launcher.setAttribute("aria-pressed", "false");
  const content = el("div", "side-bar-button-content flex flex-col items-center gap-2");
  const iconWrapper = el("span", "sidebar-icon-wrapper relative");
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("fill", "none");
  icon.setAttribute("stroke", "currentColor");
  icon.setAttribute("stroke-width", "1.8");
  icon.setAttribute("stroke-linecap", "round");
  icon.setAttribute("stroke-linejoin", "round");
  icon.setAttribute("aria-hidden", "true");
  icon.style.fill = "none";
  icon.style.stroke = "currentColor";
  icon.classList.add("side-bar-button-icon");
  for (const [tag, attributes] of [
    ["path", { d: "M12 8V5M8 5h8" }],
    ["rect", { x: "4", y: "8", width: "16", height: "13", rx: "3" }],
    ["path", { d: "M2 12v4M22 12v4M9 14h.01M15 14h.01M9 18h6" }],
  ]) {
    const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attributes)) shape.setAttribute(name, value);
    shape.style.fill = "none";
    shape.style.stroke = "currentColor";
    icon.append(shape);
  }
  iconWrapper.append(icon);
  const label = el("span", "side-bar-button-label text-center text-2xs leading-none", "全能");
  content.append(iconWrapper, label);
  launcher.append(content);
  launcher.onclick = () => {
    buildPanel();
    setPanelOpen(!document.getElementById(PANEL_ID).classList.contains("open"));
  };
  localize(launcher);

  const mount = () => {
    const target = document.querySelector('[data-testid="sidebar-top-group"]');
    if (!target) return false;
    target.append(launcher);
    return true;
  };
  if (!mount()) {
    document.body.appendChild(launcher);
    const observer = new MutationObserver(() => {
      if (mount()) observer.disconnect();
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }
}

app.registerExtension({
  name: "ComfyUI.Omniscience",
  async setup() {
    ensureStyle();
    state.locale = await resolveConfiguredLocale();
    installLocaleListener();
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