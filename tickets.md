# Tickets: 本地编码代理编排

将 Workloop 重构为通过 Claude Code 与 Codex CLI 完成规划、执行、验证、审核和交付的持久编排器。源规格见 `docs/superpowers/specs/2026-07-10-agent-runtime-orchestration-design.md`。

Work the **frontier**: any ticket whose blockers are all done. For a purely linear chain that means top to bottom.

## 建立持久工作流与 FakeRuntime 纵向闭环

**What to build:** 用户创建任务并获得结构化计划，批准后由脚本化代理执行、验证和审核；任务最终进入待交付状态，返修能够恢复执行会话，全部状态和工件可在重新加载后恢复。

**Blocked by:** None — can start immediately.

- [x] 计划批准前禁止执行。
- [x] FakeRuntime 直接修改任务工作区，并产生标准化运行结果。
- [x] 审核 `revise_code` 自动返回执行者，`pass` 进入待交付。
- [x] 状态、计划、轮次、session 和审核工件原子落盘并可重新加载。
- [x] 新路径与现有 107 项测试并存，不破坏旧工作流。

## 从注册项目创建 Git worktree

**What to build:** 用户注册一个干净 Git 项目并从不可变基线创建任务；所有代理只在独立 worktree 和任务分支中工作。

**Blocked by:** 建立持久工作流与 FakeRuntime 纵向闭环。

- [x] 项目记录仓库根、默认目标分支和配置引用。
- [x] 脏工作区默认阻止任务启动。
- [x] 任务保存 `base_commit`、worktree 和任务分支身份。
- [x] 取消或清理任务不会修改真实工作目录。

## 执行项目策略与确定性验证

**What to build:** 用户批准计划后，Workloop 只运行项目允许的测试和检查命令，保存完整证据，并阻止越权路径、网络和失败验证进入审核通过状态。

**Blocked by:** 从注册项目创建 Git worktree。

- [x] 项目策略声明验证命令、受保护路径、超时和网络规则。
- [x] 执行前后 diff 经过统一策略检查。
- [x] 验证保存命令、退出码、输出、错误和耗时。
- [x] 必需验证失败时审核不能 `pass`。

## 使用 Codex CLI 执行和返修

**What to build:** 用户批准计划后，Codex CLI 在受限 worktree 中直接编辑代码；审核返修恢复原 Codex session，Workloop 通过事件流观察进度并支持取消。

**Blocked by:** 执行项目策略与确定性验证。

- [x] 指令通过标准输入传递，不进入命令行参数。
- [x] Codex 使用 workspace-write 沙箱和非交互审批策略。
- [x] JSONL 事件转换为统一 AgentEvent。
- [x] session、runtime 版本、model、预算和最终结果落盘。
- [x] 取消终止完整进程树并记录终态。

## 使用 Claude Code 规划与独立审核

**What to build:** Claude Code 在只读 worktree 中分析真实代码、生成可批准计划，并由隔离审核会话检查完整代码、diff 和验证证据。

**Blocked by:** 执行项目策略与确定性验证。

- [x] 规划与审核使用相互隔离的 Claude session。
- [x] 工具、权限、目录和设置来源由适配器显式约束。
- [x] 计划与审核结果使用结构化 Schema。
- [x] 审核支持 `pass`、`revise_code`、`replan` 和 `blocked`。
- [x] 多轮审核能够恢复审核 session。

## 持久队列、预算与重启恢复

**What to build:** 用户可以排队多个任务；Workloop 串行调度可运行阶段，等待人工的任务释放执行槽，服务重启后能够恢复或重跑中断阶段。

**Blocked by:** 使用 Codex CLI 执行和返修；使用 Claude Code 规划与独立审核。

- [x] 队列位置、AgentRun、session、轮次和预算持久化。
- [x] 等待澄清、计划批准、权限和交付的任务不占执行槽。
- [x] 总超时、无事件超时、费用和最大轮次触发暂停。
- [x] 启动扫描将残留运行标记为 `interrupted`。
- [x] 用户可以恢复、重跑当前阶段或终止。

## 生成可审核提交并安全交付

