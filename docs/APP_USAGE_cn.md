# 浏览器聊天应用使用指南

[English version](APP_USAGE.md)

应用包含一个 Python 后端和随安装包提供的浏览器页面。需要单独运行 OpenEvent；模型和 Agent 服务由接入方提供。只使用 Python SDK 时，见 [SDK 快速开始](SDK_USAGE_cn.md)。

## 安装与验证

使用 Python 3.10 或更高版本，在当前环境安装 `openevent-sdk>=0.11.1`，然后在本仓库执行：

```sh
make check-sdk
make install
```

重复执行 `make install` 会把已安装的本项目更新为本次构建。使用虚拟环境时，通过 `PYTHON` 指定该环境的 Python 解释器。
安装目标由该解释器决定，`make install` 不接受 `--target`、`--prefix` 或 `--root`；`make verify-wheel` 的隔离验证不受影响。

构建和测试生成物位于 `build/`，安装包位于 `dist/`；`make clean` 清理这些文件。

开发验证使用 `make test`，其中页面状态测试需要 Node.js 22.12 或更高版本。`make verify-wheel` 重新构建并验证安装包包含 SDK、后端和静态页面。完整联调使用：

```sh
OPENEVENT_SERVER_BIN=/opt/openevent/bin/openevent_server make e2e
```

端到端测试会启动临时 OpenEvent 和 Chat Server，并使用临时数据目录。

安装 Playwright 和 Chromium 后，`make browser-e2e` 还会验证真实浏览器操作；可通过 `CHAT_CHROMIUM_BIN` 指定已有 Chromium 可执行文件。`make check-docs` 检查翻译结构与本地链接。

## 配置与启动

用户和 Agent 的不同非零 principal 由外部应用或部署方指定。Chat 模块只使用配置提供的 principal，不生成、分配或自行选择身份。
部署方为这些已指定的 principal 准备对应的 OpenEvent token；需要创建 token 时，通过 OpenEvent 管理接口调用 `AddToken`，传入已指定的 principal。
Agent token 只交给 Agent 服务。为浏览器另设一个独立访问口令。

创建 `chat.json`，并通过部署环境的访问控制保护其中的凭据：

```json
{
  "openevent_target": "127.0.0.1:9527",
  "channels_dir": "./channels",
  "web_token": "replace-with-browser-access-token",
  "user_principal": "9001",
  "user_openevent_token": "replace-with-user-openevent-token",
  "agent_principal": "9002",
  "rpc_timeout_ms": 2000.0,
  "max_retries": 3
}
```

相对 `channels_dir` 以配置文件所在目录为起点；目录不存在时自动创建。部署方应确保配置文件和目录只能由运行后端的身份读写，每个目录只能运行一个进程。修改配置后需要重启；配置文件和目标目录不能是符号链接。

```sh
openevent-chat --config ./chat.json --host 127.0.0.1 --port 8080
```

打开 `http://127.0.0.1:8080/api/chat/`，输入 `web_token`，然后新建会话。会话会自动创建对应的 private Channel，不需要手工写会话文件。浏览器访问口令、OpenEvent 用户凭据和 Agent 凭据是三种不同用途的秘密，不要互相替代。

生产环境通过 HTTPS 反向代理访问，并明确配置对外 origin，例如：

```sh
openevent-chat --config ./chat.json --host 127.0.0.1 --port 8080 --origin https://chat.example.com
```

代理需要转发 `/api/chat/` 和 `/static/`。浏览器在 HTTPS 下使用 Secure Cookie；本地 HTTP 仅用于开发。口令不会出现在 URL 中。“移除访问口令”会清除当前页面保存的聊天记录、草稿和 Cookie。

## 使用页面

- 新建、选择或切换会话。只有当前选中的会话自动更新消息；每个标签页独立工作。
  桌面和手机均可点击“刷新”更新会话列表或重试失败的列表请求，不会清空当前草稿。
