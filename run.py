"""
Запуск SimPolEc.

    python run.py                  → http://127.0.0.1:8000, пейдей раз в 15 минут
    SIMPOLEC_TICK=10 python run.py → пейдей раз в 10 секунд (для отладки)

Переменные окружения:
    SIMPOLEC_TICK      длина пейдея в секундах (по умолчанию 900)
    SIMPOLEC_AUTOTICK  0 = не тикать автоматически, только кнопкой
    SIMPOLEC_DB        путь к файлу базы (по умолчанию simpolec.db)
"""
import argparse

from app.main import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SimPolEc — симулятор экономики")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    serve(args.host, args.port)
