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
   section, the card's AXL/1 note when a kanban card is attached, and a closing `## Your seat`
   section telling the incoming chat that the work and open items are now its own.
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

## How it differs from session-relay, long-conversations and hermes-handoff

Each line about another plugin is taken from that plugin's own README, nothing more.

- **[session-relay](https://github.com/ikuro4kin-dev/hermes-session-relay)** hands a long chat to
  a fresh session when context usage crosses a threshold (45% by default). On Desktop its desktop
  half asks the agent for a hand-off note, opens a new chat with `host.newChat()` and submits the
  note with `host.composer.submit('new', note)`. On Telegram and other channels the flow is manual
  by default (`/new`, then the note rides your next message), with an opt-in watchdog that resets
  the chat through the gateway's own `/new`. It relays any chat that gets long.
- **[long-conversations](https://github.com/cspears32/hermes-long-conversations)** watches token
  use, stores a structured handoff brief (goals, files, decisions, dead ends, next step) and injects
  it into the first turn of the next session for the same chat or user, with a parent-to-child
  lineage file. It does not open the next session itself.
- **[hermes-handoff](https://github.com/yuzushi-dev/hermes-handoff)** prepares a semantic handoff
  and opens a new CLI session (`handoff-open`, a foreground interactive Hermes) or a Desktop session
  from its sidebar, in the same profile. The new session reads the handoff and waits for your
  confirmation before starting work. You start each handoff.

**This plugin** is narrower than all three, and only adds a decision on top of a handoff:

- It acts only while the agent is driving the computer (a `computer_use` call in the last 30
  minutes). Ordinary chats are left alone.
- The trigger is HandyDandy's size limit and HandyDandy's handoff file. It measures nothing itself.
- It skips the relay when the handoff says the outcome was delivered, and asks you when the handoff
  is unclear.
- Plain words in the chat ("no auto handoff") turn it off for that chat and the chats continued
  from it.
- A chain cap (2 by default) stops it after that many automatic continues of one task.

It shares no code with any of them. If you already run session-relay on Desktop, you do not need
both for ordinary long chats; this one is for unattended computer work where "keep going or stop"
matters.

## Same approval path as session-relay (catalog rule 12)

session-relay's `host.newChat()` and this plugin's `ctx.runAction('session.new')` are two doors
into the same host function. In the Hermes Desktop source (v0.23.3):

- `ctx.runAction('session.new')` runs the same handler as the New session key
  (`apps/desktop/src/app/hooks/use-keybinds.ts`, `'session.new'`), which calls
  `startFreshSession`, wired to `startFreshSessionDraft` (`apps/desktop/src/app/contrib/wiring.tsx`).
- `host.newChat()` (`apps/desktop/src/sdk/index.ts`) calls `newSessionInProfile`, which calls
  `requestFreshSession()` (`apps/desktop/src/store/profile.ts`); `useFreshSessionRequest` then runs
  the same `startFreshSessionDraft`.
- `startFreshSessionDraft` turns the draft's YOLO off (`setYoloActive(false)` in
  `apps/desktop/src/app/session/hooks/use-session-actions/index.ts`). The session is created by the
  app when the first message is sent, and YOLO is applied only if you arm it on that draft yourself.

So the continued chat asks for approval exactly where a chat you open by hand would ask.

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
| `test_mode` | `false` | One live check without waiting for a full context (below) |

Environment variables `HANDSOFF_<KEY>` override them (used in tests). The agent has no tool that
changes these settings; only you can.

## Test mode (for one live check)

Waiting for HandyDandy's real limit makes a live check slow. With `test_mode` on, you can trigger
one relay on demand:

1. Turn it on: `hermes config set plugins.entries.handsoff-handoff.settings.test_mode true`.
2. In a Desktop chat, have the agent run one `computer_use` action (a screenshot is enough). It asks
   for approval the way it normally does.
3. Type `handsoff-handoff test`. The agent writes a tiny test handoff to
   `plugin-data/handsoff-handoff/test-handoffs/` that says the work is not delivered, and the normal
   decision, opt-out, chain cap and Desktop steps run on it. The line in the chat says "(test)".
4. The new chat opens with the seed prompt and replies "handsoff-handoff test relay arrived".
5. Turn it off again: set `test_mode` to `false`.

Test mode does not skip the computer_use check, the opt-out or the chain cap, and a continued chat
that repeats the phrase does not trigger another test. With `test_mode` off the phrase does nothing.

## Disclosures

- **Reads outside its own data:** HandyDandy's `plugin-data/handydandy/state.json` (read only, to
  see whether this session's handoff was written and where) and the handoff file HandyDandy points to.
- **Writes:** only `plugin-data/handsoff-handoff/` (state.json, state.lock, events.log, and in test
  mode the agent writes `test-handoffs/<session>.md` there). Events log
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
python3 -m unittest discover -s tests          # API route test skips without FastAPI (20 tests)
uv run --no-project --python 3.12 --with fastapi python -m unittest discover -s tests
```

No real computer_use, no real session creation, no process aborts: HandyDandy state, clock, plugin
context and the broadcast are fakes.

## License

MIT
