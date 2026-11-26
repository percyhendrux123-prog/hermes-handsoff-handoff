"""Tests for handsoff-handoff. No real computer_use, no real session spawn, no process aborts:
HandyDandy's state, the clock, the plugin context and the broadcast are all fakes."""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core  # noqa: E402
from core import Engine, Settings, Store, decide, is_computer_call, opt_signal, seed_marker  # noqa: E402

NOT_DONE = """# Handoff
Outcome: not delivered
## Open items
- [ ] finish the export in Numbers
## Next action
Open the sheet and export the CSV.
"""
DONE = """# Handoff
Outcome: delivered
## Open items
- [x] exported
## Next action
None.
"""


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Harness:
    def __init__(self, case, settings=None, hd_present=True):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.clock = Clock()
        self.settings = settings or Settings()
        self.hd = {}  # session_id -> HandyDandy record (fake)
        self.present = hd_present
        self.store = Store(self.dir / "data", clock=self.clock)
        self.engine = Engine(lambda: self.settings, self.store, lambda sid: self.hd.get(sid),
                             lambda: self.present, clock=self.clock)

    def handoff(self, sid, text, done=True):
        p = self.dir / f"HANDOFF-{sid}.md"
        p.write_text(text)
        self.hd[sid] = {"mode": "handoff", "armed_at": self.clock() - 5, "handoff_path": str(p),
                        **({"handoff_done": self.clock()} if done else {})}
        return p

    def drive(self, sid):
        self.engine.on_tool(tool_name="computer_use", args={"action": "screenshot"}, session_id=sid)


class Reading(unittest.TestCase):
    def test_computer_call_direct_and_through_tool_call_bridge(self):
        self.assertTrue(is_computer_call("computer_use", {}))
        self.assertTrue(is_computer_call("tool_call", {"calls": [{"name": "computer_use", "arguments": {}}]}))
        self.assertFalse(is_computer_call("tool_call", {"calls": [{"name": "browser_vault"}]}))
        self.assertFalse(is_computer_call("terminal", {"command": "computer_use"}))

    def test_opt_out_phrases(self):
        for s in ["don't auto handoff", "Do not auto-handoff this one", "no auto handoff please",
                  "dont auto hand off", "no auto continue", "stop auto handoffs"]:
            self.assertIs(opt_signal(s), False, s)
        self.assertIs(opt_signal("auto handoff is ok again"), True)
        self.assertIsNone(opt_signal("write the handoff when you are done"))
        self.assertIsNone(opt_signal("handoff the auto parts"))

    def test_seed_marker(self):
        self.assertEqual(seed_marker("[handsoff-handoff chain=abc-1 step=2] Continue"), ("abc-1", 2))
        self.assertIsNone(seed_marker("from handoff x"))

    def test_decide_is_conservative(self):
        self.assertEqual(decide(NOT_DONE).verdict, "continue")
        self.assertEqual(decide(DONE).verdict, "delivered")
        self.assertEqual(decide("").verdict, "ask")
        self.assertEqual(decide("# Handoff\n## Next action\nkeep going\n").verdict, "ask")  # no outcome line
        self.assertEqual(decide(DONE + "\n- [ ] one more\n").verdict, "ask")  # delivered but open item
        self.assertEqual(decide(NOT_DONE + "Outcome: delivered\n").verdict, "ask")  # conflicting
        self.assertEqual(decide("Outcome: not delivered\n- [ ] x\n").verdict, "ask")  # no next action
        blocked = NOT_DONE + "AXL/1  A1 · BLOCKED · NEED owner:decide\n"
        self.assertEqual(decide(blocked).verdict, "ask")
        axl_done = DONE + "AXL/1  A1 · DONE · NEED none:none\n"
        self.assertEqual(decide(axl_done).verdict, "delivered")
        self.assertEqual(decide(DONE + "AXL/1  A1 · RUNNING · NEED none:none\n").verdict, "ask")
        self.assertEqual(decide(NOT_DONE + "AXL/1  A1 · DONE · NEED none:none\n").verdict, "ask")


