"""Backend routes for the Desktop half: /api/plugins/handsoff-handoff/...

The Desktop half asks which continuations are waiting for the chat it shows, claims one (exactly
one window wins), opens the new chat itself, and reports back. These routes never create a
session, run a turn, or touch approvals; they only read and update this plugin's own state file.
Plain ``def`` routes so the short file lock runs off the event loop.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter

_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, file: Path):
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, file)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_core = _load("handsoff_handoff_core", _ROOT / "core.py")


def _store():
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore
        d = Path(plugin_data_dir(_core.PLUGIN_ID))
    except Exception:
        import os
        d = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "plugin-data" / _core.PLUGIN_ID
    return _core.Store(d)


router = APIRouter()


@router.get("/pending")
def pending(session: str = "", alt: str = ""):
    ids = [s for s in (session, alt) if s]
    return {"items": _store().pending_for(ids) if ids else []}


@router.post("/claim")
def claim(body: dict):
    got = _store().claim(str((body or {}).get("id") or ""))
    return {"ok": True, **got} if got else {"ok": False}


@router.post("/result")
def result(body: dict):
    b = body or {}
    return {"ok": _store().result(str(b.get("id") or ""), bool(b.get("ok")), str(b.get("detail") or ""))}
