# Chat 协议 chat.v1

[English version](CHAT_PROTOCOL.md)

> 状态：公开协议规格
> 适用范围：OpenEvent Channel `protocol="chat.v1"` 的事件 payload

## 1. 协议边界

`chat.v1` 是面向用户与 Agent 永久会话的底层事件协议。一个 OpenEvent Channel 表示一段永久会话，
可以包含多个用户 principal 和多个 Agent principal。

Channel 历史本身不说明哪些 principal 是用户或 Agent。角色映射属于协议之外的应用职责。协议记录参与者可见的
互动事件和发送编号预留控制事件；Agent 内部状态、隐藏推理、模型请求、工具内部状态、密钥和调试日志不属于本协议。

协议定义六种 turn 事件和一种发送编号预留控制事件：

- `turn.single`
- `turn.start`
- `turn.append`
- `turn.reset`
- `turn.end`
- `turn.cancel`
- `submission.reserve`

其它事件 kind 不属于 `chat.v1`。应用可以通过可选 `extensions` 对象附加参与者可见的元数据，但扩展不能改变基础
turn 语义。

OpenEvent 不解析 JSON payload。本文定义事件是否符合协议，但不要求应用在遇到非法或冲突历史时必须拒绝、跳过、展示或停止处理。

本规格仍使用 `chat.v1` 名称，但不兼容旧版的创建事件回复引用、无内容清单的 end 或无创建位置的 cancel；旧字段 `reply_to_turns` 也不属于本规格。
不提供旧历史双读、自动转换、迁移或能力协商。部署中的 SDK、后端、浏览器和其它读写方必须同时采用本规格。
流式重置、结束内容清单和回复目标分别以第 8、9、12 节为准。

## 2. Channel 与 OpenEvent 字段

所有 Chat Channel 必须设置：

```text
ChannelInfo.protocol = "chat.v1"
```

payload 不包含版本字段。`ChannelInfo.description` 没有 `chat.v1` schema，可以保存应用自定义文本。

协议不额外限制 Channel visibility 或成员关系，成员可以动态变化。每个事件的读写权限由 OpenEvent 当前 ACL 决定。

OpenEvent 顶层字段保持原生语义：

- `EventMessage.seq` 是权威全局事件顺序，也是 `pre_seq` 和 `reply_to_seqs` 引用的事件位置；它不是 `turn_id`。
- `EventMessage.principal` 是发布者。`turn.single` 或 `turn.start` 的发布者是 turn owner；append/reset/end 的发布者必须是
  该 owner。
- `EventMessage.ts_ms` 是服务端接收时间。
- `EventMessage.uuid` 是 OpenEvent 为这一条已提交消息分配的非零 UUID；它不是 `turn_id`、TurnRef、创建事件 seq、
  `pre_seq`、回复引用或 payload 字段。UUID 的领取、消费、重复拒绝和结果不确定语义只由 OpenEvent API 契约定义。
- `EventMessage.recipients` 可以在遵循 OpenEvent 服务端发布校验规则的前提下自由承载应用语义。
  `chat.v1` 不要求它为空、在同一 turn 内保持不变或与回复关系一致。
- 除 `turn.end` 外，turn 事件的 `EventMessage.object_keys` 可以非空，对象内容和展示含义由应用定义；基础协议不把它与某个 content part 关联。
  reset 对当前附件集合的替换范围见第 8 节，不改变 ObjectKey 本身的访问或保留语义。
  `turn.end` 和 `submission.reserve` 的 `object_keys` 必须为空。

OpenEvent recipients 是筛选字段，不是 ACL。需要重建完整会话的读取方应使用
`only_my_recipient=false` 读取 Channel。

## 3. JSON、TurnRef 与公共字段

所有 `chat.v1` payload 必须是 UTF-8 JSON object。

公共字段：

