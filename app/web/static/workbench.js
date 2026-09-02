(() => {
  const state = {
    projects: [], sessions: [], project: null, session: null, mode: "chat",
    chatSessions: [],
    selectedModelAlias: null,
    catalog: { nodes: [], workflows: [] }, strategies: [], resources: { providers: [], models: [], health: [] },
    selectedWorkflowId: "default-task", selectedStrategy: "guided-develop", editingProjectId: "", editingWorkflow: null, testingProviders: new Set(),
    pendingMessage: null, streamMessage: null, sendSequence: 0,
    composerAttachments: [], selectedTools: null, toolMenuOpen: false, readingAttachments: false,
    // null = not probed yet for the current project; the pill stays quiet until
    // the readiness answer actually arrives.
    search: null, searchProjectId: "",
  };
  const THEME_KEY = "workloop-theme-minimal-v1";
  const CHAT_PROJECT_ID = "CHAT";
  const COMPOSER_TOOL_OPTIONS = ["zvec_grep_search", "zvec_grep_rg"];
  const MAX_COMPOSER_ATTACHMENTS = 5;
  const MAX_COMPOSER_ATTACHMENT_BYTES = 200000;
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  // Some providers prefix answers with blank lines.  Keep internal Markdown
  // spacing, but do not let boundary-only lines push the visible answer below
  // its avatar.  This also cleans responses saved before the backend fix.
  const cleanAssistantContent = value => String(value ?? "")
    .replace(/^(?:[ \t]*\r?\n)+/, "")
    .replace(/(?:\r?\n[ \t]*)+$/, "");
  const csv = value => String(value || "").split(",").map(item => item.trim()).filter(Boolean);
  const clone = value => JSON.parse(JSON.stringify(value));
  const MODEL_ALIAS_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
  const defaultModelAlias = modelName => {
    const leaf = String(modelName || "").trim().split("/").filter(Boolean).pop() || "";
    return leaf
      .replace(/[^A-Za-z0-9_.-]+/g, "-")
      .replace(/^[^A-Za-z0-9]+/, "")
      .replace(/[-_.]+$/, "")
      .replace(/-{2,}/g, "-");
  };
  const option = (value, label, selected = "") => `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`;
  const iconPaths = {
    "arrow-up": '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    chevron: '<path d="m9 18 6-6-6-6"/>',
    code: '<path d="m8 9-3 3 3 3"/><path d="m16 9 3 3-3 3"/><path d="m14 5-4 14"/>',
    contrast: '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18Z" fill="currentColor" stroke="none"/>',
    folder: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h5l2 2h8A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5Z"/>',
    "hard-drive": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 15h18"/><path d="M7 11h.01M11 11h.01"/>',
    maximize: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M16 3h3a2 2 0 0 1 2 2v3"/><path d="M8 21H5a2 2 0 0 1-2-2v-3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    message: '<path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/>',
    minus: '<path d="M5 12h14"/>',
    more: '<circle cx="5" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none"/>',
    panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    paperclip: '<path d="m21.4 11.1-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/>',
    pencil: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.9-4M4 4v5h5"/><path d="M4 13a8 8 0 0 0 14.9 4M20 20v-5h-5"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4.3-4.3"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
    sliders: '<path d="M4 6h6M14 6h6M4 12h10M18 12h2M4 18h2M10 18h10"/><circle cx="12" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="8" cy="18" r="2"/>',
    trash: '<path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5"/>',
    shield: '<path d="M12 3 20 6v5c0 5-3.4 8.7-8 10-4.6-1.3-8-5-8-10V6Z"/><path d="m8.5 12 2.2 2.2 4.8-5"/>',
    workflow: '<rect x="3" y="3" width="6" height="6" rx="1.5"/><rect x="15" y="15" width="6" height="6" rx="1.5"/><path d="M9 6h3a4 4 0 0 1 4 4v5M12 18H9a4 4 0 0 1-4-4V9"/>',
    x: '<path d="m6 6 12 12M18 6 6 18"/>',
  };
  const svgIcon = (name, className = "") => `<svg class="ui-icon${className ? ` ${className}` : ""}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${iconPaths[name] || iconPaths.more}</svg>`;
  window.workloopIcon = svgIcon;
  const api = async (path, options = {}) => {
    const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
    return payload;
  };
  const post = (path, value) => api(path, { method: "POST", body: JSON.stringify(value) });
  const remove = path => api(path, { method: "DELETE" });

  async function streamPost(path, value, onEvent) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(value),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `请求失败（${response.status}）`);
    }
    if (!response.body) throw new Error("浏览器不支持流式响应");
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let completed = null;

    const consume = async (flush = false) => {
      const blocks = buffer.split(/\r?\n\r?\n/);
      if (!flush) buffer = blocks.pop() || "";
      else buffer = "";
      for (const block of blocks) {
        if (!block.trim()) continue;
        let eventName = "message";
        const data = [];
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^\s/, ""));
        }
        if (!data.length) continue;
        let payload;
        try { payload = JSON.parse(data.join("\n")); }
        catch (_) { throw new Error("流式响应包含非法 JSON"); }
        if (eventName === "error") throw new Error(payload.error || "模型流式调用失败");
        if (eventName === "done") completed = payload;
        if (typeof onEvent === "function") onEvent(eventName, payload);
      }
    };

    while (true) {
      const { value: chunk, done } = await reader.read();
      buffer += decoder.decode(chunk || new Uint8Array(), { stream: !done });
      await consume(done);
      if (done) break;
    }
    if (!completed) throw new Error("流式响应未返回完成事件");
    return completed;
  }

  const toast = message => { $("toast").textContent = message; $("toast").classList.add("show"); setTimeout(() => $("toast").classList.remove("show"), 2600); };
  let confirmResolver = null;

  function confirmAction({ title = "确认删除", message = "确定要删除此项吗？", confirmLabel = "删除" } = {}) {
    const dialog = $("confirmDialog");
    if (dialog.open || confirmResolver) return Promise.resolve(false);
    $("confirmDialogTitle").textContent = title;
    $("confirmDialogMessage").textContent = message;
    $("confirmDialogAccept").textContent = confirmLabel;
    dialog.returnValue = "";
    dialog.showModal();
    $("confirmDialogCancel").focus();
    return new Promise(resolve => { confirmResolver = resolve; });
  }

  $("confirmDialog").addEventListener("close", () => {
    const resolve = confirmResolver;
    confirmResolver = null;
    if (resolve) resolve($("confirmDialog").returnValue === "confirm");
  });
  window.workloopConfirm = confirmAction;

  function resizeComposer() {
    const input = $("messageInput");
    if (!input) return;
    input.style.height = "auto";
    if (input.value) input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
  }

  function syncSendButton() {
    const button = $("sendMessage");
    const pending = Boolean(state.pendingMessage);
    if (!button) return;
    button.disabled = pending;
    button.classList.toggle("is-pending", pending);
    button.setAttribute("aria-busy", String(pending));
    button.setAttribute("aria-label", pending ? "发送中" : "发送");
    button.title = pending ? "正在发送…" : "发送";
    const iconSlot = button.querySelector(".icon-slot");
    if (iconSlot) {
      const iconName = pending ? "stop" : "arrow-up";
      iconSlot.dataset.icon = iconName;
      iconSlot.innerHTML = svgIcon(iconName);
    }
    const attach = $("attachFiles");
    if (attach) attach.disabled = pending || state.readingAttachments;
    const toolToggle = $("toggleToolMenu");
    if (toolToggle) toolToggle.disabled = pending || state.readingAttachments;
  }

  function formatFileSize(bytes) {
    const size = Number(bytes) || 0;
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderComposerAttachments() {
    const container = $("composerAttachments");
    if (!container) return;
    if (!state.composerAttachments.length) {
      container.hidden = true;
      container.innerHTML = "";
      return;
    }
    container.hidden = false;
    container.innerHTML = state.composerAttachments.map((file, index) => `<span class="composer-attachment"><span class="attachment-file-icon">${svgIcon("paperclip")}</span><span class="attachment-file-copy"><strong>${esc(file.name)}</strong><small>${formatFileSize(file.size)}</small></span><button class="icon-button small" type="button" data-remove-attachment="${index}" aria-label="移除附件 ${esc(file.name)}" title="移除附件">${svgIcon("x")}</button></span>`).join("");
    container.querySelectorAll("[data-remove-attachment]").forEach(button => button.addEventListener("click", () => {
      state.composerAttachments.splice(Number(button.dataset.removeAttachment), 1);
      renderComposerAttachments();
      syncSendButton();
    }));
  }

  function projectDefaultTools() {
    const search = state.project?.runtime_policy?.local_search;
    if (!state.project || search?.enabled === false) return [];
    return Array.isArray(search?.tools) ? search.tools.filter(item => COMPOSER_TOOL_OPTIONS.includes(item)) : [...COMPOSER_TOOL_OPTIONS];
  }

  function renderToolMenu() {
    const menu = $("toolMenu");
    if (!menu) return;
    const hasWorkspace = Boolean(state.project?.workspace_path);
    const defaults = projectDefaultTools();
    const selected = state.selectedTools === null ? defaults : state.selectedTools;
    document.querySelectorAll("[data-composer-tool-choice]").forEach(input => {
      input.checked = selected.includes(input.value);
      input.disabled = !hasWorkspace;
    });
    const hint = $("toolMenuHint");
    if (hint) {
      if (!state.project) hint.textContent = "普通对话没有工作区，工具不可用";
      else if (!hasWorkspace) hint.textContent = "请先为项目连接工作区";
      else if (state.search && state.search.ready === false) hint.textContent = "本地检索尚未就绪，发送时会自动跳过";
      else if (state.selectedTools === null) hint.textContent = "使用项目默认工具";
      else hint.textContent = selected.length ? `本条消息启用 ${selected.length} 个工具` : "本条消息不启用工具";
    }
    const toggle = $("toggleToolMenu");
    if (toggle) toggle.classList.toggle("active", state.selectedTools !== null && selected.length > 0);
  }

  function setToolMenuOpen(open) {
    state.toolMenuOpen = Boolean(open);
    const menu = $("toolMenu");
    const toggle = $("toggleToolMenu");
    if (menu) menu.hidden = !state.toolMenuOpen;
    if (toggle) toggle.setAttribute("aria-expanded", String(state.toolMenuOpen));
    if (state.toolMenuOpen) renderToolMenu();
  }

  function isTextAttachment(file) {
    return String(file.type || "").startsWith("text/") || /\.(md|markdown|txt|json|ya?ml|csv|log|xml|html?|css|js|jsx|ts|tsx|py|java|go|rs|sql|sh|bat|ps1)$/i.test(file.name);
  }

  async function readComposerAttachments() {
    const payload = [];
    for (const file of state.composerAttachments) {
      let content = "";
      if (isTextAttachment(file) && file.size <= MAX_COMPOSER_ATTACHMENT_BYTES && typeof file.text === "function") {
        try { content = await file.text(); } catch (_) { content = ""; }
      }
      payload.push({ name: file.name, mime_type: file.type || "application/octet-stream", size: file.size || 0, content });
    }
    return payload;
  }

  function scrollMessageListToBottom() {
    const list = $("messageList");
    if (!list) return;
    const scroll = () => { list.scrollTop = list.scrollHeight; };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(scroll);
    else scroll();
  }

  function renderProjects() {
    const renderSessionRows = sessions => sessions.map(session => {
      const selected = state.session?.session_id === session.session_id;
      return `<div class="session-row ${selected ? "active" : ""}"><button type="button" class="session-item ${selected ? "active" : ""}" data-session="${esc(session.session_id)}"><span class="session-icon">${svgIcon(session.mode === "task" ? "workflow" : "message")}</span><strong>${esc(session.title)}</strong></button><button type="button" class="session-delete" data-delete-session="${esc(session.session_id)}" aria-label="删除会话 ${esc(session.title)}" title="删除会话">${svgIcon("trash")}</button></div>`;
    }).join("");
    const quickChat = `<div class="project-group quick-chat-group"><button type="button" class="project-item quick-chat-item ${!state.project ? "active" : ""}" data-chat-home><span class="project-icon">${svgIcon("message")}</span><span><strong>普通对话</strong><span>无需创建项目，直接开始聊天</span></span></button>${!state.project ? `<div class="session-list">${renderSessionRows(state.chatSessions)}<button type="button" class="session-item new" data-new-chat-session><span class="session-icon">${svgIcon("plus")}</span><strong>新对话</strong></button></div>` : ""}</div>`;
    const projectMarkup = state.projects.length ? state.projects.map(project => {
      const active = state.project?.project_id === project.project_id;
      const sessions = active ? state.sessions : [];
      return `<div class="project-group"><div class="project-row ${active ? "active" : ""}"><button type="button" class="project-item ${active ? "active" : ""}" data-project="${esc(project.project_id)}"><span class="project-icon">${svgIcon("folder")}</span><span><strong>${esc(project.name)}</strong><span>${active ? `${sessions.length} 个会话 · ${project.workspace_path ? "工作区已连接" : "未连接工作区"}` : "点击查看"}</span></span></button><span class="project-actions"><button type="button" class="project-action" data-edit-project-id="${esc(project.project_id)}" aria-label="编辑项目 ${esc(project.name)}" title="编辑项目">${svgIcon("pencil")}</button><button type="button" class="project-action danger" data-delete-project="${esc(project.project_id)}" aria-label="删除项目 ${esc(project.name)}" title="删除项目">${svgIcon("trash")}</button></span></div>${active ? `<div class="session-list">${renderSessionRows(sessions)}<button type="button" class="session-item new" data-new-session><span class="session-icon">${svgIcon("plus")}</span><strong>新会话</strong></button></div>` : ""}</div>`;
    }).join("") : `<div class="inspector-empty compact-empty project-empty"><strong>还没有项目</strong><p>项目用于工作区和任务模式，普通对话不受影响。</p></div>`;
    $("projectList").innerHTML = `${quickChat}${projectMarkup}`;
    document.querySelectorAll("[data-project]").forEach(button => button.addEventListener("click", () => selectProject(button.dataset.project)));
    document.querySelectorAll("[data-edit-project-id]").forEach(button => button.addEventListener("click", () => {
      const project = state.projects.find(item => item.project_id === button.dataset.editProjectId);
      if (project) openProjectDialog(project);
    }));
    document.querySelectorAll("[data-delete-project]").forEach(button => button.addEventListener("click", () => void deleteProject(button.dataset.deleteProject)));
    document.querySelectorAll("[data-session]").forEach(button => button.addEventListener("click", () => {
      const sessionId = button.dataset.session;
      state.selectedModelAlias = null;
      state.selectedTools = null;
      state.session = (state.project ? state.sessions : state.chatSessions).find(item => item.session_id === sessionId) || null;
      state.mode = state.session?.mode || "chat";
      if (state.session?.workflow_id) state.selectedWorkflowId = state.session.workflow_id;
      if (state.session?.policy?.strategy) state.selectedStrategy = state.session.policy.strategy;
      renderMode(); renderProjects(); renderSessions();
    }));
    document.querySelectorAll("[data-delete-session]").forEach(button => button.addEventListener("click", () => deleteSession(button.dataset.deleteSession)));
    document.querySelectorAll("[data-new-session]").forEach(button => button.addEventListener("click", async () => {
      try {
        state.session = await post(`/api/v2/projects/${encodeURIComponent(state.project.project_id)}/sessions`, {
          title: "新的会话", mode: state.mode,
          workflow_id: state.mode === "task" ? state.selectedWorkflowId : "",
          policy: state.mode === "task" ? { strategy: state.selectedStrategy } : undefined,
        });
        state.sessions.unshift(state.session); renderProjects(); renderSessions();
      } catch (error) { toast(error.message); }
    }));
    document.querySelectorAll("[data-chat-home]").forEach(button => button.addEventListener("click", () => {
      if (state.project) {
        state.project = null;
        state.sessions = [];
      }
      delete document.body.dataset.projectId;
      delete document.body.dataset.projectName;
      state.mode = "chat";
      state.selectedModelAlias = null;
      state.selectedTools = null;
      state.session = state.chatSessions[0] || null;
      renderMode(); renderProjects(); renderSessions();
      window.dispatchEvent(new CustomEvent("workloop:project-selected", {
        detail: { projectId: "", projectName: "" },
      }));
    }));
    document.querySelectorAll("[data-new-chat-session]").forEach(button => button.addEventListener("click", async () => {
      try {
        const session = await post("/api/v2/sessions", { title: "新的会话", mode: "chat" });
        state.chatSessions.unshift(session);
        state.session = session;
        state.mode = "chat";
        state.selectedTools = null;
        renderProjects(); renderSessions();
      } catch (error) { toast(error.message); }
    }));
  }

  async function deleteSession(sessionId) {
    const collection = state.project ? state.sessions : state.chatSessions;
    const session = collection.find(item => item.session_id === sessionId);
    if (!session) return;
    if (state.pendingMessage?.sessionId === sessionId) { toast("消息发送期间不能删除此会话"); return; }
    const confirmed = await confirmAction({
      title: "删除会话",
      message: `确定删除会话“${session.title}”吗？其中的消息和运行上下文将一并删除。`,
      confirmLabel: "删除会话",
    });
    if (!confirmed) return;
    try {
      await remove(`/api/v2/sessions/${encodeURIComponent(sessionId)}`);
      const removedIndex = collection.findIndex(item => item.session_id === sessionId);
      if (state.project) state.sessions = state.sessions.filter(item => item.session_id !== sessionId);
      else state.chatSessions = state.chatSessions.filter(item => item.session_id !== sessionId);
      if (state.session?.session_id === sessionId) {
        const nextCollection = state.project ? state.sessions : state.chatSessions;
        state.session = nextCollection[Math.min(Math.max(removedIndex, 0), nextCollection.length - 1)] || null;
        state.mode = state.session?.mode || "chat";
        if (state.session?.workflow_id) state.selectedWorkflowId = state.session.workflow_id;
        if (state.session?.policy?.strategy) state.selectedStrategy = state.session.policy.strategy;
      }
      renderMode(); renderProjects(); renderSessions(); toast("会话已删除");
    } catch (error) { toast(error.message); }
  }

  function renderWorkflowPicker() {
    const workflows = state.catalog.workflows || [];
    if (!workflows.some(item => item.workflow_id === state.selectedWorkflowId)) state.selectedWorkflowId = workflows[0]?.workflow_id || "";
    $("workflowSelect").innerHTML = workflows.map(item => option(item.workflow_id, item.label, state.selectedWorkflowId)).join("");
    $("workflowPicker").classList.toggle("hidden", state.mode !== "task");
    $("strategyPicker").classList.toggle("hidden", state.mode !== "task");
    $("strategySelect").innerHTML = state.strategies.map(item => option(item.strategy, item.label, state.selectedStrategy)).join("");
  }

  function openTaskWorkflowPicker() {
    const select = $("workflowSelect");
    if (!select || select.disabled || !select.options.length) return;
    // Chromium exposes showPicker() for native selects. Keep focus as a
    // graceful fallback for older WebView runtimes where it is unavailable.
    try {
      if (typeof select.showPicker === "function") select.showPicker();
      else select.focus();
    } catch (_) {
      select.focus();
    }
  }

  function setInspectorOpen(open) {
    $("shell").classList.toggle("inspector-hidden", !open);
    $("showInspector").classList.toggle("visible", !open);
    $("showInspector").setAttribute("aria-expanded", String(open));
    $("closeInspector").setAttribute("aria-expanded", String(open));
    $("inspector").setAttribute("aria-hidden", String(!open));
  }

  function renderMode() {
    document.querySelectorAll("[data-mode]").forEach(item => item.classList.toggle("active", item.dataset.mode === state.mode));
    $("composerHint").textContent = state.mode === "task" ? "按所选工作流执行，节点结果自动写入上下文" : "Enter 发送，Shift + Enter 换行";
    renderWorkflowPicker(); renderModelPicker(); renderFlow();
  }

  function renderModelPicker() {
    const picker = $("modelPicker");
    const select = $("modelSelect");
    if (!picker || !select) return;
    const models = state.resources.models.filter(item => item.enabled !== false);
    if (state.mode === "task") {
      select.innerHTML = option("", "任务按节点选择模型");
      select.disabled = true;
      select.title = "任务模式按工作流节点选择模型";
      picker.classList.add("is-task");
      return;
    }
    picker.classList.remove("is-task");
    const sessionModel = state.session?.context?.inputs?.model_alias;
    const preferred = state.selectedModelAlias ?? sessionModel ?? state.project?.default_model ?? models[0]?.alias ?? "";
    const selected = models.some(item => item.alias === preferred) ? preferred : "";
    if (state.selectedModelAlias !== null && state.selectedModelAlias !== selected) state.selectedModelAlias = selected;
    select.innerHTML = models.length
      ? `${option("", "自动选择模型", selected)}${models.map(item => option(item.alias, item.alias, selected)).join("")}`
      : option("", "尚未配置模型");
    select.value = selected;
    select.disabled = !models.length;
    select.title = models.length ? "切换当前对话使用的模型" : "请先在管理中心添加模型";
  }

  function activeContextId() {
    if (state.project?.project_id) return state.project.project_id;
    return state.mode === "chat" ? CHAT_PROJECT_ID : "";
  }

  function renderSessions() {
    const projectlessChat = !state.project && state.mode === "chat";
    $("projectName").textContent = state.project?.name || (projectlessChat ? "普通对话" : "选择一个项目");
    $("sessionTitle").textContent = state.session?.title || "新的会话";
    $("composerProjectContext").textContent = state.project?.name || (projectlessChat ? "普通对话" : "选择项目");
    const contextPill = $("composerProjectContext").closest(".context-pill");
    contextPill?.classList.toggle("projectless", projectlessChat);
    const contextIcon = contextPill?.querySelector(".icon-slot");
    if (contextIcon) {
      contextIcon.dataset.icon = projectlessChat ? "message" : "folder";
      contextIcon.innerHTML = svgIcon(projectlessChat ? "message" : "folder");
    }
    $("editProject").disabled = !state.project;
    $("editProject").title = state.project ? "项目设置" : "请先选择项目";
    $("editProject").setAttribute("aria-label", state.project ? "编辑当前项目" : "项目设置（请先选择项目）");
    renderModelPicker();
    renderSearchPill();
    const messages = [...(state.session?.messages || [])];
    const pending = state.pendingMessage;
    const pendingVisible = pending && pending.projectId === activeContextId() &&
      (!state.session || !pending.sessionId || pending.sessionId === state.session.session_id);
    if (pendingVisible) messages.push({ role: "user", content: pending.content, attachments: pending.attachments, pending: true });
    const streaming = state.streamMessage;
    const streamingVisible = streaming && streaming.projectId === activeContextId() &&
      (!state.session || !streaming.sessionId || streaming.sessionId === state.session.session_id ||
        (pending && pending.sessionId === streaming.sessionId));
    if (streamingVisible) messages.push({
      role: "assistant", content: streaming.content, streaming: true,
      stream_status: streaming.status,
      model: streaming.model || "",
      elapsed_ms: streaming.elapsedMs,
    });
    $("welcome").classList.toggle("hidden", messages.length > 0);
    let stack = $("messageStack");
    if (!stack) { stack = document.createElement("div"); stack.id = "messageStack"; stack.className = "message-stack"; $("messageList").appendChild(stack); }
    stack.innerHTML = messages.map(message => `<article class="message ${message.role === "user" ? "user" : "assistant"}${message.pending ? " pending" : ""}${message.streaming ? " streaming" : ""}"><span class="message-avatar">${message.role === "user" ? "你" : (message.node_id ? "N" : "W")}</span><div class="message-body">${esc(message.role === "user" ? message.content : cleanAssistantContent(message.content))}${messageAttachmentMarkup(message)}${messageMetaMarkup(message)}</div></article>`).join("");
    scrollMessageListToBottom();
    syncSendButton();
    renderToolMenu();
    renderFlow();
  }

  function renderSearchPill() {
    const pill = $("searchPill");
    const text = $("searchPillText");
    const project = state.project;
    if (!project || !state.search || state.searchProjectId !== project.project_id) {
      pill.hidden = true;
      return;
    }
    pill.hidden = false;
    const ready = state.search.ready === true;
    text.textContent = ready ? "检索就绪" : "检索未就绪";
    pill.style.opacity = ready ? "1" : "0.5";
  }

  function renderFlow() {
    const session = state.session;
    const isTask = state.mode === "task" || session?.mode === "task";
    if (!isTask || !session) {
      $("flowPanel").classList.add("hidden"); $("inspectorEmpty").classList.remove("hidden");
      $("sessionModeLabel").textContent = "普通对话"; return;
    }
    $("flowPanel").classList.remove("hidden"); $("inspectorEmpty").classList.add("hidden");
    $("sessionModeLabel").textContent = "任务模式";
    const workflowId = session.workflow_id || state.selectedWorkflowId || "default-task";
    const nodes = state.catalog.workflows.find(item => item.workflow_id === workflowId)?.nodes || [];
    const events = (session.messages || []).filter(item => item.node_id);
    const statuses = Object.fromEntries(events.map(item => [item.node_id, item.metadata?.node_run?.status || "completed"]));
    const done = nodes.filter(node => ["completed", "skipped"].includes(statuses[node.node_id])).length;
    const current = session.status === "running" ? nodes.find(node => !statuses[node.node_id]) : null;
    const labels = Object.fromEntries(state.catalog.nodes.map(node => [node.node_type, node.label]));
    $("flowCount").textContent = `${done} / ${nodes.length}`;
    $("flowStatusText").textContent = session.status === "completed" ? "已完成" : (session.status === "waiting_for_human" ? "等待处理" : "准备运行");
    $("flowStatus").className = `status-dot ${session.status === "completed" ? "done" : (session.status === "waiting_for_human" ? "failed" : (session.status === "running" ? "running" : "idle"))}`;
    const policy = session.policy || {};
    const strategy = state.strategies.find(item => item.strategy === policy.strategy);
    $("policyStrategy").textContent = strategy?.label || policy.strategy || "引导开发";
    $("policyComplexity").textContent = policy.complexity || "M";
    $("policyRisk").textContent = policy.risk || "medium";
    $("policyPhase").textContent = policy.current_phase || "analysis";
    $("policyGate").textContent = policy.gate_status === "blocked" ? `阻塞 · ${policy.gate || "需处理"}` : policy.gate_status === "approved" ? "已批准" : "开放";
    $("policyNext").querySelector("span").textContent = policy.next_action ? `下一步：${policy.next_action}` : "";
    $("approvePolicy").classList.toggle("hidden", policy.gate_status !== "blocked");
    $("replanPolicy").classList.toggle("hidden", policy.gate_status !== "blocked");
    const flowNodes = nodes.map((node, index) => {
      const status = statuses[node.node_id] || (current?.node_id === node.node_id ? "current" : "pending");
      const statusText = { pending: "等待", current: "执行中", skipped: "跳过", completed: "完成", failed: "失败" }[status] || status;
      const stateClass = ["completed", "skipped"].includes(status) ? "done" : status === "failed" ? "failed" : status === "current" ? "current" : "";
      return { node, index, status, statusText, stateClass, label: labels[node.node_type] || node.node_type };
    });
    $("flowList").innerHTML = flowNodes.map(item => `<div class="flow-node ${item.stateClass}" data-flow-node="${esc(item.node.node_id)}"><span class="node-marker">${item.status === "completed" ? svgIcon("check") : item.index + 1}</span><div class="node-copy"><strong>${esc(item.label)}</strong><span class="node-binding">${esc(item.node.model_alias || "自动选择模型")}</span><span>${esc(item.node.node_id)} · ${esc(item.statusText)}</span></div></div>`).join("");
    $("flowCanvas").innerHTML = flowNodes.map(item => `<div class="flow-canvas-node ${item.stateClass}" data-flow-node="${esc(item.node.node_id)}"><div class="flow-canvas-index">${item.status === "completed" ? svgIcon("check") : item.index + 1}</div><div class="flow-canvas-copy"><strong>${esc(item.label)}</strong><span>${esc(item.node.node_id)}</span><small>${esc(item.node.model_alias || "自动选择模型")} · ${esc(item.statusText)}</small></div><span class="flow-canvas-more" aria-hidden="true">${svgIcon("more")}</span></div>`).join("");
    $("flowFocusStatus").textContent = $("flowStatusText").textContent;
    $("flowFocusCount").textContent = $("flowCount").textContent;
    $("contextView").textContent = JSON.stringify(session.context || {}, null, 2);
  }

  function openFlowFocus() {
    if (!(state.mode === "task" || state.session?.mode === "task") || !state.session) {
      toast("任务模式运行后才可查看流程画布");
      return;
    }
    renderFlow();
    const dialog = $("flowDialog");
    if (!dialog.open) dialog.showModal();
  }

  async function probeSearch(projectId) {
    // Readiness is advisory: a missing or unindexed zvec-grep must never block
    // the chat, so a failed probe only clears the pill.
    try {
      const status = await api(`/api/v2/projects/${encodeURIComponent(projectId)}/search`);
      if (state.project?.project_id !== projectId) return;
      state.search = status;
      state.searchProjectId = projectId;
    } catch {
      if (state.project?.project_id !== projectId) return;
      state.search = null;
      state.searchProjectId = "";
    }
    renderSearchPill();
  }

  async function selectProject(projectId) {
    try {
      state.search = null;
      state.searchProjectId = "";
      state.selectedModelAlias = null;
      state.selectedTools = null;
      state.project = state.projects.find(project => project.project_id === projectId) || null;
      state.sessions = await api(`/api/v2/projects/${encodeURIComponent(projectId)}/sessions`);
      state.session = state.sessions[0] || null;
      if (!state.session) state.session = await post(`/api/v2/projects/${encodeURIComponent(projectId)}/sessions`, { title: "新的会话", mode: state.mode, workflow_id: state.mode === "task" ? state.selectedWorkflowId : "", policy: state.mode === "task" ? { strategy: state.selectedStrategy } : undefined });
      state.mode = state.session.mode || "chat";
      if (state.session.workflow_id) state.selectedWorkflowId = state.session.workflow_id;
      if (state.session.policy?.strategy) state.selectedStrategy = state.session.policy.strategy;
      renderMode(); renderProjects(); renderSessions(); $("rail").classList.remove("open");
      void probeSearch(projectId);
      document.body.dataset.projectId = state.project.project_id;
      document.body.dataset.projectName = state.project.name;
      window.dispatchEvent(new CustomEvent("workloop:project-selected", {
        detail: { projectId: state.project.project_id, projectName: state.project.name },
      }));
    } catch (error) { toast(error.message); }
  }

  async function send() {
    const input = $("messageInput");
    const content = input.value.trim();
    const hasAttachments = state.composerAttachments.length > 0;
    if (!content && !hasAttachments) return;
    if (state.mode === "task" && !state.project) {
      toast("任务模式需要选择项目；普通对话无需项目");
      return;
    }
    if (state.mode === "task" && hasAttachments) {
      toast("附件仅支持普通对话");
      return;
    }
    if (state.pendingMessage) return;
    if (state.readingAttachments) return;
    state.readingAttachments = true;
    syncSendButton();
    let attachments;
    try {
      attachments = await readComposerAttachments();
    } catch (error) {
      state.readingAttachments = false;
      syncSendButton();
      toast(error.message || "读取附件失败");
      return;
    }
    state.readingAttachments = false;
    const requestContent = content || "请阅读附件并回答。";
    const pending = {
      id: ++state.sendSequence,
      projectId: activeContextId(),
      sessionId: state.session?.session_id || "",
      mode: state.mode,
      workflowId: state.mode === "task" ? state.selectedWorkflowId : "",
      modelAlias: state.mode === "chat" ? (state.selectedModelAlias ?? "") : "",
      tools: state.mode === "chat" && state.selectedTools !== null ? [...state.selectedTools] : null,
      attachments,
      draftContent: content,
      content: requestContent,
    };
    state.pendingMessage = pending;
    // Clear and resize before the network/model call so a slow provider never
    // makes the composer look stuck or traps the user's draft in the field.
    input.value = "";
    resizeComposer();
    syncSendButton();
    renderProjects(); renderSessions();
    void submitMessage(pending);
  }

  async function submitMessage(pending) {
    try {
      const workflowId = pending.workflowId;
      const matches = session => session && session.project_id === pending.projectId && session.mode === pending.mode &&
        (pending.mode !== "task" || session.workflow_id === workflowId);
      const sessionCollection = pending.projectId === CHAT_PROJECT_ID ? state.chatSessions : state.sessions;
      let session = matches(state.session) ? state.session : sessionCollection.find(matches);
      if (!session) {
        const sessionPath = pending.projectId === CHAT_PROJECT_ID
          ? "/api/v2/sessions"
          : `/api/v2/projects/${encodeURIComponent(pending.projectId)}/sessions`;
        session = await post(sessionPath, {
          title: pending.content.slice(0, 36), mode: pending.mode, workflow_id: workflowId,
          model_alias: pending.modelAlias,
          policy: pending.mode === "task" ? { strategy: state.selectedStrategy } : undefined,
        });
        if (pending.projectId === CHAT_PROJECT_ID) state.chatSessions.unshift(session);
        else state.sessions.unshift(session);
      }
      pending.sessionId = session.session_id;
      if (activeContextId() === pending.projectId) state.session = session;
      if (pending.mode === "chat") {
        state.streamMessage = {
          id: pending.id,
          projectId: pending.projectId,
          sessionId: session.session_id,
          content: "",
          status: "生成中…",
        };
        renderSessions();
        const completed = await streamPost(
          `/api/v2/sessions/${session.session_id}/messages/stream`,
          {
            content: pending.content,
            model_alias: pending.modelAlias,
            ...(pending.tools !== null ? { tools: pending.tools } : {}),
            ...(pending.attachments?.length ? { attachments: pending.attachments } : {}),
          },
          (eventName, payload) => {
            if (!state.streamMessage || state.streamMessage.id !== pending.id) return;
            if (eventName === "start") state.streamMessage.sessionId = payload.session_id || state.streamMessage.sessionId;
            if (eventName === "text_delta") state.streamMessage.content += String(payload.text || "");
            if (eventName === "tool_call") state.streamMessage.status = `调用 ${payload.name || "本地工具"}…`;
            if (eventName === "tool_result") state.streamMessage.status = "整理搜索结果…";
            if (eventName === "done") {
              state.streamMessage.model = payload.model || "";
              state.streamMessage.elapsedMs = payload.elapsed_ms;
              state.streamMessage.status = "";
            }
            renderSessions();
          },
        );
        session = completed.session || await api(`/api/v2/sessions/${session.session_id}`);
      } else {
        session = await post(`/api/v2/sessions/${session.session_id}/messages`, { content: pending.content });
        session = await post(`/api/v2/sessions/${session.session_id}/run`, {});
      }
      if (activeContextId() === pending.projectId) {
        state.session = session;
        state.selectedModelAlias = session.context?.inputs?.model_alias ?? pending.modelAlias;
        const collection = pending.projectId === CHAT_PROJECT_ID ? state.chatSessions : state.sessions;
        const index = collection.findIndex(item => item.session_id === session.session_id);
        if (index >= 0) collection[index] = session;
        else collection.unshift(session);
      }
      if (state.streamMessage?.id === pending.id) state.streamMessage = null;
      if (state.pendingMessage?.id === pending.id) {
        state.pendingMessage = null;
        state.composerAttachments = [];
        renderComposerAttachments();
      }
      renderProjects(); renderSessions();
    } catch (error) {
      if (state.streamMessage?.id === pending.id) state.streamMessage = null;
      if (state.pendingMessage?.id === pending.id) state.pendingMessage = null;
      // Keep a failed request recoverable without overwriting a newer draft.
      const input = $("messageInput");
      if (!input.value.trim()) { input.value = pending.draftContent || pending.content; resizeComposer(); input.focus(); }
      syncSendButton(); renderProjects(); renderSessions(); toast(error.message);
    }
  }

  async function refreshManagement() {
    [state.catalog, state.resources, state.strategies] = await Promise.all([api("/api/v2/catalog"), api("/api/v2/resources"), api("/api/v2/strategies")]);
    $("projectDefaultModel").innerHTML = option("", "自动选择模型") + state.resources.models.map(item => option(item.alias, item.alias)).join("");
    renderWorkflowPicker(); renderModelPicker(); renderProviders(); renderNodes(); renderWorkflowList();
  }

  function formatElapsed(value) {
    const milliseconds = Number(value);
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return "";
    if (milliseconds < 1000) return `${Math.max(1, Math.round(milliseconds))} ms`;
    const seconds = milliseconds / 1000;
    return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  }

  function messageMetaMarkup(message) {
    const items = [];
    if (message.node_id) {
      items.push(`<span class="message-meta-item message-node-label">${esc(message.node_id)}</span>`);
    } else if (message.role === "assistant") {
      const metadata = message.metadata || {};
      const model = String(metadata.model || message.model || "").trim();
      const elapsed = formatElapsed(metadata.elapsed_ms ?? message.elapsed_ms);
      if (model) items.push(`<span class="message-meta-item">模型 ${esc(model)}</span>`);
      if (elapsed) items.push(`<span class="message-meta-item">耗时 ${esc(elapsed)}</span>`);
    }
    if (message.pending) items.push('<span class="message-meta-item message-status">发送中…</span>');
    if (message.streaming && message.stream_status) items.push(`<span class="message-meta-item message-status">${esc(message.stream_status)}</span>`);
    if (!items.length) return "";
    return `<div class="message-meta">${items.join('<span class="message-meta-separator" aria-hidden="true">·</span>')}</div>`;
  }

  function messageAttachmentMarkup(message) {
    const attachments = message.attachments || message.metadata?.attachments;
    if (!Array.isArray(attachments) || !attachments.length) return "";
    return `<div class="message-attachments">${attachments.map(item => `<span class="message-attachment"><span class="icon-slot" aria-hidden="true">${svgIcon("paperclip")}</span>${esc(item.name || "未命名附件")}</span>`).join("")}</div>`;
  }

  function dataActions(kind, id, editable = true) {
    if (!editable) return `<span class="readonly-tag">内置</span>`;
    return `<span class="row-actions"><button class="icon-button small" type="button" data-edit-${kind}="${esc(id)}" aria-label="编辑" title="编辑">${svgIcon("pencil")}</button><button class="icon-button small danger-icon" type="button" data-delete-${kind}="${esc(id)}" aria-label="删除" title="删除">${svgIcon("trash")}</button></span>`;
  }

  const protocolLabel = value => value === "claude" ? "Claude Messages" : "OpenAI Chat";
  const providerHealthLabel = (errorType, ok = false) => {
    if (ok) return "连接正常";
    return ({
      authentication_missing: "认证失败",
      authentication_failed: "认证失败",
      rate_limited: "已限流",
      connection_failed: "连接失败",
      http_error: "接口异常",
      model_not_found: "模型不存在",
      quota_exceeded: "额度不足",
      no_models: "无可测试模型",
      provider_disabled: "已停用",
    })[errorType] || "连接失败";
  };

  function formatCheckTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(date);
  }

  function providerHealthView(provider, health) {
    if (state.testingProviders.has(provider.provider_id)) {
      return { label: "测试中", tone: "testing", detail: "正在验证协议、认证和模型可用性…", checks: [], checkedAt: "" };
    }
    if (!provider.enabled) {
      return { label: "已停用", tone: "muted", detail: "启用供应商后可测试连接", checks: [], checkedAt: "" };
    }
    const last = health?.last_check || {};
    if (!last.checked_at) {
      if (!health?.configured) {
        return { label: "认证失败", tone: "danger", detail: "尚未配置认证密钥", checks: [], checkedAt: "" };
      }
      return { label: "未测试", tone: "idle", detail: "尚未执行连接测试", checks: [], checkedAt: "" };
    }
    const label = providerHealthLabel(last.error_type, last.ok);
    const tone = last.ok ? "success" : last.error_type === "rate_limited" ? "warning" : "danger";
    return {
      label, tone, checks: Array.isArray(last.checks) ? last.checks : [],
      detail: last.ok ? "协议、认证和模型请求均通过" : (last.error || label),
      checkedAt: formatCheckTime(last.checked_at),
    };
  }

  function renderProviders() {
    const health = Object.fromEntries(state.resources.health.map(item => [item.provider_id, item]));
    const authLabel = value => ({ bearer: "Bearer Token", api_key: "API Key", token: "Token", basic: "Basic Auth", custom_header: "自定义 Header", query_param: "Query 参数", none: "无需认证" }[value] || value);
    const providerCount = state.resources.providers.length;
    const modelCount = state.resources.models.length;
    const enabledProviderCount = state.resources.providers.filter(item => item.enabled).length;
    const enabledModelCount = state.resources.models.filter(item => item.enabled).length;
    const providerStatuses = new Map(state.resources.providers.map(item => [item.provider_id, providerHealthView(item, health[item.provider_id] || {})]));
    const healthyCount = [...providerStatuses.values()].filter(status => status.tone === "success").length;
    const searchTerm = String($("providerSearch")?.value || "").trim().toLowerCase();
    const visibleProviders = state.resources.providers.filter(item => {
      if (!searchTerm) return true;
      const models = state.resources.models.filter(model => model.provider_id === item.provider_id);
      const searchable = [item.provider_id, item.label, item.base_url, ...models.flatMap(model => [model.alias, model.model, ...(model.capabilities || [])])];
      return searchable.some(value => String(value || "").toLowerCase().includes(searchTerm));
    });
    if ($("providerCount")) $("providerCount").textContent = `${providerCount} 个供应商`;
    if ($("providerStat")) $("providerStat").textContent = providerCount;
    if ($("providerStatHint")) $("providerStatHint").textContent = providerCount ? `${enabledProviderCount} 个已启用` : "等待添加供应商";
    if ($("modelStat")) $("modelStat").textContent = modelCount;
    if ($("modelStatHint")) $("modelStatHint").textContent = modelCount ? `${enabledModelCount} 个可调用` : "添加模型后即可使用";
    if ($("healthyStat")) $("healthyStat").textContent = healthyCount;
    if ($("healthyStatHint")) $("healthyStatHint").textContent = healthyCount ? `共 ${providerCount} 个供应商` : providerCount ? "运行测试以确认连接" : "尚未进行连接测试";
    if ($("modelFilterHint")) $("modelFilterHint").textContent = searchTerm
      ? `匹配 ${visibleProviders.length} / ${providerCount} 个供应商`
      : (providerCount ? "显示全部供应商" : "还没有资源");
    $("providerList").innerHTML = providerCount ? (visibleProviders.length ? visibleProviders.map(item => {
      const models = state.resources.models.filter(model => model.provider_id === item.provider_id);
      const providerHealth = health[item.provider_id] || {};
      const status = providerStatuses.get(item.provider_id) || providerHealthView(item, providerHealth);
      const checking = state.testingProviders.has(item.provider_id);
      const checks = status.checks.map(check => `<span class="protocol-check ${check.ok ? "ok" : "failed"}"><strong>${esc(protocolLabel(check.protocol))}</strong><span>${check.ok ? `${esc(check.latency_ms)} ms` : esc(providerHealthLabel(check.error_type))}</span></span>`).join("");
      return `<section class="provider-group"><header class="provider-group-head"><span class="provider-mark">${esc(item.label.slice(0, 1).toUpperCase())}</span><div class="provider-copy"><strong>${esc(item.label)}</strong><span>${esc(item.base_url)}</span></div><span class="health-status ${status.tone}"><i></i>${esc(status.label)}</span>${dataActions("provider", item.provider_id)}</header><div class="provider-meta"><span>${(item.protocols || ["openai"]).map(protocol => esc(protocolLabel(protocol))).join(" / ")}</span><span>${esc(authLabel(item.auth_type || "bearer"))}</span><span>${providerHealth.configured ? "认证已配置" : item.auth_type === "none" ? "无需密钥" : "缺少密钥"}</span><span>${models.length} 个模型</span></div><div class="provider-health"><div class="provider-health-copy" title="${esc(status.detail)}"><span>${esc(status.detail)}</span>${status.checkedAt ? `<time>最近测试 ${esc(status.checkedAt)}</time>` : ""}${checks ? `<div class="protocol-checks">${checks}</div>` : ""}</div><button class="button quiet compact test-provider" type="button" data-test-provider="${esc(item.provider_id)}" ${checking || !item.enabled ? "disabled" : ""}>${checking ? "测试中…" : "测试连接"}</button></div><div class="provider-models"><div class="provider-models-head"><strong>模型</strong><button class="button quiet compact" type="button" data-add-model="${esc(item.provider_id)}">${svgIcon("plus")} 添加模型</button></div>${models.length ? models.map(model => `<div class="provider-model-row"><span class="model-glyph">M</span><div><strong>${esc(model.alias)}</strong><span>${esc(model.model)} · ${esc(protocolLabel(model.protocol || item.protocols?.[0] || "openai"))}</span></div><span class="state-tag">${model.enabled ? "已启用" : "已停用"}</span>${dataActions("model", model.alias)}</div>`).join("") : `<div class="provider-model-empty">该供应商还没有模型</div>`}</div></section>`;
    }).join("") : `<div class="list-empty">没有匹配的供应商或模型。试试其他关键词。</div>`) : `<div class="list-empty">还没有供应商，先添加供应商再配置模型。</div>`;
    document.querySelectorAll("[data-edit-provider]").forEach(button => button.addEventListener("click", () => editProvider(button.dataset.editProvider)));
    document.querySelectorAll("[data-delete-provider]").forEach(button => button.addEventListener("click", () => deleteProvider(button.dataset.deleteProvider)));
    document.querySelectorAll("[data-test-provider]").forEach(button => button.addEventListener("click", () => testProvider(button.dataset.testProvider)));
    document.querySelectorAll("[data-add-model]").forEach(button => button.addEventListener("click", () => editModel("", button.dataset.addModel)));
    document.querySelectorAll("[data-edit-model]").forEach(button => button.addEventListener("click", () => editModel(button.dataset.editModel)));
    document.querySelectorAll("[data-delete-model]").forEach(button => button.addEventListener("click", () => deleteModel(button.dataset.deleteModel)));
  }

  async function testProvider(providerId) {
    if (state.testingProviders.has(providerId)) return;
    state.testingProviders.add(providerId); renderProviders();
    try {
      const result = await post(`/api/v2/resources/providers/${encodeURIComponent(providerId)}/test`, {});
      await refreshManagement();
      toast(result.ok ? "连接测试通过" : `测试完成：${providerHealthLabel(result.error_type)}`);
    } catch (error) {
      toast(error.message);
    } finally {
      state.testingProviders.delete(providerId); renderProviders();
    }
  }

  function editProvider(providerId = "") {
    const item = state.resources.providers.find(provider => provider.provider_id === providerId);
    $("providerForm").reset(); $("providerForm").classList.remove("hidden"); $("modelForm").classList.add("hidden");
    $("providerFormTitle").textContent = item ? "编辑供应商" : "添加供应商";
    $("providerId").value = item?.provider_id || ""; $("providerId").readOnly = Boolean(item);
    $("providerLabel").value = item?.label || ""; $("providerUrl").value = item?.base_url || "";
    const selectedProtocol = item?.protocols?.[0] || "openai";
    $("providerProtocolOpenAI").checked = selectedProtocol === "openai";
    $("providerProtocolClaude").checked = selectedProtocol === "claude";
    $("providerAuthType").value = item?.auth_type || "bearer";
    $("providerAuthHeader").value = item?.auth_header || "";
    $("providerAuthPrefix").value = ["custom_header", "token"].includes(item?.auth_type) ? (item.auth_prefix || (item?.auth_type === "token" ? "Token" : "")) : "";
    $("providerAuthUsername").value = item?.metadata?.username || "";
    $("providerAuthParam").value = item?.metadata?.query_param || "api_key";
    $("providerEnabled").checked = item?.enabled ?? true; $("providerKey").value = "";
    document.querySelectorAll("[data-provider-preset]").forEach(button => button.classList.toggle("active", Boolean(item) && button.dataset.providerPreset === item.provider_id));
    syncProviderAuth();
  }

  function syncProviderAuth() {
    const authType = $("providerAuthType").value;
    const custom = authType === "custom_header";
    const basic = authType === "basic";
    const query = authType === "query_param";
    const token = authType === "token";
    $("providerAuthHeaderField").classList.toggle("hidden", !custom);
    $("providerAuthPrefixField").classList.toggle("hidden", !(custom || token));
    $("providerAuthUsernameField").classList.toggle("hidden", !basic);
    $("providerAuthParamField").classList.toggle("hidden", !query);
    $("providerCredentialField").classList.toggle("hidden", authType === "none");
    const labels = {
      bearer: ["Authorization: Bearer ••••••••", "请求头将发送 Authorization: Bearer <token>"],
      api_key: ["x-api-key: ••••••••", "请求头将发送 x-api-key: <key>"],
      token: ["Authorization: Token ••••••••", "请求头将发送 Authorization: Token <token>"],
      basic: ["Authorization: Basic ••••••••", "使用用户名和凭据生成 Basic Authorization"],
      custom_header: [`${$("providerAuthHeader").value || "X-Auth-Token"}: ${$("providerAuthPrefix").value ? `${$("providerAuthPrefix").value} ` : ""}••••••••`, "按你填写的 Header 名和值前缀发送凭据"],
      query_param: [`?${$("providerAuthParam").value || "api_key"}=••••••••`, "凭据会追加到请求 URL；仅在供应商要求时使用"],
      none: ["请求不会附带认证信息", "适用于本地服务或由网关负责认证"],
    };
    const [preview, hint] = labels[authType] || labels.bearer;
    $("providerAuthPreview").textContent = preview;
    $("providerAuthHint").textContent = hint;
    $("providerCredentialHint").textContent = basic ? "这里填写密码或访问凭据；编辑时留空可保留原凭据" : "编辑时留空可保留原凭据；支持粘贴";
    $("providerKey").placeholder = basic ? "粘贴密码或访问凭据" : authType === "none" ? "无需填写" : "粘贴 API Key 或 Token";
  }

  async function deleteProvider(providerId) {
    const provider = state.resources.providers.find(item => item.provider_id === providerId);
    const confirmed = await confirmAction({
      title: "删除供应商",
      message: `确定删除“${provider?.label || providerId}”吗？连接配置和本地凭据将一并移除。有关联模型时不会执行删除。`,
    });
    if (!confirmed) return;
    try {
      await remove(`/api/v2/resources/providers/${encodeURIComponent(providerId)}`);
      if ($("providerId").value === providerId) $("providerForm").classList.add("hidden");
      if ($("modelProvider").value === providerId) $("modelForm").classList.add("hidden");
      await refreshManagement(); toast("供应商已删除");
    }
    catch (error) { toast(error.message); }
  }

  function renderModelProviderOptions(selectedProviderId) {
    $("modelProvider").innerHTML = state.resources.providers
      .map(provider => option(provider.provider_id, provider.label, selectedProviderId))
      .join("");
    $("modelProvider").value = selectedProviderId || "";
  }

  function setModelProtocolOptions(provider, selectedProtocol = "") {
    const protocol = provider.protocols.includes(selectedProtocol) ? selectedProtocol : provider.protocols[0];
    $("modelProtocol").innerHTML = provider.protocols
      .map(value => option(value, value === "claude" ? "Claude · Messages" : "OpenAI · Chat Completions", protocol))
      .join("");
    return protocol;
  }

  function resetPhysicalModelPicker() {
    $("modelName").innerHTML = option("", "正在获取模型列表…");
    $("modelName").value = "";
    $("modelNameCustom").value = "";
    $("modelNameCustom").classList.add("hidden");
    $("modelNameCustom").required = false;
  }

  function editModel(alias = "", providerId = "") {
    if (!state.resources.providers.length) { toast("请先添加供应商"); return; }
    const item = state.resources.models.find(model => model.alias === alias);
    const selectedProviderId = item?.provider_id || providerId || state.resources.providers[0].provider_id;
    const provider = state.resources.providers.find(value => value.provider_id === selectedProviderId);
    if (!provider) { toast("未找到模型所属供应商"); return; }
    $("modelForm").reset(); $("modelForm").classList.remove("hidden"); $("providerForm").classList.add("hidden");
    $("modelFormTitle").textContent = item ? "编辑模型" : "添加模型";
    $("modelAlias").value = item?.alias || ""; $("modelAlias").readOnly = Boolean(item);
    renderModelProviderOptions(selectedProviderId);
    setModelProtocolOptions(provider, item?.protocol || "");
    resetPhysicalModelPicker();
    $("modelName").disabled = true;
    $("modelDiscoveryStatus").textContent = "正在从供应商获取可用模型…";
    $("modelDiscoveryStatus").classList.remove("error");
    modelAliasAutoValue = item?.alias || "";
    $("modelCapabilities").value = (item?.capabilities || ["general"]).join(", ");
    $("modelTemperature").value = item?.temperature ?? ""; $("modelMaxTokens").value = item?.max_tokens ?? ""; $("modelContextWindow").value = item?.context_window_tokens ?? "";
    $("modelEnabled").checked = item?.enabled ?? true;
    discoverProviderModels(provider, item?.model || "");
  }

  let modelAliasAutoValue = "";
  let modelDiscoverySequence = 0;

  function physicalModelName() {
    return $("modelName").value === "__manual__"
      ? $("modelNameCustom").value.trim()
      : $("modelName").value;
  }

  function syncModelAliasDefault() {
    const alias = $("modelAlias");
    const modelName = defaultModelAlias(physicalModelName());
    if (!alias.readOnly && (!alias.value.trim() || alias.value === modelAliasAutoValue)) {
      alias.value = modelName;
      modelAliasAutoValue = modelName;
    }
  }

  function syncPhysicalModelMode({ focus = false } = {}) {
    const manual = $("modelName").value === "__manual__";
    $("modelNameCustom").classList.toggle("hidden", !manual);
    $("modelNameCustom").required = manual;
    if (manual && focus) $("modelNameCustom").focus();
    syncModelAliasDefault();
  }

  async function discoverProviderModels(provider, selectedModel = "") {
    const requestSequence = ++modelDiscoverySequence;
    const select = $("modelName");
    const refresh = $("refreshModelList");
    select.disabled = true;
    refresh.disabled = true;
    $("modelDiscoveryStatus").textContent = "正在从供应商获取可用模型…";
    $("modelDiscoveryStatus").classList.remove("error");
    try {
      const result = await post(`/api/v2/resources/providers/${encodeURIComponent(provider.provider_id)}/models/discover`, {
        protocol: $("modelProtocol").value,
      });
      if (requestSequence !== modelDiscoverySequence) return;
      const models = [...new Set((result.models || []).map(value => String(value).trim()).filter(Boolean))];
      select.innerHTML = option("", models.length ? "请选择物理模型" : "未发现可用模型")
        + models.map(model => option(model, model, selectedModel)).join("")
        + option("__manual__", "手动输入其他模型…");
      if (selectedModel && !models.includes(selectedModel)) {
        select.insertAdjacentHTML("afterbegin", option(selectedModel, `${selectedModel}（当前配置）`, selectedModel));
      }
      select.value = selectedModel || "";
      $("modelDiscoveryStatus").textContent = models.length
        ? `已获取 ${models.length} 个模型；列表中没有时可手动输入`
        : "供应商未返回模型；请选择手动输入";
      if (!models.length && !selectedModel) select.value = "__manual__";
      syncPhysicalModelMode();
    } catch (error) {
      if (requestSequence !== modelDiscoverySequence) return;
      select.innerHTML = option("__manual__", "手动输入物理模型…", "__manual__");
      select.value = "__manual__";
      $("modelDiscoveryStatus").textContent = `自动获取失败：${error.message}；仍可手动输入`;
      $("modelDiscoveryStatus").classList.add("error");
      syncPhysicalModelMode();
    } finally {
      if (requestSequence !== modelDiscoverySequence) return;
      select.disabled = false;
      refresh.disabled = false;
    }
  }

  async function deleteModel(alias) {
    const confirmed = await confirmAction({
      title: "删除模型",
      message: `确定删除模型“${alias}”吗？内置角色会自动改用其他可用模型；如果没有替代模型，未被任务使用的内置角色会暂时移除。`,
    });
    if (!confirmed) return;
    try {
      const result = await remove(`/api/v2/resources/models/${encodeURIComponent(alias)}`);
      if ($("modelAlias").value === alias) { $("modelForm").classList.add("hidden"); $("modelForm").reset(); }
      await refreshManagement();
      const changed = (result.reassigned_roles || []).length || (result.removed_roles || []).length;
      toast(changed ? "模型已删除，相关内置角色已自动处理" : "模型已删除");
    }
    catch (error) { toast(error.message); }
  }

  function renderNodes() {
    $("nodeCount").textContent = `${state.catalog.nodes.length} 个节点`;
    $("nodeList").innerHTML = state.catalog.nodes.map(item => `
      <div class="data-row node-row">
        <span class="node-glyph" aria-hidden="true">N</span>
        <div class="node-copy">
          <strong>${esc(item.label)}</strong>
          <span class="node-meta">
            <span class="node-type">${esc(item.node_type)}</span>
            <i aria-hidden="true">·</i>
            <span class="node-model">${esc(item.default_model || "自动选择模型")}</span>
          </span>
        </div>
        <div class="node-row-actions">
          <span class="node-output-count">${item.output_fields.length} 个输出</span>
          ${item.builtin ? '<span class="node-kind-tag">内置</span>' : dataActions("node", item.node_type, true)}
        </div>
      </div>
    `).join("");
    document.querySelectorAll("[data-edit-node]").forEach(button => button.addEventListener("click", () => editNode(button.dataset.editNode)));
    document.querySelectorAll("[data-delete-node]").forEach(button => button.addEventListener("click", () => deleteNode(button.dataset.deleteNode)));
    fillNodeModelOptions();
  }

  function fillNodeModelOptions(selected = $("nodeDefaultModel").value) {
    const enabledModels = state.resources.models.filter(item => item.enabled);
    const currentDisabled = selected && !enabledModels.some(item => item.alias === selected);
    const options = enabledModels.map(item => option(item.alias, item.alias, selected));
    if (currentDisabled) options.unshift(option(selected, `${selected}（当前模型已停用）`, selected));
    $("nodeDefaultModel").innerHTML = option("", "自动选择", selected) + options.join("");
    $("nodeDefaultModel").value = selected || "";
  }

  function editNode(nodeType = "") {
    const item = state.catalog.nodes.find(node => node.node_type === nodeType);
    if (item?.builtin) return;
    $("nodeForm").reset(); $("nodeForm").classList.remove("hidden");
    $("nodeFormTitle").textContent = item ? "编辑自定义节点" : "新建自定义节点";
    $("nodeType").value = item?.node_type || ""; $("nodeType").readOnly = Boolean(item);
    $("nodeLabel").value = item?.label || ""; $("nodeDescription").value = item?.description || "";
    $("nodeInputs").value = (item?.input_fields || []).join(", "); $("nodeOutputs").value = (item?.output_fields || []).join(", ");
    $("nodeCapabilities").value = (item?.capabilities || ["general"]).join(", "); fillNodeModelOptions(item?.default_model || "");
  }

  async function deleteNode(nodeType) {
    const confirmed = await confirmAction({ title: "删除自定义节点", message: `确定删除“${nodeType}”吗？被工作流引用时不会执行删除。` });
    if (!confirmed) return;
    try {
      await remove(`/api/v2/nodes/${encodeURIComponent(nodeType)}`);
      if ($("nodeType").value === nodeType) {
        $("nodeForm").classList.add("hidden");
        $("nodeForm").reset();
      }
      await refreshManagement();
      toast("节点已删除");
    }
    catch (error) { toast(error.message); }
  }

  function renderWorkflowList() {
    const workflows = state.catalog.workflows || [];
    $("workflowCount").textContent = `${workflows.length} 个流程`;
    if (!state.editingWorkflow || (state.editingWorkflow.workflow_id && !workflows.some(item => item.workflow_id === state.editingWorkflow.workflow_id))) {
      state.editingWorkflow = clone(workflows.find(item => item.workflow_id === state.selectedWorkflowId) || workflows[0] || newWorkflowValue());
    }
    $("workflowList").innerHTML = workflows.length
      ? workflows.map(item => `<button type="button" class="workflow-list-item ${state.editingWorkflow?.workflow_id === item.workflow_id ? "active" : ""}" data-workflow="${esc(item.workflow_id)}"><strong>${esc(item.label)}</strong><span>${(item.nodes || []).length} 个节点${item.builtin ? " · 内置" : ""}</span></button>`).join("")
      : '<div class="list-empty">还没有工作流，先创建一个可复用的流程。</div>';
    document.querySelectorAll("[data-workflow]").forEach(button => button.addEventListener("click", () => {
      state.editingWorkflow = clone(workflows.find(item => item.workflow_id === button.dataset.workflow)); renderWorkflowList(); renderWorkflowEditor();
    }));
    renderWorkflowEditor();
  }

  function newWorkflowValue() {
    return { workflow_id: "", label: "", description: "", builtin: false, nodes: [{ node_id: "step-1", node_type: state.catalog.nodes[0]?.node_type || "tool", depends_on: [], model_alias: "", prompt_template: "", on_failure: "human", config: {}, position: [0, 0] }] };
  }

  function renderWorkflowEditor() {
    const workflow = state.editingWorkflow || newWorkflowValue();
    $("workflowId").value = workflow.workflow_id || ""; $("workflowId").readOnly = Boolean(workflow.workflow_id);
    $("workflowLabel").value = workflow.label || ""; $("workflowDescription").value = workflow.description || "";
    $("deleteWorkflow").classList.toggle("hidden", !workflow.workflow_id || workflow.builtin);
    renderWorkflowNodes();
  }

  function renderWorkflowNodes() {
    const nodes = state.editingWorkflow?.nodes || [];
    $("workflowNodeList").innerHTML = nodes.length ? nodes.map((node, index) => `<article class="workflow-node-row" data-workflow-node="${index}"><div class="node-row-index">${index + 1}</div><div class="node-row-main"><div class="node-row-fields"><label>节点 ID<input data-node-field="node_id" value="${esc(node.node_id)}" required pattern="[A-Za-z0-9_-]+"></label><label>节点类型<select data-node-field="node_type">${state.catalog.nodes.map(item => option(item.node_type, item.label, node.node_type)).join("")}</select></label><label>绑定模型<select data-node-field="model_alias">${option("", "自动选择模型", node.model_alias)}${state.resources.models.map(item => option(item.alias, item.alias, node.model_alias)).join("")}</select></label><label>失败策略<select data-node-field="on_failure">${[["human", "人工处理"], ["retry", "自动重试"], ["skip", "跳过"], ["replan", "重新规划"]].map(([value, label]) => option(value, label, node.on_failure)).join("")}</select></label></div><div class="node-row-secondary"><label>依赖节点<span>使用英文逗号分隔</span><input data-node-field="depends_on" value="${esc((node.depends_on || []).join(", "))}" placeholder="step-1, step-2"></label><details><summary>提示词模板</summary><textarea data-node-field="prompt_template" rows="2" placeholder="可选；留空使用节点默认提示词">${esc(node.prompt_template || "")}</textarea></details></div></div><button class="icon-button danger-icon" type="button" data-remove-workflow-node="${index}" aria-label="移除节点" title="移除节点">${svgIcon("trash")}</button></article>`).join("") : `<div class="list-empty">至少添加一个节点才能保存工作流</div>`;
    document.querySelectorAll("[data-remove-workflow-node]").forEach(button => button.addEventListener("click", () => {
      syncWorkflowForm(); state.editingWorkflow.nodes.splice(Number(button.dataset.removeWorkflowNode), 1); renderWorkflowNodes();
    }));
  }

  function syncWorkflowForm() {
    if (!state.editingWorkflow) state.editingWorkflow = newWorkflowValue();
    state.editingWorkflow.workflow_id = $("workflowId").value.trim(); state.editingWorkflow.label = $("workflowLabel").value.trim(); state.editingWorkflow.description = $("workflowDescription").value.trim();
    const previousNodes = state.editingWorkflow.nodes || [];
    state.editingWorkflow.nodes = [...document.querySelectorAll("[data-workflow-node]")].map((row, index) => ({
      node_id: row.querySelector('[data-node-field="node_id"]').value.trim(),
      node_type: row.querySelector('[data-node-field="node_type"]').value,
      model_alias: row.querySelector('[data-node-field="model_alias"]').value,
      on_failure: row.querySelector('[data-node-field="on_failure"]').value,
      depends_on: csv(row.querySelector('[data-node-field="depends_on"]').value),
      prompt_template: row.querySelector('[data-node-field="prompt_template"]').value,
      config: { ...(previousNodes[index]?.config || {}) },
      position: previousNodes[index]?.position || [0, index * 100],
    }));
  }

  function setManagementTab(name) {
    document.querySelectorAll("[data-management-tab]").forEach(button => { const active = button.dataset.managementTab === name; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); });
    document.querySelectorAll("[data-management-panel]").forEach(panel => panel.classList.toggle("hidden", panel.dataset.managementPanel !== name));
    const managementBody = document.querySelector(".management-body");
    if (managementBody) managementBody.scrollTop = 0;
    window.dispatchEvent(new CustomEvent("workloop:management-tab", { detail: { name } }));
  }

  async function openManagement() {
    try {
      await refreshManagement();
      $("managementDialog").showModal();
      const name = document.querySelector("[data-management-tab].active")?.dataset.managementTab || "models";
      window.dispatchEvent(new CustomEvent("workloop:management-open", { detail: { name } }));
    }
    catch (error) { toast(error.message); }
  }

  async function init() {
    try {
      const selectedProjectId = state.project?.project_id || "";
      await refreshManagement();
      [state.projects, state.chatSessions] = await Promise.all([
        api("/api/v2/projects"),
        api("/api/v2/sessions"),
      ]);
      renderProjects();
      if (selectedProjectId && state.projects.some(item => item.project_id === selectedProjectId)) {
        await selectProject(selectedProjectId);
      }
      else {
        state.project = null;
        state.sessions = [];
        delete document.body.dataset.projectId;
        delete document.body.dataset.projectName;
        state.selectedModelAlias = null;
        state.selectedTools = null;
        state.session = state.chatSessions[0] || null;
        state.mode = "chat";
        renderMode(); renderProjects(); renderSessions();
      }
    } catch (error) { toast(error.message); }
  }

  async function chooseWorkspace() {
    const bridge = window.pywebview?.api;
    if (!bridge || typeof bridge.choose_workspace !== "function") {
      toast("文件夹选择仅在 Workloop 桌面版中可用，也可以手动输入绝对路径");
      $("workspaceInput").focus();
      return;
    }

    const button = $("chooseWorkspace");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const selected = await bridge.choose_workspace($("workspaceInput").value.trim());
      if (selected) {
        $("workspaceInput").value = selected;
        $("workspaceInput").dispatchEvent(new Event("input", { bubbles: true }));
        $("workspaceInput").focus();
      }
    } catch (error) {
      toast(error?.message || "无法打开文件夹选择器");
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  function openProjectDialog(project = null) {
    state.editingProjectId = project?.project_id || "";
    $("projectDialogTitle").textContent = project ? "项目设置" : "新建项目";
    $("projectSubmit").textContent = project ? "保存设置" : "创建项目";
    $("deleteProject").classList.toggle("hidden", !project);
    $("deleteProject").disabled = false;
    $("projectInput").value = project?.name || "";
    $("workspaceInput").value = project?.workspace_path || "";
    $("projectDefaultModel").value = project?.default_model || "";
    $("validationCommandsInput").value = (project?.validation_commands || []).map(command => JSON.stringify(command)).join("\n");
    $("instructionsInput").value = project?.instructions || "";
    const policy = project?.runtime_policy || {};
    const compaction = policy.compaction || {};
    const search = policy.local_search || {};
    $("compactionEnabled").checked = compaction.enabled !== false;
    $("compactionReserve").value = compaction.reserve_tokens || 4096;
    $("compactionKeepRecent").value = compaction.keep_recent_tokens || 12000;
    $("compactionSummaryMax").value = compaction.summary_max_tokens || 1500;
    $("compactionMaxRounds").value = compaction.max_compactions || 2;
    $("searchEnabled").checked = search.enabled !== false;
    $("searchMaxRounds").value = search.max_tool_rounds || 8;
    $("projectDialog").showModal();
  }

  function closeProjectDialog() {
    state.editingProjectId = "";
    $("projectForm").reset();
    $("projectDialogTitle").textContent = "新建项目";
    $("projectSubmit").textContent = "创建项目";
    $("deleteProject").classList.add("hidden");
    $("projectDialog").close();
  }

  async function deleteProject(projectId = state.editingProjectId || state.project?.project_id) {
    const project = state.projects.find(item => item.project_id === projectId);
    if (!projectId || !project) return;
    const confirmed = await confirmAction({
      title: "删除项目",
      message: `确定删除“${project.name}”吗？项目下的会话、协同任务和拆分记录会一并删除，但工作区文件不会被修改。`,
      confirmLabel: "删除项目",
    });
    if (!confirmed) return;
    const button = $("deleteProject");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      await remove(`/api/v2/projects/${encodeURIComponent(projectId)}`);
      state.projects = state.projects.filter(item => item.project_id !== projectId);
      if (state.project?.project_id === projectId) {
        state.project = null;
        state.sessions = [];
        state.session = state.chatSessions[0] || null;
        state.mode = "chat";
        state.selectedModelAlias = null;
        state.selectedTools = null;
        state.search = null;
        state.searchProjectId = "";
        delete document.body.dataset.projectId;
        delete document.body.dataset.projectName;
      }
      closeProjectDialog();
      renderMode(); renderProjects(); renderSessions();
      toast("项目已删除");
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  $("newProject").addEventListener("click", () => openProjectDialog());
  $("editProject").addEventListener("click", () => state.project ? openProjectDialog(state.project) : toast("请先选择项目"));
  $("chooseWorkspace").addEventListener("click", chooseWorkspace);
  document.querySelectorAll("[data-close-project]").forEach(button => {
    button.addEventListener("click", closeProjectDialog);
  });
  $("projectDialog").addEventListener("cancel", () => {
    state.editingProjectId = "";
    $("projectForm").reset();
    $("projectSubmit").textContent = "创建项目";
    $("deleteProject").classList.add("hidden");
  });
  $("projectForm").addEventListener("submit", async event => {
    event.preventDefault();
    try {
      const validationCommands = $("validationCommandsInput").value.split(/\r?\n/).map(line => line.trim()).filter(Boolean).map((line, index) => {
        let value; try { value = JSON.parse(line); } catch (_) { throw new Error(`第 ${index + 1} 条验证命令不是合法 JSON`); }
        if (!Array.isArray(value) || !value.length || value.some(item => typeof item !== "string" || !item)) throw new Error(`第 ${index + 1} 条验证命令必须是非空字符串数组`);
        return value;
      });
      const runtime_policy = {
        compaction: {
          enabled: $("compactionEnabled").checked,
          reserve_tokens: parseInt($("compactionReserve").value, 10),
          keep_recent_tokens: parseInt($("compactionKeepRecent").value, 10),
          summary_max_tokens: parseInt($("compactionSummaryMax").value, 10),
          max_compactions: parseInt($("compactionMaxRounds").value, 10),
        },
        local_search: {
          enabled: $("searchEnabled").checked,
          tools: ["zvec_grep_search", "zvec_grep_rg"],
          max_tool_rounds: parseInt($("searchMaxRounds").value, 10),
        },
      };
      const project = await post(state.editingProjectId ? `/api/v2/projects/${encodeURIComponent(state.editingProjectId)}` : "/api/v2/projects", {
        name: $("projectInput").value,
        workspace_path: $("workspaceInput").value.trim(),
        default_model: $("projectDefaultModel").value,
        validation_commands: validationCommands,
        instructions: $("instructionsInput").value,
        runtime_policy,
      });
      if (state.editingProjectId) state.projects = state.projects.map(item => item.project_id === project.project_id ? project : item);
      else state.projects.unshift(project);
      closeProjectDialog(); await selectProject(project.project_id);
    }
    catch (error) { toast(error.message); }
  });
  $("sendMessage").addEventListener("click", () => void send());
  $("messageInput").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } });
  $("messageInput").addEventListener("input", event => { resizeComposer(); syncSendButton(); });
  $("attachFiles").addEventListener("click", () => $("composerFileInput").click());
  $("composerFileInput").addEventListener("change", event => {
    const files = [...(event.target.files || [])];
    const existingNames = new Set(state.composerAttachments.map(file => file.name));
    const remaining = MAX_COMPOSER_ATTACHMENTS - state.composerAttachments.length;
    const accepted = [];
    for (const file of files) {
      if (accepted.length >= remaining) break;
      if (existingNames.has(file.name)) continue;
      if (file.size > MAX_COMPOSER_ATTACHMENT_BYTES) {
        toast(`附件“${file.name}”超过 ${MAX_COMPOSER_ATTACHMENT_BYTES / 1000} KB 限制`);
        continue;
      }
      accepted.push(file);
      existingNames.add(file.name);
    }
    if (files.length > accepted.length && remaining <= 0) toast(`最多添加 ${MAX_COMPOSER_ATTACHMENTS} 个附件`);
    state.composerAttachments.push(...accepted);
    event.target.value = "";
    renderComposerAttachments();
    syncSendButton();
  });
  $("deleteProject").addEventListener("click", () => void deleteProject());
  $("toggleToolMenu").addEventListener("click", () => setToolMenuOpen(!state.toolMenuOpen));
  $("closeToolMenu").addEventListener("click", () => setToolMenuOpen(false));
  $("resetComposerTools").addEventListener("click", () => {
    state.selectedTools = null;
    renderToolMenu();
  });
  document.querySelectorAll("[data-composer-tool-choice]").forEach(input => input.addEventListener("change", () => {
    state.selectedTools = [...document.querySelectorAll("[data-composer-tool-choice]:checked")].map(item => item.value);
    renderToolMenu();
  }));
  document.addEventListener("click", event => {
    const menu = $("toolMenu");
    const toggle = $("toggleToolMenu");
    if (state.toolMenuOpen && menu && toggle && !menu.contains(event.target) && !toggle.contains(event.target)) setToolMenuOpen(false);
  });
  $("modelSelect").addEventListener("change", event => {
    if (state.mode === "task") return;
    state.selectedModelAlias = event.target.value;
    renderModelPicker();
  });
  document.querySelectorAll("[data-mode]").forEach(button => button.addEventListener("click", () => {
    if (button.dataset.mode === "task" && !state.project) {
      toast("任务模式需要选择项目；普通对话无需项目");
      return;
    }
    state.mode = button.dataset.mode;
    renderMode();
    if (state.mode === "task") openTaskWorkflowPicker();
  }));
  $("workflowSelect").addEventListener("change", event => { state.selectedWorkflowId = event.target.value; renderFlow(); });
  $("strategySelect").addEventListener("change", async event => {
    state.selectedStrategy = event.target.value;
    if (state.session?.mode === "task") {
      try {
        state.session = await post(`/api/v2/sessions/${state.session.session_id}/policy`, { strategy: state.selectedStrategy });
        renderSessions();
      } catch (error) { toast(error.message); }
    }
    renderMode();
  });
  $("approvePolicy").addEventListener("click", async () => {
    if (!state.session) return;
    try { state.session = await post(`/api/v2/sessions/${state.session.session_id}/policy/approve`, {}); renderSessions(); toast("Gate 已批准"); }
    catch (error) { toast(error.message); }
  });
  $("replanPolicy").addEventListener("click", async () => {
    if (!state.session) return;
    try { state.session = await post(`/api/v2/sessions/${state.session.session_id}/policy/replan`, { reason: "用户请求重新规划" }); renderSessions(); toast("已解除 Gate，可重新运行"); }
    catch (error) { toast(error.message); }
  });
  document.querySelectorAll("[data-starter]").forEach(button => button.addEventListener("click", () => { $("messageInput").value = button.dataset.starter; resizeComposer(); syncSendButton(); $("messageInput").focus(); }));
  $("mobileMenu").addEventListener("click", () => $("rail").classList.toggle("open"));
  $("refreshProjects").addEventListener("click", init);
  $("refreshSession").addEventListener("click", () => state.session && api(`/api/v2/sessions/${state.session.session_id}`).then(session => { state.session = session; renderSessions(); }).catch(error => toast(error.message)));
  $("searchPill").addEventListener("click", () => {
    if (!state.search) return;
    const detail = state.search.ready
      ? `根目录：${state.search.root}\n后端：${state.search.backend || "zvec-grep"}`
      : `根目录：${state.search.root || "未配置"}\n错误：${state.search.error || "未就绪"}`;
    toast(detail);
  });
  $("themeToggle").addEventListener("click", () => { document.body.classList.toggle("dark"); localStorage.setItem(THEME_KEY, document.body.classList.contains("dark") ? "dark" : "light"); });
  $("closeInspector").addEventListener("click", () => setInspectorOpen(false));
  $("showInspector").addEventListener("click", () => setInspectorOpen(true));
  $("expandFlow").addEventListener("click", openFlowFocus);
  $("flowPanel").addEventListener("dblclick", openFlowFocus);
  $("flowDialogClose").addEventListener("click", () => $("flowDialog").close());
  $("flowCanvas").addEventListener("dblclick", event => { if (event.target.closest("[data-flow-node]")) $("flowDialog").close(); });

  document.querySelectorAll("[data-open-management]").forEach(button => button.addEventListener("click", openManagement));
  document.querySelectorAll("[data-close-management]").forEach(button => button.addEventListener("click", () => $("managementDialog").close()));
  document.querySelectorAll("[data-management-tab]").forEach(button => button.addEventListener("click", () => setManagementTab(button.dataset.managementTab)));
  $("providerSearch").addEventListener("input", () => renderProviders());

  $("newProvider").addEventListener("click", () => editProvider());
  $("providerForm").addEventListener("submit", async event => {
    event.preventDefault();
    try {
      const protocol = document.querySelector('input[name="providerProtocol"]:checked')?.value;
      if (!protocol) throw new Error("请选择接口协议");
      const authType = $("providerAuthType").value;
      if (authType === "custom_header" && !$("providerAuthHeader").value.trim()) throw new Error("请填写自定义 Header 名");
      if (authType === "query_param" && !$("providerAuthParam").value.trim()) throw new Error("请填写 Query 参数名");
      if (authType === "basic" && !$("providerAuthUsername").value.trim()) throw new Error("Basic Auth 需要用户名");
      const currentProvider = state.resources.providers.find(item => item.provider_id === $("providerId").value.trim());
      const savedProvider = await post("/api/v2/resources/providers", {
        provider_id: $("providerId").value.trim(), label: $("providerLabel").value.trim(), base_url: $("providerUrl").value.trim(), protocols: [protocol],
        auth_type: authType,
        auth_header: authType === "custom_header" ? $("providerAuthHeader").value.trim() : authType === "query_param" ? $("providerAuthParam").value.trim() : "",
        auth_prefix: authType === "bearer" ? "Bearer" : ["custom_header", "token"].includes(authType) ? $("providerAuthPrefix").value.trim() : "",
        metadata: { ...(currentProvider?.metadata || {}), username: $("providerAuthUsername").value.trim(), query_param: $("providerAuthParam").value.trim() },
        api_key: authType === "none" ? "" : $("providerKey").value,
        enabled: $("providerEnabled").checked,
      });
      $("providerForm").classList.add("hidden"); await refreshManagement(); editModel("", savedProvider.provider_id); toast("供应商已保存，请添加第一个模型");
    } catch (error) { toast(error.message); }
  });
  document.querySelector("[data-cancel-provider]").addEventListener("click", () => $("providerForm").classList.add("hidden"));
  $("providerAuthType").addEventListener("change", syncProviderAuth);
  $("providerAuthHeader").addEventListener("input", syncProviderAuth);
  $("providerAuthPrefix").addEventListener("input", syncProviderAuth);
  $("providerAuthParam").addEventListener("input", syncProviderAuth);
  $("providerAuthUsername").addEventListener("input", syncProviderAuth);
  [$("providerProtocolOpenAI"), $("providerProtocolClaude")].forEach(input => input.addEventListener("change", () => { if ($("providerProtocolClaude").checked && !$("providerProtocolOpenAI").checked && $("providerAuthType").value === "bearer") $("providerAuthType").value = "api_key"; syncProviderAuth(); }));
  document.querySelectorAll("[data-provider-preset]").forEach(button => button.addEventListener("click", () => {
    const preset = button.dataset.providerPreset;
    const values = {
      openai: { id: "openai", label: "OpenAI", url: "https://api.openai.com/v1", protocols: ["openai"], auth: "bearer" },
      anthropic: { id: "anthropic", label: "Anthropic", url: "https://api.anthropic.com", protocols: ["claude"], auth: "api_key" },
      openrouter: { id: "openrouter", label: "OpenRouter", url: "https://openrouter.ai/api/v1", protocols: ["openai"], auth: "bearer" },
      ollama: { id: "ollama", label: "Ollama（本地）", url: "http://localhost:11434/v1", protocols: ["openai"], auth: "none" },
      custom: { id: "", label: "", url: "", protocols: ["openai"], auth: "bearer" },
    }[preset];
    if (!values) return;
    $("providerId").value = values.id; $("providerLabel").value = values.label; $("providerUrl").value = values.url;
    $("providerProtocolOpenAI").checked = values.protocols.includes("openai"); $("providerProtocolClaude").checked = values.protocols.includes("claude");
    $("providerAuthType").value = values.auth; $("providerAuthPrefix").value = values.auth === "token" ? "Token" : "";
    document.querySelectorAll("[data-provider-preset]").forEach(item => item.classList.toggle("active", item === button));
    syncProviderAuth();
    if (preset === "custom") $("providerId").focus();
  }));
  $("modelForm").addEventListener("submit", async event => {
    event.preventDefault();
    try {
      const alias = $("modelAlias").value.trim();
      if (!alias) throw new Error("请填写显示名称");
      if (!MODEL_ALIAS_PATTERN.test(alias)) throw new Error("显示名称只能包含字母、数字、点号、连字符和下划线");
      const providerId = $("modelProvider").value;
      if (!providerId) throw new Error("请选择所属供应商");
      const protocol = $("modelProtocol").value;
      if (!protocol) throw new Error("请选择接口协议");
      const modelName = physicalModelName();
      if (!modelName) throw new Error("请选择或输入物理模型名");
      await post("/api/v2/resources/models", {
        alias,
        provider_id: providerId,
        protocol,
        model: modelName,
        capabilities: csv($("modelCapabilities").value),
        temperature: $("modelTemperature").value === "" ? null : Number($("modelTemperature").value),
        max_tokens: $("modelMaxTokens").value === "" ? null : Number($("modelMaxTokens").value),
        context_window_tokens: $("modelContextWindow").value === "" ? null : Number($("modelContextWindow").value),
        enabled: $("modelEnabled").checked,
      });
      $("modelForm").classList.add("hidden");
      await refreshManagement();
      toast("模型已保存");
    } catch (error) { toast(error.message); }
  });
  document.querySelector("[data-cancel-model]").addEventListener("click", () => $("modelForm").classList.add("hidden"));
  $("modelName").addEventListener("change", () => syncPhysicalModelMode({ focus: true }));
  $("modelNameCustom").addEventListener("input", syncModelAliasDefault);
  $("modelProvider").addEventListener("change", () => {
    const provider = state.resources.providers.find(item => item.provider_id === $("modelProvider").value);
    if (!provider) return;
    setModelProtocolOptions(provider);
    resetPhysicalModelPicker();
    discoverProviderModels(provider);
  });
  $("modelProtocol").addEventListener("change", () => {
    const provider = state.resources.providers.find(item => item.provider_id === $("modelProvider").value);
    if (provider) discoverProviderModels(provider, physicalModelName());
  });
  $("refreshModelList").addEventListener("click", () => {
    const provider = state.resources.providers.find(item => item.provider_id === $("modelProvider").value);
    if (provider) discoverProviderModels(provider, physicalModelName());
  });

  $("newNode").addEventListener("click", () => editNode());
  $("nodeForm").addEventListener("submit", async event => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector('button[type="submit"]');
    if (submit.disabled) return;
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    const originalLabel = submit.textContent;
    submit.textContent = "保存中…";
    try {
      await post("/api/v2/nodes", {
        node_type: $("nodeType").value,
        label: $("nodeLabel").value,
        description: $("nodeDescription").value,
        input_fields: csv($("nodeInputs").value),
        output_fields: csv($("nodeOutputs").value),
        capabilities: csv($("nodeCapabilities").value),
        default_model: $("nodeDefaultModel").value,
      });
      $("nodeForm").classList.add("hidden");
      await refreshManagement();
      toast("自定义节点已保存");
    } catch (error) {
      toast(error.message);
    } finally {
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
      submit.textContent = originalLabel;
    }
  });
  document.querySelector("[data-cancel-node]").addEventListener("click", () => $("nodeForm").classList.add("hidden"));

  $("newWorkflow").addEventListener("click", () => { state.editingWorkflow = newWorkflowValue(); renderWorkflowList(); renderWorkflowEditor(); });
  $("addWorkflowNode").addEventListener("click", () => { syncWorkflowForm(); const index = state.editingWorkflow.nodes.length + 1; state.editingWorkflow.nodes.push({ node_id: `step-${index}`, node_type: state.catalog.nodes[0]?.node_type || "tool", depends_on: [], model_alias: "", prompt_template: "", on_failure: "human", config: {}, position: [0, index * 100] }); renderWorkflowNodes(); });
  $("workflowForm").addEventListener("submit", async event => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector('button[type="submit"]');
    if (submit.disabled) return;
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    const originalLabel = submit.textContent;
    submit.textContent = "保存中…";
    try {
      syncWorkflowForm();
      if (!state.editingWorkflow.nodes.length) throw new Error("工作流至少需要一个节点");
      const saved = await post("/api/v2/workflows", state.editingWorkflow);
      state.selectedWorkflowId = saved.workflow_id;
      state.editingWorkflow = clone(saved);
      await refreshManagement();
      toast("工作流和模型关联已保存");
    } catch (error) {
      toast(error.message);
    } finally {
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
      submit.textContent = originalLabel;
    }
  });
  async function deleteWorkflow() {
    const id = state.editingWorkflow?.workflow_id;
    const button = $("deleteWorkflow");
    if (!id || button.disabled) return;
    const confirmed = await confirmAction({ title: "删除工作流", message: `确定删除“${id}”吗？内置工作流不会被删除。` });
    if (!confirmed) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      await remove(`/api/v2/workflows/${encodeURIComponent(id)}`);
      state.editingWorkflow = null;
      await refreshManagement();
      toast("工作流已删除");
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
  $("deleteWorkflow").addEventListener("click", deleteWorkflow);

  if (new URLSearchParams(location.search).get("desktop") === "1") {
    document.body.classList.add("desktop");
    $("titlebar").hidden = false;
    const callWindow = name => { const api = window.pywebview?.api; if (api && typeof api[name] === "function") api[name](); };
    $("winMinimize").addEventListener("click", () => callWindow("minimize"));
    $("winMaximize").addEventListener("click", () => callWindow("toggle_maximize"));
    $("winClose").addEventListener("click", () => callWindow("close"));
  }

  document.querySelectorAll("[data-icon]").forEach(element => {
    element.innerHTML = svgIcon(element.dataset.icon);
  });
  document.querySelectorAll(".dialog-head .icon-button").forEach(button => {
    button.innerHTML = svgIcon("x");
  });
  [
    ["newProvider", "plus"], ["newRole", "plus"], ["newCollaborationTask", "plus"],
    ["newNode", "plus"], ["newWorkflow", "plus"], ["addWorkflowNode", "plus"],
  ].forEach(([id, name]) => {
    const button = $(id);
    if (!button || button.querySelector(".ui-icon")) return;
    const label = button.textContent.trim().replace(/^＋\s*/, "");
    button.innerHTML = `${svgIcon(name)}<span>${label}</span>`;
  });

  const savedTheme = localStorage.getItem(THEME_KEY);
  if (savedTheme === "dark") document.body.classList.add("dark");
  init();
})();
