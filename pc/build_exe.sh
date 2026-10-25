#!/bin/sh
# Сборка NowPlayingServer.exe (Python встроен, окно консоли не открывается).
# Нужен venv с зависимостями: python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt pyinstaller
# PyInstaller плохо дружит с кириллицей в пути, поэтому сборка во временной ASCII-папке.
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-$SRC/.venv/Scripts/python}"
B="${BUILD_DIR:-${TMPDIR:-/tmp}/np-pyi}"
rm -rf "$B"; mkdir -p "$B"
cp "$SRC/server.py" "$SRC/background.py" "$SRC/clips.py" "$SRC/yandex.py" "$SRC/nowplaying_app.py" "$SRC/icon.png" "$SRC/icon.ico" "$B/"
cd "$B"
"$PY" -m PyInstaller --noconfirm --clean --onefile --windowed --name NowPlayingServer \
    --icon icon.ico --add-data "icon.png;." \
    --collect-submodules winrt --collect-submodules pystray --collect-binaries imageio_ffmpeg nowplaying_app.py >pyinstaller.log 2>&1 \
    || { tail -20 pyinstaller.log; exit 1; }
mkdir -p "$SRC/../release"
cp dist/NowPlayingServer.exe "$SRC/../release/"
ls -l "$SRC/../release/NowPlayingServer.exe"
