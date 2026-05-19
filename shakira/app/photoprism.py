"""Cliente da API REST do PhotoPrism (busca, thumbnails, auto-discovery Ingress HA)."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx

log = logging.getLogger(__name__)

DEFAULT_THUMB_SIZE = "fit_720"
INGRESS_PREFIX_RE = re.compile(r"(/api/hassio_ingress/[^/]+)")
PREFIX_CACHE_PATH = Path(os.environ.get("PHOTOPRISM_PREFIX_CACHE", "/data/photoprism_api_prefix.json"))

# Cidades comuns PT -> EN (PhotoPrism indexa nomes do geocoder, em geral em ingles).
_CITY_PT_TO_EN: dict[str, str] = {
    "nova orleans": "New Orleans",
    "nova iorque": "New York",
    "nova york": "New York",
    "san francisco": "San Francisco",
    "los angeles": "Los Angeles",
    "londres": "London",
    "paris": "Paris",
    "roma": "Rome",
    "munique": "Munich",
    "berlim": "Berlin",
    "amsterdam": "Amsterdam",
    "chicago": "Chicago",
    "miami": "Miami",
    "boston": "Boston",
    "seattle": "Seattle",
    "denver": "Denver",
    "toronto": "Toronto",
    "montreal": "Montreal",
    "cidade do mexico": "Mexico City",
    "buenos aires": "Buenos Aires",
    "rio de janeiro": "Rio de Janeiro",
    "sao paulo": "São Paulo",
}


@dataclass(frozen=True)
class PhotoResult:
    uid: str
    file_hash: str
    title: str
    taken_at: str
    place_label: str
    preview_token: str


def expand_city_variants(city: str, extra: list[str] | None = None) -> list[str]:
    """Gera variantes de nome de cidade (PT/EN, hifen) para busca OR no PhotoPrism."""
    raw = city.strip()
    if not raw:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        v = value.strip()
        if not v:
            return
        key = v.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(v)

    add(raw)
    for item in extra or []:
        if isinstance(item, str):
            add(item)

    low = raw.casefold()
    if low in _CITY_PT_TO_EN:
        add(_CITY_PT_TO_EN[low])

    nova = re.match(r"^nova\s+(.+)$", raw, re.IGNORECASE)
    if nova:
        add(f"New {nova.group(1)}")

    for v in list(out):
        if " " in v:
            add(v.replace(" ", "-"))
        if "-" in v:
            add(v.replace("-", " "))

    return out


def _city_query_value(city: str, extra_variants: list[str] | None = None) -> str:
    variants = expand_city_variants(city, extra_variants)
    if not variants:
        return ""
    if len(variants) == 1:
        return variants[0]
    return "|".join(variants)


def photo_matches_place(photo: PhotoResult, terms: list[str]) -> bool:
    """True se titulo ou PlaceLabel contem algum termo de local (case-insensitive)."""
    if not terms:
        return True
    hay = f"{photo.title} {photo.place_label}".casefold()
    for term in terms:
        t = term.strip().casefold()
        if not t:
            continue
        if t in hay:
            return True
        if " " in t and t.replace(" ", "-") in hay:
            return True
        if "-" in t and t.replace("-", " ") in hay:
            return True
    return False


def normalize_photo_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Prefer people (flexivel) sobre person (exato); preserva demais chaves."""
    out = dict(filters)
    person = out.get("person")
    if isinstance(person, str) and person.strip() and not out.get("people"):
        out["people"] = person.strip()
        out.pop("person", None)
    return out


