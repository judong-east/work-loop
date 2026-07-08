# Workloop 多模型分工能力设计（Multi-Model Role Routing）

- 日期：2026-07-03
- 状态：设计已获用户批准，待生成实施计划
- 关联代码库：`D:\judong\code\workloop`

## 1. 背景与动机

单一模型对自身输出的复核存在系统性盲区（自我偏好、同源错误模式）。本设计为 Workloop 内核引入"按环节路由到不同模型"的能力：计划制定（planner）、任务执行（executor）、结果审核（reviewer）可分别配置独立模型，并由边界策略强制"审核模型 ≠ 执行模型"，使异构制衡成为内核保证而非配置自觉。

当前内核（v1）没有任何模型调用层：`ContextEvaluator`、`DecisionEngine` 均为纯启发式规则。因此本设计同时引入两层能力：

1. 模型调用抽象（契约、后端、审计）
2. 按角色路由与策略校验

## 2. 范围

**做：**

- 模型契约：`ModelProfile` / `ModelRequest` / `ModelResponse` / 路由配置
- 配置加载与校验：`models.json`
- 角色路由：`ModelRouter`
- 两个后端：CLI 子进程后端（真实调用）、fake 后端（离线测试）
- 策略扩展：distinct 角色对校验（默认 `executor` 与 `reviewer` 必须不同模型）
- 最小编排闭环：`run_model_loop`，plan → execute → review，全过程落盘可回放
- CLI 子命令：`run-loop`
- 前置件：`task_state_from_dict`（从 `state.json` 加载已有任务，`run-loop` 依赖）
- 关联缺陷修复：`DecisionEngine` 阈值硬编码改为读取 `PolicyBoundary.min_context_confidence`

**不做（YAGNI 清单，见第 13 节）：** HTTP API 后端、自动迭代重试、prompt 模板配置化、工具调用、多任务并发。

## 3. 设计假设（已随设计一并获批）

| # | 决策 | 内容 |
|---|------|------|
| 1 | 范围 | 契约/路由层 + CLI 子进程后端 + fake 后端；后端为抽象接口，未来可加 HTTP 后端而不改内核 |
| 2 | 角色 | 角色为自由字符串；约定初始角色 `planner` / `executor` / `reviewer`；配置必须含 `default` 兜底 |
| 3 | 配置 | 项目根 `models.json`；CLI 以 `--models-config` 覆盖路径 |
| 4 | 强约束 | `PolicyBoundary.distinct_model_roles` 默认 `[["executor", "reviewer"]]`，编排开始前校验，违反即 `POLICY_BLOCKED` |

**"不同模型"判定语义**：按 `ModelProfile.model` 字符串判等。同厂商不同型号（如 opus 与 sonnet）视为不同模型；需要更强异构性时通过配置跨厂商实现，机制不变。

## 4. 架构

```
app/
├── core/
│   ├── contracts.py        [扩展] +模型契约、+task_state_from_dict
│   └── workflow.py         [扩展] +run_model_loop(task_id, ...)
├── models/                 [新增]
│   ├── __init__.py
│   ├── config.py           # models.json 加载与校验
│   ├── router.py           # ModelRouter.resolve(role) -> ModelProfile
│   └── backends/
│       ├── __init__.py
│       ├── base.py         # ModelBackend 抽象接口
│       ├── cli_backend.py  # subprocess 调用本机 CLI（shell=False）
│       └── fake_backend.py # 预置应答，离线测试
├── decision/decision_engine.py  [修改] 阈值改读 PolicyBoundary
├── policy/policy_checker.py     [扩展] +check_model_assignment
└── cli.py                       [扩展] +run-loop 子命令
```

依赖方向：`models/` 只依赖 `core/contracts.py`；`workflow.py` 依赖 `ModelBackend` 抽象而非具体后端（DIP）。契约继续集中于 `contracts.py`（跟随现有模式）。

## 5. 数据契约（加入 contracts.py）