class Flow(unittest.TestCase):
    def test_desktop_continue_queues_one_claimable_continuation(self):
        h = Harness(self)
        h.drive("s1")
        h.handoff("s1", NOT_DONE)
        a = h.engine.on_turn_end(session_id="s1", platform="desktop", surface="desktop")
        self.assertEqual(a.kind, "continue")
        self.assertIn("automatic continue 1 of 2", a.line)
        pend = h.store.pending_for(["s1"])
        self.assertEqual(len(pend), 1)
        got = h.store.claim(a.event["id"])
        self.assertIn("[handsoff-handoff chain=s1 step=1]", got["prompt"])
        self.assertIn("still asks", got["prompt"])
        self.assertIsNone(h.store.claim(a.event["id"]), "second window must lose the claim")
        self.assertEqual(h.store.pending_for(["s1"]), [])
        self.assertTrue(h.store.result(a.event["id"], True, "submitted"))
        # Same handoff is handled once.
        self.assertEqual(h.engine.on_turn_end(session_id="s1", surface="desktop").kind, "none")

    def test_not_hands_off_when_no_recent_computer_use(self):
        h = Harness(self)
        h.handoff("s1", NOT_DONE)
        self.assertEqual(h.engine.on_turn_end(session_id="s1", surface="desktop").kind, "none")
        h.drive("s2")
        h.clock.t += 31 * 60  # past the 30-minute window
        h.handoff("s2", NOT_DONE)
        self.assertEqual(h.engine.on_turn_end(session_id="s2", surface="desktop").kind, "none")

    def test_requires_handydandy_present_and_in_handoff_mode(self):
        h = Harness(self, hd_present=False)
        h.drive("s1")
        h.handoff("s1", NOT_DONE)
        self.assertEqual(h.engine.on_turn_end(session_id="s1", surface="desktop").kind, "none")
        h2 = Harness(self)
        h2.drive("s1")
        h2.handoff("s1", NOT_DONE)
        h2.hd["s1"]["mode"] = "advise"
        self.assertEqual(h2.engine.on_turn_end(session_id="s1", surface="desktop").kind, "none")

    def test_delivered_and_unsure_tell_instead_of_continuing(self):
        h = Harness(self)
        h.drive("s1")
        h.handoff("s1", DONE)
        a = h.engine.on_turn_end(session_id="s1", surface="desktop")
        self.assertEqual(a.kind, "tell")
        self.assertIn("delivered", a.line)
        h.drive("s2")
        h.handoff("s2", "# nothing clear\n")
        a = h.engine.on_turn_end(session_id="s2", surface="desktop")
        self.assertEqual(a.kind, "tell")
        self.assertIn("could not tell", a.line)
        self.assertEqual(h.store.pending_for(["s1", "s2"]), [])

    def test_opt_out_in_chat_stops_it_and_carries_into_seeded_chat(self):
        h = Harness(self)
        h.drive("s1")
        h.engine.on_user_turn(session_id="s1", user_message="go ahead, but don't auto handoff")
        h.handoff("s1", NOT_DONE)
        a = h.engine.on_turn_end(session_id="s1", surface="desktop")
        self.assertEqual(a.kind, "tell")
        self.assertIn("no auto handoff", a.line)
        # Opt back in
        h2 = Harness(self)
        h2.drive("s1")
        h2.engine.on_user_turn(session_id="s1", user_message="no auto handoff")
        h2.engine.on_user_turn(session_id="s1", user_message="auto handoff is ok")
        h2.handoff("s1", NOT_DONE)
        self.assertEqual(h2.engine.on_turn_end(session_id="s1", surface="desktop").kind, "continue")
        # A seeded chat whose chain was opted out stays opted out.
        h2.engine.on_user_turn(session_id="s1", user_message="no auto handoff")  # recorded on chain s1
        h2.engine.on_user_turn(session_id="s9", user_message="[handsoff-handoff chain=s1 step=1] Continue")
        h2.drive("s9")
        h2.handoff("s9", NOT_DONE)
        self.assertEqual(h2.engine.on_turn_end(session_id="s9", surface="desktop").kind, "tell")

    def test_setting_off(self):
        h = Harness(self, settings=Settings(auto_continue=False))
        h.drive("s1")
        h.handoff("s1", NOT_DONE)
        a = h.engine.on_turn_end(session_id="s1", surface="desktop")
        self.assertEqual(a.kind, "tell")
        self.assertIn("off in settings", a.line)
        h2 = Harness(self, settings=Settings(enabled=False))
        h2.drive("s1")
        h2.handoff("s1", NOT_DONE)
        self.assertEqual(h2.engine.on_turn_end(session_id="s1", surface="desktop").kind, "none")

    def test_loop_guard_stops_after_max_chain(self):
        h = Harness(self)  # max_chain 2
        sid = "s0"
        kinds = []
        for i in range(4):
            h.drive(sid)
            h.handoff(sid, NOT_DONE)
            a = h.engine.on_turn_end(session_id=sid, surface="desktop")
            kinds.append(a.kind)
            if a.kind != "continue":
                self.assertIn("the limit", a.line)
                break
            prompt = h.store.claim(a.event["id"])["prompt"]
            sid = f"s{i + 1}"
            h.engine.on_user_turn(session_id=sid, user_message=prompt, is_first_turn=True)
        self.assertEqual(kinds, ["continue", "continue", "tell"])

    def test_other_surfaces_and_unattended_runs_only_tell(self):
        for where in ("tui", "cli", "telegram"):
            h = Harness(self)
            h.drive("s1")
            h.handoff("s1", NOT_DONE)
            a = h.engine.on_turn_end(session_id="s1", platform=where, surface=where)
            self.assertEqual(a.kind, "tell", where)
            self.assertIn("/new", a.line)
        for where in ("cron", "kanban"):
            h = Harness(self)
            h.drive("s1")
            h.handoff("s1", NOT_DONE)
            a = h.engine.on_turn_end(session_id="s1", platform=where, surface=where)
            self.assertEqual(a.kind, "tell")
            self.assertIn("unattended", a.line)

    def test_pre_llm_call_adds_outcome_instruction_only_while_armed_and_hands_off(self):
        h = Harness(self)
        self.assertIsNone(h.engine.on_user_turn(session_id="s1", user_message="hi"))
        h.drive("s1")
        h.handoff("s1", NOT_DONE, done=False)  # armed, not yet written
        r = h.engine.on_user_turn(session_id="s1", user_message="keep going")
        self.assertIn("Outcome: delivered", r["context"])
        self.assertIn("continue 1 of at most 2", r["context"])
        h.engine.on_user_turn(session_id="s1", user_message="no auto handoff")
        self.assertIsNone(h.engine.on_user_turn(session_id="s1", user_message="keep going"))

    def test_test_mode_off_ignores_the_phrase(self):
        h = Harness(self)
        h.drive("s1")
        self.assertIsNone(h.engine.on_user_turn(session_id="s1", user_message="handsoff-handoff test"))
        self.assertEqual(h.engine.on_turn_end(session_id="s1", surface="desktop").kind, "none")

    def test_test_mode_runs_one_relay_without_handydandy_limit(self):
        h = Harness(self, settings=Settings(test_mode=True))
        h.drive("s1")
        r = h.engine.on_user_turn(session_id="s1", user_message="please run the handsoff-handoff test")
        self.assertIn("TEST MODE", r["context"])
        tp = Path(h.store.dir / "test-handoffs" / "s1.md")
        self.assertIn(str(tp), r["context"])
        # Before the agent writes the file nothing happens.
        self.assertEqual(h.engine.on_turn_end(session_id="s1", surface="desktop").kind, "none")
        tp.write_text("# Handoff (handsoff-handoff test)\nOutcome: not delivered\n- [ ] confirm\n"
                      "## Next action\nReply with one line.\n")
        a = h.engine.on_turn_end(session_id="s1", surface="desktop")
        self.assertEqual(a.kind, "continue")
        self.assertIn("(test)", a.line)
        self.assertEqual(len(h.store.pending_for(["s1"])), 1)
        # One trigger, one relay.
        self.assertEqual(h.engine.on_turn_end(session_id="s1", surface="desktop").kind, "none")

    def test_test_mode_still_needs_computer_use_and_respects_opt_out_and_cap(self):
        h = Harness(self, settings=Settings(test_mode=True, max_chain=1))
        r = h.engine.on_user_turn(session_id="s1", user_message="handsoff-handoff test")
        self.assertIn("no computer_use call", r["context"])
        h.drive("s2")
        h.engine.on_user_turn(session_id="s2", user_message="no auto handoff")
        h.engine.on_user_turn(session_id="s2", user_message="handsoff-handoff test")
        Path(h.store.dir / "test-handoffs" / "s2.md").write_text(NOT_DONE)
        a = h.engine.on_turn_end(session_id="s2", surface="desktop")
        self.assertEqual(a.kind, "tell")
        self.assertIn("no auto handoff", a.line)
        # A seeded chat that repeats the phrase does not re-arm the test.
        h.drive("s3")
        self.assertIsNone(h.engine.on_user_turn(
            session_id="s3", user_message="[handsoff-handoff chain=s9 step=1] handsoff-handoff test"))

    def test_unreadable_state_fails_closed(self):
        h = Harness(self)
        h.store.dir.mkdir(parents=True, exist_ok=True)
        h.store.file.write_text("{not json")
        h.drive("s1")
        h.handoff("s1", NOT_DONE)
        self.assertEqual(h.engine.on_turn_end(session_id="s1", surface="desktop").kind, "none")
        self.assertEqual(h.store.file.read_text(), "{not json")

    def test_pending_expires(self):
        h = Harness(self)
        h.drive("s1")
        h.handoff("s1", NOT_DONE)
        a = h.engine.on_turn_end(session_id="s1", surface="desktop")
        h.clock.t += core.PENDING_TTL_S + 1
        self.assertEqual(h.store.pending_for(["s1"]), [])
        self.assertIsNone(h.store.claim(a.event["id"]))


