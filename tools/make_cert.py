"""
Свой (самоподписанный) сертификат для локальной пробы HTTPS.

    python tools/make_cert.py                 → cert.pem и key.pem рядом с run.py
    python tools/make_cert.py --host 192.168.1.50 --days 30

Дальше:

    python run.py --cert cert.pem --key key.pem

Браузер такому сертификату не поверит и покажет предупреждение — это нормально:
подписан он сам собой, а не удостоверяющим центром. Для игры в локальной сети
этого хватает: пароль по проводу всё равно идёт зашифрованным. Для сервера в
интернете берите настоящий сертификат — бесплатный от Let's Encrypt (certbot)
или тот, что выпишет ваш обратный прокси (Caddy делает это сам).

Нужен установленный openssl: в Windows он приходит вместе с Git (Git Bash),
в Linux и macOS обычно уже есть.
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser(description="самоподписанный сертификат")
    ap.add_argument("--host", default="localhost",
                    help="имя или адрес, по которому будут заходить")
    ap.add_argument("--days", type=int, default=365, help="срок годности")
    ap.add_argument("--cert", default=os.path.join(ROOT, "cert.pem"))
    ap.add_argument("--key", default=os.path.join(ROOT, "key.pem"))
    args = ap.parse_args()

    openssl = shutil.which("openssl")
    if not openssl:
        print("openssl не найден. В Windows он есть в Git Bash "
              "(C:\\Program Files\\Git\\usr\\bin\\openssl.exe) — запустите "
              "скрипт оттуда или добавьте его в PATH.", file=sys.stderr)
        return 2

    for path in (args.cert, args.key):
        if os.path.exists(path):
            print(f"{path} уже есть — удалите его, если хотите выпустить "
                  f"новый сертификат.", file=sys.stderr)
            return 1

    # SAN обязателен: браузеры давно не смотрят на CN. Адрес кладём и как
    # DNS-имя, и как IP — заранее не известно, чем host окажется.
    alt = f"DNS:{args.host},DNS:localhost,IP:127.0.0.1"
    if args.host.replace(".", "").isdigit():
        alt = f"IP:{args.host},DNS:localhost,IP:127.0.0.1"

    cmd = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
           "-keyout", args.key, "-out", args.cert,
           "-days", str(args.days), "-subj", f"/CN={args.host}",
           "-addext", f"subjectAltName={alt}"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stderr.strip() or "openssl не справился", file=sys.stderr)
        return res.returncode

    # Ключ — секрет: пусть его читает только владелец. На Windows chmod
    # почти ничего не значит, поэтому молча пропускаем неудачу.
    try:
        os.chmod(args.key, 0o600)
    except OSError:
        pass

    print(f"Готово:\n  сертификат  {args.cert}\n  ключ        {args.key}\n")
    print(f"Запуск:\n  python run.py --cert {os.path.relpath(args.cert, ROOT)} "
          f"--key {os.path.relpath(args.key, ROOT)}")
    print("\nБраузер предупредит, что сертификат самоподписанный, — "
          "для локальной пробы так и должно быть.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
