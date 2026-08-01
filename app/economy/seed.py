"""
Начальный мир: 20 доиндустриальных государств-областей.

Каждая область — отдельное государство со столицей, казной и своим рынком
(своими ценами на товары). Экономику на старте держат крестьяне и кустари,
горожане живут торговлей и ремеслом. Госсектора и заводов почти нет — это
поле для игроков и AI-лидеров.

Государства связаны графом соседства (для карты и будущей торговли по суше).
Мировой рынок связывает все локальные рынки через арбитраж.
"""
from __future__ import annotations

import random

from .. import config
from ..auth import hash_password
from .. import models
from ..models import (
    City, Country, Good, Industry, LocalGood, MapNode, Player, Stratum, World,
)

# ---------------------------------------------------------------------------
# Товары (глобальные чертежи)
# ---------------------------------------------------------------------------
GOODS = [
    # key           название        категория       tier  якорь  хранится   порча/тик
    ("grain",       "Зерно",        "raw",          0,     2.4,  True,     config.PERISH_RATES["grain"]),
    ("wood",        "Лес",          "raw",          0,     7.0,  True,     0.0),
    ("cotton",      "Хлопок",       "raw",          0,     7.0,  True,     0.0),
    ("coal",        "Уголь",        "raw",          0,     7.9,  True,     0.0),
    ("ore",         "Руда",         "raw",          0,     9.0,  True,     0.0),
    ("sulfur",      "Сера",         "raw",          0,     9.0,  True,     0.0),
    ("boards",      "Доски",        "intermediate", 0,    30.0,  True,     0.0),
    ("cloth",       "Ткань",        "intermediate", 0,    30.0,  True,     0.0),
    ("steel",       "Сталь",        "intermediate", 0,    49.5,  True,     0.0),
    ("food",        "Еда",          "consumer",     1,    11.3,  True,     config.PERISH_RATES["food"]),
    ("services",    "Услуги",       "services",     2,    99.0,  False,    0.0),
    ("clothes",     "Одежда",       "consumer",     2,    93.0,  True,     0.0),
    ("furniture",   "Мебель",       "consumer",     2,   175.0,  True,     0.0),
    ("tools",       "Инструменты",  "consumer",     2,   188.0,  True,     0.0),
    ("electronics", "Электроника",  "consumer",     3,   344.0,  True,     0.0),
    # Военные товары. Оружие покупают сами солдаты из жалованья, снаряды —
    # казна на армейские склады.
    ("weapons",     "Оружие",       "military",     2,   120.0,  True,     0.0),
    ("shells",      "Снаряды",      "military",     2,    48.0,  True,     0.0),
    # Роскошь. В обычную корзину не входит: её начинают хотеть, только когда
    # ожидания уровня жизни дорастают до соответствующей ступени.
    ("meat",        "Мясо",         "luxury",       3,    26.0,  True,     config.PERISH_RATES["meat"]),
    ("wine",        "Вино",         "luxury",       3,    28.0,  True,     0.0),
    ("fine_clothes", "Роскошная одежда", "luxury",  3,   165.0,  True,     0.0),
    ("fine_furniture", "Роскошная мебель", "luxury", 3,  400.0,  True,     0.0),
    ("luxury_services", "Роскошные услуги", "luxury", 3,  60.0,  False,    0.0),
]

