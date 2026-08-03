"""
Сословия: кто что производит, кто сколько потребляет и кто куда переходит.

Каждое государство живёт своей экономикой: у него свои цены (Country.goods),
свой рынок и свои налоги. Поэтому функции, которым нужны цены или политика,
принимают конкретное государство, а не читают «мировое».

* **Крестьяне** живут с земли. Еду они сначала едят сами и только излишек
  несут на рынок.
* **Кустари** — те же селяне, взявшиеся за ремесло. Они зеркалят все отрасли
  и берутся за то, что выгоднее по марже, но работают несравнимо хуже завода.
* **Горожане** производят «Услуги» — товар, который покупают все остальные.
* **Рабочие** появляются, когда предприятие предлагает зарплату выше того,
  что человек имеет сейчас. Обратно в деревню хода нет.

**Пункт про зарплату найма.** Опорная зарплата для подсказки и новых строек:
если в стране есть свободные рабочие, они идут на любую зарплату, и заводы в
расчёте опорной ставки не учитываются (они заняты своим делом). Только когда
свободных рабочих нет и приходится переманивать людей с земли, к ставке
добавляется конкуренция заводов.
"""
from __future__ import annotations

import math
import random

from .. import config
from ..models import City, Country, Good, LocalGood, World
from .pricing import demand_response

EPS = 1e-9


# ---------------------------------------------------------------------------
# Кошельки сословий на рынке
# ---------------------------------------------------------------------------
def stratum_owner_id(city_id: int, stratum: str) -> int:
    """Виртуальный «владелец товара» для сословия.

    Рынок раздаёт выручку по идентификатору владельца. У игроков он
    положительный, у сословий — отрицательный, чтобы одни и те же механизмы
    консигнации и продажи работали и для завода, и для деревни.
    """
    return -(city_id * 100 + config.STRATA_ORDER.index(stratum) + 1)


def decode_owner(owner_id: int) -> tuple[int, str] | None:
    if owner_id >= 0:
        return None
    v = -owner_id
    city_id, idx = divmod(v, 100)
    return city_id, config.STRATA_ORDER[idx - 1]


def rng_for(world: World, city_id: int, salt: int = 0) -> random.Random:
    """Детерминированный разброс: один и тот же мир даёт один и тот же прогон."""
    return random.Random((world.tick * 7919 + city_id * 131 + salt) & 0x7FFFFFFF)


def lg(city: City, key: str) -> LocalGood:
    """Цены и склад товара на рынке ОБЛАСТИ (с дефолтом).

    Рынок принадлежит области, а не стране: у соседних областей одной страны
    цены могут заметно расходиться, пока их не свяжут торговые площади.
    """
    g = city.goods.get(key)
    if g is None:
        g = LocalGood()
        city.goods[key] = g
    return g


# ---------------------------------------------------------------------------
# Услуги
# ---------------------------------------------------------------------------
def material_basket_cost(city: City) -> float:
    """Стоимость материальной корзины на одного эталонного потребителя."""
    total = 0.0
    for key, spec in config.CONSUMPTION_BASKET.items():
        # услуги — не материальный товар, в материальную корзину не входят
        if key == "services":
            continue
        good = city.goods.get(key)
        if good is None:
            continue
        total += spec["qty"] * good.price
    return total


def produce_services(world: World, country: Country, city: City, market) -> None:
    """Горожане производят услуги: торговля, ремесло, искусство."""
    r = rng_for(world, city.id, salt=11)
    for key, per_head in config.SERVICE_OUTPUT.items():
        if per_head <= 0:
            continue
        st = city.s(key)
        if st.people <= EPS:
            continue
        swing = 1.0 + r.uniform(-config.SERVICE_SWING, config.SERVICE_SWING)
        qty = st.people * per_head * swing
        # услуги оказываются и потребляются в тот же пейдей
        market.deposit("services", stratum_owner_id(city.id, key), qty,
                       immediate=True)


# ---------------------------------------------------------------------------
# Крестьяне
# ---------------------------------------------------------------------------
def glut_scale(local: LocalGood) -> float:
    """Насколько сбавить выпуск, если товар уже некуда девать."""
    if local.last_demand <= EPS:
        return config.GLUT_MIN_OUTPUT if local.stock > EPS else 1.0
    cover = local.stock / local.last_demand      # запас в пейдеях спроса
    if cover <= config.GLUT_TARGET_TICKS:
        return 1.0
    excess = (cover - config.GLUT_TARGET_TICKS) / config.GLUT_ADJUST_TICKS
    return max(config.GLUT_MIN_OUTPUT, 1.0 - excess)


def produce_peasants(world: World, country: Country, city: City, market) -> float:
    """Урожай, собственное потребление и излишек на рынок.

    Возвращает, сколько еды крестьяне съели из своего урожая.
    """
    st = city.s("peasants")
    if st.people <= EPS:
        city.harvest = 1.0
        return 0.0

    r = rng_for(world, city.id, salt=3)
    harvest = 1.0 + r.uniform(-config.HARVEST_SWING, config.HARVEST_SWING)
    city.harvest = harvest

    owner = stratum_owner_id(city.id, "peasants")
    own_food = 0.0
    for good_key, per_head in config.PEASANT_YIELD.items():
        qty = st.people * per_head * harvest
        if good_key == "food":
            # сначала едят сами, на рынок идёт только излишек
            need = (st.people * config.CONSUMPTION_BASKET["food"]["qty"]
                    * config.STRATA["peasants"]["level"])
            own_food = min(qty, need)
            qty = max(0.0, qty - own_food)
        if qty > EPS:
            market.deposit(good_key, owner, qty * glut_scale(lg(city, good_key)))
    return own_food


