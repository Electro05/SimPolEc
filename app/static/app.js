'use strict';

/* ======================================================================
   SimPolEc — клиент. Ванильный JS, без сборки и зависимостей.
   ====================================================================== */

const S = {
    token: localStorage.getItem('simpolec.token') || null,
    me: null,
    world: null,
    market: [],
    series: {},
    macro: [],
    buildings: [],
    cities: [],
    industries: [],
    people: null,
    good: 'food',          // выбранный товар для графика
    region: null,          // выбранная область (её рынок и население)
    regions: [],
    xGood: 'steel',        // выбранный товар на бирже
    buildSector: 'all',    // фильтр отраслей в строительстве
    sectors: [],
    chainRules: null,
    exchange: null,
    diplo: null,
    page: 'market',
    lastTick: -1,
    secondsLeft: 0,
    // чат: канал, собеседник и последние прочитанные id по каждому каналу
    chat: null,
    chatOpen: false,
    chatCh: 'world',
    chatPeer: 0,
    chatSeen: {},
    admin: null,
};

/* ------------------------------ утилиты ------------------------------ */
const $ = id => document.getElementById(id);

function money(v) {
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(2) + ' млрд';
    if (a >= 1e6) return (v / 1e6).toFixed(2) + ' млн';
    if (a >= 1e3) return Math.round(v).toLocaleString('ru-RU');
    return v.toFixed(a < 10 ? 2 : 0);
}
function qty(v) {
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(2) + ' млн';
    if (a >= 1e3) return (v / 1e3).toFixed(1) + ' тыс';
    return Math.round(v).toLocaleString('ru-RU');
}
const pct = v => (v * 100).toFixed(1) + '%';
const esc = s => String(s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function toast(msg, isErr) {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'show' + (isErr ? ' err' : '');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.className = ''; }, 3600);
}

async function api(path, body, method) {
    const opts = {
        method: method || (body !== undefined ? 'POST' : 'GET'),
        headers: { 'Content-Type': 'application/json' },
    };
    if (S.token) opts.headers['Authorization'] = 'Bearer ' + S.token;
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({ error: 'Сервер молчит' }));
    if (!res.ok) throw new Error(data.error || 'Ошибка ' + res.status);
    return data;
}

/* ------------------------------ вход ------------------------------ */
async function doAuth(kind) {
    const username = $('u').value.trim(), password = $('p').value;
    const sel = $('regCountry');
    const country_id = sel ? Number(sel.value) : 0;
    $('authErr').textContent = '';
    try {
        const body = { username, password };
        if (kind === 'register' && country_id) body.country_id = country_id;
        const r = await api('/api/' + kind, body);
        S.token = r.token;
        localStorage.setItem('simpolec.token', r.token);
        await start();
    } catch (e) {
        $('authErr').textContent = e.message;
    }
}

async function loadCountriesForRegister() {
    const sel = $('regCountry');
    if (!sel) return;
    try {
        const r = await api('/api/countries');
        if (!r.countries || !r.countries.length) throw new Error('Список пуст');
        sel.innerHTML = r.countries.map(c =>
            `<option value="${c.id}">${esc(c.name)} — ${esc(c.capital)}, ${qty(c.population)} чел. (${c.players} игр.)</option>`
        ).join('');
    } catch (e) {
        // Молчать нельзя: пустой список выглядит как поломка формы,
        // а причина обычно в том, что сервер не поднялся или база старая.
        sel.innerHTML = '<option value="">— не удалось загрузить —</option>';
        $('authErr').textContent = 'Не удалось получить список государств: '
            + e.message + '. Проверьте, что сервер запущен.';
    }
}

/** Показать форму входа и всегда наполнить список государств.
 *  Раньше список грузился только при первом заходе. Если в браузере оставался
 *  токен от прошлой игры, срабатывала ветка «проверить токен» — токен
 *  оказывался негодным, форма показывалась, а выпадающий список так и
 *  оставался пустым. */
function showAuth() {
    $('game').style.display = 'none';
    $('auth').style.display = '';
    loadCountriesForRegister();
}

function logout() {
    api('/api/logout', {}).catch(() => {});
    localStorage.removeItem('simpolec.token');
    S.token = null;
    showAuth();
}

async function start() {
    try {
        S.me = await api('/api/me');
    } catch (e) {
        // Токен протух или мир пересоздан — возвращаем на форму входа
        // и обязательно наполняем список государств.
        localStorage.removeItem('simpolec.token');
        S.token = null;
        showAuth();
        return;
    }
    $('auth').style.display = 'none';
    $('game').style.display = '';
    await refresh(true);
    await loadChat();
}

/* ------------------------------ загрузка ------------------------------ */
async function refresh(full) {
    try {
        const rq = S.region ? '?region_id=' + S.region : '';
        const [world, me, market, buildings] = await Promise.all([
            api('/api/world'), api('/api/me'), api('/api/market' + rq),
            api('/api/buildings'),
        ]);
        S.world = world; S.me = me;
        // Админка появляется в меню только у того, кому она положена.
        $('navAdmin').hidden = !me.is_admin;
        S.market = market.goods; S.buildings = buildings.buildings;
        S.marketCountryId = market.country_id;
        S.region = market.region_id;          // сервер мог поправить выбор
        S.regionName = market.region_name;
        S.regions = market.regions || [];
        S.access = {
            access: market.access, chambers: market.chambers,
            chamber_max: market.chamber_max,
            access_per_level: market.access_per_level,
            tariff: market.tariff, import_tariff: market.import_tariff,
        };
        S.secondsLeft = world.seconds_left;

        const newTick = world.tick !== S.lastTick;
        if (newTick || full) {
            const rq2 = S.region ? '&region_id=' + S.region : '';
            const [series, macro, regions, industries, people, govBuildings,
                   mapData, el, exch, diplo, cits, annex] = await Promise.all([
                api('/api/market/history?limit=120' + rq2),
                api('/api/macro/history?limit=120'),
                api('/api/regions'), api('/api/industries?x=1' + rq2),
                api('/api/population?x=1' + rq2),
                api('/api/gov/buildings'), api('/api/map'), api('/api/elections'),
                api('/api/exchange?limit=120'), api('/api/diplomacy'),
                api('/api/gov/citizens'), api('/api/annexations'),
            ]);
            S.series = series.series; S.macro = macro.rows;
            S.cities = regions.regions; S.industries = industries.industries;
            S.sectors = industries.sectors || []; S.chainRules = industries.chain;
            S.people = people; S.govBuildings = govBuildings.buildings;
            S.canManageState = govBuildings.can_manage;
            S.map = mapData.nodes; S.elections = el;
            S.exchange = exch; S.diplo = diplo;
            S.citizens = cits; S.annex = annex;
            if (S.lastTick >= 0 && newTick) toast('Пейдей #' + world.tick + ' завершён');
            S.lastTick = world.tick;
            renderCountry(); renderBuild(); renderTop(); renderGov(); renderPeople();
            renderMap(); renderExchange(); renderWar(); renderAnnexations();
        }
        renderHeader(); renderMarket(); renderBiz();
        if (S.page === 'admin') loadAdmin();
    } catch (e) {
        console.error(e);
    }
}

/* ------------------------------ шапка ------------------------------ */
/** Шапка показывает СВОЮ страну, а не весь мир: игроку важно население его
 *  государства, а не планеты. Мировые цифры остались в подсказках. */
function renderHeader() {
    const wld = S.world.world || {};
    const h = S.world.home || wld;
    const c = S.world.country || {};
    $('hCash').textContent = money(S.me.cash) + ' ₡';
    $('hWorth').textContent = money(S.me.net_worth) + ' ₡';
    $('hTick').textContent = '#' + S.world.tick;
    $('hPop').textContent = qty(h.population);
    $('hPop').title = 'Во всём мире: ' + qty(wld.population) + ' в ' + wld.countries + ' государствах';
    $('hRegions').textContent = h.regions || 0;
    $('hRegions').className = 'v num ' + (h.revolts ? 'bad' : '');
    $('hRegions').title = h.revolts ? h.revolts + ' обл. в мятеже' : 'Областей в стране';
    $('hInd').textContent = pct(h.industrialisation);
    $('hUnemp').textContent = pct(h.unemployment);
    $('hUnemp').className = 'v num ' + (h.unemployment > .25 ? 'bad'
        : h.unemployment > .12 ? 'warn' : 'good');
    $('hSat').textContent = pct(h.satisfaction);
    $('hSat').className = 'v num ' + (h.satisfaction < .4 ? 'bad'
        : h.satisfaction < .55 ? 'warn' : 'good');
    const sol = h.living_standard || 0;
    $('hSol').textContent = sol.toFixed(2);
    $('hSol').className = 'v num ' + (sol < .3 ? 'bad' : sol < .55 ? 'warn'
        : sol < .8 ? 'good' : 'gold');
    const badge = $('hCountry');
    if (badge) badge.textContent = c.name || '';
}

/* ------------------------- переключатель областей ------------------------- */
/** Рынок и население живут в области, поэтому у витрин общий переключатель.
 *  Рисуем его одинаково везде — вид один, а страница подставляет свой id. */
function regionPicker(boxId, list, selected) {
    const box = $(boxId);
    if (!box) return;
    const rows = list || [];
    box.innerHTML = rows.length < 2 ? '' : '<div class="regions">'
        + rows.map(r => `<button class="rg${r.id === selected ? ' on' : ''}${
            r.revolt ? ' riot' : ''}" onclick="pickRegion(${r.id})"
            title="${r.revolt ? 'В области бунт' : qty(r.population) + ' чел.'}">
            ${esc(r.name)}${r.capital ? ' ★' : ''}${r.revolt ? ' 🔥' : ''}
        </button>`).join('') + '</div>';
}

function pickRegion(id) {
    S.region = id;
    refresh(true);
}

function tickClock() {
    if (!S.world) return;
    // Время остановлено администратором — обратный отсчёт врал бы.
    if (S.world.auto_tick === false) {
        $('ringArc').setAttribute('stroke-dashoffset', '0');
        $('ringLbl').textContent = 'стоп';
        return;
    }
    S.secondsLeft = Math.max(0, S.secondsLeft - 1);
    const total = S.world.tick_seconds || 900;
    const frac = 1 - S.secondsLeft / total;
    const C = 2 * Math.PI * 19;
    $('ringArc').setAttribute('stroke-dashoffset', String(C * (1 - frac)));
    const m = Math.floor(S.secondsLeft / 60), s = Math.floor(S.secondsLeft % 60);
    $('ringLbl').textContent = m > 0 ? m + ':' + String(s).padStart(2, '0') : s + 'с';
}

/* ------------------------------ рынок ------------------------------ */
const CAT = {
    raw: 'сырьё', intermediate: 'полуфабрикат',
    consumer: 'потреб.', services: 'услуги', military: 'военное',
    luxury: 'роскошь',
};

/** Доступность рынка области: одно число на оба конца торговли.
 *  Внутри страны — как быстро область сравнивается ценами с соседями,
 *  снаружи — какой объём проходит через границу. Растёт от «Торговых палат». */
function renderAccess() {
    const a = S.access || {};
    const v = a.access || 0;
    const lv = a.chambers || 0, max = a.chamber_max || 9;
    $('mAccess').textContent = pct(v);
    $('mAccess').className = 'v num ' + (v < .3 ? 'bad' : v < .6 ? 'warn'
        : v < .999 ? 'good' : 'gold');
    $('mChambers').textContent = lv + ' / ' + max;
    $('mChambers').className = 'v num ' + (lv >= max ? 'gold' : lv ? 'good' : 'dim');
    $('mTariff').textContent = pct(a.tariff || 0);
    $('mImport').textContent = pct(a.import_tariff || 0);
    const left = Math.max(0, max - lv);
    $('accessNote').innerHTML =
        'Доступность — насколько прилавок этой области включён в общую экономику. '
        + '<b>Внутри страны</b> это скорость выравнивания: за пейдей область '
        + 'закрывает <b>' + pct(v) + '</b> разрыва между своим складом и '
        + 'общестрановой нормой, поэтому на сотне рынки областей практически '
        + 'сливаются в один. <b>С заграницей</b> — просто объём: столько товара '
        + 'область пропустит через границу. Поднимает её только «Торговая палата» — '
        + 'по ' + pct(a.access_per_level || .1) + ' за уровень поверх базовых 10%'
        + (left ? ', осталось <b>' + left + '</b> ур. до полного единого рынка.'
                : '. Область включена в общий рынок полностью.');
}

function renderMarket() {
    $('marketRegion').textContent = S.regionName || '—';
    renderAccess();
    regionPicker('marketRegions', S.regions, S.region);
    $('marketRows').innerHTML = S.market.map(g => {
        const pos = Math.max(0, Math.min(1, g.position)) * 100;
        const anchorPos = ((g.anchor - g.floor) / (g.ceiling - g.floor)) * 100;
        const col = g.margin >= 1.4 ? 'var(--bad)' : g.margin >= 1.05 ? 'var(--warn)'
            : g.margin >= .9 ? 'var(--accent)' : 'var(--good)';
        const short = g.shortage > .3 ? 'bad' : g.shortage > .05 ? 'warn' : 'dim';
        return `<tr class="clickable ${g.key === S.good ? 'sel' : ''}"
                    onclick="pickGood('${g.key}')">
            <td>${esc(g.name)}</td>
            <td><span class="tag ${g.category}">${CAT[g.category] || g.category}</span></td>
            <td class="right">${g.price.toFixed(2)}</td>
            <td class="right dim" title="Средняя цена в остальных областях страны">${
                g.country_price == null ? '—' : g.country_price.toFixed(2)
                + (g.country_price > 0 ? ` <span class="${
                    g.price > g.country_price * 1.05 ? 'bad'
                    : g.price < g.country_price * 0.95 ? 'good' : 'dim'}">${
                    (g.price / g.country_price - 1 >= 0 ? '+' : '')
                    + pct(g.price / g.country_price - 1)}</span>` : '')}</td>
            <td class="right dim">${g.unit_cost.toFixed(2)}</td>
            <td class="right" style="color:${col}">×${g.margin.toFixed(2)}</td>
            <td><div class="corridor" title="от ${g.floor} до ${g.ceiling} ₡">
                <div class="track"></div>
                <div class="anchor" style="left:${anchorPos}%"></div>
                <div class="dot" style="left:${pos}%;background:${col}"></div>
            </div></td>
            <td class="right">${g.storable ? qty(g.stock) : '<span class="dim">не хранится</span>'}</td>
            <td class="right dim">${g.stock_ticks != null ? g.stock_ticks.toFixed(1) + '×' : '—'}</td>
            <td class="right ${short}">${g.shortage > .005 ? pct(g.shortage) : '—'}</td>
        </tr>`;
    }).join('');
    drawPriceChart();
    drawMacroChart();
}

