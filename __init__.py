"""handsoff-handoff: when the agent is driving the computer and HandyDandy writes the handoff,
continue in a fresh Desktop chat if the work is still going. See README.md.

No subprocess, no session created by this plugin, no approval touched: the Desktop half asks the
Desktop app to open a new chat exactly like the user's own "New chat", then types the seed prompt
into it. Other surfaces get one line telling the user where the handoff is.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

try:  # loaded as a Hermes plugin package
    from .core import PLUGIN_ID, Engine, Settings, Store
except ImportError:  # loaded flat (tests)
    from core import PLUGIN_ID, Engine, Settings, Store  # type: ignore

logger = logging.getLogger(__name__)
HANDYDANDY = "handydandy"


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore
        return Path(plugin_data_dir(PLUGIN_ID))
    except Exception:
        return _hermes_home() / "plugin-data" / PLUGIN_ID


def handydandy_session(session_id: str, home: Path | None = None) -> dict | None:
    """Read-only: HandyDandy's own record for this session (mode, armed_at, handoff_done,
    handoff_path). Never writes HandyDandy's files. None when absent or unreadable."""
    path = (home or _hermes_home()) / "plugin-data" / HANDYDANDY / "state.json"
    try:
        raw = json.loads(path.read_text())
        rec = (raw.get("sessions") or {}).get(session_id)
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def build_engine(ctx) -> Engine:
    def settings() -> Settings:
        return Settings.from_getter(lambda k, d: ctx.get_config(k, d))

    def hd_present() -> bool:
        try:
            return bool(ctx.has_plugin(HANDYDANDY))
        except Exception:
            return False

    return Engine(settings, Store(data_dir()), handydandy_session, hd_present)


def _surface(ctx) -> str:
    try:
        return ctx.session_surface() or ""
    except Exception:
        return ""


def _broadcast(event: str, payload: dict) -> bool:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event  # type: ignore
        broadcast_plugin_event(PLUGIN_ID, event, payload)
        return True
    except Exception:
        logger.debug("handsoff-handoff broadcast failed", exc_info=True)
        return False


def register(ctx):
    engine = build_engine(ctx)

    def post_tool_call(**kw):
        try:
            engine.on_tool(**kw)
        except Exception:
            logger.debug("handsoff-handoff post_tool_call failed", exc_info=True)

    def pre_llm_call(**kw):
        try:
            return engine.on_user_turn(**kw)
        except Exception:
            logger.debug("handsoff-handoff pre_llm_call failed", exc_info=True)
            return None

    def post_llm_call(**kw):
        try:
            action = engine.on_turn_end(surface=_surface(ctx), **kw)
        except Exception:
            logger.debug("handsoff-handoff post_llm_call failed", exc_info=True)
            return
        if action.kind == "none":
            return
        if action.kind == "continue" and action.event:
            if not _broadcast("continue", action.event):
                engine.store.log("broadcast_failed", id=action.event.get("id"))
        try:
            handle = ctx.post_message(action.line)
            if not getattr(handle, "ok", True):
                engine.store.log("post_failed", error=str(getattr(handle, "error", "")))
        except Exception:
            logger.debug("handsoff-handoff post_message failed", exc_info=True)

    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_llm_call", post_llm_call)
