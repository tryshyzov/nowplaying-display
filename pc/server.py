"""NowPlaying: сервер на ПК для экрана «Сейчас играет» на планшете.

Берёт трек из системной панели Windows (её используют браузеры и Яндекс Музыка),
отдаёт по локальной сети:
  GET /api/now                — что играет, позиция, цвет фона
  GET /art/<track_id>.jpg     — обложка (уменьшенная)
  GET /api/lyrics/<track_id>  — текст песни с LRCLIB (синхронный, если есть)
и отвечает на UDP-поиск, чтобы планшет сам находил ПК.
Всё хранится только в памяти.
"""
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import socket
import sys
import ssl
import time
import urllib.parse

import aiohttp
from aiohttp import web
from PIL import Image
import truststore  # проверка сертификатов через хранилище Windows (достраивает цепочки, как браузер)
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
)
from winrt.windows.storage.streams import Buffer, InputStreamOptions

HTTP_PORT = int(os.environ.get('NP_HTTP_PORT', '47801'))
UDP_PORT = int(os.environ.get('NP_UDP_PORT', '47800'))
POLL = 0.5           # секунд между опросами панели Windows
ART_SIZE = 480       # обложка для экрана 1024x600
ITUNES = 'https://itunes.apple.com/search'
LRCLIB = 'https://lrclib.net/api'
UA = 'NowPlaying-Tab3/1.0 (personal LAN display)'

log = logging.getLogger('nowplaying')
state = {'track_id': None, 'playing': False, 'title': '', 'artist': '', 'album': '', 'app': '',
         'position_ms': 0, 'duration_ms': 0, 'measured_at': 0.0, 'color': '#202020', 'text_color': '#ffffff', 'art_rev': 0}
art = {}       # track_id -> (jpeg bytes, цвет фона)
lyrics = {}    # track_id -> dict
lyrics_jobs = {}
art_attempts = {}
current = {}  # выбранная сессия Windows (для кнопок управления)
itunes_jobs = {}
itunes_cache = {}  # 'артист|название' -> url обложки или None


def now_ms():
    return time.time() * 1000


def split_title(title, artist):
    """Браузеры иногда отдают «Артист - Песня» в названии и пустого артиста."""
    if not artist and ' - ' in title:
        a, t = title.split(' - ', 1)
        return t.strip(), a.strip()
    return title, artist


def clean_title(title):
    # убираем приписки вроде «(Official Video)», счётчик уведомлений «(3)» в начале
    title = re.sub(r'^\(\d+\)\s*', '', title)
    title = re.sub(r'\s*[\(\[](official|lyric|lyrics|audio|video|mv|hd|4k)[^\)\]]*[\)\]]', '', title, flags=re.I)
    return title.strip()


JUNK = re.compile(r'tiktok|youtube shorts|#shorts', re.I)
MIN_TRACK_MS = 45000   # короче — это ролик, уведомление или реклама, а не песня


def pick_session(mgr, eligible):
    sessions = [x for x in mgr.get_sessions() if x.source_app_user_model_id in eligible]
    for s in sessions:
        if s.get_playback_info().playback_status == Status.PLAYING:
            return s
    cur = mgr.get_current_session()
    if cur is not None and cur.source_app_user_model_id in eligible:
        return cur
    return sessions[0] if sessions else None


async def read_thumbnail(ref):
    stream = await ref.open_read_async()
    size = stream.size
    if not size or size > 20 * 1024 * 1024:
        return None
    buf = Buffer(size)
    res = await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
    return bytes(memoryview(res))


