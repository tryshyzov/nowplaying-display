"""Фон для экрана часов: самый новый файл из папки на ПК -> формат, который тянет Android 4.2.

Фото (jpg/png/webp/bmp, а также гифка из одного кадра) -> JPEG до 1280 px.
Видео и анимированные гифки -> MP4 H.264 baseline, до 720p, 30 к/с, без звука, до 2 минут.
Готовый файл лежит в %LOCALAPPDATA%\\NowPlaying\\bg (хранится только текущий).
"""
import asyncio
import hashlib
import logging
import os
import subprocess
import sys

from PIL import Image

log = logging.getLogger('nowplaying')

IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi', '.gif'}
MAX_SECONDS = 120
NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0


def default_dir():
    pictures = os.path.join(os.path.expanduser('~'), 'Pictures')
    return os.environ.get('NP_BG_DIR') or os.path.join(pictures, 'NowPlaying')


class Background:
    def __init__(self, src_dir=None, cache_dir=None):
        self.src_dir = src_dir or default_dir()
        local = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        self.cache_dir = cache_dir or os.path.join(local, 'NowPlaying', 'bg')
        self.current = None      # {'id', 'type', 'path', 'ext'}
        self.pending = None      # id, который сейчас готовится
        os.makedirs(self.src_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

    def info(self):
        c = self.current
        return {'id': c['id'], 'type': c['type'], 'ext': c['ext']} if c else None

    def newest_source(self):
        best = None
        try:
            for name in os.listdir(self.src_dir):
                p = os.path.join(self.src_dir, name)
                ext = os.path.splitext(name)[1].lower()
                if os.path.isfile(p) and ext in IMAGE_EXT | VIDEO_EXT:
                    st = os.stat(p)
                    if best is None or st.st_mtime > best[1].st_mtime:
                        best = (p, st)
        except OSError:
            return None
        return best

    async def watch(self):
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception('background')
            await asyncio.sleep(10)

    async def refresh(self):
        found = self.newest_source()
        if not found:
            if self.current:
                log.info('background: folder is empty, background off')
            self.current = None
            return
        path, st = found
        bid = hashlib.sha1(f'{os.path.basename(path)}|{st.st_size}|{st.st_mtime}'.encode()).hexdigest()[:12]
        if (self.current and self.current['id'] == bid) or self.pending == bid:
            return
        self.pending = bid
        try:
            result = await asyncio.get_running_loop().run_in_executor(None, self.prepare, path, bid)
            if result:
                self.current = result
                self.cleanup(bid)
                log.info('background: %s -> %s', os.path.basename(path), result['type'])
        finally:
            self.pending = None

    def prepare(self, path, bid):
        ext = os.path.splitext(path)[1].lower()
        if ext == '.gif' and not self.is_animated(path):
            ext = '.png'
        if ext in IMAGE_EXT:
            out = os.path.join(self.cache_dir, f'{bid}.jpg')
            img = Image.open(path).convert('RGB')
            img.thumbnail((1280, 1280))
            img.save(out, 'JPEG', quality=88)
            return {'id': bid, 'type': 'image', 'path': out, 'ext': 'jpg'}
        out = os.path.join(self.cache_dir, f'{bid}.mp4')
        tmp = out + '.part.mp4'
        cmd = [ffmpeg_exe(), '-y', '-v', 'error', '-i', path, '-t', str(MAX_SECONDS), '-an',
               '-vf', "scale='min(1280,iw)':-2:flags=lanczos,fps=30,format=yuv420p",
               '-c:v', 'libx264', '-profile:v', 'baseline', '-level', '3.1', '-preset', 'veryfast',
               '-crf', '24', '-movflags', '+faststart', tmp]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW, timeout=900)
        if r.returncode != 0 or not os.path.exists(tmp):
            log.warning('background: ffmpeg failed for %s: %s', os.path.basename(path), r.stderr[-400:])
            return None
        os.replace(tmp, out)
        return {'id': bid, 'type': 'video', 'path': out, 'ext': 'mp4'}

    @staticmethod
    def is_animated(path):
        try:
            return getattr(Image.open(path), 'n_frames', 1) > 1
        except Exception:
            return False

    def cleanup(self, keep):
        for name in os.listdir(self.cache_dir):
            if not name.startswith(keep):
                try:
                    os.remove(os.path.join(self.cache_dir, name))
                except OSError:
                    pass


def ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()
