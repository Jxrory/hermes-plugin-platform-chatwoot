# Telegram Webhook 模式接入流程

## 概述

Telegram Bot API 支持两种更新接收方式：

| 模式 | 描述 | 适用场景 |
|------|------|----------|
| **Polling**（默认） | Hermes 主动发起 `getUpdates` 长轮询 | 本地开发、低延迟要求的场景 |
| **Webhook** | Telegram 将更新通过 HTTP POST 推送给 Hermes | 云部署（Fly.io、Railway 等），冷启动唤醒 |

Webhook 模式下，Telegram 将用户的每条消息作为 HTTP POST 直接推送到 Hermes 的公开 HTTPS 端点，无需主动轮询。这使云平台上的机器可以在无流量时自动休眠，收到 Telegram 推送时自动唤醒。

---

## Webhook 模式配置

### 环境变量

| 变量 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `TELEGRAM_BOT_TOKEN` | 是 | — | BotFather 获取的 bot token |
| `TELEGRAM_WEBHOOK_URL` | 是 | — | 公开可访问的 HTTPS URL |
| `TELEGRAM_WEBHOOK_PORT` | 否 | `8443` | 本地 HTTP 服务器监听端口 |
| `TELEGRAM_WEBHOOK_SECRET` | 是 | — | Webhook 密钥（防伪造更新） |

### 安全要求

