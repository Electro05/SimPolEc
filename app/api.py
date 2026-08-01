"""
Игровое API. Чистые функции над миром — HTTP-обвязка живёт в main.py.

Каждый обработчик получает Ctx и возвращает словарь, который сервер отдаст
как JSON. Ошибки выбрасываются через ApiError. Большинство данных привязано
к государству игрока (player.country_id) — у каждого государства свои цены,
казна и политика.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from . import config, db
from .auth import hash_password, new_token, verify_password
from .economy.engine import (
    _add_alliance,
    citizen_players as engine_citizens,
    confidence_tally as engine_confidence,
    declare_war as engine_declare_war,
    level_cost, make_peace,
    region_cpi as engine_region_cpi,
    region_trade_capacity as engine_region_caps,
    resolve_annexation as engine_resolve_annexation,
    run_tick, trade_capacity,
)
from .economy.pricing import price_bounds
from .economy import society
from .models import Building, Player, World


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Ctx:
    world: World
    body: dict[str, Any]
    query: dict[str, str]
    token: str | None
    player: Player | None = None

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

    def need(self, key: str) -> Any:
        if key not in self.body:
            raise ApiError(400, f"Не хватает поля «{key}»")
        return self.body[key]


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------
def register(ctx: Ctx) -> dict:
    username = str(ctx.need("username")).strip()
    password = str(ctx.need("password"))
    country_id = int(ctx.body.get("country_id") or 0)
    if not 3 <= len(username) <= 24:
        raise ApiError(400, "Имя должно быть от 3 до 24 символов")
    if len(password) < 4:
        raise ApiError(400, "Пароль должен быть не короче 4 символов")
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

    token = new_token()
    w.sessions[token] = player.id
    db.add_event(w.tick, player.id, "join",
                 f"{username} открывает своё дело в государстве {country.name}")
    return {"token": token, "username": username,
            "is_governor": player.is_governor, "country_id": country_id,
            "country_name": country.name}


def login(ctx: Ctx) -> dict:
    username = str(ctx.need("username")).strip()
    password = str(ctx.need("password"))
    w = ctx.world
    player = next((p for p in w.players.values()
                   if p.username.lower() == username.lower() and not p.is_state), None)
    if not player or not verify_password(password, player.salt, player.password_hash):
        raise ApiError(401, "Неверное имя или пароль")
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
        "warehouse": warehouse,
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
            "at_war": bool(w.wars_of(country.id)),
            "at_war_with_me": mine is not None and w.at_war(mine, country.id),
            "allied_with_me": mine is not None and w.allied(mine, country.id),
        })
    return {"nodes": nodes}


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
                     "capital": w.cities[country.capital_city_id].name})
    return {"countries": rows}


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
        employed = sum(b.employed for b in w.buildings.values() if b.city_id in ids)
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
        })
    return {
        "tick": w.tick,
        "tick_seconds": w.tick_seconds,
        "seconds_left": max(0.0, w.tick_seconds - elapsed),
        "country": _country_brief(w, c) if c else None,
        # цифры шапки — по своей стране
        "home": home,
        "world": {**block(world_cities),
                  "gdp": round(sum(co.gdp for co in alive)),
                  "treasury": round(sum(co.treasury for co in alive)),
                  "countries": len(alive)},
    }


def _country_brief(w: World, c) -> dict:
    workers = sum(city.s("workers").people for city in w.cities.values()
                  if city.country_id == c.id)
    employed = sum(b.employed for b in w.buildings.values()
                   if w.cities[b.city_id].country_id == c.id)
    leader = w.players.get(c.leader_id) if c.leader_id else None
    return {
        "id": c.id, "name": c.name, "color": c.color,
        "treasury": round(c.treasury, 2),
        "gdp": round(c.gdp, 2),
        "corporate_tax": c.corporate_tax,
        "sales_tax": c.sales_tax,
        "income_tax": c.income_tax,
        "public_spending_rate": c.public_spending_rate,
        "min_wage": c.min_wage,
        "land_rent": c.land_rent,
        "bankruptcy_limit": c.bankruptcy_limit,
        "foreign_investment_open": c.foreign_investment_open,
        "living_standard": round(society.country_living_standard(w, c), 3),
        "industrialisation": round(workers / sum(ct.population for ct in w.cities.values()
                                                 if ct.country_id == c.id), 4)
                              if workers > 0 else 0.0,
        "regions": len(w.country_regions(c.id)),
        "unemployment": round(max(0.0, 1.0 - employed / workers), 4)
                        if workers > 1 else 0.0,
        "leader": leader.username if leader else "AI (без лидера)",
        "leader_is_ai": c.leader_id is None,
        "reference_wage": round(society.reference_wage(w, c), 2),
        "alive": c.alive,
        "army": _army_brief(w, c),
    }


def _army_brief(w: World, c) -> dict:
    """Сводка по армии государства: люди, деньги, снабжение."""
    soldiers = society.army_size(w, c)
    afford = society.affordable_army(w, c)
    need_shells = soldiers * config.SHELLS_PER_SOLDIER_BATTLE
    return {
        "soldiers": round(soldiers),
        "affordable": round(afford),
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
            "position": round((local.price - lo) / (hi - lo), 4) if hi > lo else 0.5,
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
            "trade_capacity": round(engine_region_caps(w).get(city.id, 0.0), 4)}


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
    return [{"id": c.id, "name": c.name,
             "population": round(c.population),
             "capital": c.id == country.capital_city_id,
             "unrest": round(c.unrest, 3),
             "revolt": c.revolt_ticks > 0}
            for c in sorted(w.country_regions(country.id),
                            key=lambda x: (x.id != country.capital_city_id, x.name))]


def regions(ctx: Ctx) -> dict:
    """Области выбранной страны и общая сводка по каждой."""
    w = ctx.world
    c = _chosen_country(ctx)
    rows = []
    for city in w.country_regions(c.id):
        workers = city.s("workers").people
        employed = sum(b.employed for b in w.city_buildings(city.id))
        rows.append({
            "id": city.id, "name": city.name,
            "capital": city.id == c.capital_city_id,
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
    return {"regions": rows, "country_id": c.id, "country_name": c.name}


def exchange(ctx: Ctx) -> dict:
    """Биржа мирового рынка: где товар дёшев, где дорог и кто чем торгует.

    Мир замкнут: вывезенный товар обязан быть кем-то куплен, поэтому здесь
    видно обе стороны каждой сделки прошлого пейдея — кто вывез, кто ввёз и
    сколько досталось казне пошлиной.
    """
    w = ctx.world
    caps = trade_capacity(w)
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
                "is_mine": home is not None and cid == home.id}
               for cid, cap in sorted(caps.items(), key=lambda kv: -kv[1])
               if cap > 0 and w.countries[cid].alive]

    return {
        "goods": rows,
        "traders": traders,
        "tariff": config.WORLD_TRADE_TARIFF,
        "share_per_level": config.WORLD_MARKET_SHARE_PER_LEVEL,
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
            "jobs": round(sum(b.level * w.industries[b.industry_key].jobs_per_level
                              for b in w.city_buildings(c.id))),
            "harvest": round(c.harvest, 3),
            "savings": round(c.savings, 2),
            "unemployment": round(c.unemployment, 4),
            "satisfaction": round(c.satisfaction, 4),
            "avg_wage": round(c.avg_wage, 2),
        })
    return {"cities": rows}


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
               "sol": 0.0, "expect": 0.0}
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
    total_pop = sum(a["people"] for a in agg.values()) or 1.0
    strata = []
    for key in config.STRATA_ORDER:
        a = agg[key]
        spec = config.STRATA[key]
        sat = a["satisfaction"] / a["people"] if a["people"] > 1.0 else 0.0
        strata.append({
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
    employed = sum(b.employed for b in w.buildings.values()
                   if b.city_id in scope_ids)
    sol = (society.country_living_standard(w, c) if whole
           else society.region_living_standard(region))
    return {
        "strata": strata, "cities": by_city, "crafts": craft_rows,
        "army": _army_brief(w, c),
        "living_standard": round(sol, 3),
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
    }


def industries(ctx: Ctx) -> dict:
    w = ctx.world
    c = _chosen_country(ctx)
    region = _chosen_region(ctx)
    wage = society.reference_wage(w, c)
    upkeep_per_worker = config.UPKEEP_PER_LEVEL / config.JOBS_PER_LEVEL

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

        row = {
            "key": i.key, "name": i.name, "kind": i.kind,
            "jobs_per_level": i.jobs_per_level,
            "inputs": inputs,
            "inputs_ready": all(x["available"] for x in inputs),
            "build_cost": round(level_cost(i.build_cost_mult, 1), 2),
            "state_levels": state_lv, "private_levels": private_lv,
            "description": i.description,
            # Содержание штата есть и у производящих административных зданий
            # (оперный театр), поэтому считаем его всегда, а не в одной ветке.
            "upkeep_goods": [{"good": k, "name": w.goods[k].name, "qty": q}
                             for k, q in i.upkeep_goods.items()],
            "cost_per_level": round(
                sum(q * society.lg(region, k).price for k, q in i.upkeep_goods.items())
                + i.jobs_per_level * wage + config.UPKEEP_PER_LEVEL, 2),
        }
        # Ничего не выпускают не только ратуши, но и торговые площади:
        # ветвимся по наличию выходного товара, а не по виду постройки.
        if i.output_good is None:
            row.update({
                "output_good": None, "output_good_name": "—",
                "shortage": 0.0, "output_demand": 0.0, "has_buyer": True,
                "value_per_worker": 0.0,
            })
        else:
            good = w.goods[i.output_good]
            local = society.lg(region, i.output_good)
            input_cost = sum(q * society.lg(region, k).price for k, q in i.inputs.items())
            row.update({
                "output_good": i.output_good,
                "output_good_name": good.name,
                "output_per_worker": i.output_per_worker,
                "unit_price": round(local.price, 2),
                "unit_cost": round(input_cost, 2),
                "shortage": round(local.last_shortage, 4),
                "output_demand": round(local.last_demand, 1),
                "has_buyer": local.last_demand > 1.0,
                "value_per_worker": round(
                    (local.price - input_cost) * i.output_per_worker
                    - wage - upkeep_per_worker, 2) if i.output_per_worker > 0 else 0.0,
                "notional_cost": round(
                    society.notional_unit_cost(w, region, i.output_good, wage) or 0, 2),
            })
        rows.append(row)

    rows.sort(key=lambda r: (r["kind"] == "admin", -r["value_per_worker"]))
    return {"industries": rows, "reference_wage": round(wage, 2),
            "country_id": c.id, "country_name": c.name,
            "region_id": region.id, "region_name": region.name,
            "regions": _region_list(w, c)}


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
    return {"players": rows}


def events(ctx: Ctx) -> dict:
    return {"events": db.recent_events(int(ctx.query.get("limit", "25")))}


# ---------------------------------------------------------------------------
# Предприятия
# ---------------------------------------------------------------------------
def _building_dto(w: World, b: Building) -> dict:
    ind = w.industries[b.industry_key]
    cap = b.effective_level * ind.jobs_per_level
    city = w.cities[b.city_id]
    country = w.countries.get(city.country_id)
    owner = w.players.get(b.owner_id)
    local = city.goods.get(ind.output_good) if ind.output_good else None
    return {
        "id": b.id,
        "industry_key": b.industry_key,
        "industry": ind.name,
        "kind": ind.kind,
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
        "jobs": cap,
        "employed": round(b.employed),
        "fill": round(b.employed / cap, 4) if cap else 0.0,
        "active": b.active,
        "throttle": b.throttle,
        "output_good": ind.output_good,
        "output_good_name": w.goods[ind.output_good].name if ind.output_good else "—",
        "output_price": round(local.price, 2) if local else 0.0,
        "inputs": [{"good": k, "name": w.goods[k].name, "qty": q}
                   for k, q in ind.inputs.items()],
        "upkeep_goods": [{"good": k, "name": w.goods[k].name, "qty": q * b.level}
                         for k, q in ind.upkeep_goods.items()],
        "last_output": round(b.last_output, 1),
        "last_revenue": round(b.last_revenue, 2),
        "last_inputs": round(b.last_inputs, 2),
        "last_wages": round(b.last_wages, 2),
        "last_costs": round(b.last_costs, 2),
        "last_profit": round(b.last_profit, 2),
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
        country.treasury -= cost
    else:
        owner.cash -= cost
    net = cost * (1.0 - country.income_tax)
    st = city.s("town_low")
    st.cash += net
    st.income += net
    country.treasury += cost * country.income_tax


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
    _charge(ctx, owner, level_cost(ind.build_cost_mult, 1), country, city)

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
    _charge(ctx, owner, level_cost(ind.build_cost_mult, b.level + 1), country, city)
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


def set_throttle(ctx: Ctx) -> dict:
    b = _own(ctx)
    b.throttle = max(0.0, min(1.0, float(ctx.need("throttle"))))
    return {"ok": True, "building": _building_dto(ctx.world, b)}


def toggle(ctx: Ctx) -> dict:
    b = _own(ctx)
    b.active = not b.active
    return {"ok": True, "building": _building_dto(ctx.world, b)}


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
        country.treasury += refund
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
    "public_spending_rate": (0.0, 1.0),
    "land_rent": (0.0, 0.50),
    "min_wage": (0.0, 10_000.0),
    # Порог банкротства — величина отрицательная: до какого минуса государство
    # позволяет предприятиям работать в долг.
    "bankruptcy_limit": (config.BANKRUPTCY_LIMIT_MIN, 0.0),
}


def gov_policy(ctx: Ctx) -> dict:
    """Лидер меняет экономический курс СВОЕГО государства."""
    p, c = ctx.require_leader()
    changed = []
    for field, (lo, hi) in POLICY_LIMITS.items():
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
# Граждане, банкротство и субсидии
# ---------------------------------------------------------------------------
def _citizen_dto(w: World, p: Player, country) -> dict:
    blds = w.player_buildings(p.id)
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
        # сколько не хватает, чтобы выйти из банкротства
        "rescue_cost": round(max(0.0, country.bankruptcy_limit
                                 * (1.0 - config.BANKRUPTCY_EXIT_MARGIN)
                                 - p.cash), 2),
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
        "last_subsidies": round(c.last_subsidies, 2),
        "bankrupt": sum(1 for r in rows if r["bankrupt"]),
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

    c.treasury -= amount
    target.cash += amount
    c.last_subsidies += amount
    # субсидия может сразу вывести дело из банкротства
    exit_at = c.bankruptcy_limit * (1.0 - config.BANKRUPTCY_EXIT_MARGIN)
    if target.bankrupt and target.cash > exit_at:
        target.bankrupt = False
    db.add_event(w.tick, leader.id, "subsidy",
                 f"{c.name} выделяет {amount:,.0f} ₡ промышленнику "
                 f"{target.username}")
    return {"ok": True, "citizen": _citizen_dto(w, target, c),
            "treasury": round(c.treasury, 2)}


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
    """Лидер задаёт ставку жалованья и военный бюджет.

    Численность армии — производная от этих двух чисел: бюджет, делённый на
    ставку. Отдельного ползунка «сколько солдат» нет намеренно, иначе казну
    можно было бы увести в минус одним движением.
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
    db.add_event(ctx.world.tick, p.id, "army",
                 f"{c.name}: военный бюджет {c.army_budget:,.0f} ₡, "
                 f"жалованье {c.soldier_pay:,.2f} ₡")
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


