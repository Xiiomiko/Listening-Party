# listening_party.py
"""
Módulo de Radio / Listening Party — BOT INDEPENDIENTE.

Este bot NO depende de ningún otro bot ni de catálogos externos.
Tú lo agregas como admin a:
  1) El canal donde están subidas las canciones (de ahí las lee por ID de mensaje).
  2) El grupo/canal donde se va a transmitir la llamada de voz (la "fiesta").

Con /radio [ID_CANAL] [RANGO] el bot toma los mensajes de audio de ese rango,
los descarga en streaming a disco (RAM casi cero) y los transmite en la
llamada de voz del chat donde estés usando el bot.

Optimizado para transmisiones largas (12-24h) con bajo consumo de RAM.
"""

import asyncio
import html
import importlib.util
import logging
import os
import random
import re
import sys
import tempfile
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

_LIBS_OK  = (importlib.util.find_spec("pyrogram") is not None
             and importlib.util.find_spec("pytgcalls") is not None)
_LIBS_ERR = None if _LIBS_OK else "pyrogram y/o py-tgcalls no están instalados (revisa requirements.txt)"

Client = None
PyTgCalls = None
MediaStream = None

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURACIÓN (variables de entorno)
# ─────────────────────────────────────────────────────────────────────────────
_raw_api_id = os.getenv("USERBOT_API_ID", "")
_clean_api_id = re.sub(r'\D', '', _raw_api_id)
USERBOT_API_ID = int(_clean_api_id) if _clean_api_id else 0

USERBOT_API_HASH = os.getenv("USERBOT_API_HASH", "").strip().strip('"').strip("'")
USERBOT_SESSION  = os.getenv("USERBOT_SESSION", "").strip().strip('"').strip("'")