**What to build:** 审核通过后，用户获得绑定确定提交的 DeliveryReport；目标分支变化时重新整合、验证和审核，最终通过合并或拣选交付。

**Blocked by:** 持久队列、预算与重启恢复。

- [x] DeliveryReport 覆盖验收、变更、验证、审核、风险和后续步骤。
- [x] 待交付任务形成独立任务提交。
- [x] 目标分支前进使旧验证与审核失效。
- [x] 冲突暂停人工处理。
- [x] 未经人工确认不得修改目标分支。

## 将 Web 控制台重构为任务操作台

**What to build:** 用户通过 Web 提交需求、回答澄清、批准计划、查看事件与验证、处理阻塞并确认交付；页面只展示当前状态允许的主动作。

**Blocked by:** 持久队列、预算与重启恢复；生成可审核提交并安全交付。

- [x] 首页优先展示运行中、待人工、失败、阻塞和待交付任务。
- [x] 任务详情展示计划、变更、验证、审核和标准化事件。
- [x] 不同阻塞原因提供定向操作。
- [x] 模型配置收缩为 runtime 健康状态和角色 Profile。
- [x] 移除任意流程节点编辑和第一版经验记忆主导航。
- [x] 桌面、窄屏、键盘和可访问性测试通过。

## 兼容旧任务并收缩旧入口

**What to build:** 用户仍可查看旧任务及其可用工件，但新任务只进入新状态机；损坏历史不会导致整个详情失败，旧通用 CLI 写入口被安全降级。

**Blocked by:** 将 Web 控制台重构为任务操作台。

- [x] 旧任务只读展示并标明历史工作流版本。
- [x] 缺失或绝对路径工件显示局部不可用。
- [x] 旧模型配置可以迁移为 AgentProfile。
- [x] 任意命令模板不能获得可写 executor 权限。
- [x] 旧入口有明确弃用或移除路径。

## 使用 Workloop 完成真实自举验收

**What to build:** 用户使用 Workloop 修改 Workloop 自己，经历计划批准、Codex 实现、独立验证、Claude 有效返修、服务重启恢复、最终复审和 Git 交付。

**Blocked by:** 兼容旧任务并收缩旧入口。

- [x] 真实需求跨多个文件并补充测试。
- [ ] 至少完成一次有效 `revise_code` 循环。
- [x] 中途重启后从可靠节点恢复。
- [x] 最终测试、DeliveryReport、审核和提交均可审计。
- [x] 记录成功率、人工介入、时间、费用、恢复和无关 diff 指标。

### 本次验收记录（TASK-7f09af231d62）

- 结果：1/1 任务交付成功；交付提交 `f7a644a`；交付后测试 172/172 通过。
- 变更：10 个提交文件，包含指标功能、Web/API 测试及自托管过程中发现的验证和交付可靠性修复；非预期无关 diff 为 0。
- 时间：墙钟时间约 3 小时 6 分钟；累计 agent 活跃时间 2631.861 秒。
- 费用：累计 0.254638 USD。
- 恢复：19 次 recover 队列操作；服务重启后复用 executor session 并从可靠阶段继续。
- 人工介入：计划批准 1 次；宿主侧处理了基线前进、Windows 短路径、Git worktree 基线、原生 reviewer 结果规范化和确认交付。
- 审核：Claude reviewer 使用同一 session 完成两次独立复审，均判定通过；未发生 `revise_code`，因此对应验收项仍保持未完成。

## 增加受控工作流配置

**What to build:** 在不开放任意命令、任意权限或任意状态跳转的前提下，让个人任务选择可持久化的 Agent 节点工作流，并允许低风险流程自动越过计划审批。

- [x] 提供“标准审批”和“自动推进”内置工作流。
- [x] 自定义工作流固定 planner、executor、validation、reviewer、delivery 契约，并允许可选 plan approval。
- [x] Agent 节点支持附加要求，权限仍由节点类型强制决定。
- [x] 任务冻结工作流快照，目录后续修改不改变运行中任务。
- [x] Web 支持配置、选择和查看任务工作流。
- [x] 自动推进仍保留澄清、审核返修、策略验证和最终人工交付门禁。
