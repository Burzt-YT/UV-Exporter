
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

GITHUB_OWNER = "Burzt-YT"
GITHUB_REPO = "UV-Exporter"

_RELEASES_API_URL = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
)
_RELEASES_PAGE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
_DEFAULT_TIMEOUT_SEC = 6.0

class UpdateCheckError(Exception):
    pass

@dataclass
class UpdateInfo:
    latest_version: str
    tag_name: str
    release_url: str
    release_notes: str

def _parse_version(text: str) -> tuple[int, ...]:
    cleaned = text.strip().lstrip("vV")
    parts = re.findall(r"\d+", cleaned)
    if not parts:
        raise ValueError(f"Couldn't parse a version number out of '{text}'")
    return tuple(int(p) for p in parts)

def is_newer(candidate: str, current: str) -> bool:
    try:
        return _parse_version(candidate) > _parse_version(current)
    except ValueError:
        return candidate.strip() != current.strip()

def check_for_update(
    current_version: str, timeout_sec: float = _DEFAULT_TIMEOUT_SEC
) -> UpdateInfo | None:
    request = urllib.request.Request(
        _RELEASES_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{GITHUB_REPO}-update-checker",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateCheckError(
                "This repository has no published releases yet."
            ) from e
        raise UpdateCheckError(f"GitHub returned an error (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        raise UpdateCheckError(f"Couldn't reach GitHub: {e.reason}") from e
    except TimeoutError as e:
        raise UpdateCheckError("Timed out contacting GitHub.") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UpdateCheckError("GitHub returned an unexpected response.") from e

    tag_name = data.get("tag_name")
    if not tag_name:
        raise UpdateCheckError("GitHub's response didn't include a release tag.")

    if not is_newer(tag_name, current_version):
        return None

    return UpdateInfo(
        latest_version=tag_name.strip().lstrip("vV"),
        tag_name=tag_name,
        release_url=data.get("html_url") or _RELEASES_PAGE_URL,
        release_notes=(data.get("body") or "").strip(),
    )
