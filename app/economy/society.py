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


def peasant_alternative(city: City) -> float:
    """Что даёт крестьянину СВОЙ надел за пейдей, в червонцах.

    Мерка для найма на чужое поле. Крестьянин не безработный, которому некуда
    деваться: своя земля кормит его и без хозяина, поэтому на ферму он выходит,
    только если там платят не меньше. Считается по текущим ценам области —
    подешевело зерно, и своё поле стало давать меньше, а работа по найму
    привлекательнее.
    """
    return sum(per_head * lg(city, key).price
               for key, per_head in config.PEASANT_YIELD.items())


def farm_hired(world: World, city: City) -> float:
    """Сколько крестьян области занято на чужих полях в этот пейдей."""
    total = 0.0
    for b in world.city_buildings(city.id):
        ind = world.industries.get(b.industry_key)
        if ind is not None and ind.labour == "peasants":
            total += b.employed
    return total


def produce_peasants(world: World, country: Country, city: City, market) -> float:
    """Урожай со своих наделов, собственное потребление и излишек на рынок.

    Возвращает, сколько зерна крестьяне съели из своего урожая.

    **Занятый на ферме своё поле не пашет.** Иначе один и тот же человек
    снимал бы урожай дважды — и с хозяйской земли за зарплату, и со своей за
    хлеб, — а страна получала бы зерно из ниоткуда. От надела ему остаётся
    огород при избе (config.FARM_OWN_PLOT_LEFT), всё прочее он теперь покупает
    на рынке за деньги. В этом и весь смысл перемены: деревня перестаёт быть
    натуральной и становится покупателем.
    """
    st = city.s("peasants")
    if st.people <= EPS:
        city.harvest = 1.0
        return 0.0

    r = rng_for(world, city.id, salt=3)
    harvest = 1.0 + r.uniform(-config.HARVEST_SWING, config.HARVEST_SWING)
    city.harvest = harvest

    hired = min(farm_hired(world, city), st.people)
    # свободные пашут полностью, нанятые — только огород при избе
    hands = (st.people - hired) + hired * config.FARM_OWN_PLOT_LEFT

    owner = stratum_owner_id(city.id, "peasants")
    own_grain = 0.0
    for good_key, per_head in config.PEASANT_YIELD.items():
        qty = hands * per_head * harvest
        if good_key == "grain":
            # сначала едят сами, на рынок идёт только излишек. Едят при этом
            # ВСЕ крестьяне, включая нанятых, — просто нанятым своего хлеба уже
            # не хватает, и недостачу они докупают за зарплату.
            need = (st.people * config.CONSUMPTION_BASKET["grain"]["qty"]
                    * config.STRATA["peasants"]["level"])
            own_grain = min(qty, need)
            qty = max(0.0, qty - own_grain)
        if qty > EPS:
            market.deposit(good_key, owner, qty * glut_scale(lg(city, good_key)))
    return own_grain


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


def craft_ruin(peasants, artisans) -> float:
    """Насколько у ремесла ЗАБРАЛИ ЗАРАБОТОК, от 0 до 1.

    Мера денежная, и это принципиально. Пока ремесло живо, живых червонцев у
    кустаря заметно больше, чем у крестьянина: тот значительную часть хлеба
    съедает, а не продаёт. Так что превышение — норма, а вот провал денежного
    дохода НИЖЕ крестьянского означает ровно одно: сбыт ушёл. Ушёл он к тому,
    кто делает то же самое дешевле, — к заводу. Отсюда и глубина: заработок
    упал в ноль — ремесло разорено полностью.

    Мерить разорение сытостью нельзя, как бы ни просилось. Крестьянин ест со
    своего поля и живёт сытнее кустаря ПОЧТИ ВСЕГДА, безо всяких заводов, — по
    этой мерке мастерские пустели бы сами собой по всему миру, и страны без
    единого завода первыми остались бы без одежды, мебели и инструмента.
    Проверено прогоном: сословие уходило в деревню целиком, хлеб дешевел от
    лишних рук, а уровень жизни в мире падал с 0.41 до 0.25. У голода поэтому
    своя, вдевятеро меньшая доля — см. CRAFT_FLEE_HUNGER и drift_and_switch.

    Мера растёт КВАДРАТИЧНО, и это не украшение. Доход сословия скачет от
    пейдея к пейдею — урожай, цены, случай, — и мелкий провал ниже деревни
    случается сам собой, безо всяких заводов. При линейной мере такой провал на
    треть уже гнал из мастерских десятую часть людей за пейдей, и сословие
    выкашивало себя в первые же десять пейдеев мира, когда никаких заводов ещё
    нет вовсе. Квадрат оставляет мелкую рябь почти без последствий, а полный
    провал дохода в ноль — с полной силой.

    Считается по ДВУМ пейдеям — идущему и прошлому, — и берётся меньшая беда.
    Один пейдей ничего не доказывает: неурожай, случайная просадка цены, и
    деревня разом «богаче» мастерских. Разорение — это когда заработка нет и
    сегодня, и вчера.
    """
    if artisans.people <= EPS or peasants.people <= EPS:
        return 0.0
    edge = config.CRAFT_SWITCH_EDGE
    gap = 1.0
    for pi, ai in ((peasants.income_per_capita, artisans.income_per_capita),
                   (peasants.usual_income_per_capita,
                    artisans.usual_income_per_capita)):
        if pi <= EPS:
            return 0.0
        gap = min(gap, max(0.0, min(1.0, (pi - ai * edge) / pi)))
    return gap * gap


