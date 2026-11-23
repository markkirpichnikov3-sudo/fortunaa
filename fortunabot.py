"""Fortuna: Telegram-бот + API для Mini App + админ-панель.  Запуск: python bot.py"""
import asyncio, hmac, json, logging, os, secrets, time
from collections import defaultdict
from urllib.parse import parse_qsl

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton as Btn, InlineKeyboardMarkup,
                           LabeledPrice, MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo)
from dotenv import load_dotenv

from games import *

load_dotenv()
TOKEN = os.environ['BOT_TOKEN']
ADMINS = {int(x) for x in os.getenv('ADMIN_IDS', '').replace(' ', '').split(',') if x}
URL = os.environ['WEBAPP_URL'].rstrip('/')  # https://ваш-домен
PORT, DBF = int(os.getenv('PORT', 8080)), os.getenv('DB_PATH', 'fortuna.db')
SUPPORT = os.getenv('SUPPORT', '@support')
MAX_EDGE = 20.0  # потолок преимущества казино в % (защита от случайных «нулевых» RTP)
BASE = os.path.dirname(os.path.abspath(__file__))
bot, dp, r = Bot(TOKEN), Dispatcher(), Router()
dp.include_router(r)
now = lambda: int(time.time())

# ============================ БД и настройки ============================
db = None
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT,bal REAL DEFAULT 0,banned INT DEFAULT 0,sseed TEXT,
  cseed TEXT DEFAULT 'default',nonce INT DEFAULT 0,wager REAL DEFAULT 0,dep REAL DEFAULT 0,ref INT,ts INT);
