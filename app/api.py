"""
Игровое API. Чистые функции над миром — HTTP-обвязка живёт в main.py.

Каждый обработчик получает Ctx и возвращает словарь, который сервер отдаст
как JSON. Ошибки выбрасываются через ApiError. Большинство данных привязано
к государству игрока (player.country_id) — у каждого государства свои цены,
казна и политика.
"""
from __future__ import annotations

import logging
import random
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import config, db, ratelimit
from .auth import hash_password, new_token, verify_password
from .economy.engine import (
    _add_alliance,
    accept_demands as engine_accept_demands,
    bankruptcy_exit_level,
    chain_bonus_for,
    citizen_players as engine_citizens,
    confidence_tally as engine_confidence,
    declare_war as engine_declare_war,
    level_cost, make_peace,
    chamber_levels as engine_chamber_levels,
    country_access, region_access,
    region_cpi as engine_region_cpi,
    release_bankrupt as engine_release_bankrupt,
    resolve_annexation as engine_resolve_annexation,
    run_tick,
    upkeep_needs,
)
from .economy.pricing import U_MAX, U_MIN, price_bounds
from .economy import politics, society
from .models import Building, Player, World

log = logging.getLogger("simpolec")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def is_admin(player: Player | None) -> bool:
    """Допущен ли игрок в админку.

    Право берётся из двух мест: флаг в самом мире (его раздаёт администратор) и
    список имён при запуске сервера. Второй нужен, чтобы первого администратора
    вообще было откуда взять — внутри игры выдать его самому себе нельзя.
    """
    if player is None or player.is_state:
        return False
    return bool(player.is_admin
                or player.username.strip().lower() in config.ADMIN_USERS)


@dataclass
class Ctx:
    world: World
    body: dict[str, Any]
    query: dict[str, str]
    token: str | None
    player: Player | None = None
    # Адрес, с которого пришёл запрос. Нужен только входу и регистрации —
    # по нему считаются промахи (см. ratelimit).
    ip: str = "?"

    def require_player(self) -> Player:
        if self.player is None:
            raise ApiError(401, "Нужно войти в игру")
        return self.player

    def player_country(self):
        """Государство игрока (или None, если игрок/страна не найдены)."""
        p = self.player
        return self.world.countries.get(p.country_id) if p else None

    def require_leader(self) -> tuple[Player, "Country"]:
        """Игрок должен быть действующим лидером своего государства."""
        p = self.require_player()
        c = self.world.countries.get(p.country_id)
        if c is None or c.leader_id != p.id:
            raise ApiError(403, "Нужны полномочия лидера государства")
        return p, c

    def require_admin(self) -> Player:
        p = self.require_player()
        if not is_admin(p):
            raise ApiError(403, "Нужны права администратора")
        return p

    def need(self, key: str) -> Any:
        if key not in self.body:
            raise ApiError(400, f"Не хватает поля «{key}»")
        return self.body[key]


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------
def login_gate(ip: str, username: str) -> None:
    """Пускать ли к проверке пароля. Заперт — 429 и сколько ждать."""
    pause = ratelimit.login_retry_after(ip, username)
    if pause:
        raise ApiError(429, f"Слишком много неудачных попыток входа. "
                            f"Попробуйте через {ratelimit.human_pause(pause)}")


def guard_request(path: str, body: dict[str, Any], ip: str) -> None:
    """Проверки, которые дешевле сделать ДО загрузки мира.

    Мир на каждый запрос читается из базы целиком, поэтому запертую попытку
    входа стоит отбить раньше, чем она до этого чтения доберётся: иначе
    подбирающий пароль хоть и не войдёт, но нагрузит сервер как обычный
    игрок. Внутри login() та же проверка стоит ещё раз — на случай, если
    обработчик позовут в обход HTTP-обвязки.
    """
    if path == "/api/login":
        login_gate(ip, str(body.get("username") or "").strip())


def check_password(username: str, password: str) -> None:
    """Пароль, который не подберут с первой попытки.

    Требование одно — длина: короткий пароль перебирается быстрее, чем
    успевает сработать любая пауза между попытками. Совпадение с именем
    отсекаем отдельно: его подбирающий пробует первым.
    """
    if len(password) < config.PASSWORD_MIN_LEN:
        raise ApiError(400, f"Пароль должен быть не короче "
                            f"{config.PASSWORD_MIN_LEN} символов")
    if password.strip().lower() == username.strip().lower():
        raise ApiError(400, "Пароль не должен повторять имя")


def register(ctx: Ctx) -> dict:
    username = str(ctx.need("username")).strip()
    password = str(ctx.need("password"))
    country_id = int(ctx.body.get("country_id") or 0)
    # Регистрация — тоже вход, только дорогой: она заводит игрока в мире и
    # считает ему заселение. Пять аккаунтов в час с адреса хватит любому
    # живому человеку и не хватит тому, кто набивает мир пустышками.
    pause = ratelimit.register_retry_after(ctx.ip)
    if pause:
        raise ApiError(429, f"С этого адреса уже зарегистрировано "
                            f"{config.REGISTER_LIMIT} игроков. "
                            f"Следующего можно завести через "
                            f"{ratelimit.human_pause(pause)}")
    if not 3 <= len(username) <= 24:
        raise ApiError(400, "Имя должно быть от 3 до 24 символов")
    check_password(username, password)
    w = ctx.world
    if country_id not in w.countries or not w.countries[country_id].alive:
        raise ApiError(400, "Такого государства нет. Выберите область на карте.")
    if any(p.username.lower() == username.lower() for p in w.players.values()):
        raise ApiError(400, "Такое имя уже занято")

    salt, pw = hash_password(password)
    player = Player(id=w.next_player_id, username=username, password_hash=pw,
                    salt=salt, cash=config.STARTING_CAPITAL, country_id=country_id)
    w.players[player.id] = player
    w.next_player_id += 1

    # Если в государстве ещё нет лидера (управляет AI) — первый гражданин,
    # кто зарегистрировался, временно становится лидером до ближайших выборов.
    country = w.countries[country_id]
    if country.leader_id is None:
        country.leader_id = player.id
        player.governor_of = country_id

    # ЗАСЕЛЕНИЕ. В первые config.JOIN_WINDOW_TICKS пейдеев жизни мира вместе с
    # промышленником в страну приходит и его рынок сбыта: население прирастает
    # по всем сословиям сразу (см. society.queue_settlers). Так стартовый размер
    # страны определяется не жребием, а тем, скольких она привлекла: мир
    # начинается двадцатью одинаковыми крестьянскими странами, и той, которую
    # выбрали пятеро, покупателей нужно впятеро больше.
    #
    # Окно закрывается вместе с началом партии. Дальше страна растёт одной
    # рождаемостью, а опоздавший приходит на готовый рынок — тот, что вырастили
    # и застолбили первые.
    coming = 0.0
    if w.tick < config.JOIN_WINDOW_TICKS:
        rng = random.Random((w.tick * 7919 + player.id * 104729) & 0x7FFFFFFF)
        coming = sum(society.queue_settlers(country, rng).values())

    token = new_token()
    w.sessions[token] = player.id
    ratelimit.register_done(ctx.ip)
    db.add_event(w.tick, player.id, "join",
                 f"{username} открывает своё дело в государстве {country.name}"
                 + (f". За {config.JOIN_GROWTH_TICKS} пейдеев в страну прибудет "
                    f"{coming:,.0f} человек" if coming else ""))
    return {"token": token, "username": username,
            "is_governor": player.is_governor, "country_id": country_id,
            "country_name": country.name,
            "settlers": round(coming),
            "settler_ticks": config.JOIN_GROWTH_TICKS,
            # Открыто ли ещё окно заселения и сколько пейдеев до его конца
            "join_window_open": w.tick < config.JOIN_WINDOW_TICKS,
            "join_window_left": max(0, config.JOIN_WINDOW_TICKS - w.tick)}


def login(ctx: Ctx) -> dict:
    username = str(ctx.need("username")).strip()
    password = str(ctx.need("password"))
    w = ctx.world

    # Подбор пароля упирается сюда. После пяти промахов по одному имени (или
    # двадцати с одного адреса) вход запирается на минуту, и каждый следующий
    # запор вдвое длиннее предыдущего: словарь на тысячу паролей перестаёт
    # проходиться за вечер.
    login_gate(ctx.ip, username)

    player = next((p for p in w.players.values()
                   if p.username.lower() == username.lower() and not p.is_state), None)
    if player is None:
        # Пароль всё равно хешируем — по времени ответа не должно быть видно,
        # есть такой игрок или нет: иначе имена перебираются в обход счётчика.
        hash_password(password)
        ratelimit.login_failed(ctx.ip, username)
        raise ApiError(401, "Неверное имя или пароль")
    if not verify_password(password, player.salt, player.password_hash):
        ratelimit.login_failed(ctx.ip, username)
        raise ApiError(401, "Неверное имя или пароль")

    ratelimit.login_ok(ctx.ip, username)
    token = new_token()
    w.sessions[token] = player.id
    c = w.countries.get(player.country_id)
    return {"token": token, "username": player.username,
            "is_governor": player.is_governor,
            "country_id": player.country_id,
            "country_name": c.name if c else "—"}


def logout(ctx: Ctx) -> dict:
    if ctx.token:
        ctx.world.sessions.pop(ctx.token, None)
    return {"ok": True}


def me(ctx: Ctx) -> dict:
    p = ctx.require_player()
    w = ctx.world
    c = w.countries.get(p.country_id)
    # Склад показывается по странам: товар иностранного завода лежит на рынке
    # той страны, где он произведён, и оценивается по её ценам.
    warehouse = []
    for city_id, store in p.warehouses.items():
        city = w.cities.get(city_id)
        country = w.countries.get(city.country_id) if city else None
        for k, q in sorted(store.items(), key=lambda kv: -kv[1]):
            if q <= 0.5 or k not in w.goods:
                continue
            local = city.goods.get(k) if city else None
            price = local.price if local else w.goods[k].anchor
            warehouse.append({
                "good": k, "name": w.goods[k].name,
                "region_id": city_id,
                "region": city.name if city else "—",
                "country": country.name if country else "—",
                "foreign": bool(country and country.id != p.country_id),
                "qty": round(q, 1), "value": round(q * price, 2)})
    warehouse.sort(key=lambda r: -r["value"])
    return {
        "id": p.id, "username": p.username,
        "cash": round(p.cash, 2),
        "net_worth": round(w.net_worth(p), 2),
        "is_governor": p.is_governor,
        "country_id": p.country_id,
        "country_name": c.name if c else "—",
        "bankrupt": p.bankrupt,
        "bankruptcy_limit": c.bankruptcy_limit if c else 0.0,
        # Сколько не хватает, чтобы государство вообще могло закрыть дело
        "rescue_cost": round(max(0.0, bankruptcy_exit_level(c) - p.cash), 2)
                       if c else 0.0,
        "halted": sum(1 for b in w.player_buildings(p.id) if b.halted),
        "warehouse": warehouse,
        "is_admin": is_admin(p),
        "muted": p.muted(time.time()),
        "mute_until": p.mute_until,
        "mute_forever": p.mute_forever,
        "mute_reason": p.mute_reason,
    }


# ---------------------------------------------------------------------------
# Карта и государства
# ---------------------------------------------------------------------------
def map_view(ctx: Ctx) -> dict:
    """Карта-граф: узлы-государства с координатами, соседями и сводкой."""
    w = ctx.world
    nodes = []
    mine = ctx.player.country_id if ctx.player else None
    for city in w.cities.values():
        country = w.countries.get(city.country_id)
        if country is None:
            continue
        node = w.map_graph.get(city.id)
        leader = w.players.get(country.leader_id) if country.leader_id else None
        nodes.append({
            # узел карты — ОБЛАСТЬ: захваченная не исчезает, а меняет цвет,
            # и соседи могут отбить её обратно
            "id": city.id, "name": city.name,
            "country_id": country.id, "country": country.name,
            "color": country.color,
            "x": node.x if node else 0.0, "y": node.y if node else 0.0,
            "neighbors": list(node.neighbors) if node else [],
            "capital": city.id == country.capital_city_id,
            "population": round(city.population),
            "satisfaction": round(city.satisfaction, 4),
            "living_standard": round(society.region_living_standard(city), 3),
            "unrest": round(city.unrest, 3),
            "revolt": city.revolt_ticks > 0,
            "leader": leader.username if leader else "AI (без лидера)",
            "leader_is_ai": country.leader_id is None,
            "is_mine": country.id == (mine if mine is not None else -1),
            "treasury": round(country.treasury),
            "gdp": round(country.gdp),
            "foreign_investment_open": country.foreign_investment_open,
            "regions": len(w.country_regions(country.id)),
            "army": round(society.army_size(w, country)),
            # Размер страны одним словом: губерния перед тобой или держава.
            "size": society.size_title(_country_pop(w, country.id)),
            "size_rank": society.size_rank(_country_pop(w, country.id)),
            "country_population": round(_country_pop(w, country.id)),
            # РАЗВЕДКА: сколько солдат сосед держит на фронте ПРОТИВ НАС.
            # Только против нас — что стоит у него на других границах, нас не
            # касается и знать неоткуда. Число грубое: точной численности чужих
            # полков не видно ниоткуда, видно «около стольких-то».
            "front_vs_me": _front_intel(w, country, mine),
            "at_war": bool(w.wars_of(country.id)),
            "at_war_with_me": mine is not None and w.at_war(mine, country.id),
            "allied_with_me": mine is not None and w.allied(mine, country.id),
        })
    return {"nodes": nodes}


def _country_pop(w: World, country_id: int) -> float:
    return sum(c.population for c in w.cities.values()
               if c.country_id == country_id)


def _front_intel(w: World, country, viewer_id: int | None) -> int | None:
    """Сколько солдат страна держит против наблюдателя — грубо, «около».

    None означает «нам это неоткуда знать»: смотрим на самих себя или граничим
    не мы. Точное число не отдаётся намеренно — разведка не всеведуща, и
    решение «лезть или не лезть» должно приниматься с некоторой неуверенностью.
    """
    if viewer_id is None or viewer_id == country.id:
        return None
    if country.id not in w.neighbor_countries(viewer_id):
        return None
    men = society.front_soldiers(country, viewer_id)
    if men <= 0:
        return 0
    step = max(1.0, men * config.FRONT_INTEL_ROUNDING)
    return int(round(men / step) * round(step))


def countries_list(ctx: Ctx) -> dict:
    """Краткий список государств — для выбора при регистрации."""
    w = ctx.world
    rows = []
    for cid, country in w.countries.items():
        if not country.alive or country.capital_city_id not in w.cities:
            continue
        pop = sum(c.population for c in w.cities.values() if c.country_id == cid)
        players_n = sum(1 for p in w.players.values()
                        if p.country_id == cid and not p.is_state)
        rows.append({"id": cid, "name": country.name, "color": country.color,
                     "population": round(pop), "players": players_n,
                     "size": society.size_title(pop),
                     "size_rank": society.size_rank(pop),
                     "capital": w.cities[country.capital_city_id].name})
    # Окно заселения — главное, что должен знать выбирающий страну: пока оно
    # открыто, его приход приводит в страну 350 тысяч покупателей; закрылось —
    # он приходит на готовый рынок и делит его с теми, кто успел.
    return {"countries": rows,
            "join_window_open": w.tick < config.JOIN_WINDOW_TICKS,
            "join_window_left": max(0, config.JOIN_WINDOW_TICKS - w.tick),
            "settlers_per_player": sum(config.POPULATION_PER_PLAYER.values()),
            # Требование к паролю форма показывает словами и проверяет сама,
            # чтобы игрок узнал о нём до отправки, а не из ответа сервера.
            "password_min": config.PASSWORD_MIN_LEN}


# ---------------------------------------------------------------------------
# Мир
# ---------------------------------------------------------------------------
def world_state(ctx: Ctx) -> dict:
    """Шапка игры. Главное в ней — СВОЯ страна, а не весь мир.

    Раньше сверху висели цифры по всем двадцати государствам сразу, и игрок
    видел население планеты вместо населения своего государства. Мировые
    итоги остались, но ушли на второй план.
    """
    w = ctx.world
    elapsed = time.time() - w.last_tick_at
    c = ctx.player_country()
    alive = [co for co in w.countries.values() if co.alive]

    def block(cities: list) -> dict:
        pop = sum(x.population for x in cities) or 1.0
        workers = sum(x.s("workers").people for x in cities)
        ids = {x.id for x in cities}
        employed = _factory_employed(w, ids)
        return {
            "population": round(pop),
            "regions": len(cities),
            "industrialisation": round(workers / pop, 4),
            "unemployment": round(max(0.0, 1.0 - employed / workers), 4)
                             if workers > 1 else 0.0,
            "satisfaction": round(
                sum(x.satisfaction * x.population for x in cities) / pop, 4),
            "avg_wage": round(sum(x.avg_wage * x.s("workers").people for x in cities)
                              / workers, 2) if workers > 1 else 0.0,
            "living_standard": round(
                sum(society.region_living_standard(x) * x.population
                    for x in cities) / pop, 3),
        }

    world_cities = [x for x in w.cities.values()
                    if x.country_id in {co.id for co in alive}]
    home_cities = w.country_regions(c.id) if c else []
    home = block(home_cities) if home_cities else None
    if home is not None:
        home.update({
            "name": c.name,
            "gdp": round(c.gdp),
            "treasury": round(c.treasury),
            "unrest": round(max((x.unrest for x in home_cities), default=0.0), 3),
            "revolts": sum(1 for x in home_cities if x.revolt_ticks > 0),
            # Ступень размера рядом с числом душ: в шапке она и заменяет
            # восьмизначное число смыслом.
            "size": society.size_title(home["population"]),
            "size_rank": society.size_rank(home["population"]),
            "size_next": _next_size(home["population"]),
        })
    return {
        "tick": w.tick,
        "tick_seconds": w.tick_seconds,
        "seconds_left": max(0.0, w.tick_seconds - elapsed),
        "auto_tick": w.auto_tick,
        "is_admin": is_admin(ctx.player),
        "country": _country_brief(w, c) if c else None,
        # цифры шапки — по своей стране
        "home": home,
        "world": {**block(world_cities),
                  "gdp": round(sum(co.gdp for co in alive)),
                  "treasury": round(sum(co.treasury for co in alive)),
                  "countries": len(alive)},
    }


def _next_size(population: float) -> dict | None:
    """Следующая ступень размера и сколько душ до неё. None — уже вершина."""
    for threshold, name in config.COUNTRY_SIZES:
        if population < threshold:
            return {"name": name, "at": threshold,
                    "left": round(threshold - population)}
    return None