| 字段 | 规则 |
| --- | --- |
| `kind` | 必填 string，只能是 `turn.single`、`turn.start`、`turn.append`、`turn.reset`、`turn.end`、`turn.cancel` 或 `submission.reserve` |
| `turn_id` | `turn.single`、`turn.start`、`turn.append`、`turn.reset`、`turn.end` 必填；`turn.cancel` 和 `submission.reserve` 必须缺省 |
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

- `principal`：创建该 turn 的 `turn.single` 或 `turn.start` 的 OpenEvent 顶层 principal，使用 OpenEvent principal 的
  `uint64` JSON integer 规则；
- `turn_id`：满足上述字符串规则。

TurnRef 使用 `(principal, turn_id)` 精确比较。Channel 已由事件所在位置确定，因此协议文字使用 TurnRef，完整存储索引是
`(channel_id, principal, turn_id)`。写入方必须保证自己在同一 Channel、同一 principal 下不会用相同 `turn_id`
创建两个 turn；不同 principal 可以使用相同 `turn_id`，两者是不同 turn。

payload 中引用 OpenEvent seq 的位置包括 `start_seq`、`pre_seq`、`content_seqs` 和 `reply_to_seqs` 的每个数组元素。它们都使用范围
`1..18446744073709551615` 的 JSON integer，即不含 0 的 `uint64`，不能使用字符串、布尔值或小数。
`pre_seq` 指向流式 turn 的前一个链节点；`reply_to_seqs` 的目标和回复语义统一见第 12 节。

未知 payload 顶层字段不属于 `chat.v1`。应用数据必须放在 `extensions` 中。扩展值可以是任意 JSON value，但必须仍是
参与者可见的元数据，不得重新定义 `kind`、`turn_id`、`start_seq`、`pre_seq`、`content_seqs`、`error`、`reply_to_seqs`、`reserved_through`、TurnRef、回复、内容、终态或编号预留语义。
基础读取者只需确认扩展是合法 JSON object，不需要把其中的值转换成所在语言的数值或对象；转发时应保留扩展数据，具体解释由应用负责。

payload 大小上限由 OpenEvent 部署控制，`chat.v1` 不定义第二套字节上限。

## 4. Content Part

首版只支持文本 part：

```json
{
  "type": "text",
  "text": "你好"
}
```

`content` 必须是 JSON array。各事件的空数组条件统一如下：

| 事件 | 空数组条件 |
| --- | --- |
| `turn.single` | 可以为空，以支持由应用定义的纯对象附件 turn。 |
| `turn.start` | 可以为空，顶层 `object_keys` 也可以同时为空；仍创建可被取消的流式 turn，不表示完成。 |
| `turn.append` | 仅在本事件顶层 `object_keys` 非空时可以为空，用于只追加附件；`content` 与 `object_keys` 同时为空违反协议。 |
| `turn.reset` | 可以为空；清空与替换的含义见第 8 节。 |

数组中的每一项必须是只包含以下字段的 object：

- `type`：固定 string `text`；
- `text`：非空 string。

part 顺序有意义。应用对 `turn.single` 直接使用该事件的 part 顺序；对流式 turn 按合法 turn 链顺序重建内容，并在
每个事件内保持 part 列表顺序；遇到 reset 时按第 8 节替换此前聚合的内容。
分片边界没有协议语义：应用可以追加字符、词、句子或更大的文本片段。

协议不定义二进制、图片、音频、工具调用或其它 content part。应用可以在承载事件的 OpenEvent 消息上附加
ObjectKey，但这不会引入新的 `chat.v1` content 类型。

## 5. `turn.single`

`turn.single` 用一条事件创建并正常完成一个不可追加的 turn，适合用户一次性提交完整输入。写入方在发布前生成
`turn_id`，OpenEvent 顶层 principal 与 payload `turn_id` 共同组成该 turn 的 TurnRef。

```json
{
  "kind": "turn.single",
  "turn_id": "turn-user-1",
  "reply_to_seqs": [123400],
  "content": [
    {"type": "text", "text": "你好"}
  ]
}
```

