from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from app.agents.contracts import AgentAccess, ExecutionPlan
from app.agents.plan_graph import (
    ModelBinding,
    PlanGraph,
    PlanNode,
    PlanNodeAccess,
    PlanNodeKind,
)


_PROFILE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_CAPABILITY = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_RUNTIMES = {"claude_code", "codex_cli", "pi_rpc", "native"}


@dataclass(frozen=True)
class ModelOption:
    profile_id: str
    label: str
    runtime: str
    model: str
    access: AgentAccess
    capabilities: list[str]
    quality: int
    input_cost_per_million: float
    output_cost_per_million: float
    cached_input_cost_per_million: float | None = None
    provider: str = ""
    thinking: str = "medium"
    context_window: int = 0
    # Native-harness endpoint wiring. The API key itself never enters the
    # catalog: api_key_env names the environment variable (or the variable
    # points at WORKLOOP_NATIVE_KEY_FILE) that holds it.
    base_url: str = ""
    api_key_env: str = ""
    max_tokens: int = 0

    @classmethod
    def from_dict(cls, data: Any) -> "ModelOption":
        if not isinstance(data, dict):
            raise ValueError("model catalog entry must be an object")
        try:
            access = AgentAccess(str(data.get("access", "")))
        except ValueError as error:
            raise ValueError("model access is invalid") from error
        raw_capabilities = data.get("capabilities", [])
        if not isinstance(raw_capabilities, list):
            raise ValueError("model capabilities must be an array")
        option = cls(
            profile_id=str(data.get("profile_id", data.get("id", ""))).strip(),
            label=str(data.get("label", data.get("profile_id", ""))).strip(),
            runtime=str(data.get("runtime", "")).strip(),
            provider=str(data.get("provider", "")).strip(),
            model=str(data.get("model", "")).strip(),
            access=access,
            capabilities=[str(item).strip().lower() for item in raw_capabilities],
            quality=int(data.get("quality", 3)),
            input_cost_per_million=float(data.get("input_cost_per_million", 0.0)),
            output_cost_per_million=float(data.get("output_cost_per_million", 0.0)),
            cached_input_cost_per_million=(
                float(data["cached_input_cost_per_million"])
                if data.get("cached_input_cost_per_million") is not None
                else None
            ),
            thinking=str(data.get("thinking", "medium")).strip() or "medium",
            context_window=int(data.get("context_window", 0)),
            base_url=str(data.get("base_url", "")).strip(),
            api_key_env=str(data.get("api_key_env", "")).strip(),
            max_tokens=int(data.get("max_tokens", 0)),
        )
        option.validate()
        return option

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "label": self.label,
            "runtime": self.runtime,
            "provider": self.provider,
            "model": self.model,
            "access": self.access.value,
            "capabilities": list(self.capabilities),
            "quality": self.quality,
            "input_cost_per_million": self.input_cost_per_million,
            "output_cost_per_million": self.output_cost_per_million,
            "cached_input_cost_per_million": self.cached_input_cost_per_million,
            "thinking": self.thinking,
            "context_window": self.context_window,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "max_tokens": self.max_tokens,
        }

    def validate(self) -> None:
        if not _PROFILE_ID.fullmatch(self.profile_id):
            raise ValueError("model profile_id is invalid")
        if not self.label.strip() or len(self.label) > 100:
            raise ValueError("model label must be non-empty and <= 100 characters")
        if self.runtime not in _RUNTIMES:
            raise ValueError("model runtime is invalid")
        if self.runtime == "claude_code" and self.access is not AgentAccess.READ_ONLY:
            raise ValueError("claude_code profiles must be read_only")
        if self.runtime == "codex_cli" and self.access is not AgentAccess.WORKSPACE_WRITE:
            raise ValueError("codex_cli profiles must use workspace_write")
        # native runs both access levels: read tools for read_only, file
        # mutation for workspace_write, both confined by the harness.
        if self.base_url and not re.fullmatch(r"https?://[^\s]+", self.base_url):
            raise ValueError("model base_url must be an http(s) URL")
        if self.api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("model api_key_env is invalid")
        if self.max_tokens < 0:
            raise ValueError("model max_tokens cannot be negative")
        if len(self.model) > 200:
            raise ValueError("model name must be <= 200 characters")
        if not 1 <= self.quality <= 5:
            raise ValueError("model quality must be between 1 and 5")
        if self.input_cost_per_million < 0 or self.output_cost_per_million < 0:
            raise ValueError("model token prices cannot be negative")
        if (
            self.cached_input_cost_per_million is not None
            and self.cached_input_cost_per_million < 0
        ):
            raise ValueError("model cached token price cannot be negative")
        if self.context_window < 0:
            raise ValueError("model context_window cannot be negative")
        if self.thinking not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("model thinking is invalid")
        if not self.capabilities:
            raise ValueError("model capabilities cannot be empty")
        normalized = [item.strip().lower() for item in self.capabilities]
        if any(not _CAPABILITY.fullmatch(item) for item in normalized):
            raise ValueError("model capability is invalid")
        if len(set(normalized)) != len(normalized):
            raise ValueError("model capabilities cannot contain duplicates")


