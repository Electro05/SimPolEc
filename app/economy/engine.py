"""
Экономический движок SimPolEc.

Один вызов run_tick(world) = один «пейдей».

Мир состоит из множества государств, у каждого свой рынок (свои цены и склады
в Country.goods) и своя казна/налоги. Пейдей устроен так: сначала экономический
цикл по каждому государству (наём, производство, торговля, налоги), затем —
выборы лидеров, мировой рынок (связывает локальные рынки арбитражем), порча
запасов и общий пересчёт цен.

Ключевое правило: внутри тика цены заморожены. Все сделки идут по ценам,
объявленным в начале пейдея, а новые цены считаются в самом конце по факту.
"""
from __future__ import annotations

import time
from collections import defaultdict

from .. import config
from ..models import Country, Player, War, World
from . import society
from .pricing import next_price, update_anchor
from .society import EPS, decode_owner, stratum_owner_id


def level_cost(build_cost_mult: float, level: int) -> float:
    """Стоимость постройки/апгрейда до указанного уровня."""
    return (config.BUILD_COST_BASE * build_cost_mult
            * level ** config.BUILD_COST_EXPONENT)


# ---------------------------------------------------------------------------
# Рынок одного государства
# ---------------------------------------------------------------------------
class RegionMarket:
    """Рынок ОБЛАСТИ: товар лежит на нём, но принадлежит владельцу.

    Рынок живёт в области, а не в стране: у каждой свои цены, свой склад и свой
    дефицит. Владельцем товара может быть игрок (id > 0) или сословие этой
    области (id < 0). Выпуск текущего пейдея попадает в отдельный «приход» и
    вливается в склад только в конце тика — иначе производитель раньше по
    списку успевал бы продать свой товар тем, кто идёт следом.

    **Иностранные инвестиции.** Прилавок открыт не только местным: на нём
    торгует всякий, у кого в этой области есть завод. Выпуск попадает на
    ЗДЕШНИЙ рынок и продаётся здешним жителям по здешним ценам, а выручка
    уходит владельцу, где бы тот ни жил. Поэтому склад игрока хранится отдельно
    по каждой области (Player.warehouses): будь он общим, рынок затирал бы
    чужие остатки в конце пейдея, и непроданный товар просто исчезал бы вместе
    с вложенными в него деньгами.
    """

    def __init__(self, world: World, country: Country, city):
        self.world = world
        self.country = country
        self.city = city
        self.stores: dict[int, dict[str, float]] = {}

        owner_ids = set()
        for b in world.buildings.values():
            if b.city_id == city.id:
                owner_ids.add(b.owner_id)     # в том числе иностранный инвестор
        for p in world.players.values():
            # казна страны торгует на всех своих рынках, прочие — там, где у
            # них уже что-то лежит (например, привезённое из другой области)
            if p.is_state and p.country_id == country.id:
                owner_ids.add(p.id)
            elif p.warehouses.get(city.id):
                owner_ids.add(p.id)
        for pid in owner_ids:
            p = world.players.get(pid)
            if p is not None:
                self.stores[pid] = p.store(city.id)
        for key in config.STRATA_ORDER:
            self.stores[stratum_owner_id(city.id, key)] = city.s(key).warehouse

        self.lots: dict[str, dict[int, float]] = defaultdict(dict)
        for owner_id, store in self.stores.items():
            for key, qty in store.items():
                if qty > EPS and world.goods.get(key) is not None:
                    if world.goods[key].storable:
                        self.lots[key][owner_id] = qty

        self.incoming: dict[str, dict[int, float]] = defaultdict(dict)
        self.revenue: dict[int, float] = defaultdict(float)
        self.produced: dict[str, float] = defaultdict(float)
        self.tax_collected = 0.0

    def available(self, key: str) -> float:
        return sum(self.lots[key].values())

    def total(self, key: str) -> float:
        return self.available(key) + sum(self.incoming[key].values())

    def deposit(self, key: str, owner_id: int, qty: float,
                immediate: bool = False) -> None:
        if qty <= EPS:
            return
        target = self.lots if immediate else self.incoming
        target[key][owner_id] = target[key].get(owner_id, 0.0) + qty
        self.produced[key] += qty

    def buy(self, key: str, qty: float, price: float, sales_tax: float = 0.0) -> float:
        if qty <= EPS:
            return 0.0
        holders = self.lots[key]
        total = sum(holders.values())
        if total <= EPS:
            return 0.0

        taken = min(qty, total)
        share = taken / total
        self.tax_collected += taken * price * sales_tax

        for owner_id in list(holders):
            part = holders[owner_id] * share
            holders[owner_id] -= part
            if holders[owner_id] <= EPS:
                del holders[owner_id]
            self.revenue[owner_id] += part * price * (1.0 - sales_tax)
        return taken

    def flush(self) -> None:
        """Влить выпуск на склад, применить порчу, записать всё обратно в мир."""
        for key, holders in self.incoming.items():
            for owner_id, qty in holders.items():
                self.lots[key][owner_id] = self.lots[key].get(owner_id, 0.0) + qty
        self.incoming.clear()

        per_owner: dict[int, dict[str, float]] = defaultdict(dict)
        for key, holders in self.lots.items():
            good = self.world.goods.get(key)
            if good is None or not good.storable:
                continue    # неоказанные услуги просто пропадают
            # порча: perishable товары теряют долю запаса за пейдей
            decay = 1.0 - good.perish_rate
            for owner_id, qty in holders.items():
                qty *= decay
                if qty > EPS:
                    per_owner[owner_id][key] = qty

        for owner_id, store in self.stores.items():
            store.clear()
            store.update(per_owner.get(owner_id, {}))
        for key, good in self.world.goods.items():
            lg = self.city.goods.get(key)
            if lg is None:
                continue
            lg.stock = self.available(key) if good.storable else 0.0


# ---------------------------------------------------------------------------
# Рынок труда
# ---------------------------------------------------------------------------
def allocate_labor(world: World, country: Country, city_id: int) -> float:
    """Распределить рабочих города по предприятиям. Возвращает занятость."""
    city = world.cities[city_id]
    buildings = world.city_buildings(city_id)
    workers = city.s("workers").people
    active = [b for b in buildings
              if b.active and b.effective_level > 0 and b.throttle > EPS]

    for b in buildings:
        if b not in active:
            b.employed = 0.0

    if not active or workers <= EPS:
        city.unemployment = 1.0 if workers > EPS else 0.0
        return 0.0

    # Разрушенные войной уровни рабочих мест не дают, пока их не починят.
    caps = {b.id: b.effective_level * world.industries[b.industry_key].jobs_per_level
            * max(0.0, min(1.0, b.throttle)) for b in active}
    total_cap = sum(caps.values())
    if total_cap <= EPS:
        for b in active:
            b.employed = 0.0
        city.unemployment = 1.0
        return 0.0

    wages = {b.id: max(b.wage, country.min_wage) for b in active}

    # Жадное заполнение по убыванию зарплаты: кто больше платит, нанимает первым.
    supply = min(workers, total_cap)
    alloc = {b.id: 0.0 for b in active}
    remaining = supply
    for b in sorted(active, key=lambda x: (wages[x.id], -x.id), reverse=True):
        if remaining <= EPS:
            break
        take = min(caps[b.id], remaining)
        alloc[b.id] = take
        remaining -= take

    s = config.LABOR_STICKINESS
    for b in active:
        b.employed = max(0.0, min(caps[b.id], b.employed * s + alloc[b.id] * (1 - s)))

    employed = sum(b.employed for b in active)
    city.unemployment = max(0.0, 1.0 - employed / max(workers, EPS))
    if employed > EPS:
        city.avg_wage = sum(b.employed * wages[b.id] for b in active) / employed
    return employed


# ---------------------------------------------------------------------------
# Мятежи и гражданская война
# ---------------------------------------------------------------------------
def step_unrest(world: World) -> list[str]:
    """Озлобление, бунты и гражданские войны по областям.

    Нищая область копит недовольство. Дошло до края — открытый бунт: заводы
    жгут, люди гибнут, производство встаёт, казна тратится на усмирение. Если
    бунт не унять достаточно долго, область откалывается и объявляет
    собственное государство — это и есть гражданская война.
    """
    news: list[str] = []
    for city in list(world.cities.values()):
        country = world.countries.get(city.country_id)
        if country is None or not country.alive:
            continue

        if city.satisfaction < config.UNREST_TRIGGER:
            depth = (config.UNREST_TRIGGER - city.satisfaction) / max(
                config.UNREST_TRIGGER, EPS)
            city.unrest = min(1.6, city.unrest + config.UNREST_GROWTH * depth)
        else:
            city.unrest = max(0.0, city.unrest - config.UNREST_DECAY)

        if city.revolt_ticks > 0:
            # бунт уже идёт: стихает, только если область успокоилась
            if city.unrest < config.UNREST_CALM_AT:
                city.revolt_ticks = 0
                news.append(f"{city.name}: бунт подавлен, область успокоилась")
                continue
        elif city.unrest < config.UNREST_REVOLT_AT:
            continue
        else:
            news.append(f"{city.name} ({country.name}): вспыхнул бунт — "
                        f"довольство {city.satisfaction:.0%}")

        city.revolt_ticks += 1
        _riot(world, city, country)

        if city.revolt_ticks >= config.CIVIL_WAR_AFTER:
            name = _secede(world, city, country)
            if name:
                news.append(f"Гражданская война: {city.name} отделяется от "
                            f"{country.name} и провозглашает государство {name}")
    return news