# ---------------------------------------------------------------------------
# Кустари
# ---------------------------------------------------------------------------
def artisan_crafts(world: World) -> dict[str, dict]:
    """Ремёсла кустаря — зеркальное отражение всех отраслей государства."""
    crafts: dict[str, dict] = {}
    for ind in world.industries.values():
        if ind.kind != "industry" or not ind.output_good:
            continue
        if ind.output_good not in world.goods:
            continue
        crafts[ind.output_good] = {
            "out": ind.output_per_worker * config.ARTISAN_EFFICIENCY,
            "inputs": {g: q * config.ARTISAN_INPUT_PENALTY
                       for g, q in ind.inputs.items()},
        }
    return crafts


def craft_margin(city: City, craft: str, crafts: dict[str, dict]) -> float:
    """Заработок кустаря на человека за пейдей при текущих ценах."""
    spec = crafts.get(craft)
    if spec is None:
        return 0.0
    local = city.goods.get(craft)
    if local is None:
        return 0.0
    input_cost = sum(qty * lg(city, k).price for k, qty in spec["inputs"].items())
    return (local.price - input_cost) * spec["out"]


def craft_feasibility(city: City, craft: str, crafts: dict[str, dict],
                      people: float) -> float:
    """Сколько кустарей ремесло вообще способно прокормить сырьём, в долях.

    Одной маржи для выбора ремесла мало, и это не мелочь, а причина, по которой
    разваливалась вся текстильная цепочка.

    У товара, которого в стране никто не делает, цена стоит у потолка коридора,
    и расчётная маржа кустаря по ней выходит самой заманчивой в списке. Вот
    только сырья для него на прилавке нет ни унции: инструменты и оружие не
    сделать без стали, а сталь не плавит никто. Пока это не учитывалось,
    кустари толпой уходили в ремёсла, которых физически не могли исполнить, —
    в живой партии до 87% сословия стояло на инструментах и оружии, — не
    зарабатывали ничего и вымирали: за две сотни пейдеев от трети населения
    оставались единицы. А вместе с кустарями пропадала ткань и одежда, которые
    делать было больше некому: хлопок горами лежал на складах при дефиците
    одежды в 99%.

    Мерка простая и не требует знать, кто чем уже занят: сколько человек можно
    занять этим ремеслом на то сырьё, что лежит на прилавке. Хватает на всё
    сословие — единица, нет сырья вовсе — ноль. Ремесло без сырья (лес, хлопок,
    зерно) исполнимо всегда: там нужны только руки.

    Побочный, но важный эффект — цепочка поднимается сама собой, снизу вверх:
    пока ткани нет, шить не из чего, и кустари берутся ткать; появилась
    ткань — открывается шитьё.
    """
    spec = crafts.get(craft)
    if spec is None:
        return 0.0
    if not spec["inputs"] or people <= EPS:
        return 1.0
    worst = 1.0
    for key, per_unit in spec["inputs"].items():
        local = city.goods.get(key)
        if local is None:
            return 0.0
        # сколько сырья ушло бы, возьмись за это ремесло всё сословие
        need = people * spec["out"] * per_unit
        if need <= EPS:
            continue
        worst = min(worst, max(0.0, local.stock) / need)
    return min(1.0, worst)


def plan_artisans(world: World, country: Country, city: City, mix: dict[str, float],
                  crafts: dict[str, dict]) -> dict:
    """Что кустари намерены сделать за пейдей и сколько сырья для этого нужно."""
    st = city.s("artisans")
    plan: dict[str, dict] = {}
    if st.people <= EPS:
        return plan

    # Сперва полный замысел, и только потом он целиком ужимается под кассу.
    # Раньше деньги раздавались по ходу обхода, и ремесло, стоявшее в списке
    # отраслей раньше, выгребало кассу подчистую, а последним не доставалось
    # ничего: кто из кустарей будет при деле, решал порядок словаря, а не
    # выгода. Мебельщики с ткачами оставались без сырья просто потому, что
    # «Мебельная фабрика» описана ниже «Фермы».
    draft: dict[str, dict] = {}
    total_cost = 0.0
    for craft, share in mix.items():
        spec = crafts.get(craft)
        if spec is None or share <= EPS:
            continue
        people = st.people * share
        if people <= EPS:
            continue
        planned = people * spec["out"] * glut_scale(lg(city, craft))
        if planned <= EPS:
            continue
        cost = sum(planned * per_unit * lg(city, k).price
                   for k, per_unit in spec["inputs"].items())
        draft[craft] = {"planned": planned, "spec": spec, "cost": cost}
        total_cost += cost

    # кустарь не покупает в долг: план ограничен его же деньгами. Ремесло, на
    # которое сырья покупать не надо (лес, хлопок, зерно), безденежьем не
    # ограничено — там нужны только руки.
    budget = max(0.0, st.cash)
    scale = 1.0 if total_cost <= budget + EPS else max(0.0, budget / total_cost)
    for craft, item in draft.items():
        planned = item["planned"] * (scale if item["cost"] > EPS else 1.0)
        if planned <= EPS:
            continue
        plan[craft] = {
            "planned": planned,
            "inputs": {k: planned * per_unit
                       for k, per_unit in item["spec"]["inputs"].items()},
        }
    return plan


def execute_artisans(country: Country, city: City, market, plan: dict,
                     ration: dict[str, float]) -> float:
    """Закупить сырьё по общему рациону, отработать и выставить товар."""
    st = city.s("artisans")
    if not plan or st.people <= EPS:
        return 0.0

    owner = stratum_owner_id(city.id, "artisans")
    spent = 0.0
    for craft, item in plan.items():
        fill = 1.0
        for k, want in item["inputs"].items():
            if want > EPS:
                fill = min(fill, ration.get(k, 0.0))
        fill = max(0.0, min(1.0, fill))

        for k, want in item["inputs"].items():
            got = market.buy(k, want * fill, lg(city, k).price)
            spent += got * lg(city, k).price

        out = item["planned"] * fill
        if out > EPS:
            market.deposit(craft, owner, out)

    st.cash = max(0.0, st.cash - spent)
    return spent


