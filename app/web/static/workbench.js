(() => {
  const state = {
    projects: [], sessions: [], project: null, session: null, mode: "chat",
    catalog: { nodes: [], workflows: [] }, strategies: [], resources: { providers: [], models: [], health: [] },
    selectedWorkflowId: "default-task", selectedStrategy: "guided-develop", editingProjectId: "", editingWorkflow: null, testingProviders: new Set(),
  };
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const csv = value => String(value || "").split(",").map(item => item.trim()).filter(Boolean);
  const clone = value => JSON.parse(JSON.stringify(value));
  const option = (value, label, selected = "") => `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`;
  const api = async (path, options = {}) => {
    const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
    return payload;
  };
  const post = (path, value) => api(path, { method: "POST", body: JSON.stringify(value) });
  const remove = path => api(path, { method: "DELETE" });
  const toast = message => { $("toast").textContent = message; $("toast").classList.add("show"); setTimeout(() => $("toast").classList.remove("show"), 2600); };

  function renderProjects() {
    $("projectList").innerHTML = state.projects.length ? state.projects.map(project => {
      const active = state.project?.project_id === project.project_id;
      const sessions = active ? state.sessions : [];
      return `<div class="project-group"><button type="button" class="project-item ${active ? "active" : ""}" data-project="${esc(project.project_id)}"><span class="project-icon">⌂</span><span><strong>${esc(project.name)}</strong><span>${active ? `${sessions.length} 个会话 · ${project.workspace_path ? "工作区已连接" : "未连接工作区"}` : "点击查看"}</span></span></button>${active ? `<div class="session-list">${sessions.map(session => `<button type="button" class="session-item ${state.session?.session_id === session.session_id ? "active" : ""}" data-session="${esc(session.session_id)}"><span>${session.mode === "task" ? "▤" : "·"}</span><strong>${esc(session.title)}</strong></button>`).join("")}<button type="button" class="session-item new" data-new-session><span>＋</span><strong>新会话</strong></button></div>` : ""}</div>`;
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

  function renderMode() {
    document.querySelectorAll("[data-mode]").forEach(item => item.classList.toggle("active", item.dataset.mode === state.mode));
    $("composerHint").textContent = state.mode === "task" ? "按所选工作流执行，节点结果自动写入上下文" : "Enter 发送，Shift + Enter 换行";
    renderWorkflowPicker(); renderFlow();
  }

  function renderSessions() {
    $("projectName").textContent = state.project?.name || "选择一个项目";
    $("sessionTitle").textContent = state.session?.title || "新的会话";
    const messages = state.session?.messages || [];
    $("welcome").classList.toggle("hidden", messages.length > 0);
    let stack = $("messageStack");
    if (!stack) { stack = document.createElement("div"); stack.id = "messageStack"; stack.className = "message-stack"; $("messageList").appendChild(stack); }
    stack.innerHTML = messages.map(message => `<article class="message ${message.role === "user" ? "user" : "assistant"}"><span class="message-avatar">${message.role === "user" ? "你" : (message.node_id ? "N" : "W")}</span><div class="message-body"><div class="message-meta">${message.node_id ? esc(message.node_id) : (message.role === "user" ? "你" : "Workloop")}</div>${esc(message.content)}</div></article>`).join("");
    $("conversation").scrollTop = $("conversation").scrollHeight;
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
    $("flowList").innerHTML = nodes.map((node, index) => {
      const status = statuses[node.node_id] || (current?.node_id === node.node_id ? "current" : "pending");
      const statusText = { pending: "等待", current: "执行中", skipped: "跳过", completed: "完成", failed: "失败" }[status] || status;
      return `<div class="flow-node ${["completed", "skipped"].includes(status) ? "done" : status === "failed" ? "failed" : status === "current" ? "current" : ""}"><span class="node-marker">${status === "completed" ? "✓" : index + 1}</span><div class="node-copy"><strong>${esc(labels[node.node_type] || node.node_type)}</strong><span class="node-binding">${esc(node.model_alias || "自动选择模型")}</span><span>${esc(node.node_id)} · ${esc(statusText)}</span></div></div>`;
    }).join("");
    $("contextView").textContent = JSON.stringify(session.context || {}, null, 2);
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

  async function send() {
    const content = $("messageInput").value.trim();
    if (!content || !state.project) { if (!state.project) toast("请先选择或创建项目"); return; }
    $("sendMessage").disabled = true;
    try {
      const workflowId = state.mode === "task" ? state.selectedWorkflowId : "";
      if (!state.session || state.session.mode !== state.mode || (state.mode === "task" && state.session.workflow_id !== workflowId)) {
        state.session = await post(`/api/v2/projects/${state.project.project_id}/sessions`, { title: content.slice(0, 36), mode: state.mode, workflow_id: workflowId, policy: state.mode === "task" ? { strategy: state.selectedStrategy } : undefined });
        state.sessions.unshift(state.session);
      }
      state.session = await post(`/api/v2/sessions/${state.session.session_id}/messages`, { content });
      if (state.mode === "task") state.session = await post(`/api/v2/sessions/${state.session.session_id}/run`, {});
      $("messageInput").value = ""; renderProjects(); renderSessions();
    } catch (error) { toast(error.message); } finally { $("sendMessage").disabled = false; }
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
    return `<span class="row-actions"><button class="icon-button small" type="button" data-edit-${kind}="${esc(id)}" aria-label="编辑" title="编辑">✎</button><button class="icon-button small danger-icon" type="button" data-delete-${kind}="${esc(id)}" aria-label="删除" title="删除">×</button></span>`;
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
    const authLabel = value => ({ bearer: "Bearer", api_key: "x-api-key", custom_header: "自定义 Header", none: "无需认证" }[value] || value);
    $("providerList").innerHTML = state.resources.providers.length ? state.resources.providers.map(item => {
      const models = state.resources.models.filter(model => model.provider_id === item.provider_id);
      const providerHealth = health[item.provider_id] || {};
      const status = providerHealthView(item, providerHealth);
      const checking = state.testingProviders.has(item.provider_id);
      const checks = status.checks.map(check => `<span class="protocol-check ${check.ok ? "ok" : "failed"}"><strong>${esc(protocolLabel(check.protocol))}</strong><span>${check.ok ? `${esc(check.latency_ms)} ms` : esc(providerHealthLabel(check.error_type))}</span></span>`).join("");
      return `<section class="provider-group"><header class="provider-group-head"><span class="provider-mark">${esc(item.label.slice(0, 1).toUpperCase())}</span><div class="provider-copy"><strong>${esc(item.label)}</strong><span>${esc(item.base_url)}</span></div><span class="health-status ${status.tone}"><i></i>${esc(status.label)}</span>${dataActions("provider", item.provider_id)}</header><div class="provider-meta"><span>${(item.protocols || ["openai"]).map(protocol => esc(protocolLabel(protocol))).join(" / ")}</span><span>${esc(authLabel(item.auth_type || "bearer"))}</span><span>${providerHealth.configured ? "认证已配置" : item.auth_type === "none" ? "无需密钥" : "缺少密钥"}</span><span>${models.length} 个模型</span></div><div class="provider-health"><div class="provider-health-copy" title="${esc(status.detail)}"><span>${esc(status.detail)}</span>${status.checkedAt ? `<time>最近测试 ${esc(status.checkedAt)}</time>` : ""}${checks ? `<div class="protocol-checks">${checks}</div>` : ""}</div><button class="button quiet compact test-provider" type="button" data-test-provider="${esc(item.provider_id)}" ${checking || !item.enabled ? "disabled" : ""}>${checking ? "测试中…" : "测试连接"}</button></div><div class="provider-models"><div class="provider-models-head"><strong>模型</strong><button class="button quiet compact" type="button" data-add-model="${esc(item.provider_id)}">＋ 添加模型</button></div>${models.length ? models.map(model => `<div class="provider-model-row"><span class="model-glyph">M</span><div><strong>${esc(model.alias)}</strong><span>${esc(model.model)} · ${esc(protocolLabel(model.protocol || item.protocols?.[0] || "openai"))}</span></div><span class="state-tag">${model.enabled ? "已启用" : "已停用"}</span>${dataActions("model", model.alias)}</div>`).join("") : `<div class="provider-model-empty">该供应商还没有模型</div>`}</div></section>`;
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
    $("providerAuthPrefix").value = item?.auth_type === "custom_header" ? (item.auth_prefix || "") : "";
    $("providerEnabled").checked = item?.enabled ?? true; $("providerKey").value = "";
    syncProviderAuth();
  }

  function syncProviderAuth() {
    const authType = $("providerAuthType").value;
    const custom = authType === "custom_header";
    $("providerAuthHeaderField").classList.toggle("hidden", !custom);
    $("providerAuthPrefixField").classList.toggle("hidden", !custom);
    $("providerCredentialField").classList.toggle("hidden", authType === "none");
    const header = authType === "bearer" ? "Authorization" : authType === "api_key" ? "x-api-key" : authType === "custom_header" ? ($("providerAuthHeader").value || "自定义 Header") : "无认证 Header";
    const prefix = authType === "bearer" ? "Bearer " : authType === "custom_header" && $("providerAuthPrefix").value ? `${$("providerAuthPrefix").value} ` : "";
    $("providerAuthPreview").textContent = authType === "none" ? "请求不会附带认证信息" : `${header}: ${prefix}••••••••`;
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
    $("workflowNodeList").innerHTML = nodes.length ? nodes.map((node, index) => `<article class="workflow-node-row" data-workflow-node="${index}"><div class="node-row-index">${index + 1}</div><div class="node-row-main"><div class="node-row-fields"><label>节点 ID<input data-node-field="node_id" value="${esc(node.node_id)}" required pattern="[A-Za-z0-9_-]+"></label><label>节点类型<select data-node-field="node_type">${state.catalog.nodes.map(item => option(item.node_type, item.label, node.node_type)).join("")}</select></label><label>绑定模型<select data-node-field="model_alias">${option("", "自动选择模型", node.model_alias)}${state.resources.models.map(item => option(item.alias, item.alias, node.model_alias)).join("")}</select></label><label>失败策略<select data-node-field="on_failure">${[["human", "人工处理"], ["retry", "自动重试"], ["skip", "跳过"], ["replan", "重新规划"]].map(([value, label]) => option(value, label, node.on_failure)).join("")}</select></label></div><div class="node-row-secondary"><label>依赖节点<span>使用英文逗号分隔</span><input data-node-field="depends_on" value="${esc((node.depends_on || []).join(", "))}" placeholder="step-1, step-2"></label><details><summary>提示词模板</summary><textarea data-node-field="prompt_template" rows="2" placeholder="可选；留空使用节点默认提示词">${esc(node.prompt_template || "")}</textarea></details></div></div><button class="icon-button danger-icon" type="button" data-remove-workflow-node="${index}" aria-label="移除节点" title="移除节点">×</button></article>`).join("") : `<div class="list-empty">至少添加一个节点才能保存工作流</div>`;
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
  $("messageInput").addEventListener("input", event => { event.target.style.height = "auto"; event.target.style.height = `${Math.min(event.target.scrollHeight, 180)}px`; });
  document.querySelectorAll("[data-mode]").forEach(button => button.addEventListener("click", () => { state.mode = button.dataset.mode; renderMode(); }));
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
  document.querySelectorAll("[data-starter]").forEach(button => button.addEventListener("click", () => { $("messageInput").value = button.dataset.starter; $("messageInput").focus(); }));
  $("mobileMenu").addEventListener("click", () => $("rail").classList.toggle("open"));
  $("refreshProjects").addEventListener("click", init);
  $("refreshSession").addEventListener("click", () => state.session && api(`/api/v2/sessions/${state.session.session_id}`).then(session => { state.session = session; renderSessions(); }).catch(error => toast(error.message)));
  $("themeToggle").addEventListener("click", () => { document.body.classList.toggle("dark"); localStorage.setItem("workloop-theme", document.body.classList.contains("dark") ? "dark" : "light"); });
  $("closeInspector").addEventListener("click", () => { $("shell").classList.add("inspector-hidden"); $("showInspector").classList.add("visible"); });
  $("showInspector").addEventListener("click", () => { $("shell").classList.remove("inspector-hidden"); $("showInspector").classList.remove("visible"); });

  document.querySelectorAll("[data-open-management]").forEach(button => button.addEventListener("click", openManagement));
  document.querySelectorAll("[data-close-management]").forEach(button => button.addEventListener("click", () => $("managementDialog").close()));
  document.querySelectorAll("[data-management-tab]").forEach(button => button.addEventListener("click", () => setManagementTab(button.dataset.managementTab)));

  $("newProvider").addEventListener("click", () => editProvider());
  $("providerForm").addEventListener("submit", async event => { event.preventDefault(); try { const protocols = [["providerProtocolOpenAI", "openai"], ["providerProtocolClaude", "claude"]].filter(([id]) => $(id).checked).map(([, value]) => value); if (!protocols.length) throw new Error("请至少选择一种协议"); const authType = $("providerAuthType").value; await post("/api/v2/resources/providers", { provider_id: $("providerId").value, label: $("providerLabel").value, base_url: $("providerUrl").value, protocols, auth_type: authType, auth_header: authType === "custom_header" ? $("providerAuthHeader").value : "", auth_prefix: authType === "bearer" ? "Bearer" : authType === "custom_header" ? $("providerAuthPrefix").value : "", api_key: authType === "none" ? "" : $("providerKey").value, enabled: $("providerEnabled").checked }); $("providerForm").classList.add("hidden"); await refreshManagement(); toast("供应商已保存"); } catch (error) { toast(error.message); } });
  document.querySelector("[data-cancel-provider]").addEventListener("click", () => $("providerForm").classList.add("hidden"));
  $("providerAuthType").addEventListener("change", syncProviderAuth);
  $("providerAuthHeader").addEventListener("input", syncProviderAuth);
  $("providerAuthPrefix").addEventListener("input", syncProviderAuth);
  [$("providerProtocolOpenAI"), $("providerProtocolClaude")].forEach(input => input.addEventListener("change", () => { if ($("providerProtocolClaude").checked && !$("providerProtocolOpenAI").checked && $("providerAuthType").value === "bearer") $("providerAuthType").value = "api_key"; syncProviderAuth(); }));
  $("modelForm").addEventListener("submit", async event => { event.preventDefault(); try { await post("/api/v2/resources/models", { alias: $("modelAlias").value, provider_id: $("modelProvider").value, protocol: $("modelProtocol").value, model: $("modelName").value, capabilities: csv($("modelCapabilities").value), temperature: $("modelTemperature").value === "" ? null : Number($("modelTemperature").value), max_tokens: $("modelMaxTokens").value === "" ? null : Number($("modelMaxTokens").value), enabled: $("modelEnabled").checked }); $("modelForm").classList.add("hidden"); await refreshManagement(); toast("模型已保存"); } catch (error) { toast(error.message); } });
  document.querySelector("[data-cancel-model]").addEventListener("click", () => $("modelForm").classList.add("hidden"));

  $("newNode").addEventListener("click", () => editNode());
  $("nodeForm").addEventListener("submit", async event => { event.preventDefault(); try { await post("/api/v2/nodes", { node_type: $("nodeType").value, label: $("nodeLabel").value, description: $("nodeDescription").value, input_fields: csv($("nodeInputs").value), output_fields: csv($("nodeOutputs").value), capabilities: csv($("nodeCapabilities").value), default_model: $("nodeDefaultModel").value }); $("nodeForm").classList.add("hidden"); await refreshManagement(); toast("自定义节点已保存"); } catch (error) { toast(error.message); } });
  document.querySelector("[data-cancel-node]").addEventListener("click", () => $("nodeForm").classList.add("hidden"));

  $("newWorkflow").addEventListener("click", () => { state.editingWorkflow = newWorkflowValue(); renderWorkflowList(); renderWorkflowEditor(); });
  $("addWorkflowNode").addEventListener("click", () => { syncWorkflowForm(); const index = state.editingWorkflow.nodes.length + 1; state.editingWorkflow.nodes.push({ node_id: `step-${index}`, node_type: state.catalog.nodes[0]?.node_type || "tool", depends_on: [], model_alias: "", prompt_template: "", on_failure: "human", config: {}, position: [0, index * 100] }); renderWorkflowNodes(); });
  $("workflowForm").addEventListener("submit", async event => { event.preventDefault(); try { syncWorkflowForm(); if (!state.editingWorkflow.nodes.length) throw new Error("工作流至少需要一个节点"); const saved = await post("/api/v2/workflows", state.editingWorkflow); state.selectedWorkflowId = saved.workflow_id; state.editingWorkflow = clone(saved); await refreshManagement(); toast("工作流和模型关联已保存"); } catch (error) { toast(error.message); } });
  $("deleteWorkflow").addEventListener("click", async () => { const id = state.editingWorkflow?.workflow_id; if (!id || !confirm(`删除工作流 ${id}？`)) return; try { await remove(`/api/v2/workflows/${encodeURIComponent(id)}`); state.editingWorkflow = null; await refreshManagement(); toast("工作流已删除"); } catch (error) { toast(error.message); } });

  if (localStorage.getItem("workloop-theme") === "dark") document.body.classList.add("dark");
  init();
})();
