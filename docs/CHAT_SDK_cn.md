# Chat Python SDK 公开契约

[English version](CHAT_SDK.md)

> 状态：当前 SDK 的公开契约，已实现
> 适用范围：`openevent.chat_sdk` 0.1.0

## 1. 范围与前提

Chat Python SDK 提供 `chat.v1` 写入、一次一页的 Fetch 读取和无状态消息解析。它不提供 worker、Agent runtime、UI、角色映射、应用授权、业务幂等或持久化的应用投影。

SDK 要求 Python 3.10 或更高版本、当前环境已安装
`openevent-sdk>=0.8.1`，以及一个已存在、调用方可读且
`protocol="chat.v1"` 的 Channel。SDK 使用注入的 OpenEvent 公开 client，
不会从源码安装 SDK，也不会生成 protobuf 模块。

`CHAT_PROTOCOL_cn.md` 定义 payload 字段和 turn 语义。OpenEvent API 文档定义认证、ACL、Fetch、UUID、ObjectKey 和 gRPC status 的语义。当前参考 SDK 为已安装的 `0.8.1`，Python 调用形态以该版本的公开接口为准。

## 2. 构造

```python
from openevent.sdk import OpenEventClient
from openevent.chat_sdk import create_client

events = OpenEventClient("127.0.0.1:50051", timeout_ms=1000.0)
chat = create_client(
    events,
    principal=9001,
    token="...",
    channel_id=10001,
    max_retries=3,
)
```

一个 `ChatProtocolClient` 永久绑定一个非零 `principal`、一个 token 和一个非零 `channel_id`。
构造时 SDK 确认 Channel 可读、ID 一致且 `protocol="chat.v1"`，不调用 Fetch、不恢复历史、不启动后台线程，然后返回 ready client。
client 不保存按 turn 的状态表、写入对象注册表或对写入对象的强引用。

构造函数和 `create_client()` 均接受可选的 `channel_validator`，默认为 `None`。应用可提供一个同步、只读的校验函数：
SDK 完成基础校验后，把本次 GetChannel 返回的 `ChannelInfo` 传给它，只调用一次，不额外读取 Channel。
正常返回表示通过；抛出异常则构造失败，SDK 抛出 `ChannelInitializationError`，其 `failure.category="contract"`，不重试校验，
也不把原始异常文本放入 failure。成员、名称等应用规则由该函数定义，SDK 不内置这些规则。不提供函数时只执行基础校验。

`max_retries` 必须是非负整数，`0` 表示只尝试一次。历史恢复起点由显式 `resume_turn()` 调用传入，不是构造参数。
注入 client 必须提供正的有限 `timeout_ms`（单位毫秒），以及线程安全的 `get_channel`、`get_status`、`fetch`、`get_uuid`、`get_seq_by_uuid` 和 `publish_auto_seq` 方法。

SDK 只读取、不修改注入 client 的 `timeout_ms`，也不关闭注入 client 或其 gRPC channel。

`parse_message(event_message)` 是公开的无状态解析函数：它把一条 OpenEvent `EventMessage` 严格解析为 `ParsedMessage`，校验该条消息的
`chat.v1` JSON 和基础字段，但不维护跨事件 turn 状态、不执行 Fetch，也不判断应用角色。需要自己做有限范围历史分页的应用应复用它，
不要再写一套 payload 解析器。

`ParsedMessage.payload` 同样支持 `submission.reserve` 控制事件，以 `kind` 区分事件类型，`reserved_through` 保留为 Python `int`；
字段合法性以[公开协议](CHAT_PROTOCOL_cn.md)为准。它不带 turn 身份，调用方不能假定每条解析消息都有 `turn_id`。

### 2.1 一次读取一页

```python
page = chat.fetch_page(from_seq=8451, limit=100)
for message in page.messages:
    print(message.seq, message.payload)
print(page.next_seq, page.last_seq)
```

`fetch_page()` 固定读取绑定的 Channel，并设置 `only_my_recipient=false`。一次调用只执行一个逻辑 Fetch，不自动读取下一页；同一 RPC 因暂时性错误
发生的重试仍属于这次逻辑调用。返回的 `FetchPage` 包含 `messages`、`next_seq` 和 `last_seq`。SDK 校验 Fetch 分页和顺序契约并解析每条消息，
但不保存这个页面的游标，也不修改任何写入对象。调用方继续读取时必须把返回的 `next_seq` 作为下一次 `from_seq`。
`messages` 包含已解析的 `submission.reserve` 控制事件，保留其原始 seq；SDK 不因为它不创建 turn 而从页面中删除它。

