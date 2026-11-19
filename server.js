// Rust Tracker: без зависимостей, нужен только Node 18+.
const http = require('http');
const fs = require('fs');
const path = require('path');

try { // мини-загрузчик .env
  for (const l of fs.readFileSync(path.join(__dirname, '.env'), 'utf8').split('\n')) {
    const m = l.match(/^\s*([A-Z_]+)\s*=\s*(.*?)\s*$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/\s+#.*$/, '').trim().replace(/^["']|["']$/g, '');
  }
} catch {}

const E = process.env;
const KEY = E.STEAM_API_KEY;
const PORT = E.PORT || 3000;
const TG_TOKEN = E.TG_BOT_TOKEN, TG_CHAT = E.TG_CHAT_ID;
const PASS = E.ACCESS_PASSWORD || '';
const POLL_SEC = Math.max(20, +E.POLL_SEC || 60);
const BAN_MIN = Math.max(5, +E.BAN_CHECK_MIN || 30);
const TZ = E.TZ_NAME || 'Europe/Warsaw';
const RUST = 252490, RUST_S = '252490';
const BASE = 76561197960265728n;

if (!KEY || KEY.startsWith('сюда')) {
  console.error('Нет STEAM_API_KEY. Скопируйте .env.example в .env и вставьте ключ.');
  process.exit(1);
}

const now = () => Math.floor(Date.now() / 1000);
const isId = s => /^\d{17}$/.test(s || '');
const hesc = s => String(s ?? '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const link = id => 'https://steamcommunity.com/profiles/' + id;

/* ---------------- хранилище (JSON-файл) ---------------- */
const DB_FILE = path.join(__dirname, 'data', 'db.json');
let db = { players: {}, names: {} };
try { db = Object.assign(db, JSON.parse(fs.readFileSync(DB_FILE, 'utf8'))); } catch {}
let dirty = false;
const save = () => { dirty = true; };
function flush() {
  if (!dirty) return;
  dirty = false;
  fs.mkdirSync(path.dirname(DB_FILE), { recursive: true });
  fs.writeFileSync(DB_FILE + '.tmp', JSON.stringify(db));
  fs.renameSync(DB_FILE + '.tmp', DB_FILE);
}
setInterval(flush, 5000);
for (const s of ['SIGINT', 'SIGTERM']) process.on(s, () => { flush(); process.exit(0); });

/* ---------------- Steam API ---------------- */
const cache = new Map();
async function steam(p, params, ttl = 30000) {
  const u = new URL('https://api.steampowered.com/' + p);
  u.searchParams.set('key', KEY);
  for (const [k, v] of Object.entries(params)) u.searchParams.set(k, v);
  const ck = u.toString();
  const hit = cache.get(ck);
  if (hit && hit.t > Date.now()) return hit.v;
  const r = await fetch(u);
  if (!r.ok) { const e = new Error('Steam API ' + r.status); e.status = r.status; throw e; }
  const v = await r.json();
  if (ttl > 0) cache.set(ck, { v, t: Date.now() + ttl });
  if (cache.size > 2000) cache.clear();
  return v;
}

async function resolve(q) {
  q = (q || '').trim();
  let m;
  if (isId(q)) return q;
  if ((m = q.match(/^STEAM_[0-5]:([01]):(\d+)$/i))) return String(BASE + BigInt(m[2]) * 2n + BigInt(m[1]));
  if ((m = q.match(/^\[U:1:(\d+)\]$/i))) return String(BASE + BigInt(m[1]));
  if ((m = q.match(/steamcommunity\.com\/profiles\/(\d{17})/))) return m[1];
  if ((m = q.match(/steamcommunity\.com\/id\/([^\/?#\s]+)/))) q = m[1];
  if (!/^[\w-]{2,32}$/.test(q)) return null;
  const d = await steam('ISteamUser/ResolveVanityURL/v1/', { vanityurl: q }, 300000);
  return d.response.success === 1 ? d.response.steamid : null;
}

const val = r => (r.status === 'fulfilled' ? r.value : null);

async function player(id) {
  const [sum, bans, owned, recent, level, friends, stats] = await Promise.allSettled([
    steam('ISteamUser/GetPlayerSummaries/v2/', { steamids: id }),
    steam('ISteamUser/GetPlayerBans/v1/', { steamids: id }, 300000),
    steam('IPlayerService/GetOwnedGames/v1/', {
      input_json: JSON.stringify({ steamid: id, include_played_free_games: true, appids_filter: [RUST] })
    }, 120000),
    steam('IPlayerService/GetRecentlyPlayedGames/v1/', { steamid: id, count: 6 }, 120000),
    steam('IPlayerService/GetSteamLevel/v1/', { steamid: id }, 300000),
    steam('ISteamUser/GetFriendList/v1/', { steamid: id, relationship: 'friend' }, 300000),
    steam('ISteamUserStats/GetUserStatsForGame/v2/', { steamid: id, appid: RUST }, 120000)
  ]);
  const p = val(sum)?.response?.players?.[0];
  if (!p) return null;
  const rustGame = val(owned)?.response?.games?.[0] || null;
  const st = {};
  for (const s of val(stats)?.playerstats?.stats || []) st[s.name] = s.value;
  const kills = st.kill_player ?? null, deaths = st.deaths ?? st.death_player ?? null;
  const ach = val(stats)?.playerstats?.achievements?.filter(a => a.achieved).length ?? null;
  const lvl = val(level)?.response?.player_level ?? null;
  return {
    profile: p,
    isPublic: p.communityvisibilitystate === 3,
    bans: val(bans)?.players?.[0] || null,
    rust: {
      owned: !!rustGame,
      minutesTotal: rustGame?.playtime_forever ?? null,
      minutes2w: rustGame?.playtime_2weeks ?? null,
      lastPlayed: rustGame?.rtime_last_played ?? null,
      statsVisible: Object.keys(st).length > 0
    },
    m: { // сводные цифры для карточек, сравнения и рейтинга
      hours: rustGame ? Math.round(rustGame.playtime_forever / 60) : null,
      hours2w: rustGame?.playtime_2weeks != null ? Math.round(rustGame.playtime_2weeks / 60) : null,
      kills, deaths,
      kd: kills != null && deaths ? +(kills / deaths).toFixed(2) : null,
      acc: st.bullet_fired ? +(((st.bullet_hit_player || 0) / st.bullet_fired) * 100).toFixed(1) : null,
      hs: st.headshot ?? null, fired: st.bullet_fired ?? null,
      wood: st['harvest.wood'] ?? null, stone: st['harvest.stones'] ?? null,
      ach, level: lvl
    },
    recent: val(recent)?.response?.games || [],
    level: lvl,
    friends: val(friends)?.friendslist?.friends?.length ?? null
  };
}

async function status(ids, ttl = 20000) {
  const d = await steam('ISteamUser/GetPlayerSummaries/v2/', { steamids: ids.join(',') }, ttl);
  return (d.response.players || []).map(p => ({
    steamid: p.steamid, name: p.personaname, avatar: p.avatarfull,
    state: p.personastate, gameid: p.gameid || null, game: p.gameextrainfo || null,
    server: p.gameserverip || null, lastlogoff: p.lastlogoff || null
  }));
}

/* ---------------- BattleMetrics: адрес сервера -> название ---------------- */
const bm = new Map();
async function serverName(addr) {
  if (!addr || !/^[\d.]+:\d+$/.test(addr)) return null;
  const c = bm.get(addr);
  if (c && c.t > Date.now()) return c.v;
  let v = null;
  try {
    const [ip, port] = addr.split(':');
    const u = new URL('https://api.battlemetrics.com/servers');
    u.searchParams.set('filter[game]', 'rust');
    u.searchParams.set('filter[search]', ip);
    u.searchParams.set('page[size]', '25');
    const r = await fetch(u, { headers: { 'user-agent': 'rust-tracker' } });
    if (r.ok) {
      const list = (await r.json()).data || [];
      const pn = +port;
      const s = list.find(x => x.attributes.port === pn) || list.find(x => x.attributes.port + 1 === pn || x.attributes.port === pn + 1);
      if (s) v = { name: s.attributes.name, players: s.attributes.players, max: s.attributes.maxPlayers, id: s.id };
    }
  } catch {}
  bm.set(addr, { v, t: Date.now() + (v ? 600000 : 120000) });
  return v;
}

/* ---------------- Telegram ---------------- */
const tgOn = () => !!(TG_TOKEN && TG_CHAT);
async function tg(text) {
  if (!tgOn()) return false;
  try {
    const r = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: TG_CHAT, text, parse_mode: 'HTML', disable_web_page_preview: true })
    });
    return r.ok;
  } catch { return false; }
}
async function tgPhoto(buf, caption) {
  if (!tgOn()) return false;
  try {
    const fd = new FormData();
    fd.append('chat_id', TG_CHAT);
    fd.append('caption', caption);
    fd.append('parse_mode', 'HTML');
    fd.append('photo', new Blob([buf], { type: 'image/png' }), 'card.png');
    const r = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendPhoto`, { method: 'POST', body: fd });
    return r.ok;
  } catch { return false; }
}

/* ---------------- история, ники, баны ---------------- */
const fmtSlot = new Intl.DateTimeFormat('en-US', { timeZone: TZ, weekday: 'short', hour: '2-digit', hour12: false });
const WD = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
function slot(ts) {
  const parts = fmtSlot.formatToParts(new Date(ts * 1000));
  const wd = parts.find(x => x.type === 'weekday').value;
  const h = parseInt(parts.find(x => x.type === 'hour').value, 10) % 24;
  return WD.indexOf(wd) * 24 + h;
}

function recordName(id, name) {
  if (!name) return;
  const a = db.names[id] || (db.names[id] = []);
  if (!a.length || a[a.length - 1].name !== name) {
    a.push({ name, t: now() });
    if (a.length > 50) a.shift();
    save();
  }
}
function ev(rec, type, extra = {}) {
  rec.events.push({ t: now(), type, ...extra });
  if (rec.events.length > 300) rec.events.splice(0, rec.events.length - 300);
  save();
}
const pub = r => ({
  id: r.id, name: r.name, avatar: r.avatar, tags: r.tags, note: r.note, group: r.group,
  alertRust: r.alertRust, last: r.last, bans: r.bans, added: r.added
});

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const ids = Object.keys(db.players);
    for (let i = 0; i < ids.length; i += 100) {
      const list = await status(ids.slice(i, i + 100), 0);
      const t = now();
      for (const p of list) {
        const rec = db.players[p.steamid];
        if (!rec) continue;
        rec.name = p.name; rec.avatar = p.avatar;
        recordName(rec.id, p.name);
        const prev = rec.last;
        const wasOn = prev && (prev.state > 0 || prev.gameid);
        const isOn = p.state > 0 || p.gameid;
        const rustNow = p.gameid === RUST_S;
        if (prev) {
          if (!wasOn && isOn) ev(rec, 'online');
          if (wasOn && !isOn) ev(rec, 'offline');
          if (prev.gameid !== RUST_S && rustNow) {
            ev(rec, 'rust_start', { server: p.server });
            if (rec.alertRust) {
              const sn = await serverName(p.server);
              tg(`🎮 <b>${hesc(p.name)}</b> зашёл в Rust` + (sn ? `\nСервер: ${hesc(sn.name)}` : p.server ? `\nСервер: ${hesc(p.server)}` : '') + `\n${link(rec.id)}`);
            }
          } else if (prev.gameid === RUST_S && !rustNow) ev(rec, 'rust_stop');
          else if (rustNow && prev.server !== p.server) ev(rec, 'server', { server: p.server });
        }
        rec.last = { state: p.state, gameid: p.gameid, game: p.game, server: p.server, t };
        const cell = rec.heat[slot(t)];
        cell[0]++; if (isOn) cell[1]++; if (rustNow) cell[2]++;
      }
      save();
    }
  } catch (e) { console.error('poll:', e.message); }
  polling = false;
}

async function checkBans() {
  try {
    const ids = Object.keys(db.players);
    for (let i = 0; i < ids.length; i += 100) {
      const d = await steam('ISteamUser/GetPlayerBans/v1/', { steamids: ids.slice(i, i + 100).join(',') }, 0);
      for (const b of d.players || []) {
        const rec = db.players[b.SteamId];
        if (!rec) continue;
        const nb = { vac: b.NumberOfVACBans, game: b.NumberOfGameBans, community: b.CommunityBanned, econ: b.EconomyBan, days: b.DaysSinceLastBan };
        const o = rec.bans;
        if (o && (nb.vac > o.vac || nb.game > o.game || (nb.community && !o.community))) {
          ev(rec, 'ban', { vac: nb.vac, game: nb.game });
          tg(`🚨 <b>${hesc(rec.name)}</b>: новый бан\nVAC: ${nb.vac}, игровых: ${nb.game}${nb.community ? ', бан сообщества' : ''}\n${link(rec.id)}`);
        }
        rec.bans = nb;
      }
      save();
    }
  } catch (e) { console.error('bans:', e.message); }
}

/* ---------------- HTTP ---------------- */
const send = (res, code, obj) => {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(obj));
};
const body = (req, max = 3e6) => new Promise((ok, no) => {
  const c = []; let n = 0;
  req.on('data', d => { n += d.length; if (n > max) { no(new Error('Слишком большой запрос')); req.destroy(); } else c.push(d); });
  req.on('end', () => ok(Buffer.concat(c)));
  req.on('error', no);
});
const json = async req => { try { return JSON.parse((await body(req, 1e5)).toString() || '{}'); } catch { return {}; } };
const clean = (s, n) => String(s ?? '').replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, n);

http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const P = url.pathname, Q = url.searchParams;
  try {
    if (PASS) {
      const h = req.headers.authorization || '';
      const ok = h.startsWith('Basic ') && Buffer.from(h.slice(6), 'base64').toString().split(':').slice(1).join(':') === PASS;
      if (!ok) { res.writeHead(401, { 'WWW-Authenticate': 'Basic realm="Rust Tracker"' }); return res.end('Нужен пароль'); }
    }
    if (req.method === 'POST' && req.headers['x-rt'] !== '1') return send(res, 403, { error: 'forbidden' });

    if (P === '/' || P === '/index.html') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(fs.readFileSync(path.join(__dirname, 'public', 'index.html')));
    }

    if (P === '/api/config') return send(res, 200, { telegram: tgOn(), tz: TZ, pollSec: POLL_SEC, banMin: BAN_MIN, locked: !!PASS });

    if (P === '/api/player') {
      const id = await resolve(Q.get('q'));
      if (!id) return send(res, 404, { error: 'Профиль не найден. Проверьте SteamID или ссылку.' });
      const d = await player(id);
      if (!d) return send(res, 404, { error: 'Такого аккаунта нет.' });
      recordName(id, d.profile.personaname);
      d.names = db.names[id] || [];
      d.tracked = db.players[id] ? pub(db.players[id]) : null;
      d.serverInfo = d.profile.gameid === RUST_S ? await serverName(d.profile.gameserverip) : null;
      return send(res, 200, d);
    }

    if (P === '/api/tracked') {
      const out = await Promise.all(Object.values(db.players).map(async r => {
        const o = pub(r);
        o.serverName = r.last?.gameid === RUST_S ? (await serverName(r.last.server))?.name || null : null;
        return o;
      }));
      return send(res, 200, out);
    }

    if (P === '/api/track' && req.method === 'POST') {
      const b = await json(req);
      if (!isId(b.id)) return send(res, 400, { error: 'Некорректный SteamID' });
      if (db.players[b.id]) return send(res, 200, pub(db.players[b.id]));
      if (Object.keys(db.players).length >= 300) return send(res, 400, { error: 'Лимит слежки: 300 игроков' });
      const [s] = await status([b.id], 0);
      if (!s) return send(res, 404, { error: 'Аккаунт не найден' });
      const bn = (await steam('ISteamUser/GetPlayerBans/v1/', { steamids: b.id }, 0).catch(() => null))?.players?.[0];
      db.players[b.id] = {
        id: b.id, name: s.name, avatar: s.avatar, tags: [], note: '', group: clean(b.group, 30), alertRust: false, added: now(),
        last: { state: s.state, gameid: s.gameid, game: s.game, server: s.server, t: now() },
        bans: bn ? { vac: bn.NumberOfVACBans, game: bn.NumberOfGameBans, community: bn.CommunityBanned, econ: bn.EconomyBan, days: bn.DaysSinceLastBan } : null,
        heat: Array.from({ length: 168 }, () => [0, 0, 0]), events: []
      };
      recordName(b.id, s.name);
      save();
      return send(res, 200, pub(db.players[b.id]));
    }

    if (P === '/api/untrack' && req.method === 'POST') {
      const b = await json(req);
      delete db.players[b.id]; save();
      return send(res, 200, { ok: true });
    }

    if (P === '/api/meta' && req.method === 'POST') {
      const b = await json(req);
      const r = db.players[b.id];
      if (!r) return send(res, 404, { error: 'Игрок не в слежке' });
      if (Array.isArray(b.tags)) r.tags = [...new Set(b.tags.map(x => clean(x, 24)).filter(Boolean))].slice(0, 10);
      if (b.note != null) r.note = clean(b.note, 1000);
      if (b.group != null) r.group = clean(b.group, 30);
      if (b.alertRust != null) r.alertRust = !!b.alertRust;
      save();
      return send(res, 200, pub(r));
    }

    if (P === '/api/history') {
      const id = Q.get('id'), r = db.players[id];
      return send(res, 200, { heat: r?.heat || null, events: r?.events || [], names: db.names[id] || [], tz: TZ });
    }

    if (P === '/api/rating') {
      const g = Q.get('group') || '';
      const ids = Object.values(db.players).filter(r => !g || r.group === g).map(r => r.id);
      const rows = [];
      for (let i = 0; i < ids.length; i += 5) {
        await Promise.all(ids.slice(i, i + 5).map(async id => {
          const d = await player(id).catch(() => null);
          if (d) rows.push({ id, name: d.profile.personaname, avatar: d.profile.avatarfull, group: db.players[id].group, m: d.m, public: d.isPublic });
        }));
      }
      return send(res, 200, rows);
    }

    if (P === '/api/servers') {
      const by = {};
      for (const r of Object.values(db.players)) {
        if (r.last?.gameid === RUST_S) (by[r.last.server || '?'] ||= []).push({ id: r.id, name: r.name, avatar: r.avatar, group: r.group });
      }
      const rows = await Promise.all(Object.entries(by).map(async ([addr, players]) => ({
        addr: addr === '?' ? null : addr, info: addr === '?' ? null : await serverName(addr), players
      })));
      rows.sort((a, b) => b.players.length - a.players.length);
      return send(res, 200, rows);
    }

    if (P === '/api/avatar') {
      const u = new URL(Q.get('u') || '');
      if (u.protocol !== 'https:' || !/(steamstatic\.com|akamaihd\.net)$/.test(u.hostname)) return send(res, 400, { error: 'bad url' });
      const r = await fetch(u);
      if (!r.ok) return send(res, 404, { error: 'no avatar' });
      res.writeHead(200, { 'Content-Type': r.headers.get('content-type') || 'image/jpeg', 'Cache-Control': 'public, max-age=86400' });
      return res.end(Buffer.from(await r.arrayBuffer()));
    }

    if (P === '/api/share' && req.method === 'POST') {
      if (!tgOn()) return send(res, 400, { error: 'Telegram не настроен: задайте TG_BOT_TOKEN и TG_CHAT_ID в .env' });
      const png = await body(req, 3e6);
      const cap = hesc(clean(Q.get('cap'), 500));
      const ok = await tgPhoto(png, cap);
      return send(res, ok ? 200 : 502, ok ? { ok: true } : { error: 'Telegram не принял картинку. Проверьте токен и chat id.' });
    }

    if (P === '/api/test-telegram' && req.method === 'POST') {
      const ok = await tg('✅ Rust Tracker подключён к этому чату.');
      return send(res, ok ? 200 : 502, ok ? { ok: true } : { error: tgOn() ? 'Telegram не ответил. Напишите боту /start и проверьте chat id.' : 'Не заданы TG_BOT_TOKEN и TG_CHAT_ID.' });
    }

    res.writeHead(404).end('Not found');
  } catch (e) {
    send(res, e.status === 429 ? 429 : 502, { error: e.status === 429 ? 'Steam ограничил запросы, подождите минуту.' : 'Ошибка: ' + e.message });
  }
}).listen(PORT, () => {
  console.log('Rust Tracker: http://localhost:' + PORT + (tgOn() ? '  (Telegram подключён)' : '  (Telegram не настроен)'));
  setTimeout(poll, 3000); setInterval(poll, POLL_SEC * 1000);
  setTimeout(checkBans, 15000); setInterval(checkBans, BAN_MIN * 60000);
});