规则：

- `turn_id` 必须存在并满足第 3 节的字符串规则。
- 除可选 `extensions` 外，payload 必须且只能包含 `kind`、`turn_id`、`reply_to_seqs` 和 `content`；因此不得包含
  `pre_seq` 或 `target_turn`。
- `reply_to_seqs` 必须满足第 12 节。
- `content` 必须满足第 4 节。
- OpenEvent 顶层 `principal` 成为 turn owner。
- 同一 Channel 中，同一 owner principal 和 `turn_id` 只能出现一个创建事件；更早已有 `turn.single` 或 `turn.start` 时，
  再出现任一种创建事件都构成协议冲突。

`turn.single` 永久确定 TurnRef、owner、`reply_to_seqs`、完整内容和 completed 终态。它没有 `pre_seq`；其
OpenEvent `seq` 同时是该 turn 的 `start_seq` 和 `terminal_seq`。后续不需要也不能通过 `turn.end` 再完成一次。

## 6. `turn.start`

`turn.start` 创建一个允许后续 append 和 reset 的流式 turn，也是该 turn 的第一条消息。写入方在发布前生成 `turn_id`，
OpenEvent 顶层 principal 与 payload `turn_id` 共同组成该 turn 的 TurnRef。

```json
{
  "kind": "turn.start",
  "turn_id": "turn-user-1",
  "reply_to_seqs": [123400],
  "content": [
    {"type": "text", "text": "你好"}
  ]
}
```

规则：

- `turn_id` 必须存在并满足第 3 节的字符串规则。
- 除可选 `extensions` 外，payload 必须且只能包含 `kind`、`turn_id`、`reply_to_seqs` 和 `content`；因此不得包含
  `pre_seq` 或 `target_turn`。
- `reply_to_seqs` 必须满足第 12 节。
- `content` 必须满足第 4 节。
- OpenEvent 顶层 `principal` 成为 turn owner。
- 同一 Channel 中，同一 owner principal 和 `turn_id` 只能出现一个创建事件；更早已有 `turn.single` 或 `turn.start` 时，
  再出现任一种创建事件都构成协议冲突。

`turn.start` 永久确定该 turn 的 TurnRef、owner 和 `reply_to_seqs`。它没有 `pre_seq`，其 OpenEvent `seq` 是该 turn 的
`start_seq`，也是后续 append/reset/end 链的起点。

## 7. `turn.append`

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

- 要产生 turn 状态效果，目标 TurnRef 必须由同一 Channel 中更早的 `turn.start` 创建并且尚未终结。`turn.single`
  创建的 turn 已经终结，后续 append 不产生 turn 状态效果。
- 对尚未终结的流式 turn，`pre_seq` 必须存在且等于该 turn 当前合法链尾（`turn.start`、`turn.append` 或 `turn.reset`）的 OpenEvent `seq`。
- 除可选 `extensions` 外，payload 必须且只能包含 `kind`、`turn_id`、`pre_seq` 和 `content`；因此不得包含
  `reply_to_seqs` 或 `target_turn`。
- 对产生状态效果的流式 turn，OpenEvent 顶层 `principal` 必须等于 `turn.start` 确定的 owner。
- `content` 必须满足第 4 节。

## 8. `turn.reset`

`turn.reset` 替换发布 principal 所拥有的、尚未终态的流式 turn 的当前文本和附件集合，并保持同一个 turn 继续输出。
例如应用可以在模型流式调用中断后重试，用重试内容覆盖当前回复，何时重试和采用哪些模型结果仍由应用决定。

```json
{
  "kind": "turn.reset",
  "turn_id": "turn-agent-1",
  "pre_seq": 123403,
  "content": []
}
```

字段与链规则：