需要持续读取的应用自行循环调用 `fetch_page()`，在处理完本页后保存 `next_seq`，并决定到达尾部后的等待时间和停止条件。
SDK 不提供订阅、消息回调、扫描进度等待接口或后台轮询任务，也不调用 OpenEvent `Subscribe`。

## 3. 写入 API

```python
from openevent.chat_sdk import ObjectKey, TextPart, TurnRef

user_creation_seq = chat.single_turn(
    turn_id="user-1",
    reply_to_seqs=(),
    content=(TextPart("你好"),),
    recipients=(),
    object_keys=(ObjectKey(object_id=42, object_token="..."),),
)

writer = chat.start_turn(
    turn_id="reply-1",
    reply_to_seqs=(user_creation_seq,),
    content=(TextPart("你好"),),
)
reply_creation_seq = writer.creation_seq
seq = writer.append(
    content=(TextPart("报告已生成。"),),
    object_keys=(ObjectKey(object_id=43, object_token="..."),),
)
seq = writer.complete()
seq = chat.cancel_turn(target_turn=TurnRef(9002, "other-turn"))
```

所有发布方法成功返回都表示本次事件已经由 OpenEvent 提交；成功不表示应用投影或浏览器已经观察到这条消息。
`start_turn()` 返回公开导出的 `TurnWriter`，其 `creation_seq` 是本次创建事件位置；`single_turn()` 直接返回创建事件 seq。
这两个创建事件位置均可用于后续回复的 `reply_to_seqs`。`writer.append()`、`writer.complete()`、`cancel_turn()` 和
`reserve_submissions()` 返回各自事件已经提交的 seq。

- `single_turn` 创建 completed turn，`content` 可以为空。
- `start_turn` 创建流式 turn，`content` 必须非空；只有 `turn.start` 确认提交成功后才返回绑定该 turn 的写入对象。
- `writer.append` 追加内容，`writer.complete` 正常结束；它们只操作对象绑定的流式 turn，不再传入 `turn_id`，也不自动读取历史。
- `cancel_turn` 接收完整 `TurnRef`，目标 owner 可以不是当前 client principal。
- `single_turn`、`start_turn` 和 `writer.append` 接受 `object_keys: Iterable[ObjectKey] = ()`。附件只写入本次发布事件的 OpenEvent 顶层
  `object_keys`，不从此前事件继承或回写创建事件。`writer.append` 携带附件时仍须提供符合协议的非空 `content`。
- 只有 `single_turn` 和 `start_turn` 接受 `reply_to_seqs: Iterable[int] = ()`，默认不回复任何 turn。

所有发布方法都可以传入可选的 `recipients` 和 `extensions`。recipients 与 ObjectKey 保留调用方的顺序和重复项；`extensions` 必须是 JSON object。SDK 会在发布前拒绝本地可判断的非法输入。

