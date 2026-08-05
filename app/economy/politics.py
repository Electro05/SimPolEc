"""
ЗАКОНЫ, ПАРЛАМЕНТ И ЛОББИРОВАНИЕ.

Здесь живёт вторая половина государства — та, что не про деньги, а про власть.
Экономика отвечает на вопрос «сколько», политика — на вопрос «кому позволено».

Устройство простое и держится на трёх вещах.

    ЗАКОН. Семь категорий (config.LAWS), в каждой ровно один действующий
        закон. Закон не прибавляет процентов к выпуску — он переписывает
        правила: закрывает лидеру часть ползунков, меняет, из какого сословия
        берут офицеров, переделяет землю под деревней, решает, кого учить
        грамоте и что позволено выучившемуся, и чей голос вообще считается.
        Мир начинается одинаково у всех: монархия, авторитаризм, крепостное
        право, сословное образование, бесправие рабочих, свободная торговля,
        идеологии нет.
    ПАРЛАМЕНТ. Пока в стране авторитаризм, палаты нет вовсе и лидер правит
        указом. Стоит появиться выборам — собирается созыв: партии не прописаны
        заранее, а СКЛАДЫВАЮТСЯ из заготовок по тому раскладу избирателей,
        который дают действующие законы. Голосуют одни помещики — в палате
        сидят монархисты; допустили всех — приходят рабочие. Собранная палата
        ходит и сама: раз в PARLIAMENT_BILL_TICKS пейдеев она выносит на
        голосование закон, за который в ней уже есть твёрдое большинство
        (parliament_bill).
    РЕВОЛЮЦИЯ. Последний довод тех, кому законным путём сказать нечего.
        Грамотная страна осознаёт своё бесправие (society.awareness), жар
        копится в Country.revolution_heat, и однажды сословия выходят с
        ТРЕБОВАНИЯМИ — списком законов (revolution_demands). У лидера есть
        несколько пейдеев, чтобы их принять; не принял — гражданская война, и
        победившие восставшие вводят те же законы сами.
    ЛОББИРОВАНИЕ. Деньги промышленника — такой же голос, только покупной, и
        стоит он дорого. Цена растёт от избирательной системы (купить сотню
        помещиков дешевле, чем миллион мужиков) и от размера парламента. При
        всеобщем избирательном праве заметная скупка мест оставляет по стране
        след — модификатор «нечестное голосование».

Модуль намеренно ничего не знает ни про рынок, ни про войну: он читает законы,
считает голоса и двигает деньги. Всё, что от него нужно движку, вынесено в
короткие функции-справки (officer_pool, tariff_cap, tax_bounds и прочие).
"""
from __future__ import annotations

import random

from .. import config
from ..models import Country, LawVote, Party, Player, World

EPS = 1e-9


# ---------------------------------------------------------------------------
# Чтение законов
# ---------------------------------------------------------------------------
def law(country: Country, category: str) -> str:
    """Действующий закон страны в этой категории.

    Пустое место значит «как заведено»: страна из старого снимка мира, где
    законов ещё не было, живёт при монархии и авторитаризме и ничего об этом
    не знает — ровно как и должно быть.
    """
    spec = config.LAWS.get(category)
    if spec is None:
        return ""
    value = (country.laws or {}).get(category)
    if value in spec["options"]:
        return value
    return spec["default"]


def option(country: Country, category: str) -> dict:
    """Полное описание действующего закона (подписи + механика)."""
    spec = config.LAWS[category]
    return spec["options"][law(country, category)]


def option_of(category: str, key: str) -> dict | None:
    return config.LAWS.get(category, {}).get("options", {}).get(key)


def param(country: Country, category: str, key: str, default=None):
    """Механический параметр действующего закона."""
    return option(country, category).get(key, default)


def all_laws(country: Country) -> dict[str, str]:
    """Все действующие законы страны разом — в порядке config.LAW_ORDER."""
    return {cat: law(country, cat) for cat in config.LAW_ORDER}


# ---------------------------------------------------------------------------
# Что закон меняет в устройстве государства
# ---------------------------------------------------------------------------
def officer_pool(country: Country) -> list[str]:
    """Из каких сословий страна набирает офицеров.

    При монархии патент — дворянская привилегия, и корпус берут только из
    высшего общества. Республика открывает его среднему классу: командиров
    становится откуда брать, и стоят они дешевле, — но и качество командования
    упирается в потолок пониже (command_cap).
    """
    pool = param(country, "state_form", "officer_pool")
    return list(pool) if pool else list(config.OFFICER_POOL)


def officer_pay_mult(country: Country) -> float:
    """Множитель офицерского жалованья «по умолчанию» от формы государства."""
    return float(param(country, "state_form", "officer_pay_mult", 1.0))


def command_cap(country: Country | None) -> float:
    """Потолок качества командования.

    Республиканский офицер, выслужившийся из мещан, до дворянской школы не
    дотягивает: сто процентов — его предел. Это и есть цена дешёвого корпуса.
    """
    if country is None:
        return config.COMMAND_MAX
    return float(param(country, "state_form", "command_cap", config.COMMAND_MAX))


def has_elections(country: Country) -> bool:
    return bool(param(country, "suffrage", "elections", False))


def vote_weights(country: Country) -> dict[str, float]:
    """Вес голоса каждого сословия: {сословие: вес}.

    Складывается из всех законов разом. Избирательная система решает, КОГО
    вообще пускают к урнам; форма государства даёт дворянству добавочный вес
    (монархия); идеология — солдатам и офицерам (национализм) или рабочим
    (социализм); земельное устройство — помещику или деревне. Сословия, не
    попавшие в список, не голосуют вовсе: их вес не «маленький», его нет.

    Надбавки ПЕРЕМНОЖАЮТСЯ, а не складываются, и это делает союз законов
    сильнее их суммы: монархия с крепостным правом даёт помещику 2.4 × 1.5,
    социализм с коллективным хозяйством — деревне впятеро поверх всего
    прочего. Именно так закон и должен работать — не прибавкой к проценту, а
    переустройством того, чей голос вообще что-то значит.
    """
    base = dict(param(country, "suffrage", "voters", {}) or {})
    if not base:
        return {}
    mult: dict[str, float] = {}
    for category in config.LAW_ORDER:
        if category == "suffrage":
            continue        # она задаёт сам список избирателей, а не надбавки
        for key, value in (param(country, category, "vote_mult", {}) or {}).items():
            mult[key] = mult.get(key, 1.0) * float(value)
    return {key: weight * mult.get(key, 1.0) for key, weight in base.items()}