def build_search_attempts(filters: dict[str, Any]) -> list[tuple[str, dict[str, Any], bool]]:
    """
    Planos de busca em ordem: (descricao, filtros, filtrar local no cliente).
    """
    base = normalize_photo_filters(filters)
    city = base.get("city") if isinstance(base.get("city"), str) else ""
    has_subject = bool(base.get("people") or base.get("person"))
    place_terms = expand_city_variants(city) if city else []
    attempts: list[tuple[str, dict[str, Any], bool]] = []

    attempts.append(("busca com filtros informados", base, False))

    if city and has_subject:
        no_city = {k: v for k, v in base.items() if k != "city"}
        attempts.append(
            (
                f"busca por pessoa, filtrando local ({city})",
                no_city,
                True,
            )
        )

    if city and has_subject and place_terms:
        kw = dict(base)
        kw.pop("city", None)
        kw.pop("person", None)
        orleans_bits = [t for t in place_terms if len(t) >= 4]
        if orleans_bits:
            kw["query"] = " ".join(
                filter(
                    None,
                    [
                        str(kw.get("query") or "").strip(),
                        " ".join(f"keywords:{b.replace(' ', '-')}" for b in orleans_bits[:2]),
                    ],
                )
            ).strip()
            attempts.append(("busca por pessoa e palavras-chave do local", kw, True))

    return attempts


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

    people = filters.get("people")
    if isinstance(people, str) and people.strip():
        qval("people", people)
    else:
        person = filters.get("person")
        if isinstance(person, str) and person.strip():
            qval("person", person)

    city = filters.get("city")
    if isinstance(city, str) and city.strip():
        extras_raw = filters.get("city_variants")
        extras: list[str] | None = None
        if isinstance(extras_raw, list):
            extras = [str(x) for x in extras_raw if isinstance(x, str)]
        city_q = _city_query_value(city, extras)
        if city_q:
            qval("city", city_q)

    for key in ("country", "state", "label", "album"):
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


def _normalize_prefix(prefix: str) -> str:
    p = prefix.strip().rstrip("/")
    if not p:
        return ""
    return p if p.startswith("/") else f"/{p}"


