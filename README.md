# Workloop V2

Workloop is a local multi-model collaborative development workbench. It has one
V2 runtime, one resource center, and one HTTP API (`/api/v2`).

## What it does

- groups multiple model aliases under each provider;
- supports OpenAI Chat Completions and Claude Messages;
- supports OpenAI/Claude SSE streaming for ordinary chat, with a compatible
  one-shot JSON fallback for non-streaming providers;
- supports Bearer Token, API Key (`x-api-key`), Token, Basic Auth, custom-header,
  query-parameter, and no-auth providers;
- stores credentials outside provider/model catalog JSON;
- defines reusable node contracts and editable DAG workflows;
- binds every workflow node to an explicit model alias or capability default;
- defines reusable roles with their own model, instructions, capabilities, and
  workspace permission;
- coordinates multiple role-owned tasks through a dependency DAG;
- runs read-only tasks concurrently and serializes workspace writers against
  each other, without making writers wait for unrelated readers;
- automatically unblocks downstream tasks once a retried dependency completes;
- fixes one model alias to each role; tasks inherit that binding and cannot override it;
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
python -m app.cli serve --root . --port 8765
```

Open `http://127.0.0.1:8765/`.

## Desktop packaging

The repository includes a PyInstaller spec for the native desktop entry point:

```powershell
python -m PyInstaller --clean --noconfirm Workloop.spec
```

The single-file executable is written to `dist/Workloop.exe`. It starts the
same local V2 server on an ephemeral loopback port and uses pywebview when
available; without pywebview, the desktop entry point falls back to a browser.
Packaged runtime data is stored under the current user's `.workloop` directory.

## First setup

1. Open **管理中心 → 模型**.
2. Add a provider, choose its protocol and authentication mode, then add one or
   more model aliases.
3. Create a project with an absolute workspace path, a default model, and
   optional validation commands. Each validation command is one JSON argv array:

```json
["py", "-3.10", "-m", "unittest", "discover", "-s", "tests", "-q"]
```

4. Configure **管理中心 → 角色**. Every role must bind exactly one model to its
   responsibility node and declare an explicit workspace permission. Tasks inherit
   the role model and cannot replace it.
5. Create dependency-aware work under **管理中心 → 协同任务**, then run the
   coordinator. Use normal chat or task mode for one-off work.

### 上下文压缩与本地搜索

模型调用会统一经过 `ModelInvocationService`。它在每次调用前按模型的
`context_window_tokens` 和项目 `runtime_policy.compaction` 生成一个有界的
`ContextView`；超出预算时保留权威结构化状态、最近事件和最近工具结果，
并可用同一资源中心里的模型生成结构化摘要。压缩事件写回会话，因此重启后
仍能看到摘要和压缩前后 token 估算；原始 `ContextState` 不会被压缩逻辑改写。
压缩的预算、保留最近上下文、摘要回填思路参考了
[Pi 的 compaction 设计](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md)，
但没有重新引入 Pi 的 RPC 运行时。

代码检索是独立维护的本地 zvec-grep CLI 适配器。Workloop 默认只执行项目
工作区内的 `zg query`，不会连接 zvec-grep 的远程 MCP，也不会隐式刷新索引：

```powershell
# zvec-grep 当前 CLI 要求 Node.js 22+；安装后，在目标工作区单独维护本地索引
npm install -g @zvec/zvec-grep
zg index --embedding local/potion-code-16m-v2
zg status --check-ready
```