def _country_brief(w: World, c) -> dict:
    workers = sum(city.s("workers").people for city in w.cities.values()
                  if city.country_id == c.id)
    employed = _factory_employed(
        w, {city.id for city in w.cities.values() if city.country_id == c.id})
    leader = w.players.get(c.leader_id) if c.leader_id else None
    return {
        "id": c.id, "name": c.name, "color": c.color,
        "treasury": round(c.treasury, 2),
        "gdp": round(c.gdp, 2),
        "corporate_tax": c.corporate_tax,
        "sales_tax": c.sales_tax,
        "income_tax": c.income_tax,
        # Налоги, ложащиеся на население: подоходный берётся только с
        # заводских зарплат, а их получают одни рабочие.
        "poll_tax": c.poll_tax,
        "tithe": c.tithe,
        "wealth_tax": c.wealth_tax,
        "excise_tax": c.excise_tax,
        "public_spending_rate": c.public_spending_rate,
        "min_wage": c.min_wage,
        "land_rent": c.land_rent,
        "worker_insurance": c.worker_insurance,
        "tariff": c.tariff,
        "import_tariff": c.import_tariff,
        "bankruptcy_limit": c.bankruptcy_limit,
        "foreign_investment_open": c.foreign_investment_open,
        # Средняя доступность рынка страны: насколько её области срослись в
        # один рынок. Растёт только от «Торговых палат».
        "access": round(country_access(w).get(c.id, 0.0), 4),
        "living_standard": round(society.country_living_standard(w, c), 3),
        "industrialisation": round(workers / sum(ct.population for ct in w.cities.values()
                                                 if ct.country_id == c.id), 4)
                              if workers > 0 else 0.0,
        "regions": len(w.country_regions(c.id)),
        "population": round(_country_pop(w, c.id)),
        # Размер страны словом: по нему сразу видно, губерния перед тобой или
        # держава, — и он же ступень, до которой стоит дорасти.
        "size": society.size_title(_country_pop(w, c.id)),
        "size_rank": society.size_rank(_country_pop(w, c.id)),
        "size_next": _next_size(_country_pop(w, c.id)),
        # Приток населения за новых игроков: сколько ещё людей в пути и открыто
        # ли ещё окно заселения (первые пейдеи жизни мира).
        "settlers": round(sum(c.settlers.values())),
        "settlers_left": c.settlers_left,
        "join_window_open": w.tick < config.JOIN_WINDOW_TICKS,
        "join_window_left": max(0, config.JOIN_WINDOW_TICKS - w.tick),
        "settlers_per_player": sum(config.POPULATION_PER_PLAYER.values()),
        "players": sum(1 for p in w.players.values()
                       if p.country_id == c.id and not p.is_state),
        "unemployment": round(max(0.0, 1.0 - employed / workers), 4)
                        if workers > 1 else 0.0,
        "leader": leader.username if leader else "AI (без лидера)",
        "leader_is_ai": c.leader_id is None,
        "reference_wage": round(society.reference_wage(w, c), 2),
        "alive": c.alive,
        "army": _army_brief(w, c),
        "budget": _budget_brief(w, c),
        # Форма государства коротко — она нужна половине витрин: по ней
        # рисуются пределы ползунков, скидки на стройку и подпись в шапке.
        "laws": [{"key": cat,
                  "name": config.LAWS[cat]["name"],
                  "option": politics.law(c, cat),
                  "option_name": politics.option(c, cat)["name"]}
                 for cat in config.LAW_ORDER],
        "has_parliament": politics.has_elections(c),
        "parliament_seats": politics.seats(c) if politics.has_elections(c) else 0,
        "tariff_cap": politics.tariff_cap(c),
        "closed_economy": politics.is_closed(c),
        "unfair_voting": politics.unfair_voting(c, w.tick),
        "unfair_left": max(0, c.unfair_until - w.tick),
        # ПРОСВЕЩЕНИЕ И ЕГО ЦЕНА. Грамотность — источник рабочих рук, обида —
        # то, во что она обращается без прав; жар — насколько страна близка к
        # тому, чтобы предъявить требования. Три числа рядом, потому что
        # порознь они не значат ничего.
        "education": round(society.country_education(w, c), 3),
        "grievance": round(society.country_grievance(w, c), 3),
        "revolution_heat": round(c.revolution_heat, 3),
        "revolution": (c.revolution.phase
                       if c.revolution is not None else "none"),
        # Границы налоговых ползунков — уже с поправкой на идеологию: витрина
        # обязана показывать тот же предел, о который ударится сохранение.
        "policy_limits": {k: [lo, hi] for k, (lo, hi) in policy_limits(c).items()},
    }


def _budget_brief(w: World, c) -> dict:
    """Роспись казны за прошлый пейдей: откуда пришло и куда ушло.

    Не оценка задним числом, а бухгалтерия: каждое движение казны в движке
    помечено статьёй (Country.collect / .spend), поэтому сумма доходов минус
    сумма расходов в точности равна изменению остатка. Витрине остаётся
    разложить это по подписям и объяснить, на кого какая статья ложится.

    Статьи с нулём тоже идут в ответ: лидеру важно видеть, что рычаг есть, но
    не приносит ничего, — это и есть повод им воспользоваться.
    """
    ledger = c.last_budget or {}
    pop = sum(city.population for city in w.country_regions(c.id)) or 1.0

    def rows(spec, sign):
        out = []
        for key, name, note in spec:
            # `or 0.0` убирает минус-ноль: расходная статья без движения должна
            # читаться как «0», а не как «−0».
            value = (ledger.get(key, 0.0) or 0.0) * sign or 0.0
            out.append({"key": key, "name": name, "note": note,
                        "amount": round(value, 2),
                        "per_capita": round(value / pop, 4)})
        return out

    income = rows(config.BUDGET_INCOME, 1.0)
    expense = rows(config.BUDGET_EXPENSE, -1.0)
    total_in = sum(r["amount"] for r in income)
    total_out = sum(r["amount"] for r in expense)
    opening = c.last_budget_opening
    return {
        "income": income, "expense": expense,
        "total_income": round(total_in, 2),
        "total_expense": round(total_out, 2),
        "net": round(total_in - total_out, 2),
        "opening": round(opening, 2),
        "closing": round(opening + total_in - total_out, 2),
        "population": round(pop),
    }


def _fronts(w: World, c) -> list[dict]:
    """Расстановка армии по границам — по одной строке на соседа.

    Здесь же лежит и разведка: сколько людей сосед держит ПРОТИВ НАС. Число
    даётся грубым (config.FRONT_INTEL_ROUNDING) — не потому, что жалко, а
    потому, что точная численность чужих полков ниоткуда не берётся: видно
    примерно, «около десяти тысяч».
    """
    rows = []
    for nb in society.fronts_of(w, c):
        other = w.countries.get(nb)
        if other is None or not other.alive:
            continue
        mine = society.front_soldiers(c, nb)
        officers = society.front_officers(c, nb)
        theirs = society.front_soldiers(other, c.id)
        step = max(1.0, theirs * config.FRONT_INTEL_ROUNDING)
        rows.append({
            "country_id": nb, "name": other.name, "color": other.color,
            "soldiers": round(mine), "officers": round(officers),
            "command": round(society.command_quality(officers, mine, c), 3),
            # сколько офицеров нужно для полного качества командования
            "officers_needed": round(mine * config.OFFICER_TARGET_SHARE),
            "strength": round(society.front_strength(w, c, nb)),
            # разведка: чужой фронт против нас, округлённый
            "enemy_soldiers": round(theirs / step) * round(step) if theirs > 0 else 0,
            "enemy_known": theirs > 0,
            "borders": len(w.border_regions(c.id, nb)),
            "at_war": w.at_war(c.id, nb),
            "allied": w.allied(c.id, nb),
        })
    rows.sort(key=lambda r: (not r["at_war"], -r["soldiers"]))
    return rows


def _army_brief(w: World, c) -> dict:
    """Сводка по армии государства: люди, деньги, снабжение."""
    soldiers = society.army_size(w, c)
    officers = society.officer_size(w, c)
    afford = society.affordable_army(w, c)
    need_shells = soldiers * config.SHELLS_PER_SOLDIER_BATTLE
    return {
        "soldiers": round(soldiers),
        "affordable": round(afford),
        # ---- офицеры и командование ----
        "officers": round(officers),
        "officers_target": round(society.affordable_officers(w, c)),
        "officer_pay": round(society.officer_pay(c), 2),
        "officer_share": round(officers / soldiers, 4) if soldiers > 1 else 0.0,
        # Штат — рычаг лидера; «по уставу» — та доля, при которой командование
        # выходит на потолок. Держать сверх устава имеет смысл на войне: офицеры
        # гибнут быстрее солдат.
        "officer_target": round(society.officer_target_share(c), 4),
        "officer_target_share": config.OFFICER_TARGET_SHARE,
        "officer_target_max": config.OFFICER_TARGET_MAX,
        # ---- наём из высшего общества ----
        # Сколько людей вообще пойдёт в офицеры за нынешнее жалованье и во
        # сколько обходится патент. По этим двум числам видна причина недобора:
        # пусто в казне или нанимать некого.
        "officer_candidates": round(society.officer_candidates(w, c)),
        "officer_pay_needed": round(society.officer_pay_bar(w, c), 2),
        # Из кого набирают офицеров — решает ЗАКОН о государственном устройстве:
        # при монархии патент дворянский, республика открывает его мещанству.
        "officer_pool": [config.STRATA[k]["name"]
                         for k in politics.officer_pool(c)],
        "officers_hired": round(c.last_officers_hired),
        "officers_lost": round(c.last_officers_lost),
        "officer_recruit_share": config.OFFICER_RECRUIT_SHARE,
        "officer_casualty_mult": config.OFFICER_CASUALTY_MULT,
        # Качество командования «в среднем по стране» — справочно. В бою
        # считается своё на каждом фронте, см. _fronts.
        "command": round(society.command_quality(officers, soldiers, c), 3),
        # Потолок командования — тоже из закона: республике достаётся ровно
        # сотня процентов, полтораста даёт только дворянский корпус.
        "command_max": politics.command_cap(c),
        "slot_cost": round(society.soldier_slot_cost(c), 2),
        # ---- фронты ----
        "fronts": _fronts(w, c),
        "reserve": round(society.free_soldiers(w, c)),
        "reserve_officers": round(society.free_officers(w, c)),
        "move_share": config.FRONT_MOVE_SHARE,
        "soldier_pay": round(c.soldier_pay, 2),
        "budget": round(c.army_budget, 2),
        "last_cost": round(c.last_army_cost, 2),
        "shells": round(c.army_shells, 1),
        "shells_target": round(soldiers * config.SHELLS_RESERVE_PER_SOLDIER, 1),
        "shells_bought": round(c.last_shells_bought, 1),
        "battles_covered": round(c.army_shells / need_shells, 2)
                           if need_shells > 1e-9 else 0.0,
        # Две разные величины, и в интерфейсе они стоят рядом намеренно:
        # equip — ВООРУЖЁННОСТЬ, запас арсенала к штату, от неё зависит бой;
        # weapons_demand — ПОТРЕБЛЕНИЕ, спрос казны за пейдей, от него цена.
        "equip": round(c.army_equip, 4),
        "weapons": round(c.army_weapons, 1),
        "weapons_target": round(society.weapons_target(w, c), 1),
        "weapons_worn": round(c.last_weapons_worn, 1),
        "weapons_bought": round(c.last_weapons_bought, 1),
        "weapons_demand": round(society.weapons_wanted(w, c), 1),
        "strength": round(society.army_strength(w, c)),
        "mobilization_left": c.mobilization_left,
        "last_mobilized": round(c.last_mobilized),
        "budget_max_share": config.ARMY_TARGET_MAX,
    }


def _factory_employed(w: World, city_ids) -> float:
    """Сколько людей занято на ЗАВОДСКИХ местах в этих областях.

    Крестьяне, нанятые на фермы, сюда не входят, и это принципиально: рабочая
    сила у них своя (Industry.labour), сословия они не меняют, и в безработице
    не участвуют — не взяли на чужое поле, вернулся на своё. Считать их вместе
    с заводскими значит врать в обе стороны сразу: «занято» становится больше
    числа рабочих, и безработица показывает ноль там, где половина заводского
    сословия сидит без дела.
    """
    total = 0.0
    for b in w.buildings.values():
        if b.city_id not in city_ids:
            continue
        ind = w.industries.get(b.industry_key)
        if ind is None or ind.labour == "workers":
            total += b.employed
    return total


def _corridor_position(price: float, anchor: float) -> float:
    """Где цена стоит в своём коридоре: 0 — у пола, 0.5 — на якоре, 1 — у потолка.

    Считается В ЛОГАРИФМАХ и по каждой половине отдельно — ровно так, как цену
    двигает сам движок (см. economy/pricing.py: он работает с ln(price/anchor) и
    насыщает результат через tanh).

    Прежняя мерка была линейной по деньгам, и коридор от этого врал грубо.
    Границы стоят на 0.55 и 4.00 якоря, значит сам якорь на линейной шкале
    оказывался на 13% ширины — почти вплотную к левому краю. Товар, который
    продаётся ровно по нормальной цене, выглядел «на самом дне», а вся правая
    половина полосы доставалась ценам от двух себестоимостей и выше, которых в
    живой экономике почти не бывает. Смотреть на такую полосу было бессмысленно.

    Теперь якорь ровно посередине, пол — у левого края, потолок — у правого, и
    отклонение вниз читается так же честно, как отклонение вверх.
    """
    if anchor <= 0 or price <= 0:
        return 0.5
    u = math.log(price / anchor)
    span = U_MAX if u >= 0 else U_MIN
    return max(0.0, min(1.0, 0.5 + 0.5 * u / span))


def market(ctx: Ctx) -> dict:
    """Рынок конкретной ОБЛАСТИ (?region_id=), по умолчанию — столицы.

    Цены и склады у каждой области свои, поэтому переключатель областей есть
    на всех витринах. Для сравнения рядом идёт цена в соседних областях той же
    страны и мировая.
    """
    w = ctx.world
    c = _chosen_country(ctx)
    city = _chosen_region(ctx)
    siblings = [x for x in w.country_regions(c.id) if x.id != city.id]
    order = {"raw": 0, "intermediate": 1, "consumer": 2, "military": 3,
             "luxury": 4, "services": 5}
    goods = sorted(w.goods.values(), key=lambda g: (order[g.category], g.key))
    rows = []
    for g in goods:
        local = society.lg(city, g.key)
        lo, hi = price_bounds(local.anchor)
        others = [society.lg(x, g.key).price for x in siblings]
        rows.append({
            "key": g.key, "name": g.name, "category": g.category, "tier": g.tier,
            "storable": g.storable, "perish_rate": g.perish_rate,
            "price": round(local.price, 2),
            "anchor": round(local.anchor, 2),
            "unit_cost": round(local.unit_cost, 2),
            "floor": round(lo, 2), "ceiling": round(hi, 2),
            "position": round(_corridor_position(local.price, local.anchor), 4),
            "stock": round(local.stock, 1),
            "demand": round(local.last_demand, 1),
            "supply": round(local.last_supply, 1),
            "shortage": round(local.last_shortage, 4),
            "margin": round(local.price / local.unit_cost, 3) if local.unit_cost > 0 else 0.0,
            "world_price": round(w.world_prices.get(g.key, local.anchor), 2),
            "country_price": round(sum(others) / len(others), 2) if others else None,
            "stock_ticks": round(local.stock / local.last_demand, 2)
                           if g.storable and local.last_demand > 1 else None,
        })
    return {"goods": rows, "country_id": c.id, "country_name": c.name,
            "region_id": city.id, "region_name": city.name,
            "regions": _region_list(w, c),
            # Доступность рынка области: насколько её прилавок включён в общий.
            # Внутри страны это скорость выравнивания цен с соседями, снаружи —
            # объём вывоза и ввоза.
            "access": round(region_access(w).get(city.id, 0.0), 4),
            "chambers": engine_chamber_levels(w, city.id),
            "chamber_max": config.TRADE_CHAMBER_MAX_LEVEL,
            "access_per_level": config.TRADE_ACCESS_PER_LEVEL,
            "tariff": round(c.tariff, 4),
            "import_tariff": round(c.import_tariff, 4),
            # Пороги раскраски наценки и коридора — одни и те же для таблицы,
            # для полосы и для легенды под ней. Держим их на сервере, чтобы
            # правка ANCHOR_MARKUP не разъезжалась с тем, что видит игрок.
            "margin_bands": {"loss": 1.0, "normal": config.ANCHOR_MARKUP,
                             "rich": config.MARGIN_RICH_AT},
            "corridor": {"floor": config.PRICE_FLOOR_MULT,
                         "ceiling": config.PRICE_CEIL_MULT}}


def _chosen_country(ctx: Ctx):
    """Страна для просмотра: из ?country_id=, из области или страна игрока."""
    w = ctx.world
    cid = int(ctx.query.get("country_id") or 0)
    if cid and cid in w.countries:
        return w.countries[cid]
    rid = int(ctx.query.get("region_id") or 0)
    if rid in w.cities:
        c = w.countries.get(w.cities[rid].country_id)
        if c is not None:
            return c
    c = ctx.player_country()
    if c is not None and c.alive:
        return c
    return next((x for x in w.countries.values() if x.alive),
                next(iter(w.countries.values())))


def _chosen_region(ctx: Ctx):
    """Область для просмотра: из ?region_id= или столица выбранной страны.

    Рынок и население живут в области, поэтому почти всякая витрина смотрит
    на конкретную область, а не на страну целиком.
    """
    w = ctx.world
    rid = int(ctx.query.get("region_id") or 0)
    if rid in w.cities:
        return w.cities[rid]
    country = _chosen_country(ctx)
    city = w.cities.get(country.capital_city_id)
    if city is not None and city.country_id == country.id:
        return city
    regions = w.country_regions(country.id)
    return regions[0] if regions else next(iter(w.cities.values()))


def _region_list(w: World, country) -> list[dict]:
    """Список областей страны — для переключателя во всех витринах."""
    access = region_access(w)
    return [{"id": c.id, "name": c.name,
             "population": round(c.population),
             "capital": c.id == country.capital_city_id,
             "unrest": round(c.unrest, 3),
             "access": round(access.get(c.id, 0.0), 4),
             "revolt": c.revolt_ticks > 0}
            for c in sorted(w.country_regions(country.id),
                            key=lambda x: (x.id != country.capital_city_id, x.name))]


def _foreign_regions(w: World, player) -> list[dict]:
    """Чужие области, куда игроку сейчас можно вложиться.

    Это ровно те области, которые пропустит build(): государство живо, открыло
    экономику для иностранцев и не воюет с нашим. Список считается здесь, а не
    на клиенте, чтобы в «Строительстве» нельзя было выбрать область, с которой
    стройка всё равно вернёт отказ.
    """
    if player is None:
        return []
    home_id = player.country_id
    out = []
    for c in sorted(w.countries.values(), key=lambda x: x.name):
        if not c.alive or c.id == home_id or not c.foreign_investment_open:
            continue
        if w.at_war(home_id, c.id):
            continue
        for row in _region_list(w, c):
            row.update({"country_id": c.id, "country_name": c.name,
                        "color": c.color})
            out.append(row)
    return out


def _region_info(w: World, region, player) -> dict:
    """Сведения о выбранной области — включая чужую.

    Витрина строительства смотрит и на чужие области, а их нет в списке
    областей своей страны, поэтому подпись под переключателем собирается
    из этого блока, а не поиском по своему списку.
    """
    c = w.countries.get(region.country_id)
    return {
        "id": region.id, "name": region.name,
        "country_id": region.country_id,
        "country_name": c.name if c else "—",
        "capital": bool(c and region.id == c.capital_city_id),
        "foreign": bool(player and region.country_id != player.country_id),
        "population": round(region.population),
        "unemployment": round(region.unemployment, 4),
        "revolt": region.revolt_ticks > 0,
    }


