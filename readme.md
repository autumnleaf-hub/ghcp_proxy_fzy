# GHCP Proxy

GHCP Proxy provides an OpenAI-compatible local endpoint for using GitHub Copilot with Codex and the ChatGPT app. Claude Code is also supported through a compatibility integration.

For organizations that provide the official ChatGPT Excel add-in but do not enable ChatGPT for Work or direct Codex access, GHCP Proxy can alternatively route Codex through the backend available to the authenticated Excel add-in session.

The dashboard handles authentication, integrations, usage, cost estimates, and optional startup management.

## Supported Backends

| Client                | Backend                        | Use                                                              |
| --------------------- | ------------------------------ | ---------------------------------------------------------------- |
| Codex and ChatGPT app | GitHub Copilot                 | Default                                                          |
| Codex                 | ChatGPT Excel backend          | Alternative when organizational access is provided through Excel |
| Claude Code           | GHCP Proxy compatibility route | Optional                                                         |

GHCP Proxy listens on loopback only.

```text
API:       http://127.0.0.1:8001/v1
Dashboard: http://127.0.0.1:8001/
```

## Quick Start

### Prerequisites

* Python 3.11 or newer
* GitHub Copilot access when using the Copilot backend
* Codex, the ChatGPT app, or Claude Code for the client you want to configure
* Excel desktop with the official ChatGPT add-in signed in when using the Excel backend

### macOS and Linux

From the repository directory:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python proxy.py
```

### Windows PowerShell

From the repository directory:

```powershell
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install --upgrade pip
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe ./proxy.py
```

Then open `http://127.0.0.1:8001/`.

On the first run:

1. Sign in to GitHub if prompted.
2. Open **Integrations**.
3. Enable the clients you want to use.
4. Optionally install the start and stop commands.
5. Optionally enable startup at login.
6. Restart any clients that were already running.

Node.js, `npx`, and manually edited client configuration files are not required for normal setup.

## Daily Use

Start the proxy with the repository virtual environment:

```bash
./.venv/bin/python proxy.py
```

If you installed the helper commands:

```bash
start-ghproxy
```

On Windows PowerShell:

```powershell
Start-GHProxy
```

After changing an integration, restart the affected client so it reloads its provider configuration.

## Backends

### GitHub Copilot

GitHub Copilot is the default backend. Use it when your GitHub account has Copilot access.

### ChatGPT Excel

Some organizations enable the official ChatGPT add-in for Excel without enabling ChatGPT for Work or direct Codex access.

Because the Excel add-in already provides an authenticated OpenAI backend and can support code-execution workflows, similar coding tasks can be performed from Excel. However, reproducing an agent such as Codex inside Excel requires additional prompting and orchestration.

GHCP Proxy removes that indirection by connecting Codex directly to the backend available through the authenticated Excel add-in session. This lets Codex handle the coding workflow while the model requests use the organization's existing Excel access.

In current testing, this has used substantially fewer tokens than recreating a comparable Codex workflow through a large orchestration prompt inside Excel. This is an observed result, not a guaranteed token-reduction ratio.

Use one of these model names to select the Excel route automatically:

```text
gpt-6-astra
gpt-6-sol
gpt-6-luna
gpt-5.6-luna
gpt-5.6-terra
gpt-5.6-sol
```

The models use the Excel adapter reasoning levels: `low`, `medium`, `high`, and `xhigh`; Astra supports `medium`, `high`, and `xhigh`. `x-high` is accepted as an alias for `xhigh`.

The `-excel` suffix is optional for all models listed above. Existing names such as `gpt-5.6-sol-excel` remain supported, as do `gpt-6-sol-excel` and `gpt-6-luna-excel`. Both forms are advertised by `/v1/models` and route through the Excel session for `/v1/responses` and `/v1/responses/compact`, even when the Copilot SDK is enabled. Other models continue to use GitHub Copilot.

No Excel-specific prompt prefix or additional prompting syntax is required.

## Standalone ChatGPT login (experimental, no Excel install)

This fork defaults to **port 8001** (`GHCP_PORT` overrides it). It uses
`ghcp_proxy_fzy` for runtime/config/cache state and a separate model catalog,
leaving a proxy on port 8000 with its own PID, credentials, and settings.
Legacy state is not imported. Do not enable client proxy settings until you
intend to switch clients away from the old instance.

