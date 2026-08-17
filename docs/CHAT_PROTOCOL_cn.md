# Chat 协议 chat.v1

[English version](CHAT_PROTOCOL.md)

> 状态：初始规格
> 适用范围：OpenEvent Channel `protocol="chat.v1"` 的事件 payload

## 1. 协议边界

`chat.v1` 是面向用户与 Agent 永久会话的底层事件协议。一个 OpenEvent Channel 表示一段永久会话，
可以包含多个用户 principal 和多个 Agent principal。

Channel 历史本身不说明哪些 principal 是用户或 Agent。角色映射属于协议之外的应用职责。协议只记录参与者可见的
互动事件；Agent 内部状态、隐藏推理、模型请求、工具内部状态、密钥和调试日志不属于本协议。

协议只定义四种事件：

- `turn.start`
- `turn.append`
- `turn.end`
- `turn.cancel`

其它事件 kind 不属于 `chat.v1`。应用可以通过可选 `extensions` 对象附加参与者可见的元数据，但扩展不能改变基础
turn 语义。

OpenEvent 不解析 JSON payload。本文定义事件是否符合协议，但不要求应用在遇到非法或冲突历史时必须拒绝、跳过、展示或停止处理。

## 2. Channel 与 OpenEvent 字段

所有 Chat Channel 必须设置：

```text
ChannelInfo.protocol = "chat.v1"
```

payload 不包含版本字段。`ChannelInfo.description` 没有 `chat.v1` schema，可以保存应用自定义文本。

协议不额外限制 Channel visibility 或成员关系，成员可以动态变化。每个事件的读写权限由 OpenEvent 当前 ACL 决定。

OpenEvent 顶层字段保持原生语义：

- `EventMessage.seq` 是权威全局事件顺序，也是 `pre_seq` 引用的事件位置；它不是 turn ID。
- `EventMessage.principal` 是发布者。`turn.start` 的发布者是 turn owner；append/end 的发布者必须是该 owner。
- `EventMessage.ts_ms` 是服务端接收时间。
- `EventMessage.recipients` 可以在遵循 OpenEvent 服务端发布校验规则的前提下自由承载应用语义。
  `chat.v1` 不要求它为空、在同一 turn 内保持不变或与回复关系一致。
- `EventMessage.object_keys` 可以非空，其含义由应用定义；基础协议不把它与某个 content part 关联。

OpenEvent recipients 是筛选字段，不是 ACL。需要重建完整会话的读取方应使用
`only_my_recipient=false` 读取 Channel。

## 3. JSON、TurnRef 与公共字段

所有 `chat.v1` payload 必须是 UTF-8 JSON object。

公共字段：

| 字段 | 规则 |
| --- | --- |
| `kind` | 必填 string，只能是 `turn.start`、`turn.append`、`turn.end` 或 `turn.cancel` |
| `turn_id` | `turn.start`、`turn.append`、`turn.end` 必填；`turn.cancel` 必须缺省 |
| `extensions` | 可选 JSON object，只由应用解释 |

`turn_id` 必须是非空 string，UTF-8 编码不超过 128 bytes。生产者和消费者使用精确字符串相等比较，不得裁剪、
改变大小写或做 Unicode 规范化。

一个 turn 在其 Channel 内使用下面的 TurnRef 唯一标识：

```json
{
  "principal": 9001,
  "turn_id": "turn-01JABC"
}
```

TurnRef 必须是只包含以下字段的 JSON object：

- `principal`：`turn.start` 的 OpenEvent 顶层 principal，使用 OpenEvent principal 的 `uint64` JSON integer 规则；
- `turn_id`：满足上述字符串规则。

TurnRef 使用 `(principal, turn_id)` 精确比较。Channel 已由事件所在位置确定，因此协议文字使用 TurnRef，完整存储索引是
`(channel_id, principal, turn_id)`。写入方必须保证自己在同一 Channel、同一 principal 下不会用相同 `turn_id`
创建两个 turn；不同 principal 可以使用相同 `turn_id`，两者是不同 turn。

引用 OpenEvent seq 的字段只包括 `pre_seq`，其值为范围 `1..18446744073709551615` 的 JSON integer，即不含 0 的
`uint64`。

