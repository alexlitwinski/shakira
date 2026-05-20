"""Registo persistente de perfis/links Instagram por usuario."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.user_memory import USER_DATA_ROOT, sanitize_phone

log = logging.getLogger(__name__)

MAX_LINKS = int(os.environ.get("SHAKIRA_MAX_INSTAGRAM_LINKS_PER_USER", "100"))
MAX_NOTE_CHARS = int(os.environ.get("SHAKIRA_MAX_INSTAGRAM_NOTE_CHARS", "2000"))
MAX_AVATAR_BYTES = int(os.environ.get("SHAKIRA_MAX_INSTAGRAM_AVATAR_BYTES", str(2 * 1024 * 1024)))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class InstagramLinkEntry:
    id: str
    url: str
    handle: str
    user_note: str = ""
    profile_name: str = ""
    profile_bio: str = ""
    followers: int | None = None
    is_verified: bool | None = None
    avatar_filename: str = ""
    fetch_status: str = "pending"
    fetch_error: str = ""
    save_status: str = "draft"
    created_at: str = field(default_factory=_now_iso)
    saved_at: str = ""
    fetched_at: str = ""


_store_cache: dict[str, "InstagramLinksStore"] = {}


class InstagramLinksStore:
    def __init__(self, phone: str) -> None:
        self.phone = sanitize_phone(phone)
        self.root = USER_DATA_ROOT / self.phone
        self.avatars_dir = self.root / "instagram_avatars"
        self.links_path = self.root / "instagram_links.json"

    def ensure_dirs(self) -> None:
        self.avatars_dir.mkdir(parents=True, exist_ok=True)

    def _load_raw(self) -> list[dict[str, Any]]:
        if not self.links_path.is_file():
            return []
        try:
            data = json.loads(self.links_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except (json.JSONDecodeError, OSError) as e:
            log.warning("instagram_links.json corrompido phone=%s: %s", self.phone, e)
        return []

    def _save_raw(self, rows: list[dict[str, Any]]) -> None:
        self.ensure_dirs()
        self.links_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _row_to_entry(self, row: dict[str, Any]) -> InstagramLinkEntry | None:
        eid = str(row.get("id") or "").strip()
        url = str(row.get("url") or "").strip()
        handle = str(row.get("handle") or "").strip()
        if not eid or not url or not handle:
            return None
        followers = row.get("followers")
        foll: int | None = None
        if isinstance(followers, int):
            foll = followers
        elif isinstance(followers, str) and followers.strip().isdigit():
            foll = int(followers.strip())
        verified = row.get("is_verified")
        is_verified: bool | None = None
        if isinstance(verified, bool):
            is_verified = verified
        return InstagramLinkEntry(
            id=eid,
            url=url,
            handle=handle,
            user_note=str(row.get("user_note") or "").strip(),
            profile_name=str(row.get("profile_name") or "").strip(),
            profile_bio=str(row.get("profile_bio") or "").strip(),
            followers=foll,
            is_verified=is_verified,
            avatar_filename=str(row.get("avatar_filename") or "").strip(),
            fetch_status=str(row.get("fetch_status") or "pending"),
            fetch_error=str(row.get("fetch_error") or "").strip(),
            save_status=str(row.get("save_status") or "draft"),
            created_at=str(row.get("created_at") or _now_iso()),
            saved_at=str(row.get("saved_at") or "").strip(),
            fetched_at=str(row.get("fetched_at") or "").strip(),
        )

    def list_all(self) -> list[InstagramLinkEntry]:
        out: list[InstagramLinkEntry] = []
        for row in self._load_raw():
            ent = self._row_to_entry(row)
            if ent:
                out.append(ent)
        return out

    def list_saved(self) -> list[InstagramLinkEntry]:
        return [e for e in self.list_all() if e.save_status == "saved"]

    def get_by_id(self, entry_id: str) -> InstagramLinkEntry | None:
        eid = entry_id.strip()
        for row in self._load_raw():
            if str(row.get("id")) == eid:
                return self._row_to_entry(row)
        return None

    def find_by_handle(self, handle: str, *, saved_only: bool = False) -> InstagramLinkEntry | None:
        h = handle.strip().lstrip("@").lower()
        for ent in reversed(self.list_all()):
            if ent.handle.lower() != h:
                continue
            if saved_only and ent.save_status != "saved":
                continue
            return ent
        return None

    def create_draft(self, *, url: str, handle: str, user_note: str = "") -> InstagramLinkEntry:
        note = user_note.strip()[:MAX_NOTE_CHARS]
        h = handle.strip().lstrip("@").lower() or "unknown"
        entry = InstagramLinkEntry(
            id=uuid.uuid4().hex[:12],
            url=url.strip(),
            handle=h,
            user_note=note,
            save_status="draft",
            fetch_status="pending",
        )
        rows = self._load_raw()
        rows.append(asdict(entry))
        if len(rows) > MAX_LINKS:
            rows = rows[-MAX_LINKS:]
        self._save_raw(rows)
        log.info("Instagram draft phone=%s id=%s handle=%s", self.phone, entry.id, entry.handle)
        return entry

    def update_entry(self, entry: InstagramLinkEntry) -> None:
        rows = self._load_raw()
        updated = asdict(entry)
        found = False
        for i, row in enumerate(rows):
            if str(row.get("id")) == entry.id:
                rows[i] = updated
                found = True
                break
        if not found:
            rows.append(updated)
        self._save_raw(rows)

    def mark_saved(self, entry_id: str, *, user_note: str) -> InstagramLinkEntry | None:
        ent = self.get_by_id(entry_id)
        if not ent:
            return None
        ent.user_note = user_note.strip()[:MAX_NOTE_CHARS]
        ent.save_status = "saved"
        ent.saved_at = _now_iso()
        self.update_entry(ent)
        return ent

    def delete_entry(self, entry_id: str) -> bool:
        eid = entry_id.strip()
        rows = self._load_raw()
        target: dict[str, Any] | None = None
        new_rows: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("id")) == eid:
                target = row
            else:
                new_rows.append(row)
        if not target:
            return False
        fname = str(target.get("avatar_filename") or "").strip()
        if fname:
            path = self.avatars_dir / fname
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self._save_raw(new_rows)
        return True

    def avatar_path(self, entry: InstagramLinkEntry) -> Path | None:
        if not entry.avatar_filename:
            return None
        path = self.avatars_dir / entry.avatar_filename
        return path if path.is_file() else None

    def save_avatar_bytes(self, entry_id: str, data: bytes, *, ext: str = "jpg") -> str:
        if len(data) > MAX_AVATAR_BYTES:
            raise ValueError("avatar excede limite")
        self.ensure_dirs()
        safe_ext = ext.lstrip(".")[:4] or "jpg"
        filename = f"{entry_id}.{safe_ext}"
        (self.avatars_dir / filename).write_bytes(data)
        return filename

    def build_context_text(self) -> str:
        saved = self.list_saved()
        if not saved:
            return ""
        parts = ["PERFIS INSTAGRAM GUARDADOS (links que o usuario pediu para guardar):"]
        for i, e in enumerate(saved, start=1):
            note = f' nota="{e.user_note}"' if e.user_note else ""
            bio = ""
            if e.profile_bio:
                b = e.profile_bio.replace("\n", " ")
                bio = f' bio="{b[:120]}"' if len(b) > 120 else f' bio="{b}"'
            foll = f" seguidores={e.followers}" if e.followers is not None else ""
            parts.append(
                f"  {i}. (id={e.id}) @{e.handle}{note}{bio}{foll} url={e.url} fetch={e.fetch_status}"
            )
        return "\n".join(parts)

    def content_hash_suffix(self) -> str:
        return self.build_context_text()


def get_instagram_store(phone: str) -> InstagramLinksStore:
    key = sanitize_phone(phone)
    store = _store_cache.get(key)
    if store is None:
        store = InstagramLinksStore(key)
        _store_cache[key] = store
    return store
