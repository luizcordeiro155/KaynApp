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
_EVENT_LOOP_THREAD_GUARD_INSTALLED = False
_EVENT_LOOP_THREAD_GUARD_LOCK = threading.RLock()
_EVENT_LOOP_THREAD_FUNCTIONS = (
    # Funcoes sincronas observadas travando o heartbeat via psycopg/db_conn.execute.
    # Elas sao tarefas de manutencao/cache sem retorno critico para o fluxo do comando.
    "kayn_ensure_month_snapshot",
    "cache_discord_identity_from_author",
    "save_user_identity_cache",
    "kayn_v47_apply_due_roll_resets",
)
_EVENT_LOOP_THREAD_MAX_RUNNING = 4
_ROLL_DELIVERY_GUARD_INSTALLED = False
_ROLL_DELIVERY_GUARD_LOCK = threading.RLock()


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


def _run_in_daemon_thread(name: str, func: Any, *args: Any, **kwargs: Any) -> threading.Thread:
    def target() -> None:
        try:
            func(*args, **kwargs)
        except Exception:
            with contextlib.suppress(Exception):
                logger.error("Falha ao executar %s fora do event loop", name, exc_info=True)

    thread = threading.Thread(target=target, name=f"kayn-{name}-worker", daemon=True)
    thread.start()
    return thread


def _make_event_loop_thread_guard(name: str, original: Any):
    running = {"count": 0}
    lock = threading.RLock()

    def guarded_function(*args: Any, **kwargs: Any):
        if not _is_inside_running_event_loop():
            return original(*args, **kwargs)

        with lock:
            if running["count"] >= _EVENT_LOOP_THREAD_MAX_RUNNING:
                with contextlib.suppress(Exception):
                    logger.warning("%s ignorado temporariamente: limite de workers DB em andamento atingido.", name)
                return None
            running["count"] += 1

        def target() -> None:
            try:
                original(*args, **kwargs)
            except Exception:
                with contextlib.suppress(Exception):
                    logger.error("Falha ao executar %s em worker para evitar heartbeat blocked", name, exc_info=True)
            finally:
                with lock:
                    running["count"] = max(0, running["count"] - 1)

        threading.Thread(target=target, name=f"kayn-{name}-worker", daemon=True).start()
        with contextlib.suppress(Exception):
            logger.warning("%s chamado dentro do event loop; movido para worker para evitar heartbeat blocked.", name)
        return None

    guarded_function._kayn_event_loop_thread_guard = True  # type: ignore[attr-defined]
    guarded_function._kayn_event_loop_thread_original = original  # type: ignore[attr-defined]
    return guarded_function


def _install_event_loop_thread_guards() -> None:
    """Move tarefas sincronas de manutencao/cache para workers quando chamadas no loop.

    O Kayn legado ainda chama algumas rotinas de Postgres diretamente em eventos
    do discord.py. Se uma consulta ficar presa no psycopg, o heartbeat cai e os
    cards deixam de ser entregues. Estas funcoes nao precisam bloquear o comando
    atual, entao podem rodar fora do event loop.
    """
    global _EVENT_LOOP_THREAD_GUARD_INSTALLED
    with _EVENT_LOOP_THREAD_GUARD_LOCK:
        if _EVENT_LOOP_THREAD_GUARD_INSTALLED:
            return
        for name in _EVENT_LOOP_THREAD_FUNCTIONS:
            original = getattr(legacy, name, None)
            if not callable(original) or getattr(original, "_kayn_event_loop_thread_guard", False):
                continue
            setattr(legacy, name, _make_event_loop_thread_guard(name, original))
        _EVENT_LOOP_THREAD_GUARD_INSTALLED = True


def _roll_result_get(result: Any, *keys: str) -> Any:
    for key in keys:
        try:
            if isinstance(result, dict) and result.get(key) not in (None, ""):
                return result.get(key)
            value = getattr(result, key, None)
            if value not in (None, ""):
                return value
        except Exception:
            continue
    return None


