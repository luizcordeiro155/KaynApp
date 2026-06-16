"""Entrada central do Kayn.

Este modulo importa o runtime legado, expoe o objeto `bot` e adapta `bot.run()`
para manter o comportamento de inicializacao que antes ficava no final do main.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from typing import Any, Optional

import discord

from cogs import legacy_runtime as legacy

bot = legacy.bot
TOKEN = legacy.TOKEN
logger = legacy.logger

_original_bot_run = bot.run


def _force_required_gateway_intents() -> None:
    """Garante intents necessarios para eventos de moderacao/servidor.

    Entrada, saida, banimento e alteracao de cargos dependem do Server Members
    Intent. No runtime legado o padrao vinha de KAYN_ENABLE_MEMBERS_INTENT=false,
    entao os listeners existiam, mas o Discord nao enviava os eventos.
    """
    try:
        os.environ.setdefault("KAYN_ENABLE_MEMBERS_INTENT", "true")

        intents = getattr(bot, "intents", None)
        if intents is not None:
            intents.guilds = True
            intents.members = True
            intents.messages = True
            intents.message_content = True
            intents.voice_states = True

        state = getattr(bot, "_connection", None)
        state_intents = getattr(state, "_intents", None)
        if state_intents is not None:
            state_intents.guilds = True
            state_intents.members = True
            state_intents.messages = True
            state_intents.message_content = True
            state_intents.voice_states = True

        logger.info("Kayn intents obrigatorios ativos: guilds/members/messages/message_content/voice_states")
    except Exception:
        with contextlib.suppress(Exception):
            logger.error("Falha ao ativar intents obrigatorios do Kayn", exc_info=True)


def _normalize_channel_name(name: str) -> str:
    table = str.maketrans({
        "á": "a", "à": "a", "ã": "a", "â": "a", "ä": "a",
        "é": "e", "ê": "e", "è": "e", "ë": "e",
        "í": "i", "ì": "i", "î": "i", "ï": "i",
        "ó": "o", "ò": "o", "õ": "o", "ô": "o", "ö": "o",
        "ú": "u", "ù": "u", "û": "u", "ü": "u",
        "ç": "c",
    })
    return (name or "").lower().translate(table).replace("_", "-").strip()


def _find_text_channel_by_names(guild: discord.Guild, names: list[str]) -> Optional[discord.TextChannel]:
    wanted = {_normalize_channel_name(n) for n in names if n}
    try:
        for channel in getattr(guild, "text_channels", []) or []:
            if _normalize_channel_name(getattr(channel, "name", "")) in wanted:
                return channel
    except Exception:
        with contextlib.suppress(Exception):
            logger.debug("Excecao silenciosa ignorada pelo Kayn", exc_info=True)
    return None


def _install_welcome_channel_fallbacks() -> None:
    """Usa canais padrao quando o servidor ainda nao configurou a tabela.

    O sistema legado salva canais via !setboasvindas/!setdespedida/!setbanidos.
    Se o banco novo ainda nao tem essas configs, o Kayn agora tenta achar os
    canais comuns do servidor automaticamente.
    """
    old_get_channel = getattr(legacy, "kayn_v140_get_configured_channel", None)
    if not callable(old_get_channel):
        return

    if getattr(bot, "_kayn_core_welcome_fallbacks", False):
        return

    def patched_get_configured_channel(guild: discord.Guild, kind: str) -> Optional[discord.TextChannel]:
        channel = None
        with contextlib.suppress(Exception):
            channel = old_get_channel(guild, kind)
        if channel:
            return channel

        kind_norm = str(kind or "").strip().lower()
        fallback_names = {
            "welcome": ["bem-vindo", "bem-vindos", "boas-vindas", "boasvindas", "welcome"],
            "farewell": ["sairam-do-servidor", "saíram-do-servidor", "saidas", "saídas", "despedida", "despedidas", "goodbye"],
            "ban": ["banidos", "banido", "punicoes", "punições", "expulsos", "moderacao", "moderação"],
            "kick": ["banidos", "banido", "punicoes", "punições", "expulsos", "moderacao", "moderação"],
        }.get(kind_norm, [])
        return _find_text_channel_by_names(guild, fallback_names)

    legacy.kayn_v140_get_configured_channel = patched_get_configured_channel
    bot._kayn_core_welcome_fallbacks = True
    logger.info("Kayn fallback de canais boas-vindas/despedida/banidos instalado.")


_force_required_gateway_intents()
_install_welcome_channel_fallbacks()


def _install_signal_handlers() -> None:
    handler = getattr(legacy, "_signal_handler", None)
    if not callable(handler):
        return
    try:
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
    except Exception:
        with contextlib.suppress(Exception):
            logger.debug("Excecao silenciosa ignorada pelo Kayn", exc_info=True)


def _close_http_session_safely() -> None:
    close_http_session = getattr(legacy, "close_http_session", None)
    if not callable(close_http_session):
        return
    try:
        asyncio.run(close_http_session())
    except RuntimeError:
        # Se ja existir loop, o discord.py cuidara do encerramento principal.
        pass
    except Exception:
        with contextlib.suppress(Exception):
            logger.debug("Excecao silenciosa ignorada pelo Kayn", exc_info=True)


def _is_login_rate_limit(exc: BaseException) -> bool:
    checker = getattr(legacy, "kayn_v561_is_discord_login_rate_limit", None)
    return bool(callable(checker) and checker(exc))


def _is_session_closed_after_retry(exc: BaseException) -> bool:
    checker = getattr(legacy, "kayn_v562_is_session_closed_after_retry", None)
    return bool(callable(checker) and checker(exc))


def _restart_after_delay(seconds: int, reason: str) -> None:
    restart = getattr(legacy, "kayn_v562_restart_process_after_delay", None)
    if callable(restart):
        restart(seconds, reason)


def run(token: Optional[str] = None, *args: Any, **kwargs: Any) -> None:
    """Executa o bot com o mesmo backoff/limpeza do antigo bloco `if __main__`."""
    _force_required_gateway_intents()
    _install_welcome_channel_fallbacks()
    _install_signal_handlers()

    apply_logging = getattr(legacy, "kayn_v205_apply_error_only_logging", None)
    if callable(apply_logging):
        apply_logging()

    try:
        run_token = token or TOKEN
        kwargs.setdefault("log_handler", None)
        try:
            _original_bot_run(run_token, *args, **kwargs)
        except TypeError:
            kwargs.pop("log_handler", None)
            _original_bot_run(run_token, *args, **kwargs)
    except Exception as exc:
        if _is_login_rate_limit(exc):
            _restart_after_delay(180, "Discord rate limit 429/40062 no login; possivel restart em loop ou outra instancia com o mesmo token")
            return
        if _is_session_closed_after_retry(exc):
            _restart_after_delay(120, "sessao HTTP do Discord fechada apos falha de login")
            return
        raise
    finally:
        _close_http_session_safely()


# Permite que o main.py fique literalmente so com bot.run().
bot.run = run  # type: ignore[method-assign]
