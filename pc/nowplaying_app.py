"""NowPlaying Server для Windows: сервер в фоне + значок в трее.

При первом запуске:
  * добавляет себя в автозагрузку (HKCU\\...\\Run, без прав администратора);
  * если нет правил брандмауэра, просит разрешение (окно UAC) открыть
    TCP 47801 и UDP 47800 — только для локальной сети.
В меню значка: что играет, адрес сервера, автозапуск, журнал, выход.
"""
import os
import sys
import time
import traceback

# Самая первая запись — до тяжёлых импортов: так видно, запускала ли Windows программу вообще
# и на чём она упала, даже если основной журнал ещё не успел открыться.
_STARTUP_LOG = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'NowPlaying', 'startup.log')


def _startup_note(text):
    try:
        os.makedirs(os.path.dirname(_STARTUP_LOG), exist_ok=True)
        if os.path.exists(_STARTUP_LOG) and os.path.getsize(_STARTUP_LOG) > 200_000:
            os.replace(_STARTUP_LOG, _STARTUP_LOG + '.old')
        with open(_STARTUP_LOG, 'a', encoding='utf-8') as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} pid={os.getpid()} {text}\n')
    except Exception:
        pass


_startup_note(f'launch cwd={os.getcwd()!r} exe={sys.executable!r}')

try:
    import asyncio
    import ctypes
    import socket
    import subprocess
    import threading
    import winreg
    import logging

    import pystray
    from aiohttp import web
    from PIL import Image

    import server
except Exception:
    _startup_note('import failed:\n' + traceback.format_exc())
    raise
_startup_note('imports ok')

APP_NAME = 'NowPlaying Server'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
DATA_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'NowPlaying')
FIRST_RUN_FLAG = os.path.join(DATA_DIR, 'first-run-done')
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32


def message(text, error=False):
    user32.MessageBoxW(None, text, APP_NAME, 0x10 if error else 0x40)


def resource(name):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ---------- автозапуск ----------

def launch_command():
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


# Автозапуск — через Планировщик заданий («при входе в систему», с задержкой 30 с и перезапуском
# при сбое). Раньше был раздел реестра Run, но Windows иногда молча пропускает такие записи.
TASK_NAME = APP_NAME
_autostart_cache = {}


def _launch_parts():
    if getattr(sys, 'frozen', False):
        return sys.executable, ''
    pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    return pythonw, f'"{os.path.abspath(__file__)}"'


def _ps(script, timeout=60):
    return subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                          capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)


def _remove_run_key():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
            return True
    except OSError:
        return False


def autostart_enabled(refresh=False):
    if refresh or 'on' not in _autostart_cache:
        r = subprocess.run(['schtasks', '/query', '/tn', TASK_NAME, '/fo', 'csv', '/nh'],
                           capture_output=True, text=True, creationflags=NO_WINDOW)
        _autostart_cache['on'] = r.returncode == 0 and 'Disabled' not in r.stdout and 'Отключено' not in r.stdout
    return _autostart_cache['on']


def set_autostart(on):
    if on:
        exe, args = _launch_parts()
        arg = f"-Argument '{args}'" if args else ''
        script = (
            f"$a = New-ScheduledTaskAction -Execute '{exe}' {arg}; "
            "$u = \"$env:USERDOMAIN\\$env:USERNAME\"; "
            "$t = New-ScheduledTaskTrigger -AtLogOn -User $u; $t.Delay = 'PT30S'; "
            "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) "
            "-MultipleInstances IgnoreNew; "
            "$p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited; "
            f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null")
        r = _ps(script)
        if r.returncode != 0:
            logging.getLogger('nowplaying').warning('scheduled task failed, using Run key: %s', r.stderr[-300:])
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            _remove_run_key()
    else:
        _ps(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue")
        _remove_run_key()
    autostart_enabled(refresh=True)


# ---------- брандмауэр ----------

def firewall_ok():
    ps = ("$r = Get-NetFirewallRule -Direction Inbound -Enabled True -Action Allow -ErrorAction SilentlyContinue | "
          "Get-NetFirewallPortFilter | Where-Object { $_.LocalPort -in '47800','47801' }; "
          "($r | Select-Object -ExpandProperty LocalPort -Unique).Count")
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                             capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW).stdout
        return out.strip() == '2'
    except Exception:
        return True  # не смогли проверить — не дёргаем пользователя


def add_firewall_rules():
    """Окно UAC: правила только для локальной подсети."""
    ps = ("New-NetFirewallRule -DisplayName 'NowPlaying (TCP 47801)' -Direction Inbound -Action Allow "
          "-Protocol TCP -LocalPort 47801 -RemoteAddress LocalSubnet; "
          "New-NetFirewallRule -DisplayName 'NowPlaying поиск (UDP 47800)' -Direction Inbound -Action Allow "
          "-Protocol UDP -LocalPort 47800 -RemoteAddress LocalSubnet")
    shell32.ShellExecuteW(None, 'runas', 'powershell.exe',
                          f'-NoProfile -WindowStyle Hidden -Command "{ps}"', None, 0)


# ---------- сервер в отдельном потоке ----------

class ServerThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name='np-server')
        self.loop = None
        self.error = None
        self.started = threading.Event()

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        runner = web.AppRunner(server.create_app(), access_log=None)
        try:
            self.loop.run_until_complete(runner.setup())
            self.loop.run_until_complete(web.TCPSite(runner, '0.0.0.0', server.HTTP_PORT).start())
        except Exception as e:
            self.error = e
            self.started.set()
            return
        self.started.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.run_until_complete(runner.cleanup())

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('192.168.0.1', 9))  # пакет не отправляется, нужен только выбор интерфейса
            return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'


