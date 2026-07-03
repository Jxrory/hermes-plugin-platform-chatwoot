# Chatwoot Agent Bot 插件配置说明

## 文件结构

```
plugins/platforms/chatwoot/
├── __init__.py        # 导出 register()
├── plugin.yaml         # 插件元数据（Hermes 插件系统自动发现）
└── adapter.py          # ChatwootAdapter 实现（390 行）
```

## 原理

```
Telegram 用户 ──> Chatwoot ──(webhook POST)──> Hermes ChatwootAdapter
                                                   │
                                                   ├─ HMAC 签名验证
                                                   ├─ 过滤: 仅 incoming 消息
                                                   ├─ 构建 MessageEvent
                                                   ├─ Hermes Agent 处理
                                                   │   (复用 Telegram 的模型/技能/工具)
                                                   │
                                                   └─ POST Chatwoot API ──> Chatwoot 回复用户
```

Chatwoot 负责 Telegram 消息的接收和发送，Hermes 只作为 AI Agent Bot。

## 配置方式

### 方式一：只配 .env（全部走环境变量）

```bash
# .env
CHATWOOT_BASE_URL=https://chatwoot.makemoney2g.com
CHATWOOT_API_ACCESS_TOKEN=Rcp9KYu3Gkcs9HHaqdtdQd5b
CHATWOOT_WEBHOOK_SECRET=<你的 Chatwoot webhook secret>
CHATWOOT_WEBHOOK_PORT=8646
```

### 方式二：配 config.yaml（推荐）

```yaml
chatwoot:
  enabled: true
  base_url: https://chatwoot.makemoney2g.com
  api_access_token: "Rcp9KYu3Gkcs9HHaqdtdQd5b"
  webhook_secret: "<你的 Chatwoot webhook secret>"
  port: 8646
  path: /webhooks/chatwoot
  host: 0.0.0.0
  account_id: 1
```

YAML 配置优先级低于 `.env`，同时存在时 `.env` 覆盖。

## 配置 Chatwoot Webhook

1. Chatwoot 后台 → Settings → Account → Integrations → Webhooks
2. 添加 webhook:
   - **Payload URL**: `https://你的hermes域名/webhooks/chatwoot`
   - **Secret**: 任意随机字符串（与 `CHATWOOT_WEBHOOK_SECRET` 一致）
   - **Events**: 勾选 `message_created`
3. 保存

## 启动

```bash
# 启动 gateway（自动发现 chatwoot 插件）
hermes gateway start

# 或在 config.yaml 的 gateway 下显式启用
hermes gateway restart
```

## 测试验证

Chatwoot 上发送一条消息，观察 Hermes 日志：

```bash
hermes logs --follow
```

正常流程：
```
[Chatwoot] Listening on 0.0.0.0:8646/webhooks/chatwoot
[Chatwoot] Received message_created from conv 5 (incoming, account 1)
[AIAgent] Processing message in session chatwoot:1:5
[Chatwoot] Sent reply to conv 5 (message_id=105)
```

## 在 Gateway 配置中启用（可选）

如果希望在 gateway 下的 platforms 中显式配置而不是顶层：

```yaml
gateway:
  platforms:
    chatwoot:
      enabled: true
      extra:
        base_url: https://chatwoot.makemoney2g.com
        api_access_token: "Rcp9KYu3Gkcs9HHaqdtdQd5b"
        webhook_secret: "<secret>"
        port: 8646
        path: /webhooks/chatwoot
        host: 0.0.0.0
        account_id: 1
```
