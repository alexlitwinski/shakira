"""Cliente da API REST do PhotoPrism (busca, thumbnails, auto-discovery Ingress HA)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx

log = logging.getLogger(__name__)

_UPLOAD_COUNT_RE = re.compile(
    r"(\d+)\s+(?:files?|arquivos?)\s+(?:uploaded|enviados?)",
    re.IGNORECASE,
)
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/3gpp": ".3gp",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
}
_EXT_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".3gp": "video/3gpp",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
}

DEFAULT_THUMB_SIZE = "fit_720"
UPLOAD_APPROVE_POLL_ATTEMPTS = 3
UPLOAD_APPROVE_POLL_DELAY_SEC = 2.0
UPLOAD_TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
UPLOAD_TOKEN_LENGTH = 7
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

# Cenas/objetos PT (e typos comuns) -> etiquetas PhotoPrism (ingles, geradas por IA).
_SCENE_PT_TO_LABELS: dict[str, list[str]] = {
    "praia": ["beach", "coast", "sea", "sand"],
    "paria": ["beach", "coast", "sea", "sand"],
    "beach": ["beach", "coast", "sea", "sand"],
    "mar": ["sea", "beach", "coast", "ocean"],
    "oceano": ["ocean", "sea"],
    "montanha": ["mountain", "mountains", "peak"],
    "montanhas": ["mountain", "mountains", "peak"],
    "neve": ["snow"],
    "floresta": ["forest", "woods", "tree"],
    "cachoeira": ["waterfall"],
    "por do sol": ["sunset"],
    "pôr do sol": ["sunset"],
    "sunset": ["sunset"],
    "nascer do sol": ["sunrise"],
    "sunrise": ["sunrise"],
    "pool": ["pool", "swimming"],
    "piscina": ["pool", "swimming"],
    "restaurante": ["restaurant", "food"],
    "comida": ["food", "meal"],
    "animal": ["animal"],
    "cachorro": ["dog"],
    "gato": ["cat"],
    "flor": ["flower", "flowers"],
    "flores": ["flower", "flowers"],
    "casamento": ["wedding"],
    "festa": ["party", "celebration"],
    "parque": ["park"],
    "lago": ["lake"],
    "rio": ["river"],
    "deserto": ["desert"],
    "cidade": ["city", "urban"],
    "igreja": ["church"],
    "museu": ["museum"],
    "hotel": ["hotel"],
    "barco": ["boat", "ship"],
    "avião": ["airplane", "aircraft"],
    "aviao": ["airplane", "aircraft"],
}

_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "o",
        "as",
        "os",
        "da",
        "de",
        "do",
        "das",
        "dos",
        "na",
        "no",
        "nas",
        "nos",
        "em",
        "um",
        "uma",
        "e",
        "foto",
        "fotos",
        "imagem",
        "imagens",
        "quero",
        "mostra",
        "mostrar",
        "ver",
        "buscar",
        "busca",
    }
)


@dataclass(frozen=True)
class PhotoResult:
    uid: str
    file_hash: str
    title: str
    taken_at: str
    place_label: str
    preview_token: str


@dataclass(frozen=True)
class UploadResult:
    photo_uid: str
    files_uploaded: int
    import_processed: bool
    photos_approved: int
    has_video: bool = False


def format_upload_user_message(result: UploadResult, *, album: str = "") -> str:
    album_bit = f" no álbum *{album.strip()}*" if album.strip() else ""
    count = max(result.files_uploaded, 1)
    if result.files_uploaded > 0 and result.import_processed:
        if count > 1:
            if result.has_video:
                noun = f"{count} mídias enviadas"
            else:
                noun = f"{count} fotos enviadas"
        elif result.has_video:
            noun = "Vídeo enviado"
        else:
            noun = "Foto enviada"
        return f"{noun} ao PhotoPrism{album_bit}."
    if result.files_uploaded > 0:
        return (
            "O PhotoPrism recebeu o arquivo, mas a importação não foi concluída. "
            "Tente novamente em instantes."
        )
    return "Não foi possível confirmar o envio ao PhotoPrism."


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


def _norm_token(text: str) -> str:
    return text.strip().casefold()


def map_scene_labels(text: str) -> list[str]:
    """Mapeia termo de cena PT/EN para etiquetas PhotoPrism."""
    key = _norm_token(text)
    if not key:
        return []
    if key in _SCENE_PT_TO_LABELS:
        return list(_SCENE_PT_TO_LABELS[key])
    return []


def extract_scene_labels(text: str) -> list[str]:
    """Extrai etiquetas de cena de frase curta (ex.: 'na praia' -> beach, ...)."""
    labels: list[str] = []
    whole = map_scene_labels(text)
    if whole:
        return whole
    for word in re.findall(r"[\w\u00C0-\u024F'-]+", text, re.UNICODE):
        w = _norm_token(word)
        if not w or w in _QUERY_STOPWORDS:
            continue
        mapped = map_scene_labels(w)
        if mapped:
            labels.extend(mapped)
    seen: set[str] = set()
    out: list[str] = []
    for lb in labels:
        k = lb.casefold()
        if k not in seen:
            seen.add(k)
            out.append(lb)
    return out


def _merge_label_values(*groups: str | list[str] | None) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        if group is None:
            continue
        items = [group] if isinstance(group, str) else group
        for item in items:
            if not isinstance(item, str):
                continue
            for part in re.split(r"[|,]", item):
                p = part.strip()
                if not p:
                    continue
                k = p.casefold()
                if k not in seen:
                    seen.add(k)
                    out.append(p)
    return "|".join(out)


def _is_likely_city_name(text: str) -> bool:
    """Evita tratar cidade conhecida como etiqueta de cena."""
    key = _norm_token(text)
    if key in _CITY_PT_TO_EN:
        return True
    if len(text.split()) >= 2 and not map_scene_labels(text):
        return True
    return False


def _resolve_label_field(label: str) -> str:
    """Converte label PT de cena para etiquetas EN; mantem labels EN desconhecidas."""
    mapped = extract_scene_labels(label)
    if mapped:
        return _merge_label_values(mapped)
    return _merge_label_values(label)


def normalize_scene_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """
    Converte termos de cena (praia, montanha) para label PhotoPrism.
    Remove query livre que buscaria titulos/legendas em vez de etiquetas.
    """
    out = dict(filters)

    label_val = out.get("label")
    if isinstance(label_val, str) and label_val.strip():
        out["label"] = _resolve_label_field(label_val.strip())

    for field in ("query", "city"):
        val = out.get(field)
        if not isinstance(val, str) or not val.strip():
            continue
        raw = val.strip()

        scene = extract_scene_labels(raw)
        if scene:
            out["label"] = _merge_label_values(out.get("label"), scene)
            out.pop(field, None)
            log.info(
                "PhotoPrism: %s=%r convertido para label=%s",
                field,
                raw,
                out.get("label"),
            )
            continue

        if field == "city" and not _is_likely_city_name(raw):
            maybe = map_scene_labels(raw)
            if maybe:
                out["label"] = _merge_label_values(out.get("label"), maybe)
                out.pop(field, None)
                log.info(
                    "PhotoPrism: city=%r tratada como cena -> label=%s",
                    raw,
                    out.get("label"),
                )

    free = out.get("query")
    if isinstance(free, str) and free.strip():
        stripped = free.strip()
        if re.search(r"\w\s*:", stripped):
            pass
        else:
            scene = extract_scene_labels(stripped)
            if scene:
                out["label"] = _merge_label_values(out.get("label"), scene)
            log.warning(
                "PhotoPrism: query livre ignorada (use label/keywords, nao titulo): %r",
                stripped,
            )
            out.pop("query", None)

    return out


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


def parse_people_names(raw: Any) -> list[str]:
    """Extrai lista de nomes de people/person/people_list."""
    if isinstance(raw, list):
        names: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                names.extend(parse_people_names(item))
        return _dedupe_people(names)
    if not isinstance(raw, str):
        return []
    s = raw.strip()
    if not s:
        return []
    if "&" in s:
        return _dedupe_people(p.strip() for p in s.split("&") if p.strip())
    if "|" in s:
        return _dedupe_people(p.strip() for p in s.split("|") if p.strip())
    parts = re.split(r",|;|\be\b", s, flags=re.IGNORECASE)
    return _dedupe_people(p.strip() for p in parts if p.strip())


def _dedupe_people(names: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if not isinstance(name, str):
            continue
        n = name.strip()
        if not n:
            continue
        key = n.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def format_people_filter(names: list[str], *, mode: str = "all") -> str:
    """Formata filtro people do PhotoPrism (& = todas, | = qualquer)."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    sep = " & " if mode == "all" else " | "
    return sep.join(names)


