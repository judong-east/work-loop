(() => {
  const state = {
    roles: [],
    nodes: [],
    models: [],
    collaboration: { tasks: [], handoffs: [], goals: [], counts: {} },
    coordinating: false,
    editingTaskId: "",
  };

  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(
    /[&<>"']/g,
    char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
  );
  const csv = value => String(value || "").split(",").map(item => item.trim()).filter(Boolean);
  const option = (value, label, selected = "") => (
    `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`
  );
  const icon = name => window.workloopIcon ? window.workloopIcon(name) : "";
  const projectId = () => document.body.dataset.projectId || "";

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
    return payload;
  }

  const post = (path, value) => api(path, { method: "POST", body: JSON.stringify(value) });
  const remove = path => api(path, { method: "DELETE" });

  function toast(message) {
    $("toast").textContent = message;
    $("toast").classList.add("show");
    setTimeout(() => $("toast").classList.remove("show"), 2600);
  }

  async function refreshRoleResources() {
    const [roles, catalog, resources] = await Promise.all([
      api("/api/v2/roles"),
      api("/api/v2/catalog"),
      api("/api/v2/resources"),
    ]);
    state.roles = roles;
    state.nodes = catalog.nodes;
    state.models = resources.models;
    renderRoles();
    fillRoleEditorOptions();
  }

  function renderRoles() {
    const accessLabels = { read: "只读", write: "写入", validate: "验证" };
    $("roleList").innerHTML = state.roles.length
      ? state.roles.map(role => `
          <div class="data-row">
            <span class="role-glyph">R</span>
            <div>
              <strong>${esc(role.label)}</strong>
              <span>${esc(role.role_id)} · ${esc(role.node_type)} · ${esc(role.model_alias)}</span>
            </div>
            <span class="state-tag">${esc(accessLabels[role.workspace_access] || role.workspace_access)}</span>
            <span class="row-actions">
              <button class="icon-button small" type="button" data-edit-role="${esc(role.role_id)}" aria-label="编辑角色">${icon("pencil")}</button>
              <button class="icon-button small danger-icon" type="button" data-delete-role="${esc(role.role_id)}" aria-label="删除角色">${icon("trash")}</button>
            </span>
          </div>
        `).join("")
      : '<div class="list-empty">还没有角色。</div>';
    document.querySelectorAll("[data-edit-role]").forEach(button => {
      button.addEventListener("click", () => editRole(button.dataset.editRole));
    });
    document.querySelectorAll("[data-delete-role]").forEach(button => {
      button.addEventListener("click", () => deleteRole(button.dataset.deleteRole));
    });
  }

  function fillRoleEditorOptions(role = null) {
    $("roleNodeType").innerHTML = state.nodes.map(node => (
      option(node.node_type, node.label, role?.node_type)
    )).join("");
    $("roleModel").innerHTML = state.models.filter(model => model.enabled).map(model => (
      option(model.alias, model.alias, role?.model_alias)
    )).join("");
  }

  function editRole(roleId = "") {
    const role = state.roles.find(item => item.role_id === roleId);
    $("roleForm").reset();
    $("roleForm").classList.remove("hidden");
    $("roleFormTitle").textContent = role ? "编辑角色" : "新建角色";
    $("roleId").value = role?.role_id || "";
    $("roleId").readOnly = Boolean(role);
    $("roleLabel").value = role?.label || "";
    fillRoleEditorOptions(role);
    $("roleWorkspaceAccess").value = role?.workspace_access || accessForNode($("roleNodeType").value);
    $("roleCapabilities").value = (role?.capabilities || ["general"]).join(", ");
    $("roleInstructions").value = role?.instructions || "";
  }

  function accessForNode(nodeType) {
    if (nodeType === "implementation") return "write";
    if (nodeType === "testing") return "validate";
    return "read";
  }

  async function deleteRole(roleId) {
    if (!confirm(`删除角色 ${roleId}？`)) return;
    try {
      await remove(`/api/v2/roles/${encodeURIComponent(roleId)}`);
      await refreshRoleResources();
      toast("角色已删除");
    } catch (error) {
      toast(error.message);
    }
  }

  async function refreshTasks() {
    const currentProjectId = projectId();
    $("collaborationProjectName").textContent = document.body.dataset.projectName || "未选择项目";
    if (!currentProjectId) {
      state.collaboration = { tasks: [], handoffs: [], goals: [], counts: {} };
      renderTasks();
      return;
    }
    const [roles, collaboration, resources] = await Promise.all([
      api("/api/v2/roles"),
      api(`/api/v2/projects/${encodeURIComponent(currentProjectId)}/collaboration`),
      api("/api/v2/resources"),
    ]);
    state.roles = roles;
    state.collaboration = collaboration;
    state.models = resources.models;
    renderTasks();
    renderGoals();
    fillTaskEditorOptions();
  }

  function renderTasks() {
    const tasks = state.collaboration.tasks || [];
    const counts = state.collaboration.counts || {};
    const roleLabels = Object.fromEntries(state.roles.map(role => [role.role_id, role.label]));
    const statusLabels = {
      pending: "等待", running: "执行中", blocked: "阻塞", completed: "完成", failed: "失败",
    };
    $("collaborationSummary").innerHTML = Object.entries(statusLabels).map(([status, label]) => (
      `<span><strong>${Number(counts[status] || 0)}</strong>${label}</span>`
    )).join("");
    $("collaborationTaskList").innerHTML = tasks.length
      ? tasks.map(task => `
          <article class="task-card ${esc(task.status)}">
            <span class="task-status">${esc(statusLabels[task.status] || task.status)}</span>
            <div class="task-copy">
              <strong>${esc(task.title)}</strong>
              <span>${esc(roleLabels[task.role_id] || task.role_id)} · 优先级 ${esc(task.priority)}</span>
              <p>${esc(task.description)}</p>
              ${task.depends_on.length ? `<small>依赖：${task.depends_on.map(esc).join("、")}</small>` : ""}
              ${taskResultSummary(task) ? `<small class="task-result">${esc(taskResultSummary(task))}</small>` : ""}
              ${task.error ? `<small class="task-error">${esc(task.error)}</small>` : ""}
            </div>
            <span class="row-actions">
              ${["blocked", "failed"].includes(task.status) ? `<button class="button quiet compact" type="button" data-retry-task="${esc(task.task_id)}">重试</button>` : ""}
              ${task.status !== "running" ? `<button class="icon-button small" type="button" data-edit-task="${esc(task.task_id)}" aria-label="编辑任务">${icon("pencil")}</button>` : ""}
              ${task.status !== "running" ? `<button class="icon-button small danger-icon" type="button" data-delete-task="${esc(task.task_id)}" aria-label="删除任务">${icon("trash")}</button>` : ""}
            </span>
          </article>
        `).join("")
      : `<div class="list-empty">${projectId() ? "还没有协同任务。" : "请先选择项目。"}</div>`;
    $("coordinateTasks").disabled = state.coordinating || !tasks.some(task => task.status === "pending");
    $("newCollaborationTask").disabled = !projectId();
    $("decomposeGoal").disabled = !projectId() || state.coordinating || !$("goalInput").value.trim();
    renderHandoffs();
    renderGoals();
    document.querySelectorAll("[data-retry-task]").forEach(button => {
      button.addEventListener("click", () => retryTask(button.dataset.retryTask));
    });
    document.querySelectorAll("[data-edit-task]").forEach(button => {
      button.addEventListener("click", () => editTask(button.dataset.editTask));
    });
    document.querySelectorAll("[data-delete-task]").forEach(button => {
      button.addEventListener("click", () => deleteTask(button.dataset.deleteTask));
    });
  }

  function taskResultSummary(task) {
    const facts = task.result?.facts || {};
    if (facts.understanding) return String(facts.understanding);
    if (facts.changes) return String(facts.changes);
    if (facts.verdict) return `审核结论：${facts.verdict}`;
    if (facts.result) return String(facts.result);
    if (Array.isArray(facts.steps)) return `计划：${facts.steps.slice(0, 3).join("；")}`;
    if (Array.isArray(facts.applied_files)) return `已应用 ${facts.applied_files.length} 个文件`;
    if (Array.isArray(facts.checks)) {
      const passed = facts.checks.filter(check => check.status === "passed").length;
      return `验证：${passed}/${facts.checks.length} 通过`;
    }
    return "";
  }

  function renderGoals() {
    const goals = state.collaboration.goals || [];
    const tasks = Object.fromEntries((state.collaboration.tasks || []).map(task => [task.task_id, task]));
    $("goalCount").textContent = goals.length;
    $("goalList").innerHTML = goals.length
      ? goals.map(goal => `
          <div class="goal-row">
            <div class="goal-copy">
              <strong>${esc(goal.summary || goal.goal)}</strong>
              <p>${esc(goal.goal)}</p>
              <small>${(goal.task_ids || []).map(id => esc(tasks[id]?.title || id)).join(" → ") || "暂无子任务"}</small>
            </div>
            <span class="row-actions">
              <button class="icon-button small danger-icon" type="button" data-delete-goal="${esc(goal.goal_id)}" aria-label="删除目标">${icon("trash")}</button>
            </span>
          </div>
        `).join("")
      : '<div class="list-empty">拆分后的大目标会显示在这里。</div>';
    document.querySelectorAll("[data-delete-goal]").forEach(button => {
      button.addEventListener("click", () => deleteGoal(button.dataset.deleteGoal));
    });
  }

  async function decomposeGoal() {
    const goal = $("goalInput").value.trim();
    if (!projectId() || !goal || state.coordinating) return;
    $("decomposeGoal").disabled = true;
    $("decomposeGoal").textContent = "拆分中…";
    try {
      const result = await post(`/api/v2/projects/${encodeURIComponent(projectId())}/goals`, { goal });
      $("goalInput").value = "";
      await refreshTasks();
      toast(`已拆分为 ${result.task_ids.length} 个子任务`);
    } catch (error) {
      toast(error.message);
      await refreshTasks();
    } finally {
      $("decomposeGoal").textContent = "拆分为子任务";
      renderTasks();
    }
  }

  async function deleteGoal(goalId) {
    if (!confirm("删除该目标记录？（仅当其子任务全部删除后可删）")) return;
    try {
      await remove(`/api/v2/goals/${encodeURIComponent(goalId)}`);
      await refreshTasks();
      toast("目标记录已删除");
    } catch (error) {
      toast(error.message);
    }
  }

  function renderHandoffs() {
    const handoffs = state.collaboration.handoffs || [];
    const tasks = Object.fromEntries((state.collaboration.tasks || []).map(task => [task.task_id, task.title]));
    $("handoffCount").textContent = handoffs.length;
    $("handoffList").innerHTML = handoffs.length
      ? handoffs.map(item => `
          <div class="handoff-row">
            <strong>${esc(tasks[item.from_task_id] || item.from_task_id)} → ${esc(tasks[item.to_task_id] || item.to_task_id)}</strong>
            <span>${esc(item.content)}</span>
          </div>
        `).join("")
      : '<div class="list-empty">任务完成后，交接记录会显示在这里。</div>';
  }

  function fillTaskEditorOptions(task = null) {
    $("collaborationTaskRole").innerHTML = state.roles.map(role => (
      option(role.role_id, `${role.label} · ${role.model_alias}`, task?.role_id || "analyst")
    )).join("");
    $("collaborationTaskDependencies").innerHTML = (state.collaboration.tasks || [])
      .filter(item => item.task_id !== task?.task_id)
      .map(item => option(item.task_id, `${item.title} · ${item.status}`, task && task.depends_on.includes(item.task_id) ? item.task_id : ""))
      .join("");
  }

  function editTask(taskId) {
    const task = (state.collaboration.tasks || []).find(item => item.task_id === taskId);
    if (!task) return;
    state.editingTaskId = taskId;
    $("collaborationTaskForm").reset();
    fillTaskEditorOptions(task);
    $("collaborationTaskTitle").value = task.title;
    $("collaborationTaskDescription").value = task.description;
    $("collaborationTaskPriority").value = String(task.priority);
    $("collaborationTaskFormTitle").textContent = "编辑协同任务";
    $("collaborationTaskSubmit").textContent = "保存任务";
    $("collaborationTaskForm").classList.remove("hidden");
  }

  async function retryTask(taskId) {
    try {
      await post(`/api/v2/tasks/${encodeURIComponent(taskId)}/retry`, {});
      await refreshTasks();
      toast("任务已重置为等待状态");
    } catch (error) {
      toast(error.message);
    }
  }

  async function deleteTask(taskId) {
    if (!confirm(`删除任务 ${taskId}？`)) return;
    try {
      await remove(`/api/v2/tasks/${encodeURIComponent(taskId)}`);
      await refreshTasks();
      toast("任务已删除");
    } catch (error) {
      toast(error.message);
    }
  }

  async function coordinate() {
    if (!projectId() || state.coordinating) return;
    state.coordinating = true;
    $("coordinateTasks").textContent = "协同执行中…";
    renderTasks();
    try {
      const started = await post(
        `/api/v2/projects/${encodeURIComponent(projectId())}/coordinate`,
        { async: true },
      );
      if (started.started) {
        await pollCoordination();
        toast("本轮协同执行完成");
      } else {
        state.collaboration = started;
        renderTasks();
        toast("本轮协同执行完成");
      }
    } catch (error) {
      toast(error.message);
    } finally {
      state.coordinating = false;
      $("coordinateTasks").textContent = "运行协同";
      await refreshTasks().catch(() => renderTasks());
    }
  }

  async function pollCoordination() {
    for (let attempt = 0; attempt < 600; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 1500));
      try {
        state.collaboration = await api(
          `/api/v2/projects/${encodeURIComponent(projectId())}/collaboration`,
        );
        renderTasks();
      } catch (error) {
        return;
      }
      if (!state.collaboration.coordinating) return;
    }
  }

  $("newRole").addEventListener("click", () => editRole());
  $("roleNodeType").addEventListener("change", event => {
    $("roleWorkspaceAccess").value = accessForNode(event.target.value);
  });
  $("roleForm").addEventListener("submit", async event => {
    event.preventDefault();
    try {
      await post("/api/v2/roles", {
        role_id: $("roleId").value,
        label: $("roleLabel").value,
        node_type: $("roleNodeType").value,
        model_alias: $("roleModel").value,
        workspace_access: $("roleWorkspaceAccess").value,
        capabilities: csv($("roleCapabilities").value),
        instructions: $("roleInstructions").value,
      });
      $("roleForm").classList.add("hidden");
      await refreshRoleResources();
      toast("角色已保存");
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("[data-cancel-role]").addEventListener("click", () => {
    $("roleForm").classList.add("hidden");
  });

  $("newCollaborationTask").addEventListener("click", () => {
    state.editingTaskId = "";
    $("collaborationTaskForm").reset();
    $("collaborationTaskPriority").value = "50";
    fillTaskEditorOptions();
    $("collaborationTaskFormTitle").textContent = "新建协同任务";
    $("collaborationTaskSubmit").textContent = "创建任务";
    $("collaborationTaskForm").classList.remove("hidden");
  });
  $("collaborationTaskForm").addEventListener("submit", async event => {
    event.preventDefault();
    const dependencies = [...$("collaborationTaskDependencies").selectedOptions].map(item => item.value);
    const payload = {
      title: $("collaborationTaskTitle").value,
      description: $("collaborationTaskDescription").value,
      role_id: $("collaborationTaskRole").value,
      priority: Number($("collaborationTaskPriority").value),
      depends_on: dependencies,
    };
    try {
      if (state.editingTaskId) {
        await post(`/api/v2/tasks/${encodeURIComponent(state.editingTaskId)}`, payload);
        toast("协同任务已更新");
      } else {
        await post(`/api/v2/projects/${encodeURIComponent(projectId())}/tasks`, payload);
        toast("协同任务已创建");
      }
      state.editingTaskId = "";
      $("collaborationTaskForm").classList.add("hidden");
      await refreshTasks();
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("[data-cancel-collaboration-task]").addEventListener("click", () => {
    state.editingTaskId = "";
    $("collaborationTaskForm").classList.add("hidden");
  });
  $("coordinateTasks").addEventListener("click", coordinate);
  $("decomposeGoal").addEventListener("click", decomposeGoal);
  $("goalInput").addEventListener("input", () => {
    $("decomposeGoal").disabled = !projectId() || state.coordinating || !$("goalInput").value.trim();
  });

  document.querySelectorAll("[data-management-tab]").forEach(button => {
    button.addEventListener("click", async () => {
      try {
        if (button.dataset.managementTab === "roles") await refreshRoleResources();
        if (button.dataset.managementTab === "tasks") await refreshTasks();
      } catch (error) {
        toast(error.message);
      }
    });
  });
  window.addEventListener("workloop:project-selected", () => {
    const taskTab = document.querySelector('[data-management-tab="tasks"]');
    if (taskTab?.classList.contains("active")) refreshTasks().catch(error => toast(error.message));
  });
})();