_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_CACHE_DIR  = os.path.join(_BASE_DIR, "_party_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)

_userbot: "Optional[Client]"     = None
_pytgcalls: "Optional[PyTgCalls]" = None
_last_init_error: Optional[str]   = None

_admin_checker = None
_H = {}   # Solo guarda helpers externos: "expandir_rango"

_parties: dict[int, dict] = {}
_LOOP_LABELS = {"no": "Off ➡️", "cancion": "🔂 Canción", "lista": "🔁 Lista"}


def _is_admin(user_id: int) -> bool:
    if _admin_checker is not None:
        try:
            return bool(_admin_checker(user_id))
        except Exception:
            pass
    return False


def _get_party(chat_id: int) -> dict:
    return _parties.setdefault(chat_id, {
        "queue": [],
        "pending_queue": [],  # Cola en espera de confirmación
        "pos": -1,
        "loop": "lista",  # Por defecto en lista para la radio
        "paused": False,
        "volume": 100,
        "current_tmp_path": None,
    })


def _safe_remove(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _resolve_target_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup", "channel"):
        context.user_data["_party_chat"] = chat.id
        return chat.id
    return context.user_data.get("_party_chat")


# ─────────────────────────────────────────────────────────────────────────────
#  DESCARGADOR OPTIMIZADO PARA RAM (STREAMING A DISCO DIRECTO)
# ─────────────────────────────────────────────────────────────────────────────
async def _download_track_to_file(canal_id: int, msg_id: int, dst_path: str) -> tuple[bool, str]:
    """Usa el Userbot para streamear el archivo a disco en chunks. Consumo de RAM casi cero."""
    try:
        msg = await _userbot.get_messages(canal_id, msg_id)
        if not msg or getattr(msg, "empty", True) or not (msg.audio or msg.document):
            return False, "Mensaje borrado, invisible o no es un audio."

        # El userbot descarga directo al disco temporal
        await _userbot.download_media(msg, file_name=dst_path)
        return True, ""
    except Exception as e:
        return False, str(e)


async def _get_tracks_from_ranges(canal_id: int, rango_str: str) -> tuple[list, str]:
    """Escanea el canal por IDs de mensajes, ignorando los borrados/vacíos."""
    try:
        ids = _H["expandir_rango"](rango_str)
    except Exception as e:
        return [], f"Sintaxis de rango inválida: {e}"

    tracks = []
    last_error = ""
    chunk_size = 50  # Peticiones en lotes para no saturar Telegram
    for i in range(0, len(ids), chunk_size):
        chunk_ids = ids[i:i + chunk_size]
        try:
            msgs = await _userbot.get_messages(canal_id, chunk_ids)
            if not isinstance(msgs, list):
                msgs = [msgs]

            for msg in msgs:
                if msg and not getattr(msg, "empty", True) and (msg.audio or msg.document):
                    audio_obj = msg.audio or msg.document
                    mime = getattr(audio_obj, "mime_type", "")
                    if "audio" in mime:
                        title = getattr(audio_obj, "title", None) or getattr(audio_obj, "file_name", f"Pista {msg.id}")
                        perf = getattr(audio_obj, "performer", "")
                        full_title = f"{perf} - {title}" if perf else title
                        tracks.append({
                            "canal_origen": canal_id,
                            "id": msg.id,
                            "title": full_title
                        })
        except Exception as e:
            last_error = str(e)
            logging.error(f"[RADIO] Error leyendo mensajes {chunk_ids}: {e}")

    if not tracks and last_error:
        # No se encontró NADA y además hubo una excepción real (ej: el
        # userbot no está unido al canal, ID de canal inválido, etc.).
        # Se la mostramos al usuario en vez de esconderla en los logs.
        return [], f"El userbot no pudo leer el canal. Detalle: <code>{html.escape(last_error)}</code>\n\n<i>Tip: revisa que la cuenta del USERBOT_SESSION esté unida/suscrita a ese canal, no solo el bot.</i>"

    return tracks, ""


try:
    import imageio_ffmpeg
    _FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    # Si no está instalado imageio-ffmpeg, se intenta usar un ffmpeg del sistema.
    _FFMPEG_BIN = "ffmpeg"


async def _apply_volume(src_path: str, volume_pct: int) -> Optional[str]:
    if volume_pct == 100:
        return None
    dst_path = src_path + "_vol.mp3"
    factor = max(0.1, min(3.0, volume_pct / 100))
    try:
        proc = await asyncio.create_subprocess_exec(
            _FFMPEG_BIN, "-y", "-i", src_path, "-filter:a", f"volume={factor}", "-vn", dst_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=60)
        if proc.returncode == 0 and os.path.exists(dst_path):
            return dst_path
    except Exception as e:
        logging.warning(f"[PARTY] No se pudo ajustar el volumen: {e}")
    return None


async def _try_call(obj, names: list, *args):
    for name in names:
        fn = getattr(obj, name, None)
        if callable(fn):
            return await fn(*args)
    raise AttributeError(f"PyTgCalls no tiene ninguno de estos métodos: {names}")


# ─────────────────────────────────────────────────────────────────────────────
#  MOTOR DE REPRODUCCIÓN
# ─────────────────────────────────────────────────────────────────────────────
async def _play_index(chat_id: int, index: int) -> tuple[bool, str]:
    party = _parties.get(chat_id)
    if not party or index < 0 or index >= len(party["queue"]):
        return False, "Índice inválido o cola vacía."

    track = party["queue"][index]

    # Prepara el archivo temporal
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp3", dir=_CACHE_DIR)
    os.close(tmp_fd)

    # Descarga ultra-eficiente en RAM
    ok, err = await _download_track_to_file(track["canal_origen"], track["id"], tmp_path)
    if not ok:
        _safe_remove(tmp_path)
        logging.error(f"[PARTY] Salto pista '{track.get('title')}': {err}")
        party["pos"] = index
        return await _advance(chat_id, _skip_broken=True)

    play_path = await _apply_volume(tmp_path, party["volume"]) or tmp_path

    try:
        await _pytgcalls.play(chat_id, MediaStream(play_path))
    except Exception as e:
        err_msg = str(e)
        logging.error(f"[PARTY] Error reproduciendo en {chat_id}: {err_msg}")
        _safe_remove(tmp_path)
        if play_path != tmp_path:
            _safe_remove(play_path)
        return False, err_msg

    old_tmp = party.get("current_tmp_path")
    party["pos"] = index
    party["current_tmp_path"] = play_path
    party["paused"] = False
    _safe_remove(old_tmp)
    if play_path != tmp_path:
        _safe_remove(tmp_path)
    return True, ""


async def _advance(chat_id: int, _skip_broken: bool = False, _depth: int = 0) -> tuple[bool, str]:
    if _depth > 20:  # Límite para no ciclar infinito si todo el canal está borrado
        return False, "Demasiados errores seguidos intentando saltar mensajes borrados."

    party = _parties.get(chat_id)
    if not party or not party["queue"]:
        return False, "La cola de reproducción está vacía."

    nxt = party["pos"] + 1
    if nxt >= len(party["queue"]):
        if party["loop"] == "lista":
            nxt = 0
        else:
            await _stop_party(chat_id)
            return False, "Fin de la lista de reproducción."

    ok, err = await _play_index(chat_id, nxt)
    if not ok and _skip_broken:
        party["pos"] = nxt
        return await _advance(chat_id, _skip_broken=True, _depth=_depth + 1)
    return ok, err


async def _skip(chat_id: int) -> tuple[bool, str]:
    party = _parties.get(chat_id)
    if not party or not party["queue"]:
        return False, "Cola vacía."
    nxt = party["pos"] + 1
    if nxt >= len(party["queue"]):
        if party["loop"] == "lista":
            nxt = 0
        else:
            await _stop_party(chat_id)
            return False, "Fin de la lista de reproducción."
    return await _play_index(chat_id, nxt)


async def _previous(chat_id: int) -> tuple[bool, str]:
    party = _parties.get(chat_id)
    if not party or not party["queue"]:
        return False, "Cola vacía."
    prev = party["pos"] - 1
    if prev < 0:
        prev = len(party["queue"]) - 1 if party["loop"] == "lista" else 0
    return await _play_index(chat_id, prev)


async def _stop_party(chat_id: int) -> None:
    party = _parties.pop(chat_id, None)
    try:
        await _try_call(_pytgcalls, ["leave_call", "leave_group_call"], chat_id)
    except Exception as e:
        logging.warning(f"[PARTY] Error al salir del chat de voz {chat_id}: {e}")
    if party:
        _safe_remove(party.get("current_tmp_path"))


def _register_stream_end() -> None:
    try:
        from pytgcalls import filters as pytgcalls_filters
        @_pytgcalls.on_update(pytgcalls_filters.stream_end())
        async def _on_stream_end(_client, update):
            chat_id = getattr(update, "chat_id", None)
            party = _parties.get(chat_id)
            if not party:
                return
            if party["loop"] == "cancion":
                await _play_index(chat_id, party["pos"])
            else:
                await _advance(chat_id, _skip_broken=True)
    except Exception as e:
        logging.warning(f"[PARTY] No se pudo registrar filtro de stream_end: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  ARRANQUE DEL USERBOT
# ─────────────────────────────────────────────────────────────────────────────
async def start_userbot() -> None:
    global _userbot, _pytgcalls, Client, PyTgCalls, MediaStream, _last_init_error
    _last_init_error = None

    if not _LIBS_OK:
        _last_init_error = f"Faltan librerías: {_LIBS_ERR}"
        return

    if not (USERBOT_API_ID and USERBOT_API_HASH and USERBOT_SESSION):
        _last_init_error = "Faltan variables de entorno para el Userbot."
        return

    # ─────────────────────────────────────────────────────────────────────
    # Parches dinámicos para Pyrogram: algunos forks/versiones nuevas de
    # Pyrogram (necesarias para soportar versiones recientes de Python)
    # eliminan o renombran clases internas (raw types/functions, y también
    # algunas clases de error) que PyTgCalls todavía intenta importar por
    # nombre exacto. En vez de fallar con ImportError, generamos una clase
    # "dummy" al vuelo para cualquier nombre que falte, para que el import
    # no truene. Esto no afecta la reproducción normal de audio.
    # ─────────────────────────────────────────────────────────────────────
    try:
        import pyrogram.raw.types
        import pyrogram.raw.functions

        class _DummyTL:
            ID = 0
            def __init__(self, *args, **kwargs): pass
            def write(self, *args, **kwargs): return b""
            @classmethod
            def read(cls, *args, **kwargs): return cls()

        class _UniversalModuleWrapper:
            def __init__(self, orig_mod, default_base):
                self._orig = orig_mod
                self._base = default_base
            def __getattr__(self, name):
                if hasattr(self._orig, name):
                    return getattr(self._orig, name)
                dummy = type(name, (self._base,), {})
                setattr(self._orig, name, dummy)
                return dummy

        sys.modules['pyrogram.raw.types'] = _UniversalModuleWrapper(pyrogram.raw.types, _DummyTL)
        sys.modules['pyrogram.raw.functions'] = _UniversalModuleWrapper(pyrogram.raw.functions, _DummyTL)
    except Exception:
        pass

    try:
        import pyrogram.errors

        class _DummyRPCError(Exception):
            """Placeholder para clases de error de Pyrogram que no existen
            en esta versión/fork instalada (ej: GroupcallInvalid)."""
            ID = None
            CODE = 400
            NAME = "Unknown"
            MESSAGE = "{value}"

        class _ErrorsModuleWrapper:
            def __init__(self, orig_mod, default_base):
                self._orig = orig_mod
                self._base = default_base
            def __getattr__(self, name):
                if hasattr(self._orig, name):
                    return getattr(self._orig, name)
                base = getattr(self._orig, "RPCError", self._base)
                dummy = type(name, (base,), {})
                setattr(self._orig, name, dummy)
                return dummy

        sys.modules['pyrogram.errors'] = _ErrorsModuleWrapper(pyrogram.errors, _DummyRPCError)
    except Exception:
        pass

    from pyrogram import Client as _Client
    from pytgcalls import PyTgCalls as _PyTgCalls

    try:
        from pytgcalls.types import MediaStream as _MediaStream
    except ImportError:
        try:
            from pytgcalls.types.input_stream import AudioPiped as _MediaStream
        except ImportError:
            _MediaStream = None

    Client = _Client
    PyTgCalls = _PyTgCalls
    MediaStream = _MediaStream

    try:
        _userbot = Client(
            "radio_bot_userbot",
            api_id=USERBOT_API_ID,
            api_hash=USERBOT_API_HASH,
            session_string=USERBOT_SESSION,
            in_memory=True,
        )
        await _userbot.start()
        logging.info("[PARTY] ✅ Userbot conectado con éxito.")
    except Exception as e:
        _last_init_error = f"Error en Userbot: {e}"
        _userbot = None
        return

    # Pyrogram necesita "conocer" (resolver) un chat antes de poder leerlo
    # por ID directo, aunque la cuenta ya sea miembro de él. Sin esto, canales
    # a los que el userbot fue agregado hace poco (o a los que no le ha
    # llegado ningún update todavía) fallan con:
    #   PEER_ID_INVALID: Make sure you meet the peer before interacting with it
    # Recorrer get_dialogs() fuerza a Pyrogram a resolver y cachear TODOS los
    # chats/canales/grupos de los que la cuenta es miembro.
    try:
        logging.info("[PARTY] Sincronizando diálogos del userbot...")
        dialog_count = 0
        async for _dialog in _userbot.get_dialogs():
            dialog_count += 1
        logging.info(f"[PARTY] ✅ {dialog_count} chats sincronizados.")
    except Exception as e:
        logging.warning(f"[PARTY] No se pudieron sincronizar los diálogos del userbot: {e}")

    try:
        _pytgcalls = PyTgCalls(_userbot)
        _register_stream_end()
        await _pytgcalls.start()
        logging.info("[PARTY] ✅ PyTgCalls conectado.")
    except Exception as e:
        _last_init_error = f"PyTgCalls falló: {e}"
        _pytgcalls = None


# ─────────────────────────────────────────────────────────────────────────────
#  INTERFAZ VISUAL (PANEL DE CONTROL)
# ─────────────────────────────────────────────────────────────────────────────

def _build_panel_text(chat_id: int) -> str:
    party = _parties.get(chat_id)
    if not party or not party.get("queue"):
        return (
            f"🎧 <b>PANEL DE RADIO EN VIVO</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>Chat objetivo:</b> <code>{chat_id}</code>\n"
            f"🎵 <b>Estado:</b> Cola vacía / Sin reproducción\n\n"
            f"<i>Usa /ayuda_fiesta para ver los comandos del DJ.</i>"
        )

    pos = party["pos"]
    queue = party["queue"]
    current_track = queue[pos]["title"] if 0 <= pos < len(queue) else "—"
    estado = "⏸ En Pausa" if party["paused"] else "▶️ Reproduciendo"
    loop_txt = _LOOP_LABELS.get(party["loop"], "Off")

    return (
        f"🎧 <b>PANEL DE RADIO EN VIVO</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Chat objetivo:</b> <code>{chat_id}</code>\n"
        f"🎵 <b>Canción:</b> <b>{html.escape(current_track)}</b>\n"
        f"📊 <b>Progreso:</b> Pista {pos + 1} de {len(queue)}\n"
        f"▶️ <b>Estado:</b> {estado}\n"
        f"🔁 <b>Repetición:</b> {loop_txt}\n"
        f"🔊 <b>Volumen:</b> {party['volume']}%\n"
    )


def _build_panel_keyboard(chat_id: int, sub_menu: str = "main") -> InlineKeyboardMarkup:
    party = _parties.get(chat_id)
    has_queue = bool(party and party.get("queue"))
    is_paused = party.get("paused", False) if party else False

    if sub_menu == "volumen":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔉 50%", callback_data=f"lp_set_vol|50|{chat_id}"),
                InlineKeyboardButton("🔊 75%", callback_data=f"lp_set_vol|75|{chat_id}"),
                InlineKeyboardButton("🔊 100%", callback_data=f"lp_set_vol|100|{chat_id}"),
            ],
            [
                InlineKeyboardButton("📢 125%", callback_data=f"lp_set_vol|125|{chat_id}"),
                InlineKeyboardButton("📢 150%", callback_data=f"lp_set_vol|150|{chat_id}"),
            ],
            [
                InlineKeyboardButton("⬅️ Volver al Panel", callback_data=f"lp_menu|main|{chat_id}")
            ]
        ])

    # Menú Principal de Control
    row_controls = []
    if has_queue:
        pause_btn_text = "▶️ Reanudar" if is_paused else "⏸ Pausar"
        row_controls.append(InlineKeyboardButton("⏮ Ante.", callback_data=f"lp_prev|{chat_id}"))
        row_controls.append(InlineKeyboardButton(pause_btn_text, callback_data=f"lp_toggle_pause|{chat_id}"))
        row_controls.append(InlineKeyboardButton("⏭ Sig.", callback_data=f"lp_next|{chat_id}"))

    row_modes = []
    if has_queue:
        row_modes.append(InlineKeyboardButton("🔀 Mezclar", callback_data=f"lp_shuffle|{chat_id}"))
        row_modes.append(InlineKeyboardButton(f"🔁 {party['loop'].capitalize()}", callback_data=f"lp_toggle_loop|{chat_id}"))
        row_modes.append(InlineKeyboardButton("🔊 Vol", callback_data=f"lp_menu|volumen|{chat_id}"))

    keyboard = []
    if row_controls:
        keyboard.append(row_controls)
    if row_modes:
        keyboard.append(row_modes)

    keyboard.append([
        InlineKeyboardButton("📋 Ver Cola", callback_data=f"lp_show_queue|{chat_id}") if has_queue else InlineKeyboardButton("🔄 Refrescar", callback_data=f"lp_menu|main|{chat_id}")
    ])

    if has_queue:
        keyboard.append([
            InlineKeyboardButton("🛑 Detener y Salir de la Llamada", callback_data=f"lp_stop|{chat_id}")
        ])

    return InlineKeyboardMarkup(keyboard)


