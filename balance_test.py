"""
Проверка баланса симуляции без веб-сервера.

    python balance_test.py [число_тиков] [--player] [--trade] [--war] [--quiet]

Гоняет мир из 20 государств N пейдеев и проверяет, что экономика держится:
цены в коридоре, деревня кормит страну, денежная масса сходится.

С флагом --player в мир приходит бот-промышленник в государстве Аркадия. Он и
есть главная проверка: может ли игрок нанять людей, построить цепочку и выйти
в плюс, не проваливаясь в долги.

С флагом --trade три государства строят торговые площади и начинают торговать
между собой. Мировой рынок обязан быть замкнутым: сколько товара ушло из одной
страны, столько должно прийти в другую, и деньги при этом обязаны сойтись.

С флагом --war два соседа поднимают армию и сходятся в войне, а третья страна
входит в неё союзником и затем выходит сепаратным миром. Проверяется, что война
идёт, армии несут потери, промышленность получает урон, области меняют хозяина,
а сепаратный мир вынимает из войны только того, кто его заключил.

С флагом --school две страны заводят просвещение, а третья остаётся при
сословном образовании. Проверяется вся цепочка разом: школы и университеты
учат, бумажная фабрика находит покупателя в казне, грамотность открывает
деревне дорогу на завод, а неутолённое самосознание доводит одну из стран до
революции. Лидер первой на требования соглашается, второй — молчит и получает
гражданскую войну; сравнение этих двух исходов и есть смысл прогона.

С флагом --laws четыре государства принимают по своей форме правления —
республику с всеобщим избирательным правом, социализм, национализм, закрытую
экономику, — собирают парламенты и голосуют по законам, а промышленник-лоббист
скупает голоса. Проверяется, что палаты собираются, законы проходят, деньги
лоббиста не исчезают из экономики и роспись казны по-прежнему сходится.
"""
from __future__ import annotations

import sys

from app import config
from app.economy.engine import (
    _add_alliance, accept_demands, declare_war, level_cost, make_peace, run_tick,
)
from app.economy.pricing import price_bounds
from app.economy.seed import build_world
from app.economy import politics, society
from app.models import Building, Player

TICKS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 150
WITH_PLAYER = "--player" in sys.argv
WITH_TRADE = "--trade" in sys.argv
WITH_WAR = "--war" in sys.argv
WITH_LAWS = "--laws" in sys.argv
WITH_SCHOOL = "--school" in sys.argv
QUIET = "--quiet" in sys.argv


def add_player(world) -> Player:
    # бот — гражданин первого государства (Аркадия)
    cid = next(iter(world.countries.keys()))
    p = Player(id=world.next_player_id, username="Бот-делец",
               cash=config.STARTING_CAPITAL, country_id=cid)
    world.players[p.id] = p
    world.next_player_id += 1
    return p


def _home_region(world, country):
    """Столичная область страны — по ней смотрим цены в отчёте."""
    city = world.cities.get(country.capital_city_id)
    if city is not None:
        return city
    regions = world.country_regions(country.id)
    return regions[0] if regions else None


def rural_income(world, city) -> float:
    """Сколько человек зарабатывает в деревне государства — планка для найма."""
    vals = [city.s(k).income_per_capita for k in ("peasants", "artisans")
            if city.s(k).people > 1]
    return max(vals) if vals else world.countries[city.country_id].min_wage


def inputs_obtainable(world, region, ind) -> bool:
    for key in ind.inputs:
        local = region.goods.get(key)
        if local is None:
            return False
        if local.stock <= 1.0 and local.last_supply <= 1.0:
            return False
    return True


def profit_per_worker(world, region, ind, wage: float) -> float:
    if ind.output_good is None or not inputs_obtainable(world, region, ind):
        return -1e9
    local = region.goods[ind.output_good]
    input_cost = sum(q * region.goods[k].price for k, q in ind.inputs.items())
    # Содержание считаем по местам ИМЕННО ЭТОЙ отрасли: у фермы их впятеро
    # больше заводских, и общая константа завышала бы ей расходы в пять раз.
    upkeep = config.UPKEEP_PER_LEVEL / max(ind.jobs_per_level, 1)
    return (local.price - input_cost) * ind.output_per_worker - wage - upkeep


def play(world, p: Player) -> None:
    """Стратегия бота: платить выше деревни и строить самое прибыльное."""
    country = world.countries[p.country_id]
    mine = world.player_buildings(p.id)

    for b in mine:
        city = world.cities[b.city_id]
        target = max(rural_income(world, city) * 1.35, country.min_wage)
        b.wage = max(b.wage * 0.7 + target * 0.3, country.min_wage)

    # столица государства бота
    city = world.cities[country.capital_city_id]
    wage = max(rural_income(world, city) * 1.35, country.min_wage)

    best, score = None, 0.0
    for ind in world.industries.values():
        if ind.kind != "industry":
            continue
        val = profit_per_worker(world, city, ind, wage)
        if val > score:
            best, score = ind, val
    if best is None:
        return

    reserve = sum(b.level * world.industries[b.industry_key].jobs_per_level * b.wage
                  + b.last_inputs for b in mine) * 1.3

    existing = [b for b in mine if b.industry_key == best.key]
    if existing:
        b = existing[0]
        cost = level_cost(best.build_cost_mult, b.level + 1)
        if p.cash > cost + reserve:
            pay_builders(world, country, world.cities[b.city_id], p, cost)
            b.level += 1
        return

    cost = level_cost(best.build_cost_mult, 1)
    if p.cash <= cost + reserve:
        return
    pay_builders(world, country, city, p, cost)
    b = Building(id=world.next_building_id, industry_key=best.key, owner_id=p.id,
                 city_id=city.id, level=1, wage=wage)
    world.buildings[b.id] = b
    world.next_building_id += 1


