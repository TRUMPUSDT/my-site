#!/usr/bin/env python3
"""Rust Tracker на Python. Только стандартная библиотека, нужен Python 3.9+.
Два режима:
  локальный  (PUBLIC_URL пуст): один пользователь, без входа, как раньше;
  публичный  (PUBLIC_URL задан): вход через Steam, у каждого свой список слежки и свой Telegram."""
import atexit, base64, hmac, json, os, re, secrets, sys, threading, time, uuid
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    try:
        with open(os.path.join(HERE, '.env'), encoding='utf-8-sig') as f:
            for line in f:
                m = re.match(r'^\s*([A-Z_]+)\s*=\s*(.*?)\s*$', line)
                if not m or m.group(1) in os.environ:
                    continue
                v = re.sub(r'\s+#.*$', '', m.group(2))      # комментарий в конце строки
                v = re.sub(r'^["\']|["\']$', '', v.strip())
                os.environ[m.group(1)] = v
    except OSError:
        pass


load_env()
E = os.environ


def num(name, default, minimum):
    try:
        return max(minimum, int(E.get(name) or default))
    except ValueError:
        return default


KEY = E.get('STEAM_API_KEY', '')
PORT = num('PORT', 3000, 1)
HOST = E.get('HOST') or '127.0.0.1'
PUBLIC_URL = (E.get('PUBLIC_URL') or '').rstrip('/')
MULTI = bool(PUBLIC_URL)
TG_TOKEN, TG_CHAT = E.get('TG_BOT_TOKEN', ''), E.get('TG_CHAT_ID', '')
POLL_SEC = num('POLL_SEC', 60, 20)
BAN_MIN = num('BAN_CHECK_MIN', 30, 5)
MAX_WATCH = num('MAX_WATCH', 30, 1) if MULTI else 300
LOOKUPS_PER_DAY = num('LOOKUPS_PER_DAY', 300, 10)
TZ = E.get('TZ_NAME') or 'Europe/Warsaw'
RUST = '252490'
BASE = 76561197960265728
BOT_USERNAME = None
LOCAL_USER = {'sid': 'local', 'name': 'Локальный режим', 'avatar': ''}
try:
    TZI = ZoneInfo(TZ) if ZoneInfo else None
except Exception:
    TZI = None  # на Windows без tzdata: берём часовой пояс системы

