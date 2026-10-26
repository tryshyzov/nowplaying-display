"""«Дыхание» фона под музыку: уровень звука ПК 30 раз в секунду (0–1) для планшета.

Берём пиковый уровень основного устройства вывода (Core Audio, IAudioMeterInformation —
то, что рисует индикатор в микшере Windows; от положения ползунка громкости не зависит).
Сглаживаем: быстро вверх, медленно вниз — и делим на «громкость трека» (медленно
меняющийся максимум), чтобы тихие и громкие песни качали фон одинаково.

comtypes трогаем только в своём потоке (см. volume.py: в основном потоке он ломает winrt).
"""
import logging
import threading
import time

log = logging.getLogger('nowplaying')

RATE = 30   # замеров в секунду


class Pulse:
    def __init__(self):
        self.level = 0.0       # то, что отдаём планшету
        self.active = False    # меряем только пока что-то играет
        self.clients = 0
        self._thread = None

    def want(self, on):
        """Включить/выключить замеры (зовёт сервер: играет музыка и кто-то слушает)."""
        self.active = on
        if on and not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._run, name='pulse', daemon=True)
            self._thread.start()

    def _run(self):
        try:
            import comtypes
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            comtypes.CoInitialize()
        except Exception as e:
            log.warning('pulse: %s', e)
            return
        meter, env, loud, avg = None, 0.0, 0.2, 0.0
        try:
            while self.active:
                try:
                    if meter is None:
                        dev = AudioUtilities.GetSpeakers()
                        meter = dev._dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None) \
                            .QueryInterface(IAudioMeterInformation)
                    peak = float(meter.GetPeakValue())
                except Exception:   # сменилось устройство вывода — переоткроем
                    meter = None
                    time.sleep(1)
                    continue
                env = peak if peak > env else env * 0.86 + peak * 0.14          # быстро вверх, плавно вниз
                loud = max(env, loud * 0.992, 0.04)                          # «громкость трека», ~3 с памяти
                avg = avg * 0.95 + env * 0.05                                # средний уровень за ~0.7 с
                # удар — насколько звук сейчас выше среднего; плюс немного общей громкости
                punch = max(0.0, env - avg * 0.9) / max(0.02, loud - avg * 0.9)
                self.level = min(1.0, 0.3 * env / loud + 0.9 * punch)
                time.sleep(1 / RATE)
            self.level = 0.0
        finally:
            meter = None
            comtypes.CoUninitialize()
