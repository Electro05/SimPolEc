"""
Расчёт стартового равновесия государства доиндустриальной страны.

Не часть игры — инструмент проектирования. Отвечает на вопросы, которые
приходится задавать при каждой правке баланса:

* хватает ли крестьян, чтобы прокормить страну своим хозяйством;
* сколько кустари способны дать против спроса;
* сходятся ли доходы горожан от услуг с их же потреблением;
* каких товаров в стране нет вовсе — это и есть ниши для игрока.

    python tools/design_check.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config                                    # noqa: E402
from app.economy import society                           # noqa: E402
from app.economy.seed import START_SHARES, build_world    # noqa: E402


def main() -> None:
    world = build_world()
    # возьмём первое государство как образец
    country = next(iter(world.countries.values()))
    city = world.cities[country.capital_city_id]
    pop = city.population
    # рынок живёт в области, поэтому цены берём у столичной
    price = {k: g.price for k, g in city.goods.items()}
    artisan_crafts = society.artisan_crafts(world)

    counts = {k: pop * share for k, share in START_SHARES.items()}
    equivalents = sum(n * config.STRATA[k]["level"] for k, n in counts.items())

    print(f"Государство: {country.name} "
          f"({len(world.country_regions(country.id))} обл.), столица {city.name}")
    print(f"Население столичной области: {pop:,.0f}")
    print(f"В пересчёте на эталонного потребителя: {equivalents:,.0f}\n")

    print(f"{'сословие':<20}{'людей':>12}{'доля':>8}{'потребляет как':>16}")
    for key, n in counts.items():
        lvl = config.STRATA[key]["level"]
        print(f"{config.STRATA[key]['name']:<20}{n:>12,.0f}{n / pop:>7.1%}"
              f"{n * lvl:>16,.0f}")

    # ---------------- спрос ----------------
    print("\n--- Спрос и предложение на старте (за пейдей) ---")
    print(f"{'товар':<14}{'спрос':>14}{'даёт деревня':>16}{'покрытие':>10}"
          f"{'кто делает':>22}")

    peasants = counts["peasants"]
    artisans = counts["artisans"]

    per_craft = artisans / max(len(artisan_crafts), 1)
    supply: dict[str, float] = {}
    maker: dict[str, str] = {}

    # Надел считается ПО ДЕЙСТВУЮЩЕМУ ЗЕМЕЛЬНОМУ ЗАКОНУ, а не по чертежу:
    # крепостное право, с которого мир начинается, оставляет крестьянину четыре
    # пятых силы, и сводка обязана показывать ту деревню, что есть на самом деле.
    for good in config.PEASANT_YIELD:
        supply[good] = (supply.get(good, 0.0)
                        + peasants * society.peasant_yield(country, good))
        maker[good] = "крестьяне"
    for craft, spec in artisan_crafts.items():
        supply[craft] = supply.get(craft, 0.0) + per_craft * spec["out"]
        maker[craft] = ("крестьяне и кустари" if craft in config.PEASANT_YIELD
                        else "кустари")
    services = sum(counts[k] * out for k, out in config.SERVICE_OUTPUT.items()
                   if out > 0)
    supply["services"] = services
    maker["services"] = "горожане"

    # сырьё, которое кустари сами же и потребляют
    used: dict[str, float] = {}
    for craft, spec in artisan_crafts.items():
        for src, per_unit in spec["inputs"].items():
            used[src] = used.get(src, 0.0) + per_craft * spec["out"] * per_unit

    missing = []
    for key, spec in config.CONSUMPTION_BASKET.items():
        demand = equivalents * spec["qty"]
        have = supply.get(key, 0.0)
        cover = have / demand if demand > 0 else 1.0
        if have <= 0:
            missing.append(world.goods[key].name)
        print(f"{world.goods[key].name:<14}{demand:>14,.0f}{have:>16,.0f}"
              f"{cover:>9.0%}{maker.get(key, '—'):>22}")

    print("\n--- Сырьё для кустарей ---")
    print(f"{'товар':<14}{'дают крестьяне':>16}{'берут кустари':>16}{'остаток':>14}")
    for key in ("grain", "wood", "cotton"):
        have, need = supply.get(key, 0.0), used.get(key, 0.0)
        print(f"{world.goods[key].name:<14}{have:>16,.0f}{need:>16,.0f}"
              f"{have - need:>14,.0f}")

    # ---------------- деньги ----------------
    print("\n--- Сходятся ли доходы с потреблением ---")
    material = sum(spec["qty"] * price[k]
                   for k, spec in config.CONSUMPTION_BASKET.items()
                   if k != "services")
    services_cost = config.CONSUMPTION_BASKET["services"]["qty"] * price["services"]
    basket = material + services_cost
    print(f"Корзина эталонного потребителя: {basket:.2f} ₡ "
          f"(из них услуги {services_cost:.2f} ₡)")
    print(f"Потребление страны за пейдей:   {equivalents * basket:,.0f} ₡")

    town = sum(counts[k] for k in ("town_low", "town_mid", "town_high"))
    town_eq = sum(counts[k] * config.STRATA[k]["level"]
                  for k in ("town_low", "town_mid", "town_high"))
    town_income = services * price["services"]
    print(f"\nГорожан {town:,.0f}, потребляют на {town_eq * basket:,.0f} ₡, "
          f"зарабатывают услугами {town_income:,.0f} ₡ "
          f"({town_income / (town_eq * basket):.0%} покрытия)")

    village_eq = sum(counts[k] * config.STRATA[k]["level"]
                     for k in ("peasants", "artisans"))
    village_sales = sum(
        max(0.0, supply.get(k, 0.0) - used.get(k, 0.0)) * price[k]
        for k in list(config.PEASANT_YIELD) + list(artisan_crafts))
    own_grain = peasants * config.CONSUMPTION_BASKET["grain"]["qty"] \
        * config.STRATA["peasants"]["level"]
    print(f"Деревня {peasants + artisans:,.0f}, потребляет на "
          f"{village_eq * basket:,.0f} ₡, продаёт на {village_sales:,.0f} ₡ "
          f"плюс ест своего хлеба на {own_grain * price['grain']:,.0f} ₡")

    # ---------------- лестница еды ----------------
    # Четыре ступени, от дешёвой сытости к дорогому столу. Здесь видно главное:
    # во что обходится каждая и насколько она дороже предыдущей.
    print("\n--- Лестница еды ---")
    print(f"{'ступень':<18}{'на корзину':>12}{'цена':>10}{'выходит, ₡':>13}"
          f"{'кто делает':>26}")
    ladder = [("grain", config.CONSUMPTION_BASKET["grain"]["qty"]),
              ("provisions", config.CONSUMPTION_BASKET["provisions"]["qty"]),
              ("meat", config.LUXURY_BASKET["meat"]["qty"])]
    for key, qty in ladder:
        makers = [i.name for i in world.industries.values()
                  if i.output_good == key]
        if key in config.PEASANT_YIELD:
            makers.insert(0, "крестьяне")
        print(f"{world.goods[key].name:<18}{qty:>12.3f}{price[key]:>10.2f}"
              f"{qty * price[key]:>13.2f}{', '.join(makers) or '—':>26}")

    # ---------------- крестьянский труд на фермах ----------------
    print("\n--- Ферма: наём крестьян ---")
    farm = world.industries["farm"]
    alt = society.peasant_alternative(city, country)
    print(f"Своё поле даёт крестьянину {alt:.2f} ₡ за пейдей "
          f"(зерна {society.peasant_yield(country, 'grain'):.2f} "
          f"по {price['grain']:.2f} ₡ плюс лес и хлопок)")
    print(f"На ферму он выйдет от {alt * config.FARM_WAGE_EDGE:.2f} ₡ "
          f"и выйдет весь от {alt * config.FARM_WAGE_FULL:.2f} ₡")
    upkeep_per_hand = config.UPKEEP_PER_LEVEL / farm.jobs_per_level
    print(f"Ферма даёт {farm.output_per_worker:.2f} зерна с человека = "
          f"{farm.output_per_worker * price['grain']:.2f} ₡ выручки; "
          f"мест на уровень {farm.jobs_per_level:,}")
    for mult in (1.0, config.FARM_WAGE_EDGE, config.FARM_WAGE_FULL):
        wage = alt * mult
        margin = farm.output_per_worker * price["grain"] - wage - upkeep_per_hand
        print(f"  при ставке {wage:>6.2f} ₡ хозяину остаётся "
              f"{margin:>7.2f} ₡ с человека за пейдей")

    # ---------------- армия ----------------
    print("\n--- Армия и оружейная промышленность ---")
    soldiers = counts.get("soldiers", 0.0)
    officers = counts.get("officers", 0.0)
    slot = society.soldier_slot_cost(country)
    print(f"Солдат на старте: {soldiers:,.0f}, офицеров {officers:,.0f} "
          f"({officers / max(soldiers, 1):.1%} при штате "
          f"{config.OFFICER_TARGET_SHARE:.0%})")
    print(f"  место в строю обходится в {slot:.2f} ₡ "
          f"(солдат {config.SOLDIER_PAY_DEFAULT:.0f} ₡ плюс доля офицера по "
          f"{society.officer_pay(country):.0f} ₡), "
          f"бюджет {soldiers * slot:,.0f} ₡")
    print(f"  качество командования при полном штате: "
          f"{society.command_quality(officers, soldiers):.0%} к боевой силе фронта; "
          f"без единого офицера — {config.COMMAND_MIN:.0%}")
    # Склады армии: слева ЗАПАС (вооружённость), справа РАСХОД за пейдей
    # (потребление). Заводы кормит именно правая колонка.
    weapons_need = soldiers * config.WEAPONS_PER_SOLDIER
    weapons_wear = weapons_need * config.WEAPONS_WEAR
    print(f"  {world.goods['weapons'].name:<12} штат {weapons_need:>12,.0f}"
          f"   износ за пейдей {weapons_wear:>9,.0f}"
          f"   = {weapons_wear * price['weapons']:>10,.0f} ₡ заводам")
    shells_need = soldiers * config.SHELLS_RESERVE_PER_SOLDIER
    shells_burn = soldiers * config.SHELLS_PEACETIME_BURN
    print(f"  {world.goods['shells'].name:<12} резерв {shells_need:>12,.0f}"
          f"   расход за пейдей {shells_burn:>8,.0f}"
          f"   = {shells_burn * price['shells']:>10,.0f} ₡ заводам")
    print(f"  расход за бой: снарядов "
          f"{soldiers * config.SHELLS_PER_SOLDIER_BATTLE:,.0f}, оружия "
          f"{soldiers * config.WEAPONS_BATTLE_LOSS:,.0f}")
    # во что обойдётся единица оружия, если поднять всю цепочку
    for key in ("weapons", "shells"):
        ind = next(i for i in world.industries.values() if i.output_good == key)
        cost = society.notional_unit_cost(world, city, key,
                                          society.reference_wage(world, country))
        print(f"  {world.goods[key].name:<12} по рецепту {cost or 0:>9.2f} ₡"
              f"   рынок {price[key]:>9.2f} ₡"
              f"   ({', '.join(f'{world.goods[g].name} ×{q}' for g, q in ind.inputs.items())})")

    # ---------------- уровень жизни ----------------
    print("\n--- Уровень жизни и роскошь ---")
    print("Уровень жизни: 1.0 = полная обычная корзина сословия по якорным ценам.")
    print(f"{'сословие':<20}{'корзина, ₡':>13}{'покупает, ₡':>14}"
          f"{'сбережений':>13}{'ожидания':>11}")
    for key, n in counts.items():
        if n <= 0:
            continue
        st = city.s(key)
        full = society.normal_basket_value(city, key)
        own = (config.CONSUMPTION_BASKET["grain"]["qty"]
               * config.STRATA[key]["level"] * price["grain"]) if key == "peasants" else 0.0
        buy = max(full - own, 1e-9)
        exp = society.target_expectation(st, buy)
        print(f"{config.STRATA[key]['name']:<20}{full:>13.2f}{buy:>14.2f}"
              f"{st.cash / n:>13.2f}{exp:>11.2f}")

    print(f"\n{'роскошь':<20}{'открывается с':>15}{'кто дотянулся на старте':>28}")
    for good, spec in sorted(config.LUXURY_BASKET.items(),
                             key=lambda kv: kv[1]["unlock"]):
        reached = []
        for key, n in counts.items():
            if n <= 0:
                continue
            st = city.s(key)
            full = society.normal_basket_value(city, key)
            own = (config.CONSUMPTION_BASKET["grain"]["qty"]
                   * config.STRATA[key]["level"] * price["grain"]) if key == "peasants" else 0.0
            if society.target_expectation(st, max(full - own, 1e-9)) >= spec["unlock"]:
                reached.append(config.STRATA[key]["name"])
        print(f"{world.goods[good].name:<20}{spec['unlock']:>15.2f}"
              f"{', '.join(reached) or 'никто':>28}")

    # ---- просвещение и бумага ------------------------------------------
    # Два числа, которых не видно ниоткуда больше, а балансировать надо именно
    # их: сколько уровней школы нужно СТРАНЕ ЭТОГО РАЗМЕРА, чтобы выучить её
    # заметную долю, и сколько уровней бумажной фабрики прокормят эти школы.
    #
    # Равновесие грамотности выводится из того, что она тает: доля образованных
    # держится там, где выучиваемые за пейдей ровно возмещают убыль поколений,
    # то есть ёмкость = население × доля × EDU_DECAY.
    print("\n--- Просвещение: сколько школ нужно стране ---")
    country_pop = society.country_population(world, country)
    school = world.industries["school"]
    uni = world.industries["academy"]
    paper_mill = world.industries["papermill"]
    print(f"Население государства: {country_pop:,.0f}; грамотность тает на "
          f"{config.EDU_DECAY:.1%} за пейдей")
    print(f"{'доля грамотных':>16}{'учить за пейдей':>18}{'уровней школы':>15}"
          f"{'уровней университета':>22}")
    for share in (0.25, 0.50, 0.75, 0.95):
        need = country_pop * share * config.EDU_DECAY
        print(f"{share:>15.0%}{need:>18,.0f}"
              f"{need / school.education:>15.1f}{need / uni.education:>22.1f}")
    # Бумага. Её покупает одно государство, поэтому «спрос» — это буквально
    # содержание казённых зданий плюс делопроизводство палаты.
    print("\n--- Бумага: единственный товар с казённым покупателем ---")
    seats = config.PARLIAMENT_SEATS_DEFAULT
    rows = [(world.industries[k].name, world.industries[k].upkeep_goods.get("paper", 0.0))
            for k in ("school", "academy", "townhall", "trade_chamber", "opera")]
    rows.append((f"парламент ({seats} мест)",
                 seats * config.PARLIAMENT_PAPER_PER_SEAT))
    mill = paper_mill.output_per_worker * paper_mill.jobs_per_level
    print(f"{'потребитель':<26}{'бумаги за пейдей':>18}{'от уровня фабрики':>20}")
    for name, qty in rows:
        print(f"{name:<26}{qty:>18,.0f}{qty / mill:>19.1%}")
    print(f"{'уровень бумажной фабрики':<26}{mill:>18,.0f}{1.0:>19.0%}")
    print(f"При «праве образования для всех» расход бумаги удваивается "
          f"(paper_mult = 2.0).")

    print("\n--- Ниши для игрока ---")
    print("В стране никто не производит:",
          ", ".join(missing) or "всё есть")


if __name__ == "__main__":
    main()
