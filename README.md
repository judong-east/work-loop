# Workloop

Workloop is a local coding-agent orchestrator. It gives Claude Code the planner
and reviewer roles, gives Codex CLI the executor role, and keeps Git isolation,
validation evidence, recovery, review loops, and delivery gates under host
control.

Workloop executes controlled workflow definitions. The default built-in `quick`
workflow keeps a single human gate:

```text
request -> Claude plan -> Codex execute
        -> deterministic validation -> independent Claude review
        -> one-click human-confirmed Git delivery
```

A plan without open questions proceeds directly to the executor; confirming
delivery generates the delivery report and merges in the same step. The built-in
`guarded` workflow adds a plan-approval gate after the planner. The legacy id
`autopilot` still resolves to `quick`. Custom workflows may add role-specific
instructions, may include or omit the plan approval node, and may reorder or
repeat executor, validation, and reviewer nodes. The host keeps role access,
validation commands, review outcomes, and delivery authority fixed.

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

Plans may select only named validation commands from this file. `network`
defaults to `"deny"`; setting it to `"allow"` in the versioned policy is the
explicit per-project authorization for executor and validation commands to
reach the network (dependency installs, online test fixtures). Sandbox health
canaries run once per validation window instead of once per command.

## Run

```powershell
python -m app.cli serve --root . --port 8765
```

Open `http://127.0.0.1:8765/` for the multi-agent workbench. This is the only
browser UI; `/workbench` redirects to the same entry point. Register a Git
repository or any ordinary local
directory, and create a task. Ordinary directories do not need `.git` or a
Workloop policy file: Workloop keeps a managed Git snapshot under its own data
root and leaves the source directory untouched during registration.
The task console supports:

- an in-app project-directory browser for projects on any local drive;
- ordinary-directory projects backed by an isolated managed snapshot, with
  confirmed delivery writing only task changes back to the source directory;
- structured plan review with batch clarification (all open questions in one
  submission);
- per-task workflow selection with `quick` as the default preset;
- controlled custom workflows with planner, executor, and reviewer instructions;
- a visual task-graph canvas for dragging nodes, editing dependencies, and
  persisting operator-defined layouts before plan approval;
- persistent FIFO scheduling with `WORKLOOP_SLOTS` parallel execution slots
  (default 1);
- an append-only per-task event log (`logs/events.jsonl`) streamed live to the
  console over SSE (`/api/agent/tasks/{id}/events`), so progress shows within a
  second instead of the 4-second poll;
- normalized Claude/Codex events, sessions, budgets, and runtime health;
- worktree diffs, policy evidence, deterministic validation, and review issues;
- review pass verdicts whose acceptance drifts from the approved plan degrade
  to a revision round instead of failing the task;
- interrupted-stage recovery, rerun, cancellation, and budget adjustment;
- one-click delivery: the report is generated inside the confirmed call;
- auditable task commits, target-branch reintegration, and confirmed delivery;
- read-only display of `legacy-v1` tasks and their surviving artifacts.

## Workbench V2

The refactored workbench follows three boundaries described in the architecture
report:

- **Resource layer**: `ResourceCenter` groups multiple model aliases under each
  provider. Models select either OpenAI Chat Completions or Claude Messages;
  providers select Bearer, `x-api-key`, custom-header, or no authentication.
  Credential values remain in separate local secret files and never enter
  task/session JSON. The HTTP surface is `/api/v2/resources`.
- **Orchestration layer**: `NodeRegistry`, `NodeCatalog`, `WorkflowCatalog`, and
  `DagOrchestrator` validate a DAG, merge structured context between nodes,
  persist node runs, and emit resumable events. Custom node contracts are
  managed in the UI and persisted in `workbench/nodes.json` without loading
  executable code from user configuration.
- **Interaction layer**: `WorkbenchService` exposes projects and sessions with
  explicit `chat` and `task` modes. The UI at `/` includes provider and model
  CRUD, custom-node CRUD, workflow editing, explicit node-to-model bindings,
  node progress, shared context, and task governance (strategy, complexity,
  risk, next action, and quality Gates). Strategy presets are available at
  `GET /api/v2/strategies`; policy approval/replanning uses the session policy
  endpoints under `/api/v2/sessions/{id}/policy`. Its API is under `/api/v2/*`.

The existing `/api/agent/*` API remains an internal compatibility surface;
new clients should use `/api/v2/*`. A model gateway is injected into
  `WorkbenchService`. The bundled OpenAI-compatible gateway resolves stable model
  aliases through the resource center and requires structured JSON from every
  node; other vendor adapters can be injected without changing DAG or UI contracts.

## Workflows

Use the **工作流** control in the console to create a personal workflow. Every
workflow starts with one planner, ends with one delivery node, and may place or
repeat executor, validation, and reviewer nodes in between. An optional plan
approval node follows the planner. To ensure delivery evidence describes the
final writable state, at least one validation must follow the last executor and
at least one reviewer must follow that validation. These are partial-order
constraints, not a fixed middle pipeline.

Agent nodes can add instructions, but cannot change their access: planner and
reviewer remain read-only, the executor remains restricted to its task
worktree, validation remains limited to project-policy commands, and delivery
remains human-confirmed. Workflow progress is persisted by node position, so
repeated nodes resume at the interrupted occurrence instead of the first node
of the same kind.

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

