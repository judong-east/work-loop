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

## Plan Graph Execution & Per-Node Models

Two opt-in flags extend the execution model. Both default off, so the proven
single-executor loop runs unchanged when neither is set.

**Graph execution** — `WORKLOOP_EXECUTION=graph` makes plan approval drive the
task's `PlanGraph` instead of a single executor call. Each `IMPLEMENTATION` and
`INTEGRATION` node runs in topological order on the shared task worktree:

- a node carries a `ModelBinding` (`provider` / `model` / `thinking`) and an
  `on_failure` policy (`retry` / `human` / `skip` / `replan`);
- a node's upstream context is merged through the context ledger as structured
  facts, decisions, and artifacts — never raw chat history — so fan-in stays
  compact and resumable;
- per-node status persists to `AgentTask.node_runs`, so an interrupted task
  resumes by skipping completed nodes and re-running failed or pending ones;
- the merged node output feeds the existing validation → review → delivery
  path unchanged.

**Per-node worktrees** — `WORKLOOP_NODE_WORKTREE=1` (requires graph execution)
gives each implementation node its own detached git worktree for write
isolation. Before each attempt the shared task worktree's accumulated writes
are replicated into the node worktree (including upstream deletions), and on
success the node's own delta is merged back into the shared task worktree
uncommitted — so validation, review, and delivery are unchanged. Node worktrees
are deterministic per `(task_id, node_id)` and resume-safe; a crashed run's
stale worktree is pruned on the next attempt. Default off keeps the shared
single-worktree behavior.

**Pi runtime** — `WORKLOOP_RUNTIME=pi_rpc` swaps every role to `PiRpcRuntime`
(`@earendil-works/pi-coding-agent`, JSONL RPC over stdio), which honors a
per-request `--model` / `--provider` / `--thinking`. Install and authenticate
the `pi` binary first. Per-role model overrides come from
`WORKLOOP_PI_PLANNER_MODEL`, `WORKLOOP_PI_EXECUTOR_MODEL`, and
`WORKLOOP_PI_REVIEWER_MODEL`.

Pi is launched with a working directory and a tool allow-list only — unlike
Codex it gets no OS sandbox, so its `bash`/`write`/`edit` tools can reach any
path and any host, and a project policy's `network = "deny"` cannot be
enforced. Workloop cannot detect a write outside the task worktree either:
policy checks and `changes.diff` come from a snapshot of that worktree. A
write-access request under a network-denying policy is therefore refused with
`sandbox_unavailable` unless you opt in explicitly:

```powershell
$env:WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR="1"
```

Read-only planner and reviewer requests are unaffected. Use `CodexCliRuntime`
for write nodes when the isolation guarantee matters.

**Per-node model routing** — with both flags set, each plan node's
`ModelBinding` flows onto its `AgentRequest`, so one Pi runtime can route per
node — for example Opus for planning, GPT for the backend, Kimi for the UI.
Routing is by model, not by harness: every node still runs through the same
role runtime. Only the Pi runtime honors per-node model fields; the default
Claude and Codex runtimes ignore them, so the feature is a no-op there.

```powershell
$env:WORKLOOP_EXECUTION="graph"; $env:WORKLOOP_RUNTIME="pi_rpc"
python -m app.cli serve --root . --port 8765
```

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
