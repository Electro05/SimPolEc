"""
HTTP-сервер SimPolEc на стандартной библиотеке.

Никаких зависимостей: ThreadingHTTPServer + маршруты из api.py + фоновый
поток, который каждые TICK_SECONDS проводит пейдей.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, db
from .api import ROUTES, ApiError, Ctx, persist_tick
from .economy.engine import run_tick
from .economy.seed import build_world, migrate_world

log = logging.getLogger("simpolec")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ---------------------------------------------------------------------------
def bootstrap() -> None:
    db.init_db()
    if db.load_world() is None:
        log.info("Создаём новый мир...")
        world = build_world()
        db.save_world(world)
    else:
        with db.world_lock() as world:
            # Длину пейдея и автопейдей теперь крутит и админка, поэтому снимок
            # мира — источник истины. Переменные окружения перебивают его только
            # тогда, когда заданы явно: иначе каждый перезапуск сервера отменял
            # бы то, что администратор выставил в игре.
            if config.TICK_SECONDS_SET or world.tick_seconds <= 0:
                world.tick_seconds = config.TICK_SECONDS
            if config.AUTO_TICK_SET:
                world.auto_tick = config.AUTO_TICK
            # Старый снимок мира не знает про товары и отрасли, добавленные
            # после его сохранения: чертежи хранятся внутри самого мира.
            added = migrate_world(world)
            if added:
                log.info("Мир дополнен (%d): %s", len(added),
                         ", ".join(added[:8]) + ("…" if len(added) > 8 else ""))


def ticker(stop: threading.Event) -> None:
    """Фоновый планировщик пейдеев."""
    while not stop.wait(1.0):
        try:
            with db.world_lock() as world:
                if not world.auto_tick:
                    continue
                if time.time() - world.last_tick_at < world.tick_seconds:
                    continue
                res = run_tick(world)
                try:
                    persist_tick(world, res)
                except Exception:
                    log.exception("Не удалось записать историю пейдея")
            log.info("Пейдей #%s | ВВП %.0f | ИПЦ %.3f | население %.0f | "
                     "безработица %.1f%%", res["tick"], res["gdp"], res["cpi"],
                     res["population"], res["unemployment"] * 100)
        except Exception:
            log.exception("Ошибка в пейдее")


# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "SimPolEc"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # не засоряем консоль

    # --- ответы --------------------------------------------------------
    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: str) -> None:
        if not os.path.isfile(path):
            self.send_json({"error": "Не найдено"}, 404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # --- маршрутизация -------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api("GET", parsed)
            return
        rel = parsed.path.lstrip("/") or "index.html"
        safe = os.path.normpath(rel).replace("\\", "/")
        if safe.startswith(".."):
            self.send_json({"error": "Нельзя"}, 403)
            return
        self.send_file(os.path.join(STATIC_DIR, safe))

    def do_POST(self):
        self.handle_api("POST", urlparse(self.path))

    def handle_api(self, method: str, parsed) -> None:
        route = ROUTES.get((method, parsed.path))
        if not route:
            self.send_json({"error": "Неизвестный запрос"}, 404)
            return
        handler, mutates = route

        try:
            body = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(body, dict):
                    raise ApiError(400, "Тело запроса должно быть объектом")

            auth = self.headers.get("Authorization") or ""
            token = auth[7:] if auth.startswith("Bearer ") else None
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

            ctxmgr = db.world_lock() if mutates else db.world_read()
            with ctxmgr as world:
                ctx = Ctx(world=world, body=body, query=query, token=token)
                if token:
                    pid = world.sessions.get(token)
                    ctx.player = world.players.get(pid) if pid else None
                result = handler(ctx)
            self.send_json(result)

        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except json.JSONDecodeError:
            self.send_json({"error": "Некорректный JSON"}, 400)
        except Exception:
            log.exception("Ошибка обработки %s", parsed.path)
            self.send_json({"error": "Внутренняя ошибка сервера"}, 500)


# ---------------------------------------------------------------------------
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    bootstrap()

    # Поток заводим всегда: автопейдей теперь включается и выключается из
    # админки на ходу, а не только ключом при запуске.
    stop = threading.Event()
    threading.Thread(target=ticker, args=(stop,), daemon=True).start()

    httpd = ThreadingHTTPServer((host, port), Handler)
    with db.world_read() as world:
        secs, auto = world.tick_seconds, world.auto_tick
    log.info("SimPolEc запущен: http://%s:%s", host, port)
    log.info("Пейдей каждые %s сек (%.1f мин), автопейдей %s. Ctrl+C — остановить.",
             secs, secs / 60, "включён" if auto else "выключен")
    if config.ADMIN_USERS:
        log.info("Администраторы: %s", ", ".join(sorted(config.ADMIN_USERS)))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Останавливаемся...")
    finally:
        stop.set()
        httpd.server_close()
