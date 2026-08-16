"""Build the model-catalog-driven runtime stack used by the web server.

The catalog (agent-profiles.json, or the environment-derived default) selects
a runtime per model profile; this module turns catalog entries into
AgentRuntime instances and routes them. The web layer stays HTTP-only.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.agents.claude_code import ClaudeCodeProfile, ClaudeCodeRuntime
from app.agents.codex_cli import (
    CodexCliProfile,
    CodexCliRuntime,
    load_codex_cli_profile,
)
from app.agents.composition import ExecutionComposer, ModelCatalog, ModelOption
from app.agents.contracts import AgentAccess
from app.agents.native_harness import NativeHarnessProfile, NativeHarnessRuntime
from app.agents.pi_rpc import PiRpcProfile, PiRpcRuntime
from app.agents.runtime import AgentRuntime, ProfileRoutedRuntime


def build_runtime_stack(
    catalog: ModelCatalog,
) -> tuple[ProfileRoutedRuntime, ExecutionComposer]:
    composer = ExecutionComposer(
        catalog,
        optimization_goal=(
            os.environ.get("WORKLOOP_OPTIMIZATION", "balanced").strip().lower()
            or "balanced"
        ),
    )
    profile_runtimes = {
        option.profile_id: build_runtime(option) for option in catalog.list_all()
    }
    role_bindings = {
        "planner": composer.select_binding("planning", AgentAccess.READ_ONLY, "planning"),
        "executor": composer.select_binding(
            "implementation", AgentAccess.WORKSPACE_WRITE, "implementation"
        ),
        "reviewer": composer.select_binding("review", AgentAccess.READ_ONLY, "review"),
    }
    runtime = ProfileRoutedRuntime(
        {role: profile_runtimes[binding.profile_id] for role, binding in role_bindings.items()},
        profile_runtimes,
    )
    return runtime, composer


def build_runtime(option: ModelOption) -> AgentRuntime:
    if option.runtime == "pi_rpc":
        return PiRpcRuntime(
            PiRpcProfile(
                command=[os.environ.get("WORKLOOP_PI_COMMAND", "pi")],
                model=option.model,
                provider=option.provider or os.environ.get("WORKLOOP_PI_PROVIDER", ""),
                thinking=option.thinking,
                config_dir=(
                    Path(os.environ["WORKLOOP_PI_CONFIG_DIR"])
                    if os.environ.get("WORKLOOP_PI_CONFIG_DIR")
                    else None
                ),
            )
        )
    if option.runtime == "native":
        return NativeHarnessRuntime(
            NativeHarnessProfile(
                model=option.model,
                base_url=option.base_url
                or os.environ.get("WORKLOOP_NATIVE_BASE_URL", "").strip(),
                api_key_env=option.api_key_env
                or os.environ.get("WORKLOOP_NATIVE_API_KEY_ENV", "").strip()
                or "WORKLOOP_NATIVE_API_KEY",
                provider=option.provider,
                thinking=option.thinking,
                max_tokens=option.max_tokens,
            )
        )
    if option.runtime == "claude_code":
        return ClaudeCodeRuntime(ClaudeCodeProfile(model=option.model or "sonnet"))
    try:
        profile = load_codex_cli_profile(option.model)
    except ValueError:
        # A malformed or unsafe user provider must not prevent the local
        # service from starting. The explicit catalog model remains usable
        # with Codex-managed defaults.
        profile = CodexCliProfile(model=option.model or "gpt-5.2-codex")
    return CodexCliRuntime(profile)


def default_model_catalog() -> ModelCatalog:
    planner_model = os.environ.get("WORKLOOP_CLAUDE_MODEL", "sonnet")
    executor_model = os.environ.get("WORKLOOP_CODEX_MODEL", "") or "gpt-5.2-codex"
    native_base_url = os.environ.get("WORKLOOP_NATIVE_BASE_URL", "").strip()
    native_model = os.environ.get("WORKLOOP_NATIVE_MODEL", "").strip()
    if native_base_url and native_model:
        # A fully CLI-free default stack: with one base URL and one model
        # (plus a key) every role runs through the in-process harness.
        api_key_env = (
            os.environ.get("WORKLOOP_NATIVE_API_KEY_ENV", "").strip()
            or "WORKLOOP_NATIVE_API_KEY"
        )
        native_provider = os.environ.get("WORKLOOP_NATIVE_PROVIDER", "").strip()
        native_thinking = os.environ.get("WORKLOOP_NATIVE_THINKING", "medium").strip() or "medium"
        native_max_tokens = int(os.environ.get("WORKLOOP_NATIVE_MAX_TOKENS", "0") or 0)
        role_specs = [
            ("planner", AgentAccess.READ_ONLY, "WORKLOOP_NATIVE_PLANNER_MODEL"),
            ("executor", AgentAccess.WORKSPACE_WRITE, "WORKLOOP_NATIVE_EXECUTOR_MODEL"),
            ("reviewer", AgentAccess.READ_ONLY, "WORKLOOP_NATIVE_REVIEWER_MODEL"),
        ]
        role_capabilities = {
            "planner": ["planning", "architecture", "general"],
            "executor": [
                "implementation", "frontend", "backend", "security",
                "testing", "migration", "documentation",
            ],
            "reviewer": ["review", "security", "general"],
        }
        return ModelCatalog(
            [
                ModelOption(
                    profile_id=role,
                    label=role.title(),
                    runtime="native",
                    model=os.environ.get(model_env, "").strip() or native_model,
                    access=access,
                    capabilities=role_capabilities[role],
                    quality=4,
                    input_cost_per_million=0.0,
                    output_cost_per_million=0.0,
                    provider=native_provider,
                    thinking=native_thinking,
                    base_url=native_base_url,
                    api_key_env=api_key_env,
                    max_tokens=native_max_tokens,
                )
                for role, access, model_env in role_specs
            ]
        )
    return ModelCatalog(
        [
            ModelOption(
                profile_id="planner",
                label="Planner",
                runtime="claude_code",
                model=planner_model,
                access=AgentAccess.READ_ONLY,
                capabilities=["planning", "architecture", "general"],
                quality=4,
                input_cost_per_million=0.0,
                output_cost_per_million=0.0,
            ),
            ModelOption(
                profile_id="executor",
                label="Executor",
                runtime="codex_cli",
                model=executor_model,
                access=AgentAccess.WORKSPACE_WRITE,
                capabilities=[
                    "implementation", "frontend", "backend", "security",
                    "testing", "migration", "documentation",
                ],
                quality=4,
                input_cost_per_million=0.0,
                output_cost_per_million=0.0,
            ),
            ModelOption(
                profile_id="reviewer",
                label="Reviewer",
                runtime="claude_code",
                model=planner_model,
                access=AgentAccess.READ_ONLY,
                capabilities=["review", "security", "general"],
                quality=4,
                input_cost_per_million=0.0,
                output_cost_per_million=0.0,
            ),
        ]
    )