- 会话初始化完成后才能输入、上传、发送和取消；期间可以继续加载历史，失败时显示原因并允许重试初始化。
- 输入文字，也可以添加文件。选中文件后立即上传，上传完成后才能发送；单个文件大小为 1 byte 至 4 MiB。
- 文件或文件信息不符合要求时，只拒绝该次上传，不使整个服务退出。
- 选择已有消息作为回复目标，可以选择多条。流式回复会在原位置持续追加；Agent 重试时可重置同一条回复，用新输出替换中断的内容。
- 正在生成的回复暂时没有文字或重置为空时，仍显示为正在输出，并允许取消。重置不改变回复位置和已有引用，回复预览随新内容更新。
- Agent 正在输出时可以点击取消。取消结果未确认时可手动重试，页面不会自动重发；实际停止工作需要 Agent 服务支持。
- 文件信息加载失败后可手动重试；文件通过下载打开，不在聊天页执行或预览。
- 普通请求最多等待 30 秒，会话准备、上传和下载最多等待 120 秒。消息同步和会话准备遇到临时网络错误或超时会自动重试；
  会话列表、新建会话、文件信息、上传、下载和取消需要手动重试。新建会话失败时使用页面的重试入口继续。下载完整完成后才交给浏览器保存，失败不保存半份文件。
- 点击发送后，本会话的文字、附件和回复选择立即锁定，结果确认前不能修改或另发一条消息。
  发送超时或仍在处理中时，页面会自动确认原消息的结果；超时不代表消息没有发送成功。结果未知时内容继续锁定，可按页面提示手动确认。
  确认成功后清空本次内容并恢复编辑；确认没有发送时，保留内容并恢复编辑，之后可用普通“发送”按钮重新发送。
  若后端明确返回“附件不可用”，即使前次请求超时，也会结束本次发送，保留内容并恢复编辑；重新上传附件后可再次发送。
  切走会话会暂停自动确认，切回来继续；如果页面已提示错误并停止自动确认，则按提示处理。

刷新页面会丢失草稿，已发送的聊天记录和文件仍保存在 OpenEvent 中。
重置只改变当前显示的文字和附件；旧事件及其文件仍保留在底层历史中，不是删除记录或撤回文件权限。

## Agent 接入

Agent 使用配置中的 `agent_principal` 和自己的凭据连接 OpenEvent，并按 [SDK 快速开始](SDK_USAGE_cn.md) 读取和写入 `chat.v1`。对应的 Channel ID 由部署方从会话配置中取得。本项目不提供模型调用、Agent 任务调度或发现服务。

流式重置由 Agent 通过 [Chat SDK](CHAT_SDK_cn.md) 发布，协议语义见 [turn.reset](CHAT_PROTOCOL_cn.md#15-turnreset)。
浏览器没有重置按钮，模型失败判断和重试由 Agent 服务负责。

## 重启与故障处理

不要编辑、删除或从外部添加会话文件，也不要更改对应的 Channel 配置。

允许重试的 OpenEvent 调用在耗尽 `max_retries` 后，会让整个 Chat Server 非零退出，包括消息和附件读取。
后端无法确认 OpenEvent 中的 Channel 创建或文件写入结果时，服务直接退出，不自动重复写入；消息发布失败和数据完整性错误也会使服务退出。
页面可能继续自动请求以等待服务恢复。重启前，运维必须确认旧进程已结束，并且旧 OpenEvent 请求均已结束、不可能迟到提交。

普通进程重启时，服务会自动续办信息完整的未完成会话创建。若错误提示 pending 记录缺少 Channel ID、记录损坏或冲突，须由运维调查处理后再启动。
自动续办以操作系统和文件系统仍正常工作为前提，本地配置不提供掉电保护。掉电或操作系统崩溃后，运维应先核对会话配置与 OpenEvent 中的 Channel，再恢复服务。

重启后，尚未发送的附件需要重新上传。页面保留本地文件时，可以直接点击“重新上传”；否则需要重新选择文件。已发送附件仍可正常读取。