def process_art(data):
    img = Image.open(io.BytesIO(data)).convert('RGB')
    # браузеры часто дают превью 16:9 с полями; вырезаем центральный квадрат
    w, h = img.size
    if w > h * 1.2:
        img = img.crop(((w - h) // 2, 0, (w + h) // 2, h))
    img.thumbnail((ART_SIZE, ART_SIZE))
    # цвет фона: средний цвет обложки, затемнённый
    r, g, b = img.resize((1, 1), Image.LANCZOS).getpixel((0, 0))
    k = 0.45
    bg = (int(r * k), int(g * k), int(b * k))
    out = io.BytesIO()
    img.save(out, 'JPEG', quality=85)
    return out.getvalue(), '#%02x%02x%02x' % bg


def set_art(tid, jpeg, color):
    for k in [k for k in art if k != tid]:
        art.pop(k)
    art[tid] = (jpeg, color)
    state['color'] = color
    state['art_rev'] += 1


def norm(text):
    text = text.lower().replace('ё', 'е')
    text = re.sub(r'[\(\[].*?[\)\]]', ' ', text)            # (feat. ...), [Remix] и т.п.
    text = re.sub(r'\b(feat|ft|featuring)\b.*', ' ', text)
    return ' '.join(re.findall(r'\w+', text))


def artists_of(text):
    return {norm(a) for a in re.split(r',|&|\bx\b|\bfeat\.?|\bft\.?|\band\b|\sи\s', text.lower()) if norm(a)}


def same_track(title, artist, cand):
    t1, t2 = norm(title), norm(cand.get('trackName', ''))
    if not t1 or not t2 or not (t1 == t2 or t1 in t2.split(' - ')[0] or t2 in t1):
        return False
    return bool(artists_of(artist) & artists_of(cand.get('artistName', '')))


async def find_cover(app, title, artist):
    """Ищет обложку: сначала iTunes (600 px), потом Deezer (1000 px, много русской музыки)."""
    http = app['http']
    first_artist = re.split(r',|&', artist)[0].strip()
    for country in ('ru', 'us'):
        params = {'term': f'{first_artist} {title}', 'entity': 'song', 'limit': '10', 'country': country}
        try:
            async with http.get(ITUNES, params=params) as r:
                found = (await r.json(content_type=None)).get('results', []) if r.status == 200 else []
        except Exception as e:
            log.warning('itunes: %s', e)
            found = []
        match = next((c for c in found if same_track(title, artist, c)), None)
        if match and match.get('artworkUrl100'):
            return match['artworkUrl100'].replace('100x100bb', '600x600bb')
    try:
        async with http.get('https://api.deezer.com/search', params={'q': f'{first_artist} {title}', 'limit': '15'}) as r:
            found = (await r.json(content_type=None)).get('data', []) if r.status == 200 else []
    except Exception as e:
        log.warning('deezer: %s', e)
        found = []
    for c in found:
        cand = {'trackName': c.get('title', ''), 'artistName': (c.get('artist') or {}).get('name', '')}
        cover = (c.get('album') or {}).get('cover_xl')
        if cover and same_track(title, artist, cand):
            return cover
    return None


async def improve_art(app, tid, title, artist):
    """Чёткая обложка 600x600 из iTunes, только если трек точно совпал."""
    key = f'{norm(artist)}|{norm(title)}'
    url = itunes_cache.get(key, '')
    try:
        if url == '':
            url = await find_cover(app, title, artist)
            itunes_cache[key] = url
            if len(itunes_cache) > 300:
                itunes_cache.pop(next(iter(itunes_cache)))
        if url:
            async with app['http'].get(url) as r:
                data = await r.read() if r.status == 200 else None
            if data and tid == state['track_id']:
                jpeg, color = await asyncio.get_running_loop().run_in_executor(None, process_art, data)
                set_art(tid, jpeg, color)
                art_attempts[tid] = 99  # больше не перезаписывать обложкой из Windows
        log.info('sharp art for %s — %s: %s', artist, title, url or 'no match')
    except Exception as e:
        log.warning('sharp art: %s', e)
    finally:
        if len(itunes_jobs) > 50:
            itunes_jobs.pop(next(iter(itunes_jobs)))


async def poll_loop(app):
    mgr = await SessionManager.request_async()
    while True:
        try:
            await update_state(app, mgr)
        except Exception as e:  # сессия могла закрыться посреди опроса
            log.debug('poll: %s', e)
        await asyncio.sleep(POLL)


async def update_state(app, mgr):
    eligible = set()
    for x in mgr.get_sessions():
        try:
            pr = await x.try_get_media_properties_async()
            t = x.get_timeline_properties()
            length = (t.end_time - t.start_time).total_seconds() * 1000 if t.end_time else 0
            if pr.title and not JUNK.search(f'{pr.title} {pr.artist}') and not (0 < length < MIN_TRACK_MS):
                eligible.add(x.source_app_user_model_id)
        except Exception:
            pass
    s = pick_session(mgr, eligible)
    current['session'] = s
    if s is None:
        state.update(track_id=None, playing=False, title='', artist='', album='', app='')
        return
    props = await s.try_get_media_properties_async()
    tl = s.get_timeline_properties()
    pb = s.get_playback_info()
    title, artist = split_title(clean_title(props.title or ''), props.artist or props.album_artist or '')
    app_id = s.source_app_user_model_id or ''
    tid = hashlib.sha1(f'{app_id}|{title}|{artist}'.encode()).hexdigest()[:12] if title else None
    playing = pb.playback_status == Status.PLAYING

    pos = tl.position.total_seconds() * 1000 if tl.position else 0
    dur = (tl.end_time - tl.start_time).total_seconds() * 1000 if tl.end_time else 0
    upd = tl.last_updated_time.timestamp() * 1000 if tl.last_updated_time else now_ms()
    if playing and 0 < upd <= now_ms():
        pos += now_ms() - upd  # Windows обновляет позицию редко — досчитываем
    if dur:
        pos = min(pos, dur)

    if tid != state['track_id']:
        log.info('track: %s — %s [%s]', artist, title, app_id)
        state.update(color=art[tid][1] if tid in art else '#202020')
        art_attempts.clear()
    # обложку пробуем несколько раз: Яндекс Музыка отдаёт её чуть позже названия
    if tid and tid not in art and props.thumbnail and art_attempts.get(tid, 0) < 6:
        art_attempts[tid] = art_attempts.get(tid, 0) + 1
        try:
            data = await read_thumbnail(props.thumbnail)
            if data:
                jpeg, color = await asyncio.get_running_loop().run_in_executor(None, process_art, data)
                if tid not in art:  # iTunes мог успеть раньше
                    set_art(tid, jpeg, color)
        except Exception as e:
            log.warning('art: %s', e)
    if tid and artist and tid not in itunes_jobs:
        itunes_jobs[tid] = asyncio.create_task(improve_art(app, tid, title, artist))
    # текст ищем, когда известны и название, и (по возможности) длительность
    if tid and tid not in lyrics and tid not in lyrics_jobs and (dur > 0 or tid == state['track_id']):
        lyrics_jobs[tid] = asyncio.create_task(fetch_lyrics(app, tid, title, artist, props.album_title or '', dur))

    state.update(track_id=tid, playing=playing, title=title, artist=artist, album=props.album_title or '',
                 app=app_id, position_ms=int(pos), duration_ms=int(dur), measured_at=now_ms())


def parse_lrc(text):
    lines = []
    for raw in (text or '').splitlines():
        stamps = re.findall(r'\[(\d+):(\d+(?:\.\d+)?)\]', raw)
        words = re.sub(r'\[[^\]]*\]', '', raw).strip()
        for m, s in stamps:
            lines.append({'t': int((int(m) * 60 + float(s)) * 1000), 'text': words})
    return sorted(lines, key=lambda l: l['t'])


async def fetch_lyrics(app, tid, title, artist, album, dur):
    """Текст из LRCLIB. Синхронный берём только если длительность совпала (±4 с),
    иначе тайминги разъедутся (ремикс, live, другая версия)."""
    result = {'synced': [], 'plain': ''}
    try:
        http = app['http']
        q = {'track_name': title, 'artist_name': artist}
        cands = []
        if artist:
            params = dict(q, album_name=album, duration=int(dur / 1000)) if dur else dict(q)
            async with http.get(f'{LRCLIB}/get', params=params) as r:
                if r.status == 200:
                    cands.append(await r.json())
        async with http.get(f'{LRCLIB}/search', params=q if artist else {'q': title}) as r:
            if r.status == 200:
                cands += await r.json()

        def diff(c):
            return abs((c.get('duration') or 0) * 1000 - dur) if dur and c.get('duration') else None

        limit_synced = 4000 if artist else 2000    # без артиста — строже
        synced = [c for c in cands if c.get('syncedLyrics') and
                  (diff(c) is None and dur == 0 and artist or diff(c) is not None and diff(c) <= limit_synced)]
        synced.sort(key=lambda c: diff(c) or 0)
        plain = [c for c in cands if c.get('plainLyrics') and (artist or (diff(c) is not None and diff(c) <= 5000))]
        plain.sort(key=lambda c: diff(c) if diff(c) is not None else 10 ** 9)
        if synced:
            result = {'synced': parse_lrc(synced[0]['syncedLyrics']), 'plain': synced[0].get('plainLyrics') or ''}
        elif plain:
            result = {'synced': [], 'plain': plain[0]['plainLyrics']}
    except Exception as e:
        log.warning('lyrics: %s', e)
    log.info('lyrics for %s — %s: synced %d, plain %d', artist, title, len(result['synced']), len(result['plain']))
    lyrics[tid] = result
    lyrics_jobs.pop(tid, None)
    if len(lyrics) > 50:
        lyrics.pop(next(iter(lyrics)))


def jsonr(data):
    return web.Response(text=json.dumps(data, ensure_ascii=False), content_type='application/json', charset='utf-8')


async def h_now(request):
    s = dict(state)
    if s['playing'] and s['duration_ms']:
        s['position_ms'] = int(min(s['position_ms'] + now_ms() - s['measured_at'], s['duration_ms']))
    s.pop('measured_at')
    s['server_time_ms'] = int(now_ms())
    s['has_art'] = s['track_id'] in art
    s['lyrics_ready'] = s['track_id'] in lyrics
    return jsonr(s)


async def h_control(request):
    s = current.get('session')
    if s is None:
        raise web.HTTPNotFound()
    action = request.match_info['action']
    ok = await {'toggle': s.try_toggle_play_pause_async, 'next': s.try_skip_next_async,
                'prev': s.try_skip_previous_async}[action]()
    return jsonr({'ok': bool(ok)})


async def h_art(request):
    item = art.get(request.match_info['id'])
    if not item:
        raise web.HTTPNotFound()
    return web.Response(body=item[0], content_type='image/jpeg', headers={'Cache-Control': 'max-age=3600'})


async def h_lyrics(request):
    tid = request.match_info['id']
    if tid in lyrics_jobs:
        try:
            await asyncio.wait_for(asyncio.shield(lyrics_jobs[tid]), 10)
        except asyncio.TimeoutError:
            pass
    return jsonr(lyrics.get(tid) or {'synced': [], 'plain': '', 'pending': tid in lyrics_jobs})


class Discovery(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.strip() == b'NOWPLAYING_DISCOVER':
            reply = f'NOWPLAYING_HERE {HTTP_PORT} {socket.gethostname()}'.encode()
            self.transport.sendto(reply, addr)


async def on_startup(app):
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    app['http'] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers={'User-Agent': UA},
                                        connector=aiohttp.TCPConnector(ssl=ctx))
    app['poll'] = asyncio.create_task(poll_loop(app))
    loop = asyncio.get_running_loop()
    app['udp'], _ = await loop.create_datagram_endpoint(Discovery, local_addr=('0.0.0.0', UDP_PORT))


async def on_cleanup(app):
    app['poll'].cancel()
    app['udp'].close()
    await app['http'].close()


def setup_logging(log_dir=None):
    fmt = '%(asctime)s %(message)s'
    if log_dir or sys.stderr is None:  # без консоли (pythonw, exe) — пишем в файл до 1 МБ
        from logging.handlers import RotatingFileHandler
        path = os.path.join(log_dir or os.path.dirname(os.path.abspath(__file__)), 'server.log')
        logging.basicConfig(level=logging.INFO, format=fmt,
                            handlers=[RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1, encoding='utf-8')])
        return path
    logging.basicConfig(level=logging.INFO, format=fmt)
    return None


def create_app():
    app = web.Application()
    app.add_routes([
        web.get('/api/now', h_now),
        web.get(r'/art/{id:[0-9a-f]{12}}.jpg', h_art),
        web.get(r'/api/lyrics/{id:[0-9a-f]{12}}', h_lyrics),
        web.get(r'/api/control/{action:toggle|next|prev}', h_control),
        web.get('/health', lambda r: web.Response(text='ok')),
    ])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    setup_logging()
    web.run_app(create_app(), host='0.0.0.0', port=HTTP_PORT, access_log=None, print=None)


if __name__ == '__main__':
    main()