def _load_prefix_cache(base_url: str) -> str | None:
    if not PREFIX_CACHE_PATH.is_file():
        return None
    try:
        data = json.loads(PREFIX_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("base_url") == base_url.rstrip("/"):
            p = data.get("prefix")
            if isinstance(p, str) and p.strip():
                return _normalize_prefix(p)
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _save_prefix_cache(base_url: str, prefix: str) -> None:
    try:
        PREFIX_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFIX_CACHE_PATH.write_text(
            json.dumps({"base_url": base_url.rstrip("/"), "prefix": prefix}, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        log.warning("Nao foi possivel gravar cache de prefixo PhotoPrism: %s", e)


def _slug_candidates_from_host(hostname: str) -> list[str]:
    if not hostname:
        return []
    out: list[str] = [hostname]
    underscored = hostname.replace("-", "_")
    if underscored not in out:
        out.append(underscored)
    if hostname.endswith("-photoprism"):
        alt = hostname.replace("-photoprism", "_photoprism")
        if alt not in out:
            out.append(alt)
    return out


async def discover_ingress_prefix_redirect(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
) -> str | None:
    """Segue redirects do Ingress HA ate achar /api/hassio_ingress/TOKEN."""
    base = base_url.rstrip("/")
    url = f"{base}/"
    seen: set[str] = set()

    for _ in range(15):
        if url in seen:
            break
        seen.add(url)

        match = INGRESS_PREFIX_RE.search(url)
        if match:
            prefix = _normalize_prefix(match.group(1))
            log.info("PhotoPrism prefix via redirect: %s", prefix)
            return prefix

        try:
            r = await client.get(url, headers=headers, follow_redirects=False, timeout=15.0)
        except httpx.RequestError as e:
            log.warning("PhotoPrism discovery redirect falhou em %s: %s", url, e)
            break

        for candidate in (str(r.url), r.headers.get("location") or ""):
            m = INGRESS_PREFIX_RE.search(candidate)
            if m:
                prefix = _normalize_prefix(m.group(1))
                log.info("PhotoPrism prefix via Location/url: %s", prefix)
                return prefix

        if r.status_code not in (301, 302, 303, 307, 308):
            break

        loc = r.headers.get("location")
        if not loc:
            break
        url = urljoin(url, loc)

    return None


async def discover_ingress_prefix_supervisor(
    client: httpx.AsyncClient,
    supervisor_token: str,
    hostname: str,
) -> str | None:
    if not supervisor_token.strip():
        return None

    headers = {"Authorization": f"Bearer {supervisor_token.strip()}"}
    for slug in _slug_candidates_from_host(hostname):
        url = f"http://supervisor/addons/{slug}/info"
        try:
            r = await client.get(url, headers=headers, timeout=12.0)
        except httpx.RequestError:
            continue
        if r.status_code != 200:
            log.debug("Supervisor addons/%s/info -> %s", slug, r.status_code)
            continue
        try:
            payload = r.json()
        except Exception:
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            continue
        for key in ("ingress_entry", "ingress_path"):
            entry = data.get(key)
            if isinstance(entry, str) and "hassio_ingress" in entry:
                prefix = _normalize_prefix(entry.split("/library")[0].split("/login")[0])
                log.info("PhotoPrism prefix via Supervisor (%s): %s", slug, prefix)
                return prefix
    return None


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
        self._prefix = _normalize_prefix(api_prefix)
        self._token = token.strip()
        self._prefix_source = "config" if self._prefix else ""

    @property
    def api_base(self) -> str:
        return f"{self._base}{self._prefix}"

    @property
    def prefix_source(self) -> str:
        return self._prefix_source

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

    async def _api_reachable(self) -> bool:
        url = self._url("/api/v1/config")
        try:
            r = await self._client.get(url, headers=self._headers(), timeout=12.0)
        except httpx.RequestError:
            return False
        ct = (r.headers.get("content-type") or "").lower()
        if r.status_code in (200, 401) and "application/json" in ct:
            return True
        body = (r.text or "")[:200].lower()
        return r.status_code == 200 and not ("nginx" in body and "<html" in body)

    async def ensure_api_prefix(self, *, supervisor_token: str = "") -> bool:
        """Resolve prefixo Ingress HA se a API nao estiver na raiz."""
        if self._prefix and await self._api_reachable():
            return True

        if self._prefix and not await self._api_reachable():
            log.warning("PhotoPrism prefix configurado invalido: %s", self._prefix)
            self._prefix = ""

        cached = _load_prefix_cache(self._base)
        if cached:
            self._prefix = cached
            self._prefix_source = "cache"
            if await self._api_reachable():
                log.info("PhotoPrism API OK com prefixo em cache: %s", self._prefix)
                return True
            self._prefix = ""

        host = urlparse(self._base).hostname or ""
        via_sup = await discover_ingress_prefix_supervisor(
            self._client, supervisor_token, host
        )
        if via_sup:
            self._prefix = via_sup
            self._prefix_source = "supervisor"
            if await self._api_reachable():
                _save_prefix_cache(self._base, self._prefix)
                return True
            self._prefix = ""

        via_redir = await discover_ingress_prefix_redirect(
            self._client, self._base, self._headers()
        )
        if via_redir:
            self._prefix = via_redir
            self._prefix_source = "redirect"
            if await self._api_reachable():
                _save_prefix_cache(self._base, self._prefix)
                log.info("PhotoPrism API base: %s", self.api_base)
                return True

        return False

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
            str(r.request.url)[:220],
            r.status_code,
            r.headers.get("content-type", "-"),
            r.headers.get("server", "-"),
            is_pp,
            nginx_404,
        )
        if r.status_code >= 400 or not is_pp:
            log.warning("PhotoPrism corpo (%s): %s", context, _body_preview(body))

    async def probe(self, *, supervisor_token: str = "") -> dict[str, Any]:
        """Testa API (com auto-discovery de prefixo Ingress)."""
        await self.ensure_api_prefix(supervisor_token=supervisor_token)

        test_urls = [
            self._url("/api/v1/config"),
            self._url("/api/v1/photos?count=1&primary=true"),
        ]
        probes: list[dict[str, Any]] = []
        any_pp = False
        any_nginx = False

        for url in test_urls:
            try:
                r = await self._client.get(url, headers=self._headers(), timeout=15.0)
                body = r.text[:400] if r.text else ""
                is_pp, nginx_404 = _analyze_response(r.status_code, r.headers, body)
                any_pp = any_pp or is_pp
                any_nginx = any_nginx or nginx_404
                probes.append(
                    {
                        "url": url,
                        "status": r.status_code,
                        "content_type": r.headers.get("content-type", ""),
                        "server": r.headers.get("server", ""),
                        "looks_like_photoprism": is_pp,
                        "looks_like_nginx_404": nginx_404,
                        "body_preview": _body_preview(body),
                    }
                )
            except httpx.RequestError as e:
                probes.append({"url": url, "error": str(e)})

        hints: list[str] = []
        if any_pp:
            summary = "ok"
        elif any_nginx:
            summary = "ingress_sem_api"
            hints.append(
                "O add-on PhotoPrism usa Ingress do HA. O Shakira tenta detectar o prefixo "
                "/api/hassio_ingress/... automaticamente. Verifique o token (app password)."
            )
        else:
            summary = "unreachable_or_wrong_url"
            hints.append("Confirme photoprism_url=http://<slug>-photoprism:2342 e o app password.")

        if self._prefix:
            hints.insert(0, f"Prefixo API: {self._prefix} (fonte: {self._prefix_source or 'config'})")

        return {
            "base_url": self._base,
            "api_prefix": self._prefix or None,
            "api_base": self.api_base,
            "prefix_source": self._prefix_source or None,
            "token_set": bool(self._token),
            "summary": summary,
            "hints": hints,
            "probes": probes,
        }

    async def search_photos(
        self,
        *,
        filters: dict[str, Any] | None = None,
        count: int = 5,
        supervisor_token: str = "",
    ) -> tuple[list[PhotoResult], str | None]:
        if not await self.ensure_api_prefix(supervisor_token=supervisor_token):
            diag = await self.probe(supervisor_token=supervisor_token)
            raise PhotoprismError(
                "API PhotoPrism nao encontrada (Ingress HA?). Veja photoprism_probe no painel /status.",
                diagnostic=diag,
            )

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
        log.info(
            "PhotoPrism filtros=%s q=%r prefix=%s (%s)",
            filters,
            q or "(vazio)",
            self._prefix or "(raiz)",
            self._prefix_source,
        )

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
            diag = await self.probe(supervisor_token=supervisor_token)
            log.error("PhotoPrism diagnostico:\n%s", json.dumps(diag, indent=2))
            hint = diag["hints"][0] if diag.get("hints") else ""
            raise PhotoprismError(f"Busca falhou: HTTP {r.status_code}. {hint}", diagnostic=diag)

        preview_token = r.headers.get("X-Preview-Token") or r.headers.get("x-preview-token")
        try:
            rows = r.json()
        except Exception as e:
            raise PhotoprismError("Resposta JSON invalida") from e

        if not isinstance(rows, list):
            return [], preview_token

        log.info("PhotoPrism encontrou %s resultado(s)", len(rows))

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
        from urllib.parse import quote

        token = preview_token or "public"
        path = f"/api/v1/t/{quote(file_hash, safe='')}/{quote(token, safe='')}/{size}"
        url = self._url(path)
        self._log_request("GET", url)
        r = await self._client.get(url, headers=self._headers(json_accept=False), timeout=90.0)
        self._log_response(r, context="thumbnail")
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
    def __init__(self, message: str, *, diagnostic: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic or {}


class PhotoprismAuthError(PhotoprismError):
    pass