def craft_potential(world: World, city: City) -> float:
    """Сколько бы заработал кустарь при нынешних ценах — на человека за пейдей.

    Нужно там, где сравнивать не с чем: ремесла в области не осталось, доход
    его равен нулю, и по доходу деревню обратно к верстаку уже не позвать.
    Берётся лучшее из исполнимых ремёсел — то, за которое и взялись бы, — а
    исполнимость считается по той горстке людей, что придёт за один пейдей: на
    целое сословие сырья на прилавке, конечно, не хватит.
    """
    crafts = artisan_crafts(world)
    if not crafts:
        return 0.0
    newcomers = max(city.s("peasants").people * config.CRAFT_SWITCH_SHARE, 1.0)
    best = 0.0
    for craft in crafts:
        margin = craft_margin(city, craft, crafts)
        if margin <= best:
            continue
        if craft_feasibility(city, craft, crafts, newcomers) >= 1.0:
            best = margin
    return best


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


def consume(world: World, country: Country, city: City, market,
            own_grain: float) -> dict[str, dict]:
    """Каждое сословие тратит свои деньги по приоритету потребностей.

    Порядок трат — по ступеням: сперва хлеб, потом обиход, потом дорогие вещи и
    только в самом конце роскошь. Сама еда при этом тоже лестница: зерно (ступень
    1) даёт сытость за гроши, продукты (2) и продовольствие (3) — то же брюхо,
    но заметно лучшую жизнь, мясо — уже роскошь. Разбогатевшее сословие
    поднимает ожидания: берёт больше обычного и постепенно добавляет мясо, вино,
    хорошее платье, обстановку и оперу. Сколько корзин оно в итоге потребило —
    и есть его уровень жизни.

    `own_grain` — хлеб, который крестьяне сняли со своих наделов и съели, минуя
    рынок. Он такая же еда, как купленная, и в уровень жизни входит наравне.
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

        # крестьяне уже поели своего хлеба — покупать им столько не придётся
        covered_grain = (min(own_grain, base_needs.get("grain", 0.0))
                         if key == "peasants" else 0.0)
        purchase_value = sum(
            base_needs[g] * lg(city, g).anchor for g in base_needs
            if g in city.goods) - covered_grain * lg(city, "grain").anchor

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

        # своё съеденное покупать не надо (covered_grain посчитан выше)
        market_needs = dict(needs)
        market_needs["grain"] = max(0.0, needs.get("grain", 0.0) - covered_grain)

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
            got = covered_grain if g == "grain" else 0.0
            fill[g] = min(1.0, got / base_qty) if base_qty > EPS else 1.0

        paid = 0.0
        # Ценность потреблённого меряем в ценах ЯКОРЯ, а не рынка: иначе
        # уровень жизни прыгал бы от одной только инфляции, хотя человек ест
        # ровно то же самое.
        consumed_value = covered_grain * lg(city, "grain").anchor
        # Расшифровка уровня жизни: что и сколько сословие взяло за этот пейдей.
        taken: dict[str, float] = {}
        if covered_grain > EPS:
            taken["grain"] = covered_grain      # своё, с поля, а не с рынка
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
            got = bought + (covered_grain if g == "grain" else 0.0)
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
    """Заводы переманивают людей зарплатой. Возвращает, сколько пришло.

    Считаются только предприятия с ЗАВОДСКИМИ местами. Ферма мест для рабочих
    не создаёт: на ней трудятся крестьяне, не меняя сословия (Industry.labour).
    Если её сюда пустить, ферма своими двадцатью тысячами мест выдёргивала бы
    деревню в рабочие, а сама оставалась бы без рук.
    """
    buildings = [b for b in world.city_buildings(city.id)
                 if b.active and b.effective_level > 0 and b.throttle > EPS
                 and world.industries[b.industry_key].labour == "workers"]
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
    #
    # Мерки обязаны остаться РАЗНЫМИ, как бы ни хотелось свести их к одной.
    # Свести к уровню жизни нельзя: крестьянин ест со своего поля и потому живёт
    # сытнее кустаря почти всегда — по этой мерке мастерские опустели бы
    # начисто, и страна осталась бы без одежды, мебели и инструмента. Свести к
    # деньгам тоже нельзя: у кустаря их почти всегда больше, и деревня набилась
    # бы в мастерские до голода. Каждая мерка тянет в свою сторону, и держит
    # равновесие именно их пара.
    #
    # У денежной мерки при этом есть предохранитель, без которого пара мерок
    # вырождается в ловушку. Денежный доход кустаря выше крестьянского почти
    # всегда — по самому устройству хозяйства, а не потому, что ремесло удалось.
    # Значит, стоит уровню жизни кустаря провалиться, и получается дурная петля:
    # деревня продолжает идти на червонцы в мастерские, где уже голодают, и
    # кустари копятся до четверти страны при нулевом довольстве. Никто не идёт в
    # ремесло, люди которого на глазах бедствуют, — CRAFT_MISERY_STOP и есть эта
    # граница.
    peasants, artisans = city.s("peasants"), city.s("artisans")
    pi, ai = peasants.income_per_capita, artisans.income_per_capita
    ps, as_ = peasants.living_standard, artisans.living_standard
    edge = config.CRAFT_SWITCH_EDGE
    ruin = craft_ruin(peasants, artisans)

    # Бегут из ремесла по двум причинам, и берётся худшая. Доли у них разные на
    # порядок, и это главное в механике:
    #   * ЗАБРАЛИ СБЫТ — денежный доход провалился ниже крестьянского. Так
    #     действует завод: цену он перебивает, и кустарю остаётся верстак без
    #     заказов (craft_ruin). Здесь уходят быстро — заменить их выпуском есть
    #     кому, тот самый завод и заменит;
    #   * НЕ КОРМИТ — сословие живёт хуже деревни и при этом бедствует (голод
    #     без разницы в достатке гонит некуда, разница без голода терпима,
    #     поэтому меры перемножены). Здесь уходят вдевятеро медленнее: голодное
    #     ремесло — обычное состояние доиндустриальной страны, и вычерпать его
    #     до дна значит оставить мир без одежды. См. CRAFT_FLEE_HUNGER.
    need = misery(as_)
    pull = min(1.0, max(0.0, (ps - as_) / max(ps, EPS)))
    flee = max(config.CRAFT_FLEE_SHARE * ruin,
               config.CRAFT_FLEE_HUNGER * pull * need)

    # Тянет к верстаку заработок, гонит от него голод и разорение — и обе силы
    # действуют ОДНОВРЕМЕННО, поэтому считаются оба потока, а движется разница.
    # Ветвлением «или туда, или сюда» тут не обойтись, и это не придирка к
    # стилю: голод у кустаря почти никогда не равен нулю, и ветка бегства,
    # стоящая первой, навсегда запирала бы приток — сословие таяло бы до нуля в
    # любой стране, даже там, где ремесло прекрасно кормит. Разница же сама
    # находит равновесие: опустеют мастерские — вырастет цена их изделий,
    # приток перевесит отток, и деревня потянется обратно.
    attract = ai > pi * edge and misery(as_) < config.CRAFT_MISERY_STOP
    if not attract and artisans.people <= peasants.people * config.CRAFT_EXTINCT_FLOOR:
        # Ремесло в области пропало или почти пропало. Доходов у него нет,
        # значит и сравнивать не с чем — деревню зовут обратно к верстаку сами
        # цены: раз ремесло снова стоит заметно дороже надела, кто-то за него
        # возьмётся. Без этой ветки первый же завод убивал бы ремесло в стране
        # навсегда — вместе с одеждой, мебелью и инструментом, которые до
        # второго завода делать больше некому.
        attract = craft_potential(world, city) > max(pi, EPS) * config.CRAFT_REVIVAL_EDGE

    inflow = peasants.people * config.CRAFT_SWITCH_SHARE if attract else 0.0
    outflow = 0.0
    if artisans.people > EPS and flee > EPS:
        outflow = artisans.people * min(1.0, config.CRAFT_SWITCH_SHARE + flee)
        # ПРОПАСТЬ СОВСЕМ ремесло может только от разорения, не от бедности:
        # обезлюдевший и потерявший сбыт остаток расходится по деревне целиком.
        # По голоду так нельзя — голодают кустари и там, где заводов нет вовсе,
        # и страна осталась бы без одежды на ровном месте.
        if (ruin >= config.CRAFT_RUIN_AT
                and artisans.people <= peasants.people * config.CRAFT_EXTINCT_FLOOR):
            outflow, inflow = artisans.people, 0.0

    if outflow > inflow:
        move_people(artisans, peasants, outflow - inflow)
    elif inflow > outflow and peasants.people > EPS:
        move_people(peasants, artisans, inflow - outflow)


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


def country_population(world: World, country: Country) -> float:
    return sum(c.population for c in country_cities(world, country))


def size_title(population: float) -> str:
    """Во что складывается число душ: крошечная страна, маленькая, ... крупная.

    Одно слово говорит о стране больше, чем восьмизначное число: сразу видно,
    губерния перед тобой или держава. Ступени заданы в config.COUNTRY_SIZES и
    идут по возрастанию, поэтому подходит последняя, чей порог пройден.
    """
    title = config.COUNTRY_SIZES[0][1]
    for threshold, name in config.COUNTRY_SIZES:
        if population >= threshold:
            title = name
    return title


def size_rank(population: float) -> int:
    """Номер ступени размера, 0 — крошечная. Нужен витринам для раскраски."""
    rank = 0
    for i, (threshold, _name) in enumerate(config.COUNTRY_SIZES):
        if population >= threshold:
            rank = i
    return rank


def army_cap_share(world: World, country: Country) -> float:
    """Какую долю населения страна вообще способна поставить под ружьё.

    Чем сытнее живут люди, тем больше их можно оторвать от хозяйства, не
    развалив его: богатое общество терпит призыв заметно легче бедного.
    """
    bonus = config.ARMY_SOL_BONUS * prosperity(country_living_standard(world, country))
    return config.ARMY_TARGET_MAX * (1.0 + bonus)


def officer_size(world: World, country: Country) -> float:
    return sum(c.s("officers").people for c in country_cities(world, country))


def officer_pay(country: Country) -> float:
    """Жалованье офицера за пейдей — отдельный рычаг лидера.

    Ноль в Country.officer_pay значит «как заведено»: кратное солдатскому
    (config.OFFICER_PAY_MULT). Ставка ниже солдатской не имеет смысла — на
    таких условиях патента не возьмёт никто, — поэтому снизу она подпирается
    жалованьем солдата.
    """
    default = country.soldier_pay * config.OFFICER_PAY_MULT
    if country.officer_pay <= EPS:
        return default
    return max(country.soldier_pay * config.OFFICER_PAY_MIN_MULT,
               min(country.officer_pay,
                   country.soldier_pay * config.OFFICER_PAY_MAX_MULT))


def officer_target_share(country: Country) -> float:
    """Штат: сколько офицеров на солдата лидер хочет содержать.

    Ноль значит «по уставу» — config.OFFICER_TARGET_SHARE, ровно столько,
    сколько нужно для полного качества командования. Держать сверх штата стоит
    на войне: офицеры гибнут быстрее солдат, и лишние идут им на смену раньше,
    чем успеет доучиться новый набор.
    """
    if country.officer_target <= EPS:
        return config.OFFICER_TARGET_SHARE
    return min(config.OFFICER_TARGET_MAX, country.officer_target)


def officer_candidates(world: World, country: Country) -> float:
    """Сколько людей в стране вообще пойдёт в офицеры за нынешнее жалованье.

    Это и есть предел найма, не считая денег: патент берут в высшем обществе, а
    оно невелико и служить задаром не станет. Витрине число нужно, чтобы лидер
    видел причину недобора — казна пуста или нанимать больше некого.
    """
    pay = officer_pay(country)
    total = 0.0
    for city in country_cities(world, country):
        for key in config.OFFICER_POOL:
            st = city.s(key)
            if st.people <= EPS:
                continue
            if pay >= _officer_wage_bar(country, st):
                total += st.people
    return total


def officer_pay_bar(world: World, country: Country) -> float:
    """Какое жалованье нужно, чтобы в офицеры пошло ВЫСШЕЕ ОБЩЕСТВО.

    Первое сословие в config.OFFICER_POOL и есть то, ради которого механика
    затевалась: пока ставка ниже этой черты, патент берут только средние
    городские слои, а высший класс на службу не идёт. Витрине число нужно
    прямым ответом на вопрос «сколько платить».
    """
    key = config.OFFICER_POOL[0] if config.OFFICER_POOL else "town_high"
    people = 0.0
    weighted = 0.0
    for city in country_cities(world, country):
        st = city.s(key)
        if st.people <= EPS:
            continue
        people += st.people
        weighted += _officer_wage_bar(country, st) * st.people
    if people <= EPS:
        return 0.0
    return weighted / people


def _officer_wage_bar(country: Country, st) -> float:
    """Ниже какого жалованья это сословие в офицеры не пойдёт.

    Мера — привычный доход сословия за ПРОШЛЫЙ пейдей: свой доход за идущий
    человек ещё не получил, а к началу пейдея счётчик уже обнулён (см.
    Stratum.usual_income_per_capita). Высший класс живёт лучше всех в стране, и
    перебить его достаток стоит дорого. Планка при этом чуть ниже единицы
    (config.OFFICER_PAY_EDGE): служба даёт положение, чин и мундир, поэтому за
    патентом идут и с некоторой потерей в деньгах — но не даром.
    """
    alternative = max(st.usual_income_per_capita, country.min_wage * 0.5)
    return alternative * config.OFFICER_PAY_EDGE


def soldier_slot_cost(country: Country) -> float:
    """Во что обходится казне один солдат ВМЕСТЕ с положенной ему долей офицера.

    Считать иначе нельзя. Офицеры содержатся из того же военного бюджета, что и
    солдаты, и если набирать армию по одной солдатской ставке, а потом ставить
    офицеров сверх неё, бюджет неминуемо не сойдётся: жалованья не хватит, и
    армия начнёт разбегаться ровно тогда, когда её доукомплектовали командирами.
    Поэтому «место в армии» стоит жалованье солдата плюс положенную ему долю
    офицерского — и численность выводится уже из этой цены. Обе величины
    берутся по РЫЧАГАМ ЛИДЕРА: поднял офицерское жалованье или штат — место в
    строю подорожало, и армия при том же бюджете стала меньше.
    """
    return (country.soldier_pay
            + officer_target_share(country) * officer_pay(country))


def affordable_army(world: World, country: Country) -> float:
    """Сколько солдат страна может содержать на свой военный бюджет.

    Численность армии определяется не желанием лидера, а арифметикой: твёрдая
    сумма бюджета, делённая на цену одного места в строю (солдат плюс его доля
    офицера). Сверху — потолок по доле населения, который растёт вместе с
    уровнем жизни.
    """
    slot = soldier_slot_cost(country)
    if slot <= EPS or country.army_budget <= EPS:
        return 0.0
    pop = sum(c.population for c in country_cities(world, country))
    return min(country.army_budget / slot,
               pop * army_cap_share(world, country))


def affordable_officers(world: World, country: Country) -> float:
    """Сколько офицеров положено армии по штату, назначенному лидером."""
    return affordable_army(world, country) * officer_target_share(country)


def pay_army(world: World, country: Country) -> float:
    """Жалованье армии — отдельная статья казны, до всех прочих расходов.

    Платят и солдатам, и офицерам, из одного военного бюджета и по одному
    правилу: не больше того, что в нём есть. Кому не хватило — тот уходит
    домой, и так армия сама сходится к размеру, который страна тянет.

    Долг перед офицером считается по его ставке (кратной солдатской), поэтому
    недоплата бьёт по обоим сословиям в одинаковой доле: казна не выбирает,
    кого обидеть.
    """
    cities = country_cities(world, country)
    soldiers = sum(c.s("soldiers").people for c in cities)
    officers = sum(c.s("officers").people for c in cities)
    if (soldiers + officers) <= EPS or country.soldier_pay <= EPS:
        country.last_army_cost = 0.0
        return 0.0

    obligation = soldiers * country.soldier_pay + officers * officer_pay(country)
    want = min(obligation, max(0.0, country.army_budget))
    paid = min(want, max(0.0, country.treasury))
    country.spend("army_pay", paid)
    country.last_army_cost = paid
    # Доля закрытого жалованья считается от полного долга перед армией, а не
    # от урезанного бюджета: солдат сверх бюджета — это тоже недоплаченный
    # солдат, и он уходит домой.
    ratio = paid / obligation if obligation > EPS else 1.0

    for key, rate in (("soldiers", country.soldier_pay),
                      ("officers", officer_pay(country))):
        total = soldiers if key == "soldiers" else officers
        if total <= EPS:
            continue
        due = total * rate * ratio
        for city in cities:
            st = city.s(key)
            if st.people <= EPS:
                continue
            share = st.people / total
            st.cash += due * share
            st.income += due * share

    if ratio < 0.999:
        unpaid = 1.0 - ratio
        for city in cities:
            for key in ("soldiers", "officers"):
                st = city.s(key)
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
                # Сравнивается с привычным доходом за ПРОШЛЫЙ пейдей: свой за
                # идущий человек ещё не получил, а счётчик к началу пейдея уже
                # обнулён. По текущему income выходил бы ноль, и на службу шли
                # бы за любые деньги — то есть правила не было бы вовсе.
                alternative = max(st.usual_income_per_capita, country.min_wage * 0.5)
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


def commission_officers(world: World, country: Country) -> float:
    """НАЙМ ОФИЦЕРОВ ИЗ ВЫСШЕГО ОБЩЕСТВА — и роспуск лишних.

    Устроен ровно как призыв солдат (см. conscript), с тремя отличиями, и в них
    вся разница между рядовым и командиром:

      ОТКУДА. Не из деревни и не из бедноты, а из высшего общества и, если его
          не хватило, из среднего класса (config.OFFICER_POOL). Командовать
          учат, и патент достаётся тому, у кого есть образование и положение;
      ПОЧЁМ. Высший класс живёт лучше всех в стране, и перебить его привычный
          доход стоит дорого — отсюда и кратное жалованье офицера. Скупому
          лидеру высшее общество откажет, и корпус придётся набирать из
          среднего класса: его больше, но и приходит он с более низких доходов,
          а значит и служит охотнее лишь до тех пор, пока платят;
      СКОЛЬКО. Не больше штата (officer_target_share) и не больше того, что
          тянет военный бюджет: офицер содержится из той же суммы, что и
          солдаты, — каждый патент это несколько несодержанных рядовых.

    Сословие отдаёт за пейдей не больше OFFICER_RECRUIT_SHARE своих людей:
    офицерский корпус нельзя восполнить одним приказом, и после большой войны
    армия ходит обезглавленной ещё долго.

    Возвращает изменение численности (плюс — набрали, минус — распустили).
    """
    cities = country_cities(world, country)
    pop = sum(c.population for c in cities)
    country.last_officers_hired = 0.0
    if pop <= EPS:
        return 0.0
    officers = sum(c.s("officers").people for c in cities)
    gap = affordable_officers(world, country) - officers
    pay = officer_pay(country)

    if gap > EPS:
        came = 0.0
        for city in cities:
            need = gap * (city.population / pop)
            for key in config.OFFICER_POOL:
                if need <= EPS:
                    break
                st = city.s(key)
                if st.people <= EPS:
                    continue
                if pay < _officer_wage_bar(country, st):
                    continue
                limit = st.people * config.OFFICER_RECRUIT_SHARE
                moved = move_people(st, city.s("officers"), min(limit, need))
                need -= moved
                came += moved
        country.last_officers_hired = came
        return came

    if gap < -EPS:
        gone = 0.0
        for city in cities:
            st = city.s("officers")
            if st.people <= EPS:
                continue
            excess = -gap * (city.population / pop)
            # Отставной офицер уходит в средний класс, а не обратно в высшее
            # общество: чин остался, состояние — нет. Поэтому долгие качели
            # «набрали — распустили» медленно размывают верхушку общества, и
            # держать штат ровным лидеру выгоднее, чем дёргать его туда-сюда.
            gone += move_people(st, city.s("town_mid"),
                                min(excess, st.people * config.OFFICER_DISCHARGE_SHARE))
        return -gone
    return 0.0


# ---------------------------------------------------------------------------
# Фронты
# ---------------------------------------------------------------------------
def front_key(country_id: int) -> str:
    """Ключ фронта в снимке мира. Строка — потому что снимок это JSON."""
    return str(int(country_id))


def fronts_of(world: World, country: Country) -> list[int]:
    """С кем у страны вообще может быть фронт — со всеми соседями по карте."""
    return world.neighbor_countries(country.id)


def front_soldiers(country: Country, enemy_id: int) -> float:
    return max(0.0, country.fronts.get(front_key(enemy_id), 0.0))


def front_officers(country: Country, enemy_id: int) -> float:
    return max(0.0, country.front_officers.get(front_key(enemy_id), 0.0))


def normalize_fronts(world: World, country: Country) -> None:
    """Привести расстановку в соответствие с тем, что у страны реально есть.

    Расстановка живёт своей жизнью и легко расходится с действительностью:
    солдаты гибнут в бою, разбегаются от недоплаты, распускаются по сокращению
    бюджета, а сосед, против которого стоял фронт, может вовсе исчезнуть с
    карты. Поэтому перед каждым пейдеем расстановка приводится к трём правилам:
    фронты только против нынешних соседей, на фронте не больше людей, чем есть
    в армии, и при нехватке все фронты ужимаются в одинаковой доле — оголять
    одно направление ради другого должен лидер, а не арифметика.
    """
    live = {front_key(x) for x in fronts_of(world, country)}
    for holder in (country.fronts, country.front_officers):
        for key in [k for k in holder if k not in live]:
            holder.pop(key, None)

    for holder, total in ((country.fronts, army_size(world, country)),
                          (country.front_officers, officer_size(world, country))):
        for key in list(holder):
            holder[key] = max(0.0, holder[key])
            if holder[key] <= EPS:
                holder.pop(key, None)
        placed = sum(holder.values())
        if placed > total + EPS and placed > EPS:
            scale = total / placed
            for key in list(holder):
                holder[key] *= scale


def free_soldiers(world: World, country: Country) -> float:
    """Резерв: люди, не поставленные ни на один фронт."""
    return max(0.0, army_size(world, country) - sum(country.fronts.values()))


def free_officers(world: World, country: Country) -> float:
    return max(0.0, officer_size(world, country)
               - sum(country.front_officers.values()))


def deploy_reserve(world: World, country: Country, enemy_id: int) -> float:
    """Выдвинуть весь свободный резерв на указанный фронт.

    Вызывается при объявлении войны — и для обеих сторон сразу. Без этого
    страна, которой война объявлена, встречала бы врага пустой границей просто
    потому, что лидер не успел ничего нажать (а у AI-государств нажимать
    некому). Войска с ДРУГИХ фронтов при этом не трогаются: снимать их —
    отдельное решение лидера, и оно должно чего-то стоить.
    """
    key = front_key(enemy_id)
    moved = free_soldiers(world, country)
    if moved > EPS:
        country.fronts[key] = country.fronts.get(key, 0.0) + moved
    officers = free_officers(world, country)
    if officers > EPS:
        country.front_officers[key] = country.front_officers.get(key, 0.0) + officers
    return moved


def set_front(world: World, country: Country, enemy_id: int,
              soldiers: float | None = None,
              officers: float | None = None) -> dict:
    """Приказ лидера: столько-то людей на фронт против такого-то соседа.

    Взять их можно из резерва и с других фронтов, но не мгновенно: за пейдей с
    каждого направления снимается не больше config.FRONT_MOVE_SHARE стоящих
    там людей. Резерв идёт первым — он ни от кого не прикрывает, — и только
    когда его не хватило, начинают оголять соседние фронты, начиная с самых
    многолюдных.

    Ограничение на переброску и есть суть механики. Будь она мгновенной, фронты
    превратились бы в одну кнопку «весь кулак сюда», нажимаемую в последний
    момент, и никакого выбора направления не осталось бы.
    """
    normalize_fronts(world, country)
    key = front_key(enemy_id)
    if enemy_id not in fronts_of(world, country):
        return {"soldiers": front_soldiers(country, enemy_id),
                "officers": front_officers(country, enemy_id),
                "moved": 0.0, "short": 0.0,
                "error": "С этим государством нет общей границы"}

    result = {"moved": 0.0, "short": 0.0}
    for holder, want, total_free in (
            (country.fronts, soldiers, free_soldiers(world, country)),
            (country.front_officers, officers, free_officers(world, country))):
        if want is None:
            continue
        want = max(0.0, float(want))
        have = max(0.0, holder.get(key, 0.0))
        if want <= have:
            # Отвод назад в резерв тоже не мгновенен: тем же темпом.
            back = min(have - want, have * config.FRONT_MOVE_SHARE)
            holder[key] = have - back
            result["moved"] += back
            continue

        need = want - have
        take = min(need, total_free)          # сперва резерв
        need -= take
        if need > EPS:
            # затем — с других фронтов, начиная с самых многолюдных
            donors = sorted((k for k in holder if k != key),
                            key=lambda k: -holder[k])
            for other in donors:
                if need <= EPS:
                    break
                can = holder[other] * config.FRONT_MOVE_SHARE
                give = min(can, need)
                holder[other] -= give
                take += give
                need -= give
        holder[key] = have + take
        result["moved"] += take
        result["short"] += max(0.0, need)

    normalize_fronts(world, country)
    result["soldiers"] = front_soldiers(country, enemy_id)
    result["officers"] = front_officers(country, enemy_id)
    return result


def command_quality(officers: float, soldiers: float) -> float:
    """КАЧЕСТВО КОМАНДОВАНИЯ на фронте — от COMMAND_MIN до COMMAND_MAX.

    Меряется одним: хватает ли на стоящих здесь солдат положенных им по штату
    офицеров (config.OFFICER_TARGET_SHARE). Хватает — потолок; нет ни одного —
    единица процента, то есть почти ничего.

    Считается именно ПО ФРОНТУ, а не по стране, и это главное в механике:
    офицеры, оставленные в тылу или расставленные по спокойным границам, не
    командуют никем. Держать их надо там, где будет бой.
    """
    if soldiers <= EPS:
        return 0.0
    target = max(config.OFFICER_TARGET_SHARE, 1e-9)
    filled = min(1.0, (officers / soldiers) / target)
    return config.COMMAND_MIN + (config.COMMAND_MAX - config.COMMAND_MIN) * filled


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


def army_supply_purse(country: Country) -> float:
    """Сколько казна вправе потратить на закупку оружия и снарядов за пейдей.

    Предохранитель к мгновенному пополнению: недостача закрывается за один
    пейдей, но не ценой всей казны. Без него страна, спешно вооружающая армию,
    выгребала бы остаток до нуля — и та же армия следующим пейдеем разошлась бы
    по домам, не получив жалованья (оно платится первой статьёй, из того же
    остатка).
    """
    return max(0.0, country.treasury) * config.ARMY_SUPPLY_TREASURY_SHARE


def restock_shells(world: World, country: Country, city: City, market,
                   share: float = 1.0) -> float:
    """Казна закупает снаряды в армейский резерв на рынке одной области.

    Именно здесь у снарядного завода появляется покупатель: армия сама на
    рынок не ходит, за неё платит государство.

    Пополнение мгновенное: за пейдей закрывается вся недостача, сколько её
    закроют прилавок и казна. Области при этом обходятся подряд, а не по
    разнарядке от населения: если снаряды лежат в одной области, а половина
    народу живёт в другой, разнарядка оставляла бы армию полупустой при полных
    складах.
    """
    gap = shells_gap(world, country)
    if gap <= EPS or share <= EPS:
        return 0.0

    want = gap * config.SHELLS_RESTOCK_SHARE * share
    price = lg(city, "shells").price
    if price > EPS:
        want = min(want, army_supply_purse(country) / price)
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

    Как и со снарядами, недостача закрывается за один пейдей целиком: мера —
    запас на прилавках и деньги в казне, а не квота.
    """
    want = weapons_wanted(world, country) * share
    if want <= EPS:
        return 0.0

    price = lg(city, "weapons").price
    if price > EPS:
        want = min(want, army_supply_purse(country) / price)
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