# ---------------------------------------------------------------------------
# Отрасли. Ни одна не построена — это чертежи, доступные игроку.
# ---------------------------------------------------------------------------
J = config.JOBS_PER_LEVEL
INDUSTRIES = [
    # key, название, выход, ед/работник, мест/уровень, входы, множитель цены
    ("farm",       "Ферма",                  "grain",      26.0, J, {}, 0.9),
    ("logging",    "Лесозаготовка",          "wood",        9.0, J, {}, 1.0),
    ("plantation", "Плантация",              "cotton",      9.0, J, {}, 0.9),
    ("coalmine",   "Угольная шахта",         "coal",        8.0, J, {}, 1.1),
    ("mine",       "Рудник",                 "ore",         7.0, J, {}, 1.1),
    ("sulfurmine", "Серный рудник",          "sulfur",      7.0, J, {}, 1.1),

    ("sawmill",    "Лесопилка",              "boards",      4.5, J, {"wood": 2.0}, 1.2),
    ("weaver",     "Ткацкая фабрика",        "cloth",       4.5, J, {"cotton": 2.0}, 1.2),
    ("smelter",    "Металлургический завод", "steel",       3.2, J,
     {"ore": 2.0, "coal": 1.0}, 1.6),

    ("foodplant",  "Пищевой завод",          "food",       11.0, J, {"grain": 2.0}, 1.2),
    ("tailor",     "Швейная фабрика",        "clothes",     2.6, J, {"cloth": 2.0}, 1.3),
    ("furniture",  "Мебельная фабрика",      "furniture",   1.7, J, {"boards": 4.0}, 1.4),
    ("toolworks",  "Инструментальный завод", "tools",       1.6, J,
     {"steel": 2.0, "boards": 1.0}, 1.6),
    ("electro",    "Завод электроники",      "electronics", 0.9, J,
     {"steel": 1.0, "tools": 1.0}, 2.2),

    # Оружейная промышленность. Обе отрасли сидят на стали, поэтому воевать
    # может лишь страна, поднявшая всю цепочку от руды и угля.
    ("armsworks",  "Оружейный завод",        "weapons",     1.2, J,
     {"steel": 2.0, "wood": 1.0}, 1.8),
    ("shellworks", "Снарядный завод",        "shells",      2.2, J,
     {"steel": 1.0, "sulfur": 2.0}, 1.7),

    # Роскошь. Всё это переводит дешёвое сырьё в дорогой товар, поэтому
    # окупается только там, где людям есть на что его покупать.
    ("ranch",      "Скотоводческая ферма",   "meat",        1.2, J,
     {"grain": 4.0}, 1.2),
    ("winery",     "Винодельня",             "wine",        0.8, J,
     {"grain": 3.0}, 1.3),
    ("couture",    "Ателье",                 "fine_clothes", 0.9, J,
     {"cloth": 4.0}, 1.5),
    ("finewood",   "Мебельная мануфактура",  "fine_furniture", 0.6, J,
     {"boards": 6.0, "cloth": 1.0}, 1.7),

    # Рынок — увеличивает долю местной продукции, уходящую на мировой рынок.
    # Не производит товаров и почти не содержит штат; его смысл — пропускная
    # способность внешней торговли государства.
    ("market",     "Торговая площадь",       None,          0.0, 200, {}, 1.0),
]

# ---------------------------------------------------------------------------
# Административные здания. Ничего не производят, содержат служащих и
# потребляют товары. Строит только государство.
# ---------------------------------------------------------------------------
ADMIN = [
    # key, название, мест/уровень, потребление на уровень, множитель, описание
    ("townhall", "Ратуша", 900, {"food": 420.0, "furniture": 26.0}, 1.5,
     "Управа города. Содержит служащих и создаёт спрос на еду и мебель."),
    ("trade_chamber", "Торговая палата", 700,
     {"food": 320.0, "tools": 18.0}, 2.0,
     "Ведомство внешней торговли. Пока только содержит штат — "
     "торговля между странами появится позже."),
    ("academy", "Академия", 600, {"food": 280.0, "furniture": 18.0}, 1.8,
     "Учебное заведение. Пока только содержит штат — "
     "развитие технологий появится позже."),
]

# ---------------------------------------------------------------------------
# Административные здания, которые ещё и производят. Роскошные услуги — опера,
# салоны, галереи — дело казённое: их ставит город, а не фабрикант, и они
# единственный их источник. Оперу нельзя завезти из-за границы (услуга не
# хранится) и нельзя сделать кустарно — только построить у себя.
# ---------------------------------------------------------------------------
CULTURE = [
    # key, название, выпуск, ед/работник, мест/уровень, входы,
    # содержание на уровень, множитель цены, описание
    ("opera", "Оперный театр", "luxury_services", 0.5, J, {"wine": 0.35},
     {"food": 300.0, "fine_furniture": 6.0}, 2.4,
     "Опера, салоны и галереи. Единственный источник роскошных услуг — "
     "того самого, на что тратит деньги разбогатевшее общество. "
     "Строит только государство."),
]

