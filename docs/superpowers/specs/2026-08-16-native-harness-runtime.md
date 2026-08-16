# Workloop 原生 Harness 运行时：摆脱终端 CLI

- 日期：2026-08-16
- 状态：已实现
- 关联：2026-08-07 能力与价格感知编排（该文档"终端和供应商 CLI 是执行适配器"的假设由本文档推翻）

## 背景

Workloop 此前把每个模型角色都交给一个终端 CLI 子进程执行：Claude Code、
Codex CLI、Pi RPC。这带来三重限制：

1. 模型受制于外部 harness 的固定工具集、权限模型和提示词包装，无法按任务
   自主决定工具用法；
2. 每个 CLI 都有自己的安装、登录、配置、版本和协议漂移（Pi 0.83 的
   session stats 就是例子）；
3. 无沙箱运行时（Pi）写节点要么整体拒绝、要么整机放开。

两个参照系证明这不是必须的：

- **Pi**（`@earendil-works/pi-coding-agent`）：极简 harness，模型 + 工具循环
  就是全部；
- **DeepSeek Harness**（2026-08-13 开源，MIT）：提出 "Model + Harness =
  Agent"，模型负责思考推理，harness 负责工程化执行（工具调度、会话、重试），
  models/tools/sessions 全部插件化，不绑定终端。

## 决策

Workloop 自己当 harness。新增 `native` 运行时（`NativeHarnessRuntime`）：

- 直接调用任意 OpenAI 兼容 `chat/completions` 端点（DeepSeek、GLM、Kimi、
  OpenAI 等），在进程内运行工具调用循环，不启动任何 CLI 子进程；
- 工具由宿主实现（`harness_tools`）：`read_file`、`list_files`、
  `search_content`、`write_file`、`edit_file`、`run_command`。模型自主决定
  用哪些工具、按什么顺序、何时结束；工具结果以普通文本回给模型，被拒绝的
  调用返回错误说明而不是异常，模型可自行纠正；
- 结构化输出沿用 Pi 的文本 JSON 契约与既有的单次修复重试路径。

## 边界（比 Pi 更强，与 Codex 对齐的部分）

- 所有文件工具在进程内强制路径解析到任务 worktree 之内，越界路径与项目
  保护路径（`.env`、`secrets/**` 等）直接拒绝——这是 Pi 做不到的真隔离；
- `run_command` 是唯一能离开 worktree 的工具，因此只有它沿用
  `WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR` 门槛：项目策略拒绝网络且未显式
  放开时，该工具不出现在模型的工具列表里，文件工具不受影响；
- 只读角色只有读/列/搜三种工具；
- 确定性验证、独立审核、确认交付门禁完全不变，native 只替换模型执行层。

## 会话、预算与事件

- 会话是宿主可读写的 JSON 消息日志（`.workloop-native-sessions/<task>/`），
  同节点重试/返修恢复会话，跨节点不共享，逐轮原子落盘；
- 请求级总超时、空闲超时、工具轮数上限（默认 60）与用户取消（含在途 HTTP
  调用）都由运行时强制；usage 按 prompt/completion/cached 累加，费用由
  目录单价估算（与 2026-08-07 规格一致）；
- 工具开始/完成、消息增量、终止事件进入统一事件流，控制台实时可见。

## 配置

模型目录条目（schema v2）新增 `base_url`、`api_key_env`、`max_tokens`。
密钥永不进入目录：`api_key_env` 指名环境变量，或用
`WORKLOOP_NATIVE_KEY_FILE` 指向密钥文件（兼容裸 key、`K=v`、JSON）。

一条环境变量路径可以整栈切到 native（无任何 CLI 依赖）：

```powershell
$env:WORKLOOP_NATIVE_BASE_URL="https://api.deepseek.com/v1"
$env:WORKLOOP_NATIVE_MODEL="DeepSeek-V4-Flash"
$env:DEEPSEEK_API_KEY="..."
python -m app.cli serve --root . --port 8765
```

`WORKLOOP_NATIVE_PLANNER_MODEL` / `_EXECUTOR_MODEL` / `_REVIEWER_MODEL`
可分角色覆盖，另有 `WORKLOOP_NATIVE_PROVIDER`、`WORKLOOP_NATIVE_THINKING`、
`WORKLOOP_NATIVE_MAX_TOKENS`。CLI 运行时（claude_code/codex_cli/pi_rpc）
保持可用，可与之混排。
