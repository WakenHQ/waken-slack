"""Unit tests for SlackSource. `slack_sdk.socket_mode.aiohttp.SocketModeClient`
and `slack_sdk.web.async_client.AsyncWebClient` are fully faked out -- no
network access, no real WebSocket connection, and no Slack tokens needed."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from waken import Event, Response, Source

from waken_slack import SlackSource
from waken_slack import source as source_module


def test_slack_source_satisfies_source_protocol() -> None:
    source = SlackSource(target="claude", app_token="xapp-t", bot_token="xoxb-t")
    assert isinstance(source, Source)


class _FakeSocketModeClient:
    """Stands in for `slack_sdk.socket_mode.aiohttp.SocketModeClient`."""

    def __init__(self, *, app_token: str, web_client: Any) -> None:
        self.app_token = app_token
        self.web_client = web_client
        self.socket_mode_request_listeners: list[Any] = []
        self.connected = False
        self.closed = False
        self.sent_responses: list[SocketModeResponse] = []

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.sent_responses.append(response)


class _FakeAsyncWebClient:
    """Stands in for `slack_sdk.web.async_client.AsyncWebClient`."""

    def __init__(self, *, token: str) -> None:
        self.token = token


class _FakeRuntime:
    """Stands in for `waken.Runtime`: records dispatched Events, mints
    deterministic session ids keyed by (source, external_key)."""

    def __init__(self) -> None:
        self.dispatched: list[Event] = []
        self._sessions: dict[tuple[str, str], str] = {}

    def session(self, source: str, external_key: str) -> str:
        key = (source, external_key)
        if key not in self._sessions:
            self._sessions[key] = f"session-{len(self._sessions)}"
        return self._sessions[key]

    async def dispatch(self, event: Event, *, retry: bool = False) -> Response:
        self.dispatched.append(event)
        return Response(text="ok")


@pytest.fixture(autouse=True)
def _patch_slack_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_module, "SocketModeClient", _FakeSocketModeClient)
    monkeypatch.setattr(source_module, "AsyncWebClient", _FakeAsyncWebClient)


def _make_source() -> SlackSource:
    return SlackSource(target="claude", app_token="xapp-t", bot_token="xoxb-t")


def _events_api_request(event: dict[str, Any]) -> SocketModeRequest:
    return SocketModeRequest(
        type="events_api",
        envelope_id="envelope-1",
        payload={"team_id": "T1", "event": event},
    )


async def test_start_connects_and_registers_listener() -> None:
    source = _make_source()
    runtime = _FakeRuntime()

    await source.start(runtime)  # type: ignore[arg-type]

    client = source._client
    assert isinstance(client, _FakeSocketModeClient)
    assert client.connected is True
    assert client.app_token == "xapp-t"
    assert client.web_client.token == "xoxb-t"
    assert source._handle_request in client.socket_mode_request_listeners


async def test_stop_closes_the_client() -> None:
    source = _make_source()
    runtime = _FakeRuntime()
    await source.start(runtime)  # type: ignore[arg-type]
    client = source._client
    assert isinstance(client, _FakeSocketModeClient)

    await source.stop()

    assert client.closed is True
    assert source._client is None


async def test_genuine_message_dispatches_the_right_event() -> None:
    source = _make_source()
    runtime = _FakeRuntime()
    await source.start(runtime)  # type: ignore[arg-type]
    client = source._client
    assert isinstance(client, _FakeSocketModeClient)

    req = _events_api_request(
        {
            "type": "message",
            "channel": "C123",
            "user": "U1",
            "text": "hello there",
            "ts": "111.222",
        }
    )
    await source._handle_request(client, req)  # type: ignore[arg-type]
    await asyncio.sleep(0)  # let the fire-and-forget dispatch task run

    assert len(runtime.dispatched) == 1
    event = runtime.dispatched[0]
    assert event.source == "slack"
    assert event.target == "claude"
    assert event.payload == {"prompt": "hello there"}
    assert event.session_id == runtime.session("slack", external_key="C123:111.222")
    assert event.metadata == {"channel": "C123", "thread_ts": "111.222"}

    # The envelope must always be acknowledged, regardless of outcome.
    assert len(client.sent_responses) == 1
    assert client.sent_responses[0].envelope_id == "envelope-1"


async def test_thread_reply_uses_thread_ts_not_own_ts() -> None:
    source = _make_source()
    runtime = _FakeRuntime()
    await source.start(runtime)  # type: ignore[arg-type]
    client = source._client
    assert isinstance(client, _FakeSocketModeClient)

    req = _events_api_request(
        {
            "type": "message",
            "channel": "C123",
            "user": "U1",
            "text": "a reply",
            "ts": "333.444",
            "thread_ts": "111.222",
        }
    )
    await source._handle_request(client, req)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert len(runtime.dispatched) == 1
    event = runtime.dispatched[0]
    assert event.metadata == {"channel": "C123", "thread_ts": "111.222"}
    assert event.session_id == runtime.session("slack", external_key="C123:111.222")


@pytest.mark.parametrize(
    "event",
    [
        # A bot's message (including SlackOutput's own replies): must be
        # ignored to avoid an infinite reply loop.
        {
            "type": "message",
            "channel": "C123",
            "bot_id": "B999",
            "text": "I am a bot",
            "ts": "1.1",
        },
        # A message edit.
        {
            "type": "message",
            "channel": "C123",
            "user": "U1",
            "text": "edited",
            "ts": "1.1",
            "subtype": "message_changed",
        },
        # A message deletion.
        {
            "type": "message",
            "channel": "C123",
            "subtype": "message_deleted",
            "ts": "1.1",
        },
        # A channel-join notice.
        {
            "type": "message",
            "channel": "C123",
            "user": "U1",
            "subtype": "channel_join",
            "ts": "1.1",
        },
    ],
)
async def test_non_genuine_messages_are_not_dispatched(event: dict[str, Any]) -> None:
    source = _make_source()
    runtime = _FakeRuntime()
    await source.start(runtime)  # type: ignore[arg-type]
    client = source._client
    assert isinstance(client, _FakeSocketModeClient)

    req = _events_api_request(event)
    await source._handle_request(client, req)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert runtime.dispatched == []
    # Still acknowledged even when filtered out.
    assert len(client.sent_responses) == 1


async def test_non_message_event_type_is_ignored() -> None:
    source = _make_source()
    runtime = _FakeRuntime()
    await source.start(runtime)  # type: ignore[arg-type]
    client = source._client
    assert isinstance(client, _FakeSocketModeClient)

    req = _events_api_request({"type": "reaction_added", "user": "U1"})
    await source._handle_request(client, req)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert runtime.dispatched == []


async def test_non_events_api_request_is_acknowledged_but_ignored() -> None:
    source = _make_source()
    runtime = _FakeRuntime()
    await source.start(runtime)  # type: ignore[arg-type]
    client = source._client
    assert isinstance(client, _FakeSocketModeClient)

    req = SocketModeRequest(
        type="interactive", envelope_id="envelope-2", payload={"type": "block_actions"}
    )
    await source._handle_request(client, req)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert runtime.dispatched == []
    assert len(client.sent_responses) == 1
    assert client.sent_responses[0].envelope_id == "envelope-2"


def test_tokens_fall_back_to_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-from-env")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")

    source = SlackSource(target="claude")

    assert source._app_token == "xapp-from-env"
    assert source._bot_token == "xoxb-from-env"


def test_missing_app_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")

    with pytest.raises(KeyError):
        SlackSource(target="claude")