- 除可选 `extensions` 外，payload 必须且只能包含 `kind`、`turn_id`、`pre_seq` 和 `content`；不得包含
  `reply_to_seqs` 或 `target_turn`。`turn_id` 和 `pre_seq` 满足第 3 节，`content` 满足第 4 节。
- 要产生状态效果，目标 TurnRef 必须由同一 Channel 中更早的 `turn.start` 创建并且尚未终态；OpenEvent 顶层
  `principal` 必须等于该 owner。reset 不创建 turn，不能修改其它 owner 的 turn。
- `pre_seq` 必须等于该 turn 当前合法链尾，即最近的合法 `turn.start`、`turn.append` 或 `turn.reset` 的 seq。
  生效后 reset 自身的 seq 成为新链尾，后续 append、reset 或 end 连接这个新链尾。
- reset 不绕过旧写入。写入方必须先确定旧链写入的结果，且不会再有针对旧链尾的未决发布，才能在正确链尾上提交 reset。
  reset 与旧 append 使用同一前驱会构成第 11 节禁止的分叉；仅仅没有查到旧消息，不代表可以忽略它仍可能提交的请求。
- reset 不是终态。可以连续重置，也可以在 reset 后直接 end；不要求重置后一定出现 append。
  已 completed 或 cancelled 的 turn（包括 single turn）不会因 reset 重新打开；终态后的处理统一遵守第 11 节。

当前内容的替换规则：

- 按 seq 处理一个生效的 reset 时，丢弃此前为该 turn 聚合的文本，以 reset 的 `content` 作为新的文本 part 列表。
  空数组表示清空文本；非空数组表示用这些 part 立即替换。后续合法 append 追加到这个新列表。
- 同时清空此前为该 turn 聚合的附件集合，以 reset 所在 `EventMessage.object_keys` 作为新附件集合的起点。
  该列表为空表示清空附件；可以在新列表中再次引用仍需保留的 ObjectKey。后续事件的对象内容和展示继续由应用解释，
  但不得把 reset 之前的事件附件重新作为当前集合的一部分。`object_keys` 始终位于 OpenEvent 顶层，不新增同名 payload 字段。
- `content` 和顶层 `object_keys` 可以同时为空。此时 turn 仍存在且仍为流式状态，只是当前没有文本或附件。
- reset 不改变 owner、TurnRef、`start_seq` 或创建时的 `reply_to_seqs`，也不改变按创建位置排序的结果。
  reset seq 不是回复目标；完成后的回复目标由第 12 节定义。
- 旧 start、append、reset 及其对象引用仍留在不可变历史中；reset 不删除对象、不撤销访问权限，也不撤销任何已经发生的应用副作用。
  无论实时读取还是从头重放，同一事件前缀必须得到相同的当前文本、附件边界、链尾和终态。

例如以下事件都属于同一个 TurnRef：

| seq | 事件 | `pre_seq` | 当前文本 | 当前状态 |
| --- | --- | --- | --- | --- |
| 100 | start，content 为“旧” | 无 | 旧 | 流式 |
| 101 | append，content 为“回答” | 100 | 旧回答 | 流式 |
| 105 | reset，content 为“新” | 101 | 新 | 流式 |
| 106 | append，content 为“回答” | 105 | 新回答 | 流式 |
| 108 | end | 106 | 新回答 | completed |

如果 cancel 在 seq 104 已经生效，表中的 reset 和后续事件均不改变 turn，最终保留“旧回答”和 cancelled 状态。

## 9. `turn.end`

`turn.end` 完成发布 principal 所拥有的流式 turn。正常结束记录有效内容事件的位置，不重复正文或附件：

```json
{
  "kind": "turn.end",
  "turn_id": "turn-user-1",
  "start_seq": 123401,
  "pre_seq": 123403,
  "content_seqs": [123401, 123402, 123403]
}
```

规则：

- 除可选 `extensions` 外，公共必填字段为 `kind`、`turn_id`、`start_seq` 和 `pre_seq`；此外必须且只能有
  `content_seqs` 或 `error` 其中一个字段。不得包含 `content`、`reply_to_seqs` 或 `target_turn`，顶层 `object_keys` 必须为空。
