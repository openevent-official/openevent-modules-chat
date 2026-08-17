# OpenEvent Chat 模块

[English version](README.md)

OpenEvent Chat 定义面向用户与 Agent 永久会话的精简事件协议 `chat.v1`。一个 Channel 以 append-only
方式保存 turn 事件。文本可以在模型生成过程中持续追加，不同 turn 也可以安全交错。

项目同时提供 Python SDK `openevent.chat_sdk`，用于严格解析 `chat.v1`、恢复 turn 写入状态并安全构造
start/append/end/cancel。项目不提供 worker、Agent runtime、模型集成或用户界面。

## 协议

使用本协议的 Channel 必须设置：

```text
ChannelInfo.protocol = "chat.v1"
```

协议定义四种事件：

- `turn.start`：使用写入方生成的 `turn_id` 创建 turn 并携带第一段文本内容；turn 由 owner principal 与
  `turn_id` 共同标识。
- `turn.append`：向已有 turn 追加文本内容。
- `turn.end`：正常完成 turn。
- `turn.cancel`：独立中止已有 turn。

`turn.cancel` 只记录取消事实。应用应通过自己的 Fetch/Subscribe 观察生效的取消事件，并自行停止对应的模型、工具或
其它业务处理；Chat 协议不直接中止应用任务。

一个 turn 进入终态后，针对它的后续 append、end 和 cancel 即使已经提交，也不改变已决终态、最终内容或终态前链尾。
实现不需要为这些事件增加特殊的解析或验证顺序。

完整公开规格见 [docs/CHAT_PROTOCOL_cn.md](docs/CHAT_PROTOCOL_cn.md)。

## Python SDK

SDK 要求 Python 3.10 或更高版本，以及当前环境中已安装的 `openevent-sdk>=0.4.4`。常用入口：

```python
from openevent.chat_sdk import TextPart, create_client

with create_client(
    openevent_client,
    principal=9001,
    token="...",
    channel_ids=[10001],
) as chat:
    seq = chat.start_turn(
        channel_id=10001,
        turn_id="turn-01JABC",
        reply_to_turns=[],
        content=[TextPart("你好")],
    )
```

安装、解析、发布、失败状态和并发约束见 [docs/SDK_USAGE_cn.md](docs/SDK_USAGE_cn.md)。

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
- 使用 Python SDK 时需要当前环境已安装 `openevent-sdk>=0.4.4`；直接实现协议的应用使用 ObjectKey 附件时也需要
  OpenEvent `0.4.4` 或更高版本。
- 已提前创建 `protocol="chat.v1"` 的非系统 Channel。

OpenEvent 负责保存事件并执行自身的 Channel ACL，不解析或校验 `chat.v1` JSON。
