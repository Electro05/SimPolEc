"""
Запуск SimPolEc.

    python run.py                  → http://127.0.0.1:8000, пейдей раз в 15 минут
    SIMPOLEC_TICK=10 python run.py → пейдей раз в 10 секунд (для отладки)

Наружу (не на 127.0.0.1) поднимать только по HTTPS — иначе пароли игроков
пойдут открытым текстом:

    python run.py --host 0.0.0.0 --port 443 --cert cert.pem --key key.pem
    python tools/make_cert.py      → сертификат для локальной пробы

Переменные окружения:
    SIMPOLEC_TICK        длина пейдея в секундах (по умолчанию 900)
    SIMPOLEC_AUTOTICK    0 = не тикать автоматически, только кнопкой
    SIMPOLEC_DB          путь к файлу базы (по умолчанию simpolec.db)
    SIMPOLEC_TLS_CERT    сертификат (.pem) — включает https
    SIMPOLEC_TLS_KEY     закрытый ключ к нему
    SIMPOLEC_HTTP_PORT   порт, с которого перенаправлять на https (0 = не надо)
    SIMPOLEC_TRUST_PROXY 1 = верить X-Forwarded-For (только за своим прокси)
"""
import argparse
import os

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SimPolEc — симулятор экономики")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--cert", help="файл сертификата (.pem) — включает HTTPS")
    ap.add_argument("--key", help="закрытый ключ к сертификату")
    ap.add_argument("--http-port", type=int, default=None,
                    help="порт, который перенаправляет на https (напр. 80)")
    args = ap.parse_args()

    # Ключи командной строки — те же настройки, только с другой стороны.
    # Кладём их в окружение ДО импорта app.config: он читает окружение один
    # раз при загрузке модуля.
    if args.cert:
        os.environ["SIMPOLEC_TLS_CERT"] = args.cert
    if args.key:
        os.environ["SIMPOLEC_TLS_KEY"] = args.key
    if args.http_port is not None:
        os.environ["SIMPOLEC_HTTP_PORT"] = str(args.http_port)

    from app.main import serve
    serve(args.host, args.port)