# ---------------------------------------------------------------------------
# Земельное устройство
# ---------------------------------------------------------------------------
def peasant_yield_mult(country: Country | None) -> float:
    """Во сколько раз крестьянин снимает со СВОЕГО надела против полной силы.

    Сто процентов не даёт ни один уклад, и это главное в категории: земля либо
    барская, либо нарезана клочками, либо скуплена под товарные фермы. Лучше
    всех работает коллективное хозяйство (0.90), хуже всех — коммерческое
    землевладение (0.35), при котором деревню с земли попросту согнали.

    Множитель приложен к ЗЕРНУ и только к нему — см. пояснение к config.LAWS
    ["land"]: лес и хлопок крестьянин берёт промыслом, а не с пашни.
    """
    if country is None:
        return 1.0
    return float(param(country, "land", "peasant_yield", 1.0))


def grain_rent(country: Country | None) -> float:
    """Доля зерновой выручки деревни, уходящая высшему классу сверх ренты.

    Крепостное право берёт своё не со всего, что деревня продала, а именно с
    ХЛЕБА: барщина — это про пашню. Земельная рента (Country.land_rent) при
    этом никуда не девается и берётся отдельно, со всей выручки.
    """
    if country is None:
        return 0.0
    return max(0.0, float(param(country, "land", "grain_rent", 0.0)))


def farm_peasant_share(country: Country | None) -> float:
    """Какую долю выручки хозяйская ферма отдаёт работающей на ней деревне.

    Коллективное хозяйство — единственный уклад, где ферма перестаёт быть
    чужим полем: большая часть выручки расходится по крестьянам сверх
    зарплаты. Для хозяина это и есть цена социализма — построить ферму можно,
    но снимать с неё всю прибыль больше нельзя.
    """
    if country is None:
        return 0.0
    return max(0.0, min(1.0, float(param(country, "land", "farm_peasant_share", 0.0))))


def industry_jobs(country: Country | None, industry) -> float:
    """Мест на уровень с поправкой на закон.

    Земельное устройство трогает одни КРЕСТЬЯНСКИЕ отрасли (Industry.labour):
    коммерческое землевладение скупает землю под товарное хозяйство, и ферма
    вмещает втрое больше рук. Заводов это не касается ничем.
    """
    jobs = float(industry.jobs_per_level)
    if country is None or getattr(industry, "labour", "workers") != "peasants":
        return jobs
    return jobs * float(param(country, "land", "farm_jobs_mult", 1.0))


def industry_output(country: Country | None, industry) -> float:
    """Выпуск на работника с поправкой на закон — по той же причине, что и места."""
    out = float(industry.output_per_worker)
    if country is None or getattr(industry, "labour", "workers") != "peasants":
        return out
    return out + float(param(country, "land", "farm_output_bonus", 0.0))


# ---------------------------------------------------------------------------
# Образование
# ---------------------------------------------------------------------------
def school_allowed(country: Country | None) -> bool:
    """Позволяет ли закон вообще строить школы.

    Сословное образование школ не признаёт: грамота — дворянская привилегия, и
    учить деревню государству попросту нечем и незачем. Это и есть первая
    стена, в которую упирается всякий, кто хочет промышленности: рабочих не из
    кого набрать, пока закон не переменён.
    """
    if country is None:
        return True
    return not bool(param(country, "education", "school_ban", False))


def school_efficiency(country: Country | None) -> float:
    """Во сколько раз школа учит быстрее или медленнее полной силы.

    Религиозная школа даёт половину: грамоте да закону божьему учат, а
    арифметике и чертежу — не очень. Государственная и всеобщая — полную.
    """
    if country is None:
        return 1.0
    return max(0.0, float(param(country, "education", "school_efficiency", 1.0)))


def educated_strata(country: Country | None, kind: str) -> list[str]:
    """Кого этому разряду учебных заведений позволено учить.

    `kind` — Industry.education_kind: "school" или "university". Списки живут в
    законе, а не в чертеже здания, и в этом весь смысл категории: одно и то же
    построенное здание при разных законах учит разных людей. Университет,
    открытый при сословном образовании, работает на одно высшее общество; тот
    же самый университет после «права образования для всех» — на всю страну.
    """
    if country is None:
        return list(config.STRATA_ORDER)
    key = "university_strata" if kind == "university" else "school_strata"
    return list(param(country, "education", key, []) or [])


def paper_mult(country: Country | None) -> float:
    """Во сколько раз больше бумаги уходит на казённые здания и палату.

    Всеобщее право учиться удваивает расход разом по всей стране: учебники
    нужны не сотне дворянских сыновей, а всем. Это и есть цена просвещения,
    которую платят бумагой, а не только червонцами.
    """
    if country is None:
        return 1.0
    return max(0.0, float(param(country, "education", "paper_mult", 1.0)))


# ---------------------------------------------------------------------------
# Права рабочих и самосознание
# ---------------------------------------------------------------------------
def rights_penalty(country: Country | None, stratum: str) -> float:
    """Насколько закон о правах обижает это сословие, 0..1.

    Величина сама по себе довольства НЕ режет — она умножается на
    САМОСОЗНАНИЕ сословия (society.awareness). В этом вся механика: безграмотная
    деревня терпит бесправие молча, потому что не считает его бесправием, а
    выученная не терпит вовсе. Один и тот же закон стоит стране тем дороже, чем
    больше в ней школ.
    """
    if country is None:
        return 0.0
    spec = param(country, "labour_rights", "unrest_penalty", {}) or {}
    return max(0.0, float(spec.get(stratum, 0.0)))


def represented(country: Country) -> dict[str, float]:
    """Насколько каждое сословие представлено во власти, 0..1.

    Складывается из двух половин, и обе нужны:

        ГОЛОС — пускают ли сословие к урнам вообще (vote_weights). Нет голоса —
            нет и представительства, сколько бы депутатов ни сидело в палате;
        ПАЛАТА — какая доля кресел занята партиями, которые за это сословие
            стоят (PARTY_ARCHETYPES → appeal). Голосовать и не иметь в палате
            никого — это половина беды, но всё же половина.

    При авторитаризме не представлен НИКТО: палаты нет, к урнам не пускают
    никого, и грамотная страна остаётся без единого законного способа быть
    услышанной. Отсюда и берутся революции в просвещённых самодержавиях.
    """
    weights = vote_weights(country)
    top = max(weights.values()) if weights else 0.0
    total_seats = float(sum(p.seats for p in country.parties))

    appeal: dict[str, float] = {}
    if total_seats > EPS:
        by_key = {a["key"]: a for a in config.PARTY_ARCHETYPES}
        for party in country.parties:
            arch = by_key.get(party.key)
            if arch is None:
                continue
            share = party.seats / total_seats
            for stratum, love in arch["appeal"].items():
                appeal[stratum] = appeal.get(stratum, 0.0) + share * min(1.0, love)

    out: dict[str, float] = {}
    for key in config.STRATA_ORDER:
        voice = (min(1.0, weights.get(key, 0.0) / top) if top > EPS else 0.0)
        seats_share = min(1.0, appeal.get(key, 0.0))
        # Голос весит больше кресел: право голосовать — это основа, а депутат,
        # который тебя любит, — приятное сверх неё.
        out[key] = max(0.0, min(1.0, 0.65 * voice + 0.35 * seats_share))
    return out


