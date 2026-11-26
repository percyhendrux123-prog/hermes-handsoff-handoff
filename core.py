"""handsoff-handoff core: pure logic, no Hermes imports, unit-tested on its own.

What it decides, in order:
1. Is the agent driving the computer? A ``computer_use`` tool call in this session within
   ``active_window_minutes`` (seen by the post_tool_call hook), never a guess.
2. Did HandyDandy (handoff mode) write this session's handoff this turn? Read-only look at
   HandyDandy's own state record for the session.
3. Is the work still going? ``decide()`` reads the handoff conservatively. Anything short of a
   clear "not delivered, here is the next action" is ``ask``.
4. May we continue? Not if the user said "no auto handoff" in this session, not if the setting is
   off, not past ``max_chain`` automatic continues for the same task, and only on a surface where
   the host itself can open the new chat (Desktop, through the desktop half).

The engine never starts a process, never creates a session itself and never answers an approval.
On Desktop it queues one continuation and lets the Desktop app open the new chat the way the user's
own "New chat" does. Everywhere else it tells the user the handoff is ready and asks.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

PLUGIN_ID = "handsoff-handoff"
MARK = "[handsoff-handoff]"
COMPUTER_TOOLS = ("computer_use",)
AUTO_SURFACES = ("desktop",)  # surfaces whose host can open a new chat for us
UNATTENDED = ("cron", "kanban", "batch", "subagent")
PENDING_TTL_S = 15 * 60

SEED_RE = re.compile(r"\[handsoff-handoff chain=([A-Za-z0-9_.:-]{1,80}) step=(\d{1,3})\]")
OPT_OUT_RE = re.compile(
    r"\b(?:do\s*n[o']?t|don'?t|dont|no|never|stop|without)\s+(?:the\s+|any\s+)?"
    r"auto(?:matic(?:ally)?)?[\s-]*(?:hand[\s-]?offs?|continu(?:e|ing|ation))\b",
    re.I,
)
OPT_IN_RE = re.compile(
    r"\b(?:auto(?:matic)?[\s-]*hand[\s-]?off\s+(?:is\s+)?(?:ok|okay|on|fine|allowed)|"
    r"(?:turn\s+on|resume|allow)\s+auto(?:matic)?[\s-]*hand[\s-]?offs?)\b",
    re.I,
)
OUTCOME_RE = re.compile(r"^\s*(?:[-*]\s*)?\**outcome\**\s*:\s*\**\s*(not\s+delivered|delivered)\b", re.I | re.M)
OPEN_TODO_RE = re.compile(r"^\s*[-*]\s*\[\s\]\s+\S", re.M)
AXL_RE = re.compile(r"^AXL/1\s+\S+\s+·\s+(READY|RUNNING|BLOCKED|REVIEW|DONE|PARKED)\s+·", re.M)
NEXT_RE = re.compile(r"^#{1,6}\s*next\s+action\b.*?$\n(?P<body>(?:(?!^#{1,6}\s).*\n?)*)", re.I | re.M)


def _as_int(v: Any, d: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _as_bool(v: Any, d: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("1", "true", "yes", "on"):
            return True
        if t in ("0", "false", "no", "off"):
            return False
    return d


@dataclass
class Settings:
    enabled: bool = True
    auto_continue: bool = True
    active_window_minutes: int = 30
    max_chain: int = 2

    @classmethod
    def from_getter(cls, getter: Callable[[str, Any], Any]) -> "Settings":
        d = cls()

        def get(key: str, default: Any) -> Any:
            env = os.environ.get(f"HANDSOFF_{key.upper()}")
            return env if env is not None else getter(key, default)

        return cls(
            enabled=_as_bool(get("enabled", d.enabled), d.enabled),
            auto_continue=_as_bool(get("auto_continue", d.auto_continue), d.auto_continue),
            active_window_minutes=max(1, _as_int(get("active_window_minutes", d.active_window_minutes),
                                                 d.active_window_minutes)),
            max_chain=max(0, _as_int(get("max_chain", d.max_chain), d.max_chain)),
        )


# ---------------------------------------------------------------- reading the chat and the handoff
def is_computer_call(tool_name: str, args: Any) -> bool:
    """A direct computer_use call, or one made through the deferred-tool bridge (``tool_call``)."""
    if tool_name in COMPUTER_TOOLS:
        return True
    if tool_name == "tool_call" and isinstance(args, dict):
        calls = args.get("calls")
        if isinstance(calls, list):
            return any(isinstance(c, dict) and c.get("name") in COMPUTER_TOOLS for c in calls)
    return False


def opt_signal(text: str) -> Optional[bool]:
    """False = the user said not to auto-handoff; True = they turned it back on; None = neither."""
    if not isinstance(text, str) or not text:
        return None
    if OPT_OUT_RE.search(text):
        return False
    if OPT_IN_RE.search(text):
        return True
    return None


def seed_marker(text: str) -> Optional[tuple[str, int]]:
    if not isinstance(text, str):
        return None
    m = SEED_RE.search(text)
    return (m.group(1), int(m.group(2))) if m else None


@dataclass
class Decision:
    verdict: str  # "continue" | "delivered" | "ask"
    reasons: list[str] = field(default_factory=list)


def decide(handoff: str) -> Decision:
    """Conservative read of a handoff. Only a clear "not delivered" plus a next action continues."""
    if not isinstance(handoff, str) or not handoff.strip():
        return Decision("ask", ["handoff is empty or unreadable"])
    outcomes = {m.group(1).lower().replace("  ", " ") for m in OUTCOME_RE.finditer(handoff)}
    outcomes = {"not delivered" if o.startswith("not") else "delivered" for o in outcomes}
    todos = len(OPEN_TODO_RE.findall(handoff))
    axl = [m.group(1) for m in AXL_RE.finditer(handoff)]
    nxt = NEXT_RE.search(handoff)
    has_next = bool(nxt and nxt.group("body").strip())
    if len(outcomes) != 1:
        return Decision("ask", ["no single Outcome line" if not outcomes else "conflicting Outcome lines"])
    outcome = outcomes.pop()
    if axl and any(s in ("BLOCKED", "REVIEW", "PARKED") for s in axl):
        return Decision("ask", [f"AXL note says {','.join(sorted(set(axl)))}: a person is needed"])
    if outcome == "delivered":
        if todos:
            return Decision("ask", [f"Outcome says delivered but {todos} open item(s) remain"])
        if axl and any(s != "DONE" for s in axl):
            return Decision("ask", ["Outcome says delivered but the AXL note is not DONE"])
        return Decision("delivered", ["Outcome: delivered, no open items"])
    # not delivered
    if axl and all(s == "DONE" for s in axl):
        return Decision("ask", ["Outcome says not delivered but the AXL note says DONE"])
    if not has_next:
        return Decision("ask", ["not delivered, but the handoff names no next action"])
    return Decision("continue", [f"Outcome: not delivered; {todos} open item(s); next action present"])


def handoff_addendum(chain_step: int, max_chain: int) -> str:
    """Extra lines asked of the agent while HandyDandy's handoff instruction is in force."""
    return (
        f"{MARK} The agent was driving the computer in this session, so this handoff may be "
        "continued automatically in a fresh chat. In the handoff file add, near the top, exactly one "
        "line `Outcome: delivered` or `Outcome: not delivered` (delivered only if the user's requested "
        "result exists and you have proof of it), list every unfinished item as a `- [ ] ` checkbox, "
        "and keep a `## Next action` section. If a kanban card is attached, include its AXL/1 note. "
        f"This would be automatic continue {chain_step} of at most {max_chain}."
    )