Start from the repository on Windows:

```powershell
./.venv/Scripts/python.exe proxy.py
```

1. Open `http://127.0.0.1:8001/` and select **Sign in with ChatGPT (no Excel)**
   in setup or Integrations, under GPT Excel / BPS.
2. Complete official OpenAI sign-in. This follows CPA/Codex OAuth with PKCE;
   the proxy never receives your password.
3. The callback listens on **127.0.0.1:1455** while login is pending
   (10-minute timeout). Do not run a CPA/Codex login at the same time.
4. Return and click **Test BPS access**. This sends a small `gpt-6-sol` request
   and may consume credits. **OAuth success does not prove BPS access**:
   token audience, workspace permissions, or model availability may differ.

Access and refresh tokens use Windows DPAPI encryption in the new state
directory. Other platforms keep credentials in memory. Tokens refresh before
BPS requests near expiry. The Excel cache reader cannot overwrite OAuth.
Manage each saved credential separately in **登录凭证** (add, enable/disable, verify, rename, delete). New OAuth logins add or update an account instead of replacing the pool. This implementation does not read or modify EasyCLIProxyAPI's saved accounts.

Local actions (JSON, same-origin): `POST /api/config/excel-oauth/start`,
`POST /api/config/excel-oauth/cancel`, `POST /api/config/excel-oauth/test`.
Status is in `GET /api/config/excel-session`; credentials and callback codes
are not returned in status or logged.

OAuth reference: `router-for-me/CLIProxyAPI`,
`internal/auth/codex/openai_auth.go`. BPS compatibility remains experimental
until the explicit inference check succeeds for your account.

## Windows 桌面管理器

项目根目录的 `BPS-Manager.exe` 是轻量托盘管理器，使用 Windows .NET Framework / WinForms，依赖本项目已有 `.venv/Scripts/python.exe` 和 `proxy.py` 启动代理；不是整个 Python 服务的独立打包版。

- 小卡片提供服务状态、打开 `/ui` 管理页面、启动/停止、监听端口（默认 8001）及当前用户登录 Windows 时自启动。端口变化后，API 客户端的 Base URL 也需要相应修改。
- 顶栏空白处可拖动，最小化按钮缩至任务栏；X 关闭窗口隐藏到系统托盘；右键托盘可重新打开及退出。托盘管理器退出与停止服务是不同操作，按退出提示选择，不会因误点窗口关闭而断开服务。
- 启停前验证监听端口、进程所有者、项目目录、PID / 创建时间及实例 ID，不会按端口号盲目终止程序。更新后的 `python proxy.py` 无论由控制台还是管理器启动，均支持管理器请求正常退出；旧版本或无法证明身份的进程会拒绝或要求明确确认，不能将“端口占用”等同于“本项目服务”。
- 本次新增 `GET /api/desktop/identity` 和 `POST /api/desktop/stop`，仅限本机同源。停止需要当前 PID 和实例 ID；过期实例返回 409。源码和可复现编译脚本位于 `tools/desktop-manager`。
- 网页不再提供旧后台代理 / 终端命令安装卡片；已有终端入口不因移除界面而被自动卸载。

### 自动更新开关

设置页面可关闭 / 开启自动更新检查。开关持久保存，关闭后取消后台检查，之后启动也不主动检查；重新开启恢复定时任务，不重启代理。`GHCP_AUTO_UPDATE` 若被显式设置，则优先于界面设置，界面会显示环境变量控制。该开关不改变开发者模式及本地修改保护；关闭也不删除未提交代码。

## BPS 路由、出站代理与多凭证

本版本仅启用 BPS 上游。`/v1/responses` 和 `/v1/responses/compact` 先匹配启用的规则，未匹配的模型名（包括停用或删除的别名）统一回退 `gpt-6-astra-excel`，不会再尝试 Copilot。非字符串 model 返回 400。Copilot 登录、凭证读取/刷新、SDK 及后台扫描均硬禁用，旧环境变量不能重新启用；模型发现仅返回本地 BPS 目录。原 Copilot 专用 Chat Completions / Anthropic Messages 入口返回 501，并提示改用 Responses。


