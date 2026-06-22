"""Entrada central do Kayn.

Este modulo importa o runtime legado, expoe o objeto `bot` e adapta `bot.run()`
para manter o comportamento de inicializacao que antes ficava no final do main.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import threading
from typing import Any, Optional

import discord

# Precisa vir ANTES do legacy_runtime, porque o KaynBot e criado durante essa
# importacao. Se ficar depois, o bot nasce sem members intent e o Discord nao
# entrega eventos de entrar/sair/banir membros.
os.environ["KAYN_ENABLE_MEMBERS_INTENT"] = "true"

from cogs import legacy_runtime as legacy

bot = legacy.bot
TOKEN = legacy.TOKEN
logger = legacy.logger

_original_bot_run = bot.run
_ORIGINAL_MESSAGEABLE_SEND = discord.abc.Messageable.send
_SCHEMA_GUARD_INSTALLED = False
_SCHEMA_PREWARMED = False
_SCHEMA_GUARD_LOCK = threading.RLock()
_SCHEMA_ENSURE_FUNCTIONS = (
    # Funcoes vistas nos traces de heartbeat blocked. A v262 chama a cadeia
    # mais nova de migracoes, mas mantemos as anteriores para cobrir deploys
    # onde algum on_ready/task ainda invoque uma versao especifica.
    "kayn_v262_ensure_schema",
    "kayn_v255_ensure_schema",
    "kayn_v254_ensure_schema",
    "kayn_v247_ensure_schema",
    # Cadeia acionada pelo comando !roll via vote bonus/daily missions.
    "ensure_kayn_v514_schema",
    "ensure_kayn_v508_schema",
    "ensure_daily_missions_schema",
)


def _force_required_gateway_intents() -> None:
    """Garante intents necessarios para eventos de moderacao/servidor.

    Entrada, saida, banimento e alteracao de cargos dependem do Server Members
    Intent. Isto nao escolhe canais automaticamente: as mensagens continuam indo
    somente para os canais configurados pelos comandos do proprio Kayn.
    """
    try:
        os.environ["KAYN_ENABLE_MEMBERS_INTENT"] = "true"

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

        logger.info("Kayn intents obrigatorios ativos antes do login: guilds/members/messages/message_content/voice_states")
    except Exception:
        with contextlib.suppress(Exception):
            logger.error("Falha ao ativar intents obrigatorios do Kayn", exc_info=True)


def _channel_debug_name(channel: Any) -> str:
    try:
        guild = getattr(channel, "guild", None)
        guild_name = getattr(guild, "name", None) or getattr(guild, "id", "sem-guild")
        channel_name = getattr(channel, "name", None) or getattr(channel, "id", "sem-canal")
        return f"{guild_name}#{channel_name}"
    except Exception:
        return "canal-desconhecido"


def _install_safe_message_send() -> None:
    """Evita que Missing Permissions derrube eventos como on_message.

    O Discord retorna 403/50013 quando o Kayn tenta responder em canal sem
    permissao de Enviar Mensagens ou Inserir Links/Embeds. Isso nao e bug de
    codigo nem deve estourar traceback no on_message; o envio e ignorado e o
    restante do bot continua funcionando.
    """
    try:
        if getattr(discord.abc.Messageable.send, "_kayn_safe_send", False):
            return

        async def kayn_safe_send(self, *args: Any, **kwargs: Any):
            try:
                return await _ORIGINAL_MESSAGEABLE_SEND(self, *args, **kwargs)
            except discord.Forbidden as exc:
                code = getattr(exc, "code", None)
                if code == 50013:
                    with contextlib.suppress(Exception):
                        logger.warning("Kayn sem permissao para enviar mensagem em %s; envio ignorado.", _channel_debug_name(self))
                    return None
                raise

        kayn_safe_send._kayn_safe_send = True  # type: ignore[attr-defined]
        discord.abc.Messageable.send = kayn_safe_send
        logger.info("Kayn safe send ativo: 403 Missing Permissions nao derruba eventos.")
    except Exception:
        with contextlib.suppress(Exception):
            logger.error("Falha instalando safe send do Kayn", exc_info=True)


def _is_inside_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _make_schema_guard(name: str, original: Any):
    state = {"done": False, "running": False}
    lock = threading.RLock()

    def guarded_schema(*args: Any, **kwargs: Any):
        if state["done"]:
            return None

        def run_original() -> None:
            try:
                original(*args, **kwargs)
            except Exception:
                with contextlib.suppress(Exception):
                    logger.error("Falha ao executar %s fora do event loop", name, exc_info=True)
                raise
            finally:
                with lock:
                    state["done"] = True
                    state["running"] = False

        if _is_inside_running_event_loop():
            with lock:
                if state["done"] or state["running"]:
                    return None
                state["running"] = True

            thread = threading.Thread(
                target=run_original,
                name=f"kayn-{name}-schema",
                daemon=True,
            )
            thread.start()
            with contextlib.suppress(Exception):
                logger.warning("%s chamado dentro do event loop; schema movido para thread para evitar heartbeat blocked.", name)
            return None

        with lock:
            if state["done"] or state["running"]:
                return None
            state["running"] = True
        run_original()
        return None

    guarded_schema._kayn_schema_guard = True  # type: ignore[attr-defined]
    guarded_schema._kayn_schema_original = original  # type: ignore[attr-defined]
    return guarded_schema


def _install_schema_guard() -> None:
    """Evita DDL/ensure_schema sincronas no event loop do Discord.

    Os logs de producao mostram heartbeat blocked enquanto on_ready/tasks chamam
    ensure_schema() e psycopg fica aguardando cur.execute(). Essas chamadas sao
    sincronas; se rodam no event loop, o gateway para de responder. A solucao e
    preaquecer o schema antes do login e transformar chamadas posteriores em
    no-op, ou thread quando ainda nao tiverem rodado.
    """
    global _SCHEMA_GUARD_INSTALLED
    with _SCHEMA_GUARD_LOCK:
        if _SCHEMA_GUARD_INSTALLED:
            return
        for name in _SCHEMA_ENSURE_FUNCTIONS:
            original = getattr(legacy, name, None)
            if not callable(original) or getattr(original, "_kayn_schema_guard", False):
                continue
            setattr(legacy, name, _make_schema_guard(name, original))
        _SCHEMA_GUARD_INSTALLED = True


def _prewarm_schema_before_gateway() -> None:
    """Roda migracoes antes de abrir o gateway do Discord.

    Se o banco demorar ou algum indice bloquear, isso atrasa apenas o startup.
    Depois que o gateway estiver conectado, chamadas repetidas de ensure_schema
    nao travam mais o heartbeat.
    """
    global _SCHEMA_PREWARMED
    if _SCHEMA_PREWARMED or os.getenv("KAYN_SKIP_SCHEMA_PREWARM", "").lower() in {"1", "true", "yes", "on"}:
        return

    _install_schema_guard()
    any_success = False
    for name in _SCHEMA_ENSURE_FUNCTIONS:
        ensure_schema = getattr(legacy, name, None)
        if not callable(ensure_schema):
            continue
        try:
            ensure_schema()
            any_success = True
            with contextlib.suppress(Exception):
                logger.info("Kayn schema prewarm concluido via %s antes do gateway.", name)
        except Exception:
            with contextlib.suppress(Exception):
                logger.error("Falha no prewarm de schema via %s; tentando proxima opcao.", name, exc_info=True)
    _SCHEMA_PREWARMED = True
    if not any_success:
        with contextlib.suppress(Exception):
            logger.warning("Kayn schema prewarm nao encontrou nenhuma funcao de schema executavel.")


_force_required_gateway_intents()
_install_safe_message_send()
_install_schema_guard()


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
    _install_safe_message_send()
    _install_schema_guard()
    _prewarm_schema_before_gateway()
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
