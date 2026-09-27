# BPS 代理（ghcp_proxy_fzy）

一个运行在本机的 **ChatGPT / BPS 代理**，主要用于将 Codex 的 Responses 请求转发到已登录账号可访问的 BPS 后端，并提供中文网页控制台、多凭证管理、模型路由和 Windows 托盘管理器。

> **先看这里：当前分支仅启用 BPS 上游。** 虽然仓库保留了 GHCP Proxy 的命名和部分历史兼容代码，但 GitHub Copilot 登录、凭证读取和 SDK 已禁用，不会在 BPS 失败时回退到 Copilot。原 Chat Completions / Anthropic Messages 入口返回 `501`，不能将本项目当作完整的 OpenAI 或 Anthropic API 替代品。
>
> BPS 兼容功能仍具有实验性质。登录成功不代表账号拥有 BPS 或某个模型的访问权限；本项目不会创建订阅权益，也不保证避免上游风控、403 或账号限制。请只使用自己有权使用的账号和服务，并遵守服务提供方及所在组织的规定。

## 目录

- [功能与兼容范围](#功能与兼容范围)
- [安装](#安装)
- [首次登录与验证](#首次登录与验证)
- [连接 Codex](#连接-codex)
- [日常启动与停止](#日常启动与停止)
- [控制台与设置](#控制台与设置)
- [升级](#升级)
- [数据保存与备份](#数据保存与备份)
- [接口与高级配置](#接口与高级配置)
- [常见问题](#常见问题)
- [开发与测试](#开发与测试)

## 功能与兼容范围

- **Responses 代理**：支持模型发现、Responses 请求和兼容压缩接口，保留流式输出、客户端工具调用与工具结果回放。
- **登录凭证**：支持独立 ChatGPT OAuth 登录，或读取已有的 Excel 插件会话；可添加、验证、启停、重命名和删除凭证。
- **多账号调度**：最多管理 32 个凭证；新会话轮询可用凭证，同一会话优先复用原凭证，在安全条件下进行故障切换。
- **模型路由**：管理内置别名与自定义映射，将客户端请求名称映射到本地注册的 BPS 模型。
- **并发与排队**：按代理进程限制同时处理的推理请求，超限先进先出排队，队列满时直接拒绝新请求。
- **中文控制台**：查看请求、会话、用量、模型路由、审批路由和设置；费用展示是本地估算，不是服务商账单。
- **图片和附件**：支持图片输入、客户端本地文件工具，以及本地文件上传接口；具体文件处理仍受格式、依赖和上游能力限制。
- **Windows 桌面管理器**：通过 `BPS-Manager.exe` 启停代理、打开控制台和设置当前用户登录时自启动。

默认地址如下，服务仅监听本机回环地址：

| 用途 | 地址 |
| --- | --- |
| 网页控制台 | `http://127.0.0.1:8001/ui`，根路径 `/` 也可访问 |
| API 基础地址 | `http://127.0.0.1:8001/v1` |
| OAuth 临时回调 | 本机 `1455` 端口，仅登录过程中使用 |

**不要将本服务直接暴露到公网或不可信局域网。** 模型请求可以触发客户端已授权的工具，代理并不会替代客户端的权限与审批机制。

## 安装

### 准备条件

1. 安装 **Git** 和 **Python 3.11 或更高版本**。
2. 准备 Codex，或能够使用 Responses 协议的客户端。
3. 准备有权访问 BPS 的 ChatGPT 账号。使用独立 OAuth 登录不需要安装 Excel；只有复用 Excel 会话时才需要已登录官方 ChatGPT 插件的桌面 Excel。
4. 确保本机 `8001` 端口可用；登录时还需要 `1455` 端口可用。
5. 网络应能访问 GitHub、依赖下载源及登录和推理服务。需要网络代理时，启动后可在控制台配置出站代理。

每台电脑都应单独创建虚拟环境并登录。**不要复制其他电脑的 `.venv` 或加密凭证文件来完成安装。**

### Windows（PowerShell）

在准备放置项目的目录打开 PowerShell：

```powershell
git clone https://github.com/autumnleaf-hub/ghcp_proxy_fzy.git
cd ghcp_proxy_fzy

py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

.\.venv\Scripts\python.exe .\proxy.py
```

确认版本输出不低于 Python 3.11。如果 `py` 不存在，先正确安装 Python；也可以使用已确认版本的 Python 可执行文件创建虚拟环境。后续安装依赖、启动和测试都使用本项目 `.venv` 内的解释器，不需要激活环境。

看到服务启动后，在浏览器打开 `http://127.0.0.1:8001/ui`。前台运行时不要关闭 PowerShell 窗口；按 `Ctrl+C` 可停止服务。

完成依赖安装后，可以改用项目根目录的 **`BPS-Manager.exe`** 启动和管理服务，详见[日常启动与停止](#日常启动与停止)。

### macOS / Linux

在终端执行：

```bash
git clone https://github.com/autumnleaf-hub/ghcp_proxy_fzy.git
cd ghcp_proxy_fzy

python3 --version
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

./.venv/bin/python ./proxy.py
```

确认 `python3` 至少为 3.11。若系统缺少 `venv` 支持，先通过系统包管理器补齐，再创建虚拟环境。

浏览器打开 `http://127.0.0.1:8001/ui`。该方式前台运行，按 `Ctrl+C` 停止；Windows 托盘管理器不能在 macOS / Linux 上运行。

**平台差异：** 当前 Windows 凭证支持加密持久化，其他平台的多凭证 / OAuth 状态仅保存在内存中。重启后可能需要重新登录；macOS 的 Excel 会话可从已登录插件的本地存储重新读取，Linux 不提供同等的桌面 Excel 会话读取方式。

以上手动安装流程不要求 Node.js 或 `npx`。仓库中的 `install_macos.sh` 仍带有历史 Node.js 检查和旧目录约定，建议以本文手动命令为准。

## 首次登录与验证

### 方式一：独立登录 ChatGPT（无需 Excel）

1. 启动代理，打开控制台。
2. 在首次设置中选择 **“登录 ChatGPT（无需 Excel）”**，或进入 **“凭证与登录” → “添加登录”**。
3. 在打开的官方登录页面完成授权。使用 OAuth + PKCE 流程，代理不接收你的账号密码。
4. 登录完成后返回控制台，找到新增凭证，点击 **“验证 BPS”**。
5. 确认验证结果及启用状态，再连接客户端。

注意：

- OAuth 登录期间本机监听 `127.0.0.1:1455`，回调地址使用 `http://localhost:1455/auth/callback`；登录流程约 10 分钟超时，一次完成一个账号。
- 不要同时启动其他占用 `1455` 端口的 Codex / CPA 登录流程。
- **验证 BPS 会发送一个小型模型请求，可能消耗额度。** OAuth 授权成功本身不能证明模型访问权限。
- 同一个账号再次登录会更新已有条目，不是无限新增重复凭证。

### 方式二：复用 Excel 插件会话

- **Windows**：在桌面 Excel 中打开官方 ChatGPT 插件并登录；代理可从 Office WebView2 本地存储发现会话。
- **macOS**：在桌面 Excel 中打开并登录 ChatGPT 插件；代理可读取 Excel 使用的 WebKit 本地会话。
- 如果未发现会话或会话过期，重新打开 / 刷新插件，再回到控制台检查和验证。

无需抓包、开发者工具、调试端口或安装自定义证书。Excel 会话与独立 OAuth 是不同凭证来源，不应通过手动复制浏览器令牌来替代正常登录。

## 连接 Codex

### 使用控制台配置

1. 确认至少一个凭证已启用并通过 BPS 验证。
2. 在 **“概览”** 页的客户端路由提示中，点击 **“启用代理：Codex”**。
3. 完全退出并重新打开 Codex，让客户端重新读取配置和模型目录。
4. 选择可用 BPS 模型，发送一个简单请求，并在控制台 **“请求”** 页确认记录。

启用会修改本机 Codex 配置，项目会备份被修改的配置。设置页另有 **“代理退出时恢复 Codex 和 Claude 的原始配置”** 选项，请按自己的使用方式选择；如果启用恢复，代理退出后客户端可能重新使用原配置。

**不要为了使用 Codex 而点击“全部启用”。** 界面保留了部分 Claude 配置入口，但当前分支的 Anthropic Messages 接口不可用，启用 Claude 代理并不代表能够正常推理。

### 手动配置（可选）

更推荐使用控制台，以便同时维护配置备份和模型目录。需要手动配置时，先备份 `~/.codex/config.toml`，再合并以下配置，不要覆盖现有的 MCP、项目和其他个人设置：

```toml
model_provider = "custom"
model = "gpt-6-sol-excel"
approvals_reviewer = "user"

[model_providers.custom]
name = "OpenAI"
base_url = "http://127.0.0.1:8001/v1"
wire_api = "responses"
```

如果文件已有同名字段或表，应修改原配置，不能重复添加 TOML 表。模型名称可按账号实际权限替换；手动片段不会自动生成项目维护的模型目录。修改后重启 Codex。

本机代理的 Base URL 不应填写为网络代理地址，也不要将 ChatGPT OAuth 令牌填入客户端配置。

## 日常启动与停止

### Windows 托盘管理器

双击仓库根目录的 `BPS-Manager.exe`，可查看服务状态、设置监听端口、启动 / 停止代理、打开网页控制台及设置当前用户登录 Windows 时自启动。

- **它不是完整的 Python 独立安装包。** 管理器仍需要同目录下的 `proxy.py`、项目代码和 `.venv\Scripts\python.exe`；不能只复制 EXE 到其他电脑使用。
- 关闭窗口的 **X** 会隐藏到托盘，不等于停止代理。右键托盘图标可重新打开或退出；退出管理器与停止服务是不同操作，请按提示选择。
- 改变监听端口后，客户端 Base URL 也要同步更新。
- 管理器会核对进程所有者、项目路径和服务身份，不会仅按端口号强制结束其他程序。
- 更新 EXE 前应退出托盘管理器，避免 Windows 文件占用导致拉取失败。

如果更改仓库位置，请重新打开新位置的管理器并检查自启动设置，不要保留指向旧位置的启动项。

### 命令行运行

Windows：

```powershell
.\.venv\Scripts\python.exe .\proxy.py
```

macOS / Linux：

```bash
./.venv/bin/python ./proxy.py
```

所有命令都从仓库根目录执行。不要同时启动多个占用相同端口的代理；停止前先等待重要请求完成。

## 控制台与设置

### 页面说明

| 页面 | 用途 |
| --- | --- |
| 概览 | 运行状态、汇总信息及客户端路由提示 |
| 用量分析 | 查看本地记录的 Token 和费用估算 |
| 请求 / 会话 | 排查请求错误、查看会话与用量记录 |
| 模型路由 | 管理调用名称、内置别名和目标模型 |
| 审批路由 | 查看及配置项目提供的审批路由选项 |
| 设置 | 并发队列、出站代理、调试日志、配置恢复及更新 |
| 凭证与登录 | 添加、验证、启用、停用、重命名和删除凭证 |

### 并发限制与等待队列

在 **“设置” → “并发限制与等待队列”** 中配置：

| 设置 | 默认值 | 含义 |
| --- | --- | --- |
| 并发限制 | `0` | `0` 表示无限制；正整数表示该代理进程最多同时处理的推理请求数 |
| 等待队列大小 | `10` | 达到并发上限后的最多等待请求数；`0` 表示不排队 |

例如设置 **并发 `3`、队列 `10`**：

- 最多 3 个请求正在处理，另有最多 10 个请求等待。
- 等待请求按先进先出顺序获得空闲位置。
- 在 3 个请求处理、10 个请求等待的情况下，新到请求立即返回 **HTTP `429`**，错误码为 **`concurrency_queue_full`**，不会转发上游或加入队列。
- 这里的“丢弃”是明确拒绝该次请求，不是让连接无响应；客户端是否重试由客户端决定。

流式请求从开始处理到结束或取消一直占用并发位置；排队客户端断开会移除等待项。压缩请求和 BPS 验证也计入限制，管理与状态接口不占用推理位置。调低配置不会主动取消已在执行或等待的请求。

保存成功后配置立即生效，并写入用户配置目录下的 `request-concurrency.json`。并发为 `0` 时不因该限制排队，队列容量不限制正常请求。首次升级到包含此功能的版本后，必须先重启 Python 服务，不能只刷新页面。

### 出站代理

设置页预填地址 `http://127.0.0.1:7890`，**默认关闭**。这是代理服务访问上游时使用的网络代理，不是 Codex 的 API Base URL。

- 关闭显式代理时，沿用既有环境代理行为，不等同于强制直连。
- 开启后，BPS 推理、图片上传和 OAuth 换票 / 刷新使用指定 HTTP(S) 代理；连接失败不会静默回退直连。
- 显式代理设置不受环境 `NO_PROXY` 等覆盖，保留 TLS 证书校验。
- 不支持在代理 URL 中保存明文用户名 / 密码，也不能指向本服务自身。
- 保存后新请求使用新配置，不会主动关闭已经进行的流。

### 模型路由

本地注册的目标模型包含 `gpt-6-astra`、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.6-luna`、`gpt-5.6-terra` 和 `gpt-5.6-sol`；规范的 `-excel` 名称可直接使用，例如 `gpt-6-sol-excel`。

内置规则提供裸名称和 `-basispoints` 别名，可修改、启停或删除；自定义规则优先，内置规则独立于自定义映射开关。规则只匹配一次，不递归。没有匹配到规则的名称（包括已删除 / 停用的别名）统一回退到 `gpt-6-astra-excel`，不会去请求 Copilot。

**模型出现在列表中不代表账号一定有权限。** 如需确认请求实际走到哪个模型，请检查请求记录；推理强度以当前客户端模型目录公布的选项为准。

### 多凭证与故障切换

新会话轮询可用凭证，同一会话尽量保持原凭证。认证失败、额度不足、限流或暂时上游错误可能触发凭证暂停 / 冷却；在尚未输出且允许安全重放时，一次请求最多尝试 3 个不同凭证。已经开始输出的流不会自动重放，避免重复执行工具。

删除或停用凭证可能影响绑定会话。跨账号无法恢复的加密历史、仅服务端保存的上下文或没有原文件的账号专属附件，可能需要新建会话或重新上传文件；故障切换并不保证所有历史都能迁移。

## 升级

### 其他电脑直接 `git pull` 就行吗？

**不能只拉取后继续使用旧进程。** `git pull` 更新的是磁盘文件，已经运行的 Python 服务不会自动加载新代码。推荐完整流程：

1. 等待当前请求完成，停止代理；Windows 还应退出托盘管理器。
2. 在该电脑的仓库目录检查本地修改，拉取代码。
3. 使用该电脑的项目虚拟环境同步依赖。
4. 重新启动代理 / 管理器，刷新网页控制台；客户端配置或模型目录有变化时也重启 Codex。

若确认此次更新没有任何依赖变化，第 3 步可以跳过；**不确定就执行安装依赖命令**。不要仅复制 Python 文件或 EXE 来代替完整更新。

### Windows 升级命令

先停止服务并退出管理器，再在仓库根目录执行：

```powershell
git status --short --branch
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**任一步骤失败都先停下排查，不要带着失败结果继续启动。** 成功后双击 `BPS-Manager.exe` 启动服务，或执行：

```powershell
.\.venv\Scripts\python.exe .\proxy.py
```

### macOS / Linux 升级命令

停止正在运行的服务，再在仓库根目录执行：

```bash
git status --short --branch
git pull --ff-only && ./.venv/bin/python -m pip install -r requirements.txt
```

两步成功后启动：

```bash
./.venv/bin/python ./proxy.py
```

### 有本地修改、分支分叉或旧版本时

- `git status` 显示未提交修改时，先备份或提交自己的代码。不要为了升级直接运行 `git reset --hard` 或删除整个仓库。
- `git pull --ff-only` 因分支分叉失败时，先检查本地提交，再决定如何合并；不要盲目强制覆盖。
- 缺少 `.venv`、更换 Python 版本或从 ZIP 安装时，按[安装](#安装)中的流程重新准备环境。ZIP 目录没有 Git 历史，不能直接 `git pull`；建议另建 Git 克隆目录，并重新检查配置及启动路径。
- Windows 提示 `BPS-Manager.exe` 被占用时，确认已从托盘退出，而不是只关闭窗口。
- 旧服务无法被新管理器识别时，在原启动终端正常停止，再通过新版启动；不要结束无法确认身份的进程。

### 控制台更新功能

设置页提供自动检查开关、立即检查和拉取更新；检查开关持久保存，默认开启。环境变量 `GHCP_AUTO_UPDATE` 如有设置会优先控制开关。

**自动检查不等于依赖已安装、所有进程已重启。** 使用页面拉取后，请根据提示检查依赖并在安全时机重启；涉及依赖、管理器 EXE 或跨多个版本升级时，优先使用上面的手动流程。有本地代码修改时，先了解用户 / 开发者模式和冲突提示，不要随意使用覆盖操作。

### 升级后检查

- 打开控制台，确认页面可用、凭证状态正常。
- 检查模型路由、出站代理、并发限制等设置是否符合预期。
- 确认 Codex 仍指向正确端口；需要时重新启用客户端代理并重启客户端。
- 若要验证真实模型访问，主动点击“验证 BPS”或发送一个小请求；这可能消耗额度。
- 可用 `git log -1 --oneline` 查看本机代码版本。其他电脑不会因为这一台推送或升级而自动更新，需各自执行升级流程。

## 数据保存与备份

默认应用目录名为 **`ghcp_proxy_fzy`**，与历史 `ghcp_proxy` 实例隔离。

| 平台 | 配置 | 运行状态 / 日志 | 缓存 |
| --- | --- | --- | --- |
| Windows | `%APPDATA%\ghcp_proxy_fzy` | `%LOCALAPPDATA%\ghcp_proxy_fzy` | `%LOCALAPPDATA%\ghcp_proxy_fzy\Cache` |
| macOS | `~/Library/Application Support/ghcp_proxy_fzy` | 同配置目录 | `~/Library/Caches/ghcp_proxy_fzy` |
| Linux | `~/.config/ghcp_proxy_fzy` | `~/.local/state/ghcp_proxy_fzy` | `~/.cache/ghcp_proxy_fzy` |

Linux 遵循相应的 `XDG_*` 目录设置；`GHCP_CONFIG_DIR`、`GHCP_STATE_DIR`、`GHCP_CACHE_DIR` 可覆盖默认位置。控制台显示的实际路径优先于本文默认值。

- Windows 凭证使用当前用户的 DPAPI 加密保存，凭证池文件为 `bps-credentials.dpapi`。**复制到另一台电脑或另一个 Windows 用户下不等于能解密使用**，换电脑应重新登录。
- 非 Windows 平台当前的 OAuth / 多凭证仅保存在内存中，不能依赖普通目录备份恢复这些登录状态。
- 并发、路由等配置以及本地运行数据不随 Git 推送同步；升级代码通常不需要删除这些目录。
- Codex 配置保存在 `~/.codex`，项目生成的模型目录为 `ghcp-proxy-fzy-models.json`。备份前注意其中可能包含个人配置或其他服务信息。
- 托盘管理器自己的状态和日志位于 `%LOCALAPPDATA%\BPS-Manager` 下按项目区分的目录，与 Python 服务状态不同。

需要备份时，先停止代理，再备份对应用户数据目录和客户端配置。凭证、日志、附件、数据库及配置备份都可能含敏感信息，不要提交到 Git 或公开上传。

### 调试日志与隐私

完整提示词 / 请求体调试记录默认关闭，由设置中的 `debug_prompt_logging_enabled` 控制。排查问题时才临时开启，复现后关闭。`request-trace.jsonl` 可用于关联请求 ID，完整记录也可能包含提示词、文件内容和工具参数；分享日志前必须脱敏。

## 接口与高级配置

### 主要接口

| 方法与路径 | 用途 / 限制 |
| --- | --- |
| `GET /v1/models` | 本地 BPS 模型目录，不代表所有模型已获授权 |
| `POST /v1/responses` | 主要推理入口，支持流式和非流式请求 |
| `POST /v1/responses/compact` | 兼容上下文压缩与摘要回放 |
| `POST /v1/files` | 上传本地附件，使用 multipart 表单 |
| `GET /v1/files`、`GET /v1/files/{id}`、`GET /v1/files/{id}/content` | 列表、元数据和内容 |
| `DELETE /v1/files/{id}` | 删除本地附件 |
| `GET/POST /api/config/concurrency` | 读取状态 / 保存并发和队列配置 |
| `GET /api/credentials`、`POST/DELETE /api/credentials/{id}` | 凭证列表、修改与删除 |
| `POST /api/credentials/{id}/test` | 验证指定凭证的 BPS 权限，可能消耗额度 |
| `POST /v1/chat/completions`、`POST /v1/messages` | 当前分支不提供推理支持，返回 `501` |

本机管理写接口要求同源检查和 `Content-Type: application/json`，包括删除凭证请求。不要为了方便调用而移除这些安全检查。附件接口有自己的输入和访问检查，不应将这条 JSON 要求套用到 multipart 文件上传。

### 图片、文件与工具的边界

- 支持单图、多图、纯图片以及文字和图片交错输入；图片保留顺序，但仍受上游大小、格式和上下文限制。
- 本地文件上传、客户端工具读取文件、BPS 账号下的附件是不同流程；并非任意 `input_file`、远程文件 ID 或不透明压缩历史都能在账号之间通用。
- 浏览器、MCP、终端和计算机操作依赖当前客户端实际提供的工具及权限，代理不会凭空增加未启用工具。
- 工具参数会校验当前目录与 JSON Schema；非法调用不会执行。部分转换错误可尝试一次纠正，失败时返回带请求 ID 的安全诊断，不无限重试。
- 批量传输能力不等于上游保证稳定并行调用；不能仅凭离线转换成功就宣称所有模型支持原生多工具并行。

### 常用环境变量

环境变量应在启动服务前设置；与界面设置同时存在时，按各项说明判断优先级。

| 变量 | 用途 |
| --- | --- |
| `GHCP_PORT` | 监听端口，默认 `8001`；仅绑定 `127.0.0.1` |
| `GHCP_CONFIG_DIR` / `GHCP_STATE_DIR` / `GHCP_CACHE_DIR` | 覆盖配置、状态、缓存目录 |
| `GHCP_AUTO_UPDATE` | 覆盖自动更新检查开关，如 `0` 关闭 |
| `GHCP_AUTO_UPDATE_MODE` | 更新模式：`user` 或 `developer` |
| `GHCP_UPSTREAM_TIMEOUT_SECONDS` | 上游请求超时配置，默认 `300` 秒 |
| `GHCP_UPSTREAM_PROXY` | HTTP / HTTPS 上游代理；也支持 `GHCP_HTTP_PROXY`、`GHCP_HTTPS_PROXY`、`GHCP_NO_PROXY` |
| `GHCP_UPSTREAM_TLS_VERIFY` / `GHCP_UPSTREAM_HTTP2` | 高级 TLS 校验与 HTTP/2 行为配置；不要以关闭证书校验作为常规排障方案 |

未启用界面显式出站代理时，也会考虑标准 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 等环境变量。建议普通用户优先使用控制台的出站代理设置。

例如在 Windows PowerShell 中临时使用其他端口并关闭自动更新检查：

```powershell
$env:GHCP_PORT = "8002"
$env:GHCP_AUTO_UPDATE = "0"
.\.venv\Scripts\python.exe .\proxy.py
```

此时控制台和 API 地址也变成 `8002`。以上环境变量仅作用于当前 PowerShell 及其子进程，不会自动修改从资源管理器启动的托盘管理器配置。

## 常见问题

### 页面打不开 / 端口已被占用

先检查终端或托盘管理器的服务状态，确认启动成功及实际端口。默认控制台是 `http://127.0.0.1:8001/ui`，不是 `8000`。若端口已被其他实例占用，先核实进程身份；不要直接结束不认识的程序。

### 提示缺少 Python 模块

通常是使用了错误的 Python，或升级后没有同步依赖。从仓库根目录执行对应平台的虚拟环境安装命令：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
./.venv/bin/python -m pip install -r requirements.txt
```

不要只向系统 Python 安装依赖。`requirements.txt` 会包含附件处理依赖文件，不需要凭报错逐个猜测安装包。

### 登录成功，但验证 BPS 返回 401 / 403

OAuth 成功只证明登录流程完成，不等于拥有 BPS 权限。检查账号 / 工作区权限、凭证有效期以及请求记录中的上游错误；必要时正常重新登录、重新验证。代理可能暂停该凭证，问题解决后再主动启用。

**403 既不能单凭状态码认定永久封号，也没有“自动解封”的保证。** 不要连续高频重试；保留脱敏后的请求 ID 和错误代码用于排查，不要分享令牌、Cookie 或完整凭证文件。

### OAuth 提示 1455 端口忙

完成或取消另一个正在进行的登录流程，再重试。本项目一次只进行一个 OAuth 登录，不需要修改系统代理或关闭无关进程。

### 删除凭证提示 `Use application/json.`

新版页面已为删除请求补上 JSON Content-Type。先更新并强制刷新页面；如果手动调用管理接口，确认 DELETE 请求也携带 `Content-Type: application/json`，且满足本机同源要求。不要取消服务端校验来绕过错误。

### 并发设置请求返回 404 / 页面有功能但保存失败

可能只拉取了文件、旧 Python 服务还在运行。等待请求完成，正常停止后重新启动，再刷新页面。配置接口首次部署必须重启；之后保存并发 / 队列设置不需要重启。

### 请求返回 `concurrency_queue_full`

这是本机排队容量已满，当前请求没有发往上游，不是账号被封。等待后重试，或根据实际资源适当调整并发和队列；不要通过无限重试制造更大的排队压力。

### Codex 仍然连接旧服务 / 选的模型没有按预期使用

确认客户端代理已启用，完全退出并重启 Codex；检查 Base URL、监听端口及模型目录。再检查内置 / 自定义路由规则：未知模型和已停用别名会回退到 Astra，不会走 Copilot。

### 请求超时或网络代理连接失败

先检查出站代理地址和本机代理软件是否正常，再查看上游错误。必要时调整 `GHCP_UPSTREAM_TIMEOUT_SECONDS`，但增加超时不会修复登录权限或网络不通。正在使用本代理的对话中不要直接重启服务，以免中断当前操作。

## 开发与测试

生产模块保留在仓库根目录，测试统一位于 `tests/`。更多说明见 [tests/README.md](tests/README.md)；自动生成的 `mutants/` 不是日常编辑或测试目标。

所有测试从仓库根目录执行，并使用项目虚拟环境。测试使用标准库 `unittest`，不要求 pytest。

### 定向回归

Windows：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_request_concurrency tests.test_request_concurrency_api tests.test_dashboard_features -v
```

macOS / Linux：

```bash
./.venv/bin/python -B -m unittest tests.test_request_concurrency tests.test_request_concurrency_api tests.test_dashboard_features -v
```

并发回归使用隔离配置和伪请求，主要以并发 `3` 等小规模有限值验证，不测试无限制并发，也不向真实账号发起压力测试。前端 JavaScript 检查需要 Node.js；普通启动服务不需要。

### 全量测试发现

Windows：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -t . -v
```

macOS / Linux：

```bash
./.venv/bin/python -B -m unittest discover -s tests -t . -v
```

请保留 `-t .`，保证测试以 `tests.test_*` 包名导入。部分检查依赖额外平台工具或 `requirements-e2e.txt`，环境不满足时可能跳过；运行前先查看测试说明。

### 构建 Windows 管理器

普通用户可使用仓库提供的 EXE。修改管理器源码后，可在有 .NET Framework 编译器的 Windows 上执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\desktop-manager\build.ps1 -SelfTest
```

这会重新生成根目录的 `BPS-Manager.exe` 并执行管理器自测，构建前应退出正在运行的管理器。

## 许可证

本项目沿用仓库中的 [LICENSE](LICENSE)（Unlicense）。账号、客户端及上游服务仍受各自服务条款与授权约束。