- **模型路由**：左侧输入一个或多个调用名称（半角或中文逗号分隔），右侧选择本框架注册的 BPS 模型。列表仅包含 Astra / Sol / Luna 三个 GPT-6 模型及原有三个 GPT-5.6 模型，不再从通用价格表填入 Opus 等不可用模型。重复别名、空别名和未知目标会被拒绝。旧规则中的不支持目标会提示迁移警告，不会让设置页面失效。
- **内置别名可管理**：模型路由页默认列出六组裸模型名 / `-basispoints` 别名，可修改目标、启停或删除。内置规则独立于自定义映射总开关，自定义规则优先；明确保存为空不会重新生成。规范的 `-excel` 名称仍可直接使用；实际 BPS 请求使用对应裸名称。规则只匹配一次，不递归。该适配器的原生端点为 `/v1/responses` 和 `/v1/responses/compact`。模型列出不等于每个账号均有访问权限。
- **出站代理**：设置中预填 `http://127.0.0.1:7890`，默认关闭。关闭表示沿用既有环境代理行为；开启后，BPS 推理、图片上传和 OAuth 换票/刷新使用指定 HTTP(S) 代理，忽略环境 `NO_PROXY` 等覆盖，不在连接失败时偷偷直连。显式代理连接保持 TLS 证书校验。不支持在代理 URL 中明文保存用户名/密码，也不能指向本服务自身。保存后新请求采用新设置，进行中的流不会被关闭。
- **登录凭证**：支持最多 32 个凭证；同账号重新登录更新既有条目。优先显示登录邮箱，另保留自定义标签；无可恢复邮箱的旧会话明确提示邮箱不可用，不猜测账号。可设置名称、启停、单独验证和删除。OAuth 回调仍使用本机 1455 端口，一次完成一个登录流程。验证会发送一个小型模型请求，可能消耗额度。框架不会自动登录其他账号。
- **负载均衡和故障切换**：新会话轮询可用凭证，同会话优先保持原凭证。认证失败、额度/限流或暂时上游故障会暂停/冷却该凭证，并在尚未输出内容时最多尝试 3 个不同凭证；支持 HTTP 错误和流式握手后的早期 SSE 失败。已开始输出的请求不自动重放，避免重复工具调用。可主动启用凭证或重新登录恢复暂停账号。
- **跨账号上下文**：切换时从原始本地图片/附件重新上传，移除原账号的加密推理；可读的本地压缩摘要继续保留。仅服务器持有的历史、不可解码的压缩上下文或没有原文件的账号专属附件不能安全迁移，会明确返回错误，要求完整本地历史、新建会话或重新上传，不会静默丢弃上下文。
- Windows 使用当前用户 DPAPI 加密并原子保存凭证、刷新令牌和有限的会话绑定。其他平台目前仅驻留内存，界面会说明。旧单账号凭证自动迁移一次；删除和停用不会被旧缓存或刷新悄悄撤销。管理接口仅允许本机同源 JSON 请求，状态接口不返回令牌或原始账号 ID。

管理 API：`GET/POST /api/config/outbound-proxy`、`GET /api/credentials`、`POST/DELETE /api/credentials/{id}`、`POST /api/credentials/{id}/test`。保存设置无需重启；首次部署本次代码更新需要操作者自行重启服务。

## Excel Setup

The Excel backend uses the authenticated session created by the official ChatGPT add-in.

It does not require network capture, DevTools, a debugging port, custom certificates, or operating-system proxy changes.

### Windows

Excel desktop must have the ChatGPT add-in signed in at least once. GHCP Proxy discovers the session from Office WebView2 local storage.