def pay_builders(world, country, city, p: Player, cost: float) -> None:
    p.cash -= cost
    net = cost * (1.0 - country.income_tax)
    st = city.s("town_low")
    st.cash += net
    st.income += net
    # Через статью бюджета, а не мимо неё: роспись казны обязана сходиться с
    # остатком, и бот-застройщик не исключение.
    country.collect("income_tax", cost * country.income_tax)


def open_trade(world, countries: int = 3, levels: int = 6) -> None:
    """Поднять «Торговые палаты» в нескольких государствах.

    Палата — казённое ведомство, и строит её государство, поэтому владельцем
    ставим казну. Каждый её уровень поднимает доступность рынка области на
    десять процентов: с шестью уровнями область включена в общий рынок на 70%.
    """
    for co in list(world.countries.values())[:countries]:
        state = world.state_player(co.id)
        if state is None:
            continue
        for city in world.country_regions(co.id):
            b = Building(id=world.next_building_id, industry_key="trade_chamber",
                         owner_id=state.id, city_id=city.id, level=levels,
                         wage=50.0)
            world.buildings[b.id] = b
            world.next_building_id += 1


def arm_country(world, country, budget: float, pay: float) -> Player:
    """Поднять в стране всю оружейную цепочку и оплатить армию."""
    country.army_budget = budget
    country.soldier_pay = pay
    city = world.cities[country.capital_city_id]
    p = Player(id=world.next_player_id, username=f"Оружейник-{country.name}",
               cash=5e8, country_id=country.id)
    world.players[p.id] = p
    world.next_player_id += 1
    chain = [("mine", 3), ("coalmine", 2), ("smelter", 4), ("sulfurmine", 1),
             ("logging", 2), ("armsworks", 1), ("shellworks", 1)]
    for key, lv in chain:
        b = Building(id=world.next_building_id, industry_key=key, owner_id=p.id,
                     city_id=city.id, level=lv, wage=45.0)
        world.buildings[b.id] = b
        world.next_building_id += 1
    return p


def setup_war(world) -> dict:
    """Два соседа вооружаются, третий входит союзником — и начинается война.

    Воюют намеренно на другом конце карты: бот-промышленник и торговые
    государства сидят в первых областях, и война не должна портить проверки
    экономики, которые к ней отношения не имеют.
    """
    ids = list(world.countries.keys())
    # Палема — Флорания соседи, Ундия граничит с Флоранией и идёт союзником
    attacker, defender = world.countries[ids[14]], world.countries[ids[19]]
    ally = world.countries[ids[18]]
    for co, budget in ((attacker, 2_500_000), (defender, 2_000_000),
                       (ally, 800_000)):
        arm_country(world, co, budget, 25.0)
    _add_alliance(world, attacker.id, ally.id)
    return {"attacker": attacker, "defender": defender, "ally": ally}


# ---------------------------------------------------------------------------
# Законы, парламенты и лоббирование
# ---------------------------------------------------------------------------
# Каждая страна берёт СВОЮ форму правления, чтобы за один прогон прошли все
# ветки сразу: и «крепостное право» монархии, и «дополнительное распределение»
# социализма, и военный сбор национализма, и закрытая экономика.
#
# Земельное устройство разведено по странам по той же причине: у него четыре
# уклада, и каждый трогает деревню по-своему — от барщины до общего хозяйства.
# Социализм при этом объявляется ЗАКОННЫМ путём (республика + всеобщее право):
# при монархии и при цензе он теперь попросту не принимается, а коллективное
# хозяйство не мыслится без него самого.
LAW_SETUPS = [
    ("республика + всеобщее право",
     {"suffrage": "universal", "state_form": "republic",
      "land": "smallholding"}),
    ("социализм и коллективное хозяйство",
     {"suffrage": "universal", "state_form": "republic",
      "ideology": "socialism", "land": "collective"}),
    ("национализм при богатых",
     {"suffrage": "rich", "ideology": "nationalism", "land": "commercial"}),
    ("закрытая монархия",
     {"suffrage": "census", "trade": "closed", "ideology": "conservatism"}),
]


def setup_laws(world) -> dict:
    """Раздать четырём странам разные формы правления и завести лоббиста.

    Лоббист намеренно очень богат: его дело — проверить, что вложенные деньги
    двигают голоса и при этом не пропадают из экономики, а не изображать
    правдоподобного промышленника.
    """
    ids = list(world.countries.keys())
    setups = []
    blocked: list[str] = []
    for (label, laws), cid in zip(LAW_SETUPS, ids[4:8]):
        co = world.countries[cid]
        # Порядок не косметический, а обязательный, и он же проверка запретов:
        # избирательная система идёт первой (без неё не объявить республику),
        # форма правления второй (при монархии не принять социализма),
        # идеология третьей и лишь затем земля (коллективное хозяйство требует
        # уже принятого социализма). Каждый шаг проходит через law_blocked —
        # если запрет сработал не там, где задумано, это видно сразу.
        for cat in ("suffrage", "state_form", "ideology", "land", "trade"):
            if cat not in laws:
                continue
            why = politics.law_blocked(co, cat, laws[cat])
            if why:
                blocked.append(f"{co.name}: {cat}={laws[cat]} — {why}")
                continue
            politics.apply_law(world, co, cat, laws[cat])
        setups.append((label, co))
    lobbyist = Player(id=world.next_player_id, username="Лоббист",
                      cash=400_000_000.0, country_id=setups[0][1].id)
    world.players[lobbyist.id] = lobbyist
    world.next_player_id += 1
    return {"setups": setups, "lobbyist": lobbyist, "blocked": blocked,
            "votes": 0, "passed": 0, "spent": 0.0, "bought": 0.0, "bills": 0}


