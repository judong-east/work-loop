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
    role_bindings: dict[str, str] | None = None,
    key_root: Path | None = None,
) -> tuple[ProfileRoutedRuntime, ExecutionComposer]:
    composer = ExecutionComposer(
        catalog,
        optimization_goal=(
            os.environ.get("WORKLOOP_OPTIMIZATION", "balanced").strip().lower()
            or "balanced"
        ),
    )
    profile_runtimes = {
        option.profile_id: build_runtime(option, key_root)
        for option in catalog.list_all()
    }
    explicit = {
        role: profile_id
        for role, profile_id in (role_bindings or {}).items()
        if profile_id and profile_id in profile_runtimes
    }
    auto = {
        "planner": composer.select_binding("planning", AgentAccess.READ_ONLY, "planning"),
        "executor": composer.select_binding(
            "implementation", AgentAccess.WORKSPACE_WRITE, "implementation"
        ),
        "reviewer": composer.select_binding("review", AgentAccess.READ_ONLY, "review"),
    }
    bindings = {role: explicit.get(role) or auto[role].profile_id for role in auto}
    runtime = ProfileRoutedRuntime(
        {role: profile_runtimes[profile_id] for role, profile_id in bindings.items()},
        profile_runtimes,
    )
    return runtime, composer


def _resolve_key_file(api_key_file: str, key_root: Path | None) -> str:
    if not api_key_file:
        return ""
    path = Path(api_key_file)
    if path.is_absolute() or key_root is None:
        return str(path)
    return str((Path(key_root) / path).resolve())


def build_runtime(option: ModelOption, key_root: Path | None = None) -> AgentRuntime:
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
                api_key_file=_resolve_key_file(option.api_key_file, key_root),
                proxy=option.proxy,
                protocol=option.protocol,
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


_NATIVE_ROLE_SPECS = [
    ("planner", AgentAccess.READ_ONLY, "WORKLOOP_NATIVE_PLANNER_MODEL"),
    ("executor", AgentAccess.WORKSPACE_WRITE, "WORKLOOP_NATIVE_EXECUTOR_MODEL"),
    ("reviewer", AgentAccess.READ_ONLY, "WORKLOOP_NATIVE_REVIEWER_MODEL"),
]
_NATIVE_ROLE_CAPABILITIES = {
    "planner": ["planning", "architecture", "general"],
    "executor": [
        "implementation", "frontend", "backend", "security",
        "testing", "migration", "documentation",
    ],
    "reviewer": ["review", "security", "general"],
}


def native_catalog(
    base_url: str,
    model: str,
    provider: str = "",
    thinking: str = "medium",
    max_tokens: int = 0,
    api_key_env: str = "WORKLOOP_NATIVE_API_KEY",
    role_models: dict[str, str] | None = None,
    protocol: str = "openai_chat",
) -> ModelCatalog:
    """One endpoint, one model: every role runs through the native harness."""

    def role_model(role: str) -> str:
        override = (role_models or {}).get(role, "").strip()
        return override or model

    return ModelCatalog(
        [
            ModelOption(
                profile_id=role,
                label=role.title(),
                runtime="native",
                model=role_model(role),
                access=access,
                capabilities=_NATIVE_ROLE_CAPABILITIES[role],
                quality=4,
                input_cost_per_million=0.0,
                output_cost_per_million=0.0,
                provider=provider,
                thinking=thinking,
                base_url=base_url,
                api_key_env=api_key_env,
                protocol=protocol,
                max_tokens=max_tokens,
            )
            for role, access, _model_env in _NATIVE_ROLE_SPECS
        ]
    )


def default_model_catalog() -> ModelCatalog:
    native_base_url = os.environ.get("WORKLOOP_NATIVE_BASE_URL", "").strip()
    native_model = os.environ.get("WORKLOOP_NATIVE_MODEL", "").strip()
    if native_base_url and native_model:
        # A fully CLI-free default stack: with one base URL and one model
        # (plus a key) every role runs through the in-process harness.
        api_key_env = (
            os.environ.get("WORKLOOP_NATIVE_API_KEY_ENV", "").strip()
            or "WORKLOOP_NATIVE_API_KEY"
        )
        return native_catalog(
            native_base_url,
            native_model,
            provider=os.environ.get("WORKLOOP_NATIVE_PROVIDER", "").strip(),
            thinking=os.environ.get("WORKLOOP_NATIVE_THINKING", "medium").strip() or "medium",
            max_tokens=int(os.environ.get("WORKLOOP_NATIVE_MAX_TOKENS", "0") or 0),
            api_key_env=api_key_env,
            protocol=os.environ.get("WORKLOOP_NATIVE_PROTOCOL", "openai_chat").strip()
            or "openai_chat",
            role_models={
                role: os.environ.get(model_env, "").strip()
                for role, _access, model_env in _NATIVE_ROLE_SPECS
            },
        )
    # The console must be able to start before credentials are configured.
    # Keep a native-only placeholder stack whose health check clearly reports
    # the missing endpoint/key; never silently spend through installed CLIs.
    return native_catalog(
        "",
        "unconfigured",
        provider="unconfigured",
        protocol="codex",
    )