def _riot(world: World, city, country: Country) -> None:
    """Один пейдей бунта: погромы, жертвы и расходы на усмирение."""
    r = society.rng_for(world, city.id, salt=131)
    for key in config.STRATA_ORDER:
        st = city.s(key)
        if st.people <= EPS:
            continue
        gone = st.people * config.REVOLT_CASUALTIES
        st.people = max(0.0, st.people - gone)
    for b in world.city_buildings(city.id):
        if b.effective_level > 0 and r.random() < config.REVOLT_BUILDING_DAMAGE:
            b.damage = min(b.level, b.damage + 1)
    country.treasury -= max(0.0, country.treasury) * config.REVOLT_TREASURY_LOSS


def _secede(world: World, city, country: Country) -> str | None:
    """Область откалывается и становится самостоятельным государством."""
    from ..models import Country as CountryModel

    if len(world.country_regions(country.id)) <= 1:
        return None                 # отделяться от самого себя некуда

    new_id = world.next_country_id
    world.next_country_id += 1
    name = f"Свободная {city.name}"
    rebel = CountryModel(
        id=new_id, name=name, capital_city_id=city.id,
        treasury=max(0.0, country.treasury) * 0.15,
        corporate_tax=country.corporate_tax, sales_tax=country.sales_tax,
        income_tax=country.income_tax,
        public_spending_rate=country.public_spending_rate,
        min_wage=country.min_wage, land_rent=country.land_rent,
        leader_id=None, color="#c1121f",
        soldier_pay=country.soldier_pay,
        bankruptcy_limit=country.bankruptcy_limit,
    )
    country.treasury -= rebel.treasury
    world.countries[new_id] = rebel
    city.country_id = new_id
    city.unrest = 0.0
    city.revolt_ticks = 0

    # своя казна новому государству
    from ..auth import hash_password
    salt, pw = hash_password("__no-login__")
    state = Player(id=world.next_player_id, username=f"Казна:{name[:12]}",
                   password_hash=pw, salt=salt, is_state=True, country_id=new_id)
    world.players[state.id] = state
    world.next_player_id += 1

    # часть армии переходит к восставшим
    defect = society.army_size(world, country) * config.CIVIL_WAR_ARMY_DEFECT
    rebel.army_budget = country.army_budget * config.CIVIL_WAR_ARMY_DEFECT
    country.army_budget -= rebel.army_budget
    rebel.army_shells = country.army_shells * config.CIVIL_WAR_ARMY_DEFECT
    country.army_shells -= rebel.army_shells
    # Перебежчики уходят со своим оружием — арсенал делится вместе с людьми.
    rebel.army_weapons = country.army_weapons * config.CIVIL_WAR_ARMY_DEFECT
    country.army_weapons -= rebel.army_weapons
    if defect > EPS:
        soldiers = city.s("soldiers")
        soldiers.people += defect
        for other in world.country_regions(country.id):
            share = other.s("soldiers").people
            if share > EPS:
                other.s("soldiers").people = max(
                    0.0, share * (1.0 - config.CIVIL_WAR_ARMY_DEFECT))

    if country.capital_city_id == city.id:
        left = world.country_regions(country.id)
        country.capital_city_id = left[0].id if left else 0

    # и сразу война за независимость
    declare_war(world, country.id, new_id)
    return name


# ---------------------------------------------------------------------------
# Банкротство
# ---------------------------------------------------------------------------
def update_bankruptcy(world: World, country: Country) -> list[str]:
    """Пересчитать, кто из промышленников страны разорился, а кто выбрался.

    Порог задаёт само государство. Выход из банкротства требует подняться
    заметно выше порога — иначе дело мигало бы «банкрот / не банкрот» каждый
    пейдей, дёргая занятость всего города.
    """
    news: list[str] = []
    limit = country.bankruptcy_limit
    # Касса сама по себе порог не пробьёт: закупки ужимаются до остатка, и она
    # лишь подползает к нему сверху. Поэтому банкротство объявляем, когда
    # кредита почти не осталось.
    trigger = limit + abs(limit) * config.BANKRUPTCY_TRIGGER_MARGIN
    exit_at = limit * (1.0 - config.BANKRUPTCY_EXIT_MARGIN)
    for p in world.players.values():
        if p.is_state or p.country_id != country.id:
            continue
        if not p.bankrupt and p.cash <= trigger:
            p.bankrupt = True
            p.bankrupt_since = world.tick
            news.append(f"{p.username} разорился: дело встало "
                        f"({p.cash:,.0f} ₡ при пороге {limit:,.0f} ₡)")
        elif p.bankrupt and p.cash > exit_at:
            p.bankrupt = False
            news.append(f"{p.username} рассчитался с долгами и снова в деле")
    return news


# ---------------------------------------------------------------------------
# Дотация казённого сектора одного государства
# ---------------------------------------------------------------------------
def support_state_sector(world: World, country: Country) -> float:
    state = world.state_player(country.id)
    if state is None or country.treasury <= EPS:
        return 0.0

    need = 0.0
    for b in world.buildings.values():
        if b.owner_id != state.id or not b.active:
            continue
        city = world.cities.get(b.city_id)
        if city is None or city.country_id != country.id:
            continue
        ind = world.industries[b.industry_key]
        lv = b.effective_level
        jobs = lv * ind.jobs_per_level * max(0.0, min(1.0, b.throttle))
        planned = jobs * ind.output_per_worker
        # сырьё считаем по ценам ТОЙ области, где стоит цех
        need += (jobs * max(b.wage, country.min_wage)
                 + lv * config.UPKEEP_PER_LEVEL
                 + sum(planned * r * society.lg(city, g).price
                       for g, r in ind.inputs.items())
                 + sum(lv * q * society.lg(city, g).price
                       for g, q in ind.upkeep_goods.items()))

    gap = need - state.cash
    if gap <= 0:
        return 0.0
    grant = min(gap, country.treasury * config.SUBSIDY_LIMIT)
    state.cash += grant
    country.treasury -= grant
    return grant


# ---------------------------------------------------------------------------
# Сводка рынка страны: среднее по её областям
# ---------------------------------------------------------------------------
def country_quote(world: World, country: Country, key: str) -> dict:
    """Как товар выглядит «в среднем по стране».

    Рынка у страны нет, но для витрин и мировой торговли нужен один
    представительный набор чисел. Цена берётся средневзвешенной по складам
    (а где складов нет — по числу жителей), склад и спрос просто складываются.
    """
    price_num = price_den = 0.0
    pop_num = pop_den = 0.0
    stock = demand = supply = anchor_num = cost_num = 0.0
    regions = world.country_regions(country.id)
    for city in regions:
        local = city.goods.get(key)
        if local is None:
            continue
        pop = city.population
        stock += local.stock
        demand += local.last_demand
        supply += local.last_supply
        price_num += local.price * local.stock
        price_den += local.stock
        pop_num += local.price * pop
        pop_den += pop
        anchor_num += local.anchor * pop
        cost_num += local.unit_cost * pop
    if price_den > EPS:
        price = price_num / price_den
    elif pop_den > EPS:
        price = pop_num / pop_den
    else:
        price = world.goods[key].anchor if key in world.goods else 0.0
    return {
        "price": price,
        "anchor": anchor_num / pop_den if pop_den > EPS else price,
        "unit_cost": cost_num / pop_den if pop_den > EPS else price,
        "stock": stock, "demand": demand, "supply": supply,
        "regions": len(regions),
    }


def _empty_country_result(country: Country) -> dict:
    """Итоги пейдея государства, у которого не осталось областей."""
    country.gdp = 0.0
    return {
        "markets": {}, "input_demand": {}, "consumer_demand": {},
        "production": {}, "prod_cost": {}, "consumer_spend": 0.0,
        "total_wages": 0.0, "subsidy": 0.0, "employed_total": 0.0,
        "total_workers": 0.0, "pop": 0.0, "village": 0.0,
        "artisan_spend": 0.0, "public_spend": 0.0, "value_added": 0.0,
        "army_cost": 0.0, "mobilized": 0.0, "bankruptcies": [],
        "living_standard": 1.0,
    }


