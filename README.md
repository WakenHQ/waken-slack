# waken-slack

[![CI](https://github.com/WakenHQ/waken-slack/actions/workflows/ci.yml/badge.svg)](https://github.com/WakenHQ/waken-slack/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey)](https://github.com/WakenHQ/waken-slack/blob/main/LICENSE)

Slack `Source`/`Output` for [Waken](https://github.com/WakenHQ/waken) --
"nginx for AI agents." Unlike a `Target` adapter (`waken-claude`,
`waken-gemini`, ...), Slack here is a *channel*: `SlackSource` turns incoming
Slack messages into waken `Event`s, and `SlackOutput` posts a `Target`'s
`Response` back to the channel/thread it came from.

## Install

```bash
pip install waken-slack
```

Needs a Slack app with **Socket Mode enabled**, and two tokens -- unlike most
sibling adapters, which need only one:

- `SLACK_APP_TOKEN` -- app-level token, starts `xapp-`, needs the
  `connections:write` scope. Used only to open the Socket Mode connection.
- `SLACK_BOT_TOKEN` -- bot token, starts `xoxb-`. Used to post messages
  (`SlackOutput`) and to authenticate the Socket Mode client (`SlackSource`).

The app also needs the `message.channels` (or `message.groups`/`message.im`,
depending on where it should listen) Events API subscription turned on, even
though delivery happens over the socket rather than HTTP.

## Usage

```python
from waken import Runtime
from waken_claude import ClaudeAdapter
from waken_slack import SlackOutput, SlackSource

runtime = Runtime()
runtime.target("claude", ClaudeAdapter())
runtime.source("slack", SlackSource(target="claude"))
runtime.output("slack", SlackOutput())
runtime.run()
```

`SlackSource` resolves `SLACK_APP_TOKEN`/`SLACK_BOT_TOKEN` from the
environment by default, or accepts them as constructor arguments. Every
incoming message becomes an `Event(source="slack", target="claude",
payload={"prompt": text}, session_id=runtime.session("slack",
external_key=f"{channel}:{thread_ts}"), metadata={"channel": ...,
"thread_ts": ...})` -- replies in the same Slack thread resume the same
waken session. `SlackOutput` reads `event.metadata["channel"]`/`["thread_ts"]`
back off to know where to post the reply.

Bot messages (including `SlackOutput`'s own replies) and non-message
subtypes (edits, deletes, channel-join notices, ...) are filtered out before
dispatch -- otherwise a reply posted back into Slack would re-trigger
`SlackSource` and loop forever.

## Design notes

**Socket Mode, not the HTTP Events API.** `waken`'s built-in
`WebhookSource` (`POST /webhook/{name}`) exists for exactly this kind of
integration, but its route only passes the parsed JSON *body* to a parser
callback -- no request headers. Slack's HTTP Events API needs the raw
`X-Slack-Signature`/`X-Slack-Request-Timestamp` headers to verify a
request's HMAC signature, which is therefore not currently possible through
`WebhookSource`. This is a real gap in `waken` core (worth fixing for any
future HTTP-webhook integration that needs header access), not something
this package works around.

Socket Mode sidesteps it entirely: a persistent WebSocket, authenticated by
the app-level token, needs no public endpoint and no signature
verification -- the connection itself is the auth. This also matches the
`Source` protocol's own documented flexibility (`start()`/`stop()` own "a
socket, a poll timer, a subprocess" between them).

**`slack_sdk` directly, not `slack_bolt`.** `SlackSource` talks to
`slack_sdk.socket_mode.aiohttp.SocketModeClient` directly rather than going
through `slack_bolt`'s `App`/`AsyncSocketModeHandler`. `slack_bolt` brings a
full listener/middleware/OAuth/multi-workspace framework this package has no
use for -- one `message` event listener and an acknowledgement is all Socket
Mode strictly requires.

## Development

```bash
git clone https://github.com/WakenHQ/waken-slack
cd waken-slack
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Tests mock the Slack SDK entirely (`SocketModeClient`, `AsyncWebClient`) --
no real network access, WebSocket connection, or Slack tokens needed to run
the suite.

## License

[MIT](https://github.com/WakenHQ/waken-slack/blob/main/LICENSE)
