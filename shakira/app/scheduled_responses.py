"""Respostas agendadas por utilizador (tempo ou estado de entidade)."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from app.user_memory import USER_DATA_ROOT, sanitize_phone

log = logging.getLogger(__name__)

MAX_PENDING_PER_USER = int(os.environ.get("SHAKIRA_MAX_SCHEDULED_PER_USER", "10"))
MAX_CONTEXT_ENTITIES = 5
DEFAULT_EXPIRE_DAYS = int(os.environ.get("SHAKIRA_SCHEDULED_EXPIRE_DAYS", "7"))
MAX_ENTITY_MISS_HOURS = int(os.environ.get("SHAKIRA_SCHEDULED_ENTITY_MISS_HOURS", "24"))

TriggerType = Literal["time", "entity"]
TriggerOn = Literal["enter", "match"]
ScheduleStatus = Literal["pending", "fired", "cancelled", "expired"]
ScheduleKind = Literal["notify", "action"]
MIN_ACTION_DELAY_SECONDS = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class ScheduledResponse:
    id: str
    phone: str
    created_at: str
    status: ScheduleStatus
    context: str
    label: str = ""
    kind: ScheduleKind = "notify"
    trigger_type: TriggerType = "entity"
    fire_at: str | None = None
    fire_after_seconds: int | None = None
    entity_id: str | None = None
    when_state: str | None = None
    trigger_on: TriggerOn = "enter"
    context_entities: list[str] = field(default_factory=list)
    expires_at: str | None = None
    last_known_state: str | None = None
    entity_miss_count: int = 0
    fired_at: str | None = None
    action_domain: str | None = None
    action_service: str | None = None
    action_service_data: dict[str, Any] = field(default_factory=dict)
    action_entity_id: str | None = None

    def is_pending(self) -> bool:
        return self.status == "pending"

    def effective_fire_at(self) -> datetime | None:
        if self.trigger_type != "time":
            return None
        if self.fire_at:
            return _parse_iso(self.fire_at)
        if self.fire_after_seconds and self.created_at:
            created = _parse_iso(self.created_at)
            if created:
                return created + timedelta(seconds=self.fire_after_seconds)
        return None

    def summary(self) -> str:
        trigger_part: str
        if self.trigger_type == "time":
            fire = self.effective_fire_at()
            when = fire.isoformat() if fire else "tempo indefinido"
            trigger_part = f"time @ {when}"
        else:
            trigger_part = f"entity {self.entity_id} {self.when_state} ({self.trigger_on})"
        if self.kind == "action":
            action = self.action_entity_id or "?"
            svc = f"{self.action_domain}/{self.action_service}" if self.action_domain else "acao"
            return f"acao {svc} em {action} · {trigger_part}"
        return trigger_part


class ScheduledResponsesStore:
    """Persiste agendamentos em {USER_DATA_ROOT}/{phone}/scheduled_responses.json."""

    def __init__(self, phone: str) -> None:
        self.phone = sanitize_phone(phone)
        self.root = USER_DATA_ROOT / self.phone
        self.path = self.root / "scheduled_responses.json"

    def _load_raw(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except (json.JSONDecodeError, OSError) as e:
            log.warning("scheduled_responses.json corrompido phone=%s: %s", self.phone, e)
        return []

    def _save_raw(self, rows: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _row_to_entry(self, row: dict[str, Any]) -> ScheduledResponse | None:
        sid = str(row.get("id") or "").strip()
        ctx = str(row.get("context") or "").strip()
        if not sid or not ctx:
            return None
        trigger_type = str(row.get("trigger_type") or "entity").lower()
        if trigger_type not in ("time", "entity"):
            trigger_type = "entity"
        trigger_on = str(row.get("trigger_on") or "enter").lower()
        if trigger_on not in ("enter", "match"):
            trigger_on = "enter"
        status = str(row.get("status") or "pending").lower()
        if status not in ("pending", "fired", "cancelled", "expired"):
            status = "pending"
        entities_raw = row.get("context_entities")
        context_entities: list[str] = []
        if isinstance(entities_raw, list):
            for e in entities_raw[:MAX_CONTEXT_ENTITIES]:
                if isinstance(e, str) and e.strip():
                    context_entities.append(e.strip())
        fire_after = row.get("fire_after_seconds")
        fire_after_seconds = int(fire_after) if isinstance(fire_after, (int, float)) else None
        kind = str(row.get("kind") or "notify").lower()
        if kind not in ("notify", "action"):
            kind = "notify"
        action_data_raw = row.get("action_service_data")
        action_service_data: dict[str, Any] = {}
        if isinstance(action_data_raw, dict):
            action_service_data = dict(action_data_raw)
        return ScheduledResponse(
            id=sid,
            phone=str(row.get("phone") or self.phone),
            created_at=str(row.get("created_at") or _now_iso()),
            status=status,  # type: ignore[arg-type]
            context=ctx,
            label=str(row.get("label") or "").strip(),
            kind=kind,  # type: ignore[arg-type]
            trigger_type=trigger_type,  # type: ignore[arg-type]
            fire_at=str(row.get("fire_at")).strip() if row.get("fire_at") else None,
            fire_after_seconds=fire_after_seconds,
            entity_id=str(row.get("entity_id")).strip() if row.get("entity_id") else None,
            when_state=str(row.get("when_state")).strip() if row.get("when_state") else None,
            trigger_on=trigger_on,  # type: ignore[arg-type]
            context_entities=context_entities,
            expires_at=str(row.get("expires_at")).strip() if row.get("expires_at") else None,
            last_known_state=str(row.get("last_known_state")).strip()
            if row.get("last_known_state") is not None
            else None,
            entity_miss_count=int(row.get("entity_miss_count") or 0),
            fired_at=str(row.get("fired_at")).strip() if row.get("fired_at") else None,
            action_domain=str(row.get("action_domain")).strip() if row.get("action_domain") else None,
            action_service=str(row.get("action_service")).strip() if row.get("action_service") else None,
            action_service_data=action_service_data,
            action_entity_id=str(row.get("action_entity_id")).strip()
            if row.get("action_entity_id")
            else None,
        )

    def list_all(self) -> list[ScheduledResponse]:
        out: list[ScheduledResponse] = []
        for row in self._load_raw():
            entry = self._row_to_entry(row)
            if entry:
                out.append(entry)
        return out

    def list_pending(self) -> list[ScheduledResponse]:
        return [s for s in self.list_all() if s.is_pending()]

    def count_pending(self) -> int:
        return len(self.list_pending())

    def get_by_id(self, schedule_id: str) -> ScheduledResponse | None:
        sid = schedule_id.strip()
        for entry in self.list_all():
            if entry.id == sid:
                return entry
        return None

    def find_by_label(self, label: str) -> ScheduledResponse | None:
        needle = label.strip().lower()
        if not needle:
            return None
        pending = self.list_pending()
        for entry in pending:
            if entry.label.lower() == needle:
                return entry
        for entry in pending:
            if needle in entry.label.lower() or needle in entry.context.lower():
                return entry
        return None

    def add(
        self,
        *,
        context: str,
        trigger_type: TriggerType,
        label: str = "",
        kind: ScheduleKind = "notify",
        fire_at: str | None = None,
        fire_after_seconds: int | None = None,
        entity_id: str | None = None,
        when_state: str | None = None,
        trigger_on: TriggerOn = "enter",
        context_entities: list[str] | None = None,
        last_known_state: str | None = None,
        action_domain: str | None = None,
        action_service: str | None = None,
        action_service_data: dict[str, Any] | None = None,
        action_entity_id: str | None = None,
    ) -> ScheduledResponse:
        pending = self.list_pending()
        if len(pending) >= MAX_PENDING_PER_USER:
            raise ValueError(f"limite de {MAX_PENDING_PER_USER} agendamentos pendentes atingido")

        entities: list[str] = []
        if context_entities:
            for e in context_entities[:MAX_CONTEXT_ENTITIES]:
                if isinstance(e, str) and e.strip():
                    entities.append(e.strip())
        if entity_id and entity_id.strip() and entity_id.strip() not in entities:
            entities.insert(0, entity_id.strip())
            entities = entities[:MAX_CONTEXT_ENTITIES]

        expires = datetime.now(timezone.utc) + timedelta(days=DEFAULT_EXPIRE_DAYS)
        entry = ScheduledResponse(
            id=uuid.uuid4().hex[:12],
            phone=self.phone,
            created_at=_now_iso(),
            status="pending",
            context=context.strip(),
            label=label.strip()[:120],
            kind=kind,
            trigger_type=trigger_type,
            fire_at=fire_at,
            fire_after_seconds=fire_after_seconds,
            entity_id=entity_id.strip() if entity_id else None,
            when_state=when_state.strip() if when_state else None,
            trigger_on=trigger_on,
            context_entities=entities,
            expires_at=expires.isoformat(),
            last_known_state=last_known_state,
            action_domain=action_domain.strip() if action_domain else None,
            action_service=action_service.strip() if action_service else None,
            action_service_data=dict(action_service_data) if action_service_data else {},
            action_entity_id=action_entity_id.strip() if action_entity_id else None,
        )
        rows = self._load_raw()
        rows.append(asdict(entry))
        self._save_raw(rows)
        log.info(
            "Agendamento criado phone=%s id=%s trigger=%s",
            self.phone,
            entry.id,
            entry.summary(),
        )
        return entry

    def update(self, entry: ScheduledResponse) -> None:
        rows = self._load_raw()
        updated = False
        for i, row in enumerate(rows):
            if str(row.get("id")) == entry.id:
                rows[i] = asdict(entry)
                updated = True
                break
        if not updated:
            rows.append(asdict(entry))
        self._save_raw(rows)

    def mark_fired(self, schedule_id: str) -> ScheduledResponse | None:
        entry = self.get_by_id(schedule_id)
        if not entry or not entry.is_pending():
            return None
        entry.status = "fired"
        entry.fired_at = _now_iso()
        self.update(entry)
        log.info("Agendamento disparado phone=%s id=%s", self.phone, schedule_id)
        return entry

    def cancel(self, schedule_id: str) -> bool:
        entry = self.get_by_id(schedule_id)
        if not entry or not entry.is_pending():
            return False
        entry.status = "cancelled"
        self.update(entry)
        log.info("Agendamento cancelado phone=%s id=%s", self.phone, schedule_id)
        return True

    def mark_expired(self, schedule_id: str) -> None:
        entry = self.get_by_id(schedule_id)
        if not entry or not entry.is_pending():
            return
        entry.status = "expired"
        self.update(entry)
        log.info("Agendamento expirado phone=%s id=%s", self.phone, schedule_id)


def get_scheduled_store(phone: str) -> ScheduledResponsesStore:
    return ScheduledResponsesStore(phone)


def load_all_pending_globally() -> list[ScheduledResponse]:
    """Carrega todos os agendamentos pending de todos os utilizadores."""
    root = USER_DATA_ROOT
    if not root.is_dir():
        return []
    out: list[ScheduledResponse] = []
    for user_dir in root.iterdir():
        if not user_dir.is_dir():
            continue
        path = user_dir / "scheduled_responses.json"
        if not path.is_file():
            continue
        try:
            phone = sanitize_phone(user_dir.name)
        except ValueError:
            continue
        store = ScheduledResponsesStore(phone)
        out.extend(store.list_pending())
    return out


def count_all_pending_globally() -> int:
    return len(load_all_pending_globally())


def ensure_runner_started() -> None:
    """Inicia o executor se houver agendamentos pending (import tardio)."""
    from app.scheduled_responses_runner import ensure_scheduled_runner_running

    ensure_scheduled_runner_running()


def format_pending_for_prompt(entries: list[ScheduledResponse]) -> str:
    if not entries:
        return ""
    lines = ["Agendamentos pendentes deste usuario:"]
    for entry in entries:
        label_part = f' label="{entry.label}"' if entry.label else ""
        kind_part = " [acao]" if entry.kind == "action" else ""
        lines.append(
            f'- id={entry.id}{kind_part}{label_part}: {entry.summary()} — {entry.context[:120]}'
        )
    return "\n".join(lines)


def serialize_pending_item(entry: ScheduledResponse) -> dict[str, Any]:
    """Representacao JSON de um agendamento pending para API/painel."""
    fire = entry.effective_fire_at()
    return {
        "id": entry.id,
        "phone": entry.phone,
        "kind": entry.kind,
        "label": entry.label,
        "context": entry.context,
        "trigger_type": entry.trigger_type,
        "trigger_summary": entry.summary(),
        "entity_id": entry.entity_id,
        "when_state": entry.when_state,
        "trigger_on": entry.trigger_on,
        "fire_at": entry.fire_at,
        "fire_after_seconds": entry.fire_after_seconds,
        "effective_fire_at": fire.isoformat() if fire else None,
        "context_entities": list(entry.context_entities),
        "created_at": entry.created_at,
        "expires_at": entry.expires_at,
        "last_known_state": entry.last_known_state,
        "action_domain": entry.action_domain,
        "action_service": entry.action_service,
        "action_entity_id": entry.action_entity_id,
        "action_service_data": dict(entry.action_service_data),
    }