# ---------------------------------------------------------------------------
# Экономический цикл одного государства
# ---------------------------------------------------------------------------
def run_country(world: World, country: Country) -> dict:
    """Один пейдей экономики одного государства.

    Хозяйство считается ПО ОБЛАСТЯМ: у каждой свой рынок, свои цены, свой
    склад и своя рабочая сила. Общими на страну остаются только казна, налоги,
    политика и армия. Поэтому почти всё, что раньше было одним словарём на
    страну, стало словарём по областям.
    """
    inds = world.industries
    cities = world.country_regions(country.id)
    if not cities:
        return _empty_country_result(country)

    for city in cities:
        for st in city.strata.values():
            st.income = 0.0
        society.update_service_cost(city)

    markets = {c.id: RegionMarket(world, country, c) for c in cities}
    price0 = {c.id: {k: society.lg(c, k).price for k in world.goods} for c in cities}

    # --- Фаза 0: армия -----------------------------------------------------
    # Жалованье солдатам — первая статья бюджета, до всех прочих расходов:
    # неоплаченная армия разбегается и воевать становится некому. Призыв и
    # мобилизация идут до найма, чтобы забранные приказом люди не успели
    # выйти на смену.
    society.pay_army(world, country)
    society.conscript(world, country)
    mobilized = society.mobilize(world, country)
    if country.mobilization_left > 0:
        country.mobilization_left -= 1
    # Мирный расход боеприпасов: учения, порча, разворованное. Армия жжёт
    # снаряды всегда, а не только на войне, — иначе снарядные заводы стоят без
    # покупателя, а военные товары вечно валяются по бросовой цене.
    country.army_shells = max(0.0, country.army_shells
                              - society.army_size(world, country)
                              * config.SHELLS_PEACETIME_BURN)
    # То же и с оружием: оно ломается и теряется в мирное время. Списываем до
    # закупки, чтобы спрос этого пейдея уже видел образовавшуюся дыру.
    country.last_weapons_bought = 0.0
    society.wear_weapons(world, country)

    # --- Фаза 1–2: наём и распределение рабочих ----------------------------
    employed_by_city: dict[int, float] = {}
    for city in cities:
        society.recruit_workers(world, country, city)
        employed_by_city[city.id] = allocate_labor(world, country, city.id)

    # Бунтующая область почти не работает: часть цехов стоит, часть разграблена.
    morale = {
        city.id: max(0.6, min(1.3, 1.0 + config.MORALE_PRODUCTIVITY
                              * (city.satisfaction - 0.65) / 0.35))
        * (config.REVOLT_OUTPUT_PENALTY if city.revolt_ticks > 0 else 1.0)
        for city in cities
    }

    # --- Фаза 3: деревня и услуги ------------------------------------------
    own_food: dict[int, float] = {}
    craft_plans: dict[int, dict] = {}
    input_demand: dict[int, dict[str, float]] = {c.id: defaultdict(float)
                                                 for c in cities}

    crafts = society.artisan_crafts(world)
    for city in cities:
        m = markets[city.id]
        own_food[city.id] = society.produce_peasants(world, country, city, m)
        society.produce_services(world, country, city, m)

        artisans = city.s("artisans")
        mix = society.artisan_mix(world, country, city, artisans.craft_mix)
        artisans.craft_mix = mix
        plan = society.plan_artisans(world, country, city, mix, crafts)
        craft_plans[city.id] = plan
        for item in plan.values():
            for g, q in item["inputs"].items():
                input_demand[city.id][g] += q

    # --- Фаза 4: предприятия --------------------------------------------
    subsidy = support_state_sector(world, country)
    plans: list[tuple] = []

    for b in world.buildings.values():
        city = world.cities.get(b.city_id)
        if city is None or city.country_id != country.id:
            continue
        b.last_output = b.last_revenue = b.last_costs = 0.0
        b.last_inputs = b.last_wages = b.last_profit = 0.0
        if not b.active or b.employed <= EPS:
            continue
        ind = inds[b.industry_key]
        owner = world.players.get(b.owner_id)
        if owner is None:
            continue
        if owner.bankrupt:
            # Дело признано банкротом: цеха стоят, пока владелец не выберется
            # сам или пока государство не выделит субсидию.
            b.employed = 0.0
            continue

        employed = b.employed
        wage = max(b.wage, country.min_wage)
        wage_bill = employed * wage
        upkeep = b.effective_level * config.UPKEEP_PER_LEVEL

        # Ветвимся по наличию выпуска, а не по виду здания: административная
        # постройка тоже может что-то производить (оперный театр — роскошные
        # услуги), и содержание штата при этом никуда не девается.
        needs = {g: b.effective_level * q for g, q in ind.upkeep_goods.items()}
        if ind.output_good:
            planned = employed * ind.output_per_worker * morale[b.city_id]
            for g, r in ind.inputs.items():
                needs[g] = needs.get(g, 0.0) + planned * r
        else:
            planned = 0.0

        p0 = price0[city.id]
        input_cost = sum(q * p0[g] for g, q in needs.items() if g in p0)
        total_need = wage_bill + upkeep + input_cost
        # Предприятие работает в долг: зарплаты и сырьё оплачиваются и в минус,
        # пока касса не упёрлась в порог банкротства, установленный
        # государством. Казна себе в долг не влезает — её и так дотируют.
        purse = owner.cash if owner.is_state else owner.cash - country.bankruptcy_limit
        if total_need > purse + EPS and total_need > EPS:
            scale = max(0.0, purse / total_need)
            employed *= scale
            wage_bill *= scale
            upkeep *= scale
            planned *= scale
            needs = {g: q * scale for g, q in needs.items()}
            b.employed = employed

        for g, q in needs.items():
            input_demand[city.id][g] += q
        # Владельца несём с собой в плане, а не полагаемся на переменную цикла:
        # платит за цех именно его хозяин, кто бы ни шёл следующим по списку.
        plans.append((b, owner, employed, wage_bill, upkeep, needs, planned))

    # Рацион сырья считается в каждой области отдельно: склад соседней области
    # здешнему цеху недоступен, пока товар не привезли торговые площади.
    ration = {c.id: {g: (1.0 if want <= EPS
                         else min(1.0, markets[c.id].available(g) / want))
                     for g, want in input_demand[c.id].items()}
              for c in cities}

    artisan_spend = 0.0
    for city in cities:
        artisan_spend += society.execute_artisans(
            city, city, markets[city.id], craft_plans[city.id], ration[city.id])

    production: dict[int, dict[str, float]] = {c.id: defaultdict(float) for c in cities}
    prod_cost: dict[int, dict[str, float]] = {c.id: defaultdict(float) for c in cities}
    total_wages = 0.0
    value_added = 0.0
    wage_income: dict[int, float] = defaultdict(float)

    for b, owner, employed, wage_bill, upkeep, needs, planned in plans:
        ind = inds[b.industry_key]
        market = markets[b.city_id]
        p0 = price0[b.city_id]
        rat = ration[b.city_id]

        fill = 1.0
        for g, q in needs.items():
            if q > EPS:
                fill = min(fill, rat.get(g, 0.0))

        spent_inputs = 0.0
        for g, q in needs.items():
            got = market.buy(g, q * fill, p0[g])
            spent_inputs += got * p0[g]

        # Нет сырья — цех простаивает, но люди остаются на местах и получают
        # полную зарплату; выпуск режется по доле подвезённого сырья.
        output = planned * fill if ind.output_good else 0.0
        if output > EPS:
            # Услуги нельзя положить на склад: их оказывают и потребляют в тот
            # же пейдей, поэтому на прилавок они попадают сразу.
            market.deposit(ind.output_good, b.owner_id, output,
                           immediate=not world.goods[ind.output_good].storable)
            production[b.city_id][ind.output_good] += output
            prod_cost[b.city_id][ind.output_good] += spent_inputs + wage_bill + upkeep

        owner.cash -= spent_inputs + wage_bill + upkeep
        total_wages += wage_bill
        if ind.output_good:
            value_added += output * p0[ind.output_good] - spent_inputs

        b.last_output = output
        b.last_inputs = spent_inputs
        b.last_wages = wage_bill
        b.last_costs = spent_inputs + wage_bill + upkeep
        wage_income[b.city_id] += wage_bill + upkeep

    for cid, gross in wage_income.items():
        st = world.cities[cid].s("workers")
        st.income += gross * (1.0 - country.income_tax)
        st.cash += gross * (1.0 - country.income_tax)
        country.treasury += gross * country.income_tax

    # --- Фаза 5: госрасходы -----------------------------------------------
    public_spend = max(0.0, country.treasury) * country.public_spending_rate
    country.treasury -= public_spend
    pop_total = sum(c.population for c in cities) or 1.0
    for city in cities:
        for key in config.STRATA_ORDER:
            st = city.s(key)
            if st.people <= EPS:
                continue
            share = st.people / pop_total
            grant = public_spend * share
            st.cash += grant
            st.income += grant

    # --- Фаза 6: потребление ----------------------------------------------
    consumer_demand: dict[int, dict[str, float]] = {c.id: defaultdict(float)
                                                    for c in cities}
    fulfilment: dict[int, dict] = {}
    consumer_spend = 0.0
    for city in cities:
        res = society.consume(world, country, city, markets[city.id],
                              own_food[city.id])
        fulfilment[city.id] = res
        for r in res.values():
            for g, qty in r.get("plan", {}).items():
                consumer_demand[city.id][g] += qty
            consumer_spend += r["spent"]

    # Казна докупает снаряды и оружие на армейские склады — вот покупатели
    # снарядного и оружейного заводов. Заказ делится между областями по
    # населению: армию снабжают отовсюду.
    #
    # На рынок идёт ПОТРЕБЛЕНИЕ (износ плюс закрытие недостачи), а не
    # вооружённость: полный арсенал не обнуляет спрос, он опускает его до
    # износа. Сама вооружённость пересчитывается ниже, уже из арсенала.
    country.last_shells_bought = 0.0
    want_shells = society.shells_wanted(world, country)
    want_weapons = society.weapons_wanted(world, country)
    pop_all = sum(c.population for c in cities) or 1.0
    for city in cities:
        share = city.population / pop_all
        consumer_demand[city.id]["shells"] += want_shells * share
        consumer_demand[city.id]["weapons"] += want_weapons * share
        society.restock_shells(world, country, city, markets[city.id], share)
        society.restock_weapons(world, country, city, markets[city.id], share)

    # Вооружённость — доля штатного арсенала, лежащая на складах. Именно она,
    # а не численность и не покупки за этот пейдей, решает исход боёв.
    society.update_army_equip(world, country)

    for city in cities:
        country.treasury += markets[city.id].tax_collected

    # --- Фаза 7: выручка и налоги -----------------------------------------
    for city in cities:
        for owner_id, rev in markets[city.id].revenue.items():
            who = decode_owner(owner_id)
            if who is None:
                p = world.players.get(owner_id)
                if p:
                    p.cash += rev
                continue
            home_id, key = who
            home = world.cities.get(home_id)
            if home is None:
                continue
            st = home.s(key)
            if key == "peasants" and country.land_rent > 0:
                rent = rev * country.land_rent
                high = home.s("town_high")
                high.cash += rent
                high.income += rent
                rev -= rent
            st.cash += rev
            st.income += rev

    profit_by_owner: dict[int, float] = defaultdict(float)
    for b, *_ in plans:
        ind = inds[b.industry_key]
        b.last_revenue = (b.last_output * price0[b.city_id][ind.output_good]
                          if ind.output_good else 0.0)
        b.last_profit = b.last_revenue - b.last_costs
        b.loss_streak = b.loss_streak + 1 if b.last_profit < 0 else 0
        profit_by_owner[b.owner_id] += b.last_profit

    for owner_id, profit in profit_by_owner.items():
        p = world.players.get(owner_id)
        if p and not p.is_state and profit > EPS:
            tax = profit * country.corporate_tax
            p.cash -= tax
            country.treasury += tax

    # --- Фаза 9 (часть 1): общество ---------------------------------------
    # Пока идёт мобилизация, страна недовольна вся: у одних забрали сыновей,
    # у других встали заводы. Это и есть цена армии, набранной приказом — но
    # сытое сословие переносит её заметно легче голодного.
    mobilizing = country.mobilization_left > 0
    for city in cities:
        employed = employed_by_city.get(city.id, 0.0)
        for key in config.STRATA_ORDER:
            st = city.s(key)
            if st.people <= EPS:
                continue
            unemp = city.unemployment if key == "workers" else 0.0
            score = society.satisfaction_score(
                fulfilment[city.id].get(key, {}).get("fill", {}), unemp,
                st.living_standard)
            if mobilizing:
                bite = config.MOBILIZATION_DISCONTENT * (
                    1.0 - config.SOL_HARDSHIP_CUSHION
                    * society.prosperity(st.living_standard))
                score *= (1.0 - bite)
            i = config.SATISFACTION_INERTIA
            st.satisfaction = max(0.0, min(1.0, st.satisfaction * i + score * (1 - i)))
        pop = city.population or 1.0
        city.satisfaction = sum(st.satisfaction * st.people
                                for st in city.strata.values()) / pop
        society.drift_and_switch(world, city, employed)
        society.demography(world, city)

    for city in cities:
        markets[city.id].flush()
    bankruptcies = update_bankruptcy(world, country)

    # Госпредприятия отдают излишек кассы в казну государства.
    state = world.state_player(country.id)
    if state is not None and state.cash > EPS:
        country.treasury += state.cash
        state.cash = 0.0

    # --- Локальные макропоказатели ----------------------------------------
    total_workers = sum(c.s("workers").people for c in cities)
    employed_total = sum(employed_by_city.values())
    village = sum(rev for m in markets.values()
                  for oid, rev in m.revenue.items() if oid < 0)
    country.gdp = value_added + village - artisan_spend + public_spend
    pop = sum(c.population for c in cities)
    country.industrialisation = total_workers / pop if pop > EPS else 0.0

    return {
        "markets": markets,
        "input_demand": input_demand,
        "consumer_demand": consumer_demand,
        "production": production,
        "prod_cost": prod_cost,
        "consumer_spend": consumer_spend,
        "total_wages": total_wages,
        "subsidy": subsidy,
        "employed_total": employed_total,
        "total_workers": total_workers,
        "pop": pop,
        "village": village,
        "artisan_spend": artisan_spend,
        "public_spend": public_spend,
        "value_added": value_added,
        "army_cost": country.last_army_cost,
        "mobilized": mobilized,
        "bankruptcies": bankruptcies,
        "living_standard": society.country_living_standard(world, country),
    }


