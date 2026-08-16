"""Plan-graph execution for AgentWorkflow, split out for module size.

``GraphExecutionMixin`` is mixed into :class:`AgentWorkflow` and expects the
host to provide the persistence and orchestration seams the graph loop uses:
``store``, ``context_ledger``, ``git_worktrees``, ``get_project()``,
``_invoke_agent()``, ``_pause()``, and ``_repair_node_output()``. The methods
here own only graph concerns: per-node request assembly, topological
execution with resume, per-node worktrees, failure policies, and merging node
outputs into one ExecutionResult.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path

from app.agents import prompts
from app.agents.context_ledger import ContextPack
from app.agents.contracts import (
    AgentAccess,
    AgentPolicy,
    AgentRequest,
    AgentResult,
    AgentTask,
    AgentTaskStatus,
    ExecutionPlan,
    ExecutionResult,
    ReviewResult,
)
from app.agents.plan_graph import (
    PlanGraph,
    PlanNode,
    PlanNodeKind,
    PlanNodeAccess,
)
from app.agents.task_budget import agent_budget, task_budget_error
from app.agents.workflow_config import WorkflowNode
from app.core.contracts import utc_now
from app.projects.contracts import ProjectPolicy
from app.tools.workspace import FileChange, Workspace


class GraphExecutionMixin:
    @staticmethod
    def _node_request_fields(
        task: AgentTask,
        kind: PlanNodeKind,
        role: str,
    ) -> dict[str, str]:
        def fallback() -> dict[str, str]:
            phase_key = "node:review" if role == "reviewer" else role
            return {
                "model_profile_id": "",
                "session_key": phase_key,
                "session_id": task.sessions.get(phase_key, task.sessions.get(role, "")),
                "node_id": "review" if role == "reviewer" else "",
                "provider": "",
                "model": "",
                "thinking": "",
                "context_ref": task.artifacts.get("context_plan", ""),
            }

        if not task.plan_graph:
            return fallback()
        try:
            graph = PlanGraph.from_dict(task.plan_graph)
        except (TypeError, ValueError):
            return fallback()
        if kind is PlanNodeKind.REVIEW and graph.review_model.profile_id:
            binding = graph.review_model
            session_key = "node:review"
            return {
                "model_profile_id": binding.profile_id,
                "session_key": session_key,
                "session_id": task.sessions.get(
                    session_key, task.sessions.get("reviewer", "")
                ),
                "node_id": "review",
                "provider": binding.provider,
                "model": binding.model,
                "thinking": binding.thinking,
                "context_ref": task.artifacts.get("context_plan", ""),
            }
        candidates = [node for node in graph.nodes if node.kind is kind and node.enabled]
        if not candidates:
            return fallback()
        node = next((item for item in candidates if item.model.model or item.model.provider), candidates[0])
        session_key = f"node:{node.node_id}"
        return {
            "model_profile_id": node.model.profile_id,
            "session_key": session_key,
            "session_id": task.sessions.get(session_key, task.sessions.get(role, "")),
            "node_id": node.node_id,
            "provider": node.model.provider,
            "model": node.model.model,
            "thinking": node.model.thinking,
            "context_ref": task.artifacts.get("context_plan", ""),
        }

    # ------------------------------------------------------------------
    # Graph-driven execution (one executor call per implementation node)
    # ------------------------------------------------------------------

    @staticmethod
    def _node_fields(node: PlanNode, context_ref: str) -> dict[str, str]:
        return {
            "model_profile_id": node.model.profile_id,
            "session_key": f"node:{node.node_id}",
            "node_id": node.node_id,
            "provider": node.model.provider,
            "model": node.model.model,
            "thinking": node.model.thinking,
            "context_ref": context_ref,
        }

    def _node_context(self, task: AgentTask, node: PlanNode) -> ContextPack | None:
        """Merge the ContextPacks produced by this node's dependencies (plus the
        approved-plan pack) into one durable handoff pack. Returns ``None`` when
        there is no upstream context to inject — the node then runs standalone.
        """
        packs: list[ContextPack] = []
        plan_summary = ""
        for dependency in node.depends_on:
            reference = str(task.node_runs.get(dependency, {}).get("context_ref", ""))
            if reference:
                pack = self.context_ledger.read_ref(task.task_id, reference)
                if pack is not None:
                    packs.append(pack)
        plan_reference = task.artifacts.get("context_plan", "")
        if plan_reference:
            plan_pack = self.context_ledger.read_ref(task.task_id, plan_reference)
            if plan_pack is not None:
                packs.append(plan_pack)
                plan_summary = plan_pack.summary
        if not packs:
            return None
        return self.context_ledger.merge(
            task_id=task.task_id,
            node_id=node.node_id,
            packs=packs,
            summary=plan_summary or node.title or node.node_id,
            inputs=list(node.inputs),
        )


    def _execute_plan_graph(
        self,
        task: AgentTask,
        plan: ExecutionPlan,
        policy: ProjectPolicy,
        effective_agent_policy: AgentPolicy,
        workspace: Workspace,
        workspace_path: Path,
        base: dict[str, str],
        round_dir: Path,
        review_feedback: ReviewResult | None,
        workflow_node: WorkflowNode,
    ) -> AgentTask | None:
        """Run the implementation/integration nodes of the task PlanGraph in
        topological order, one executor call per node, with structured context
        handoff between nodes. Returns ``None`` when the graph finished cleanly
        (the combined ``ExecutionResult`` is written to ``round_dir``); returns
        the task on a terminal outcome (paused/failed/blocked)."""
        if task.plan_graph:
            try:
                graph = PlanGraph.from_dict(task.plan_graph)
            except (TypeError, ValueError) as error:
                return self._pause(
                    task,
                    "invalid_plan_graph",
                    f"任务执行图无效：{error}",
                    resume_phase=AgentTaskStatus.EXECUTING,
                )
        else:
            graph = PlanGraph.from_execution_plan(plan)
        if not graph.execution_nodes():
            self.store.write_json(round_dir / "execution.json", ExecutionResult())
            return None

        completed: set[str] = set()
        failed: set[str] = set()
        # Replay finished nodes (resume path): completed/skipped nodes are left
        # done; everything else (pending/running/failed) is re-run. Pre-complete
        # the planning node, which represents the already-approved plan.
        for node in graph.nodes:
            status = str(task.node_runs.get(node.node_id, {}).get("status", ""))
            if status in {"completed", "skipped"}:
                completed.add(node.node_id)
            if node.kind is PlanNodeKind.PLANNING and node.node_id not in completed:
                completed.add(node.node_id)
                task.node_runs.setdefault(
                    node.node_id,
                    {
                        "status": "completed",
                        "round": task.iteration,
                        "session_id": task.sessions.get("planner", ""),
                        "context_ref": task.artifacts.get("context_plan", ""),
                        "run_ref": "",
                        "result_ref": "",
                        "started_at": "",
                        "finished_at": "",
                        "error": "",
                    },
                )
        self.store.save(task)

        while True:
            ready = [
                node for node in graph.ready(completed, failed)
                if node in graph.execution_nodes()
            ]
            if not ready:
                break
            for node in ready:
                outcome = self._run_plan_node(
                    task, node, plan, policy, effective_agent_policy,
                    workspace, workspace_path, base, review_feedback, workflow_node,
                )
                if outcome is not None:
                    return outcome
                completed.add(node.node_id)
                self.store.save(task)

        unfinished = [
            node.node_id
            for node in graph.execution_nodes()
            if node.node_id not in completed
        ]
        if unfinished:
            return self._pause(
                task,
                "plan_graph_blocked",
                "执行图存在无法调度的启用节点：" + ", ".join(unfinished),
                resume_phase=AgentTaskStatus.EXECUTING,
            )

        # Assemble the combined result from every completed implementation node,
        # including nodes finished on a prior run that we just replayed (resume).
        per_node_results: list[ExecutionResult] = []
        for node in graph.execution_nodes():
            if node.node_id not in completed:
                continue
            result_ref = str(task.node_runs.get(node.node_id, {}).get("result_ref", ""))
            if not result_ref:
                continue
            path = self.store.task_dir(task.task_id) / result_ref
            if path.is_file():
                per_node_results.append(
                    ExecutionResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
        self.store.write_json(round_dir / "execution.json", self._merge_node_results(per_node_results))
        return None

    def _reset_write_nodes_for_revision(
        self,
        task: AgentTask,
        plan: ExecutionPlan,
    ) -> None:
        """Clear write-node state and every transitive dependent before revision.

        ``_execute_plan_graph`` replays completed ``node_runs``. A revision must
        rerun write nodes and recompute their downstream analysis while retaining
        each node's own session, so no dependent consumes stale ContextPacks.
        Planning state remains unchanged; a rejected plan clears all node runs.

        No-op outside graph execution, where the single executor call already
        re-runs each round."""
        if not task.graph_execution or not task.node_runs:
            return
        try:
            graph = PlanGraph.from_dict(task.plan_graph)
        except (TypeError, ValueError):
            graph = PlanGraph.from_execution_plan(plan)
        invalidated = {node.node_id for node in graph.write_nodes()}
        changed = True
        while changed:
            changed = False
            for node in graph.execution_nodes():
                if node.node_id not in invalidated and any(
                    dependency in invalidated for dependency in node.depends_on
                ):
                    invalidated.add(node.node_id)
                    changed = True
        for node in graph.execution_nodes():
            if node.node_id not in invalidated:
                continue
            previous = task.node_runs.get(node.node_id, {})
            task.node_runs[node.node_id] = {
                "status": "pending",
                "round": task.iteration,
                "session_id": str(previous.get("session_id", "")),
                "context_ref": "",
                "run_ref": "",
                "result_ref": "",
                "started_at": "",
                "finished_at": "",
                "error": "",
            }
        self.store.save(task)

    def _run_plan_node(
        self,
        task: AgentTask,
        node: PlanNode,
        plan: ExecutionPlan,
        policy: ProjectPolicy,
        effective_agent_policy: AgentPolicy,
        workspace: Workspace,
        workspace_path: Path,
        base: dict[str, str],
        review_feedback: ReviewResult | None,
        workflow_node: WorkflowNode,
    ) -> AgentTask | None:
        """Execute one plan node via the executor runtime. Returns ``None`` on
        success or skip (node state persisted); returns the task on a terminal
        pause/failure that should stop the round.

        When ``task.node_worktree`` is set, the node runs in its own detached
        git worktree. Before each attempt the task worktree's accumulated writes
        are replicated into the node worktree (so the node sees its upstream),
        and on success the node's own delta is merged back into the shared task
        worktree, leaving it uncommitted so validation/review/delivery stay
        unchanged. The node worktree is always cleaned up in ``finally``."""
        state = task.node_runs.setdefault(
            node.node_id,
            {
                "status": "pending",
                "round": task.iteration,
                "session_id": "",
                "context_ref": "",
                "run_ref": "",
                "result_ref": "",
                "started_at": "",
                "finished_at": "",
                "error": "",
            },
        )
        if state.get("status") in {"completed", "skipped"}:
            return None

        budget_error = task_budget_error(task)
        if budget_error:
            return self._pause(
                task, budget_error, "节点执行前预算已耗尽。",
                resume_phase=AgentTaskStatus.EXECUTING,
            )

        context = self._node_context(task, node)
        context_ref = ""
        if context is not None:
            context_ref = f"artifacts/context/{node.node_id}/{context.version}.json"

        writes_workspace = node.access is PlanNodeAccess.WORKSPACE_WRITE
        use_node_worktree = task.graph_execution and task.node_worktree and writes_workspace
        node_worktree_path: Path | None = None
        node_workspace: Workspace | None = None
        node_before: dict[str, str] | None = None
        request_workspace = workspace_path
        if use_node_worktree:
            node_worktree_path = self._prepare_node_worktree(task, node)
            node_workspace = Workspace(node_worktree_path)
            request_workspace = node_worktree_path

        resumed_session = str(
            state.get("session_id", "")
            or task.sessions.get(f"node:{node.node_id}", "")
        )
        state["session_id"] = resumed_session
        request = AgentRequest(
            task_id=task.task_id,
            role="executor" if writes_workspace else "worker",
            instructions=prompts.node_instructions(
                node,
                context,
                review_feedback,
                workflow_node.instructions,
                self.store.task_dir(task.task_id),
            ),
            workspace=request_workspace,
            access=(
                AgentAccess.WORKSPACE_WRITE
                if writes_workspace
                else AgentAccess.READ_ONLY
            ),
            policy=effective_agent_policy,
            budget=agent_budget(task),
            artifact_root=self.store.task_dir(task.task_id),
            session_id=resumed_session,
            workflow_node_id=workflow_node.node_id,
            **self._node_fields(node, context_ref),
        )

        max_attempts = 2 if node.on_failure == "retry" else 1
        result: AgentResult | None = None
        node_result: ExecutionResult | None = None
        try:
            for _ in range(max_attempts):
                state["status"] = "running"
                state["started_at"] = utc_now()
                self.store.save(task)
                if use_node_worktree:
                    # Reset the node worktree to the task's current state so each
                    # attempt starts from a clean upstream baseline.
                    self._replicate_into_node_worktree(workspace, base, node_workspace)
                    node_before = node_workspace.snapshot()
                result = self._invoke_agent(task, request)
                if result.session_id:
                    state["session_id"] = result.session_id
                    request = replace(request, session_id=result.session_id)
                    self.store.save(task)
                if result.succeeded:
                    try:
                        node_result = ExecutionResult.from_dict(result.output)
                        break
                    except ValueError as error:
                        # The model produced output we cannot parse. Try one
                        # bounded self-repair (re-invoke with the parse error and
                        # a schema reminder) before declaring the attempt failed;
                        # a real text-JSON runtime often self-corrects on the
                        # second turn. On success, adopt the repaired result so
                        # the node's session/run refs point at the last invoke.
                        repaired = self._repair_node_output(
                            task, request, error, result.output
                        )
                        if repaired is not None:
                            node_result, result = repaired
                            break
                        result = AgentResult(
                            succeeded=False, error=f"节点 {node.node_id} 结果无效：{error}"
                        )
                # failed attempt; loop again if attempts remain
            else:
                assert result is not None
                return self._handle_node_failure(task, node, result, plan, policy, state)

            # Success: merge the node's own writes back into the shared task
            # worktree (uncommitted), so the post-graph diff/validation/review/
            # delivery pipeline is unchanged.
            if use_node_worktree:
                self._apply_file_changes(
                    node_workspace,
                    node_workspace.changes_since(node_before),
                    workspace,
                )

            result_ref = (
                f"artifacts/node-runs/{workflow_node.node_id}/"
                f"{node.node_id}/{task.iteration}.json"
            )
            self.store.write_json(self.store.task_dir(task.task_id) / result_ref, node_result)
            state["status"] = "completed"
            state["session_id"] = result.session_id
            state["run_ref"] = task.artifacts.get("last_agent_run", "")
            state["result_ref"] = result_ref
            state["finished_at"] = utc_now()
            state["error"] = ""
            pack = ContextPack(
                task_id=task.task_id,
                node_id=node.node_id,
                summary=node.title or node.node_id,
                facts=list(node_result.completed_steps),
                inputs=list(node.inputs),
                artifacts=[result_ref],
                source_sessions=[result.session_id] if result.session_id else [],
            )
            state["context_ref"] = self.context_ledger.write(pack)
            self.store.save(task)
            return None
        finally:
            if node_worktree_path is not None:
                self._remove_node_worktree(task, node_worktree_path)

    # ------------------------------------------------------------------
    # Per-node worktree helpers (graph execution only)
    # ------------------------------------------------------------------

    def _prepare_node_worktree(self, task: AgentTask, node: PlanNode) -> Path:
        """Create a fresh detached worktree for one node at the task's current
        delivery baseline. The path is deterministic from ``(task_id, node_id)``
        so resume is safe: a stale worktree from a crashed run is pruned first.

        The baseline must be ``delivery_base_commit or base_commit`` — the same
        expression ``_prepared_from_task`` uses. After an integration rebase the
        shared task worktree sits on the advanced target commit, so building
        node worktrees from the original ``base_commit`` would hand the node a
        tree that the replicated delta cannot reconcile."""
        path = self.store.task_dir(task.task_id) / "node-worktrees" / node.node_id
        project = self.get_project(task.project_id)
        return self.git_worktrees.add_node_worktree(
            Path(project.repository),
            path,
            task.delivery_base_commit or task.base_commit,
        )

    def _remove_node_worktree(self, task: AgentTask, path: Path) -> None:
        """Remove a node worktree, best-effort: a stuck worktree does not
        corrupt the task result and is pruned on the next ``add_node_worktree``."""
        try:
            project = self.get_project(task.project_id)
            self.git_worktrees.remove_node_worktree(Path(project.repository), path)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _replicate_into_node_worktree(
        workspace: Workspace,
        base: dict[str, str],
        node_workspace: Workspace,
    ) -> None:
        """Mirror the shared task worktree into the node worktree, including
        upstream deletions, by applying ``task.changes_since(base)`` with real
        bytes (binary-safe)."""
        GraphExecutionMixin._apply_file_changes(
            workspace, workspace.changes_since(base), node_workspace
        )

    @staticmethod
    def _apply_file_changes(
        src: Workspace,
        changes: list[FileChange],
        dst: Workspace,
    ) -> None:
        """Apply file changes from ``src`` into ``dst`` by copying real bytes
        (binary-safe) for writes and unlinking for deletes. ``changes`` is used
        only for path/action detection (from ``changes_since``); content is read
        from the source filesystem, not the (text-only) change record."""
        for change in changes:
            if change.action == "delete":
                target = dst.root / change.path
                if target.exists():
                    target.unlink()
                continue
            source = src.root / change.path
            target = dst.root / change.path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def _handle_node_failure(
        self,
        task: AgentTask,
        node: PlanNode,
        result: AgentResult,
        plan: ExecutionPlan,
        policy: ProjectPolicy,
        state: dict,
    ) -> AgentTask | None:
        state["status"] = "failed"
        if result.session_id:
            state["session_id"] = result.session_id
        state["error"] = result.error
        state["finished_at"] = utc_now()
        self.store.save(task)
        if node.on_failure == "skip":
            # Skip this node and let its dependents proceed; not terminal.
            state["status"] = "skipped"
            self.store.save(task)
            return None
        # NOTE(v1): node-level replan is mapped to a human pause rather than an
        # automatic replan, because a true replan needs a reviewer-style
        # handoff that node failures do not carry. The review-driven REPLAN
        # path (REVIEWING phase -> reviewer verdict REPLAN) stays fully intact.
        return self._pause(
            task,
            "node_failed",
            f"节点 {node.node_id} 执行失败：{result.error}",
            resume_phase=AgentTaskStatus.EXECUTING,
        )

    # ------------------------------------------------------------------
    # Bounded output self-repair (text-JSON runtimes only)
    # ------------------------------------------------------------------


    @staticmethod
    def _merge_node_results(results: list[ExecutionResult]) -> ExecutionResult:
        completed_steps: list[str] = []
        modified_files: list[str] = []
        tests: list = []
        deviations: list[str] = []
        remaining_risks: list[str] = []
        next_steps: list[str] = []
        for result in results:
            completed_steps.extend(result.completed_steps)
            modified_files.extend(result.modified_files)
            tests.extend(result.tests)
            deviations.extend(result.deviations)
            remaining_risks.extend(result.remaining_risks)
            next_steps.extend(result.next_steps)
        return ExecutionResult(
            completed_steps=list(dict.fromkeys(completed_steps)),
            modified_files=list(dict.fromkeys(modified_files)),
            tests=list(tests),
            deviations=list(dict.fromkeys(deviations)),
            remaining_risks=list(dict.fromkeys(remaining_risks)),
            next_steps=list(dict.fromkeys(next_steps)),
        )

    @staticmethod
    def _explicit_policy_tests(output: dict, policy: ProjectPolicy) -> list[str]:
        serialized = json.dumps(output, ensure_ascii=False)
        return [
            command.name
            for command in policy.validation_commands
            if re.search(
                rf"(?<![A-Za-z0-9_.-]){re.escape(command.name)}(?![A-Za-z0-9_.-])",
                serialized,
            )
        ]