def artisan_mix(world: World, country: Country, city: City,
                previous: dict[str, float]) -> dict[str, float]:
    """Во что кустари вкладываются в этот пейдей."""
    crafts = artisan_crafts(world)
    keys = list(crafts)
    if not keys:
        return {}

    base = 1.0 / len(keys)
    prev = {k: max(previous.get(k, 0.0), 0.0) for k in keys}
    if sum(prev.values()) <= EPS:
        prev = {k: base for k in keys}

    # Тянет к ремеслу выгода, но только к тому, которое можно ИСПОЛНИТЬ: маржа
    # взвешивается тем, на скольких человек хватит сырья (см. craft_feasibility).
    # Без этого веса сословие уходило в ремёсла, для которых на рынке нет сырья,
    # и вымирало от бескормицы.
    people = city.s("artisans").people
    margins = {c: max(craft_margin(city, c, crafts), 0.0)
               * craft_feasibility(city, c, crafts, people) for c in keys}
    total = sum(margins.values())
    if total <= EPS:
        target = {c: base for c in keys}
    else:
        target = {c: m / total for c, m in margins.items()}

    s = config.ARTISAN_SWITCH_SPEED
    mix = {c: prev[c] * (1 - s) + target[c] * s for c in keys}
    norm = sum(mix.values()) or 1.0
    return {c: v / norm for c, v in mix.items()}


# ---------------------------------------------------------------------------
# Потребление
# ---------------------------------------------------------------------------
def stratum_basket(key: str) -> dict[str, dict]:
    """Корзина сословия: общая для всех плюс его собственные нужды.

    Солдату сверх обычного набора нужно оружие — оно изнашивается и теряется.
    Эта надбавка не входит в расчёт довольства (жизненный уровень меряется
    общей корзиной), зато определяет, насколько армия вооружена.
    """
    extra = config.STRATA_EXTRA_BASKET.get(key)
    if not extra:
        return config.CONSUMPTION_BASKET
    merged = dict(config.CONSUMPTION_BASKET)
    merged.update(extra)
    return merged


def normal_basket_value(city: City, key: str) -> float:
    """Стоимость полной обычной корзины сословия по ЯКОРНЫМ ценам.

    Это эталон уровня жизни: потребил на столько — живёшь как положено, то
    есть ровно на 1.0. Якорь вместо рыночной цены взят намеренно — иначе
    уровень жизни двигался бы от одной лишь инфляции, хотя человек ест ровно
    то же самое.
    """
    level = config.STRATA[key]["level"]
    total = 0.0
    for g, spec in config.CONSUMPTION_BASKET.items():
        local = city.goods.get(g)
        if local is not None:
            total += spec["qty"] * level * local.anchor
    return max(total, EPS)


def target_expectation(st, purchase_value: float) -> float:
    """К какому уровню жизни сословие стремится при своих сбережениях.

    Сытость прошлого пейдея тут ни при чём: ожидания растит достаток.
    Сословие, у которого на счетах лежит много корзин, начинает считать
    нормой жить лучше — и наоборот.

    Достаток меряется тем, что сословию РЕАЛЬНО НАДО КУПИТЬ, а не полной
    корзиной: крестьянин, который ест со своего поля, богат уже тем, что ему
    не нужно покупать хлеб, — и именно поэтому у него раньше прочих заводятся
    деньги на мясо.
    """
    if st.people <= EPS or purchase_value <= EPS:
        return 1.0
    depth = max(st.cash, 0.0) / (st.people * purchase_value)
    target = 1.0 + config.WEALTH_ELASTICITY * math.log(
        max(depth / config.TARGET_WEALTH_TICKS, 1e-3))
    return max(config.EXPECTATION_MIN, min(config.EXPECTATION_MAX, target))


def luxury_share(expectation: float, unlock: float) -> float:
    """Какая доля роскошного товара уже вошла в привычку при таких ожиданиях."""
    if expectation <= unlock:
        return 0.0
    return min(1.0, (expectation - unlock) / config.LUXURY_RAMP)