未知 payload 顶层字段不属于 `chat.v1`。应用数据必须放在 `extensions` 中。扩展值可以是任意 JSON value，但必须仍是
参与者可见的互动元数据，不得重新定义 `kind`、`turn_id`、`pre_seq`、TurnRef、回复、内容或终态语义。

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

part 顺序有意义。应用按合法 turn 链顺序重建 turn 内容，并在每个事件内保持 part 列表顺序。
分片边界没有协议语义：应用可以追加字符、词、句子或更大的文本片段。

协议不定义二进制、图片、音频、工具调用或其它 content part。应用可以在承载事件的 OpenEvent 消息上附加
ObjectKey，但这不会引入新的 `chat.v1` content 类型。

## 5. `turn.start`

`turn.start` 是创建 turn 的唯一事件，也是该 turn 的第一条消息。写入方在发布前生成 `turn_id`，OpenEvent 顶层
principal 与 payload `turn_id` 共同组成该 turn 的 TurnRef。

```json
{
  "kind": "turn.start",
  "turn_id": "turn-user-1",
  "reply_to_turns": [
    {"principal": 9002, "turn_id": "turn-agent-1"}
  ],
  "content": [
    {"type": "text", "text": "你好"}
  ]
}
```

规则：

- `turn_id` 必须存在并满足第 3 节的字符串规则。
- 除可选 `extensions` 外，payload 必须且只能包含 `kind`、`turn_id`、`reply_to_turns` 和 `content`；因此不得包含
  `pre_seq`、`target_turn` 或 `status`。
- `reply_to_turns` 必须存在且为 JSON array，可以为空。
- 每一项必须是合法 TurnRef；数组内不得出现重复 TurnRef，也不得等于当前 turn 的 TurnRef。
- 每个被引用 turn 必须已经在同一 Channel 中出现更早的 `turn.start`；它可以仍在输出，也可以已经终结。
- `content` 必须满足第 4 节。
- OpenEvent 顶层 `principal` 成为 turn owner。
- 同一 Channel 中，第二个使用相同 owner principal 和 `turn_id` 的 `turn.start` 构成协议冲突。

`turn.start` 永久确定该 turn 的 TurnRef、owner 和 `reply_to_turns`。它没有 `pre_seq`，其 OpenEvent `seq` 是后续
append/end 链的起点。

## 6. `turn.append`

`turn.append` 向发布 principal 所拥有的已有 turn 追加内容。

```json
{
  "kind": "turn.append",
  "turn_id": "turn-user-1",
  "pre_seq": 123402,
  "content": [
    {"type": "text", "text": "，世界"}
  ]
}
```

规则：

- 目标 TurnRef 必须已经在同一 Channel 中出现 `turn.start`。
- `pre_seq` 必须存在且等于该 turn 前一个合法 `turn.start` 或 `turn.append` 的 OpenEvent `seq`。
- 除可选 `extensions` 外，payload 必须且只能包含 `kind`、`turn_id`、`pre_seq` 和 `content`；因此不得包含
  `reply_to_turns`、`target_turn` 或 `status`。
- OpenEvent 顶层 `principal` 必须等于 `turn.start` 确定的 owner。
- `content` 必须满足第 4 节。

## 7. `turn.end`

`turn.end` 用于正常完成发布 principal 所拥有的 turn。

```json
{
  "kind": "turn.end",
  "turn_id": "turn-user-1",
  "pre_seq": 123403,
  "status": "completed"
}
```

规则：

- 目标 TurnRef 必须已经在同一 Channel 中出现 `turn.start`。
- `pre_seq` 必须存在且等于该 turn 前一个合法 `turn.start` 或 `turn.append` 的 OpenEvent `seq`。
- `status` 必须固定为 `completed`。
- 除可选 `extensions` 外，payload 必须且只能包含 `kind`、`turn_id`、`pre_seq` 和 `status`；因此不得包含
  `content`、`reply_to_turns` 或 `target_turn`。
- 该事件只保存完成状态，不重复保存最终内容快照。

## 8. `turn.cancel`

`turn.cancel` 用于独立中止已有 turn。由于取消者可以不是 turn owner，payload 使用完整 `target_turn` TurnRef 指定目标。

```json
{
  "kind": "turn.cancel",
  "target_turn": {
    "principal": 9002,
    "turn_id": "turn-agent-1"
  }
}
```

规则：

