"""Socket Mode `Source`: turns incoming Slack messages into waken `Event`s.

See the package README's "Design notes" section for why this uses Slack's
Socket Mode (a persistent, app-token-authenticated WebSocket) instead of
routing through core's `waken.plugins.sources.webhook.WebhookSource` /
`POST /webhook/{name}`: that route only hands its parser callback the parsed
JSON body (see `waken.server.create_app`'s `webhook()` handler), not request
headers, and Slack's HTTP Events API needs the raw `X-Slack-Signature` /
`X-Slack-Request-Timestamp` headers to verify a request's HMAC signature.
There is currently no way to do that verification through `WebhookSource`.
Socket Mode sidesteps the problem entirely -- the app-level token itself is
the authentication, so no public endpoint or signature check is needed.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient
from waken import Event

if TYPE_CHECKING:
    from waken.runtime import Runtime

# `message` event subtypes that are never a genuine new message typed by a
# human and must not be dispatched. `bot_message` is listed for completeness
# but the real loop-prevention guard is the `bot_id` check in
# `_handle_request` below -- a reply posted by `SlackOutput` (via the bot
# token) always carries `bot_id`, subtype or not.
_IGNORED_SUBTYPES = frozenset(
    {
        "bot_message",
        "message_changed",
        "message_deleted",
        "message_replied",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
    }
)


class SlackSource:
    """Connects to Slack over Socket Mode and dispatches `message` events.

    Not inherited from: `waken.Source` is a `Protocol`, and implementing
    `start()`/`stop()` is enough to satisfy it structurally.

    Args:
        target: name of the `waken` Target every incoming Slack message is
            routed to (e.g. `"claude"`).
        app_token: Slack app-level token (`xapp-...`, needs the
            `connections:write` scope). Defaults to `$SLACK_APP_TOKEN`.
        bot_token: Slack bot token (`xoxb-...`). Defaults to
            `$SLACK_BOT_TOKEN`. Only used to authenticate the
            `apps.connections.open` call that mints a Socket Mode URL --
            `SlackSource` never posts messages itself, that's `SlackOutput`'s
            job.
        source_name: the `Event.source` (and `runtime.session()` source key)
            every dispatched event carries. Defaults to `"slack"`.
    """

    def __init__(
        self,
        target: str,
        *,
        app_token: str | None = None,
        bot_token: str | None = None,
        source_name: str = "slack",
    ) -> None:
        self.target = target
        self.source_name = source_name
        self._app_token = app_token or os.environ["SLACK_APP_TOKEN"]
        self._bot_token = bot_token or os.environ["SLACK_BOT_TOKEN"]
        self._runtime: Runtime | None = None
        self._client: SocketModeClient | None = None

    async def start(self, runtime: Runtime) -> None:
        self._runtime = runtime
        self._client = SocketModeClient(
            app_token=self._app_token,
            web_client=AsyncWebClient(token=self._bot_token),
        )
        self._client.socket_mode_request_listeners.append(self._handle_request)
        # `connect()` opens the WebSocket and schedules its own background
        # receive-loop task, then returns -- it does not block forever (that
        # would be `AsyncSocketModeHandler.start_async()`'s job, one layer up
        # in slack_bolt, which this package doesn't depend on). No extra
        # `asyncio.create_task()` wrapping is needed here.
        # `slack_sdk` ships a `py.typed` marker but leaves several of its own
        # methods unannotated (including this one), which mypy --strict
        # flags as a call to an untyped function from typed code.
        await self._client.connect()  # type: ignore[no-untyped-call]

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()  # type: ignore[no-untyped-call]
            self._client = None
        self._runtime = None

    async def _handle_request(
        self, client: AsyncBaseSocketModeClient, req: SocketModeRequest
    ) -> None:
        # Acknowledge every envelope immediately, regardless of type or
        # outcome -- Slack resends an unacknowledged Socket Mode envelope
        # after ~3 seconds, and dispatch to a Target can easily take longer
        # than that.
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        if req.type != "events_api":
            return  # interactive components / slash commands: not handled here

        event: dict[str, Any] = req.payload.get("event", {})
        if event.get("type") != "message":
            return
        if event.get("bot_id") is not None:
            return  # our own reply (or any other bot's message) -- avoid echo loops
        if event.get("subtype") in _IGNORED_SUBTYPES:
            return

        text = event.get("text", "")
        channel = event["channel"]
        # Reply in-thread if this message is already part of one; otherwise
        # start a new thread rooted at this message's own timestamp, so a
        # follow-up in the same thread resumes the same waken session.
        thread_ts = event.get("thread_ts") or event["ts"]

        runtime = self._runtime
        assert runtime is not None  # start() always runs before dispatch
        waken_event = Event(
            source=self.source_name,
            target=self.target,
            payload={"prompt": text},
            session_id=runtime.session(
                self.source_name, external_key=f"{channel}:{thread_ts}"
            ),
            metadata={"channel": channel, "thread_ts": thread_ts},
        )
        # Fire-and-forget, same reasoning as core's WebhookSource/
        # FilesystemSource: nothing here is a synchronous caller waiting on
        # the Response, so failures are queued/retried by the runtime
        # (`retry=True`) instead of propagating to this listener.
        asyncio.create_task(runtime.dispatch(waken_event, retry=True))
