# Workbench V2 Architecture

Workbench V2 is the only live product architecture. It does not expose a
compatibility API and does not assemble a second agent runtime.

## Boundaries

```text
HTTP/UI -> WorkbenchService
           |-- ResourceCenter
           |-- Project / Session repositories
           |-- ModelInvocationService
           `-- DagOrchestrator
                |-- NodeRegistry + WorkflowCatalog
                |-- ModelGateway
                `-- WorkspaceRuntime

ModelInvocationService -> ContextCompactor -> ModelGateway
                       `-> local ZvecGrepClient -> `zg` CLI

普通聊天还可通过 `messages/stream` 走 SSE：ModelGateway 解析 OpenAI/Claude
流式事件，应用层聚合工具调用并在最终结果完成后持久化 assistant 消息；旧的
整包 JSON 消息接口和任务节点契约保持不变。

HTTP/UI -> CollaborationService
           |-- RoleProfile catalog
           |-- CollaborationTask + TaskGraph
           |-- Handoff repository
           `-- Workbench role-task executor
```

The domain owns provider/model references, projects, sessions, task policy,
structured context, workflows, nodes, node-run events, and the context view
contract. Infrastructure owns HTTP model protocols, secret files, JSON
persistence, workspace I/O, and process execution. The web layer only maps
`/api/v2` resources to application methods.

## Collaboration model

`RoleProfile` is a reusable execution identity. It binds a responsibility node,
exactly one enabled model alias, instructions, capabilities, and one workspace
access mode: `read`, `write`, or `validate`. A role without a model is invalid.

`CollaborationTask` is the durable unit of development work. It owns one role,
priority, dependency list, execution session, compact result, error, and status.
It does not select a model: execution always resolves the current model bound to
the task's role, and non-empty task-level model overrides are rejected.
`TaskGraph` verifies that dependencies exist and form a DAG.

`CollaborationService` executes one ready dependency wave at a time. Independent
read-only tasks use a bounded thread pool. Write and validation roles are
serialized. Completed tasks publish `Handoff` records to direct dependents;
failed or blocked dependencies propagate a blocked status.

Every role task still runs through `DagOrchestrator` as a single-node workflow.
This keeps model routing, output contracts, workspace writes, validation,
events, and quality Gates consistent. Collaboration sessions are durable but
hidden from the ordinary conversation list.

Default roles are materialized lazily after at least one enabled provider/model
pair exists. This keeps a fresh workspace empty and prevents unbound legacy
role records from being used; valid role records are replaced through the normal
role save path when their old record is malformed.

## Model resources

A `ModelProvider` is a connection and authentication policy. A provider may
serve any number of `ModelAlias` records and may expose OpenAI, Claude, or both
protocols. Authentication can be Bearer/API Key/Token headers, Basic Auth,
custom headers, query parameters, or no-auth for local gateways. Workflows store
aliases, never physical provider model names.

Credential values are written under `workbench/resources/secrets`; serialized
providers contain only a credential reference.

`ModelAlias.context_window_tokens` is optional provider metadata used by the
compactor. If it is absent, the invocation boundary uses a conservative 32k
window and project/node policy can override the reserve and recent-history
budgets.

Only the output reserve is a hard constraint: `reserve_tokens` must be smaller
than the window. `keep_recent_tokens` is a sub-budget that is clamped to at most
half of the per-request input budget, so a small-window model is never rejected
by a large default. That sub-budget is spent newest-first across tool results and
node events, which is what makes the setting observable.

## Project workspace

A project may declare:

- one absolute `workspace_path`;
- one default model alias;
- durable instructions and knowledge references;
- zero or more validation commands represented as argv arrays.
- optional `runtime_policy.compaction` and `runtime_policy.local_search`
  settings; old projects receive safe defaults when loaded.

`WorkspaceRuntime.snapshot` reads a bounded UTF-8 view while excluding common
generated and metadata directories. It does not follow directory or file
symlinks.