class Wiring(unittest.TestCase):
    """register() with a fake ctx: hooks, post_message and broadcast, no Hermes needed."""

    def test_register_and_continue_posts_one_line_and_broadcasts(self):
        import importlib
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        import os
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        self.addCleanup(lambda: os.environ.__setitem__("HERMES_HOME", old) if old else os.environ.pop("HERMES_HOME", None))
        sent = []
        fake_events = types.ModuleType("hermes_cli.plugin_events")
        fake_events.broadcast_plugin_event = lambda pid, ev, payload: sent.append((pid, ev, payload))
        fake_pkg = types.ModuleType("hermes_cli")
        sys.modules["hermes_cli"] = sys.modules.get("hermes_cli", fake_pkg)
        prev = sys.modules.get("hermes_cli.plugin_events")
        sys.modules["hermes_cli.plugin_events"] = fake_events
        self.addCleanup(lambda: sys.modules.__setitem__("hermes_cli.plugin_events", prev) if prev
                        else sys.modules.pop("hermes_cli.plugin_events", None))
        plugin = importlib.import_module("__init__") if False else None
        spec = importlib.util.spec_from_file_location("hh_plugin", ROOT / "__init__.py")
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)

        posts, hooks = [], {}
        ctx = types.SimpleNamespace(
            get_config=lambda k, d=None: d,
            has_plugin=lambda name: name == "handydandy",
            session_surface=lambda: "desktop",
            post_message=lambda text: posts.append(text) or types.SimpleNamespace(ok=True),
            register_hook=lambda name, fn: hooks.__setitem__(name, fn),
        )
        plugin.register(ctx)
        self.assertEqual(set(hooks), {"post_tool_call", "pre_llm_call", "post_llm_call"})
        hp = home / "HANDOFF.md"
        hp.write_text(NOT_DONE)
        hd_state = home / "plugin-data" / "handydandy" / "state.json"
        hd_state.parent.mkdir(parents=True)
        hd_state.write_text(json.dumps({"sessions": {"s1": {"mode": "handoff", "armed_at": 1, "handoff_done": 2,
                                                            "handoff_path": str(hp)}}}))
        before = hd_state.read_bytes()
        hooks["post_tool_call"](tool_name="computer_use", args={}, session_id="s1")
        hooks["post_llm_call"](session_id="s1", platform="desktop", assistant_response="ok")
        self.assertEqual(len(posts), 1)
        self.assertIn("opening a new chat", posts[0])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0:2], ("handsoff-handoff", "continue"))
        self.assertEqual(sent[0][2]["session_id"], "s1")
        self.assertEqual(hd_state.read_bytes(), before, "HandyDandy's state is read, never written")


class SeatTests(unittest.TestCase):
    def test_seat_on_every_handoff_and_seed(self):
        self.assertIn(core.SEAT_HEADING, core.handoff_addendum(1, 3))
        self.assertIn(core.SEAT_TEXT, core.handoff_addendum(1, 3))
        self.assertIn(core.SEAT_HEADING, core.test_instruction("/tmp/x.md"))
        self.assertIn("seat is yours", core.seed_prompt("c", 1, "/tmp/x.md"))

    def test_seat_section_does_not_change_verdict(self):
        h = ("# Handoff\nOutcome: not delivered\n- [ ] a\n## Next action\ndo a\n"
             + core.SEAT_HEADING + "\n" + core.SEAT_TEXT + "\n")
        self.assertEqual(decide(h).verdict, "continue")


if __name__ == "__main__":
    unittest.main()