def _update_local_prices(world: World, country: Country, res: dict) -> None:
    """Фаза 9 (часть 2): пересчёт цен — в каждой области своих."""
    wage_ref = society.reference_wage(world, country)
    for city in world.country_regions(country.id):
        market = res["markets"].get(city.id)
        if market is None:
            continue
        _update_region_prices(world, city, market, wage_ref,
                              res["production"].get(city.id, {}),
                              res["prod_cost"].get(city.id, {}),
                              res["input_demand"].get(city.id, {}),
                              res["consumer_demand"].get(city.id, {}))


def _sane_unit_cost(actual: float, previous: float, notional: float | None) -> float:
    """Себестоимость по факту, но с оглядкой на рецепт.

    Цех, которому не подвезли сырьё, платит зарплату полностью, а выпускает
    почти ничего. Делить одно на другое буквально нельзя: при выпуске в
    сотые доли единицы «себестоимость» уходит в миллионы, за ней ползёт
    якорь, за якорем цена — и область улетает в гиперинфляцию на ровном
    месте. Рецептурная оценка от цен на сырьё и труд никуда не убегает и
    служит здесь потолком здравого смысла.
    """
    if notional is None or notional <= EPS:
        return actual
    return min(actual, notional * config.UNIT_COST_SANITY)


def _update_region_prices(world: World, city, market, wage_ref: float,
                          production: dict, prod_cost: dict,
                          input_demand: dict, consumer_demand: dict) -> None:
    total_demand: dict[str, float] = defaultdict(float)
    for src in (input_demand, consumer_demand):
        for g, q in src.items():
            total_demand[g] += q

    for key, good in world.goods.items():
        local = society.lg(city, key)
        notional = society.notional_unit_cost(world, city, key, wage_ref)
        if not good.storable:
            supply = market.produced.get(key, 0.0)
        else:
            # предложение = выпуск этого пейдея + остаток на складе
            out_amount = production.get(key, 0.0)
            supply = out_amount + market.available(key)
            if out_amount > EPS:
                local.unit_cost = _sane_unit_cost(prod_cost[key] / out_amount,
                                                  local.unit_cost, notional)
            elif notional is not None:
                local.unit_cost = local.unit_cost * 0.7 + notional * 0.3

        local.anchor = update_anchor(local.anchor, local.unit_cost)
        demand = total_demand.get(key, 0.0)
        local.price = next_price(local.price, local.anchor, demand, supply,
                                 market.total(key) if good.storable else supply)
        local.last_demand = demand
        local.last_supply = supply
        local.last_sold = min(demand, supply)
        local.last_shortage = 0.0 if demand <= EPS else max(
            0.0, 1.0 - min(1.0, supply / demand))


# ---------------------------------------------------------------------------
# Мировой рынок: арбитраж между государствами
# ---------------------------------------------------------------------------
def region_trade_capacity(world: World) -> dict[int, float]:
    """Какую долю своих запасов ОБЛАСТЬ способна вывезти за пейдей.

    Обозы ходили и до всякой биржи, поэтому у каждой области есть базовая
    пропускная способность: торговля работает всегда, просто вяло. «Торговые
    площади» её увеличивают — в этом и весь их смысл, а не в том, чтобы
    открыть границу с нуля.
    """
    levels: dict[int, int] = defaultdict(int)
    for b in world.buildings.values():
        if b.industry_key == "market":
            levels[b.city_id] += b.effective_level
    caps: dict[int, float] = {}
    for city in world.cities.values():
        caps[city.id] = min(
            config.TRADE_SHARE_CAP,
            config.TRADE_SHARE_BASE
            + levels[city.id] * config.WORLD_MARKET_SHARE_PER_LEVEL)
    return caps


def trade_capacity(world: World) -> dict[int, float]:
    """Пропускная способность государства — лучшая из его областей."""
    caps = region_trade_capacity(world)
    out: dict[int, float] = {}
    for country in world.countries.values():
        regions = world.country_regions(country.id)
        out[country.id] = max((caps.get(c.id, 0.0) for c in regions), default=0.0)
    return out