模型可使用两个受限工具：`zvec_grep_search`（索引语义/词法检索）和
`zvec_grep_rg`（托管 ripgrep 精确检索）。工具参数由 Workloop 校验，搜索
根目录固定为项目的绝对 `workspace_path`，结果有超时和输出上限。若本机没有
`zg`，普通模型调用仍可运行；只有模型实际请求本地搜索时，该工具调用会返回
可恢复的本地工具错误。
路由和参数遵循
[zvec-grep 的 CLI 查询约定](https://github.com/zvec-ai/zvec-grep/blob/main/docs/02-cli.md)
以及其[检索流水线说明](https://github.com/zvec-ai/zvec-grep/blob/main/docs/04-pipeline.md)。

项目可以通过 `runtime_policy` 调整行为，例如：

```json
{
  "compaction": {
    "enabled": true,
    "reserve_tokens": 4096,
    "keep_recent_tokens": 12000,
    "summary_max_tokens": 1500,
    "max_compactions": 2
  },
  "local_search": {
    "enabled": true,
    "tools": ["zvec_grep_search", "zvec_grep_rg"],
    "max_tool_rounds": 8
  }
}
```

`reserve_tokens` 是留给模型输出的额度，也是 Claude 协议 `max_tokens` 的来源。
`keep_recent_tokens` 是最近事件与工具结果的子预算，按"最新优先"消费；当它
超过单次请求预算的一半时会被自动收窄，而不是让调用失败——因此 8k 级别的小
窗口模型在默认策略下依然可用。

搜索工具只在两个条件同时满足时才会通告给模型：项目配置了
`workspace_path`，且本机能启动 zvec-grep 可执行文件。否则该次调用直接不带
工具，避免每轮工具失败各消耗一次真实模型请求。可用性可通过
`GET /api/v2/projects/{project_id}/search` 查询。

工具调用轮次用尽时，网关会撤下工具并要求模型基于已获得的证据给出最终结果，
输出中带 `tool_rounds_exhausted` 标记，而不是丢弃全部已完成的工作。每一轮
请求发送前都会重新按预算裁剪工具轨迹，最旧的完整工具轮次先被丢弃。阻塞与
流式两条路径都会把 `reserve_tokens` 作为请求的 `max_tokens` 发出，因此同一份
预算的输入侧和输出侧不会互相矛盾。

结构化摘要只会使用显式声明 `summarization` 能力的模型别名；没有这样的别名
时使用确定性摘要，不会回退到任意一个已启用模型。

### 聊天流式输出

普通对话发送到 `POST /api/v2/sessions/{session_id}/messages/stream` 时，
服务端使用 SSE 推送 `start`、`text_delta`、`tool_call`、`tool_result`、
`done` 或 `error` 事件。OpenAI-compatible 和 Claude Messages 模型会分别
按各自的流式协议解析；聊天文本在传输中是增量的，最终仍以完整 assistant
消息写入会话。工具调用参数必须聚合完成后才执行，避免半截 JSON 被当成结果。

旧的 `POST /api/v2/sessions/{session_id}/messages` 接口继续返回完整 JSON，
任务模式仍使用原有的整包节点结果和原子文件发布契约。若本地模型端点不
支持 SSE，流式服务会通过兼容网关回退为单个 `text_delta` 和 `done` 事件。

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
  — optional loop-role bindings, defaulting to the long-horizon node's model alias;
  each configured loop role still uses one fixed alias for every round.

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

Each role has one fixed `model_alias`. A task selects a role and always runs on
that role's model; model routing is changed by editing the role, not by changing
individual task payloads.

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
- `app/application`: use-case facade and the shared model invocation boundary;
- `app/infrastructure`: JSON persistence, provider gateway, workspace runtime,
  and the local zvec-grep adapter;
- `app/web`: the V2-only local API and workbench UI.

There is no compatibility `/api/agent` surface or second task/runtime model.

## API

Core read endpoints:

- `GET /api/v2/catalog`
- `GET /api/v2/strategies`
- `GET /api/v2/resources`
- `GET /api/v2/projects`
- `GET /api/v2/projects/{project_id}/workspace`
- `GET /api/v2/projects/{project_id}/search` — local zvec-grep readiness
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
python -m compileall -q app tests
node --check app/web/static/workbench.js
node --check app/web/static/collaboration.js
python -m unittest discover -s tests -v
git diff --check
```
