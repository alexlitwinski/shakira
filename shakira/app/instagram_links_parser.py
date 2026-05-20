"""Parse e validacao de URLs Instagram."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/[^\s<>\"']+",
    re.IGNORECASE,
)
_RESERVED_PATHS = frozenset(
    {
        "p",
        "reel",
        "reels",
        "tv",
        "stories",
        "explore",
        "accounts",
        "direct",
        "about",
        "legal",
        "developer",
    }
)


@dataclass(frozen=True)
class ParsedInstagramUrl:
    handle: str
    canonical_url: str
    original_url: str
    is_profile_url: bool = True


class InstagramParseError(ValueError):
    pass


def is_instagram_url(url: str) -> bool:
    try:
        parse_instagram_url(url)
        return True
    except InstagramParseError:
        return False


def extract_instagram_urls(text: str) -> list[str]:
    return list(dict.fromkeys(_URL_RE.findall(text or "")))


def extract_note_without_urls(text: str, urls: list[str]) -> str:
    t = text or ""
    for u in urls:
        t = t.replace(u, " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def canonical_profile_url(handle: str) -> str:
    h = handle.strip().lstrip("@").lower()
    if not h or not re.fullmatch(r"[a-z0-9._]{1,30}", h):
        raise InstagramParseError("username Instagram invalido")
    return f"https://www.instagram.com/{h}/"


def parse_instagram_url(url: str) -> ParsedInstagramUrl:
    raw = (url or "").strip()
    if not raw:
        raise InstagramParseError("URL vazia")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower().split(":")[0]
    if host not in _INSTAGRAM_HOSTS:
        raise InstagramParseError("nao e URL Instagram")

    path = (parsed.path or "").strip("/")
    if not path:
        raise InstagramParseError("caminho Instagram vazio")

    segments = [s for s in path.split("/") if s]
    first = segments[0].lower()
    if first in ("p", "reel", "reels", "tv"):
        clean = raw.split("?")[0].rstrip("/")
        return ParsedInstagramUrl(
            handle="",
            canonical_url=clean,
            original_url=clean,
            is_profile_url=False,
        )
    handle = _profile_handle_from_segment(first)
    return ParsedInstagramUrl(
        handle=handle,
        canonical_url=canonical_profile_url(handle),
        original_url=raw.split("?")[0].rstrip("/"),
        is_profile_url=True,
    )


def _profile_handle_from_segment(first: str) -> str:
    if first in _RESERVED_PATHS:
        raise InstagramParseError(f"tipo de link Instagram nao suportado: {first}")
    handle = first.lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", handle):
        raise InstagramParseError("username invalido na URL")
    return handle