function pickGood(key) { S.good = key; renderMarket(); }

/* --------------------------- графики (SVG) --------------------------- */
function lineChart(svgId, sets, opts) {
    const svg = $(svgId);
    if (!svg) return;
    const W = svg.clientWidth || 600, H = svg.clientHeight || 190;
    const L = 52, R = 10, T = 12, B = 22;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

    const all = sets.flatMap(s => s.data);
    if (!all.length) { svg.innerHTML = ''; return; }
    let lo = Math.min(...all.map(d => d.y)), hi = Math.max(...all.map(d => d.y));
    if (opts && opts.zero) lo = Math.min(lo, 0);
    if (hi - lo < 1e-9) { hi = lo + 1; lo -= 1; }
    const pad = (hi - lo) * .12; lo -= pad; hi += pad;

    const xs = all.map(d => d.x);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const px = x => L + (x1 === x0 ? 0 : (x - x0) / (x1 - x0)) * (W - L - R);
    const py = y => T + (1 - (y - lo) / (hi - lo)) * (H - T - B);

    let out = '';
    for (let i = 0; i <= 3; i++) {
        const v = lo + (hi - lo) * i / 3, y = py(v);
        out += `<line x1="${L}" y1="${y}" x2="${W - R}" y2="${y}" stroke="#262e3a" stroke-width="1"/>`;
        out += `<text x="${L - 6}" y="${y + 3.5}" fill="#5d6878" font-size="10"
                 text-anchor="end">${opts && opts.fmt ? opts.fmt(v) : v.toFixed(1)}</text>`;
    }
    out += `<text x="${L}" y="${H - 6}" fill="#5d6878" font-size="10">#${x0}</text>`;
    out += `<text x="${W - R}" y="${H - 6}" fill="#5d6878" font-size="10" text-anchor="end">#${x1}</text>`;

    for (const s of sets) {
        if (!s.data.length) continue;
        const d = s.data.map((p, i) => (i ? 'L' : 'M') + px(p.x).toFixed(1) + ' ' + py(p.y).toFixed(1)).join(' ');
        out += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width || 1.8}"
                 stroke-linejoin="round" ${s.dash ? 'stroke-dasharray="4 3"' : ''}/>`;
    }
    svg.innerHTML = out;
}

function drawPriceChart() {
    const g = S.market.find(x => x.key === S.good) || S.market[0];
    if (!g) return;
    $('chartGood').textContent = g.name;
    const rows = S.series[g.key] || [];
    lineChart('priceChart', [
        { data: rows.map(r => ({ x: r.tick, y: r.anchor })), color: '#8b97a8', width: 1.4, dash: true },
        { data: rows.map(r => ({ x: r.tick, y: r.price })), color: '#4c9aff' },
    ], { fmt: v => v.toFixed(v < 10 ? 2 : 0) });
}

function drawMacroChart() {
    const m = S.macro;
    lineChart('macroChart', [
        { data: m.map(r => ({ x: r.tick, y: r.cpi })), color: '#3fb950' },
        { data: m.map(r => ({ x: r.tick, y: r.unemployment })), color: '#d29922' },
        { data: m.map(r => ({ x: r.tick, y: r.satisfaction })), color: '#a371f7' },
    ], { fmt: v => v.toFixed(2), zero: true });
}

/* --------------------------- биржа мирового рынка --------------------------- */
function renderExchange() {
    const x = S.exchange;
    if (!x) return;
    const rows = x.goods || [];
    if (!rows.some(g => g.key === S.xGood)) S.xGood = (rows[0] || {}).key;

    $('xCap').textContent = pct(x.my_capacity);
    $('xCap').className = 'v num ' + (x.my_capacity > 0 ? 'good' : 'bad');
    $('xTariff').textContent = pct(x.tariff);
    $('xImport').textContent = pct(x.import_tariff || 0);
    // знаменатель — государства, а не узлы карты: узел карты это область
    const states = new Set((S.map || []).map(n => n.country_id));
    $('xTraders').textContent = (x.traders || []).length + ' / ' + states.size;
    $('xVolume').textContent = qty(rows.reduce((a, g) => a + (g.volume || 0), 0));

    $('xRows').innerHTML = rows.map(g => {
        const cheap = g.cheapest, dear = g.dearest;
        const traded = g.volume > 0;
        return `<tr class="clickable ${g.key === S.xGood ? 'sel' : ''}"
                    onclick="pickXGood('${g.key}')">
            <td>${esc(g.name)}</td>
            <td><span class="tag ${g.category}">${CAT[g.category] || g.category}</span></td>
            <td class="right">${g.world_price.toFixed(2)}</td>
            <td class="dim">${cheap ? esc(cheap.name) + ' — ' + cheap.price.toFixed(2) : '—'}</td>
            <td class="dim">${dear ? esc(dear.name) + ' — ' + dear.price.toFixed(2) : '—'}</td>
            <td class="right ${g.spread > .5 ? 'good' : 'dim'}">${pct(g.spread)}</td>
            <td class="right dim">${qty(g.offered || 0)}</td>
            <td class="right dim">${qty(g.wanted || 0)}</td>
            <td class="right ${traded ? 'good' : 'dim'}">${traded ? qty(g.volume) : '—'}</td>
        </tr>`;
    }).join('');

    const g = rows.find(r => r.key === S.xGood) || rows[0];
    if (!g) return;
    $('xChartGood').textContent = g.name;
    $('xQuoteGood').textContent = g.name;
    $('xTradeGood').textContent = g.name;

    // одна строка на государство: цена страны — средняя по её областям,
    // рядом разброс, чтобы было видно, где обозы не успевают выровнять рынок
    $('xQuoteRows').innerHTML = (g.quotes || []).map(q => `<tr class="${q.is_mine ? 'sel' : ''}">
        <td><span class="dot-c" style="background:${q.color}"></span> ${esc(q.name)}${
            q.is_mine ? ' <span class="tag">вы</span>' : ''}</td>
        <td class="right">${q.price.toFixed(2)}</td>
        <td class="right dim" style="font-size:12px" title="${
            esc(q.low_region)} — ${q.low.toFixed(2)}, ${esc(q.high_region)} — ${q.high.toFixed(2)}">${
            q.regions > 1 ? q.low.toFixed(2) + '–' + q.high.toFixed(2) : '—'}</td>
        <td class="right ${q.spread > .05 ? 'bad' : q.spread < -.05 ? 'good' : 'dim'}">${
            (q.spread >= 0 ? '+' : '') + pct(q.spread)}</td>
        <td class="right dim">${qty(q.stock)}</td>
        <td class="right dim">${qty(q.demand)}</td>
        <td class="right ${q.capacity > 0 ? 'good' : 'dim'}">${
            q.capacity > 0 ? pct(q.capacity) : 'закрыта'}</td>
    </tr>`).join('');

    drawWorldChart();

    const side = (title, list, unit, key) => `<div class="card" style="margin:0">
        <h2>${title}</h2>
        ${list.length ? `<div class="tw"><table><tbody>${list.map(r => `<tr>
            <td>${esc(r.name)}</td>
            <td class="right">${qty(r.qty)}</td>
            <td class="right dim">по ${r.local_price.toFixed(2)} ₡ дома</td>
            <td class="right ${key === 'duty' ? 'good' : 'dim'}">${money(r[key])} ₡ ${unit}</td>
        </tr>`).join('')}</tbody></table></div>`
        : '<div class="empty">Сделок не было</div>'}</div>`;
    $('xTrades').innerHTML = side('Вывезли', g.exports || [], 'пошлины', 'duty')
        + side('Ввезли', g.imports || [], 'заплачено', 'paid');
}

/** Мировая цена против домашней. Вынесено из renderExchange: график живёт
 *  в сворачиваемой карточке и должен уметь перерисоваться сам, когда её
 *  раскроют, — иначе ширину он снимет с нулевой (свёрнутой) рамки. */
function drawWorldChart() {
    const x = S.exchange;
    if (!x) return;
    const g = (x.goods || []).find(r => r.key === S.xGood) || (x.goods || [])[0];
    if (!g) return;
    const world = (x.history || {})[g.key] || [];
    const local = (S.series || {})[g.key] || [];
    lineChart('worldChart', [
        { data: local.map(r => ({ x: r.tick, y: r.price })), color: '#4c9aff' },
        { data: world.map(r => ({ x: r.tick, y: r.price })), color: '#e3b341' },
    ], { fmt: v => v.toFixed(v < 10 ? 2 : 0) });
}

function pickXGood(key) { S.xGood = key; renderExchange(); }

/* ------------------------------ население ------------------------------ */
const CLASS_COLOR = { 'низший': '#8b97a8', 'средний': '#4c9aff', 'высший': '#e3b341' };

/** Что и сколько сословие потребляет: «Еда 0.78 / 0.91» с долей покрытия.
 *  Число слева — взято на человека за пейдей, справа — положено по корзине. */
function consumptionCells(rows) {
    if (!rows || !rows.length) return '<span class="dim">ничего не покупают</span>';
    const num = v => v >= 10 ? v.toFixed(0) : v >= 1 ? v.toFixed(2) : v.toFixed(3);
    return rows.map(r => {
        const col = r.share === null ? 'dim'
            : r.share < .5 ? 'bad' : r.share < .9 ? 'warn' : 'good';
        const of = r.norm > 0 ? ` / ${num(r.norm)}` : '';
        const tail = r.share === null ? '' : ` <span class="dim">${pct(r.share)}</span>`;
        return `<span class="tag eat${r.luxury ? ' luxury' : ''}"`
            + ` title="${esc(r.name)}: взято ${num(r.per_capita)}`
            + `${r.norm > 0 ? `, положено ${num(r.norm)}` : ''} на человека за пейдей">`
            + `${esc(r.name)} <b class="${col}">${num(r.per_capita)}</b>`
            + `<span class="dim">${of}</span>${tail}</span>`;
    }).join(' ');
}

function renderPeople() {
    const p = S.people;
    if (!p) return;
    $('peopleRegion').textContent = p.region_name || (S.world.country || {}).name || '—';
    regionPicker('peopleRegions', p.regions, p.region_id);
    $('pWorkers').textContent = qty(p.workers);
    $('pEmployed').textContent = qty(p.employed);
    $('pIdle').textContent = qty(p.unemployed);
    $('pIdle').className = 'v num ' + (p.unemployed > p.workers * .2 ? 'bad' : 'dim');
    $('pWage').textContent = p.reference_wage.toFixed(2) + ' ₡';

    const solCol = v => v < .75 ? 'bad' : v < 1.0 ? 'warn' : v < 1.25 ? 'good' : 'gold';
    $('pSol').textContent = (p.living_standard || 0).toFixed(2);
    $('pSol').className = 'v num ' + solCol(p.living_standard || 0);

    $('strataRows').innerHTML = p.strata.map(s => `<tr>
        <td>${esc(s.name)}${s.can_hire
            ? ' <span class="tag" title="Этих людей можно переманить на завод">наём</span>' : ''}</td>
        <td><span class="tag" style="color:${CLASS_COLOR[s.class]};border-color:${CLASS_COLOR[s.class]}55">${s.class}</span></td>
        <td class="right">${qty(s.people)}</td>
        <td class="right dim">${pct(s.share)}</td>
        <td><div class="bar" style="margin:0"><i style="width:${(s.share * 100).toFixed(1)}%;
             background:${CLASS_COLOR[s.class]}"></i></div></td>
        <td class="right">${s.income_per_capita.toFixed(2)} ₡</td>
        <td class="right ${solCol(s.living_standard)}"
            title="Во сколько раз больше обычной корзины сословие себе позволяет">${
            s.living_standard.toFixed(2)}</td>
        <td class="right dim" title="Уровень жизни, который сословие считает для себя нормой">${
            s.expectation.toFixed(2)}</td>
        <td class="right ${s.satisfaction < .4 ? 'bad' : s.satisfaction < .55 ? 'warn' : 'good'}">${pct(s.satisfaction)}</td>
        <td class="right dim">${money(s.savings)} ₡</td>
    </tr>`).join('');

    // лестница роскоши: что и на каком уровне жизни открывается
    $('ladderBox').innerHTML = '<div class="ladder">' + (p.luxury_ladder || []).map(l =>
        `<span class="step"><b>${l.unlock.toFixed(2)}</b> ${esc(l.name)}</span>`).join('')
        + '</div>';

    $('solRows').innerHTML = p.strata.filter(s => s.people > 0).map(s => `<tr>
        <td>${esc(s.name)}</td>
        <td class="right ${solCol(s.living_standard)}">${s.living_standard.toFixed(2)}</td>
        <td><div class="bar" style="margin:0" title="1.0 — обычная корзина">
            <i style="width:${Math.min(100, s.living_standard / 2 * 100).toFixed(0)}%;
               background:${s.living_standard < 1 ? 'var(--warn)' : 'var(--gold)'}"></i></div></td>
        <td style="font-size:12px">${consumptionCells(s.consumption)}</td>
        <td class="dim" style="font-size:13px">${s.luxuries && s.luxuries.length
            ? s.luxuries.map(l => `${esc(l.name)} <span class="dim">${pct(l.share)}</span>`).join(', ')
            : '<span class="dim">только необходимое</span>'}</td>
    </tr>`).join('');

    $('craftRows').innerHTML = p.crafts.length ? p.crafts.map(c => `<tr>
        <td>${esc(c.name)}</td><td class="right">${qty(c.people)}</td>
        <td class="right dim">${pct(c.share)}</td></tr>`).join('')
        : '<tr><td colspan="3" class="empty">Кустарей не осталось</td></tr>';

    $('cityStrataRows').innerHTML = p.cities.map(c => {
        const town = c.strata.town_low.people + c.strata.town_mid.people
            + c.strata.town_high.people;
        const h = c.harvest;
        return `<tr class="clickable ${c.id === p.region_id ? 'sel' : ''}"
                    onclick="pickRegion(${c.id})">
            <td>${esc(c.name)}${c.capital ? ' ★' : ''}${
                c.revolt ? ' <span class="bad">бунт</span>' : ''}</td>
            <td class="right ${h < .9 ? 'bad' : h > 1.1 ? 'good' : 'dim'}">${(h * 100).toFixed(0)}%</td>
            <td class="right">${qty(c.strata.peasants.people)}</td>
            <td class="right">${qty(c.strata.artisans.people)}</td>
            <td class="right">${qty(c.strata.workers.people)}</td>
            <td class="right">${qty(town)}</td>
            <td class="right ${c.unemployment > .25 ? 'bad' : 'dim'}">${pct(c.unemployment)}</td>
            <td class="right ${c.living_standard < .3 ? 'bad'
                : c.living_standard < .55 ? 'warn' : 'good'}">${c.living_standard.toFixed(2)}</td>
        </tr>`;
    }).join('');
}

