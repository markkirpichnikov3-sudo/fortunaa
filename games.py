"""Игровой движок Fortuna: provably-fair RNG и математика 17 игр.

Единственный рычаг «шансов» — преимущество казино edge (%) по каждой игре.
Все множители считаются из edge так, что RTP = 100% − edge, а таблицы выплат,
которые видит игрок в Mini App, берутся из этих же функций (честно и прозрачно).
"""
import hashlib, hmac, math
from functools import lru_cache
from math import comb


class Err(Exception):
    """Ошибка для игрока (показывается в Mini App)."""


GAMES = ['dice', 'coin', 'slots', 'wheel', 'limbo', 'crash', 'plinko', 'keno', 'roulette', 'mines',
         'rocket', 'darts', 'cups', 'scratch', 'hilo', 'pen', 'duel']
GN = {'dice': '🎲 Dice', 'coin': '🪙 Монета', 'slots': '🎰 Слоты', 'wheel': '🎡 Колесо', 'limbo': '📈 Limbo',
      'crash': '💥 Crash', 'plinko': '🔺 Plinko', 'keno': '🎱 Keno', 'roulette': '🎯 Рулетка', 'mines': '💣 Mines',
      'rocket': '🚀 Ракета', 'darts': '🎯 Дартс', 'cups': '🥤 Напёрстки', 'scratch': '🎟 Скретч',
      'hilo': '🃏 Выше/ниже', 'pen': '⚽ Пенальти', 'duel': '⚔️ Дуэль'}

WHEEL = [0, 2, 0, 1.5, 0, 3, 0, 1.5, 0, 2, 0, 10]
SLOTS = [(.004, '7️⃣', 50), (.012, '💎', 15), (.03, '🔔', 6), (.06, '🍋', 3), (.12, '🍒', 1.5)]
SYMS = ['7️⃣', '💎', '🔔', '🍋', '🍒', '⭐']
PL = {'low': [10, 3, 1.6, 1.4, 1.1, 1, .5, 1, 1.1, 1.4, 1.6, 3, 10],
      'mid': [33, 11, 4, 2, 1.1, .6, .3, .6, 1.1, 2, 4, 11, 33],
      'high': [170, 24, 8.1, 2, .7, .2, .2, .2, .7, 2, 8.1, 24, 170]}
RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
DARTS = [(.02, 20), (.08, 5), (.2, 1.5), (.7, 0)]
SCRATCH = [(.01, 40), (.04, 8), (.12, 2.5), (.83, 0)]
OUTSIDE = {'red', 'black', 'even', 'odd', 'low', 'high', 'd1', 'd2', 'd3'}


class Rng:
    """Provably fair: HMAC-SHA256(server_seed, "client_seed:nonce:counter") -> поток чисел [0,1)."""

    def __init__(s, server, client, nonce):
        s.k, s.m, s.c, s.buf = server.encode(), f'{client}:{nonce}', 0, []

    def f(s):
        if not s.buf:
            h = hmac.new(s.k, f'{s.m}:{s.c}'.encode(), hashlib.sha256).digest()
            s.c += 1
            s.buf = [int.from_bytes(h[i:i + 4], 'big') / 2 ** 32 for i in range(0, 32, 4)][::-1]
        return s.buf.pop()

    def i(s, n):
        return int(s.f() * n)

    def shuffle(s, l):
        l = list(l)
        for i in range(len(l) - 1, 0, -1):
            j = s.i(i + 1)
            l[i], l[j] = l[j], l[i]
        return l


sha = lambda x: hashlib.sha256(x.encode()).hexdigest()
r2 = lambda x: math.floor(x * 100 + 1e-9) / 100
f2 = lambda x: f'{float(x):.2f}'


# ---------- таблицы множителей (зависят от edge, e = edge/100) ----------
def sc(g, e):
    return (1 - e) / (sum(WHEEL) / len(WHEEL) if g == 'wheel' else sum(p * m for p, _, m in SLOTS))


def segs(e):
    k = sc('wheel', e)
    return [round(m * k, 2) for m in WHEEL]


def paytable(e):
    return [[s, round(m * sc('slots', e), 2)] for _, s, m in SLOTS]