# ---------------------------------------------------------------------------
# 20 государств-областей
# ---------------------------------------------------------------------------
# Названия и столицы. Координаты (x, y) заданы в условной сетке 0..100 для
# отрисовки SVG-карты; соседи — граф соседства для торговли по суше.
# Палитра цветов подобрана различимой на тёмном фоне.
STATES = [
    # (название государства, столица, население, x, y, соседи(индексы 0-based), цвет)
    ("Аркадия",    "Аркад",      1_200_000, 15, 12, [1, 5],         "#e07a5f"),
    ("Белогория",  "Белогорск",  1_600_000, 32, 10, [0, 2, 6],      "#81b29a"),
    ("Вольтария",  "Вольта",     1_050_000, 50, 12, [1, 3, 6, 7],   "#3d5a80"),
    ("Гардения",   "Гарден",      900_000, 68, 10, [2, 4, 8],       "#f2cc8f"),
    ("Дорния",     "Дорн",       1_100_000, 85, 13, [3, 9],         "#a8763e"),
    ("Ефесия",     "Ефес",        850_000, 18, 28, [0, 6, 10],      "#b5179e"),
    ("Железный Край", "Железогорск", 1_300_000, 36, 27, [1, 2, 5, 7, 11], "#6a4c93"),
    ("Зарема",     "Зарем",       980_000, 52, 29, [2, 6, 8, 12],   "#1b998b"),
    ("Иверия",     "Ивер",       1_050_000, 70, 28, [3, 7, 9, 13],  "#c44536"),
    ("Калина",     "Калин",       870_000, 86, 30, [4, 8, 14],      "#5a7a9a"),
    ("Лумина",     "Лум",         760_000, 14, 46, [5, 11, 15],     "#d4a373"),
    ("Маразия",    "Мараз",       990_000, 33, 45, [6, 10, 12, 16], "#8e7dbe"),
    ("Норвия",     "Норв",        880_000, 51, 47, [7, 11, 13, 17], "#2a9d8f"),
    ("Оркония",    "Оркон",       910_000, 69, 46, [8, 12, 14, 18], "#bc4749"),
    ("Палема",     "Палем",       820_000, 86, 48, [9, 13, 19],     "#577590"),
    ("Ривения",    "Ривен",       740_000, 16, 64, [10, 16],        "#9d4edd"),
    ("Сария",      "Сар",         860_000, 34, 63, [11, 15, 17],    "#4cc9f0"),
    ("Таврия",     "Тавр",        800_000, 52, 65, [12, 16, 18],    "#f94144"),
    ("Ундия",      "Унд",         770_000, 70, 64, [13, 17, 19],    "#90be6d"),
    ("Флорания",   "Флоран",      890_000, 87, 66, [14, 18],        "#f9844a"),
]

START_SHARES = {
    "peasants": 0.44,
    "artisans": 0.30,
    "workers": 0.00,
    "town_low": 0.11,
    "town_mid": 0.09,
    "town_high": 0.05,
    # Небольшой гарнизон есть у всех: он и создаёт стартовый спрос на оружие.
    "soldiers": 0.01,
}

START_STOCK_TICKS = 1.0  # на сколько пейдеев спроса заполнить стартовый склад
START_CASH_PER_LEVEL = 0.6   # сбережения сословия на «уровень» потребления

# Каждое государство состоит из двух областей: столичной и провинциальной.
# У каждой свой рынок со своими ценами, и именно область — то, что переходит
# из рук в руки на войне. Одной области на страну было бы мало: тогда любой
# захват сразу стирал бы государство с карты, а торговать внутри страны было
# бы не с кем.
REGION_OFFSETS = [
    # (доля населения, смещение по x, смещение по y)
    (0.60, -2.3, -1.9),      # столичная область
    (0.40,  2.3,  1.9),      # провинция
]

#: Названия провинций — вторых областей каждого государства (по порядку STATES).
PROVINCES = [
    "Приарканье", "Белые Холмы", "Нижняя Вольта", "Гарденский Луг", "Задорнье",
    "Ефесский Берег", "Рудная Падь", "Заречье", "Иверский Дол", "Калиновый Бор",
    "Лумские Топи", "Маразская Степь", "Норвский Фьорд", "Орконский Кряж",
    "Палемская Пуща", "Ривенский Плёс", "Сарская Долина", "Таврский Мыс",
    "Ундский Затон", "Флоранский Сад",
]


