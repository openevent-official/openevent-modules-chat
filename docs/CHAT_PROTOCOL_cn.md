# Chat 协议 chat.v1

[English version](CHAT_PROTOCOL.md)

> 状态：初始规格
> 适用范围：OpenEvent Channel `protocol="chat.v1"` 的事件 payload

## 1. 协议边界

`chat.v1` 是面向用户与 Agent 永久会话的底层事件协议。一个 OpenEvent Channel 表示一段永久会话，
可以包含多个用户 principal 和多个 Agent principal。

Channel 历史本身不说明哪些 principal 是用户或 Agent。角色映射属于协议之外的应用职责。协议只记录参与者可见的
互动事件；Agent 内部状态、隐藏推理、模型请求、工具内部状态、密钥和调试日志不属于本协议。

协议只定义三种事件：

- `turn.append`
- `turn.end`
- `turn.cancel`

其它事件 kind 不属于 `chat.v1`。应用可以通过可选 `extensions` 对象附加参与者可见的元数据，
但扩展不能改变基础 turn 语义。

OpenEvent 不解析 JSON payload。本文定义事件是否符合协议，但不要求应用在遇到非法或冲突历史时必须拒绝、
跳过、展示或停止处理。

## 2. Channel 与 OpenEvent 字段

所有 Chat Channel 必须设置：

```text
ChannelInfo.protocol = "chat.v1"
```

payload 不包含版本字段。`ChannelInfo.description` 没有 `chat.v1` schema，可以保存应用自定义文本。

协议不额外限制 Channel visibility 或成员关系，成员可以动态变化。每个事件的读写权限由 OpenEvent 当前 ACL 决定。

OpenEvent 顶层字段保持原生语义：

- `EventMessage.seq` 是权威全局事件顺序。
- `EventMessage.principal` 是发布者；对于 turn 内容，它同时确定内容所有者。
- `EventMessage.ts_ms` 是服务端接收时间。
- `EventMessage.recipients` 可以在遵循 OpenEvent 服务端发布校验规则的前提下自由承载应用语义。
  `chat.v1` 不要求它为空、在同一 turn 内保持不变或与回复关系一致。
- `EventMessage.object_keys` 可以非空，其含义由应用定义；基础协议不把它与某个 content part 关联。

OpenEvent recipients 是筛选字段，不是 ACL。需要重建完整会话的读取方应使用
`only_my_recipient=false` 读取 Channel。

## 3. JSON 与公共字段

所有 `chat.v1` payload 必须是 UTF-8 JSON object。

公共字段：

| 字段 | 规则 |
| --- | --- |
| `kind` | 必填 string，只能是 `turn.append`、`turn.end` 或 `turn.cancel` |
| `turn_id` | 必填非空 string，UTF-8 编码不超过 128 bytes |
| `extensions` | 可选 JSON object，只由应用解释 |

`turn_id` 使用精确字符串相等比较。生产者和消费者不得裁剪、改变大小写或做 Unicode 规范化。
一个 `turn_id` 在一个 Channel 内最多标识一个 turn，唯一性范围是 `(channel_id, turn_id)`。
协议不规定应用如何生成它。

引用 OpenEvent seq 的字段使用范围为 `1..18446744073709551615` 的 JSON integer，即不含 0 的 `uint64`。

未知 payload 顶层字段不属于 `chat.v1`。应用数据必须放在 `extensions` 中。扩展值可以是任意 JSON value，
但必须仍是参与者可见的互动元数据，不得重新定义 `kind`、`turn_id`、`pre_seq`、回复、内容或终态语义。

payload 大小上限由 OpenEvent 部署控制，`chat.v1` 不定义第二套字节上限。

## 4. Content Part

首版只支持文本 part：

```json
{
  "type": "text",
  "text": "你好"
}
```

content 必须是非空 JSON array。每一项必须是只包含以下字段的 object：

- `type`：固定 string `text`；
- `text`：非空 string。

part 顺序有意义。应用按合法 append 链顺序重建 turn 内容，并在每个事件内保持 part 列表顺序。
分片边界没有协议语义：应用可以追加字符、词、句子或更大的文本片段。

协议不定义二进制、图片、音频、工具调用或其它 content part。应用可以在承载事件的 OpenEvent 消息上附加
ObjectKey，但这不会引入新的 `chat.v1` content 类型。

## 5. `turn.append`

`turn.append` 用于创建 turn 或向已有 turn 追加内容。

### 5.1 首次追加

```json
{
  "kind": "turn.append",
  "turn_id": "turn_01JABC",
  "reply_to_turn_ids": ["turn_01JAAA", "turn_01JAAB"],
  "content": [
    {"type": "text", "text": "你好"}
  ]
}
```

规则：

- 不得包含 `pre_seq`。
- `reply_to_turn_ids` 必须存在且为 JSON array，可以为空。
- 每个回复 ID 必须满足 `turn_id` 字符串规则。
- 数组内的回复 ID 不得重复，也不得等于当前事件的 `turn_id`。
- 每个被引用 turn 必须已经在同一 Channel 中出现首次 append；它可以仍在输出，也可以已经终结。
- `content` 必须满足第 4 节。
- OpenEvent 顶层 `principal` 成为该 turn 的内容所有者。