/* ------------------------ мои предприятия ------------------------ */
/** Разброс зарплат по списку предприятий + заполнение поля «зп всем».
 *  Поле подставляется только когда игрок в нём не набирает: иначе число
 *  переписывалось бы прямо под курсором. */
function wageSpread(list, inputId) {
    if (!list.length) return '';
    const wages = list.map(b => b.wage);
    const lo = Math.min(...wages), hi = Math.max(...wages);
    const el = $(inputId);
    if (el && document.activeElement !== el) el.value = Math.round(hi);
    return lo === hi ? `Сейчас у всех ${lo.toFixed(2)} ₡`
        : `Сейчас от ${lo.toFixed(2)} до ${hi.toFixed(2)} ₡`;
}

/** Из чего сложилась прибавка за цепочку — подсказка на цифре бонуса. */
function chainTitle(chain) {
    if (!chain) return '';
    const parts = [];
    if (chain.links && chain.links.length)
        parts.push('звенья цепочки: ' + chain.links.join(', '));
    if (chain.mates && chain.mates.length)
        parts.push('соседи по отрасли: ' + chain.mates.join(', '));
    return parts.join('; ') || 'связанных предприятий рядом нет';
}

function renderBiz() {
    // Не перерисовываем список, пока игрок набирает зарплату в одном из полей:
    // иначе поле пересоздастся под курсором и ввод оборвётся на полуслове.
    const act = document.activeElement;
    if (act && act.tagName === 'INPUT' && $('bizList') && $('bizList').contains(act)) return;
    const bs = S.buildings;
    const sum = k => bs.reduce((a, b) => a + b[k], 0);
    $('sRev').textContent = money(sum('last_revenue')) + ' ₡';
    $('sInp').textContent = money(sum('last_inputs')) + ' ₡';
    $('sWage').textContent = money(sum('last_wages')) + ' ₡';
    const profit = sum('last_profit');
    $('sProfit').textContent = (profit >= 0 ? '+' : '') + money(profit) + ' ₡';
    $('sProfit').className = 'v num ' + (profit >= 0 ? 'good' : 'bad');
    $('sEmp').textContent = qty(sum('employed'));
    // Сбыт: доля выпуска, которую вообще удалось продать. Именно она объясняет
    // разрыв между «завод работает» и «денег нет».
    const made = sum('last_output'), soldQ = sum('last_sold');
    $('sSell').textContent = made > 0 ? pct(Math.min(1, soldQ / made)) : '—';
    $('sSell').className = 'v num ' + (made <= 0 ? 'dim'
        : soldQ / made < .6 ? 'bad' : soldQ / made < .9 ? 'warn' : 'good');
    $('sSell').title = made > 0
        ? `Продано ${qty(soldQ)} из ${qty(made)} выпущенных` : '';
    const unsold = sum('last_unsold');
    $('sUnsold').textContent = qty(unsold);
    $('sUnsold').className = 'v num ' + (unsold > 1 ? 'warn' : 'dim');
    $('sUnsold').title = 'Непроданный выпуск за прошлый пейдей. Он лежит на рынке '
        + 'и денег пока не принёс';
    $('bizWageCard').style.display = bs.length ? '' : 'none';
    $('bizWageHint').textContent = wageSpread(bs, 'bizWageAll');

    const hint = S.people ? S.people.reference_wage : 0;
    const ruined = S.me.bankrupt ? `<div class="card bad" style="margin-bottom:14px">
        <b>Вы банкрот.</b> Касса ${money(S.me.cash)} ₡ упёрлась в порог
        ${money(S.me.bankruptcy_limit)} ₡, установленный государством: все ваши
        предприятия остановлены и сами уже не запустятся. Строить и расширяться
        тоже нельзя. Дело о банкротстве закрывает <b>только государство</b> — и оно
        же решает, открыть всё хозяйство или одни прибыльные цеха.
        ${S.me.rescue_cost > 0 ? `Чтобы это стало возможно, кассе не хватает
            <b>${money(S.me.rescue_cost)} ₡</b> — просите субсидию у лидера или
            распродайте склад и снесите убыточное.`
            : 'Касса уже выше порога выхода — дело за решением лидера.'}
        </div>` : (S.me.halted ? `<div class="card warn" style="margin-bottom:14px">
        Под замком осталось предприятий: <b>${S.me.halted}</b>. Государство закрыло
        ваше банкротство, но эти цеха признало убыточными и не открыло. Запустить их
        может только лидер — либо снесите их сами.</div>` : '');
    $('bizList').innerHTML = ruined + (bs.length ? bs.map(b => {
        const ins = b.inputs.length
            ? b.inputs.map(i => `${esc(i.name)} ×${i.qty}`).join(', ') : 'ничего';
        const lowWage = b.wage < hint && b.fill < .95;
        const admin = b.kind === 'admin';
        const out = S.market.find(g => g.key === b.output_good);
        const noBuyer = !admin && out && out.demand < 1 && b.last_output > 0;
        // цех укомплектован людьми, но ничего не выпускает — значит нет сырья
        const starved = !admin && b.employed > 1 && b.last_output < 1 && b.active;
        // выпустил, но не продал: рынок затоварен, деньги не пришли
        const glut = !admin && b.sell_through != null && b.sell_through < .9;
        return `<div class="biz${b.damage ? ' hurt' : ''}${b.halted ? ' sealed' : ''}">
            <h3>${esc(b.industry)}
                <span class="lvl">${b.state ? '<span class="tag">казна</span> ' : ''}${
                    b.foreign ? '<span class="tag">за границей</span> ' : ''}ур. ${b.level}${
                    b.damage ? ` <span class="bad">(−${b.damage} разрушено)</span>` : ''}</span></h3>
            <div class="where">${esc(b.city)} · ${admin
                ? 'содержит служащих: ' + (b.upkeep_goods.map(i => `${esc(i.name)} ×${qty(i.qty)}`).join(', ') || '—')
                : 'производит ' + esc(b.output_good_name) + ' по ' + b.output_price + ' ₡ · сырьё: ' + ins}</div>
            <div class="bar"><i style="width:${Math.min(100, b.fill * 100)}%"></i></div>
            <dl>
                <dt>Рабочие</dt><dd>${qty(b.employed)} / ${qty(b.jobs)} (${pct(b.fill)})</dd>
                <dt>Зарплата</dt><dd class="${lowWage ? 'warn' : ''}">${b.wage.toFixed(2)} ₡</dd>
                ${b.chain_bonus > 0 ? `<dt>Цепочка</dt>
                <dd class="chain" title="${chainTitle(b.chain)}">+${pct(b.chain_bonus)} к выпуску</dd>` : ''}
                ${admin ? '' : `<dt>Выпуск</dt><dd>${qty(b.last_output)}</dd>
                <dt>Продано</dt><dd class="${b.sell_through == null ? ''
                    : b.sell_through < .6 ? 'bad' : b.sell_through < .9 ? 'warn' : 'good'}"
                    title="Деньги приходят в момент продажи, а не выпуска">${qty(b.last_sold)}${
                    b.sell_through != null ? ` (${pct(b.sell_through)})` : ''}</dd>
                <dt>На рынке лежит</dt><dd class="dim">${qty(b.last_stock)}</dd>
                <dt>Выручка</dt><dd>${money(b.last_revenue)} ₡</dd>`}
                <dt>Расходы</dt><dd>${money(b.last_costs)} ₡</dd>
                <dt>${admin ? 'Содержание' : 'Прибыль'}</dt>
                <dd class="${b.last_profit >= 0 ? 'good' : 'bad'}">${
                    (b.last_profit >= 0 ? '+' : '') + money(b.last_profit)} ₡</dd>
            </dl>
            ${b.halted ? `<div class="warn" style="font-size:12px;margin-bottom:8px">
                Цех остановлен банкротством и запечатан. Запустить его может только
                государство, закрыв дело о банкротстве.</div>` : ''}
            ${lowWage ? `<div class="warn" style="font-size:12px;margin-bottom:8px">
                Мало платите — люди не идут. В деревне зарабатывают ${hint.toFixed(2)} ₡.</div>` : ''}
            ${noBuyer ? `<div class="bad" style="font-size:12px;margin-bottom:8px">
                ${esc(b.output_good_name)} никто не покупает — товар копится на складе,
                а цена падает. Нужен тот, кто пустит его в дело.</div>`
            : glut ? `<div class="warn" style="font-size:12px;margin-bottom:8px">
                Продано лишь ${pct(b.sell_through)} выпуска — остальное легло на склад
                и денег не принесло, хотя зарплаты и налоги уплачены. Сбавьте выпуск,
                снизьте цену спросом или ищите рынок в соседней области.</div>` : ''}
            ${starved ? `<div class="bad" style="font-size:12px;margin-bottom:8px">
                Не хватает сырья — цех простаивает. Постройте поставщика
                или переждите.</div>` : ''}
            ${b.damage ? `<div class="bad" style="font-size:12px;margin-bottom:8px">
                Война выбила ${b.damage} ур. — работает ${b.effective_level} из ${b.level}.
                Пока не почините, эти цеха не дают ни рабочих мест, ни выпуска.</div>` : ''}
            <div class="acts">
                <label class="wagefield" title="Зарплата применяется сразу, как только вы её ввели">
                    <span>З/п</span>
                    <input type="number" id="w${b.id}" value="${b.wage.toFixed(0)}" min="0"
                           onchange="setWage(${b.id})">
                </label>
                <button class="sm" onclick="upgrade(${b.id})" ${S.me.bankrupt ? 'disabled' : ''}
                    title="Следующий уровень">Ур. ${b.level + 1} — ${money(b.upgrade_cost)} ₡</button>
                ${b.damage ? `<button class="sm primary" onclick="repairBiz(${b.id})"
                    title="Восстановить разрушенные уровни">Починить — ${money(b.repair_cost)} ₡</button>` : ''}
                <button class="sm" onclick="toggleBiz(${b.id})" ${b.halted ? 'disabled' : ''}
                    title="${b.halted ? 'Запечатано банкротством' : ''}">${
                    b.active ? 'Стоп' : 'Пуск'}</button>
                <button class="sm danger" onclick="demolish(${b.id})">Снести</button>
            </div>
        </div>`;
    }).join('') : '<div class="empty">Пока ни одного предприятия. Загляните во вкладку «Строительство».</div>');

    const wh = S.me.warehouse || [];
    $('whCard').style.display = wh.length ? '' : 'none';
    $('whRows').innerHTML = wh.map(w => `<tr><td>${esc(w.name)}</td>
        <td class="dim">${esc(w.country)}${w.foreign
            ? ' <span class="tag">за границей</span>' : ''}</td>
        <td class="right">${qty(w.qty)}</td><td class="right">${money(w.value)} ₡</td></tr>`).join('');
}

async function act(fn) {
    try { await fn(); await refresh(true); }
    catch (e) { toast(e.message, true); }
}
/** Зарплата применяется прямо из поля, без отдельной кнопки: ввели число —
 *  оно ушло на сервер. Перерисовку списка при этом откладываем, иначе поле
 *  пересоздалось бы прямо под курсором. */
const setWage = id => {
    const el = $('w' + id);
    const wage = Number(el.value);
    if (!isFinite(wage) || wage < 0) { toast('Зарплата должна быть числом', true); return; }
    api('/api/buildings/wage', { id, wage })
        .then(r => { toast('Зарплата: ' + r.building.wage.toFixed(2) + ' ₡'); })
        .catch(e => toast(e.message, true));
};
/** Одна ставка на все предприятия разом — своя или казённая. */
const setWageAll = scope => act(async () => {
    const el = $(scope === 'state' ? 'govWageAll' : 'bizWageAll');
    const wage = Number(el.value);
    // Пустое поле — это не «поставить всем ноль»: одним промахом мимо кнопки
    // так можно обнулить зарплату на всех заводах разом.
    if (!el.value.trim() || !isFinite(wage) || wage < 0) {
        toast('Введите зарплату', true);
        return;
    }
    const r = await api('/api/buildings/wage_all', { wage, scope });
    toast(`Зарплата ${wage.toFixed(2)} ₡ — на ${r.changed} из ${r.total} предприятий`
        + (r.raised_to_min_wage
            ? `; ${r.raised_to_min_wage} подняты до местного МРОТ` : ''));
});
const upgrade = id => act(async () => {
    const r = await api('/api/buildings/upgrade', { id });
    toast('Уровень ' + r.building.level);
});
const toggleBiz = id => act(() => api('/api/buildings/toggle', { id }));
const repairBiz = id => act(async () => {
    const r = await api('/api/buildings/repair', { id });
    toast('Восстановлено, потрачено ' + money(r.spent) + ' ₡');
});
const demolish = id => act(async () => {
    if (!confirm('Снести? Вернётся половина вложений.')) return;
    const r = await api('/api/buildings/demolish', { id });
    toast('Снесено, возврат ' + money(r.refund) + ' ₡');
});

