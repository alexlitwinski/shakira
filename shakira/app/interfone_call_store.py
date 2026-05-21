"""Historico de chamadas do interfone (imagens + metadados)."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DEFAULT_INTERFONE_DATA_PATH = "/homeassistant/shakira_interfone"
FALLBACK_INTERFONE_DATA_PATHS: tuple[str, ...] = (
    "/homeassistant/shakira_interfone",
    "/config/shakira_interfone",
)
DEFAULT_TIMEZONE = os.environ.get("SHAKIRA_DEFAULT_TIMEZONE", "America/Sao_Paulo")
MAX_CALLS = int(os.environ.get("SHAKIRA_MAX_INTERFONE_CALLS", "300"))

_INTERFONE_ROOT: Path | None = None


def resolve_interfone_data_root(configured: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if configured and str(configured).strip():
        candidates.append(Path(str(configured).strip()))
    env = os.environ.get("SHAKIRA_INTERFONE_DATA_PATH", "").strip()
    if env:
        candidates.append(Path(env))
    for p in FALLBACK_INTERFONE_DATA_PATHS:
        candidates.append(Path(p))
    candidates.append(Path(DEFAULT_INTERFONE_DATA_PATH))

    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        parent = path.parent
        if path.is_dir() or parent.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            return path

    chosen = candidates[0]
    chosen.mkdir(parents=True, exist_ok=True)
    return chosen


def configure_interfone_data_root(path: Path | str | None = None) -> Path:
    global _INTERFONE_ROOT
    root = resolve_interfone_data_root(path)
    (root / "images").mkdir(parents=True, exist_ok=True)
    _INTERFONE_ROOT = root
    return root


def interfone_data_root() -> Path:
    if _INTERFONE_ROOT is None:
        return configure_interfone_data_root()
    return _INTERFONE_ROOT


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_local_datetime(iso_utc: str, *, tz_name: str = DEFAULT_TIMEZONE) -> str:
    raw = (iso_utc or "").strip()
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(ZoneInfo(tz_name))
        return local.strftime("%d/%m/%Y %H:%M")
    except (ValueError, ZoneInfoNotFoundError):
        return raw[:16]


@dataclass
class InterfoneCallRecord:
    id: str
    started_at: str
    camera_id: str
    gemini_summary: str = ""
    gemini_description: str = ""
    image_file: str = ""
    attend_window_seconds: int = 180
    portao_social_opened: bool = False
    portao_servico_opened: bool = False
    hall_person_detected: bool = False
    attended: bool = False
    finalized_at: str = ""

    def attended_label(self) -> str:
        return "Atendida" if self.attended else "Não atendida"

    def attend_details(self) -> str:
        parts: list[str] = []
        if self.portao_social_opened:
            parts.append("portão social aberto")
        if self.portao_servico_opened:
            parts.append("portão de serviço aberto")
        if self.hall_person_detected:
            parts.append("pessoa no hall interno")
        if not parts:
            return "Nenhum sinal de atendimento na janela."
        return "Sinais: " + "; ".join(parts) + "."


class InterfoneCallStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or interfone_data_root()
        self._index_path = self.root / "calls.json"
        self._images_dir = self.root / "images"
        self._images_dir.mkdir(parents=True, exist_ok=True)

    def _load_raw(self) -> dict[str, Any]:
        if not self._index_path.is_file():
            return {"calls": []}
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("interfone calls.json invalido: %s", e)
            return {"calls": []}
        if not isinstance(data, dict):
            return {"calls": []}
        return data

    def _save_raw(self, data: dict[str, Any]) -> None:
        calls = data.get("calls")
        if isinstance(calls, list) and len(calls) > MAX_CALLS:
            data["calls"] = calls[-MAX_CALLS:]
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._index_path)

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> InterfoneCallRecord | None:
        cid = str(row.get("id") or "").strip()
        if not cid:
            return None
        window = row.get("attend_window_seconds")
        try:
            window_sec = int(window) if window is not None else 180
        except (TypeError, ValueError):
            window_sec = 180
        return InterfoneCallRecord(
            id=cid,
            started_at=str(row.get("started_at") or ""),
            camera_id=str(row.get("camera_id") or ""),
            gemini_summary=str(row.get("gemini_summary") or ""),
            gemini_description=str(row.get("gemini_description") or ""),
            image_file=str(row.get("image_file") or ""),
            attend_window_seconds=max(30, window_sec),
            portao_social_opened=bool(row.get("portao_social_opened")),
            portao_servico_opened=bool(row.get("portao_servico_opened")),
            hall_person_detected=bool(row.get("hall_person_detected")),
            attended=bool(row.get("attended")),
            finalized_at=str(row.get("finalized_at") or ""),
        )

    def list_calls(self, *, limit: int = 10) -> list[InterfoneCallRecord]:
        data = self._load_raw()
        rows = data.get("calls") or []
        records: list[InterfoneCallRecord] = []
        for row in reversed(rows):
            if not isinstance(row, dict):
                continue
            rec = self._row_to_record(row)
            if rec:
                records.append(rec)
            if len(records) >= max(1, limit):
                break
        return records

    def get_call(self, call_id: str) -> InterfoneCallRecord | None:
        data = self._load_raw()
        for row in data.get("calls") or []:
            if isinstance(row, dict) and str(row.get("id")) == call_id:
                return self._row_to_record(row)
        return None

    def image_path(self, record: InterfoneCallRecord) -> Path | None:
        if not record.image_file:
            return None
        path = self._images_dir / record.image_file
        return path if path.is_file() else None

    def create_call(
        self,
        *,
        camera_id: str,
        image_bytes: bytes,
        gemini_summary: str,
        gemini_description: str,
        attend_window_seconds: int,
    ) -> InterfoneCallRecord:
        call_id = uuid.uuid4().hex[:12]
        image_name = ""
        if image_bytes:
            image_name = f"{call_id}.jpg"
            (self._images_dir / image_name).write_bytes(image_bytes)

        record = InterfoneCallRecord(
            id=call_id,
            started_at=_now_iso(),
            camera_id=camera_id,
            gemini_summary=gemini_summary.strip(),
            gemini_description=gemini_description.strip(),
            image_file=image_name,
            attend_window_seconds=max(30, attend_window_seconds),
        )
        data = self._load_raw()
        calls = data.setdefault("calls", [])
        if not isinstance(calls, list):
            calls = []
            data["calls"] = calls
        calls.append(asdict(record))
        self._save_raw(data)
        log.info("Chamada interfone registada id=%s camera=%s", call_id, camera_id)
        return record

    def update_call(self, record: InterfoneCallRecord) -> None:
        data = self._load_raw()
        calls = data.get("calls") or []
        updated = asdict(record)
        for i, row in enumerate(calls):
            if isinstance(row, dict) and str(row.get("id")) == record.id:
                calls[i] = updated
                self._save_raw(data)
                return
        calls.append(updated)
        self._save_raw(data)

    def finalize_call(
        self,
        call_id: str,
        *,
        portao_social_opened: bool,
        portao_servico_opened: bool,
        hall_person_detected: bool,
    ) -> InterfoneCallRecord | None:
        record = self.get_call(call_id)
        if not record:
            return None
        record.portao_social_opened = portao_social_opened
        record.portao_servico_opened = portao_servico_opened
        record.hall_person_detected = hall_person_detected
        record.attended = (
            portao_social_opened or portao_servico_opened or hall_person_detected
        )
        record.finalized_at = _now_iso()
        self.update_call(record)
        log.info(
            "Chamada interfone finalizada id=%s attended=%s",
            call_id,
            record.attended,
        )
        return record


def get_interfone_store(data_path: str | None = None) -> InterfoneCallStore:
    if data_path:
        root = configure_interfone_data_root(data_path)
    else:
        root = interfone_data_root()
    return InterfoneCallStore(root)


def count_interfone_calls() -> int:
    data = InterfoneCallStore()._load_raw()
    calls = data.get("calls") or []
    return len(calls) if isinstance(calls, list) else 0