def regions(ctx: Ctx) -> dict:
    """Области выбранной страны и общая сводка по каждой."""
    w = ctx.world
    c = _chosen_country(ctx)
    access = region_access(w)
    rows = []
    for city in w.country_regions(c.id):
        workers = city.s("workers").people
        employed = _factory_employed(w, {city.id})
        rows.append({
            "id": city.id, "name": city.name,
            "capital": city.id == c.capital_city_id,
            # Доступность рынка и палаты, которые её дают
            "access": round(access.get(city.id, 0.0), 4),
            "chambers": engine_chamber_levels(w, city.id),
            "population": round(city.population),
            "workers": round(workers), "employed": round(employed),
            "unemployment": round(city.unemployment, 4),
            "satisfaction": round(city.satisfaction, 4),
            "living_standard": round(society.region_living_standard(city), 3),
            "avg_wage": round(city.avg_wage, 2),
            "harvest": round(city.harvest, 3),
            "unrest": round(city.unrest, 3),
            "revolt_ticks": city.revolt_ticks,
            "cpi": round(engine_region_cpi(city), 3),
            "buildings": len(w.city_buildings(city.id)),
            "neighbors": [{"id": n, "name": w.cities[n].name,
                           "country": w.countries[w.cities[n].country_id].name,
                           "mine": w.cities[n].country_id == c.id}
                          for n in w.region_neighbors(city.id) if n in w.cities],
        })
    rows.sort(key=lambda r: (not r["capital"], r["name"]))
    return {"regions": rows, "country_id": c.id, "country_name": c.name,
            "access": round(country_access(w).get(c.id, 0.0), 4),
            "chamber_max": config.TRADE_CHAMBER_MAX_LEVEL,
            "access_per_level": config.TRADE_ACCESS_PER_LEVEL}


def exchange(ctx: Ctx) -> dict:
    """Биржа мирового рынка: где товар дёшев, где дорог и кто чем торгует.

    Мир замкнут: вывезенный товар обязан быть кем-то куплен, поэтому здесь
    видно обе стороны каждой сделки прошлого пейдея — кто вывез, кто ввёз и
    сколько досталось казне пошлиной.
    """
    w = ctx.world
    caps = country_access(w)
    home = ctx.player_country()
    order = {"raw": 0, "intermediate": 1, "consumer": 2, "military": 3,
             "luxury": 4, "services": 5}
    # области каждой страны — один раз на всю выдачу, а не на каждый товар
    by_country = [(country, w.country_regions(country.id))
                  for country in w.countries.values() if country.alive]

    rows = []
    for g in sorted(w.goods.values(), key=lambda g: (order[g.category], g.key)):
        if not g.storable:
            continue        # услуги через границу не возят
        wp = w.world_prices.get(g.key, g.anchor)
        # Котировка — ОДНА НА ГОСУДАРСТВО. Рынок живёт в области, у каждой своя
        # цена, но через границу торгуют страны: без свёртки одна и та же
        # страна попадала в список столько раз, сколько у неё областей.
        quotes = []
        for country, cities_ in by_country:
            locals_ = [(city, city.goods[g.key])
                       for city in cities_ if g.key in city.goods]
            if not locals_:
                continue

            # Средняя цена по стране взвешена по обороту: страна торгует по той
            # цене, по которой у неё реально берут товар, а не по среднему
            # арифметическому между людной столицей и пустой окраиной. Если
            # спроса нет вовсе, взвешивать нечем — берём простое среднее.
            weights = [max(local.last_demand, 0.0) for _, local in locals_]
            total_w = sum(weights)
            if total_w > 1e-9:
                price = sum(local.price * wgt
                            for (_, local), wgt in zip(locals_, weights)) / total_w
                shortage = sum(local.last_shortage * wgt
                               for (_, local), wgt in zip(locals_, weights)) / total_w
            else:
                price = sum(local.price for _, local in locals_) / len(locals_)
                shortage = sum(local.last_shortage
                               for _, local in locals_) / len(locals_)

            low = min(locals_, key=lambda pair: pair[1].price)
            high = max(locals_, key=lambda pair: pair[1].price)
            quotes.append({
                "country_id": country.id, "name": country.name,
                "color": country.color,
                "price": round(price, 2),
                "regions": len(locals_),
                # разброс внутри страны: показывает, где обозы не справляются
                "low": round(low[1].price, 2), "low_region": low[0].name,
                "high": round(high[1].price, 2), "high_region": high[0].name,
                "stock": round(sum(local.stock for _, local in locals_), 1),
                "demand": round(sum(local.last_demand for _, local in locals_), 1),
                "shortage": round(shortage, 4),
                "spread": round(price / wp - 1.0, 4) if wp > 0 else 0.0,
                # пропускная способность границы — величина государства, а не
                # области: раньше её искали по region_id и всегда получали 0
                "capacity": round(caps.get(country.id, 0.0), 4),
                "is_mine": home is not None and country.id == home.id,
            })
        quotes.sort(key=lambda q: q["price"])
        trade = w.last_trades.get(g.key) or {}
        rows.append({
            "key": g.key, "name": g.name, "category": g.category,
            "world_price": round(wp, 2),
            "cheapest": quotes[0] if quotes else None,
            "dearest": quotes[-1] if quotes else None,
            "spread": round(quotes[-1]["price"] / quotes[0]["price"] - 1.0, 4)
                      if len(quotes) > 1 and quotes[0]["price"] > 0 else 0.0,
            "volume": trade.get("volume", 0.0),
            "offered": trade.get("offered", 0.0),
            "wanted": trade.get("wanted", 0.0),
            "exports": trade.get("exports", []),
            "imports": trade.get("imports", []),
            "quotes": quotes,
        })

    traders = [{"country_id": cid, "name": w.countries[cid].name,
                "capacity": round(cap, 4),
                "tariff": round(w.countries[cid].tariff, 4),
                "import_tariff": round(w.countries[cid].import_tariff, 4),
                "is_mine": home is not None and cid == home.id}
               for cid, cap in sorted(caps.items(), key=lambda kv: -kv[1])
               if cap > 0 and w.countries[cid].alive]

    return {
        "goods": rows,
        "traders": traders,
        "tariff": round(home.tariff, 4) if home else config.WORLD_TRADE_TARIFF,
        "import_tariff": (round(home.import_tariff, 4) if home
                          else config.IMPORT_TARIFF_DEFAULT),
        "access_per_level": config.TRADE_ACCESS_PER_LEVEL,
        "chamber_max": config.TRADE_CHAMBER_MAX_LEVEL,
        "my_capacity": round(caps.get(home.id, 0.0), 4) if home else 0.0,
        "history": db.world_price_series(int(ctx.query.get("limit", "120"))),
    }


def market_history(ctx: Ctx) -> dict:
    """История цен выбранной ОБЛАСТИ (?region_id=)."""
    limit = int(ctx.query.get("limit", "120"))
    region = _chosen_region(ctx)
    return {"region_id": region.id, "region_name": region.name,
            "series": db.price_series(region.id, limit),
            "world": db.world_price_series(limit)}


def macro_history(ctx: Ctx) -> dict:
    limit = int(ctx.query.get("limit", "120"))
    cid = _chosen_country(ctx).id
    return {"country_id": cid, "rows": db.macro_series(cid, limit)}


def cities(ctx: Ctx) -> dict:
    """Города государства игрока. С ?all=1 — все города мира."""
    w = ctx.world
    scope = _chosen_country(ctx)
    show_all = ctx.query.get("all") == "1"
    rows = []
    for c in w.cities.values():
        if not show_all and c.country_id != scope.id:
            continue
        country = w.countries.get(c.country_id)
        rows.append({
            "id": c.id, "name": c.name, "country_id": c.country_id,
            "country": country.name if country else "—",
            "mine": c.country_id == scope.id,
            "population": round(c.population),
            "workers": round(c.s("workers").people),
            "jobs": round(sum(
                b.level * politics.industry_jobs(
                    country, w.industries[b.industry_key])
                for b in w.city_buildings(c.id))),
            "harvest": round(c.harvest, 3),
            "savings": round(c.savings, 2),
            "unemployment": round(c.unemployment, 4),
            "satisfaction": round(c.satisfaction, 4),
            "avg_wage": round(c.avg_wage, 2),
        })
    return {"cities": rows}


def _consumption_rows(w: World, key: str, agg: dict) -> list[dict]:
    """Что и сколько сословие потребляет прямо сейчас — на одного человека.

    Уровень жизни — одно число, и по нему не понять, чего людям не хватает:
    хлеба, одежды или оперы. Здесь та же величина разложена по товарам: сколько
    чего взяли за пейдей и какую долю положенного это покрывает.

    «Положено» — обычная корзина сословия с поправкой на его уровень
    потребления; для роскоши — та её часть, которую сословие уже считает для
    себя нормой (society.luxury_share от ожиданий). Роскошь, до которой оно ещё
    не доросло, в список не попадает: её никто и не ждал.
    """
    people = agg["people"]
    if people <= 1.0:
        return []
    level = config.STRATA[key]["level"]
    expect = agg["expect"] / people
    eaten = agg["eaten"]

    norms: dict[str, float] = {
        g: spec["qty"] * level for g, spec in config.CONSUMPTION_BASKET.items()
    }
    for g, spec in config.LUXURY_BASKET.items():
        share = society.luxury_share(expect, spec["unlock"])
        if share > 0.001:
            norms[g] = spec["qty"] * level * share

    rows = []
    for g in set(norms) | set(eaten):
        good = w.goods.get(g)
        if good is None:
            continue
        per_head = eaten.get(g, 0.0) / people
        norm = norms.get(g, 0.0)
        rows.append({
            "good": g, "name": good.name,
            "per_capita": round(per_head, 4),
            "norm": round(norm, 4),
            "share": round(per_head / norm, 3) if norm > 1e-9 else None,
            "luxury": g in config.LUXURY_BASKET,
            "tier": config.CONSUMPTION_BASKET.get(g, {}).get("tier", 4),
        })
    # Сперва необходимое, потом роскошь; внутри — по ступеням потребности.
    rows.sort(key=lambda r: (r["luxury"], r["tier"], r["good"]))
    return rows


def population(ctx: Ctx) -> dict:
    """Население ОБЛАСТИ (?region_id=), либо всей страны при ?scope=country.

    По умолчанию показывается конкретная область: сословия, их достаток и
    уровень жизни у соседних областей разные, и усреднять их по стране —
    значит прятать самое интересное.
    """
    w = ctx.world
    c = _chosen_country(ctx)
    whole = ctx.query.get("scope") == "country"
    region = None if whole else _chosen_region(ctx)
    scope_cities = w.country_regions(c.id) if whole else [region]

    agg = {k: {"people": 0.0, "cash": 0.0, "income": 0.0, "satisfaction": 0.0,
               "sol": 0.0, "expect": 0.0, "edu": 0.0, "griev": 0.0, "eaten": {}}
           for k in config.STRATA_ORDER}
    for city in scope_cities:
        for key in config.STRATA_ORDER:
            st = city.s(key)
            a = agg[key]
            a["people"] += st.people
            a["cash"] += st.cash
            a["income"] += st.income
            a["satisfaction"] += st.satisfaction * st.people
            a["sol"] += st.living_standard * st.people
            a["expect"] += st.expectation * st.people
            a["edu"] += st.education * st.people
            a["griev"] += st.grievance * st.people
            for g, qty in (st.consumed or {}).items():
                a["eaten"][g] = a["eaten"].get(g, 0.0) + qty
    total_pop = sum(a["people"] for a in agg.values()) or 1.0
    strata = []
    for key in config.STRATA_ORDER:
        a = agg[key]
        spec = config.STRATA[key]
        sat = a["satisfaction"] / a["people"] if a["people"] > 1.0 else 0.0
        strata.append({
            "consumption": _consumption_rows(w, key, a),
            "key": key, "name": spec["name"], "class": spec["class"],
            "level": spec["level"],
            "people": round(a["people"]),
            "share": a["people"] / total_pop,
            "income_per_capita": round(a["income"] / a["people"], 2) if a["people"] else 0.0,
            "satisfaction": round(sat, 4),
            "savings": round(a["cash"]),
            "can_hire": key in config.LABOUR_POOL,
            "living_standard": round(a["sol"] / a["people"], 3) if a["people"] > 1 else 0.0,
            "expectation": round(a["expect"] / a["people"], 3) if a["people"] > 1 else 0.0,
            # ОБРАЗОВАНИЕ и то, во что оно обходится государству. Смотреть на
            # эти два числа надо вместе: первое — доля сословия, годная на
            # завод, второе — та же грамотность, обернувшаяся требованием
            # прав. Одно без другого не бывает.
            "education": round(a["edu"] / a["people"], 3) if a["people"] > 1 else 0.0,
            "grievance": round(a["griev"] / a["people"], 3) if a["people"] > 1 else 0.0,
            # Какая доля сословия годится на завод при нынешней грамотности.
            # Промышленнику это и есть ответ на вопрос «кого я вообще могу
            # переманить», а он куда важнее общей численности.
            "worker_pool": round(society.worker_pool_share(
                a["edu"] / a["people"]), 3)
                if a["people"] > 1 and key in config.LABOUR_POOL else None,
            # что из роскоши это сословие уже себе позволяет
            "luxuries": [
                {"good": g, "name": w.goods[g].name,
                 "share": round(society.luxury_share(
                     a["expect"] / a["people"] if a["people"] > 1 else 0.0,
                     spec["unlock"]), 3)}
                for g, spec in config.LUXURY_BASKET.items()
                if g in w.goods and society.luxury_share(
                    a["expect"] / a["people"] if a["people"] > 1 else 0.0,
                    spec["unlock"]) > 0.001
            ],
        })

    # разбивка по областям страны — всегда по всем, это и есть сравнение
    by_city = []
    for city in w.country_regions(c.id):
        by_city.append({
            "id": city.id, "name": city.name,
            "capital": city.id == c.capital_city_id,
            "harvest": round(city.harvest, 4),
            "unemployment": round(city.unemployment, 4),
            "satisfaction": round(city.satisfaction, 4),
            "living_standard": round(society.region_living_standard(city), 3),
            "unrest": round(city.unrest, 3),
            "revolt": city.revolt_ticks > 0,
            "strata": {k: {"people": round(city.s(k).people)} for k in config.STRATA_ORDER},
        })

    # чем заняты кустари — в выбранной области (или во всей стране)
    crafts: dict[str, float] = {}
    total_artisans = 0.0
    for city in scope_cities:
        st = city.s("artisans")
        total_artisans += st.people
        for craft, share in (st.craft_mix or {}).items():
            crafts[craft] = crafts.get(craft, 0.0) + st.people * share
    craft_rows = [{"good": k, "name": w.goods[k].name if k in w.goods else k,
                   "people": round(v), "share": round(v / max(total_artisans, 1), 4)}
                  for k, v in sorted(crafts.items(), key=lambda kv: -kv[1])]

    workers = sum(a["people"] for k, a in agg.items() if k == "workers")
    scope_ids = {city.id for city in scope_cities}
    # Занятость считается по СОСЛОВИЯМ, а не одной кучей. Крестьяне на фермах —
    # тоже наёмные руки, но в рабочие они не переходят и в безработице не
    # участвуют: не взяли на чужое поле — вернулся на своё. Сложи их с
    # заводскими, и «занято» перестало бы сходиться с числом рабочих.
    employed = _factory_employed(w, scope_ids)
    farm_hands = sum(b.employed for b in w.buildings.values()
                     if b.city_id in scope_ids
                     and (w.industries.get(b.industry_key) is not None
                          and w.industries[b.industry_key].labour == "peasants"))
    sol = (society.country_living_standard(w, c) if whole
           else society.region_living_standard(region))
    # ПРОСВЕЩЕНИЕ СТРАНЫ одной строкой: сколько людей учат прямо сейчас, кого
    # именно позволяет учить закон и во что уже обошлась грамотность.
    teaching = {}
    for city in scope_cities:
        for kind, room in society.teaching_capacity(w, city).items():
            if kind == "school":
                room *= politics.school_efficiency(c)
            teaching[kind] = teaching.get(kind, 0.0) + room

    return {
        "strata": strata, "cities": by_city, "crafts": craft_rows,
        "army": _army_brief(w, c),
        "living_standard": round(sol, 3),
        "education": {
            "average": round(society.country_education(w, c), 3),
            "grievance": round(society.country_grievance(w, c), 3),
            "law": politics.option(c, "education")["name"],
            "school_allowed": politics.school_allowed(c),
            "school_efficiency": round(politics.school_efficiency(c), 3),
            "schools": [config.STRATA[k]["name"]
                        for k in politics.educated_strata(c, "school")
                        if k in config.STRATA],
            "universities": [config.STRATA[k]["name"]
                             for k in politics.educated_strata(c, "university")
                             if k in config.STRATA],
            # Скольких страна выучивает за пейдей — уже с поправкой на закон.
            "capacity_school": round(teaching.get("school", 0.0)),
            "capacity_university": round(teaching.get("university", 0.0)),
            "decay": config.EDU_DECAY,
            "worker_floor": config.EDU_WORKER_FLOOR,
            "rights_law": politics.option(c, "labour_rights")["name"],
        },
        "scope": "country" if whole else "region",
        "region_id": None if whole else region.id,
        "region_name": None if whole else region.name,
        "regions": _region_list(w, c),
        "unrest": 0.0 if whole else round(region.unrest, 3),
        "revolt_ticks": 0 if whole else region.revolt_ticks,
        "luxury_ladder": [
            {"good": g, "name": w.goods[g].name, "unlock": spec["unlock"]}
            for g, spec in sorted(config.LUXURY_BASKET.items(),
                                  key=lambda kv: kv[1]["unlock"])
            if g in w.goods
        ],
        "country_id": c.id, "country_name": c.name,
        "reference_wage": round(society.reference_wage(w, c), 2),
        "workers": round(workers), "employed": round(employed),
        "unemployed": round(max(0.0, workers - employed)),
        # Крестьяне, нанятые на фермы: деревня на жалованье, а не рабочие.
        "farm_hands": round(farm_hands),
        "farm_wage_floor": round(
            society.peasant_alternative(region, c) if region is not None else 0.0, 2),
    }