def army_morale(world: World, country: Country) -> float:
    """Средний дух армии, взвешенный по числу солдат."""
    cities = country_cities(world, country)
    soldiers = sum(c.s("soldiers").people for c in cities)
    if soldiers <= EPS:
        return 0.0
    return sum(c.s("soldiers").satisfaction * c.s("soldiers").people
               for c in cities) / soldiers


def combat_strength(world: World, country: Country, soldiers: float,
                    officers: float) -> float:
    """Чего стоит в бою отряд из стольких-то солдат при стольких-то офицерах.

    Общая формула для всего: и для армии страны целиком, и для отдельного
    фронта. Множители по порядку — люди, вооружённость, снабжение снарядами,
    дух и КАЧЕСТВО КОМАНДОВАНИЯ. Толпа без оружия и снарядов почти ничего не
    стоит, а без офицеров она вдобавок и не умеет ничего: полностью
    укомплектованный командирами отряд стоит двух с половиной безофицерских.

    Вооружённость и снабжение — общестрановые: арсенал и снарядные склады у
    страны одни, и делить их по фронтам было бы честно, но добавило бы игроку
    ещё одну расстановку поверх той, что уже есть. Здесь достаточно того, что
    голодная по снарядам армия слаба на всех фронтах разом.
    """
    if soldiers <= EPS:
        return 0.0
    equip = config.EQUIP_STRENGTH_MIN + (1.0 - config.EQUIP_STRENGTH_MIN) * \
        max(0.0, min(1.0, country.army_equip))
    need = army_size(world, country) * config.SHELLS_PER_SOLDIER_BATTLE
    supply = 1.0 if need <= EPS else min(1.0, country.army_shells / need)
    supply = config.SUPPLY_STRENGTH_MIN + (1.0 - config.SUPPLY_STRENGTH_MIN) * supply
    morale = army_morale(world, country)
    command = 1.0 + command_quality(officers, soldiers)
    return soldiers * equip * supply * (0.6 + 0.6 * morale) * command