- 要产生状态效果，目标必须是同一 Channel 中更早创建且尚未终态的流式 turn，发布 principal 必须是 owner。
  `start_seq` 必须等于其 `turn.start.seq`；`pre_seq` 必须等于当前有效的 start、append 或 reset 链尾。
- 正常结束的 `content_seqs` 必须是非空、严格升序的非零 uint64 JSON integer 数组。每项满足
  `start_seq <= seq <= pre_seq`，最后一项必须等于 `pre_seq`。
- 该列表必须准确列出当前内容的所有有效事件：没有生效 reset 时为 start 及其后全部有效 append；
  有 reset 时为最后一次生效 reset 及其后全部有效 append。被 reset 覆盖的事件不列入；内容和附件都为空的 start 或 reset 仍必须列入。
  列表中的事件必须属于该 TurnRef，按有效链相连，不能遗漏或加入其它事件。首项是 start 时等于 `start_seq`；否则必须是该 turn 的有效 reset。
- 列表没有另设数量上限，整个 payload 仍受 OpenEvent 大小限制。SDK 对结束清单过大的处理见
  [SDK 写入对象契约](CHAT_SDK_cn.md#51-独立写入对象)。

无法提交过大的正常结束清单时，可以使用小型错误结束：

```json
{
  "kind": "turn.end",
  "turn_id": "turn-user-1",
  "start_seq": 123401,
  "pre_seq": 123403,
  "error": "turn_too_long"
}
```

`error` 目前只允许字符串 `turn_too_long`，不得同时包含 `content_seqs`。它同样进入 completed，仍可被回复；
最终正文与附件为空，读取方不得继续把已累积的部分内容当作该 turn 的最终内容。历史事件和对象不删除。
错误结束与正常结束遵循相同的链尾、owner 和最早终态规则。

## 10. `turn.cancel`

`turn.cancel` 独立中止已有 turn，不需要 owner 再发 end。取消者可以不是 owner，因此同时携带完整目标身份和创建位置：

```json
{
  "kind": "turn.cancel",
  "target_turn": {
    "principal": 9002,
    "turn_id": "turn-agent-1"
  },
  "start_seq": 123401
}
```

- 除可选 `extensions` 外，payload 必须且只能包含 `kind`、`target_turn` 和 `start_seq`。
  不含 `pre_seq`、`content`、`content_seqs`、`error` 或 `reply_to_seqs`。
- `target_turn` 必须是合法 TurnRef，`start_seq` 必须等于该目标在同一 Channel 中更早的 single/start 创建事件 seq。
- 任何获准向 Channel 发布的 principal 都可以取消，不要求等于目标 owner，也不依赖用户或 Agent 角色。
- cancel 不携带有效内容清单；它即时参与第 11 节的最早终态判定。生效时保留此前有效内容，并纳入 cancel 自身的顶层附件。
  对已经终态的目标，cancel 不产生状态效果。

## 11. 链与终态语义

`turn.single` 自身同时是创建事件和 completed 终态。流式 turn 的 start、有效 append/reset 与有效 end 通过 `pre_seq` 形成单链：

- start 是链头，没有前驱，其 seq 是第一个链尾。
- 后续 append、reset 或 end 指向当前链尾；最早终态之前，同一前驱不能有多个后继。
- cancel 通过 `target_turn` 和 `start_seq` 关联目标，不是链后继，不含 `pre_seq`。

流式 turn 的有效终态是该 TurnRef 的 end/cancel 中 OpenEvent 全局 seq 最小的终态事件；有效 end 为 completed，
有效 cancel 为 cancelled。两者并发时，所有读取方依据同一顺序得到同一结果。`terminal_seq` 指这个有效终态事件的位置，
不等同于创建位置或最后一条内容事件的位置；它是派生值，不新增同名 payload 字段。

按 seq 升序处理。一旦终态，后续 append/reset/end/cancel 即使提交，也不能改变终态、最终内容、附件或终态前链尾；
不要求实现对这些迟到事件额外规定解析或验证顺序，也不需要根据终态判断其动态合法性。
终态前的有效 start/append/reset 按第 8 节聚合内容；正常 end 的清单必须与该聚合一致，错误 end 按第 9 节清空最终正文和附件。
cancel 保留取消时的聚合及自身附件。single 的最终内容固定为其自身内容和附件。

不同 turn 可以并发，其事件可以在全局序列中交错；各自的链决定局部内容顺序。

## 12. 回复关系

`reply_to_seqs` 引用被回复 turn 的有效正常结束位置，表达因果上下文，不表达路由或授权：

- 只有 single/start 必须包含这个 JSON array；可以为空。元素为第 3 节的非零 uint64 JSON integer，不得重复，
  保留写入方顺序，不要求按 seq 排序，也不赋予列表顺序额外优先级。
- 每项必须是同一 Channel 中更早已提交、且实际成为 completed 终态的 single 或 end 的 seq。
  不得引用 start、append、reset、cancel、编号预留、其它 Channel 的消息、当前事件或未提交事件。
  早于 end 已生效的 cancel 会使该 end 失去回复目标资格。
- cancelled turn 不可回复；带 `error="turn_too_long"` 的有效 end 仍是 completed，可以回复。
- 引用的是整个已完成 turn。创建事件固定其原始回复列表，后续不变；回复关系不推导 OpenEvent recipients。
- `start_seq` 是 single/start 的事件位置，用于定位创建信息；`terminal_seq` 才是回复目标。single 的两者相等，
  流式 turn 的两者不同。TurnRef 仍标识 turn，UUID 不用作回复引用。

写入方提供满足这些条件的终态位置。协议不要求 SDK 为发布回复额外读取历史；普通历史读取可以建立正常终态位置到 turn 的映射。
需要按目标终态读取内容时，使用 [SDK 定点读取契约](CHAT_SDK_cn.md#33-按终态位置读取内容)；这不改变回复的合法性前提。

例如 A 的 start 为 100、append 为 101、有效 end 为 102，B 的 single 为 110；新 turn C 在 111 可以引用
`[102, 110]`，不能引用 100 或 101。若 A 在 102 被取消，则不能回复 A。每条回复边都指向当前创建事件之前的正常终态，
因此因果图不会形成环。

## 13. `submission.reserve`

`submission.reserve` 在当前 Channel 中持久记录发送编号的预留上限，让应用可以一次预留一批 `submission_id`，
随后从内存向发送方发放编号。编号空间按 Channel 隔离；不同 Channel 可以使用相同编号。编号不是 OpenEvent `seq` 或 UUID，
预留事件自身的 `seq` 也不表示任何一条用户消息已经提交。

```json
{
  "kind": "submission.reserve",
  "reserved_through": 10000
}
```

规则：

- 除可选 `extensions` 外，payload 必须且只能包含 `kind` 和 `reserved_through`，不得包含 `turn_id`、`content`、
  `reply_to_seqs`、`pre_seq` 或 `target_turn`。
- `reserved_through` 是包含端点的编号预留上限，必须是范围 `1..18446744073709551615` 的 JSON integer，
  不能使用字符串、布尔值或小数。它不是对 OpenEvent 事件位置的引用。
- OpenEvent 顶层 `object_keys` 必须为空；principal、recipients、ACL 和 UUID 沿用第 2 节的通用规则。
- 该事件不创建或修改 turn，不进入 `pre_seq` 链，不产生内容或终态，也不能作为回复目标。读取方必须把它识别为合法控制事件，
  在正常历史读取中处理其消息位置；不应把它当成聊天内容或未知 kind。

使用此能力的应用负责为每个 Channel 指定同一时刻唯一的编号分配者，并保证每个编号只发放一次。这只约束编号分配，
不限制该 Channel 中参与聊天的用户、Agent 或 turn 写入者数量。基础协议不提供分配者选举、部署仲裁或 HTTP 请求去重。

分配者先提交覆盖拟发放编号的预留事件，确认 OpenEvent 提交成功后才能发放这些编号。恢复时，以该 Channel 已提交的
预留记录中最大的 `reserved_through` 为已预留上限，新编号必须大于这个上限；没有预留记录时可从 `1` 开始。
未发放、未使用或发送失败的编号可以留下空号，不得重新发放；达到 `uint64` 上限后不得回绕。

预留记录的含义是“这些编号已经被保留”，不是“这一批中的消息都已经写入”。超时的旧预留请求可能稍后才提交，
因此历史允许相同或更小的预留上限；按 `seq` 读取时，不要求 `reserved_through` 严格递增，也不把重叠的预留声明视为重复发号。
恢复取最大值，不能只取最后一条记录。编号分配顺序也不决定用户消息的提交顺序。

如何把编号映射为 `turn_id`、前端每次领取多少、旧批编号是否过期、结果查询保留多久，以及换批前如何等待正在发布的消息，
均由应用定义。它们不改变本节的预留事件字段或 turn 状态机。

## 14. 应用职责

以下职责属于应用，而不是 `chat.v1`：

- 为自己发布的 `turn.single` 和 `turn.start` 生成 `turn_id`，并保证同一 Channel、同一 principal 下不重复；
- 使用第 13 节的编号预留能力时，指定每个 Channel 的编号分配者，决定编号到 `turn_id` 的映射和批量领取、有效期、查询策略；
- 按第 12 节提供合法的 `reply_to_seqs`；
- 把 principal 映射为用户或 Agent；
- Channel visibility、成员变化和授权策略；
- recipients 和事件路由；
- 解释 `extensions` 和 ObjectKey；
- 决定何时重置尚未终态的流式 turn，以及重试、进度提示和历史审计的展示策略；reset 不直接重试模型或撤销工具执行；
- 决定 Chat 发布调用返回错误后的应用行为。直接使用 OpenEvent 发布的生产者遵守 OpenEvent UUID 契约；
  Chat Python SDK 在内部领取并传递 UUID；
- OpenEvent 已消费消息 UUID 拒绝之外的业务级幂等和去重；
- 决定如何处理非法 JSON、未知 kind、缺失引用、重复 single/start 创建、断链和分叉；
- 展示并发 turn 和中止状态；应用按 seq 读取事件，使用 Chat SDK 时自行调度 `fetch_page()`，观察生效的 `turn.cancel`，
  并负责停止对应的模型、工具或其它业务处理。Chat 协议和 SDK 只记录取消事实，不直接中止应用任务。

TurnRef 标识一个逻辑 turn。OpenEvent UUID 只标识一条底层消息：重复 UUID 会被拒绝，且服务端不比较 payload、Channel、
principal 或其它字段。它不创建 Chat operation ID，也不提供业务级幂等。重建 turn 时，协议不扫描或校验 UUID 是否已消费。

## 15. 安全与持久化

- Channel ACL 才是保密边界，recipients 不是。
- 新加入的 Channel 成员可能读取全部保留历史。
- 移除成员不能收回其已经获得的数据或 ObjectKey。
- ObjectKey 是 bearer capability，可以永久转交。
- 任何具备 Channel 写权限的 principal 都能中止任意已有 TurnRef。
- Chat 事件是 append-only；reset 只改变未终态 turn 的当前聚合，不改写或删除旧事件，也不撤销旧 ObjectKey。
  协议不定义历史编辑、删除、脱敏或保留期操作。

应用不得把参与者不应读取的隐藏推理、凭据、Agent 私有状态或其它数据写入 payload、extensions 或附加对象。