Check the detected session status with:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/config/excel-session
```

### macOS

Excel desktop must have the ChatGPT add-in open and signed in. GHCP Proxy discovers the session from the local WebKit storage used by Excel.

If the session is missing or expired, reopen or refresh the signed-in Excel task pane and retry the request.

### Clear the Excel Session

Clear the cached session without stopping the proxy:

```powershell
Invoke-RestMethod -Method Delete http://127.0.0.1:8001/api/config/excel-session
```

Then reopen or refresh the ChatGPT add-in before retrying.

## Usage and Billing

The dashboard tracks usage and provides local cost estimates by backend and token type.

GitHub Copilot and Excel-backed usage are tracked separately. Provider-side usage and billing records remain authoritative.

The Excel route uses access already available through the authenticated Excel add-in session. GHCP Proxy does not create or modify organizational entitlements.

For current GitHub Copilot pricing and limits, see:

* [Models and pricing](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing)
* [Usage limits](https://docs.github.com/en/copilot/concepts/rate-limits)

## Integrations

The dashboard's **Integrations** page manages local client configuration. It can:

* connect Codex to GHCP Proxy
* connect the ChatGPT app to GHCP Proxy
* connect Claude Code to GHCP Proxy
* install start and stop commands
* enable or disable startup at login
* restore previous client configuration when an integration is disabled

Existing client configuration is backed up before replacement. Most users should manage integrations through the dashboard rather than editing configuration files manually.

## Configuration

Set environment variables before starting the proxy.

| Variable                        | Purpose                                     | Default or notes               |
| ------------------------------- | ------------------------------------------- | ------------------------------ |
| `GHCP_UPSTREAM_TIMEOUT_SECONDS` | Timeout for upstream non-streaming requests | `300` seconds                  |
| `GHCP_UPSTREAM_PROXY`           | Proxy for HTTP and HTTPS upstream traffic   | Can be overridden per protocol |
| `GHCP_HTTP_PROXY`               | HTTP upstream proxy                         | Optional                       |
| `GHCP_HTTPS_PROXY`              | HTTPS upstream proxy                        | Optional                       |
| `GHCP_NO_PROXY`                 | Hosts excluded from proxying                | Optional                       |
| `GHCP_UPSTREAM_TLS_VERIFY`      | Upstream TLS certificate verification       | Configure as required          |
| `GHCP_UPSTREAM_HTTP2`           | HTTP/2 for upstream requests                | Configure as required          |

Standard `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` variables are also honored.

### Request Concurrency and Queue

The dashboard **Settings** page includes a global concurrency limit and a
bounded FIFO queue. The concurrency limit defaults to **0** (unlimited); the
waiting queue defaults to **10** requests. With a finite limit, requests wait
until a slot is released. Requests arriving when the queue is full receive
HTTP **429** with `concurrency_queue_full` and are not sent upstream. A queue
capacity of **0** disables waiting.

The limit covers Responses, Responses Compact, Chat Completions, Messages and
BPS verification requests. Streaming requests hold their slot until completion
or cancellation. Disconnected queued requests are removed. Management and
status endpoints remain available even when all slots and queue entries are used.
Lowering limits does not cancel requests that are already active or queued.

Settings are saved atomically in `request-concurrency.json` in the user config
directory and apply immediately after a successful save. Restart the proxy once
after installing this feature to load the new backend; later settings changes
do not require restarting. Regression tests use small finite limits, primarily
**3**, isolated configuration files and fake requests, not unrestricted traffic.

### Enterprise Proxy Example

```bash
export GHCP_UPSTREAM_PROXY=http://proxy.example.com:8080
export GHCP_UPSTREAM_TLS_VERIFY=1
export GHCP_UPSTREAM_HTTP2=0