def army_strength(world: World, country: Country) -> float:
    """Чего стоила бы вся армия страны, собранная в один кулак.

    Величина справочная — для витрин и для оценки соседа. В бою участвует не
    она, а сила КОНКРЕТНОГО ФРОНТА (front_strength): войска, оставленные на
    других границах и в резерве, в сражении не помогают ничем.
    """
    return combat_strength(world, country, army_size(world, country),
                           officer_size(world, country))


def front_strength(world: World, country: Country, enemy_id: int) -> float:
    """Чего стоит фронт против конкретного государства.

    Это и есть та сила, которая дерётся. Пустой фронт — пустая граница: ни
    резерв, ни полки, стоящие против другого соседа, за него не вступятся.
    """
    return combat_strength(world, country,
                           front_soldiers(country, enemy_id),
                           front_officers(country, enemy_id))


# ---------------------------------------------------------------------------
# Приток населения за новых игроков
# ---------------------------------------------------------------------------
def queue_settlers(country: Country, rng: random.Random) -> dict[str, float]:
    """Записать в очередь приток людей за одного нового промышленника.

    Рынок сбыта — это люди, и пока население зависело от одной рождаемости,
    каждый новый игрок приходил делить УЖЕ ИМЕЮЩИЙСЯ спрос: второй швейной
    фабрике в стране продавать было некому, и вся выгода доставалась тому, кто
    успел построиться раньше. Теперь вместе с промышленником в страну приходит
    и его рынок.

    Приходят люди ПО ВСЕМ СОСЛОВИЯМ сразу, а не одними рабочими: заводу нужны
    не только руки, но и покупатели, а покупает вся страна. Сколько именно
    приведёт за собой конкретный игрок — величина случайная, и своя по каждому
    сословию: одному достанется деревня, другому городская беднота.
    """
    swing = config.JOIN_GROWTH_SWING
    batch: dict[str, float] = {}
    for key, base in config.POPULATION_PER_PLAYER.items():
        n = base * rng.uniform(1.0 - swing, 1.0 + swing)
        if n <= EPS:
            continue
        batch[key] = n
        country.settlers[key] = country.settlers.get(key, 0.0) + n
    # Отсчёт начинается заново: подошедший позже игрок растягивает и остаток
    # прежнего притока, а не догоняет его отдельной очередью.
    country.settlers_left = config.JOIN_GROWTH_TICKS
    return batch


