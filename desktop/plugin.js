// handsoff-handoff, Desktop half.
//
// When the agent half queues a continuation (the agent was driving the computer, HandyDandy wrote
// the handoff, the work is still going), the window that shows that chat:
//   1. claims it from this plugin's own backend (exactly one window wins),
//   2. opens a new chat with the app's own "New chat" action (ctx.runAction('session.new')),
//   3. types the seed prompt into that new chat's composer and sends it, as the user would.
// The new chat is created by the Desktop app with its normal settings, so approvals behave as in
// any chat the user opens. Nothing here answers an approval, starts a process, or picks a model.
import { host } from '@hermes/plugin-sdk'

const ID = 'handsoff-handoff'
const sleep = ms => new Promise(r => setTimeout(r, ms))
const busy = new Set()

function shownIds() {
  const ids = []
  for (const atom of [host.state.focusedStoredSessionId, host.state.focusedSessionId, host.state.activeSessionId]) {
    try {
      const v = atom && atom.get()
      if (v) ids.push(v)
    } catch (_) {}
  }
  return ids
}

async function rest(ctx, path, opts) {
  try {
    return await ctx.rest(path, opts)
  } catch (_) {
    return null
  }
}

async function run(ctx, id) {
  if (!id || busy.has(id)) return
  busy.add(id)
  try {
    const got = await rest(ctx, '/claim', { method: 'POST', body: { id } })
    if (!got || !got.ok || typeof got.prompt !== 'string') return // another window has it, or it expired
    const opened = ctx.runAction('session.new')
    if (!opened || !opened.ok) {
      await rest(ctx, '/result', { method: 'POST', body: { id, ok: false, detail: `session.new: ${opened && opened.reason}` } })
      host.notify({ kind: 'warning', message: 'Could not open a new chat. The handoff is saved.', detail: got.handoff_path })
      return
    }
    // The fresh draft answers to 'new' once the primary pane shows it.
    let sent = false
    for (let i = 0; i < 30 && !sent; i++) {
      await sleep(150)
      try { sent = host.composer.submit('new', got.prompt) === true } catch (_) { sent = false }
    }
    await rest(ctx, '/result', { method: 'POST', body: { id, ok: sent, detail: sent ? 'submitted' : 'no new-chat composer answered' } })
    host.notify(sent
      ? { kind: 'info', message: `Continued in a new chat (automatic continue ${got.step} of ${got.max_chain}).`, detail: got.handoff_path }
      : { kind: 'warning', message: 'New chat opened, but the handoff prompt was not sent. Paste: from handoff', detail: got.handoff_path })
  } finally {
    busy.delete(id)
  }
}

async function sweep(ctx) {
  const ids = shownIds()
  if (!ids.length) return
  const q = `/pending?session=${encodeURIComponent(ids[0])}${ids[1] ? `&alt=${encodeURIComponent(ids[1])}` : ''}`
  const res = await rest(ctx, q)
  for (const item of (res && Array.isArray(res.items) ? res.items : [])) await run(ctx, item.id)
}

export default {
  id: ID,
  name: 'Handsoff Handoff',
  register(ctx) {
    // Push: the agent half broadcasts plugin.handsoff-handoff.continue with {id, session_id}.
    ctx.onEvent(`plugin.${ID}.continue`, ({ payload }) => {
      if (!payload || typeof payload.id !== 'string') return
      if (!shownIds().includes(payload.session_id)) return // only the window showing that chat acts
      void run(ctx, payload.id)
    })
    // Pull: a chat opened later (or a missed event) picks up its waiting continuation.
    try {
      const stop = host.state.focusedStoredSessionId.subscribe(() => { void sweep(ctx) })
      if (typeof stop === 'function' && ctx.onDispose) ctx.onDispose(stop)
    } catch (_) {}
  }
}