def _make_industries(world: World) -> None:
    for key, name, out, opw, jpl, inputs, mult in INDUSTRIES:
        kind = "market" if key == "market" else "industry"
        world.industries[key] = Industry(
            key=key, name=name, output_good=out, output_per_worker=opw,
            jobs_per_level=jpl, inputs=dict(inputs), build_cost_mult=mult, kind=kind)
    for key, name, jpl, upkeep, mult, desc in ADMIN:
        world.industries[key] = Industry(
            key=key, name=name, output_good=None, output_per_worker=0.0,
            jobs_per_level=jpl, inputs={}, build_cost_mult=mult, kind="admin",
            upkeep_goods=dict(upkeep), description=desc)
    for key, name, out, opw, jpl, inputs, upkeep, mult, desc in CULTURE:
        world.industries[key] = Industry(
            key=key, name=name, output_good=out, output_per_worker=opw,
            jobs_per_level=jpl, inputs=dict(inputs), build_cost_mult=mult,
            kind="admin", upkeep_goods=dict(upkeep), description=desc)


def build_world() -> World:
    random.seed(1337)   # детерминированный старт для воспроизводимости тестов
    w = World()
    w.tick_seconds = config.TICK_SECONDS

    # --- глобальные чертежи товаров ---
    for key, name, cat, tier, anchor, storable, perish in GOODS:
        w.goods[key] = Good(key=key, name=name, category=cat, tier=tier,
                            anchor=anchor, storable=storable, perish_rate=perish)

    _make_industries(w)

    # --- 20 государств, в каждом по две области ---
    crafts = _artisan_crafts(w)
    country_regions: dict[int, list[int]] = {}
    for idx, (cname, capital_name, pop, x, y, neighbors, color) in enumerate(STATES):
        cid = w.next_country_id
        w.next_country_id += 1

        region_ids = []
        for r, (pop_share, dx, dy) in enumerate(REGION_OFFSETS):
            name = capital_name if r == 0 else PROVINCES[idx]
            city = City(id=w.next_city_id, name=name, country_id=cid,
                        workforce_share=0.58 if pop < 1_000_000 else 0.54)
            w.next_city_id += 1

            for key, share in START_SHARES.items():
                people = pop * share * pop_share
                level = config.STRATA[key]["level"]
                city.strata[key] = Stratum(
                    people=people,
                    cash=people * level * START_CASH_PER_LEVEL,
                    satisfaction=0.68,
                )
            city.s("artisans").craft_mix = {c: 1.0 / max(len(crafts), 1) for c in crafts}

            # рынок области — копия глобальных якорей
            for gkey, good in w.goods.items():
                city.goods[gkey] = LocalGood(
                    price=good.anchor, anchor=good.anchor,
                    unit_cost=good.anchor / config.ANCHOR_MARKUP)

            w.cities[city.id] = city
            w.map_graph[city.id] = MapNode(x=x + dx, y=y + dy, neighbors=[])
            region_ids.append(city.id)

        country_regions[cid] = region_ids
        city = w.cities[region_ids[0]]      # столица

        # --- государство ---
        country = Country(
            id=cid, name=cname, capital_city_id=city.id,
            treasury=config.START_TREASURY,
            corporate_tax=config.CORPORATE_TAX,
            sales_tax=config.SALES_TAX,
            income_tax=config.INCOME_TAX,
            public_spending_rate=config.PUBLIC_SPENDING_RATE,
            min_wage=config.MIN_WAGE,
            land_rent=config.LAND_RENT,
            leader_id=None,           # AI управляет до первых выборов
            foreign_investment_open=False,
            color=color,
            soldier_pay=config.SOLDIER_PAY_DEFAULT,
            # бюджет ровно под стартовый гарнизон
            army_budget=pop * START_SHARES["soldiers"] * config.SOLDIER_PAY_DEFAULT,
            # Гарнизон начинает вооружённым: иначе первые пейдеи все страны
            # стоят с пустыми арсеналами и разом выгребают рынок оружия.
            army_weapons=pop * START_SHARES["soldiers"] * config.WEAPONS_PER_SOLDIER,
            army_equip=1.0,
        )
        w.countries[cid] = country

        # --- казённый игрок государства (Казна) ---
        salt, pw = hash_password("__no-login__")
        state = Player(id=w.next_player_id, username=f"Казна:{cname[:8]}",
                       password_hash=pw, salt=salt, cash=0.0,
                       is_state=True, country_id=cid)
        w.players[state.id] = state
        w.next_player_id += 1

    # --- граф соседства по областям --------------------------------------
    # Внутри страны области смежны всегда. Между странами связываются те две
    # области, что ближе всего друг к другу: именно по ним и пройдёт фронт.
    country_ids = list(w.countries.keys())
    for a, b in _country_links(country_ids):
        _link(w, *_closest_pair(w, country_regions[a], country_regions[b]))
    for ids in country_regions.values():
        for i in range(len(ids) - 1):
            _link(w, ids[i], ids[i + 1])

    # --- стартовые мировые цены = средние по якорям ---
    w.world_prices = {k: g.anchor for k, g in w.goods.items()}

    # --- стартовые склады еды/зерна у крестьян каждой области ---
    for city in w.cities.values():
        peasants = city.s("peasants")
        # зерно и еда — на ~1 пейдей внутреннего спроса
        food_demand = peasants.people * config.CONSUMPTION_BASKET["food"]["qty"]
        peasants.warehouse["food"] = food_demand * START_STOCK_TICKS
        peasants.warehouse["grain"] = food_demand * 0.5 * START_STOCK_TICKS
        city.goods["food"].stock = peasants.warehouse["food"]
        city.goods["grain"].stock = peasants.warehouse["grain"]

    return w


