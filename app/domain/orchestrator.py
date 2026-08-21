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
        max_attempts: int = 2,
    ):
        self.registry = registry
        self.store = store
        self.gateway = gateway
        self.event_sink = event_sink
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
        session.mode = session.mode
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
                completed.add(node_id)
                self._emit(OrchestrationEvent("node_completed", session.session_id, node_id, run.status, run.output))
            elif run.status == "skipped":
                skipped.add(node_id)
                self._emit(OrchestrationEvent("node_skipped", session.session_id, node_id, run.status, {"error": run.error}))
            else:
                session.status = "waiting_for_human" if node.on_failure == "human" else "failed"
                session.context = context.merge({"errors": [run.error]})
                self.store.save(session)
                self._emit(OrchestrationEvent("node_failed", session.session_id, node_id, run.status, {"error": run.error}))
                if node.on_failure == "replan":
                    session.status = "needs_replan"
                return session
            self.store.save(session)

        session.status = "completed"
        session.context = context
        self.store.save(session)
        self._emit(OrchestrationEvent("session_completed", session.session_id, status=session.status))
        return session

    def _run_node(self, session: Session, node: WorkflowNode, context: ContextState) -> NodeRun:
        definition = self.registry.get(node.node_type)
        run = NodeRun(node_id=node.node_id, model_alias=node.model_alias or definition.default_model, status="running", started_at=utc_now())
        self._emit(OrchestrationEvent("node_started", session.session_id, node.node_id, run.status, {"model_alias": run.model_alias}))
        handler = self.registry.handler(node.node_type)
        for attempt in range(1, self.max_attempts + 1):
            run.attempt = attempt
            try:
                if handler is not None:
                    output = handler({
                        "session": session,
                        "node": node,
                        "context": context,
                    })
                else:
                    gateway_node = replace(
                        node,
                        config={**node.config, "_output_fields": list(definition.output_fields)},
                    )
                    output = self.gateway.complete(model_alias=run.model_alias, node=gateway_node, context=context)
                definition.validate_output(output)
                run.status = "completed"
                run.output = output
                run.finished_at = utc_now()
                return run
            except Exception as error:  # node policy decides whether this is terminal
                run.error = str(error)
                if attempt < self.max_attempts and node.on_failure == "retry":
                    self._emit(OrchestrationEvent("node_retrying", session.session_id, node.node_id, "retrying", {"attempt": attempt, "error": str(error)}))
                    continue
                run.finished_at = utc_now()
                if node.on_failure == "skip":
                    run.status = "skipped"
                else:
                    run.status = "failed"
                return run
        return run

    def _emit(self, event: OrchestrationEvent) -> None:
        if self.event_sink is not None:
            self.event_sink(event)