New tasks execute their composed `PlanGraph` by default (the former
`WORKLOOP_EXECUTION` opt-in no longer exists). Plan approval drives the graph
instead of a single executor call, and each `IMPLEMENTATION` and
`INTEGRATION` node runs in topological order on the shared task worktree:

- a node carries a `ModelBinding` (`provider` / `model` / `thinking`) and an
  `on_failure` policy (`retry` / `human` / `skip` / `replan`);
- a node's upstream context is merged through the context ledger as structured
  facts, decisions, and artifacts — never raw chat history — so fan-in stays
  compact and resumable;
- per-node status persists to `AgentTask.node_runs`, so an interrupted task
  resumes by skipping completed nodes and re-running failed or pending ones;
- canvas coordinates persist in `PlanGraph.layout`; moving a node changes only
  its presentation, while dependency edges remain the source of execution order;
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

**Pi runtime** — model-catalog entries with `"runtime": "pi_rpc"` run through
`PiRpcRuntime` (`@earendil-works/pi-coding-agent`, JSONL RPC over stdio), which
honors a per-request `--model` / `--provider` / `--thinking`. Install and
authenticate the `pi` binary first; `WORKLOOP_PI_COMMAND`,
`WORKLOOP_PI_PROVIDER`, and `WORKLOOP_PI_CONFIG_DIR` adjust the launch and
config location.

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

**Native harness runtime** — model entries with `"runtime": "native"` run
without any CLI subprocess: Workloop itself is the harness (the
"Model + Harness = Agent" split behind Pi and DeepSeek Harness). It calls an
OpenAI-compatible chat-completions endpoint directly and runs the tool-calling
loop in-process, so the model is not constrained by an external harness's tool
set — it autonomously uses the `read_file` / `list_files` / `search_content` /
`write_file` / `edit_file` (and, when offered, `run_command`) tools that
Workloop implements:

```json
{
  "profile_id": "deepseek-writer",
  "label": "DeepSeek writer",
  "runtime": "native",
  "provider": "deepseek",
  "model": "DeepSeek-V4-Flash",
  "access": "workspace_write",
  "capabilities": ["implementation", "frontend", "backend", "testing", "general"],
  "quality": 4,
  "input_cost_per_million": 0.28,
  "output_cost_per_million": 0.42,
  "base_url": "https://api.deepseek.com/v1",
  "api_key_env": "DEEPSEEK_API_KEY"
}
```

- The API key never enters the catalog: `api_key_env` names an environment
  variable, or `WORKLOOP_NATIVE_KEY_FILE` points at a key file (bare key,
  `KEY=value`, or JSON).
- File tools are confined to the task worktree in-process and honor protected
  paths — stronger isolation than PiRpcRuntime's working-directory-only model.
- `run_command` is the only tool that can leave the worktree, so it alone
  carries the `WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR` gate when the project
  policy denies network; the file tools keep working regardless.
- Read-only roles get read/list/search only. Sessions persist as JSON message
  logs under `.workloop-native-sessions/<task>/` and resume on retry or
  revision; budgets, cancellation, and the event stream behave like the other
  runtimes.
- A fully CLI-free default stack needs two environment variables (plus a key):

```powershell
$env:WORKLOOP_NATIVE_BASE_URL="https://api.deepseek.com/v1"
$env:WORKLOOP_NATIVE_MODEL="DeepSeek-V4-Flash"
$env:DEEPSEEK_API_KEY="..."
python -m app.cli serve --root . --port 8765
```

Per-role overrides: `WORKLOOP_NATIVE_PLANNER_MODEL`, `_EXECUTOR_MODEL`,
`_REVIEWER_MODEL`; global tuning: `WORKLOOP_NATIVE_PROVIDER`,
`WORKLOOP_NATIVE_THINKING`, `WORKLOOP_NATIVE_MAX_TOKENS`. The CLI runtimes
remain available and can be mixed with native entries per node.

**Per-node model routing** — each plan node's `ModelBinding` flows onto its
`AgentRequest`, so one runtime family can route per node — for example Opus
for planning, GPT for the backend, Kimi for the UI. Routing is by model, not
by harness: every node still runs through the runtime its model profile
selects. The Pi and native runtimes honor per-node model fields; the Claude
and Codex runtimes ignore them, so the feature is a no-op there.

Per-node worktrees stay opt-in:

```powershell
$env:WORKLOOP_NODE_WORKTREE="1"
python -m app.cli serve --root . --port 8765
```

## Parallel Slots

`WORKLOOP_SLOTS`（默认 1，上限 8）controls how many tasks execute at the same
time. Every task owns its isolated Git worktree, so distinct tasks are safe to
run in parallel; one task still occupies exactly one slot for the whole stage,
and deliveries into the same target repository are serialized by the host.
Set it before starting the server:

```powershell
$env:WORKLOOP_SLOTS="3"
python -m app.cli serve --root . --port 8765
```

## Legacy Workflow

The legacy-v1 kernel (old `WorkloopKernel`, its model backends, decision and
evaluation modules) has been removed from the codebase. The former
`create-task`, `run-loop`, `resume`, `deliver`, and `memory` CLI commands exit
with an explanation, legacy Web write endpoints return `410 Gone`, and the old
`/api/tasks`/`/api/models`/`/api/workflow`/`/api/memory` endpoints are gone.
Existing `tasks/<id>/state.json` records remain readable through the read-only
history view (`/api/agent/history`), including local-unavailable markers for
missing or escaping artifact references.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Tests use scripted runtimes and temporary Git repositories. No Claude or Codex
login is required for the automated suite.
