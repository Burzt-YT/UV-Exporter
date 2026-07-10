"""Persists per-model UI selections (checked groups, UV channel, render
options) to disk, so re-opening the same model doesn't require manually
re-finding and re-checking the same parts every time.

Keyed primarily by absolute file path. Also keeps a secondary lookup by
just the filename, since the same model often gets re-exported from a
slightly different location (a re-cloned repo, a synced drive folder, a
renamed parent dir) and falling back to "no saved selection" in that case
would defeat the point. Path-keyed entries always win when both exist.
"""

import json
import os

_STORE_FILENAME = "uv_template_exporter_selections.json"


def _store_path() -> str:
    """~/.config/uv_template_exporter on Linux, %APPDATA%/UVTemplateExporter
    on Windows, falling back to the home directory if neither env var is
    set (e.g. a stripped-down environment)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "UVTemplateExporter")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        d = os.path.join(base, "uv-template-exporter")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _STORE_FILENAME)


def _load_all() -> dict:
    path = _store_path()
    if not os.path.isfile(path):
        return {"by_path": {}, "by_name": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"by_path": {}, "by_name": {}}
    data.setdefault("by_path", {})
    data.setdefault("by_name", {})
    return data


def _save_all(data: dict) -> None:
    path = _store_path()
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except OSError:
        # Best-effort: a failed save shouldn't crash the app, it just means
        # the selection won't be remembered next time.
        pass


def save_selection(file_path: str, selection: dict) -> None:
    """selection is a plain JSON-serializable dict: checked group names,
    UV channel, render option values. Saved under both the absolute path
    and the bare filename."""
    data = _load_all()
    abs_path = os.path.abspath(file_path)
    name = os.path.basename(file_path)
    data["by_path"][abs_path] = selection
    data["by_name"][name] = selection
    _save_all(data)


def load_selection(file_path: str) -> dict | None:
    """Returns the saved selection dict for this file, or None if nothing
    was ever saved for it. Tries the exact path first, then falls back to
    matching by filename alone."""
    data = _load_all()
    abs_path = os.path.abspath(file_path)
    if abs_path in data["by_path"]:
        return data["by_path"][abs_path]
    name = os.path.basename(file_path)
    return data["by_name"].get(name)


def has_selection(file_path: str) -> bool:
    return load_selection(file_path) is not None


def clear_selection(file_path: str) -> None:
    data = _load_all()
    abs_path = os.path.abspath(file_path)
    name = os.path.basename(file_path)
    data["by_path"].pop(abs_path, None)
    data["by_name"].pop(name, None)
    _save_all(data)