# ---------------------------------------------------------------------------
# Обмен между областями одной страны
# ---------------------------------------------------------------------------
def domestic_trade_step(world: World) -> None:
    """Развезти товар внутри страны из дешёвых областей в дорогие.

    Это и есть работа торговых площадей: рынок каждой области сам по себе, но
    обозы выравнивают цены между ними. Казна выкупает излишек там, где дёшево,
    и выкладывает его на прилавок там, где дорого, — а разницу оставляет себе
    как выручку купца. Товар и деньги при этом сходятся в ноль.
    """
    caps = region_trade_capacity(world)
    for country in world.countries.values():
        if not country.alive:
            continue
        regions = world.country_regions(country.id)
        if len(regions) < 2:
            continue                    # развозить некуда
        state = world.state_player(country.id)
        if state is None:
            continue

        for key, good in world.goods.items():
            if not good.storable:
                continue                # услуги не возят
            quotes = [(c, c.goods.get(key)) for c in regions]
            quotes = [(c, g) for c, g in quotes if g is not None]
            if len(quotes) < 2:
                continue
            mean = sum(g.price for _, g in quotes) / len(quotes)
            if mean <= EPS:
                continue

            sellers = [(c, g) for c, g in quotes
                       if g.price < mean * (1.0 - config.DOMESTIC_TRADE_GAP)
                       and g.stock > EPS]
            buyers = [(c, g) for c, g in quotes
                      if g.price > mean * (1.0 + config.DOMESTIC_TRADE_GAP)]
            if not sellers or not buyers:
                continue

            offered = sum(g.stock * caps.get(c.id, 0.0) for c, g in sellers)
            wanted = sum(max(0.0, g.last_demand - g.stock) * caps.get(c.id, 0.0)
                         for c, g in buyers)
            traded = min(offered, wanted)
            if traded <= EPS:
                continue

            # выкупаем у дешёвых областей
            bought = 0.0
            paid = 0.0
            for c, g in sellers:
                share = g.stock * caps.get(c.id, 0.0) / offered
                qty = traded * share
                got = _ship_out(world, c, key, qty, g.price)
                bought += got
                paid += got * g.price
                g.stock = max(0.0, g.stock - got)
            if bought <= EPS:
                continue
            country.treasury -= paid

            # и выкладываем в дорогих
            for c, g in buyers:
                want = max(0.0, g.last_demand - g.stock) * caps.get(c.id, 0.0)
                share = want / wanted if wanted > EPS else 0.0
                qty = bought * share
                if qty <= EPS:
                    continue
                store = state.store(c.id)
                store[key] = store.get(key, 0.0) + qty
                g.stock += qty


def world_market_step(world: World) -> None:
    """Мировой рынок как клиринг между ОБЛАСТЯМИ разных стран.

    Сорок областей и составляют весь мир, поэтому вывезенный товар обязан быть
    кем-то куплен: внешнего покупателя из ниоткуда здесь нет. Товар физически
    уходит со складов продавцов на склад казны покупателя, деньги идут
    навстречу, пошлина оседает в казне вывозящей страны. Итог: и товар, и
    деньги сходятся в ноль по миру.

    Торгуют именно области, а не страны: у каждой свои цены, и вывозить
    выгодно оттуда, где дёшево, — даже если в соседней области той же страны
    дорого. Пропускная способность есть у всех и без торговых площадей, те
    лишь увеличивают её.
    """
    caps = region_trade_capacity(world)
    world.last_trades = {}
    tariff = config.WORLD_TRADE_TARIFF

    for key, good in world.goods.items():
        if not good.storable:
            continue                    # услуги через границу не возят
        wp = world.world_prices.get(key, good.anchor)
        seller_gets = wp * (1.0 - tariff)

        # --- кто и сколько готов вывезти -------------------------------
        sellers: list[tuple] = []
        for city in world.cities.values():
            country = world.countries.get(city.country_id)
            local = city.goods.get(key)
            cap = caps.get(city.id, 0.0)
            if country is None or not country.alive or local is None:
                continue
            if cap <= EPS or local.stock <= EPS:
                continue
            if seller_gets > local.price:          # дома дешевле — вывозим
                sellers.append((city, country, local, local.stock * cap))
        supply = sum(s[3] for s in sellers)
        if supply <= EPS:
            world.world_prices[key] = _blend_world_price(world, key, wp)
            continue

        # --- кто и сколько готов ввезти --------------------------------
        buyers: list[tuple] = []
        for city in world.cities.values():
            country = world.countries.get(city.country_id)
            local = city.goods.get(key)
            cap = caps.get(city.id, 0.0)
            if country is None or not country.alive or local is None or cap <= EPS:
                continue
            if local.price > wp * (1.0 + 0.02):    # дома дороже — выгодно ввезти
                gap = max(0.0, local.last_demand - local.stock)
                want = min(gap, max(local.last_demand, local.stock) * cap)
                if want > EPS and country.treasury > want * wp:
                    buyers.append((city, country, local, want))
        demand = sum(b[3] for b in buyers)
        traded = min(supply, demand)
        if traded <= EPS:
            world.world_prices[key] = _blend_world_price(world, key, wp)
            continue

        # --- сделка: товар в одну сторону, деньги в другую --------------
        exports, imports = [], []
        for city, country, local, offer in sellers:
            qty = traded * offer / supply
            shipped = _ship_out(world, city, key, qty, seller_gets)
            if shipped > EPS:
                country.treasury += shipped * wp * tariff   # вывозная пошлина
                local.stock = max(0.0, local.stock - shipped)
                exports.append({"country_id": country.id, "name": country.name,
                                "region_id": city.id, "region": city.name,
                                "qty": round(shipped, 1),
                                "local_price": round(local.price, 2),
                                "duty": round(shipped * wp * tariff, 2)})

        for city, country, local, want in buyers:
            qty = traded * want / demand
            cost = qty * wp
            if qty <= EPS or country.treasury < cost:
                continue
            country.treasury -= cost
            _ship_in(world, country, city, key, qty)
            local.stock += qty
            imports.append({"country_id": country.id, "name": country.name,
                            "region_id": city.id, "region": city.name,
                            "qty": round(qty, 1),
                            "local_price": round(local.price, 2),
                            "paid": round(cost, 2)})

        # Мировая цена идёт за дисбалансом: избыток предложения её опускает,
        # избыток спроса поднимает.
        imbalance = (demand - supply) / (demand + supply)
        wp *= (1.0 + config.WORLD_PRICE_ELASTICITY * imbalance)
        world.world_prices[key] = _blend_world_price(world, key, wp)
        # Сводка сделки — её показывает биржа мирового рынка.
        world.last_trades[key] = {
            "volume": round(traded, 1),
            "world_price": round(world.world_prices[key], 2),
            "offered": round(supply, 1),
            "wanted": round(demand, 1),
            "exports": exports,
            "imports": imports,
        }


def _blend_world_price(world: World, key: str, wp: float) -> float:
    """Подтянуть мировую цену к средневзвешенной по складам всех областей."""
    num = den = 0.0
    for city in world.cities.values():
        country = world.countries.get(city.country_id)
        local = city.goods.get(key)
        if country is None or not country.alive or local is None:
            continue
        if local.stock <= EPS:
            continue
        num += local.price * local.stock
        den += local.stock
    if den <= EPS:
        return max(wp, 1e-6)
    target = num / den
    s = config.WORLD_PRICE_SMOOTHING
    return max(wp * (1 - s) + target * s, 1e-6)


def _stock_holders(world: World, city, key: str) -> list:
    """Кто держит товар на рынке этой ОБЛАСТИ: её сословия и игроки.

    Возвращает список (кошелёк, склад, количество) — единый интерфейс для
    деревни, горожан и предприятий.
    """
    holders = []
    for stratum_key in config.STRATA_ORDER:
        st = city.s(stratum_key)
        qty = st.warehouse.get(key, 0.0)
        if qty > EPS:
            holders.append((st, st.warehouse, qty))
    for p in world.players.values():
        store = p.warehouses.get(city.id)
        if not store:
            continue
        qty = store.get(key, 0.0)
        if qty > EPS:
            holders.append((p, store, qty))
    return holders


def _ship_out(world: World, city, key: str, qty: float,
              unit_revenue: float) -> float:
    """Вывезти товар из области: снять со складов владельцев и заплатить им.

    Именно со складов, а не с агрегата city.goods[key].stock. Агрегат —
    производная величина, он пересчитывается из складов в конце пейдея,
    поэтому «списанный» из него товар возвращался бы на место, а деньги за
    него оставались бы у продавца. Так из воздуха печатались бы деньги при
    каждой сделке.
    """
    holders = _stock_holders(world, city, key)
    total = sum(h[2] for h in holders)
    if total <= EPS or qty <= EPS:
        return 0.0
    shipped = min(qty, total)
    frac = shipped / total
    for owner, store, have in holders:
        part = have * frac
        left = have - part
        if left > EPS:
            store[key] = left
        else:
            store.pop(key, None)
        payout = part * unit_revenue
        owner.cash += payout
        if hasattr(owner, "income"):        # сословие — доход учитывается
            owner.income += payout
    return shipped


def _ship_in(world: World, country: Country, city, key: str, qty: float) -> None:
    """Ввезти товар: он ложится на склад казны в этой области.

    Дальше казна продаёт его на здешнем рынке как обычный товар.
    """
    state = world.state_player(country.id)
    if state is None or qty <= EPS:
        return
    store = state.store(city.id)
    store[key] = store.get(key, 0.0) + qty