def grievance(country: Country, stratum: str, represented_share: float) -> float:
    """Обида сословия на устройство государства, 0..1 — БЕЗ учёта грамотности.

    Две причины, и они складываются: сословие не представлено во власти и
    сословие обижено законом о правах. Умножать на самосознание — дело
    society.awareness: здесь считается только то, на что вообще можно
    обижаться, а осознать это ещё надо уметь.
    """
    voiceless = 1.0 - max(0.0, min(1.0, represented_share))
    return max(0.0, min(1.0, 0.6 * voiceless + rights_penalty(country, stratum)))


def tariff_cap(country: Country) -> float:
    """До какой ставки торговая система позволяет поднять пошлины."""
    return float(param(country, "trade", "tariff_max", config.TARIFF_MAX))


def is_closed(country: Country) -> bool:
    """Закрытая экономика: страна не торгует с заграницей вовсе."""
    return bool(param(country, "trade", "closed", False))


def tax_bounds(country: Country) -> dict[str, tuple[float, float]]:
    """Пределы налоговых рычагов, которые накладывают действующие законы.

    {поле: (не ниже, не выше)}. Только те поля, по которым законы вообще
    что-то говорят: остальные остаются в общих границах api.POLICY_LIMITS.
    Здесь смыкаются два конца социализма — прибыль обложена наглухо, а с
    бедных брать почти нечего, — и здесь же земельное устройство отменяет
    ренту помещику: крестьянское владение и коллективное хозяйство запирают
    land_rent в ноль, и лидер уже не вернёт его ползунком.

    Смотрятся ВСЕ категории, а не одна идеология. Если два закона говорят об
    одном рычаге, берётся более строгое из сказанного: закон не отменяет
    другой закон, они действуют вместе.
    """
    out: dict[str, tuple[float, float]] = {}
    for category in config.LAW_ORDER:
        lows = param(country, category, "tax_min", {}) or {}
        highs = param(country, category, "tax_max", {}) or {}
        for field_name in set(lows) | set(highs):
            lo, hi = out.get(field_name, (0.0, 1e18))
            out[field_name] = (max(lo, float(lows.get(field_name, 0.0))),
                               min(hi, float(highs.get(field_name, 1e18))))
    return out


def clamp_policies(country: Country) -> list[str]:
    """Втянуть ставки лидера в границы, которые задал новый закон.

    Вызывается сразу после смены закона. Без этого страна, объявившая открытую
    экономику при стопроцентной пошлине, продолжала бы жить по старой ставке,
    пока лидер не догадается её тронуть, — а закон, который ничего не меняет
    до следующего движения мышью, законом не является.

    Возвращает список того, что пришлось поправить, — для новостной строки.
    """
    changed: list[str] = []
    cap = tariff_cap(country)
    for field_name, title in (("tariff", "вывозная пошлина"),
                              ("import_tariff", "ввозная пошлина")):
        if getattr(country, field_name) > cap + EPS:
            setattr(country, field_name, cap)
            changed.append(f"{title} срезана до {cap:.0%}")
    for field_name, (lo, hi) in tax_bounds(country).items():
        value = getattr(country, field_name, None)
        if value is None:
            continue
        if value < lo - EPS:
            setattr(country, field_name, lo)
            changed.append(f"«{field_name}» поднят до {lo:.0%}")
        elif value > hi + EPS:
            setattr(country, field_name, hi)
            changed.append(f"«{field_name}» срезан до {hi:.0%}")
    return changed


def build_discount(country: Country, industry, state_owned: bool) -> float:
    """Скидка на постройку, 0..1. Складывается из идеологии и назначения цеха.

    Скидки разные и достаются разным: либерализм удешевляет стройку
    ПРОМЫШЛЕННИКУ, консерватизм — КАЗНЕ (и любую), национализм — ВОЕННУЮ, но
    зато всем сразу. Больше двух третей цены скидка не съедает никогда.
    """
    spec = param(country, "ideology", "build_discount", {}) or {}
    total = 0.0
    if state_owned:
        total += float(spec.get("state", 0.0))
    else:
        total += float(spec.get("player", 0.0))
    if industry is not None and getattr(industry, "sector", "") == "military":
        total += float(spec.get("military", 0.0))
    return max(0.0, min(0.66, total))


def state_build_blocked(country: Country, industry) -> bool:
    """Запрещает ли идеология казне строить именно это.

    Либерализм выгоняет государство из хозяйства, но не из управления:
    административные здания (ратуша, торговая палата, академия) строит только
    оно — их запрет не касается.
    """
    if not param(country, "ideology", "state_build_ban", False):
        return False
    return getattr(industry, "kind", "industry") != "admin"


def unfair_voting(country: Country, tick: int) -> bool:
    """Ходит ли ещё по стране слух о нечестном голосовании."""
    return tick < country.unfair_until


# ---------------------------------------------------------------------------
# Парламент: цена кресла и цена голоса
# ---------------------------------------------------------------------------
def _clamp_seats(value: int) -> int:
    return int(max(config.PARLIAMENT_SEATS_MIN,
                   min(config.PARLIAMENT_SEATS_MAX, value)))


def seats(country: Country) -> int:
    """Мест в ДЕЙСТВУЮЩЕЙ палате: по ним и цена голоса, и содержание."""
    return _clamp_seats(country.parliament_seats or config.PARLIAMENT_SEATS_DEFAULT)


def next_seats(country: Country) -> int:
    """С каким числом кресел соберётся следующий созыв."""
    if not country.parliament_seats_next:
        return seats(country)
    return _clamp_seats(country.parliament_seats_next)


def parliament_cost(country: Country) -> float:
    """Содержание палаты за пейдей. Ноль там, где палаты нет."""
    if not has_elections(country):
        return 0.0
    return seats(country) * config.PARLIAMENT_SEAT_COST