def now_playing_text():
    st = server.state
    if not st.get('track_id'):
        return 'Ничего не играет'
    text = f"{st['artist']} — {st['title']}" if st.get('artist') else st['title']
    return ('▶ ' if st.get('playing') else '❚❚ ') + text[:90]


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    log_path = server.setup_logging(DATA_DIR)
    # любые необработанные ошибки — в журнал, иначе без консоли программа «молча» пропадает
    import logging
    log = logging.getLogger('nowplaying')
    sys.excepthook = lambda t, v, tb: log.critical('crash', exc_info=(t, v, tb))
    threading.excepthook = lambda a: log.critical('crash in thread %s', a.thread and a.thread.name,
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    log.info('NowPlaying Server started (pid %d)', os.getpid())

    # один экземпляр
    kernel32.CreateMutexW(None, False, 'Local\\NowPlayingServer')
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _startup_note('already running, exit')
        message('NowPlaying Server уже запущен — значок в трее рядом с часами.')
        return

    srv = ServerThread()
    srv.start()
    srv.started.wait(15)
    if srv.error:
        message(f'Не удалось запустить сервер на порту {server.HTTP_PORT}:\n{srv.error}\n\n'
                'Возможно, он уже запущен другим способом.', error=True)
        return

    first_run = not os.path.exists(FIRST_RUN_FLAG)
    if first_run:
        set_autostart(True)
        open(FIRST_RUN_FLAG, 'w').close()
    elif _remove_run_key():
        # переезд со старой версии: автозапуск был в реестре Run — переносим в Планировщик заданий
        _startup_note('migrating autostart from Run key to Task Scheduler')
        set_autostart(True)
    autostart_enabled(refresh=True)
    fw = {'ok': firewall_ok()}
    if first_run and not fw['ok']:
        add_firewall_rules()

    def toggle_autostart(icon, item):
        set_autostart(not autostart_enabled())

    def fix_firewall(icon, item):
        add_firewall_rules()

    def open_log(icon, item):
        if log_path and os.path.exists(log_path):
            os.startfile(log_path)

    def open_bg_folder(icon, item):
        os.startfile(server.BG.src_dir)

    def quit_app(icon, item):
        srv.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: now_playing_text(), None, enabled=False),
        pystray.MenuItem(lambda item: f'Адрес для планшета: {local_ip()}:{server.HTTP_PORT}', None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Запускать вместе с Windows', toggle_autostart, checked=lambda item: autostart_enabled()),
        pystray.MenuItem('Разрешить в брандмауэре…', fix_firewall, visible=lambda item: not fw['ok']),
        pystray.MenuItem('Папка фона для часов…', open_bg_folder),
        pystray.MenuItem('Открыть журнал', open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Выход', quit_app),
    )
    icon = pystray.Icon('NowPlaying', Image.open(resource('icon.png')), APP_NAME, menu)

    def refresher(icon):
        icon.visible = True
        if first_run:
            icon.notify('Сервер запущен и добавлен в автозагрузку. '
                        'Откройте «Сейчас играет» на планшете — он найдёт компьютер сам.', APP_NAME)
        last_fw_check = time.time()
        while srv.is_alive():
            try:
                icon.title = f'{APP_NAME}\n{now_playing_text()}'[:127]
                if not fw['ok'] and time.time() - last_fw_check > 20:  # после окна UAC
                    fw['ok'] = firewall_ok()
                    last_fw_check = time.time()
                icon.update_menu()
            except Exception:
                logging.getLogger('nowplaying').exception('tray refresh')
            time.sleep(3)
        logging.getLogger('nowplaying').warning('server thread stopped')

    icon.run(setup=refresher)
    logging.getLogger('nowplaying').info('tray closed, exiting')


if __name__ == '__main__':
    try:
        # сразу после включения ПК даём Windows дособрать панель задач, иначе значок в трее может не встать
        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        uptime = ctypes.windll.kernel32.GetTickCount64() / 1000
        if uptime < 180:
            _startup_note(f'early start (uptime {uptime:.0f}s), waiting 20s')
            time.sleep(20)
        main()
        _startup_note('exit normally')
    except SystemExit:
        _startup_note('exit (SystemExit)')
        raise
    except Exception:
        _startup_note('crash: ' + traceback.format_exc())
        try:
            ctypes.windll.user32.MessageBoxW(None, 'NowPlaying Server упал при запуске. Подробности: ' + _STARTUP_LOG,
                                             'NowPlaying Server', 0x10)
        except Exception:
            pass
        raise