class ModelCatalog:
    def __init__(self, models: list[ModelOption]):
        if not models:
            raise ValueError("model catalog cannot be empty")
        by_id: dict[str, ModelOption] = {}
        for model in models:
            model.validate()
            if model.profile_id in by_id:
                raise ValueError(f"duplicate model profile: {model.profile_id}")
            by_id[model.profile_id] = model
        self._models = tuple(models)
        self._by_id = by_id

    def list_all(self) -> list[ModelOption]:
        return list(self._models)

    def get(self, profile_id: str) -> ModelOption:
        try:
            return self._by_id[profile_id]
        except KeyError as error:
            raise ValueError(f"unknown model profile: {profile_id}") from error

    @classmethod
    def from_dict(cls, data: Any) -> "ModelCatalog":
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise ValueError("model catalog must contain a models array")
        return cls([ModelOption.from_dict(item) for item in data["models"]])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "models": [model.to_dict() for model in self._models],
        }


class ExecutionComposer:
    """Compile an approved plan into a priced, capability-aware task graph."""

    def __init__(self, catalog: ModelCatalog, optimization_goal: str = "balanced"):
        if optimization_goal not in {"cost", "balanced", "quality"}:
            raise ValueError("optimization_goal must be cost, balanced, or quality")
        self.catalog = catalog
        self.optimization_goal = optimization_goal

    def compose(self, plan: ExecutionPlan) -> PlanGraph:
        planning = self._select("planning", AgentAccess.READ_ONLY, plan.requirement_understanding)
        nodes: list[PlanNode] = []
        previous = ""
        for index, step in enumerate(plan.steps, start=1):
            capability = self._step_capability(step)
            selected = self._select(capability, AgentAccess.WORKSPACE_WRITE, step)
            node_id = f"step-{index}"
            title = (step[:159] + "...") if len(step) > 160 else step
            nodes.append(
                PlanNode(
                    node_id=node_id,
                    title=title,
                    kind=PlanNodeKind.IMPLEMENTATION,
                    capability=capability,
                    depends_on=[previous] if previous else [],
                    instructions=step,
                    model=self._binding(selected, capability, step),
                    access=PlanNodeAccess.WORKSPACE_WRITE,
                    inputs=["execution-plan"],
                    outputs=[f"{node_id}-changes"],
                )
            )
            previous = node_id

        reviewer = self._select("review", AgentAccess.READ_ONLY, plan.requirement_understanding)
        graph = PlanGraph(
            requirement_summary=plan.requirement_understanding,
            nodes=nodes,
            planning_model=self._binding(
                planning, "planning", plan.requirement_understanding
            ),
            review_model=self._binding(
                reviewer, "review", plan.requirement_understanding
            ),
        )
        graph.validate()
        return graph

    def select_binding(
        self,
        capability: str,
        access: AgentAccess,
        text: str,
    ) -> ModelBinding:
        selected = self._select(capability, access, text)
        return self._binding(selected, capability, text)

    def validate(self, graph: PlanGraph) -> None:
        graph.validate()
        by_id = {node.node_id: node for node in graph.nodes}
        if not graph.execution_nodes():
            raise ValueError("composed graph must contain at least one enabled model node")
        self._validate_phase_binding(graph.planning_model, "planning")
        self._validate_phase_binding(graph.review_model, "review")
        for node in graph.nodes:
            if not node.enabled:
                continue
            if node.kind not in {
                PlanNodeKind.IMPLEMENTATION,
                PlanNodeKind.INTEGRATION,
                PlanNodeKind.CUSTOM,
            }:
                raise ValueError(f"composed graph node kind is not executable: {node.kind.value}")
            dependencies = [by_id[item] for item in node.depends_on]
            if any(not dependency.enabled for dependency in dependencies):
                raise ValueError(f"enabled node {node.node_id} cannot depend on a disabled node")
            if not node.model.profile_id:
                raise ValueError(f"model-executed node {node.node_id} requires a model profile")
            option = self.catalog.get(node.model.profile_id)
            expected = (
                AgentAccess.WORKSPACE_WRITE
                if node.access is PlanNodeAccess.WORKSPACE_WRITE
                else AgentAccess.READ_ONLY
            )
            if option.access is not expected:
                raise ValueError(
                    f"model profile {option.profile_id} cannot satisfy {node.access.value} node {node.node_id}"
                )
            if node.access is PlanNodeAccess.WORKSPACE_WRITE and (
                "implementation" not in option.capabilities
                and "general" not in option.capabilities
            ):
                raise ValueError(
                    f"model profile {option.profile_id} lacks implementation for node {node.node_id}"
                )
            if node.capability not in option.capabilities and "general" not in option.capabilities:
                raise ValueError(
                    f"model profile {option.profile_id} lacks {node.capability} for node {node.node_id}"
                )
            if node.capability in {"security", "migration", "architecture"} and option.quality < 4:
                raise ValueError(
                    f"model profile {option.profile_id} quality is too low for {node.capability}"
                )

    def _validate_phase_binding(self, binding: ModelBinding, capability: str) -> None:
        if not binding.profile_id:
            raise ValueError(f"{capability} phase requires a model profile")
        option = self.catalog.get(binding.profile_id)
        if option.access is not AgentAccess.READ_ONLY:
            raise ValueError(f"{capability} phase model must be read_only")
        if capability not in option.capabilities and "general" not in option.capabilities:
            raise ValueError(f"model profile {option.profile_id} lacks {capability}")

    def normalize(self, graph: PlanGraph) -> PlanGraph:
        """Resolve editable profile IDs to trusted runtime/model/pricing metadata."""
        executable_kinds = {
            PlanNodeKind.IMPLEMENTATION,
            PlanNodeKind.INTEGRATION,
            PlanNodeKind.CUSTOM,
        }
        by_id = {node.node_id: node for node in graph.nodes}

        def executable_dependencies(node: PlanNode) -> list[str]:
            resolved: list[str] = []

            def add(node_id: str, visiting: set[str]) -> None:
                if node_id in visiting:
                    return
                dependency = by_id[node_id]
                if dependency.kind in executable_kinds:
                    resolved.append(node_id)
                    return
                for parent in dependency.depends_on:
                    add(parent, visiting | {node_id})

            for dependency_id in node.depends_on:
                add(dependency_id, set())
            return list(dict.fromkeys(resolved))

        nodes: list[PlanNode] = []
        for node in graph.nodes:
            if node.kind not in executable_kinds:
                continue
            normalized_node = replace(
                node,
                depends_on=executable_dependencies(node),
            )
            if node.model.profile_id:
                option = self.catalog.get(node.model.profile_id)
            elif node.enabled:
                access = (
                    AgentAccess.WORKSPACE_WRITE
                    if node.access is PlanNodeAccess.WORKSPACE_WRITE
                    else AgentAccess.READ_ONLY
                )
                option = self._select(
                    node.capability,
                    access,
                    node.instructions or node.title,
                )
            else:
                nodes.append(normalized_node)
                continue
            capability = node.capability
            binding = self._binding(option, capability, node.instructions or node.title)
            if node.model.selection_reason.startswith("用户"):
                binding = replace(binding, selection_reason=node.model.selection_reason)
            nodes.append(replace(normalized_node, model=binding))
        planning_option = (
            self.catalog.get(graph.planning_model.profile_id)
            if graph.planning_model.profile_id
            else self._select("planning", AgentAccess.READ_ONLY, graph.requirement_summary)
        )
        review_option = (
            self.catalog.get(graph.review_model.profile_id)
            if graph.review_model.profile_id
            else self._select("review", AgentAccess.READ_ONLY, graph.requirement_summary)
        )
        normalized = replace(
            graph,
            nodes=nodes,
            planning_model=self._binding(
                planning_option, "planning", graph.requirement_summary
            ),
            review_model=self._binding(review_option, "review", graph.requirement_summary),
        )
        self.validate(normalized)
        return normalized

    @staticmethod
    def _step_capability(step: str) -> str:
        lowered = step.lower()
        groups = [
            ("security", ("security", "auth", "token", "credential", "permission", "安全", "认证", "权限")),
            ("frontend", ("frontend", "react", "vue", "css", "html", "interface", " ui ", "页面", "前端", "界面")),
            ("testing", ("test", "spec", "coverage", "测试", "验证")),
            ("migration", ("migration", "migrate", "schema", "迁移")),
            ("backend", ("backend", "api", "database", "service", "server", "后端", "数据库", "接口")),
            ("documentation", ("documentation", "docs", "readme", "文档")),
        ]
        padded = f" {lowered} "
        return next(
            (capability for capability, terms in groups if any(term in padded for term in terms)),
            "implementation",
        )

    def _select(self, capability: str, access: AgentAccess, text: str) -> ModelOption:
        candidates = [model for model in self.catalog.list_all() if model.access is access]
        role_capability = "implementation" if access is AgentAccess.WORKSPACE_WRITE else capability
        eligible = [
            model
            for model in candidates
            if role_capability in model.capabilities or "general" in model.capabilities
        ]
        specialized = [
            model
            for model in eligible
            if capability == role_capability
            or capability in model.capabilities
            or "general" in model.capabilities
        ]
        if specialized:
            eligible = specialized
        if not eligible:
            raise ValueError(
                f"no {access.value} model can satisfy capability {capability}"
            )
        minimum_quality = 4 if capability in {"security", "migration", "architecture"} else 1
        qualified = [model for model in eligible if model.quality >= minimum_quality]
        if not qualified:
            raise ValueError(
                f"no {access.value} model meets quality {minimum_quality} for {capability}"
            )
        eligible = qualified

        def key(model: ModelOption) -> tuple:
            cost = self._estimated_cost(model, text)
            exact = 1.0 if capability in model.capabilities else 0.0
            if self.optimization_goal == "cost":
                return (-exact, cost, -float(model.quality), model.profile_id)
            if self.optimization_goal == "quality":
                return (-exact, -float(model.quality), cost, model.profile_id)
            value = (model.quality + exact * 2.0) / max(cost, 0.000001)
            return (-exact, -value, cost, model.profile_id)

        return min(eligible, key=key)

    def _binding(self, model: ModelOption, capability: str, text: str) -> ModelBinding:
        return ModelBinding(
            profile_id=model.profile_id,
            runtime=model.runtime,
            provider=model.provider,
            model=model.model,
            thinking=model.thinking,
            estimated_cost_usd=self._estimated_cost(model, text),
            selection_reason=(
                f"selected for {capability}; quality={model.quality}; "
                f"estimated_cost_usd={self._estimated_cost(model, text):.6f}; "
                f"optimization={self.optimization_goal}"
            ),
        )

    @staticmethod
    def _estimated_cost(model: ModelOption, text: str) -> float:
        estimated_input = max(256, len(text) // 4 + 512)
        estimated_output = max(128, len(text) // 8 + 256)
        return (
            estimated_input * model.input_cost_per_million
            + estimated_output * model.output_cost_per_million
        ) / 1_000_000