def lobby_difficulty(country: Country) -> float:
    """КОЭФФИЦИЕНТ СЛОЖНОСТИ ЛОББИРОВАНИЯ — во сколько раз дороже обычного.

    Три сомножителя, и каждый — следствие принятых законов:

        избирательная система — купить сотню помещиков дешевле, чем миллион
            мужиков, поэтому всеобщее право дороже цензового вдвое с лишним;
        форма государства — республиканского депутата подкупить проще
            придворного: у него нет ни титула, ни наследного места;
        размер парламента — чем больше кресел, тем каждое дешевле по
            отдельности, но тем больше их нужно для перевеса. Считается по
            отношению к обычной палате.
    """
    mult = float(param(country, "suffrage", "lobby_mult", 1.0))
    mult *= float(param(country, "state_form", "lobby_mult", 1.0))
    mult *= seats(country) / config.PARLIAMENT_SEATS_DEFAULT
    return max(0.05, mult)


def seat_price(country: Country) -> float:
    """Во что обходится промышленнику один голос в парламенте."""
    return config.LOBBY_SEAT_COST * lobby_difficulty(country)


def min_stake(country: Country) -> float:
    """Меньше этого в лоббирование не заходят вовсе."""
    return config.LOBBY_MIN_STAKE * lobby_difficulty(country)


def lobby_power(country: Country) -> float:
    """Какая доля купленного голоса доходит до урны."""
    return float(param(country, "suffrage", "lobby_power", 0.0))


# ---------------------------------------------------------------------------
# Куда уходят деньги лоббиста
# ---------------------------------------------------------------------------
def _cities(world: World, country: Country) -> list:
    return world.country_regions(country.id)


def _spread_cash(world: World, country: Country, amount: float,
                 shares: dict[str, float]) -> None:
    """Раздать деньги сословиям страны в заданной пропорции.

    Деньги лоббиста не сгорают: взятка оседает в кошельке депутата, агитация
    кормит газетчиков и трактирщиков. Денежная масса мира обязана сходиться,
    поэтому всё, что списано с игрока, обязательно кому-нибудь достаётся.
    """
    if amount <= EPS:
        return
    cities = _cities(world, country)
    total_weight = sum(shares.values())
    if not cities or total_weight <= EPS:
        return
    people = {key: sum(c.s(key).people for c in cities) for key in shares}
    for key, weight in shares.items():
        pot = amount * weight / total_weight
        head = people.get(key, 0.0)
        if head <= EPS:
            # Сословия в стране нет — деньги достаются столице целиком.
            st = cities[0].s(key)
            st.cash += pot
            st.income += pot
            continue
        for city in cities:
            st = city.s(key)
            if st.people <= EPS:
                continue
            gain = pot * st.people / head
            st.cash += gain
            st.income += gain


def _bribe(world: World, country: Country, amount: float) -> None:
    """Взятка парламентёрам — в кошельки тех сословий, из которых палата."""
    _spread_cash(world, country, amount, config.LOBBY_BRIBE_STRATA)


def _campaign(world: World, country: Country, amount: float) -> None:
    """Предвыборные вложения — по голосующим сословиям: агитация и газеты."""
    weights = vote_weights(country)
    _spread_cash(world, country, amount,
                 weights or dict(config.LOBBY_BRIBE_STRATA))


# ---------------------------------------------------------------------------
# Выборы парламента
# ---------------------------------------------------------------------------
def _voter_mass(world: World, country: Country) -> dict[str, float]:
    """Сколько «голосов» даёт каждое сословие: люди × вес его голоса."""
    weights = vote_weights(country)
    out: dict[str, float] = {}
    for city in _cities(world, country):
        for key, weight in weights.items():
            people = city.s(key).people
            if people > EPS:
                out[key] = out.get(key, 0.0) + people * weight
    return out


def _platform_match(platform: dict[str, str], laws: dict[str, str]) -> float:
    """Насколько программа партии совпадает с тем, как страна живёт сейчас."""
    if not platform:
        return 1.0
    same = sum(1 for cat, opt in platform.items() if laws.get(cat) == opt)
    return same / len(platform)


def _drift_platform(platform: dict[str, str], rng: random.Random) -> dict[str, str]:
    """Дать партии случайную позицию по вопросу, к которому заготовка равнодушна.

    Без этого партии одного и того же вида голосовали бы по всем законам
    одинаково во всех странах мира, и голосование по «неудобному» вопросу
    заранее читалось бы по списку мест. Каждая палата должна быть хоть немного
    своей — отсюда и дрейф.
    """
    out = dict(platform)
    free = [cat for cat in config.LAW_ORDER if cat not in out]
    if free and rng.random() < 0.35:
        cat = rng.choice(free)
        out[cat] = rng.choice(list(config.LAWS[cat]["options"]))
    return out


def _allocate(scores: dict[str, float], total_seats: int) -> dict[str, int]:
    """Раздать кресла по методу наибольших остатков — до последнего места."""
    total = sum(scores.values())
    if total <= EPS:
        return {}
    exact = {k: v / total * total_seats for k, v in scores.items()}
    out = {k: int(v) for k, v in exact.items()}
    left = total_seats - sum(out.values())
    order = sorted(exact, key=lambda k: (-(exact[k] - int(exact[k])), k))
    for i in range(left):
        out[order[i % len(order)]] += 1
    return out


