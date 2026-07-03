"""
Chatwoot Agent Bot adapter.

Receives webhook events from Chatwoot (message_created) and replies as an
AI agent bot via the Chatwoot API.

Flow:
  Chatwoot ──POST (webhook)──> Hermes ChatwootAdapter
                                  │
                                  ├─ Validate x-chatwoot-signature (HMAC-SHA256)
                                  ├─ Filter: only message_type=incoming
                                  ├─ Build MessageEvent with conversation context
                                  ├─ Hermes Agent processes and generates response
                                  │
                                  └─ POST (API) ──> Chatwoot /api/v1/.../messages
                                                      (content + message_type=outgoing)
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, Optional

try:
    from aiohttp import web, ClientSession, ClientTimeout

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None
    ClientSession = None

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.platforms.helpers import strip_markdown
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8646
DEFAULT_WEBHOOK_PATH = "/webhooks/chatwoot"

# Mapping from Chatwoot conversation.channel to Hermes Platform.
# The LLM uses source.platform to tailor response formatting; by detecting the
# real source channel (Telegram, WhatsApp, etc.) we get platform-appropriate
# output instead of treating everything as a generic Chatwoot relay message.
_CHATWOOT_CHANNEL_MAP: Dict[str, Platform] = {
    "Channel::Telegram": Platform.TELEGRAM,
    "Channel::WhatsApp": Platform.WHATSAPP,
    "Channel::Email": Platform.EMAIL,
    "Channel::Sms": Platform.SMS,
}


def check_chatwoot_requirements() -> bool:
    return AIOHTTP_AVAILABLE


class ChatwootAdapter(BasePlatformAdapter):
    """Chatwoot Agent Bot adapter.

    Listens for Chatwoot webhook events, processes them through
    the Hermes agent, and replies via the Chatwoot API.
    """

    splits_long_messages = False
    supports_code_blocks = True

    def __init__(self, config):
        super().__init__(config, Platform("chatwoot"))
        self._base_url: str = ""
        self._api_token: str = ""
        self._webhook_secret: str = ""
        self._host: str = DEFAULT_HOST
        self._port: int = DEFAULT_PORT
        self._webhook_path: str = DEFAULT_WEBHOOK_PATH
        self._runner: Optional[web.AppRunner] = None
        self._http_client: Optional[ClientSession] = None
        self._account_id: Optional[int] = None
        # Track source platform per conversation so send() can apply
        # platform-appropriate formatting (MarkdownV2 for Telegram, etc.)
        self._conversation_platforms: Dict[str, Platform] = {}

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.error("[Chatwoot] aiohttp not installed")
            return False

        self._base_url = os.getenv("CHATWOOT_BASE_URL", "").rstrip("/")
        self._api_token = os.getenv("CHATWOOT_API_ACCESS_TOKEN", "")
        self._webhook_secret = os.getenv("CHATWOOT_WEBHOOK_SECRET", "")

        if not self._base_url:
            logger.error("[Chatwoot] CHATWOOT_BASE_URL is not set")
            return False
        if not self._api_token:
            logger.error("[Chatwoot] CHATWOOT_API_ACCESS_TOKEN is not set")
            return False
        if not self._webhook_secret:
            logger.error("[Chatwoot] CHATWOOT_WEBHOOK_SECRET is not set")
            return False

        extra = getattr(self.config, "extra", {}) or {}
        self._host = extra.get("host", os.getenv("CHATWOOT_WEBHOOK_HOST", DEFAULT_HOST))
        self._port = int(
            extra.get("port", os.getenv("CHATWOOT_WEBHOOK_PORT", str(DEFAULT_PORT)))
        )
        self._webhook_path = extra.get(
            "path", os.getenv("CHATWOOT_WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH)
        )
        self._account_id = extra.get("account_id")

        self._http_client = ClientSession(
            base_url=self._base_url,
            timeout=ClientTimeout(total=30),
            headers={
                "api_access_token": self._api_token,
                "Content-Type": "application/json",
            },
        )

        app = web.Application()
        app.router.add_post(self._webhook_path, self._handle_webhook)
        app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        self._mark_connected()
        logger.info(
            "[Chatwoot] Listening on %s:%d%s",
            self._host, self._port, self._webhook_path,
        )
        return True

    async def disconnect(self) -> None:
        if self._http_client:
            await self._http_client.close()
            self._http_client = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()
        logger.info("[Chatwoot] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Post the agent's response back to the Chatwoot conversation.

        ``chat_id`` is formatted as ``chatwoot:{account_id}:{conversation_id}``
        by ``_handle_webhook``, so we extract the account and conversation IDs
        from it.
        """
        if not self._http_client:
            return SendResult(success=False, error="Not connected")
        if not content or not content.strip():
            return SendResult(success=True, message_id=None)

        # Apply platform-appropriate formatting based on the source channel.
        # Telegram: convert standard Markdown (from LLM) to MarkdownV2 so
        # Chatwoot's Telegram integration can render bold/italic/code etc.
        # Other platforms (WhatsApp, Email, SMS): strip to plain text since
        # they don't support rich markdown through Chatwoot.
        source_platform = self._conversation_platforms.get(chat_id)
        if source_platform == Platform.TELEGRAM:
            content = _markdown_to_markdownv2(content)
        else:
            content = strip_markdown(content)

        parts = chat_id.split(":", 2)
        if len(parts) != 3:
            return SendResult(success=False, error=f"Invalid chat_id: {chat_id}")
        _, account_id, conversation_id = parts

        try:
            resp = await self._http_client.post(
                f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages",
                json={"content": content, "message_type": "outgoing"},
            )
            if resp.status >= 400:
                body = await resp.text()
                logger.error(
                    "[Chatwoot] Failed to send message to conv %s (HTTP %d): %s",
                    conversation_id, resp.status, body,
                )
                return SendResult(
                    success=False,
                    error=f"Chatwoot API error: HTTP {resp.status}",
                    retryable=resp.status >= 500,
                )
            result = await resp.json()
            msg_id = str(result.get("id", ""))
            return SendResult(success=True, message_id=msg_id)
        except Exception as e:
            logger.error("[Chatwoot] send() error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "platform": "chatwoot"})

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        raw_body = await request.read()

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return web.json_response({"error": "Bad JSON"}, status=400)

        body = payload.get("body", payload)
        event = body.get("event", "")
        message_type = body.get("message_type", "")

        if event != "message_created":
            return web.json_response({"status": "ignored", "event": event})

        if message_type != "incoming":
            return web.json_response({"status": "ignored", "message_type": message_type})

        conversation = body.get("conversation", {})
        account = body.get("account", {}) or conversation.get("account", {})
        sender = body.get("sender", {})

        account_id = str(account.get("id", ""))
        conversation_id = str(conversation.get("id", ""))

        if not account_id or not conversation_id:
            logger.warning("[Chatwoot] Missing account_id or conversation_id")
            return web.json_response({"error": "Missing identifiers"}, status=400)

        sender_name = sender.get("name", "Unknown")

        content = body.get("content", "")

        channel = conversation.get("channel", "")
        source_platform = _CHATWOOT_CHANNEL_MAP.get(channel, Platform("chatwoot"))

        session_chat_id = f"chatwoot:{account_id}:{conversation_id}"
        # Remember the source platform so send() can apply correct formatting
        self._conversation_platforms[session_chat_id] = source_platform
        source = SessionSource(
            platform=source_platform,
            chat_id=session_chat_id,
            chat_type="dm",
            user_id=f"sender:{sender.get('id', '')}",
            user_name=sender_name,
        )

        event_obj = MessageEvent(
            text=content,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=str(body.get("id", "")),
        )

        task = asyncio.create_task(self.handle_message(event_obj))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return web.json_response({"status": "accepted"})

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "chatwoot"}


