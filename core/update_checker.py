"""Checks GitHub for a newer release of this app.

Talks directly to the GitHub REST API (no auth token, no extra
dependencies -- just the standard library) rather than scraping the repo's
HTML releases page, since the API hands back the one field this needs
(the release tag) as clean JSON instead of something that would have to be
parsed out of markup.
"""

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
    """Raised when the check itself couldn't be completed -- no network,
    rate-limited, the repo has no published releases yet, or an
    unexpected response shape. Distinct from "checked fine, no update
    available", which just returns None instead of raising."""


@dataclass
class UpdateInfo:
    latest_version: str  # e.g. "1.2.0" -- the release tag with a leading "v" stripped
    tag_name: str  # the raw tag as published, e.g. "v1.2.0"
    release_url: str  # human-facing release page to open in a browser
    release_notes: str  # release body text, possibly empty


def _parse_version(text: str) -> tuple[int, ...]:
    """Turns a version-ish string into a comparable tuple of ints, e.g.
    "v1.12.3" -> (1, 12, 3). Non-numeric trailing content (e.g. "-beta.1")
    is dropped rather than raising -- GitHub tags aren't guaranteed to be
    strict semver, and a best-effort numeric comparison is enough to
    detect "is there something newer" here."""
    cleaned = text.strip().lstrip("vV")
    parts = re.findall(r"\d+", cleaned)
    if not parts:
        raise ValueError(f"Couldn't parse a version number out of '{text}'")
    return tuple(int(p) for p in parts)


def is_newer(candidate: str, current: str) -> bool:
    """True if `candidate` is a newer version than `current`. Falls back
    to a plain string-inequality check if either side doesn't parse as a
    version, so a malformed/unusual tag makes the comparison less precise
    rather than crashing the checker."""
    try:
        return _parse_version(candidate) > _parse_version(current)
    except ValueError:
        return candidate.strip() != current.strip()


def check_for_update(
    current_version: str, timeout_sec: float = _DEFAULT_TIMEOUT_SEC
) -> UpdateInfo | None:
    """Queries the GitHub releases API for this repo's latest published
    release. Returns an UpdateInfo if it's newer than current_version, or
    None if already up to date (or ahead of it, e.g. a local dev build).

    Raises UpdateCheckError if the check itself couldn't be completed.
    Callers doing a silent background check on startup should catch this
    and just skip notifying -- a flaky connection or a rate-limited API
    shouldn't interrupt opening the app -- while a manual "Check for
    Updates" action can surface the message directly.
    """
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