def hold_parliament_election(world: World, country: Country,
                             rng: random.Random) -> str:
    """Собрать новый созыв.

    Порядок такой. Считаем, сколько голосов даёт каждое сословие (численность ×
    вес по избирательной системе). Раскладываем эти голоса между заготовками
    партий по тому, насколько каждая из них сословию близка. Умножаем на ТЯГУ К
    ПЕРЕМЕНАМ — случайную величину этих выборов: при высокой даже дворянское
    собрание может проголосовать за реформы, которые ему прямо невыгодны, при
    низкой всё остаётся как было. И только потом добавляем купленные голоса.

    Возвращает строку новостей.
    """
    # Назначенный лидером размер палаты вступает в силу ровно здесь: прежний
    # созыв дожил свой срок в том составе, в каком его избирали.
    if country.parliament_seats_next:
        country.parliament_seats = next_seats(country)
        country.parliament_seats_next = 0

    total_seats = seats(country)
    mass = _voter_mass(world, country)
    reform = rng.random()
    laws = all_laws(country)

    scores: dict[str, float] = {}
    built: dict[str, Party] = {}
    bids = country.lobby_bids or {}
    for arch in config.PARTY_ARCHETYPES:
        support = sum(mass.get(key, 0.0) * weight
                      for key, weight in arch["appeal"].items())
        # Партию, за которую не голосует НИКТО, промышленник всё же может
        # вывести на выборы — деньгами. В этом и смысл лоббирования появления
        # партии: рабочая партия там, где рабочие лишены голоса, живой
        # поддержки не наберёт ни одного голоса, но купленные места у неё
        # будут. Без этой ветки вложенные в неё деньги пропадали бы молча.
        if support <= EPS and not bids.get(arch["key"]):
            continue
        platform = _drift_platform(arch["platform"], rng)
        match = _platform_match(arch["platform"], laws)
        # Тяга к переменам двигает расклад в обе стороны: партии перемен она
        # прибавляет ровно столько, сколько отнимает у партий порядка.
        factor = 0.55 + config.REFORM_DRIVE_WEIGHT * (
            reform * (1.0 - match) + (1.0 - reform) * match)
        # Небольшой разброс на каждую партию: выборы — не арифметика.
        scores[arch["key"]] = support * factor * rng.uniform(0.85, 1.15)
        built[arch["key"]] = Party(key=arch["key"], name=rng.choice(arch["names"]),
                                   color=arch["color"], platform=platform)

    bought_total = 0.0
    if scores:
        price = seat_price(country)
        power = lobby_power(country)
        # Сколько «очков» поддержки стоит одно кресло. Считается по живым
        # голосам — иначе купленное место не с чем сравнивать. Если живых
        # голосов нет вовсе (за все партии не подано ни одного), кресло стоит
        # единицу: тогда палату целиком и определяют деньги.
        total_score = sum(scores.values())
        unit = total_score / total_seats if total_score > EPS else 1.0
        for key, pledges in (country.lobby_bids or {}).items():
            if key not in built:
                continue
            money = sum(pledges.values())
            bought = money / price * power if price > EPS else 0.0
            if bought <= EPS:
                continue
            bought_total += bought
            built[key].bought = round(bought, 2)
            scores[key] = scores.get(key, 0.0) + bought * unit

    # Мелочь в палату не проходит: порог отсекает партии, за которые почти
    # никто не голосовал, а из прошедших остаются самые крупные.
    if scores:
        floor = sum(scores.values()) * config.PARTY_MIN_SHARE
        kept = {k: v for k, v in scores.items() if v >= floor}
        if not kept:
            kept = {max(scores, key=lambda k: scores[k]): max(scores.values())}
        if len(kept) > config.PARTY_MAX_COUNT:
            top = sorted(kept, key=lambda k: -kept[k])[:config.PARTY_MAX_COUNT]
            kept = {k: kept[k] for k in top}
        scores = kept

    allocation = _allocate(scores, total_seats)
    parties: list[Party] = []
    for key, seat_count in allocation.items():
        party = built[key]
        party.seats = seat_count
        party.votes = round(scores[key], 2)
        parties.append(party)
    parties.sort(key=lambda p: -p.seats)

    country.parties = parties
    country.parliament_tick = world.tick
    country.lobby_bids = {}

    # Скупка мест при всеобщем избирательном праве бесследно не проходит.
    if (param(country, "suffrage", "unfair", False)
            and total_seats > 0
            and bought_total / total_seats >= config.UNFAIR_LOBBY_SHARE):
        country.unfair_until = world.tick + config.UNFAIR_TICKS

    lead = parties[0] if parties else None
    tail = ", ".join(f"{p.name} — {p.seats}" for p in parties[:4])
    return (f"{country.name}: выборы в парламент ({total_seats} мест). "
            f"Первая сила — {lead.name if lead else '—'}. {tail}"
            + (". Ходят слухи о нечестном голосовании"
               if unfair_voting(country, world.tick) else ""))


# ---------------------------------------------------------------------------
# Голосование по закону
# ---------------------------------------------------------------------------
def party_stance(party: Party, category: str, opt: str) -> int:
    """Отношение партии к предложению: +1 за, −1 против, 0 безразлично."""
    want = party.platform.get(category)
    if want is None:
        return 0
    return 1 if want == opt else -1


def _yes_share(party: Party, category: str, opt: str,
               rng: random.Random | None) -> float:
    """Какая доля фракции проголосует «за».

    Партия не голосовальная машина: по своему вопросу она стоит горой или
    насмерть, а по чужому решает как придётся — отсюда и случайная доля
    в промежутке PARTY_NEUTRAL_MIN…MAX. Без rng берётся середина промежутка:
    так считается ОЖИДАЕМАЯ поддержка, по которой назначается цена взноса.
    """
    stance = party_stance(party, category, opt)
    if stance > 0:
        return 1.0
    if stance < 0:
        return 0.0
    if rng is None:
        return (config.PARTY_NEUTRAL_MIN + config.PARTY_NEUTRAL_MAX) / 2
    return rng.uniform(config.PARTY_NEUTRAL_MIN, config.PARTY_NEUTRAL_MAX)


def expected_support(country: Country, category: str, opt: str) -> float:
    """Ожидаемая доля палаты «за», 0..1. По ней и назначается цена взноса."""
    total = sum(p.seats for p in country.parties)
    if total <= 0:
        return 0.0
    return sum(p.seats * _yes_share(p, category, opt, None)
               for p in country.parties) / total


def finance_cost(world: World, country: Country, player: Player,
                 category: str, opt: str, worth: float | None = None) -> float:
    """Во что обойдётся промышленнику САМА постановка вопроса.

    Считается из двух вещей сразу, как и задумано: насколько закон и без того
    нравится палате — чем меньше поддержки, тем дороже уговорить её хотя бы
    заняться вопросом, — и насколько богат заказчик. Второе тут не жадность, а
    защита от даровой политики: для крупного дельца взнос обязан быть заметен,
    иначе он ставил бы вопросы на голосование каждый пейдей.

    Капитал можно передать готовым: витрина считает цену сразу по всем законам
    мира, а перебирать ради каждого все постройки мира незачем.
    """
    if worth is None:
        worth = world.net_worth(player)
    support = expected_support(country, category, opt)
    base = config.LAW_FINANCE_BASE * lobby_difficulty(country) * (1.5 - support)
    return max(base, worth * config.LAW_FINANCE_WORTH_SHARE)


def open_law_vote(world: World, country: Country, category: str, opt: str,
                  proposer: Player | None, financed: bool = False) -> LawVote:
    """Поставить закон на голосование и сразу записать расклад палаты.

    Мнение депутатов снимается ОДИН РАЗ, при постановке вопроса, и дальше не
    пересчитывается: фракции своё сказали. Всё, что происходит потом, — это
    торг за уже высказанные голоса, то есть лоббирование.
    """
    rng = random.Random((world.tick * 7919 + country.id * 104729
                         + hash(category + opt)) & 0x7FFFFFFF)
    total = sum(p.seats for p in country.parties)
    yes = sum(p.seats * _yes_share(p, category, opt, rng) for p in country.parties)
    vote = LawVote(
        law=category, option=opt,
        started_tick=world.tick,
        ends_tick=world.tick + config.LAW_VOTE_TICKS,
        proposer_id=proposer.id if proposer else None,
        financed=financed,
        seats_for=round(yes, 2), seats_against=round(total - yes, 2))
    country.law_vote = vote
    return vote