./.venv/bin/python proxy.py
```

## Troubleshooting

### Missing Python packages

Install dependencies and launch the proxy using the same virtual environment:

```bash
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python proxy.py
```

Windows PowerShell:

```powershell
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe ./proxy.py
```

### Dashboard does not open

Check the terminal running `proxy.py`.

If it exited, resolve the reported error and restart it. If another process is using port `8001`, stop the old GHCP Proxy instance or conflicting process first.

### GitHub sign-in does not complete

Keep the dashboard open while completing the GitHub device-code flow with the account that has Copilot access.

After approval, return to the dashboard and wait for the status to refresh.

### Client still uses its old provider

Completely restart the client after changing its integration.

If necessary, disable and re-enable the integration from the dashboard and start a new client session.

### Upstream requests time out

Increase the timeout before starting the proxy:

```bash
export GHCP_UPSTREAM_TIMEOUT_SECONDS=600
./.venv/bin/python proxy.py
```

Windows PowerShell:

```powershell
$env:GHCP_UPSTREAM_TIMEOUT_SECONDS = "600"
./.venv/Scripts/python.exe ./proxy.py
```

### Excel session is missing or expired

Open the official ChatGPT add-in in Excel and confirm that it is signed in.

Refresh or reopen the task pane, then check:

```text
http://127.0.0.1:8001/api/config/excel-session
```

If necessary, clear the cached session and retry.

### Excel requests use the wrong backend

Make sure the selected model is one of the supported Excel models (the `-excel` suffix is optional):

```text
gpt-6-astra
gpt-6-sol
gpt-6-luna
gpt-5.6-luna
gpt-5.6-terra
gpt-5.6-sol
```

The names above and their `-excel` aliases use the Excel backend and require a valid Excel session. Other model names use GitHub Copilot. Restart the proxy after upgrading and refresh generated client configuration to update the model picker.


### Codex 文件与工具兼容范围

- 保持 Codex 的本地文件流程：文件路径或附件描述进入上下文，再由客户端文件/终端工具读取。中文、空格、反斜杠和多行参数不会由代理主动改写；实际文件解析仍取决于客户端工具和本机依赖。此项不等于新增 OpenAI `/v1/files` 上传接口，也不承诺任意 `input_file` 可直接交给 BPS。
- 浏览器、MCP 和 computer-use 走客户端工具转发：保留工具命名空间、调用 ID、自定义工具原始输入以及工具结果中的文字/截图；缓存未命中时重建完整命名空间名称。代理不会自行赋予客户端未启用的桌面控制权限。
- 桥接转换器可解析同一响应中的多个工具调用，覆盖流式和非流式响应；流式只发送一次完成事件，保留各调用的输出索引。但这不表示上游能够稳定生成并行调用：真实 BPS 双工具请求目前仍可能只返回一条，因此模型能力继续公布 `parallel_tool_calls: false`，不能把离线批量转换测试当作上游并行能力验收。无法匹配客户端目录或参数结构的上游工具调用会以 `tool_conversion_rejected` 助手提示收束本轮（未执行任何工具），不再人为生成 `response.failed` 或 HTTP 502 造成 Codex 硬断流。此提示不是工具执行成功；用户可以继续重试。原生 Excel 工具不直接透传，也不部分派发损坏批次。真实上游失败仍保持失败状态。诊断日志仅含工具名和失败分类，不记录参数内容。
- 工具参数使用 JSON Schema 校验：支持本地 `$ref` / `$defs`、`oneOf` / `anyOf` / `allOf`、布尔 schema 和 nullable；按客户端实际目录解析命名空间，并保留 `inputSchema` / `input_schema`。禁止读取外部 schema 引用，不会为了参数校验访问网络或本地文件。升级时请使用仓库虚拟环境安装 `requirements.txt` 中新增的 `jsonschema` / `referencing` 依赖。
- 传输解析只接受单个 JSON 工具对象（兼容明确 JSON 围栏和有限的旧嵌套封装），不执行脚本、不猜测缺失工具名、不从任意代码中抽取调用。 对 JSON 字符串中的裸换行、回车、制表符做保值转义；反斜杠紧接真实控制字符时按控制字符转义处理，保留前面成对的反斜杠，避免凭空多出参数字符。已合法 JSON 优先直接解析，不对合法字面量再次反转义；不猜修缺失引号、逗号或拼接对象，其他非法控制字符仍拒绝。旧文本 marker 也经过相同的目录和参数校验，不能绕过无效原生批次的拒绝。
- 整批通过校验后才写入工具回放缓存；无效批次不会污染后续上下文。拒绝轮次的孤立加密 reasoning 不再回放，之前成功工具轮次的 reasoning 保留。流末尾缺失最终工具列表时不会透传待决的原生工具事件；真实的上游失败、未完成和截断仍明确保留为失败/未完成。
- 拒绝日志包含 `request_id`、工具名、请求的命名空间、失败分类、固定字段的结构摘要及当轮有效工具目录（名称、数量、指纹，不含 schema 或参数值）。`malformed_transport` 还记录具体解析阶段。客户端拒绝提示也附请求 ID，便于关联日志。启用现有请求追踪时，`request-trace.jsonl` 额外记录 `client_tool_rejected` 事件（`dispatched: false`），可通过请求 ID 关联原始请求；不会为此开启完整 prompt/body 调试记录。
- 定向离线回归（Windows）：`.venv/Scripts/python.exe -X utf8 -B -m unittest tests.test_codex_tool_schema tests.test_codex_tool_schema_integration tests.test_client_tool_transport tests.test_codex_bridge_regressions tests.test_codex_transport_recovery tests.test_codex_tool_compat tests.test_excel_images tests.test_excel_upstream -q`。桥接边界测试使用假上游并阻断网络；测试不启动或重启代理监听服务。

### 工具选择与压缩回放

- 按当前请求的 `tool_choice` 执行工具选择：`none` 禁止工具；`required` 要求至少一个合法调用；指定 function/custom 工具时只允许匹配的名称、命名空间和类型。不存在的指定工具在请求入口返回 400，不新增或猜测工具。历史调用仍按完整原始目录回放，不被本轮选择过滤掉。
- `parallel_tool_calls: false` 时整批最多一条调用；违规批次不部分执行，走同样的一次纠正与安全收束流程。指定工具却只返回正文的完成响应也会尝试一次纠正。
- 本地生成的兼容压缩项目在送往 BPS 前展开为摘要消息；保留压缩边界后的输入，不重复旧历史。不解析或伪造上游不透明的原生压缩数据。此项修复了本地 `/v1/responses/compact` 结果回放触发的 400。

### 工具转换自动纠正

- 纠正上下文附上原失败工具候选，作为明确标记未执行、不可信的数据；最多 8 项、总 JSON 64 KiB，超限整批省略，不截断或部分派发。末尾纠正指令只允许按原任务和当前目录修复表示形式，避免从头重新规划并重复生成同类封装错误。

- 对已完成但无法转换的上游工具批次（错误封装、工具名不在当前目录、参数不符），在尚未派发任何工具且输出索引可安全保留时，自动请求模型纠正一次；流式、非流式均适用。未知工具不会被加入白名单，错误参数不会被代理猜写，损坏批次不会部分执行。
- 纠正推理最多一次，总等待上限 120 秒（连接上限 10 秒、写入和连接池等待上限 30 秒；读取受同一 120 秒总预算约束，避免模型生成期间被更短的空闲时限提前切断），可能产生额外模型用量；已报告的两次 token 用量合并计入同一请求。纠正成功后沿原连接返回经过完整校验的工具调用，只发送一次完成事件；如果能力确实不在当轮目录，可返回正常文字说明。
- 纠正提示要求底层参数值不预转义，再依次序列化客户端对象和外层参数；保留 LF/CRLF、反斜杠、Unicode 与尾随空白。代理不会把已经合法的字面量反斜杠加 n 强行替换成换行；目录与 schema 校验也不等于证明模型生成的命令语义正确。
- 二次输出仍不合法、纠正超时或失败时，返回带请求 ID 的原有安全诊断，不无限重试。取消会传播，真正上游失败／截断不伪装成成功。已有调用后又出现可见输出等无法安全重新编号的异常结构不自动重放。
- 纠正结束日志包含独立的 `correction_diagnostic`，区分二次候选状态、结构、工具目录/schema、重复 ID 等拒绝原因；`correction_transport_details` 只记录 JSON 错误类别、位置和长度，不记录原文、参数或片段。原请求诊断仍保留，不再用它冒充二次失败原因。
- 追踪新增 `client_tool_correction_started` / `client_tool_correction_finished`，记录同一 `request_id`、结果分类、当轮工具目录及 token 用量，不记录原始参数、授权头或异常消息正文。父线程和子线程可能拥有不同工具目录，历史提及不等于本轮可调用。

### BPS 图片输入兼容性

`/v1/responses` 支持单图、多图、纯图片（不需要正文）以及文字与图片交错排列。代理不设置额外图片张数上限、不截断图片、不插入虚构的用户正文；实际仍受上游单图大小、格式、请求体和上下文限制。

- 支持 `input_image.image_url` 字符串、`image_url: {url, detail}`、`image_base64` 配合 `media_type`，以及已有的 `file_id`。
- 用户消息中的 base64 图片上传为 BPS 附件；历史 `function_call_output` 中的图片保留 inline URL。两处不能一律替换为 `file_id`，否则“历史工具图片 + 新用户图片”会触发 BPS 422。
- 图片顺序和 `detail` 保留；重复图片在同一账号范围内复用附件缓存，不同账号不会混用文件 ID。
- 图片参数错误返回带具体输入位置的 400；上游上传失败与参数错误分开报告。

修改代码不会替正在运行的 8001 自动加载新逻辑。若当前会话正在使用该端口，不要在会话中直接重启；请等待安全时机手动重启后再验证。