# ---------------------------------------------------------------------------
# Война
# ---------------------------------------------------------------------------
def declare_war(world: World, attacker_id: int, defender_id: int) -> War:
    """Объявить войну соседу. Союзники обеих сторон втягиваются немедленно."""
    war = War(id=world.next_war_id, attackers=[attacker_id],
              defenders=[defender_id], started_tick=world.tick)
    world.next_war_id += 1

    # Союзники идут за своим — но только те, кто не связан с обеими сторонами
    # сразу: в таком случае союзник остаётся в стороне.
    for ally in world.allies_of(attacker_id):
        c = world.countries.get(ally)
        if c and c.alive and ally != defender_id and not world.allied(ally, defender_id):
            war.attackers.append(ally)
    for ally in world.allies_of(defender_id):
        c = world.countries.get(ally)
        if (c and c.alive and ally != attacker_id
                and ally not in war.attackers and not world.allied(ally, attacker_id)):
            war.defenders.append(ally)

    world.wars[war.id] = war
    return war


def make_peace(world: World, war: War, a: int, b: int) -> None:
    """Сепаратный мир — именно между этой парой, а не выход всех из войны.

    Мир заключают двое, поэтому и записывается он парой. Союзник, которому
    война надоела, перестаёт воевать со своим противником, но его товарищи
    продолжают: фронт между ними никуда не делся. Из войны страна выпадает
    только тогда, когда помирилась со всеми своими противниками разом.
    """
    key = war.pair_key(a, b)
    if key not in war.peace:
        war.peace.append(key)
    # фронт этой пары закрыт: накопленная оккупация сгорает
    war.occupation.pop(f"{a}>{b}", None)
    war.occupation.pop(f"{b}>{a}", None)

    for cid in (a, b):
        if war.enemies_of(cid):
            continue            # ещё есть с кем воевать — остаётся в войне
        if cid in war.attackers:
            war.attackers.remove(cid)
        if cid in war.defenders:
            war.defenders.remove(cid)

    # Война кончилась, если сторона опустела или воевать больше некому.
    if not war.attackers or not war.defenders or not any(
            war.enemies_of(c) for c in war.participants()):
        war.ended = True
        war.ended_tick = world.tick


def _battle(world: World, war: War, region_a: int, region_d: int) -> None:
    """Один пейдей боёв на фронте между двумя ОБЛАСТЯМИ.

    Фронт проходит не между странами, а между конкретными соседними
    областями: воюют армии стран, но разоряются именно те области, где идут
    бои, и оккупация копится на конкретную область — её и займут.
    """
    ra, rd = world.cities[region_a], world.cities[region_d]
    ca, cd = world.countries[ra.country_id], world.countries[rd.country_id]
    sa = society.army_strength(world, ca)
    sd = society.army_strength(world, cd)
    if sa <= EPS and sd <= EPS:
        return                      # воевать некому — фронт стоит

    # Силы страны делятся между её фронтами: на два направления сразу
    # армии хватает хуже, чем на одно.
    sa /= max(1, _front_count(world, war, ca.id))
    sd /= max(1, _front_count(world, war, cd.id))

    r = society.rng_for(world, region_a * 1000 + region_d, salt=91)
    luck = config.BATTLE_LUCK
    fa = sa * (1.0 + r.uniform(-luck, luck))
    fd = sd * (1.0 + r.uniform(-luck, luck))
    total = fa + fd
    if total <= EPS:
        return

    # Расход снарядов и потери оружия: стреляют оба, независимо от исхода.
    spent_a = _burn_shells(world, ca)
    spent_d = _burn_shells(world, cd)
    _lose_weapons(world, ca)
    _lose_weapons(world, cd)

    # При равных силах каждый теряет BATTLE_LOSS_RATE своей армии; перевес
    # противника увеличивает потери, свой — уменьшает.
    loss_a = min(0.60, config.BATTLE_LOSS_RATE * 2.0 * fd / total)
    loss_d = min(0.60, config.BATTLE_LOSS_RATE * 2.0 * fa / total)
    dead_a = _kill_soldiers(world, ca, loss_a)
    dead_d = _kill_soldiers(world, cd, loss_d)

    edge = fa / total - 0.5         # -0.5 … +0.5, перевес нападающего
    ruin_d = _ravage(world, rd, max(0.0, 0.5 + edge), r)
    ruin_a = _ravage(world, ra, max(0.0, 0.5 - edge), r)

    adv = edge * 2.0                # -1 … +1
    _bump_occupation(war, ca.id, region_d, adv * config.OCCUPATION_SPEED)
    _bump_occupation(war, cd.id, region_a, -adv * config.OCCUPATION_SPEED)

    war.last_report.append({
        "attacker": ca.id, "attacker_name": ca.name,
        "defender": cd.id, "defender_name": cd.name,
        "region_attacker": ra.name, "region_defender": rd.name,
        "strength_attacker": round(sa), "strength_defender": round(sd),
        "edge": round(adv, 3),
        "losses_attacker": round(dead_a), "losses_defender": round(dead_d),
        "shells_attacker": round(spent_a), "shells_defender": round(spent_d),
        "civilians_attacker": round(ruin_a["civilians"]),
        "civilians_defender": round(ruin_d["civilians"]),
        "buildings_attacker": ruin_a["buildings"],
        "buildings_defender": ruin_d["buildings"],
        "occupation": round(war.occupation.get(f"{ca.id}>{region_d}", 0.0), 3),
        "counter_occupation": round(war.occupation.get(f"{cd.id}>{region_a}", 0.0), 3),
    })


def _front_count(world: World, war: War, country_id: int) -> int:
    """Сколько областей страны сейчас под боями в этой войне."""
    n = 0
    for enemy in war.enemies_of(country_id):
        n += len(world.border_regions(country_id, enemy))
    return n


def _bump_occupation(war: War, country_id: int, region_id: int, delta: float) -> None:
    """Накопить перевес страны над конкретной чужой областью."""
    key = f"{country_id}>{region_id}"
    war.occupation[key] = max(0.0, min(1.2, war.occupation.get(key, 0.0) + delta))


def _burn_shells(world: World, country: Country) -> float:
    need = society.army_size(world, country) * config.SHELLS_PER_SOLDIER_BATTLE
    spent = min(need, country.army_shells)
    country.army_shells = max(0.0, country.army_shells - spent)
    return spent


def _lose_weapons(world: World, country: Country) -> float:
    """Оружие, разбитое и брошенное в бою. Воевать — дорого и по арсеналу."""
    lost = min(society.army_size(world, country) * config.WEAPONS_BATTLE_LOSS,
               country.army_weapons)
    country.army_weapons = max(0.0, country.army_weapons - lost)
    society.update_army_equip(world, country)
    return lost


def _kill_soldiers(world: World, country: Country, share: float) -> float:
    """Убыль армии. Деньги погибших пропадают вместе с ними."""
    if share <= EPS:
        return 0.0
    dead = 0.0
    for city in society.country_cities(world, country):
        st = city.s("soldiers")
        if st.people <= EPS:
            continue
        gone = st.people * share
        st.cash = max(0.0, st.cash * (1.0 - share))
        st.people = max(0.0, st.people - gone)
        dead += gone
    return dead


def _ravage(world: World, city, intensity: float, r) -> dict:
    """Разорение ОБЛАСТИ, где идут бои: люди, казна и промышленность.

    Достаётся именно прифронтовой области, а не всей стране: тыл живёт своей
    жизнью, а под обстрелом гибнут те, кто оказался на линии огня.

    Уровни предприятий не сносятся насовсем, а выводятся из строя: считаются
    разрушенными, пока владелец не заплатит за починку. Так война бьёт по
    экономике, но не стирает вложенный труд игрока безвозвратно.
    """
    out = {"civilians": 0.0, "buildings": 0, "treasury": 0.0}
    if intensity <= EPS:
        return out

    loss = config.WAR_CIVILIAN_LOSS * intensity
    for key in config.STRATA_ORDER:
        if key == "soldiers":
            continue                # армия гибнет в бою, а не под обстрелом
        st = city.s(key)
        if st.people <= EPS:
            continue
        gone = st.people * loss
        st.people = max(0.0, st.people - gone)
        st.cash = max(0.0, st.cash * (1.0 - loss))
        out["civilians"] += gone

    for b in world.city_buildings(city.id):
        if b.effective_level <= 0:
            continue
        if r.random() < config.WAR_BUILDING_DAMAGE_CHANCE * intensity:
            b.damage = min(b.level, b.damage + 1)
            out["buildings"] += 1

    country = world.countries.get(city.country_id)
    if country is not None:
        damage = max(0.0, country.treasury) * config.WAR_TREASURY_DAMAGE * intensity
        country.treasury -= damage
        out["treasury"] = damage
    return out


