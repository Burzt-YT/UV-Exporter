#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 was not found on PATH. Install Python 3.10+ first."
    exit 1
fi

if ! python3 -c "import PySide6" >/dev/null 2>&1; then
    echo "Installing required package: PySide6 ..."
    python3 -m pip install --upgrade pip >/dev/null
    python3 -m pip install PySide6 || python3 -m pip install PySide6 --break-system-packages
fi

if ! python3 -c "import msgpack" >/dev/null 2>&1; then
    echo "Installing required package: msgpack (for .cdae support) ..."
    python3 -m pip install msgpack || python3 -m pip install msgpack --break-system-packages
fi

if ! python3 -c "import zstandard" >/dev/null 2>&1; then
    echo "Installing required package: zstandard (for compressed .cdae support) ..."
    python3 -m pip install zstandard || python3 -m pip install zstandard --break-system-packages
fi

python3 main.py
