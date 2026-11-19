#!/usr/bin/env python3
"""Rust Tracker на Python. Только стандартная библиотека, нужен Python 3.9+.
Интерфейс (public/index.html), файл .env и база data/db.json общие с версией на Node."""
import atexit, base64, hmac, json, os, re, sys, threading, time, uuid
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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
TG_TOKEN, TG_CHAT = E.get('TG_BOT_TOKEN', ''), E.get('TG_CHAT_ID', '')
PASS = E.get('ACCESS_PASSWORD', '')
POLL_SEC = num('POLL_SEC', 60, 20)
BAN_MIN = num('BAN_CHECK_MIN', 30, 5)
TZ = E.get('TZ_NAME') or 'Europe/Warsaw'
RUST = '252490'
BASE = 76561197960265728
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
DB_FILE = os.path.join(HERE, 'data', 'db.json')
db = {'players': {}, 'names': {}}
lock = threading.RLock()
dirty = False
try:
    with open(DB_FILE, encoding='utf-8') as f:
        db.update(json.load(f))
except (OSError, ValueError):
    pass


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
tg_on = lambda: bool(TG_TOKEN and TG_CHAT)


def tg(text):
    if not tg_on():
        return False
    try:
        fetch('https://api.telegram.org/bot%s/sendMessage' % TG_TOKEN,
              data=json.dumps({'chat_id': TG_CHAT, 'text': text, 'parse_mode': 'HTML',
                               'disable_web_page_preview': True}).encode(),
              headers={'Content-Type': 'application/json'})
        return True
    except Exception:
        return False


def tg_photo(png, caption):
    if not tg_on():
        return False
    b = uuid.uuid4().hex
    parts = []
    for k, v in (('chat_id', TG_CHAT), ('caption', caption), ('parse_mode', 'HTML')):
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


def pub(r):
    return {k: r.get(k) for k in ('id', 'name', 'avatar', 'tags', 'note', 'group', 'alertRust', 'last', 'bans', 'added')}


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
                            if rec.get('alertRust'):
                                alerts.append((p['name'], p['server'], rec['id']))
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
        for name, srv, sid in alerts:
            sn = server_name(srv)
            tg('🎮 <b>%s</b> зашёл в Rust' % hesc(name) +
               ('\nСервер: ' + hesc(sn['name']) if sn else '\nСервер: ' + hesc(srv) if srv else '') + '\n' + plink(sid))
    except Exception as e:
        print('poll:', e, file=sys.stderr)
    finally:
        poll_lock.release()