CREATE TABLE IF NOT EXISTS tx(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INT,kind TEXT,amt REAL,note TEXT,ts INT);
CREATE TABLE IF NOT EXISTS bets(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INT,game TEXT,amt REAL,pay REAL,ts INT);
CREATE TABLE IF NOT EXISTS pays(cid TEXT PRIMARY KEY,uid INT,stars INT,ts INT);
CREATE TABLE IF NOT EXISTS wds(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INT,amt REAL,st TEXT DEFAULT 'new',ts INT);
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY,amt REAL,uses INT,used INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS pused(code TEXT,uid INT,PRIMARY KEY(code,uid));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT);
"""
CFG = {g: {'on': 1, 'edge': 2.7 if g == 'roulette' else 3.0, 'min': 1.0, 'max': 1000.0} for g in GAMES}
SET = {'maint': 0, 'wager_x': 1.0, 'dep_bonus': 0.0, 'ref_pct': 5.0, 'min_dep': 1, 'max_dep': 10000,
       'min_wd': 50, 'max_win': 100000}
SL = {'maint': 'Тех. работы (1 вкл / 0 выкл)', 'wager_x': 'Отыгрыш (× от депозита)', 'dep_bonus': 'Бонус к депозиту, %',
      'ref_pct': 'Реферал, % от депозитов', 'min_dep': 'Мин. депозит, ⭐', 'max_dep': 'Макс. депозит, ⭐',
      'min_wd': 'Мин. вывод, ⭐', 'max_win': 'Макс. выигрыш за ставку, ⭐'}


async def q(sql, *a, one=False, many=False):
    async with db.execute(sql, a) as c:
        res = await c.fetchone() if one else await c.fetchall() if many else c.rowcount
    await db.commit()
    return res


async def load():
    for k, o in (('cfg', CFG), ('set', SET)):
        row = await q('SELECT v FROM kv WHERE k=?', k, one=True)
        for kk, v in (json.loads(row['v']) if row else {}).items():
            if kk in o:
                o[kk].update(v) if isinstance(v, dict) else o.__setitem__(kk, v)


async def save():
    for k, o in (('cfg', CFG), ('set', SET)):
        await q('INSERT OR REPLACE INTO kv VALUES(?,?)', k, json.dumps(o))


async def ensure(uid, name, ref=None):
    await q('INSERT OR IGNORE INTO users(id,name,sseed,ts,ref) VALUES(?,?,?,?,?)', uid, name[:40],
            secrets.token_hex(16), now(), ref)
    return await q('SELECT * FROM users WHERE id=?', uid, one=True)


async def bal(uid): return (await q('SELECT bal FROM users WHERE id=?', uid, one=True))['bal']
async def addbal(uid, x): await q('UPDATE users SET bal=ROUND(bal+?,2) WHERE id=?', x, uid)
async def rec(uid, g, a, pay): await q('INSERT INTO bets(uid,game,amt,pay,ts) VALUES(?,?,?,?,?)', uid, g, a, pay, now())
async def log(uid, kind, amt, note=''): await q('INSERT INTO tx(uid,kind,amt,note,ts) VALUES(?,?,?,?,?)', uid, kind, amt, note, now())


async def take(uid, a):  # атомарное списание ставки (и зачёт в отыгрыш)
    return await q('UPDATE users SET bal=ROUND(bal-?,2),wager=MAX(0,ROUND(wager-?,2)) WHERE id=? AND bal>=?', a, a, uid, a)


async def notify_admins(text, markup=None):
    for ad in ADMINS:
        try:
            await bot.send_message(ad, text, reply_markup=markup)
        except Exception:
            pass


# ============================ API для Mini App ============================
def auth(init):
    d = dict(parse_qsl(init or '', keep_blank_values=True))
    h = d.pop('hash', None)
    if not h or 'user' not in d:
        return None
    key = hmac.new(b'WebAppData', TOKEN.encode(), 'sha256').digest()
    chk = '\n'.join(f'{k}={v}' for k, v in sorted(d.items()))
    if not hmac.compare_digest(hmac.new(key, chk.encode(), 'sha256').hexdigest(), h):
        return None
    if now() - int(d.get('auth_date', 0)) > 86400:
        return None
    return json.loads(d['user'])


H, LK = {}, defaultdict(asyncio.Lock)
route = lambda name: (lambda f: H.setdefault(name, f) and f)


async def api(req):
    try:
        b = await req.json()
    except Exception:
        b = {}
    u, p = auth(b.get('initData')), req.match_info['p']
    if not u:
        return web.json_response({'error': 'Откройте приложение из Telegram'}, status=401)
    if p not in H:
        return web.json_response({'error': 'Нет такого метода'}, status=404)
    try:
        async with LK[u['id']]:
            row = await ensure(u['id'], u.get('first_name') or u.get('username') or str(u['id']))
            if row['banned']:
                raise Err('Аккаунт заблокирован')
            if SET['maint'] and p != 'me':
                raise Err('Технические работы, зайдите позже')
            return web.json_response(await H[p](u['id'], row, b))
    except Err as e:
        return web.json_response({'error': str(e)}, status=400)
    except Exception:
        logging.exception('api %s', p)
        return web.json_response({'error': 'Ошибка сервера'}, status=500)


def amt(x, c):
    try:
        a = round(float(x), 2)
    except (TypeError, ValueError):
        raise Err('Неверная ставка')
    if not c['on']:
        raise Err('Игра недоступна')
    if not c['min'] <= a <= c['max']:
        raise Err(f"Ставка {c['min']:g}–{c['max']:g} ⭐")
    return a


async def mget(uid):
    row = await q('SELECT v FROM kv WHERE k=?', f'mines:{uid}', one=True)
    return json.loads(row['v']) if row else None


async def mput(uid, st): await q('INSERT OR REPLACE INTO kv VALUES(?,?)', f'mines:{uid}', json.dumps(st))
async def mdel(uid): await q('DELETE FROM kv WHERE k=?', f'mines:{uid}')


@route('me')
async def h_me(uid, u, b):
    st, E = await mget(uid), lambda g: CFG[g]['edge'] / 100
    return {'balance': f2(u['bal']), 'seed_hash': sha(u['sseed']), 'client_seed': u['cseed'], 'nonce': u['nonce'],
            'wager_left': f2(u['wager']), 'cfg': CFG, 'segs': segs(E('wheel')), 'syms': SYMS, 'stars_rate': 1,
            'pts': pts(CFG), 'paytable': paytable(E('slots')), 'plinko': {k: plm(k, E('plinko')) for k in PL},
            'mines': st and {'open': st['open'], 'm': st['m'], 'amount': st['amount'],
                             'mult': mm(len(st['open']), st['m'], st['e'])}}


@route('play')
async def h_play(uid, u, b):
    g = b.get('game')
    if g not in ENGINE:
        raise Err('Нет такой игры')
    c, p = CFG[g], (b.get('pick') if isinstance(b.get('pick'), dict) else {})
    a = amt(b.get('amount'), c)
    if g in ('limbo', 'crash'):
        try:
            t = float(p.get('target', 2))
        except (TypeError, ValueError):
            raise Err('Неверная цель')
        if a * t > SET['max_win']:
            raise Err(f"Макс. выигрыш {SET['max_win']:g} ⭐ — уменьшите ставку или цель")
    mult, info = ENGINE[g](Rng(u['sseed'], u['cseed'], u['nonce']), p, c['edge'] / 100)  # проверка выбора до списания
    if not await take(uid, a):
        raise Err('Недостаточно средств')
    await q('UPDATE users SET nonce=nonce+1 WHERE id=?', uid)
    pay = min(r2(a * mult), SET['max_win'])
    if pay:
        await addbal(uid, pay)
    await rec(uid, g, a, pay)
    n = await q('SELECT bal,nonce FROM users WHERE id=?', uid, one=True)
    return {'win': pay > 0, 'payout': f2(pay), 'mult': f2(mult), 'balance': f2(n['bal']), 'nonce': n['nonce'], 'info': info}


@route('mines/start')
async def h_ms(uid, u, b):
    c = CFG['mines']
    a = amt(b.get('amount'), c)
    try:
        m = int(b.get('mines'))
    except (TypeError, ValueError):
        raise Err('Неверные параметры')
    if not 1 <= m <= 24:
        raise Err('Мин: от 1 до 24')
    if await mget(uid):
        raise Err('Раунд уже идёт')
    if not await take(uid, a):
        raise Err('Недостаточно средств')
    R = Rng(u['sseed'], u['cseed'], u['nonce'])
    await q('UPDATE users SET nonce=nonce+1 WHERE id=?', uid)
    await mput(uid, {'amount': a, 'm': m, 'open': [], 'e': c['edge'] / 100, 'mines': R.shuffle(range(25))[:m]})
    return {'ok': True, 'balance': f2(await bal(uid))}


@route('mines/pick')
async def h_mp(uid, u, b):
    st = await mget(uid)
    try:
        i = int(b.get('i'))
    except (TypeError, ValueError):
        i = -1
    if not st or not 0 <= i < 25 or i in st['open']:
        raise Err('Нельзя открыть')
    if i in st['mines']:
        await mdel(uid)
        await rec(uid, 'mines', st['amount'], 0)
        return {'boom': True, 'mines': st['mines']}
    st['open'].append(i)
    m = mm(len(st['open']), st['m'], st['e'])
    pay = min(r2(st['amount'] * m), SET['max_win'])
    if len(st['open']) == 25 - st['m'] or pay >= SET['max_win']:  # открыто всё безопасное или достигнут лимит выигрыша
        await mdel(uid)
        await addbal(uid, pay)
        await rec(uid, 'mines', st['amount'], pay)
        return {'safe': True, 'done': True, 'mult': m, 'payout': f2(pay), 'balance': f2(await bal(uid)), 'mines': st['mines']}
    await mput(uid, st)
    return {'safe': True, 'mult': m}


@route('mines/cash')
async def h_mc(uid, u, b):
    st = await mget(uid)
    if not st or not st['open']:
        raise Err('Откройте хотя бы одну клетку')
    m = mm(len(st['open']), st['m'], st['e'])
    pay = min(r2(st['amount'] * m), SET['max_win'])
    await mdel(uid)
    await addbal(uid, pay)
    await rec(uid, 'mines', st['amount'], pay)
    return {'payout': f2(pay), 'mult': m, 'balance': f2(await bal(uid)), 'mines': st['mines']}


# ---- Ракета: общие раунды для всех игроков ----
RK = dict(id=0, phase='bet', hash='', seed='', crash=1.0, t_end=0, t0=0, bets={}, hist=[])


async def rk_pay(uid, m):
    v = RK['bets'][uid]
    if v['cashed']:
        return
    v['cashed'] = m  # флаг ставим синхронно: защита от двойной выплаты
    pay = min(r2(v['a'] * m), SET['max_win'])
    await addbal(uid, pay)
    await rec(uid, 'rocket', v['a'], pay)


async def rocket_loop():
    while True:
        try:
            seed = secrets.token_hex(16)
            RK.update(id=RK['id'] + 1, phase='bet', hash=sha(seed), seed='', bets={}, t_end=time.time() + 7,
                      crash=crash_point(seed, CFG['rocket']['edge'] / 100))
            await asyncio.sleep(7)
            RK.update(phase='fly', t0=time.time())
            while True:
                m = rkm(time.time() - RK['t0'])
                for uid, v in list(RK['bets'].items()):
                    if not v['cashed'] and v['auto'] and v['auto'] <= RK['crash'] and m >= v['auto']:
                        await rk_pay(uid, v['auto'])
                if m >= RK['crash']:
                    break
                await asyncio.sleep(.1)
            RK.update(phase='crashed', seed=seed, hist=[RK['crash']] + RK['hist'][:19])
            for uid, v in RK['bets'].items():
                if not v['cashed']:
                    await rec(uid, 'rocket', v['a'], 0)
            await q("DELETE FROM kv WHERE k='rkbets'")
            await asyncio.sleep(4)
        except Exception:
            logging.exception('rocket')
            await asyncio.sleep(2)


@route('rocket/state')
async def h_rs(uid, u, b):
    t, fly = time.time(), RK['phase'] == 'fly'
    feed = [{'u': 'ВЫ' if k == uid else '•••' + str(k)[-3:], 'a': f2(v['a']), 'c': v['cashed']} for k, v in RK['bets'].items()]
    my = RK['bets'].get(uid)
    return {'id': RK['id'], 'phase': RK['phase'], 'hash': RK['hash'], 'history': RK['hist'], 'rate': .09,
            'players': len(feed), 'pool': f2(sum(v['a'] for v in RK['bets'].values())), 'balance': f2(u['bal']),
            'left': max(0, RK['t_end'] - t) if RK['phase'] == 'bet' else 0, 'elapsed': t - RK['t0'] if fly else 0,
            'my': my and {'amount': f2(my['a']), 'cashed': my['cashed'], 'auto': my['auto']}, 'feed': feed[-8:],
            **({'crash': RK['crash'], 'seed': RK['seed']} if RK['phase'] == 'crashed' else {})}


@route('rocket/bet')
async def h_rb(uid, u, b):
    a = amt(b.get('amount'), CFG['rocket'])
    if RK['phase'] != 'bet':
        raise Err('Приём ставок закрыт')
    if uid in RK['bets']:
        raise Err('Ставка уже сделана')
    try:
        au = float(b.get('auto') or 0)
    except (TypeError, ValueError):
        au = 0
    if au and au < 1.01:
        raise Err('Автовывод — от 1.01×')
    if not await take(uid, a):
        raise Err('Недостаточно средств')
    if RK['phase'] != 'bet':  # раунд стартовал, пока списывали
        await addbal(uid, a)
        raise Err('Приём ставок закрыт')
    RK['bets'][uid] = {'a': a, 'auto': au or None, 'cashed': None}
    await q('INSERT OR REPLACE INTO kv VALUES(?,?)', 'rkbets', json.dumps({str(k): v['a'] for k, v in RK['bets'].items()}))
    return {'balance': f2(await bal(uid))}


@route('rocket/cash')
async def h_rc(uid, u, b):
    v = RK['bets'].get(uid)
    if RK['phase'] != 'fly' or not v or v['cashed']:
        raise Err('Сейчас нельзя забрать')
    m = rkm(time.time() - RK['t0'])
    if m >= RK['crash']:
        raise Err('Ракета улетела')
    await rk_pay(uid, m)
    return {'mult': m, 'payout': f2(min(r2(v['a'] * m), SET['max_win'])), 'balance': f2(await bal(uid))}


# ---- деньги: пополнение Stars, заявка на вывод, сиды ----
async def invoice_link(uid, stars):
    if not SET['min_dep'] <= stars <= SET['max_dep']:
        raise Err(f"Сумма от {SET['min_dep']:g} до {SET['max_dep']:g} ⭐")
    return await bot.create_invoice_link(
        title='Пополнение баланса', description=f'Fortuna: пополнение на {stars} ⭐', payload=f'dep:{uid}:{stars}',
        provider_token='', currency='XTR', prices=[LabeledPrice(label='Пополнение', amount=stars)])


@route('deposit')
async def h_dep(uid, u, b):
    try:
        s = int(float(b.get('amount')))
    except (TypeError, ValueError):
        raise Err('Неверная сумма')
    return {'kind': 'stars', 'url': await invoice_link(uid, s), 'stars': s}


@route('withdraw')
async def h_wd(uid, u, b):
    try:
        a = round(float(b.get('amount')), 2)
    except (TypeError, ValueError):
        raise Err('Неверная сумма')
    if a < SET['min_wd']:
        raise Err(f"Минимум {SET['min_wd']:g} ⭐")
    if u['wager'] > 0:
        raise Err(f"Сначала отыграйте ещё {f2(u['wager'])} ⭐")
    if not await q('UPDATE users SET bal=ROUND(bal-?,2) WHERE id=? AND bal>=?', a, uid, a):
        raise Err('Недостаточно средств')
    await q('INSERT INTO wds(uid,amt,ts) VALUES(?,?,?)', uid, a, now())
    wid = (await q('SELECT last_insert_rowid() i', one=True))['i']
    await notify_admins(f"💸 Заявка #{wid}: {u['name']} ({uid}) — {a:g} ⭐",
                        kb([('✅ Выплачено', f'a:wd:ok:{wid}'), ('❌ Отклонить', f'a:wd:no:{wid}')]))
    return {'ok': True}


@route('seed')
async def h_seed(uid, u, b):
    cs = ''.join(ch for ch in str(b.get('client_seed') or u['cseed']) if ch.isalnum() or ch in '-_')[:32] or 'default'
    new = secrets.token_hex(16)
    await q('UPDATE users SET sseed=?,cseed=?,nonce=0 WHERE id=?', new, cs, uid)
    return {'revealed_seed': u['sseed'], 'seed_hash': sha(new)}


# ============================ Бот: клавиатуры и FSM ============================
def btn(t, d): return Btn(text=t, web_app=WebAppInfo(url=d)) if d.startswith('https://') else Btn(text=t, callback_data=d)
def kb(*rows): return InlineKeyboardMarkup(inline_keyboard=[[btn(*x) for x in row] for row in rows])


MENU = lambda: kb([('🎰 Играть', URL)], [('⭐ Пополнить', 'u:dep'), ('👤 Профиль', 'u:me')],
                  [('🎁 Промокод', 'u:pr'), ('👥 Рефералы', 'u:ref')], [('🏆 Топ', 'u:top'), ('🧾 История', 'u:hist')])


class S(StatesGroup):
    val = State(); uid = State(); bc = State(); dep = State(); promo = State()


async def show(x, text, markup=None):
    try:
        if isinstance(x, CallbackQuery):
            await x.message.edit_text(text, reply_markup=markup)
            await x.answer()
        else:
            await x.answer(text, reply_markup=markup)
    except TelegramBadRequest:
        if isinstance(x, CallbackQuery):
            await x.answer()


# ============================ Пользовательская часть ============================
@r.message(CommandStart())
async def start(m: Message, command: CommandObject):
    ref = None
    if command.args and command.args[:4] == 'ref_' and command.args[4:].isdigit():
        ref = int(command.args[4:])
        if ref == m.from_user.id or not await q('SELECT 1 FROM users WHERE id=?', ref, one=True):
            ref = None
    await ensure(m.from_user.id, m.from_user.full_name, ref)
    await m.answer('🎰 Fortuna — 17 игр в одном приложении.\n\nПополнение только в Telegram Stars ⭐.\n'
                   'Только для 18+. Играйте ответственно: ставьте лишь то, что готовы потерять.', reply_markup=MENU())


@r.message(Command('paysupport'))
async def paysupport(m: Message):
    await m.answer(f'Вопросы по платежам и возвратам: {SUPPORT}. Укажите ID платежа из «Настройки → Мои Stars».')


@r.message(Command('promo'))
async def promo_cmd(m: Message, command: CommandObject):
    await ensure(m.from_user.id, m.from_user.full_name)
    await m.answer(await redeem(m.from_user.id, command.args or ''))


async def redeem(uid, code):
    code = code.strip().upper()
    p = await q('SELECT * FROM promos WHERE code=?', code, one=True)
    if not p:
        return '❌ Промокод не найден'
    if not await q('INSERT OR IGNORE INTO pused VALUES(?,?)', code, uid):
        return '❌ Вы уже использовали этот промокод'
    if not await q('UPDATE promos SET used=used+1 WHERE code=? AND used<uses', code):
        await q('DELETE FROM pused WHERE code=? AND uid=?', code, uid)
        return '❌ Промокод закончился'
    await q('UPDATE users SET bal=ROUND(bal+?,2),wager=wager+? WHERE id=?', p['amt'], p['amt'] * SET['wager_x'], uid)
    await log(uid, 'promo', p['amt'], code)
    return f"✅ Начислено {p['amt']:g} ⭐ (нужно отыграть ×{SET['wager_x']:g})"


@r.callback_query(F.data.startswith('u:'))
async def ucb(cb: CallbackQuery, state: FSMContext):
    d, uid = cb.data[2:], cb.from_user.id
    u = await ensure(uid, cb.from_user.full_name)
    if u['banned']:
        return await cb.answer('Аккаунт заблокирован', show_alert=True)
    back = [('⬅️ Меню', 'u:m')]
    if d == 'm':
        await show(cb, '🎰 Fortuna — главное меню', MENU())
    elif d == 'me':
        await show(cb, f"👤 {u['name']}\nБаланс: {f2(u['bal'])} ⭐\nПополнено: {f2(u['dep'])} ⭐\n"
                       f"Нужно отыграть до вывода: {f2(u['wager'])} ⭐", kb(back))
    elif d == 'dep':
        bonus = f"\n🎁 Бонус к пополнению: +{SET['dep_bonus']:g}%" if SET['dep_bonus'] else ''
        await show(cb, f"⭐ Пополнение в Telegram Stars (1 ⭐ = 1 ⭐){bonus}\nВыберите сумму:",
                   kb([(f'{n} ⭐', f'u:d:{n}') for n in (50, 100, 250)], [(f'{n} ⭐', f'u:d:{n}') for n in (500, 1000, 5000)],
                      [('✏️ Другая сумма', 'u:dc')], back))
    elif d.startswith('d:'):
        try:
            n = int(d[2:])
            if not SET['min_dep'] <= n <= SET['max_dep']:
                raise Err(f"Сумма от {SET['min_dep']:g} до {SET['max_dep']:g} ⭐")
            await bot.send_invoice(uid, title='Пополнение баланса', description=f'Fortuna: {n} ⭐', payload=f'dep:{uid}:{n}',
                                   provider_token='', currency='XTR', prices=[LabeledPrice(label='Пополнение', amount=n)])
            await cb.answer()
        except Err as e:
            await cb.answer(str(e), show_alert=True)
    elif d == 'dc':
        await state.set_state(S.dep)
        await cb.message.answer('Введите сумму пополнения в ⭐ (целое число):')
        await cb.answer()
    elif d == 'pr':
        await state.set_state(S.promo)
        await cb.message.answer('Отправьте промокод:')
        await cb.answer()
    elif d == 'ref':
        n = (await q('SELECT COUNT(*) n FROM users WHERE ref=?', uid, one=True))['n']
        await show(cb, f"👥 Приглашено: {n}\nВы получаете {SET['ref_pct']:g}% от пополнений друзей.\n\n"
                       f"Ваша ссылка:\nhttps://t.me/{(await bot.me()).username}?start=ref_{uid}", kb(back))
    elif d == 'top':
        rows = await q('SELECT name,wager_sum FROM (SELECT u.name name,SUM(b.amt) wager_sum FROM bets b JOIN users u ON u.id=b.uid '
                       'WHERE b.ts>? GROUP BY b.uid) ORDER BY wager_sum DESC LIMIT 10', now() - 7 * 86400, many=True)
        await show(cb, '🏆 Топ-10 по обороту за 7 дней\n\n' + ('\n'.join(
            f"{i}. {x['name'][:3]}••• — {f2(x['wager_sum'])} ⭐" for i, x in enumerate(rows, 1)) or 'Пока пусто'), kb(back))
    elif d == 'hist':
        rows = await q('SELECT game,amt,pay FROM bets WHERE uid=? ORDER BY id DESC LIMIT 10', uid, many=True)
        await show(cb, '🧾 Последние ставки\n\n' + ('\n'.join(
            f"{GN[x['game']]}: {x['amt']:g} → {x['pay']:g} ⭐" for x in rows) or 'Ставок ещё не было'), kb(back))


@r.message(S.dep)
async def dep_custom(m: Message, state: FSMContext):
    await state.clear()
    try:
        n = int(m.text.strip())
        if not SET['min_dep'] <= n <= SET['max_dep']:
            raise ValueError
    except (ValueError, AttributeError):
        return await m.answer(f"Сумма от {SET['min_dep']:g} до {SET['max_dep']:g} ⭐")
    await bot.send_invoice(m.from_user.id, title='Пополнение баланса', description=f'Fortuna: {n} ⭐', payload=f'dep:{m.from_user.id}:{n}',
                           provider_token='', currency='XTR', prices=[LabeledPrice(label='Пополнение', amount=n)])


@r.message(S.promo)
async def promo_in(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await redeem(m.from_user.id, m.text or ''))


# ---- платежи Telegram Stars ----
@r.pre_checkout_query()
async def pre(c: PreCheckoutQuery):
    u = await q('SELECT banned FROM users WHERE id=?', c.from_user.id, one=True)
    ok = c.invoice_payload.startswith('dep:') and not SET['maint'] and not (u and u['banned'])
    await c.answer(ok=bool(ok), error_message=None if ok else 'Пополнение сейчас недоступно')


@r.message(F.successful_payment)
async def paid(m: Message):
    sp, uid = m.successful_payment, m.from_user.id
    stars = sp.total_amount
    if not await q('INSERT OR IGNORE INTO pays VALUES(?,?,?,?)', sp.telegram_payment_charge_id, uid, stars, now()):
        return  # уже зачислено
    bonus = r2(stars * SET['dep_bonus'] / 100)
    tot = stars + bonus
    await q('UPDATE users SET bal=ROUND(bal+?,2),dep=dep+?,wager=wager+? WHERE id=?', tot, stars, tot * SET['wager_x'], uid)
    await log(uid, 'deposit', stars, sp.telegram_payment_charge_id)
    ref = (await q('SELECT ref FROM users WHERE id=?', uid, one=True))['ref']
    if ref and SET['ref_pct']:
        rb = r2(stars * SET['ref_pct'] / 100)
        if rb:
            await q('UPDATE users SET bal=ROUND(bal+?,2),wager=wager+? WHERE id=?', rb, rb * SET['wager_x'], ref)
            try:
                await bot.send_message(ref, f'👥 Реферальный бонус: +{rb:g} ⭐')
            except Exception:
                pass
    await m.answer(f'✅ Баланс пополнен на {stars} ⭐' + (f' (+{bonus:g} бонус)' if bonus else ''), reply_markup=MENU())
    await notify_admins(f'⭐ Депозит {stars} ⭐ от {m.from_user.full_name} ({uid})')


# ============================ Админ-панель ============================
isadm = F.from_user.id.in_(ADMINS)


async def panel():
    d = now() - 86400
    u = await q('SELECT COUNT(*) n,COALESCE(SUM(bal),0) b FROM users', one=True)
    w = await q("SELECT COUNT(*) n,COALESCE(SUM(amt),0) a FROM wds WHERE st='new'", one=True)
    b1 = await q('SELECT COUNT(*) n,COALESCE(SUM(amt-pay),0) p FROM bets WHERE ts>?', d, one=True)
    return (f"🛠 Админ-панель Fortuna\nИгроков: {u['n']} · на балансах: {f2(u['b'])} ⭐\n"
            f"За 24ч: ставок {b1['n']}, прибыль казино {f2(b1['p'])} ⭐\nЗаявок на вывод: {w['n']} ({f2(w['a'])} ⭐)"
            f"\nТех. работы: {'ВКЛ' if SET['maint'] else 'выкл'}",
            kb([('📊 Статистика', 'a:st'), ('🎲 Шансы и игры', 'a:g')], [('👤 Игроки', 'a:u'), ('💸 Выводы', 'a:w')],
               [('🎁 Промокоды', 'a:p'), ('📢 Рассылка', 'a:bc')], [('⚙️ Настройки', 'a:s'), ('💾 Бэкап БД', 'a:bk')]))


def gcard(g):
    c = CFG[g]
    return (f"{GN[g]}\nПреимущество казино: {c['edge']:g}% (RTP {100 - c['edge']:g}%)\nСтавка: {c['min']:g}–{c['max']:g} ⭐\n"
            f"Статус: {'✅ включена' if c['on'] else '⛔ выключена'}\n\nМножители пересчитываются автоматически, "
            f"игроки видят актуальные значения и edge в приложении.",
            kb([('−1', f'a:ge:{g}:-1'), ('−0.5', f'a:ge:{g}:-0.5'), ('+0.5', f'a:ge:{g}:0.5'), ('+1', f'a:ge:{g}:1')],
               [('✏️ Edge %', f'a:gi:{g}:edge'), ('✏️ Мин', f'a:gi:{g}:min'), ('✏️ Макс', f'a:gi:{g}:max')],
               [('⛔ Выключить' if c['on'] else '✅ Включить', f'a:gt:{g}')], [('⬅️ К списку игр', 'a:g')]))


def setg(g, f, v):
    c = CFG[g]
    if f == 'edge':
        c['edge'] = round(min(MAX_EDGE, max(0.0, v)), 2)
    elif f == 'min':
        c['min'] = min(max(.01, v), c['max'])
    else:
        c['max'] = max(v, c['min'])


async def ucard(uid):
    u = await q('SELECT * FROM users WHERE id=?', uid, one=True)
    if not u:
        return 'Игрок не найден', kb([('⬅️ Меню', 'a:m')])
    st = await q('SELECT COUNT(*) n,COALESCE(SUM(amt),0) a,COALESCE(SUM(pay),0) p FROM bets WHERE uid=?', uid, one=True)
    return (f"👤 {u['name']} ({uid})\nБаланс: {f2(u['bal'])} ⭐ · депозиты: {f2(u['dep'])} ⭐ · отыграть: {f2(u['wager'])} ⭐\n"
            f"Ставок: {st['n']} · оборот {f2(st['a'])} · выплаты {f2(st['p'])}\nСтатус: {'🚫 в бане' if u['banned'] else '✅ активен'}",
            kb([('💰 Изменить баланс', f'a:ua:{uid}'), ('✅ Разбан' if u['banned'] else '🚫 Бан', f'a:ub:{uid}')], [('⬅️ Меню', 'a:m')]))


@r.message(Command('admin'), isadm)
async def admin(m: Message, state: FSMContext):
    await state.clear()
    await show(m, *await panel())


@r.message(Command('newpromo'), isadm)
async def newpromo(m: Message, command: CommandObject):
    try:
        code, a, n = (command.args or '').split()
        await q('INSERT OR REPLACE INTO promos(code,amt,uses,used) VALUES(?,?,?,0)', code.upper(), float(a), int(n))
        await m.answer(f'✅ Промокод {code.upper()}: {float(a):g} ⭐ × {int(n)} активаций')
    except ValueError:
        await m.answer('Формат: /newpromo КОД СУММА АКТИВАЦИЙ')


@r.message(Command('refund'), isadm)
async def refund(m: Message, command: CommandObject):
    try:
        uid, cid = (command.args or '').split()
        p = await q('SELECT stars FROM pays WHERE cid=? AND uid=?', cid, int(uid), one=True)
        await bot.refund_star_payment(user_id=int(uid), telegram_payment_charge_id=cid)
        await q('UPDATE users SET bal=MAX(0,ROUND(bal-?,2)) WHERE id=?', p['stars'] if p else 0, int(uid))
        await q('DELETE FROM pays WHERE cid=?', cid)
        await log(m.from_user.id, 'refund', p['stars'] if p else 0, cid)
        await m.answer('✅ Stars возвращены игроку, баланс уменьшен')
    except Exception as e:
        await m.answer(f'Формат: /refund USER_ID CHARGE_ID\nОшибка: {e}')


@r.callback_query(F.data.startswith('a:'), isadm)
async def adm(cb: CallbackQuery, state: FSMContext):
    p = cb.data.split(':')[1:]
    s, aid = p[0], cb.from_user.id
    if s == 'm':
        await state.clear()
        await show(cb, *await panel())
    elif s == 'st':
        rows = await q('SELECT game,COUNT(*) n,SUM(amt) a,SUM(pay) p FROM bets GROUP BY game ORDER BY a DESC', many=True)
        dp_ = await q('SELECT COALESCE(SUM(stars),0) s,COUNT(*) n FROM pays', one=True)
        t = f"📊 Депозитов: {dp_['n']} на {dp_['s']} ⭐\n\nИгра · ставок · оборот · факт. RTP / план\n" + '\n'.join(
            f"{GN[x['game']]} · {x['n']} · {f2(x['a'])} · {x['p'] / x['a'] * 100:.1f}% / {100 - CFG[x['game']]['edge']:g}%" for x in rows)
        await show(cb, t, kb([('⬅️ Меню', 'a:m')]))
    elif s == 'g':
        if len(p) == 1:
            rows = [[(f"{GN[g]} {CFG[g]['edge']:g}%{'' if CFG[g]['on'] else ' ⛔'}", f'a:g:{g}') for g in GAMES[i:i + 2]]
                    for i in range(0, len(GAMES), 2)]
            await show(cb, '🎲 Выберите игру. Число — преимущество казино (edge). Чем оно ниже, тем выше выплаты игрокам.',
                       kb(*rows, [('✏️ Edge для всех игр', 'a:gi:all:edge')], [('⬅️ Меню', 'a:m')]))
        else:
            await show(cb, *gcard(p[1]))
    elif s == 'ge':
        setg(p[1], 'edge', CFG[p[1]]['edge'] + float(p[2]))
        await save()
        await log(aid, 'cfg', CFG[p[1]]['edge'], f'{p[1]} edge')
        await show(cb, *gcard(p[1]))
    elif s == 'gt':
        CFG[p[1]]['on'] ^= 1
        await save()
        await log(aid, 'cfg', CFG[p[1]]['on'], f'{p[1]} on/off')
        await show(cb, *gcard(p[1]))
    elif s == 'gi':
        await state.set_state(S.val)
        await state.update_data(t='g', g=p[1], f=p[2])
        await cb.message.answer(f'Введите новое значение ({p[2]}) числом. Edge ограничен 0–{MAX_EDGE:g}%.')
        await cb.answer()
    elif s == 's':
        if len(p) > 1:
            if p[1] == 'maint':
                SET['maint'] ^= 1
                await save()
            else:
                await state.set_state(S.val)
                await state.update_data(t='s', k=p[1])
                await cb.message.answer(f'{SL[p[1]]} — введите число:')
                return await cb.answer()
        await show(cb, '⚙️ Настройки (нажмите, чтобы изменить)', kb(
            *[[(f'{SL[k]}: {SET[k]:g}', f'a:s:{k}')] for k in SL], [('⬅️ Меню', 'a:m')]))
    elif s == 'u':
        await state.set_state(S.uid)
        await cb.message.answer('Отправьте ID игрока:')
        await cb.answer()
    elif s == 'ub':
        await q('UPDATE users SET banned=1-banned WHERE id=?', int(p[1]))
        await show(cb, *await ucard(int(p[1])))
    elif s == 'ua':
        await state.set_state(S.val)
        await state.update_data(t='bal', uid=int(p[1]))
        await cb.message.answer('Введите сумму изменения баланса (например 100 или -50):')
        await cb.answer()
    elif s == 'w':
        rows = await q("SELECT w.*,u.name FROM wds w JOIN users u ON u.id=w.uid WHERE st='new' ORDER BY w.id LIMIT 8", many=True)
        await show(cb, '💸 Заявки на вывод\n\n' + ('\n'.join(f"#{x['id']} · {x['name']} ({x['uid']}) · {x['amt']:g} ⭐" for x in rows) or 'Нет новых заявок'),
                   kb(*[[(f"✅ #{x['id']}", f"a:wd:ok:{x['id']}"), (f"❌ #{x['id']}", f"a:wd:no:{x['id']}")] for x in rows], [('⬅️ Меню', 'a:m')]))
    elif s == 'wd':
        w = await q("SELECT * FROM wds WHERE id=? AND st='new'", int(p[2]), one=True)
        if not w:
            return await cb.answer('Уже обработано', show_alert=True)
        ok = p[1] == 'ok'
        await q('UPDATE wds SET st=? WHERE id=?', 'paid' if ok else 'rejected', w['id'])
        if not ok:
            await addbal(w['uid'], w['amt'])
        await log(aid, 'wd_ok' if ok else 'wd_no', w['amt'], f"#{w['id']} user {w['uid']}")
        try:
            await bot.send_message(w['uid'], f"{'✅ Вывод' if ok else '❌ Заявка отклонена, средства возвращены:'} {w['amt']:g} ⭐" + (' выплачен' if ok else ''))
        except Exception:
            pass
        await cb.answer('Готово')
        await cb.message.edit_reply_markup(reply_markup=None)
    elif s == 'p':
        rows = await q('SELECT * FROM promos ORDER BY rowid DESC LIMIT 10', many=True)
        await show(cb, '🎁 Промокоды\nСоздать: /newpromo КОД СУММА АКТИВАЦИЙ\n\n' + (
            '\n'.join(f"{x['code']} · {x['amt']:g} ⭐ · {x['used']}/{x['uses']}" for x in rows) or 'Пока нет'), kb([('⬅️ Меню', 'a:m')]))
    elif s == 'bc':
        await state.set_state(S.bc)
        await cb.message.answer('Отправьте текст рассылки:')
        await cb.answer()
    elif s == 'bcgo':
        txt = (await state.get_data()).get('txt')
        await state.clear()
        if txt:
            asyncio.create_task(broadcast(txt, aid))
        await show(cb, '📢 Рассылка запущена, пришлю отчёт.', kb([('⬅️ Меню', 'a:m')]))
    elif s == 'bk':
        path = os.path.join(BASE, 'backup.db')
        if os.path.exists(path):
            os.remove(path)
        await db.execute('VACUUM INTO ?', (path,))
        await cb.message.answer_document(FSInputFile(path))
        await cb.answer()


async def broadcast(txt, aid):
    ok = bad = 0
    for x in await q('SELECT id FROM users WHERE banned=0', many=True):
        try:
            await bot.send_message(x['id'], txt)
            ok += 1
        except Exception:
            bad += 1
        await asyncio.sleep(.05)
    await bot.send_message(aid, f'📢 Рассылка завершена: доставлено {ok}, ошибок {bad}')


@r.message(S.uid, isadm)
async def adm_uid(m: Message, state: FSMContext):
    await state.clear()
    try:
        await show(m, *await ucard(int(m.text.strip())))
    except (ValueError, AttributeError):
        await m.answer('Нужен числовой ID')


@r.message(S.bc, isadm)
async def adm_bc(m: Message, state: FSMContext):
    await state.update_data(txt=m.text)
    await m.answer(f'Отправить всем игрокам?\n\n{m.text}', reply_markup=kb([('✅ Отправить', 'a:bcgo'), ('❌ Отмена', 'a:m')]))


@r.message(S.val, isadm)
async def adm_val(m: Message, state: FSMContext):
    d = await state.get_data()
    try:
        v = float((m.text or '').replace(',', '.'))
    except ValueError:
        return await m.answer('Нужно число')
    await state.clear()
    if d['t'] == 'g':
        for g in (GAMES if d['g'] == 'all' else [d['g']]):
            setg(g, d['f'], v)
        await save()
        await log(m.from_user.id, 'cfg', v, f"{d['g']} {d['f']}")
        await show(m, *(gcard(d['g']) if d['g'] != 'all' else await panel()))
    elif d['t'] == 's':
        SET[d['k']] = int(v) if d['k'] in ('maint', 'min_dep', 'max_dep') else v
        await save()
        await log(m.from_user.id, 'cfg', v, d['k'])
        await m.answer('✅ Сохранено', reply_markup=kb([('⚙️ Настройки', 'a:s'), ('⬅️ Меню', 'a:m')]))
    else:  # изменение баланса игрока
        await addbal(d['uid'], v)
        await log(d['uid'], 'adjust', v, f'admin {m.from_user.id}')
        await show(m, *await ucard(d['uid']))


# ============================ Запуск ============================
async def main():
    global db
    logging.basicConfig(level=logging.INFO)
    db = await aiosqlite.connect(DBF)
    db.row_factory = aiosqlite.Row
    await db.execute('PRAGMA journal_mode=WAL')
    await db.executescript(SCHEMA)
    await load()
    row = await q("SELECT v FROM kv WHERE k='rkbets'", one=True)  # вернуть ставки ракеты, зависшие при перезапуске
    for k, a in (json.loads(row['v']) if row else {}).items():
        await addbal(int(k), a)
    await q("DELETE FROM kv WHERE k='rkbets'")
    app = web.Application()
    app.router.add_post('/api/{p:.+}', api)
    app.router.add_get('/', lambda _: web.FileResponse(os.path.join(BASE, 'webapp', 'index.html')))
    run = web.AppRunner(app)
    await run.setup()
    await web.TCPSite(run, '0.0.0.0', PORT).start()
    asyncio.create_task(rocket_loop())
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text='🎰 Играть', web_app=WebAppInfo(url=URL)))
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