/* ------------------------------ стройка ------------------------------ */
/** Подходит ли отрасль под строку поиска: имя, товар, сырьё или отрасль. */
function matchesQuery(i, q) {
    if (!q) return true;
    const hay = [i.name, i.output_good_name, i.sector_name, i.description || '']
        .concat((i.inputs || []).map(x => x.name)).join(' ').toLowerCase();
    return hay.includes(q);
}

function renderBuild() {
    // Строим в области: у каждой свой рынок сырья и своя рабочая сила.
    regionPicker('buildRegions', S.regions, S.region);
    const here = S.cities.find(c => c.id === S.region);
    const wage = S.people ? S.people.reference_wage : 0;
    $('buildHint').innerHTML = `${here ? `Строим в области <b>${esc(here.name)}</b>${
        here.capital ? ' (столица)' : ''} — ${qty(here.population)} чел.,
        безработица ${pct(here.unemployment)}.<br>` : ''}Чтобы люди пошли на завод, платить надо больше,
        чем даёт привычное занятие — сейчас это <b class="good">${wage.toFixed(2)} ₡</b> за пейдей.
        Новому предприятию зарплата ставится с запасом автоматически. Если в стране есть
        свободные рабочие, они нанимаются и при меньшей ставке.`;

    const ch = S.chainRules || {};
    $('chainNote').innerHTML = `Разрозненные заводы работают хуже связанных.
        Каждое <b>звено цепочки</b> во дворе — сосед, который делает для вас сырьё
        или пускает ваш выпуск в дело, — прибавляет
        <b class="good">+${pct(ch.link_bonus || 0)}</b> к выпуску, каждый <b>сосед по
        отрасли</b> — ещё <b class="good">+${pct(ch.sector_bonus || 0)}</b>, до
        <b>+${pct(ch.cap || 0)}</b> в сумме. Считается по вашим предприятиям в одной
        области, поэтому выгоднее поднимать цепочку целиком и в одной отрасли,
        чем хватать всё подряд. Второго такого же завода в одной области не строят —
        только новый уровень.`;

    // Фильтры: отрасль и поиск. Отраслей уже два десятка, и без них таблица
    // превращается в простыню.
    const sectors = S.sectors || [];
    const cur = S.buildSector || 'all';
    $('buildSectors').innerHTML = [{ key: 'all', name: 'Все отрасли' }]
        .concat(sectors).map(s =>
            `<button class="rg${s.key === cur ? ' on' : ''}"
                onclick="pickSector('${s.key}')">${esc(s.name)}</button>`).join('');

    const q = ($('buildSearch').value || '').trim().toLowerCase();
    const prod = S.industries.filter(i => i.kind !== 'admin'
        && (cur === 'all' || i.sector === cur) && matchesQuery(i, q));

    $('buildRows').innerHTML = prod.length ? prod.map(i => {
        const ins = i.inputs.length
            ? i.inputs.map(x => `<span class="${x.available ? '' : 'bad'}">${esc(x.name)} ×${x.qty}</span>`).join(', ')
            : '<span class="dim">ничего</span>';
        const short = i.shortage > .3 ? 'bad' : i.shortage > .05 ? 'warn' : 'dim';
        const sell = i.sell_through;
        const chain = i.chain || {};
        // Такой же завод здесь уже стоит — значит только уровень
        const mine = i.mine, stateHere = i.state_here;
        return `<tr>
            <td>${esc(i.name)}<br><span class="tag">${esc(i.sector_name)}</span></td>
            <td>${esc(i.output_good_name)} <span class="dim">${i.output_per_worker}/раб.</span></td>
            <td class="dim" style="font-size:13px">${ins}</td>
            <td class="right ${i.value_per_worker > 0 ? 'good' : 'bad'}">${i.value_per_worker.toFixed(1)} ₡
                ${sell != null && sell < .995 ? `<br><span class="dim" style="font-size:12px"
                    title="С поправкой на сбыт: непроданное денег не приносит">${
                    i.value_per_worker_net.toFixed(1)} ₡ по факту</span>` : ''}</td>
            <td class="right ${sell == null ? 'dim' : sell < .6 ? 'bad'
                : sell < .9 ? 'warn' : 'good'}">${sell == null ? '—' : pct(sell)}</td>
            <td class="right ${chain.bonus > 0 ? 'chain' : 'dim'}"
                title="${chainTitle(chain)}">${chain.bonus > 0 ? '+' + pct(chain.bonus) : '—'}</td>
            <td class="right">${i.inputs_ready
                ? '<span class="good">есть</span>' : '<span class="bad">нет в стране</span>'}</td>
            <td class="right ${i.has_buyer ? 'dim' : 'bad'}">${i.has_buyer
                ? qty(i.output_demand) : 'покупателя нет'}</td>
            <td class="right ${short}">${i.shortage > .005 ? pct(i.shortage) : '—'}</td>
            <td class="right dim">${i.state_levels} / ${i.private_levels}</td>
            <td class="right">${mine ? money(mine.upgrade_cost) : money(i.build_cost)} ₡</td>
            <td class="right" style="white-space:nowrap">
                ${mine
                    ? `<button class="sm primary" onclick="upgrade(${mine.id})"
                        title="Такое предприятие здесь у вас уже есть — второе не строят">
                        Ур. ${mine.level} → ${mine.level + 1}</button>`
                    : `<button class="sm primary" onclick="buildBiz('${i.key}')">Построить</button>`}
                ${S.me.is_governor ? (stateHere
                    ? `<button class="sm" onclick="upgrade(${stateHere.id})"
                        title="Казённое предприятие этой отрасли здесь уже есть">
                        казной ур. ${stateHere.level + 1}</button>`
                    : `<button class="sm" onclick="buildBiz('${i.key}', true)"
                        title="За счёт казны. Цепочка казны здесь: ${
                            (i.state_chain || {}).bonus
                                ? '+' + pct(i.state_chain.bonus) + ' — ' + chainTitle(i.state_chain)
                                : 'связанных казённых предприятий рядом нет'}">казной</button>`) : ''}
            </td>
        </tr>`;
    }).join('') : '<tr><td colspan="12" class="empty">Ничего не нашлось — '
        + 'смените отрасль или запрос</td></tr>';

    const admins = S.industries.filter(i => i.kind === 'admin');
    $('adminCard').style.display = admins.length ? '' : 'none';
    $('adminRows').innerHTML = admins.map(i => {
        // Здание с пределом развития: показываем, сколько уровней уже занято в
        // этой области и во что обойдётся следующий — у палаты он кусается.
        const here = i.state_here || i.mine;
        const capped = i.max_level && i.levels_here >= i.max_level;
        const next = here ? here.upgrade_cost : i.build_cost;
        return `<tr>
        <td>${esc(i.name)}${i.max_level
            ? ` <span class="tag ${capped ? 'luxury' : ''}">${i.levels_here} / ${i.max_level} ур.</span>` : ''}</td>
        <td class="dim" style="font-size:13px;max-width:320px">${esc(i.description)}</td>
        <td class="dim" style="font-size:13px">${i.upkeep_goods.map(x =>
            `${esc(x.name)} ×${x.qty}`).join(', ') || '—'}</td>
        <td class="right">${qty(i.jobs_per_level)}</td>
        <td class="right dim">${money(i.cost_per_level)} ₡</td>
        <td class="right ${next > 1e7 ? 'warn' : ''}">${money(next)} ₡</td>
        <td class="right">${capped
            ? '<span class="dim">развита до предела</span>'
            : `<button class="sm ${S.me.is_governor ? 'primary' : ''}"
            ${S.me.is_governor ? '' : 'disabled title="Нужны полномочия гос.деятеля"'}
            onclick="${here ? `upgrade(${here.id})` : `buildBiz('${i.key}', true)`}">${
                here ? 'Поднять уровень' : 'Построить казной'}</button>`}</td>
    </tr>`;
    }).join('');
}

function pickSector(key) { S.buildSector = key; renderBuild(); }

const buildBiz = (key, state) => act(async () => {
    const r = await api('/api/buildings/build',
        { industry_key: key, city_id: S.region, state: !!state });
    toast((state ? 'Казна строит: ' : 'Построено: ')
        + r.building.industry + ' в городе ' + r.building.city);
});

/* ------------------------------ страна ------------------------------ */
function renderCountry() {
    // области своей страны: у каждой свой рынок, своё довольство и своё
    // озлобление — сравнивать их между собой и есть смысл этой таблицы
    $('cityRows').innerHTML = S.cities.map(c => {
        const unrest = c.unrest || 0;
        const state = c.revolt_ticks
            ? `<span class="bad">бунт, ${c.revolt_ticks} пейдей(-ев)</span>`
            : unrest > .5 ? '<span class="warn">зреет недовольство</span>'
                : '<span class="dim">спокойно</span>';
        return `<tr class="clickable ${c.id === S.region ? 'sel' : ''}"
                    onclick="pickRegion(${c.id})">
            <td>${esc(c.name)}${c.capital ? ' <span class="tag">столица</span>' : ''}</td>
            <td class="right">${qty(c.population)}</td>
            <td class="right ${c.unemployment > .25 ? 'bad' : c.unemployment > .12 ? 'warn' : 'good'}">${pct(c.unemployment)}</td>
            <td class="right ${c.satisfaction < .4 ? 'bad' : c.satisfaction < .55 ? 'warn' : 'good'}">${pct(c.satisfaction)}</td>
            <td class="right ${c.living_standard < .3 ? 'bad' : c.living_standard < .55 ? 'warn' : 'good'}">${c.living_standard.toFixed(2)}</td>
            <td class="right">${c.avg_wage.toFixed(2)} ₡</td>
            <td class="right ${(c.access || 0) < .3 ? 'bad' : (c.access || 0) < .6 ? 'warn'
                : (c.access || 0) < .999 ? 'good' : 'gold'}"
                title="Торговых палат: ${c.chambers || 0} ур. Доступность решает, насколько быстро цены здесь сравниваются с остальной страной и какой объём область пропускает через границу">${
                pct(c.access || 0)}</td>
            <td><div class="bar" style="margin:0" title="Озлобление ${pct(unrest)}">
                <i style="width:${Math.min(100, unrest * 100).toFixed(0)}%;
                   background:${unrest > .7 ? 'var(--bad)' : 'var(--warn)'}"></i></div></td>
            <td class="dim" style="font-size:12px">${state}</td>
        </tr>`;
    }).join('');

    drawGdpChart();

    api('/api/events?limit=20').then(r => {
        $('eventRows').innerHTML = r.events.length
            ? r.events.map(e => `<li><b>#${e.tick}</b>${esc(e.message)}</li>`).join('')
            : '<li class="dim">Пока ничего не произошло</li>';
    }).catch(() => {});
}

function drawGdpChart() {
    if (!S.macro) return;
    lineChart('gdpChart', [
        { data: S.macro.map(r => ({ x: r.tick, y: r.gdp })), color: '#4c9aff' },
        { data: S.macro.map(r => ({ x: r.tick, y: r.treasury })), color: '#e3b341' },
    ], { fmt: money, zero: true });
}

/** Перерисовать все графики разом. Нужен там, где ширина рамки только что
 *  изменилась: раскрыли свёрнутую карточку, повернули телефон, потянули окно.
 *  lineChart снимает ширину с живого DOM (svg.clientWidth), поэтому без этого
 *  график, нарисованный в свёрнутом виде, остаётся растянутым по запасным
 *  600 пикселям. */
function redrawCharts() {
    if (!S.world) return;
    if (S.market && S.market.length) drawPriceChart();
    if (S.macro) { drawMacroChart(); drawGdpChart(); }
    if (S.exchange) drawWorldChart();
}

/* ------------------------------ государство ------------------------------ */
function renderGov() {
    const c = S.world.country;
    if (!c) return;
    const wld = S.world.world || {};
    $('gTreasury').textContent = money(c.treasury) + ' ₡';
    $('gGdp').textContent = money(c.gdp) + ' ₡';
    $('gWage').textContent = (wld.avg_wage || 0).toFixed(2) + ' ₡';
    $('gInd').textContent = pct(c.industrialisation);
    renderBudget(c.budget);
    renderGovBuildings();
    renderElections();
    renderConfidence();
    renderCitizens();
    if (document.activeElement && document.activeElement.tagName === 'INPUT') return;
    $('gCorp').value = Math.round(c.corporate_tax * 100);
    $('gSales').value = Math.round(c.sales_tax * 100);
    $('gInc').value = Math.round(c.income_tax * 100);
    // Подушная подать — не доля, а твёрдая сумма с души, поэтому без ×100.
    $('gPoll').value = (c.poll_tax || 0).toFixed(2);
    $('gTithe').value = Math.round((c.tithe || 0) * 100);
    $('gWealth').value = ((c.wealth_tax || 0) * 100).toFixed(1);
    $('gExcise').value = Math.round((c.excise_tax || 0) * 100);
    $('gSpend').value = Math.round(c.public_spending_rate * 100);
    $('gRent').value = Math.round(c.land_rent * 100);
    $('gTariff').value = Math.round((c.tariff || 0) * 100);
    $('gImport').value = Math.round((c.import_tariff || 0) * 100);
    $('gMin').value = Math.round(c.min_wage);
    $('gBank').value = Math.round(c.bankruptcy_limit);
    const cb = $('gForeign');
    if (cb) cb.checked = !!c.foreign_investment_open;
    const can = S.me.is_governor && c.leader_is_ai === false && c.id === S.me.country_id;
    $('govSave').disabled = !can;
    $('govNote').innerHTML = can
        ? 'Вы лидер государства «' + c.name + '». Налоги наполняют казну, госрасходы '
        + 'возвращают деньги людям. Земельная рента забирает часть выручки деревни в '
        + 'пользу высшего класса. Помните, что <b>подоходный налог</b> удерживается '
        + 'только с заводских зарплат, а их получают одни рабочие: деревня и горожане '
        + 'проходят мимо него целиком. Взять с них можно <b>подушной податью</b> (с души, '
        + 'ложится на всех поровну и потому больнее всего — на бедных), <b>оброком</b> '
        + '(доля рыночной выручки крестьян и кустарей), <b>налогом на сбережения</b> '
        + '(с накопленного, тяжелее всего для высшего класса) и <b>акцизом на роскошь</b> '
        + '(его платит только тот, кто уже живёт лучше обычного). '
        + '<b>Вывозная пошлина</b> берётся с каждой сделки на '
        + 'бирже и оседает в казне, но чем она выше, тем меньше своим смысла вывозить. '
        + '<b>Ввозная</b> защищает своих: чужой товар обходится в мировую цену плюс '
        + 'пошлину, и пока дома дешевле — импорт до прилавка не дойдёт. Откройте страну '
        + 'для иностранных инвестиций, чтобы чужие промышленники могли строить у вас.'
        : (c.leader_is_ai
            ? 'Государством управляет AI. Зарегистрируйтесь здесь и победите на выборах, '
              + 'чтобы стать лидером.'
            : 'Лидер государства — «' + (c.leader || 'другой игрок') + '». Здесь видно, '
              + 'по каким правилам живёт страна.');
}