def _markdown_to_markdownv2(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2 format.

    Handles bold, italic, strikethrough, spoiler, inline code, fenced code
    blocks, headers, links, and escapes all remaining MarkdownV2-special
    characters so the message renders correctly when Chatwoot sends it to
    Telegram with ``parse_mode=MarkdownV2``.
    """
    if not text:
        return text

    placeholders: dict = {}
    counter = [0]

    def _ph(value: str) -> str:
        key = f"\x00PH{counter[0]}\x00"
        counter[0] += 1
        placeholders[key] = value
        return key

    def _escape_mdv2(s: str) -> str:
        return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', s)

    s = text

    # 1) Protect fenced code blocks
    s = re.sub(
        r'(```(?:[^\n]*\n)?[\s\S]*?```)',
        lambda m: _ph(m.group(0)),
        s,
    )

    # 2) Protect inline code
    s = re.sub(
        r'(`[^`]+`)',
        lambda m: _ph(m.group(0)),
        s,
    )

    # 3) Convert links [text](url)
    s = re.sub(
        r'\[([^\]]+)\]\(([^()]+)\)',
        lambda m: _ph(f'[{_escape_mdv2(m.group(1))}]({m.group(2).replace(")", "\\)")})'),
        s,
    )

    # 4) Convert headers ## Title → *Title*
    s = re.sub(
        r'^#{1,6}\s+(.+)$',
        lambda m: _ph(f'*{_escape_mdv2(m.group(1).strip())}*'),
        s,
        flags=re.MULTILINE,
    )

    # 5) Convert bold **text** → *text*
    s = re.sub(
        r'\*\*(.+?)\*\*',
        lambda m: _ph(f'*{_escape_mdv2(m.group(1))}*'),
        s,
    )

    # 6) Convert italic *text* → _text_
    s = re.sub(
        r'\*([^*\n]+)\*',
        lambda m: _ph(f'_{_escape_mdv2(m.group(1))}_'),
        s,
    )

    # 7) Convert strikethrough ~~text~~ → ~text~
    s = re.sub(
        r'~~(.+?)~~',
        lambda m: _ph(f'~{_escape_mdv2(m.group(1))}~'),
        s,
    )

    # 8) Convert spoiler ||text||
    s = re.sub(
        r'\|\|(.+?)\|\|',
        lambda m: _ph(f'||{_escape_mdv2(m.group(1))}||'),
        s,
    )

    # 9) Escape remaining special chars outside protected regions
    s = _escape_mdv2(s)

    # 10) Restore placeholders in reverse order
    for key in reversed(list(placeholders.keys())):
        s = s.replace(key, placeholders[key])

    return s


async def _standalone_send(pconfig, chat_id, message, **kwargs) -> dict:
    """Standalone sender for cron delivery — opens ephemeral connection."""
    base_url = os.getenv("CHATWOOT_BASE_URL", "").rstrip("/")
    token = os.getenv("CHATWOOT_API_ACCESS_TOKEN", "")
    if not base_url or not token:
        return {"error": "CHATWOOT_BASE_URL or CHATWOOT_API_ACCESS_TOKEN not set"}
    parts = chat_id.split(":", 2)
    if len(parts) != 3:
        return {"error": f"Invalid chat_id: {chat_id}"}
    _, account_id, conversation_id = parts
    # Standalone sender has no platform context — strip markdown for safety
    message = strip_markdown(message)
    try:
        from aiohttp import ClientSession, ClientTimeout

        async with ClientSession(
            base_url=base_url,
            timeout=ClientTimeout(total=30),
            headers={
                "api_access_token": token,
                "Content-Type": "application/json",
            },
        ) as session:
            async with session.post(
                f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages",
                json={"content": message, "message_type": "outgoing"},
            ) as resp:
                if resp.status >= 400:
                    return {"error": f"HTTP {resp.status}"}
                result = await resp.json()
                return {"success": True, "message_id": str(result.get("id", ""))}
    except Exception as e:
        return {"error": str(e)}


def _build_adapter(config) -> ChatwootAdapter:
    return ChatwootAdapter(config)


def _is_connected(config) -> bool:
    return bool(
        os.getenv("CHATWOOT_BASE_URL")
        and os.getenv("CHATWOOT_API_ACCESS_TOKEN")
        and os.getenv("CHATWOOT_WEBHOOK_SECRET")
    )


def _apply_yaml_config(yaml_cfg: dict, chatwoot_cfg: dict) -> Optional[dict]:
    """Translate config.yaml ``chatwoot:`` keys into env vars + extra.

    Users configure Chatwoot in ``config.yaml``::

        chatwoot:
          enabled: true
          base_url: https://chatwoot.example.com
          api_access_token: "<token>"
          webhook_secret: "<secret>"
          port: 8646
          path: /webhooks/chatwoot
          host: 0.0.0.0
          account_id: 1

    Env vars take precedence over YAML. Non-env keys (``port``, ``path``,
    ``host``, ``account_id``) are returned as ``extra`` dict.
    """
    if not os.getenv("CHATWOOT_BASE_URL") and chatwoot_cfg.get("base_url"):
        os.environ["CHATWOOT_BASE_URL"] = chatwoot_cfg["base_url"]
    if not os.getenv("CHATWOOT_API_ACCESS_TOKEN") and chatwoot_cfg.get("api_access_token"):
        os.environ["CHATWOOT_API_ACCESS_TOKEN"] = chatwoot_cfg["api_access_token"]
    if not os.getenv("CHATWOOT_WEBHOOK_SECRET") and chatwoot_cfg.get("webhook_secret"):
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = chatwoot_cfg["webhook_secret"]
    extra = {}
    for key in ("port", "path", "host", "account_id"):
        if key in chatwoot_cfg:
            extra[key] = chatwoot_cfg[key]
    return extra or None


def interactive_setup() -> None:
    """Interactive setup wizard for Chatwoot Agent Bot."""
    from hermes_cli.config import get_env_value, save_env_value
    from hermes_cli.cli_output import (
        prompt,
        prompt_yes_no,
        print_info,
        print_success,
        print_warning,
    )

    print()
    print_info("Chatwoot Agent Bot configuration")
    print()

    existing_url = get_env_value("CHATWOOT_BASE_URL")
    if existing_url:
        print_success(f"Chatwoot is already configured (URL: {existing_url})")
        if not prompt_yes_no("Reconfigure Chatwoot?", False):
            return

    base_url = prompt("  Chatwoot server URL", default=existing_url or "https://chatwoot.example.com")
    if base_url:
        save_env_value("CHATWOOT_BASE_URL", base_url.rstrip("/"))

    api_token = prompt("  API access token", password=True)
    if api_token:
        save_env_value("CHATWOOT_API_ACCESS_TOKEN", api_token)

    webhook_secret = prompt("  Webhook secret", password=True)
    if webhook_secret:
        save_env_value("CHATWOOT_WEBHOOK_SECRET", webhook_secret)

    print()
    print_success("Chatwoot Agent Bot configured!")
    print_info("Then set up the webhook in Chatwoot:")
    print_info(
        "  Settings → Account → Integrations → Webhooks → "
        "Payload URL: https://your-hermes-server/webhooks/chatwoot"
    )


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="chatwoot",
        label="Chatwoot",
        adapter_factory=_build_adapter,
        check_fn=check_chatwoot_requirements,
        is_connected=_is_connected,
        required_env=[
            "CHATWOOT_BASE_URL",
            "CHATWOOT_API_ACCESS_TOKEN",
            "CHATWOOT_WEBHOOK_SECRET",
        ],
        install_hint="pip install 'hermes-agent[aiohttp]'",
        setup_fn=interactive_setup,
        standalone_sender_fn=_standalone_send,
        emoji="💬",
        allow_update_command=True,
        apply_yaml_config_fn=_apply_yaml_config,
    )
