"""Видео на фон экрана трека из Яндекс Музыки: видеошот или превью клипа.

Трек ищется в открытом поиске Яндекс Музыки (без входа в аккаунт). Берём видеошот
трека (backgroundVideoUri, короткая нарезка из клипа), а если его нет, но есть клип, то
10-секундное превью клипа (тоже нарезка; весь клип без аккаунта не отдают). Видео
перекодируется под Android 4.2: без чёрных полос, кадр 16:9 по центру, размытие,
без звука, H.264 baseline. Готовые файлы лежат в %LOCALAPPDATA%\\NowPlaying\\clips
(последние 20).
"""
import asyncio
import logging
import os
import re
import subprocess
import time

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
        # у этих треков видео не нашлось: track_id -> (длительность, когда искали).
        # При смене трека Windows иногда отдаёт длительность прошлой песни, поэтому
        # с другой длительностью ищем снова, а отказ забываем через час
        self.missing = {}
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
        if not self.enabled or not tid or not title or tid in self.ready or tid in self.jobs:
            return
        miss = self.missing.get(tid)
        if miss and miss[0] == dur_ms and time.time() - miss[1] < 3600:
            return
        self.jobs[tid] = asyncio.create_task(self._job(http, tid, title, artist, dur_ms))

    async def _job(self, http, tid, title, artist, dur_ms):
        try:
            kind, urls = await self.find(http, title, artist, dur_ms)
            path = None
            for url in urls:
                path = await self.fetch(http, tid, url, skip_intro=kind == 'clip preview')
                if path:
                    break
            if path:
                self.ready[tid] = path
                self.cleanup()
                log.info('clip for %s — %s: ready (%s)', artist, title, kind)
            else:
                log.info('clip for %s — %s: none', artist, title)
                self.missing[tid] = (dur_ms, time.time())
        except Exception as e:
            log.warning('clip: %s', e)  # сеть: в следующий раз попробуем снова
        finally:
            self.jobs.pop(tid, None)

    @staticmethod
    async def api(http, path, params=None):
        for attempt in range(2):  # Яндекс иногда отвечает пустотой на первый запрос
            async with http.get(f'{YM_API}/{path}', params=params) as r:
                if r.status == 200:
                    try:
                        return ((await r.json(content_type=None)) or {}).get('result')
                    except ValueError:
                        pass
            await asyncio.sleep(1)
        return None

    async def find(self, http, title, artist, dur_ms):
        """(вид, [ссылки на видео по порядку предпочтения]) или (None, [])."""
        res = await self.api(http, 'search', {'text': f'{artist} {title}'.strip(), 'type': 'track', 'page': '0'})
        tracks = ((res or {}).get('tracks') or {}).get('results') or []
        want_title = norm(title)
        want_artists = {norm(a) for a in re.split(r',|&| feat\.? | ft\.? ', artist or '', flags=re.I) if a.strip()}
        matches = []
        for t in tracks:
            if norm(t.get('title')) != want_title:
                continue
            names = {norm(a.get('name')) for a in t.get('artists') or []}
            if want_artists and not (names & want_artists):
                continue
            if dur_ms and t.get('durationMs') and abs(t['durationMs'] - dur_ms) > 6000:
                continue
            matches.append(t)
        for t in matches:
            if t.get('backgroundVideoUri'):
                return 'shot', [t['backgroundVideoUri']]
        # видеошота нет — ищем клип (clipIds есть только в полной карточке трека)
        for t in matches[:3]:
            full = await self.api(http, f'tracks/{t["id"]}')
            ids = (full[0] if full else {}).get('clipIds') or []
            if not ids:
                continue
            clip = await self.api(http, 'clips', {'clipIds': ','.join(str(i) for i in ids[:3])})
            for c in clip or []:
                prev = c.get('previewUrl') or ''
                if prev:
                    longer = re.sub(r'-\d+s-(\d+)\.mp4$', r'-10s-\1.mp4', prev)
                    return 'clip preview', [longer, prev] if longer != prev else [prev]
        return None, []

    async def fetch(self, http, tid, url, skip_intro=False):
        src = os.path.join(self.cache_dir, f'{tid}.src')
        try:
            with open(src, 'wb') as f:
                async with http.get(url, timeout=aiohttp.ClientTimeout(total=180)) as r:
                    if r.status != 200:
                        return None   # например, 10-секундного превью нет — возьмём следующее
                    async for chunk in r.content.iter_chunked(256 * 1024):
                        f.write(chunk)
            return await asyncio.get_running_loop().run_in_executor(None, self.prepare, src, tid, skip_intro)
        finally:
            try:
                os.remove(src)
            except OSError:
                pass

    @staticmethod
    def analyze(src):
        """(рамка без чёрных полос или None, время первой смены сцены после 0.5 с или None)."""
        r = subprocess.run([ffmpeg_exe(), '-hide_banner', '-i', src, '-t', '12', '-an', '-vf',
                            "cropdetect=24:16:1,select='gt(scene,0.3)',showinfo", '-f', 'null', '-'],
                           capture_output=True, text=True, creationflags=NO_WINDOW, timeout=120)
        crops = re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)', r.stderr)
        size = re.search(r'Video: .*?, (\d{2,5})x(\d{2,5})', r.stderr)
        crop = None
        if crops and size:
            w, h = int(size.group(1)), int(size.group(2))
            # самая частая рамка: тёмные сцены дают случайные узкие рамки, полосы — постоянные
            best = max(set(crops), key=crops.count)
            cw, ch = int(best[0]), int(best[1])
            if (cw, ch) != (w, h) and cw >= w * 0.5 and ch >= h * 0.5:
                crop = ':'.join(best)
        cuts = [float(t) for t in re.findall(r'pts_time:([\d.]+)', r.stderr)]
        cut = next((t for t in cuts if 0.5 <= t <= 2.5), None)
        return crop, cut

    def prepare(self, src, tid, skip_intro=False):
        out = os.path.join(self.cache_dir, f'{tid}.mp4')
        tmp = os.path.join(self.cache_dir, f'{tid}.part.mp4')
        # у старых клипов чёрные полосы; превью клипа начинается с первой секунды клипа,
        # где часто заставка лейбла — пропускаем её до первой смены сцены
        crop, cut = self.analyze(src)
        start = ['-ss', f'{cut:.2f}'] if skip_intro and cut else []
        crop = f'crop={crop},' if crop else ''
        # фон: кадр 16:9 по центру, маленький и размытый — планшет растянет на весь экран
        vf = (crop + 'scale=512:288:force_original_aspect_ratio=increase,crop=512:288,'
              'gblur=sigma=3,setsar=1,fps=30,format=yuv420p')
        cmd = [ffmpeg_exe(), '-y', '-v', 'error', *start, '-i', src, '-t', '30', '-an', '-vf', vf,
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
