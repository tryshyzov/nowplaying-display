"""Громкость Windows (основное устройство вывода) через Core Audio (pycaw).

comtypes при импорте включает COM в однопоточном режиме (STA) в том потоке, где его
импортировали. Если это случится в основном потоке сервера, перестаёт работать опрос
панели «Сейчас играет» (winrt). Поэтому pycaw импортируем только в рабочем потоке —
функции отсюда вызываются через run_in_executor.
"""


def _endpoint():
    import comtypes
    from pycaw.pycaw import AudioUtilities
    comtypes.CoInitialize()
    return AudioUtilities.GetSpeakers().EndpointVolume


def _done():
    import comtypes
    comtypes.CoUninitialize()


def get():
    """(громкость 0–100, выключен ли звук). Только из рабочего потока."""
    v = _endpoint()
    try:
        return round(v.GetMasterVolumeLevelScalar() * 100), bool(v.GetMute())
    finally:
        del v
        _done()


def set_level(level):
    v = _endpoint()
    try:
        v.SetMasterVolumeLevelScalar(max(0, min(100, level)) / 100, None)
        if level > 0 and v.GetMute():
            v.SetMute(0, None)   # прибавили громкость — значит, звук нужен
        return round(v.GetMasterVolumeLevelScalar() * 100)
    finally:
        del v
        _done()
