# Bot de Radio / Listening Party (independiente)

Bot de Telegram standalone, separado por completo de tu otro bot. Su única
función es tomar canciones de un canal (por rango de ID de mensaje) y
transmitirlas en la llamada de voz de cualquier grupo/canal donde lo agregues.

## 1. Qué se quitó respecto al código original

El módulo `listening_party.py` que tenías era parte de un bot más grande con
un "Catálogo Web" (canales fijos, códigos, `decodificar_datos`, etc.) atado a
la lógica de ese otro bot. Ese catálogo web **no** se copió aquí porque
depende de datos/funciones exclusivas del otro bot. Lo que sí se mantuvo
igual, funcionando de forma 100% independiente:

- `/radio [ID_CANAL] [RANGO]`
- `/radio_espera [ID_CANAL] [MSG_ID]`
- `/radio_add [ID_CANAL] [RANGO]` + botón de confirmación
- `/fiesta` (panel con botones: pausa, siguiente/anterior, mezclar, loop, volumen, ver cola, detener)
- `/ayuda_fiesta`

Es decir: exactamente el "modo radio por rango de mensajes", que es lo que
pediste — le pasas número de canal + rango y él arma la transmisión solo.

## 2. El error de PyTgCalls

```
PyTgCalls falló: cannot import name 'GroupcallInvalid' from 'pyrogram.errors'
```

Esto pasa porque la versión/fork de Pyrogram instalada en tu entorno (para
soportar Python 3.14 en Render) ya no trae esa clase de error interna, pero
PyTgCalls todavía intenta importarla por nombre exacto.

**Solución aplicada** (dentro de `start_userbot()` en `listening_party.py`):
antes de importar `pytgcalls`, se envuelve el módulo `pyrogram.errors` para
que, si falta alguna clase de error (como `GroupcallInvalid`), se genere
automáticamente una clase "dummy" en vez de fallar con `ImportError`. Esto es
el mismo truco que el código original ya usaba para `pyrogram.raw.types` y
`pyrogram.raw.functions`, solo que ahora también cubre `pyrogram.errors`.
Con esto el import ya no debería tronar, sin necesidad de cambiar versiones.

## 3. Variables de entorno necesarias en Render

| Variable            | Descripción                                                             |
|---------------------|--------------------------------------------------------------------------|
| `BOT_TOKEN`          | Token del **nuevo** bot que creaste con @BotFather para este proyecto.  |
| `USERBOT_API_ID`     | El mismo `api_id` que ya tienes (de my.telegram.org).                  |
| `USERBOT_API_HASH`   | El mismo `api_hash` que ya tienes.                                      |
| `USERBOT_SESSION`    | La misma `session_string` del userbot que ya tienes generada.          |
| `ADMIN_IDS`          | Tu ID de Telegram (y los que quieras), separados por coma. Ej: `123456789,987654321` |
| `PORT`               | La pone Render automáticamente si es "Web Service". No la toques.      |

Como ya tienes las llaves del userbot creadas, solo te falta el `BOT_TOKEN`
de este nuevo bot (créalo con @BotFather si no lo has hecho) y definir
`ADMIN_IDS` con tu ID.

## 4. Despliegue en Render

1. Sube esta carpeta (`main.py`, `listening_party.py`, `requirements.txt`) a
   un repo nuevo (o carpeta nueva dentro de tu repo).
2. En `requirements.txt`, pega las mismas versiones de `pyrogram`/fork,
   `TgCrypto` y `py-tgcalls` que ya usabas en el bot viejo (las que ya
   sabes que instalan bien en Render con Python 3.14). No hace falta
   cambiarlas.
3. Crea el servicio en Render (Web Service o Background Worker, como
   prefieras — el código soporta ambos).
   - Build command: `pip install -r requirements.txt`
   - Start command: `python main.py`
   - Asegúrate de que el entorno tenga `ffmpeg` disponible, igual que en
     tu bot anterior (mismo Build Command/Dockerfile que ya usabas ahí,
     ya que ahí ya lo tenías resuelto).
4. Agrega las variables de entorno de la tabla de arriba.
5. Agrega el bot (con `BOT_TOKEN`) como **administrador** al canal de
   canciones y al grupo/canal de transmisión. La cuenta del userbot
   (`USERBOT_SESSION`) también debe ser miembro/admin de ambos, ya que es
   la que efectivamente descarga los audios y entra a la llamada de voz.
6. Dentro del grupo de transmisión, ejecuta por ejemplo:
   `/radio -1001234567890 1-20`

## 5. Notas

- El bot es completamente independiente: puedes meterlo en cualquier canal
  de canciones y cualquier grupo/canal de transmisión, sin tocar tu otro bot.
- Puedes correr este bot y el otro al mismo tiempo sin conflicto, siempre y
  cuando cada uno tenga su propio `BOT_TOKEN`. La cuenta userbot (Pyrogram)
  sí puede ser la misma en ambos si quieres, Telegram permite que una misma
  cuenta esté en varias llamadas de voz de chats distintos a la vez.