Implementation nodes propose `file_changes`. Only complete text-file writes are
accepted. Paths must be relative and remain under the configured workspace.
Writes to repository metadata, `.workloop`, or the root `workbench` data
directory are rejected, as are deletion, non-UTF-8 overwrite, path traversal,
symlink targets, duplicate paths, and oversized batches. A symlink is rejected
before path resolution, because resolving first would dereference it: an
in-workspace link would be followed silently and the published evidence would
name the link instead of the file actually written. Files are published
atomically. Complete generated file contents are discarded after publication;
only path and hash evidence enters durable session context.

Publication runs once per node execution, outside the retry loop. Failures
before any side effect still retry under the node's `retry` policy; a failure
after files are published is terminal, because batches are atomic individually
but there is no rollback across attempts, and re-invoking the model would write
a second, different version of the same files.

Validation commands are project-owned argv arrays. They run with `shell=False`,
a small inherited environment, a fixed timeout, and bounded captured output.
They are never inferred from model text.

## Execution and Gates

The default graph is:

```text
requirement -> planning -> implementation -> testing -> review
```

Each model node goes through `ModelInvocationService`. `ContextCompactor`
estimates the request budget, keeps the authoritative structured state intact,
and creates a per-call `ContextView`. When the configured threshold is crossed,
it clips low-value history, asks an optional summary model for a structured
summary, applies a deterministic hard bound, and persists a
`context_compaction` event. The original `ContextState` is never replaced by
the compact view. Completed outputs are validated against the node contract
before entering shared context.

The invocation service can expose `zvec_grep_search` and `zvec_grep_rg` as
provider-native tools. Both routes run through `ZvecGrepClient`, which invokes
the separately maintained local `zg` executable with `shell=False`, fixed
project-root scope, bounded output, and a timeout. Ordinary calls use
`--refresh off`; index creation/refresh remains an explicit local zvec-grep
operation. No remote MCP or Remote Embedding endpoint is configured by
Workloop.

Tools are advertised only when a workspace root exists and the local executable
can be launched; an unavailable backend degrades to a plain call rather than
failing once per round. The per-request input budget is re-applied before every
round of both the blocking and streaming tool loops, dropping the oldest complete
tool rounds first, because the transcript grows on each round and would otherwise
exceed the window the compactor prepared. Both loops also send the compaction
output reserve as the request's `max_tokens`, so the two halves of the same
budget cannot disagree. When the round cap is reached, the gateway withdraws the
tools and requests a final answer from the evidence already collected, marking
the output `tool_rounds_exhausted`.

`local_only` and `allow_remote_embedding` are invariants enforced in
`Project.validate`, not tunable policy fields; they are absent from the default
policy so they do not read as options.

A failed validation command or a review verdict other than `pass` produces a
blocked `quality_review` Gate. Human and replan failures also block. The normal
run endpoint cannot bypass a blocked Gate; the caller must explicitly approve
or replan. Repeating the same failed phase three times produces a
`loop_detected` Gate.

## Persistence and recovery

Projects, sessions, roles, collaboration tasks, and handoffs are separate atomic
JSON records. Node events include a serialized `NodeRun`, allowing the
orchestrator to skip completed or explicitly skipped nodes on resume.
Provider/model catalogs, workflows, node contracts, health results, and session
events are durable local data.

## Security properties

- server binds only to `127.0.0.1`;
- cross-origin writes are rejected;
- request bodies are bounded and must be JSON objects;
- record identifiers and workflow/node identifiers are validated;
- secrets never enter session context or API responses;
- model output cannot invoke a shell;
- zvec-grep arguments are structured and the local CLI is invoked with
  `shell=False`;
- only user-configured validation argv arrays are executed;
- model file operations are bounded, atomic, workspace-relative writes;
- project coordination is single-owner, and workspace writers/validators are
  serialized;
- task results and handoffs are size-bounded and never contain provider secrets.
