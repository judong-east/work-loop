# Workloop

Workloop is a local coding-agent orchestrator. It gives Claude Code the planner
and reviewer roles, gives Codex CLI the executor role, and keeps Git isolation,
validation evidence, recovery, review loops, and delivery gates under host
control.

Workloop executes controlled workflow definitions. The built-in `guarded`
workflow is:

```text
request -> Claude plan -> human approval -> Codex execute
        -> deterministic validation -> independent Claude review
        -> delivery report -> human-confirmed Git delivery
```

The built-in `autopilot` workflow removes only the plan approval gate: a plan
without open questions proceeds directly to Codex. Git delivery always requires
explicit human confirmation. Custom workflows may add role-specific
instructions and may include or omit the plan approval node, while the host
keeps node order, access, validation, review outcomes, and delivery authority
fixed.

## Requirements

- Python 3.11 or newer
- Git
- Claude Code installed and authenticated
- Codex CLI installed and authenticated
- a clean local Git repository with a versioned Workloop project policy

Workloop binds its local server to `127.0.0.1` only. Agent tasks run in
dedicated Git worktrees outside the registered repository.

## Project Policy

Add `.workloop/project.toml` to each repository before registration:

```toml
schema_version = 1

[permissions]
protected_paths = [".git/**", ".env", "secrets/**"]
network = "deny"

[validation]
timeout_seconds = 300
commands = [
  { name = "tests", argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-q"] }
]

[evidence]
redact_patterns = ["API_KEY=*"]
```

Plans may select only named validation commands from this file. The first
version always denies agent network access and pauses when broader authority is
required.

## Run

```powershell
python -m app.cli serve --root . --port 8765
```

Open `http://127.0.0.1:8765`, register a clean Git project, and create a task.
The task console supports:

- structured plan review and clarification;
- per-task workflow selection and immutable workflow snapshots;
- controlled custom workflows with planner, executor, and reviewer instructions;
- persistent FIFO scheduling with one local agent slot;
- normalized Claude/Codex events, sessions, budgets, and runtime health;
- worktree diffs, policy evidence, deterministic validation, and review issues;
- interrupted-stage recovery, rerun, cancellation, and budget adjustment;
- auditable task commits, target-branch reintegration, and confirmed delivery;
- read-only display of `legacy-v1` tasks and their surviving artifacts.

## Workflows

Use the **工作流** control in the console to create a personal workflow. Every
workflow contains one planner, executor, validation, reviewer, and delivery
node, plus an optional plan approval node. Agent nodes can add instructions,
but cannot change their access: planner and reviewer remain read-only, the
executor remains restricted to its task worktree, validation remains limited to
project-policy commands, and delivery remains human-confirmed.

The selected definition is copied into each task state. Later edits to the
catalog therefore do not change an in-flight task or its recovery behavior.

## Agent Profiles

Defaults come from `WORKLOOP_CLAUDE_MODEL` and `WORKLOOP_CODEX_MODEL`. A migrated
`agent-profiles.json` can set role models without exposing launcher commands:

```json
{
  "schema_version": 1,
  "roles": {
    "planner": {"runtime": "claude_code", "model": "sonnet", "access": "read_only"},
    "executor": {"runtime": "codex_cli", "model": "gpt-5.2-codex", "access": "workspace_write"},
    "reviewer": {"runtime": "claude_code", "model": "sonnet", "access": "read_only"}
  }
}
```

The console migration endpoint converts a legacy `models.json`, discards every
command template, and writes this constrained format under the Workloop data
root. Restart the server after migration. Runtime type and access cannot be
changed by this file.

When Codex selects a custom provider in `~/.codex/config.toml`, Workloop copies
only that provider's name, base URL, Responses protocol, authentication flag,
and default model into explicit CLI overrides. Codex still runs with
`--ignore-user-config`, so user MCP servers, hooks, commands, and permission
settings are not loaded into executor tasks.

## Legacy Workflow

The former `create-task`, `run-loop`, `resume`, `deliver`, and `memory` CLI
commands are disabled. Legacy Web write endpoints return `410 Gone`; arbitrary
CLI command templates can no longer obtain executor access. Existing
`tasks/<id>/state.json` records remain available through the read-only history
view. Missing, malformed, absolute, or escaping artifact references are shown
as local unavailable items rather than failing the entire task detail.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Tests use scripted runtimes and temporary Git repositories. No Claude or Codex
login is required for the automated suite.
