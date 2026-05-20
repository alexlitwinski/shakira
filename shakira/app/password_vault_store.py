"""Armazenamento encriptado de senhas por utilizador."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.user_memory import USER_DATA_ROOT, sanitize_phone
from app.vault_crypto import decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)

MAX_VAULT_ENTRIES = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class VaultEntry:
    id: str
    label: str
    secret_enc: str
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)


@dataclass
class VaultPending:
    mode: str  # save | retrieve_pick
    stage: str  # label | secret | pick
    label: str = ""
    candidates: list[str] = field(default_factory=list)


class PasswordVaultStore:
    def __init__(self, phone: str) -> None:
        self.phone = sanitize_phone(phone)
        self.root = USER_DATA_ROOT / self.phone
        self.vault_path = self.root / "vault.enc.json"
        self.pending_path = self.root / "vault_pending.json"

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _load_raw(self) -> list[dict[str, Any]]:
        if not self.vault_path.is_file():
            return []
        try:
            data = json.loads(self.vault_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return [x for x in data["entries"] if isinstance(x, dict)]
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except (json.JSONDecodeError, OSError) as e:
            log.warning("vault.enc.json corrompido phone=%s: %s", self.phone, e)
        return []

    def _save_raw(self, rows: list[dict[str, Any]]) -> None:
        self.ensure_dirs()
        payload = {"version": 1, "entries": rows}
        self.vault_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_labels(self) -> list[str]:
        return [str(r.get("label") or "") for r in self._load_raw() if r.get("label")]

    def find_entries(self, query: str) -> list[VaultEntry]:
        q = (query or "").strip().casefold()
        out: list[VaultEntry] = []
        for row in self._load_raw():
            label = str(row.get("label") or "")
            if not label:
                continue
            if not q or q in label.casefold():
                out.append(
                    VaultEntry(
                        id=str(row.get("id") or ""),
                        label=label,
                        secret_enc=str(row.get("secret_enc") or ""),
                        created_at=str(row.get("created_at") or ""),
                        updated_at=str(row.get("updated_at") or ""),
                    )
                )
        return [e for e in out if e.id and e.secret_enc]

    def get_entry(self, entry_id: str) -> VaultEntry | None:
        for row in self._load_raw():
            if str(row.get("id") or "") == entry_id:
                return VaultEntry(
                    id=entry_id,
                    label=str(row.get("label") or ""),
                    secret_enc=str(row.get("secret_enc") or ""),
                    created_at=str(row.get("created_at") or ""),
                    updated_at=str(row.get("updated_at") or ""),
                )
        return None

    def upsert_entry(
        self,
        *,
        master_key: str,
        label: str,
        secret: str,
    ) -> VaultEntry:
        label = label.strip()
        secret = secret.strip()
        if not label or not secret:
            raise ValueError("rotulo e senha sao obrigatorios")

        rows = self._load_raw()
        secret_enc = encrypt_secret(master_key, self.phone, secret)
        now = _now_iso()

        for row in rows:
            if str(row.get("label") or "").casefold() == label.casefold():
                row["secret_enc"] = secret_enc
                row["updated_at"] = now
                self._save_raw(rows)
                return VaultEntry(
                    id=str(row.get("id") or ""),
                    label=label,
                    secret_enc=secret_enc,
                    created_at=str(row.get("created_at") or now),
                    updated_at=now,
                )

        if len(rows) >= MAX_VAULT_ENTRIES:
            raise ValueError(f"limite de {MAX_VAULT_ENTRIES} senhas no cofre")

        entry = VaultEntry(
            id=uuid.uuid4().hex[:12],
            label=label,
            secret_enc=secret_enc,
            created_at=now,
            updated_at=now,
        )
        rows.append(asdict(entry))
        self._save_raw(rows)
        log.info("Senha guardada no cofre phone=%s label=%s", self.phone, label)
        return entry

    def decrypt_entry(self, entry: VaultEntry, *, master_key: str) -> str:
        return decrypt_secret(master_key, self.phone, entry.secret_enc)

    def load_pending(self) -> VaultPending | None:
        if not self.pending_path.is_file():
            return None
        try:
            row = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(row, dict):
            return None
        return VaultPending(
            mode=str(row.get("mode") or ""),
            stage=str(row.get("stage") or ""),
            label=str(row.get("label") or ""),
            candidates=[str(x) for x in row.get("candidates") or [] if str(x).strip()],
        )

    def save_pending(self, pending: VaultPending) -> None:
        self.ensure_dirs()
        self.pending_path.write_text(
            json.dumps(asdict(pending), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def clear_pending(self) -> None:
        if self.pending_path.is_file():
            try:
                self.pending_path.unlink(missing_ok=True)
            except OSError:
                pass


def get_vault_store(phone: str) -> PasswordVaultStore:
    return PasswordVaultStore(phone)