def parliament_bill(world: World, country: Country,
                    rng: random.Random) -> str:
    """СОБСТВЕННАЯ ИНИЦИАТИВА ПАЛАТЫ: закон, который она вносит сама.

    Раз в config.PARLIAMENT_BILL_TICKS пейдеев избранный парламент перестаёт
    ждать, пока ему что-нибудь принесут лидер или лоббист, и выносит на
    голосование то, за что в нём и без всякого вмешательства есть твёрдое
    большинство. Без этого законы менялись бы только там, где сидит живой
    игрок, а два десятка AI-государств так и жили бы при монархии до конца
    партии — при палатах, набитых республиканцами.

    Выбирается САМОЕ ПРОХОДНОЕ из возможного, а не самое желанное: палата
    берётся за вопрос, который решён заранее. Поэтому мало ожидаемой поддержки
    выше половины — расклад тут же и разыгрывается (open_law_vote), и если
    жребий по безразличным фракциям выдал меньшинство, предложение снимается и
    палата берётся за следующее. Так «поддержка больше половины» означает не
    вероятность, а факт: без денег со стороны такой закон пройдёт наверняка.

    Возвращает строку новостей (пустую, если вносить оказалось нечего).
    """
    if not has_elections(country) or not country.parties:
        return ""
    if country.law_vote is not None:
        return ""
    if world.tick - country.last_law_tick < config.LAW_COOLDOWN_TICKS:
        return ""

    candidates: list[tuple[float, float, str, str]] = []
    for category in config.LAW_ORDER:
        for opt in config.LAWS[category]["options"]:
            if law_blocked(country, category, opt):
                continue
            support = expected_support(country, category, opt)
            if support <= config.PARLIAMENT_BILL_SUPPORT:
                continue
            # Второй ключ — случайный: между двумя одинаково проходными
            # законами палата выбирает не по алфавиту.
            candidates.append((-support, rng.random(), category, opt))
    if not candidates:
        return ""

    candidates.sort()
    total = float(sum(p.seats for p in country.parties))
    for _support, _jitter, category, opt in candidates:
        vote = open_law_vote(world, country, category, opt, None)
        if total > 0 and vote.seats_for > total / 2:
            spec = option_of(category, opt) or {}
            return (f"{country.name}: парламент сам выносит на голосование "
                    f"новый {config.LAWS[category]['name'].lower()} — "
                    f"«{spec.get('name', opt)}» "
                    f"({vote.seats_for:.0f} против {vote.seats_against:.0f})")
        country.law_vote = None     # жребий не сложился — берёмся за следующее
    return ""


def lobby_swing(country: Country) -> float:
    """Сколько голосов перекуплено деньгами — со знаком.

    Перевес вложенных средств одной стороны над другой и есть число
    перекупленных мест: депутаты не считают, кто сколько дал, они считают
    разницу. Доля дошедшего до урны зависит от избирательной системы.
    """
    vote = country.law_vote
    if vote is None:
        return 0.0
    net = sum(vote.lobby_for.values()) - sum(vote.lobby_against.values())
    price = seat_price(country)
    if price <= EPS:
        return 0.0
    return net / price * lobby_power(country)


def vote_tally(country: Country) -> dict:
    """Расклад идущего голосования вместе с купленными голосами — для витрины."""
    vote = country.law_vote
    total = float(sum(p.seats for p in country.parties)) or 0.0
    if vote is None or total <= 0:
        return {"for": 0.0, "against": 0.0, "swing": 0.0, "total": total}
    swing = lobby_swing(country)
    yes = max(0.0, min(total, vote.seats_for + swing))
    return {"for": yes, "against": total - yes, "swing": swing, "total": total}


def apply_law(world: World, country: Country, category: str, opt: str) -> list[str]:
    """Ввести закон в силу и привести государство в соответствие с ним."""
    country.laws = dict(country.laws or {})
    country.laws[category] = opt
    country.last_law_tick = world.tick
    notes = clamp_policies(country)

    # Авторитаризм распускает палату: голосовать больше некому и незачем.
    if not has_elections(country):
        if country.parties:
            notes.append("парламент распущен")
        country.parties = []
        country.parliament_tick = -1
        country.law_vote = None
        country.lobby_bids = {}
    elif not country.parties:
        # Выборы только что появились — созыв соберётся ближайшим пейдеем.
        country.parliament_tick = -1
    return notes


def law_blocked(country: Country, category: str, opt: str) -> str | None:
    """Почему этот закон принять нельзя. None — можно.

    Запреты смысловые, а не балансные, и бывают двух родов:

        НЕСОВМЕСТИМОСТЬ (`forbidden_with`) — закон и действующий порядок
            отрицают друг друга. Республику не объявить, не отменив прежде
            авторитаризм: выборной власти неоткуда взяться там, где выборов
            нет вовсе. Социализма не провозгласить при монархии, а монархии не
            восстановить при социализме — запрет двусторонний;
        ПРЕДПОСЫЛКА (`requires`) — закон немыслим без другого, уже принятого.
            Социализм требует всеобщего избирательного права: рабочему
            государству неоткуда взяться там, где рабочий не голосует.
            Коллективное хозяйство требует самого социализма.

    Разница между ними не только в формулировке. Несовместимость перечисляет
    то, чего быть НЕ ДОЛЖНО, предпосылка — единственное, что подходит; вторым
    способом «социализм только при всеобщем праве» записывается одной строкой,
    а первым пришлось бы перечислять все прочие избирательные системы и
    дописывать этот список при каждой новой.
    """
    spec = option_of(category, opt)
    if spec is None:
        return "Такого закона нет"
    if law(country, category) == opt:
        return "Этот закон уже действует"
    for other, forbidden in (spec.get("forbidden_with") or {}).items():
        if law(country, other) in forbidden:
            name = config.LAWS[other]["options"][law(country, other)]["name"]
            return (f"«{spec['name']}» несовместим с действующим законом "
                    f"«{name}». Сперва измените: "
                    f"{config.LAWS[other]['name'].lower()}.")
    for other, needed in (spec.get("requires") or {}).items():
        if law(country, other) in needed:
            continue
        want = ", ".join(f"«{config.LAWS[other]['options'][k]['name']}»"
                         for k in needed if k in config.LAWS[other]["options"])
        return (f"«{spec['name']}» требует, чтобы прежде был принят закон "
                f"{want} ({config.LAWS[other]['name'].lower()}).")
    return None


