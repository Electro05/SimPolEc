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
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, quote

from . import config, db
from .api import ROUTES, ApiError, Ctx, guard_request, persist_tick
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
    def send_safety_headers(self) -> None:
        """Заголовки, которые одинаковы у любого ответа.

        CSP разрешает inline-скрипты и стили не от хорошей жизни: разметка
        держит обработчики прямо в onclick, а половина блоков — в атрибуте
        style. Пользы это не отменяет: всё внешнее (чужие скрипты, картинки,
        шрифты, запросы на сторону) политика по-прежнему рубит.
        """
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")
        # HSTS обещает браузеру, что сюда ходят только по https. Отправлять
        # его без TLS нельзя: браузер запомнит обещание и перестанет пускать
        # на локальный http.
        if getattr(self.server, "tls", False) and config.HSTS_SECONDS > 0:
            self.send_header("Strict-Transport-Security",
                             f"max-age={config.HSTS_SECONDS}")

    def client_ip(self) -> str:
        """Адрес запроса — ключ счётчика неудачных входов.

        За обратным прокси настоящий адрес приходит в X-Forwarded-For, но
        верить заголовку можно только когда прокси действительно свой:
        иначе подбирающий пароль просто подставит новый адрес на каждую
        попытку. Отсюда явный ключ SIMPOLEC_TRUST_PROXY.
        """
        if config.TRUST_PROXY:
            fwd = self.headers.get("X-Forwarded-For") or ""
            first = fwd.split(",")[0].strip()
            if first:
                return first[:64]
        return self.client_address[0]

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_safety_headers()
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
        self.send_safety_headers()
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

            # Запертую попытку входа отбиваем до мира: мир читается из базы
            # целиком, и подбирающий пароль не должен оплачивать это чтение
            # чужими руками.
            guard_request(parsed.path, body, self.client_ip())

            ctxmgr = db.world_lock() if mutates else db.world_read()
            with ctxmgr as world:
                ctx = Ctx(world=world, body=body, query=query, token=token,
                          ip=self.client_ip())
                if token:
                    pid = world.sessions.get(token)
                    ctx.player = world.players.get(pid) if pid else None
                result = handler(ctx)
            self.send_json(result)

        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Кривое тело — вина отправителя, а не сервера: 400, а не 500.
            self.send_json({"error": "Некорректный JSON"}, 400)
        except Exception:
            log.exception("Ошибка обработки %s", parsed.path)
            self.send_json({"error": "Внутренняя ошибка сервера"}, 500)


# ---------------------------------------------------------------------------
class GameServer(ThreadingHTTPServer):
    """Сервер игры. С сертификатом — https, без него — обычный http."""
    daemon_threads = True
    tls_context: ssl.SSLContext | None = None
    tls = False

    def get_request(self):
        sock, addr = super().get_request()
        if self.tls_context is None:
            return sock, addr
        try:
            return self.tls_context.wrap_socket(sock, server_side=True), addr
        except OSError:
            # Обычно это кто-то постучался по http на https-порт (или браузер
            # не сошёлся в шифрах). Соединение закрываем молча: ssl.SSLError —
            # это OSError, и socketserver сам его проглотит, не роняя поток.
            sock.close()
            raise


class RedirectHandler(BaseHTTPRequestHandler):
    """Отправляет http-гостя на https и больше ничего не умеет.

    Держать открытым http нельзя — по нему уходит пароль. Но и просто
    закрыть порт неудобно: игрок наберёт адрес без схемы, браузер пойдёт на
    80-й и получит пустоту. Поэтому здесь стоит указатель.
    """
    server_version = "SimPolEc"
    protocol_version = "HTTP/1.1"
    https_port = 443

    def log_message(self, fmt, *args):
        pass

    def _redirect(self):
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        if not host:
            host = self.server.server_address[0]
        target = f"https://{host}"
        if self.https_port != 443:
            target += f":{self.https_port}"
        target += quote(self.path, safe="/?=&%#+,;:@$!*'()~")
        self.send_response(308)             # 308 сохраняет метод и тело
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    do_GET = do_POST = do_HEAD = _redirect


def build_tls_context() -> ssl.SSLContext | None:
    """Контекст TLS по путям из настроек (None — сертификата не задали)."""
    cert, key = config.TLS_CERT, config.TLS_KEY
    if not cert and not key:
        return None
    if not (cert and key):
        raise SystemExit("Для HTTPS нужны оба ключа: SIMPOLEC_TLS_CERT и "
                         "SIMPOLEC_TLS_KEY (--cert и --key)")
    for path in (cert, key):
        if not os.path.isfile(path):
            raise SystemExit(f"Файл не найден: {path}. Сертификат для пробы "
                             f"делает python tools/make_cert.py")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # Ниже 1.2 не опускаемся: старые версии протокола давно ломаные.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def start_http_redirect(host: str, http_port: int, https_port: int) -> ThreadingHTTPServer:
    handler = type("Redirect", (RedirectHandler,), {"https_port": https_port})
    srv = ThreadingHTTPServer((host, http_port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    tls_context = build_tls_context()
    bootstrap()

    # Поток заводим всегда: автопейдей теперь включается и выключается из
    # админки на ходу, а не только ключом при запуске.
    stop = threading.Event()
    threading.Thread(target=ticker, args=(stop,), daemon=True).start()

    httpd = GameServer((host, port), Handler)
    httpd.tls_context = tls_context
    httpd.tls = tls_context is not None
    scheme = "https" if httpd.tls else "http"

    redirect = None
    if httpd.tls and config.HTTP_REDIRECT_PORT:
        try:
            redirect = start_http_redirect(host, config.HTTP_REDIRECT_PORT, port)
        except OSError as exc:
            # 80-й порт обычно занят или требует прав. Это досадно, но игру
            # ронять незачем: она и без указателя работает.
            log.warning("Не удалось занять порт %s под перенаправление на "
                        "https (%s). Игра работает, но по http гостя встретит "
                        "пустота.", config.HTTP_REDIRECT_PORT, exc)

    with db.world_read() as world:
        secs, auto = world.tick_seconds, world.auto_tick
    log.info("SimPolEc запущен: %s://%s:%s", scheme, host, port)
    if httpd.tls:
        log.info("TLS включён (%s)", config.TLS_CERT)
        if redirect:
            log.info("Порт %s перенаправляет на https", config.HTTP_REDIRECT_PORT)
    elif host not in ("127.0.0.1", "localhost", "::1"):
        # Наружу без TLS пароли уходят открытым текстом — молчать об этом
        # нельзя, но и запускаться отказываться незачем: за прокси с TLS
        # такой запуск как раз нормален.
        log.warning("ВНИМАНИЕ: сервер слушает %s без TLS — пароли пойдут "
                    "открытым текстом. Задайте SIMPOLEC_TLS_CERT/KEY или "
                    "поставьте впереди https-прокси.", host)
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
        if redirect:
            redirect.shutdown()
            redirect.server_close()