- `target_turn` 必须存在且为合法 TurnRef，并且目标 turn 已经在同一 Channel 中出现 `turn.start`。
- 除可选 `extensions` 外，payload 必须且只能包含 `kind` 和 `target_turn`；因此不得包含顶层 `turn_id`、`pre_seq`、
  `status`、`content` 或 `reply_to_turns`。
- 任何被 OpenEvent 允许向该 Channel 发布消息的 principal 都可以发布，不要求它等于 `target_turn.principal`，也不要求
  协议知道它的用户或 Agent 角色。
- cancel 本身就是终态事件，不需要后续 `turn.end` 确认。

## 9. 链与终态语义

对于同一个 TurnRef，最早终态之前的 `turn.start`、合法 `turn.append` 与作为该最早终态的合法 `turn.end` 通过
`pre_seq` 形成单链：

- `turn.start` 是链头，没有前驱；其 OpenEvent `seq` 是第一个链尾，但不是 turn ID。
- 后续 append 或正常 end 指向当前 append 链尾。
- 在最早终态之前，最多只能有一个 append 或 end 使用同一个 `pre_seq` 作为前驱；多个后继构成分叉，违反协议。
- `turn.cancel` 通过 `target_turn` 关联 turn，不是链后继，永远不包含 `pre_seq`。

一个 turn 的终态事件是在该 TurnRef 的所有符合格式的 `turn.end` 和 `turn.cancel` 中，OpenEvent 全局 `seq` 最小的
事件。因此，并发完成与中止会得到所有读取方一致的确定结果。

事件按 OpenEvent `seq` 升序处理。一个 TurnRef 一旦进入终态，之后针对它的 append、end 或 cancel 都不能改变已经
确定的终态、最终内容或终态前链尾；这些事件即使已经提交，也不产生 turn 状态效果。协议不要求实现为终态后事件增加
特殊的解析、字段提取或验证顺序，应用也不需要判断这些事件相对于终态是否“合法”。只有最早终态 `seq` 之前的合法
start/append 参与最终内容重建。

不同 TurnRef 可以同时保持打开并并发追加，它们的事件可以在 OpenEvent 全局顺序中交错；每个 turn 的 `pre_seq`
链决定自己的局部内容顺序。

## 10. 回复关系

`reply_to_turns` 是 TurnRef 数组，表示因果上下文，不表示路由或授权。

- 它由 `turn.start` 固定，后续不能修改。
- 空数组表示没有协议层父 turn。
- 多个 TurnRef 允许一个 turn 同时回应多个更早的 turn。
- 被引用 turn 不必已经终结，因此输入和输出可以重叠。
- 回复关系不隐含任何 OpenEvent `recipients` 值。

由于每个回复目标必须已经存在，回复边始终指向同一 Channel 内全局 seq 更早的 `turn.start`，不会形成环。

## 11. 应用职责

以下职责属于应用，而不是 `chat.v1`：

- 为自己发布的 `turn.start` 生成 `turn_id`，并保证同一 Channel、同一 principal 下不重复；
- 把 principal 映射为用户或 Agent；
- Channel visibility、成员变化和授权策略；
- recipients 和事件路由；
- 解释 `extensions` 和 ObjectKey；
- 发布对账、重试、去重和幂等；
- 决定如何处理非法 JSON、未知 kind、缺失引用、重复 start、断链和分叉；
- 展示并发 turn 和中止状态；通过自己的 Fetch/Subscribe 观察生效的 `turn.cancel`，并负责停止对应的模型、工具
  或其它业务处理。Chat 协议和 SDK 只记录取消事实，不直接中止应用任务。

TurnRef 标识一个逻辑 turn，但协议没有标识单次 append/end/cancel 发布的 operation ID。应用在发布结果不确定后重试时，
必须自行对账 OpenEvent 历史，或者接受产生重复记录或非法分叉的风险。

## 12. 安全与持久化

- Channel ACL 才是保密边界，recipients 不是。
- 新加入的 Channel 成员可能读取全部保留历史。
- 移除成员不能收回其已经获得的数据或 ObjectKey。
- ObjectKey 是 bearer capability，可以永久转交。
- 任何具备 Channel 写权限的 principal 都能中止任意已有 TurnRef。
- Chat 事件是 append-only；协议不定义编辑、删除、脱敏或保留期操作。

应用不得把参与者不应读取的隐藏推理、凭据、Agent 私有状态或其它数据写入 payload、extensions 或附加对象。