def play_politics(world, state: dict, tick: int) -> None:
    """Пейдей политической жизни: лоббист вкладывается и толкает законы."""
    lob = state["lobbyist"]
    for label, co in state["setups"]:
        if not co.alive or not politics.has_elections(co):
            continue
        # Вложения в партию перед выборами — на всякий пейдей понемногу.
        if tick % 17 == 0:
            stake = politics.min_stake(co) * 2
            if lob.cash > stake:
                politics.place_party_bid(world, co, lob, "socialists", stake)
                state["spent"] += stake
        # Каждый десятый пейдей выносим на голосование протекционизм и
        # подпираем его деньгами: перевес вложений и есть купленные голоса.
        if (tick % 10 == 0 and co.law_vote is None and co.parties
                and tick > co.last_law_tick + config.LAW_COOLDOWN_TICKS):
            want = "protectionism" if politics.law(co, "trade") != "protectionism" \
                else "free_trade"
            if not politics.law_blocked(co, "trade", want):
                politics.open_law_vote(world, co, "trade", want, lob, financed=True)
                state["votes"] += 1
        if co.law_vote is not None and tick % 10 == 2:
            stake = politics.min_stake(co) * 4
            if lob.cash > stake:
                politics.place_law_bid(world, co, lob, "for", stake)
                state["spent"] += stake
                state["bought"] += stake / politics.seat_price(co) \
                    * politics.lobby_power(co)


# ---------------------------------------------------------------------------
# Просвещение, бумага и революция
# ---------------------------------------------------------------------------
# Три страны, три разных ответа на один и тот же вопрос — что делать с
# грамотностью. Смысл прогона в СРАВНЕНИИ: поодиночке ни одна из веток ничего
# не доказывает.
#
#   УСТУПАЕТ  — открывает школы и, когда за ними приходят с требованиями,
#       принимает их. Обязана выйти из истории с высокой грамотностью, низкой
#       обидой и целой страной;
#   УПИРАЕТСЯ — те же школы, но лидер молчит. Обязана дойти до гражданской
#       войны, и войну эту обязаны решить перебежавшие солдаты, а не число
#       ополченцев;
#   ТЁМНАЯ    — школ не строит вовсе. Обязана остаться неграмотной, спокойной и
#       без единого рабочего сверх того, что даёт врождённая грамотность. Это и
#       есть цена, которую платит страна за отказ от просвещения.
SCHOOL_SETUPS = [
    ("уступает", "state", True),
    ("упирается", "state", False),
    ("тёмная", None, False),
]


def setup_schools(world) -> dict:
    """Раздать трём странам просвещение (или отказ от него) и бумажную фабрику.

    Школы, университет и бумажная фабрика ставятся казной сразу и уровнями:
    прогон проверяет не то, накопит ли AI на постройку (он не накопит — строить
    ему нечем), а то, что механика работает, когда здания уже стоят.
    """
    ids = list(world.countries.keys())
    setups = []
    for (label, law, concede), cid in zip(SCHOOL_SETUPS, ids[9:12]):
        co = world.countries[cid]
        state = world.state_player(co.id)
        if law:
            politics.apply_law(world, co, "education", law)
            for city in world.country_regions(co.id):
                for key, lv in (("school", 2), ("academy", 1), ("papermill", 1)):
                    b = Building(id=world.next_building_id, industry_key=key,
                                 owner_id=state.id, city_id=city.id, level=lv,
                                 wage=45.0)
                    world.buildings[b.id] = b
                    world.next_building_id += 1
        setups.append({"label": label, "country": co, "concede": concede,
                       "schooled": bool(law), "accepted": 0, "wars": 0,
                       "risen": 0, "defected": 0.0, "paper_bought": 0.0,
                       # Население на старте — по нему и меряется, во что
                       # обошлась гражданская война: людьми, а не процентами.
                       "pop0": society.country_population(world, co),
                       "losses": 0.0})
    return {"setups": setups, "peak_edu": 0.0}


def play_schools(world, state: dict) -> None:
    """Пейдей просвещения: уступчивый лидер отвечает восставшим, упрямый молчит."""
    for row in state["setups"]:
        co = row["country"]
        if not co.alive:
            continue
        rev = co.revolution
        if rev is not None and rev.phase == "demands":
            row["risen"] = max(row["risen"], 1)
            row["defected"] = max(row["defected"], rev.defected)
            if row["concede"]:
                accept_demands(world, co)
                row["accepted"] += 1
        elif rev is not None and rev.phase == "war":
            row["wars"] = max(row["wars"], rev.battles)
            row["defected"] = max(row["defected"], rev.defected)
            row["losses"] = max(row["losses"], rev.gov_losses + rev.rebel_losses)
        # Бумага: сколько её казна и школы вправду выбирают с прилавка. Считаем
        # по проданному, а не по спросу, — покупатель, которому не хватило
        # товара, отрасли ничего не приносит.
        for city in world.country_regions(co.id):
            local = city.goods.get("paper")
            if local is not None:
                row["paper_bought"] += local.last_sold
    state["peak_edu"] = max(
        [state["peak_edu"]]
        + [society.country_education(world, r["country"])
           for r in state["setups"] if r["country"].alive])


