"""`Output` that delivers a Response back to Slack via `chat.postMessage`."""

from __future__ import annotations

import os
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient
from waken import Event, Response


class SlackOutput:
    """Posts `response.text` back to the channel/thread an `Event` came from.

    Not inherited from: `waken.Output` is a `Protocol`; implementing
    `deliver()` is enough to satisfy it structurally.

    Expects `event.metadata["channel"]` (required) and
    `event.metadata["thread_ts"]` (optional) -- exactly what `SlackSource`
    stashes on every `Event` it dispatches. Using `SlackOutput` with events
    from a different `Source` requires populating the same metadata keys.

    `response.files` isn't uploaded here -- Slack's file upload API
    (`files.getUploadURLExternal` + `files.completeUploadExternal`) is a
    multi-step flow with its own semantics; out of scope for this initial
    version. `response.text` is the only field delivered.
    """

    def __init__(self, bot_token: str | None = None, **client_kwargs: Any) -> None:
        self._client = AsyncWebClient(
            token=bot_token or os.environ["SLACK_BOT_TOKEN"], **client_kwargs
        )

    async def deliver(self, event: Event, response: Response) -> None:
        if not response.text:
            return
        await self._client.chat_postMessage(
            channel=event.metadata["channel"],
            thread_ts=event.metadata.get("thread_ts"),
            text=response.text,
        )