def industries(ctx: Ctx) -> dict:
    w = ctx.world
    c = _chosen_country(ctx)
    region = _chosen_region(ctx)
    wage = society.reference_wage(w, c)
    me = ctx.player
    state = w.state_player(c.id)

    rows = []
    for i in w.industries.values():
        inputs = [{"good": k, "name": w.goods[k].name, "qty": q,
                   "price": round(society.lg(region, k).price, 2),
                   "available": society.lg(region, k).stock > 1.0
                                or society.lg(region, k).last_supply > 1.0}
                  for k, q in i.inputs.items()]
        state_lv = private_lv = 0
        for b in w.buildings.values():
            if b.industry_key != i.key:
                continue
            city = w.cities.get(b.city_id)
            if city is None or city.country_id != c.id:
                continue
            owner = w.players.get(b.owner_id)
            if owner and owner.is_state:
                state_lv += b.level
            else:
                private_lv += b.level

        # Что уже стоит в ЭТОЙ области: второй такой же завод не построить,
        # можно только поднять уровень. Отдельно своё и отдельно казённое —
        # это разные хозяева.
        def owned(owner) -> dict | None:
            if owner is None:
                return None
            b = _existing_building(w, owner.id, region.id, i.key)
            if b is None:
                return None
            return {"id": b.id, "level": b.level, "damage": b.damage,
                    "upgrade_cost": round(
                        build_price(c, i, b.level + 1, owner.is_state), 2)}

        # Места и выпуск показываются ПО ЗАКОНАМ ЭТОЙ СТРАНЫ, а не по чертежу
        # отрасли: земельное устройство переписывает и то и другое у крестьянских
        # предприятий (politics.industry_jobs / industry_output), и промышленник
        # обязан видеть настоящую ферму до того, как решит её строить.
        jobs_per_level = politics.industry_jobs(c, i)
        output_per_worker = politics.industry_output(c, i)
        # Содержание на работника считается по местам ИМЕННО ЭТОЙ отрасли.
        # Общая константа врала бы всякий раз, когда мест не «как у всех»: у
        # «Фермы» их впятеро больше, и её расходы выходили бы впятеро завышенными,
        # а прибыль на работника — отрицательной на ровном месте.
        upkeep_per_worker = config.UPKEEP_PER_LEVEL / max(jobs_per_level, 1)
        row = {
            "key": i.key, "name": i.name, "kind": i.kind,
            "sector": i.sector,
            "sector_name": config.SECTORS.get(i.sector, i.sector),
            "jobs_per_level": round(jobs_per_level),
            # Кто на этом предприятии работает: рабочие или крестьяне. У «Фермы»
            # это крестьяне — сословия они не меняют, поэтому её можно ставить
            # с первого пейдея, когда рабочих в стране ещё нет.
            "labour": i.labour,
            "labour_name": config.STRATA.get(i.labour, {}).get("name", i.labour),
            "inputs": inputs,
            "inputs_ready": all(x["available"] for x in inputs),
            # Цена постройки идёт двумя числами: своя и казённая. Идеология
            # даёт скидку не всем сразу — при либерализме дешевле строит
            # промышленник, при консерватизме казна, — и на витрине это должно
            # быть видно до того, как игрок нажмёт кнопку.
            "build_cost": round(build_price(c, i, 1, False), 2),
            "build_cost_state": round(build_price(c, i, 1, True), 2),
            "state_blocked": politics.state_build_blocked(c, i),
            "state_levels": state_lv, "private_levels": private_lv,
            # Предел развития и сколько уже занято В ЭТОЙ ОБЛАСТИ: у «Торговой
            # палаты» девять уровней на область, и считаются они по всем хозяевам.
            "max_level": i.max_level,
            "levels_here": _levels_in_city(w, region.id, i.key),
            "mine": owned(me),
            "state_here": owned(state),
            # Что даст цепочка, если поставить этот цех здесь и сейчас
            "chain": (chain_bonus_for(w, me.id, region.id, i.key)
                      if me is not None else {"bonus": 0.0, "links": [],
                                              "mates": []}),
            "state_chain": (chain_bonus_for(w, state.id, region.id, i.key)
                            if state is not None else {"bonus": 0.0, "links": [],
                                                       "mates": []}),
            "description": i.description,
            # Содержание штата есть и у производящих административных зданий
            # (оперный театр), поэтому считаем его всегда, а не в одной ветке.
            # Расход бумаги показывается уже с поправкой на закон об
            # образовании: всеобщее право учиться удваивает его разом по всем
            # зданиям, и промышленник обязан видеть настоящую цифру.
            "upkeep_goods": [{"good": k, "name": w.goods[k].name, "qty": round(q, 1)}
                             for k, q in upkeep_needs(c, i, 1).items()],
            "cost_per_level": round(
                sum(q * society.lg(region, k).price
                    for k, q in upkeep_needs(c, i, 1).items())
                + jobs_per_level * wage * (1.0 + max(0.0, c.worker_insurance))
                + config.UPKEEP_PER_LEVEL, 2),
            # ---- учебное заведение ----
            # Ёмкость на уровень и кого ему позволено учить ПО ЗДЕШНЕМУ ЗАКОНУ.
            # Одно и то же здание при разных законах учит разных людей, и
            # видеть это надо до постройки, а не после.
            "education": round(i.education, 1),
            "education_kind": i.education_kind,
            "education_strata": [
                config.STRATA[k]["name"]
                for k in politics.educated_strata(c, i.education_kind)
                if k in config.STRATA] if i.education_kind else [],
            "education_efficiency": (
                round(politics.school_efficiency(c), 3)
                if i.education_kind == "school" else 1.0),
            "education_blocked": (
                i.education_kind == "school" and not politics.school_allowed(c)),
        }
        # Ничего не выпускают не только ратуши, но и торговые площади:
        # ветвимся по наличию выходного товара, а не по виду постройки.
        if i.output_good is None:
            row.update({
                "output_good": None, "output_good_name": "—",
                "shortage": 0.0, "output_demand": 0.0, "has_buyer": True,
                "value_per_worker": 0.0, "sell_through": None,
                "value_per_worker_net": 0.0,
            })
        else:
            good = w.goods[i.output_good]
            local = society.lg(region, i.output_good)
            input_cost = sum(q * society.lg(region, k).price for k, q in i.inputs.items())
            # Сбыт: какая доля выставленного на прилавок вообще нашла
            # покупателя в прошлый пейдей. Затоваренный рынок виден отсюда
            # заранее — и «прибыль на работника» на нём не настоящая.
            sell_through = (min(1.0, local.last_sold / local.last_supply)
                            if local.last_supply > 1.0 else None)
            gross = ((local.price - input_cost) * output_per_worker
                     - wage - upkeep_per_worker) if output_per_worker > 0 else 0.0
            net = ((local.price * (sell_through if sell_through is not None else 1.0)
                    - input_cost) * output_per_worker
                   - wage - upkeep_per_worker) if output_per_worker > 0 else 0.0
            row.update({
                "output_good": i.output_good,
                "output_good_name": good.name,
                "output_per_worker": round(output_per_worker, 2),
                "unit_price": round(local.price, 2),
                "unit_cost": round(input_cost, 2),
                "shortage": round(local.last_shortage, 4),
                "output_demand": round(local.last_demand, 1),
                "has_buyer": local.last_demand > 1.0,
                "sell_through": round(sell_through, 4)
                                if sell_through is not None else None,
                "value_per_worker": round(gross, 2),
                # То же, но с поправкой на сбыт: столько останется, если
                # непроданное так и пролежит на складе.
                "value_per_worker_net": round(net, 2),
                "notional_cost": round(
                    society.notional_unit_cost(w, region, i.output_good, wage) or 0, 2),
            })
        rows.append(row)

    rows.sort(key=lambda r: (r["kind"] == "admin", -r["value_per_worker"]))
    home = ctx.player_country()
    return {"industries": rows, "reference_wage": round(wage, 2),
            "country_id": c.id, "country_name": c.name,
            "region_id": region.id, "region_name": region.name,
            "regions": _region_list(w, c),
            # Переключатель в «Строительстве» живёт своей жизнью: свои области
            # он берёт всегда у своей страны (даже когда смотрим чужую), а
            # рядом отдельной полосой идут открытые для иностранцев чужие.
            "home_regions": _region_list(w, home) if home else [],
            "home_country_id": home.id if home else None,
            "foreign_regions": _foreign_regions(w, ctx.player),
            "region": _region_info(w, region, ctx.player),
            "sectors": [{"key": k, "name": v} for k, v in config.SECTORS.items()],
            "chain": {"link_bonus": config.CHAIN_LINK_BONUS,
                      "sector_bonus": config.CHAIN_SECTOR_BONUS,
                      "cap": config.CHAIN_BONUS_CAP}}


def leaderboard(ctx: Ctx) -> dict:
    w = ctx.world
    rows = []
    for p in w.players.values():
        blds = w.player_buildings(p.id)
        if p.is_state and not blds:
            continue
        country = w.countries.get(p.country_id)
        if p.is_state:
            net = cash = country.treasury if country else 0.0
        else:
            net = w.net_worth(p)
            cash = p.cash
        rows.append({
            "username": p.username,
            "is_state": p.is_state,
            "country": country.name if country else "—",
            "country_id": p.country_id,
            "net_worth": round(net, 2),
            "cash": round(cash, 2),
            "buildings": len(blds),
            "levels": sum(b.level for b in blds),
            "employees": round(sum(b.employed for b in blds)),
            "profit": round(sum(b.last_profit for b in blds), 2),
        })
    rows.sort(key=lambda r: -r["net_worth"])

    # Рейтинг государств — по населению, потому что рынок сбыта это люди, и
    # ступень размера здесь главная строка, а не приписка.
    countries = []
    for c in w.countries.values():
        if not c.alive:
            continue
        pop = _country_pop(w, c.id)
        countries.append({
            "id": c.id, "name": c.name, "color": c.color,
            "population": round(pop),
            "size": society.size_title(pop),
            "size_rank": society.size_rank(pop),
            "regions": len(w.country_regions(c.id)),
            "gdp": round(c.gdp),
            "treasury": round(c.treasury),
            "army": round(society.army_size(w, c)),
            "players": sum(1 for p in w.players.values()
                           if p.country_id == c.id and not p.is_state),
        })
    countries.sort(key=lambda r: -r["population"])
    return {"players": rows, "countries": countries,
            "sizes": [{"at": at, "name": name} for at, name in config.COUNTRY_SIZES]}


def events(ctx: Ctx) -> dict:
    return {"events": db.recent_events(int(ctx.query.get("limit", "25")))}


# ---------------------------------------------------------------------------
# Предприятия
# ---------------------------------------------------------------------------
def _building_dto(w: World, b: Building) -> dict:
    ind = w.industries[b.industry_key]
    # Мест в цехе два числа, и путать их нельзя: полный штат по уровням — и
    # тот, что цех держит на заданной хозяином мощности. Сокращённая смена —
    # это НЕ недобор людей, поэтому «Рабочие» и заполненность считаются от
    # рабочей мощности: иначе цех на половинном ходу вечно выглядел бы
    # недоукомплектованным и пугал бы хозяина мнимой нехваткой рук.
    city = w.cities[b.city_id]
    country = w.countries.get(city.country_id)
    # Мест — по законам той страны, где цех стоит: земельное устройство меняет
    # вместимость крестьянских предприятий (politics.industry_jobs).
    cap_full = b.effective_level * politics.industry_jobs(country, ind)
    cap = cap_full * max(0.0, min(1.0, b.throttle))
    owner = w.players.get(b.owner_id)
    local = city.goods.get(ind.output_good) if ind.output_good else None
    return {
        "id": b.id,
        "industry_key": b.industry_key,
        "industry": ind.name,
        "kind": ind.kind,
        "sector": ind.sector,
        "sector_name": config.SECTORS.get(ind.sector, ind.sector),
        "state": bool(owner and owner.is_state),
        "city": city.name,
        "city_id": b.city_id,
        "country_id": city.country_id,
        "country": country.name if country else "—",
        "level": b.level,
        "effective_level": b.effective_level,
        "damage": b.damage,
        "repair_cost": round(_repair_cost(w, b), 2),
        "foreign": bool(owner and not owner.is_state
                        and owner.country_id != city.country_id),
        "wage": round(b.wage, 2),
        "jobs": round(cap),
        "jobs_full": cap_full,
        "employed": round(b.employed),
        "fill": round(b.employed / cap, 4) if cap else 0.0,
        "active": b.active,
        # Запечатано банкротством: сам хозяин такой цех не запустит, решение
        # за государством.
        "halted": b.halted,
        "throttle": b.throttle,
        # Бонус за производственную цепочку и отраслевой кластер
        "chain_bonus": round(b.chain_bonus, 4),
        "chain": chain_bonus_for(w, b.owner_id, b.city_id, b.industry_key,
                                 skip_id=b.id),
        "output_good": ind.output_good,
        "output_good_name": w.goods[ind.output_good].name if ind.output_good else "—",
        "output_price": round(local.price, 2) if local else 0.0,
        "inputs": [{"good": k, "name": w.goods[k].name, "qty": q}
                   for k, q in ind.inputs.items()],
        "upkeep_goods": [{"good": k, "name": w.goods[k].name, "qty": q * b.level}
                         for k, q in ind.upkeep_goods.items()],
        "last_output": round(b.last_output, 1),
        # Выпустить не значит продать: выручка — это то, за что реально
        # заплатили, а непроданное осело на складе и денег не принесло.
        "last_sold": round(b.last_sold, 1),
        "last_unsold": round(b.last_unsold, 1),
        "last_stock": round(b.last_stock, 1),
        "sell_through": round(b.last_sold / b.last_output, 4)
                        if b.last_output > 1e-9 else None,
        "last_revenue": round(b.last_revenue, 2),
        "last_inputs": round(b.last_inputs, 2),
        "last_wages": round(b.last_wages, 2),
        "last_costs": round(b.last_costs, 2),
        "last_profit": round(b.last_profit, 2),
        "last_active_profit": round(b.last_active_profit, 2),
        "upkeep": round(b.effective_level * config.UPKEEP_PER_LEVEL, 2),
        "upgrade_cost": round(level_cost(ind.build_cost_mult, b.level + 1), 2),
    }


def _repair_cost(w: World, b: Building) -> float:
    """Во сколько обойдётся восстановление всех выбитых войной уровней."""
    if b.damage <= 0:
        return 0.0
    mult = w.industries[b.industry_key].build_cost_mult
    lost = range(b.level - b.damage + 1, b.level + 1)
    return sum(level_cost(mult, lv) for lv in lost) * config.REPAIR_COST_SHARE


def my_buildings(ctx: Ctx) -> dict:
    """Только СВОИ предприятия.

    Казённые сюда намеренно не подмешиваются, даже лидеру: они принадлежат не
    ему, а государству, и управляются во вкладке «Государство». Иначе лидер
    видел бы в своём списке чужие заводы и путался, чью прибыль считает.
    """
    p = ctx.require_player()
    w = ctx.world
    return {"buildings": [_building_dto(w, b) for b in w.player_buildings(p.id)]}


def state_buildings(ctx: Ctx) -> dict:
    """Казённые предприятия государства — ими управляет лидер отсюда."""
    w = ctx.world
    c = _chosen_country(ctx)
    state = w.state_player(c.id)
    rows = [_building_dto(w, b) for b in w.player_buildings(state.id)] if state else []
    rows.sort(key=lambda r: (r["city"], r["industry"]))
    return {"buildings": rows, "country_id": c.id, "country_name": c.name,
            "can_manage": bool(ctx.player and c.leader_id == ctx.player.id)}


def _owner_for(ctx: Ctx, state_owned: bool, country) -> Player:
    """Кто платит и кому принадлежит стройка."""
    w = ctx.world
    if not state_owned:
        return ctx.require_player()
    # казной строит только лидер этого государства
    p, c = ctx.require_leader()
    if c.id != country.id:
        raise ApiError(403, "Казной чужого государства строить нельзя")
    owner = w.state_player(country.id)
    if owner is None:
        raise ApiError(500, "В государстве нет казны")
    return owner


def _charge(ctx: Ctx, owner: Player, cost: float, country, city) -> None:
    """Списать стоимость стройки и отдать её городу как зарплату строителей."""
    w = ctx.world
    state_owned = owner.is_state
    purse = country.treasury if state_owned else owner.cash
    if purse < cost:
        who = "в казне" if state_owned else "у вас"
        raise ApiError(400, f"Не хватает средств: нужно {cost:,.0f} ₡, {who} "
                            f"{purse:,.0f} ₡")
    if state_owned:
        country.spend("construction", cost)
    else:
        owner.cash -= cost
    net = cost * (1.0 - country.income_tax)
    st = city.s("town_low")
    st.cash += net
    st.income += net
    country.collect("income_tax", cost * country.income_tax)


def build_price(country, ind, level: int, state_owned: bool) -> float:
    """Цена уровня С УЧЁТОМ идеологии.

    Скидки достаются разным: либерализм удешевляет стройку промышленнику,
    консерватизм — казне, национализм — военным цехам, но зато всем сразу.
    Одна и та же постройка в двух соседних странах стоит по-разному, и это
    ровно то, ради чего идеологию и принимают.
    """
    base = level_cost(ind.build_cost_mult, level)
    return base * (1.0 - politics.build_discount(country, ind, state_owned))


def _existing_building(w: World, owner_id: int, city_id: int, key: str):
    """Уже построенное предприятие этой отрасли у этого хозяина в этой области."""
    return next((b for b in w.buildings.values()
                 if b.owner_id == owner_id and b.city_id == city_id
                 and b.industry_key == key), None)


def _levels_in_city(w: World, city_id: int, key: str) -> int:
    """Сколько уровней этой отрасли стоит в области — у всех хозяев сразу.

    Предел развития («Торговая палата» — девять уровней) считается по области
    целиком, иначе его обошли бы, поставив вторую палату от другого лица.
    """
    return sum(b.level for b in w.buildings.values()
               if b.city_id == city_id and b.industry_key == key)


def build(ctx: Ctx) -> dict:
    w = ctx.world
    p = ctx.require_player()
    key = str(ctx.need("industry_key"))
    city_id = int(ctx.need("city_id"))
    state_owned = bool(ctx.body.get("state", False))

    ind = w.industries.get(key)
    if not ind:
        raise ApiError(404, "Такой отрасли нет")
    city = w.cities.get(city_id)
    if city is None:
        raise ApiError(404, "Такого города нет")
    country = w.countries.get(city.country_id)
    if country is None:
        raise ApiError(404, "Город не принадлежит государству")
    if ind.kind == "admin" and not state_owned:
        raise ApiError(403, "Административные здания строит только государство")
    # Либерализм выгоняет казну из хозяйства: государственных заводов при нём
    # не строят вовсе. Управлять оно при этом не перестаёт — ратушу, торговую
    # палату и академию строить по-прежнему некому, кроме него.
    if state_owned and politics.state_build_blocked(country, ind):
        raise ApiError(403, "Идеология государства («"
                            + politics.option(country, "ideology")["name"]
                            + "») запрещает казне строить предприятия. "
                              "Казне остались только административные здания.")
    # СОСЛОВНОЕ ОБРАЗОВАНИЕ школ не признаёт: грамота — дворянская привилегия,
    # и учить деревню государству не позволено. Это первая стена, в которую
    # упирается всякий, кто хочет промышленности, — и сломать её можно только
    # законом, а не деньгами.
    if (ind.education_kind == "school"
            and not politics.school_allowed(country)):
        raise ApiError(403, "Действующий закон об образовании («"
                            + politics.option(country, "education")["name"]
                            + "») не позволяет строить школы. "
                              "Переменить его — дело парламента или указа.")
    if not state_owned and p.bankrupt:
        raise ApiError(403, "Вы признаны банкротом: строить нельзя, пока "
                            "государство не закроет дело о банкротстве")

    # Иностранные стройки: вне своего гражданства — только если государство
    # открыло режим иностранных инвестиций и вы с ним не воюете. Выпуск такого
    # завода пойдёт на ЗДЕШНИЙ рынок, а прибыль — вам за границу.
    if not state_owned and city.country_id != p.country_id:
        if not country.foreign_investment_open:
            raise ApiError(403, "Это государство закрыто для иностранных инвестиций. "
                                "Лидер должен открыть страну для стройки иностранцев.")
        if w.at_war(p.country_id, city.country_id):
            raise ApiError(403, f"Ваша страна воюет с {country.name} — "
                                f"вкладываться во врага нельзя")

    owner = _owner_for(ctx, state_owned, country)

    # Второго такого же завода во дворе не ставят: вместо десятка одинаковых
    # лесопилок поднимается одна, но уровнями. В соседней области — пожалуйста,
    # там свой рынок и своя рабочая сила.
    if ind.max_level and _levels_in_city(w, city_id, key) >= ind.max_level:
        raise ApiError(400, f"«{ind.name}» в области {city.name} уже развита до "
                            f"предела ({ind.max_level} ур.)")

    if config.ONE_BUILDING_PER_INDUSTRY:
        twin = _existing_building(w, owner.id, city_id, key)
        if twin is not None:
            who = "У государства" if state_owned else "У вас"
            cost = build_price(country, ind, twin.level + 1, state_owned)
            raise ApiError(400,
                           f"{who} в области {city.name} уже есть «{ind.name}» "
                           f"(ур. {twin.level}). Второе такое же не строят — "
                           f"поднимите уровень за {cost:,.0f} ₡ или стройте "
                           f"в другой области.")

    _charge(ctx, owner, build_price(country, ind, 1, state_owned), country, city)

    b = Building(id=w.next_building_id, industry_key=key, owner_id=owner.id,
                 city_id=city_id, level=1,
                 wage=max(society.reference_wage(w, country) * 1.2, country.min_wage))
    w.buildings[b.id] = b
    w.next_building_id += 1

    who = "Государство" if state_owned else p.username
    db.add_event(w.tick, p.id, "build",
                 f"{who} строит «{ind.name}» в городе {city.name} ({country.name})")
    return {"ok": True, "building": _building_dto(w, b),
            "cash": round(p.cash, 2)}