# ─────────────────────────────────────────────────────────────────────────────
#  COMANDOS DE RADIO EN VIVO (INTELIGENTES POR MENSAJE)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_help_fiesta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando de ayuda para entender la Listening Party y el Modo Radio"""
    if not _is_admin(update.effective_user.id):
        return

    texto_ayuda = (
        "🎧 <b>TUTORIAL DE RADIO EN VIVO (Listening Party)</b> 🎧\n\n"
        "Este bot está diseñado para transmisiones largas (12-24h) consumiendo CERO memoria RAM.\n\n"
        "Agrégame como <b>administrador</b> al canal donde están tus canciones (para poder leerlas) "
        "y al grupo/canal donde quieres transmitir (para poder hablar en la llamada de voz).\n\n"

        "<b>1️⃣ EMPEZAR A TRANSMITIR:</b>\n"
        "Sube tus canciones a un canal de Telegram. Para decirle al bot que reproduzca del mensaje 1 al 20:\n"
        "👉 <code>/radio -100123456789 1-20</code>\n\n"

        "<b>2️⃣ PONER MÚSICA DE ESPERA:</b>\n"
        "Sube una pista o sonido genérico al canal. Para ponerlo en bucle infinito mientras subes más cosas:\n"
        "👉 <code>/radio_espera -100123456789 21</code>\n"
        "<i>(El bot saltará a ese mensaje y lo repetirá sin parar).</i>\n\n"

        "<b>3️⃣ CARGAR LA NUEVA TANDA (CONFIRMACIÓN):</b>\n"
        "Ya subiste lo nuevo (ej: del 22 al 50). Usa este comando para prepararlas:\n"
        "👉 <code>/radio_add -100123456789 22-50</code>\n"
        "<i>El bot escaneará el canal pero NO las reproducirá. Te enviará un botón de confirmación. Al tocar ese botón, el bot apaga automáticamente la música de espera y arranca con tu nueva lista.</i>\n\n"

        "<b>🎛 PANEL VISUAL:</b>\n"
        "👉 <code>/fiesta</code> - Abre el panel con botones para pausar, mezclar, ver la cola y controlar bucles.\n\n"

        "<b>⚠️ SI ACABAS DE AGREGAR EL USERBOT A UN CANAL NUEVO:</b>\n"
        "Ejecuta primero <code>/sync_canales</code> una vez, o el bot no podrá leerlo "
        "(error <code>PEER_ID_INVALID</code>)."
    )
    await update.message.reply_text(texto_ayuda, parse_mode="HTML")