def consume(world: World, country: Country, city: City, market, own_food: float) -> dict[str, dict]:
    """Каждое сословие тратит свои деньги по приоритету потребностей.

    Порядок трат — по ступеням: сперва еда, потом обиход, потом дорогие вещи и
    только в самом конце роскошь. Разбогатевшее сословие поднимает ожидания:
    берёт больше обычного (той же еды) и постепенно добавляет мясо, вино,
    хорошее платье, обстановку и оперу. Сколько корзин оно в итоге потребило —
    и есть его уровень жизни.
    """
    result: dict[str, dict] = {}

    for key in config.STRATA_ORDER:
        basket = stratum_basket(key)
        st = city.s(key)
        if st.people <= EPS:
            st.consumed = {}
            result[key] = {"fill": {}, "spent": 0.0, "sol": 0.0}
            continue
        level = config.STRATA[key]["level"]

        # Базовая потребность — без надбавки за ожидания. Довольство меряется
        # именно ею: разбогатевший человек хочет больше, но нельзя, чтобы от
        # роста аппетита он становился НЕСЧАСТНЕЕ, покупая при этом больше
        # прежнего. Излишек сверх базы уходит в уровень жизни, а не в упрёк.
        base_needs = {g: st.people * spec["qty"] * level
                      for g, spec in basket.items()}

        # крестьяне уже поели со своего поля — покупать им столько не придётся
        covered_food = (min(own_food, base_needs.get("food", 0.0))
                        if key == "peasants" else 0.0)
        purchase_value = sum(
            base_needs[g] * lg(city, g).anchor for g in base_needs
            if g in city.goods) - covered_food * lg(city, "food").anchor

        # --- ожидания: медленно ползут за достатком -----------------------
        target = target_expectation(st, max(purchase_value, EPS) / st.people)
        inertia = (config.EXPECTATION_RISE_INERTIA if target > st.expectation
                   else config.EXPECTATION_FALL_INERTIA)
        st.expectation = st.expectation * inertia + target * (1.0 - inertia)
        expectation = st.expectation

        # --- чего хотим: обычная корзина × ожидания + открывшаяся роскошь ---
        # Ожидание безразмерно и само служит множителем: 1.0 — обычная корзина.
        appetite = max(config.DEMAND_SCALE_MIN,
                       min(config.DEMAND_SCALE_MAX, expectation))
        needs = {g: base_needs[g] * appetite for g in base_needs}
        tiers = {g: spec["tier"] for g, spec in basket.items()}
        elasticity = {g: spec["elasticity"] for g, spec in basket.items()}
        top_share = 0.0
        for g, spec in config.LUXURY_BASKET.items():
            share = luxury_share(expectation, spec["unlock"])
            if share <= EPS or g not in city.goods:
                continue
            needs[g] = st.people * spec["qty"] * level * share
            tiers[g] = 4                     # роскошь идёт после всего прочего
            elasticity[g] = spec["elasticity"]
            top_share = max(top_share, share)

        # своё съеденное покупать не надо (covered_food посчитан выше)
        market_needs = dict(needs)
        market_needs["food"] = max(0.0, needs.get("food", 0.0) - covered_food)

        wanted = {}
        for g, qty in market_needs.items():
            local = city.goods.get(g)
            if local is None or qty <= EPS:
                continue
            wanted[g] = demand_response(qty, local.price, local.anchor,
                                        elasticity[g])

        # --- бюджет по ступеням -------------------------------------------
        # Хлеб идёт первым и неприкосновенен. Дальше, если роскошь уже вошла в
        # привычку, под неё бронируется доля оставшегося: иначе её не купили бы
        # никогда — обычная корзина в этой экономике дороже, чем люди могут
        # себе позволить, и «на сдачу» ничего не остаётся. От вина отказываются
        # не в пользу еды, а в пользу новых инструментов.
        spendable = max(0.0, st.cash) * (1.0 - config.SAVINGS_RATE)
        plan: dict[str, float] = {}

        def spend(tier: int, budget: float) -> float:
            keys = [g for g in wanted if tiers[g] == tier]
            cost = sum(wanted[g] * lg(city, g).price for g in keys)
            if cost <= EPS:
                return budget
            f = 1.0 if budget >= cost else budget / cost
            for g in keys:
                plan[g] = wanted[g] * f
            return max(0.0, budget - cost * f)

        spendable = spend(1, spendable)
        reserve = spendable * min(config.LUXURY_BUDGET_CAP,
                                  config.LUXURY_BUDGET_WEIGHT * top_share)
        spendable -= reserve
        for tier in (2, 3):
            spendable = spend(tier, spendable)
        spendable = spend(4, reserve + spendable)

        fill: dict[str, float] = {}
        for g, base_qty in base_needs.items():
            got = covered_food if g == "food" else 0.0
            fill[g] = min(1.0, got / base_qty) if base_qty > EPS else 1.0

        paid = 0.0
        # Ценность потреблённого меряем в ценах ЯКОРЯ, а не рынка: иначе
        # уровень жизни прыгал бы от одной только инфляции, хотя человек ест
        # ровно то же самое.
        consumed_value = covered_food * lg(city, "food").anchor
        # Расшифровка уровня жизни: что и сколько сословие взяло за этот пейдей.
        taken: dict[str, float] = {}
        if covered_food > EPS:
            taken["food"] = covered_food        # своё, с поля, а не с рынка
        for g, want in plan.items():
            price = lg(city, g).price
            # Акциз ложится только на роскошь: мясо, вино, дорогое платье,
            # обстановку и оперу. Хлеб бедняка он не трогает вовсе — в этом
            # весь смысл отдельной ставки.
            excise = country.excise_tax if g in config.LUXURY_BASKET else 0.0
            bought = market.buy(g, want, price, sales_tax=country.sales_tax,
                                excise=excise)
            paid += bought * price
            consumed_value += bought * lg(city, g).anchor
            if bought > EPS:
                taken[g] = taken.get(g, 0.0) + bought
            base_qty = base_needs.get(g, 0.0)
            got = bought + (covered_food if g == "food" else 0.0)
            fill[g] = min(1.0, got / base_qty) if base_qty > EPS else 1.0
        st.consumed = taken

        # --- уровень жизни: во сколько обычных корзин вышло -----------------
        sol = consumed_value / (st.people * normal_basket_value(city, key))
        i = config.LIVING_STANDARD_INERTIA
        st.living_standard = st.living_standard * i + sol * (1.0 - i)

        st.cash = max(0.0, st.cash - paid)
        st.spent = paid
        result[key] = {"fill": fill, "spent": paid, "plan": plan,
                       "sol": st.living_standard, "expectation": expectation}

    return result


