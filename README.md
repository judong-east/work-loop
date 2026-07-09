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

## Multi-Model Role Routing + Code Review Loop

`run-loop` runs plan -> execute -> code review with role-routed models, and the boundary
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

The loop is an iterative code-review cycle:

- The executor must output structured file changes as JSON
  (`{"changes": [{"path": ..., "action": "write|delete", "content": ...}]}`).
- Changes are policy-checked (`deny_paths`, sandbox escape protection) and applied
  atomically to the task sandbox `workspace/` — never to the real project directory.
- The system generates a unified diff and the reviewer must return a structured
  verdict (`{"verdict": "pass|revise|block", "summary": ..., "issues": [{"file",
  "line", "severity", "message", "suggestion"}]}`).
- `revise` feeds the issues back to the executor for another round, up to
  `PolicyBoundary.max_iterations`; `pass` -> `done`; `block`, unparseable output,
  or exhausted iterations -> `clarification_required`.

Loop artifacts are written under `artifacts/` (`plan.md`, `model_calls/<n>-<role>/`,
`rounds/<n>/{changes.json,policy_check.json,changes.diff,review.json}`), all referenced
by task-relative paths. The final file state lives in `workspace/`.

## Full Workflow: requirement + files -> CLI models -> review -> deliver

```powershell
# 1. 需求 + 指定文件/目录作为上下文（可重复 --context-file）
python -m app.cli create-task `
  --title "订单去重" --goal "识别重复订单" `
  --input "目标：识别重复订单。验收标准：通过测试。" `
  --context-file docs\需求.md --context-file src

# 2. 播种真实代码进沙箱并跑循环（executor/reviewer 可配 claude/codex 等 CLI）
python -m app.cli run-loop --task-id TASK-xxx --models-config models.json --workspace-from src

# 3. 任务停在 clarification_required / policy_blocked 时：查看问题、人工答复、重跑门禁
python -m app.cli resume --task-id TASK-xxx                # 列出待确认问题
python -m app.cli resume --task-id TASK-xxx --answer "确认：阈值取 0.8。"
#    回到 ready_for_plan 后再次 run-loop

# 4. done 之后，把审核通过的变更写回真实目录（交互确认；--yes 跳过）
python -m app.cli deliver --task-id TASK-xxx --dest src
```

Notes:

- Unparseable executor/reviewer output is retried once with a corrective prompt
  before the task stops at `clarification_required`.
- Re-running `run-loop` on a resumed task overwrites `rounds/` and `model_calls/`
  artifacts; the full history stays in `logs/audit.jsonl`.
- `deliver` computes add/modify/delete against the seeded base snapshot, re-checks
  `deny_paths` against the destination, and asks for interactive confirmation —
  this is the human gate for the restricted `write_file` semantics. Consider
  delivering to a clean directory first to inspect the result.
- Before writing anything, `deliver` also runs a delivery gate
  (`DeliveryGateEvaluator`): the recorded review verdict must be `pass` and a
  delivery note (plan / review summary) must exist. The gate result is persisted
  to `evaluations/delivery_gate.json`. This defends against missing or tampered
  artifacts even when `state.json` says `done`.

## Experience Memory (review-first, cross-task)

The kernel keeps a persistent experience store in `memory/experience.jsonl`
(append-only JSONL; records fold by id, latest wins — history is never rewritten).

- Capture: blocker-severity review issues and human clarification answers are
  automatically suggested as `pending` experiences (deduplicated against all
  history, including rejected ones).
- Review-first: nothing is injected silently. Only experiences you approve are
  injected — bounded to the 10 most recent — into the planner prompt of later
  tasks. Human-authored entries are approved directly.

```powershell
python -m app.cli memory                                  # 列出 待评审/已批准/已驳回
python -m app.cli memory --approve EXP-xxx                # 批准
python -m app.cli memory --reject EXP-xxx                 # 驳回
python -m app.cli memory --add "所有 API 返回必须带 request_id"   # 手工录入（直接生效）
```

## Web Console

```powershell
python -m app.cli serve          # http://127.0.0.1:8765 （Ctrl+C 停止）
python -m app.cli serve --port 9000
```

The console is a zero-dependency local page (stdlib `http.server` + vanilla JS,
binds `127.0.0.1` only). It covers the whole workflow visually:

- create tasks (title/goal/requirement + context file paths)
- a pipeline stepper per task (requirement gate -> plan -> execute -> review -> deliver)
- start `run-loop` with one click (models config + optional seed directory);
  the loop runs in a background thread and the page polls progress per round
- inspect plan, per-round diffs (colored), structured review issues, delivery record
- answer pending questions (`resume`) in a text box
- deliver with an explicit red confirm button — the web equivalent of the
  CLI's interactive `input()` gate for restricted `write_file`
- an Experience Memory tab to review pending experiences (approve/reject),
  add manual ones, and see what will be injected into future planning

## Reliability Rules

- Every task has state.
- Every context section has a source reference.
- Every evaluator returns structured issues.
- Every decision records action, reason, confidence, and next state.
- Boundary policy can block execution before any agent or tool acts.
- Events and audit logs are persisted for replay and debugging.
- Captured experience stays pending until a human approves it; only approved
  experience is ever injected into later tasks.

