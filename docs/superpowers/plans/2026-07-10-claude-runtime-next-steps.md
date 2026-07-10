# Claude Code 只读规划与独立审核实施计划

## 需求理解

下一票实现 `ClaudeCodeRuntime`，让现有持久工作流使用 Claude Code 完成两个只读角色：

- `planner`：直接搜索和读取隔离 worktree，输出可供用户批准、Codex 执行的 `ExecutionPlan`。
- `reviewer`：使用独立于 planner 的 Claude session，读取完整工作区、批准计划、diff 和 Workloop 验证证据，输出 `ReviewResult`。

规划结果存在 `open_questions` 时继续停在人工门禁之前。审核结果沿用现有 `pass | revise_code | replan | blocked` 处置；`revise_code` 后恢复原 reviewer session，跟踪问题是否解决。源规格见 `docs/superpowers/specs/2026-07-10-agent-runtime-orchestration-design.md` 第 7、9、13、14 节，票据见 `tickets.md` 的“使用 Claude Code 规划与独立审核”。

当前基线提交为 `2af46c2`。Claude Code `2.1.198` 已安装并通过 OAuth 登录；Codex CLI `0.144.1` 已安装但未登录，后者不阻塞 Claude Runtime 的离线协议测试，但会阻塞真实完整工作流。

## 非目标

- 不实现持久队列、服务重启恢复、DeliveryReport、Git 交付或 Web 控制台。
- 不实现 Anthropic API Runtime，不自行编写模型工具循环。
- 不允许 Claude 编辑文件、调用网络工具、加载 MCP、插件、hooks 或用户级动态配置。
- 第一版不向 Claude reviewer 开放 Bash。项目测试继续由 Workloop 的 `DeterministicValidator` 独立执行，Claude 只审核其证据。
- 不重构已通过复审的 Codex Runtime，除非共享协议抽取有明确测试收益。

## 涉及文件与符号

- `app/agents/claude_code.py`：新增 `ClaudeCodeProfile`、`ClaudeCodeRuntime`，负责命令构建、健康检查、预算、取消和 session 恢复。
- `app/agents/claude_protocol.py`：新增 ExecutionPlan/ReviewResult Schema 与 Claude stream-json 事件解析，避免把供应商协议继续堆入单个 Runtime 文件。
- `app/agents/contracts.py`：仅在 Claude 事件或结果暴露现有契约缺口时做向后兼容扩展。
- `app/agents/runtime.py`：复用 `AgentRuntime`、`RoleRoutedRuntime`；除非测试证明生命周期契约不足，否则不修改。
- `app/agents/workflow.py`：验证 planner/reviewer session 隔离、reviewer 恢复及现有人工门禁；保持角色无关。
- `app/core/process_tree.py`、`app/core/redaction.py`：直接复用进程树终止和统一脱敏。
- `tests/test_claude_runtime.py`：新增本地假 Claude 进程协议测试。
- `tests/fixtures/claude/`：保存与 Claude Code `2.1.198` 对齐的最小 stream-json fixture。
- `tests/test_agent_workflow.py`：补 Claude planner + Codex executor + Claude reviewer 的角色路由集成测试。
- `tickets.md`：双轴复审通过后勾选本票五项。

## 实施步骤

1. 先写失败测试，固定 stdin、cwd、只读权限参数、设置来源、Schema、事件、session、超时、取消和健康检查行为。
2. 定义 `ClaudeCodeProfile`。launcher 的 executable 之后禁止携带任意 CLI 选项，所有模型、权限、工具和设置来源由 Workloop 追加并冻结。
3. 构建非交互命令：提示只走 stdin；使用 `--print`、`--input-format text`、`--output-format stream-json`、`--json-schema`；显式选择 model 和 worktree。
4. 强制只读能力：使用原生 plan/只读权限模式，只提供 `Read`、`Glob`、`Grep`，显式禁用编辑、写入、Web、MCP、插件与动态设置来源。不得使用任何 bypass permissions 参数。
5. 实现 planner 与 reviewer 两套版本化 JSON Schema，并把 Claude 原始事件转换为统一 `AgentEvent`；每次调用必须恰好一个最终终态事件。
6. 提取 session、结构化结果、usage、CLI 版本、model 和有效配置；恢复时只接受当前角色已持久化的 session。planner 和 reviewer 不得共享 session。
7. 复用总超时、无事件超时、进程树取消和脱敏逻辑。提示、结果、原始事件和错误在落盘前按项目策略脱敏。
8. 健康检查验证命令存在、版本解析和 `claude auth status` 认证映射，不发起模型调用或网络请求。
9. 通过 `RoleRoutedRuntime` 完成 Fake Claude planner -> Fake Codex executor -> Workloop validator -> 独立 Fake Claude reviewer 的持久工作流测试，并覆盖一次 `revise_code` 后 reviewer session 恢复。
10. 运行定向与全量测试，执行 Spec/Standards 双轴复审；两路均 PASS 后勾选票据并单独提交。

