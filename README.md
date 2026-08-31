# Workloop V2

Workloop is a local multi-model collaborative development workbench. It has one
V2 runtime, one resource center, and one HTTP API (`/api/v2`).

## What it does

- groups multiple model aliases under each provider;
- supports OpenAI Chat Completions and Claude Messages;
- supports Bearer, `x-api-key`, custom-header, and no-auth providers;
- stores credentials outside provider/model catalog JSON;
- defines reusable node contracts and editable DAG workflows;
- binds every workflow node to an explicit model alias or capability default;
- defines reusable roles with their own model, instructions, capabilities, and
  workspace permission;
- coordinates multiple role-owned tasks through a dependency DAG;
- runs read-only tasks concurrently and serializes workspace writers against
  each other, without making writers wait for unrelated readers;
- automatically unblocks downstream tasks once a retried dependency completes;
- lets each task override the role's model with its own model alias;
- supports editing pending/blocked/failed tasks and re-validates the DAG;
- runs coordination asynchronously and exposes the orchestration event log;
- persists task-to-task handoffs for downstream roles;
- persists project, session, context, node run, strategy, and Gate state;
- reads a configured local workspace into a bounded context snapshot;
- applies model-proposed file writes atomically inside that workspace;
- rejects absolute paths, traversal, symlink escape, protected paths, deletion,
  duplicate writes, and oversized change sets;
- runs explicit project validation commands without a command shell;
- blocks failed validation or review through a quality Gate;
- calls the same V2 model gateway for ordinary chat and task nodes.

## Start

```powershell
py -3.10 -m app.cli serve --root . --port 8765
```

Open `http://127.0.0.1:8765/`.

## First setup

1. Open **管理中心 → 模型**.
2. Add a provider, choose its protocol and authentication mode, then add one or
   more model aliases.
3. Create a project with an absolute workspace path, a default model, and
   optional validation commands. Each validation command is one JSON argv array:

```json
["py", "-3.10", "-m", "unittest", "discover", "-s", "tests", "-q"]
```

4. Configure **管理中心 → 角色**. A role binds one responsibility node to one
   model and an explicit workspace permission.
5. Create dependency-aware work under **管理中心 → 协同任务**, then run the
   coordinator. Use normal chat or task mode for one-off work.

## Default workflow

```text
requirement -> planning -> implementation -> testing -> review
```

The implementation model returns complete, structured file writes:

```json
{
  "changes": "Implement the requested behavior",
  "file_changes": [
    {
      "operation": "write",
      "path": "src/example.py",
      "content": "complete UTF-8 file content"
    }
  ],
  "artifacts": {},
  "decisions": []
}
```

Workloop validates and publishes these writes atomically. The testing node then
runs only the commands configured on the project. A failed command or non-pass
review blocks the session with a `quality_review` Gate.

## Long-horizon execution

