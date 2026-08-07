# Workloop 能力与价格感知的任务级编排

- 日期：2026-08-07
- 状态：已实现
- 替代：2026-07-10 固定角色编排设计中的模型选择、会话和记忆部分

## 目标

Workloop 的核心目标是利用多个模型在能力、质量和价格上的差异完成任务。终端和供应商 CLI 是执行适配器，不是流程设计的中心。

新任务采用以下路径：

```text
需求
-> 规划模型生成 ExecutionPlan
-> ExecutionComposer 按步骤识别能力并选择模型
-> 生成任务专属 PlanGraph
-> 用户批准前检查或调整节点、依赖、启停、能力、权限、指令和模型
-> 节点通过对应 ModelProfile 的 Runtime 执行
-> 节点用 ContextPack 和工件引用交接
-> 宿主执行确定性验证与交付门禁
```

## 模型目录

服务从 Workloop 数据根的 `agent-profiles.json` 读取模型目录。价格由操作者维护，代码不内置可能过期的厂商价格。旧的 `roles` 格式仍可读取，但只作为兼容目录；它不能表达按能力和价格选择。

```json
{
  "schema_version": 2,
  "models": [
    {
      "profile_id": "frontend-efficient",
      "label": "Frontend efficient",
      "runtime": "pi_rpc",
      "provider": "provider-id",
      "model": "model-id",
      "access": "workspace_write",
      "capabilities": ["implementation", "frontend", "testing"],
      "quality": 4,
      "input_cost_per_million": 0.0,
      "cached_input_cost_per_million": 0.0,
      "output_cost_per_million": 0.0,
      "thinking": "medium",
      "context_window": 128000
    }
  ]
}
```

每个目录至少需要：

- 一个具备 `planning` 的只读模型；
- 一个具备 `review` 的只读模型；
- 一个具备 `implementation` 的写模型。

实现步骤还可声明 `frontend`、`backend`、`security`、`testing`、`migration`、`architecture` 和 `documentation`。安全、迁移与架构步骤要求质量等级至少为 4；目录没有合格模型时，编排立即失败并要求操作者补充模型配置。`WORKLOOP_OPTIMIZATION` 可设为 `cost`、`balanced` 或 `quality`。

## 编排边界

`PlanGraph` 只包含真正由模型执行的任务节点：`implementation`、`integration` 和 `custom`。用户可以新增、删除、启停节点，调整依赖，并为每个节点声明 `capability`、`access`、指令和模型。所有启用节点都会由图解释器按依赖顺序实际调用；只读节点可以研究或分析，写节点可以修改工作区。执行开始后任务图冻结。

规划和独立审核的模型绑定保存在图级字段 `planning_model` 与 `review_model`，但不是任务 DAG 中的伪节点。确定性验证、独立审核流程和确认交付是宿主强制门禁，位于 DAG 之外，不能通过删除或停用任务节点绕过。终端目标不决定流程；实际 Runtime 由节点的模型 profile 选择并通过 `model_profile_id` 路由。

运行时按 `model_profile_id` 路由。角色路由只服务于旧任务和缺少任务图的持久数据，不参与新任务的模型选择。Claude Code 配置只允许只读访问，Codex CLI 配置只允许工作区写访问，Pi RPC 可按配置承担两种访问级别。

## 记忆与 Token

每个节点有独立 Session。只有同一节点的崩溃恢复、重试和返修会恢复该 Session；不同节点不共享不断增长的聊天历史。

跨节点交接使用有上限的 `ContextPack`，包含任务目标、关键事实、约束、决策和工件引用。持久化 ContextPack 总计不超过 12000 字符，注入单个节点提示的交接段不超过 10000 字符。实现节点不重复接收完整计划，审核节点不在提示词中复制完整 diff，而是在只读工作区按需检查变更工件。

跨任务记忆只注入人工批准的经验。规划前按当前标题和需求计算相关性，最多选择 5 条、单条最多 600 字、总计最多 2400 字。不相关或待审批经验不进入提示词。

任务预算记录输入、输出和缓存输入 Token。可以分别设置输入、输出或总 Token 上限；达到上限后任务暂停，不启动下一个模型节点。Runtime 没有返回总费用时，Workloop 使用目录中的普通输入、缓存输入和输出单价估算费用。
