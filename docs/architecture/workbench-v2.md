# Workbench V2 Architecture

This document turns the supplied V2 design report into the repository's
concrete boundaries.

## Request flow

```text
browser -> /api/v2 -> WorkbenchService
                       |-- ResourceCenter (providers, aliases, credentials)
                       |-- JsonCollection (projects, sessions)
                       `-- DagOrchestrator
                             |-- NodeRegistry (contracts and handlers)
                             |-- WorkflowCatalog (DAG persistence)
                             `-- ModelGateway (vendor adapter)
```

The old agent runtime remains behind `/api/agent` while task data migrates. It
is deliberately not imported by the V2 domain objects, so a new provider or
node type cannot change delivery permissions in the compatibility runtime.

## Domain contracts

`Project` owns durable instructions, knowledge references, and a default model
alias. `Session` belongs to one project and has an explicit `SessionMode`:

- `chat` records a user message and an immediate assistant response;
- `task` records the user request and runs a `WorkflowDefinition`.

`ContextState` is the only implicit handoff between nodes. It has named buckets
for facts, artifacts, decisions, inputs, and errors. Node output is validated by
its `NodeDefinition` before it is merged, so malformed output stops at the
responsible node instead of poisoning every downstream prompt.

## Failure and resume rules

Every node produces a `NodeRun` event. The orchestrator skips completed or
skipped nodes when resuming a persisted session. A node can declare `retry`,
`human`, `skip`, or `replan` as its failure policy. Dependencies are checked
before execution and a cycle is rejected before any model is called.

## Resource and security rules

Provider JSON stores only a `credential_ref`; the actual API key is in a
provider-specific file under `resources/secrets`. The UI receives provider
metadata and health state, never secret contents. Model aliases are the stable
binding in workflow nodes, so changing a physical model does not require editing
the workflow graph. `OpenAICompatibleGateway` resolves those aliases and sends a
contract-specific JSON request to `/chat/completions`; invalid model output fails
at the producing node before context is updated.

## Extending the system

Register a `NodeDefinition` and a Python handler with `NodeRegistry`, or load a
contract-only catalog from JSON/YAML. Handlers receive a session, node, and
bounded `ContextState`; they return a structured object matching the node's
output fields. Model-backed nodes use the injected `ModelGateway` instead.