def _own(ctx: Ctx) -> Building:
    p = ctx.require_player()
    w = ctx.world
    b = w.buildings.get(int(ctx.need("id")))
    if b is None:
        raise ApiError(404, "Предприятие не найдено")
    owner = w.players.get(b.owner_id)
    if b.owner_id == p.id:
        return b
    # лидер государства управляет его казёнными зданиями
    if owner and owner.is_state:
        c = w.countries.get(p.country_id)
        if c is not None and c.leader_id == p.id and owner.country_id == c.id:
            return b
    raise ApiError(404, "Предприятие не найдено")


def upgrade(ctx: Ctx) -> dict:
    w = ctx.world
    b = _own(ctx)
    ind = w.industries[b.industry_key]
    owner = w.players[b.owner_id]
    city = w.cities[b.city_id]
    country = w.countries[city.country_id]
    if owner.bankrupt:
        raise ApiError(403, "Дело признано банкротом: расширяться нельзя, пока "
                            "государство не закроет банкротство")
    if ind.max_level and _levels_in_city(w, b.city_id, b.industry_key) >= ind.max_level:
        raise ApiError(400, f"«{ind.name}» развита до предела: "
                            f"{ind.max_level} ур. — выше не поднять")
    _charge(ctx, owner, build_price(country, ind, b.level + 1, owner.is_state),
            country, city)
    b.level += 1
    return {"ok": True, "building": _building_dto(w, b),
            "cash": round(ctx.require_player().cash, 2)}


def set_wage(ctx: Ctx) -> dict:
    w = ctx.world
    b = _own(ctx)
    wage = float(ctx.need("wage"))
    country = w.countries[w.cities[b.city_id].country_id]
    if wage < country.min_wage:
        raise ApiError(400, f"Ниже МРОТ ({country.min_wage:.0f} ₡)")
    b.wage = wage
    return {"ok": True, "building": _building_dto(w, b)}


def set_wage_all(ctx: Ctx) -> dict:
    """Одна ставка на все предприятия разом — свои или казённые.

    Ставить зарплату каждому цеху поодиночке невыносимо, когда их полтора
    десятка: рабочие уходят туда, где платят больше, и держать разнобой почти
    никогда не нужно. МРОТ у каждой страны свой, поэтому для заграничных цехов
    ставка не отвергается, а поднимается до тамошнего минимума.
    """
    w = ctx.world
    p = ctx.require_player()
    wage = float(ctx.need("wage"))
    if wage < 0 or wage != wage:
        raise ApiError(400, "Зарплата должна быть неотрицательным числом")
    state_scope = str(ctx.body.get("scope") or "mine") == "state"

    if state_scope:
        _, country = ctx.require_leader()
        owner = w.state_player(country.id)
        if owner is None:
            raise ApiError(500, "В государстве нет казны")
        targets = w.player_buildings(owner.id)
    else:
        targets = w.player_buildings(p.id)

    changed = raised = 0
    for b in targets:
        country = w.countries[w.cities[b.city_id].country_id]
        value = max(wage, country.min_wage)
        if value > wage + 1e-9:
            raised += 1
        if abs(b.wage - value) > 1e-9:
            b.wage = value
            changed += 1
    return {"ok": True, "changed": changed, "total": len(targets),
            "raised_to_min_wage": raised, "wage": round(wage, 2),
            "buildings": [_building_dto(w, b) for b in targets]}


def set_throttle(ctx: Ctx) -> dict:
    """Мощность цеха: какую долю штатных мест он держит и, значит, выпускает.

    Рычаг против затоваривания. Остановить цех целиком — мера тупая: люди
    расходятся, склад лежит мёртвым грузом, а содержание уровней платится всё
    равно. Половинная мощность оставляет цех живым, вдвое сокращая и смену, и
    расход сырья, и выпуск. Содержание построек от мощности не зависит — здание
    стоит и на четверть хода, и это цена решения.
    """
    b = _own(ctx)
    value = float(ctx.need("throttle"))
    if value != value:
        raise ApiError(400, "Мощность должна быть числом от 0 до 1")
    b.throttle = max(0.0, min(1.0, value))
    return {"ok": True, "building": _building_dto(ctx.world, b)}


def toggle(ctx: Ctx) -> dict:
    w = ctx.world
    b = _own(ctx)
    if b.halted and not b.active:
        raise ApiError(403, "Предприятие остановлено банкротством. Запустить "
                            "его может только государство, закрыв дело "
                            "о банкротстве.")
    b.active = not b.active
    return {"ok": True, "building": _building_dto(w, b)}


def repair(ctx: Ctx) -> dict:
    """Починить выбитые войной уровни предприятия.

    Казённое здание чинит казна, частное — сам владелец из своего кармана.
    Деньги, как и при стройке, уходят не в пустоту, а строителям — городской
    бедноте того города, где стоит предприятие.
    """
    w = ctx.world
    b = _own(ctx)
    if b.damage <= 0:
        raise ApiError(400, "Предприятие цело — чинить нечего")
    owner = w.players[b.owner_id]
    if owner.bankrupt:
        # Чинить остановленный цех бессмысленно: он всё равно не работает, а
        # деньги нужны, чтобы поднять кассу до порога выхода.
        raise ApiError(403, "Дело признано банкротом: ремонт возможен после того, "
                            "как государство закроет банкротство")
    city = w.cities[b.city_id]
    country = w.countries[city.country_id]

    levels = int(ctx.body.get("levels") or b.damage)
    levels = max(1, min(levels, b.damage))
    mult = w.industries[b.industry_key].build_cost_mult
    # чинят сверху вниз: сначала самый дорогой из выбитых уровней
    lost = list(range(b.level - b.damage + 1, b.level + 1))
    cost = sum(level_cost(mult, lv) for lv in lost[:levels]) * config.REPAIR_COST_SHARE

    _charge(ctx, owner, cost, country, city)
    b.damage -= levels
    db.add_event(w.tick, ctx.require_player().id, "repair",
                 f"{owner.username} восстанавливает «{w.industries[b.industry_key].name}» "
                 f"в городе {city.name} ({levels} ур.)")
    return {"ok": True, "building": _building_dto(w, b),
            "spent": round(cost, 2), "cash": round(ctx.require_player().cash, 2)}


def demolish(ctx: Ctx) -> dict:
    w = ctx.world
    b = _own(ctx)
    ind = w.industries[b.industry_key]
    owner = w.players[b.owner_id]
    country = w.countries[w.cities[b.city_id].country_id]
    refund = sum(level_cost(ind.build_cost_mult, lv)
                 for lv in range(1, b.level + 1)) * config.DEMOLISH_REFUND
    if owner.is_state:
        country.collect("construction", refund)
    else:
        owner.cash += refund
    del w.buildings[b.id]
    return {"ok": True, "refund": round(refund, 2),
            "cash": round(ctx.require_player().cash, 2)}


# ---------------------------------------------------------------------------
# Государство: политика и выборы
# ---------------------------------------------------------------------------
POLICY_LIMITS = {
    "corporate_tax": (0.0, 0.75),
    "sales_tax": (0.0, 0.50),
    "income_tax": (0.0, 0.60),
    # Налоги с населения. Подушная подать задаётся не долей, а твёрдой суммой
    # с души — в этом её суть, и потому у неё свой предел, в червонцах.
    "poll_tax": (0.0, 500.0),
    "tithe": (0.0, 0.50),
    "wealth_tax": (0.0, 0.20),
    "excise_tax": (0.0, 0.80),
    "public_spending_rate": (0.0, 1.0),
    "land_rent": (0.0, 0.50),
    "min_wage": (0.0, 10_000.0),
    # Страховка работника: доля фонда оплаты труда, которую предприятие платит
    # сверх зарплаты прямо работающему сословию. Ставку крутит лидер, нижнюю
    # границу задаёт закон о правах рабочих (politics.tax_bounds).
    "worker_insurance": (0.0, 0.60),
    # Пошлины. Вывозная кормит казну с каждой сделки, но отбивает у своих охоту
    # вывозить; ввозная защищает своего производителя от дешёвого чужого товара.
    "tariff": (0.0, config.TARIFF_MAX),
    "import_tariff": (0.0, config.TARIFF_MAX),
    # Порог банкротства — величина отрицательная: до какого минуса государство
    # позволяет предприятиям работать в долг.
    "bankruptcy_limit": (config.BANKRUPTCY_LIMIT_MIN, 0.0),
}


def policy_limits(country) -> dict[str, tuple[float, float]]:
    """Пределы рычагов лидера С УЧЁТОМ действующих законов.

    Общие границы (POLICY_LIMITS) — это предел здравого смысла: выше него
    ставку не поднять никогда. Законы сужают его дальше, и в этом их главное
    действие: либерализм не позволяет обложить прибыль, консерватизм не даёт
    отменить подати, открытая экономика запирает пошлины на десяти процентах.
    Ползунок, упёршийся в закон, — это и есть повод его менять.
    """
    limits = dict(POLICY_LIMITS)
    cap = politics.tariff_cap(country)
    for field_name in ("tariff", "import_tariff"):
        lo, hi = limits[field_name]
        limits[field_name] = (lo, min(hi, cap))
    for field_name, (lo, hi) in politics.tax_bounds(country).items():
        base_lo, base_hi = limits.get(field_name, (0.0, 1.0))
        limits[field_name] = (max(base_lo, lo), min(base_hi, hi))
    return limits


def gov_policy(ctx: Ctx) -> dict:
    """Лидер меняет экономический курс СВОЕГО государства."""
    p, c = ctx.require_leader()
    changed = []
    for field, (lo, hi) in policy_limits(c).items():
        if ctx.body.get(field) is None:
            continue
        val = float(ctx.body[field])
        if not lo <= val <= hi:
            raise ApiError(400, f"«{field}» должно быть в пределах {lo}–{hi}")
        if abs(getattr(c, field) - val) > 1e-9:
            setattr(c, field, val)
            changed.append(field)
    # отдельный флаг — режим иностранных инвестиций
    if "foreign_investment_open" in ctx.body:
        c.foreign_investment_open = bool(ctx.body["foreign_investment_open"])
        changed.append("foreign_investment_open")
    if changed:
        db.add_event(ctx.world.tick, p.id, "policy",
                     f"{p.username} меняет курс государства {c.name}")
    return {"ok": True, "changed": changed}


def elections(ctx: Ctx) -> dict:
    """Статус выборов по государствам (и список кандидатов своей страны)."""
    w = ctx.world
    out = []
    for cid, country in w.countries.items():
        if not country.alive:
            continue
        out.append({
            "country_id": cid, "country_name": country.name,
            "phase": country.election.phase,
            "started_tick": country.election.started_tick,
            "votes_cast": len(country.election.votes),
        })
    # кандидаты = граждане государства игрока (для голосования)
    c = ctx.player_country()
    candidates = []
    if c is not None:
        for pl in w.players.values():
            if pl.country_id == c.id and not pl.is_state:
                tally = sum(1 for v in c.election.votes.values() if v == pl.id)
                candidates.append({"id": pl.id, "username": pl.username,
                                   "votes": tally,
                                   "is_me": ctx.player is not None and pl.id == ctx.player.id})
    confidence = None
    if c is not None:
        confidence = engine_confidence(w, c)
        confidence["my_vote"] = (c.election.confidence.get(ctx.player.id)
                                 if ctx.player else None)
        leader = w.players.get(c.leader_id) if c.leader_id else None
        confidence["leader"] = leader.username if leader else None
        confidence["min_players"] = config.CONFIDENCE_MIN_PLAYERS
    return {"elections": out, "country_id": c.id if c else None,
            "phase": c.election.phase if c else "none",
            "snap": c.election.snap if c else False,
            "confidence": confidence,
            "candidates": candidates}


def cast_vote(ctx: Ctx) -> dict:
    """Голосовать за кандидата в лидеры своего государства."""
    w = ctx.world
    p = ctx.require_player()
    c = ctx.player_country()
    if c is None:
        raise ApiError(400, "Вы не принадлежите государству")
    if c.election.phase != "voting":
        raise ApiError(400, "Голосование сейчас не идёт")
    candidate_id = int(ctx.need("candidate_id"))
    candidate = w.players.get(candidate_id)
    if candidate is None or candidate.is_state or candidate.country_id != c.id:
        raise ApiError(400, "Такого кандидата нет в вашем государстве")
    c.election.votes[p.id] = candidate_id
    return {"ok": True, "votes_cast": len(c.election.votes)}


# ---------------------------------------------------------------------------
# Законы государства, парламент и лоббирование
# ---------------------------------------------------------------------------
def _party_dto(w: World, country, party, viewer: Player | None) -> dict:
    """Фракция в палате: сколько мест, за что стоит и сколько в неё вложено."""
    total = sum(p.seats for p in country.parties) or 1
    bids = (country.lobby_bids or {}).get(party.key, {})
    return {
        "key": party.key, "name": party.name, "color": party.color,
        "seats": party.seats,
        "share": round(party.seats / total, 4),
        "votes": round(party.votes),
        # Мест, добавленных деньгами промышленников на прошлых выборах.
        "bought": round(party.bought, 1),
        "platform": [
            {"law": cat, "law_name": config.LAWS[cat]["name"],
             "option": opt,
             "option_name": config.LAWS[cat]["options"][opt]["name"],
             "current": politics.law(country, cat) == opt}
            for cat, opt in party.platform.items() if cat in config.LAWS
            and opt in config.LAWS[cat]["options"]],
        # Заявки на БУДУЩИЕ выборы: свои и общие.
        "pledged": round(sum(bids.values()), 2),
        "my_pledge": round(bids.get(viewer.id, 0.0), 2) if viewer else 0.0,
    }


def _law_vote_dto(w: World, country, viewer: Player | None) -> dict | None:
    """Идущее голосование по закону — вместе с раскладом и лоббированием."""
    vote = country.law_vote
    if vote is None:
        return None
    spec = politics.option_of(vote.law, vote.option) or {}
    tally = politics.vote_tally(country)
    proposer = w.players.get(vote.proposer_id) if vote.proposer_id else None
    return {
        "law": vote.law, "law_name": config.LAWS[vote.law]["name"],
        "option": vote.option, "option_name": spec.get("name", vote.option),
        "note": spec.get("note", ""),
        "effects": list(spec.get("effects", [])),
        "started_tick": vote.started_tick, "ends_tick": vote.ends_tick,
        "ticks_left": max(0, vote.ends_tick - w.tick),
        "proposer": proposer.username if proposer else "лидер государства",
        "financed": vote.financed,
        # Мнение палаты снято при постановке вопроса и больше не меняется:
        # дальше идёт только торг за уже высказанные голоса.
        "seats_for": round(vote.seats_for, 1),
        "seats_against": round(vote.seats_against, 1),
        "for": round(tally["for"], 1),
        "against": round(tally["against"], 1),
        "total": round(tally["total"], 1),
        "swing": round(tally["swing"], 1),
        "money_for": round(sum(vote.lobby_for.values()), 2),
        "money_against": round(sum(vote.lobby_against.values()), 2),
        "my_for": round(vote.lobby_for.get(viewer.id, 0.0), 2) if viewer else 0.0,
        "my_against": round(vote.lobby_against.get(viewer.id, 0.0), 2)
                      if viewer else 0.0,
        "passing": tally["for"] > tally["against"],
    }


def _parliament_dto(w: World, country, viewer: Player | None) -> dict:
    seats = politics.seats(country)
    elected = country.parliament_tick
    return {
        "seats": seats,
        "seats_next": politics.next_seats(country),
        "seats_min": config.PARLIAMENT_SEATS_MIN,
        "seats_max": config.PARLIAMENT_SEATS_MAX,
        "seat_cost": config.PARLIAMENT_SEAT_COST,
        "cost": round(politics.parliament_cost(country), 2),
        "elections": politics.has_elections(country),
        "elected_tick": elected,
        "term": config.PARLIAMENT_TERM_TICKS,
        "next_election": (max(0, elected + config.PARLIAMENT_TERM_TICKS - w.tick)
                          if elected >= 0 else 0),
        "parties": [_party_dto(w, country, p, viewer) for p in country.parties],
        "taken": sum(p.seats for p in country.parties),
    }


def _can_lobby(w: World, player: Player | None, country) -> bool:
    """Кому вообще позволено вкладывать деньги в здешнюю политику.

    Гражданам — само собой. Но и чужому промышленнику, у которого в этой
    стране стоят заводы: он платит здешние налоги и живёт по здешним законам,
    так что интерес у него не меньший. Казна в политику не играет.
    """
    if player is None or player.is_state or player.bankrupt:
        return False
    if player.country_id == country.id:
        return True
    return any(b.owner_id == player.id
               and w.cities.get(b.city_id) is not None
               and w.cities[b.city_id].country_id == country.id
               for b in w.buildings.values())


def _revolution_dto(w: World, country) -> dict | None:
    """Идущая революция: кто восстал, чего требует и сколько осталось на ответ.

    Отдаётся всем, а не одному лидеру: революция — не служебная тайна, и
    промышленнику знать о ней важнее всех. Кнопки же есть только у лидера.
    """
    rev = country.revolution
    if rev is None:
        return None
    return {
        "phase": rev.phase,
        "outcome": rev.outcome,
        "started_tick": rev.started_tick,
        "deadline_tick": rev.deadline_tick,
        "ticks_left": max(0, rev.deadline_tick - w.tick)
                      if rev.phase == "demands" else 0,
        "strata": [config.STRATA[k]["name"] for k in rev.strata
                   if k in config.STRATA],
        "demands": [
            {"law": cat, "law_name": config.LAWS[cat]["name"], "option": opt,
             "option_name": (politics.option_of(cat, opt) or {}).get("name", opt),
             "note": (politics.option_of(cat, opt) or {}).get("note", ""),
             "effects": list((politics.option_of(cat, opt) or {}).get("effects", []))}
            for cat, opt in rev.demands.items() if cat in config.LAWS],
        "rebels": round(rev.rebels),
        "defected": round(rev.defected),
        "support": round(rev.support, 4),
        "battles": rev.battles,
        "momentum": round(rev.momentum, 3),
        "gov_losses": round(rev.gov_losses),
        "rebel_losses": round(rev.rebel_losses),
        "army": round(society.army_size(w, country)),
    }


def _require_revolution(ctx: Ctx):
    """Лидер и идущая у него революция — или внятная ошибка."""
    p, c = ctx.require_leader()
    rev = c.revolution
    if rev is None or rev.phase in ("none", "done"):
        raise ApiError(400, "В стране сейчас нет восстания")
    return p, c, rev


def revolution_accept(ctx: Ctx) -> dict:
    """Лидер принимает требования восставших.

    Принять их можно и в разгар гражданской войны, а не только пока идёт срок
    ответа: договориться никогда не поздно — просто до войны это обошлось бы
    без сожжённой столицы и без разбежавшейся армии.
    """
    p, c, _rev = _require_revolution(ctx)
    lines = engine_accept_demands(ctx.world, c)
    for line in lines:
        db.add_event(ctx.world.tick, p.id, "revolution", line)
    return {"ok": True, "news": lines,
            "revolution": _revolution_dto(ctx.world, c)}