async def cmd_radio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reemplaza la cola actual con el rango del canal y empieza a reproducir."""
    if not _is_admin(update.effective_user.id): return
    if not _pytgcalls or not _userbot:
        await update.message.reply_text("⚠️ <b>El reproductor no está listo.</b>", parse_mode="HTML")
        return

    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Uso: <code>/radio [ID_CANAL] [RANGO]</code>\nEjemplo: <code>/radio -100123456 1-20</code>", parse_mode="HTML")
        return

    try: canal_id = int(context.args[0])
    except: return await update.message.reply_text("ID de canal inválido.")

    rango_str = context.args[1]
    chat_destino = _resolve_target_chat(update, context)

    msg = await update.message.reply_text("🔍 Escaneando mensajes y filtrando audios...", parse_mode="HTML")
    tracks, err = await _get_tracks_from_ranges(canal_id, rango_str)

    if err or not tracks:
        return await msg.edit_text(f"❌ No se encontraron audios válidos en ese rango.\n{err}", parse_mode="HTML")

    party = _get_party(chat_destino)
    _safe_remove(party.get("current_tmp_path"))
    party.update({"queue": tracks, "pending_queue": [], "pos": -1, "loop": "lista", "paused": False, "current_tmp_path": None})

    await msg.edit_text(f"✅ <b>¡Cargadas {len(tracks)} canciones!</b> Conectando a la llamada...", parse_mode="HTML")

    ok, err = await _advance(chat_destino, _skip_broken=True)
    if not ok:
        await context.bot.send_message(chat_destino, f"⚠️ Error: <code>{err}</code>", parse_mode="HTML")

    text = _build_panel_text(chat_destino)
    markup = _build_panel_keyboard(chat_destino, "main")
    await context.bot.send_message(chat_destino, text, reply_markup=markup, parse_mode="HTML")


async def cmd_radio_espera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Salta inmediatamente a un mensaje específico y lo pone en Bucle Infinito."""
    if not _is_admin(update.effective_user.id): return
    if not _pytgcalls or not _userbot: return

    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Uso: <code>/radio_espera [ID_CANAL] [MSG_ID_UNICO]</code>\nEjemplo: <code>/radio_espera -100123456 21</code>", parse_mode="HTML")
        return

    try: canal_id = int(context.args[0])
    except: return
    rango_str = context.args[1]
    chat_destino = _resolve_target_chat(update, context)

    msg = await update.message.reply_text("⏳ Cargando pista de espera...", parse_mode="HTML")
    tracks, err = await _get_tracks_from_ranges(canal_id, rango_str)

    if err or not tracks:
        return await msg.edit_text(f"❌ Ese mensaje no contiene un audio válido.\n{err}", parse_mode="HTML")

    party = _get_party(chat_destino)
    _safe_remove(party.get("current_tmp_path"))

    # Reemplazamos la cola, le decimos que haga bucle de ESTA única canción
    party.update({"queue": tracks, "pending_queue": [], "pos": -1, "loop": "cancion", "paused": False, "current_tmp_path": None})

    await msg.edit_text(f"✅ <b>Música de espera activada. Bucle infinito (Canción) encendido.</b>\nSube la nueva tanda y luego usa <code>/radio_add</code>.", parse_mode="HTML")
    await _advance(chat_destino, _skip_broken=True)

    text = _build_panel_text(chat_destino)
    markup = _build_panel_keyboard(chat_destino, "main")
    await context.bot.send_message(chat_destino, text, reply_markup=markup, parse_mode="HTML")


