from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol

from app.core.contracts import utc_now

from .models import ContextState, NodeRun, Session, WorkflowDefinition, WorkflowNode
from .node_registry import NodeRegistry


class ModelGateway(Protocol):
    def complete(self, *, model_alias: str, node: WorkflowNode, context: ContextState) -> dict[str, Any]: ...


class SessionStore(Protocol):
    def save(self, session: Session) -> Session: ...


@dataclass
class OrchestrationEvent:
    event_type: str
    session_id: str
    node_id: str = ""
    status: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "session_id": self.session_id,
            "node_id": self.node_id,
            "status": self.status,
            "payload": dict(self.payload),
            "at": self.at,
        }


EventSink = Callable[[OrchestrationEvent], None]
OutputProcessor = Callable[[Session, WorkflowNode, ContextState, dict[str, Any]], dict[str, Any]]


class OrchestrationError(RuntimeError):
    def __init__(self, message: str, *, node_id: str = "", recoverable: bool = False):
        super().__init__(message)
        self.node_id = node_id
        self.recoverable = recoverable


class DagOrchestrator:
    """Execute a workflow definition against a durable shared session state.

    This class owns ordering, retries, failure policy, and event semantics.  It
    does not know how a model is called or where a session is stored, which keeps
    vendor integrations and HTTP concerns outside the orchestration layer.
    """

    def __init__(
        self,
        registry: NodeRegistry,
        store: SessionStore,
        gateway: ModelGateway,
        *,
        event_sink: EventSink | None = None,
        output_processor: OutputProcessor | None = None,
        invoker: Any | None = None,
        max_attempts: int = 2,
    ):
        self.registry = registry
        self.store = store
        self.gateway = gateway
        self.event_sink = event_sink
        self.output_processor = output_processor
        self.invoker = invoker
        self.max_attempts = max(1, max_attempts)

    def validate(self, workflow: WorkflowDefinition) -> list[str]:
        workflow.validate(self.registry)
        ids = {node.node_id for node in workflow.nodes}
        indegree = {node.node_id: 0 for node in workflow.nodes}
        outgoing = {node.node_id: [] for node in workflow.nodes}
        for node in workflow.nodes:
            for dependency in node.depends_on:
                indegree[node.node_id] += 1
                outgoing[dependency].append(node.node_id)
        queue = [node_id for node_id, count in indegree.items() if count == 0]
        ordered: list[str] = []
        while queue:
            node_id = queue.pop(0)
            ordered.append(node_id)
            for child in outgoing[node_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(ordered) != len(ids):
            raise ValueError("workflow dependencies must form a DAG")
        return ordered

    def run(self, session: Session, workflow: WorkflowDefinition, *, resume: bool = True) -> Session:
        order = self.validate(workflow)
        if session.policy.gate_status == "blocked":
            session.status = "needs_replan"
            self.store.save(session)
            self._emit(OrchestrationEvent(
                "policy_gate_blocked", session.session_id, status=session.status,
                payload={"gate": session.policy.gate},
            ))
            return session
        session.status = "running"
        self._emit(OrchestrationEvent("session_started", session.session_id, status=session.status))
        self.store.save(session)

        runs = {message.node_id: NodeRun.from_dict(message.metadata["node_run"])
                for message in session.messages
                if message.role == "event" and message.node_id and isinstance(message.metadata.get("node_run"), dict)}
        nodes = {node.node_id: node for node in workflow.nodes}
        completed = {node_id for node_id, run in runs.items() if run.status == "completed"}
        skipped = {node_id for node_id, run in runs.items() if run.status == "skipped"}
        context = session.context

        for node_id in order:
            node = nodes[node_id]
            if resume and node_id in completed | skipped:
                continue
            if any(dependency not in completed | skipped for dependency in node.depends_on):
                raise OrchestrationError(
                    f"node {node_id} has unfinished dependencies", node_id=node_id, recoverable=True
                )
            if self._record_policy_progress(session, node, context):
                session.status = "needs_replan"
                session.policy.gate = "loop_detected"
                session.policy.gate_status = "blocked"
                session.add_message(
                    "event",
                    f"{node.node_id}: loop detected",
                    node_id=node.node_id,
                    metadata={"policy_gate": "loop_detected"},
                )
                self.store.save(session)
                self._emit(OrchestrationEvent(
                    "policy_loop_detected", session.session_id, node.node_id,
                    session.status, {"gate": "loop_detected"},
                ))
                return session
            run = self._run_node(session, node, context)
            runs[node_id] = run
            session.add_message(
                "event",
                f"{node.node_id}: {run.status}",
                node_id=node.node_id,
                metadata={"node_run": run.to_dict()},
            )
            if run.status == "completed":
                context = context.merge(run.output)
                session.context = context
                next_node = next((item for item in order if item not in completed | skipped and item != node_id), "")
                session.policy.current_phase = node.node_type
                session.policy.next_action = f"run:{next_node}" if next_node else "complete"
                completed.add(node_id)
                self._emit(OrchestrationEvent("node_completed", session.session_id, node_id, run.status, run.output))
            elif run.status == "skipped":
                skipped.add(node_id)
                self._emit(OrchestrationEvent("node_skipped", session.session_id, node_id, run.status, {"error": run.error}))
            else:
                session.status = "waiting_for_human" if node.on_failure == "human" else "failed"
                if run.output:
                    context = context.merge(run.output)
                session.context = context.merge({"errors": [run.error]})
                output_gate = run.output.get("gate", {}) if isinstance(run.output, dict) else {}
                if isinstance(output_gate, dict) and output_gate.get("status") == "blocked":
                    session.policy.gate = str(output_gate.get("name", "quality_review"))
                    session.policy.gate_status = "blocked"
                elif node.on_failure == "human":
                    session.policy.gate = "human_review"
                    session.policy.gate_status = "blocked"
                elif node.on_failure == "replan":
                    session.policy.gate = "plan_approval"
                    session.policy.gate_status = "blocked"
                self.store.save(session)
                self._emit(OrchestrationEvent("node_failed", session.session_id, node_id, run.status, {"error": run.error}))
                if node.on_failure == "replan":
                    session.status = "needs_replan"
                return session
            self.store.save(session)

        session.status = "completed"
        session.context = context
        session.policy.current_phase = "completed"
        session.policy.next_action = ""
        session.policy.gate = ""
        if session.policy.gate_status != "blocked":
            session.policy.gate_status = "approved"
        self.store.save(session)
        self._emit(OrchestrationEvent("session_completed", session.session_id, status=session.status))
        return session

    def _run_node(self, session: Session, node: WorkflowNode, context: ContextState) -> NodeRun:
        definition = self.registry.get(node.node_type)
        run = NodeRun(node_id=node.node_id, model_alias=node.model_alias or definition.default_model, status="running", started_at=utc_now())
        self._emit(OrchestrationEvent("node_started", session.session_id, node.node_id, run.status, {"model_alias": run.model_alias}))
        handler = self.registry.handler(node.node_type)
        output: dict[str, Any] | None = None
        for attempt in range(1, self.max_attempts + 1):
            run.attempt = attempt
            try:
                if handler is not None:
                    output = handler({
                        "session": session,
                        "node": node,
                        "context": context,
                        "context_pack": self._context_pack(session, node, context),
                    })
                else:
                    if self.invoker is not None:
                        output = self.invoker.invoke(
                            session=session,
                            node=node,
                            context=context,
                            model_alias=run.model_alias,
                            output_fields=definition.output_fields,
                        )
                    else:
                        gateway_node = replace(
                            node,
                            config={
                                **node.config,
                                "_output_fields": list(definition.output_fields),
                                "_context_pack": self._context_pack(session, node, context),
                            },
                        )
                        output = self.gateway.complete(model_alias=run.model_alias, node=gateway_node, context=context)
                    if "inputs" in output or "errors" in output:
                        raise ValueError("model output cannot replace authoritative inputs or errors")
                # Reject malformed output, and validate the shared-state
                # envelope, before any side effect runs.  ``merge`` is called
                # for its validation only; the authoritative context is
                # replaced by the caller after the run succeeds.
                definition.validate_output(output)
                context.merge(output)
                break
            except Exception as error:  # node policy decides whether this is terminal
                run.error = str(error)
                if attempt < self.max_attempts and node.on_failure == "retry":
                    self._emit(OrchestrationEvent("node_retrying", session.session_id, node.node_id, "retrying", {"attempt": attempt, "error": str(error)}))
                    continue
                return self._failed_run(run, node)

        if output is None:  # defensive: the loop always assigns or returns
            run.error = run.error or "node produced no output"
            return self._failed_run(run, node)

        # Side effects run exactly once, outside the retry loop.  A publish
        # followed by a validation failure must not re-invoke the model and
        # write a second, different version of the same files: the first batch
        # is already durable and there is no cross-attempt rollback.
        try:
            if self.output_processor is not None:
                output = self.output_processor(session, node, context, output)
            definition.validate_output(output)
            context.merge(output)
        except Exception as error:  # noqa: BLE001 - failure policy owns the verdict
            run.error = str(error)
            return self._failed_run(run, node)

        gate = output.get("gate", {}) if isinstance(output, dict) else {}
        facts = output.get("facts", {}) if isinstance(output.get("facts", {}), dict) else {}
        review_blocked = (
            node.node_type == "review"
            and str(output.get("verdict", facts.get("verdict", "pass"))).lower() != "pass"
        )
        if review_blocked and not gate:
            gate = {
                "name": "quality_review", "status": "blocked",
                "reason": f"review verdict: {output.get('verdict', 'revise')}",
            }
            output["gate"] = gate
        if isinstance(gate, dict) and gate.get("status") == "blocked":
            run.status = "failed"
            run.output = output
            run.error = str(gate.get("reason", "quality gate blocked"))
            run.finished_at = utc_now()
            return run
        run.status = "completed"
        run.output = output
        run.finished_at = utc_now()
        return run

    @staticmethod
    def _failed_run(run: NodeRun, node: WorkflowNode) -> NodeRun:
        """Apply the node's failure policy to an exhausted or side-effect failure."""

        run.finished_at = utc_now()
        run.status = "skipped" if node.on_failure == "skip" else "failed"
        return run

    @staticmethod
    def _context_pack(session: Session, node: WorkflowNode, context: ContextState) -> dict[str, Any]:
        recent_events = [
            {
                "node_id": message.node_id,
                "content": message.content,
                "status": message.metadata.get("node_run", {}).get("status", ""),
            }
            for message in session.messages
            if message.role == "event"
        ][-12:]
        return {
            "task": {
                "session_id": session.session_id,
                "title": session.title,
                "status": session.status,
                "policy": session.policy.to_dict(),
                "next_action": session.policy.next_action,
            },
            "node": {
                "node_id": node.node_id,
                "node_type": node.node_type,
                "model_alias": node.model_alias,
                "depends_on": list(node.depends_on),
            },
            "shared_context": context.to_dict(),
            "recent_events": recent_events,
        }

    @staticmethod
    def _record_policy_progress(session: Session, node: WorkflowNode, context: ContextState) -> bool:
        signature = {
            "phase": node.node_type,
            "next_action": f"run:{node.node_id}",
            "context_version": context.version,
            "at": utc_now(),
        }
        history = session.policy.history
        history.append(signature)
        recent = history[-3:]
        same_phase = len(recent) == 3 and all(
            item.get("phase") == signature["phase"]
            and item.get("next_action") == signature["next_action"]
            for item in recent
        )
        same_context = same_phase and all(
            item.get("context_version") == signature["context_version"] for item in recent
        )
        failed_repeats = sum(
            1
            for message in session.messages
            if message.node_id == node.node_id
            and isinstance(message.metadata.get("node_run"), dict)
            and message.metadata["node_run"].get("status") == "failed"
        )
        loop = same_phase and (same_context or failed_repeats >= 2)
        session.policy.history = history[-50:]
        session.policy.current_phase = node.node_type
        session.policy.next_action = signature["next_action"]
        return loop

    def _emit(self, event: OrchestrationEvent) -> None:
        if self.event_sink is not None:
            self.event_sink(event)
