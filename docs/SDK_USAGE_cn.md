# Chat Python SDK 使用指南

[English version](SDK_USAGE.md)

## 1. 安装与依赖

`openevent-modules-chat` 要求 Python 3.10 或更高版本，并依赖当前 Python 环境中已经安装的
`openevent-sdk>=0.4.4`。

```bash
make build
make install
```

构建和测试不会从仓库中的 `openevent-sdk` 子模块安装 SDK，也不会在运行时生成 proto。

## 2. 无状态解析

```python
from openevent.chat_sdk import TurnStart, parse_message, parse_payload

event = parse_payload(payload_bytes)
parsed = parse_message(event_message)

if isinstance(parsed.payload, TurnStart):
    print(parsed.turn_ref, parsed.payload.content)
```

`parse_payload` 严格要求 UTF-8 JSON、唯一对象字段名、有限 JSON number 和协议规定的精确字段集合。
`parse_message` 另外保留 `seq`、`ts_ms`、Channel ID、发布 principal、recipients 和 ObjectKeys。
ObjectKey token 的 `repr` 始终脱敏。

## 3. 创建有状态写入客户端

调用方先创建一个支持并发 RPC 的 `openevent.sdk.OpenEventClient`，再绑定固定身份和非空 Channel 集合：

```python
from openevent.chat_sdk import create_client
from openevent.sdk import OpenEventClient

openevent_client = OpenEventClient("127.0.0.1:9527")
chat = create_client(
    openevent_client,
    principal=9001,
    token="...",
    channel_ids=[10001, 10002],
)
```

初始化会校验所有 Channel 的 `protocol="chat.v1"`，并阻塞恢复到固定水位。Chat client 不取得注入的
OpenEvent client 或 gRPC channel 的所有权；两者必须在 Chat client 关闭后再关闭。

## 4. 发布 turn

```python
from openevent.chat_sdk import ObjectKey, TextPart, TurnRef

start_seq = chat.start_turn(
    channel_id=10001,
    turn_id="turn-user-1",
    reply_to_turns=[],
    content=[TextPart("你好")],
    recipients=[],
    object_keys=[ObjectKey(7001, "object-token")],
    extensions={"ui": {"language": "zh-CN"}},
)

append_seq = chat.append_turn(
    channel_id=10001,
    turn_id="turn-user-1",
    content=[TextPart("，世界")],
)

end_seq = chat.complete_turn(channel_id=10001, turn_id="turn-user-1")
cancel_seq = chat.cancel_turn(
    channel_id=10001,
    target_turn=TurnRef(principal=9002, turn_id="turn-agent-1"),
)
```

SDK 在每次发布前由调用方线程通过 GetStatus 取得固定水位 `W`，用 `max(sync_target_seq, W)` 增大 client 共享的单调同步
目标，并等待唯一的内部同步线程使用 Fetch 追平自己的 `W`。之后 SDK 自动选择 append/end 的 `pre_seq` 并发布，在同步线程
处理到已提交 seq 后才返回。同步线程只执行 Fetch 和状态更新，不执行 GetStatus 或 PublishAutoSeq。同一 client 中同一
TurnRef 同时只能有一个发布调用；不同 TurnRef 可以并发发布。

应用仍应直接使用 OpenEvent Fetch/Subscribe 读取、展示和处理 Chat 事件，再通过 `parse_message` 解析。

## 5. 错误与关闭

- `ChatProtocolError` 表示调用输入或单条 payload 不符合 `chat.v1`。
- `TurnNotFoundError`、`TurnAlreadyExistsError` 和 `TurnBusyError` 是发布前可确定的 turn 状态错误。
- `SyncReadError.publish_sent` 固定为 `False`，表示发布前同步失败且本次没有发送 PublishAutoSeq。
- `PublishFailedError.code` 保留 PublishAutoSeq 的 gRPC status。
- `PublishCommittedSyncError.seq` 表示消息已经提交，但内部顺序确认没有完成；不得把它当成未提交重试。
- `ClientFailedError` 表示 client 已永久 fail-stop，后续有状态调用不会再发起网络请求。

应使用 context manager 或显式调用 `close()`。关闭不会关闭注入的 OpenEvent client；并发、重复 `close()` 是幂等的。

## 6. 验证

```bash
make test
make build
```

真实端到端测试只使用当前环境已安装的 `openevent-sdk`。运行前配置：

```bash
export OPENEVENT_E2E_ADDR=127.0.0.1:9527
export OPENEVENT_E2E_PRINCIPAL=9001
export OPENEVENT_E2E_TOKEN=...
export OPENEVENT_E2E_CHAT_CHANNEL_ID=10001
make e2e
```

指定 Channel 必须预先存在、可由该身份完整读取，并设置 `protocol="chat.v1"`。