def normalize_people_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Normaliza pessoa(s): multiplos nomes usam AND (&) por padrao no PhotoPrism."""
    out = dict(filters)
    mode_raw = out.pop("people_mode", None)
    mode = str(mode_raw or "all").strip().lower()
    if mode not in ("all", "any"):
        mode = "all"

    names: list[str] = []
    people_list = out.pop("people_list", None)
    if isinstance(people_list, list):
        names = parse_people_names(people_list)

    people_raw = out.get("people")
    if people_raw is not None:
        parsed = parse_people_names(people_raw)
        if parsed:
            names = parsed
        if isinstance(people_raw, str) and "|" in people_raw and mode_raw is None:
            mode = "any"

    person_raw = out.pop("person", None)
    if person_raw is not None and not names:
        names = parse_people_names(person_raw)

    if names:
        out["people"] = format_people_filter(names, mode=mode)
        if len(names) > 1:
            out["_people_mode"] = mode
            out["_people_names"] = names
    else:
        out.pop("people", None)

    return out


def normalize_photo_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Prefer people (flexivel); multiplas pessoas com AND; cenas -> label PhotoPrism."""
    return normalize_scene_filters(normalize_people_filters(filters))


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
        place_bits = [t.replace(" ", "-") for t in place_terms if len(t) >= 4]
        if place_bits:
            kw["keywords"] = "|".join(place_bits[:3])
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

    label = filters.get("label")
    if isinstance(label, str) and label.strip():
        qval("label", _merge_label_values(label))

    keywords = filters.get("keywords")
    if isinstance(keywords, str) and keywords.strip():
        qval("keywords", _merge_label_values(keywords))

    for key in ("country", "state", "album"):
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
        stripped = free.strip()
        if re.search(r"\w\s*:", stripped):
            parts.append(stripped)
        else:
            log.warning(
                "PhotoPrism: query livre omitida da busca (sem filtro estruturado): %r",
                stripped,
            )

    return " ".join(parts)