```python
@dataclass
class ModelProfile:
    name: str                    # profile 标识，如 "reviewer-alt"
    provider: str                # "cli" | "fake"
    model: str                   # 模型标识，如 "claude-opus-4-8"
    command: list[str] = field(default_factory=list)
                                 # CLI 命令模板；仅支持 {prompt} 与 {model} 两个占位符
    timeout_seconds: int = 300

@dataclass
class ModelRequest:
    task_id: str
    role: str
    prompt: str

@dataclass
class ModelResponse:
    text: str
    profile_name: str
    model: str
    duration_seconds: float
    succeeded: bool
    error: str = ""

@dataclass
class ModelRoutingConfig:
    profiles: dict[str, ModelProfile]   # 以 name 为键
    roles: dict[str, str]               # 角色名 -> profile 名；必须含 "default"
```

新增反序列化：`task_state_from_dict(data) -> TaskState`（`run-loop` 加载已有任务的前置件）。

## 6. 配置文件格式与校验规则

`models.json` 示例：

```json
{
  "profiles": [
    {"name": "plan-opus",   "provider": "cli", "model": "claude-opus-4-8", "command": ["claude", "-p", "{prompt}", "--model", "{model}"]},
    {"name": "exec-sonnet", "provider": "cli", "model": "claude-sonnet-5", "command": ["claude", "-p", "{prompt}", "--model", "{model}"]},
    {"name": "review-alt",  "provider": "cli", "model": "gpt-x",           "command": ["other-cli", "{prompt}"]}
  ],
  "roles": {
    "planner": "plan-opus",
    "executor": "exec-sonnet",
    "reviewer": "review-alt",
    "default": "exec-sonnet"
  }
}
```

加载期校验（`config.py`，失败即抛异常，不进入执行）：

1. `roles` 必须包含 `default`
2. `roles` 引用的每个 profile 必须存在
3. profile `name` 不得重复
4. `provider == "cli"` 时 `command` 非空且包含 `{prompt}`
5. `timeout_seconds > 0`

路由规则（`router.py`）：`resolve(role)` 命中 `roles[role]`；未配置的角色回落 `roles["default"]`；每次解析结果（角色 → profile → model，是否兜底）写入审计日志。

## 7. 策略校验

`PolicyBoundary` 新增字段：

```python
distinct_model_roles: list[list[str]] = field(default_factory=lambda: [["executor", "reviewer"]])
```

`PolicyChecker.check_model_assignment(policy, routing) -> PolicyCheck`：对每组角色解析出各自 `model`，组内出现相同值即 `passed=False`，issue 注明冲突角色与模型。该检查在编排开始前执行（策略先于一切执行的既有原则），失败时任务转 `POLICY_BLOCKED`，任何模型都不会被调用。

## 8. 编排流程 `run_model_loop`

入口：`WorkloopKernel.run_model_loop(task_id, routing, backend)`。前置条件：任务状态为 `READY_FOR_PLAN`（由 `create-task` 产生），否则显式报错。

```
check_model_assignment ──(fail)──> POLICY_BLOCKED，结束
  │
  ├─ planner  输入：任务标题+goal+context pack 各 section 内容
  │           产物：artifacts/plan.md
  │           状态：READY_FOR_PLAN -> READY_FOR_IMPLEMENTATION
  ├─ executor 输入：goal + plan.md
  │           产物：artifacts/execution.md
  │           状态：-> VALIDATION
  └─ reviewer 输入：goal + plan.md + execution.md，要求输出固定 JSON
              产物：artifacts/review.json
              verdict == "pass"            -> DONE
              verdict == "revise"|"block"  -> CLARIFICATION_REQUIRED（v1 不自动迭代，交人工）
```

reviewer 输出 JSON schema（写入 prompt 要求模型遵守）：

```json
{"verdict": "pass | revise | block", "issues": ["字符串数组，可为空"]}
```

prompt 构造：v1 硬编码于编排层（f-string 拼装上述输入字段），不做模板配置化。

**落盘与审计**（延续内核溯源原则——没有可回放的调用记录就无法事后研究盲区）：

