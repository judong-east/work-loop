"""Single model invocation boundary for Workloop V2."""

from __future__ import annotations

import json
from dataclasses import replace
from collections.abc import Callable, Iterator
from typing import Any

from app.domain.context_compaction import ContextCompactor
from app.domain.models import ContextState, Session, SessionMode, WorkflowNode
from app.domain.orchestrator import ModelGateway
from app.domain.tooling import SEARCH_TOOLS, ToolSpec
from app.infrastructure.zvec_grep import ZvecGrepClient


class ModelInvocationService:
    """Prepare context, optionally run local search tools, then call a gateway.

    The service deliberately keeps ``ModelGateway.complete`` compatible with
    existing test doubles and integrations.  Provider-native tool calling is
    used only when the gateway exposes ``complete_with_tools``.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        search_client: ZvecGrepClient | None = None,
        compactor: ContextCompactor | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.gateway = gateway
        self.search_client = search_client or ZvecGrepClient()
        # The sink makes compaction observable for flows whose session is not
        # persisted (goal decomposition runs on an ephemeral session, so its
        # compaction messages would otherwise vanish with the object).
        self.compactor = compactor or ContextCompactor(
            summary_callback=self._summarize,
            event_sink=event_sink,
        )

    def invoke(
        self,
        *,
        session: Session,
        node: WorkflowNode,
        context: ContextState,
        model_alias: str = "",
        output_fields: tuple[str, ...] | list[str] = (),
    ) -> dict[str, Any]:
        effective_alias = model_alias or node.model_alias
        context_window = self._context_window(effective_alias)
        budget = self.compactor.budget_for(
            context=context,
            node=node,
            model_context_window=context_window,
        )
        view = self.compactor.prepare(
            session=session,
            node=node,
            context=context,
            model_context_window=context_window,
        )
        internal_config = {
            **node.config,
            "_output_fields": list(output_fields) or node.config.get("_output_fields", []),
            "_context_pack": view.context_pack,
            "_context_view": view.to_dict(),
            "_reserve_tokens": budget.reserve_tokens,
        }
        gateway_node = replace(node, config=internal_config)
        tool_names = self._tool_names(node, context)
        complete_with_tools = getattr(self.gateway, "complete_with_tools", None)
        if tool_names and callable(complete_with_tools):
            specs = [SEARCH_TOOLS[name] for name in tool_names if name in SEARCH_TOOLS]
            if specs:
                output, events = complete_with_tools(
                    model_alias=effective_alias,
                    node=gateway_node,
                    context=context,
                    tools=specs,
                    tool_executor=self._execute_tool(context),
                    max_rounds=self._tool_rounds(node, context),
                    transcript_budget_tokens=budget.trigger_tokens,
                )
                self._persist_tool_events(session, node, events)
                return output
        return self.gateway.complete(
            model_alias=effective_alias,
            node=gateway_node,
            context=context,
        )

    def invoke_stream(
        self,
        *,
        session: Session,
        node: WorkflowNode,
        context: ContextState,
        model_alias: str = "",
        output_fields: tuple[str, ...] | list[str] = (),
    ) -> Iterator[dict[str, Any]]:
        """Yield provider-neutral model events while preserving invocation policy.

        Gateways that implement ``stream_complete_with_tools`` provide true
        provider streaming.  Older/fake gateways remain compatible: their
        existing ``invoke`` path runs once and is represented as one delta plus
        a done event, so HTTP callers can use the same SSE contract.
        """

        effective_alias = model_alias or node.model_alias
        context_window = self._context_window(effective_alias)
        budget = self.compactor.budget_for(
            context=context,
            node=node,
            model_context_window=context_window,
        )
        view = self.compactor.prepare(
            session=session,
            node=node,
            context=context,
            model_context_window=context_window,
        )
        internal_config = {
            **node.config,
            "_output_fields": list(output_fields) or node.config.get("_output_fields", []),
            "_context_pack": view.context_pack,
            "_context_view": view.to_dict(),
            "_reserve_tokens": budget.reserve_tokens,
            "_stream_plain_text": True,
        }
        gateway_node = replace(node, config=internal_config)
        tool_names = self._tool_names(node, context)
        stream_with_tools = getattr(self.gateway, "stream_complete_with_tools", None)
        if callable(stream_with_tools):
            specs = [SEARCH_TOOLS[name] for name in tool_names if name in SEARCH_TOOLS]
            tool_events: list[dict[str, Any]] = []
            for event in stream_with_tools(
                model_alias=effective_alias,
                node=gateway_node,
                context=context,
                tools=specs,
                tool_executor=self._execute_tool(context),
                max_rounds=self._tool_rounds(node, context),
                transcript_budget_tokens=budget.trigger_tokens,
            ):
                if event.get("type") == "tool_result" and isinstance(event.get("event"), dict):
                    tool_events.append(dict(event["event"]))
                yield event
            self._persist_tool_events(session, node, tool_events)
            return

        output = self.invoke(
            session=session,
            node=node,
            context=context,
            model_alias=model_alias,
            output_fields=output_fields,
        )
        visible = str(output.get("result", output)) if isinstance(output, dict) else str(output)
        if visible:
            yield {"type": "text_delta", "text": visible}
        yield {
            "type": "done",
            "output": output,
            "model": output.get("model", effective_alias) if isinstance(output, dict) else effective_alias,
            "tool_events": [],
        }

    def invoke_ephemeral(
        self,
        *,
        project_id: str,
        title: str,
        node: WorkflowNode,
        context: ContextState,
        model_alias: str = "",
        output_fields: tuple[str, ...] | list[str] = (),
    ) -> dict[str, Any]:
        """Invoke a model for non-session flows such as goal decomposition."""

        session = Session.create(project_id, title, SessionMode.TASK)
        return self.invoke(
            session=session,
            node=node,
            context=context,
            model_alias=model_alias,
            output_fields=output_fields,
        )

    def _summarize(self, summary_input: str, previous_summary: dict[str, Any] | None) -> dict[str, Any]:
        summarize = getattr(self.gateway, "summarize_context", None)
        if not callable(summarize):
            raise RuntimeError("gateway does not support context summarization")
        alias = self._summary_alias()
        if not alias:
            raise RuntimeError("no summary model alias configured")
        return summarize(
            model_alias=alias,
            summary_input=summary_input,
            previous_summary=previous_summary,
        )

    def _summary_alias(self) -> str:
        """Return a model explicitly declared for summarization, or nothing.

        There is deliberately no "first enabled alias" fallback: that made the
        summarizer depend on catalog enumeration order and could route
        compaction to the main model whose window is already under pressure.
        Without an explicit summarization model the compactor uses its
        deterministic summary instead.
        """

        resources = getattr(self.gateway, "resources", None)
        if resources is None:
            return ""
        for item in resources.list_models():
            if "summarization" not in item.capabilities:
                continue
            try:
                resources.resolve(item.alias)
            except (KeyError, ValueError):
                continue
            return item.alias
        return ""

    def _context_window(self, alias: str) -> int | None:
        resources = getattr(self.gateway, "resources", None)
        if resources is None or not alias:
            return None
        try:
            _, model = resources.resolve(alias)
        except (KeyError, ValueError):
            return None
        return model.context_window_tokens

    @staticmethod
    def _local_search_policy(context: ContextState) -> dict[str, Any]:
        """Read the project's local-search policy defensively, in one place."""

        project = context.inputs.get("project", {})
        policy = project.get("runtime_policy", {}) if isinstance(project, dict) else {}
        search = policy.get("local_search", {}) if isinstance(policy, dict) else {}
        return search if isinstance(search, dict) else {}

    def _tool_names(self, node: WorkflowNode, context: ContextState) -> list[str]:
        has_override = isinstance(node.config, dict) and "tools" in node.config
        raw = node.config.get("tools") if has_override else None
        if not has_override:
            search = self._local_search_policy(context)
            raw = search.get("tools", []) if search.get("enabled", True) else []
        if not isinstance(raw, list):
            return []
        names = [str(item) for item in raw if str(item) in SEARCH_TOOLS]
        if not names:
            return []
        # A tool is only offered when it can actually run.  Without a workspace
        # root or a local zvec-grep binary, every call would fail and burn one
        # model round each, up to the round limit.
        project = context.inputs.get("project", {})
        root = str(project.get("workspace_path", "")) if isinstance(project, dict) else ""
        if not root:
            return []
        if not self.search_client.available():
            return []
        return names

    def _tool_rounds(self, node: WorkflowNode, context: ContextState) -> int:
        raw = node.config.get("max_tool_rounds") if isinstance(node.config, dict) else None
        if raw is None:
            raw = self._local_search_policy(context).get("max_tool_rounds", 8)
        try:
            return max(1, min(int(raw), 12))
        except (TypeError, ValueError):
            return 8

    def _execute_tool(self, context: ContextState):
        project = context.inputs.get("project", {})
        root = str(project.get("workspace_path", "")) if isinstance(project, dict) else ""

        def execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if not root:
                raise ValueError("project workspace is not configured")
            if name == "zvec_grep_search":
                return self.search_client.semantic_search(
                    root=root,
                    query=arguments.get("query", ""),
                    fts=self._string_list(arguments, "fts"),
                    globs=self._string_list(arguments, "globs"),
                    file_types=self._string_list(arguments, "file_types"),
                    limit=arguments.get("limit", 10),
                    freshness=str(arguments.get("freshness", "eventual")),
                    fuse=bool(arguments.get("fuse", False)),
                )
            if name == "zvec_grep_rg":
                return self.search_client.exact_search(
                    root=root,
                    pattern=arguments.get("pattern", ""),
                    path=str(arguments.get("path") or ""),
                    globs=self._string_list(arguments, "globs"),
                    file_types=self._string_list(arguments, "file_types"),
                    literal=bool(arguments.get("literal", False)),
                    ignore_case=bool(arguments.get("ignore_case", False)),
                    context_lines=arguments.get("context_lines", 0),
                    limit=arguments.get("limit", 100),
                )
            raise ValueError(f"unknown local search tool: {name}")

        return execute

    @staticmethod
    def _string_list(arguments: dict[str, Any], key: str) -> list[str]:
        value = arguments.get(key, [])
        if value in (None, ""):
            return []
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"tool argument {key} must be an array of strings")
        return list(value)

    @staticmethod
    def _persist_tool_events(session: Session, node: WorkflowNode, events: list[dict[str, Any]]) -> None:
        for event in events:
            session.add_message(
                "tool",
                json.dumps(event, ensure_ascii=False, default=str),
                node_id=node.node_id,
                metadata={"tool_event": event},
            )