# ---------------------------------------------------------------------------
# Война и дипломатия
# ---------------------------------------------------------------------------
def _war_dto(w: World, war, viewer_id: int | None) -> dict:
    def names(ids):
        return [{"id": i, "name": w.countries[i].name} for i in ids
                if i in w.countries]
    side = war.side_of(viewer_id) if viewer_id else None
    return {
        "id": war.id,
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
                "population": round(sum(x.population for x in w.cities.values()
                                        if x.country_id == nb)),
                "army": round(society.army_size(w, other)),
                "strength": round(society.army_strength(w, other)),
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


def force_tick(ctx: Ctx) -> dict:
    """Ручной пейдей — для отладки и одиночной игры."""
    return run_tick(ctx.world)


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

    ("GET", "/api/buildings"): (my_buildings, False),
    ("POST", "/api/buildings/build"): (build, True),
    ("POST", "/api/buildings/upgrade"): (upgrade, True),
    ("POST", "/api/buildings/wage"): (set_wage, True),
    ("POST", "/api/buildings/throttle"): (set_throttle, True),
    ("POST", "/api/buildings/toggle"): (toggle, True),
    ("POST", "/api/buildings/demolish"): (demolish, True),
    ("POST", "/api/buildings/repair"): (repair, True),

    ("GET", "/api/gov/buildings"): (state_buildings, False),
    ("GET", "/api/gov/citizens"): (citizens, False),
    ("POST", "/api/gov/subsidy"): (gov_subsidy, True),
    ("POST", "/api/gov/policy"): (gov_policy, True),
    ("POST", "/api/gov/confidence"): (confidence_vote, True),
    ("GET", "/api/annexations"): (annexations, False),
    ("POST", "/api/annexations/decide"): (annex_decide, True),
    ("POST", "/api/gov/army"): (gov_army, True),
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

    ("POST", "/api/tick"): (force_tick, True),
}
