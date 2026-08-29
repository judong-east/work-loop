# Workbench V2 Architecture

Workbench V2 is the only live product architecture. It does not expose a
compatibility API and does not assemble a second agent runtime.

## Boundaries

```text
HTTP/UI -> WorkbenchService
           |-- ResourceCenter
           |-- Project / Session repositories
           `-- DagOrchestrator
                |-- NodeRegistry + WorkflowCatalog
                |-- ModelGateway
                `-- WorkspaceRuntime
```

The domain owns provider/model references, projects, sessions, task policy,
structured context, workflows, nodes, and node-run events. Infrastructure owns
HTTP model protocols, secret files, JSON persistence, workspace I/O, and process
execution. The web layer only maps `/api/v2` resources to application methods.

## Model resources

A `ModelProvider` is a connection and authentication policy. A provider may
serve any number of `ModelAlias` records and may expose OpenAI, Claude, or both
protocols. Workflows store aliases, never physical provider model names.

Credential values are written under `workbench/resources/secrets`; serialized
providers contain only a credential reference.

## Project workspace

A project may declare:

- one absolute `workspace_path`;
- one default model alias;
- durable instructions and knowledge references;
- zero or more validation commands represented as argv arrays.

`WorkspaceRuntime.snapshot` reads a bounded UTF-8 view while excluding common
generated and metadata directories. It does not follow directory or file
symlinks.

Implementation nodes propose `file_changes`. Only complete text-file writes are
accepted. Paths must be relative and remain under the configured workspace.
Writes to repository metadata, `.workloop`, or the root `workbench` data
directory are rejected, as are deletion, non-UTF-8 overwrite, path traversal,
symlink targets, duplicate paths, and oversized batches. Files are published
atomically. Complete generated file contents are discarded after publication;
only path and hash evidence enters durable session context.

Validation commands are project-owned argv arrays. They run with `shell=False`,
a small inherited environment, a fixed timeout, and bounded captured output.
They are never inferred from model text.

## Execution and Gates

The default graph is:

```text
requirement -> planning -> implementation -> testing -> review
```

Each node receives a bounded `context_pack` with task policy, node identity,
structured shared context, workspace snapshot, and recent events. Completed
outputs are validated against the node contract before entering shared context.

A failed validation command or a review verdict other than `pass` produces a
blocked `quality_review` Gate. Human and replan failures also block. The normal
run endpoint cannot bypass a blocked Gate; the caller must explicitly approve
or replan. Repeating the same failed phase three times produces a
`loop_detected` Gate.

## Persistence and recovery

Projects and sessions are separate atomic JSON records. Node events include a
serialized `NodeRun`, allowing the orchestrator to skip completed or explicitly
skipped nodes on resume. Provider/model catalogs, workflows, node contracts,
health results, and session events are durable local data.

## Security properties

- server binds only to `127.0.0.1`;
- cross-origin writes are rejected;
- request bodies are bounded and must be JSON objects;
- record identifiers and workflow/node identifiers are validated;
- secrets never enter session context or API responses;
- model output cannot invoke a shell;
- only user-configured validation argv arrays are executed;
- model file operations are bounded, atomic, workspace-relative writes.