def revolution_reject(ctx: Ctx) -> dict:
    """Лидер отказывает восставшим — и тем открывает гражданскую войну.

    Отдельная кнопка нужна затем, чтобы отказ был РЕШЕНИЕМ, а не молчанием:
    промолчав, лидер получит то же самое по истечении срока, но узнает об этом
    из новостей. Здесь же он берёт войну на себя сознательно — и она начинается
    в тот же пейдей, не дожидаясь конца срока.
    """
    p, c, rev = _require_revolution(ctx)
    if rev.phase != "demands":
        raise ApiError(400, "Гражданская война уже идёт")
    rev.deadline_tick = ctx.world.tick
    line = (f"{c.name}: лидер отказывает восставшим — "
            f"требования отклонены, страна идёт к гражданской войне")
    db.add_event(ctx.world.tick, p.id, "revolution", line)
    return {"ok": True, "news": [line],
            "revolution": _revolution_dto(ctx.world, c)}


def laws(ctx: Ctx) -> dict:
    """Всё о форме государства: законы, палата, голосование и цены лоббирования."""
    w = ctx.world
    c = _chosen_country(ctx)
    me = ctx.player
    is_leader = bool(me and c.leader_id == me.id)
    decree = not politics.has_elections(c)
    cooldown = max(0, c.last_law_tick + config.LAW_COOLDOWN_TICKS - w.tick)

    # Капитал считаем один раз на все четырнадцать законов: взнос за
    # рассмотрение зависит от него, а перебирать ради каждого закона все
    # постройки мира незачем.
    worth = w.net_worth(me) if (me and not me.is_state) else None

    categories = []
    for cat in config.LAW_ORDER:
        spec = config.LAWS[cat]
        current = politics.law(c, cat)
        options = []
        for key, opt in spec["options"].items():
            blocked = politics.law_blocked(c, cat, key)
            options.append({
                "key": key, "name": opt["name"], "note": opt.get("note", ""),
                "effects": list(opt.get("effects", [])),
                "current": key == current,
                "blocked": blocked if key != current else None,
                # Насколько нынешняя палата к этому расположена и во что
                # обойдётся промышленнику одна лишь постановка вопроса.
                "support": round(politics.expected_support(c, cat, key), 4),
                "finance_cost": round(
                    politics.finance_cost(w, c, me, cat, key, worth), 2)
                    if worth is not None else None,
            })
        categories.append({
            "key": cat, "name": spec["name"], "note": spec.get("note", ""),
            "current": current,
            "current_name": spec["options"][current]["name"],
            "options": options,
        })

    return {
        "country_id": c.id, "country_name": c.name,
        "is_leader": is_leader,
        "leader": (w.players.get(c.leader_id).username
                   if c.leader_id and w.players.get(c.leader_id) else None),
        "decree": decree,
        "laws": categories,
        "parliament": _parliament_dto(w, c, me),
        "vote": _law_vote_dto(w, c, me),
        # РЕВОЛЮЦИЯ живёт на этой же витрине, и не случайно: требует она
        # именно законов, и отвечать на неё лидеру приходится тем же, чем он
        # правит. Жар показывается всегда — по нему видно, как близко страна к
        # тому, чтобы предъявить требования.
        "revolution": _revolution_dto(w, c),
        "revolution_heat": round(c.revolution_heat, 3),
        "grievance": round(society.country_grievance(w, c), 3),
        "education": round(society.country_education(w, c), 3),
        "cooldown_left": cooldown,
        "vote_ticks": config.LAW_VOTE_TICKS,
        "can_lobby": _can_lobby(w, me, c),
        "lobby": {
            "difficulty": round(politics.lobby_difficulty(c), 3),
            "seat_price": round(politics.seat_price(c), 2),
            "min_stake": round(politics.min_stake(c), 2),
            "power": politics.lobby_power(c),
        },
        # Заготовки партий: из них и собирается палата на каждых выборах.
        # Вкладываться можно в любую — даже в ту, что в прошлый раз не прошла.
        "archetypes": [
            {"key": a["key"], "name": a["names"][0], "color": a["color"],
             "platform": [
                 {"law_name": config.LAWS[cat]["name"],
                  "option_name": config.LAWS[cat]["options"][opt]["name"]}
                 for cat, opt in a["platform"].items()],
             "pledged": round(sum((c.lobby_bids or {})
                                  .get(a["key"], {}).values()), 2),
             "my_pledge": round((c.lobby_bids or {})
                                .get(a["key"], {}).get(me.id, 0.0), 2)
                          if me else 0.0}
            for a in config.PARTY_ARCHETYPES],
        "unfair": {
            "active": politics.unfair_voting(c, w.tick),
            "left": max(0, c.unfair_until - w.tick),
            "penalty": config.UNFAIR_DISCONTENT,
        },
        "cash": round(me.cash, 2) if me else 0.0,
    }


def law_propose(ctx: Ctx) -> dict:
    """Лидер предлагает новый закон.

    При авторитаризме предложение и есть решение: закон вступает в силу тем же
    пейдеем. Там, где выборы уже есть, лидер только ставит вопрос — дальше
    решает палата, а промышленники могут двигать её деньгами.
    """
    w = ctx.world
    leader, c = ctx.require_leader()
    category = str(ctx.need("law"))
    opt = str(ctx.need("option"))
    if category not in config.LAWS:
        raise ApiError(400, "Такой категории законов нет")
    blocked = politics.law_blocked(c, category, opt)
    if blocked:
        raise ApiError(400, blocked)
    if c.law_vote is not None:
        raise ApiError(400, "Палата уже рассматривает другой закон — "
                            "дождитесь итога голосования")
    left = c.last_law_tick + config.LAW_COOLDOWN_TICKS - w.tick
    if left > 0:
        raise ApiError(400, f"Страна только что переменила закон. "
                            f"Следующий можно вынести через {left} пейдей(-ев)")

    name = config.LAWS[category]["options"][opt]["name"]
    if not politics.has_elections(c):
        notes = politics.apply_law(w, c, category, opt)
        db.add_event(w.tick, leader.id, "law",
                     f"{c.name}: указом лидера вводится «{name}»"
                     + (" (" + "; ".join(notes) + ")" if notes else ""))
        return {"ok": True, "applied": True, "notes": notes}

    if not c.parties:
        raise ApiError(400, "Парламент ещё не собран — голосовать некому")
    vote = politics.open_law_vote(w, c, category, opt, leader)
    db.add_event(w.tick, leader.id, "law",
                 f"{c.name}: лидер выносит на голосование «{name}» — "
                 f"предварительно {vote.seats_for:.0f} за, "
                 f"{vote.seats_against:.0f} против")
    return {"ok": True, "applied": False, "vote": _law_vote_dto(w, c, leader)}


def law_finance(ctx: Ctx) -> dict:
    """Промышленник оплачивает САМУ постановку вопроса — мимо лидера.

    Голосование запускается без него: деньги идут в казну, палата садится
    рассматривать закон. Цена считается из того, насколько закон и без того
    нравится депутатам, и из капитала заказчика — чтобы для крупного дельца
    взнос оставался заметным.
    """
    w = ctx.world
    p = ctx.require_player()
    c = _chosen_country(ctx)
    if not _can_lobby(w, p, c):
        raise ApiError(403, "Вкладываться в политику этой страны вам не с чего")
    if not politics.has_elections(c):
        raise ApiError(400, "В стране авторитаризм: законы меняет лидер указом, "
                            "и рассматривать их некому")
    if not c.parties:
        raise ApiError(400, "Парламент ещё не собран")
    if c.law_vote is not None:
        raise ApiError(400, "Палата уже занята другим законом")
    left = c.last_law_tick + config.LAW_COOLDOWN_TICKS - w.tick
    if left > 0:
        raise ApiError(400, f"Страна только что переменила закон. "
                            f"Следующий можно вынести через {left} пейдей(-ев)")
    category = str(ctx.need("law"))
    opt = str(ctx.need("option"))
    if category not in config.LAWS:
        raise ApiError(400, "Такой категории законов нет")
    blocked = politics.law_blocked(c, category, opt)
    if blocked:
        raise ApiError(400, blocked)

    cost = politics.finance_cost(w, c, p, category, opt)
    if p.cash < cost:
        raise ApiError(400, f"Взнос за рассмотрение — {cost:,.0f} ₡, "
                            f"у вас {p.cash:,.0f} ₡")
    p.cash -= cost
    c.collect("law_fee", cost)
    name = config.LAWS[category]["options"][opt]["name"]
    vote = politics.open_law_vote(w, c, category, opt, p, financed=True)
    db.add_event(w.tick, p.id, "law",
                 f"{p.username} вносит {cost:,.0f} ₡ за рассмотрение закона "
                 f"«{name}» в государстве {c.name}")
    return {"ok": True, "cost": round(cost, 2), "cash": round(p.cash, 2),
            "vote": _law_vote_dto(w, c, p)}


def law_lobby(ctx: Ctx) -> dict:
    """Подкупить палату за или против идущего законопроекта.

    Считается не сумма, а РАЗНИЦА вложений сторон: депутаты берут у обоих, а
    голосуют за того, кто дал больше. Деньги не исчезают — они оседают в
    кошельках тех сословий, из которых палата и набрана.
    """
    w = ctx.world
    p = ctx.require_player()
    c = _chosen_country(ctx)
    if not _can_lobby(w, p, c):
        raise ApiError(403, "Вкладываться в политику этой страны вам не с чего")
    if c.law_vote is None:
        raise ApiError(400, "Палата сейчас ничего не рассматривает")
    side = str(ctx.body.get("side") or "for")
    if side not in ("for", "against"):
        raise ApiError(400, "Сторона — «for» или «against»")
    amount = float(ctx.need("amount"))
    lowest = politics.min_stake(c)
    if amount < lowest:
        raise ApiError(400, f"Меньше {lowest:,.0f} ₡ в палате не берут "
                            f"(сложность лоббирования "
                            f"×{politics.lobby_difficulty(c):.2f})")
    if p.cash < amount:
        raise ApiError(400, f"У вас только {p.cash:,.0f} ₡")

    politics.place_law_bid(w, c, p, side, amount)
    seats = amount / politics.seat_price(c) * politics.lobby_power(c)
    db.add_event(w.tick, p.id, "lobby",
                 f"{p.username} вкладывает {amount:,.0f} ₡ "
                 f"{'за' if side == 'for' else 'против'} законопроекта "
                 f"в государстве {c.name} (≈{seats:.0f} голосов)")
    return {"ok": True, "cash": round(p.cash, 2), "seats": round(seats, 1),
            "vote": _law_vote_dto(w, c, p)}


def party_lobby(ctx: Ctx) -> dict:
    """Вложить деньги в партию до выборов: купленные голоса лягут в её итог."""
    w = ctx.world
    p = ctx.require_player()
    c = _chosen_country(ctx)
    if not _can_lobby(w, p, c):
        raise ApiError(403, "Вкладываться в политику этой страны вам не с чего")
    if not politics.has_elections(c):
        raise ApiError(400, "В стране нет выборов — вкладываться не во что")
    key = str(ctx.need("party"))
    if not any(a["key"] == key for a in config.PARTY_ARCHETYPES):
        raise ApiError(404, "Такой партии нет")
    amount = float(ctx.need("amount"))
    lowest = politics.min_stake(c)
    if amount < lowest:
        raise ApiError(400, f"Партия не станет связываться меньше чем "
                            f"за {lowest:,.0f} ₡")
    if p.cash < amount:
        raise ApiError(400, f"У вас только {p.cash:,.0f} ₡")

    politics.place_party_bid(w, c, p, key, amount)
    name = next(a["names"][0] for a in config.PARTY_ARCHETYPES if a["key"] == key)
    db.add_event(w.tick, p.id, "lobby",
                 f"{p.username} вкладывает {amount:,.0f} ₡ в «{name}» "
                 f"({c.name}) перед выборами")
    return {"ok": True, "cash": round(p.cash, 2),
            "parliament": _parliament_dto(w, c, p),
            "pledged": round(sum(c.lobby_bids.get(key, {}).values()), 2)}


def parliament_size(ctx: Ctx) -> dict:
    """Лидер задаёт число кресел в палате.

    Решение с двумя концами: большая палата дорого обходится казне (своя
    статья расходов), зато её труднее перекупить — цена голоса растёт вместе
    с числом кресел. Действует со следующего созыва: разгонять действующий
    парламент ради арифметики нельзя.
    """
    w = ctx.world
    leader, c = ctx.require_leader()
    seats = int(ctx.need("seats"))
    if not config.PARLIAMENT_SEATS_MIN <= seats <= config.PARLIAMENT_SEATS_MAX:
        raise ApiError(400, f"Мест должно быть от {config.PARLIAMENT_SEATS_MIN} "
                            f"до {config.PARLIAMENT_SEATS_MAX}")
    # Число откладывается до ближайших выборов: действующая палата остаётся
    # в том составе, в каком её избирали, вместе со своей ценой голоса и
    # содержанием. Назначить прежний размер — значит отменить назначение.
    c.parliament_seats_next = 0 if seats == politics.seats(c) else seats
    db.add_event(w.tick, leader.id, "law",
                 f"{c.name}: со следующего созыва в палате будет {seats} мест "
                 f"(содержание {seats * config.PARLIAMENT_SEAT_COST:,.0f} ₡ "
                 f"за пейдей)")
    return {"ok": True, "parliament": _parliament_dto(w, c, leader)}


# ---------------------------------------------------------------------------
# Граждане, банкротство и субсидии
# ---------------------------------------------------------------------------
def _citizen_dto(w: World, p: Player, country) -> dict:
    blds = w.player_buildings(p.id)
    halted = [b for b in blds if b.halted]
    exit_at = bankruptcy_exit_level(country)
    return {
        "id": p.id, "username": p.username,
        "cash": round(p.cash, 2),
        "net_worth": round(w.net_worth(p), 2),
        "bankrupt": p.bankrupt,
        "bankrupt_since": p.bankrupt_since,
        "is_leader": country.leader_id == p.id,
        "buildings": len(blds),
        "levels": sum(b.level for b in blds),
        "damaged": sum(b.damage for b in blds),
        "employees": round(sum(b.employed for b in blds)),
        "profit": round(sum(b.last_profit for b in blds), 2),
        # Запечатанные банкротством цеха: их открывает только государство.
        "halted": len(halted),
        "halted_profitable": sum(1 for b in halted if b.last_active_profit >= 0),
        # сколько не хватает, чтобы государство могло снять банкротство
        "rescue_cost": round(max(0.0, exit_at - p.cash), 2),
        "can_release": p.cash > exit_at,
    }


def citizens(ctx: Ctx) -> dict:
    """Список промышленников государства — кто в деле, а кто разорился."""
    w = ctx.world
    c = _chosen_country(ctx)
    rows = [_citizen_dto(w, p, c) for p in engine_citizens(w, c.id)]
    rows.sort(key=lambda r: (not r["bankrupt"], -r["net_worth"]))
    return {
        "citizens": rows,
        "country_id": c.id, "country_name": c.name,
        "treasury": round(c.treasury, 2),
        "bankruptcy_limit": c.bankruptcy_limit,
        "exit_level": round(bankruptcy_exit_level(c), 2),
        "last_subsidies": round(c.last_subsidies, 2),
        "bankrupt": sum(1 for r in rows if r["bankrupt"]),
        "is_leader": bool(ctx.player and c.leader_id == ctx.player.id),
    }


def gov_subsidy(ctx: Ctx) -> dict:
    """Лидер выделяет промышленнику деньги из казны.

    Прямая выплата на счёт: разорившееся дело иначе стоит намертво — на пустой
    кассе не нанять людей, чтобы заработать на найм людей.
    """
    w = ctx.world
    leader, c = ctx.require_leader()
    target = w.players.get(int(ctx.need("player_id")))
    if target is None or target.is_state or target.country_id != c.id:
        raise ApiError(404, "Такого гражданина нет в вашем государстве")
    amount = float(ctx.need("amount"))
    if amount <= 0:
        raise ApiError(400, "Сумма должна быть положительной")
    if c.treasury < amount:
        raise ApiError(400, f"В казне только {c.treasury:,.0f} ₡")

    c.spend("player_subsidy", amount)
    target.cash += amount
    c.last_subsidies += amount
    # Само по себе банкротство субсидия НЕ снимает: деньги и решение — разные
    # вещи. Наполнив кассу, лидер отдельной кнопкой решает, открывать ли дело
    # целиком или только его прибыльную часть (см. gov_bankruptcy).
    db.add_event(w.tick, leader.id, "subsidy",
                 f"{c.name} выделяет {amount:,.0f} ₡ промышленнику "
                 f"{target.username}")
    return {"ok": True, "citizen": _citizen_dto(w, target, c),
            "treasury": round(c.treasury, 2)}


def gov_bankruptcy(ctx: Ctx) -> dict:
    """Государство закрывает дело о банкротстве и решает, что запускать.

    Сам промышленник из банкротства не выходит: иначе получался круг, из
    которого экономика не выбиралась — распродал склад, всплыл, снова
    завалил рынок, снова разорился. Поэтому решение здесь, и у него два
    варианта: поднять всё хозяйство разом или открыть только те цеха, что в
    последний рабочий пейдей давали прибыль. Остальные остаются под замком —
    их можно открыть позже или снести.
    """
    w = ctx.world
    leader, c = ctx.require_leader()
    target = w.players.get(int(ctx.need("player_id")))
    if target is None or target.is_state or target.country_id != c.id:
        raise ApiError(404, "Такого гражданина нет в вашем государстве")
    mode = str(ctx.body.get("mode") or "all")
    if mode not in ("all", "profitable"):
        raise ApiError(400, "Режим — либо «all», либо «profitable»")
    if not target.bankrupt and not any(b.halted for b in w.player_buildings(target.id)):
        raise ApiError(400, f"{target.username} не банкрот — открывать нечего")

    exit_at = bankruptcy_exit_level(c)
    if target.bankrupt and target.cash <= exit_at:
        raise ApiError(400,
                       f"С кассой {target.cash:,.0f} ₡ дело провалится тем же "
                       f"пейдеем. Сначала субсидия: не хватает "
                       f"{exit_at - target.cash:,.0f} ₡ до {exit_at:,.0f} ₡")

    res = engine_release_bankrupt(w, target, mode)
    db.add_event(w.tick, leader.id, "bankruptcy",
                 f"{c.name} закрывает банкротство {target.username}: "
                 f"запущено {res['opened']} предприятий"
                 + (f", {res['sealed']} убыточных оставлены под замком"
                    if res["sealed"] else ""))
    return {"ok": True, "citizen": _citizen_dto(w, target, c), **res}


# ---------------------------------------------------------------------------
# Судьба промышленников на захваченной земле
# ---------------------------------------------------------------------------
def _annex_dto(w: World, a) -> dict:
    def row(pid: int, decided: str | None) -> dict:
        p = w.players.get(pid)
        here = [b for b in w.buildings.values()
                if b.owner_id == pid and b.city_id == a.city_id]
        return {
            "player_id": pid,
            "username": p.username if p else "—",
            "home": (w.countries[p.country_id].name
                     if p and p.country_id in w.countries else "—"),
            "buildings": len(here),
            "levels": sum(b.level for b in here),
            "employees": round(sum(b.employed for b in here)),
            "industries": sorted({w.industries[b.industry_key].name for b in here}),
            "decision": decided,
        }

    return {
        "id": a.id,
        "city": a.city_name, "city_id": a.city_id,
        "former": a.former_name,
        "created_tick": a.created_tick,
        "deadline": a.created_tick + config.ANNEX_DECISION_TICKS,
        "resolved": a.resolved,
        "pending": [row(pid, None) for pid in a.pending],
        "decided": [row(pid, d) for pid, d in a.decisions.items()],
    }