def _roll_result_fallback_message(result: Any) -> str:
    asset = _roll_result_get(result, "asset") or {}
    name = (
        _roll_result_get(result, "name", "asset_name", "title", "display_name")
        or _roll_result_get(asset, "name", "asset_name", "title", "display_name")
        or "resultado do roll"
    )
    rarity = _roll_result_get(result, "rarity") or _roll_result_get(asset, "rarity")
    kind = _roll_result_get(result, "kind", "type") or _roll_result_get(asset, "kind", "type")
    asset_id = _roll_result_get(result, "asset_id", "id") or _roll_result_get(asset, "asset_id", "id")

    parts = [f"🎲 **Resultado do roll:** **{name}**"]
    if rarity:
        parts.append(f"⭐ **Raridade:** `{rarity}`")
    if kind:
        parts.append(f"📦 **Tipo:** `{kind}`")
    if asset_id:
        parts.append(f"🆔 **ID:** `{asset_id}`")
    parts.append("⚠️ O card visual falhou, mas o roll foi entregue em texto e não foi devolvido.")
    return "\n".join(parts)[:1900]


async def _send_roll_text_fallback(ctx: Any, result: Any) -> bool:
    message = _roll_result_fallback_message(result)
    allowed_mentions = discord.AllowedMentions.none()
    for sender_name in ("reply", "send"):
        sender = getattr(ctx, sender_name, None)
        if not callable(sender):
            continue
        try:
            kwargs = {"allowed_mentions": allowed_mentions}
            if sender_name == "reply":
                kwargs["mention_author"] = False
            sent = await asyncio.wait_for(sender(message, **kwargs), timeout=8.0)
            return sent is not None
        except TypeError:
            try:
                sent = await asyncio.wait_for(sender(message), timeout=8.0)
                return sent is not None
            except Exception:
                continue
        except Exception:
            continue
    return False


def _install_roll_delivery_guard() -> None:
    """Garante que !roll entregue texto quando o card/imagem falhar.

    O legado devolve o roll quando kayn_v86_deliver_roll_result retorna False.
    Em producao isso estava acontecendo apenas com !roll, indicando falha no
    envio do card/anexo/embed, nao no sorteio. Este guard tenta o card normal e,
    se falhar, envia uma resposta textual e retorna True para evitar refund falso.
    """
    global _ROLL_DELIVERY_GUARD_INSTALLED
    with _ROLL_DELIVERY_GUARD_LOCK:
        if _ROLL_DELIVERY_GUARD_INSTALLED:
            return
        original = getattr(legacy, "kayn_v86_deliver_roll_result", None)
        if not callable(original) or getattr(original, "_kayn_roll_delivery_guard", False):
            _ROLL_DELIVERY_GUARD_INSTALLED = True
            return

        async def guarded_roll_delivery(ctx: Any, result: Any, *args: Any, **kwargs: Any) -> bool:
            try:
                ok = await original(ctx, result, *args, **kwargs)
                if ok:
                    return True
                with contextlib.suppress(Exception):
                    logger.warning("Kayn !roll: card delivery retornou falso; tentando fallback em texto.")
            except Exception:
                with contextlib.suppress(Exception):
                    logger.error("Kayn !roll: card delivery falhou; tentando fallback em texto.", exc_info=True)

            fallback_ok = await _send_roll_text_fallback(ctx, result)
            if fallback_ok:
                return True
            with contextlib.suppress(Exception):
                logger.error("Kayn !roll: fallback em texto tambem falhou.")
            return False

        guarded_roll_delivery._kayn_roll_delivery_guard = True  # type: ignore[attr-defined]
        guarded_roll_delivery._kayn_roll_delivery_original = original  # type: ignore[attr-defined]
        setattr(legacy, "kayn_v86_deliver_roll_result", guarded_roll_delivery)
        _ROLL_DELIVERY_GUARD_INSTALLED = True


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
    """Evita DDL/ensure_schema sincronas no event loop do Discord."""
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
    """Roda migracoes antes de abrir o gateway do Discord."""
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


def _prewarm_maintenance_before_gateway() -> None:
    """Executa manutencoes conhecidas antes do gateway, quando possivel."""
    for name in ("kayn_ensure_month_snapshot",):
        func = getattr(legacy, name, None)
        original = getattr(func, "_kayn_event_loop_thread_original", func)
        if not callable(original):
            continue
        try:
            original()
            with contextlib.suppress(Exception):
                logger.info("Kayn maintenance prewarm concluido via %s antes do gateway.", name)
        except Exception:
            with contextlib.suppress(Exception):
                logger.error("Falha no maintenance prewarm via %s; seguindo startup.", name, exc_info=True)


_force_required_gateway_intents()
_install_safe_message_send()
_install_schema_guard()
_install_event_loop_thread_guards()
_install_roll_delivery_guard()


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
    _install_event_loop_thread_guards()
    _install_roll_delivery_guard()
    _prewarm_schema_before_gateway()
    _prewarm_maintenance_before_gateway()
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