def satisfaction_score(fill: dict[str, float], unemployment: float,
                       living_standard: float = 1.0) -> float:
    """Довольство: закрыты ли базовые нужды и как хорошо живётся сверх того.

    Основа — по-прежнему ступени необходимого: голодного не утешит опера.
    Но жизнь заметно выше нормы и сама по себе радует, и смягчает удары —
    безработицу и мобилизацию переносить на сытый желудок куда легче. Отсюда и
    берётся возможность сытой страны держать армию больше: люди терпят.
    """
    basket = config.CONSUMPTION_BASKET
    score = 0.0
    for tier, weight in config.TIER_WEIGHTS.items():
        keys = [g for g in basket if basket[g]["tier"] == tier]
        vals = [fill.get(g, 0.0) for g in keys]
        score += weight * (sum(vals) / len(vals) if vals else 0.0)

    surplus = prosperity(living_standard)
    score += config.SOL_SATISFACTION_BONUS * surplus
    # Нужда бьёт по довольству напрямую и сильнее, чем радует достаток:
    # привыкнуть к хорошему легко, терпеть нищету — тяжело. Именно отсюда
    # растут бунты.
    score -= config.SOL_MISERY_PENALTY * misery(living_standard)
    hardship = 0.35 * unemployment * (1.0 - config.SOL_HARDSHIP_CUSHION * surplus)
    return max(0.0, min(1.0, score * (1.0 - hardship)))


def misery(living_standard: float) -> float:
    """Насколько сословие бедствует, в долях от 0 до 1.

    0 — на жизнь хватает, 1 — беспросветная нужда. Это и есть топливо бунтов.
    """
    return max(0.0, min(1.0, (config.SOL_MISERY_AT - living_standard)
                        / config.SOL_MISERY_SPAN))


def prosperity(living_standard: float) -> float:
    """Насколько сословие живёт в достатке, в долях от 0 до 1.

    0 — живёт скудно, 1 — достаток, дальше которого прибавки уже нет. Одна
    мера на всё: и прибавка к довольству, и стойкость к мобилизации, и потолок
    армии считаются от неё.
    """
    return max(0.0, min(1.0, (living_standard - config.SOL_PROSPER_AT)
                        / config.SOL_SATISFACTION_SPAN))


# ---------------------------------------------------------------------------
# Мобильность сословий
# ---------------------------------------------------------------------------
def move_people(src, dst, count: float) -> float:
    """Перевести людей вместе с их сбережениями."""
    count = max(0.0, min(count, src.people))
    if count <= EPS:
        return 0.0
    share = count / src.people
    cash = src.cash * share
    src.people -= count
    src.cash -= cash
    dst.people += count
    dst.cash += cash
    return count


def recruit_workers(world: World, country: Country, city: City) -> float:
    """Заводы переманивают людей зарплатой. Возвращает, сколько пришло."""
    buildings = [b for b in world.city_buildings(city.id)
                 if b.active and b.effective_level > 0 and b.throttle > EPS]
    if not buildings:
        return 0.0

    openings = sum(b.effective_level * world.industries[b.industry_key].jobs_per_level
                   * max(0.0, min(1.0, b.throttle)) for b in buildings)
    workers = city.s("workers")
    deficit = openings - workers.people
    if deficit <= EPS:
        return 0.0

    best_wage = max(max(b.wage, country.min_wage) for b in buildings)
    came = 0.0
    for key in config.LABOUR_POOL:
        st = city.s(key)
        if st.people <= EPS:
            continue
        # доход, от которого человек отказывается, уходя на завод
        alternative = max(st.income_per_capita, country.min_wage * 0.5)
        if best_wage < alternative * config.CONVERSION_WAGE_EDGE:
            continue
        eagerness = min(1.0, best_wage / max(alternative, EPS)
                        / config.CONVERSION_WAGE_EDGE - 1.0 + 0.35)
        limit = st.people * config.CONVERSION_MAX_SHARE * eagerness
        came += move_people(st, workers, min(limit, deficit - came))
        if deficit - came <= EPS:
            break
    return came


def drift_and_switch(world: World, city: City, employed: float) -> None:
    """Безработица, возврат к земле и переход между деревенскими сословиями."""
    workers = city.s("workers")
    idle = max(0.0, workers.people - employed)

    if workers.people > EPS and idle / workers.people > 0.02:
        workers.idle_streak += 1.0
    else:
        workers.idle_streak = max(0.0, workers.idle_streak - 1.5)

    if workers.idle_streak >= config.IDLE_TICKS_BEFORE_DRIFT and idle > EPS:
        move_people(workers, city.s("town_low"), idle * config.IDLE_DRIFT_SHARE)

    # Деревня выбирает между полем и ремеслом по тому, КАК ЖИВЁТСЯ, а не по
    # тому, сколько денег прошло через руки. Разница принципиальная: крестьянин
    # ест со своего поля и потому при вдвое меньшем денежном доходе живёт
    # заметно сытнее кустаря, которому хлеб приходится покупать. Пока
    # сравнивали денежные доходы, кустарь мог «выигрывать» у крестьянина,
    # сидя при этом впроголодь.
    # Идут в ремесло на заработок, а бегут из него от голода — и это две разные
    # мерки. Деньгами ремесло манит: у кустаря на руках живые червонцы, каких у
    # крестьянина нет. Зато прокормиться на них труднее, и когда ремесло
    # перестаёт кормить, уходят обратно к земле, сколько бы оно ни платило.
    # Две мерки дают равновесие: опустеют мастерские — вырастет маржа
    # оставшихся, и деревня потянется обратно.
    peasants, artisans = city.s("peasants"), city.s("artisans")
    pi, ai = peasants.income_per_capita, artisans.income_per_capita
    ps, as_ = peasants.living_standard, artisans.living_standard
    if ai > pi * 1.12 and peasants.people > EPS:
        move_people(peasants, artisans, peasants.people * config.CRAFT_SWITCH_SHARE)
    elif ps > as_ * 1.12 and artisans.people > EPS:
        # Ремесло, которое не кормит, пустеет тем быстрее, чем хуже кустарю
        # живётся и чем сытнее на земле. Уходят к земле: там он хотя бы сыт.
        pull = min(1.0, (ps - as_) / max(ps, EPS))
        share = (config.CRAFT_SWITCH_SHARE
                 + config.CRAFT_FLEE_SHARE * pull * misery(as_))
        move_people(artisans, peasants, artisans.people * share)


