"""Proofly: checks an AI agent's claimed Base transfers against chain evidence.

Hermes registration only. The verification logic lives in ``verifier`` / ``intent`` (no Hermes
imports) and the hook callbacks in ``hooks``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("proofly")

PLUGIN_NAME = "proofly"


def _open_ledger():
    """Profile-scoped SQLite under ``<HERMES_HOME>/plugin-data/proofly/`` via the public storage API."""
    from plugins.plugin_storage import plugin_db

    from .ledger import Ledger

    return Ledger(lambda: plugin_db(PLUGIN_NAME))


def register(ctx):
    from . import commands, schemas
    from .hooks import ProoflyRuntime, load_settings

    settings = load_settings(ctx.get_config)
    for warning in settings.warnings:
        logger.warning("proofly: %s", warning)
    runtime = ProoflyRuntime(settings, ledger_factory=_open_ledger)

    ctx.register_hook("post_tool_call", runtime.on_post_tool_call)
    ctx.register_hook("transform_tool_result", runtime.on_transform_tool_result)
    # hold_stream: the user sees the reply only after Proofly has checked it (every session of a
    # profile that loads Proofly stops streaming replies; see README "Visibility").
    ctx.register_hook("transform_llm_output", runtime.on_transform_llm_output, hold_stream=True)

    ctx.register_tool(
        name=schemas.PROOFLY_VERIFY_TX["name"],
        toolset="proofly",
        schema=schemas.PROOFLY_VERIFY_TX,
        handler=runtime.handle_verify_tool,
        description="Read-only Base transaction / transfer verification",
        emoji="\U0001f50e",
    )

    def _command(raw_args: str = "", **kwargs):
        try:
            return commands.handle(runtime, raw_args, **kwargs)
        except Exception as exc:  # e.g. a locked ledger: answer the user instead of raising
            logger.warning("proofly: /proofly failed", exc_info=True)
            return f"Proofly could not complete that command ({type(exc).__name__})."

    ctx.register_command(
        "proofly",
        handler=_command,
        description="Proofly: Base transfer verdicts, status and on-demand checks",
        args_hint="[status|last|history|check <hash>|clear|help]",
    )