def annexations(ctx: Ctx) -> dict:
    """Занятые области, судьбу промышленников в которых решает лидер."""
    w = ctx.world
    c = ctx.player_country()
    if c is None:
        return {"annexations": [], "is_leader": False}
    mine = [a for a in w.annexations.values() if a.country_id == c.id]
    mine.sort(key=lambda a: (a.resolved, a.created_tick))
    return {
        "annexations": [_annex_dto(w, a) for a in mine],
        "is_leader": bool(ctx.player and c.leader_id == ctx.player.id),
        "expel_refund": config.EXPEL_REFUND,
        "decision_ticks": config.ANNEX_DECISION_TICKS,
        "tick": w.tick,
    }


def annex_decide(ctx: Ctx) -> dict:
    """Решить судьбу промышленников: оставить в деле или выдворить со сносом.

    Без player_id решение применяется ко всем, чью судьбу ещё не решили, —
    это и есть кнопки «оставить всех» и «снести всё».
    """
    w = ctx.world
    leader, c = ctx.require_leader()
    a = w.annexations.get(int(ctx.need("annex_id")))
    if a is None or a.country_id != c.id:
        raise ApiError(404, "Такой области за вами не числится")
    if a.resolved:
        raise ApiError(400, "Судьба этой области уже решена")
    decision = str(ctx.need("decision"))
    if decision not in ("keep", "expel"):
        raise ApiError(400, "Решение — либо «keep», либо «expel»")

    targets = ([int(ctx.body["player_id"])] if ctx.body.get("player_id") is not None
               else list(a.pending))
    for pid in targets:
        if pid not in a.pending:
            raise ApiError(400, "Этот промышленник в списке не значится")
    results = [engine_resolve_annexation(w, a, pid, decision) for pid in targets]

    razed = sum(r.get("buildings", 0) for r in results)
    if decision == "expel":
        db.add_event(w.tick, leader.id, "annex",
                     f"{c.name} выдворяет из области {a.city_name} "
                     f"{len(targets)} промышленник(-ов), снесено {razed} предприятий")
    else:
        db.add_event(w.tick, leader.id, "annex",
                     f"{c.name} оставляет в деле {len(targets)} промышленник(-ов) "
                     f"в области {a.city_name}")
    return {"ok": True, "annexation": _annex_dto(w, a), "results": results}


# ---------------------------------------------------------------------------
# Вотум доверия
# ---------------------------------------------------------------------------
def confidence_vote(ctx: Ctx) -> dict:
    """Гражданин выражает лидеру доверие или недоверие.

    Недоверие большинства граждан-игроков смещает лидера и назначает
    внеочередные выборы — проверка идёт в конце пейдея.
    """
    w = ctx.world
    p = ctx.require_player()
    c = ctx.player_country()
    if c is None:
        raise ApiError(400, "Вы не принадлежите государству")
    if c.leader_id is None:
        raise ApiError(400, "Государством и так никто не правит")
    verdict = str(ctx.need("verdict"))
    if verdict not in ("trust", "distrust"):
        raise ApiError(400, "Ответ — либо «trust», либо «distrust»")
    c.election.confidence[p.id] = verdict
    return {"ok": True, "confidence": engine_confidence(w, c)}


# ---------------------------------------------------------------------------
# Армия
# ---------------------------------------------------------------------------
def gov_army(ctx: Ctx) -> dict:
    """Лидер задаёт военный бюджет и обе ставки жалованья — солдатскую и
    офицерскую, — а также штат офицеров.

    Численность армии — производная от этих чисел: бюджет, делённый на цену
    места в строю (жалованье солдата плюс положенная ему доля офицера).
    Отдельного ползунка «сколько солдат» нет намеренно, иначе казну можно было
    бы увести в минус одним движением.

    Офицерские рычаги устроены так же и решают ровно один вопрос — пойдёт ли на
    службу высшее общество. Оно живёт лучше всех в стране, и патент берёт лишь
    тогда, когда жалованье перебивает его привычный доход; скупому лидеру
    достаются офицеры из среднего класса.
    """
    p, c = ctx.require_leader()
    if ctx.body.get("soldier_pay") is not None:
        pay = float(ctx.body["soldier_pay"])
        if pay < 0:
            raise ApiError(400, "Жалованье не может быть отрицательным")
        c.soldier_pay = pay
    if ctx.body.get("army_budget") is not None:
        budget = float(ctx.body["army_budget"])
        if budget < 0:
            raise ApiError(400, "Военный бюджет не может быть отрицательным")
        c.army_budget = budget
    if ctx.body.get("officer_pay") is not None:
        opay = float(ctx.body["officer_pay"])
        if opay < 0:
            raise ApiError(400, "Жалованье не может быть отрицательным")
        if opay > 0 and opay < c.soldier_pay * config.OFFICER_PAY_MIN_MULT:
            raise ApiError(400, "Офицеру не платят меньше солдата: "
                                f"не ниже {c.soldier_pay:,.2f} ₡")
        c.officer_pay = min(opay, c.soldier_pay * config.OFFICER_PAY_MAX_MULT)
    if ctx.body.get("officer_target") is not None:
        target = float(ctx.body["officer_target"])
        if target < 0:
            raise ApiError(400, "Штат не может быть отрицательным")
        if target > config.OFFICER_TARGET_MAX:
            raise ApiError(400, "Больше "
                                f"{config.OFFICER_TARGET_MAX * 100:.0f}% офицеров "
                                "на солдата содержать бессмысленно")
        c.officer_target = target
    db.add_event(ctx.world.tick, p.id, "army",
                 f"{c.name}: военный бюджет {c.army_budget:,.0f} ₡, "
                 f"жалованье {c.soldier_pay:,.2f} ₡, "
                 f"офицеру {society.officer_pay(c):,.2f} ₡ "
                 f"(штат {society.officer_target_share(c) * 100:.1f}%)")
    return {"ok": True, "army": _army_brief(ctx.world, c)}


def gov_mobilize(ctx: Ctx) -> dict:
    """Объявить насильную мобилизацию.

    Людей забирают приказом, в том числе с заводов, и страна на всё это время
    становится заметно недовольнее.
    """
    p, c = ctx.require_leader()
    c.mobilization_left = min(config.MOBILIZATION_MAX_TICKS,
                              c.mobilization_left + config.MOBILIZATION_TICKS)
    db.add_event(ctx.world.tick, p.id, "war",
                 f"{c.name} объявляет мобилизацию на {c.mobilization_left} пейдеев")
    return {"ok": True, "mobilization_left": c.mobilization_left}


def gov_demobilize(ctx: Ctx) -> dict:
    """Отменить мобилизацию досрочно."""
    p, c = ctx.require_leader()
    c.mobilization_left = 0
    db.add_event(ctx.world.tick, p.id, "war", f"{c.name} прекращает мобилизацию")
    return {"ok": True, "mobilization_left": 0}


def gov_front(ctx: Ctx) -> dict:
    """Поставить на фронт против соседа столько-то солдат и офицеров.

    Людей берут сперва из резерва, потом с соседних фронтов — и не мгновенно:
    за пейдей с каждого направления снимается не больше доли стоящих там людей
    (config.FRONT_MOVE_SHARE). Поэтому перебросить весь кулак на угрожаемую
    границу в последний момент нельзя: решать, где держать войска, приходится
    заранее, а ошибку исправлять несколько пейдеев.
    """
    w = ctx.world
    p, c = ctx.require_leader()
    target = int(ctx.need("country_id"))
    if target not in w.countries:
        raise ApiError(400, "Такого государства нет")
    soldiers = ctx.body.get("soldiers")
    officers = ctx.body.get("officers")
    if soldiers is None and officers is None:
        raise ApiError(400, "Не указано, сколько людей ставить на фронт")

    res = society.set_front(
        w, c, target,
        soldiers=None if soldiers is None else max(0.0, float(soldiers)),
        officers=None if officers is None else max(0.0, float(officers)))
    if res.get("error"):
        raise ApiError(400, res["error"])

    db.add_event(w.tick, p.id, "war",
                 f"{c.name}: на фронт с {w.countries[target].name} назначено "
                 f"{res['soldiers']:,.0f} солдат и {res['officers']:,.0f} офицеров")
    return {"ok": True, "army": _army_brief(w, c),
            "moved": round(res["moved"]), "short": round(res["short"])}


# ---------------------------------------------------------------------------
# Война и дипломатия
# ---------------------------------------------------------------------------
def _war_dto(w: World, war, viewer_id: int | None) -> dict:
    def names(ids):
        return [{"id": i, "name": w.countries[i].name} for i in ids
                if i in w.countries]
    side = war.side_of(viewer_id) if viewer_id else None
    revolt = war.kind == "revolt"
    return {
        "id": war.id,
        "kind": war.kind,
        "revolt": revolt,
        # Мятежникам мира не предлагают — фронт об этом должен знать, чтобы не
        # рисовать кнопку, которая всё равно вернёт отказ.
        "can_peace": not revolt,
        "attackers": names(war.attackers),
        "defenders": names(war.defenders),
        "started_tick": war.started_tick,
        "ended": war.ended,
        "my_side": side,
        "my_enemies": names(war.enemies_of(viewer_id)) if viewer_id else [],
        "separate_peace": [
            {"a": w.countries[int(k.split(":")[0])].name,
             "b": w.countries[int(k.split(":")[1])].name}
            for k in war.peace
            if int(k.split(":")[0]) in w.countries and int(k.split(":")[1]) in w.countries
        ],
        "occupation": [
            {"winner": w.countries[int(k.split(">")[0])].name,
             "loser": w.countries[int(k.split(">")[1])].name,
             "winner_id": int(k.split(">")[0]),
             "loser_id": int(k.split(">")[1]),
             "progress": round(v, 3)}
            for k, v in sorted(war.occupation.items(), key=lambda kv: -kv[1])
            if v > 0.001 and int(k.split(">")[0]) in w.countries
            and int(k.split(">")[1]) in w.countries
        ],
        "report": war.last_report,
    }


def diplomacy(ctx: Ctx) -> dict:
    """Полная картина: войны, союзы, предложения и соседи."""
    w = ctx.world
    c = ctx.player_country()
    cid = c.id if c else None
    me_leader = bool(c and ctx.player and c.leader_id == ctx.player.id)

    wars = [_war_dto(w, war, cid) for war in w.active_wars()]
    my_wars = [x for x in wars if x["my_side"]] if cid else []

    neighbors = []
    if cid:
        # Соседство считается по ВСЕМ областям страны, а не по одной столице:
        # граничит хоть одна область — государство уже сосед, и воевать с ним
        # можно. Раньше здесь брали map_graph[cid], то есть искали страну в
        # графе ОБЛАСТЕЙ по её id: список получался и куцым, и случайным.
        for nb in w.neighbor_countries(cid):
            other = w.countries.get(nb)
            if other is None or not other.alive:
                continue
            neighbors.append({
                "id": nb, "name": other.name, "color": other.color,
                "borders": len(w.border_regions(cid, nb)),
                "leader": (w.players[other.leader_id].username
                           if other.leader_id and other.leader_id in w.players
                           else "AI (без лидера)"),
                "leader_is_ai": other.leader_id is None,
                "population": round(_country_pop(w, nb)),
                "size": society.size_title(_country_pop(w, nb)),
                "size_rank": society.size_rank(_country_pop(w, nb)),
                "army": round(society.army_size(w, other)),
                "strength": round(society.army_strength(w, other)),
                # Что сосед держит ПРОТИВ НАС и что мы держим против него.
                # Первое — грубая разведка, второе — свой приказ, точное.
                "front_vs_me": _front_intel(w, other, cid),
                "my_front": round(society.front_soldiers(c, nb)) if c else 0,
                "my_front_officers": round(society.front_officers(c, nb)) if c else 0,
                "my_front_command": round(society.command_quality(
                    society.front_officers(c, nb),
                    society.front_soldiers(c, nb), c), 3) if c else 0.0,
                "at_war": w.at_war(cid, nb),
                "allied": w.allied(cid, nb),
            })

    allies = [{"id": a, "name": w.countries[a].name,
               "army": round(society.army_size(w, w.countries[a])),
               "at_war": bool(w.wars_of(a))}
              for a in (w.allies_of(cid) if cid else []) if a in w.countries]

    incoming, outgoing = [], []
    for off in w.offers.values():
        row = {
            "id": off.id, "kind": off.kind, "war_id": off.war_id,
            "from_id": off.from_country, "to_id": off.to_country,
            "from": w.countries[off.from_country].name
                    if off.from_country in w.countries else "—",
            "to": w.countries[off.to_country].name
                  if off.to_country in w.countries else "—",
            "created_tick": off.created_tick,
        }
        if off.to_country == cid:
            incoming.append(row)
        elif off.from_country == cid:
            outgoing.append(row)

    return {
        "country_id": cid,
        "is_leader": me_leader,
        "wars": wars,
        "my_wars": my_wars,
        "neighbors": neighbors,
        "allies": allies,
        "incoming": incoming,
        "outgoing": outgoing,
        "peace_min_ticks": config.PEACE_MIN_TICKS,
        "army": _army_brief(w, c) if c else None,
        "tick": w.tick,
    }


def war_declare(ctx: Ctx) -> dict:
    """Объявить войну соседу. Пока это право только лидера государства."""
    w = ctx.world
    p, c = ctx.require_leader()
    target_id = int(ctx.need("country_id"))
    target = w.countries.get(target_id)
    if target is None or not target.alive:
        raise ApiError(404, "Такого государства нет")
    if target_id == c.id:
        raise ApiError(400, "Самому себе войну не объявить")
    if config.WAR_NEIGHBORS_ONLY and not w.are_neighbors(c.id, target_id):
        raise ApiError(400, f"{target.name} — не сосед. Воевать можно только "
                            f"через общую границу")
    if w.at_war(c.id, target_id):
        raise ApiError(400, f"Война с {target.name} уже идёт")
    if w.allied(c.id, target_id):
        raise ApiError(400, "Сначала расторгните союз")

    war = engine_declare_war(w, c.id, target_id)
    db.add_event(w.tick, p.id, "war",
                 f"{c.name} объявляет войну государству {target.name}"
                 + (f" (втянуты союзники: "
                    f"{len(war.attackers) + len(war.defenders) - 2})"
                    if len(war.attackers) + len(war.defenders) > 2 else ""))
    return {"ok": True, "war": _war_dto(w, war, c.id)}


def peace_offer(ctx: Ctx) -> dict:
    """Предложить сепаратный мир одному из противников."""
    from .models import Offer

    w = ctx.world
    p, c = ctx.require_leader()
    war = w.wars.get(int(ctx.need("war_id")))
    if war is None or war.ended:
        raise ApiError(404, "Такой войны нет")
    if war.side_of(c.id) is None:
        raise ApiError(403, "Вы не участвуете в этой войне")
    if war.kind == "revolt":
        raise ApiError(400, "С мятежниками не договариваются: восстание можно "
                            "только подавить или проиграть")
    target_id = int(ctx.need("country_id"))
    if target_id not in war.enemies_of(c.id):
        raise ApiError(400, "Это государство вам не противник в этой войне")
    if w.tick - war.started_tick < config.PEACE_MIN_TICKS:
        left = config.PEACE_MIN_TICKS - (w.tick - war.started_tick)
        raise ApiError(400, f"Слишком рано просить мира: ещё {left} пейдей(-я)")
    for off in w.offers.values():
        if (off.kind == "peace" and off.war_id == war.id
                and {off.from_country, off.to_country} == {c.id, target_id}):
            raise ApiError(400, "Предложение уже на столе")

    off = Offer(id=w.next_offer_id, kind="peace", from_country=c.id,
                to_country=target_id, war_id=war.id, created_tick=w.tick)
    w.offers[off.id] = off
    w.next_offer_id += 1
    db.add_event(w.tick, p.id, "war",
                 f"{c.name} предлагает мир государству {w.countries[target_id].name}")
    return {"ok": True, "offer_id": off.id}


def _take_offer(ctx: Ctx, kind: str):
    """Найти адресованное нам предложение и проверить полномочия."""
    w = ctx.world
    p, c = ctx.require_leader()
    off = w.offers.get(int(ctx.need("offer_id")))
    if off is None or off.kind != kind:
        raise ApiError(404, "Предложение не найдено")
    if off.to_country != c.id:
        raise ApiError(403, "Это предложение адресовано не вам")
    return p, c, off


def peace_accept(ctx: Ctx) -> dict:
    """Принять сепаратный мир: из войны выходят двое, для прочих она идёт."""
    w = ctx.world
    p, c, off = _take_offer(ctx, "peace")
    war = w.wars.get(off.war_id or -1)
    if war is None or war.ended:
        w.offers.pop(off.id, None)
        raise ApiError(400, "Война уже закончена")
    other = w.countries.get(off.from_country)
    make_peace(w, war, off.from_country, off.to_country)
    w.offers.pop(off.id, None)
    db.add_event(w.tick, p.id, "war",
                 f"{c.name} и {other.name if other else '—'} заключают "
                 f"сепаратный мир")
    return {"ok": True, "war_ended": war.ended}


def offer_decline(ctx: Ctx) -> dict:
    """Отклонить любое предложение, адресованное вам."""
    w = ctx.world
    _, c = ctx.require_leader()
    off = w.offers.get(int(ctx.need("offer_id")))
    if off is None or off.to_country != c.id:
        raise ApiError(404, "Предложение не найдено")
    w.offers.pop(off.id, None)
    return {"ok": True}


def alliance_offer(ctx: Ctx) -> dict:
    """Предложить союз. Союзники втягиваются в войны друг друга."""
    from .models import Offer

    w = ctx.world
    p, c = ctx.require_leader()
    target_id = int(ctx.need("country_id"))
    target = w.countries.get(target_id)
    if target is None or not target.alive:
        raise ApiError(404, "Такого государства нет")
    if target_id == c.id:
        raise ApiError(400, "С самим собой не союзничают")
    if w.allied(c.id, target_id):
        raise ApiError(400, f"Союз с {target.name} уже заключён")
    if w.at_war(c.id, target_id):
        raise ApiError(400, "Сначала заключите мир")
    for off in w.offers.values():
        if (off.kind == "alliance"
                and {off.from_country, off.to_country} == {c.id, target_id}):
            raise ApiError(400, "Предложение уже на столе")

    off = Offer(id=w.next_offer_id, kind="alliance", from_country=c.id,
                to_country=target_id, created_tick=w.tick)
    w.offers[off.id] = off
    w.next_offer_id += 1
    db.add_event(w.tick, p.id, "diplomacy",
                 f"{c.name} предлагает союз государству {target.name}")
    return {"ok": True, "offer_id": off.id}


def alliance_accept(ctx: Ctx) -> dict:
    w = ctx.world
    p, c, off = _take_offer(ctx, "alliance")
    if w.at_war(off.from_country, off.to_country):
        w.offers.pop(off.id, None)
        raise ApiError(400, "С противником союз не заключают")
    _add_alliance(w, off.from_country, off.to_country)
    w.offers.pop(off.id, None)
    other = w.countries.get(off.from_country)
    db.add_event(w.tick, p.id, "diplomacy",
                 f"{c.name} и {other.name if other else '—'} заключают союз")
    return {"ok": True}


def alliance_break(ctx: Ctx) -> dict:
    w = ctx.world
    p, c = ctx.require_leader()
    target_id = int(ctx.need("country_id"))
    if not w.allied(c.id, target_id):
        raise ApiError(400, "Такого союза нет")
    w.alliances = [a for a in w.alliances if set(a) != {c.id, target_id}]
    other = w.countries.get(target_id)
    db.add_event(w.tick, p.id, "diplomacy",
                 f"{c.name} разрывает союз с {other.name if other else '—'}")
    return {"ok": True}


