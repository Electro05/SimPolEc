"""
Хранилище: SQLite на стандартной библиотеке.

Мир целиком лежит одним JSON-снимком (таблица world), история цен и
макропоказателей — в обычных таблицах, чтобы строить графики запросом.
Все изменения мира идут под глобальным замком: тик и действия игроков
не должны накладываться.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager

from . import config
from .models import World

log = logging.getLogger("simpolec")

_lock = threading.RLock()
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS world (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    data    TEXT NOT NULL
);
-- История ведётся по каждому государству отдельно: цены, склады и
-- макропоказатели у двадцати областей свои.
CREATE TABLE IF NOT EXISTS price_history (
    tick    INTEGER NOT NULL,
    region  INTEGER NOT NULL,
    good    TEXT NOT NULL,
    price   REAL NOT NULL,
    anchor  REAL NOT NULL,
    stock   REAL NOT NULL,
    demand  REAL NOT NULL,
    supply  REAL NOT NULL,
    PRIMARY KEY (tick, region, good)
);
CREATE TABLE IF NOT EXISTS macro_history (
    tick         INTEGER NOT NULL,
    country      INTEGER NOT NULL,
    gdp          REAL, population REAL, unemployment REAL,
    satisfaction REAL, avg_wage REAL, treasury REAL,
    cpi          REAL, money_supply REAL, living_standard REAL,
    PRIMARY KEY (tick, country)
);
CREATE TABLE IF NOT EXISTS world_price_history (
    tick   INTEGER NOT NULL,
    good   TEXT NOT NULL,
    price  REAL NOT NULL,
    PRIMARY KEY (tick, good)
);
CREATE TABLE IF NOT EXISTS event_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tick      INTEGER NOT NULL,
    player_id INTEGER,
    kind      TEXT NOT NULL,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_tick ON event_log(tick);
-- Чат игроков. Живёт отдельной таблицей, а не внутри снимка мира: переписка
-- растёт бесконечно, а снимок целиком перечитывается и переписывается на
-- каждое действие любого игрока.
--
-- Имя отправителя хранится рядом с сообщением нарочно: игрок может сменить
-- страну, разориться и исчезнуть, а сказанное им остаётся сказанным.
CREATE TABLE IF NOT EXISTS chat (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,      -- unix-время
    tick       INTEGER NOT NULL,
    channel    TEXT NOT NULL,      -- world / country / private
    country_id INTEGER,            -- для channel = country
    from_id    INTEGER NOT NULL,
    from_name  TEXT NOT NULL,
    to_id      INTEGER,            -- для channel = private
    to_name    TEXT,
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat_channel ON chat(channel, country_id, id);
CREATE INDEX IF NOT EXISTS ix_chat_to ON chat(to_id, id);
CREATE INDEX IF NOT EXISTS ix_chat_from ON chat(from_id, id);
"""


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


#: Ожидаемый набор колонок таблиц истории. Нужен для миграции: SQLite
#: выполняет CREATE TABLE IF NOT EXISTS молча, ничего не меняя в уже
#: существующей таблице, поэтому после смены схемы старая база продолжает
#: жить со старыми колонками и запись истории падает на каждом пейдее.
HISTORY_COLUMNS = {
    "price_history": {"tick", "region", "good", "price", "anchor", "stock",
                      "demand", "supply"},
    "macro_history": {"tick", "country", "gdp", "population", "unemployment",
                      "satisfaction", "avg_wage", "treasury", "cpi",
                      "money_supply", "living_standard"},
    "world_price_history": {"tick", "good", "price"},
}


def migrate_history(conn: sqlite3.Connection) -> list[str]:
    """Пересоздать таблицы истории, если их схема устарела.

    История — производные данные для графиков, её не жалко: проще выкинуть и
    накопить заново, чем тащить сложную миграцию. Снимок мира при этом не
    трогается, игра продолжается с того же места.
    """
    rebuilt = []
    for table, expected in HISTORY_COLUMNS.items():
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if not row:
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if have != expected:
            conn.execute(f"DROP TABLE {table}")
            rebuilt.append(table)
    return rebuilt


