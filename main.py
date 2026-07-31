# main.py
"""
Bot de Radio / Listening Party — INDEPENDIENTE.

Este bot es completamente aparte de cualquier otro bot que tengas.
Solo necesita sus propias variables de entorno (ver .env.example) y
funciona en cualquier canal/grupo donde lo agregues como administrador.

Flujo típico:
  1. Agrega este bot como ADMIN al canal donde subes las canciones.
  2. Agrega este bot (y el userbot, con la misma cuenta de USERBOT_SESSION)
     como ADMIN al grupo/canal donde vas a transmitir la llamada de voz.
  3. Dentro de ese grupo: /radio -100XXXXXXXXXX 1-20
"""

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram.ext import Application

from listening_party import setup_listening_party_handlers, start_userbot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  VARIABLES DE ENTORNO
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {
    int(x) for x in _raw_admins.replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
}


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS LOCALES (antes vivían en el bot viejo, ahora son propios de este)
# ─────────────────────────────────────────────────────────────────────────────
def expandir_rango(rango_str: str) -> list[int]:
    """
    Convierte un string como "1-20" o "1,5,7-10,15" en una lista de IDs
    de mensaje de Telegram.
    """
    ids: list[int] = []
    for parte in rango_str.split(","):
        parte = parte.strip()
        if not parte:
            continue
        if "-" in parte:
            a_str, b_str = parte.split("-", 1)
            a, b = int(a_str.strip()), int(b_str.strip())
            if a > b:
                a, b = b, a
            ids.extend(range(a, b + 1))
        else:
            ids.append(int(parte))
    return ids


def is_admin(user_id: int) -> bool:
    """Solo los IDs listados en ADMIN_IDS pueden usar los comandos del bot."""
    if not ADMIN_IDS:
        # Si no configuraste ADMIN_IDS, por seguridad NO se permite a nadie.
        logger.warning("ADMIN_IDS no está configurado: nadie puede usar los comandos.")
        return False
    return user_id in ADMIN_IDS


async def _post_init(application: Application) -> None:
    await start_userbot()


# ─────────────────────────────────────────────────────────────────────────────
#  SERVIDOR HTTP MÍNIMO (necesario si en Render lo despliegas como "Web Service")
# ─────────────────────────────────────────────────────────────────────────────
def _keep_alive_server() -> None:
    port = int(os.getenv("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK - Bot de radio activo")

        def log_message(self, *_args):
            pass  # Silencia el log de cada request

    try:
        HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except Exception as e:
        logger.warning(f"No se pudo levantar el servidor de keep-alive: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Falta la variable de entorno BOT_TOKEN.")

    # Si tu servicio en Render es "Web Service", Render exige que se abra
    # el puerto $PORT. Si es "Background Worker" este hilo no hace daño,
    # simplemente no lo usa nadie.
    threading.Thread(target=_keep_alive_server, daemon=True).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    setup_listening_party_handlers(
        application,
        admin_checker=is_admin,
        expandir_rango=expandir_rango,
    )

    logger.info("🎧 Bot de Radio iniciado. Esperando comandos...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