## 约束

- 提示不得出现在 argv、进程标题或普通日志中。
- planner/reviewer 请求必须是 `AgentAccess.READ_ONLY`；收到 executor 或 workspace-write 请求时立即 `policy_blocked`。
- 不信任提示词作为安全边界，权限必须由 Claude CLI 参数和 Workloop diff 门禁共同执行。
- 不加载用户、项目或本地 settings，不允许 fallback model 静默切换模型。
- 不启用 WebSearch、WebFetch、Edit、Write、NotebookEdit、MCP、Chrome 或额外目录。
- 原始 Claude 输出不能直接推进业务状态，必须先通过对应结构化契约校验。
- 任务身份、session、runtime 版本、model、预算和权限配置必须落盘且可复查。
- 保持当前用户未提交文件不变；只暂存本票涉及文件。

## 验收标准

- Claude planner 能在真实临时 Git worktree 中搜索和读取代码，并生成可由 `ExecutionPlan.from_dict` 接受的完整计划。
- `open_questions` 非空的计划不能进入 Codex 执行阶段。
- planner 和 reviewer 使用不同 session；多轮 reviewer 调用恢复 reviewer 自己的 session。
- Claude reviewer 能输出四种 verdict，`pass` 仍受现有逐项验收和 blocker 校验约束。
- 命令行不包含提示，且不包含 bypass、写权限、网络、额外目录或动态配置参数。
- 结构化失败、认证失败、超时和取消都产生正确错误类型与唯一终态事件。
- 取消能够终止完整 Claude 进程树；提示、事件、结果和错误均无敏感信息泄漏。
- 健康检查准确报告 Claude Code `2.1.198` 的版本与当前认证状态。
- 角色路由集成测试证明 Claude 规划、Codex 执行、Workloop 验证、Claude 审核可以串联，并能自动完成一次返修循环。
- Spec 与 Standards 复审均为 PASS，全量测试通过。

## 必须运行的测试

```powershell
python -m compileall -q app
python -m unittest tests.test_claude_runtime -v
python -m unittest tests.test_agent_workflow -v
python -m unittest tests.test_codex_runtime tests.test_process_tree -v
python -m unittest discover -s tests -q
git diff --check
```

另外执行不调用模型的本机健康探测：`claude --version` 与 `claude auth status`。禁止把真实付费模型调用作为自动测试前置条件。

## 风险

- Claude Code stream-json 事件字段会随 CLI 版本变化。解析器必须容忍未知事件，但缺少 session、最终 result 或 structured output 时不得成功。
- CLI 的 plan 模式与工具白名单仍需通过受控假进程验证最终 argv；Workloop 的执行前后 diff 门禁继续作为第二道防线。
- `--json-schema` 与 stream-json 的最终结构化结果字段需要以 `2.1.198` fixture 固定，升级 CLI 时必须更新协议 fixture。
- reviewer 不直接运行测试会减少代理自主性，但能保持只读边界；独立验证证据已经满足第一版审核输入需求。
- Codex 当前未登录，真实端到端烟雾测试将在 Claude Runtime 完成后仍需先处理 Codex 认证。

## 未决问题

无。第一版采用保守边界：Claude planner/reviewer 均不开放 Bash，测试只由 Workloop 独立运行。若后续确实需要 reviewer 复跑命令，应作为单独权限票据设计命令级沙箱和人工授权，不在本票中隐式扩大权限。