async def cmd_radio_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prepara las canciones, NO las reproduce, y pide confirmación."""
    if not _is_admin(update.effective_user.id): return

    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Uso: <code>/radio_add [ID_CANAL] [RANGO]</code>", parse_mode="HTML")
        return

    try: canal_id = int(context.args[0])
    except: return
    rango_str = context.args[1]
    chat_destino = _resolve_target_chat(update, context)

    msg = await update.message.reply_text("🔍 Escaneando las nuevas canciones en el canal...", parse_mode="HTML")
    tracks, err = await _get_tracks_from_ranges(canal_id, rango_str)

    if err or not tracks:
        return await msg.edit_text(f"❌ No se encontraron audios válidos.\n{err}", parse_mode="HTML")

    # Guardamos en la cola "pendiente"
    party = _get_party(chat_destino)
    party["pending_queue"] = tracks

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Confirmar y Reproducir", callback_data=f"lp_confirm_add|{chat_destino}")
    ]])

    await msg.edit_text(
        f"✅ <b>¡Encontradas {len(tracks)} canciones nuevas!</b>\n\n"
        f"Están listas y en espera. Toca el botón de abajo para <b>apagar la música de espera</b> y empezar a reproducir esta tanda ahora mismo.",
        parse_mode="HTML",
        reply_markup=kb
    )


async def cmd_sync_canales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-sincroniza los diálogos del userbot. Úsalo después de agregar el
    userbot a un canal/grupo nuevo, para que el bot pueda leerlo sin tener
    que reiniciar el servicio en Render."""
    if not _is_admin(update.effective_user.id):
        return

    if not _userbot:
        await update.message.reply_text("⚠️ El userbot no está conectado todavía.")
        return

    msg = await update.message.reply_text("🔄 Sincronizando canales y grupos del userbot...")
    try:
        dialog_count = 0
        async for _dialog in _userbot.get_dialogs():
            dialog_count += 1
        await msg.edit_text(
            f"✅ <b>Listo.</b> El userbot ahora reconoce {dialog_count} chats.\n"
            f"Ya puedes usar <code>/radio</code> con canales a los que lo hayas agregado recientemente.",
            parse_mode="HTML"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error al sincronizar: <code>{html.escape(str(e))}</code>", parse_mode="HTML")


async def cmd_fiesta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return

    if context.args and context.args[0].lower() in ["help", "ayuda"]:
        await cmd_help_fiesta(update, context)
        return

    if not _pytgcalls:
        err_msg = _last_init_error or "Error desconocido al iniciar"
        await update.message.reply_text(
            f"⚠️ <b>La Listening Party no está lista.</b>\n\n"
            f"<b>Diagnóstico del servidor:</b>\n"
            f"<code>{html.escape(err_msg)}</code>",
            parse_mode="HTML"
        )
        return

    chat_id = _resolve_target_chat(update, context)
    if not chat_id and context.args:
        try:
            chat_id = int(context.args[0])
            context.user_data["_party_chat"] = chat_id
        except ValueError:
            pass

    if not chat_id:
        await update.message.reply_text(
            "📍 <b>¿En qué chat quieres transmitir?</b>\n\n"
            "Usa el comando indicando el ID del grupo:\n"
            "<code>/fiesta -1001234567890</code>\n\n"
            "<i>(O ejecuta /fiesta directamente dentro del grupo)</i>",
            parse_mode="HTML"
        )
        return

    _get_party(chat_id)
    text = _build_panel_text(chat_id)
    markup = _build_panel_keyboard(chat_id, "main")
    await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
#  CALLBACKS DEL PANEL
# ─────────────────────────────────────────────────────────────────────────────

async def party_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_admin(query.from_user.id):
        await query.answer("⛔️ Sin autorización.", show_alert=True)
        return

    data = query.data.split("|")
    action = data[0]

    if action == "lp_confirm_add":
        chat_id = int(data[1])
        party = _parties.get(chat_id)
        if not party or not party.get("pending_queue"):
            await query.answer("No hay canciones en espera.", show_alert=True)
            return

        await query.answer("▶️ Iniciando la nueva tanda...")

        # 1. Pasa la cola pendiente a la cola oficial
        party["queue"] = party.pop("pending_queue")
        party["pending_queue"] = []
        party["pos"] = -1
        party["loop"] = "lista"  # Cambiamos de 'cancion' (espera) a 'lista' normal

        # 2. _advance forzará a pyTgCalls a saltar lo que esté sonando y arrancar lo nuevo
        await _advance(chat_id, _skip_broken=True)

        text = _build_panel_text(chat_id)
        markup = _build_panel_keyboard(chat_id, "main")
        try:
            await query.edit_message_text("✅ <b>Transición exitosa.</b>", parse_mode="HTML")
            await context.bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
        except Exception: pass
        return

    if action == "lp_menu":
        menu_type = data[1]
        chat_id = int(data[2])
        text = _build_panel_text(chat_id)
        markup = _build_panel_keyboard(chat_id, menu_type)
        try: await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception: pass
        await query.answer()
        return

    if action == "lp_toggle_pause":
        chat_id = int(data[1])
        party = _parties.get(chat_id)
        if party:
            if party["paused"]:
                try:
                    await _try_call(_pytgcalls, ["resume", "resume_stream"], chat_id)
                    party["paused"] = False
                    await query.answer("▶️ Reproducción reanudada.")
                except Exception as e:
                    await query.answer(f"Error: {e}", show_alert=True)
            else:
                try:
                    await _try_call(_pytgcalls, ["pause", "pause_stream"], chat_id)
                    party["paused"] = True
                    await query.answer("⏸ Reproducción pausada.")
                except Exception as e:
                    await query.answer(f"Error: {e}", show_alert=True)
            text = _build_panel_text(chat_id)
            markup = _build_panel_keyboard(chat_id, "main")
            try: await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception: pass
        return

    if action == "lp_next":
        chat_id = int(data[1])
        await query.answer("⏭ Saltando canción...")
        ok, err = await _skip(chat_id)
        text = _build_panel_text(chat_id)
        markup = _build_panel_keyboard(chat_id, "main")
        try: await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception: pass
        return

    if action == "lp_prev":
        chat_id = int(data[1])
        await query.answer("⏮ Canción anterior...")
        ok, err = await _previous(chat_id)
        text = _build_panel_text(chat_id)
        markup = _build_panel_keyboard(chat_id, "main")
        try: await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception: pass
        return

    if action == "lp_shuffle":
        chat_id = int(data[1])
        party = _parties.get(chat_id)
        if party and party["queue"]:
            resto = party["queue"][party["pos"] + 1:]
            random.shuffle(resto)
            party["queue"] = party["queue"][:party["pos"] + 1] + resto
            await query.answer("🔀 Resto de la cola mezclada con éxito.")
        return

    if action == "lp_toggle_loop":
        chat_id = int(data[1])
        party = _parties.get(chat_id)
        if party:
            modes = ["no", "cancion", "lista"]
            curr = party["loop"]
            next_idx = (modes.index(curr) + 1) % len(modes)
            party["loop"] = modes[next_idx]
            await query.answer(f"Repetición: {_LOOP_LABELS[party['loop']]}")
            text = _build_panel_text(chat_id)
            markup = _build_panel_keyboard(chat_id, "main")
            try: await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception: pass
        return

    if action == "lp_set_vol":
        vol = int(data[1])
        chat_id = int(data[2])
        party = _parties.get(chat_id)
        if party:
            party["volume"] = vol
            await query.answer(f"🔊 Volumen ajustado al {vol}%")
            text = _build_panel_text(chat_id)
            markup = _build_panel_keyboard(chat_id, "main")
            try: await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception: pass
        return

    if action == "lp_show_queue":
        chat_id = int(data[1])
        party = _parties.get(chat_id)
        if not party or not party.get("queue"):
            await query.answer("Cola vacía.", show_alert=True)
            return

        queue = party["queue"]
        pos = party["pos"]
        lineas = []
        for i, t in enumerate(queue[:15]):
            marca = "▶️ " if i == pos else f"{i+1}. "
            lineas.append(f"{marca}{html.escape(t['title'])}")

        total = len(queue)
        extra = f"\n\n<i>...y {total - 15} más</i>" if total > 15 else ""
        texto = f"📋 <b>COLA DE REPRODUCCIÓN ({total} temas)</b>\n\n" + "\n".join(lineas) + extra

        await query.answer()
        await query.message.reply_text(texto, parse_mode="HTML")
        return

    if action == "lp_stop":
        chat_id = int(data[1])
        await _stop_party(chat_id)
        await query.answer("🛑 Reproducción detenida y Userbot desconectado.")
        text = _build_panel_text(chat_id)
        markup = _build_panel_keyboard(chat_id, "main")
        try: await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception: pass
        return


# ─────────────────────────────────────────────────────────────────────────────
#  SETUP
# ─────────────────────────────────────────────────────────────────────────────
def setup_listening_party_handlers(
    application: Application,
    admin_checker,
    expandir_rango,
) -> None:
    global _admin_checker, _H
    _admin_checker = admin_checker
    _H = {
        "expandir_rango": expandir_rango,
    }

    application.add_handler(CommandHandler("ayuda_fiesta", cmd_help_fiesta))
    application.add_handler(CommandHandler("fiesta", cmd_fiesta))
    application.add_handler(CommandHandler("sync_canales", cmd_sync_canales))
    application.add_handler(CommandHandler("radio", cmd_radio))
    application.add_handler(CommandHandler("radio_espera", cmd_radio_espera))
    application.add_handler(CommandHandler("radio_add", cmd_radio_add))
    application.add_handler(CallbackQueryHandler(party_callback_handler, pattern=r"^lp_"))

    if _LIBS_OK:
        logging.info("[PARTY] ✅ Módulo de Radio / Listening Party cargado (Streaming a Disco).")
    else:
        logging.warning(f"[PARTY] Comandos /fiesta registrados pero SIN dependencias: {_LIBS_ERR}")