def resolve_law_vote(world: World, country: Country) -> str:
    """Подвести итог голосования: перевес мест решает, а деньги его двигают."""
    vote = country.law_vote
    if vote is None:
        return ""
    tally = vote_tally(country)
    country.law_vote = None
    spec = option_of(vote.law, vote.option)
    title = spec["name"] if spec else vote.option
    category_name = config.LAWS[vote.law]["name"].lower()
    money = sum(vote.lobby_for.values()) + sum(vote.lobby_against.values())
    tail = (f" При {money:,.0f} ₡ лоббирования ({tally['swing']:+.0f} мест)."
            if money > EPS else "")

    if tally["for"] > tally["against"]:
        # Пока голосовали, закон мог стать несовместимым с другим принятым —
        # тогда постановление остаётся на бумаге.
        blocked = law_blocked(country, vote.law, vote.option)
        if blocked:
            return (f"{country.name}: «{title}» принят палатой, но не вступил "
                    f"в силу — {blocked.lower()}")
        notes = apply_law(world, country, vote.law, vote.option)
        extra = (" (" + "; ".join(notes) + ")") if notes else ""
        return (f"{country.name}: парламент принимает новый {category_name} — "
                f"«{title}» ({tally['for']:.0f} против "
                f"{tally['against']:.0f}).{tail}{extra}")
    return (f"{country.name}: парламент отклоняет «{title}» "
            f"({tally['for']:.0f} против {tally['against']:.0f}).{tail}")


# ---------------------------------------------------------------------------
# Деньги, которые двигают законы
# ---------------------------------------------------------------------------
def place_party_bid(world: World, country: Country, player: Player,
                    party_key: str, amount: float) -> None:
    """Вложить деньги в партию до выборов. Списывается сразу, считается потом."""
    player.cash -= amount
    bids = country.lobby_bids.setdefault(party_key, {})
    bids[player.id] = bids.get(player.id, 0.0) + amount
    _campaign(world, country, amount)


def place_law_bid(world: World, country: Country, player: Player,
                  side: str, amount: float) -> None:
    """Подкупить палату за или против идущего законопроекта."""
    vote = country.law_vote
    if vote is None:
        return
    player.cash -= amount
    pot = vote.lobby_for if side == "for" else vote.lobby_against
    pot[player.id] = pot.get(player.id, 0.0) + amount
    _bribe(world, country, amount)


# ---------------------------------------------------------------------------
# Деньги, которые двигают законы обратно — в экономику
# ---------------------------------------------------------------------------
def serfdom_payout(world: World, country: Country, public_spend: float,
                   tithe: float) -> float:
    """КРЕПОСТНОЕ ПРАВО — содержание дворянства при монархии.

    Высшее общество кормится с государства дважды: долей госрасходов сверх
    того, что причитается ему по числу душ, и частью оброка, собранного с
    деревни. И то и другое проходит отдельной статьёй казны, чтобы было видно,
    во что стране обходится дворянское сословие. Республика статью отменяет —
    и вместе с ней исчезает главный источник дохода высшего класса.

    Возвращает выплаченное.
    """
    share = float(param(country, "state_form", "serfdom_spending", 0.0))
    rent = float(param(country, "state_form", "serfdom_tithe", 0.0))
    total = public_spend * share + tithe * rent
    total = min(total, max(0.0, country.treasury))
    if total <= EPS:
        return 0.0
    country.spend("serfdom", total)
    _spread_cash(world, country, total, {"town_high": 1.0})
    return total


def redistribution_payout(world: World, country: Country) -> float:
    """ДОПОЛНИТЕЛЬНОЕ РАСПРЕДЕЛЕНИЕ — доля казны низшим и средним слоям.

    То, ради чего при социализме с промышленника и берут половину прибыли.
    Раздаётся не поровну: беднейшим сословиям достаётся больше, среднему
    классу — меньше всех (config.LAWS → socialism → redistribution_strata).
    """
    share = float(param(country, "ideology", "redistribution", 0.0))
    if share <= EPS:
        return 0.0
    total = min(max(0.0, country.treasury) * share, max(0.0, country.treasury))
    if total <= EPS:
        return 0.0
    country.spend("redistribution", total)
    _spread_cash(world, country, total,
                 param(country, "ideology", "redistribution_strata", {}) or {})
    return total


def collect_army_levy(world: World, country: Country, army_cost: float) -> float:
    """ВОЕННЫЙ СБОР — доля содержания армии, переложенная на промышленников.

    Национализм считает армию делом общим, а платить за общее дело положено
    тому, у кого есть чем. Сбор раскладывается между гражданами-промышленниками
    по их капиталу: у кого дело крупнее, тот и платит больше. С банкрота не
    берут ничего — брать нечего.
    """
    share = float(param(country, "ideology", "army_levy", 0.0))
    if share <= EPS or army_cost <= EPS:
        return 0.0
    payers = [p for p in world.players.values()
              if p.country_id == country.id and not p.is_state and not p.bankrupt]
    worth = {p.id: max(0.0, world.net_worth(p)) for p in payers}
    total_worth = sum(worth.values())
    if total_worth <= EPS:
        return 0.0
    due = army_cost * share
    taken = 0.0
    for p in payers:
        # Больше кассы с промышленника не берут: сбор не должен загонять дело
        # в банкротство — разорённый плательщик не заплатит и в следующий раз.
        part = min(due * worth[p.id] / total_worth, max(0.0, p.cash))
        p.cash -= part
        taken += part
    if taken > EPS:
        country.collect("army_levy", taken)
    return taken


def charge_parliament(world: World, country: Country) -> float:
    """Содержание палаты за пейдей — отдельной статьёй казны.

    Деньги не сгорают: жалованье депутата оседает в кошельке того сословия,
    из которого депутат и вышел. Денежная масса мира обязана сходиться, иначе
    большой парламент незаметно высасывал бы из страны деньги.
    """
    cost = parliament_cost(country)
    if cost <= EPS:
        return 0.0
    cost = min(cost, max(0.0, country.treasury))
    country.spend("parliament", cost)
    _spread_cash(world, country, cost, config.LOBBY_BRIBE_STRATA)
    return cost


# ---------------------------------------------------------------------------
# Требования восставших
# ---------------------------------------------------------------------------
#: Сколько законов за раз способна потребовать одна революция. Больше — и
#: требования перестают быть требованиями, превращаясь в полную перепись
#: государства за один пейдей.
MAX_DEMANDS = 3

