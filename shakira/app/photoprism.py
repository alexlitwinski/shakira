"""Cliente da API REST do PhotoPrism (busca e thumbnails)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

log = logging.getLogger(__name__)

DEFAULT_THUMB_SIZE = "fit_720"
PROBE_PATHS = (
    "/api/v1/config",
    "/api/v1/photos?count=1&primary=true",
    "/api/v1/index",
    "/",
)


@dataclass(frozen=True)
class PhotoResult:
    uid: str
    file_hash: str
    title: str
    taken_at: str
    place_label: str
    preview_token: str


@dataclass
class ProbeResult:
    url: str
    status: int | None
    content_type: str
    server: str
    body_preview: str
    looks_like_photoprism: bool
    looks_like_nginx_404: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "content_type": self.content_type,
            "server": self.server,
            "looks_like_photoprism": self.looks_like_photoprism,
            "looks_like_nginx_404": self.looks_like_nginx_404,
            "body_preview": self.body_preview[:200],
            "error": self.error,
        }


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


def _body_preview(text: str, limit: int = 280) -> str:
    t = " ".join(text.split())
    if len(t) <= limit:
        return t
    return t[: limit - 3] + "..."


def _analyze_response(status: int | None, headers: httpx.Headers, body: str) -> tuple[bool, bool]:
    server = (headers.get("server") or "").lower()
    ct = (headers.get("content-type") or "").lower()
    body_l = body.lower()
    nginx_404 = status == 404 and ("nginx" in body_l or "nginx" in server)
    is_pp = (
        "application/json" in ct
        or "photoprism" in server
        or (status == 200 and body.strip().startswith(("{", "[")))
        or (status == 401 and "application/json" in ct)
    )
    return is_pp, nginx_404


class PhotoprismClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        token: str,
        api_prefix: str = "",
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._prefix = (api_prefix or "").strip().rstrip("/")
        if self._prefix and not self._prefix.startswith("/"):
            self._prefix = "/" + self._prefix
        self._token = token.strip()

    @property
    def api_base(self) -> str:
        return f"{self._base}{self._prefix}"

    def _headers(self, *, json_accept: bool = True) -> dict[str, str]:
        h: dict[str, str] = {
            "X-Auth-Token": self._token,
            "Authorization": f"Bearer {self._token}",
        }
        if json_accept:
            h["Accept"] = "application/json"
        return h

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.api_base}{path}"

    def _log_request(self, method: str, url: str, params: dict[str, Any] | None = None) -> None:
        full = url
        if params:
            full = f"{url}?{urlencode({k: v for k, v in params.items()})}"
        log.info("PhotoPrism >> %s %s", method, full)

    def _log_response(self, r: httpx.Response, *, context: str) -> None:
        body = r.text[:500] if r.text else ""
        is_pp, nginx_404 = _analyze_response(r.status_code, r.headers, body)
        log.info(
            "PhotoPrism << %s %s status=%s ct=%s server=%s photoprism=%s nginx404=%s",
            context,
            str(r.request.url)[:200],
            r.status_code,
            r.headers.get("content-type", "-"),
            r.headers.get("server", "-"),
            is_pp,
            nginx_404,
        )
        if r.status_code >= 400 or not is_pp:
            log.warning(
                "PhotoPrism corpo (%s): %s",
                context,
                _body_preview(body),
            )

    async def probe(self) -> dict[str, Any]:
        """Testa caminhos comuns e devolve diagnostico para /status ou logs."""
        parsed = urlparse(self._base)
        probes: list[ProbeResult] = []
        for path in PROBE_PATHS:
            url = f"{self._base}{path}"
            try:
                r = await self._client.get(
                    url,
                    headers=self._headers(),
                    timeout=15.0,
                    follow_redirects=True,
                )
                body = r.text[:400] if r.text else ""
                is_pp, nginx_404 = _analyze_response(r.status_code, r.headers, body)
                probes.append(
                    ProbeResult(
                        url=url,
                        status=r.status_code,
                        content_type=r.headers.get("content-type", ""),
                        server=r.headers.get("server", ""),
                        body_preview=_body_preview(body),
                        looks_like_photoprism=is_pp,
                        looks_like_nginx_404=nginx_404,
                    )
                )
            except httpx.RequestError as e:
                probes.append(
                    ProbeResult(
                        url=url,
                        status=None,
                        content_type="",
                        server="",
                        body_preview="",
                        looks_like_photoprism=False,
                        looks_like_nginx_404=False,
                        error=str(e),
                    )
                )

        any_pp = any(p.looks_like_photoprism for p in probes)
        any_nginx = any(p.looks_like_nginx_404 for p in probes)
        hints: list[str] = []
        if any_nginx and not any_pp:
            hints.append(
                "A porta responde com nginx 404, nao com a API PhotoPrism. "
                "Use o hostname interno do add-on (ex: http://SLUG-photoprism:2342), "
                "nao o IP:porta do host, a menos que a API esteja exposta."
            )
        if parsed.hostname and not parsed.hostname.endswith("-photoprism"):
            hints.append(
                "No HA, tente photoprism_url=http://<slug>-photoprism:2342 "
                "(slug visivel na pagina do add-on PhotoPrism)."
            )
        if self._prefix:
            hints.append(f"Prefixo API configurado: {self._prefix}")

        summary = "ok" if any_pp else ("nginx_proxy?" if any_nginx else "unreachable_or_wrong_url")
        return {
            "base_url": self._base,
            "api_prefix": self._prefix or None,
            "api_base": self.api_base,
            "token_set": bool(self._token),
            "summary": summary,
            "hints": hints,
            "probes": [p.to_dict() for p in probes],
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

        url = self._url("/api/v1/photos")
        self._log_request("GET", url, params)
        log.info("PhotoPrism filtros=%s q=%r", filters, q or "(vazio)")

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

        self._log_response(r, context="search")

        if r.status_code == 401:
            raise PhotoprismAuthError("Token PhotoPrism invalido ou expirado")
        if r.status_code >= 400:
            diag = await self.probe()
            log.error("PhotoPrism diagnostico apos erro %s:\n%s", r.status_code, json.dumps(diag, indent=2))
            hint = ""
            if diag.get("hints"):
                hint = " " + diag["hints"][0]
            raise PhotoprismError(
                f"Busca falhou: HTTP {r.status_code}.{hint}",
                diagnostic=diag,
            )

        preview_token = r.headers.get("X-Preview-Token") or r.headers.get("x-preview-token")
        try:
            rows = r.json()
        except Exception as e:
            raise PhotoprismError("Resposta JSON invalida (URL pode estar errada)") from e

        if not isinstance(rows, list):
            log.warning("PhotoPrism search retornou tipo %s, esperado lista", type(rows).__name__)
            return [], preview_token

        log.info("PhotoPrism encontrou %s resultado(s)", len(rows))

        results: list[PhotoResult] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            file_hash = _extract_file_hash(row)
            if not file_hash:
                log.debug("PhotoPrism item sem hash: uid=%s", row.get("UID"))
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
        from urllib.parse import quote

        token = preview_token or "public"
        path = f"/api/v1/t/{quote(file_hash, safe='')}/{quote(token, safe='')}/{size}"
        url = self._url(path)
        self._log_request("GET", url)
        r = await self._client.get(url, headers=self._headers(json_accept=False), timeout=90.0)
        self._log_response(r, context="thumbnail")
        if r.status_code >= 400:
            raise PhotoprismError(f"Thumbnail HTTP {r.status_code}")
        log.info("PhotoPrism thumbnail ok hash=%s bytes=%s", file_hash[:12], len(r.content))
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
    def __init__(self, message: str, *, diagnostic: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic or {}


class PhotoprismAuthError(PhotoprismError):
    pass