- 每次调用在 `artifacts/model_calls/<序号>-<role>/` 下写 `prompt.txt`、`response.txt`、`meta.json`（profile、model、时长、succeeded、error）
- 事件：`model.invoked` / `model.failed`；审计：路由解析记录、每次状态迁移
- 本期所有新增工件引用一律存**相对 task 目录的路径**，不复制既有绝对路径缺陷（旧字段迁移为独立后续项）

CLI：

```
python -m app.cli run-loop --task-id TASK-xxx [--root .] [--models-config models.json]
```

## 9. 错误处理

| 情形 | 处理 |
|------|------|
| CLI 子进程超时 / 非零退出 / 空输出 | `ModelResponse(succeeded=False)`，失败工件落盘 + `model.failed` 事件，任务转 `FAILED`，绝不静默 |
| reviewer 输出无法解析为合法 JSON 或 verdict 非法 | 视为审核不可信：原始文本落盘，任务转 `CLARIFICATION_REQUIRED`（宁可误停，不可误过） |
| 配置错误（缺 default、引用缺失、重名、cli 缺 command） | 加载期抛异常，不进入执行 |
| distinct 校验冲突 | `POLICY_BLOCKED`，见第 7 节 |
| `run-loop` 指定的任务不存在或状态不是 `READY_FOR_PLAN` | CLI 显式报错退出 |

## 10. 安全

- 子进程一律 `shell=False`、列表传参；命令模板仅做 `{prompt}` / `{model}` 字面占位替换，无 shell 注入面
- `models.json` 可定义任意本机命令，与代码同权限——本工具为本地个人工具，此边界与现状一致；不引入网络与密钥

## 11. 测试策略

全部核心测试走 fake 后端，离线可跑：

1. 配置：合法配置加载成功；缺 default / 引用缺失 / 重名 / cli 缺 command 各自报错
2. 路由：角色命中；未配置角色回落 default；解析记录进审计
3. 策略：executor 与 reviewer 同 model → 阻断；不同 → 放行；自定义 distinct 组生效
4. 编排：三环节产物齐全、状态走到 `DONE`；reviewer 返回 revise → `CLARIFICATION_REQUIRED`；executor 失败 → `FAILED` 且失败工件存在；reviewer 输出非法 JSON → `CLARIFICATION_REQUIRED`
5. CLI 后端：命令模板渲染正确（mock subprocess，不真调模型）；超时与非零退出映射为失败响应
6. 回归：`DecisionEngine` 阈值改传导后，现有 4 个测试保持通过；自定义 `min_context_confidence` 在决策路径生效

## 12. 关联缺陷修复（随本期实施）

- 阈值双重定义修复（实施期修订版）：`min_context_confidence` 的判定**唯一归属 `PolicyChecker.check_context`**；删除 `decision_engine.py` 中的置信度分支（原第 48-55 行）。原因：workflow 中 policy 门禁先于决策执行且使用同一 score 与阈值，DecisionEngine 的置信度分支在 kernel 路径上不可达为真（死分支），保留它即保留逻辑重复。删除后行为不变，且自定义宽松阈值时不再被硬编码 0.65 错误拦截。`decide_after_context` 签名不变（不新增 boundary 参数）。
  - 初版方案（给 DecisionEngine 传入 boundary 参数）在代码质量审查中被推翻：它只消除了字面量重复，判定逻辑仍在两处且一处为死代码。

## 13. 明确不做（YAGNI）

- HTTP API 后端（接口已为其预留，实现另期）
- review 不过后的自动迭代重试（`max_iterations` 字段继续保留不用）
- prompt 模板配置化
- 模型工具调用（tool use）
- 多任务并发与队列
- 旧任务数据绝对路径迁移（独立后续项）

## 14. 后续项（不在本期）

- 旧 `state.json` 绝对路径引用迁移为相对路径
- HTTP API 后端（Anthropic / OpenAI 兼容）
- revise verdict 的自动回改循环（受 `max_iterations` 约束）
- `EventBus` 更名为 `EventRecorder`（命名准确性）