# ---------------------------------------------------------------------------
# Армия
# ---------------------------------------------------------------------------
def country_cities(world: World, country: Country) -> list[City]:
    return [c for c in world.cities.values() if c.country_id == country.id]


def army_size(world: World, country: Country) -> float:
    return sum(c.s("soldiers").people for c in country_cities(world, country))


def region_living_standard(city: City) -> float:
    """Средний уровень жизни области, взвешенный по числу людей."""
    num = den = 0.0
    for key in config.STRATA_ORDER:
        st = city.s(key)
        if st.people > EPS:
            num += st.living_standard * st.people
            den += st.people
    return num / den if den > EPS else 1.0


def country_living_standard(world: World, country: Country) -> float:
    """Средний уровень жизни в стране, взвешенный по числу людей."""
    num = den = 0.0
    for city in country_cities(world, country):
        for key in config.STRATA_ORDER:
            st = city.s(key)
            if st.people > EPS:
                num += st.living_standard * st.people
                den += st.people
    return num / den if den > EPS else 1.0


def army_cap_share(world: World, country: Country) -> float:
    """Какую долю населения страна вообще способна поставить под ружьё.

    Чем сытнее живут люди, тем больше их можно оторвать от хозяйства, не
    развалив его: богатое общество терпит призыв заметно легче бедного.
    """
    bonus = config.ARMY_SOL_BONUS * prosperity(country_living_standard(world, country))
    return config.ARMY_TARGET_MAX * (1.0 + bonus)


def affordable_army(world: World, country: Country) -> float:
    """Сколько солдат страна может содержать на свой военный бюджет.

    Численность армии определяется не желанием лидера, а арифметикой:
    твёрдая сумма бюджета, делённая на ставку жалованья. Сверху — потолок по
    доле населения, который растёт вместе с уровнем жизни.
    """
    if country.soldier_pay <= EPS or country.army_budget <= EPS:
        return 0.0
    pop = sum(c.population for c in country_cities(world, country))
    return min(country.army_budget / country.soldier_pay,
               pop * army_cap_share(world, country))


def pay_army(world: World, country: Country) -> float:
    """Жалованье солдатам — отдельная статья казны, до всех прочих расходов.

    Платят не больше военного бюджета: лишние солдаты остаются без жалованья и
    разбегаются по домам. Так армия сама сходится к тому размеру, который
    страна тянет, и казна не уходит в минус.
    """
    cities = country_cities(world, country)
    soldiers = sum(c.s("soldiers").people for c in cities)
    if soldiers <= EPS or country.soldier_pay <= EPS:
        country.last_army_cost = 0.0
        return 0.0

    obligation = soldiers * country.soldier_pay
    want = min(obligation, max(0.0, country.army_budget))
    paid = min(want, max(0.0, country.treasury))
    country.spend("army_pay", paid)
    country.last_army_cost = paid
    # Доля закрытого жалованья считается от полного долга перед армией, а не
    # от урезанного бюджета: солдат сверх бюджета — это тоже недоплаченный
    # солдат, и он уходит домой.
    ratio = paid / obligation if obligation > EPS else 1.0

    for city in cities:
        st = city.s("soldiers")
        if st.people <= EPS:
            continue
        share = st.people / soldiers
        st.cash += paid * share
        st.income += paid * share

    if ratio < 0.999:
        unpaid = 1.0 - ratio
        for city in cities:
            st = city.s("soldiers")
            move_people(st, city.s("town_low"),
                        st.people * unpaid * config.ARMY_DESERTION_SHARE)
    return paid


def conscript(world: World, country: Country) -> float:
    """Довести армию до численности, которую оплачивает военный бюджет.

    Набирают ровно столько, на сколько хватает твёрдой суммы бюджета при
    текущей ставке жалованья, — и ни человеком больше. Призыв идёт постепенно
    и только если жалованье не хуже привычного дохода: добровольца силой не
    гонят, солдат — такое же занятие, как завод.

    Возвращает изменение численности (плюс — призвали, минус — распустили).
    """
    cities = country_cities(world, country)
    pop = sum(c.population for c in cities)
    if pop <= EPS:
        return 0.0
    soldiers = sum(c.s("soldiers").people for c in cities)
    target = affordable_army(world, country)
    gap = target - soldiers

    if gap > EPS:
        came = 0.0
        for city in cities:
            need = gap * (city.population / pop)
            for key in config.ARMY_POOL:
                if need <= EPS:
                    break
                st = city.s(key)
                if st.people <= EPS:
                    continue
                alternative = max(st.income_per_capita, country.min_wage * 0.5)
                if country.soldier_pay < alternative * config.ARMY_PAY_EDGE:
                    continue
                limit = st.people * config.ARMY_RECRUIT_SHARE
                moved = move_people(st, city.s("soldiers"), min(limit, need))
                need -= moved
                came += moved
        return came

    if gap < -EPS:
        gone = 0.0
        for city in cities:
            st = city.s("soldiers")
            if st.people <= EPS:
                continue
            excess = -gap * (city.population / pop)
            gone += move_people(st, city.s("town_low"),
                                min(excess, st.people * config.ARMY_DISCHARGE_SHARE))
        return -gone
    return 0.0