def _resolve_occupation(world: World, war: War) -> list[str]:
    """Довести оккупацию до конца: полный перевес — и ОБЛАСТЬ меняет хозяина.

    Область при этом никуда не девается: она остаётся на карте, просто под
    другим флагом, и её можно отбить обратно — вплоть до последней области
    государства.
    """
    news = []
    for key, value in list(war.occupation.items()):
        if value < 1.0:
            continue
        winner_id, region_id = (int(x) for x in key.split(">"))
        winner = world.countries.get(winner_id)
        city = world.cities.get(region_id)
        if not winner or city is None:
            war.occupation.pop(key, None)
            continue
        loser = world.countries.get(city.country_id)
        if loser is None or loser.id == winner.id:
            war.occupation.pop(key, None)   # область уже наша
            continue

        _transfer_region(world, city, loser, winner)
        war.occupation[key] = 0.0
        # у бывшего хозяина накопленное на эту область сгорает
        war.occupation.pop(f"{loser.id}>{region_id}", None)
        news.append(f"{winner.name} занимает область {city.name} ({loser.name})")
        if not world.country_regions(loser.id):
            _dissolve_country(world, loser, winner)
            news.append(f"{loser.name} прекращает существование — "
                        f"все области отошли к {winner.name}")
    return news


def _transfer_region(world: World, city, loser: Country, winner: Country) -> None:
    """Передать область победителю вместе с её рынком и населением.

    Рынок области переезжает вместе с ней: цены, склады и товар остаются на
    месте — меняется только флаг. Заводы тоже не отбирают: они остаются за
    своими хозяевами, но те оказываются иностранцами в чужой теперь стране.
    Что с ними делать — оставить дело или выгнать со сносом — решает
    лидер-победитель, для чего здесь и заводится запись Annexation.
    """
    from ..models import Annexation

    city.country_id = winner.id
    if loser.capital_city_id == city.id:
        left = world.country_regions(loser.id)
        loser.capital_city_id = left[0].id if left else 0
    if winner.capital_city_id == 0 or winner.capital_city_id not in world.cities:
        winner.capital_city_id = city.id

    # чьи предприятия оказались на занятой земле
    pending = []
    for b in world.city_buildings(city.id):
        owner = world.players.get(b.owner_id)
        if owner is None or owner.is_state or owner.id in pending:
            continue
        if owner.country_id != winner.id:
            pending.append(owner.id)
    if pending:
        a = Annexation(id=world.next_annex_id, country_id=winner.id,
                       former_country_id=loser.id, city_id=city.id,
                       city_name=city.name, former_name=loser.name,
                       created_tick=world.tick, pending=pending)
        world.annexations[a.id] = a
        world.next_annex_id += 1

    # Казённый товар на этом рынке достаётся новой казне вместе с областью.
    old_state = world.state_player(loser.id)
    new_state = world.state_player(winner.id)
    if old_state is not None and new_state is not None:
        store = old_state.warehouses.pop(city.id, None)
        if store:
            dest = new_state.store(city.id)
            for k, q in store.items():
                dest[k] = dest.get(k, 0.0) + q


def _dissolve_country(world: World, loser: Country, winner: Country) -> None:
    """Государство без областей исчезает: всё его имущество отходит победителю."""
    loser.alive = False
    winner.treasury += max(0.0, loser.treasury)
    loser.treasury = 0.0
    loser.election.phase = "none"
    loser.election.votes = {}
    if loser.leader_id is not None:
        prev = world.players.get(loser.leader_id)
        if prev and prev.governor_of == loser.id:
            prev.governor_of = None
    loser.leader_id = None

    # Граждане побеждённого становятся подданными победителя. Склады трогать
    # не надо: они привязаны к областям, а области уже сменили флаг.
    for p in world.players.values():
        if p.country_id == loser.id and not p.is_state:
            p.country_id = winner.id

    # Граф соседства правится сам собой: он построен по областям, а области
    # никуда не делись — просто сменили хозяина.
    world.alliances = [a for a in world.alliances if loser.id not in a]
    for war in world.active_wars():
        if loser.id in war.attackers:
            war.attackers.remove(loser.id)
        if loser.id in war.defenders:
            war.defenders.remove(loser.id)
        if not war.attackers or not war.defenders:
            war.ended = True
            war.ended_tick = world.tick


def expel_player(world: World, annex, player_id: int) -> dict:
    """Выгнать промышленника с занятой земли: заводы под снос, деньги обратно.

    Сносится только то, что стоит в этой области — дело того же игрока в
    других странах его не касается. Возврат меньше, чем при добровольном
    сносе: имущество уходит наспех и наполовину разграбленным.
    """
    p = world.players.get(player_id)
    out = {"player": p.username if p else "—", "buildings": 0, "refund": 0.0}
    if p is None:
        return out
    winner = world.countries.get(annex.country_id)
    for b in list(world.buildings.values()):
        if b.owner_id != player_id or b.city_id != annex.city_id:
            continue
        ind = world.industries[b.industry_key]
        out["refund"] += sum(level_cost(ind.build_cost_mult, lv)
                             for lv in range(1, b.level + 1)) * config.EXPEL_REFUND
        del world.buildings[b.id]
        out["buildings"] += 1
    p.cash += out["refund"]

    # товар, оставшийся на рынке занятой области, конфискуется победителем
    if winner is not None:
        store = p.warehouses.get(winner.id)
        state = world.state_player(winner.id)
        if store and state is not None:
            dest = state.store(winner.id)
            for k, q in store.items():
                dest[k] = dest.get(k, 0.0) + q
            store.clear()
    return out


def resolve_annexation(world: World, annex, player_id: int, decision: str) -> dict:
    """Записать решение лидера по одному промышленнику и исполнить его."""
    annex.decisions[player_id] = decision
    if player_id in annex.pending:
        annex.pending.remove(player_id)
    result = {"decision": decision}
    if decision == "expel":
        result.update(expel_player(world, annex, player_id))
    else:
        p = world.players.get(player_id)
        result["player"] = p.username if p else "—"
    if not annex.pending:
        annex.resolved = True
    return result


def step_annexations(world: World) -> list[str]:
    """Довести до конца просроченные решения по занятым областям.

    Лидеру даётся ANNEX_DECISION_TICKS пейдеев. Не решил — считается, что
    промышленников оставили: захват и без того разорителен, чтобы вдобавок
    сносить работающие заводы по недосмотру.
    """
    news: list[str] = []
    for a in list(world.annexations.values()):
        if a.resolved:
            if world.tick - a.created_tick > config.ANNEX_DECISION_TICKS * 3:
                world.annexations.pop(a.id, None)   # уже история
            continue
        if world.tick - a.created_tick < config.ANNEX_DECISION_TICKS:
            continue
        default = "keep" if config.ANNEX_DEFAULT_KEEP else "expel"
        for pid in list(a.pending):
            resolve_annexation(world, a, pid, default)
        a.resolved = True
        news.append(f"{a.city_name}: срок вышел, иностранных промышленников "
                    f"{'оставили в деле' if default == 'keep' else 'выдворили'}")
    return news


def step_wars(world: World) -> list[str]:
    """Провести пейдей всех идущих войн. Возвращает новости для хроники."""
    news: list[str] = []
    for war in world.active_wars():
        war.last_report = []
        # Фронт проходит по конкретным парам смежных областей.
        fronts: list[tuple[int, int]] = []
        for a in war.attackers:
            for d in war.defenders:
                if war.at_peace(a, d):
                    continue
                if not (world.countries[a].alive and world.countries[d].alive):
                    continue
                fronts += world.border_regions(a, d)
        # Нет общей границы — «странная война»: стороны в состоянии войны,
        # но воевать физически негде.
        for ra, rd in fronts:
            _battle(world, war, ra, rd)
        news += _resolve_occupation(world, war)
        if not war.attackers or not war.defenders:
            war.ended = True
            war.ended_tick = world.tick
    return news


def step_diplomacy(world: World) -> list[str]:
    """Реакция AI-государств на предложения и уборка протухших офферов."""
    news: list[str] = []
    for offer in list(world.offers.values()):
        target = world.countries.get(offer.to_country)
        source = world.countries.get(offer.from_country)
        if target is None or source is None or not target.alive or not source.alive:
            world.offers.pop(offer.id, None)
            continue
        if world.tick - offer.created_tick > 20:
            world.offers.pop(offer.id, None)       # предложение протухло
            continue
        if target.leader_id is not None:
            continue                               # решает живой лидер

        if offer.kind == "peace":
            war = world.wars.get(offer.war_id or -1)
            if war is None or war.ended:
                world.offers.pop(offer.id, None)
                continue
            # проигрывает ли AI: копится ли у противника оккупация его областей
            losing = max((war.occupation.get(f"{offer.from_country}>{c.id}", 0.0)
                          for c in world.country_regions(offer.to_country)),
                         default=0.0)
            tired = world.tick - war.started_tick >= config.AI_PEACE_AFTER_TICKS
            if losing > 0.15 or tired:
                make_peace(world, war, offer.from_country, offer.to_country)
                world.offers.pop(offer.id, None)
                news.append(f"{target.name} заключает сепаратный мир с {source.name}")
        elif offer.kind == "alliance":
            if not world.at_war(offer.from_country, offer.to_country):
                _add_alliance(world, offer.from_country, offer.to_country)
                world.offers.pop(offer.id, None)
                news.append(f"{target.name} принимает союз с {source.name}")
    return news


def _add_alliance(world: World, a: int, b: int) -> None:
    if a != b and not world.allied(a, b):
        world.alliances.append(sorted([a, b]))