now = lambda: int(time.time())
is_id = lambda s: bool(re.fullmatch(r'\d{17}', s or ''))
hesc = lambda s: str('' if s is None else s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
plink = lambda i: 'https://steamcommunity.com/profiles/' + i


def clean(s, n):
    return re.sub(r'[\x00-\x1f]', ' ', str('' if s is None else s)).strip()[:n]


def rnd(x):  # округление как в JS, а не «банковское»
    return int(x + 0.5)


# ---------------- хранилище (JSON-файл) ----------------
# players: общие данные об отслеживаемых аккаунтах (история одна на всех)
# watch:   watch[кто_следит][за_кем] = теги, заметка, группа, флаг оповещения
# users, sessions, optout: вход, сессии и отказавшиеся от слежки
DB_FILE = os.path.join(HERE, 'data', 'db.json')
db = {'players': {}, 'names': {}, 'watch': {}, 'users': {}, 'sessions': {}, 'optout': []}
lock = threading.RLock()
dirty = False
try:
    with open(DB_FILE, encoding='utf-8') as f:
        db.update(json.load(f))
except (OSError, ValueError):
    pass


def migrate():  # старый формат (теги внутри игрока) -> новый
    for tid, rec in db['players'].items():
        if 'tags' in rec:
            db['watch'].setdefault('local', {})[tid] = {
                'tags': rec.pop('tags'), 'note': rec.pop('note', ''), 'group': rec.pop('group', ''),
                'alertRust': rec.pop('alertRust', False), 'added': rec.get('added', int(time.time()))}
    t = int(time.time())
    db['sessions'] = {k: v for k, v in db['sessions'].items() if v['exp'] > t}


migrate()


def save():
    global dirty
    dirty = True


def flush():
    global dirty
    with lock:
        if not dirty:
            return
        dirty = False
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        with open(DB_FILE + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(db, f, ensure_ascii=False)
        os.replace(DB_FILE + '.tmp', DB_FILE)


atexit.register(flush)

# ---------------- Steam API ----------------


class SteamError(Exception):
    def __init__(self, status):
        super().__init__('Steam API %s' % status)
        self.status = status


cache = {}


def fetch(url, data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers


def steam(path, params, ttl=30):
    q = dict(params)
    q['key'] = KEY
    url = 'https://api.steampowered.com/' + path + '?' + urllib.parse.urlencode(q)
    hit = cache.get(url)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        body, _ = fetch(url)
    except urllib.error.HTTPError as e:
        raise SteamError(e.code)
    v = json.loads(body.decode('utf-8'))
    if ttl > 0:
        cache[url] = (time.time() + ttl, v)
    if len(cache) > 2000:
        cache.clear()
    return v


def safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception:
        return None


def dig(d, *keys):
    for k in keys:
        if d is None:
            return None
        try:
            d = d[k]
        except (KeyError, IndexError, TypeError):
            return None
    return d


def resolve(q):
    q = (q or '').strip()
    if is_id(q):
        return q
    m = re.fullmatch(r'STEAM_[0-5]:([01]):(\d+)', q, re.I)
    if m:
        return str(BASE + int(m.group(2)) * 2 + int(m.group(1)))
    m = re.fullmatch(r'\[U:1:(\d+)\]', q, re.I)
    if m:
        return str(BASE + int(m.group(1)))
    m = re.search(r'steamcommunity\.com/profiles/(\d{17})', q)
    if m:
        return m.group(1)
    m = re.search(r'steamcommunity\.com/id/([^/?#\s]+)', q)
    if m:
        q = m.group(1)
    if not re.fullmatch(r'[\w-]{2,32}', q):
        return None
    d = steam('ISteamUser/ResolveVanityURL/v1/', {'vanityurl': q}, 300)
    return d['response']['steamid'] if d['response'].get('success') == 1 else None


pool = ThreadPoolExecutor(max_workers=12)


def player(sid):
    jobs = {
        'sum': (steam, 'ISteamUser/GetPlayerSummaries/v2/', {'steamids': sid}, 30),
        'bans': (steam, 'ISteamUser/GetPlayerBans/v1/', {'steamids': sid}, 300),
        'owned': (steam, 'IPlayerService/GetOwnedGames/v1/', {'input_json': json.dumps(
            {'steamid': sid, 'include_played_free_games': True, 'appids_filter': [int(RUST)]})}, 120),
        'recent': (steam, 'IPlayerService/GetRecentlyPlayedGames/v1/', {'steamid': sid, 'count': 6}, 120),
        'level': (steam, 'IPlayerService/GetSteamLevel/v1/', {'steamid': sid}, 300),
        'friends': (steam, 'ISteamUser/GetFriendList/v1/', {'steamid': sid, 'relationship': 'friend'}, 300),
        'stats': (steam, 'ISteamUserStats/GetUserStatsForGame/v2/', {'steamid': sid, 'appid': RUST}, 120),
    }
    futs = {k: pool.submit(safe, *v) for k, v in jobs.items()}
    r = {k: f.result() for k, f in futs.items()}
    p = dig(r['sum'], 'response', 'players', 0)
    if not p:
        return None
    game = dig(r['owned'], 'response', 'games', 0)
    st = {s['name']: s['value'] for s in (dig(r['stats'], 'playerstats', 'stats') or [])}
    kills = st.get('kill_player')
    deaths = st['deaths'] if st.get('deaths') is not None else st.get('death_player')
    ach_list = dig(r['stats'], 'playerstats', 'achievements')
    ach = len([a for a in ach_list if a.get('achieved')]) if ach_list is not None else None
    lvl = dig(r['level'], 'response', 'player_level')
    fired = st.get('bullet_fired')
    return {
        'profile': p,
        'isPublic': p.get('communityvisibilitystate') == 3,
        'bans': dig(r['bans'], 'players', 0),
        'rust': {
            'owned': bool(game),
            'minutesTotal': game.get('playtime_forever') if game else None,
            'minutes2w': game.get('playtime_2weeks') if game else None,
            'lastPlayed': game.get('rtime_last_played') if game else None,
            'statsVisible': len(st) > 0,
        },
        'm': {
            'hours': rnd(game['playtime_forever'] / 60) if game else None,
            'hours2w': rnd(game['playtime_2weeks'] / 60) if game and game.get('playtime_2weeks') is not None else None,
            'kills': kills, 'deaths': deaths,
            'kd': round(kills / deaths, 2) if kills is not None and deaths else None,
            'acc': round((st.get('bullet_hit_player') or 0) / fired * 100, 1) if fired else None,
            'hs': st.get('headshot'), 'fired': fired,
            'wood': st.get('harvest.wood'), 'stone': st.get('harvest.stones'),
            'ach': ach, 'level': lvl,
        },
        'stats': st,
        'recent': dig(r['recent'], 'response', 'games') or [],
        'level': lvl,
        'friends': len(dig(r['friends'], 'friendslist', 'friends')) if dig(r['friends'], 'friendslist', 'friends') is not None else None,
    }


def status(ids, ttl=20):
    d = steam('ISteamUser/GetPlayerSummaries/v2/', {'steamids': ','.join(ids)}, ttl)
    return [{
        'steamid': p['steamid'], 'name': p.get('personaname'), 'avatar': p.get('avatarfull'),
        'state': p.get('personastate', 0), 'gameid': p.get('gameid') or None,
        'game': p.get('gameextrainfo') or None, 'server': p.get('gameserverip') or None,
        'lastlogoff': p.get('lastlogoff') or None,
    } for p in d['response'].get('players', [])]


# ---------------- BattleMetrics: адрес сервера -> название ----------------
bm = {}


def server_name(addr):
    if not addr or not re.fullmatch(r'[\d.]+:\d+', addr):
        return None
    c = bm.get(addr)
    if c and c[0] > time.time():
        return c[1]
    v = None
    try:
        ip, port = addr.split(':')
        q = urllib.parse.urlencode({'filter[game]': 'rust', 'filter[search]': ip, 'page[size]': 25})
        body, _ = fetch('https://api.battlemetrics.com/servers?' + q, headers={'User-Agent': 'rust-tracker'})
        lst = json.loads(body).get('data', [])
        pn = int(port)
        s = next((x for x in lst if x['attributes']['port'] == pn), None) or \
            next((x for x in lst if x['attributes']['port'] + 1 == pn or x['attributes']['port'] == pn + 1), None)
        if s:
            a = s['attributes']
            v = {'name': a['name'], 'players': a.get('players'), 'max': a.get('maxPlayers'), 'id': s['id']}
    except Exception:
        pass
    bm[addr] = (time.time() + (600 if v else 120), v)
    return v


# ---------------- Telegram ----------------
def tg_api(method, payload, timeout=15):
    try:
        body, _ = fetch('https://api.telegram.org/bot%s/%s' % (TG_TOKEN, method), data=json.dumps(payload).encode(),
                        headers={'Content-Type': 'application/json'}, timeout=timeout)
        return json.loads(body)
    except Exception:
        return None


def tg(chat, text):
    if not (TG_TOKEN and chat):
        return False
    r = tg_api('sendMessage', {'chat_id': chat, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True})
    return bool(r and r.get('ok'))


def tg_photo(chat, png, caption):
    if not (TG_TOKEN and chat):
        return False
    b = uuid.uuid4().hex
    parts = []
    for k, v in (('chat_id', chat), ('caption', caption), ('parse_mode', 'HTML')):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode())
    parts.append(('--%s\r\nContent-Disposition: form-data; name="photo"; filename="card.png"\r\n'
                  'Content-Type: image/png\r\n\r\n' % b).encode() + png + b'\r\n')
    parts.append(('--%s--\r\n' % b).encode())
    try:
        fetch('https://api.telegram.org/bot%s/sendPhoto' % TG_TOKEN, data=b''.join(parts),
              headers={'Content-Type': 'multipart/form-data; boundary=' + b}, timeout=30)
        return True
    except Exception:
        return False


def chat_of(sid):
    """Куда слать сообщения этому пользователю."""
    if not TG_TOKEN:
        return None
    if not MULTI:
        return TG_CHAT or None
    return dig(db['users'].get(sid), 'tg', 'chat')


def user_chat(user):
    with lock:
        return chat_of(user['sid'])


def watchers(tid):
    with lock:
        return [(sid, w[tid]) for sid, w in db['watch'].items() if tid in w]


def drop_orphans():
    used = set()
    for w in db['watch'].values():
        used.update(w)
    for tid in [t for t in db['players'] if t not in used]:
        del db['players'][tid]


def handle_update(u):
    """Входящее сообщение боту: привязка чата по коду с сайта, /stop, /status."""
    m = u.get('message') or {}
    chat, text = dig(m, 'chat', 'id'), (m.get('text') or '').strip()
    if not chat or dig(m, 'chat', 'type') != 'private':
        return
    if text.startswith('/start'):
        code = text[6:].strip()
        if not code:
            tg(chat, 'Привет! Чтобы получать оповещения, откройте сайт, зайдите в «Настройки» и нажмите «Подключить Telegram».')
            return
        with lock:
            user = next((x for x in db['users'].values() if x.get('link_code') == code and x.get('link_exp', 0) > now()), None)
            if user:
                for x in db['users'].values():
                    if dig(x, 'tg', 'chat') == chat:
                        x.pop('tg', None)
                user['tg'] = {'chat': chat, 't': now()}
                user.pop('link_code', None)
                user.pop('link_exp', None)
                save()
        tg(chat, '✅ Готово, %s. Сюда будут приходить новые баны игроков из вашей слежки и вход в Rust, если вы включили это у игрока.\n/status — кто сейчас онлайн, /stop — отключить.' % hesc(user['name'])
           if user else 'Ссылка устарела. Нажмите «Подключить Telegram» на сайте ещё раз.')
    elif text.startswith('/stop'):
        with lock:
            for x in db['users'].values():
                if dig(x, 'tg', 'chat') == chat:
                    x.pop('tg', None)
            save()
        tg(chat, 'Оповещения отключены. Подключить снова можно на сайте.')
    elif text.startswith('/status'):
        with lock:
            user = next((x for x in db['users'].values() if dig(x, 'tg', 'chat') == chat), None)
            lines = []
            if user:
                for tid in db['watch'].get(user['sid'], {}):
                    r = db['players'].get(tid)
                    l = (r or {}).get('last') or {}
                    if l.get('gameid') == RUST:
                        lines.append('🟠 %s в Rust' % hesc(r['name']))
                    elif l.get('state', 0) > 0 or l.get('gameid'):
                        lines.append('🟢 %s в сети' % hesc(r['name']))
        tg(chat, '\n'.join(lines) if lines else ('Сейчас никого из слежки нет в сети.' if user else 'Чат не привязан к аккаунту. Подключите Telegram на сайте.'))


def bot_loop():
    global BOT_USERNAME
    BOT_USERNAME = dig(tg_api('getMe', {}), 'result', 'username')
    off = 0
    while True:
        try:
            r = tg_api('getUpdates', {'offset': off, 'timeout': 25, 'allowed_updates': ['message']}, timeout=35)
            if r is None:
                time.sleep(5)
                continue
            for u in r.get('result', []):
                off = u['update_id'] + 1
                try:
                    handle_update(u)
                except Exception as e:
                    print('bot:', e, file=sys.stderr)
        except Exception:
            time.sleep(5)


# ---------------- история, ники, баны ----------------
def slot(ts):
    d = datetime.fromtimestamp(ts, TZI) if TZI else datetime.fromtimestamp(ts)
    return d.weekday() * 24 + d.hour  # Пн = 0


def record_name(sid, name):
    if not name:
        return
    with lock:
        a = db['names'].setdefault(sid, [])
        if not a or a[-1]['name'] != name:
            a.append({'name': name, 't': now()})
            del a[:-50]
            save()


def ev(rec, typ, **extra):
    rec['events'].append(dict(t=now(), type=typ, **extra))
    del rec['events'][:-300]
    save()


def W(user):  # список слежки конкретного пользователя
    return db['watch'].setdefault(user['sid'], {})


def pub(user, tid):
    r, m = db['players'][tid], W(user).get(tid, {})
    return {'id': tid, 'name': r['name'], 'avatar': r['avatar'], 'last': r.get('last'), 'bans': r.get('bans'),
            'added': m.get('added'), 'tags': m.get('tags', []), 'note': m.get('note', ''),
            'group': m.get('group', ''), 'alertRust': m.get('alertRust', False)}


def notify(alerts):
    for kind, tid, name, a, b, c in alerts:
        if kind == 'rust':
            sn = server_name(a)
            text = '🎮 <b>%s</b> зашёл в Rust' % hesc(name) + \
                   ('\nСервер: ' + hesc(sn['name']) if sn else '\nСервер: ' + hesc(a) if a else '') + '\n' + plink(tid)
        else:
            text = '🚨 <b>%s</b>: новый бан\nVAC: %s, игровых: %s%s\n%s' % (
                hesc(name), a, b, ', бан сообщества' if c else '', plink(tid))
        for sid, meta in watchers(tid):
            if kind == 'rust' and not meta.get('alertRust'):
                continue
            chat = chat_of(sid)
            if chat:
                tg(chat, text)
                time.sleep(0.05)


poll_lock = threading.Lock()


def poll():
    if not poll_lock.acquire(blocking=False):
        return
    try:
        with lock:
            ids = list(db['players'])
        alerts = []
        for i in range(0, len(ids), 100):
            lst = status(ids[i:i + 100], 0)
            t = now()
            for p in lst:
                with lock:
                    rec = db['players'].get(p['steamid'])
                    if not rec:
                        continue
                    rec['name'], rec['avatar'] = p['name'], p['avatar']
                    record_name(rec['id'], p['name'])
                    prev = rec.get('last')
                    was_on = prev and (prev['state'] > 0 or prev['gameid'])
                    is_on = p['state'] > 0 or p['gameid']
                    rust_now = p['gameid'] == RUST
                    if prev:
                        if not was_on and is_on:
                            ev(rec, 'online')
                        if was_on and not is_on:
                            ev(rec, 'offline')
                        if prev['gameid'] != RUST and rust_now:
                            ev(rec, 'rust_start', server=p['server'])
                            alerts.append(('rust', rec['id'], p['name'], p['server'], None, None))
                        elif prev['gameid'] == RUST and not rust_now:
                            ev(rec, 'rust_stop')
                        elif rust_now and prev.get('server') != p['server']:
                            ev(rec, 'server', server=p['server'])
                    rec['last'] = {'state': p['state'], 'gameid': p['gameid'], 'game': p['game'], 'server': p['server'], 't': t}
                    cell = rec['heat'][slot(t)]
                    cell[0] += 1
                    if is_on:
                        cell[1] += 1
                    if rust_now:
                        cell[2] += 1
            save()
        notify(alerts)
    except Exception as e:
        print('poll:', e, file=sys.stderr)
    finally:
        poll_lock.release()


def check_bans():
    try:
        with lock:
            ids = list(db['players'])
        alerts = []
        for i in range(0, len(ids), 100):
            d = steam('ISteamUser/GetPlayerBans/v1/', {'steamids': ','.join(ids[i:i + 100])}, 0)
            for b in d.get('players', []):
                with lock:
                    rec = db['players'].get(b['SteamId'])
                    if not rec:
                        continue
                    nb = {'vac': b['NumberOfVACBans'], 'game': b['NumberOfGameBans'], 'community': b['CommunityBanned'],
                          'econ': b['EconomyBan'], 'days': b['DaysSinceLastBan']}
                    o = rec.get('bans')
                    if o and (nb['vac'] > o['vac'] or nb['game'] > o['game'] or (nb['community'] and not o['community'])):
                        ev(rec, 'ban', vac=nb['vac'], game=nb['game'])
                        alerts.append(('ban', rec['id'], rec['name'], nb['vac'], nb['game'], nb['community']))
                    rec['bans'] = nb
            save()
        notify(alerts)
    except Exception as e:
        print('bans:', e, file=sys.stderr)


# ---------------- вход через Steam (OpenID 2.0) ----------------
def openid_login_url():
    p = {'openid.ns': 'http://specs.openid.net/auth/2.0', 'openid.mode': 'checkid_setup',
         'openid.return_to': PUBLIC_URL + '/auth/callback', 'openid.realm': PUBLIC_URL,
         'openid.identity': 'http://specs.openid.net/auth/2.0/identifier_select',
         'openid.claimed_id': 'http://specs.openid.net/auth/2.0/identifier_select'}
    return 'https://steamcommunity.com/openid/login?' + urllib.parse.urlencode(p)


def openid_verify(Q):
    """Возвращает SteamID64, если Steam подтвердил вход, иначе None."""
    if Q.get('openid.mode') != 'id_res' or Q.get('openid.return_to') != PUBLIC_URL + '/auth/callback':
        return None
    m = re.fullmatch(r'https://steamcommunity\.com/openid/id/(\d{17})', Q.get('openid.claimed_id', ''))
    if not m:
        return None
    chk = dict(Q)
    chk['openid.mode'] = 'check_authentication'
    try:
        body, _ = fetch('https://steamcommunity.com/openid/login', data=urllib.parse.urlencode(chk).encode(),
                        headers={'Content-Type': 'application/x-www-form-urlencoded'})
    except Exception:
        return None
    return m.group(1) if b'is_valid:true' in body else None


def login_user(sid):
    try:
        s = (status([sid], 0) or [{}])[0]
    except Exception:
        s = {}
    t = now()
    with lock:
        u = db['users'].setdefault(sid, {'sid': sid, 'created': t})
        u['name'] = s.get('name') or u.get('name') or 'Игрок ' + sid[-4:]
        u['avatar'] = s.get('avatar') or u.get('avatar', '')
        tok = secrets.token_urlsafe(32)
        db['sessions'] = {k: v for k, v in db['sessions'].items() if v['exp'] > t}
        db['sessions'][tok] = {'sid': sid, 'exp': t + 30 * 86400}
        save()
    return tok


# ---------------- рекламные баннеры (public/banners.json) ----------------
def load_banners():
    """Читает banners.json при каждом запросе: правите файл, и всё сразу обновляется."""
    try:
        with open(os.path.join(HERE, 'public', 'banners.json'), encoding='utf-8-sig') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {'contact': None, 'slots': []}
    today = time.strftime('%Y-%m-%d')
    c = data.get('contact')
    contact = c if isinstance(c, str) and re.fullmatch(r'@[A-Za-z0-9_]{5,32}', c) else None
    slots = []
    for x in data.get('slots') or []:
        if not isinstance(x, dict):
            continue
        img, url = str(x.get('image', '')), str(x.get('url', ''))
        if not (img.startswith('https://') and url.startswith('https://')):
            continue                                  # только https, иначе пропускаем
        if x.get('from') and str(x['from']) > today:
            continue
        if x.get('until') and str(x['until']) < today:
            continue
        slots.append({'position': x.get('position') if x.get('position') in ('home', 'profile') else 'profile',
                      'image': img[:500], 'url': url[:500], 'title': clean(x.get('title'), 80)})
    return {'contact': contact, 'slots': slots[:20]}


# ---------------- лимиты ----------------
rl, daily = {}, {}


def rate_ok(key, limit, window=60):
    t = time.time()
    a = [x for x in rl.get(key, []) if x > t - window]
    ok = len(a) < limit
    if ok:
        a.append(t)
    rl[key] = a
    if len(rl) > 20000:
        rl.clear()
    return ok


def lookup_ok(user):
    if not MULTI:
        return True
    k = (user['sid'], time.strftime('%Y-%m-%d'))
    if len(daily) > 20000:
        daily.clear()
    daily[k] = daily.get(k, 0) + 1
    return daily[k] <= LOOKUPS_PER_DAY and rate_ok(('p', user['sid']), 20)


# ---------------- HTTP ----------------
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def raw(self, code, data, ctype, extra=()):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        if ctype.startswith('text/html'):
            self.send_header('Content-Security-Policy', CSP)
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send(self, code, obj, extra=()):
        self.raw(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8', extra)

    def redirect(self, url, extra=()):
        self.raw(302, b'', 'text/plain', [('Location', url)] + list(extra))

    def body(self, mx=3_000_000):
        n = int(self.headers.get('Content-Length') or 0)
        if n > mx:
            raise ValueError('Слишком большой запрос')
        return self.rfile.read(n) if n else b''

    def jbody(self):
        try:
            return json.loads(self.body(100_000) or b'{}')
        except ValueError:
            return {}

    def token(self):
        try:
            c = cookies.SimpleCookie(self.headers.get('Cookie', ''))
            return c['rt_session'].value if 'rt_session' in c else None
        except Exception:
            return None

    def user(self):
        if not MULTI:
            return LOCAL_USER
        tok = self.token()
        with lock:
            s = db['sessions'].get(tok) if tok else None
            if s and s['exp'] > now():
                return db['users'].get(s['sid'])
        return None

    def cookie(self, value, age):
        return ('Set-Cookie', 'rt_session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s' % (
            value, age, '; Secure' if PUBLIC_URL.startswith('https') else ''))

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def route(self, method):
        u = urllib.parse.urlsplit(self.path)
        P, Q = u.path, dict(urllib.parse.parse_qsl(u.query))
        try:
            if method == 'POST' and self.headers.get('x-rt') != '1':
                return self.send(403, {'error': 'forbidden'})
            self.dispatch(method, P, Q)
        except SteamError as e:
            self.send(429 if e.status == 429 else 502,
                      {'error': 'Steam ограничил запросы, подождите минуту.' if e.status == 429 else 'Ошибка: ' + str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send(502, {'error': 'Ошибка: %s' % e})
            except Exception:
                pass

    def page(self, name):
        with open(os.path.join(HERE, 'public', name), 'rb') as f:
            self.raw(200, f.read(), 'text/html; charset=utf-8')

    def dispatch(self, method, P, Q):
        if P in ('/', '/index.html'):
            return self.page('index.html')
        if P == '/privacy':
            return self.page('privacy.html')

        # --- вход и выход ---
        if P == '/auth/login':
            return self.redirect(openid_login_url() if MULTI else '/')
        if P == '/auth/callback':
            sid = openid_verify(Q) if MULTI else None
            if not sid:
                return self.raw(400, 'Не удалось войти через Steam. Вернитесь на сайт и попробуйте ещё раз.'.encode('utf-8'), 'text/plain; charset=utf-8')
            return self.redirect('/', [self.cookie(login_user(sid), 30 * 86400)])
        if P == '/auth/logout' and method == 'POST':
            tok = self.token()
            with lock:
                db['sessions'].pop(tok, None)
                save()
            return self.send(200, {'ok': True}, [self.cookie('', 0)])

        user = self.user()
        if P == '/api/me':
            chat = user_chat(user) if user else None
            me = {'mode': 'multi' if MULTI else 'local', 'user': None, 'tz': TZ, 'pollSec': POLL_SEC, 'banMin': BAN_MIN,
                  'maxWatch': MAX_WATCH, 'telegram': {'configured': bool(TG_TOKEN), 'linked': bool(chat), 'bot': BOT_USERNAME}}
            if user:
                me['user'] = {'sid': user['sid'], 'name': user['name'], 'avatar': user.get('avatar', ''),
                              'optout': user['sid'] in db['optout']}
            return self.send(200, me)
        if P.startswith('/api/') and not user:
            return self.send(401, {'error': 'Войдите через Steam', 'login': True})

        if P == '/api/banners':
            return self.send(200, load_banners())

        # --- профили ---
        if P == '/api/player':
            if not lookup_ok(user):
                return self.send(429, {'error': 'Слишком много поисков. Подождите минуту или попробуйте завтра.'})
            sid = resolve(Q.get('q'))
            if not sid:
                return self.send(404, {'error': 'Профиль не найден. Проверьте SteamID или ссылку.'})
            d = player(sid)
            if not d:
                return self.send(404, {'error': 'Такого аккаунта нет.'})
            with lock:
                opt = sid in db['optout']
            if not opt:
                record_name(sid, d['profile'].get('personaname'))
            with lock:
                d['names'] = [] if opt else list(db['names'].get(sid, []))
                d['tracked'] = pub(user, sid) if sid in W(user) and sid in db['players'] else None
                d['optout'] = opt
            d['serverInfo'] = server_name(d['profile'].get('gameserverip')) if d['profile'].get('gameid') == RUST else None
            return self.send(200, d)

        # --- слежка ---
        if P == '/api/tracked':
            with lock:
                recs = [pub(user, t) for t in W(user) if t in db['players']]

            def add(o):
                l = o.get('last') or {}
                sn = server_name(l.get('server')) if l.get('gameid') == RUST else None
                o['serverName'] = sn['name'] if sn else None
                return o
            return self.send(200, list(pool.map(add, recs)))

        if P == '/api/track' and method == 'POST':
            b = self.jbody()
            sid = b.get('id')
            if not is_id(sid):
                return self.send(400, {'error': 'Некорректный SteamID'})
            with lock:
                if sid in db['optout']:
                    return self.send(403, {'error': 'Этот игрок отказался от слежки.'})
                if sid in W(user):
                    return self.send(200, pub(user, sid))
                if len(W(user)) >= MAX_WATCH:
                    return self.send(400, {'error': 'Лимит слежки: %d игроков. Уберите кого-нибудь из списка.' % MAX_WATCH})
                exists = sid in db['players']
            if not exists:
                lst = status([sid], 0)
                if not lst:
                    return self.send(404, {'error': 'Аккаунт не найден'})
                s = lst[0]
                bn = dig(safe(steam, 'ISteamUser/GetPlayerBans/v1/', {'steamids': sid}, 0), 'players', 0)
            with lock:
                if not exists and sid not in db['players']:
                    db['players'][sid] = {
                        'id': sid, 'name': s['name'], 'avatar': s['avatar'], 'added': now(),
                        'last': {'state': s['state'], 'gameid': s['gameid'], 'game': s['game'], 'server': s['server'], 't': now()},
                        'bans': {'vac': bn['NumberOfVACBans'], 'game': bn['NumberOfGameBans'], 'community': bn['CommunityBanned'],
                                 'econ': bn['EconomyBan'], 'days': bn['DaysSinceLastBan']} if bn else None,
                        'heat': [[0, 0, 0] for _ in range(168)], 'events': []}
                    record_name(sid, s['name'])
                W(user)[sid] = {'tags': [], 'note': '', 'group': clean(b.get('group'), 30), 'alertRust': False, 'added': now()}
                save()
                return self.send(200, pub(user, sid))

        if P == '/api/untrack' and method == 'POST':
            b = self.jbody()
            with lock:
                W(user).pop(b.get('id'), None)
                drop_orphans()
                save()
            return self.send(200, {'ok': True})

        if P == '/api/meta' and method == 'POST':
            b = self.jbody()
            with lock:
                m = W(user).get(b.get('id'))
                if m is None or b.get('id') not in db['players']:
                    return self.send(404, {'error': 'Игрок не в слежке'})
                if isinstance(b.get('tags'), list):
                    tags = []
                    for x in b['tags']:
                        x = clean(x, 24)
                        if x and x not in tags:
                            tags.append(x)
                    m['tags'] = tags[:10]
                if b.get('note') is not None:
                    m['note'] = clean(b['note'], 1000)
                if b.get('group') is not None:
                    m['group'] = clean(b['group'], 30)
                if b.get('alertRust') is not None:
                    m['alertRust'] = bool(b['alertRust'])
                save()
                return self.send(200, pub(user, b['id']))

        if P == '/api/history':
            sid = Q.get('id')
            with lock:
                r = db['players'].get(sid) if sid in W(user) else None
                return self.send(200, {'heat': r['heat'] if r else None, 'events': list(r['events']) if r else [],
                                       'names': list(db['names'].get(sid, [])), 'tz': TZ})

        if P == '/api/rating':
            if MULTI and not rate_ok(('r', user['sid']), 4):
                return self.send(429, {'error': 'Рейтинг можно обновлять не чаще 4 раз в минуту.'})
            g = Q.get('group') or ''
            with lock:
                recs = [(t, m.get('group', '')) for t, m in W(user).items() if t in db['players'] and (not g or m.get('group') == g)]

            def one(item):
                d = safe(player, item[0])
                if not d:
                    return None
                return {'id': item[0], 'name': d['profile'].get('personaname'), 'avatar': d['profile'].get('avatarfull'),
                        'group': item[1], 'm': d['m'], 'public': d['isPublic']}
            return self.send(200, [x for x in pool.map(one, recs) if x])

        if P == '/api/servers':
            by = {}
            with lock:
                for t, m in W(user).items():
                    r = db['players'].get(t)
                    l = (r or {}).get('last') or {}
                    if l.get('gameid') == RUST:
                        by.setdefault(l.get('server') or '?', []).append(
                            {'id': t, 'name': r['name'], 'avatar': r['avatar'], 'group': m.get('group', '')})
            rows = [{'addr': None if a == '?' else a, 'info': None if a == '?' else server_name(a), 'players': pl}
                    for a, pl in by.items()]
            rows.sort(key=lambda x: -len(x['players']))
            return self.send(200, rows)

        if P == '/api/avatar':
            u = urllib.parse.urlsplit(Q.get('u') or '')
            host = u.hostname or ''
            if u.scheme != 'https' or not (host.endswith('steamstatic.com') or host.endswith('akamaihd.net')):
                return self.send(400, {'error': 'bad url'})
            try:
                data, hd = fetch(u.geturl())
            except Exception:
                return self.send(404, {'error': 'no avatar'})
            return self.raw(200, data, hd.get('Content-Type') or 'image/jpeg', [('Cache-Control', 'public, max-age=86400')])

        # --- Telegram ---
        if P == '/api/tg/link' and method == 'POST':
            if not MULTI:
                return self.send(400, {'error': 'В локальном режиме впишите TG_CHAT_ID в .env'})
            if not TG_TOKEN or not BOT_USERNAME:
                return self.send(400, {'error': 'Бот не настроен на сервере или ещё запускается. Попробуйте через минуту.'})
            code = secrets.token_hex(8)
            with lock:
                user['link_code'], user['link_exp'] = code, now() + 900
                save()
            return self.send(200, {'url': 'https://t.me/%s?start=%s' % (BOT_USERNAME, code)})

        if P == '/api/tg/unlink' and method == 'POST':
            with lock:
                user.pop('tg', None)
                save()
            return self.send(200, {'ok': True})

        if P == '/api/share' and method == 'POST':
            chat = user_chat(user)
            if not chat:
                return self.send(400, {'error': 'Сначала подключите Telegram во вкладке «Настройки».'})
            png = self.body()
            ok = tg_photo(chat, png, hesc(clean(Q.get('cap'), 500)))
            return self.send(200 if ok else 502, {'ok': True} if ok else {'error': 'Telegram не принял картинку. Попробуйте подключить бота заново.'})

        if P == '/api/test-telegram' and method == 'POST':
            chat = user_chat(user)
            if not chat:
                return self.send(400, {'error': 'Telegram не подключён.'})
            if tg(chat, '✅ Rust Tracker подключён к этому чату.'):
                return self.send(200, {'ok': True})
            return self.send(502, {'error': 'Telegram не ответил. Напишите боту /start и подключите заново.'})

        # --- приватность ---
        if P == '/api/optout' and method == 'POST':
            if not MULTI:
                return self.send(400, {'error': 'Доступно только в публичном режиме'})
            on = bool(self.jbody().get('on'))
            me = user['sid']
            with lock:
                if on:
                    if me not in db['optout']:
                        db['optout'].append(me)
                    for w in db['watch'].values():
                        w.pop(me, None)
                    db['players'].pop(me, None)
                    db['names'].pop(me, None)
                elif me in db['optout']:
                    db['optout'].remove(me)
                save()
            return self.send(200, {'optout': on})

        if P == '/api/delete-me' and method == 'POST':
            if not MULTI:
                return self.send(400, {'error': 'Доступно только в публичном режиме'})
            me = user['sid']
            with lock:
                db['users'].pop(me, None)
                db['watch'].pop(me, None)
                db['sessions'] = {k: v for k, v in db['sessions'].items() if v['sid'] != me}
                drop_orphans()
                save()
            return self.send(200, {'ok': True}, [self.cookie('', 0)])

        self.raw(404, b'Not found', 'text/plain')


def loop(fn, interval, delay):
    time.sleep(delay)
    while True:
        fn()
        time.sleep(interval)


def main():
    if not KEY or KEY.startswith('сюда'):
        sys.exit('Нет STEAM_API_KEY. Скопируйте .env.example в .env и вставьте ключ.')
    threading.Thread(target=lambda: [flush() or time.sleep(5) for _ in iter(int, 1)], daemon=True).start()
    for fn, iv, dl in ((poll, POLL_SEC, 3), (check_bans, BAN_MIN * 60, 15)):
        threading.Thread(target=loop, args=(fn, iv, dl), daemon=True).start()
    if TG_TOKEN and MULTI:
        threading.Thread(target=bot_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print('Rust Tracker (%s): %s' % ('публичный режим' if MULTI else 'локальный режим', PUBLIC_URL or 'http://localhost:%s' % PORT))
    print('Telegram: ' + ('бот включён' if TG_TOKEN else 'не настроен'))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        flush()


if __name__ == '__main__':
    main()