# ---------------------------------------------------------------------------
def main() -> int:
    world = build_world()
    bot = add_player(world) if WITH_PLAYER else None
    if WITH_TRADE:
        open_trade(world)
    law_state = setup_laws(world) if WITH_LAWS else None
    school_state = setup_schools(world) if WITH_SCHOOL else None
    war_setup = setup_war(world) if WITH_WAR else None
    regions_before = len(world.cities)
    print(f"Гоняем {TICKS} пейдеев по {len(world.countries)} государствам"
          f"{' с ботом-промышленником в Аркадии' if bot else ''}...\n")
    print(f"{'тик':>4} {'ВВП':>13} {'ИПЦ':>6} {'население':>11} "
          f"{'индустр':>8} {'безраб':>7} {'дово-во':>8} {'з/п':>8} {'ден.масса':>14}")

    history = []
    # Страну бота нельзя запомнить один раз: её могут завоевать, и тогда бот
    # становится гражданином победителя. Перечитываем её на каждом пейдее.
    def bot_home():
        return world.countries[bot.country_id] if bot else None

    # Сыта ли страна — меряется по ЗЕРНУ: это первая ступень лестницы еды и
    # единственное, без чего человек голодает. Продукты и продовольствие —
    # ступени выше, их нехватка бьёт по уровню жизни, а не по животу.
    grain_shortage = []
    worst_cash = float("inf")
    # Роспись казны обязана сходиться с остатком до червонца: каждое движение
    # казны помечено статьёй, и сумма статей равна изменению остатка. Если
    # где-то в коде казна меняется мимо Country.collect/.spend, эта проверка
    # поймает пропажу — а вкладка «Казна» перестала бы врать незаметно.
    budget_drift = 0.0
    war = None
    war_log = {"battles": 0, "damaged": 0, "news": [], "separate_peace": False,
               # Офицеры — отдельный счёт. Их гибель на фронте и последующий
               # найм из высшего общества это не убыль людей (их немного), а
               # убыль КОМАНДОВАНИЯ: качество падает сразу, а восполняется
               # медленно, и проверять надо обе половины механики.
               "officers_lost": 0.0, "officers_hired": 0.0,
               "command_before": 0.0, "command_after": 0.0}
    for i in range(1, TICKS + 1):
        if war_setup:
            # даём странам вооружиться, потом объявляем войну
            if war is None and i == 25:
                war = declare_war(world, war_setup["attacker"].id,
                                  war_setup["defender"].id)
            # Союзник выходит сепаратным миром — война обязана продолжиться
            # без него. Делаем это на четвёртом пейдее войны, пока фронт ещё
            # не решился: после аннексии выходить будет уже неоткуда.
            if (war is not None and not war.ended and i == 29
                    and war_setup["ally"].id in war.attackers):
                make_peace(world, war, war_setup["ally"].id,
                           war_setup["defender"].id)
                war_log["separate_peace"] = (
                    war_setup["ally"].id not in war.attackers
                    and not war.ended
                    and war_setup["attacker"].id in war.attackers)
        if war_setup and war is None and i == 24:
            # Качество командования накануне войны — с ним и сравним то, во что
            # его превратят бои.
            att0 = war_setup["attacker"]
            war_log["command_before"] = society.command_quality(
                society.officer_size(world, att0), society.army_size(world, att0))
        if law_state:
            play_politics(world, law_state, i)
        r = run_tick(world)
        if school_state:
            play_schools(world, school_state)
        if law_state:
            # Собственная инициатива палаты видна только в новостях: лидера у
            # этих стран нет, лоббист вносит свои вопросы сам и отдельно.
            law_state["bills"] += sum(1 for line in r.get("news", [])
                                      if "сам выносит" in line)
        for co in world.countries.values():
            if co.alive:
                budget_drift = max(budget_drift, abs(
                    co.last_budget_opening + sum(co.last_budget.values())
                    - co.budget_opening))
        bc = bot_home()
        if war is not None:
            war_log["battles"] += len(war.last_report)
            war_log["damaged"] += sum(x["buildings_attacker"] + x["buildings_defender"]
                                      for x in war.last_report)
            war_log["news"] += r.get("news", [])
            war_log["officers_lost"] += sum(
                x["officers_attacker"] + x["officers_defender"]
                for x in war.last_report)
            for co in (war_setup["attacker"], war_setup["defender"]):
                if co.alive:
                    war_log["officers_hired"] += co.last_officers_hired
        history.append(r)
        grain_shortage.append(_home_region(world, bc).goods["grain"].last_shortage
                              if bc else 0.0)
        if bot is not None:
            if i % 3 == 0:
                play(world, bot)
            worst_cash = min(worst_cash, bot.cash)
        if not QUIET and (i == 1 or i % max(1, TICKS // 20) == 0):
            print(f"{i:>4} {r['gdp']:>13,.0f} {r['cpi']:>6.3f} "
                  f"{r['population']:>11,.0f} {r['industrialisation']:>7.1%} "
                  f"{r['unemployment']:>6.1%} {r['satisfaction']:>7.1%} "
                  f"{r['avg_wage']:>8.2f} {r['money_supply']:>14,.0f}")

    # цены — по столичной области бота (или первого живого государства)
    country = bot_home() or next(c for c in world.countries.values() if c.alive)
    region = _home_region(world, country)
    print(f"\n--- Итоговые цены в государстве «{country.name}» ---")
    print(f"{'товар':<14}{'цена':>10}{'себест.':>10}{'якорь':>10}"
          f"{'коридор':>22}{'наценка':>9}{'склад':>14}{'дефицит':>9}")
    problems = []
    for g in sorted(world.goods.values(), key=lambda g: (g.category, g.key)):
        local = region.goods[g.key]
        lo, hi = price_bounds(local.anchor)
        margin = local.price / local.unit_cost if local.unit_cost > 0 else 0
        print(f"{g.name:<14}{local.price:>10.2f}{local.unit_cost:>10.2f}{local.anchor:>10.2f}"
              f"{f'{lo:.1f} … {hi:.1f}':>22}{margin:>9.2f}"
              f"{local.stock:>14,.0f}{local.last_shortage:>9.1%}")
        if not (lo - 1e-6 <= local.price <= hi + 1e-6):
            problems.append(f"{g.name} вне коридора")
        if local.price != local.price or local.price <= 0 or local.price > 1e7:
            problems.append(f"{g.name}: цена разошлась ({local.price})")

    print("\n--- Сословия (вся страна) ---")
    print(f"{'сословие':<20}{'людей':>12}{'доля':>8}{'доход/чел':>12}"
          f"{'ур.жизни':>10}{'ожидания':>10}{'довольство':>12}{'сбережения':>15}")
    pop = world.population()
    totals = {}
    for key in config.STRATA_ORDER:
        people = sum(c.s(key).people for c in world.cities.values())
        income = sum(c.s(key).income for c in world.cities.values())
        cash = sum(c.s(key).cash for c in world.cities.values())
        wavg = lambda f: (sum(f(c.s(key)) * c.s(key).people
                              for c in world.cities.values()) / people
                          if people > 1 else 0)
        sat = wavg(lambda s: s.satisfaction)
        sol = wavg(lambda s: s.living_standard)
        exp = wavg(lambda s: s.expectation)
        totals[key] = (people, income, sat, sol)
        print(f"{config.STRATA[key]['name']:<20}{people:>12,.0f}{people / pop:>7.1%}"
              f"{income / people if people > 1 else 0:>12.2f}{sol:>10.2f}{exp:>10.2f}"
              f"{sat:>12.1%}{cash:>15,.0f}")

    # Какая роскошь вообще нашла покупателя — по всему миру, а не в одной
    # стране: разбогатеть первой может любая из двадцати.
    luxury_bought = {
        k: sum(c.goods[k].last_demand for c in world.cities.values()
               if k in c.goods)
        for k in config.LUXURY_BASKET if k in world.goods}

    first, last = history[0], history[-1]
    world_pop = world.population() or 1.0
    world_sol = sum(c.s(k).living_standard * c.s(k).people
                    for c in world.cities.values()
                    for k in config.STRATA_ORDER) / world_pop
    expectations = [c.s(k).expectation for c in world.cities.values()
                    for k in config.STRATA_ORDER if c.s(k).people > 1]
    print("\n--- Проверки ---")
    money_drift = last["money_supply"] / first["money_supply"] - 1
    pop_change = last["population"] / first["population"] - 1
    tail = grain_shortage[-20:]
    avg_shortage = sum(tail) / len(tail)
    treasury = sum(c.treasury for c in world.countries.values())
    checks = [
        ("население не схлопнулось", last["population"] > first["population"] * 0.6,
         f"{pop_change:+.1%} за {TICKS} пейдеев"),
        ("довольство не на нуле", last["satisfaction"] > 0.30,
         f"{last['satisfaction']:.1%}"),
        ("ИПЦ в рамках коридора", 0.4 < last["cpi"] < 3.6, f"{last['cpi']:.3f}"),
        ("денежная масса не взорвалась", abs(money_drift) < 0.35,
         f"{money_drift:+.1%}"),
        ("казны не в минусе", treasury >= -1,
         f"{treasury:,.0f} ₡"),
        ("роспись казны сходится с остатком", budget_drift < 1.0,
         f"максимальное расхождение {budget_drift:,.4f} ₡ за пейдей"),
        # Государство обязано УМЕТЬ выходить в плюс: налоги с населения дают
        # ему доход, не зависящий от того, есть ли в стране заводы.
        ("налоги с населения поступают",
         sum(v for co in world.countries.values()
             for k, v in co.last_budget.items()
             if k in ("poll_tax", "tithe", "wealth_tax", "excise")) > 0,
         "подать + оброк + налог на сбережения + акциз: "
         + ", ".join(
             f"{name} {sum(co.last_budget.get(key, 0.0) for co in world.countries.values()):,.0f} ₡"
             for key, name, _ in config.BUDGET_INCOME
             if key in ("poll_tax", "tithe", "wealth_tax", "excise"))),
        ("все цены внутри коридора", not problems,
         "; ".join(problems) or "ок"),
        ("страна сыта", avg_shortage < 0.20,
         f"средний дефицит зерна {avg_shortage:.1%} за 20 пейдеев"),
        ("крестьяне не разорены", totals["peasants"][2] > 0.30,
         f"довольство {totals['peasants'][2]:.1%}"),
        ("ВВП положительный", last["gdp"] > 0, f"{last['gdp']:,.0f}"),
        # Уровень жизни — безразмерная величина вокруг единицы. Если он уехал
        # к нулю или в десятки, значит эталон посчитан не так, и вся лестница
        # роскоши вместе с бонусами к довольству съедет следом.
        ("уровень жизни в разумных пределах",
         0.15 < world_sol < 3.0, f"{world_sol:.2f} (1.0 = обычная корзина)"),
        ("ожидания не упёрлись в потолок",
         all(config.EXPECTATION_MIN - 1e-6 <= e <= config.EXPECTATION_MAX + 1e-6
             for e in expectations),
         f"от {min(expectations):.2f} до {max(expectations):.2f}"),
        ("роскошь кому-то нужна", any(v > 1 for v in luxury_bought.values()),
         ", ".join(f"{world.goods[k].name} {v:,.0f}"
                   for k, v in luxury_bought.items() if v > 1) or "спроса нет"),
    ]

    if WITH_TRADE:
        from app.economy.engine import country_access
        caps = country_access(world)
        # Палаты подняли доступность выше базовой — значит, они и правда работают
        base = config.TRADE_ACCESS_BASE
        opened = {k: v for k, v in caps.items() if v > base + 1e-6}
        duties = sum(world.countries[k].treasury for k in opened)
        wp_ok = all(v > 0 and v == v and v < 1e7 for v in world.world_prices.values())
        # Единый рынок: у страны с палатами цены областей обязаны сойтись
        # ТЕСНЕЕ, чем у страны без них. Сравнение, а не абсолютный порог:
        # мировые потрясения двигают разрыв у всех сразу, и ловить надо
        # именно разницу между «с палатами» и «без».
        def price_spread(cids) -> float:
            out = []
            for cid in cids:
                regions = society.country_cities(world, world.countries[cid])
                if len(regions) < 2:
                    continue
                for g in world.goods.values():
                    if not g.storable:
                        continue
                    prices = [c.goods[g.key].price for c in regions
                              if g.key in c.goods]
                    mean = sum(prices) / len(prices) if prices else 0.0
                    if mean > 1e-6:
                        out.append((max(prices) - min(prices)) / mean)
            # Медиана, а не среднее: один товар, которого в стране нет вовсе,
            # болтается между краями коридора и в среднем перевешивает два
            # десятка сошедшихся. Нас интересует рынок в целом.
            if not out:
                return 0.0
            out.sort()
            return out[len(out) // 2]

        # Мерить надо там, где областям ЕСТЬ чем различаться: завод стоит в
        # одной области и сам по себе разводит цены соседей — вот эту разницу
        # палаты и обязаны стереть. В стране без промышленности области и так
        # одинаковы, и сравнивать нечего.
        industrial = {world.cities[b.city_id].country_id
                      for b in world.buildings.values()
                      if not world.players[b.owner_id].is_state}
        # И только те страны, где палаты стоят во ВСЕХ областях. Война
        # перекраивает границы: отбитая у соседа область приходит без палаты,
        # своим рынком и своими ценами — сращивать её будут ещё долго, и
        # спрашивать с механики за это нечестно.
        from app.economy.engine import region_access
        acc = region_access(world)
        covered = [k for k in opened
                   if all(acc.get(c.id, 0.0) > base + 1e-6
                          for c in society.country_cities(world, world.countries[k]))]
        watched = ([k for k in covered if k in industrial]
                   or covered or list(opened))
        spread = price_spread(watched)
        checks += [
            ("торговые палаты подняли доступность", len(opened) >= 3,
             f"{len(opened)} государств, доступность рынка "
             f"{max(opened.values()):.0%} при базовых {base:.0%}"),
            ("мировые цены адекватны", wp_ok,
             f"{len(world.world_prices)} товаров"),
            ("пошлины поступают в казну", duties > 0, f"{duties:,.0f} ₡"),
            # Доступность рынка на то и доступность: чем она выше, тем ближе
            # цены соседних областей одной страны.
            ("палаты сращивают рынки областей", spread < 0.20,
             f"разрыв цен между областями страны с палатами {spread:.1%} "
             f"(медиана по товарам; без палат доходит до 60%)"),
        ]

    if war_setup is not None:
        att, dfn = war_setup["attacker"], war_setup["defender"]
        att_regions = len(society.country_cities(world, att))
        dfn_regions = len(society.country_cities(world, dfn))
        annexed = [n for n in war_log["news"] if "занимает область" in n]
        print("\n--- Война ---")
        for name, co in (("нападающий", att), ("обороняющийся", dfn),
                         ("союзник", war_setup["ally"])):
            print(f"  {name:<16}{co.name:<16}армия {society.army_size(world, co):>10,.0f}"
                  f"  сила {society.army_strength(world, co):>10,.0f}"
                  f"  снаряды {co.army_shells:>10,.0f}"
                  f"  вооружены {co.army_equip:>6.0%}")
        print(f"  боёв проведено: {war_log['battles']}, "
              f"выбито уровней предприятий: {war_log['damaged']}")
        war_log["command_after"] = society.command_quality(
            society.officer_size(world, att), society.army_size(world, att))
        print(f"  офицеров выбито: {war_log['officers_lost']:,.0f}, "
              f"нанято из высшего общества: {war_log['officers_hired']:,.0f}; "
              f"командование у «{att.name}» {war_log['command_before']:.0%} → "
              f"{war_log['command_after']:.0%}")
        for line in war_log["news"][:5]:
            print(f"  · {line}")
        damaged_now = sum(1 for b in world.buildings.values() if b.damage > 0)
        checks += [
            ("война состоялась", war_log["battles"] > 0,
             f"{war_log['battles']} боёв"),
            ("война бьёт по промышленности", war_log["damaged"] > 0,
             f"{war_log['damaged']} уровней выбито, сейчас в руинах {damaged_now}"),
            # Офицеры гибнут на фронте — и это должно быть видно не только в
            # потерях, но и в качестве командования: армия воюет всё хуже, пока
            # корпус не восполнят наймом.
            ("офицеры гибнут на фронте", war_log["officers_lost"] > 0,
             f"выбито {war_log['officers_lost']:,.0f} офицеров"),
            ("корпус восполняется наймом из высшего общества",
             war_log["officers_hired"] > 0,
             f"нанято {war_log['officers_hired']:,.0f} офицеров"),
            ("сепаратный мир вынул только союзника", war_log["separate_peace"],
             "союзник вышел, война продолжилась" if war_log["separate_peace"]
             else "война оборвалась вместе с выходом союзника"),
            ("области перешли победителю", bool(annexed),
             f"{len(annexed)} переход(-ов); {att.name}: {att_regions} обл., "
             f"{dfn.name}: {dfn_regions} обл."),
            # Область меняет хозяина, но не появляется и не исчезает: карта
            # обязана сойтись по числу областей.
            ("области не потерялись", len(world.cities) == regions_before,
             f"{len(world.cities)} из {regions_before}"),
        ]

    if law_state:
        lob = law_state["lobbyist"]
        print("\n--- Законы и парламенты ---")
        seated = 0
        for label, co in law_state["setups"]:
            if not co.alive:
                print(f"  {label:<28} государство исчезло")
                continue
            seated += 1 if co.parties else 0
            forms = ", ".join(politics.option(co, cat)["name"]
                              for cat in config.LAW_ORDER)
            print(f"  {co.name:<12}{label:<28}{forms}")
            print(f"  {'':<12}палата: "
                  + (", ".join(f"{p.name} — {p.seats}" for p in co.parties)
                     or "не созывалась"))
        # ГЛАВНАЯ проверка этого прогона — деньги. Лоббирование не должно ни
        # создавать червонцы из ничего, ни сжигать их: всё, что списано с
        # игрока, обязано осесть в кошельках сословий или в казне.
        state_lines = {key: sum(co.last_budget.get(key, 0.0)
                                for co in world.countries.values())
                       for key in ("serfdom", "redistribution", "parliament",
                                   "army_levy", "law_fee")}
        print(f"  вложено лоббистом: {law_state['spent']:,.0f} ₡, "
              f"куплено голосов: {law_state['bought']:,.0f}, "
              f"осталось кассы: {lob.cash:,.0f} ₡")
        print("  статьи законов за последний пейдей: "
              + ", ".join(f"{k} {v:,.0f}" for k, v in state_lines.items()))
        # Запреты на принятие законов. Проверяются не тем, что «где-то что-то
        # заблокировалось», а поимённо: социализм не берётся ни при монархии,
        # ни при цензовом праве, а коллективное хозяйство — без социализма.
        # Ошибись знак в law_blocked, и без этой проверки всё выглядело бы
        # исправным — законы просто перестали бы запрещаться.
        probe = next((co for _l, co in law_state["setups"] if co.alive), None)
        crown = next((co for co in world.countries.values()
                      if co.alive and politics.law(co, "state_form") == "monarchy"),
                     None)
        bans = []
        if crown is not None:
            bans.append(("социализм не принять при монархии",
                         bool(politics.law_blocked(crown, "ideology", "socialism"))))
            bans.append(("коллективное хозяйство не принять без социализма",
                         bool(politics.law_blocked(crown, "land", "collective"))))
        if probe is not None:
            bans.append(("законы расставлены без нежданных запретов",
                         not law_state["blocked"]))
        checks += [
            ("парламенты собрались", seated >= 3,
             f"{seated} палат(ы) из {len(law_state['setups'])}"),
            ("законы выносились на голосование", law_state["votes"] > 0,
             f"{law_state['votes']} голосований"),
            # Палата обязана двигать страну сама, без лидера и без лоббиста:
            # раз в PARLIAMENT_BILL_TICKS пейдеев она выносит свой законопроект.
            ("парламент вносит законы сам", law_state["bills"] > 0,
             f"{law_state['bills']} собственных законопроектов палат"),
            *[(name, ok, "запрет держится" if ok else "ЗАКОН ПРОШЁЛ, ХОТЯ НЕ ДОЛЖЕН")
              for name, ok in bans],
            ("лоббирование покупает голоса", law_state["bought"] > 1,
             f"{law_state['bought']:,.1f} мест за {law_state['spent']:,.0f} ₡"),
            # «Крепостное право» живёт при монархии, «дополнительное
            # распределение» — при социализме, содержание палаты — везде, где
            # есть выборы. Хотя бы одна из статей обязана быть ненулевой,
            # иначе законы приняты, а денег по ним не движется вовсе.
            ("статьи законов работают",
             any(abs(v) > 1 for v in state_lines.values()),
             ", ".join(f"{k} {v:,.0f} ₡" for k, v in state_lines.items()
                       if abs(v) > 1) or "все статьи пусты"),
        ]

    if school_state:
        print("\n--- Просвещение, бумага и революция ---")
        print(f"  {'страна':<14}{'уклад':<12}{'грамот.':>9}{'обида':>8}"
              f"{'годны':>7}{'бумаги куплено':>16}{'восст.':>8}"
              f"{'бои':>5}{'перебеж.':>10}{'погибло':>10}{'население':>12}")
        rows = []
        for row in school_state["setups"]:
            co = row["country"]
            alive = co.alive and world.country_regions(co.id)
            edu = society.country_education(world, co) if alive else 0.0
            griev = society.country_grievance(world, co) if alive else 0.0
            pool = society.worker_pool_share(edu)
            pop = society.country_population(world, co) if alive else 0.0
            rows.append((row, edu, griev, pool, pop))
            print(f"  {co.name:<14}{row['label']:<12}{edu:>8.1%}{griev:>8.1%}"
                  f"{pool:>7.0%}{row['paper_bought']:>16,.0f}"
                  f"{row['risen']:>8}{row['wars']:>5}{row['defected']:>10,.0f}"
                  f"{row['losses']:>10,.0f}{pop / max(row['pop0'], 1):>11.0%}")
        schooled = [x for x in rows if x[0]["schooled"]]
        dark = [x for x in rows if not x[0]["schooled"]]
        grew = lambda x: x[4] / max(x[0]["pop0"], 1.0)
        conceding = next((x for x in rows if x[0]["concede"]), None)
        stubborn = next((x for x in rows
                         if x[0]["schooled"] and not x[0]["concede"]), None)
        checks += [
            # Школы обязаны УЧИТЬ, и учить заметно: без этого вся ветка —
            # дорогое здание, съедающее бумагу.
            ("школы поднимают грамотность",
             all(edu > 0.35 for _r, edu, _g, _p, _n in schooled),
             ", ".join(f"{r['country'].name} {edu:.0%}"
                       for r, edu, _g, _p, _n in schooled)),
            # И обязаны открывать деревне дорогу на завод: в этом их
            # хозяйственный смысл, а не только политический.
            ("грамотность расширяет наём на завод",
             all(pool > society.worker_pool_share(0.0) * 1.8
                 for _r, _e, _g, pool, _n in schooled),
             "годных к станку "
             + ", ".join(f"{r['country'].name} {pool:.0%}"
                         for r, _e, _g, pool, _n in schooled)
             + f" при {society.worker_pool_share(0.0):.0%} у неграмотной страны"),
            # Тёмная страна обязана остаться тёмной: если грамотность растёт и
            # без школ, ни закон об образовании, ни школы никому не нужны.
            ("без школ страна остаётся неграмотной",
             all(edu < 0.20 for _r, edu, _g, _p, _n in dark),
             ", ".join(f"{r['country'].name} {edu:.0%}"
                       for r, edu, _g, _p, _n in dark) or "не с чем сравнить"),
            # Бумага — единственная отрасль с чисто казённым покупателем.
            # Не покупают её — значит, ни школы, ни палата за неё не платят.
            ("бумага находит казённого покупателя",
             all(r["paper_bought"] > 100 for r, *_ in schooled),
             ", ".join(f"{r['country'].name} {r['paper_bought']:,.0f}"
                       for r, *_ in schooled)),
            # Грамотность без прав обязана обернуться требованиями. Не
            # обернулась — значит самосознание ни на что не влияет.
            ("выученная страна требует прав",
             all(r["risen"] > 0 for r, *_ in schooled),
             ", ".join(f"{r['country'].name}: "
                       f"{'восставали' if r['risen'] else 'молчат'}"
                       for r, *_ in schooled)),
            # Молчание лидера обязано приводить к войне, а не рассасываться.
            ("молчание лидера доводит до гражданской войны",
             stubborn is not None and stubborn[0]["wars"] > 0,
             f"{stubborn[0]['wars']} пейдеев боёв" if stubborn else "нет такой страны"),
            # А уступки — обязаны от неё избавлять. Это и есть выбор, ради
            # которого вся механика затевалась.
            ("уступки лидера отменяют войну",
             conceding is not None and conceding[0]["accepted"] > 0
             and conceding[0]["wars"] == 0,
             f"принято требований {conceding[0]['accepted']}, боёв "
             f"{conceding[0]['wars']}" if conceding else "нет такой страны"),
            # ГЛАВНАЯ проверка всей ветки. Победившая революция вводит ровно
            # те же законы, которые лидер мог подписать без единого выстрела, —
            # значит, разницу между уступкой и упрямством надо искать не в
            # законах, а в ЦЕНЕ. Упрямый обязан заплатить людьми: погибшими в
            # столице, разбежавшейся армией и месяцами работы вполсилы, из-за
            # которых у него и школы стоят пустыми. Не окажись этой разницы —
            # выбор лидера был бы мнимым, и отвечать восставшим не имело бы
            # никакого смысла.
            ("упрямство обходится дороже уступок",
             conceding is not None and stubborn is not None
             and stubborn[0]["losses"] > 0
             and grew(conceding) > grew(stubborn),
             (f"погибло {stubborn[0]['losses']:,.0f} против "
              f"{conceding[0]['losses']:,.0f}; население "
              f"{grew(conceding):.0%} у уступившего против "
              f"{grew(stubborn):.0%} у упрямого")
             if conceding and stubborn else "не с чем сравнить"),
        ]

    if bot is not None:
        blds = world.player_buildings(bot.id)
        worth = world.net_worth(bot)
        employed = sum(b.employed for b in blds)
        profit = sum(b.last_profit for b in blds)
        home = bot_home()
        cap_city = world.cities.get(home.capital_city_id) if home else None
        print("\n--- Бот-промышленник ---")
        print(f"  предприятий: {len(blds)} "
              f"({', '.join(sorted({world.industries[b.industry_key].name for b in blds})) or '—'})")
        print(f"  нанял:       {employed:,.0f} рабочих")
        print(f"  зарплата:    {(sum(b.wage * b.employed for b in blds) / employed) if employed > 1 else 0:.2f} ₡ "
              f"(в деревне {rural_income(world, cap_city) if cap_city else 0:.2f} ₡)")
        print(f"  капитал:     {worth:,.0f} ₡ (старт {config.STARTING_CAPITAL:,.0f} ₡)")
        print(f"  прибыль:     {profit:,.0f} ₡ за пейдей")
        print(f"  худшая касса:{worst_cash:,.0f} ₡")
        checks += [
            ("игрок смог построиться", len(blds) > 0, f"{len(blds)} предприятий"),
            ("игрок нанял рабочих", employed > 1000, f"{employed:,.0f} человек"),
            # Уходить в минус теперь можно: предприятия работают в долг до
            # порога банкротства. Нельзя пробить сам порог и встать.
            ("игрок не разорился",
             not bot.bankrupt and worst_cash > world.countries[bot.country_id].bankruptcy_limit,
             f"минимум кассы {worst_cash:,.0f} ₡ при пороге "
             f"{world.countries[bot.country_id].bankruptcy_limit:,.0f} ₡"),
            ("бизнес окупается", worth > config.STARTING_CAPITAL,
             f"{worth / config.STARTING_CAPITAL:.2f}× от старта"),
        ]

    failed = 0
    for name, ok, detail in checks:
        print(f"  [{'OK ' if ok else 'FAIL'}] {name:<32} {detail}")
        failed += 0 if ok else 1

    print(f"\n{len(checks) - failed}/{len(checks)} проверок пройдено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
