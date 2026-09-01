# Agent Note: Add local Pi-style compaction and zvec-grep search

Status: implemented

## Problem

Workloop V2 passed bounded snapshots and structured `ContextState` to model
nodes, but it had no token-aware compaction and no model-callable local
workspace search. Large task state and future search transcripts could outgrow
a provider context window, while model nodes could not retrieve focused local
evidence.

## Decision

Keep Workloop's DAG, Gate, structured output, and atomic file publishing as the
authoritative runtime. Add `ModelInvocationService` as the single application
boundary for chat, DAG nodes, long-horizon episodes, collaboration tasks, and
goal decomposition. It prepares a Pi-inspired `ContextView`, compacts older
state into a bounded structured summary when the model budget is exceeded, and
persists `context_compaction` events without mutating authoritative
`ContextState`.

Expose local zvec-grep through `ZvecGrepClient` with semantic and managed-rg
routes. The client invokes the separately maintained `zg` executable directly
with `shell=False`, fixed project-root scope, argument validation, timeouts,
output bounds, and direct mode. Normal indexed searches use refresh-off; index
creation and refresh remain explicit local zvec-grep operations. No remote MCP
or Remote Embedding route is configured by Workloop. Search tools are
provider-native and bounded by a maximum tool-round policy; existing
`ModelGateway.complete` callers and fake gateways stay compatible.

## Alternatives considered

- Reintroduce the historical Pi RPC runtime: rejected because it would create a
  second agent lifecycle, persistence model, and file-write boundary.
- Reimplement BM25, vector search, and indexing inside Workloop: rejected
  because zvec-grep already owns those concerns and should remain independently
  upgradeable.
- Add only a fixed-character snapshot cap: rejected because it does not account
  for model context, tool transcripts, or semantic preservation.
- Expose zvec-grep's raw command-string tool to the model: rejected because the
  Workloop tool contract should validate structured arguments and project scope.

## Consequences

- Model aliases can optionally declare `context_window_tokens`; projects can
  tune compaction and local-search policy through `runtime_policy`.
- Context summaries and tool events are durable session evidence, while the
  original structured context and existing workspace-write contract remain
  authoritative.
- A missing local `zg` installation does not prevent ordinary model calls; a
  search request returns a bounded tool error and the readiness endpoint reports
  the local dependency state.
- The web API exposes `GET /api/v2/projects/{id}/search` for readiness and the
  model form exposes the context-window setting. New tests cover compaction,
  quotas, local argv construction, tool loops, policy enforcement, and
  compatibility.
