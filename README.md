# Workloop

Workloop is a minimal reliable loop-engineering kernel for programmer work automation. It intentionally starts with the system core, not with a pile of prompts.

The first version implements these contracts:

- Task state
- Context pack with provenance
- Boundary policy check
- Context evaluation
- Decision result
- Callback event
- Artifact and audit log

## Core Loop

```text
Task -> Build Context -> Evaluate -> Check Policy -> Decide -> Emit Events -> Persist Artifacts
```

This is the baseline for later agents:

- requirement extraction
- operator experience structuring
- solution design
- code context analysis
- implementation planning
- code modification
- validation
- delivery

## Run

```powershell
cd D:\judong\code\workloop
python -m app.cli create-task `
  --title "异常订单处理优化" `
  --goal "形成可靠方案并准备实现计划" `
  --input "目标：减少异常订单人工处理时间。验收标准：能识别重复订单，并通过测试。"
```

The task output is written under:

```text
tasks/<task-id>/
  state.json
  contexts/
  evaluations/
  decisions/
  callbacks/
  artifacts/
  logs/audit.jsonl
```

## Multi-Model Role Routing

`run-loop` runs plan -> execute -> review with role-routed models, and the boundary
policy enforces reviewer model != executor model before any model is invoked.

```powershell
python -m app.cli run-loop `
  --task-id TASK-xxx `
  --models-config models_smoke.json
```

`models_smoke.json` is an offline example config (fake backend). A real config uses
`"provider": "cli"` with a command template such as
`["claude", "-p", "{prompt}", "--model", "{model}"]`; only `{prompt}` and `{model}`
placeholders are substituted, and the command runs with `shell=False`.

Roles: `planner`, `executor`, `reviewer`; unknown roles fall back to `default`.
Loop artifacts are written under `artifacts/` (`plan.md`, `execution.md`,
`review.json`, `model_calls/<n>-<role>/`), all referenced by task-relative paths.

## Reliability Rules

- Every task has state.
- Every context section has a source reference.
- Every evaluator returns structured issues.
- Every decision records action, reason, confidence, and next state.
- Boundary policy can block execution before any agent or tool acts.
- Events and audit logs are persisted for replay and debugging.

