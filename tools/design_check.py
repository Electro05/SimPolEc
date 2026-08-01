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

    for good, per_head in config.PEASANT_YIELD.items():
        supply[good] = supply.get(good, 0.0) + peasants * per_head
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
    own_food = peasants * config.CONSUMPTION_BASKET["food"]["qty"] \
        * config.STRATA["peasants"]["level"]
    print(f"Деревня {peasants + artisans:,.0f}, потребляет на "
          f"{village_eq * basket:,.0f} ₡, продаёт на {village_sales:,.0f} ₡ "
          f"плюс ест своего на {own_food * price['food']:,.0f} ₡")

    # ---------------- армия ----------------
    print("\n--- Армия и оружейная промышленность ---")
    soldiers = counts.get("soldiers", 0.0)
    print(f"Солдат на старте: {soldiers:,.0f} "
          f"(бюджет {soldiers * config.SOLDIER_PAY_DEFAULT:,.0f} ₡ "
          f"при ставке {config.SOLDIER_PAY_DEFAULT:.0f} ₡)")
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
        own = (config.CONSUMPTION_BASKET["food"]["qty"]
               * config.STRATA[key]["level"] * price["food"]) if key == "peasants" else 0.0
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
            own = (config.CONSUMPTION_BASKET["food"]["qty"]
                   * config.STRATA[key]["level"] * price["food"]) if key == "peasants" else 0.0
            if society.target_expectation(st, max(full - own, 1e-9)) >= spec["unlock"]:
                reached.append(config.STRATA[key]["name"])
        print(f"{world.goods[good].name:<20}{spec['unlock']:>15.2f}"
              f"{', '.join(reached) or 'никто':>28}")

    print("\n--- Ниши для игрока ---")
    print("В стране никто не производит:",
          ", ".join(missing) or "всё есть")


if __name__ == "__main__":
    main()