def mobilize(world: World, country: Country) -> float:
    """Насильный призыв: людей забирают приказом, а не жалованьем.

    Отличий от обычного призыва три. Во-первых, не смотрят на доход: под ружьё
    идут и те, кому служба невыгодна. Во-вторых, выгребают в том числе рабочих
    прямо с заводов — цеха останутся недоукомплектованными, и промышленность
    просядет. В-третьих, доля берётся случайно по каждому сословию и городу:
    где-то заберут почти никого, где-то выметут подчистую.

    Плата за это — падение довольства во всей стране на всё время мобилизации
    (см. MOBILIZATION_DISCONTENT в engine.run_country).
    """
    country.last_mobilized = 0.0
    if country.mobilization_left <= 0:
        return 0.0

    cities = country_cities(world, country)
    pop = sum(c.population for c in cities)
    if pop <= EPS:
        return 0.0

    soldiers = sum(c.s("soldiers").people for c in cities)
    # Приказ позволяет перебрать сверх оплаченного, но не бесконечно: лишние
    # солдаты останутся без жалованья и разбегутся сами.
    ceiling = min(affordable_army(world, country) * config.MOBILIZATION_TARGET_BONUS,
                  pop * army_cap_share(world, country))
    room = ceiling - soldiers
    if room <= EPS:
        return 0.0

    taken = 0.0
    for city in cities:
        r = rng_for(world, city.id, salt=57)
        for key in config.MOBILIZATION_POOL:
            if room - taken <= EPS:
                break
            st = city.s(key)
            if st.people <= EPS:
                continue
            share = r.uniform(config.MOBILIZATION_SHARE_MIN,
                              config.MOBILIZATION_SHARE_MAX)
            want = min(st.people * share, room - taken)
            taken += move_people(st, city.s("soldiers"), want)

    country.last_mobilized = taken
    return taken


def restock_shells(world: World, country: Country, city: City, market,
                   share: float = 1.0) -> float:
    """Казна закупает снаряды в армейский резерв на рынке одной области.

    Именно здесь у снарядного завода появляется покупатель: армия сама на
    рынок не ходит, за неё платит государство. Заказ делится между областями
    страны пропорционально их населению — армия снабжается отовсюду.
    """
    gap = shells_gap(world, country)
    if gap <= EPS or share <= EPS:
        return 0.0

    want = gap * config.SHELLS_RESTOCK_SHARE * share
    price = lg(city, "shells").price
    if price > EPS:
        want = min(want, max(0.0, country.treasury) / price)
    if want <= EPS:
        return 0.0

    got = market.buy("shells", want, price)
    country.spend("army_supply", got * price)
    country.army_shells += got
    country.last_shells_bought += got
    return got


def shells_gap(world: World, country: Country) -> float:
    """Насколько армейский резерв снарядов ниже целевого."""
    target = army_size(world, country) * config.SHELLS_RESERVE_PER_SOLDIER
    return max(0.0, target - country.army_shells)


def shells_wanted(world: World, country: Country) -> float:
    """Сколько снарядов казна хотела бы купить — для расчёта спроса и цены."""
    return shells_gap(world, country) * config.SHELLS_RESTOCK_SHARE


# --- оружие: запас на складе и его расход ---------------------------------
# Две разные величины, и в этом весь смысл разделения (см. WEAPONS_* в config):
# weapons_target/army_equip — ВООРУЖЁННОСТЬ, состояние армии, решает исход боя;
# weapons_wanted            — ПОТРЕБЛЕНИЕ, спрос на рынке, двигает цену.

def weapons_target(world: World, country: Country) -> float:
    """Сколько оружия армии положено по штату."""
    return army_size(world, country) * config.WEAPONS_PER_SOLDIER


def weapons_gap(world: World, country: Country) -> float:
    """Насколько арсенал ниже штатного."""
    return max(0.0, weapons_target(world, country) - country.army_weapons)


def weapons_wear(world: World, country: Country) -> float:
    """Износ за пейдей: сломанное, потерянное и разворованное.

    Не зависит от того, полон арсенал или пуст: изнашивается то оружие, что
    на руках. Именно этот расход и кормит оружейный завод в мирный год —
    без него вооружённая армия перестала бы покупать вовсе.
    """
    held = min(country.army_weapons, weapons_target(world, country))
    return max(0.0, held) * config.WEAPONS_WEAR


def weapons_wanted(world: World, country: Country) -> float:
    """Спрос казны на оружие за пейдей: износ плюс закрытие недостачи.

    Полностью вооружённая армия покупает только на замену изношенному,
    недовооружённая — кратно больше. Отсюда цена и реагирует разом и на
    вооружённость, и на потребление.
    """
    return (weapons_wear(world, country)
            + weapons_gap(world, country) * config.WEAPONS_RESTOCK_SHARE)


def restock_weapons(world: World, country: Country, city: City, market,
                    share: float = 1.0) -> float:
    """Казна докупает оружие в арсенал на рынке одной области.

    Здесь у оружейного завода и появляется покупатель с настоящими деньгами.
    Раньше им был карман солдата, которому одной винтовки стоило дороже двух
    жалований, — потому завод и не окупался.
    """
    want = weapons_wanted(world, country) * share
    if want <= EPS:
        return 0.0

    price = lg(city, "weapons").price
    if price > EPS:
        want = min(want, max(0.0, country.treasury) / price)
    if want <= EPS:
        return 0.0

    got = market.buy("weapons", want, price)
    country.spend("army_supply", got * price)
    country.army_weapons += got
    country.last_weapons_bought += got
    return got


def wear_weapons(world: World, country: Country) -> float:
    """Списать износ арсенала. Вызывается раз за пейдей, до закупки."""
    worn = min(weapons_wear(world, country), country.army_weapons)
    country.army_weapons = max(0.0, country.army_weapons - worn)
    country.last_weapons_worn = worn
    return worn


def update_army_equip(world: World, country: Country) -> None:
    """Пересчитать вооружённость из арсенала.

    Ни сглаживания, ни памяти о прошлом тике здесь не нужно: запас на складе
    сам по себе величина медленная. Раньше сглаживали как раз потому, что
    мерили покупки за пейдей, — и всё равно армия «разоружалась» в любой
    бедный пейдей.
    """
    target = weapons_target(world, country)
    country.army_equip = (0.0 if target <= EPS
                          else max(0.0, min(1.0, country.army_weapons / target)))