/** Роспись казны: каждая статья — своя строка, доли считаются от итога.
 *
 *  Статьи с нулём не прячем намеренно: пустая строка «Подушная подать» — это
 *  подсказка, что рычаг есть и он не задействован. Пустой остаётся только та
 *  сторона отчёта, где вообще ничего не было.
 */
function renderBudget(b) {
    if (!b) return;
    const sign = v => (v > 0 ? 'good' : v < 0 ? 'bad' : 'dim');
    $('bOpen').textContent = money(b.opening) + ' ₡';
    $('bIn').textContent = '+' + money(b.total_income) + ' ₡';
    $('bOut').textContent = '−' + money(b.total_expense) + ' ₡';
    const net = $('bNet');
    net.textContent = (b.net >= 0 ? '+' : '−') + money(Math.abs(b.net)) + ' ₡';
    net.className = 'v num ' + sign(b.net);
    $('bClose').textContent = money(b.closing) + ' ₡';

    const rows = (list, total) => list.length
        ? list.map(r => `<tr title="${esc(r.note)}">
            <td>${esc(r.name)}</td>
            <td class="right ${r.amount ? '' : 'dim'}">${money(r.amount)}</td>
            <td class="right dim">${r.per_capita.toFixed(2)}</td>
            <td class="right dim">${total > 0 ? pct(r.amount / total) : '—'}</td>
          </tr>`).join('')
        : '<tr><td colspan="4" class="dim">Ничего не было</td></tr>';
    $('budgetIn').innerHTML = rows(b.income, b.total_income);
    $('budgetOut').innerHTML = rows(b.expense, b.total_expense);
}

function renderElections() {
    const box = $('govElections');
    if (!box) return;
    const el = S.elections;
    if (!el) { box.innerHTML = ''; return; }
    const phase = el.phase;
    let html = '<div style="font-size:13px;margin-bottom:8px">';
    if (phase === 'voting') {
        html += '<span class="tag" style="background:var(--accent)">Идёт голосование</span> '
            + 'Выборы лидера государства. Отдайте голос за кандидата.';
    } else {
        html += '<span class="dim">Голосование не идёт. ';
        html += phase === 'campaign' ? 'Идёт предвыборная кампания.' : 'Ожидание следующего цикла.';
        html += '</span>';
    }
    html += '</div>';
    const cands = el.candidates || [];
    if (cands.length) {
        html += '<div class="tw"><table style="font-size:13px"><tbody>' + cands.map(cd =>
            `<tr><td>${esc(cd.username)}${cd.is_me ? ' (вы)' : ''}</td>
             <td class="right dim">${cd.votes} гол.</td>
             <td class="right">${phase === 'voting' && !cd.is_me
                ? `<button class="sm" onclick="voteFor(${cd.id})">Голосовать</button>` : ''}</td></tr>`
        ).join('') + '</tbody></table></div>';
    } else {
        html += '<div class="dim" style="font-size:13px">Кандидатов (игроков-граждан) пока нет.</div>';
    }
    box.innerHTML = html;
}

const voteFor = id => act(async () => {
    await api('/api/elections/vote', { candidate_id: id });
    toast('Голос отдан');
});

/* ------------------------ доверие лидеру ------------------------ */
function renderConfidence() {
    const box = $('confidenceBox');
    if (!box) return;
    const cf = S.elections && S.elections.confidence;
    if (!cf) { box.innerHTML = ''; return; }
    if (!cf.leader) {
        box.innerHTML = '<div class="dim" style="font-size:13px">Лидера сейчас нет — '
            + 'вотум не о ком.</div>';
        return;
    }
    const need = cf.needed, have = cf.distrust;
    const frac = cf.players ? Math.min(1, have / Math.max(need, 1)) : 0;
    box.innerHTML = `<div style="font-size:13px;margin-bottom:8px">
            Лидер — <b>${esc(cf.leader)}</b>. Недоверие выразили
            <b class="${have >= need ? 'bad' : ''}">${have}</b> из ${cf.players}
            промышленников, для внеочередных выборов нужно <b>${need}</b>.
            ${cf.trust ? `Доверие подтвердили ${cf.trust}.` : ''}
        </div>
        <div class="bar" style="margin:0"><i style="width:${(frac * 100).toFixed(0)}%;
            background:${have >= need ? 'var(--bad)' : 'var(--warn)'}"></i></div>
        <div style="font-size:12px;margin-top:8px" class="${cf.my_vote === 'distrust' ? 'bad'
            : cf.my_vote === 'trust' ? 'good' : 'dim'}">
            ${cf.my_vote === 'distrust' ? 'Вы выразили недоверие.'
            : cf.my_vote === 'trust' ? 'Вы поддерживаете лидера.'
            : 'Вы ещё не высказались.'}
            ${!cf.enough_players ? ` Пока в стране меньше ${cf.min_players} промышленников,`
                + ' вотум не действует.' : ''}
        </div>`;
}

const voteConfidence = verdict => act(async () => {
    const r = await api('/api/gov/confidence', { verdict });
    toast(verdict === 'trust' ? 'Вы поддержали лидера'
        : `Недоверие: ${r.confidence.distrust} из ${r.confidence.players}`);
});

