"""Cliente da API REST do PhotoPrism (busca e thumbnails)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

DEFAULT_THUMB_SIZE = "fit_720"


@dataclass(frozen=True)
class PhotoResult:
    uid: str
    file_hash: str
    title: str
    taken_at: str
    place_label: str
    preview_token: str


def build_search_query(filters: dict[str, Any]) -> str:
    """Monta string de filtros PhotoPrism para o parametro q."""
    parts: list[str] = []

    def qval(key: str, value: str) -> None:
        v = value.strip()
        if not v:
            return
        if " " in v or "&" in v or "|" in v:
            parts.append(f'{key}:"{v}"')
        else:
            parts.append(f"{key}:{v}")

    person = filters.get("person")
    if isinstance(person, str) and person.strip():
        qval("person", person)

    people = filters.get("people")
    if isinstance(people, str) and people.strip():
        qval("people", people)

    for key in ("city", "country", "state", "label", "album"):
        val = filters.get(key)
        if isinstance(val, str) and val.strip():
            qval(key, val)

    year = filters.get("year")
    if year is not None:
        parts.append(f"year:{int(year)}")

    month = filters.get("month")
    if month is not None:
        parts.append(f"month:{int(month)}")

    day = filters.get("day")
    if day is not None:
        parts.append(f"day:{int(day)}")

    after = filters.get("after")
    if isinstance(after, str) and after.strip():
        qval("after", after)

    before = filters.get("before")
    if isinstance(before, str) and before.strip():
        qval("before", before)

    taken = filters.get("taken")
    if isinstance(taken, str) and taken.strip():
        qval("taken", taken)

    free = filters.get("query")
    if isinstance(free, str) and free.strip():
        parts.append(free.strip())

    return " ".join(parts)


class PhotoprismClient:
    def __init__(self, client: httpx.AsyncClient, *, base_url: str, token: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._token = token.strip()

    def _headers(self) -> dict[str, str]:
        return {
            "X-Auth-Token": self._token,
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    async def search_photos(
        self,
        *,
        filters: dict[str, Any] | None = None,
        count: int = 5,
    ) -> tuple[list[PhotoResult], str | None]:
        """Busca fotos. Retorna (resultados, preview_token dos headers)."""
        filters = filters or {}
        count = max(1, min(int(count), 10))
        q = build_search_query(filters)

        params: dict[str, str | int | bool] = {
            "count": count,
            "offset": 0,
            "merged": True,
            "primary": True,
            "public": True,
        }
        if q:
            params["q"] = q

        url = f"{self._base}/api/v1/photos"
        try:
            r = await self._client.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=60.0,
            )
        except httpx.RequestError as e:
            log.exception("PhotoPrism inacessivel: %s", e)
            raise

        if r.status_code == 401:
            raise PhotoprismAuthError("Token PhotoPrism invalido ou expirado")
        if r.status_code >= 400:
            log.warning("PhotoPrism search %s: %s", r.status_code, r.text[:500])
            raise PhotoprismError(f"Busca falhou: HTTP {r.status_code}")

        preview_token = r.headers.get("X-Preview-Token") or r.headers.get("x-preview-token")
        try:
            rows = r.json()
        except Exception as e:
            raise PhotoprismError("Resposta JSON invalida") from e

        if not isinstance(rows, list):
            return [], preview_token

        results: list[PhotoResult] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            file_hash = _extract_file_hash(row)
            if not file_hash:
                continue
            results.append(
                PhotoResult(
                    uid=str(row.get("UID") or row.get("uid") or ""),
                    file_hash=file_hash,
                    title=str(row.get("Title") or row.get("title") or "").strip(),
                    taken_at=str(row.get("TakenAtLocal") or row.get("TakenAt") or ""),
                    place_label=str(row.get("PlaceLabel") or row.get("place_label") or "").strip(),
                    preview_token=preview_token or "public",
                )
            )
            if len(results) >= count:
                break

        return results, preview_token

    async def get_thumbnail_bytes(
        self,
        *,
        file_hash: str,
        preview_token: str,
        size: str = DEFAULT_THUMB_SIZE,
    ) -> bytes:
        token = preview_token or "public"
        path = f"/api/v1/t/{quote(file_hash, safe='')}/{quote(token, safe='')}/{size}"
        url = f"{self._base}{path}"
        r = await self._client.get(url, headers=self._headers(), timeout=90.0)
        if r.status_code >= 400:
            raise PhotoprismError(f"Thumbnail HTTP {r.status_code}")
        return r.content


def _extract_file_hash(row: dict[str, Any]) -> str:
    h = row.get("Hash") or row.get("hash")
    if isinstance(h, str) and h.strip():
        return h.strip()
    files = row.get("Files") or row.get("files")
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, dict):
            fh = first.get("Hash") or first.get("hash")
            if isinstance(fh, str) and fh.strip():
                return fh.strip()
    return ""


class PhotoprismError(Exception):
    pass


class PhotoprismAuthError(PhotoprismError):
    pass
