# OpenEvent Chat 模块

[English version](README.md)

OpenEvent Chat 定义面向用户与 Agent 永久会话的精简事件协议 `chat.v1`。一个 Channel 以 append-only
方式保存 turn 事件和发送编号预留控制事件。文本可以在模型生成过程中持续追加，尚未结束的流式内容可以重置后重新输出，
不同 turn 也可以安全交错。

> Chat SDK、Chat Server、浏览器和 Agent 必须全部支持当前 `chat.v1`，包括流式重置、空内容 start 和纯附件 append；不兼容旧版读取规则。

项目包含用于 `chat.v1` 写入、一次一页的 Fetch 读取和无状态消息解析的 Python SDK。
流式消息通过调用方持有的写入对象追加、重置和结束；续写旧消息时显式恢复写入对象。SDK 创建时不自动恢复历史；
需要持续读取时，调用方自行循环 Fetch 并保存读取位置。
项目还提供一个聊天后端和浏览器页面，支持会话、文件和主动消息拉取；不包含 Agent runtime 或模型集成。

## 协议

使用本协议的 Channel 必须设置：

```text
ChannelInfo.protocol = "chat.v1"
```

协议定义六种 turn 事件和一种发送编号预留控制事件：

- `turn.single`：用一条事件创建并正常完成 turn，携带完整文本内容和回复引用。
- `turn.start`：使用写入方生成的 `turn_id` 创建流式 turn，可以先建立再输出内容；turn 由 owner principal 与
  `turn_id` 共同标识。
- `turn.append`：向已有 turn 追加文字或附件，内容约束见[协议第 4 节](docs/CHAT_PROTOCOL_cn.md#4-content-part)。
- `turn.reset`：替换仍在输出的 turn 的当前可见内容，保留同一条回复及其引用；完整规则见
  [协议第 15 节](docs/CHAT_PROTOCOL_cn.md#15-turnreset)。
- `turn.end`：正常完成 turn。
- `turn.cancel`：独立中止已有 turn。
- `submission.reserve`：记录当前 Channel 的发送编号预留上限，不创建 turn；完整规则见
  [协议第 14 节](docs/CHAT_PROTOCOL_cn.md#14-submissionreserve)。

回复通过 `reply_to_seqs` 引用目标 turn 的创建事件 seq，具体语义见
[协议第 11 节](docs/CHAT_PROTOCOL_cn.md#11-回复关系)。

`turn.cancel` 只记录取消事实。使用 Chat SDK 时，应用通过 `fetch_page()` 读取生效的取消事件，并自行停止对应的模型、工具或
其它业务处理；Chat 协议不直接中止应用任务。

turn 的终态不会被后续事件改变，完整规则见[协议第 10 节](docs/CHAT_PROTOCOL_cn.md#10-链与终态语义)。

完整公开规格见 [docs/CHAT_PROTOCOL_cn.md](docs/CHAT_PROTOCOL_cn.md)。

## Python SDK

可安装的 Python SDK 位于 `src/openevent/chat_sdk`。公开读取与写入 API、状态、重试和生命周期契约以
[docs/CHAT_SDK_cn.md](docs/CHAT_SDK_cn.md) 为准；入门调用见
[docs/SDK_USAGE_cn.md](docs/SDK_USAGE_cn.md)。

## 浏览器应用

后端与静态页面随同一个安装包提供。执行 `make install` 后，用 `openevent-chat --config ./chat.json` 启动。
配置、使用和重启说明见 [应用使用指南](docs/APP_USAGE_cn.md)。

## 边界

`chat.v1` 有意不定义：

- principal 是用户还是 Agent；
- Channel visibility 或成员管理策略；
- OpenEvent `recipients` 的应用语义；
- Agent 内部状态、模型请求、工具、隐藏推理或调度过程；
- 业务级幂等、重试或非法历史处理策略。

应用可以在 turn 事件上附加 OpenEvent ObjectKey，也可以在协议的 `extensions` 对象中保存用户可见的应用元数据。
基础协议只支持文本 content part，不允许增加上述七种之外的事件 kind。

## 运行前提

- 支持 Channel、事件历史和 Fetch 的 OpenEvent server。
- Python SDK 需要当前环境已安装 `openevent-sdk>=0.11.1`；当前参考 SDK 为 `0.11.1`。
- 支持消息 UUID 的 OpenEvent server，具体以 OpenEvent API 文档为准。
- 直接使用 SDK 时，需提前创建 `protocol="chat.v1"` 的非系统 Channel；浏览器应用会自动创建。

构建或测试前运行 `make check-sdk`，确认当前环境已安装的 SDK 可用。`make e2e`
还要求 `OPENEVENT_SERVER_BIN` 指向可执行的 OpenEvent server；它只使用已安装的
SDK，不会从源码安装 SDK。

OpenEvent 负责保存事件并执行自身的 Channel ACL，不解析或校验 `chat.v1` JSON。
