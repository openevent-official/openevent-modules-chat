# Chat Python SDK 快速开始

[English version](SDK_USAGE.md)

本文只给出最小调用示例。完整的公开 API、重试和生命周期规则见
[CHAT_SDK_cn.md](CHAT_SDK_cn.md)。

以下示例使用按创建事件 seq 回复、独立写入对象、显式恢复、空内容 start 与 reset 接口。

## 运行前提

安装 `openevent-modules-chat` 及其 `openevent-sdk>=0.11.1` 依赖。构造
client 前，需要创建一个 `protocol="chat.v1"` 的非系统 Channel。

## 创建 Client

```python
from openevent.sdk import OpenEventClient
from openevent.chat_sdk import TextPart, create_client

events = OpenEventClient("127.0.0.1:50051", timeout_ms=1000.0)
chat = create_client(
    events,
    principal=9001,
    token="...",
    channel_id=10001,
)
```

每个 `(principal, channel_id)` 只部署一个活跃写入进程；不同 turn 可以各自持有写入对象。SDK 不拥有传入的
OpenEvent client 或它的 gRPC channel。
构造不会读取历史；需要历史时由调用方用 `fetch_page(from_seq, limit)` 一页一页读取。

## 写入与读取

```python
user_creation_seq = chat.single_turn(
    turn_id="user-1",
    reply_to_seqs=(),
    content=(TextPart("你好"),),
)

writer = chat.start_turn(
    turn_id="reply-1",
    reply_to_seqs=(user_creation_seq,),
    content=(TextPart("你好"),),
)
reply_creation_seq = writer.creation_seq
writer.append(content=(TextPart("，很高兴见到你。"),))
reply_end_seq = writer.complete()
del writer

fetch_seq = 1
while True:
    page = chat.fetch_page(from_seq=fetch_seq, limit=100)
    for message in page.messages:
        print(message.seq, message.payload)
    fetch_seq = page.next_seq
    if fetch_seq > reply_end_seq:
        break
```

写入方法成功返回表示 OpenEvent 已经提交消息。示例中的应用循环从历史起点逐页读取，
处理完本页再保存 `next_seq`，读过回复结束事件的位置后结束。持续读取也由应用组织读取和处理循环；
到达尾部后的等待和停止条件见[单页读取契约](CHAT_SDK_cn.md#21-一次读取一页)。

保存 `single_turn` 返回的 seq，或 `start_turn` 返回对象的 `creation_seq`，回复时传入 `reply_to_seqs`。
SDK 只检查字段形状，不读取历史核验目标；调用方按 [协议第 11 节](CHAT_PROTOCOL_cn.md#11-回复关系) 选择合法目标。

流式 turn 使用 `start_turn` 取得写入对象，然后零次或多次调用它的 `append()`，最后调用 `complete()`。
写入对象保存自己的链尾，client 不保存 turn 状态；完成后可释放对象。使用 `cancel_turn` 记录对既有 TurnRef 的取消；正常读取观察到有效取消后，
调用方停止续写并释放对应对象，释放本身不发布事件或撤销在途请求。
继续旧 turn 必须显式调用 `resume_turn()`；前提和失败处理见[写入 API](CHAT_SDK_cn.md#3-写入-api)。
输出中途生成文件时，参见[写入 API 的追加附件示例](CHAT_SDK_cn.md#3-写入-api)。

## 重置未结束的输出

模型流中断后，应用先停止接收旧模型输出，并等待旧的 Chat 写入全部确认完成，再在同一个 writer 上重置并写入重试输出。
下面先用空内容建立可取消的 turn，再追加模型输出；空内容的完整条件见[协议第 4 节](CHAT_PROTOCOL_cn.md#4-content-part)。

```python
writer = chat.start_turn(
    turn_id="retryable-reply-1",
    reply_to_seqs=(user_creation_seq,),
    content=(),
)
writer.append(content=(TextPart("中断前的部分内容"),))
# 已停止旧模型的输出调度，旧 Chat 写入均已确认；本地 writer 仍为 open。
writer.reset()
# 可以直接继续重试输出；正常读取观察到有效取消时停止续写。
writer.append(content=(TextPart("重试得到的新内容"),))
writer.complete()
```

也可以用 `writer.reset(content=(TextPart("重试的第一段"),))` 一次清除旧内容并写入新首段。
reset 成功返回只表示事件已提交，不能把提交成功当作用户尚未取消的证明。应用可以在重试前回读，但这不是必须的同步屏障，
也不能排除回读后发生新的取消；按正常读取观察到有效取消时停止续写即可。
网页仍显示同一个 turn，创建位置和回复关系不变。内容与附件的替换、取消竞态、发布未决和恢复限制只由
[公开写入契约](CHAT_SDK_cn.md#31-独立写入对象)规定；不能用 reset 绕过未决写入或重新打开已经结束的 turn。

## 关闭

```python
chat.close()
events.close()
```

示例先等待 Chat 调用结束，再释放 `events`；中断在途请求的方式见[生命周期契约](CHAT_SDK_cn.md#5-client-生命周期)。
方法失败通过异常返回，详见[错误契约](CHAT_SDK_cn.md#4-uuid重试与错误)。