def _body_preview(text: str, limit: int = 280) -> str:
    t = " ".join(text.split())
    if len(t) <= limit:
        return t
    return t[: limit - 3] + "..."


def _parse_pp_json_response(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _pp_response_message(data: dict[str, Any]) -> str:
    for key in ("message", "error", "details", "Msg"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_upload_count(message: str) -> int | None:
    if not message:
        return None
    match = _UPLOAD_COUNT_RE.search(message)
    if match:
        return int(match.group(1))
    low = message.casefold()
    if "file uploaded" in low and "files uploaded" not in low:
        return 1
    if "arquivo enviado" in low and "arquivos enviados" not in low:
        return 1
    return None


def _is_import_processed_message(message: str) -> bool:
    low = message.casefold()
    return (
        "upload has been processed" in low
        or "upload foi processado" in low
        or "upload was processed" in low
        or "o upload foi processado" in low
    )


def _photo_recently_touched(row: dict[str, Any], since_ts: float) -> bool:
    for field in ("UpdatedAt", "CreatedAt", "EditedAt"):
        ts = _parse_iso_timestamp(str(row.get(field) or ""))
        if ts is not None and ts >= since_ts - 30:
            return True
    return False


def _normalize_upload_filename(filename: str, mime_type: str = "") -> tuple[str, str]:
    name = (filename.strip() or "arquivo").replace("\\", "/").split("/")[-1]
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    known_exts = tuple(_EXT_TO_MIME)

    lower = name.casefold()
    has_ext = any(lower.endswith(ext) for ext in known_exts)
    if not has_ext:
        if mime.startswith("video/"):
            ext = _MIME_TO_EXT.get(mime, ".mp4")
        else:
            ext = _MIME_TO_EXT.get(mime, ".jpg")
        name = f"{name}{ext}"
        if not mime:
            mime = _EXT_TO_MIME.get(ext, "image/jpeg")

    if not mime:
        for ext, content_type in _EXT_TO_MIME.items():
            if lower.endswith(ext):
                mime = content_type
                break
    if not mime:
        mime = "image/jpeg"

    return name, mime


def _parse_iso_timestamp(value: str) -> float | None:
    raw = value.strip()
    if not raw or raw.startswith("0001-"):
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        from datetime import datetime

        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


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


def _generate_upload_token(length: int = UPLOAD_TOKEN_LENGTH) -> str:
    return "".join(secrets.choice(UPLOAD_TOKEN_ALPHABET) for _ in range(length))


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
        self._user_uid = ""

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

    async def find_album_uid(self, name: str, *, supervisor_token: str = "") -> str | None:
        """Busca album por titulo (match parcial) e retorna UID."""
        query = name.strip()
        if not query:
            return None
        if not await self.ensure_api_prefix(supervisor_token=supervisor_token):
            return None

        url = self._url("/api/v1/albums")
        params: dict[str, str | int] = {"count": 50, "q": query}
        self._log_request("GET", url, params)
        try:
            r = await self._client.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=30.0,
            )
        except httpx.RequestError:
            return None
        if r.status_code >= 400:
            return None
        try:
            rows = r.json()
        except Exception:
            return None
        if not isinstance(rows, list):
            return None

        q_low = query.casefold()
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("Title") or row.get("title") or "").strip()
            uid = str(row.get("UID") or row.get("uid") or "").strip()
            if not uid:
                continue
            if title.casefold() == q_low or q_low in title.casefold():
                return uid
        if rows and isinstance(rows[0], dict):
            uid = str(rows[0].get("UID") or rows[0].get("uid") or "").strip()
            return uid or None
        return None

    async def _get_user_uid(self, *, supervisor_token: str = "") -> str:
        """UID do usuario autenticado (necessario para upload via API)."""
        if self._user_uid:
            return self._user_uid

        if not await self.ensure_api_prefix(supervisor_token=supervisor_token):
            raise PhotoprismError("API PhotoPrism indisponivel para upload.")

        url = self._url("/api/v1/session")
        self._log_request("GET", url)
        try:
            r = await self._client.get(url, headers=self._headers(), timeout=15.0)
        except httpx.RequestError as e:
            raise PhotoprismError(f"Sessao PhotoPrism indisponivel: {e}") from e

        self._log_response(r, context="session")
        if r.status_code == 401:
            raise PhotoprismAuthError("Token PhotoPrism invalido ou expirado")
        if r.status_code >= 400:
            raise PhotoprismError(f"Sessao PhotoPrism falhou: HTTP {r.status_code}")

        try:
            payload = r.json()
        except Exception as e:
            raise PhotoprismError("Resposta de sessao PhotoPrism invalida") from e

        user = payload.get("user") if isinstance(payload, dict) else None
        if isinstance(user, dict):
            uid = str(user.get("UID") or user.get("uid") or "").strip()
            if uid:
                self._user_uid = uid
                return uid

        raise PhotoprismError("Nao foi possivel obter o usuario do PhotoPrism.")

    async def _collect_recent_photo_uids(self, since_ts: float, *, limit: int = 5) -> list[str]:
        url = self._url("/api/v1/photos")
        params: dict[str, str | int | bool] = {
            "count": limit,
            "offset": 0,
            "merged": True,
            "primary": True,
            "order": "added",
        }
        try:
            r = await self._client.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=20.0,
            )
        except httpx.RequestError:
            return []
        if r.status_code >= 400:
            return []
        try:
            rows = r.json()
        except Exception:
            return []
        if not isinstance(rows, list):
            return []

        uids: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            uid = str(row.get("UID") or row.get("uid") or "").strip()
            if uid and _photo_recently_touched(row, since_ts):
                uids.append(uid)
        return uids

    async def _approve_photo_uids(self, uids: list[str]) -> int:
        clean = [uid for uid in uids if uid]
        if not clean:
            return 0

        url = self._url("/api/v1/batch/photos/approve")
        self._log_request("POST", url)
        try:
            r = await self._client.post(
                url,
                headers=self._headers(),
                json={"photos": clean},
                timeout=30.0,
            )
        except httpx.RequestError as e:
            log.warning("PhotoPrism aprovar fotos falhou: %s", e)
            return 0

        self._log_response(r, context="approve")
        if r.status_code == 401:
            raise PhotoprismAuthError("Token PhotoPrism invalido ou expirado")
        if r.status_code >= 400:
            log.warning(
                "PhotoPrism aprovar fotos HTTP %s: %s",
                r.status_code,
                _body_preview(r.text or ""),
            )
            return 0

        log.info("PhotoPrism aprovou %s foto(s) apos upload", len(clean))
        return len(clean)

    async def _approve_recent_uploads(self, since_ts: float, *, expected: int = 1) -> tuple[str, int]:
        """Aguarda indexacao breve e tira fotos do estado A revisar."""
        limit = max(1, min(expected, 5))
        uids: list[str] = []
        for attempt in range(UPLOAD_APPROVE_POLL_ATTEMPTS):
            uids = await self._collect_recent_photo_uids(since_ts, limit=limit)
            if uids:
                break
            if attempt + 1 < UPLOAD_APPROVE_POLL_ATTEMPTS:
                await asyncio.sleep(UPLOAD_APPROVE_POLL_DELAY_SEC)

        if not uids:
            log.warning("PhotoPrism: foto importada nao encontrada para aprovar")
            return "", 0

        approved = await self._approve_photo_uids(uids[:expected])
        return uids[0], approved

    async def upload_media_files(
        self,
        files: list[tuple[bytes, str, str]],
        *,
        album: str = "",
        supervisor_token: str = "",
    ) -> UploadResult:
        """Envia um ou mais arquivos (foto/video) ao PhotoPrism."""
        clean_files = [(raw, name, mime) for raw, name, mime in files if raw]
        if not clean_files:
            raise PhotoprismError("Arquivo vazio; nada para enviar ao PhotoPrism.")

        has_video = any(
            (mime or "").startswith("video/") or name.lower().endswith((".mp4", ".mov", ".webm", ".mkv", ".avi", ".3gp"))
            for _, name, mime in clean_files
        )

        if not await self.ensure_api_prefix(supervisor_token=supervisor_token):
            diag = await self.probe(supervisor_token=supervisor_token)
            raise PhotoprismError(
                "API PhotoPrism indisponivel para envio.",
                diagnostic=diag,
            )

        user_uid = await self._get_user_uid(supervisor_token=supervisor_token)
        upload_token = _generate_upload_token()
        upload_url = self._url(f"/api/v1/users/{user_uid}/upload/{upload_token}")
        upload_started_at = time.time()

        album_uid = ""
        album_name = album.strip()
        if album_name:
            album_uid = await self.find_album_uid(album_name, supervisor_token=supervisor_token) or ""

        headers = self._headers(json_accept=False)
        headers.pop("Accept", None)

        files_uploaded = 0
        for raw, filename, mime_type in clean_files:
            safe_name, content_type = _normalize_upload_filename(filename, mime_type)
            multipart = {"files": (safe_name, raw, content_type)}
            self._log_request("POST", upload_url)
            try:
                r = await self._client.post(
                    upload_url,
                    headers=headers,
                    files=multipart,
                    timeout=180.0,
                )
            except httpx.RequestError as e:
                raise PhotoprismError(f"Envio ao PhotoPrism falhou: {e}") from e

            self._log_response(r, context="upload")

            if r.status_code == 401:
                raise PhotoprismAuthError("Token PhotoPrism invalido ou expirado")
            if r.status_code >= 400:
                body = _body_preview(r.text or "")
                detail = f": {body}" if body else ""
                raise PhotoprismError(f"Upload falhou: HTTP {r.status_code}{detail}")

            post_payload = _parse_pp_json_response(r)
            post_message = _pp_response_message(post_payload)
            if post_message:
                log.info("PhotoPrism upload POST resposta: %s", post_message)
            count = _extract_upload_count(post_message)
            if count == 0:
                detail = post_message or "nenhum arquivo aceito"
                raise PhotoprismError(
                    f"PhotoPrism rejeitou {safe_name} ({detail}). "
                    "Verifique formato, tamanho e permissoes de upload."
                )
            files_uploaded += count if count is not None else 1

        process_url = self._url(f"/api/v1/users/{user_uid}/upload/{upload_token}")
        process_body: dict[str, list[str]] = {}
        if album_uid:
            process_body["albums"] = [album_uid]

        self._log_request("PUT", process_url)
        try:
            r2 = await self._client.put(
                process_url,
                headers=self._headers(),
                json=process_body,
                timeout=240.0,
            )
        except httpx.RequestError as e:
            raise PhotoprismError(f"Importacao no PhotoPrism falhou: {e}") from e

        self._log_response(r2, context="upload-process")

        if r2.status_code == 401:
            raise PhotoprismAuthError("Token PhotoPrism invalido ou expirado")
        if r2.status_code >= 400:
            body = _body_preview(r2.text or "")
            detail = f": {body}" if body else ""
            raise PhotoprismError(f"Importacao falhou: HTTP {r2.status_code}{detail}")

        process_payload = _parse_pp_json_response(r2)
        process_message = _pp_response_message(process_payload)
        if process_message:
            log.info("PhotoPrism upload PUT resposta: %s", process_message)

        import_processed = _is_import_processed_message(process_message) or r2.status_code == 200
        photo_uid = ""
        photos_approved = 0
        if import_processed and files_uploaded > 0:
            photo_uid, photos_approved = await self._approve_recent_uploads(
                upload_started_at,
                expected=files_uploaded,
            )
        return UploadResult(
            photo_uid=photo_uid,
            files_uploaded=files_uploaded,
            import_processed=import_processed,
            photos_approved=photos_approved,
            has_video=has_video,
        )

    async def upload_photo(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        album: str = "",
        mime_type: str = "",
        supervisor_token: str = "",
    ) -> UploadResult:
        """Envia um arquivo ao PhotoPrism."""
        return await self.upload_media_files(
            [(file_bytes, filename, mime_type)],
            album=album,
            supervisor_token=supervisor_token,
        )

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
