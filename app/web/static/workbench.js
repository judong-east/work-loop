(() => {
  const state = {
    projects: [], sessions: [], project: null, session: null, mode: "chat",
    catalog: { nodes: [], workflows: [] }, strategies: [], resources: { providers: [], models: [], health: [] },
    selectedWorkflowId: "default-task", selectedStrategy: "guided-develop", editingProjectId: "", editingWorkflow: null, testingProviders: new Set(),
    pendingMessage: null, sendSequence: 0,
  };
  const THEME_KEY = "workloop-theme-minimal-v1";
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const csv = value => String(value || "").split(",").map(item => item.trim()).filter(Boolean);
  const clone = value => JSON.parse(JSON.stringify(value));
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
  const toast = message => { $("toast").textContent = message; $("toast").classList.add("show"); setTimeout(() => $("toast").classList.remove("show"), 2600); };

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
  }

  function scrollMessageListToBottom() {
    const list = $("messageList");
    if (!list) return;
    const scroll = () => { list.scrollTop = list.scrollHeight; };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(scroll);
    else scroll();
  }

  function renderProjects() {
    $("projectList").innerHTML = state.projects.length ? state.projects.map(project => {
      const active = state.project?.project_id === project.project_id;
      const sessions = active ? state.sessions : [];
      return `<div class="project-group"><button type="button" class="project-item ${active ? "active" : ""}" data-project="${esc(project.project_id)}"><span class="project-icon">${svgIcon("folder")}</span><span><strong>${esc(project.name)}</strong><span>${active ? `${sessions.length} 个会话 · ${project.workspace_path ? "工作区已连接" : "未连接工作区"}` : "点击查看"}</span></span></button>${active ? `<div class="session-list">${sessions.map(session => `<button type="button" class="session-item ${state.session?.session_id === session.session_id ? "active" : ""}" data-session="${esc(session.session_id)}"><span class="session-icon">${svgIcon(session.mode === "task" ? "workflow" : "message")}</span><strong>${esc(session.title)}</strong></button>`).join("")}<button type="button" class="session-item new" data-new-session><span class="session-icon">${svgIcon("plus")}</span><strong>新会话</strong></button></div>` : ""}</div>`;
    }).join("") : `<div class="inspector-empty compact-empty"><strong>还没有项目</strong><p>先创建项目，把工作上下文固定下来。</p></div>`;
    document.querySelectorAll("[data-project]").forEach(button => button.addEventListener("click", () => selectProject(button.dataset.project)));
    document.querySelectorAll("[data-session]").forEach(button => button.addEventListener("click", () => {
      state.session = state.sessions.find(item => item.session_id === button.dataset.session) || null;
      state.mode = state.session?.mode || "chat";
      if (state.session?.workflow_id) state.selectedWorkflowId = state.session.workflow_id;
      if (state.session?.policy?.strategy) state.selectedStrategy = state.session.policy.strategy;
      renderMode(); renderProjects(); renderSessions();
    }));
    document.querySelectorAll("[data-new-session]").forEach(button => button.addEventListener("click", async () => {
      try {
        state.session = await post(`/api/v2/projects/${state.project.project_id}/sessions`, {
          title: "新的会话", mode: state.mode,
          workflow_id: state.mode === "task" ? state.selectedWorkflowId : "",
          policy: state.mode === "task" ? { strategy: state.selectedStrategy } : undefined,
        });
        state.sessions.unshift(state.session); renderProjects(); renderSessions();
      } catch (error) { toast(error.message); }
    }));
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
    renderWorkflowPicker(); renderFlow();
  }

  function renderSessions() {
    $("projectName").textContent = state.project?.name || "选择一个项目";
    $("sessionTitle").textContent = state.session?.title || "新的会话";
    $("composerProjectContext").textContent = state.project?.name || "选择项目";
    const messages = [...(state.session?.messages || [])];
    const pending = state.pendingMessage;
    const pendingVisible = pending && pending.projectId === state.project?.project_id &&
      (!state.session || !pending.sessionId || pending.sessionId === state.session.session_id);
    if (pendingVisible) messages.push({ role: "user", content: pending.content, pending: true });
    $("welcome").classList.toggle("hidden", messages.length > 0);
    let stack = $("messageStack");
    if (!stack) { stack = document.createElement("div"); stack.id = "messageStack"; stack.className = "message-stack"; $("messageList").appendChild(stack); }
    stack.innerHTML = messages.map(message => `<article class="message ${message.role === "user" ? "user" : "assistant"}${message.pending ? " pending" : ""}"><span class="message-avatar">${message.role === "user" ? "你" : (message.node_id ? "N" : "W")}</span><div class="message-body"><div class="message-meta">${message.node_id ? esc(message.node_id) : (message.role === "user" ? "你" : "Workloop")}${message.pending ? '<span class="message-status">发送中…</span>' : ""}</div>${esc(message.content)}</div></article>`).join("");
    scrollMessageListToBottom();
    syncSendButton();
    renderFlow();
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

  async function selectProject(projectId) {
    try {
      state.project = state.projects.find(project => project.project_id === projectId) || null;
      state.sessions = await api(`/api/v2/projects/${encodeURIComponent(projectId)}/sessions`);
      state.session = state.sessions[0] || null;
      if (!state.session) state.session = await post(`/api/v2/projects/${encodeURIComponent(projectId)}/sessions`, { title: "新的会话", mode: state.mode, workflow_id: state.mode === "task" ? state.selectedWorkflowId : "", policy: state.mode === "task" ? { strategy: state.selectedStrategy } : undefined });
      state.mode = state.session.mode || "chat";
      if (state.session.workflow_id) state.selectedWorkflowId = state.session.workflow_id;
      if (state.session.policy?.strategy) state.selectedStrategy = state.session.policy.strategy;
      renderMode(); renderProjects(); renderSessions(); $("rail").classList.remove("open");
      document.body.dataset.projectId = state.project.project_id;
      document.body.dataset.projectName = state.project.name;
      window.dispatchEvent(new CustomEvent("workloop:project-selected", {
        detail: { projectId: state.project.project_id, projectName: state.project.name },
      }));
    } catch (error) { toast(error.message); }
  }

  function send() {
    const input = $("messageInput");
    const content = input.value.trim();
    if (!content || !state.project) { if (!state.project) toast("请先选择或创建项目"); return; }
    if (state.pendingMessage) return;
    const pending = {
      id: ++state.sendSequence,
      projectId: state.project.project_id,
      sessionId: state.session?.session_id || "",
      mode: state.mode,
      workflowId: state.mode === "task" ? state.selectedWorkflowId : "",
      content,
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
      let session = matches(state.session) ? state.session : state.sessions.find(matches);
      if (!session) {
        session = await post(`/api/v2/projects/${pending.projectId}/sessions`, {
          title: pending.content.slice(0, 36), mode: pending.mode, workflow_id: workflowId,
          policy: pending.mode === "task" ? { strategy: state.selectedStrategy } : undefined,
        });
      }
      session = await post(`/api/v2/sessions/${session.session_id}/messages`, { content: pending.content });
      if (pending.mode === "task") session = await post(`/api/v2/sessions/${session.session_id}/run`, {});
      if (state.project?.project_id === pending.projectId) {
        state.session = session;
        const index = state.sessions.findIndex(item => item.session_id === session.session_id);
        if (index >= 0) state.sessions[index] = session;
        else state.sessions.unshift(session);
      }
      if (state.pendingMessage?.id === pending.id) state.pendingMessage = null;
      renderProjects(); renderSessions();
    } catch (error) {
      if (state.pendingMessage?.id === pending.id) state.pendingMessage = null;
      // Keep a failed request recoverable without overwriting a newer draft.
      const input = $("messageInput");
      if (!input.value.trim()) { input.value = pending.content; resizeComposer(); input.focus(); }
      syncSendButton(); renderProjects(); renderSessions(); toast(error.message);
    }
  }

  async function refreshManagement() {
    [state.catalog, state.resources, state.strategies] = await Promise.all([api("/api/v2/catalog"), api("/api/v2/resources"), api("/api/v2/strategies")]);
    const modelCount = state.resources.models.length;
    $("resourceHint").textContent = modelCount ? `${modelCount} 个模型可用` : "尚未配置模型";
    $("modelLabel").textContent = modelCount ? `${modelCount} 个模型` : "尚未配置模型";
    $("projectDefaultModel").innerHTML = option("", "自动选择模型") + state.resources.models.map(item => option(item.alias, item.alias)).join("");
    renderWorkflowPicker(); renderProviders(); renderNodes(); renderWorkflowList();
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
    if ($("providerCount")) $("providerCount").textContent = `${providerCount} 个供应商`;
    $("providerList").innerHTML = state.resources.providers.length ? state.resources.providers.map(item => {
      const models = state.resources.models.filter(model => model.provider_id === item.provider_id);
      const providerHealth = health[item.provider_id] || {};
      const status = providerHealthView(item, providerHealth);
      const checking = state.testingProviders.has(item.provider_id);
      const checks = status.checks.map(check => `<span class="protocol-check ${check.ok ? "ok" : "failed"}"><strong>${esc(protocolLabel(check.protocol))}</strong><span>${check.ok ? `${esc(check.latency_ms)} ms` : esc(providerHealthLabel(check.error_type))}</span></span>`).join("");
      return `<section class="provider-group"><header class="provider-group-head"><span class="provider-mark">${esc(item.label.slice(0, 1).toUpperCase())}</span><div class="provider-copy"><strong>${esc(item.label)}</strong><span>${esc(item.base_url)}</span></div><span class="health-status ${status.tone}"><i></i>${esc(status.label)}</span>${dataActions("provider", item.provider_id)}</header><div class="provider-meta"><span>${(item.protocols || ["openai"]).map(protocol => esc(protocolLabel(protocol))).join(" / ")}</span><span>${esc(authLabel(item.auth_type || "bearer"))}</span><span>${providerHealth.configured ? "认证已配置" : item.auth_type === "none" ? "无需密钥" : "缺少密钥"}</span><span>${models.length} 个模型</span></div><div class="provider-health"><div class="provider-health-copy" title="${esc(status.detail)}"><span>${esc(status.detail)}</span>${status.checkedAt ? `<time>最近测试 ${esc(status.checkedAt)}</time>` : ""}${checks ? `<div class="protocol-checks">${checks}</div>` : ""}</div><button class="button quiet compact test-provider" type="button" data-test-provider="${esc(item.provider_id)}" ${checking || !item.enabled ? "disabled" : ""}>${checking ? "测试中…" : "测试连接"}</button></div><div class="provider-models"><div class="provider-models-head"><strong>模型</strong><button class="button quiet compact" type="button" data-add-model="${esc(item.provider_id)}">${svgIcon("plus")} 添加模型</button></div>${models.length ? models.map(model => `<div class="provider-model-row"><span class="model-glyph">M</span><div><strong>${esc(model.alias)}</strong><span>${esc(model.model)} · ${esc(protocolLabel(model.protocol || item.protocols?.[0] || "openai"))}</span></div><span class="state-tag">${model.enabled ? "已启用" : "已停用"}</span>${dataActions("model", model.alias)}</div>`).join("") : `<div class="provider-model-empty">该供应商还没有模型</div>`}</div></section>`;
    }).join("") : `<div class="list-empty">还没有供应商，先添加供应商再配置模型。</div>`;
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
    $("providerProtocolOpenAI").checked = item ? item.protocols.includes("openai") : true;
    $("providerProtocolClaude").checked = item ? item.protocols.includes("claude") : false;
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
    if (!confirm(`删除供应商 ${providerId}？`)) return;
    try { await remove(`/api/v2/resources/providers/${encodeURIComponent(providerId)}`); await refreshManagement(); toast("供应商已删除"); }
    catch (error) { toast(error.message); }
  }

  function editModel(alias = "", providerId = "") {
    if (!state.resources.providers.length) { toast("请先添加供应商"); return; }
    const item = state.resources.models.find(model => model.alias === alias);
    const selectedProviderId = item?.provider_id || providerId;
    const provider = state.resources.providers.find(value => value.provider_id === selectedProviderId);
    if (!provider) { toast("未找到模型所属供应商"); return; }
    $("modelForm").reset(); $("modelForm").classList.remove("hidden"); $("providerForm").classList.add("hidden");
    $("modelFormTitle").textContent = item ? "编辑模型" : "添加模型";
    $("modelAlias").value = item?.alias || ""; $("modelAlias").readOnly = Boolean(item);
    $("modelProvider").innerHTML = option(provider.provider_id, provider.label, provider.provider_id);
    $("modelProtocol").innerHTML = provider.protocols.map(protocol => option(protocol, protocol === "claude" ? "Claude · Messages" : "OpenAI · Chat Completions", item?.protocol || provider.protocols[0])).join("");
    $("modelName").value = item?.model || ""; $("modelCapabilities").value = (item?.capabilities || ["general"]).join(", ");
    $("modelTemperature").value = item?.temperature ?? ""; $("modelMaxTokens").value = item?.max_tokens ?? "";
    $("modelEnabled").checked = item?.enabled ?? true;
  }

  async function deleteModel(alias) {
    if (!confirm(`删除模型别名 ${alias}？`)) return;
    try { await remove(`/api/v2/resources/models/${encodeURIComponent(alias)}`); await refreshManagement(); toast("模型已删除"); }
    catch (error) { toast(error.message); }
  }

  function renderNodes() {
    $("nodeList").innerHTML = state.catalog.nodes.map(item => `<div class="data-row"><span class="node-glyph">N</span><div><strong>${esc(item.label)}</strong><span>${esc(item.node_type)} · ${esc(item.default_model || "自动选择模型")}</span></div><span class="state-tag">${item.output_fields.length} 个输出</span>${dataActions("node", item.node_type, !item.builtin)}</div>`).join("");
    document.querySelectorAll("[data-edit-node]").forEach(button => button.addEventListener("click", () => editNode(button.dataset.editNode)));
    document.querySelectorAll("[data-delete-node]").forEach(button => button.addEventListener("click", () => deleteNode(button.dataset.deleteNode)));
    $("nodeDefaultModel").innerHTML = option("", "自动选择") + state.resources.models.map(item => option(item.alias, item.alias)).join("");
  }

  function editNode(nodeType = "") {
    const item = state.catalog.nodes.find(node => node.node_type === nodeType);
    if (item?.builtin) return;
    $("nodeForm").reset(); $("nodeForm").classList.remove("hidden");
    $("nodeFormTitle").textContent = item ? "编辑自定义节点" : "新建自定义节点";
    $("nodeType").value = item?.node_type || ""; $("nodeType").readOnly = Boolean(item);
    $("nodeLabel").value = item?.label || ""; $("nodeDescription").value = item?.description || "";
    $("nodeInputs").value = (item?.input_fields || []).join(", "); $("nodeOutputs").value = (item?.output_fields || []).join(", ");
    $("nodeCapabilities").value = (item?.capabilities || ["general"]).join(", "); $("nodeDefaultModel").value = item?.default_model || "";
  }

  async function deleteNode(nodeType) {
    if (!confirm(`删除自定义节点 ${nodeType}？`)) return;
    try { await remove(`/api/v2/nodes/${encodeURIComponent(nodeType)}`); await refreshManagement(); toast("节点已删除"); }
    catch (error) { toast(error.message); }
  }

  function renderWorkflowList() {
    const workflows = state.catalog.workflows || [];
    if (!state.editingWorkflow) state.editingWorkflow = clone(workflows.find(item => item.workflow_id === state.selectedWorkflowId) || workflows[0] || newWorkflowValue());
    $("workflowList").innerHTML = workflows.map(item => `<button type="button" class="workflow-list-item ${state.editingWorkflow?.workflow_id === item.workflow_id ? "active" : ""}" data-workflow="${esc(item.workflow_id)}"><strong>${esc(item.label)}</strong><span>${item.nodes.length} 个节点${item.builtin ? " · 内置" : ""}</span></button>`).join("");
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
    state.editingWorkflow.nodes = [...document.querySelectorAll("[data-workflow-node]")].map((row, index) => ({
      node_id: row.querySelector('[data-node-field="node_id"]').value.trim(),
      node_type: row.querySelector('[data-node-field="node_type"]').value,
      model_alias: row.querySelector('[data-node-field="model_alias"]').value,
      on_failure: row.querySelector('[data-node-field="on_failure"]').value,
      depends_on: csv(row.querySelector('[data-node-field="depends_on"]').value),
      prompt_template: row.querySelector('[data-node-field="prompt_template"]').value,
      config: {}, position: [0, index * 100],
    }));
  }

  function setManagementTab(name) {
    document.querySelectorAll("[data-management-tab]").forEach(button => { const active = button.dataset.managementTab === name; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); });
    document.querySelectorAll("[data-management-panel]").forEach(panel => panel.classList.toggle("hidden", panel.dataset.managementPanel !== name));
  }

  async function openManagement() {
    try { await refreshManagement(); $("managementDialog").showModal(); }
    catch (error) { toast(error.message); }
  }

  async function init() {
    try {
      await refreshManagement(); state.projects = await api("/api/v2/projects"); renderProjects();
      if (state.projects[0]) await selectProject(state.projects[0].project_id); else { renderMode(); renderSessions(); }
    } catch (error) { toast(error.message); }
  }

  function openProjectDialog(project = null) {
    state.editingProjectId = project?.project_id || "";
    $("projectDialogTitle").textContent = project ? "项目设置" : "新建项目";
    $("projectInput").value = project?.name || "";
    $("workspaceInput").value = project?.workspace_path || "";
    $("projectDefaultModel").value = project?.default_model || "";
    $("validationCommandsInput").value = (project?.validation_commands || []).map(command => JSON.stringify(command)).join("\n");
    $("instructionsInput").value = project?.instructions || "";
    $("projectDialog").showModal();
  }

  function closeProjectDialog() {
    state.editingProjectId = "";
    $("projectForm").reset();
    $("projectDialog").close();
  }

  $("newProject").addEventListener("click", () => openProjectDialog());
  $("editProject").addEventListener("click", () => state.project ? openProjectDialog(state.project) : toast("请先选择项目"));
  document.querySelectorAll("[data-close-project]").forEach(button => {
    button.addEventListener("click", closeProjectDialog);
  });
  $("projectDialog").addEventListener("cancel", () => {
    state.editingProjectId = "";
    $("projectForm").reset();
  });
  $("projectForm").addEventListener("submit", async event => {
    event.preventDefault();
    try {
      const validationCommands = $("validationCommandsInput").value.split(/\r?\n/).map(line => line.trim()).filter(Boolean).map((line, index) => {
        let value; try { value = JSON.parse(line); } catch (_) { throw new Error(`第 ${index + 1} 条验证命令不是合法 JSON`); }
        if (!Array.isArray(value) || !value.length || value.some(item => typeof item !== "string" || !item)) throw new Error(`第 ${index + 1} 条验证命令必须是非空字符串数组`);
        return value;
      });
      const project = await post(state.editingProjectId ? `/api/v2/projects/${encodeURIComponent(state.editingProjectId)}` : "/api/v2/projects", {
        name: $("projectInput").value,
        workspace_path: $("workspaceInput").value.trim(),
        default_model: $("projectDefaultModel").value,
        validation_commands: validationCommands,
        instructions: $("instructionsInput").value,
      });
      if (state.editingProjectId) state.projects = state.projects.map(item => item.project_id === project.project_id ? project : item);
      else state.projects.unshift(project);
      closeProjectDialog(); await selectProject(project.project_id);
    }
    catch (error) { toast(error.message); }
  });
  $("sendMessage").addEventListener("click", send);
  $("messageInput").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); } });
  $("messageInput").addEventListener("input", event => { resizeComposer(); syncSendButton(); });
  document.querySelectorAll("[data-mode]").forEach(button => button.addEventListener("click", () => {
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

  $("newProvider").addEventListener("click", () => editProvider());
  $("providerForm").addEventListener("submit", async event => {
    event.preventDefault();
    try {
      const protocols = [["providerProtocolOpenAI", "openai"], ["providerProtocolClaude", "claude"]].filter(([id]) => $(id).checked).map(([, value]) => value);
      if (!protocols.length) throw new Error("请至少选择一种协议");
      const authType = $("providerAuthType").value;
      if (authType === "custom_header" && !$("providerAuthHeader").value.trim()) throw new Error("请填写自定义 Header 名");
      if (authType === "query_param" && !$("providerAuthParam").value.trim()) throw new Error("请填写 Query 参数名");
      if (authType === "basic" && !$("providerAuthUsername").value.trim()) throw new Error("Basic Auth 需要用户名");
      const currentProvider = state.resources.providers.find(item => item.provider_id === $("providerId").value.trim());
      const savedProvider = await post("/api/v2/resources/providers", {
        provider_id: $("providerId").value.trim(), label: $("providerLabel").value.trim(), base_url: $("providerUrl").value.trim(), protocols,
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
  $("modelForm").addEventListener("submit", async event => { event.preventDefault(); try { await post("/api/v2/resources/models", { alias: $("modelAlias").value, provider_id: $("modelProvider").value, protocol: $("modelProtocol").value, model: $("modelName").value, capabilities: csv($("modelCapabilities").value), temperature: $("modelTemperature").value === "" ? null : Number($("modelTemperature").value), max_tokens: $("modelMaxTokens").value === "" ? null : Number($("modelMaxTokens").value), enabled: $("modelEnabled").checked }); $("modelForm").classList.add("hidden"); await refreshManagement(); toast("模型已保存"); } catch (error) { toast(error.message); } });
  document.querySelector("[data-cancel-model]").addEventListener("click", () => $("modelForm").classList.add("hidden"));

  $("newNode").addEventListener("click", () => editNode());
  $("nodeForm").addEventListener("submit", async event => { event.preventDefault(); try { await post("/api/v2/nodes", { node_type: $("nodeType").value, label: $("nodeLabel").value, description: $("nodeDescription").value, input_fields: csv($("nodeInputs").value), output_fields: csv($("nodeOutputs").value), capabilities: csv($("nodeCapabilities").value), default_model: $("nodeDefaultModel").value }); $("nodeForm").classList.add("hidden"); await refreshManagement(); toast("自定义节点已保存"); } catch (error) { toast(error.message); } });
  document.querySelector("[data-cancel-node]").addEventListener("click", () => $("nodeForm").classList.add("hidden"));

  $("newWorkflow").addEventListener("click", () => { state.editingWorkflow = newWorkflowValue(); renderWorkflowList(); renderWorkflowEditor(); });
  $("addWorkflowNode").addEventListener("click", () => { syncWorkflowForm(); const index = state.editingWorkflow.nodes.length + 1; state.editingWorkflow.nodes.push({ node_id: `step-${index}`, node_type: state.catalog.nodes[0]?.node_type || "tool", depends_on: [], model_alias: "", prompt_template: "", on_failure: "human", config: {}, position: [0, index * 100] }); renderWorkflowNodes(); });
  $("workflowForm").addEventListener("submit", async event => { event.preventDefault(); try { syncWorkflowForm(); if (!state.editingWorkflow.nodes.length) throw new Error("工作流至少需要一个节点"); const saved = await post("/api/v2/workflows", state.editingWorkflow); state.selectedWorkflowId = saved.workflow_id; state.editingWorkflow = clone(saved); await refreshManagement(); toast("工作流和模型关联已保存"); } catch (error) { toast(error.message); } });
  $("deleteWorkflow").addEventListener("click", async () => { const id = state.editingWorkflow?.workflow_id; if (!id || !confirm(`删除工作流 ${id}？`)) return; try { await remove(`/api/v2/workflows/${encodeURIComponent(id)}`); state.editingWorkflow = null; await refreshManagement(); toast("工作流已删除"); } catch (error) { toast(error.message); } });

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
  if (savedTheme !== "light") document.body.classList.add("dark");
  init();
})();