def check_bans():
    try:
        with lock:
            ids = list(db['players'])
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
                        tg('🚨 <b>%s</b>: новый бан\nVAC: %s, игровых: %s%s\n%s' % (
                            hesc(rec['name']), nb['vac'], nb['game'], ', бан сообщества' if nb['community'] else '', plink(rec['id'])))
                    rec['bans'] = nb
            save()
    except Exception as e:
        print('bans:', e, file=sys.stderr)


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def raw(self, code, data, ctype, extra=()):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

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

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def route(self, method):
        u = urllib.parse.urlsplit(self.path)
        P, Q = u.path, dict(urllib.parse.parse_qsl(u.query))
        try:
            if PASS:
                h = self.headers.get('Authorization', '')
                pw = ''
                if h.startswith('Basic '):
                    try:
                        pw = base64.b64decode(h[6:]).decode('utf-8').partition(':')[2]
                    except Exception:
                        pass
                if not hmac.compare_digest(pw, PASS):
                    return self.raw(401, 'Нужен пароль'.encode('utf-8'), 'text/plain; charset=utf-8',
                                    [('WWW-Authenticate', 'Basic realm="Rust Tracker"')])
            if method == 'POST' and self.headers.get('x-rt') != '1':
                return self.send(403, {'error': 'forbidden'})
            self.handle_api(method, P, Q)
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

    def handle_api(self, method, P, Q):
        if P in ('/', '/index.html'):
            with open(os.path.join(HERE, 'public', 'index.html'), 'rb') as f:
                return self.raw(200, f.read(), 'text/html; charset=utf-8')

        if P == '/api/config':
            return self.send(200, {'telegram': tg_on(), 'tz': TZ, 'pollSec': POLL_SEC, 'banMin': BAN_MIN, 'locked': bool(PASS)})

        if P == '/api/player':
            sid = resolve(Q.get('q'))
            if not sid:
                return self.send(404, {'error': 'Профиль не найден. Проверьте SteamID или ссылку.'})
            d = player(sid)
            if not d:
                return self.send(404, {'error': 'Такого аккаунта нет.'})
            record_name(sid, d['profile'].get('personaname'))
            with lock:
                d['names'] = list(db['names'].get(sid, []))
                d['tracked'] = pub(db['players'][sid]) if sid in db['players'] else None
            d['serverInfo'] = server_name(d['profile'].get('gameserverip')) if d['profile'].get('gameid') == RUST else None
            return self.send(200, d)

        if P == '/api/tracked':
            with lock:
                recs = [pub(r) for r in db['players'].values()]

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
                if sid in db['players']:
                    return self.send(200, pub(db['players'][sid]))
                if len(db['players']) >= 300:
                    return self.send(400, {'error': 'Лимит слежки: 300 игроков'})
            lst = status([sid], 0)
            if not lst:
                return self.send(404, {'error': 'Аккаунт не найден'})
            s = lst[0]
            bn = dig(safe(steam, 'ISteamUser/GetPlayerBans/v1/', {'steamids': sid}, 0), 'players', 0)
            with lock:
                db['players'][sid] = {
                    'id': sid, 'name': s['name'], 'avatar': s['avatar'], 'tags': [], 'note': '',
                    'group': clean(b.get('group'), 30), 'alertRust': False, 'added': now(),
                    'last': {'state': s['state'], 'gameid': s['gameid'], 'game': s['game'], 'server': s['server'], 't': now()},
                    'bans': {'vac': bn['NumberOfVACBans'], 'game': bn['NumberOfGameBans'], 'community': bn['CommunityBanned'],
                             'econ': bn['EconomyBan'], 'days': bn['DaysSinceLastBan']} if bn else None,
                    'heat': [[0, 0, 0] for _ in range(168)], 'events': [],
                }
                record_name(sid, s['name'])
                save()
                return self.send(200, pub(db['players'][sid]))

        if P == '/api/untrack' and method == 'POST':
            b = self.jbody()
            with lock:
                db['players'].pop(b.get('id'), None)
                save()
            return self.send(200, {'ok': True})

        if P == '/api/meta' and method == 'POST':
            b = self.jbody()
            with lock:
                r = db['players'].get(b.get('id'))
                if not r:
                    return self.send(404, {'error': 'Игрок не в слежке'})
                if isinstance(b.get('tags'), list):
                    tags = []
                    for x in b['tags']:
                        x = clean(x, 24)
                        if x and x not in tags:
                            tags.append(x)
                    r['tags'] = tags[:10]
                if b.get('note') is not None:
                    r['note'] = clean(b['note'], 1000)
                if b.get('group') is not None:
                    r['group'] = clean(b['group'], 30)
                if b.get('alertRust') is not None:
                    r['alertRust'] = bool(b['alertRust'])
                save()
                return self.send(200, pub(r))

        if P == '/api/history':
            sid = Q.get('id')
            with lock:
                r = db['players'].get(sid)
                return self.send(200, {'heat': r['heat'] if r else None, 'events': list(r['events']) if r else [],
                                       'names': list(db['names'].get(sid, [])), 'tz': TZ})

        if P == '/api/rating':
            g = Q.get('group') or ''
            with lock:
                recs = [(r['id'], r.get('group', '')) for r in db['players'].values() if not g or r.get('group') == g]

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
                for r in db['players'].values():
                    l = r.get('last') or {}
                    if l.get('gameid') == RUST:
                        by.setdefault(l.get('server') or '?', []).append(
                            {'id': r['id'], 'name': r['name'], 'avatar': r['avatar'], 'group': r.get('group', '')})
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

        if P == '/api/share' and method == 'POST':
            if not tg_on():
                return self.send(400, {'error': 'Telegram не настроен: задайте TG_BOT_TOKEN и TG_CHAT_ID в .env'})
            png = self.body()
            ok = tg_photo(png, hesc(clean(Q.get('cap'), 500)))
            return self.send(200 if ok else 502, {'ok': True} if ok else {'error': 'Telegram не принял картинку. Проверьте токен и chat id.'})

        if P == '/api/test-telegram' and method == 'POST':
            ok = tg('✅ Rust Tracker подключён к этому чату.')
            if ok:
                return self.send(200, {'ok': True})
            return self.send(502, {'error': 'Telegram не ответил. Напишите боту /start и проверьте chat id.' if tg_on()
                                   else 'Не заданы TG_BOT_TOKEN и TG_CHAT_ID.'})

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
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    srv.daemon_threads = True
    print('Rust Tracker: http://localhost:%s%s' % (PORT, '  (Telegram подключён)' if tg_on() else '  (Telegram не настроен)'))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        flush()


if __name__ == '__main__':
    main()
