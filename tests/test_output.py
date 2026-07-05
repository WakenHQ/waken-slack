"""Unit tests for SlackOutput. `slack_sdk.web.async_client.AsyncWebClient` is
fully faked out -- no network access and no Slack token needed."""

from __future__ import annotations

from typing import Any

import pytest
from waken import Event, Output, Response

from waken_slack import SlackOutput
from waken_slack import output as output_module


class _FakeAsyncWebClient:
    """Stands in for `slack_sdk.web.async_client.AsyncWebClient`."""

    def __init__(self, *, token: str, **kwargs: Any) -> None:
        self.token = token
        self.init_kwargs = kwargs
        self.calls: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True}


@pytest.fixture(autouse=True)
def _patch_async_web_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output_module, "AsyncWebClient", _FakeAsyncWebClient)


def test_slack_output_satisfies_output_protocol() -> None:
    output = SlackOutput(bot_token="xoxb-t")
    assert isinstance(output, Output)


def test_client_constructed_with_bot_token_and_forwards_kwargs() -> None:
    output = SlackOutput(bot_token="xoxb-t", proxy="http://proxy.local")

    client = output._client
    assert isinstance(client, _FakeAsyncWebClient)
    assert client.token == "xoxb-t"
    assert client.init_kwargs == {"proxy": "http://proxy.local"}


def test_bot_token_falls_back_to_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")

    output = SlackOutput()

    assert output._client.token == "xoxb-from-env"  # type: ignore[attr-defined]


def test_missing_bot_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    with pytest.raises(KeyError):
        SlackOutput()


async def test_deliver_posts_text_to_channel_and_thread() -> None:
    output = SlackOutput(bot_token="xoxb-t")
    client = output._client
    assert isinstance(client, _FakeAsyncWebClient)

    event = Event(
        source="slack",
        target="claude",
        payload={"prompt": "hi"},
        metadata={"channel": "C123", "thread_ts": "111.222"},
    )
    response = Response(text="Hello back!")

    await output.deliver(event, response)

    assert client.calls == [
        {"channel": "C123", "thread_ts": "111.222", "text": "Hello back!"}
    ]


async def test_deliver_without_thread_ts_passes_none() -> None:
    output = SlackOutput(bot_token="xoxb-t")
    client = output._client
    assert isinstance(client, _FakeAsyncWebClient)

    event = Event(
        source="slack",
        target="claude",
        payload={"prompt": "hi"},
        metadata={"channel": "C123"},
    )
    response = Response(text="Hello back!")

    await output.deliver(event, response)

    assert client.calls == [
        {"channel": "C123", "thread_ts": None, "text": "Hello back!"}
    ]


@pytest.mark.parametrize("text", [None, ""])
async def test_deliver_skips_when_text_is_empty(text: str | None) -> None:
    output = SlackOutput(bot_token="xoxb-t")
    client = output._client
    assert isinstance(client, _FakeAsyncWebClient)

    event = Event(
        source="slack",
        target="claude",
        payload={"prompt": "hi"},
        metadata={"channel": "C123", "thread_ts": "111.222"},
    )
    response = Response(text=text)

    await output.deliver(event, response)

    assert client.calls == []
