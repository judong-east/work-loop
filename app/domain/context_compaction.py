"""Bounded, durable context views for model invocations.

The V2 runtime stores an authoritative structured :class:`ContextState`.  This
module deliberately keeps that state intact and produces a smaller view for an
individual model call.  It follows the useful parts of Pi's compaction model:
budget before a call, preserve recent/critical material, generate a structured
summary when a summarizer is available, and persist a compact event that can be
used after a restart.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable

from app.core.contracts import new_id, utc_now

from .models import ContextState, Session, WorkflowNode


DEFAULT_CONTEXT_WINDOW_TOKENS = 32_000
DEFAULT_RESERVE_TOKENS = 4_096
DEFAULT_KEEP_RECENT_TOKENS = 12_000
DEFAULT_SUMMARY_MAX_TOKENS = 1_500
DEFAULT_MAX_COMPACTIONS = 2
DEFAULT_MAX_SUMMARY_INPUT_CHARS = 60_000
DEFAULT_MAX_FACT_CHARS = 3_000
DEFAULT_MAX_FILE_CHARS = 1_200
DEFAULT_MAX_ARTIFACT_CHARS = 2_000
DEFAULT_MAX_PROJECT_INSTRUCTIONS_CHARS = 6_000
DEFAULT_MAX_REQUEST_CHARS = 4_000


SummaryCallback = Callable[[str, dict[str, Any] | None], dict[str, Any]]


def estimate_tokens(value: Any) -> int:
    """Return a conservative, dependency-free token estimate.

    Providers use different tokenizers and this project intentionally has no
    tokenizer dependency.  UTF-8 byte and character estimates are combined so
    CJK-heavy content does not receive the optimistic English-only estimate.
    """

    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if not text:
        return 1
    char_estimate = (len(text) + 3) // 4
    byte_estimate = (len(text.encode("utf-8")) + 2) // 3
    return max(1, char_estimate, byte_estimate)


@dataclass(frozen=True)
class ContextBudget:
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS
    summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS
    max_compactions: int = DEFAULT_MAX_COMPACTIONS
    enabled: bool = True

    def validate(self) -> None:
        if self.context_window_tokens <= 0:
            raise ValueError("context_window_tokens must be positive")
        if self.reserve_tokens <= 0:
            raise ValueError("reserve_tokens must be positive")
        if self.keep_recent_tokens <= 0:
            raise ValueError("keep_recent_tokens must be positive")
        if self.summary_max_tokens <= 0:
            raise ValueError("summary_max_tokens must be positive")
        if self.max_compactions < 0:
            raise ValueError("max_compactions cannot be negative")
        # Only the output reserve is a hard structural constraint.  A large
        # ``keep_recent_tokens`` is clamped by ``recent_tokens`` instead of
        # rejected, so an 8k model stays usable with default policy.
        if self.reserve_tokens >= self.context_window_tokens:
            raise ValueError("reserve_tokens must be smaller than the context window")

    @property
    def trigger_tokens(self) -> int:
        """Maximum input tokens for one request."""

        return self.context_window_tokens - self.reserve_tokens

    @property
    def recent_tokens(self) -> int:
        """Effective sub-budget for recent events and tool results.

        This is the value the reducer actually spends, so a configured
        ``keep_recent_tokens`` larger than the request budget degrades into a
        smaller share instead of failing the call.
        """

        return max(1, min(self.keep_recent_tokens, self.trigger_tokens // 2))

    @property
    def summary_input_tokens(self) -> int:
        """Upper bound for what may be handed to the summarization model."""

        return max(1, self.trigger_tokens - self.summary_max_tokens)


@dataclass
class ContextView:
    shared_context: dict[str, Any]
    context_pack: dict[str, Any]
    summary: dict[str, Any] | None
    estimated_tokens: int
    compacted: bool = False
    compaction_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "shared_context": copy.deepcopy(self.shared_context),
            "context_pack": copy.deepcopy(self.context_pack),
            "summary": copy.deepcopy(self.summary),
            "estimated_tokens": self.estimated_tokens,
            "compacted": self.compacted,
            "compaction_id": self.compaction_id,
        }


class ContextCompactor:
    """Prepare bounded model context without mutating authoritative state."""

    def __init__(
        self,
        *,
        summary_callback: SummaryCallback | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.summary_callback = summary_callback
        self.event_sink = event_sink

    def budget_for(
        self,
        *,
        context: ContextState,
        node: WorkflowNode,
        model_context_window: int | None = None,
    ) -> ContextBudget:
        project = context.inputs.get("project", {})
        project_policy = project.get("runtime_policy", {}) if isinstance(project, dict) else {}
        policy = project_policy.get("compaction", {}) if isinstance(project_policy, dict) else {}
        node_policy = node.config.get("compaction", {}) if isinstance(node.config, dict) else {}
        if not isinstance(policy, dict):
            policy = {}
        if not isinstance(node_policy, dict):
            node_policy = {}

        def integer(name: str, fallback: int) -> int:
            value = node_policy.get(name, policy.get(name, fallback))
            try:
                return int(value)
            except (TypeError, ValueError):
                return fallback

        window = integer(
            "context_window_tokens",
            model_context_window or DEFAULT_CONTEXT_WINDOW_TOKENS,
        )
        budget = ContextBudget(
            context_window_tokens=max(1, window),
            reserve_tokens=max(1, integer("reserve_tokens", DEFAULT_RESERVE_TOKENS)),
            keep_recent_tokens=max(1, integer("keep_recent_tokens", DEFAULT_KEEP_RECENT_TOKENS)),
            summary_max_tokens=max(1, integer("summary_max_tokens", DEFAULT_SUMMARY_MAX_TOKENS)),
            max_compactions=max(0, integer("max_compactions", DEFAULT_MAX_COMPACTIONS)),
            enabled=bool(node_policy.get("enabled", policy.get("enabled", True))),
        )
        # Reject only structurally impossible budgets (non-positive values, or a
        # reserve that leaves no input room).  An oversized keep-recent value is
        # clamped by ``ContextBudget.recent_tokens``, so ordinary small-window
        # models do not fail here.
        budget.validate()
        return budget

    def prepare(
        self,
        *,
        session: Session,
        node: WorkflowNode,
        context: ContextState,
        model_context_window: int | None = None,
    ) -> ContextView:
        budget = self.budget_for(
            context=context,
            node=node,
            model_context_window=model_context_window,
        )
        raw_context = context.to_dict()
        context_pack = self._context_pack(
            session,
            node,
            raw_context,
            recent_tokens=budget.recent_tokens,
        )
        initial = self._compose_view(raw_context, context_pack)
        initial_tokens = estimate_tokens(initial)
        if not budget.enabled or initial_tokens <= budget.trigger_tokens:
            return ContextView(raw_context, context_pack, self._latest_summary(session), initial_tokens)

        current = self._deterministic_reduce(raw_context)
        reduced_pack = self._context_pack(
            session,
            node,
            current,
            recent_tokens=budget.recent_tokens,
        )
        reduced = self._compose_view(current, reduced_pack)
        reduced_tokens = estimate_tokens(reduced)
        summary = self._latest_summary(session)
        compactions = self._compaction_count(session)

        can_persist_compaction = compactions < budget.max_compactions
        if reduced_tokens > budget.trigger_tokens and can_persist_compaction:
            # The summarization call is itself a model request and must fit the
            # summarizer's window.  Bound it by the budget rather than by a
            # fixed character constant that ignores the model entirely.
            summary_input = self._summary_input(
                raw_context,
                context_pack,
                previous_summary=summary,
                max_tokens=budget.summary_input_tokens,
            )
            if self.summary_callback is not None:
                try:
                    candidate = self.summary_callback(summary_input, summary)
                    if isinstance(candidate, dict):
                        summary = self._bound_summary(candidate, budget.summary_max_tokens)
                except Exception as error:  # summary is an optimization, not authority
                    self._emit({
                        "event_type": "context_compaction_failed",
                        "session_id": session.session_id,
                        "node_id": node.node_id,
                        "error": str(error),
                    })
            if summary is None:
                summary = self._deterministic_summary(raw_context, context_pack)
            summary = self._bound_summary(summary, budget.summary_max_tokens)
            current = self._apply_summary(current, summary)
            reduced_pack = self._context_pack(
                session,
                node,
                current,
                recent_tokens=budget.recent_tokens,
                summary=summary,
            )
            reduced = self._compose_view(current, reduced_pack)
            reduced_tokens = estimate_tokens(reduced)

        # A final deterministic reduction guarantees that a failed or weak
        # summarizer cannot cause an unbounded request.  Critical inputs are
        # preserved; the caller can still reject an impossible budget.
        if reduced_tokens > budget.trigger_tokens:
            current = self._hard_reduce(current, budget.trigger_tokens)
            reduced_pack = self._context_pack(
                session,
                node,
                current,
                recent_tokens=min(budget.recent_tokens, max(1, budget.trigger_tokens // 4)),
                summary=summary,
            )
            reduced = self._compose_view(current, reduced_pack)
            reduced_tokens = estimate_tokens(reduced)

        # Once the configured durable-compaction quota is exhausted we still
        # return a bounded view, but do not append duplicate events on every
        # retry.  ``enabled`` is the hard off switch; ``max_compactions`` is a
        # persistence/summarisation quota.
        if not can_persist_compaction:
            return ContextView(current, reduced_pack, summary, reduced_tokens, True, "")

        compaction_id = new_id("COMPACT")
        entry = {
            "compaction_id": compaction_id,
            "node_id": node.node_id,
            "source_context_version": context.version,
            "tokens_before": initial_tokens,
            "tokens_after": reduced_tokens,
            "summary": copy.deepcopy(summary),
            "created_at": utc_now(),
        }
        session.add_message(
            "event",
            f"context compacted: {node.node_id}",
            node_id=node.node_id,
            metadata={"context_compaction": entry},
        )
        self._emit({
            "event_type": "context_compaction_completed",
            "session_id": session.session_id,
            "node_id": node.node_id,
            **entry,
        })
        return ContextView(current, reduced_pack, summary, reduced_tokens, True, compaction_id)

    @staticmethod
    def _compose_view(context: dict[str, Any], context_pack: dict[str, Any]) -> dict[str, Any]:
        # ``context_pack`` retains its historical ``shared_context`` field for
        # diagnostics, but it must not be counted or sent twice to a model.
        bounded_pack = {
            key: value for key, value in context_pack.items() if key != "shared_context"
        }
        return {"shared_context": context, "context_pack": bounded_pack}

    @classmethod
    def _context_pack(
        cls,
        session: Session,
        node: WorkflowNode,
        context: dict[str, Any],
        *,
        recent_tokens: int,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build one diagnostic envelope bounded by the recent-history budget.

        ``recent_tokens`` is spent newest-first across tool results and events,
        which is what makes the configured ``keep_recent_tokens`` observable
        instead of decorative.
        """

        tool_budget = max(1, recent_tokens * 2 // 3)
        recent_tools = cls._recent_within(
            [message for message in session.messages if message.role == "tool"],
            tool_budget,
            lambda message: {
                "node_id": message.node_id,
                "content": message.content,
                "tool": message.metadata.get("tool_event", {}).get("name", ""),
            },
        )
        spent = estimate_tokens(recent_tools) if recent_tools else 0
        recent_events = cls._recent_within(
            [message for message in session.messages if message.role == "event"],
            max(1, recent_tokens - spent),
            lambda message: {
                "node_id": message.node_id,
                "content": message.content,
                "status": message.metadata.get("node_run", {}).get("status", ""),
            },
        )
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
            "shared_context": copy.deepcopy(context),
            "recent_events": recent_events,
            "recent_tools": recent_tools,
            "summary": copy.deepcopy(summary),
        }

    @classmethod
    def _recent_within(
        cls,
        messages: list[Any],
        budget_tokens: int,
        project: Callable[[Any], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Take newest-first entries until ``budget_tokens`` is exhausted."""

        selected: list[dict[str, Any]] = []
        spent = 0
        for message in reversed(messages):
            entry = project(message)
            cost = estimate_tokens(entry)
            if cost > budget_tokens:
                # A single oversized entry is clipped rather than dropped: the
                # most recent tool result is usually the reason for the call.
                if selected:
                    break
                entry["content"] = cls._clip(entry.get("content", ""), max(200, budget_tokens * 3))
                selected.append(entry)
                break
            if spent + cost > budget_tokens:
                break
            spent += cost
            selected.append(entry)
        selected.reverse()
        return selected

    @staticmethod
    def _clip(value: Any, limit: int) -> Any:
        if not isinstance(value, str):
            encoded = json.dumps(value, ensure_ascii=False, default=str)
            return encoded if len(encoded) <= limit else encoded[:limit] + "…"
        if len(value) <= limit:
            return value
        head = max(1, int(limit * 0.65))
        tail = max(1, limit - head)
        return value[:head] + "\n…[truncated]…\n" + value[-tail:]

    @classmethod
    def _deterministic_reduce(cls, context: dict[str, Any]) -> dict[str, Any]:
        reduced = copy.deepcopy(context)
        facts = reduced.get("facts", {})
        if isinstance(facts, dict):
            reduced["facts"] = {str(key): cls._clip(value, DEFAULT_MAX_FACT_CHARS) for key, value in facts.items()}
        artifacts = reduced.get("artifacts", {})
        if isinstance(artifacts, dict):
            reduced["artifacts"] = {
                str(key): cls._clip(value, DEFAULT_MAX_ARTIFACT_CHARS)
                for key, value in artifacts.items()
            }
        reduced["decisions"] = list(reduced.get("decisions", []))[-20:]
        reduced["errors"] = list(reduced.get("errors", []))[-20:]
        inputs = reduced.get("inputs", {})
        if isinstance(inputs, dict):
            if "request" in inputs:
                inputs["request"] = cls._clip(inputs["request"], DEFAULT_MAX_REQUEST_CHARS)
            project = inputs.get("project")
            if isinstance(project, dict):
                project["instructions"] = cls._clip(
                    project.get("instructions", ""),
                    DEFAULT_MAX_PROJECT_INSTRUCTIONS_CHARS,
                )
                if isinstance(project.get("knowledge_refs"), list):
                    project["knowledge_refs"] = project["knowledge_refs"][-50:]
                inputs["project"] = project
            workspace = inputs.get("workspace")
            if isinstance(workspace, dict):
                files = workspace.get("files", [])
                if isinstance(files, list):
                    compact_files: list[dict[str, Any]] = []
                    for item in files[:40]:
                        if not isinstance(item, dict):
                            continue
                        compact_item = {
                            key: item[key]
                            for key in ("path", "size", "binary", "truncated")
                            if key in item
                        }
                        if "content" in item and not item.get("binary"):
                            compact_item["content"] = cls._clip(item.get("content", ""), DEFAULT_MAX_FILE_CHARS)
                        compact_files.append(compact_item)
                    workspace["files"] = compact_files
                inputs["workspace"] = workspace
            # Project instructions are authoritative.  Preserve them unless
            # the hard fallback below has no other way to fit the budget.
            reduced["inputs"] = inputs
        return reduced

    @classmethod
    def _hard_reduce(cls, context: dict[str, Any], target_tokens: int) -> dict[str, Any]:
        reduced = cls._deterministic_reduce(context)
        inputs = reduced.get("inputs", {})
        if isinstance(inputs, dict):
            workspace = inputs.get("workspace")
            if isinstance(workspace, dict) and isinstance(workspace.get("files"), list):
                for item in workspace["files"]:
                    if isinstance(item, dict) and "content" in item:
                        item["content"] = cls._clip(item["content"], 240)
                workspace["files"] = workspace["files"][:20]
            if workspace is not None:
                inputs["workspace"] = workspace
            reduced["inputs"] = inputs
        facts = reduced.get("facts", {})
        if isinstance(facts, dict):
            reduced["facts"] = {key: cls._clip(value, 600) for key, value in list(facts.items())[-20:]}
        reduced["decisions"] = list(reduced.get("decisions", []))[-8:]
        artifacts = reduced.get("artifacts", {})
        if isinstance(artifacts, dict):
            reduced["artifacts"] = {
                key: cls._clip(value, 400)
                for key, value in list(artifacts.items())[-12:]
            }
        inputs = reduced.get("inputs", {})
        if isinstance(inputs, dict):
            if "request" in inputs:
                inputs["request"] = cls._clip(inputs["request"], 1_200)
            project = inputs.get("project")
            if isinstance(project, dict):
                project["instructions"] = cls._clip(project.get("instructions", ""), 1_500)
                inputs["project"] = project
            reduced["inputs"] = inputs
        # Keep cutting low-value collections until the estimate is below the
        # target or no safe content remains to remove.  The running total is
        # adjusted per section instead of re-serializing the whole context on
        # every iteration, which kept this loop quadratic on the hot path.
        total = estimate_tokens(reduced)
        while total > target_tokens:
            changed = False
            for section in ("errors", "decisions", "facts"):
                value = reduced.get(section)
                if isinstance(value, list) and len(value) > 3:
                    before = estimate_tokens(value)
                    trimmed = value[-max(1, len(value) // 2):]
                    reduced[section] = trimmed
                    total -= before - estimate_tokens(trimmed)
                    changed = True
                    break
                if isinstance(value, dict) and len(value) > 3:
                    before = estimate_tokens(value)
                    keys = list(value)[-max(1, len(value) // 2):]
                    trimmed_map = {item_key: value[item_key] for item_key in keys}
                    reduced[section] = trimmed_map
                    total -= before - estimate_tokens(trimmed_map)
                    changed = True
                    break
            if not changed:
                break
        if estimate_tokens(reduced) > target_tokens:
            # An unusually small model window can leave no room for the normal
            # diagnostic envelope.  Keep only the request anchor, project
            # identity/instructions, file paths, and the latest error; this is
            # still enough for the model to ask for more evidence explicitly.
            minimal_inputs: dict[str, Any] = {}
            source_inputs = reduced.get("inputs", {})
            if isinstance(source_inputs, dict):
                if "request" in source_inputs:
                    minimal_inputs["request"] = cls._clip(source_inputs["request"], 300)
                source_project = source_inputs.get("project")
                if isinstance(source_project, dict):
                    minimal_inputs["project"] = {
                        key: cls._clip(source_project.get(key, ""), 300)
                        for key in ("project_id", "name", "instructions", "workspace_path")
                        if source_project.get(key, "")
                    }
                source_workspace = source_inputs.get("workspace")
                if isinstance(source_workspace, dict):
                    minimal_inputs["workspace"] = {
                        "root": source_workspace.get("root", ""),
                        "files": [
                            {"path": item.get("path", ""), "size": item.get("size", 0)}
                            for item in source_workspace.get("files", [])[:10]
                            if isinstance(item, dict) and item.get("path")
                        ],
                    }
            reduced = {
                "facts": {},
                "artifacts": {},
                "decisions": [],
                "inputs": minimal_inputs,
                "errors": list(reduced.get("errors", []))[-1:],
                "version": reduced.get("version", 1),
            }
        return reduced

    @classmethod
    def _bound_summary(cls, summary: dict[str, Any], summary_max_tokens: int) -> dict[str, Any]:
        """Keep an untrusted model summary within its configured token budget."""

        if not isinstance(summary, dict):
            return cls._deterministic_summary({}, {})
        limit = max(256, int(summary_max_tokens) * 4)
        encoded = json.dumps(summary, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded) <= limit:
            return copy.deepcopy(summary)
        # Preserve the stable summary envelope first, then clip individual
        # fields.  This keeps the summary useful without allowing a model to
        # bypass the compaction budget by returning a large nested object.
        bounded: dict[str, Any] = {}
        for key, value in summary.items():
            remaining = max(128, limit - len(json.dumps(bounded, ensure_ascii=False, default=str)))
            if isinstance(value, list):
                bounded[key] = [cls._clip(item, max(80, remaining // max(1, len(value)))) for item in value[-20:]]
            elif isinstance(value, dict):
                bounded[key] = {
                    str(item_key): cls._clip(item_value, max(80, remaining // max(1, len(value))))
                    for item_key, item_value in list(value.items())[-20:]
                }
            else:
                bounded[key] = cls._clip(value, max(80, remaining))
            if len(json.dumps(bounded, ensure_ascii=False, default=str)) >= limit:
                break
        return bounded

    @staticmethod
    def _summary_input(
        context: dict[str, Any],
        context_pack: dict[str, Any],
        *,
        previous_summary: dict[str, Any] | None,
        max_tokens: int,
    ) -> str:
        value = {
            "context": context,
            "context_pack": context_pack,
            "previous_summary": previous_summary,
        }
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        # ``estimate_tokens`` combines char and UTF-8 byte estimates, so invert
        # it conservatively at 3 bytes/token and cap by the absolute char limit.
        char_limit = min(max(1, max_tokens) * 3, DEFAULT_MAX_SUMMARY_INPUT_CHARS)
        if len(encoded) <= char_limit:
            return encoded
        return encoded[:char_limit] + "…[summary input truncated]"

    @staticmethod
    def _deterministic_summary(context: dict[str, Any], context_pack: dict[str, Any]) -> dict[str, Any]:
        inputs = context.get("inputs", {}) if isinstance(context, dict) else {}
        facts = context.get("facts", {}) if isinstance(context, dict) else {}
        return {
            "goal": str(inputs.get("request", "")) if isinstance(inputs, dict) else "",
            "constraints": [
                str(item)
                for item in (
                    context_pack.get("task", {}).get("policy", {}).get("gate", ""),
                    context_pack.get("task", {}).get("policy", {}).get("next_action", ""),
                )
                if item
            ],
            "done": [str(key) for key in facts.keys()] if isinstance(facts, dict) else [],
            "in_progress": [],
            "blocked": list(context.get("errors", []))[-10:] if isinstance(context, dict) else [],
            "decisions": list(context.get("decisions", []))[-10:] if isinstance(context, dict) else [],
            "critical_context": [],
            "searched_files": [],
            "modified_files": [],
            "open_questions": [],
        }

    @staticmethod
    def _apply_summary(context: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
        reduced = copy.deepcopy(context)
        reduced["context_summary"] = copy.deepcopy(summary)
        return reduced

    @staticmethod
    def _latest_summary(session: Session) -> dict[str, Any] | None:
        for message in reversed(session.messages):
            value = message.metadata.get("context_compaction")
            if isinstance(value, dict) and isinstance(value.get("summary"), dict):
                return copy.deepcopy(value["summary"])
        return None

    @staticmethod
    def _compaction_count(session: Session) -> int:
        return sum(1 for message in session.messages if isinstance(message.metadata.get("context_compaction"), dict))

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(payload)