def persist_tick(world: World, res: dict) -> None:
    """Записать историю пейдея для графиков — по каждой области отдельно.

    Цены и склады живут не в чертеже товара, а в City.goods: у сорока областей
    они свои. Живёт здесь, а не в main, потому что пейдей крутит не только
    планировщик: его же проводит админка, и история обязана писаться одинаково.
    """
    tick = world.tick
    prices, macro = [], []
    # Военные новости пейдея — в ту же хронику, что и стройки с выборами.
    for line in res.get("news", []):
        db.add_event(tick, None, "war", line)
    for cid, country in world.countries.items():
        if not country.alive:
            continue
        for city in world.country_regions(cid):
            for key, lg in city.goods.items():
                prices.append((tick, city.id, key, lg.price, lg.anchor, lg.stock,
                               lg.last_demand, lg.last_supply))
        m = res.get("countries", {}).get(cid)
        if m:
            macro.append((tick, cid, m["gdp"], m["population"], m["unemployment"],
                          m["satisfaction"], m["avg_wage"], m["treasury"],
                          m["cpi"], m["money_supply"], m["living_standard"]))
    db.write_price_history(prices)
    if macro:
        db.write_macro(macro)
    if world.world_prices:
        db.write_world_prices(tick, world.world_prices)
    db.prune_history(tick)


def _tick_once(world: World) -> dict:
    """Провести пейдей и записать историю.

    История — дело второстепенное: если запись графиков сорвётся, мир всё равно
    обязан сохраниться, иначе игра молча перестанет идти.
    """
    res = run_tick(world)
    try:
        persist_tick(world, res)
    except Exception:
        log.exception("Не удалось записать историю пейдея")
    return res


def force_tick(ctx: Ctx) -> dict:
    """Ручной пейдей — теперь только из админки."""
    ctx.require_admin()
    return _tick_once(ctx.world)


# ---------------------------------------------------------------------------
# Чат: мировой, государственный и личный
# ---------------------------------------------------------------------------
CHAT_CHANNELS = ("world", "country", "private")


def _chat_contacts(w: World, me: Player) -> list[dict]:
    """С кем можно переписываться лично: все живые игроки, кроме казны и себя.

    Свои земляки идут первыми — с ними разговор заходит чаще, чем с
    промышленником с другого конца карты.
    """
    rows = []
    for p in w.players.values():
        if p.is_state or p.id == me.id:
            continue
        country = w.countries.get(p.country_id)
        rows.append({
            "id": p.id, "username": p.username,
            "country_id": p.country_id,
            "country": country.name if country else "—",
            "color": country.color if country else "#6b7a8f",
            "same_country": p.country_id == me.country_id,
            "is_leader": bool(country and country.leader_id == p.id),
        })
    rows.sort(key=lambda r: (not r["same_country"], r["username"].lower()))
    return rows


def chat_view(ctx: Ctx) -> dict:
    """Все три канала разом: мир, своё государство и личная переписка.

    Одним запросом, а не тремя, нарочно: клиент опрашивает чат по таймеру, и
    три ветки в одном ответе избавляют от трёх кругов сети на каждый опрос.
    """
    p = ctx.require_player()
    w = ctx.world
    limit = max(1, min(200, int(ctx.query.get("limit") or config.CHAT_HISTORY)))
    country = w.countries.get(p.country_id)
    now = time.time()
    return {
        "world": db.chat_channel("world", None, limit),
        "country": db.chat_channel("country", p.country_id, limit),
        # Личное отдаётся общим списком, а клиент сам раскладывает его по
        # собеседникам: переписок мало, а лишний параметр «с кем» заставлял бы
        # перезапрашивать чат при каждом переключении вкладки.
        "private": db.chat_private(p.id, limit * 3),
        "contacts": _chat_contacts(w, p),
        "me": p.id,
        "country_name": country.name if country else "—",
        "muted": p.muted(now),
        "mute_until": p.mute_until,
        "mute_forever": p.mute_forever,
        "mute_reason": p.mute_reason,
        "is_admin": is_admin(p),
    }


def chat_send(ctx: Ctx) -> dict:
    p = ctx.require_player()
    w = ctx.world
    now = time.time()

    if p.muted(now):
        if p.mute_forever:
            when = "Вам навсегда закрыт доступ к чату"
        else:
            left = max(1, int((p.mute_until - now) / 60))
            when = f"Вы лишены слова ещё на {left} мин."
        raise ApiError(403, when + (f": {p.mute_reason}" if p.mute_reason else ""))

    channel = str(ctx.body.get("channel") or "world")
    if channel not in CHAT_CHANNELS:
        raise ApiError(400, "Неизвестный канал")
    text = " ".join(str(ctx.need("text")).split())
    if not text:
        raise ApiError(400, "Пустое сообщение")
    if len(text) > config.CHAT_MAX_LEN:
        text = text[:config.CHAT_MAX_LEN]

    to_id = to_name = country_id = None
    if channel == "private":
        to_id = int(ctx.body.get("to_id") or 0)
        target = w.players.get(to_id)
        if target is None or target.is_state or target.id == p.id:
            raise ApiError(400, "Такого собеседника нет")
        to_name = target.username
    elif channel == "country":
        if p.country_id not in w.countries:
            raise ApiError(400, "У вас нет государства")
        country_id = p.country_id

    # Защита от потока сообщений — последней, после всех проверок: иначе
    # неверный запрос получал бы «не так быстро» вместо своей ошибки.
    if now - db.chat_last_ts(p.id) < config.CHAT_MIN_INTERVAL:
        raise ApiError(429, "Не так быстро")

    msg = db.chat_add(w.tick, channel, country_id, p.id, p.username,
                      to_id, to_name, text, now)
    return {"message": msg}


# ---------------------------------------------------------------------------
# Админка
# ---------------------------------------------------------------------------
def _admin_player_dto(w: World, p: Player, now: float) -> dict:
    country = w.countries.get(p.country_id)
    blds = w.player_buildings(p.id)
    return {
        "id": p.id, "username": p.username,
        "is_state": p.is_state, "is_admin": is_admin(p),
        "admin_flag": p.is_admin,     # выданное в игре, без списка при запуске
        "country_id": p.country_id,
        "country": country.name if country else "—",
        "is_leader": bool(country and country.leader_id == p.id),
        "cash": round(p.cash, 2),
        "net_worth": round(w.net_worth(p), 2),
        "buildings": len(blds),
        "bankrupt": p.bankrupt,
        "muted": p.muted(now),
        "mute_until": p.mute_until,
        "mute_forever": p.mute_forever,
        "mute_reason": p.mute_reason,
    }


def admin_state(ctx: Ctx) -> dict:
    """Всё, чем админка управляет: игроки, государства и ход времени."""
    ctx.require_admin()
    w = ctx.world
    now = time.time()
    countries = []
    for cid, c in sorted(w.countries.items()):
        leader = w.players.get(c.leader_id) if c.leader_id else None
        countries.append({
            "id": cid, "name": c.name, "color": c.color, "alive": c.alive,
            "treasury": round(c.treasury, 2),
            "gdp": round(c.gdp, 2),
            "population": round(sum(x.population for x in w.country_regions(cid))),
            "regions": len(w.country_regions(cid)),
            "leader_id": c.leader_id,
            "leader": leader.username if leader else "AI (без лидера)",
            "army_budget": round(c.army_budget, 2),
            "at_war": bool(w.wars_of(cid)),
        })
    return {
        "players": [_admin_player_dto(w, p, now) for p in
                    sorted(w.players.values(), key=lambda x: (x.is_state, x.id))],
        "countries": countries,
        "tick": w.tick,
        "tick_seconds": w.tick_seconds,
        "auto_tick": w.auto_tick,
        "seconds_left": max(0, round(w.tick_seconds - (now - w.last_tick_at))),
        "now": now,
        "env_admins": sorted(config.ADMIN_USERS),
        "chat_channels": list(CHAT_CHANNELS),
    }


def _admin_target(ctx: Ctx) -> Player:
    """Игрок, над которым совершается админское действие."""
    p = ctx.world.players.get(int(ctx.need("player_id")))
    if p is None:
        raise ApiError(404, "Такого игрока нет")
    return p


def admin_tick(ctx: Ctx) -> dict:
    """Прокрутить несколько пейдеев подряд.

    История пишется по каждому, а не только по последнему: иначе на графиках
    оставались бы дыры ровно там, где администратор двигал время.
    """
    ctx.require_admin()
    count = max(1, min(50, int(ctx.body.get("count") or 1)))
    res = {}
    for _ in range(count):
        res = _tick_once(ctx.world)
    return {"ok": True, "ticks": count, "tick": res.get("tick", ctx.world.tick)}


def admin_time(ctx: Ctx) -> dict:
    """Ход времени: автопейдей и длина пейдея."""
    ctx.require_admin()
    w = ctx.world
    if "auto_tick" in ctx.body:
        w.auto_tick = bool(ctx.body["auto_tick"])
    if "tick_seconds" in ctx.body:
        w.tick_seconds = max(1, min(86_400, int(ctx.body["tick_seconds"])))
    return {"ok": True, "auto_tick": w.auto_tick, "tick_seconds": w.tick_seconds}


def admin_mute(ctx: Ctx) -> dict:
    """Лишить игрока слова — на срок или навсегда."""
    ctx.require_admin()
    target = _admin_target(ctx)
    if target.is_state:
        raise ApiError(400, "Казна и так молчит")
    # Ни чужого администратора, ни себя самого: сперва снимите права.
    if is_admin(target):
        raise ApiError(403, "Сначала снимите с него права администратора")
    forever = bool(ctx.body.get("forever"))
    minutes = max(0.0, float(ctx.body.get("minutes") or 0))
    reason = " ".join(str(ctx.body.get("reason") or "").split())[:200]
    if not forever and minutes <= 0:
        raise ApiError(400, "Укажите срок или ставьте немоту навсегда")

    target.mute_forever = forever
    target.mute_until = 0.0 if forever else time.time() + minutes * 60
    target.mute_reason = reason
    db.add_event(ctx.world.tick, target.id, "admin",
                 f"{target.username} лишён слова в чате "
                 + ("навсегда" if forever else f"на {minutes:g} мин.")
                 + (f" — {reason}" if reason else ""))
    return {"ok": True, "player": _admin_player_dto(ctx.world, target, time.time())}


def admin_unmute(ctx: Ctx) -> dict:
    ctx.require_admin()
    target = _admin_target(ctx)
    target.mute_forever = False
    target.mute_until = 0.0
    target.mute_reason = ""
    return {"ok": True, "player": _admin_player_dto(ctx.world, target, time.time())}


def admin_grant(ctx: Ctx) -> dict:
    """Выдать или отобрать права администратора.

    Отобрать право, выданное списком при запуске сервера, отсюда нельзя — оно
    живёт снаружи игры и снимается там же.
    """
    admin = ctx.require_admin()
    target = _admin_target(ctx)
    if target.is_state:
        raise ApiError(400, "Казна не может быть администратором")
    if target.id == admin.id and not ctx.body.get("value"):
        raise ApiError(400, "Снять права с самого себя нельзя")
    target.is_admin = bool(ctx.body.get("value"))
    return {"ok": True, "player": _admin_player_dto(ctx.world, target, time.time())}


def admin_cash(ctx: Ctx) -> dict:
    """Выдать игроку денег или списать их (отрицательная сумма)."""
    ctx.require_admin()
    target = _admin_target(ctx)
    amount = float(ctx.need("amount"))
    target.cash += amount
    db.add_event(ctx.world.tick, target.id, "admin",
                 f"{target.username}: касса изменена на {amount:+,.0f} ₡ "
                 f"решением администратора")
    return {"ok": True, "cash": round(target.cash, 2)}


def admin_treasury(ctx: Ctx) -> dict:
    """Пополнить или обчистить казну государства."""
    ctx.require_admin()
    country = ctx.world.countries.get(int(ctx.need("country_id")))
    if country is None:
        raise ApiError(404, "Такого государства нет")
    amount = float(ctx.need("amount"))
    # Через bookkeeping, а не прямым присвоением: роспись казны обязана
    # сходиться с остатком до червонца, это проверяется тестом.
    if amount >= 0:
        country.collect("spoils", amount)
    else:
        country.spend("losses", -amount)
    return {"ok": True, "treasury": round(country.treasury, 2)}


def admin_bankrupt(ctx: Ctx) -> dict:
    """Объявить банкротом или, наоборот, вернуть в дело."""
    ctx.require_admin()
    target = _admin_target(ctx)
    value = bool(ctx.body.get("value"))
    target.bankrupt = value
    target.bankrupt_since = ctx.world.tick if value else 0
    if not value:
        # Выпускать дело из банкротства без снятия замков бессмысленно: цеха
        # так и останутся стоять, а хозяин не сможет их запустить.
        for b in ctx.world.player_buildings(target.id):
            if b.halted:
                b.halted = False
                b.active = True
    return {"ok": True, "bankrupt": target.bankrupt}


def admin_leader(ctx: Ctx) -> dict:
    """Посадить игрока во главе государства или вернуть страну под AI."""
    ctx.require_admin()
    w = ctx.world
    country = w.countries.get(int(ctx.need("country_id")))
    if country is None:
        raise ApiError(404, "Такого государства нет")
    raw = ctx.body.get("player_id")
    if raw in (None, "", 0, "0"):
        old = w.players.get(country.leader_id) if country.leader_id else None
        if old is not None:
            old.governor_of = None
        country.leader_id = None
        return {"ok": True, "leader": None}

    target = w.players.get(int(raw))
    if target is None or target.is_state:
        raise ApiError(404, "Такого игрока нет")
    if target.country_id != country.id:
        raise ApiError(400, "Лидером может быть только гражданин страны")
    old = w.players.get(country.leader_id) if country.leader_id else None
    if old is not None and old.id != target.id:
        old.governor_of = None
    country.leader_id = target.id
    target.governor_of = country.id
    db.add_event(w.tick, target.id, "admin",
                 f"{target.username} поставлен во главе государства {country.name}")
    return {"ok": True, "leader": target.username}


def admin_chat_delete(ctx: Ctx) -> dict:
    ctx.require_admin()
    if not db.chat_delete(int(ctx.need("id"))):
        raise ApiError(404, "Сообщение уже удалено")
    return {"ok": True}


def admin_chat_clear(ctx: Ctx) -> dict:
    ctx.require_admin()
    channel = str(ctx.body.get("channel") or "world")
    if channel not in CHAT_CHANNELS:
        raise ApiError(400, "Неизвестный канал")
    country_id = ctx.body.get("country_id")
    removed = db.chat_clear(channel, int(country_id) if country_id else None)
    return {"ok": True, "removed": removed}


# ---------------------------------------------------------------------------
Route = tuple[Callable[[Ctx], dict], bool]

ROUTES: dict[tuple[str, str], Route] = {
    ("POST", "/api/register"): (register, True),
    ("POST", "/api/login"): (login, True),
    ("POST", "/api/logout"): (logout, True),
    ("GET", "/api/me"): (me, False),

    ("GET", "/api/world"): (world_state, False),
    ("GET", "/api/market"): (market, False),
    ("GET", "/api/exchange"): (exchange, False),
    ("GET", "/api/market/history"): (market_history, False),
    ("GET", "/api/macro/history"): (macro_history, False),
    ("GET", "/api/cities"): (cities, False),
    ("GET", "/api/regions"): (regions, False),
    ("GET", "/api/population"): (population, False),
    ("GET", "/api/industries"): (industries, False),
    ("GET", "/api/leaderboard"): (leaderboard, False),
    ("GET", "/api/events"): (events, False),

    ("GET", "/api/map"): (map_view, False),
    ("GET", "/api/countries"): (countries_list, False),
    ("GET", "/api/elections"): (elections, False),
    ("POST", "/api/elections/vote"): (cast_vote, True),

    # Законы, парламент и лоббирование
    ("GET", "/api/laws"): (laws, False),
    ("POST", "/api/laws/propose"): (law_propose, True),
    ("POST", "/api/laws/finance"): (law_finance, True),
    ("POST", "/api/laws/lobby"): (law_lobby, True),
    ("POST", "/api/parliament/lobby"): (party_lobby, True),
    ("POST", "/api/parliament/seats"): (parliament_size, True),
    # Ответ лидера восставшим. Кнопок две, потому что решений тоже два, и
    # молчание — это третье, отдельное: оно даёт войну по истечении срока.
    ("POST", "/api/revolution/accept"): (revolution_accept, True),
    ("POST", "/api/revolution/reject"): (revolution_reject, True),

    ("GET", "/api/buildings"): (my_buildings, False),
    ("POST", "/api/buildings/build"): (build, True),
    ("POST", "/api/buildings/upgrade"): (upgrade, True),
    ("POST", "/api/buildings/wage"): (set_wage, True),
    ("POST", "/api/buildings/wage_all"): (set_wage_all, True),
    ("POST", "/api/buildings/throttle"): (set_throttle, True),
    ("POST", "/api/buildings/toggle"): (toggle, True),
    ("POST", "/api/buildings/demolish"): (demolish, True),
    ("POST", "/api/buildings/repair"): (repair, True),

    ("GET", "/api/gov/buildings"): (state_buildings, False),
    ("GET", "/api/gov/citizens"): (citizens, False),
    ("POST", "/api/gov/subsidy"): (gov_subsidy, True),
    ("POST", "/api/gov/bankruptcy"): (gov_bankruptcy, True),
    ("POST", "/api/gov/policy"): (gov_policy, True),
    ("POST", "/api/gov/confidence"): (confidence_vote, True),
    ("GET", "/api/annexations"): (annexations, False),
    ("POST", "/api/annexations/decide"): (annex_decide, True),
    ("POST", "/api/gov/army"): (gov_army, True),
    ("POST", "/api/gov/front"): (gov_front, True),
    ("POST", "/api/gov/mobilize"): (gov_mobilize, True),
    ("POST", "/api/gov/demobilize"): (gov_demobilize, True),

    ("GET", "/api/diplomacy"): (diplomacy, False),
    ("POST", "/api/war/declare"): (war_declare, True),
    ("POST", "/api/war/peace/offer"): (peace_offer, True),
    ("POST", "/api/war/peace/accept"): (peace_accept, True),
    ("POST", "/api/offers/decline"): (offer_decline, True),
    ("POST", "/api/alliance/offer"): (alliance_offer, True),
    ("POST", "/api/alliance/accept"): (alliance_accept, True),
    ("POST", "/api/alliance/break"): (alliance_break, True),

    # Чат живёт в своей таблице, а не в снимке мира, — переписывать мир ради
    # реплики незачем, поэтому оба маршрута идут под read-замком.
    ("GET", "/api/chat"): (chat_view, False),
    ("POST", "/api/chat/send"): (chat_send, False),

    ("GET", "/api/admin/state"): (admin_state, False),
    ("POST", "/api/admin/tick"): (admin_tick, True),
    ("POST", "/api/admin/time"): (admin_time, True),
    ("POST", "/api/admin/mute"): (admin_mute, True),
    ("POST", "/api/admin/unmute"): (admin_unmute, True),
    ("POST", "/api/admin/grant"): (admin_grant, True),
    ("POST", "/api/admin/cash"): (admin_cash, True),
    ("POST", "/api/admin/treasury"): (admin_treasury, True),
    ("POST", "/api/admin/bankrupt"): (admin_bankrupt, True),
    ("POST", "/api/admin/leader"): (admin_leader, True),
    ("POST", "/api/admin/chat/delete"): (admin_chat_delete, False),
    ("POST", "/api/admin/chat/clear"): (admin_chat_clear, False),

    ("POST", "/api/tick"): (force_tick, True),
}