def army_strength(world: World, country: Country) -> float:
    """Боевая сила армии: люди × вооружение × снабжение × дух.

    Толпа без оружия и снарядов почти ничего не стоит — отсюда и смысл всей
    оружейной промышленности.
    """
    cities = country_cities(world, country)
    soldiers = sum(c.s("soldiers").people for c in cities)
    if soldiers <= EPS:
        return 0.0
    morale = (sum(c.s("soldiers").satisfaction * c.s("soldiers").people
                  for c in cities) / soldiers) if soldiers > EPS else 0.0
    equip = config.EQUIP_STRENGTH_MIN + (1.0 - config.EQUIP_STRENGTH_MIN) * \
        max(0.0, min(1.0, country.army_equip))
    need = soldiers * config.SHELLS_PER_SOLDIER_BATTLE
    supply = 1.0 if need <= EPS else min(1.0, country.army_shells / need)
    supply = config.SUPPLY_STRENGTH_MIN + (1.0 - config.SUPPLY_STRENGTH_MIN) * supply
    return soldiers * equip * supply * (0.6 + 0.6 * morale)


def demography(world: World, city: City) -> None:
    """Рождаемость и смертность по сословиям — каждое по своему довольству."""
    n = config.SATISFACTION_NEUTRAL
    for key in config.STRATA_ORDER:
        st = city.s(key)
        if st.people <= EPS:
            continue
        if st.satisfaction >= n:
            rate = config.POP_GROWTH_MAX * (st.satisfaction - n) / (1.0 - n)
        else:
            rate = config.POP_DECLINE_MAX * (n - st.satisfaction) / n
        st.people = max(0.0, st.people * (1.0 + rate))


# ---------------------------------------------------------------------------
def reference_wage(world: World, country: Country) -> float:
    """Во сколько в этом государстве обходится труд одного человека за пейдей.

    Логика найма (п.1): если в стране есть свободные рабочие, они готовы идти
    на завод при любой зарплате — поэтому опорная ставка определяется только
    МРОТ и доходами деревни, а зарплаты уже работающих заводов НЕ учитываются
    (они заняты своими людьми). Только когда свободных рабочих нет и рабочих
    приходится переманивать у земли, в ставку входит конкуренция заводов.
    """
    base = country.min_wage
    # доходы деревни в городах этой страны
    village = 0.0
    for city in world.cities.values():
        if city.country_id != country.id:
            continue
        for key in ("peasants", "artisans"):
            st = city.s(key)
            if st.people > 1:
                village = max(village, st.income_per_capita)
    base = max(base, village)

    # есть ли свободные рабочие (не занятые на заводах) в стране?
    total_workers = 0.0
    total_employed = 0.0
    wages = []
    for city in world.cities.values():
        if city.country_id != country.id:
            continue
        w = city.s("workers").people
        total_workers += w
        for b in world.city_buildings(city.id):
            total_employed += b.employed
            if b.employed > 1:
                wages.append(b.wage)
    free_workers = max(0.0, total_workers - total_employed)

    # нет свободных рабочих — переманиваем у земли, заводы конкурируют зп
    if free_workers <= EPS and wages:
        base = max(base, sum(wages) / len(wages))

    # На нулевом пейдее доходов ещё никто не получал: ориентир — стоимость
    # корзины, иначе первого завода не нанять никого.
    #
    # Условие тут именно «деревня НИЧЕГО не заработала», а не «заработала
    # меньше МРОТ». Со вторым запасной путь срабатывал в любой неурожайный
    # пейдей: ставка прыгала с пяти червонцев на полсотни, за ней подскакивала
    # расчётная себестоимость всего деревенского, а с нею якорь и цены — и
    # экономику трясло на ровном месте.
    if village <= EPS:
        base = max(base, max((material_basket_cost(c)
                              for c in country_cities(world, country)),
                             default=0.0))
    return max(base, 0.01)


def notional_unit_cost(world: World, city: City, key: str, wage: float) -> float | None:
    """Во сколько обошёлся бы товар, если бы его кто-то делал.

    Считается ровно по рецепту отрасли и в правильных единицах, а это разные
    единицы: зарплата и содержание идут НА РАБОТНИКА, а сырьё — НА ЕДИНИЦУ
    ВЫПУСКА. Значит, делить на выработку надо только труд с содержанием, а
    стоимость сырья прибавляется как есть.

    Прежняя формула делила наоборот, и промахивалась тем сильнее, чем больше
    сырья съедает рецепт: сталь по ней выходила вдвое дешевле настоящей, а
    вместе с себестоимостью занижались и якорь, и коридор цены, и «наценка ×N»
    в интерфейсе. Игрок видел на рынке ×1.0, строил завод — и тот уходил в
    минус, потому что настоящая себестоимость была вдвое выше показанной.
    """
    if key in config.PEASANT_YIELD:
        share = config.PEASANT_EFFORT.get(key, 0.25)
        return wage * share / config.PEASANT_YIELD[key]

    for ind in world.industries.values():
        if ind.output_good != key:
            continue
        inputs = sum(q * lg(city, g).price for g, q in ind.inputs.items())
        upkeep = config.UPKEEP_PER_LEVEL / max(ind.jobs_per_level, 1)
        per_worker = max(ind.output_per_worker, EPS)
        return (wage + upkeep) / per_worker + inputs
    return None


def update_service_cost(city: City) -> None:
    """Себестоимость услуг привязана к стоимости материальной корзины области."""
    for key in ("services", "luxury_services"):
        local = city.goods.get(key)
        if local is None:
            continue
        mult = (config.SERVICE_REL_COST if key == "services"
                else config.SERVICE_REL_COST * config.LUXURY_SERVICE_MULT)
        local.unit_cost = max(0.05, material_basket_cost(city) * mult)