/* ------------------------ граждане и субсидии ------------------------ */
function renderCitizens() {
    const d = S.citizens;
    if (!d || !$('citizenRows')) return;
    const lead = !!d.is_leader;
    $('cBankrupt').textContent = d.bankrupt;
    $('cBankrupt').className = 'v num ' + (d.bankrupt ? 'bad' : 'good');
    $('cLimit').textContent = money(d.bankruptcy_limit) + ' ₡';
    $('cExit').textContent = money(d.exit_level) + ' ₡';
    $('cExit').title = 'Пока касса ниже этого числа, снимать банкротство '
        + 'бессмысленно — дело провалится тем же пейдеем';
    $('cSubs').textContent = money(d.last_subsidies) + ' ₡';
    $('cTreasury').textContent = money(d.treasury) + ' ₡';

    $('citizenRows').innerHTML = d.citizens.length ? d.citizens.map(p => `<tr>
        <td>${esc(p.username)}${p.is_leader ? ' <span class="tag">лидер</span>' : ''}${
            p.username === S.me.username ? ' <span class="tag">вы</span>' : ''}</td>
        <td class="right ${p.cash < 0 ? 'bad' : ''}">${money(p.cash)} ₡</td>
        <td class="right dim">${money(p.net_worth)} ₡</td>
        <td class="right">${p.buildings}${p.damaged
            ? ` <span class="bad" title="разрушено уровней">(−${p.damaged})</span>` : ''}${
            p.halted ? ` <span class="warn" title="запечатано банкротством">(⏸${p.halted})</span>` : ''}</td>
        <td class="right dim">${qty(p.employees)}</td>
        <td class="right ${p.profit >= 0 ? 'good' : 'bad'}">${
            (p.profit >= 0 ? '+' : '') + money(p.profit)} ₡</td>
        <td>${p.bankrupt
            ? `<span class="bad">банкрот с #${p.bankrupt_since}</span>`
            : p.halted ? '<span class="warn">часть цехов под замком</span>'
                : '<span class="dim">в деле</span>'}</td>
        <td class="right" style="white-space:nowrap">${lead ? `
            <input type="number" id="sub${p.id}" style="width:110px" step="100000"
                   value="${Math.max(0, Math.round(p.rescue_cost)) || ''}"
                   placeholder="сумма">
            <button class="sm ${p.bankrupt ? 'primary' : ''}"
                onclick="giveSubsidy(${p.id})">Выдать</button>` : ''}</td>
        <td class="right" style="white-space:nowrap">${
            !lead || (!p.bankrupt && !p.halted) ? '<span class="dim">—</span>'
            : !p.can_release && p.bankrupt
                ? `<span class="dim" title="Сначала субсидия">не хватает ${
                    money(p.rescue_cost)} ₡</span>`
            : `<button class="sm primary" onclick="endBankruptcy(${p.id},'all')"
                   title="Открыть всё хозяйство">Открыть всё (${p.halted})</button>
               <button class="sm" onclick="endBankruptcy(${p.id},'profitable')"
                   title="Открыть только те цеха, что работали в плюс; убыточные останутся под замком">
                   Только прибыльные (${p.halted_profitable})</button>`}</td>
    </tr>`).join('') : '<tr><td colspan="9" class="empty">Промышленников пока нет</td></tr>';
}

/** Государство закрывает дело о банкротстве и решает, что запускать обратно. */
const endBankruptcy = (id, mode) => act(async () => {
    const r = await api('/api/gov/bankruptcy', { player_id: id, mode });
    toast(`Банкротство закрыто: запущено ${r.opened} предприятий`
        + (r.sealed ? `, ${r.sealed} убыточных остались под замком` : ''));
});

const giveSubsidy = id => act(async () => {
    const amount = Number($('sub' + id).value);
    if (!(amount > 0)) { toast('Введите сумму субсидии', true); return; }
    const r = await api('/api/gov/subsidy', { player_id: id, amount });
    toast('Выдано ' + money(amount) + ' ₡, в казне ' + money(r.treasury) + ' ₡');
});

/** Казённые предприятия управляются ОТСЮДА, а не из «Моих предприятий»:
 *  они принадлежат государству, а не лидеру лично. */
function renderGovBuildings() {
    const bs = S.govBuildings || [];
    const box = $('govBuildings');
    if (!box) return;
    const act = document.activeElement;
    if (act && act.tagName === 'INPUT' && box.contains(act)) return;
    const can = !!S.canManageState;
    // «Установить зп всем» — только лидеру и только если есть чему её ставить
    const wageBox = $('govWageBox');
    if (wageBox) {
        wageBox.style.display = can && bs.length ? '' : 'none';
        if ($('govWageBtn')) $('govWageBtn').disabled = !can;
        wageSpread(bs, 'govWageAll');
    }
    box.innerHTML = bs.length ? bs.map(b => {
        const admin = b.kind === 'admin';
        const ins = b.inputs.length
            ? b.inputs.map(i => `${esc(i.name)} ×${i.qty}`).join(', ') : 'ничего';
        const profit = b.last_profit;
        return `<div class="biz${b.damage ? ' hurt' : ''}">
            <h3>${esc(b.industry)}
                <span class="lvl">ур. ${b.level}${b.damage
                    ? ` <span class="bad">(−${b.damage})</span>` : ''}</span></h3>
            <div class="where">${esc(b.city)} · ${b.output_good
                ? 'производит ' + esc(b.output_good_name) + ' · сырьё: ' + ins
                : 'содержит служащих: ' + (b.upkeep_goods.map(i =>
                    `${esc(i.name)} ×${qty(i.qty)}`).join(', ') || '—')}</div>
            <div class="bar"><i style="width:${Math.min(100, b.fill * 100)}%"></i></div>
            <dl>
                <dt>Рабочие</dt><dd>${qty(b.employed)} / ${qty(b.jobs)} (${pct(b.fill)})</dd>
                <dt>Зарплата</dt><dd>${b.wage.toFixed(2)} ₡</dd>
                ${b.chain_bonus > 0 ? `<dt>Цепочка</dt>
                <dd class="chain" title="${chainTitle(b.chain)}">+${pct(b.chain_bonus)} к выпуску</dd>` : ''}
                ${b.output_good ? `<dt>Выпуск</dt><dd>${qty(b.last_output)}</dd>
                <dt>Продано</dt><dd class="${b.sell_through == null ? ''
                    : b.sell_through < .6 ? 'bad' : b.sell_through < .9 ? 'warn' : 'good'}">${
                    qty(b.last_sold)}${b.sell_through != null
                        ? ` (${pct(b.sell_through)})` : ''}</dd>
                <dt>Выручка</dt><dd>${money(b.last_revenue)} ₡</dd>` : ''}
                <dt>${admin && !b.output_good ? 'Содержание' : 'Прибыль'}</dt>
                <dd class="${profit >= 0 ? 'good' : 'bad'}">${
                    (profit >= 0 ? '+' : '') + money(profit)} ₡</dd>
            </dl>
            ${b.damage ? `<div class="bad" style="font-size:12px;margin-bottom:8px">
                Разрушено ${b.damage} ур. — работает ${b.effective_level} из ${b.level}.</div>` : ''}
            ${can ? `<div class="acts">
                <label class="wagefield" title="Применяется сразу">
                    <span>З/п</span>
                    <input type="number" id="w${b.id}" value="${b.wage.toFixed(0)}"
                           min="0" onchange="setWage(${b.id})">
                </label>
                <button class="sm" onclick="upgrade(${b.id})">Ур. ${b.level + 1} — ${
                    money(b.upgrade_cost)} ₡</button>
                ${b.damage ? `<button class="sm primary" onclick="repairBiz(${b.id})">Починить — ${
                    money(b.repair_cost)} ₡</button>` : ''}
                <button class="sm" onclick="toggleBiz(${b.id})">${b.active ? 'Стоп' : 'Пуск'}</button>
                <button class="sm danger" onclick="demolish(${b.id})">Снести</button>
            </div>` : '<div class="dim" style="font-size:12px">Управляет лидер государства.</div>'}
        </div>`;
    }).join('') : '<div class="empty">У государства пока нет предприятий.</div>';
}

const savePolicy = () => act(async () => {
    const body = {
        corporate_tax: Number($('gCorp').value) / 100,
        sales_tax: Number($('gSales').value) / 100,
        income_tax: Number($('gInc').value) / 100,
        // подушная подать задаётся в червонцах с души, а не в процентах
        poll_tax: Number($('gPoll').value),
        tithe: Number($('gTithe').value) / 100,
        wealth_tax: Number($('gWealth').value) / 100,
        excise_tax: Number($('gExcise').value) / 100,
        public_spending_rate: Number($('gSpend').value) / 100,
        land_rent: Number($('gRent').value) / 100,
        tariff: Number($('gTariff').value) / 100,
        import_tariff: Number($('gImport').value) / 100,
        min_wage: Number($('gMin').value),
        bankruptcy_limit: Math.min(0, Number($('gBank').value)),
    };
    const cb = $('gForeign');
    if (cb) body.foreign_investment_open = !!cb.checked;
    await api('/api/gov/policy', body);
    toast('Курс утверждён');
});

/* ------------------- судьба промышленников на занятой земле ------------------- */
function renderAnnexations() {
    const box = $('annexList');
    if (!box) return;
    const d = S.annex;
    if (!d) { box.innerHTML = ''; return; }
    const list = d.annexations || [];
    const open = list.filter(a => !a.resolved);
    $('annexCard').style.display = list.length ? '' : 'none';
    if (!list.length) return;

    const lead = d.is_leader;
    box.innerHTML = list.map(a => {
        const left = a.deadline - d.tick;
        const rows = (arr, decided) => arr.map(r => `<tr>
            <td>${esc(r.username)} <span class="dim">(${esc(r.home)})</span></td>
            <td class="right">${r.buildings} предпр., ${r.levels} ур.</td>
            <td class="right dim">${qty(r.employees)} рабочих</td>
            <td class="dim" style="font-size:12px">${r.industries.map(esc).join(', ') || '—'}</td>
            <td class="right" style="white-space:nowrap">${decided
                ? (r.decision === 'expel'
                    ? '<span class="bad">выдворен, заводы снесены</span>'
                    : '<span class="good">оставлен в деле</span>')
                : (lead ? `<button class="sm" onclick="decideAnnex(${a.id},'keep',${r.player_id})">Оставить</button>
                   <button class="sm danger" onclick="decideAnnex(${a.id},'expel',${r.player_id})">Снести</button>`
                    : '<span class="dim">решает лидер</span>')}</td>
        </tr>`).join('');
        return `<div class="biz${a.resolved ? '' : ' hurt'}">
            <h3>${esc(a.city)} <span class="lvl">отнята у ${esc(a.former)}${
                a.resolved ? '' : ` · решить за ${Math.max(0, left)} пейдей(-ев)`}</span></h3>
            ${a.pending.length ? `<div class="tw"><table style="font-size:13px"><tbody>${
                rows(a.pending, false)}</tbody></table></div>` : ''}
            ${a.decided.length ? `<div class="tw"><table style="font-size:12px;margin-top:8px"><tbody>${
                rows(a.decided, true)}</tbody></table></div>` : ''}
            ${a.resolved ? '<div class="muted" style="font-size:12px;margin-top:8px">Решено.</div>'
            : lead && a.pending.length ? `<div class="acts" style="margin-top:10px">
                <button class="sm primary" onclick="decideAnnex(${a.id},'keep')">Оставить всех</button>
                <button class="sm danger" onclick="decideAnnex(${a.id},'expel')">Снести всё</button>
              </div>` : ''}
        </div>`;
    }).join('');

    if (open.length && lead) {
        box.insertAdjacentHTML('afterbegin',
            `<div class="warn" style="font-size:13px;margin-bottom:12px">
                Ждут вашего решения: ${open.length} обл. Не решите в срок — промышленников
                оставят в деле.</div>`);
    }
}

const decideAnnex = (annexId, decision, playerId) => act(async () => {
    if (decision === 'expel' && !confirm(playerId
        ? 'Выдворить промышленника? Его заводы в этой области снесут.'
        : 'Выдворить всех? Все их заводы в этой области снесут.')) return;
    const body = { annex_id: annexId, decision };
    if (playerId !== undefined) body.player_id = playerId;
    const r = await api('/api/annexations/decide', body);
    const razed = (r.results || []).reduce((a, x) => a + (x.buildings || 0), 0);
    toast(decision === 'expel' ? `Выдворено, снесено предприятий: ${razed}`
        : 'Промышленники оставлены в деле');
});

/* --------------------------- война и дипломатия --------------------------- */
function renderWar() {
    const d = S.diplo;
    if (!d) return;
    const a = d.army || {};
    const lead = d.is_leader;

    $('aSold').textContent = qty(a.soldiers || 0);
    $('aAfford').textContent = qty(a.affordable || 0);
    // вооружённость — ЗАПАС арсенала к штату; расход — ПОТРЕБЛЕНИЕ за пейдей.
    // Это разные вещи: полный арсенал не значит, что завод остался без заказа.
    $('aEquip').textContent = pct(a.equip || 0);
    $('aEquip').title = `${qty(a.weapons || 0)} / ${qty(a.weapons_target || 0)} ед. оружия на складах`;
    $('aEquip').className = 'v num ' + (a.equip < .35 ? 'bad' : a.equip < .75 ? 'warn' : 'good');
    $('aWeapUse').textContent = qty(a.weapons_demand || 0);
    $('aWeapUse').title = `Износ за пейдей ${qty(a.weapons_worn || 0)}, `
        + `куплено ${qty(a.weapons_bought || 0)} из ${qty(a.weapons_demand || 0)} заказанных`;
    $('aWeapUse').className = 'v num '
        + ((a.weapons_bought || 0) + 1e-6 < (a.weapons_demand || 0) * .9 ? 'warn' : 'dim');
    $('aShells').textContent = qty(a.shells || 0) + ' / ' + qty(a.shells_target || 0);
    $('aShells').className = 'v num ' + ((a.battles_covered || 0) < 1 ? 'warn' : 'good');
    $('aStr').textContent = qty(a.strength || 0);
    $('aCost').textContent = money(a.last_cost || 0) + ' ₡';

    $('warNote').innerHTML = lead
        ? 'Численность армии — это не ползунок, а арифметика: <b>военный бюджет ÷ '
        + 'жалованье солдату</b>. Больше этого числа никого не наберут, поэтому казна '
        + 'не уйдёт в минус случайно. Оружие и снаряды казна закупает на ваш рынок '
        + 'на армейские склады. <b>Вооружённость</b> — это запас на складах: он '
        + 'остаётся у армии и решает исход боя. <b>Расход оружия</b> — сколько '
        + 'списывается и докупается за пейдей: это и есть заказ оружейным заводам, '
        + 'он не пропадает даже у полностью вооружённой армии. Толпа без оружия и '
        + 'снарядов почти ничего не стоит в бою.'
        : 'Армией и войной распоряжается лидер государства — «'
        + esc((S.world.country || {}).leader || '—') + '». Здесь видно её состояние.';

    if (!(document.activeElement && document.activeElement.tagName === 'INPUT')) {
        $('aBudget').value = Math.round(a.budget || 0);
        $('aPay').value = (a.soldier_pay || 0).toFixed(0);
    }
    const pay = Number($('aPay').value) || 0;
    const budget = Number($('aBudget').value) || 0;
    $('aHint').innerHTML = pay > 0
        ? `Хватит на <b class="good">${qty(Math.floor(budget / pay))}</b> солдат`
        : '<span class="bad">При нулевом жалованье армии не будет</span>';
    ['armySave', 'mobBtn', 'demobBtn'].forEach(id => { $(id).disabled = !lead; });

    $('mobState').innerHTML = a.mobilization_left > 0
        ? `<div class="bad" style="font-size:13px">Идёт мобилизация: осталось
           ${a.mobilization_left} пейдей(-ев). За прошлый пейдей забрали
           ${qty(a.last_mobilized)} человек — в том числе прямо с заводов.
           Довольство по всей стране снижено, пока мобилизация не кончится.</div>`
        : `<div class="muted" style="font-size:13px">Мобилизации нет. Приказ соберёт
           людей быстро и не глядя на жалованье — в том числе рабочих с ваших же
           заводов, — но всю страну это разозлит.</div>`;

    // ---- войны ----
    $('warList').innerHTML = (d.wars || []).length ? d.wars.map(w => {
        const mine = !!w.my_side;
        const sides = s => s.map(x => esc(x.name)).join(', ') || '—';
        const occ = (w.occupation || []).map(o => `<tr>
            <td>${esc(o.winner)} занимает ${esc(o.loser)}</td>
            <td style="width:140px"><div class="bar" style="margin:0">
                <i style="width:${Math.min(100, o.progress * 100).toFixed(0)}%;
                   background:var(--bad)"></i></div></td>
            <td class="right dim">${pct(o.progress)}</td></tr>`).join('');
        const rep = (w.report || []).map(r => `<tr>
            <td>${esc(r.attacker_name)} ⚔ ${esc(r.defender_name)}</td>
            <td class="right dim">${qty(r.strength_attacker)} : ${qty(r.strength_defender)}</td>
            <td class="right bad">−${qty(r.losses_attacker)} / −${qty(r.losses_defender)}</td>
            <td class="right dim">${qty(r.civilians_attacker + r.civilians_defender)} мирных</td>
            <td class="right dim">${r.buildings_attacker + r.buildings_defender} ур. разрушено</td>
        </tr>`).join('');
        const peace = (w.separate_peace || []).map(p =>
            `${esc(p.a)} — ${esc(p.b)}`).join('; ');
        // Восстание — не война государств: договариваться с мятежниками не с
        // кем, поэтому вместо кнопок мира здесь стоит предупреждение.
        const revolt = w.revolt ? `<div class="bad" style="font-size:13px;margin-bottom:8px">
            <b>Восстание.</b> ${esc(sides(w.defenders))} откололась и требует
            независимости. Мира в такой войне не бывает: область можно только
            вернуть силой — или потерять насовсем.</div>` : '';
        return `<div class="biz${mine ? ' hurt' : ''}">
            <h3>${w.revolt ? 'Восстание' : 'Война'} #${w.id}
                ${mine ? '<span class="tag">вы участвуете</span>' : ''}
                ${w.revolt ? '<span class="tag military">мира не будет</span>' : ''}
                <span class="lvl">с пейдея #${w.started_tick}</span></h3>
            <div class="where">${sides(w.attackers)} <b>против</b> ${sides(w.defenders)}</div>
            ${revolt}
            ${peace ? `<div class="muted" style="font-size:12px;margin-bottom:8px">
                Сепаратный мир: ${peace}</div>` : ''}
            ${occ ? `<div class="tw"><table style="font-size:13px"><tbody>${occ}</tbody></table></div>` : ''}
            ${rep ? `<div class="tw"><table style="font-size:12px;margin-top:8px"><tbody>${rep}</tbody></table></div>`
                : '<div class="muted" style="font-size:12px">Боёв не было: у сторон нет общей границы.</div>'}
            ${mine && lead && w.can_peace ? `<div class="acts" style="margin-top:10px">${
                (w.my_enemies || []).map(e =>
                    `<button class="sm" onclick="offerPeace(${w.id},${e.id})">Мир с ${esc(e.name)}</button>`
                ).join('')}</div>` : ''}
        </div>`;
    }).join('') : '<div class="empty">В мире тихо — ни одной войны.</div>';

    // Своё восстание — событие, которое нельзя не заметить: выносим отдельным
    // предупреждением над списком войн.
    const myRevolts = (d.my_wars || []).filter(w => w.revolt);
    if (myRevolts.length) {
        $('warList').insertAdjacentHTML('afterbegin',
            `<div class="card bad" style="margin-bottom:14px">
                <b>В стране восстание!</b> ${myRevolts.map(w =>
                    esc(w.defenders.map(x => x.name).join(', '))).join('; ')} —
                отколовшиеся области с пейдея #${myRevolts[0].started_tick}.
                Мир с мятежниками невозможен: пока восстание не подавлено,
                области считаются потерянными, а часть армии ушла к ним.
            </div>`);
    }

    // ---- предложения ----
    const inc = (d.incoming || []).map(o => `<tr>
        <td>${o.kind === 'peace' ? 'Мир' : 'Союз'}</td>
        <td>от ${esc(o.from)}</td>
        <td class="dim">пейдей #${o.created_tick}</td>
        <td class="right">${lead ? `<button class="sm primary"
            onclick="acceptOffer(${o.id},'${o.kind}')">Принять</button>
            <button class="sm danger" onclick="declineOffer(${o.id})">Отклонить</button>` : ''}</td>
    </tr>`).join('');
    const out = (d.outgoing || []).map(o => `<tr>
        <td>${o.kind === 'peace' ? 'Мир' : 'Союз'}</td>
        <td>${esc(o.to)}</td>
        <td class="dim">пейдей #${o.created_tick}</td>
        <td class="right dim">ждём ответа</td></tr>`).join('');
    $('offerList').innerHTML = (inc || out)
        ? `<div class="tw"><table style="font-size:13px"><tbody>${inc}${out}</tbody></table></div>`
        : '<div class="empty">Предложений нет.</div>';

    // ---- соседи ----
    $('neighborRows').innerHTML = (d.neighbors || []).length ? d.neighbors.map(n => {
        const rel = n.at_war ? '<span class="bad">война</span>'
            : n.allied ? '<span class="good">союз</span>'
                : '<span class="dim">мир</span>';
        let acts = '';
        if (lead) {
            if (n.at_war) acts = '<span class="dim">воюете</span>';
            else if (n.allied) acts = `<button class="sm danger"
                onclick="breakAlliance(${n.id})">Расторгнуть союз</button>`;
            else acts = `<button class="sm" onclick="offerAlliance(${n.id})">Союз</button>
                <button class="sm danger" onclick="declareWar(${n.id})">Война</button>`;
        }
        return `<tr>
            <td><span class="dot-c" style="background:${n.color}"></span> ${esc(n.name)}
                <span class="dim" style="font-size:12px"
                      title="Через столько пар областей идёт общая граница">${
                    n.borders > 1 ? n.borders + ' участка границы' : ''}</span></td>
            <td class="dim">${esc(n.leader)}</td>
            <td class="right">${qty(n.population)}</td>
            <td class="right">${qty(n.army)}</td>
            <td class="right">${qty(n.strength)}</td>
            <td>${rel}</td>
            <td class="right" style="white-space:nowrap">${acts}</td>
        </tr>`;
    }).join('') : '<tr><td colspan="7" class="empty">Соседей не осталось</td></tr>';
}

const saveArmy = () => act(async () => {
    await api('/api/gov/army', {
        army_budget: Number($('aBudget').value),
        soldier_pay: Number($('aPay').value),
    });
    toast('Военный бюджет утверждён');
});
const mobilize = () => act(async () => {
    if (!confirm('Объявить мобилизацию? Людей заберут приказом, в том числе с ваших '
        + 'заводов, а довольство по всей стране упадёт.')) return;
    const r = await api('/api/gov/mobilize', {});
    toast('Мобилизация объявлена на ' + r.mobilization_left + ' пейдеев');
});
const demobilize = () => act(async () => {
    await api('/api/gov/demobilize', {});
    toast('Мобилизация прекращена');
});
const declareWar = id => act(async () => {
    if (!confirm('Объявить войну? Втянутся союзники обеих сторон, а области могут '
        + 'сменить хозяина.')) return;
    await api('/api/war/declare', { country_id: id });
    toast('Война объявлена');
});
const offerPeace = (warId, id) => act(async () => {
    await api('/api/war/peace/offer', { war_id: warId, country_id: id });
    toast('Предложение мира отправлено');
});
const offerAlliance = id => act(async () => {
    await api('/api/alliance/offer', { country_id: id });
    toast('Предложение союза отправлено');
});
const acceptOffer = (id, kind) => act(async () => {
    await api(kind === 'peace' ? '/api/war/peace/accept' : '/api/alliance/accept',
        { offer_id: id });
    toast(kind === 'peace' ? 'Мир заключён' : 'Союз заключён');
});
const declineOffer = id => act(async () => {
    await api('/api/offers/decline', { offer_id: id });
    toast('Предложение отклонено');
});
const breakAlliance = id => act(async () => {
    await api('/api/alliance/break', { country_id: id });
    toast('Союз расторгнут');
});

/* ------------------------------ карта ------------------------------ */
function renderMap() {
    // Карта рисуется по ОБЛАСТЯМ: захваченная область не исчезает, а меняет
    // цвет владельца — и её видно, чтобы отбить обратно.
    const nodes = S.map || [];
    const svg = $('mapSvg');
    if (!svg) return;
    const byId = {};
    nodes.forEach(n => byId[n.id] = n);
    let out = '';
    const drawn = new Set();
    nodes.forEach(n => {
        (n.neighbors || []).forEach(nb => {
            const k = [n.id, nb].sort().join('-');
            if (drawn.has(k)) return;
            drawn.add(k);
            const m = byId[nb];
            if (!m) return;
            // граница между странами видна толще, чем дорога внутри страны
            const border = m.country_id !== n.country_id;
            out += `<line x1="${n.x}" y1="${n.y}" x2="${m.x}" y2="${m.y}"`
                + ` stroke="${border ? '#3b4657' : '#2a3240'}"`
                + ` stroke-width="${border ? 0.35 : 0.6}"`
                + (border ? '' : ' stroke-dasharray="1 0.7"') + '/>';
        });
    });
    nodes.forEach(n => {
        const fill = n.is_mine ? 'var(--accent)' : n.color;
        const stroke = n.revolt ? '#f85149' : n.is_mine ? '#fff'
            : n.at_war_with_me ? '#f85149' : n.allied_with_me ? '#3fb950' : '#11151c';
        const width = n.revolt ? 0.7 : n.is_mine ? 0.5
            : (n.at_war_with_me || n.allied_with_me) ? 0.6 : 0.2;
        const r = n.capital ? 2.6 : 2.0;
        out += `<circle cx="${n.x}" cy="${n.y}" r="${r}" fill="${fill}"`
            + ` stroke="${stroke}" stroke-width="${width}"`
            + ` style="cursor:pointer" onclick="pickMapNode(${n.id})"/>`
            + (n.revolt ? `<circle cx="${n.x}" cy="${n.y}" r="${r + 1}" fill="none"`
                + ` stroke="#f85149" stroke-width="0.25" stroke-dasharray="0.7 0.7"/>` : '')
            + `<text x="${n.x}" y="${n.y - r - 0.7}" fill="#9aa6b6" font-size="1.9"`
            + ` text-anchor="middle">${esc(n.name)}</text>`;
    });
    svg.innerHTML = out;

    const info = $('mapInfo');
    if (!info) return;
    const sel = (S.mapPick && byId[S.mapPick]) || nodes.find(n => n.is_mine) || nodes[0];
    if (!sel) return;
    const rel = sel.is_mine ? '—'
        : sel.at_war_with_me ? '<span class="bad">война с вами</span>'
        : sel.allied_with_me ? '<span class="good">союз с вами</span>'
        : '<span class="dim">мир</span>';
    info.innerHTML = `<div class="biz${sel.revolt ? ' hurt' : ''}">`
        + `<h3>${esc(sel.name)} ${sel.capital ? '<span class="tag">столица</span>' : ''}`
        + `${sel.is_mine ? '<span class="tag">ваша страна</span>' : ''}`
        + `<span class="lvl">${esc(sel.country)}</span></h3>`
        + `<dl><dt>Население</dt><dd>${qty(sel.population)}</dd>`
        + `<dt>Довольство</dt><dd class="${sel.satisfaction < .4 ? 'bad'
            : sel.satisfaction < .55 ? 'warn' : 'good'}">${pct(sel.satisfaction)}</dd>`
        + `<dt>Уровень жизни</dt><dd>${sel.living_standard.toFixed(2)}</dd>`
        + `<dt>Озлобление</dt><dd class="${sel.revolt ? 'bad' : 'dim'}">${
            sel.revolt ? 'БУНТ' : pct(sel.unrest)}</dd>`
        + `<dt>Всего областей у страны</dt><dd>${sel.regions}</dd>`
        + `<dt>Лидер</dt><dd>${esc(sel.leader)}</dd>`
        + `<dt>Армия страны</dt><dd>${qty(sel.army)}</dd>`
        + `<dt>Отношения</dt><dd>${rel}</dd>`
        + `<dt>Казна</dt><dd>${money(sel.treasury)} ₡</dd>`
        + `<dt>Инвестиции</dt><dd>${sel.foreign_investment_open
            ? '<span class="good">открыты</span>'
            : '<span class="dim">закрыты</span>'}</dd></dl>`
        + (sel.is_mine ? `<div class="acts"><button class="sm"
            onclick="pickRegion(${sel.id})">Смотреть рынок области</button></div>` : '')
        + `</div>`;
}

