"""
Механика рыночных цен с мягкими границами.

Идея
----
У каждого товара есть "якорь" — сглаженная себестоимость производства
(сырьё по текущим ценам + труд + содержание), умноженная на нормальную маржу.
Якорь сам плавает вместе с экономикой: подорожало сырьё — подорожал и якорь.

Цена ходит вокруг якоря в коридоре [FLOOR x anchor, CEIL x anchor], например
от 0.55 до 4.0 себестоимости. Но коридор — мягкий: вместо жёсткого обрезания
мы работаем в логарифмическом пространстве и прогоняем результат через tanh.

    u = ln(price / anchor)                 # отклонение от якоря в логах
    u += elasticity * imbalance            # толчок от спроса/предложения
    u  = u_max * tanh(u / u_max)           # насыщение у границы

tanh даёт три нужных свойства:
  1. около якоря tanh(x) ~ x, то есть цена движется свободно и линейно;
  2. у границы производная стремится к нулю — чем дальше от якоря, тем
     труднее сдвинуть цену дальше (естественное сопротивление рынка);
  3. границы недостижимы математически — цена никогда не станет 0 и никогда
     не улетит в бесконечность, без некрасивых "стенок".

Поэтому хлеб в кризис может подорожать втрое, а мебель при затоваривании
упасть почти вдвое ниже себестоимости — но не в 100 раз.
"""
from __future__ import annotations

import math

from ..config import (
    ANCHOR_MARKUP, ANCHOR_SMOOTHING, MAX_LOG_STEP, PRICE_CEIL_MULT,
    PRICE_ELASTICITY, PRICE_FLOOR_MULT, SCARCITY_KICK,
)

U_MAX = math.log(PRICE_CEIL_MULT)         # > 0
U_MIN = abs(math.log(PRICE_FLOOR_MULT))   # > 0


def imbalance(demand: float, supply: float) -> float:
    """Нормированный дисбаланс в диапазоне [-1, 1].

    +1 — спрос есть, предложения нет вообще (острый дефицит)
    -1 — товар произведён, но никому не нужен (затоваривание)
     0 — рынок сбалансирован
    """
    total = demand + supply
    if total <= 1e-9:
        return 0.0
    return (demand - supply) / total


def soft_bound(u: float) -> float:
    """Насыщение отклонения u = ln(price/anchor) внутри мягкого коридора."""
    if u >= 0.0:
        return U_MAX * math.tanh(u / U_MAX)
    return -U_MIN * math.tanh(-u / U_MIN)


def update_anchor(prev_anchor: float, unit_cost: float) -> float:
    """Сглаженный якорь: EMA от себестоимости с нормальной наценкой."""
    target = max(unit_cost * ANCHOR_MARKUP, 0.05)
    a = ANCHOR_SMOOTHING
    return prev_anchor * a + target * (1.0 - a)


def next_price(
    price: float,
    anchor: float,
    demand: float,
    supply: float,
    stock: float,
    elasticity: float = PRICE_ELASTICITY,
) -> float:
    """Цена следующего тика."""
    anchor = max(anchor, 1e-6)
    price = max(price, 1e-6)

    u = math.log(price / anchor)

    imb = imbalance(demand, supply)
    delta = elasticity * imb

    # Пустой склад при живом спросе — отдельный толчок вверх: покупатели
    # реально не смогли купить, а не просто "хотели бы дешевле".
    if stock <= 1e-9 and demand > 1e-9:
        delta += SCARCITY_KICK
    # Склад забит выше двух тиков спроса — давление вниз, надо разгружать.
    elif demand > 1e-9 and stock > demand * 2.0:
        glut = min((stock / max(demand, 1e-9) - 2.0) / 6.0, 1.0)
        delta -= SCARCITY_KICK * glut

    # Страховка от резонанса: цена не прыгает больше чем на MAX_LOG_STEP за тик
    delta = max(-MAX_LOG_STEP, min(MAX_LOG_STEP, delta))

    return anchor * math.exp(soft_bound(u + delta))


def price_bounds(anchor: float) -> tuple[float, float]:
    """Фактический коридор цены для отображения в интерфейсе."""
    return anchor * PRICE_FLOOR_MULT, anchor * PRICE_CEIL_MULT


def demand_response(base_qty: float, price: float, anchor: float,
                    elasticity: float) -> float:
    """Ценовая эластичность спроса.

    Дорого — покупают меньше, дёшево — больше. Считаем относительно якоря,
    а не абсолютной цены, чтобы механика не ломалась при общей инфляции.
    """
    if anchor <= 1e-9 or base_qty <= 0.0:
        return max(base_qty, 0.0)
    ratio = max(price / anchor, 1e-6)
    factor = ratio ** (-elasticity)
    # спрос не может вырасти больше чем в 1.6 раза и упасть ниже 25% от нормы
    return base_qty * max(0.25, min(1.6, factor))