def seed_prompt(chain: str, step: int, handoff_path: str) -> str:
    return (
        f"[handsoff-handoff chain={chain} step={step}] Continue the work from the handoff at "
        f"{handoff_path}. Read it first and follow its standing orders. Approvals work exactly as "
        "usual in this chat: computer_use and every other gated action still asks where it asked "
        "before. If the outcome turns out to be delivered already, say so and stop."
    )


# ---------------------------------------------------------------- state
class Store:
    """Cross-process JSON state guarded by flock, like HandyDandy's (fail closed when unreadable)."""

    LOCK_WAIT_S = 5.0

    def __init__(self, data_dir: Path, clock: Callable[[], float] = time.time):
        self.dir = Path(data_dir)
        self.file = self.dir / "state.json"
        self.lockf = self.dir / "state.lock"
        self.logf = self.dir / "events.log"
        self.clock = clock
        self.lock = threading.RLock()

    def _read(self) -> Optional[dict]:
        try:
            raw = json.loads(self.file.read_text())
        except FileNotFoundError:
            return {"sessions": {}, "chains": {}, "pending": {}}
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        for k in ("sessions", "chains", "pending"):
            if not isinstance(raw.setdefault(k, {}), dict):
                return None
        return raw

    def _write(self, data: dict) -> bool:
        try:
            sessions = sorted(data["sessions"].items(), key=lambda kv: kv[1].get("seen", 0))[-300:]
            data["sessions"] = dict(sessions)
            now = self.clock()
            data["pending"] = {k: v for k, v in data["pending"].items()
                               if now - v.get("created", 0) < 24 * 3600}
            tmp = self.file.with_name(f".state.{os.getpid()}.{threading.get_ident()}.tmp")
            with tmp.open("w") as f:
                json.dump(data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.file)
            return True
        except Exception:
            return False

    @contextmanager
    def txn(self):
        """Yields the state dict (mutate it) or None when locked out / unreadable. Saved on exit."""
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            fh = open(self.lockf, "a+")
            got = False
            try:
                deadline = time.monotonic() + self.LOCK_WAIT_S
                while True:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        got = True
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            break
                        time.sleep(0.05)
                data = self._read() if got else None
                if got and data is None:
                    self.log("state_unreadable", fix=f"inspect or delete {self.file}")
                yield data
                if data is not None:
                    self._write(data)
            finally:
                if got:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()

    def log(self, event: str, **fields: Any) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
            with self.logf.open("a") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception:
            pass

    # -- pending continuations (the Desktop half claims them) --
    def pending_for(self, session_ids: list[str]) -> list[dict]:
        now = self.clock()
        with self.txn() as st:
            if st is None:
                return []
            return [dict(id=k, **{x: v[x] for x in ("session_id", "handoff_path", "step", "max_chain")})
                    for k, v in st["pending"].items()
                    if not v.get("claimed") and now - v.get("created", 0) < PENDING_TTL_S
                    and v.get("session_id") in session_ids]

    def claim(self, pid: str) -> Optional[dict]:
        """Atomic: exactly one window gets the prompt."""
        now = self.clock()
        with self.txn() as st:
            if st is None:
                return None
            p = st["pending"].get(pid)
            if not p or p.get("claimed") or now - p.get("created", 0) >= PENDING_TTL_S:
                return None
            p["claimed"] = now
            self.log("claimed", id=pid, session_id=p.get("session_id"))
            return {"id": pid, "prompt": p["prompt"], "handoff_path": p["handoff_path"],
                    "step": p["step"], "max_chain": p["max_chain"]}

    def result(self, pid: str, ok: bool, detail: str = "") -> bool:
        with self.txn() as st:
            if st is None or pid not in st["pending"]:
                return False
            st["pending"][pid]["result"] = {"ok": bool(ok), "detail": str(detail)[:200], "t": self.clock()}
            self.log("result", id=pid, ok=bool(ok), detail=str(detail)[:200])
            return True