首次 append 永久确定该 turn 的所有者和 `reply_to_turn_ids`。

### 5.2 后续追加

```json
{
  "kind": "turn.append",
  "turn_id": "turn_01JABC",
  "pre_seq": 123456,
  "content": [
    {"type": "text", "text": "，世界"}
  ]
}
```

规则：

- `pre_seq` 必须存在，且等于该 turn 前一个合法 `turn.append` 的 OpenEvent `seq`。
- 不得包含 `reply_to_turn_ids`。
- OpenEvent 顶层 `principal` 必须等于首次 append 确定的所有者。
- `content` 必须满足第 4 节。
- 该事件必须早于 turn 的最早终态事件。

## 6. `turn.end`

`turn.end` 用于正常完成 turn。

```json
{
  "kind": "turn.end",
  "turn_id": "turn_01JABC",
  "pre_seq": 123457,
  "status": "completed"
}
```

规则：

- turn 必须已经有首次 append。
- `pre_seq` 必须存在，且等于该 turn 前一个合法 `turn.append` 的 OpenEvent `seq`。
- OpenEvent 顶层 `principal` 必须等于 turn 所有者。
- `status` 必须固定为 `completed`；`chat.v1` 没有 `failed` 状态。
- 不得包含 `content` 或 `reply_to_turn_ids`。
- 该事件只保存完成状态，不重复保存最终内容快照。

## 7. `turn.cancel`

`turn.cancel` 用于独立中止已有 turn。

```json
{
  "kind": "turn.cancel",
  "turn_id": "turn_01JABC"
}
```

规则：

- 目标 turn 必须已经在同一 Channel 中出现首次 append。
- 不得包含 `pre_seq`、`status`、`content` 或 `reply_to_turn_ids`。
- 任何被 OpenEvent 允许向该 Channel 发布消息的 principal 都可以发布，不要求它是 turn 所有者，
  也不要求协议知道它的用户或 Agent 角色。
- cancel 本身就是终态事件，不需要后续 `turn.end` 确认。

## 8. 链与终态语义

对于同一个 turn，合法 `turn.append` 与一个合法 `turn.end` 通过 `pre_seq` 形成单链。

- 首次 append 没有前驱。
- 后续 append 或正常 end 指向当前 append 链尾。
- 最多只能有一个 append 或 end 使用同一个 `pre_seq` 作为前驱；多个后继构成分叉，违反协议。
- `turn.cancel` 不是链后继，永远不包含 `pre_seq`。

一个 turn 的终态事件是在其所有符合格式的 `turn.end` 和 `turn.cancel` 中，OpenEvent 全局 `seq`
最小的事件。因此，并发完成与中止会得到所有读取方一致的确定结果。

只有早于该终态 `seq` 的合法 append 参与最终内容重建。更晚的 append、end 或 cancel 记录仍保留在不可变
OpenEvent 历史中，但不会改变 turn 的最终结果。本协议不规定应用必须如何报告或处理这些记录。

不同 turn 可以同时保持打开并并发追加，它们的事件可以在 OpenEvent 全局顺序中交错；每个 turn 的
`pre_seq` 链决定自己的局部内容顺序。

## 9. 回复关系

`reply_to_turn_ids` 表示因果上下文，不表示路由或授权。

- 它由首次 append 固定，后续不能修改。
- 空数组表示没有协议层父 turn。
- 多个 ID 允许一个 turn 同时回应多个更早的 turn。
- 被引用 turn 不必已经终结，因此输入和输出可以重叠。
- 回复关系不隐含任何 OpenEvent `recipients` 值。

由于每个回复目标必须已经存在，回复边始终指向同一 Channel 内更早的首次 append，不会形成环。

## 10. 应用职责

以下职责属于应用，而不是 `chat.v1`：

- 把 principal 映射为用户或 Agent；
- Channel visibility、成员变化和授权策略；
- recipients 和事件路由；
- 解释 `extensions` 和 ObjectKey；
- 发布对账、重试、去重和幂等；
- 决定如何处理非法 JSON、未知 kind、缺失引用、断链、分叉和终态后事件；
- 展示并发 turn 和中止状态。

协议没有 `event_id`。应用在发布结果不确定后重试时，必须自行对账 OpenEvent 历史，或者接受产生重复记录或
非法分叉的风险。

## 11. 安全与持久化

- Channel ACL 才是保密边界，recipients 不是。
- 新加入的 Channel 成员可能读取全部保留历史。
- 移除成员不能收回其已经获得的数据或 ObjectKey。
- ObjectKey 是 bearer capability，可以永久转交。
- 任何具备 Channel 写权限的 principal 都能中止任意已有 turn。
- Chat 事件是 append-only；协议不定义编辑、删除、脱敏或保留期操作。

应用不得把参与者不应读取的隐藏推理、凭据、Agent 私有状态或其它数据写入 payload、extensions 或附加对象。
