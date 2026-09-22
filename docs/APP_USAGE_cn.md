# 浏览器聊天应用使用指南

[English version](APP_USAGE.md)

应用包含一个 Python 后端和随安装包提供的浏览器页面。需要单独运行 OpenEvent；模型和 Agent 服务由接入方提供。只使用 Python SDK 时，见 [SDK 快速开始](SDK_USAGE_cn.md)。

## 安装与验证

使用 Python 3.10 或更高版本，在当前环境安装 `openevent-sdk>=0.8.1`，然后在本仓库执行：

```sh
make check-sdk
make install
```

重复执行 `make install` 会把已安装的本项目更新为本次构建。使用虚拟环境时，通过 `PYTHON` 指定该环境的 Python 解释器。
安装目标由该解释器决定，`make install` 不接受 `--target`、`--prefix` 或 `--root`；`make verify-wheel` 的隔离验证不受影响。

通过 `make` 构建和验证时，源码副本、包信息、缓存及临时文件都放在 `build/`，最终安装包放在 `dist/`。`make clean` 清理这些生成物。

开发验证使用 `make test`，其中页面状态测试需要 Node.js 22.12 或更高版本。`make verify-wheel` 重新构建并验证安装包包含 SDK、后端和静态页面。完整联调使用：

```sh
OPENEVENT_SERVER_BIN=/opt/openevent/bin/openevent_server make e2e
```

端到端测试会启动临时 OpenEvent 和 Chat Server，使用临时数据目录；只依赖当前 Python 环境已安装的 OpenEvent SDK，不从子模块安装或生成 SDK。

安装 Playwright 和 Chromium 后，`make browser-e2e` 还会验证真实浏览器操作；可通过 `CHAT_CHROMIUM_BIN` 指定已有 Chromium 可执行文件。`make check-docs` 检查翻译结构与本地链接。

## 配置与启动

先通过 OpenEvent 管理接口为用户和 Agent 分配不同的非零 principal，并取得各自的 OpenEvent token。Agent token 只交给 Agent 服务。为浏览器另设一个独立访问口令。

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

相对 `channels_dir` 以配置文件所在目录为起点；目录不存在时自动创建。部署方应确保配置文件和目录只能由运行后端的身份读写，后端不按操作系统权限位检查。后端通过跨平台文件锁保持独占，每个目录只能运行一个进程。配置在启动时读取，不热加载；配置文件和目标目录不能是符号链接。

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

- 新建、选择或切换会话。只有当前选中的会话定时拉取消息，其他会话保留本页缓存；每个标签页独立工作。
- 输入文字，也可以添加文件。选中文件后立即上传，上传完成后才能发送；单个文件大小为 1 byte 至 4 MiB。
- 选择已有消息作为回复目标，可以选择多条。流式回复会在原位置持续追加。
- Agent 正在输出时可以发送取消事件。取消结果未确认时可手动重试，页面不会自动重发。Agent 服务需自行读取该事件并停止实际工作。
- 文件信息由同一会话的消息共用。加载失败后可手动重试；文件通过下载打开，不在聊天页执行或预览。
- 发送结果暂时不明确时，页面查询该编号或读取历史确认。不要把网络超时理解成消息没有写入。

页面正式记录只来自历史拉取。刷新页面会丢失草稿及内存缓存，已提交的聊天记录和文件仍保存在 OpenEvent 中。

## Agent 接入

Agent 使用配置中的 `agent_principal` 和自己的凭据连接 OpenEvent，并按 [SDK 快速开始](SDK_USAGE_cn.md) 读取和写入 `chat.v1`。对应的 Channel ID 由部署方从会话配置中取得；浏览器不接触原始身份或 Channel ID。本项目不提供模型调用、Agent 任务调度或发现服务。

## 重启与故障处理

会话目录保存不可变的会话配置；不要编辑、删除或从外部添加会话文件。服务运行期间不定期检查这些文件或 Channel 元数据，部署方负责保持它们不变。

发布失败或数据完整性错误会让整个 Chat Server 非零退出。重启前，运维必须确认旧进程已结束，并且旧 OpenEvent 请求均已结束、不可能迟到提交。

`.pending/` 中与已提交配置完全匹配的事务会在启动时自动清理。没有正式配置的创建事务需要运维根据错误检查处理，服务不会盲目重新创建 Channel。

本地配置不提供掉电保护。创建请求的去重和恢复适用于进程退出且操作系统、文件系统仍正常工作的情况；掉电或操作系统崩溃后，本地配置或事务记录可能丢失，运维应先核对会话配置与 OpenEvent 中的 Channel，再恢复服务。这不改变 OpenEvent 自身的数据保证。

重启会丢失尚未发送附件的内存凭据。页面保留本地文件时，可以点击“重新上传”；已发送附件仍可从历史正常读取。发送编号从 OpenEvent 中历史最大预留上限之后重新分配，不依赖本地编号文件。