def settle_newcomers(world: World, country: Country) -> float:
    """Впустить очередную долю притока. Один вызов — один пейдей.

    Приток размазан по JOIN_GROWTH_TICKS пейдеям нарочно: свались он разом,
    рынок получил бы ступеньку спроса, цены прыгнули бы, а половина новичков
    осталась бы без хлеба в тот же пейдей. Люди приезжают НЕ ГОЛЫМИ — с теми же
    сбережениями на душу, что у их сословия на месте: иначе приток размывал бы
    кассу сословия и делал страну беднее ровно тогда, когда она растёт.
    """
    if country.settlers_left <= 0 or not country.settlers:
        return 0.0
    cities = country_cities(world, country)
    if not cities:
        country.settlers, country.settlers_left = {}, 0
        return 0.0

    pop = sum(c.population for c in cities)
    share = 1.0 / country.settlers_left
    arrived = 0.0
    for key in list(country.settlers):
        batch = country.settlers[key] * share
        if batch <= EPS:
            country.settlers.pop(key, None)
            continue
        country.settlers[key] -= batch
        arrived += batch
        for city in cities:
            part = batch * (city.population / pop if pop > EPS
                            else 1.0 / len(cities))
            st = city.s(key)
            per_head = (st.cash / st.people if st.people > 1.0
                        else config.STRATA[key]["level"] * 0.6)
            st.people += part
            st.cash += part * per_head

    country.settlers_left -= 1
    if country.settlers_left <= 0:
        country.settlers, country.settlers_left = {}, 0
    return arrived


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
