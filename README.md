# handsoff-handoff

While the agent is driving the computer, a long session ends with a clean handoff instead of a
stall. If the work is still going, the Hermes Desktop app opens a new chat from that handoff and
keeps going. You can stop it with a sentence.

Requires [HandyDandy](https://github.com/percyhendrux123-prog/hermes-handydandy) in `handoff`
mode. HandyDandy decides when a session is too long and has the agent write the handoff. This
plugin adds the "keep going" part for hands-off (computer_use) work.

## What it does

1. **Notices the agent is driving the computer.** A `computer_use` tool call in this session within
   the last 30 minutes (setting), seen by the `post_tool_call` hook. A call through the deferred
   `tool_call` bridge counts too. Nothing is guessed.
2. **Makes the handoff say whether the job is done.** While HandyDandy's handoff instruction is in
   force for a hands-off session, the agent is also asked for one `Outcome: delivered` or
   `Outcome: not delivered` line, a `- [ ]` checkbox for every unfinished item, a `## Next action`
   section, and the card's AXL/1 note when a kanban card is attached.
3. **Decides, carefully.** It continues only when the handoff says `Outcome: not delivered` and
   names a next action, with no AXL note saying DONE, BLOCKED, REVIEW or PARKED. It counts as
   delivered only when it says `Outcome: delivered`, has no open checkboxes, and any AXL note says
   DONE. Anything else (no outcome line, conflicting lines, a blocked card) means it tells you the
   handoff is saved and asks you, instead of continuing.
4. **Continues on Desktop.** The plugin's Desktop half opens a new chat with the app's own
   **New chat** action and sends the seed prompt (`[handsoff-handoff chain=… step=…] Continue the
   work from the handoff at …`) into it, as if you had typed it. One line in the old chat tells you
   what happened, and a toast in the app confirms the new chat.
5. **Stops itself.** At most 2 automatic continues per task (setting `max_chain`), then it stops
   and tells you. Saying "no auto handoff" or "don't auto handoff" in the chat turns it off for
   that chat and the chats continued from it. "auto handoff is ok" turns it back on. The
   `auto_continue` setting turns continuing off everywhere (you still get the line).

On the terminal (CLI, TUI) and on messaging chats (Telegram and others) it does not continue. It
tells you, in one line, where the handoff is and to send `/new`, then `from handoff <path>`. The
reason is below.

## Why only Desktop continues on its own

Hermes v0.23.3 and v0.23.4 give a Python plugin no way to start a new session:

- `ctx.inject_message` only adds a turn to a session that already exists (terminal queue, Desktop/TUI
  session queue, or a messaging chat's current session).
- On the messaging gateway an injected `/new` is deliberately not run as a command
  (`allow_gateway_control=False` in `gateway/run_plugin_injection.py`), so a plugin cannot reset a chat.
- `session.create` / `prompt.submit` (gateway local sessions) need a server-authenticated client
  identity with `session:create`. A plugin has none, and borrowing one would route around the host.
- v0.23.4 adds `turn_client()`, `session_viewers()` and `on_session_prompt` hooks. None of them
  start a session.

The Desktop plugin SDK does have a host-owned path: `ctx.runAction('session.new')` (the app's own
New chat) and `host.composer.submit('new', text)` (send in the fresh chat, same authority as
typing). Because the Desktop app itself opens the chat, it gets the same settings and approval
behaviour as any chat you open. The only other way would be launching a Hermes process ourselves,
which this plugin never does (catalog rule 12).

## Approvals

Unchanged. The plugin never answers, skips or pre-approves anything, never sets YOLO, and never
starts a process. In the continued chat, `computer_use` and every other gated action asks exactly
where a new chat you opened would ask. One difference to know: approvals you granted "for this
session" in the old chat do not carry over, so the new chat may ask again. That is stricter, never
looser. Unattended runs (cron, kanban workers) never continue.

## How it differs from hermes-handoff (yuzushi-dev)

[hermes-handoff](https://github.com/yuzushi-dev/hermes-handoff) is a manual tool: you prepare a
handoff and open it in a new CLI session (its `handoff-open` command starts a foreground,
interactive Hermes with YOLO and non-interactive flags removed) or from a Desktop sidebar, and the
new session waits for your confirmation. This plugin is automatic and narrower: it fires only at
HandyDandy's size limit, only for computer_use sessions, decides on its own whether the work is
still going, continues without a confirmation step (bounded by the chain limit and your opt-out),
and starts no process at all. No code is shared.

## Install (not done for you)

This repo is local only. To try it: copy or link the folder to `~/.hermes/plugins/handsoff-handoff`,
add `handsoff-handoff` to `plugins.enabled` in `config.yaml` (that also turns on the small backend
the Desktop half talks to), restart Hermes, then enable **Handsoff Handoff** in Desktop
**Capabilities → Plugins** (the Desktop half is off until you turn it on). HandyDandy must be
enabled with `mode: handoff`.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Whole plugin on/off |
| `auto_continue` | `true` | On Desktop, open the next chat when the work is still going |
| `active_window_minutes` | `30` | How recent a computer_use call must be |
| `max_chain` | `2` | Automatic continues per task before it stops and tells you |

Environment variables `HANDSOFF_<KEY>` override them (used in tests).

## Disclosures

- **Reads outside its own data:** HandyDandy's `plugin-data/handydandy/state.json` (read only, to
  see whether this session's handoff was written and where) and the handoff file HandyDandy points to.
- **Writes:** only `plugin-data/handsoff-handoff/` (state.json, state.lock, events.log). Events log
  session ids, decisions and handoff paths, never handoff contents.
- **Backend routes:** `GET /api/plugins/handsoff-handoff/pending`, `POST /claim`, `POST /result`,
  behind the dashboard's normal auth gate. They only read and update this plugin's state.
- **Desktop actions:** opens a new chat (`session.new`) and sends one prompt in it. Only the window
  showing the original chat acts, and only once (an atomic claim). A waiting continuation expires
  after 15 minutes.
- **Messages:** posts one line in the session where the handoff was written. It messages no one else.
- **No** network calls, shell commands, subprocesses, background services, credentials or telemetry.
- **Not verified live yet:** the Desktop half has not run in a real Desktop window; the session id
  the agent sees is matched against the window's stored and runtime ids, which needs a live check.

## Tests

```bash
python3 -m unittest discover -s tests          # API route test skips without FastAPI
uv run --no-project --python 3.12 --with fastapi python -m unittest discover -s tests
```

No real computer_use, no real session creation, no process aborts: HandyDandy state, clock, plugin
context and the broadcast are fakes.

## License

MIT
