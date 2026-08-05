"""
Защита входа от подбора пароля.

Считалка неудачных попыток с растущей паузой. Ключей два и работают они
вместе:

* **имя игрока** — от подбора пароля к одному аккаунту, откуда бы ни шли
  попытки. Ботнет со ста адресами упирается в тот же счётчик, что и один
  адрес;
* **адрес** — от перебора имён с одной машины. Порог здесь выше: за одним
  адресом может сидеть целая школа через NAT, и запирать её из-за одного
  забывчивого игрока незачем.

После LOGIN_FAIL_LIMIT промахов подряд ключ запирается на LOGIN_LOCK_SECONDS,
и каждый следующий запор вдвое длиннее предыдущего — до LOGIN_LOCK_MAX. Это
и есть главная мера: перебор словаря из тысячи паролей растягивается с минут
на годы, а живому человеку, промахнувшемуся четыре раза, стоит минуту
подождать.

Счётчики живут в памяти процесса и обнуляются при перезапуске сервера.
Перезапуск — не тот рычаг, до которого дотянется подбирающий пароль, так что
хранить их в базе смысла нет.
"""
from __future__ import annotations

import threading
import time

from . import config

# ключ → [промахов в окне, время первого промаха, до какого времени заперт,
#         сколько раз уже запирали]
_hits: dict[str, list[float]] = {}
_lock = threading.Lock()

_FAILS, _FIRST, _UNTIL, _LOCKS = 0, 1, 2, 3

# Записи без единого обращения дольше этого срока выбрасываются: словарь не
# должен расти вместе с числом когда-либо заходивших адресов.
_FORGET = 24 * 3600.0
_SWEEP_EVERY = 512          # раз во столько обращений подметаем словарь
_calls = 0


# ---------------------------------------------------------------------------
def _sweep(now: float) -> None:
    """Выбросить давно неинтересные записи (вызывать под _lock)."""
    stale = [k for k, v in _hits.items()
             if now - v[_FIRST] > _FORGET and now > v[_UNTIL]]
    for k in stale:
        _hits.pop(k, None)


def _entry(key: str, now: float) -> list[float]:
    e = _hits.get(key)
    if e is None:
        e = _hits[key] = [0.0, now, 0.0, 0.0]
    return e


def _retry_after(key: str, now: float) -> float:
    """Сколько секунд ключу осталось сидеть взаперти (0 — свободен)."""
    e = _hits.get(key)
    if e is None:
        return 0.0
    return max(0.0, e[_UNTIL] - now)


def _fail(key: str, limit: int, now: float) -> None:
    """Отметить промах и, если их набралось довольно, запереть ключ."""
    e = _entry(key, now)
    if now - e[_FIRST] > config.LOGIN_FAIL_WINDOW:
        e[_FAILS], e[_FIRST] = 0.0, now       # окно прошло — счёт заново
    e[_FAILS] += 1
    if e[_FAILS] >= limit:
        e[_LOCKS] += 1
        # Каждый следующий запор вдвое длиннее: первый — минута, десятый — час.
        pause = min(config.LOGIN_LOCK_SECONDS * (2 ** (e[_LOCKS] - 1)),
                    config.LOGIN_LOCK_MAX)
        e[_UNTIL] = now + pause
        e[_FAILS], e[_FIRST] = 0.0, now


# ---------------------------------------------------------------------------
def login_retry_after(ip: str, username: str) -> float:
    """Сколько секунд ждать до следующей попытки входа (0 — можно пробовать)."""
    global _calls
    now = time.time()
    with _lock:
        _calls += 1
        if _calls % _SWEEP_EVERY == 0:
            _sweep(now)
        return max(_retry_after("u:" + username.lower(), now),
                   _retry_after("ip:" + ip, now))


def login_failed(ip: str, username: str) -> None:
    """Пароль не подошёл: записать промах обоим ключам."""
    now = time.time()
    with _lock:
        _fail("u:" + username.lower(), config.LOGIN_FAIL_LIMIT, now)
        _fail("ip:" + ip, config.LOGIN_IP_FAIL_LIMIT, now)


def login_ok(ip: str, username: str) -> None:
    """Вход удался: счётчик промахов обнуляем.

    Память о прошлых запорах (_LOCKS) при этом остаётся: иначе подбирающий,
    у которого есть один свой аккаунт, сбрасывал бы себе паузу удачным входом
    после каждой серии промахов. Она истечёт сама вместе с записью.
    """
    now = time.time()
    with _lock:
        for key in ("u:" + username.lower(), "ip:" + ip):
            e = _hits.get(key)
            if e is not None:
                e[_FAILS], e[_FIRST], e[_UNTIL] = 0.0, now, 0.0


# ---------------------------------------------------------------------------
def register_retry_after(ip: str) -> float:
    """Не слишком ли часто с этого адреса заводят аккаунты."""
    now = time.time()
    key = "reg:" + ip
    with _lock:
        e = _hits.get(key)
        if e is None:
            return 0.0
        if now - e[_FIRST] > config.REGISTER_WINDOW:
            return 0.0
        if e[_FAILS] < config.REGISTER_LIMIT:
            return 0.0
        return max(1.0, config.REGISTER_WINDOW - (now - e[_FIRST]))


def register_done(ip: str) -> None:
    now = time.time()
    key = "reg:" + ip
    with _lock:
        e = _entry(key, now)
        if now - e[_FIRST] > config.REGISTER_WINDOW:
            e[_FAILS], e[_FIRST] = 0.0, now
        e[_FAILS] += 1


# ---------------------------------------------------------------------------
def human_pause(seconds: float) -> str:
    """«через 45 секунд» / «через 12 минут» — для текста ошибки."""
    s = int(seconds + 0.999)
    if s < 60:
        return f"{s} сек"
    m = (s + 59) // 60
    if m < 60:
        return f"{m} мин"
    return f"{(m + 59) // 60} ч"


def reset() -> None:
    """Забыть всё (нужно тестам)."""
    with _lock:
        _hits.clear()