# ---------------------------------------------------------------------------
# Выборы лидеров государств
# ---------------------------------------------------------------------------
def citizen_players(world: World, country_id: int) -> list:
    """Игроки-граждане государства (казна не в счёт)."""
    return [p for p in world.players.values()
            if p.country_id == country_id and not p.is_state]


def confidence_tally(world: World, country: Country) -> dict:
    """Подсчёт вотума доверия лидеру.

    Недоверие большинства (50% + 1 от всех граждан-игроков, а не от
    проголосовавших) распускает лидера и назначает внеочередные выборы.
    Считать от проголосовавших нельзя: тогда два недовольных из двадцати
    свергали бы лидера просто потому, что остальные промолчали.
    """
    voters = citizen_players(world, country.id)
    total = len(voters)
    votes = country.election.confidence
    ids = {p.id for p in voters}
    distrust = sum(1 for pid, v in votes.items() if v == "distrust" and pid in ids)
    trust = sum(1 for pid, v in votes.items() if v == "trust" and pid in ids)
    needed = int(total * config.CONFIDENCE_MAJORITY) + 1
    return {
        "players": total, "trust": trust, "distrust": distrust,
        "needed": needed,
        "share": distrust / total if total else 0.0,
        "enough_players": total >= config.CONFIDENCE_MIN_PLAYERS,
        "triggered": (total >= config.CONFIDENCE_MIN_PLAYERS
                      and distrust >= needed),
    }


def step_confidence(world: World) -> list[str]:
    """Проверить вотумы недоверия и назначить внеочередные выборы."""
    news: list[str] = []
    for country in world.countries.values():
        if not country.alive or country.leader_id is None:
            continue
        el = country.election
        if el.phase != "none":
            continue                    # выборы и так идут
        tally = confidence_tally(world, country)
        if not tally["triggered"]:
            continue
        leader = world.players.get(country.leader_id)
        el.phase = "voting"
        el.started_tick = world.tick
        el.snap = True
        el.votes = {}
        el.confidence = {}
        if leader is not None and leader.governor_of == country.id:
            leader.governor_of = None
        country.leader_id = None
        news.append(f"{country.name}: вотум недоверия "
                    f"({tally['distrust']} из {tally['players']}) — "
                    f"лидер {leader.username if leader else '—'} смещён, "
                    f"назначены внеочередные выборы")
    return news


def step_elections(world: World) -> None:
    """Продвинуть машину выборов во всех государствах."""
    t = world.tick
    for country in world.countries.values():
        if not country.alive:
            continue
        el = country.election
        # самое первое голосование стартует на тике FIRST_ELECTION_TICKS
        first_due = (t == config.FIRST_ELECTION_TICKS)
        cycle_due = (t > config.FIRST_ELECTION_TICKS
                     and (t - config.FIRST_ELECTION_TICKS) % config.ELECTION_CYCLE_TICKS == 0)

        if el.phase == "none" and (first_due or cycle_due):
            el.phase = "voting"
            el.started_tick = t
            el.votes = {}
            continue

        if el.phase == "voting":
            duration = t - el.started_tick
            if duration >= config.ELECTION_DURATION_TICKS:
                _resolve_election(world, country)


def _resolve_election(world: World, country: Country) -> None:
    """Подвести итоги голосования и назначить лидера."""
    el = country.election
    tally: dict[int, int] = defaultdict(int)
    for voter, candidate in el.votes.items():
        tally[candidate] += 1
    el.phase = "none"
    el.votes = {}
    el.snap = False
    el.confidence = {}      # новый лидер начинает с чистого доверия

    if not tally:
        # никто не голосовал — лидер остается прежним (возможно, AI/None)
        return
    winner = max(tally, key=lambda c: (tally[c], -c))
    # снять governor_of у прежнего лидера, если он был игроком
    if country.leader_id is not None:
        prev = world.players.get(country.leader_id)
        if prev and prev.governor_of == country.id:
            prev.governor_of = None
    winner_p = world.players.get(winner)
    if winner_p:
        # снять полномочия в прежней стране, если был лидером где-то ещё
        if winner_p.governor_of is not None and winner_p.governor_of != country.id:
            old = world.countries.get(winner_p.governor_of)
            if old and old.leader_id == winner_p.id:
                old.leader_id = None
        winner_p.governor_of = country.id
        country.leader_id = winner_p.id


# ---------------------------------------------------------------------------
# Главный тик
# ---------------------------------------------------------------------------
def run_tick(world: World) -> dict:
    world.tick += 1
    world.last_tick_at = time.time()

    # экономика по каждому государству
    country_results: dict[int, dict] = {}
    for country in world.countries.values():
        if not country.alive:
            continue        # государство исчезло с карты — экономики у него нет
        country_results[country.id] = run_country(world, country)

    # обозы развозят товар внутри страны, потом мировой рынок связывает страны
    domestic_trade_step(world)
    world_market_step(world)

    # пересчёт локальных цен (после мирового рынка — он меняет склады)
    for country in world.countries.values():
        if country.id in country_results:
            _update_local_prices(world, country, country_results[country.id])

    # войны и дипломатия
    news = (step_wars(world) + step_diplomacy(world)
            + step_annexations(world) + step_unrest(world))
    for r in country_results.values():
        news += r.get("bankruptcies", [])

    # вотум недоверия может назначить внеочередные выборы — до обычных
    news += step_confidence(world)
    step_elections(world)

    # --- агрегированные макропоказатели всего мира ------------------------
    pop = world.population()
    total_workers = sum(c.s("workers").people for c in world.cities.values())
    employed_total = sum(r["employed_total"] for r in country_results.values())
    unemployment = (max(0.0, 1.0 - employed_total / total_workers)
                    if total_workers > EPS else 0.0)
    gdp = sum(r["value_added"] + r["village"] - r["artisan_spend"] + r["public_spend"]
              for r in country_results.values())
    treasury = sum(c.treasury for c in world.countries.values())
    money = (sum(st.cash for c in world.cities.values() for st in c.strata.values())
             + sum(p.cash for p in world.players.values())
             + treasury)
    cpi = compute_cpi(world)
    subsidy = sum(r["subsidy"] for r in country_results.values())

    # --- сводка по каждому государству отдельно ---------------------------
    # Нужна для истории и графиков: у двадцати областей свои цены, казна и
    # безработица, и усреднять их по миру бессмысленно.
    per_country: dict[int, dict] = {}
    for cid, country in world.countries.items():
        r = country_results.get(cid)
        if r is None:
            continue
        cities = [c for c in world.cities.values() if c.country_id == cid]
        cpop = sum(c.population for c in cities) or 1.0
        workers = sum(c.s("workers").people for c in cities)
        per_country[cid] = {
            "gdp": r["value_added"] + r["village"] - r["artisan_spend"]
            + r["public_spend"],
            "population": cpop,
            "unemployment": (max(0.0, 1.0 - r["employed_total"] / workers)
                             if workers > EPS else 0.0),
            "satisfaction": sum(c.satisfaction * c.population for c in cities) / cpop,
            "avg_wage": (sum(c.avg_wage * c.s("workers").people for c in cities)
                         / workers if workers > EPS else 0.0),
            "treasury": country.treasury,
            "cpi": country_cpi(world, country),
            "living_standard": r["living_standard"],
            "money_supply": (
                sum(st.cash for c in cities for st in c.strata.values())
                + sum(p.cash for p in world.players.values() if p.country_id == cid)
                + country.treasury),
        }

    return {
        "tick": world.tick,
        "countries": per_country,
        "news": news,
        "wars": len(world.active_wars()),
        "gdp": gdp,
        "population": pop,
        "cpi": cpi,
        "consumer_spend": sum(r["consumer_spend"] for r in country_results.values()),
        "wages": sum(r["total_wages"] for r in country_results.values()),
        "treasury": treasury,
        "money_supply": money,
        "subsidy": subsidy,
        "unemployment": unemployment,
        "industrialisation": total_workers / pop if pop > EPS else 0.0,
        "satisfaction": sum(c.satisfaction * c.population
                            for c in world.cities.values()) / max(pop, 1),
        "avg_wage": (sum(c.avg_wage * c.s("workers").people
                         for c in world.cities.values()) / total_workers
                     if total_workers > EPS else 0.0),
    }


# ---------------------------------------------------------------------------
def region_cpi(city) -> float:
    """Индекс потребительских цен одной области."""
    num = den = 0.0
    for key, spec in config.CONSUMPTION_BASKET.items():
        local = city.goods.get(key)
        if local is None:
            continue
        num += local.price * spec["qty"]
        den += local.anchor * spec["qty"]
    return num / den if den > EPS else 1.0


def country_cpi(world: World, country: Country) -> float:
    """ИПЦ государства: средний по областям, вес — население."""
    num = den = 0.0
    for city in world.country_regions(country.id):
        w = city.population
        if w <= EPS:
            continue
        num += region_cpi(city) * w
        den += w
    return num / den if den > EPS else 1.0


def compute_cpi(world: World) -> float:
    """Индекс потребительских цен всего мира, взвешенный по населению."""
    num = den = 0.0
    for city in world.cities.values():
        country = world.countries.get(city.country_id)
        if country is None or not country.alive:
            continue
        w = city.population
        if w <= EPS:
            continue
        num += region_cpi(city) * w
        den += w
    return num / den if den > EPS else 1.0