def plm(risk, e):
    t = PL[risk]
    b = sum(comb(12, k) / 4096 * m for k, m in enumerate(t))
    return [round(m * (1 - e) / b, 2) for m in t]


KENO_CAP = 1000  # потолок множителя Keno; недостающий RTP честно перераспределяется на остальные уровни


@lru_cache(None)
def keno_table(k, e):
    f = max(1, (k + 1) // 2)
    P = {h: comb(k, h) * comb(40 - k, 10 - h) / comb(40, 10) for h in range(f, k + 1)}
    capped, free, left = {}, set(P), 1 - e
    while free:
        s = left / len(free)
        over = [h for h in free if s / P[h] > KENO_CAP]
        if not over:
            break
        for h in over:
            capped[h] = KENO_CAP
            left -= KENO_CAP * P[h]
            free.discard(h)
    t = {h: round(s / P[h], 2) for h in free}
    t.update(capped)
    return t


def kenoM(k, h, e):
    return keno_table(k, round(e, 6)).get(h, 0)


def mm(n, m, e):  # множитель Mines после n открытых клеток при m минах
    x = 1.0
    for i in range(n):
        x *= (25 - i) / (25 - m - i)
    return round(x * (1 - e), 4)


def T(t, r, e):  # таблица исходов [(вероятность, множитель)] -> (индекс, множители)
    k = (1 - e) / sum(p * m for p, m in t)
    ms = [round(m * k, 2) for _, m in t]
    a, i = 0, 0
    while i < len(t) - 1:
        a += t[i][0]
        if r < a:
            break
        i += 1
    return i, ms


def crash_point(seed, e):  # для Rocket: точка вылета из хэша раунда
    r = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], 'big') / 2 ** 64
    return max(1.0, math.floor(min(1e4, (1 - e) / max(1 - r, 1e-9)) * 100) / 100)


rkm = lambda t: math.floor(math.exp(.09 * t) * 100) / 100  # множитель ракеты в момент t (сек)


def pts(CFG):  # подписи с выплатами под картами игр — всегда соответствуют текущему edge
    e = lambda g: CFG[g]['edge'] / 100
    f = lambda x: f'{x:.2f}'.rstrip('0').rstrip('.')
    d, s = T(DARTS, 0, e('darts'))[1], T(SCRATCH, 0, e('scratch'))[1]
    return {'darts': f'Яблочко ×{f(d[0])} · кольцо ×{f(d[1])} · поле ×{f(d[2])}',
            'cups': f"Выберите стакан. Угадали, где шарик, — ×{f(3 * (1 - e('cups')))}",
            'scratch': f'Три одинаковых: 💎 ×{f(s[0])} · 🔔 ×{f(s[1])} · 🍒 ×{f(s[2])}',
            'pen': f"Вратарь прыгает в случайный угол · гол ×{f(1.5 * (1 - e('pen')))}",
            'duel': f"Побеждает большая сумма · при равенстве ставка проигрывает · ×{f((1 - e('duel')) / .44367)}"}


# ---------- игры мгновенного результата: (rng, pick, e) -> (mult, info) ----------
def _int(x, lo, hi, msg='Неверный выбор'):
    try:
        v = int(x)
    except (TypeError, ValueError):
        raise Err(msg)
    if not lo <= v <= hi:
        raise Err(msg)
    return v


def g_dice(R, p, e):
    try:
        ch = float(p.get('chance', 50))
    except (TypeError, ValueError):
        raise Err('Неверный шанс')
    if not 2 <= ch <= 95:
        raise Err('Шанс 2–95%')
    roll = R.f() * 100
    w = roll >= 100 - ch if p.get('over') else roll < ch
    return ((1 - e) * 100 / ch if w else 0), {'roll': round(roll, 2)}


def g_coin(R, p, e):
    res = 0 if R.f() < .5 else 1
    return (2 * (1 - e) if res == (1 if p.get('side') else 0) else 0), {'res': res}


def g_wheel(R, p, e):
    s = segs(e)
    i = R.i(len(s))
    return s[i], {'idx': i}


def g_limbo(R, p, e):
    try:
        t = float(p.get('target', 2))
    except (TypeError, ValueError):
        raise Err('Неверная цель')
    if not 1.01 <= t <= 10000:
        raise Err('Цель 1.01–10000×')
    m = math.floor(min(1e4, (1 - e) / max(1 - R.f(), 1e-9)) * 100) / 100
    return (t if m >= t else 0), {'crash': max(m, 1)}