# ---------------------------------------------------------------- engine
@dataclass
class Action:
    """What the Hermes wiring should do after a turn: post ``line`` (if any) in the session and,
    for ``continue``, broadcast ``event`` to the Desktop half."""
    kind: str  # "none" | "tell" | "continue"
    line: str = ""
    event: Optional[dict] = None


class Engine:
    def __init__(self, settings_fn: Callable[[], Settings], store: Store,
                 handydandy_fn: Callable[[str], Optional[dict]],
                 handydandy_present_fn: Callable[[], bool],
                 clock: Callable[[], float] = time.time):
        self.settings_fn = settings_fn
        self.store = store
        self.hd = handydandy_fn
        self.hd_present = handydandy_present_fn
        self.clock = clock

    def _sess(self, st: dict, sid: str) -> dict:
        s = st["sessions"].setdefault(sid, {})
        s["seen"] = self.clock()
        return s

    def _active(self, s: dict, settings: Settings) -> bool:
        last = s.get("computer_at")
        return bool(last) and self.clock() - float(last) <= settings.active_window_minutes * 60

    # post_tool_call
    def on_tool(self, tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> None:
        if not session_id or not is_computer_call(tool_name, args):
            return
        with self.store.txn() as st:
            if st is not None:
                self._sess(st, session_id)["computer_at"] = self.clock()

    # pre_llm_call
    def on_user_turn(self, session_id: str = "", user_message: Any = "", platform: str = "",
                     **_: Any) -> Optional[dict]:
        settings = self.settings_fn()
        if not session_id or not settings.enabled:
            return None
        text = user_message if isinstance(user_message, str) else ""
        sig = opt_signal(text)
        seed = seed_marker(text)
        with self.store.txn() as st:
            if st is None:
                return None
            s = self._sess(st, session_id)
            if sig is not None:
                s["opted_out"] = not sig
                self.store.log("opt_out" if not sig else "opt_in", session_id=session_id)
            if seed and not s.get("chain"):
                s["chain"], s["step"] = seed
                # A seeded chat inherits the user's earlier opt-out for the same task.
                if st["chains"].get(seed[0], {}).get("opted_out"):
                    s["opted_out"] = True
            if sig is not None:  # the root chat's chain is its own session id
                st["chains"].setdefault(s.get("chain") or session_id, {})["opted_out"] = not sig
            if s.get("opted_out") or not self._active(s, settings) or not self.hd_present():
                return None
            hd = self.hd(session_id) or {}
            if hd.get("mode") != "handoff" or not hd.get("armed_at") or hd.get("handoff_done"):
                return None
            next_step = int(s.get("step", 0)) + 1
        return {"context": handoff_addendum(next_step, settings.max_chain)}

    # post_llm_call (HandyDandy has already recorded handoff_done in transform_llm_output)
    def on_turn_end(self, session_id: str = "", platform: str = "", surface: str = "",
                    **_: Any) -> Action:
        settings = self.settings_fn()
        if not session_id or not settings.enabled or not self.hd_present():
            return Action("none")
        hd = self.hd(session_id) or {}
        path = hd.get("handoff_path") or ""
        if hd.get("mode") != "handoff" or not hd.get("handoff_done") or not path:
            return Action("none")
        where = (surface or platform or "").lower()
        with self.store.txn() as st:
            if st is None:
                return Action("none")
            s = self._sess(st, session_id)
            if s.get("handled") == path:
                return Action("none")
            if not self._active(s, settings):
                return Action("none")  # not a hands-off session: HandyDandy's own notice stands
            s["handled"] = path
            try:
                text = Path(path).read_text(errors="replace")
            except OSError:
                text = ""
            d = decide(text)
            chain = s.get("chain") or session_id
            step = int(s.get("step", 0)) + 1
            used = int(st["chains"].get(chain, {}).get("used", 0))
            self.store.log("decided", session_id=session_id, verdict=d.verdict, reasons=d.reasons,
                           surface=where, chain=chain, step=step)
            if d.verdict == "delivered":
                return Action("tell", f"{MARK} Handoff saved at {path}. The outcome reads as delivered, "
                                      "so nothing continues automatically.")
            if d.verdict == "ask":
                return Action("tell", f"{MARK} Handoff saved at {path}. I could not tell the work is still "
                                      f"going ({'; '.join(d.reasons)}). Say \"from handoff {path}\" in a new "
                                      "chat to continue.")
            # verdict == continue
            if s.get("opted_out") or st["chains"].get(chain, {}).get("opted_out"):
                return Action("tell", f"{MARK} Handoff saved at {path}. You said no auto handoff, so start "
                                      f"the next chat yourself with: from handoff {path}")
            if not settings.auto_continue:
                return Action("tell", f"{MARK} Handoff saved at {path}. Auto-continue is off in settings; "
                                      f"start the next chat with: from handoff {path}")
            if used >= settings.max_chain:
                return Action("tell", f"{MARK} Handoff saved at {path}. This task already continued "
                                      f"automatically {used} time(s), the limit, so I stopped here. Start "
                                      f"the next chat yourself with: from handoff {path}")
            if any(u in where for u in UNATTENDED):
                return Action("tell", f"{MARK} Handoff saved at {path}. This run is unattended, so it does "
                                      "not continue on its own.")
            if where not in AUTO_SURFACES:
                return Action("tell", f"{MARK} Handoff saved at {path}. Hermes gives plugins no way to open "
                                      f"a new chat on {where or 'this surface'}; send /new, then: from handoff "
                                      f"{path}")
            pid = uuid.uuid4().hex
            st["pending"][pid] = {"session_id": session_id, "handoff_path": path, "chain": chain,
                                  "step": step, "max_chain": settings.max_chain,
                                  "prompt": seed_prompt(chain, step, path), "created": self.clock()}
            st["chains"].setdefault(chain, {})["used"] = used + 1
            self.store.log("queued", id=pid, session_id=session_id, chain=chain, step=step)
        return Action(
            "continue",
            f"{MARK} Handoff saved at {path}. The work is still going, so the Desktop app is opening a "
            f"new chat from it (automatic continue {step} of {settings.max_chain}). Say \"no auto handoff\" "
            "to stop that next time.",
            {"id": pid, "session_id": session_id},
        )