#: Кто считается «низом» при составлении требований. Революция низов требует
#: всеобщего права и профсоюзов; революция мещан и дворян — ценза и собраний.
_LOWER = {"peasants", "artisans", "workers", "town_low", "soldiers"}


def _wanted_laws(country: Country, strata: list[str]) -> list[tuple[str, str]]:
    """Чего именно хотят восставшие — в порядке важности.

    Список зависит от того, КТО восстал. Деревня и завод требуют голоса для
    всех и права торговаться с хозяином; мещане и офицеры — всего лишь ценза и
    права собраний. Ровно поэтому революция при цензовом праве и революция при
    самодержавии кончаются разными законами.
    """
    from_below = any(key in _LOWER for key in strata)
    wants: list[tuple[str, str]] = []

    # 1. ГОЛОС. Первое, чего требует всякий, кто осознал себя сословием.
    suffrage = law(country, "suffrage")
    if from_below:
        if suffrage != "universal":
            wants.append(("suffrage", "universal"))
    elif suffrage in ("authoritarian", "rich"):
        wants.append(("suffrage", "census"))

    # 2. ПРАВА. Второе — то, ради чего голос и нужен.
    rights = law(country, "labour_rights")
    ladder = ["none", "assembly", "unions", "civil"]
    step = ladder.index(rights) if rights in ladder else 0
    if step < len(ladder) - 1:
        # Низы перешагивают ступень: им мало права собраться, им нужен договор
        # с хозяином. Мещане довольствуются следующей ступенью.
        wants.append(("labour_rights",
                      ladder[min(len(ladder) - 1, step + (2 if from_below else 1))]))

    # 3. ГРАМОТА. Третье — чтобы дети восставших знали больше отцов.
    edu = law(country, "education")
    edu_ladder = ["elite", "religious", "state", "universal"]
    edu_step = edu_ladder.index(edu) if edu in edu_ladder else 0
    if edu_step < len(edu_ladder) - 1:
        wants.append(("education", edu_ladder[edu_step + (2 if from_below else 1)]
                      if edu_step + (2 if from_below else 1) < len(edu_ladder)
                      else edu_ladder[-1]))

    # 4. ПРЕСТОЛ и ЗЕМЛЯ. До них доходит не всякая революция — на них уже не
    # хватает места в списке требований, если первые три не удовлетворены.
    if law(country, "state_form") == "monarchy":
        wants.append(("state_form", "republic"))
    if from_below and law(country, "land") == "serfdom":
        wants.append(("land", "smallholding"))
    return wants


def revolution_demands(country: Country, strata: list[str]) -> dict[str, str]:
    """Составить список требований, который восставшие подадут лидеру.

    Требования обязаны быть ИСПОЛНИМЫМИ: закон, который нельзя принять при
    нынешнем порядке, требовать бессмысленно — лидер не смог бы его ввести, даже
    захотев, и революция кончилась бы ничем. Поэтому каждое следующее
    требование проверяется уже с учётом предыдущих: республику восставшие
    требуют не «вообще», а вместе со всеобщим правом, без которого её не
    объявить.

    Проверка ведётся по временно подменённым законам страны — так `law_blocked`
    видит именно тот порядок, который сложится, если требования удовлетворят.
    """
    saved = dict(country.laws or {})
    chosen: dict[str, str] = {}
    try:
        country.laws = dict(saved)
        for category, opt in _wanted_laws(country, strata):
            if len(chosen) >= MAX_DEMANDS:
                break
            if law(country, category) == opt:
                continue
            if law_blocked(country, category, opt):
                continue
            chosen[category] = opt
            country.laws[category] = opt
    finally:
        country.laws = saved
    return chosen


def apply_demands(world: World, country: Country,
                  demands: dict[str, str]) -> list[str]:
    """Ввести требования восставших в силу — добровольно или после поражения.

    Порядок обхода — config.LAW_ORDER, а не тот, в каком требования записаны:
    зависимости между законами односторонние, и всеобщее право обязано
    вступить в силу раньше республики, которой оно нужно. Требование, ставшее
    к этому моменту непринимаемым, просто пропускается — насильно ввести
    невозможный закон нельзя и победившей революции.
    """
    done: list[str] = []
    for category in config.LAW_ORDER:
        opt = demands.get(category)
        if not opt or law_blocked(country, category, opt):
            continue
        apply_law(world, country, category, opt)
        spec = option_of(category, opt) or {}
        done.append(f"{config.LAWS[category]['name'].lower()} — "
                    f"«{spec.get('name', opt)}»")
    return done


def demands_text(demands: dict[str, str]) -> str:
    """Требования одной строкой — для новостей и витрины."""
    return "; ".join(
        f"{config.LAWS[cat]['name'].lower()}: "
        f"«{(option_of(cat, opt) or {}).get('name', opt)}»"
        for cat, opt in demands.items() if cat in config.LAWS)


# ---------------------------------------------------------------------------
# Ход политической машины
# ---------------------------------------------------------------------------
def step_politics(world: World) -> list[str]:
    """Пейдей политики: созывы, голосования и остывающие модификаторы."""
    news: list[str] = []
    for country in world.countries.values():
        if not country.alive:
            continue
        if not has_elections(country):
            # При авторитаризме палаты нет: если она осталась от прежних
            # законов, распускаем и снимаем всё, что она рассматривала.
            if country.parties or country.law_vote:
                country.parties = []
                country.law_vote = None
                country.lobby_bids = {}
                country.parliament_tick = -1
                news.append(f"{country.name}: парламент распущен")
            continue

        due = (country.parliament_tick < 0
               or world.tick - country.parliament_tick >= config.PARLIAMENT_TERM_TICKS)
        if due:
            rng = random.Random((world.tick * 6151 + country.id * 24593)
                                & 0x7FFFFFFF)
            news.append(hold_parliament_election(world, country, rng))

        if country.law_vote is not None and world.tick >= country.law_vote.ends_tick:
            line = resolve_law_vote(world, country)
            if line:
                news.append(line)

        # Собственная инициатива палаты — ПОСЛЕ подведения итогов прошлого
        # голосования, иначе один вопрос запирал бы другой на весь свой срок.
        # Отсчёт ведётся от попытки, а не от удачи: палате, которой сегодня
        # нечего вынести, незачем пробовать снова каждый пейдей.
        if world.tick - country.parliament_bill_tick >= config.PARLIAMENT_BILL_TICKS:
            country.parliament_bill_tick = world.tick
            rng = random.Random((world.tick * 3571 + country.id * 49157)
                                & 0x7FFFFFFF)
            line = parliament_bill(world, country, rng)
            if line:
                news.append(line)
    return news
