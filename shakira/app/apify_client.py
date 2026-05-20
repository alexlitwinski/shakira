"""Cliente REST Apify para actors (Instagram Profile Scraper)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

APIFY_BASE = "https://api.apify.com/v2"
_SYNC_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=10.0)


def actor_id_for_api(actor: str) -> str:
    """apify/instagram-profile-scraper -> apify~instagram-profile-scraper"""
    a = (actor or "").strip()
    if "/" in a:
        return a.replace("/", "~")
    return a


async def run_actor_sync_dataset_items(
    http: httpx.AsyncClient,
    *,
    actor: str,
    token: str,
    run_input: dict[str, Any],
) -> list[dict[str, Any]]:
    if not token.strip():
        raise ValueError("token Apify vazio")
    act = actor_id_for_api(actor)
    url = f"{APIFY_BASE}/acts/{act}/run-sync-get-dataset-items"
    log.info("Apify run-sync actor=%s", act)
    resp = await http.post(
        url,
        params={"token": token.strip()},
        json=run_input,
        timeout=_SYNC_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        items = data.get("items") or data.get("data")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    return []


def map_instagram_profile_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normaliza item do dataset Apify para campos internos."""
    handle = (
        str(row.get("username") or row.get("userName") or row.get("handle") or "")
        .strip()
        .lstrip("@")
        .lower()
    )
    name = str(row.get("fullName") or row.get("full_name") or row.get("name") or "").strip()
    bio = str(row.get("biography") or row.get("bio") or "").strip()
    followers = row.get("followersCount") or row.get("followers") or row.get("followerCount")
    foll: int | None = None
    if isinstance(followers, int):
        foll = followers
    elif isinstance(followers, float):
        foll = int(followers)
    elif isinstance(followers, str) and followers.replace(",", "").isdigit():
        foll = int(followers.replace(",", ""))
    verified = row.get("verified") or row.get("isVerified")
    is_verified: bool | None = None
    if isinstance(verified, bool):
        is_verified = verified
    avatar = (
        str(row.get("profilePicUrlHD") or row.get("profilePicUrl") or row.get("avatar") or "")
        .strip()
    )
    return {
        "handle": handle,
        "profile_name": name,
        "profile_bio": bio,
        "followers": foll,
        "is_verified": is_verified,
        "avatar_url": avatar,
    }
