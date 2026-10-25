"""Видео на фон экрана трека: видеошоты Яндекс Музыки (короткие нарезки из клипа).

Трек ищется в открытом поиске Яндекс Музыки (без входа в аккаунт). Если у найденного
трека есть видеошот (backgroundVideoUri), сервер скачивает его и перекодирует под
Android 4.2: кадр под экран 16:9 (видео обрезается по центру), размытие, без звука,
H.264 baseline. Готовые файлы лежат в %LOCALAPPDATA%\\NowPlaying\\clips (последние 20).
"""
import asyncio
import logging
import os
import re
import subprocess

import aiohttp

from background import NO_WINDOW, ffmpeg_exe

log = logging.getLogger('nowplaying')

YM_API = 'https://api.music.yandex.net'
KEEP = 20


def norm(s):
    s = (s or '').lower().replace('ё', 'е')
    s = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', s)   # (feat. …), [Explicit] и т. п.
    return re.sub(r'[^\w]+', ' ', s).strip()


class Clips:
    def __init__(self, cache_dir=None):
        local = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        self.cache_dir = cache_dir or os.path.join(local, 'NowPlaying', 'clips')
        self.enabled = True
        self.ready = {}     # track_id -> путь к готовому mp4
        self.missing = set()  # у этих треков видео нет — не искать снова
        self.jobs = {}
        os.makedirs(self.cache_dir, exist_ok=True)
        for name in os.listdir(self.cache_dir):
            if re.fullmatch(r'[0-9a-f]{12}\.mp4', name):
                self.ready[name[:12]] = os.path.join(self.cache_dir, name)
            else:  # недокачанное с прошлого запуска
                try:
                    os.remove(os.path.join(self.cache_dir, name))
                except OSError:
                    pass

    def info(self, tid):
        return tid if self.enabled and tid in self.ready else None

    def request(self, http, tid, title, artist, dur_ms):
        """Запустить поиск видео для трека (один раз на трек)."""
        if not self.enabled or not tid or not title or tid in self.ready or tid in self.missing or tid in self.jobs:
            return
        self.jobs[tid] = asyncio.create_task(self._job(http, tid, title, artist, dur_ms))

    async def _job(self, http, tid, title, artist, dur_ms):
        try:
            url = await self.find(http, title, artist, dur_ms)
            if not url:
                log.info('clip for %s — %s: none', artist, title)
                self.missing.add(tid)
                return
            path = await self.fetch(http, tid, url)
            if path:
                self.ready[tid] = path
                self.cleanup()
                log.info('clip for %s — %s: ready', artist, title)
            else:
                self.missing.add(tid)
        except Exception as e:
            log.warning('clip: %s', e)  # сеть: в следующий раз попробуем снова
        finally:
            self.jobs.pop(tid, None)

    async def find(self, http, title, artist, dur_ms):
        params = {'text': f'{artist} {title}'.strip(), 'type': 'track', 'page': '0'}
        data = None
        for attempt in range(2):  # Яндекс иногда отвечает пустотой на первый запрос
            async with http.get(f'{YM_API}/search', params=params) as r:
                if r.status == 200:
                    try:
                        data = await r.json(content_type=None)
                        break
                    except ValueError:
                        pass
            await asyncio.sleep(1)
        tracks = (((data or {}).get('result') or {}).get('tracks') or {}).get('results') or []
        want_title = norm(title)
        want_artists = {norm(a) for a in re.split(r',|&| feat\.? | ft\.? ', artist or '', flags=re.I) if a.strip()}
        for t in tracks:
            if not t.get('backgroundVideoUri'):
                continue
            if norm(t.get('title')) != want_title:
                continue
            names = {norm(a.get('name')) for a in t.get('artists') or []}
            if want_artists and not (names & want_artists):
                continue
            if dur_ms and t.get('durationMs') and abs(t['durationMs'] - dur_ms) > 6000:
                continue
            return t['backgroundVideoUri']
        return None

    async def fetch(self, http, tid, url):
        src = os.path.join(self.cache_dir, f'{tid}.src')
        with open(src, 'wb') as f:
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=180)) as r:
                if r.status != 200:
                    return None
                async for chunk in r.content.iter_chunked(256 * 1024):
                    f.write(chunk)
        try:
            return await asyncio.get_running_loop().run_in_executor(None, self.prepare, src, tid)
        finally:
            try:
                os.remove(src)
            except OSError:
                pass

    def prepare(self, src, tid):
        out = os.path.join(self.cache_dir, f'{tid}.mp4')
        tmp = os.path.join(self.cache_dir, f'{tid}.part.mp4')
        # фон: кадр 16:9 по центру, маленький и размытый — планшет растянет на весь экран
        vf = ('scale=512:288:force_original_aspect_ratio=increase,crop=512:288,'
              'gblur=sigma=3,fps=30,format=yuv420p')
        cmd = [ffmpeg_exe(), '-y', '-v', 'error', '-i', src, '-t', '30', '-an', '-vf', vf,
               '-c:v', 'libx264', '-profile:v', 'baseline', '-level', '3.1', '-preset', 'veryfast',
               '-crf', '24', '-movflags', '+faststart', tmp]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW, timeout=300)
        if r.returncode != 0 or not os.path.exists(tmp):
            log.warning('clip: ffmpeg failed: %s', r.stderr[-400:])
            return None
        os.replace(tmp, out)
        return out

    def cleanup(self):
        if len(self.ready) <= KEEP:
            return
        old = sorted(self.ready, key=lambda t: os.path.getmtime(self.ready[t]))[:len(self.ready) - KEEP]
        for t in old:
            try:
                os.remove(self.ready.pop(t))
            except OSError:
                pass