SDK 将调用方提供的 `reply_to_seqs` 按原顺序写入 payload 的同名 JSON 数值数组，不排序或自动去重。本地检查只覆盖协议字段形状：
元素必须是非 bool 的正 uint64 整数，不能有重复 seq；非法输入抛出 `ChatProtocolError`。
SDK 不为回复引用调用 Fetch 或 GetStatus，不核验目标是否存在、所属 Channel 或事件 kind，也不把 seq 解析回 `TurnRef`。
调用方负责选择合法目标；目标必须是同一 Channel 中更早的 `turn.single` 或 `turn.start` 创建事件，目标 turn 可以尚未结束。
完整目标规则以 [协议第 11 节](CHAT_PROTOCOL_cn.md#11-回复关系) 为准。

### 3.1 独立写入对象

`TurnWriter` 由 `start_turn()` 或 `resume_turn()` 返回，绑定创建它的 client 及其 principal、Channel。对象公开只读的
`turn_id` 和 `creation_seq`；内部保存最后确认提交的 `last_seq`，以及 `open`、`completed` 或 `unresolved` 状态。
它不保存完整消息内容或会话历史，也不支持切换到另一个 client。

公开写入签名为：

```text
writer.append(*, content, recipients=(), object_keys=(), extensions=None) -> int
writer.complete(*, recipients=(), extensions=None) -> int
```

新建对象的 `last_seq` 等于 `creation_seq`。每次 append 或 complete 用当前 `last_seq` 填写 `pre_seq`；append 成功后在返回前
更新 `last_seq`，complete 成功后在返回前更新 `last_seq` 并进入 `completed`。同一个对象内的状态判断、发布、重试和结果登记串行执行；不同 turn
的对象可以独立写入。对象状态是本地写入状态，不是会话展示投影；`completed` 只表示本对象的 end 已提交，不改变协议的最早终态规则。

同一 turn 同时只由一个写入对象负责；调用方持有它并用它完成后续追加和结束。正常读取观察到有效取消后，调用方停止续写并释放对象；
`cancel_turn()` 只发布取消事实，不反查或修改任何对象。对象释放或析构不发起 RPC，不自动发布 end/cancel，也不表示已发出的请求被取消。
client 不保留对象引用，因此无需按 turn 回收接口或终态记录表。对象每次写入先检查绑定 client 的生命周期；client 失败或关闭后，所有绑定它的对象都不能继续写入。

append 或 complete 的发布结果不确定时，对象进入 `unresolved`，之后的 append、complete 抛出 `TurnWriterStateError`，不能沿旧链尾继续写。
一次历史读取或释放对象都不能证明旧写入不会再提交；恢复该 turn 前必须满足第 3.2 节的旧写入处置条件。
尚未发出 Publish，或能确定整次逻辑发布未提交且不会再提交时，失败保留原 `last_seq` 和 `open` 状态。`completed` 对象也拒绝后续写入。
`start_turn()` 失败时不返回对象；如果创建结果不确定，同样必须先处置原写入，再决定是否恢复。

### 3.2 显式恢复已有流式 turn

以下示例恢复一条尚未结束的旧 turn，原写入已满足下文的交接条件：

```python
writer = chat.resume_turn("unfinished-1", state_start_seq=1)
seq = writer.append(content=(TextPart("继续。"),))
seq = writer.complete()
```

公开签名为 `resume_turn(turn_id: str, *, state_start_seq: int = 1) -> TurnWriter`。它只恢复当前 client principal 在绑定 Channel 中的
目标 turn，不发布消息。`state_start_seq` 必须是非 bool 的正 uint64 整数，且不得晚于目标创建事件。

恢复前，调用方必须停止旧对象的写入，确认该 turn 的旧请求都已结束、不会再有迟到的提交；有不确定发布时，先确认原结果或保证原请求不会再提交。
不能通过丢弃未决对象再调用恢复来绕过这个前提。client 不登记同一 turn 的对象，也不代替调用方协调交接。

SDK 取得一次 GetStatus 水位，从 `state_start_seq` 按页读完固定范围，用临时状态只恢复目标的创建 seq、链尾和终态。目标仍可续写时，
返回 `open` 的新对象，并设置其 `creation_seq` 和 `last_seq`；目标不存在时抛出 `TurnNotFoundError`，已经终结时抛出 `TurnWriterStateError`。
失败不返回对象，也不在 client 中留下目标状态。恢复过程解析 `submission.reserve` 并推进读取位置，不把它当作 turn 事件。

只有显式 `resume_turn()` 扫描恢复历史；构造、普通 `fetch_page()`、创建、取消和对象的 append/complete 都不自动触发恢复。
恢复期间的错误和整个 client 的失败范围遵循第 4～5 节。

### 3.3 发布编号预留控制事件

```python
reserve_seq = chat.reserve_submissions(
    reserved_through=10000,
    recipients=(),
    extensions=None,
)
```

公开签名为 `reserve_submissions(reserved_through: int, *, recipients=(), extensions=None) -> int`。
它发布一条 `submission.reserve` 控制事件，返回该预留事件已提交的 OpenEvent seq；返回值不是 `submission_id`，也不表示任何用户消息已提交。
`reserved_through` 必须是非 bool 的正 uint64 整数，本地非法输入抛出 `ChatProtocolError`。payload 完整字段规则由
[公开协议](CHAT_PROTOCOL_cn.md)定义，方法不接受 turn、正文、回复或附件参数。

该方法不调用 Fetch 或 GetStatus，也不创建写入对象；它按第 4 节的同一 UUID 规则完成逻辑发布与重试。
SDK 不计算批大小、不比较历史预留上限，也不分配 `submission_id` 或协调多个分配者。
发号范围、内存库存、过期和换批条件由调用应用管理，不改变通用 SDK 的并发契约。

## 4. UUID、重试与错误

每次逻辑发布只在内部通过 `get_uuid()` 取得一个 UUID。第一次
`PublishAutoSeq` 前，SDK 冻结 payload、recipients、ObjectKeys 和 UUID；重试时完全复用这些值。

`DEADLINE_EXCEEDED`、`UNKNOWN`、`UNAVAILABLE` 和 `INTERNAL` 最多额外重试 `max_retries` 次。每次重试前固定等待 100 ms，避免在暂时性的网络或服务端故障期间立即连续请求。SDK 不使用指数退避或随机抖动。

同一 UUID 的 Publish 重试若返回 `ALREADY_EXISTS`，SDK 调用
`get_seq_by_uuid(uuid)` 并返回此前已经提交的 seq。它不扫描历史 UUID，也不会为同一次逻辑发布领取第二个 UUID。

写入对象的未决处理见第 3.1 节。
显式恢复中的 GetStatus 和 Fetch 错误按第 5 节统一处理。`PublishFailedError` 仍可能对应一次未完成对账但实际已提交的发布。
它的 `uuid` 属性始终是本次逻辑发布取得的非零 UUID，调用方可以把它连同结构化失败信息写入脱敏错误日志；它不是下一次 Chat 写入的参数，
也不提供跨进程恢复或重发能力。应用必须自行决定业务恢复策略。

需要表达根因的公开错误都通过只读 `failure: FailureInfo` 返回同一种结构；`FailureInfo` 从 `openevent.chat_sdk` 公开导出：

```python
@dataclass(frozen=True)
class FailureInfo:
    stage: str
    category: Literal[
        "external_unavailable", "authentication", "permission", "not_found",
        "protocol", "contract", "lifecycle",
    ]
    grpc_code: grpc.StatusCode | None
    retryable: bool
    detail: str
```

- `stage` 指出失败发生在哪一步，例如 `GetChannel`、`GetStatus`、`Fetch`、`get_uuid`、`PublishAutoSeq` 或 `GetSeqByUuid`。
- `category` 是应用应当用于分支判断的稳定分类；不要解析 `detail`。
- `grpc_code` 保留最后一次收到的 gRPC status；失败不是 gRPC 返回或尚未收到 status 时为 `None`。
- `retryable` 只说明根因在外部条件恢复后重新建立 client 可能成功，不是要求调用方立即重试，更不表示写入可以安全重发。
- `detail` 必须说清具体原因，例如 `Fetch failed after 4 attempts` 或 `Fetch returned seq 119 followed by seq 118`，不能只写
  `operation failed`。它可以用于脱敏日志，但不能包含 token、ObjectKey、payload 或完整正文。

分类按根因确定：外部服务或传输暂时不能完成合法调用是 `external_unavailable`；凭据无效、ACL 拒绝和资源明确不存在分别是
`authentication`、`permission`、`not_found`；非法 `chat.v1` 输入或历史是 `protocol`；OpenEvent 响应违反公开契约、服务明确报告
持久化数据损坏，或 SDK 发出的已校验请求仍被当作非法请求拒绝，是 `contract`；已经发出的操作只因显式关闭
而无法给出结果、且没有更具体根因时才是 `lifecycle`。

gRPC status 需要结合操作阶段解释，稳定映射如下：

| gRPC status 或本地根因 | `category` |
| --- | --- |
| `CANCELLED`、`DEADLINE_EXCEEDED`、`UNKNOWN`、`UNAVAILABLE`、暂时性 `RESOURCE_EXHAUSTED`，以及重试耗尽的暂时性 `INTERNAL` | `external_unavailable` |
| `UNAUTHENTICATED` | `authentication` |
| `PERMISSION_DENIED` | `permission` |
| 明确的资源不存在 | `not_found` |
| 非法 `chat.v1` 输入或历史 | `protocol` |
| `DATA_LOSS`、SDK 已确认的持久化数据损坏、SDK 已校验请求收到 `INVALID_ARGUMENT`、意外的 `ABORTED`/`ALREADY_EXISTS`、非法响应或不可能的本地状态 | `contract` |
| 只由显式关闭造成的操作终止 | `lifecycle` |

如果某个 status 在具体 RPC 中有更明确的公开语义，使用那个语义；不能仅凭错误文本猜测。具体 status 始终保留在 `grpc_code`。
`retryable=true` 只用于外部条件恢复后重建 client 可能成功的 `external_unavailable`；其它分类固定为 `false`。

`ChannelInitializationError`、`FetchPageError`、`SyncReadError`、`UuidAllocationError`、`PublishFailedError` 和 `ClientFailedError`
都公开 `failure`。包装错误必须保留最初的 `FailureInfo`，不能在 `ClientFailedError` 中丢成笼统的“client failed”。
`PublishFailedError` 另外公开 `uuid`。正常的 `chat.close()` 和本地 turn 前提错误通过各自明确的返回、异常类型及消息表达，
不伪造远程失败信息。

以下错误属于公开 API：

- `ChatProtocolError`：SDK 输入或单条 payload 非法。
- `ChannelInitializationError`：client 未能进入 ready；`failure` 给出 Channel 校验阶段和根因。
- `FetchPageError`：单页 Fetch 或单页消息解析失败；`failure` 给出实际阶段和根因。
- `TurnNotFoundError`：显式恢复没有找到目标 turn。
- `TurnWriterStateError`：写入对象已完成或未决，或者显式恢复的目标已终结。
- `SyncReadError` 和 `UuidAllocationError`：尚未发出 Publish；`failure` 给出失败的 RPC 或生命周期根因。
- `PublishFailedError`：取得 UUID 后，PublishAutoSeq 或 UUID 查询失败，或已经开始的 Publish 被 client 生命周期中断。其 `uuid` 和
  `failure` 可用于记录错误；无论 `failure.retryable` 为何，都不能据此自动重发一条新的 Chat 消息。
- `ClientFailedError` 和 `ClientClosedError`：client 永久失败或已关闭。

## 5. Client 生命周期

构造完成后 client 是 ready，随后只能永久失败或关闭。失败或关闭的 client 都不能恢复或继续使用。`resume_turn()` 是一次完整恢复操作：
GetStatus 或恢复 Fetch 的远端错误重试耗尽，或返回不可重试的远端错误，均使整个 client 永久失败；参数非法、目标不存在或已终结只拒绝本次恢复。
协议历史非法、Fetch 响应非法或本地派生状态损坏也会让 client 永久失败。错误通过当前同步方法抛出的异常通知调用方；
之后的 client 调用及绑定对象的写入均抛出保留原始 `FailureInfo` 的 `ClientFailedError`。单页 Fetch 的普通调用错误只结束本次调用；调用方自行决定是否重试或退出。

```python
chat.close()
```

`close()` 幂等，并支持 context manager。它拒绝新工作，唤醒重试等待，并等待已经在途的工作结束，包括绑定对象发起的写入。
关闭后绑定对象的写入抛出 `ClientClosedError`。关闭不需要枚举写入对象，不取消已经发出的 RPC，也不关闭注入的 OpenEvent client。

底层连接由它的所有者管理。需要中断该连接上的在途 RPC 时，由所有者调用 `OpenEventClient.close()`；具体行为遵循 OpenEvent SDK 的公开关闭契约。
关闭共享连接会影响所有使用它的 Chat clients；取消 RPC 不能证明远端写入没有提交。

## 6. 部署与限制

每个 `(principal, channel_id)` 最多部署一个活跃 Chat 写入进程；同一进程可以持有不同 turn 的多个写入对象。SDK 不提供跨进程锁、租约、持久化游标、checkpoint 或应用投影。
应用必须协调写入者、保存自身需要的读取位置和业务恢复状态，并自行安排重启后的读取与处理。

SDK 构造和对象写入不重建历史。显式 `resume_turn()` 的耗时随读取范围增长，不维护编号分配状态或核验历史预留额度。
每条消息最多接受 1024 个 ObjectKey，SDK 错误不会记录 ObjectKey token、principal token、payload 或完整正文。