function pickMapNode(id) { S.mapPick = id; renderMap(); }

/* ------------------------------ рейтинг ------------------------------ */
function renderTop() {
    api('/api/leaderboard').then(r => {
        $('topRows').innerHTML = r.players.length ? r.players.map((p, i) => `<tr>
            <td class="dim">${i + 1}</td>
            <td>${esc(p.username)}${p.is_state ? ' <span class="tag">государство</span>' : ''}${
                p.username === S.me.username ? ' <span class="tag">вы</span>' : ''}</td>
            <td class="dim">${esc(p.country)}</td>
            <td class="right">${money(p.net_worth)} ₡</td>
            <td class="right dim">${money(p.cash)} ₡</td>
            <td class="right">${p.buildings}</td>
            <td class="right">${p.levels}</td>
            <td class="right">${qty(p.employees)}</td>
            <td class="right ${p.profit >= 0 ? 'good' : 'bad'}">${
                (p.profit >= 0 ? '+' : '') + money(p.profit)} ₡</td>
        </tr>`).join('') : '<tr><td colspan="8" class="empty">Пока никого</td></tr>';
    }).catch(() => {});
}

/* ==========================================================================
   ЧАТ
   Три канала в одном окне: мир, своё государство и личная переписка. Окно
   свёрнуто в кружок и живёт поверх всех разделов — переписка не должна
   заставлять уходить с рынка или из строительства.
   ========================================================================== */
const CHAT_SEEN_KEY = 'simpolec.chatseen';

function loadSeen() {
    try { return JSON.parse(localStorage.getItem(CHAT_SEEN_KEY)) || {}; }
    catch (e) { return {}; }
}
function saveSeen() {
    try { localStorage.setItem(CHAT_SEEN_KEY, JSON.stringify(S.chatSeen)); }
    catch (e) {}
}

const chatTime = ts => new Date(ts * 1000)
    .toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });

/** Ключ «до какого сообщения дочитано». У личных он свой на каждого
 *  собеседника: непрочитанное от одного не должно гаситься чтением другого. */
const seenKey = (ch, peer) => ch === 'private' ? 'p' + (peer || 0) : ch;

/** Сообщения открытой вкладки. Личные приходят одним списком — переписку с
 *  выбранным собеседником выбираем здесь, чтобы не дёргать сервер на каждое
 *  переключение. */
function chatMessages() {
    const c = S.chat;
    if (!c) return [];
    if (S.chatCh !== 'private') return c[S.chatCh] || [];
    if (!S.chatPeer) return [];
    return (c.private || []).filter(
        m => m.from_id === S.chatPeer || m.to_id === S.chatPeer);
}

/** Сколько нового в каждом канале и от каждого собеседника.
 *  Своё же сообщение непрочитанным не считается. */
function chatUnread() {
    const c = S.chat;
    const out = { world: 0, country: 0, private: 0, peers: {} };
    if (!c) return out;
    const fresh = (list, key) => (list || []).filter(
        m => m.id > (S.chatSeen[key] || 0) && m.from_id !== c.me).length;
    out.world = fresh(c.world, 'world');
    out.country = fresh(c.country, 'country');
    (c.private || []).forEach(m => {
        if (m.from_id === c.me) return;
        if (m.id <= (S.chatSeen[seenKey('private', m.from_id)] || 0)) return;
        out.peers[m.from_id] = (out.peers[m.from_id] || 0) + 1;
        out.private++;
    });
    return out;
}

/** То, что игрок сейчас видит, считается прочитанным. */
function markSeen() {
    if (!S.chatOpen || !S.chat) return;
    const list = chatMessages();
    if (!list.length) return;
    const key = seenKey(S.chatCh, S.chatPeer);
    const last = list[list.length - 1].id;
    if (last > (S.chatSeen[key] || 0)) { S.chatSeen[key] = last; saveSeen(); }
}

function badge(el, n) {
    if (!el) return;
    el.hidden = !n;
    el.textContent = n > 99 ? '99+' : n;
}

function renderChat() {
    const c = S.chat;
    const box = $('chat');
    if (!c) { box.classList.add('hidden'); return; }
    box.classList.toggle('hidden', !S.chatOpen);

    // Отмечаем прочитанным ДО подсчёта значков: иначе на открытой вкладке
    // счётчик мигал бы на каждом опросе.
    markSeen();
    const u = chatUnread();
    badge($('chatBadge'), u.world + u.country + u.private);
    ['world', 'country', 'private'].forEach(ch => {
        badge(document.querySelector(`#chat .badge[data-badge="${ch}"]`), u[ch]);
        const tab = document.querySelector(`#chat .ctabs button[data-ch="${ch}"]`);
        if (tab) tab.classList.toggle('on', ch === S.chatCh);
    });
    if (!S.chatOpen) return;

    // --- список собеседников: видно, от кого пришло новое ---
    const peerBox = $('chatPeerBox');
    peerBox.hidden = S.chatCh !== 'private';
    if (S.chatCh === 'private') {
        const sel = $('chatPeer');
        const opts = ['<option value="0">— выберите собеседника —</option>'].concat(
            (c.contacts || []).map(p => {
                const n = u.peers[p.id] || 0;
                return `<option value="${p.id}">${esc(p.username)} — ${esc(p.country)}`
                    + (p.is_leader ? ' (лидер)' : '') + (n ? ` • ${n}` : '') + '</option>';
            }));
        const html = opts.join('');
        if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; }
        sel.value = String(S.chatPeer || 0);
    }

    // --- лента ---
    const list = chatMessages();
    const log = $('chatLog');
    // Если игрок отмотал ленту вверх, читая старое, — не дёргать его вниз.
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.innerHTML = list.length ? list.map(m => {
        const mine = m.from_id === c.me;
        const to = (S.chatCh === 'private' && mine && m.to)
            ? `<span class="to">→ ${esc(m.to)}</span>` : '';
        const del = c.is_admin
            ? `<button class="del" title="Удалить сообщение" onclick="admDeleteMsg(${m.id})">✕</button>` : '';
        return `<div class="msg${mine ? ' mine' : ''}">
            <div class="who"><b>${esc(m.from)}</b>${to}${del}<span class="t">${chatTime(m.ts)}</span></div>
            <div class="body">${esc(m.text)}</div></div>`;
    }).join('') : `<div class="empty">${S.chatCh === 'private' && !S.chatPeer
        ? 'Выберите, кому написать' : 'Пока тихо'}</div>`;
    if (atBottom || S.chatJump) { log.scrollTop = log.scrollHeight; S.chatJump = false; }

    // --- подпись под полем ввода ---
    const note = $('chatNote'), input = $('chatInput');
    let text = '', bad = false;
    if (c.muted) {
        bad = true;
        text = c.mute_forever ? 'Вам закрыт доступ к чату'
            : 'Вы лишены слова до ' + chatTime(c.mute_until);
        if (c.mute_reason) text += ' — ' + c.mute_reason;
    } else if (S.chatCh === 'world') {
        text = 'Слышит весь мир';
    } else if (S.chatCh === 'country') {
        text = 'Слышат только граждане: ' + c.country_name;
    } else {
        const peer = (c.contacts || []).find(p => p.id === S.chatPeer);
        text = peer ? 'Личное сообщение: ' + peer.username : 'Собеседник не выбран';
    }
    note.textContent = text;
    note.className = 'cnote' + (bad ? ' bad' : '');
    input.disabled = c.muted || (S.chatCh === 'private' && !S.chatPeer);
}

async function loadChat() {
    if (!S.token) return;
    try {
        S.chat = await api('/api/chat');
        renderChat();
    } catch (e) {
        // Чат — дело десятое: молчащий сервер не должен ронять игру.
        console.error(e);
    }
}

