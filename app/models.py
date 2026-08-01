"""
Состояние мира SimPolEc — обычные dataclass-объекты, без ORM.

Мир состоит из множества государств (Country), каждое со своей казной,
налогами, ценами и рынком. Города и игроки привязаны к государствам.
Товары разделены на глобальные чертежи (Good — имя, категория, порча) и
локальные цены/склады (LocalGood) — по экземпляру на государство.
Весь мир целиком помещается в память и хранится одним JSON-снимком в SQLite.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


SCHEMA_VERSION = 4   # 1 = одно государство, 2 = множество государств,
#                      3 = склады по странам, армия, война и дипломатия,
#                      4 = рынок у каждой области, карта по областям


#: Рынок страны из снимка версии 3, отложенный до миграции в области.
#: Заполняется в World.from_json, разбирается в economy.seed.migrate_world.
_LEGACY_COUNTRY_GOODS: dict[int, dict[str, "LocalGood"]] = {}

#: Версия последнего загруженного снимка. По ней migrate_world понимает, что
#: именно надо переносить. Определять это по данным нельзя: id областей и стран
#: в старых мирах совпадали, и «карта по странам» неотличима от «карты по
#: областям» простым сравнением ключей.
_LOADED_VERSION: list[int] = [SCHEMA_VERSION]


@dataclass
class Good:
    """Глобальный чертёж товара (не меняется между государствами)."""
    key: str
    name: str
    category: str          # raw / intermediate / consumer / services
    tier: int = 0          # 1 базовое, 2 обычное, 3 роскошь
    anchor: float = 10.0   # стартовая себестоимость с наценкой (для инициализации)
    storable: bool = True  # услуги нельзя положить на склад
    perish_rate: float = 0.0   # доля запаса, теряемая за пейдей (еда/зерно портятся)


@dataclass
class LocalGood:
    """Цены и склад конкретного товара в конкретной области.

    Рынок живёт на уровне ОБЛАСТИ, а не государства: в каждой свои цены, свой
    склад и свой дефицит. Страна — это лишь набор областей с общей казной и
    общими налогами. Торговые площади связывают области между собой и
    выравнивают цены внутри страны, но пока их нет, соседние области могут
    жить с совсем разной дороговизной.
    """
    price: float = 10.0
    anchor: float = 10.0   # сглаженная себестоимость с наценкой
    unit_cost: float = 8.7
    stock: float = 0.0
    last_demand: float = 0.0
    last_supply: float = 0.0
    last_sold: float = 0.0
    last_shortage: float = 0.0


@dataclass
class Industry:
    key: str
    name: str
    output_good: str | None          # None у административных зданий
    output_per_worker: float
    jobs_per_level: int
    inputs: dict[str, float] = field(default_factory=dict)   # на единицу выпуска
    build_cost_mult: float = 1.0
    kind: str = "industry"           # industry / admin / market
    upkeep_goods: dict[str, float] = field(default_factory=dict)  # на уровень
    description: str = ""


@dataclass
class Stratum:
    """Сословие внутри города."""
    people: float = 0.0
    cash: float = 0.0
    income: float = 0.0        # доход за прошлый пейдей, всего
    spent: float = 0.0
    satisfaction: float = 0.65
    idle_streak: float = 0.0   # для рабочих: сколько пейдеев без работы
    # Ожидаемый уровень жизни: во сколько корзин эталонного потребителя
    # сословие считает нормальным жить. Растёт от сбережений, падает от нужды —
    # и то и другое медленно. Умножает обычную корзину и открывает роскошь.
    expectation: float = 1.0
    # Фактический уровень жизни: сколько корзин на человека реально потреблено.
    living_standard: float = 1.0
    # непроданный товар сословия, лежащий на рынке
    warehouse: dict[str, float] = field(default_factory=dict)
    # чем заняты кустари: доля людей по ремёслам
    craft_mix: dict[str, float] = field(default_factory=dict)

    @property
    def income_per_capita(self) -> float:
        return self.income / self.people if self.people > 1 else 0.0


@dataclass
class City:
    """Область: свой рынок, своё население, своё хозяйство.

    Область — основная единица карты и войны. Она может перейти другому
    государству и быть отбита обратно, но никуда не исчезает.
    """
    id: int
    name: str
    country_id: int = 0          # государство, которому принадлежит область
    workforce_share: float = 0.55   # доля сословия, способная выйти на работу
    cash: float = 0.0               # не используется, оставлено для истории
    satisfaction: float = 0.65
    unemployment: float = 0.0
    avg_wage: float = 12.0
    harvest: float = 1.0            # урожайность прошлого пейдея
    # Накопленное озлобление. Растёт от нищеты, выливается в бунты, а из
    # затяжного бунта вырастает гражданская война.
    unrest: float = 0.0
    revolt_ticks: int = 0           # сколько пейдеев область в мятеже
    strata: dict[str, Stratum] = field(default_factory=dict)
    # рынок области: {good_key: LocalGood}
    goods: dict[str, LocalGood] = field(default_factory=dict)

    # ---- население ----
    @property
    def population(self) -> float:
        return sum(s.people for s in self.strata.values())

    def s(self, key: str) -> Stratum:
        st = self.strata.get(key)
        if st is None:
            st = Stratum()
            self.strata[key] = st
        return st

    @property
    def workers(self) -> float:
        return self.s("workers").people

    @property
    def savings(self) -> float:
        return sum(s.cash for s in self.strata.values())


@dataclass
class Building:
    id: int
    industry_key: str
    owner_id: int
    city_id: int
    level: int = 1
    wage: float = 12.0
    employed: float = 0.0
    active: bool = True
    throttle: float = 1.0
    last_output: float = 0.0
    last_revenue: float = 0.0
    last_costs: float = 0.0
    last_inputs: float = 0.0
    last_wages: float = 0.0
    last_profit: float = 0.0
    loss_streak: int = 0
    # Разрушенные войной уровни. Работают только level - damage уровней,
    # пока владелец не оплатит починку.
    damage: int = 0

    @property
    def effective_level(self) -> int:
        """Сколько уровней реально работает после военных разрушений."""
        return max(0, self.level - self.damage)


@dataclass
class Player:
    id: int
    username: str
    password_hash: str = ""
    salt: str = ""
    cash: float = 0.0
    is_state: bool = False       # казённый «игрок»: его касса — это казна
    country_id: int = 0          # гражданство игрока
    governor_of: int | None = None   # id государства, которым управляет (None — обычный игрок)
    # Дело признано банкротом: касса пробила порог, установленный государством.
    # Все предприятия стоят, пока владелец не выберется или не получит субсидию.
    bankrupt: bool = False
    bankrupt_since: int = 0          # с какого пейдея
    # Склады игрока по ОБЛАСТЯМ: {city_id: {good_key: qty}}.
    #
    # Раздельно по областям — потому что рынок живёт в области. Завод выпускает
    # товар на тот рынок, где он стоит, и продаёт его тамошним жителям по
    # тамошним ценам, а деньги приходят владельцу, где бы тот ни жил. С одним
    # общим складом рынок затирал бы чужие остатки, и непроданный товар просто
    # исчезал бы вместе с вложенными в него деньгами.
    warehouses: dict[int, dict[str, float]] = field(default_factory=dict)

    # Обратная совместимость со старым кодом/фронтом, оперирующим is_governor.
    @property
    def is_governor(self) -> bool:
        return self.governor_of is not None

    def store(self, city_id: int) -> dict[str, float]:
        """Склад игрока на рынке конкретной области (создаётся по требованию)."""
        s = self.warehouses.get(city_id)
        if s is None:
            s = {}
            self.warehouses[city_id] = s
        return s

    def all_stock(self) -> dict[str, float]:
        """Весь товар игрока во всех областях, сложенный по видам."""
        total: dict[str, float] = {}
        for store in self.warehouses.values():
            for k, q in store.items():
                total[k] = total.get(k, 0.0) + q
        return total


@dataclass
class Election:
    """Текущая стадия выборов лидера государства."""
    phase: str = "none"          # none / campaign / voting
    started_tick: int = 0
    # голоса: {id голосующего игрока: id кандидата}
    votes: dict[int, int] = field(default_factory=dict)
    # Были ли выборы назначены досрочно — по вотуму недоверия.
    snap: bool = False
    # Вотум доверия: {id игрока: "trust" | "distrust"}. Живёт всё время между
    # выборами, а не отдельной кампанией: недоверие можно выразить когда угодно.
    confidence: dict[int, str] = field(default_factory=dict)


@dataclass
class Country:
    id: int
    name: str
    capital_city_id: int = 0
    treasury: float = 0.0
    corporate_tax: float = 0.18
    sales_tax: float = 0.06
    income_tax: float = 0.10
    public_spending_rate: float = 0.35
    min_wage: float = 2.0
    land_rent: float = 0.10      # доля выручки крестьян в пользу высшего класса
    gdp: float = 0.0
    industrialisation: float = 0.0   # доля рабочих в населении
    leader_id: int | None = None        # Player.id действующего лидера (None = AI)
    foreign_investment_open: bool = False   # разрешены ли стройки иностранцев
    color: str = "#6b7a8f"       # цвет государства на карте

    # ---- армия ----
    soldier_pay: float = 0.0     # жалованье солдату за пейдей, отдельной статьёй
    # Военный бюджет: твёрдая сумма из доходов казны за пейдей. Именно он, а не
    # желание лидера, задаёт численность армии: army_budget / soldier_pay — вот
    # сколько солдат страна может содержать. Так казна не уходит в минус от
    # того, что под ружьё призвали всех подряд.
    army_budget: float = 0.0
    army_shells: float = 0.0     # снаряды на армейских складах
    army_weapons: float = 0.0    # оружие на армейских складах — ЗАПАС
    # Вооружённость: запас оружия к целевому, 0..1. Считается из army_weapons,
    # а не из покупок за пейдей, — это состояние армии, а не её спрос.
    army_equip: float = 0.0
    mobilization_left: int = 0   # пейдеев насильной мобилизации ещё осталось
    last_mobilized: float = 0.0  # сколько человек забрали приказом за пейдей

    # Порог банкротства: до какого минуса государство позволяет предприятиям
    # работать в долг. Ниже него дело встаёт и ждёт субсидии.
    bankruptcy_limit: float = -3_000_000.0
    last_subsidies: float = 0.0  # выдано субсидий за прошлый пейдей
    last_army_cost: float = 0.0  # сколько казна отдала армии за прошлый пейдей
    last_shells_bought: float = 0.0
    last_weapons_bought: float = 0.0   # закуплено оружия за пейдей
    last_weapons_worn: float = 0.0     # списано оружия за пейдей — ПОТРЕБЛЕНИЕ
    alive: bool = True           # False — государство потеряло все области

    # Рынка у страны нет: он живёт в каждой области отдельно (City.goods).
    # Страна — это казна, налоги, армия, политика и набор областей.
    # текущие выборы лидера
    election: Election = field(default_factory=Election)


@dataclass
class War:
    """Война между двумя коалициями государств.

    Стороны — списки, а не пара стран: в войну втягиваются союзники, и выйти
    из неё можно поодиночке (сепаратный мир), не заканчивая войну для всех.
    """
    id: int
    attackers: list[int] = field(default_factory=list)
    defenders: list[int] = field(default_factory=list)
    started_tick: int = 0
    ended: bool = False
    ended_tick: int = 0
    # накопленное превосходство на фронте: ключ "нападающий>обороняющийся"
    occupation: dict[str, float] = field(default_factory=dict)
    # пары, заключившие сепаратный мир: "меньший:больший". Друг с другом они
    # больше не воюют, но война для остальных продолжается.
    peace: list[str] = field(default_factory=list)
    # сводка боёв прошлого пейдея — для интерфейса
    last_report: list[dict] = field(default_factory=list)

    def side_of(self, country_id: int) -> str | None:
        if country_id in self.attackers:
            return "attackers"
        if country_id in self.defenders:
            return "defenders"
        return None

    def enemies_of(self, country_id: int) -> list[int]:
        """Противники по своей стороне, с кем ещё не заключён сепаратный мир."""
        side = self.side_of(country_id)
        if side == "attackers":
            other = list(self.defenders)
        elif side == "defenders":
            other = list(self.attackers)
        else:
            return []
        return [e for e in other if not self.at_peace(country_id, e)]

    @staticmethod
    def pair_key(a: int, b: int) -> str:
        lo, hi = (a, b) if a <= b else (b, a)
        return f"{lo}:{hi}"

    def at_peace(self, a: int, b: int) -> bool:
        return self.pair_key(a, b) in self.peace

    def participants(self) -> list[int]:
        return list(self.attackers) + list(self.defenders)


@dataclass
class Annexation:
    """Захваченная область, судьбу чужих промышленников в которой решает
    лидер-победитель: оставить их дело или выгнать со сносом заводов."""
    id: int
    country_id: int              # кто захватил — его лидер и решает
    former_country_id: int       # у кого отняли
    city_id: int
    city_name: str = ""
    former_name: str = ""
    created_tick: int = 0
    resolved: bool = False
    # id игроков, чью судьбу ещё не решили
    pending: list[int] = field(default_factory=list)
    # что решили: {id игрока: "keep" | "expel"}
    decisions: dict[int, str] = field(default_factory=dict)


@dataclass
class Offer:
    """Дипломатическое предложение: сепаратный мир или союз."""
    id: int
    kind: str                  # peace / alliance
    from_country: int
    to_country: int
    war_id: int | None = None  # для мира — в какой войне
    created_tick: int = 0


@dataclass
class MapNode:
    """Узел карты-графа: ОБЛАСТЬ с координатами и связями соседства.

    Карта рисуется по областям, а не по государствам: захваченная область не
    исчезает с карты, а меняет цвет — и соседи могут отбить её обратно.
    """
    x: float = 0.0
    y: float = 0.0
    neighbors: list[int] = field(default_factory=list)   # id соседних областей


@dataclass
class World:
    tick: int = 0
    last_tick_at: float = 0.0     # unix-время последнего пейдея
    tick_seconds: int = 900
    next_building_id: int = 1
    next_player_id: int = 1
    next_city_id: int = 1
    next_country_id: int = 1
    next_war_id: int = 1
    next_offer_id: int = 1
    next_annex_id: int = 1

    countries: dict[int, Country] = field(default_factory=dict)
    goods: dict[str, Good] = field(default_factory=dict)        # чертежи товаров
    industries: dict[str, Industry] = field(default_factory=dict)
    cities: dict[int, City] = field(default_factory=dict)
    buildings: dict[int, Building] = field(default_factory=dict)
    players: dict[int, Player] = field(default_factory=dict)
    sessions: dict[str, int] = field(default_factory=dict)  # token -> player_id
    # граф карты: {country_id: MapNode}
    map_graph: dict[int, MapNode] = field(default_factory=dict)
    # мировые цены товаров (единый ориентир для мировой торговли)
    world_prices: dict[str, float] = field(default_factory=dict)
    # сделки мировой биржи за прошлый пейдей: {good_key: сводка}
    last_trades: dict[str, Any] = field(default_factory=dict)

    # войны и дипломатия
    wars: dict[int, War] = field(default_factory=dict)
    offers: dict[int, Offer] = field(default_factory=dict)
    # союзы: список пар [a, b] (хранится отсортированным, без повторов)
    alliances: list[list[int]] = field(default_factory=list)
    # захваченные области, судьба игроков в которых ещё не решена
    annexations: dict[int, Annexation] = field(default_factory=dict)

    # ---------------- сериализация ----------------
    def to_json(self) -> dict[str, Any]:
        def city_json(c: City) -> dict:
            d = asdict(c)
            d["strata"] = {k: asdict(v) for k, v in c.strata.items()}
            d["goods"] = {k: asdict(v) for k, v in c.goods.items()}
            return d

        def country_json(c: Country) -> dict:
            return asdict(c)

        def player_json(p: Player) -> dict:
            d = {k: v for k, v in asdict(p).items() if k != "warehouses"}
            # ключ страны в JSON обязан быть строкой
            d["warehouses"] = {str(k): dict(v) for k, v in p.warehouses.items()}
            return d

        def annex_json(a: Annexation) -> dict:
            d = {k: v for k, v in asdict(a).items() if k != "decisions"}
            d["decisions"] = {str(k): v for k, v in a.decisions.items()}
            return d

        return {
            "version": SCHEMA_VERSION,
            "tick": self.tick,
            "last_tick_at": self.last_tick_at,
            "tick_seconds": self.tick_seconds,
            "next_building_id": self.next_building_id,
            "next_player_id": self.next_player_id,
            "next_city_id": self.next_city_id,
            "next_country_id": self.next_country_id,
            "next_war_id": self.next_war_id,
            "next_offer_id": self.next_offer_id,
            "next_annex_id": self.next_annex_id,
            "goods": {k: asdict(v) for k, v in self.goods.items()},
            "industries": {k: asdict(v) for k, v in self.industries.items()},
            "countries": {str(k): country_json(v) for k, v in self.countries.items()},
            "cities": {str(k): city_json(v) for k, v in self.cities.items()},
            "buildings": {str(k): asdict(v) for k, v in self.buildings.items()},
            "players": {str(k): player_json(v) for k, v in self.players.items()},
            "sessions": self.sessions,
            "map_graph": {str(k): asdict(v) for k, v in self.map_graph.items()},
            "world_prices": self.world_prices,
            "last_trades": self.last_trades,
            "wars": {str(k): asdict(v) for k, v in self.wars.items()},
            "offers": {str(k): asdict(v) for k, v in self.offers.items()},
            "alliances": [list(a) for a in self.alliances],
            "annexations": {str(k): annex_json(v) for k, v in self.annexations.items()},
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "World":
        _LEGACY_COUNTRY_GOODS.clear()
        _LOADED_VERSION[0] = int(d.get("version", 1))
        def city(v: dict) -> City:
            strata = {k: Stratum(**s) for k, s in (v.pop("strata", {}) or {}).items()}
            goods = {k: LocalGood(**g) for k, g in (v.pop("goods", {}) or {}).items()}
            c = City(**v)
            c.strata = strata
            c.goods = goods
            return c

        def country(v: dict) -> Country:
            # В снимке версии 3 рынок лежал у страны. Переносить его сюда
            # некуда — области ещё не разобраны, поэтому просто откладываем
            # его в сторону, а migrate_world раздаст цены областям.
            legacy = {k: LocalGood(**g) for k, g in (v.pop("goods", None) or {}).items()}
            election = dict(v.pop("election", None) or {})
            confidence = election.pop("confidence", None) or {}
            c = Country(**v)
            c.election = Election(**election)
            c.election.confidence = {int(k): str(x) for k, x in confidence.items()}
            if legacy:
                _LEGACY_COUNTRY_GOODS[c.id] = legacy
            return c

        def annexation(v: dict) -> Annexation:
            decisions = v.pop("decisions", None) or {}
            a = Annexation(**v)
            a.decisions = {int(k): str(x) for k, x in decisions.items()}
            return a

        def player(v: dict) -> Player:
            # Снимок версии 2 хранил один общий склад. Считаем, что весь товар
            # лежал на домашнем рынке игрока, и раскладываем его по странам.
            legacy = v.pop("warehouse", None)
            raw = v.pop("warehouses", None) or {}
            p = Player(**v)
            p.warehouses = {int(k): dict(s) for k, s in raw.items()}
            if legacy:
                home = p.store(p.country_id)
                for k, q in legacy.items():
                    home[k] = home.get(k, 0.0) + q
            return p

        return cls(
            tick=d.get("tick", 0),
            last_tick_at=d.get("last_tick_at", 0.0),
            tick_seconds=d.get("tick_seconds", 900),
            next_building_id=d.get("next_building_id", 1),
            next_player_id=d.get("next_player_id", 1),
            next_city_id=d.get("next_city_id", 1),
            next_country_id=d.get("next_country_id", 1),
            next_war_id=d.get("next_war_id", 1),
            next_offer_id=d.get("next_offer_id", 1),
            next_annex_id=d.get("next_annex_id", 1),
            goods={k: Good(**v) for k, v in d.get("goods", {}).items()},
            industries={k: Industry(**v) for k, v in d.get("industries", {}).items()},
            countries={int(k): country(dict(v))
                       for k, v in d.get("countries", {}).items()},
            cities={int(k): city(dict(v)) for k, v in d.get("cities", {}).items()},
            buildings={int(k): Building(**v) for k, v in d.get("buildings", {}).items()},
            players={int(k): player(dict(v)) for k, v in d.get("players", {}).items()},
            sessions={k: int(v) for k, v in d.get("sessions", {}).items()},
            map_graph={int(k): MapNode(**v) for k, v in d.get("map_graph", {}).items()},
            world_prices=dict(d.get("world_prices", {})),
            last_trades=dict(d.get("last_trades", {})),
            wars={int(k): War(**v) for k, v in d.get("wars", {}).items()},
            offers={int(k): Offer(**v) for k, v in d.get("offers", {}).items()},
            alliances=[list(a) for a in d.get("alliances", [])],
            annexations={int(k): annexation(dict(v))
                         for k, v in d.get("annexations", {}).items()},
        )

    # ---------------- удобные выборки ----------------
    def player_buildings(self, player_id: int) -> list[Building]:
        return [b for b in self.buildings.values() if b.owner_id == player_id]

    def city_buildings(self, city_id: int) -> list[Building]:
        return [b for b in self.buildings.values() if b.city_id == city_id]

    def population(self) -> float:
        return sum(c.population for c in self.cities.values())

    def country_of_city(self, city_id: int) -> Country | None:
        city = self.cities.get(city_id)
        return self.countries.get(city.country_id) if city else None

    def country_regions(self, country_id: int) -> list[City]:
        """Области государства."""
        return [c for c in self.cities.values() if c.country_id == country_id]

    def region_neighbors(self, city_id: int) -> list[int]:
        node = self.map_graph.get(city_id)
        return list(node.neighbors) if node else []

    def border_regions(self, a_country: int, b_country: int) -> list[tuple[int, int]]:
        """Пары соседних областей, лежащих по разные стороны границы."""
        out = []
        for city in self.country_regions(a_country):
            for nb in self.region_neighbors(city.id):
                other = self.cities.get(nb)
                if other is not None and other.country_id == b_country:
                    out.append((city.id, nb))
        return out

    def neighbor_countries(self, country_id: int) -> list[int]:
        """Государства, с которыми есть общая граница хотя бы по одной области."""
        seen: list[int] = []
        for city in self.country_regions(country_id):
            for nb in self.region_neighbors(city.id):
                other = self.cities.get(nb)
                if other is None or other.country_id == country_id:
                    continue
                if other.country_id not in seen:
                    seen.append(other.country_id)
        return seen

    def country_of_player(self, player_id: int) -> Country | None:
        p = self.players.get(player_id)
        return self.countries.get(p.country_id) if p else None

    def state_player(self, country_id: int) -> Player | None:
        """Казённый игрок конкретного государства."""
        for p in self.players.values():
            if p.is_state and p.country_id == country_id:
                return p
        return None

    def net_worth(self, player: Player) -> float:
        """Касса + товар на всех рынках + ликвидационная стоимость предприятий."""
        from . import config

        stock_value = 0.0
        for city_id, store in player.warehouses.items():
            city = self.cities.get(city_id)
            for k, q in store.items():
                good = self.goods.get(k)
                if good is None:
                    continue
                local = city.goods.get(k) if city else None
                stock_value += q * (local.price if local else good.anchor)
        assets = 0.0
        for b in self.buildings.values():
            if b.owner_id != player.id:
                continue
            mult = self.industries[b.industry_key].build_cost_mult
            assets += sum(config.BUILD_COST_BASE * mult
                          * lv ** config.BUILD_COST_EXPONENT
                          for lv in range(1, b.level + 1)) * config.DEMOLISH_REFUND
        return player.cash + stock_value + assets

    # ---------------- войны и союзы ----------------
    def active_wars(self) -> list[War]:
        return [w for w in self.wars.values() if not w.ended]

    def wars_of(self, country_id: int) -> list[War]:
        return [w for w in self.active_wars() if w.side_of(country_id) is not None]

    def at_war(self, a: int, b: int) -> bool:
        """Воюют ли две страны друг против друга прямо сейчас."""
        return any(b in w.enemies_of(a) for w in self.active_wars())

    def allied(self, a: int, b: int) -> bool:
        return any(set(pair) == {a, b} for pair in self.alliances)

    def allies_of(self, country_id: int) -> list[int]:
        out = []
        for pair in self.alliances:
            if country_id in pair:
                out.append(pair[0] if pair[1] == country_id else pair[1])
        return out

    def are_neighbors(self, a: int, b: int) -> bool:
        """Есть ли у двух ГОСУДАРСТВ общая граница (хоть по одной области)."""
        return bool(self.border_regions(a, b))

    def neighbor_countries(self, country_id: int) -> list[int]:
        """Государства, граничащие ХОТЬ С ОДНОЙ областью этого государства.

        Соседство живёт на графе областей, а не государств: отбили область —
        и граница поехала вместе с ней, а вчерашний дальний сосед стал
        ближним. Поэтому список считается заново, а не хранится.
        """
        out: list[int] = []
        seen = {country_id}
        for city in self.country_regions(country_id):
            for nb in self.region_neighbors(city.id):
                other = self.cities.get(nb)
                if other is None or other.country_id in seen:
                    continue
                seen.add(other.country_id)
                out.append(other.country_id)
        return out