def g_plinko(R, p, e):
    risk = p.get('risk')
    if risk not in PL:
        raise Err('Неверный риск')
    path = [0 if R.f() < .5 else 1 for _ in range(12)]
    k = sum(path)
    return plm(risk, e)[k], {'path': path, 'slot': k}


def g_keno(R, p, e):
    nums = p.get('nums')
    if not isinstance(nums, list) or not 1 <= len(nums) <= 10:
        raise Err('Выберите 1–10 чисел')
    nums = sorted({_int(n, 1, 40) for n in nums})
    d = sorted(R.shuffle(range(1, 41))[:10])
    hits = len(set(nums) & set(d))
    return kenoM(len(nums), hits, e), {'drawn': d, 'hits': hits}


def g_roulette(R, p, e):
    b = str(p.get('bet'))
    if b[0] == 'n':
        t = _int(b[1:], 0, 36)
    elif b not in OUTSIDE:
        raise Err('Неверная ставка')
    n = R.i(37)
    if b[0] == 'n':
        w, base = n == t, 36
    elif not n:
        w, base = False, 2
    else:
        w, base = {'red': (n in RED, 2), 'black': (n not in RED, 2), 'even': (n % 2 == 0, 2), 'odd': (n % 2 == 1, 2),
                   'low': (n <= 18, 2), 'high': (n > 18, 2), 'd1': (n <= 12, 3), 'd2': (12 < n <= 24, 3),
                   'd3': (n > 24, 3)}[b]
    return (base * (1 - e) * 37 / 36 if w else 0), {'n': n}


def g_darts(R, p, e):
    i, ms = T(DARTS, R.f(), e)
    return ms[i], {'ring': i}


def g_cups(R, p, e):
    cup = _int(p.get('cup'), 0, 2)
    q = R.i(3)
    return (3 * (1 - e) if q == cup else 0), {'b': q}


def g_scratch(R, p, e):
    S = ['💎', '🔔', '🍒', '🍋', '⭐', '🍀']
    i, ms = T(SCRATCH, R.f(), e)
    D = R.shuffle([x for s in S for x in (s, s)])
    cells = [S[i]] * 3 + [x for x in D if x != S[i]][:6] if i < 3 else D[:9]
    return ms[i], {'cells': R.shuffle(cells)}


def g_hilo(R, p, e):
    h, hi = _int(p.get('base'), 1, 13), bool(p.get('hi'))
    w = (13 - h) / 13 if hi else (h - 1) / 13
    if w <= 0:
        raise Err('С этой картой так ставить нельзя')
    n = R.i(13) + 1
    return ((1 - e) / w if (n > h if hi else n < h) else 0), {'n': n}


def g_pen(R, p, e):
    z = _int(p.get('z'), 0, 2)
    k = R.i(3)
    return (1.5 * (1 - e) if k != z else 0), {'k': k, 'z': z}


def g_duel(R, p, e):
    y, z = [R.i(6) + 1, R.i(6) + 1], [R.i(6) + 1, R.i(6) + 1]
    return ((1 - e) / .44367 if sum(y) > sum(z) else 0), {'y': y, 'b': z}


def g_slots(R, p, e):
    r, acc, sym, mult = R.f(), 0, None, 0
    for pr, s, m in SLOTS:
        acc += pr
        if r < acc:
            sym, mult = s, round(m * sc('slots', e), 2)
            break
    if sym:
        reels = [sym] * 3
    else:
        reels = [SYMS[R.i(6)] for _ in range(3)]
        while len(set(reels)) == 1:
            reels = [SYMS[R.i(6)] for _ in range(3)]
    return mult, {'reels': reels}


ENGINE = {'dice': g_dice, 'coin': g_coin, 'slots': g_slots, 'wheel': g_wheel, 'limbo': g_limbo, 'crash': g_limbo,
          'plinko': g_plinko, 'keno': g_keno, 'roulette': g_roulette, 'darts': g_darts, 'cups': g_cups,
          'scratch': g_scratch, 'hilo': g_hilo, 'pen': g_pen, 'duel': g_duel}
