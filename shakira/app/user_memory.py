"""Memoria persistente e arquivos por usuario (telefone WhatsApp)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

USER_DATA_ROOT = Path(os.environ.get("SHAKIRA_USER_DATA_ROOT", "/data/shakira_users"))
MAX_MEMORIES = int(os.environ.get("SHAKIRA_MAX_MEMORIES_PER_USER", "200"))
MAX_MEMORY_CHARS = int(os.environ.get("SHAKIRA_MAX_MEMORY_ENTRY_CHARS", "4000"))
MAX_FILES = int(os.environ.get("SHAKIRA_MAX_FILES_PER_USER", "50"))
MAX_FILE_BYTES = int(os.environ.get("SHAKIRA_MAX_FILE_BYTES", str(15 * 1024 * 1024)))


def sanitize_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        raise ValueError("telefone invalido")
    return digits


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MemoryEntry:
    id: str
    text: str
    label: str = ""
    created_at: str = field(default_factory=_now_iso)


@dataclass
class StoredFile:
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    label: str = ""
    caption: str = ""
    created_at: str = field(default_factory=_now_iso)


@dataclass
class PendingFile:
    """Arquivo aguardando decisao do usuario (registro pessoal vs galeria)."""

    id: str
    filename: str
    mime_type: str
    mediatype: str
    size_bytes: int
    is_image: bool
    caption: str = ""
    stage: str = "destination"  # destination | description | processing
    created_at: str = field(default_factory=_now_iso)


@dataclass
class InboundMedia:
    """Midia recebida no webhook Evolution."""

    mediatype: str  # image | document | video | audio
    filename: str
    mimetype: str
    caption: str
    message_record: dict[str, Any]


@dataclass
class InboundContent:
    """Mensagem recebida no webhook Evolution (texto e/ou midia)."""

    phone: str
    text: str
    media: InboundMedia | None = None
    record: dict[str, Any] | None = None


class UserMemoryStore:
    """Armazena memorias de texto e arquivos em /data/shakira_users/{phone}/."""

    def __init__(self, phone: str) -> None:
        self.phone = sanitize_phone(phone)
        self.root = USER_DATA_ROOT / self.phone
        self.files_dir = self.root / "files"
        self.pending_dir = self.root / "pending"
        self.memories_path = self.root / "memories.json"
        self.files_manifest_path = self.root / "files_manifest.json"
        self.pending_meta_path = self.root / "pending_file.json"

    def ensure_dirs(self) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)

    def _load_memories_raw(self) -> list[dict[str, Any]]:
        if not self.memories_path.is_file():
            return []
        try:
            data = json.loads(self.memories_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except (json.JSONDecodeError, OSError) as e:
            log.warning("memories.json corrompido phone=%s: %s", self.phone, e)
        return []

    def _save_memories_raw(self, rows: list[dict[str, Any]]) -> None:
        self.ensure_dirs()
        self.memories_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_memories(self) -> list[MemoryEntry]:
        out: list[MemoryEntry] = []
        for row in self._load_memories_raw():
            mid = str(row.get("id") or "").strip()
            text = str(row.get("text") or "").strip()
            if not mid or not text:
                continue
            out.append(
                MemoryEntry(
                    id=mid,
                    text=text,
                    label=str(row.get("label") or "").strip(),
                    created_at=str(row.get("created_at") or _now_iso()),
                )
            )
        return out

    def add_memory(self, text: str, *, label: str = "") -> MemoryEntry:
        t = text.strip()
        if not t:
            raise ValueError("texto da memoria vazio")
        if len(t) > MAX_MEMORY_CHARS:
            t = t[:MAX_MEMORY_CHARS]
        entry = MemoryEntry(
            id=uuid.uuid4().hex[:12],
            text=t,
            label=label.strip()[:120],
        )
        rows = self._load_memories_raw()
        rows.append(asdict(entry))
        if len(rows) > MAX_MEMORIES:
            rows = rows[-MAX_MEMORIES:]
        self._save_memories_raw(rows)
        log.info("Memoria salva phone=%s id=%s chars=%s", self.phone, entry.id, len(t))
        return entry

    def delete_memory(self, memory_id: str) -> bool:
        mid = memory_id.strip()
        rows = self._load_memories_raw()
        new_rows = [r for r in rows if str(r.get("id")) != mid]
        if len(new_rows) == len(rows):
            return False
        self._save_memories_raw(new_rows)
        return True

    def delete_file(self, file_id: str) -> bool:
        fid = file_id.strip()
        rows = self._load_files_manifest()
        target: dict[str, Any] | None = None
        new_rows: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("id")) == fid:
                target = row
            else:
                new_rows.append(row)
        if not target:
            return False
        self._delete_file_disk(target)
        self._save_files_manifest(new_rows)
        log.info("Arquivo apagado phone=%s id=%s", self.phone, fid)
        return True

    def _load_files_manifest(self) -> list[dict[str, Any]]:
        if not self.files_manifest_path.is_file():
            return []
        try:
            data = json.loads(self.files_manifest_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except (json.JSONDecodeError, OSError) as e:
            log.warning("files_manifest.json corrompido phone=%s: %s", self.phone, e)
        return []

    def _save_files_manifest(self, rows: list[dict[str, Any]]) -> None:
        self.ensure_dirs()
        self.files_manifest_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_files(self) -> list[StoredFile]:
        out: list[StoredFile] = []
        for row in self._load_files_manifest():
            fid = str(row.get("id") or "").strip()
            fname = str(row.get("filename") or "").strip()
            if not fid or not fname:
                continue
            out.append(
                StoredFile(
                    id=fid,
                    filename=fname,
                    mime_type=str(row.get("mime_type") or "application/octet-stream"),
                    size_bytes=int(row.get("size_bytes") or 0),
                    label=str(row.get("label") or "").strip(),
                    caption=str(row.get("caption") or "").strip(),
                    created_at=str(row.get("created_at") or _now_iso()),
                )
            )
        return out

    def _safe_filename(self, name: str) -> str:
        base = Path(name).name or "arquivo"
        base = re.sub(r"[^\w.\- ]", "_", base, flags=re.UNICODE).strip()
        return base[:180] or "arquivo"

    def save_file(
        self,
        data: bytes,
        *,
        filename: str,
        mime_type: str = "application/octet-stream",
        label: str = "",
        caption: str = "",
    ) -> StoredFile:
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"arquivo excede limite de {MAX_FILE_BYTES // (1024 * 1024)} MB")
        self.ensure_dirs()
        safe_name = self._safe_filename(filename)
        fid = uuid.uuid4().hex[:12]
        disk_name = f"{fid}_{safe_name}"
        path = self.files_dir / disk_name
        path.write_bytes(data)

        entry = StoredFile(
            id=fid,
            filename=safe_name,
            mime_type=mime_type or "application/octet-stream",
            size_bytes=len(data),
            label=label.strip()[:120],
            caption=caption.strip()[:500],
        )
        rows = self._load_files_manifest()
        rows.append(asdict(entry))
        if len(rows) > MAX_FILES:
            # Remove arquivos mais antigos do disco
            for old in rows[:-MAX_FILES]:
                self._delete_file_disk(old)
            rows = rows[-MAX_FILES:]
        self._save_files_manifest(rows)
        log.info(
            "Arquivo salvo phone=%s id=%s name=%s bytes=%s",
            self.phone,
            fid,
            safe_name,
            len(data),
        )
        return entry

    def _delete_file_disk(self, row: dict[str, Any]) -> None:
        fid = str(row.get("id") or "")
        fname = str(row.get("filename") or "")
        if not fid:
            return
        for path in self.files_dir.glob(f"{fid}_*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if fname:
            for path in self.files_dir.glob(f"*_{fname}"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def get_file_path(self, file_id: str) -> tuple[StoredFile, Path] | None:
        fid = file_id.strip()
        for meta in self.list_files():
            if meta.id != fid:
                continue
            for path in self.files_dir.glob(f"{fid}_*"):
                if path.is_file():
                    return meta, path
        return None

    def find_file(
        self,
        *,
        file_id: str = "",
        filename: str = "",
        label: str = "",
    ) -> tuple[StoredFile, Path] | None:
        if file_id.strip():
            return self.get_file_path(file_id.strip())

        files = self.list_files()
        if not files:
            return None

        fn = filename.strip().casefold()
        lb = label.strip().casefold()
        if fn:
            for meta in reversed(files):
                if meta.filename.casefold() == fn or fn in meta.filename.casefold():
                    hit = self.get_file_path(meta.id)
                    if hit:
                        return hit
        if lb:
            for meta in reversed(files):
                if lb in meta.label.casefold() or lb in meta.caption.casefold():
                    hit = self.get_file_path(meta.id)
                    if hit:
                        return hit
        return None

    def build_context_text(self) -> str:
        """Texto injetado no prompt Gemini (memorias + indice de arquivos)."""
        memories = self.list_memories()
        files = self.list_files()
        if not memories and not files:
            return ""

        parts: list[str] = [
            "MEMORIA PERSISTENTE DO USUARIO (informacoes que ele pediu para guardar ou lembrar):"
        ]
        if memories:
            parts.append("Notas e fatos guardados:")
            for i, m in enumerate(memories, start=1):
                label = f" [{m.label}]" if m.label else ""
                parts.append(f"  {i}. (id={m.id}){label} {m.text}")
        if files:
            parts.append("Arquivos guardados (use send_user_file para reenviar):")
            for i, f in enumerate(files, start=1):
                extra = []
                if f.label:
                    extra.append(f"rotulo={f.label}")
                if f.caption:
                    extra.append(f"legenda={f.caption[:80]}")
                meta = f" ({', '.join(extra)})" if extra else ""
                parts.append(
                    f"  {i}. id={f.id} nome={f.filename} tipo={f.mime_type} tamanho={f.size_bytes}{meta}"
                )
        return "\n".join(parts)

    def content_hash(self) -> str:
        payload = self.build_context_text().encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def cache_meta_path(self) -> Path:
        return self.root / "gemini_memory_cache.json"

    def save_pending_file(
        self,
        data: bytes,
        *,
        filename: str,
        mime_type: str,
        mediatype: str,
        caption: str = "",
    ) -> PendingFile:
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"arquivo excede limite de {MAX_FILE_BYTES // (1024 * 1024)} MB")
        self.clear_pending_file()
        self.ensure_dirs()
        safe_name = self._safe_filename(filename)
        pid = uuid.uuid4().hex[:12]
        is_image = mediatype == "image" or (mime_type or "").startswith("image/")
        path = self.pending_dir / f"{pid}_{safe_name}"
        path.write_bytes(data)
        pending = PendingFile(
            id=pid,
            filename=safe_name,
            mime_type=mime_type or "application/octet-stream",
            mediatype=mediatype,
            size_bytes=len(data),
            is_image=is_image,
            caption=caption.strip()[:500],
        )
        self.pending_meta_path.write_text(
            json.dumps(asdict(pending), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("Arquivo pendente phone=%s id=%s", self.phone, pid)
        return pending

    def get_pending_file(self) -> tuple[PendingFile, Path] | None:
        if not self.pending_meta_path.is_file():
            return None
        try:
            row = json.loads(self.pending_meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(row, dict):
            return None
        pid = str(row.get("id") or "").strip()
        if not pid:
            return None
        pending = PendingFile(
            id=pid,
            filename=str(row.get("filename") or "arquivo"),
            mime_type=str(row.get("mime_type") or "application/octet-stream"),
            mediatype=str(row.get("mediatype") or "document"),
            size_bytes=int(row.get("size_bytes") or 0),
            is_image=bool(row.get("is_image")),
            caption=str(row.get("caption") or ""),
            stage=str(row.get("stage") or "destination"),
            created_at=str(row.get("created_at") or _now_iso()),
        )
        for path in self.pending_dir.glob(f"{pid}_*"):
            if path.is_file():
                return pending, path
        return None

    def set_pending_stage(self, stage: str) -> bool:
        if not self.pending_meta_path.is_file():
            return False
        try:
            row = json.loads(self.pending_meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if not isinstance(row, dict):
            return False
        row["stage"] = stage.strip() or "destination"
        self.pending_meta_path.write_text(
            json.dumps(row, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True

    def clear_pending_file(self) -> None:
        if self.pending_meta_path.is_file():
            try:
                hit = self.get_pending_file()
                if hit:
                    _, path = hit
                    path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                self.pending_meta_path.unlink(missing_ok=True)
            except OSError:
                pass
        if self.pending_dir.is_dir():
            for path in self.pending_dir.glob("*"):
                try:
                    if path.is_file():
                        path.unlink(missing_ok=True)
                except OSError:
                    pass


def get_store(phone: str) -> UserMemoryStore:
    return UserMemoryStore(phone)
