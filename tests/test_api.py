"""Backend routes, called as plain functions (skipped when FastAPI is not installed)."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import fastapi  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False


@unittest.skipUnless(HAVE, "fastapi not installed; run with the Hermes venv python")
class Routes(unittest.TestCase):
    def test_pending_claim_result(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["HERMES_HOME"] = tmp.name
        spec = importlib.util.spec_from_file_location("hh_api", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        api._store = lambda: api._core.Store(Path(tmp.name) / "data")  # keep tests off the real profile
        store = api._store()
        with store.txn() as st:
            st["pending"]["p1"] = {"session_id": "s1", "handoff_path": "/x.md", "chain": "s1", "step": 1,
                                   "max_chain": 2, "prompt": "seed", "created": store.clock()}
        self.assertEqual([i["id"] for i in api.pending(session="s1")["items"]], ["p1"])
        self.assertEqual(api.pending(session="other")["items"], [])
        self.assertEqual(api.claim({"id": "p1"})["prompt"], "seed")
        self.assertFalse(api.claim({"id": "p1"})["ok"])
        self.assertTrue(api.result({"id": "p1", "ok": True})["ok"])
        self.assertFalse(api.result({"id": "nope", "ok": True})["ok"])


if __name__ == "__main__":
    unittest.main()
