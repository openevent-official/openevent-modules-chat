# OpenEvent Chat 模块

[English version](README.md)

OpenEvent Chat 定义面向用户与 Agent 永久会话的精简事件协议 `chat.v1`。一个 Channel 以 append-only
方式保存 turn 事件。文本可以在模型生成过程中持续追加，不同 turn 也可以安全交错。

当前项目只包含协议文档，不提供 SDK、worker、Agent runtime、模型集成或用户界面。

## 协议

使用本协议的 Channel 必须设置：

```text
ChannelInfo.protocol = "chat.v1"
```

协议定义三种事件：

- `turn.append`：创建 turn 或向其中追加文本内容。
- `turn.end`：正常完成 turn。
- `turn.cancel`：独立中止已有 turn。

完整公开规格见 [docs/CHAT_PROTOCOL_cn.md](docs/CHAT_PROTOCOL_cn.md)。

## 边界

`chat.v1` 有意不定义：

- principal 是用户还是 Agent；
- Channel visibility 或成员管理策略；
- OpenEvent `recipients` 的应用语义；
- Agent 内部状态、模型请求、工具、隐藏推理或调度过程；
- 幂等、重试或非法历史处理策略。

应用可以附加 OpenEvent ObjectKey，也可以在协议的 `extensions` 对象中保存用户可见的应用元数据。
基础协议只支持文本 content part，不允许增加其他事件 kind。

## 运行前提

- 支持 Channel、事件历史和订阅的 OpenEvent server。
- 使用 ObjectKey 附件时需要 OpenEvent `0.4.4` 或更高版本。
- 已提前创建 `protocol="chat.v1"` 的非系统 Channel。

OpenEvent 负责保存事件并执行自身的 Channel ACL，不解析或校验 `chat.v1` JSON。
