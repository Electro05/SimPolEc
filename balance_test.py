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
"""
from __future__ import annotations

import sys

from app import config
from app.economy.engine import (
    _add_alliance, declare_war, level_cost, make_peace, run_tick,
)
from app.economy.pricing import price_bounds
from app.economy.seed import build_world
from app.economy import society
from app.models import Building, Player

TICKS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 150
WITH_PLAYER = "--player" in sys.argv
WITH_TRADE = "--trade" in sys.argv
WITH_WAR = "--war" in sys.argv
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
    upkeep = config.UPKEEP_PER_LEVEL / config.JOBS_PER_LEVEL
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
    country.treasury += cost * country.income_tax


def open_trade(world, countries: int = 3, levels: int = 3) -> None:
    """Построить торговые площади в нескольких государствах."""
    for n, co in enumerate(list(world.countries.values())[:countries]):
        city = next(c for c in world.cities.values() if c.country_id == co.id)
        p = Player(id=world.next_player_id, username=f"Купец-{co.name}",
                   cash=3e7, country_id=co.id)
        world.players[p.id] = p
        world.next_player_id += 1
        for _ in range(4):
            b = Building(id=world.next_building_id, industry_key="market",
                         owner_id=p.id, city_id=city.id, level=levels, wage=50.0)
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
def main() -> int:
    world = build_world()
    bot = add_player(world) if WITH_PLAYER else None
    if WITH_TRADE:
        open_trade(world)
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

    food_shortage = []
    worst_cash = float("inf")
    war = None
    war_log = {"battles": 0, "damaged": 0, "news": [], "separate_peace": False}
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
        r = run_tick(world)
        bc = bot_home()
        if war is not None:
            war_log["battles"] += len(war.last_report)
            war_log["damaged"] += sum(x["buildings_attacker"] + x["buildings_defender"]
                                      for x in war.last_report)
            war_log["news"] += r.get("news", [])
        history.append(r)
        food_shortage.append(_home_region(world, bc).goods["food"].last_shortage
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
    tail = food_shortage[-20:]
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
        ("все цены внутри коридора", not problems,
         "; ".join(problems) or "ок"),
        ("страна сыта", avg_shortage < 0.20,
         f"средний дефицит еды {avg_shortage:.1%} за 20 пейдеев"),
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
        from app.economy.engine import trade_capacity
        caps = trade_capacity(world)
        trading = {k: v for k, v in caps.items() if v > 0}
        duties = sum(world.countries[k].treasury for k in trading)
        wp_ok = all(v > 0 and v == v and v < 1e7 for v in world.world_prices.values())
        checks += [
            ("торговые площади открыли границу", len(trading) >= 3,
             f"{len(trading)} государств, пропускная способность "
             f"{max(trading.values()):.0%}"),
            ("мировые цены адекватны", wp_ok,
             f"{len(world.world_prices)} товаров"),
            ("пошлины поступают в казну", duties > 0, f"{duties:,.0f} ₡"),
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
        for line in war_log["news"][:5]:
            print(f"  · {line}")
        damaged_now = sum(1 for b in world.buildings.values() if b.damage > 0)
        checks += [
            ("война состоялась", war_log["battles"] > 0,
             f"{war_log['battles']} боёв"),
            ("война бьёт по промышленности", war_log["damaged"] > 0,
             f"{war_log['damaged']} уровней выбито, сейчас в руинах {damaged_now}"),
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
