# Chat Python SDK

[English version](SDK_USAGE.md)

## 1. 实现状态

当前公开仓库包含可安装的 Python SDK、构建入口和 SDK 单元测试，以及
`chat.v1` 公开协议。

目标实现要求 Python 3.10 或更高版本，并使用已安装的
`openevent-sdk>=0.6.0`。实现必须使用 OpenEvent 公开 client，不得从源码子模块安装 SDK 或生成 protobuf 模块。

## 2. 协议解析

直接实现协议的应用必须按照 [CHAT_PROTOCOL_cn.md](CHAT_PROTOCOL_cn.md) 校验每条 payload，按 OpenEvent `seq`
顺序处理，并将 `EventMessage.uuid` 视为 OpenEvent 消息去重标识。OpenEvent 不解析或校验 `chat.v1` JSON。

## 3. SDK 边界

SDK 是 `chat.v1` 的有状态写入辅助，不提供 worker、Agent runtime、模型集成、UI 或应用层授权。公开 API 不暴露
消息 UUID 的分配；注入的 OpenEvent client 维护本地 UUID 池，SDK 每次真正发布时在内部取得一个 UUID。

SDK 同时提供订阅回调注册接口。应用通过 Chat SDK 接收按 seq 排序的 `ParsedMessage`；SDK 内部使用 Fetch 轮询，不调用
OpenEvent `Subscribe`。订阅 Fetch 到达尾部时按退避继续轮询，RPC 失败按统一规则重试。

```python
from openevent.sdk import OpenEventClient
from openevent.chat_sdk import create_client

events = OpenEventClient("127.0.0.1:50051", timeout=1.0)
chat = create_client(
    events,
    principal=9001,
    token="...",
    channel_id=10001,
    on_failure=lambda error: print(error),
)
```

目标调用方式：

```python
subscription = chat.register_subscription_callback(
    on_message,
    from_seq=1,
    on_error=on_subscription_error,
)

subscription.close()
```

`from_seq=0`（默认值）以首次 Fetch 线性化时返回的水位为起点，只接收其后产生的新消息；它不承诺覆盖注册调用开始前的全部历史。
需要无缝覆盖既有历史时传入明确的起点（通常为 `1`）。传入大于 0 的 seq 会通过 Fetch 从该位置接收历史和后续消息。
订阅句柄的 `wait_until_scanned(seq)` 等待 Fetch 返回的 `next_seq` 越过指定水位。

创建 Chat SDK client 时必须传入 `on_failure`。当 client 在 READY 状态下因内部 Fetch、订阅 Fetch 或协议状态故障永久进入 FAILED
时，SDK 调用一次该回调；初始化失败、单次写入错误和 `close()` 不触发它。回调只用于通知，不能恢复 client；即使调用方不需要处理
通知，也必须显式传入空操作回调。回调同步执行且没有 SDK 超时，必须在有限时间内返回。

完整的同步、恢复、生命周期和错误规则属于本 SDK 发布的实现契约。

## 4. 运行前提

- 支持 `chat.v1` Channel、事件历史和消息 UUID 的 OpenEvent server。
- 已提前创建 `protocol="chat.v1"` 的非系统 Channel。
- 使用未来 Python SDK 时，当前环境已安装 `openevent-sdk>=0.6.0`。