**`TELEGRAM_WEBHOOK_SECRET` 是强制要求。** 如果不设置该值，python-telegram-bot 会传递 `secret_token=None`，网络上的任何人都可以伪造 Telegram 更新请求向 webhook 端点注入恶意内容（见 [GHSA-3vpc-7q5r-276h](https://github.com/NousResearch/hermes-agent/security/advisories/GHSA-3vpc-7q5r-276h)）。如果设置了 `TELEGRAM_WEBHOOK_URL` 但没有设置 `TELEGRAM_WEBHOOK_SECRET`，`connect()` 会抛出 `RuntimeError` 拒绝启动。

生成密钥并配置：

```bash
export TELEGRAM_WEBHOOK_SECRET="$(openssl rand -hex 32)"
```

该密钥会通过 `setWebhook` 注册到 Telegram，Telegram 随后每次推送更新时会在 HTTP 头 `X-Telegram-Bot-Api-Secret-Token` 中携带该密钥。

---

## 连接流程（详细时序）

### 一、GatewayRunner 启动阶段

```
GatewayRunner.start()
  │
  ├─ 加载 GatewayConfig（解析 platforms.telegram 配置）
  ├─ _create_adapter("telegram", config)
  │   ├─ 检查 platform_registry
  │   │   └─ 插件路径执行 TelegramAdapter(config) 构造
  │   └─ TelegramAdapter.__init__()
  │       ├─ 解析 extra 配置（rich_messages, status_indicator, dm_topics 等）
  │       ├─ 初始化 HTTPXRequest 参数池
  │       ├─ 设置通知模式
  │       └─ 初始化状态跟踪变量
  │
  └─ adapter.connect()
```

### 二、connect() 详细流程

```
TelegramAdapter.connect(is_reconnect=False)
  │
  ├─ 前置检查
  │   ├─ TELEGRAM_AVAILABLE（python-telegram-bot 是否安装）
  │   ├─ config.token 是否配置
  │   └─ _acquire_platform_lock('telegram-bot-token', token)
  │
  ├─ 构建 Application（PTB 核心对象）
  │   ├─ Application.builder().token(token)
  │   ├─ 如果配置了 custom_base_url → builder.base_url(url)
  │   ├─ 如果配置了 local_mode → builder.local_mode(True)
  │   └─ 配置 HTTPXRequest（连接池 / 超时 / Fallback IP / 代理）
  │       ├─ request（通用请求）和 get_updates_request（webhook 注册用）
  │       └─ builder.request(request).get_updates_request(get_updates_request)
  │
  ├─ builder.build() → self._app
  │   └─ self._bot = self._app.bot
  │
  ├─ 注册 Handler（与 Polling 模式共享）
  │   ├─ MessageHandler(TEXT & ~COMMAND)       → _handle_text_message
  │   ├─ MessageHandler(COMMAND)               → _handle_command
  │   ├─ MessageHandler(LOCATION|VENUE)        → _handle_location_message
  │   ├─ MessageHandler(PHOTO|VIDEO|AUDIO|...) → _handle_media_message
  │   └─ CallbackQueryHandler                  → _handle_callback_query
  │
  ├─ 连接重试
  │   ├─ 最多 8 次重试（指数退避：2^attempt 秒，上限 15s）
  │   └─ app.initialize() —— 建立与 Telegram API 的初始连接
  │
  ├─ app.start() —— 启动 Updater/Dispatcher 内部任务
  │
  ├─ **判断 webhook / polling 模式**
  │   │
  │   ├─ webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL")
  │   │
  │   ├─ 🔴 WEBHOOK 分支（当 webhook_url 非空）
  │   │   │
  │   │   ├─ 验证 TELEGRAM_WEBHOOK_SECRET 非空
  │   │   │   └─ 空 → 抛出 RuntimeError（拒接启动）
  │   │   │
  │   │   ├─ 解析 webhook_path（从 URL 中提取路径部分，默认 "/telegram"）
  │   │   │
  │   │   ├─ PTB 内部行为（start_webhook()）:
  │   │   │   │
  │   │   │   ├─ 1. 启动内置 aiohttp HTTP 服务器
  │   │   │   │   ├─ 监听 0.0.0.0:webhook_port（默认 8443）
  │   │   │   │   ├─ 注册路由 url_path（默认 "/telegram"）
  │   │   │   │   └─ 接受 POST 请求
  │   │   │   │
  │   │   │   ├─ 2. 注册 webhook 到 Telegram 服务器
  │   │   │   │   ├─ 调用 Bot API setWebhook
  │   │   │   │   │   ├─ url = webhook_url（如 https://app.fly.dev/telegram）
  │   │   │   │   │   ├─ secret_token = webhook_secret
  │   │   │   │   │   ├─ allowed_updates = Update.ALL_TYPES
  │   │   │   │   │   └─ drop_pending_updates = True（首次启动）
  │   │   │   │   │
  │   │   │   │   └─ Telegram 返回 OK
  │   │   │   │       └─ 此后所有该机器人的更新通过 POST 推送
  │   │   │   │
  │   │   │   └─ 3. PTB 注册 webhook handler
  │   │   │       ├─ 验证 X-Telegram-Bot-Api-Secret-Token 头
  │   │   │       ├─ 反序列化 Update JSON
  │   │   │       └─ 派发到 Dispatcher → 已注册的 Handler
  │   │   │
  │   │   ├─ self._webhook_mode = True
  │   │   └─ 日志: "Webhook server listening on 0.0.0.0:8443/telegram"
  │   │
  │   └─ 🟢 POLLING 分支（当 webhook_url 为空）
  │       ├─ 调用 delete_webhook（清理可能残留的 webhook 注册）
  │       └─ start_polling() —— 开始长轮询
  │
  ├─ **注册 BotCommand 菜单**（两模式共享）
  │   ├─ 从中央 COMMAND_REGISTRY 生成 BotCommand 列表
  │   ├─ Telegram 限制：最多 60 个命令（可通过 extra.command_menu 调整）
  │   ├─ 注册三个 scope：
  │   │   ├─ BotCommandScopeDefault（全平台默认）
  │   │   ├─ BotCommandScopeAllPrivateChats（私聊）
  │   │   └─ BotCommandScopeAllGroupChats（群组）
  │   └─ 论坛主题的 Command 在首次消息时惰性注册
  │
  ├─ self._mark_connected()
  │
  ├─ **Webhook 模式跳过心跳循环**
  │   └─ 仅在 polling 模式下启动 _polling_heartbeat_loop
  │       └─ Webhook 模式下 Telegram 推送更新，无长轮询连接
  │
  ├─ 设置状态指示器（可选）
  │   └─ set_my_short_description("Online")
  │
  └─ 设置 DM Topics（可选）
      └─ create_forum_topic 创建私聊主题
```

## Webhook 更新接收流程

```
Telegram 用户发送消息
        │
        ▼
Telegram Bot API 服务器
        │
        ├─ 查找该 bot 注册的 webhook URL
        ├─ 构造 Update JSON 对象
        ├─ 添加 HTTP 头: X-Telegram-Bot-Api-Secret-Token
        ├─ HTTP POST → webhook_url
        │
        ▼
HTTPS（互联网）
        │
        ▼
云负载均衡器（如 Fly.io Anycast）
        │
        ├─ 如果机器处于休眠状态 → 自动唤醒
        └─ 转发请求到 Hermes 进程
            │
            ▼
        PTB 内置 aiohttp HTTP Server
        监听 0.0.0.0:8443（或端口）
        路由 POST /telegram（或路径）
            │
            ├─ 1. 验证 Secret Token
            │   └─ 比较 X-Telegram-Bot-Api-Secret-Token 头与配置的 secret
            │       ├─ 匹配 → 继续处理
            │       └─ 不匹配 → 返回 401 Unauthorized
            │
            ├─ 2. 反序列化 JSON 为 Update 对象
            │
            └─ 3. 派发到 Dispatcher.process_update()
                │
                └─ 按优先级路由到已注册的 Handler
                    ├─ MessageHandler(filters.COMMAND)          → _handle_command
                    ├─ MessageHandler(filters.TEXT & ~COMMAND)  → _handle_text_message
                    ├─ MessageHandler(filters.PHOTO|VIDEO|...)  → _handle_media_message
                    ├─ MessageHandler(filters.LOCATION|VENUE)   → _handle_location_message
                    └─ CallbackQueryHandler                     → _handle_callback_query
```

---

## 回复流程（Webhook 模式与 Polling 相同）

```
_handle_text_message/command/media (adapter.py)
  │
  ├─ 授权检查 (_is_user_authorized_from_message)
  ├─ 批处理决策（文本聚合 / 相册合并）
  ├─ 构建 MessageEvent
  │   ├─ _build_message_event()
  │   ├─ 缓存媒体文件（get_file() + download_as_bytearray()）
  │   └─ 设置 event.text / media_urls / media_types
  │
  └─ self.handle_message(event)
      │
      └─ GatewayRunner._process_message()
          │
          ├─ 认证 / 会话查找
          ├─ 启动 AIAgent.run_conversation()
          │
          └─ stream_consumer._send_or_edit()
              │
              └─ adapter.send() → Bot API（HTTP 出站）
                  ├─ sendMessage / sendPhoto / sendDocument 等
                  └─ Telegram Bot API → 用户客户端
```

**关键点：** Webhook 模式的更新是**入站 HTTP**，但回复始终是**出站 Bot API 调用**。这与 Polling 模式完全相同——回复不是通过 webhook 响应体返回的。`start_webhook()` 的 HTTP 响应仅用于向 Telegram 确认已收到更新（返回 200 OK），实际回复通过单独的出站 API 调用完成。

---

## 与 Polling 模式的关键差异

| 维度 | Polling | Webhook |
|------|---------|---------|
| 连接方向 | 出站（Hermes → Telegram） | 入站（Telegram → Hermes） |
| 触发方式 | 每 30s 长轮询 | Telegram 实时推送 |
| 服务器要求 | 无（纯出站） | 需要公开 HTTPS 端点、TCP 端口监听 |
| TLS | 不需要 | 必须 HTTPS（由云平台或反向代理终止） |
| 冷启动 | 持续连接 | 入站 HTTP 可以唤醒休眠机器 |
| 连接池管理 | 需要心跳检测 + CLOSE-WAIT 防护 | 无需心跳（无长连接） |
| 认证 | Bot token（出站认证） | Bot token + Secret Token（入站验证） |
| 更新可靠性 | PTB 内部重试 + 409 冲突恢复 | Telegram 自动重试（失败时） |
| 部署推荐 | 本地 / VPS | 云平台（Fly.io、Railway、K8s） |
| 连接建立 | 无外部依赖 | 需要公开 HTTPS URL + 端口（默认 8443） |

---

## PTB 内部机制详解

### start_webhook() 做了什么

`start_webhook()` 是 python-telegram-bot 的 `Updater` 方法，封装了以下步骤：

```python
# PTB 内部伪代码
async def start_webhook(self, listen, port, url_path, webhook_url, secret_token, ...):
    # 1. 启动内置 WebhookServer（基于 aiohttp）
    self.http_server = WebhookServer(listen, port)
    await self.http_server.start()
    # 注册路由: POST url_path → handle_request
    self.http_server.add_route("POST", url_path, self._handle_webhook_request)

    # 2. 调用 Telegram Bot API 注册 webhook
    await self.bot.set_webhook(
        url=webhook_url,              # 公开 HTTPS URL
        secret_token=secret_token,    # 验证密钥
        allowed_updates=allowed_updates,
        drop_pending_updates=...,     # 是否丢弃等待中的更新
    )

    # 3. 设置 _running = True
```

### setWebhook 调用

PTB 的 `start_webhook()` 内部调用 Bot API `setWebhook` 方法，向 Telegram 服务器注册：

```
POST https://api.telegram.org/bot<TOKEN>/setWebhook
Content-Type: application/json

{
  "url": "https://app.fly.dev/telegram",
  "secret_token": "a1b2c3d4e5f6...",
  "allowed_updates": ["message", "callback_query", ...],
  "drop_pending_updates": true
}
```

成功返回 `{"ok": true, "result": true}`。

### 更新到达时的处理

```python
# PTB 内部伪代码
async def _handle_webhook_request(self, request):
    # 验证 Secret Token
    received_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if received_token != self.secret_token:
        return web.Response(status=401, text="Unauthorized")

    # 反序列化为 Update
    body = await request.json()
    update = Update.de_json(body, self.bot)

    # 派发到 Dispatcher
    await self.dispatcher.process_update(update)

    # 返回 200 OK（Telegram 确认收到）
    return web.Response(status=200, text="OK")
```

### Telegram 的重试逻辑

如果 Hermes 的 webhook 端点返回非 200 响应或不可达，Telegram 会重试：

- 重试间隔从 1s 开始，最长到 1h
- 最多重试 24 小时
- 重试耗尽后更新被丢弃

---

## 安全注意事项

1. **Secret Token 必须设置** — 不使用 secret token 的 webhook 可以被任何人伪造更新注入
2. **使用 HTTPS** — Telegram 要求 webhook URL 必须是 HTTPS；云平台（Fly.io、Railway）自动提供 TLS
3. **端口选择** — Telegram 要求 webhook 端口在 `8443`、`443`、`80`、`88`、`2053` 之一（8443 是默认）
4. **清理残留 webhook** — 启动 Polling 模式前自动调用 `deleteWebhook`，避免抢占导致 409 冲突
5. **Webhook 路径** — 通过 `urllib.parse` 从 `TELEGRAM_WEBHOOK_URL` 解析路径部分，默认 `/telegram`

---

## 配置示例（Fly.io 部署）

```
# .env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_WEBHOOK_URL=https://hermes-agent.fly.dev/telegram
TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 32)
```

```
# fly.toml
[env]
  TELEGRAM_WEBHOOK_URL = "https://hermes-agent.fly.dev/telegram"

[[services]]
  internal_port = 8443
  protocol = "tcp"

  [[services.ports]]
    handlers = ["tls"]
    port = 443
```

---

## 代码参考

| 文件 | 关键位置 | 说明 |
|------|----------|------|
| `plugins/platforms/telegram/adapter.py` | `connect()` 第 2450-2839 行 | Webhook/Polling 连接全流程 |
| `plugins/platforms/telegram/adapter.py` | `disconnect()` 第 2870-2921 行 | 断连（停止 webhook 服务器） |
| `plugins/platforms/telegram/adapter.py` | `_probe_pending_updates()` 第 1866 行 | Webhook 模式下跳过 |
| `gateway/config.py` | `_enable_platform_defaults()` 第 1113 行 | 默认配置注入 |
| `tests/gateway/test_telegram_webhook_secret.py` | 全文 | Secret Token 强制验证测试 |
| `hermes_cli/tips.py` | 第 408 行 | 提示用户设置 TELEGRAM_WEBHOOK_SECRET |