def init_db() -> None:
    conn = connect()
    rebuilt = migrate_history(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    if rebuilt:
        log.info("Схема истории обновлена, таблицы пересозданы: %s",
                 ", ".join(rebuilt))


def reset_db() -> None:
    """Удалить файл БД (используется тестами)."""
    close()
    for suffix in ("", "-wal", "-shm"):
        path = config.DB_PATH + suffix
        if os.path.exists(path):
            os.remove(path)


def close() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ---------------------------------------------------------------------------
def load_world() -> World | None:
    row = connect().execute("SELECT data FROM world WHERE id = 1").fetchone()
    if not row:
        return None
    return World.from_json(json.loads(row["data"]))


def save_world(world: World) -> None:
    data = json.dumps(world.to_json(), ensure_ascii=False, separators=(",", ":"))
    conn = connect()
    conn.execute(
        "INSERT INTO world (id, data) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data", (data,))
    conn.commit()


@contextmanager
def world_lock():
    """Эксклюзивный доступ к миру: загрузить, изменить, сохранить."""
    with _lock:
        world = load_world()
        if world is None:
            raise RuntimeError("Мир не инициализирован")
        yield world
        save_world(world)


@contextmanager
def world_read():
    """Только чтение — без сохранения."""
    with _lock:
        world = load_world()
        if world is None:
            raise RuntimeError("Мир не инициализирован")
        yield world


# ---------------------------------------------------------------------------
def write_price_history(rows: list[tuple]) -> None:
    """rows: (tick, region_id, good, price, anchor, stock, demand, supply)

    История цен ведётся по ОБЛАСТЯМ: рынок живёт там, и графики должны
    показывать цену конкретной области, а не среднее по стране.
    """
    conn = connect()
    conn.executemany(
        "INSERT OR REPLACE INTO price_history "
        "(tick, region, good, price, anchor, stock, demand, supply) "
        "VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def write_macro(rows: list[tuple]) -> None:
    """rows: (tick, country, gdp, pop, unemp, sat, wage, treasury, cpi, money, sol)"""
    conn = connect()
    conn.executemany(
        "INSERT OR REPLACE INTO macro_history "
        "(tick, country, gdp, population, unemployment, satisfaction, avg_wage, "
        " treasury, cpi, money_supply, living_standard) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def write_world_prices(tick: int, prices: dict[str, float]) -> None:
    conn = connect()
    conn.executemany(
        "INSERT OR REPLACE INTO world_price_history (tick, good, price) "
        "VALUES (?,?,?)", [(tick, k, v) for k, v in prices.items()])
    conn.commit()


def add_event(tick: int, player_id: int | None, kind: str, message: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO event_log (tick, player_id, kind, message) VALUES (?,?,?,?)",
        (tick, player_id, kind, message))
    conn.commit()


def prune_history(tick: int) -> None:
    # Чат живёт не пейдеями, а числом сообщений: в мёртвой партии он не растёт
    # вовсе, в людной переполнится задолго до порога истории.
    chat_prune()
    cutoff = tick - config.HISTORY_LIMIT
    if cutoff <= 0:
        return
    conn = connect()
    conn.execute("DELETE FROM price_history WHERE tick < ?", (cutoff,))
    conn.execute("DELETE FROM macro_history WHERE tick < ?", (cutoff,))
    conn.execute("DELETE FROM world_price_history WHERE tick < ?", (cutoff,))
    conn.execute("DELETE FROM event_log WHERE tick < ?", (cutoff - 150,))
    conn.commit()


def price_series(region_id: int, limit: int = 120) -> dict[str, list[dict]]:
    rows = connect().execute(
        "SELECT * FROM price_history WHERE region = ? AND tick > "
        "(SELECT COALESCE(MAX(tick),0) - ? FROM price_history) ORDER BY tick",
        (region_id, limit)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["good"], []).append(
            {"tick": r["tick"], "price": round(r["price"], 2),
             "anchor": round(r["anchor"], 2)})
    return out


def world_price_series(limit: int = 120) -> dict[str, list[dict]]:
    rows = connect().execute(
        "SELECT * FROM world_price_history WHERE tick > "
        "(SELECT COALESCE(MAX(tick),0) - ? FROM world_price_history) ORDER BY tick",
        (limit,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["good"], []).append(
            {"tick": r["tick"], "price": round(r["price"], 2)})
    return out


def macro_series(country_id: int, limit: int = 120) -> list[dict]:
    rows = connect().execute(
        "SELECT * FROM macro_history WHERE country = ? ORDER BY tick DESC LIMIT ?",
        (country_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------------------------------------------------------------------------
# Чат
# ---------------------------------------------------------------------------
def _chat_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "ts": r["ts"], "tick": r["tick"],
            "channel": r["channel"], "country_id": r["country_id"],
            "from_id": r["from_id"], "from": r["from_name"],
            "to_id": r["to_id"], "to": r["to_name"], "text": r["text"]}


def chat_add(tick: int, channel: str, country_id: int | None, from_id: int,
             from_name: str, to_id: int | None, to_name: str | None,
             text: str, ts: float) -> dict:
    conn = connect()
    cur = conn.execute(
        "INSERT INTO chat (ts, tick, channel, country_id, from_id, from_name, "
        " to_id, to_name, text) VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, tick, channel, country_id, from_id, from_name, to_id, to_name, text))
    conn.commit()
    return {"id": cur.lastrowid, "ts": ts, "tick": tick, "channel": channel,
            "country_id": country_id, "from_id": from_id, "from": from_name,
            "to_id": to_id, "to": to_name, "text": text}


def chat_channel(channel: str, country_id: int | None, limit: int) -> list[dict]:
    """Последние сообщения общего или государственного канала (по возрастанию)."""
    conn = connect()
    if country_id is None:
        rows = conn.execute(
            "SELECT * FROM chat WHERE channel = ? ORDER BY id DESC LIMIT ?",
            (channel, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM chat WHERE channel = ? AND country_id = ? "
            "ORDER BY id DESC LIMIT ?", (channel, country_id, limit)).fetchall()
    return [_chat_row(r) for r in reversed(rows)]


def chat_private(player_id: int, limit: int) -> list[dict]:
    """Вся личная переписка игрока — и отправленное, и полученное."""
    rows = connect().execute(
        "SELECT * FROM chat WHERE channel = 'private' AND (from_id = ? OR to_id = ?) "
        "ORDER BY id DESC LIMIT ?", (player_id, player_id, limit)).fetchall()
    return [_chat_row(r) for r in reversed(rows)]


def chat_last_ts(player_id: int) -> float:
    """Когда игрок писал в последний раз — для защиты от флуда."""
    row = connect().execute(
        "SELECT MAX(ts) AS ts FROM chat WHERE from_id = ?", (player_id,)).fetchone()
    return float(row["ts"] or 0.0)


def chat_delete(msg_id: int) -> bool:
    conn = connect()
    cur = conn.execute("DELETE FROM chat WHERE id = ?", (msg_id,))
    conn.commit()
    return cur.rowcount > 0


def chat_clear(channel: str, country_id: int | None) -> int:
    """Очистить канал целиком (админская мера против потока брани)."""
    conn = connect()
    if channel == "country" and country_id is not None:
        cur = conn.execute("DELETE FROM chat WHERE channel = 'country' "
                           "AND country_id = ?", (country_id,))
    else:
        cur = conn.execute("DELETE FROM chat WHERE channel = ?", (channel,))
    conn.commit()
    return cur.rowcount


def chat_prune() -> None:
    """Держать в базе только последние config.CHAT_KEEP сообщений."""
    conn = connect()
    conn.execute(
        "DELETE FROM chat WHERE id <= "
        "(SELECT COALESCE(MAX(id), 0) - ? FROM chat)", (config.CHAT_KEEP,))
    conn.commit()


def recent_events(limit: int = 30) -> list[dict]:
    rows = connect().execute(
        "SELECT tick, kind, message FROM event_log ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]