Large tasks can run through a multi-round Manager → Executor → Auditor loop
(the `long_horizon` node type), adapted from
[AMAP-ML/LongHorizon-Harness](https://github.com/AMAP-ML/LongHorizon-Harness)
(MIT). The built-in workflow **长时程任务流** (`long-horizon-task`) uses it:

```text
planning -> long_horizon -> (testing, review)
```

Each round:

1. the **Manager** plans one subtask and rewrites the durable task state; every
   completed fact must cite the audit round that verified it;
2. the **Executor** runs that subtask with a fresh context and proposes
   `file_changes`, which are published through the same atomic workspace
   validation as the implementation node;
3. the **Auditor** independently verifies the round and returns
   `status` / `integrity` / `contract_audit`. Unresolved blocking constraints,
   violations, or an unaligned contract audit downgrade a `complete` verdict.

A `done` route is accepted only when the latest audit is clean. Protocol
violations (an invalid route, a premature `done`) are fed back to the Manager
as harness feedback instead of failing the node. Node config:

- `max_rounds` — rounds per invocation, default `8`, clamped to `1..25`;
- `manager_model_alias` / `executor_model_alias` / `auditor_model_alias`
  — optional per-role model overrides, defaulting to the node's model alias.

When the budget is exhausted without a clean audit, or the Manager routes
`blocked`/`ask`, the session is blocked behind a `longhorizon_rounds`,
`longhorizon_blocked`, or `longhorizon_input_needed` Gate. Approving the policy
and re-running resumes from the persisted round ledger (session event messages)
instead of starting over.

## Collaborative development

Collaboration adds a task graph above individual model workflows:

```text
需求分析师 -> 架构师 -> 开发者 -> 测试工程师 -> 审核员
                  `-> independent read tasks may run in parallel
```

Each task owns a role, priority, dependencies, execution session, status, and
compact result. Completing a task creates durable handoffs for its dependents.
A failed task blocks downstream work instead of being silently skipped. When a
blocked dependency is retried and completes, its blocked dependents are
unblocked automatically on the next coordination round — no manual requeueing.

Each task can pin its own `model_alias`; it overrides the role's binding for
that single task, so one developer role can fan work out across several models.

### Goal decomposition

A large goal can be split into role-owned subtasks automatically. Submit a goal
through **管理中心 → 协同任务 → 大目标拆分**, or `POST /api/v2/projects/{id}/goals`:

1. the planning model (the `planning`-node role's model, else the project
   default) proposes a ref-based draft plan;
2. the plan is validated against the configured roles and the DAG rules —
   unknown roles, unknown refs, self references, and cycles are all rejected
   before anything is persisted;
3. valid items become durable tasks in topological order, grouped under a
   Goal record so the whole decomposition stays traceable.

Pass `subtasks` in the request body to split manually without calling a model,
and `auto_coordinate: true` to run the coordinator immediately after splitting.

## Architecture

```text
Browser
  -> /api/v2
     -> WorkbenchService
        -> ResourceCenter          provider / model / credential metadata
        -> JsonCollection          projects / sessions
        -> DagOrchestrator         order / retry / resume / Gate / context
           -> ModelGateway         OpenAI or Claude protocol
           -> WorkspaceRuntime     snapshot / atomic write / validation
     -> CollaborationService
        -> RoleProfile             responsibility / model / permission
        -> CollaborationTask       owner / dependency / result
        -> Handoff + TaskGraph     context transfer / coordination
```

The implementation lives in:

- `app/domain`: durable contracts, node catalog, workflow catalog, orchestration;
- `app/application`: use-case facade;
- `app/infrastructure`: JSON persistence, provider gateway, workspace runtime;
- `app/web`: the V2-only local API and workbench UI.

There is no compatibility `/api/agent` surface or second task/runtime model.

## API

Core read endpoints:

- `GET /api/v2/catalog`
- `GET /api/v2/strategies`
- `GET /api/v2/resources`
- `GET /api/v2/projects`
- `GET /api/v2/projects/{project_id}/workspace`
- `GET /api/v2/projects/{project_id}/sessions`
- `GET /api/v2/projects/{project_id}/collaboration`
- `GET /api/v2/roles`
- `GET /api/v2/sessions/{session_id}`
- `GET /api/v2/events?after=&limit=`

Core write endpoints:

- `POST /api/v2/projects`
- `POST /api/v2/projects/{project_id}`
- `POST /api/v2/projects/{project_id}/sessions`
- `POST /api/v2/projects/{project_id}/tasks`
- `POST /api/v2/projects/{project_id}/coordinate` — synchronous by default;
  pass `{"async": true}` to run in the background (returns `202`; poll
  `GET /api/v2/projects/{project_id}/collaboration`, which reports `coordinating`)
- `POST /api/v2/projects/{project_id}/goals`
- `POST /api/v2/tasks/{task_id}` — edit a non-running task
- `POST /api/v2/tasks/{task_id}/retry`
- `POST /api/v2/roles`
- `POST /api/v2/sessions/{session_id}/messages`
- `POST /api/v2/sessions/{session_id}/run`
- `POST /api/v2/sessions/{session_id}/policy`
- `POST /api/v2/sessions/{session_id}/policy/approve`
- `POST /api/v2/sessions/{session_id}/policy/replan`
- `POST /api/v2/resources/providers`
- `POST /api/v2/resources/models`
- `POST /api/v2/nodes`
- `POST /api/v2/workflows`

The server binds to `127.0.0.1` and rejects cross-origin writes. Provider keys
remain in local secret files and are never returned through the API.

## Tests

```powershell
py -3.10 -m compileall -q app tests
node --check app/web/static/workbench.js
node --check app/web/static/collaboration.js
py -3.10 -m unittest discover -s tests -v
git diff --check
```