function toggleChat() {
    S.chatOpen = !S.chatOpen;
    S.chatJump = true;
    // Первый опрос мог не дойти — иначе кружок нажимался бы вхолостую.
    if (S.chatOpen && !S.chat) { loadChat(); return; }
    renderChat();
    if (S.chatOpen) $('chatInput').focus();
}

function pickChat(ch) {
    S.chatCh = ch;
    S.chatJump = true;
    renderChat();
}

function pickPeer(id) {
    S.chatPeer = Number(id) || 0;
    S.chatJump = true;
    renderChat();
}

async function sendChat() {
    const el = $('chatInput');
    const text = el.value.trim();
    if (!text) return;
    if (S.chatCh === 'private' && !S.chatPeer) {
        toast('Выберите собеседника', true);
        return;
    }
    el.value = '';
    try {
        await api('/api/chat/send',
                  { channel: S.chatCh, text, to_id: S.chatPeer });
        S.chatJump = true;
        await loadChat();
    } catch (e) {
        el.value = text;      // написанное не теряем
        toast(e.message, true);
    }
}

/* ==========================================================================
   АДМИНКА
   ========================================================================== */
async function loadAdmin() {
    if (!S.me || !S.me.is_admin) return;
    try {
        S.admin = await api('/api/admin/state');
        renderAdmin();
    } catch (e) {
        console.error(e);
    }
}

/** Общая обёртка админских действий: подтянуть админку и обновить всю игру —
 *  правки админа меняют мир, а не только эту страницу. */
async function admAct(fn) {
    try {
        await fn();
        await loadAdmin();
        await refresh(true);
    } catch (e) {
        toast(e.message, true);
    }
}

const admNum = (id, fallback) => {
    const v = Number($(id).value);
    return isFinite(v) ? v : fallback;
};

function muteLabel(p, now) {
    if (p.mute_forever) return '<span class="bad">навсегда</span>';
    if (p.mute_until > now) {
        return `<span class="warn">до ${chatTime(p.mute_until)}</span>`;
    }
    return '<span class="dim">говорит</span>';
}

function renderAdmin() {
    const a = S.admin;
    if (!a) return;
    $('admTick').textContent = '#' + a.tick;
    $('admAuto').textContent = a.auto_tick ? 'идёт' : 'остановлен';
    $('admAuto').className = 'v ' + (a.auto_tick ? 'good' : 'warn');
    $('admLen').textContent = a.tick_seconds + ' с';
    $('admLeft').textContent = a.auto_tick ? a.seconds_left + ' с' : '—';
    $('admAutoBtn').textContent = a.auto_tick ? 'Остановить время' : 'Запустить время';
    if (document.activeElement !== $('admSeconds')) $('admSeconds').value = a.tick_seconds;

    // --- игроки ---
    const humans = a.players.filter(p => !p.is_state);
    $('admPlayerRows').innerHTML = humans.length ? humans.map(p => {
        const tags = (p.is_admin ? ' <span class="tag">админ</span>' : '')
                   + (p.is_leader ? ' <span class="tag">лидер</span>' : '');
        return `<tr>
            <td>${esc(p.username)}${tags}</td>
            <td>${esc(p.country)}</td>
            <td class="right">${money(p.cash)} ₡</td>
            <td class="right">${money(p.net_worth)} ₡</td>
            <td class="right">${p.buildings}</td>
            <td>${p.bankrupt ? '<span class="bad">банкрот</span>'
                             : '<span class="good">в деле</span>'}</td>
            <td>${muteLabel(p, a.now)}</td>
            <td><div class="acts">
                <button class="sm" onclick="admMute(${p.id},false)">Немота</button>
                <button class="sm danger" onclick="admMute(${p.id},true)">Навсегда</button>
                <button class="sm" onclick="admUnmute(${p.id})">Снять</button>
                <button class="sm" onclick="admCash(${p.id})">Деньги</button>
                <button class="sm" onclick="admBankrupt(${p.id},${!p.bankrupt})">${
                    p.bankrupt ? 'Вернуть в дело' : 'В банкроты'}</button>
                <button class="sm" onclick="admGrant(${p.id},${!p.admin_flag})">${
                    p.admin_flag ? 'Снять админа' : 'В админы'}</button>
            </div></td></tr>`;
    }).join('') : '<tr><td colspan="8" class="empty">Живых игроков ещё нет</td></tr>';

    // --- государства ---
    $('admCountryRows').innerHTML = a.countries.map(c => {
        const citizens = humans.filter(p => p.country_id === c.id);
        const opts = ['<option value="0">AI (без лидера)</option>'].concat(
            citizens.map(p => `<option value="${p.id}"${
                p.id === c.leader_id ? ' selected' : ''}>${esc(p.username)}</option>`));
        return `<tr${c.alive ? '' : ' class="dim"'}>
            <td><i class="dot-c" style="background:${esc(c.color)}"></i>${esc(c.name)}${
                c.at_war ? ' <span class="tag military">война</span>' : ''}</td>
            <td class="right">${qty(c.population)}</td>
            <td class="right">${c.regions}</td>
            <td class="right">${money(c.treasury)} ₡</td>
            <td class="right">${money(c.gdp)} ₡</td>
            <td><select onchange="admLeader(${c.id},this.value)">${opts.join('')}</select></td>
            <td><div class="acts">
                <button class="sm" onclick="admTreasury(${c.id})">Внести в казну</button>
            </div></td></tr>`;
    }).join('');
}

const admRunTicks = () => admAct(async () => {
    const count = Math.max(1, Math.min(50, Math.round(admNum('admCount', 1))));
    const r = await api('/api/admin/tick', { count });
    toast('Проведено пейдеев: ' + r.ticks + ' (сейчас #' + r.tick + ')');
});

const admSaveTime = () => admAct(async () => {
    await api('/api/admin/time', { tick_seconds: Math.round(admNum('admSeconds', 900)) });
    toast('Длина пейдея изменена');
});

const admToggleAuto = () => admAct(async () => {
    const r = await api('/api/admin/time', { auto_tick: !S.admin.auto_tick });
    toast(r.auto_tick ? 'Время идёт' : 'Время остановлено');
});

const admMute = (id, forever) => admAct(async () => {
    await api('/api/admin/mute', {
        player_id: id, forever,
        minutes: Math.max(1, admNum('admMuteMin', 15)),
        reason: $('admMuteReason').value,
    });
    toast(forever ? 'Игрок лишён слова навсегда' : 'Игрок лишён слова');
});

const admUnmute = id => admAct(async () => {
    await api('/api/admin/unmute', { player_id: id });
    toast('Слово возвращено');
});

const admCash = id => admAct(async () => {
    const amount = admNum('admAmount', 0);
    if (!amount) throw new Error('Укажите сумму');
    const r = await api('/api/admin/cash', { player_id: id, amount });
    toast('Касса игрока: ' + money(r.cash) + ' ₡');
});

const admTreasury = id => admAct(async () => {
    const amount = admNum('admTreasuryAmount', 0);
    if (!amount) throw new Error('Укажите сумму');
    const r = await api('/api/admin/treasury', { country_id: id, amount });
    toast('Казна: ' + money(r.treasury) + ' ₡');
});

const admBankrupt = (id, value) => admAct(async () => {
    await api('/api/admin/bankrupt', { player_id: id, value });
    toast(value ? 'Дело объявлено банкротом' : 'Дело возвращено в строй');
});

const admGrant = (id, value) => admAct(async () => {
    await api('/api/admin/grant', { player_id: id, value });
    toast(value ? 'Права выданы' : 'Права сняты');
});

const admLeader = (countryId, playerId) => admAct(async () => {
    const r = await api('/api/admin/leader',
                        { country_id: countryId, player_id: Number(playerId) || 0 });
    toast(r.leader ? 'Новый лидер: ' + r.leader : 'Страна возвращена под AI');
});

const admDeleteMsg = id => admAct(async () => {
    await api('/api/admin/chat/delete', { id });
    await loadChat();
});

const admClearChat = channel => admAct(async () => {
    const what = { world: 'мировой чат', country: 'чат вашего государства',
                   private: 'все личные сообщения' }[channel];
    if (!confirm('Стереть ' + what + '? Это необратимо.')) return;
    const r = await api('/api/admin/chat/clear',
                        { channel, country_id: S.me.country_id });
    toast('Удалено сообщений: ' + r.removed);
    await loadChat();
});

/* ------------------------------ навигация ------------------------------ */
/** Узкий экран. Один порог на весь клиент — тот же, что в style.css. */
const narrow = () => window.matchMedia('(max-width: 720px)').matches;
const NAV_MQ = window.matchMedia('(max-width: 720px)');

/** Переключение раздела. Вынесено из обработчика: на телефоне те же разделы
 *  открываются ещё и из листа «Ещё», а дублировать логику ради этого незачем. */
function goPage(page) {
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('on'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('on'));
    const btn = document.querySelector(`nav button[data-page="${page}"]`);
    if (!btn) return;
    btn.classList.add('on');
    S.page = page;
    $('page-' + page).classList.add('on');
    // Раздел из-под «Ещё» подсвечивает саму кнопку «Ещё»: иначе на телефоне
    // не видно, где ты находишься.
    $('navMore').classList.toggle('on', !btn.dataset.pin);
    closeNavSheet();

    if (page === 'market') renderMarket();
    if (page === 'country') renderCountry();
    if (page === 'people') renderPeople();
    if (page === 'top') renderTop();
    if (page === 'map') renderMap();
    if (page === 'gov') renderGov();
    if (page === 'exchange') renderExchange();
    if (page === 'war') { renderWar(); renderAnnexations(); }
    if (page === 'admin') loadAdmin();
    // Страница только что показалась — до этого её svg были нулевой ширины.
    redrawCharts();
}

document.querySelectorAll('nav button[data-page]').forEach(btn => {
    btn.onclick = () => goPage(btn.dataset.page);
});

/* ------------------------- лист «Ещё» (телефон) ------------------------- */
function toggleNavSheet() {
    const sheet = $('navSheet');
    if (sheet.classList.contains('open')) return closeNavSheet();
    // Лист собираем из самого меню, чтобы список разделов жил в одном месте.
    $('navSheetBody').innerHTML = [...document.querySelectorAll('nav button[data-page]')]
        .filter(b => !b.dataset.pin && !b.hidden)
        .map(b => `<button class="${b.dataset.page === S.page ? 'on' : ''}"
            onclick="goPage('${b.dataset.page}')">${esc(b.textContent.trim())}</button>`)
        .join('');
    sheet.classList.add('open');
}

/** Закрыть лист. Клик по подложке закрывает, клик по кнопке внутри — нет. */
function closeNavSheet(e) {
    if (e && e.target !== $('navSheet')) return;
    $('navSheet').classList.remove('open');
}

/* ---------------------- сворачиваемые подразделы ---------------------- */
/* На телефоне раздел открывается со свёрнутыми карточками, кроме помеченной
   data-main, — иначе до нужной таблицы приходится мотать пол-экрана. На ПК
   всё развёрнуто. Что игрок открыл или закрыл руками, помним и уважаем. */
const UI_KEY = 'simpolec.sections';

function loadSections() {
    try { return JSON.parse(localStorage.getItem(UI_KEY)) || {}; }
    catch (e) { return {}; }
}

function applySections() {
    const saved = loadSections();
    const phone = narrow();
    document.querySelectorAll('details.card[data-sec]').forEach(d => {
        const key = d.dataset.sec;
        d.open = key in saved ? saved[key]
            : (!phone || d.hasAttribute('data-main'));
    });
}

// Событие toggle не всплывает — слушаем на фазе перехвата.
document.addEventListener('toggle', e => {
    const d = e.target;
    if (!(d instanceof HTMLDetailsElement)) return;
    if (d.classList.contains('card') && d.dataset.sec) {
        const saved = loadSections();
        saved[d.dataset.sec] = d.open;
        try { localStorage.setItem(UI_KEY, JSON.stringify(saved)); } catch (err) {}
    }
    // Карточку раскрыли — только теперь у её svg появилась настоящая ширина.
    if (d.open && d.querySelector('.chart')) redrawCharts();
}, true);

// Смена ширины экрана меняет умолчания, но не затирает выбор игрока.
NAV_MQ.addEventListener('change', () => {
    closeNavSheet();
    applySections();
    redrawCharts();
});

['u', 'p'].forEach(id => $(id).addEventListener('keydown', e => {
    if (e.key === 'Enter') doAuth('login');
}));

let resizeTimer = null;
window.addEventListener('resize', () => {
    if (!S.world) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(redrawCharts, 150);
});

// Обработчики висят в разметке на onclick, поэтому кладём их в window явно.
Object.assign(window, {
    doAuth, logout, showAuth, pickGood, savePolicy, pickMapNode, pickRegion,
    voteFor, setWage, setWageAll, upgrade, toggleBiz, demolish, buildBiz, repairBiz,
    pickXGood, saveArmy, mobilize, demobilize, declareWar, offerPeace,
    offerAlliance, acceptOffer, declineOffer, breakAlliance,
    voteConfidence, giveSubsidy, decideAnnex, endBankruptcy,
    pickSector, renderBuild,
    goPage, toggleNavSheet, closeNavSheet,
    toggleChat, pickChat, pickPeer, sendChat,
    admRunTicks, admSaveTime, admToggleAuto, admMute, admUnmute, admCash,
    admTreasury, admBankrupt, admGrant, admLeader, admDeleteMsg, admClearChat,
});

applySections();
S.chatSeen = loadSeen();

// Enter отправляет реплику — в чате это привычнее кнопки.
$('chatInput').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});

setInterval(tickClock, 1000);
setInterval(() => { if (S.token) refresh(false); }, 5000);
// Чат опрашивается отдельно от игры: он должен обновляться живо, а тянуть
// ради этого все двенадцать витрин мира незачем.
setInterval(() => { if (S.token) loadChat(); }, 5000);
// Список государств готовим всегда, ещё до проверки токена: если токен
// окажется негодным, форма входа появится уже с заполненным списком.
loadCountriesForRegister();
if (S.token) start();
else showAuth();