def _country_links(country_ids: list[int]) -> list[tuple[int, int]]:
    """Пары соседних государств из таблицы STATES (без повторов)."""
    seen = set()
    out = []
    for idx, (*_, neighbors, _color) in enumerate(STATES):
        for j in neighbors:
            key = tuple(sorted((idx, j)))
            if key in seen:
                continue
            seen.add(key)
            out.append((country_ids[key[0]], country_ids[key[1]]))
    return out


def _closest_pair(world: World, left: list[int], right: list[int]) -> tuple[int, int]:
    """Две ближайшие друг к другу области из двух наборов."""
    best, best_d = (left[0], right[0]), None
    for a in left:
        na = world.map_graph[a]
        for b in right:
            nb = world.map_graph[b]
            d = (na.x - nb.x) ** 2 + (na.y - nb.y) ** 2
            if best_d is None or d < best_d:
                best, best_d = (a, b), d
    return best


def _link(world: World, a: int, b: int) -> None:
    """Сделать две области смежными (связь обоюдная)."""
    if a == b:
        return
    for x, y in ((a, b), (b, a)):
        node = world.map_graph.get(x)
        if node is not None and y not in node.neighbors:
            node.neighbors.append(y)


def migrate_world(world: World) -> list[str]:
    """Дополнить уже сохранённый мир тем, что появилось в новых версиях.

    Чертежи товаров и отраслей лежат внутри снимка мира, поэтому старая база
    после обновления игры осталась бы без новых отраслей навсегда: серы,
    оружия и снарядов в ней просто нет. Здесь мы дописываем недостающее, не
    трогая ничего уже существующего — цены, склады и постройки остаются как
    были, игра продолжается с того же места.

    Возвращает список того, что добавлено (для лога).
    """
    added: list[str] = []

    for key, name, cat, tier, anchor, storable, perish in GOODS:
        if key in world.goods:
            continue
        world.goods[key] = Good(key=key, name=name, category=cat, tier=tier,
                                anchor=anchor, storable=storable, perish_rate=perish)
        added.append(f"товар «{name}»")

    known = set(world.industries)
    _make_industries(world)
    for key, ind in world.industries.items():
        if key not in known:
            added.append(f"отрасль «{ind.name}»")

    # --- версия 3 -> 4: рынок переезжает из страны в области ---------------
    if models._LEGACY_COUNTRY_GOODS:
        for country_id, goods in models._LEGACY_COUNTRY_GOODS.items():
            regions = world.country_regions(country_id)
            if not regions:
                continue
            total_pop = sum(c.population for c in regions) or 1.0
            for city in regions:
                share = city.population / total_pop
                for gkey, src in goods.items():
                    if gkey in city.goods:
                        continue
                    # цены общие, склад делится по числу жителей
                    city.goods[gkey] = LocalGood(
                        price=src.price, anchor=src.anchor,
                        unit_cost=src.unit_cost, stock=src.stock * share,
                        last_demand=src.last_demand * share,
                        last_supply=src.last_supply * share,
                        last_sold=src.last_sold * share,
                        last_shortage=src.last_shortage)
        models._LEGACY_COUNTRY_GOODS.clear()
        added.append("рынки областей")

    # --- версия 3 -> 4: склады игроков переезжают со стран на области ------
    for p in world.players.values():
        moved: dict[int, dict[str, float]] = {}
        for key, store in list(p.warehouses.items()):
            if key in world.cities:
                moved.setdefault(key, {})
                for g, q in store.items():
                    moved[key][g] = moved[key].get(g, 0.0) + q
                continue
            country = world.countries.get(key)
            if country is None:
                continue
            regions = world.country_regions(key)
            if not regions:
                continue
            # весь товар кладём в столицу — делить его между областями нечестно
            target = country.capital_city_id
            if target not in world.cities:
                target = regions[0].id
            dest = moved.setdefault(target, {})
            for g, q in store.items():
                dest[g] = dest.get(g, 0.0) + q
        p.warehouses = moved

    # у каждой области должны быть цены и склад на каждый товар
    for city in world.cities.values():
        for gkey, good in world.goods.items():
            if gkey not in city.goods:
                city.goods[gkey] = LocalGood(
                    price=good.anchor, anchor=good.anchor,
                    unit_cost=good.anchor / config.ANCHOR_MARKUP)

    # --- версия 3 -> 4: граф карты переезжает со стран на области ----------
    if world.map_graph and models._LOADED_VERSION[0] < 4:
        old = dict(world.map_graph)
        world.map_graph = {}
        for country_id, node in old.items():
            regions = world.country_regions(country_id)
            for i, city in enumerate(regions):
                dx = -2.3 if i % 2 == 0 else 2.3
                dy = -1.9 if i % 2 == 0 else 1.9
                world.map_graph[city.id] = MapNode(x=node.x + dx, y=node.y + dy,
                                                   neighbors=[])
        for country_id, node in old.items():
            mine = [c.id for c in world.country_regions(country_id)]
            for i in range(len(mine) - 1):
                _link(world, mine[i], mine[i + 1])
            for nb_country in node.neighbors:
                theirs = [c.id for c in world.country_regions(nb_country)]
                if mine and theirs:
                    _link(world, *_closest_pair(world, mine, theirs))
        added.append("карта областей")

    for country in world.countries.values():
        # Армии в старом мире не было: даём ставку жалованья, чтобы лидеру
        # было от чего отталкиваться. Бюджет остаётся нулевым — набирать
        # армию или нет, решает он сам.
        if country.soldier_pay <= 0.0:
            country.soldier_pay = config.SOLDIER_PAY_DEFAULT
        if country.bankruptcy_limit >= 0.0:
            country.bankruptcy_limit = config.BANKRUPTCY_LIMIT
        # Раньше оружие не хранилось на складах — вооружённость мерили тем,
        # что солдаты успели купить за пейдей. Переносим уже достигнутую
        # вооружённость в арсенал, иначе идущая партия разом окажется с
        # безоружной армией, хотя игрок ничего не менял.
        if country.army_weapons <= 0.0:
            soldiers = sum(c.s("soldiers").people
                           for c in world.country_regions(country.id))
            country.army_weapons = (soldiers * config.WEAPONS_PER_SOLDIER
                                    * max(0.0, min(1.0, country.army_equip)))

    # Уровень жизни в старом мире не считался: заводим сословиям осмысленные
    # стартовые ожидания, иначе первые пейдеи они провели бы «в нужде» с нуля.
    for city in world.cities.values():
        for key in config.STRATA_ORDER:
            st = city.s(key)
            if st.expectation <= 0.0:
                st.expectation = 1.0
            if st.living_standard <= 0.0:
                st.living_standard = 1.0

    for gkey, good in world.goods.items():
        world.world_prices.setdefault(gkey, good.anchor)

    return added


def _artisan_crafts(world: World) -> dict[str, dict]:
    """Локальная копия society.artisan_crafts для инициализации craft_mix.

    Вынесена сюда, чтобы seed не зависел от импорта society (тот тянет pricing
    и может усложнить порядок загрузки).
    """
    crafts: dict[str, dict] = {}
    for ind in world.industries.values():
        if ind.kind != "industry" or not ind.output_good:
            continue
        if ind.output_good not in world.goods:
            continue
        crafts[ind.output_good] = {
            "out": ind.output_per_worker * config.ARTISAN_EFFICIENCY,
            "inputs": {g: q * config.ARTISAN_INPUT_PENALTY for g, q in ind.inputs.items()},
        }
    return crafts
