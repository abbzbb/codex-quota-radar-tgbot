# Codex Quota Radar Telegram Bot

一个可长期运行的 Telegram Bot，用于：

- 查询本机 Codex/ChatGPT 账号额度、已用百分比、重置时间、套餐类型和限制状态。
- 订阅 [Codex Radar RSS](https://codexradar.com/feed.xml) 并向 Telegram 自动提醒。
- 轮询 `https://codex-reset-radar.pages.dev/current.json`，将预测摘要、窗口状态、官方动态、OpenAI 状态事件、Model IQ 更新、额度校准去重转发到 Telegram 频道（默认 `@codex_radar`）。
- 轮询 Sub2API 管理端支付订单列表，对订单支付成功、发放完成、失败、退款等状态变化发送 Telegram 通知。
- 使用 SQLite 保存额度历史、提醒设置、每日报告设置和 RSS 状态。
- 使用 `python-telegram-bot` JobQueue 执行低额度提醒、每日报告和 RSS 检查。
- 使用 `matplotlib` 生成额度趋势图。

本项目不会读取 Codex token、`auth.json`、cookie 或其他敏感认证文件；只通过本机 `codex app-server --listen stdio://` JSON-RPC 接口查询账号与 rate limits。

## 文件说明

```text
.
├── bot.py                                      # 兼容入口，保留 from bot import ...
├── codex_quota_radar_tgbot/                   # Bot 主包
│   ├── app.py                                  # Application 构建和启动
│   ├── config.py                               # 环境变量和日志配置
│   ├── codex_rpc.py                            # Codex app-server JSON-RPC 客户端
│   ├── db.py                                   # SQLite 表和辅助函数
│   ├── current_json.py                         # current.json 拉取、提取、格式化和去重 ID
│   ├── sub2api_payment.py                      # Sub2API 支付订单拉取、格式化和事件 ID
│   ├── rss.py                                  # RSS/Atom 拉取和解析
│   ├── charts.py                               # matplotlib 趋势图
│   ├── handlers.py                             # Telegram 命令和 inline keyboard
│   ├── jobs.py                                 # 后台定时任务
│   └── formatting.py                           # 展示、脱敏和文本工具
├── requirements.txt                            # Python 依赖
├── .env.example                                # 环境变量示例
├── README.md                                   # 使用说明
├── .gitignore                                  # 忽略 .env/数据库/虚拟环境等
└── systemd/
    └── codex-quota-radar-bot.service.example  # systemd 服务示例
```

## 安装

要求 Python 3.10+。

```bash
git clone https://github.com/<your-username>/codex-quota-radar-tgbot.git
cd codex-quota-radar-tgbot
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

如果你的系统中 `python` 指向 Python 3.10+，也可以使用 `python` 代替 `python3`。

## 创建 Telegram Bot Token

1. 在 Telegram 中打开 `@BotFather`。
2. 发送 `/newbot`。
3. 按提示输入 Bot 名称和 username。
4. 复制 BotFather 返回的 token，写入 `.env` 的 `TELEGRAM_BOT_TOKEN`。

## 获取自己的 Telegram chat_id

方法之一：

1. 先临时将 `.env` 中 `ALLOWED_CHAT_IDS=` 留空。
2. 启动 Bot 后给 Bot 发送 `/start`。
3. 可用任意 Telegram getUpdates 工具或在代码日志/临时调试中查看 chat id。
4. 获得 chat_id 后写入：

```env
ALLOWED_CHAT_IDS=123456789
```

多个 chat 用英文逗号分隔：

```env
ALLOWED_CHAT_IDS=123456789,987654321
```

留空表示允许所有 chat，不推荐长期使用。

## 安装和登录 Codex CLI

请先确保本机已安装 Codex CLI，并且当前运行 Bot 的用户可以执行：

```bash
codex --version
codex app-server --listen stdio://
```

如果尚未登录 Codex，请先按 Codex CLI 的官方登录流程完成登录。Bot 不会读取任何认证文件，只会启动本机 Codex app-server 并通过 JSON-RPC 调用：

- `account/read`
- `account/rateLimits/read`

## 配置 `.env`

复制示例文件：

```bash
cp .env.example .env
nano .env
```

示例：

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
ALLOWED_CHAT_IDS=123456789
TELEGRAM_PROXY=
TELEGRAM_CONNECT_TIMEOUT=20
TELEGRAM_READ_TIMEOUT=20
TELEGRAM_WRITE_TIMEOUT=20
TELEGRAM_POOL_TIMEOUT=30
TELEGRAM_CONNECTION_POOL_SIZE=32
TELEGRAM_GET_UPDATES_CONNECTION_POOL_SIZE=8
CODEX_CMD=codex app-server --listen stdio://
RPC_TIMEOUT_SECONDS=60
HTTPS_PROXY=
HTTP_PROXY=
ALL_PROXY=
NO_PROXY=localhost,127.0.0.1
TIMEZONE=Asia/Shanghai
CACHE_SECONDS=20
DB_PATH=codex_quota_radar_bot.sqlite3
CHECK_INTERVAL_MINUTES=15
QUOTA_SAMPLE_INTERVAL_MINUTES=15
WATCH_NOTIFY_ERRORS=0
ENABLE_RAW=0
RADAR_FEED_URL=https://codexradar.com/feed.xml
RADAR_CHECK_INTERVAL_MINUTES=3
RADAR_BOOTSTRAP_SILENT=1
CURRENT_JSON_FORWARD_ENABLED=1
CURRENT_JSON_URL=https://codex-reset-radar.pages.dev/current.json
CURRENT_JSON_CHANNEL_ID=@codex_radar
CURRENT_JSON_CHECK_INTERVAL_MINUTES=3
CURRENT_JSON_BOOTSTRAP_SILENT=1
CURRENT_JSON_MAX_ITEMS_PER_CHECK=5
SUB2API_PAYMENT_NOTIFY_ENABLED=0
SUB2API_BASE_URL=https://your-sub2api.example.com
SUB2API_ADMIN_API_KEY=admin-your-64hex-api-key
SUB2API_PAYMENT_ORDERS_PATH=/api/v1/admin/payment/orders
SUB2API_PAYMENT_NOTIFY_CHAT_IDS=123456789
SUB2API_PAYMENT_NOTIFY_STATUSES=PAID,COMPLETED,FAILED,REFUNDED
SUB2API_PAYMENT_CHECK_INTERVAL_SECONDS=60
SUB2API_PAYMENT_PAGE_SIZE=50
SUB2API_PAYMENT_PAGES=1
SUB2API_PAYMENT_REQUEST_TIMEOUT_SECONDS=15
SUB2API_PAYMENT_BOOTSTRAP_SILENT=1
CHART_MAX_POINTS=300
```

配置说明：

- `TELEGRAM_BOT_TOKEN`：BotFather 提供的 token。
- `ALLOWED_CHAT_IDS`：允许访问的 Telegram chat_id；留空允许所有 chat。
- `TELEGRAM_PROXY`：Telegram API 代理；网络无法直连 `api.telegram.org` 时可填 HTTP 代理，例如 `http://127.0.0.1:7890`。
- `TELEGRAM_CONNECT_TIMEOUT` / `TELEGRAM_READ_TIMEOUT` / `TELEGRAM_WRITE_TIMEOUT` / `TELEGRAM_POOL_TIMEOUT`：Telegram API 连接/读写/等待空闲连接的超时。
- `TELEGRAM_CONNECTION_POOL_SIZE` / `TELEGRAM_GET_UPDATES_CONNECTION_POOL_SIZE`：Telegram HTTP 连接池大小；代理慢、后台任务和用户命令并发时可适当调大。
- `CODEX_CMD`：Codex app-server 启动命令。
- `RPC_TIMEOUT_SECONDS`：等待 Codex JSON-RPC 单个响应的超时时间；`account/rateLimits/read` 偶尔较慢时可调大。
- `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`：Codex CLI 访问 `chatgpt.com` 失败时使用的网络代理；Bot 启动后会让子进程继承这些环境变量。
- `TIMEZONE`：用于显示重置时间和每日报告时间。
- `CACHE_SECONDS`：`/quota` 缓存秒数，避免频繁启动 Codex app-server。
- `DB_PATH`：SQLite 数据库路径。
- `CHECK_INTERVAL_MINUTES`：低额度提醒后台检查间隔。
- `QUOTA_SAMPLE_INTERVAL_MINUTES`：后台均匀保存 Codex 额度快照的间隔；`/chart` 优先使用这些固定间隔样本绘图，避免手动查询时间不均匀造成趋势误导。
- `WATCH_NOTIFY_ERRORS`：后台额度检查失败时是否推送错误到 Telegram；默认 `0`，避免 ChatGPT/Codex 上游网络波动刷屏。
- `ENABLE_RAW`：设为 `1` 才允许 `/raw` 输出调试 JSON。
- `RADAR_FEED_URL`：Codex Radar RSS feed 地址。
- `RADAR_CHECK_INTERVAL_MINUTES`：RSS 后台检查间隔。
- `RADAR_BOOTSTRAP_SILENT`：`1` 表示开启订阅时只记录最新项，不推送历史旧消息。
- `CURRENT_JSON_FORWARD_ENABLED`：是否启用 `current.json` 到 Telegram 频道的后台转发。
- `CURRENT_JSON_URL`：要轮询的 JSON 地址，默认 `https://codex-reset-radar.pages.dev/current.json`。
- `CURRENT_JSON_CHANNEL_ID`：目标频道，默认 `@codex_radar`；Bot 必须是该频道管理员并有发消息权限。
- `CURRENT_JSON_CHECK_INTERVAL_MINUTES`：`current.json` 检查间隔。
- `CURRENT_JSON_BOOTSTRAP_SILENT`：`1` 表示第一次启动只记录当前已有条目，之后只转发新条目；设为 `0` 会在首次检查推送最多 `CURRENT_JSON_MAX_ITEMS_PER_CHECK` 条历史条目。
- `CURRENT_JSON_MAX_ITEMS_PER_CHECK`：每次最多转发的新条目数，防止刷屏。
- `SUB2API_PAYMENT_NOTIFY_ENABLED`：是否启用 Sub2API 支付订单通知。
- `SUB2API_BASE_URL`：Sub2API 后端基础地址，例如 `https://sub2api.example.com`。
- `SUB2API_ADMIN_API_KEY`：Sub2API Admin API Key，会通过 `x-api-key` 请求头访问管理端只读订单列表；不要发到群里或提交到仓库。
- `SUB2API_PAYMENT_ORDERS_PATH`：管理端订单列表路径，默认 `/api/v1/admin/payment/orders`。
- `SUB2API_PAYMENT_NOTIFY_CHAT_IDS`：接收支付订单通知的 Telegram chat_id；留空时回退使用 `ALLOWED_CHAT_IDS`。
- `SUB2API_PAYMENT_NOTIFY_STATUSES`：要通知的订单状态，默认 `PAID,COMPLETED,FAILED,REFUNDED`。如需退款申请也提醒，可加入 `REFUND_REQUESTED,REFUNDING`。
- `SUB2API_PAYMENT_CHECK_INTERVAL_SECONDS`：支付订单轮询间隔，单位秒。
- `SUB2API_PAYMENT_PAGE_SIZE` / `SUB2API_PAYMENT_PAGES`：每轮拉取的订单页大小和页数。订单量很大时可提高页数，但注意不要过度请求。
- `SUB2API_PAYMENT_REQUEST_TIMEOUT_SECONDS`：请求 Sub2API 的超时时间。
- `SUB2API_PAYMENT_BOOTSTRAP_SILENT`：`1` 表示首次启动只记录当前已有订单状态，不推送历史旧订单；之后只通知新状态事件。
- `CHART_MAX_POINTS`：趋势图最多绘制的历史点数。

## 启动

```bash
source .venv/bin/activate
python bot.py
```

启动成功后日志会显示 Bot running。然后在 Telegram 中发送 `/start`。

## Telegram 命令

### 基础命令

- `/start`：显示 Bot 简介和命令列表。
- `/help`：显示完整帮助。
- `/health`：检查当前时间、时区、数据库路径、Codex 命令、Codex app-server 可调用性、账号邮箱/套餐/rate limits 可读性、Radar RSS 和 current.json 可读性。

### Codex 额度

- `/quota`：读取当前 Codex 额度，默认使用缓存。
- `/refresh`：强制刷新 Codex 额度，不使用缓存。
- `/raw`：当 `ENABLE_RAW=1` 时输出 `account/read` 和 `account/rateLimits/read` 原始 JSON；否则提示未启用。
- `/watch 20`：开启低额度提醒；当最紧张额度剩余 ≤20% 时提醒。阈值必须为 0 到 100。
- `/watch_off`：关闭低额度提醒。
- `/daily 09:00`：开启每日额度报告，使用 `.env` 中的 `TIMEZONE`。
- `/daily_off`：关闭每日额度报告。
- `/history`：显示最近 12 条额度查询历史。
- `/chart 24h`、`/chart 1d`、`/chart 7d`、`/chart 30d`：生成额度趋势图；优先使用后台按 `QUOTA_SAMPLE_INTERVAL_MINUTES` 均匀记录的全局样本，样本不足时才回退当前 chat 的旧历史。

### Codex Radar RSS

- `/radar`：手动读取最近 3 条 Codex Radar 更新。
- `/radar_watch`：开启当前 chat 的 RSS 自动提醒。
- `/radar_check`：手动检查是否有新条目；最多推送最近 5 条，按旧到新顺序发送。
- `/radar_off`：关闭当前 chat 的 RSS 自动提醒，不删除历史状态。

### Codex Reset Radar 频道转发

这部分没有 Telegram 命令，随 Bot 后台任务自动运行。启用 `CURRENT_JSON_FORWARD_ENABLED=1` 后，Bot 会按 `CURRENT_JSON_CHECK_INTERVAL_MINUTES` 轮询 `CURRENT_JSON_URL`，提取并去重转发到 `CURRENT_JSON_CHANNEL_ID`。当前会转发：

- Codex reset 概率预测摘要（包括 low/none 状态）；
- Codex reset/window 当前窗口状态（包括未开启/已关闭状态）；
- 官方 Codex 动态；
- OpenAI Status 事件；
- Codex reset/card/window 确认信息；
- Model IQ 更新；
- `model_iq.quota_calibration` 额度校准结果。
为防止重复和漏转，数据库会记录每条派生消息的 stable id，并额外记录按类别/标题/摘要归一化后的相似内容指纹；同类内容只发生百分比、日期、URL 或少量数字变化时不会重复转发。`current.json` 中的社区 reset/额度讨论（如 `complaint_examples`）只作为概率背景，不转发到频道。

### Sub2API 支付订单通知

这部分没有 Telegram 命令，随 Bot 后台任务自动运行。推荐使用 Sub2API 自带的支付/回调/发放流程处理真实支付，本 Bot 只通过管理端只读接口轮询订单状态并发通知：

- 调用接口：`GET {SUB2API_BASE_URL}/api/v1/admin/payment/orders?page=1&page_size=...&status=...`
- 认证方式：请求头 `x-api-key: ${SUB2API_ADMIN_API_KEY}`
- 默认通知状态：`PAID`（已支付，等待发放）、`COMPLETED`（已完成）、`FAILED`（失败）、`REFUNDED`（已退款）。
- 去重方式：SQLite 记录 `订单 ID + 状态` 事件，同一个订单同一状态只通知一次；从 `PAID` 变为 `COMPLETED` 会各通知一次。
- 首次启动：默认 `SUB2API_PAYMENT_BOOTSTRAP_SILENT=1`，只标记当前已有订单，不把历史订单刷到 Telegram。

最小配置示例：

```env
SUB2API_PAYMENT_NOTIFY_ENABLED=1
SUB2API_BASE_URL=https://your-sub2api.example.com
SUB2API_ADMIN_API_KEY=admin-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SUB2API_PAYMENT_NOTIFY_CHAT_IDS=123456789
```

## Inline Keyboard 快捷操作

`/start` 和 `/help` 会显示快捷按钮，支持：

- 📊 查询额度
- 🔄 强制刷新
- 📈 趋势图，并可选择 `24h` / `1d` / `7d` / `30d`
- 🕘 历史
- 🛰️ Radar
- 🔎 检查更新
- 🩺 健康检查
- ❓ 帮助

所有按钮回调同样会检查 `ALLOWED_CHAT_IDS` 权限。

## systemd 后台运行

复制服务文件：

```bash
sudo cp systemd/codex-quota-radar-bot.service.example /etc/systemd/system/codex-quota-radar-bot.service
sudo nano /etc/systemd/system/codex-quota-radar-bot.service
```

将示例中的路径替换为真实路径，例如：

```ini
WorkingDirectory=/path/to/codex-quota-radar-tgbot
ExecStart=/path/to/codex-quota-radar-tgbot/.venv/bin/python /path/to/codex-quota-radar-tgbot/bot.py
```

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now codex-quota-radar-bot
sudo systemctl status codex-quota-radar-bot
```

查看日志：

```bash
journalctl -u codex-quota-radar-bot -f
```

注意：systemd 运行用户必须能访问 `.env`、SQLite 路径，并能执行 `codex app-server --listen stdio://`。

## 测试与验收

### 语法检查

```bash
python -m py_compile bot.py
# 或
python3 -m py_compile bot.py
```

### RSS 解析测试

```bash
python - <<'PY'
import asyncio
from bot import fetch_radar_feed_items

async def main():
    items = await fetch_radar_feed_items()
    print("items:", len(items))
    for item in items[:3]:
        print(item.get("title"), item.get("published"), item.get("link"))

asyncio.run(main())
PY
```

### current.json 解析测试

```bash
python - <<'PY'
import asyncio
from bot import fetch_current_json_items

async def main():
    items = await fetch_current_json_items()
    print("items:", len(items))
    counts = {}
    for item in items:
        counts[item.get("kind")] = counts.get(item.get("kind"), 0) + 1
    print("counts:", counts)
    for item in items[:3]:
        print(item.get("kind"), item.get("title"), item.get("published"), item.get("url"))

asyncio.run(main())
PY
```

### Codex RPC 测试

在已经登录 Codex 的机器上测试：

```bash
python - <<'PY'
import asyncio
from bot import fetch_codex_payload, format_quota

async def main():
    payload = await fetch_codex_payload()
    print(format_quota(payload))

asyncio.run(main())
PY
```

### 手动命令测试

启动：

```bash
python bot.py
```

Telegram 中依次测试：

```text
/start
/help
/health
/quota
/refresh
/watch 20
/watch_off
/daily 09:00
/daily_off
/history
/chart 7d
/radar
/radar_watch
/radar_check
/radar_off
```

如果 `ENABLE_RAW=1`，再测试：

```text
/raw
```

## 常见问题排查

### `codex app-server` 找不到

- 确认 `codex --version` 可运行。
- 确认 systemd 的运行用户 PATH 中包含 Codex CLI。
- 必要时在 `.env` 中把 `CODEX_CMD` 写成绝对路径，例如：

```env
CODEX_CMD=/home/you/.local/bin/codex app-server --listen stdio://
```

### 未登录 Codex

先在同一个用户下完成 Codex CLI 登录。Bot 不会读取 token/auth 文件；未登录时 JSON-RPC 调用可能失败。

### 后台额度检查偶尔失败

后台 `/watch` 定时任务会强制刷新 Codex rate limits。该接口依赖 Codex CLI 访问 `https://chatgpt.com/backend-api/wham/usage`，所以网络或 ChatGPT 上游波动时可能失败。默认情况下，后台任务会记录日志并优先使用最近缓存，不再向 Telegram 推送临时失败；如果你希望收到失败提醒，可设置 `WATCH_NOTIFY_ERRORS=1`。

### 读取不到 rate limits

- 如果错误是 `等待 JSON-RPC id=3 响应超时`，表示 `account/rateLimits/read` 没在超时时间内返回。可在 `.env` 调大 `RPC_TIMEOUT_SECONDS=90` 或 `120` 后重启。
- 如果错误包含 `failed to fetch codex rate limits`、`chatgpt.com/backend-api/wham/usage` 或 `error sending request`，表示 Codex CLI/app-server 访问 ChatGPT 后端失败。请确认当前用户可访问 `chatgpt.com`，或在 `.env` 设置 `HTTPS_PROXY=http://127.0.0.1:7890`、`HTTP_PROXY=http://127.0.0.1:7890` 后重启。
- 运行 `/health` 查看 Codex app-server 是否可调用。
- 使用 `ENABLE_RAW=1` 后调用 `/raw` 查看非敏感 JSON 结构。
- Codex app-server 返回结构可能变化，代码会优先读取 `rateLimitsByLimitId.codex`，再回退 `rateLimits`。

### Telegram Bot 没响应

- 如果日志出现 `telegram.error.TimedOut`、`httpx.ConnectTimeout` 或 `Network is unreachable`，说明机器无法直连 Telegram API。请检查网络，或在 `.env` 配置 `TELEGRAM_PROXY=http://127.0.0.1:7890` 这类 HTTP 代理。
- 如果日志出现 `httpx.PoolTimeout` 或 `Pool timeout: All connections in the connection pool are occupied`，说明 Telegram HTTP 连接池里的连接都被占用，请优先检查代理是否卡住、是否启动了多个 bot 进程；也可以调大 `TELEGRAM_POOL_TIMEOUT=60`、`TELEGRAM_CONNECTION_POOL_SIZE=64` 后重启。
- 检查 `TELEGRAM_BOT_TOKEN` 是否正确。
- 检查 Bot 是否正在运行。
- 如果配置了 `ALLOWED_CHAT_IDS`，确认当前 chat_id 在列表中；否则会回复 `无权限。`。
- 查看日志或 systemd `journalctl`。

### `/chart` 报 matplotlib 错误

- 确认已安装 `matplotlib>=3.8.0`。
- 本程序使用 `Agg` 后端，适合无图形界面的 systemd 环境。
- 均匀采样记录不足 2 条时无法画出可靠趋势；请等待后台采样至少两个周期。旧手动历史仍会作为临时回退。

### RSS 或 current.json 读取失败

- 检查网络能否访问 `RADAR_FEED_URL` 和 `CURRENT_JSON_URL`。
- 确认 RSS feed 返回 XML，`current.json` 返回 JSON，而不是网页 HTML 或错误页。
- 后台任务失败不会退出 Bot，会记录 `last_error` 并写日志。

### current.json 没有转发到频道

- 确认 `CURRENT_JSON_FORWARD_ENABLED=1` 且 `CURRENT_JSON_CHANNEL_ID=@codex_radar`。
- 确认 Bot 已加入 `@codex_radar` 并是管理员，具备发消息权限。
- 默认 `CURRENT_JSON_BOOTSTRAP_SILENT=1`，首次启动只记录当前已有条目，不会回放历史；后续 JSON 出现新 stable id 且未命中相似内容指纹时才会转发。要测试历史推送，可临时设置为 `0` 或清理对应 `channel_forward_state`、`channel_forward_seen`、`channel_forward_fingerprints` 状态。
- 查看日志中的 `current_json_channel_job fetch failed` 或 `current_json_channel_job send failed`。

### systemd 环境变量或路径问题

- 确认 `WorkingDirectory` 指向项目目录。
- 确认 `.env` 位于 `WorkingDirectory` 下。
- 确认 `ExecStart` 使用虚拟环境中的 Python。
- 确认 systemd 运行用户可以执行 Codex CLI，并且该用户已登录 Codex。
- 如果数据库路径是相对路径，它相对于 `WorkingDirectory`。

## 后续可选增强

- 将大文件 `bot.py` 拆分为包结构（需要同步更新验收命令）。
- 增加更细粒度的单元测试。
- 增加 Telegram inline keyboard 交互。
- 增加多 feed 支持。
