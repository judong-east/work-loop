# Agent Note: 模型流式输出

Status: implemented

## Problem

此前模型网关以一次性 JSON 读取响应，HTTP 消息接口和前端只能等待完整
Session 返回；异步协同轮询只展示状态，不能展示模型生成过程。

## Decision

Workloop 增加了兼容旧接口的端到端 SSE 聊天链路。`OpenAICompatibleGateway`
现在可以为 OpenAI Chat Completions 和 Claude Messages 请求 `stream=true`，
解析 SSE 事件，并以 provider-neutral 的 `text_delta`、`tool_call`、
`tool_result`、`done` 事件向上层传递。聊天流使用内部纯文本模式，最终仍
包装为现有 `result` 字段并以一条完整 assistant 消息持久化；工具调用参数
在网关内聚合完成后执行，之后继续下一轮模型流。

HTTP 新增
`POST /api/v2/sessions/{session_id}/messages/stream`，使用 `text/event-stream`
并在每个事件后 flush。前端用 `fetch` 的 `ReadableStream` 解析 SSE，增量
更新消息气泡；现有 `/messages` 一次性 JSON 接口和任务模式整包节点契约
不变。流式网关不可用时，调用服务通过已有非流式路径回退为一个文本增量和
一个完成事件。

## Alternatives considered

- 只在 HTTP 层切块：模型网关仍然整包读取，无法提前产生真实内容。
- 删除结构化 JSON 契约：会破坏现有任务节点、工具和持久化上下文契约。
- 仅依赖异步协同轮询：只能展示状态，不能展示模型文本增量。

## Consequences

收益：本地 OpenAI-compatible/Claude SSE 服务可以在普通对话中实时显示文本；
工具调用、上下文压缩和最终 Session 落盘仍沿用原有边界；不支持 SSE 的端点
仍可使用兼容接口。代价：流式接口为聊天专用，任务节点的结构化输出仍在节点
完成后原子提交；不同本地网关的 SSE 细节可能不同，解析器保留单包 JSON 回退；
客户端断开时服务端关闭迭代器并将会话恢复为 idle。
