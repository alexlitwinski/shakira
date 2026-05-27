"""Gerenciador e banco de dados SQLite para histórico de presença na rua."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from app.user_memory import USER_DATA_ROOT

log = logging.getLogger(__name__)

# Fuso horário local do utilizador (-03:00)
TZ_LOCAL = timezone(timedelta(hours=-3))


class StreetPresenceStore:
    """Banco de dados SQLite para persistência histórica das passagens na rua."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            self.db_path = USER_DATA_ROOT / "street_presence.db"
        else:
            self.db_path = db_path
        self.ensure_db()

    def ensure_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS street_presence_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT UNIQUE,
                    state TEXT
                )
            """)
            conn.commit()

    def register_event(self, timestamp: datetime, state: str = "on") -> bool:
        """
        Registra um evento de presença no banco de dados com debounce de 10 segundos.
        Retorna True se o evento foi registrado, False caso duplicado ou ignorado no debounce.
        """
        ts_utc = timestamp.astimezone(timezone.utc)
        ts_str = ts_utc.isoformat().replace("+00:00", "Z")

        # Verifica debounce: se o último evento registrado foi a menos de 10 segundos
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT timestamp FROM street_presence_events WHERE state = 'on' ORDER BY timestamp DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if row:
                last_ts_str = row[0]
                if last_ts_str.endswith("Z"):
                    last_ts_str = last_ts_str[:-1] + "+00:00"
                last_dt = datetime.fromisoformat(last_ts_str)
                diff = (ts_utc - last_dt).total_seconds()
                
                # Debounce: se for menor que 10 segundos, ignoramos
                if 0 <= diff < 10.0 and state == "on":
                    log.debug("StreetPresenceStore: Debounce ativo (diff=%.1fs). Ignorado.", diff)
                    return False

            try:
                conn.execute(
                    "INSERT INTO street_presence_events (timestamp, state) VALUES (?, ?)",
                    (ts_str, state),
                )
                conn.commit()
                log.info("StreetPresenceStore: Evento registrado: %s", ts_str)
                return True
            except sqlite3.IntegrityError:
                # UNIQUE constraint no timestamp
                return False

    def _count_between(self, start: datetime, end: datetime) -> int:
        start_str = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_str = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM street_presence_events WHERE state = 'on' AND timestamp >= ? AND timestamp <= ?",
                (start_str, end_str),
            )
            return cursor.fetchone()[0]

    def count_today(self, now: datetime | None = None) -> int:
        """Retorna a contagem de eventos no dia civil local corrente."""
        if now is None:
            now = datetime.now(timezone.utc)
        now_local = now.astimezone(TZ_LOCAL)
        start_local = datetime(now_local.year, now_local.month, now_local.day, 0, 0, 0, tzinfo=TZ_LOCAL)
        end_local = datetime(now_local.year, now_local.month, now_local.day, 23, 59, 59, tzinfo=TZ_LOCAL)
        return self._count_between(start_local, end_local)

    def count_yesterday(self, now: datetime | None = None) -> int:
        """Retorna a contagem de eventos no dia civil local anterior."""
        if now is None:
            now = datetime.now(timezone.utc)
        now_local = now.astimezone(TZ_LOCAL)
        yesterday_local = now_local - timedelta(days=1)
        start_local = datetime(yesterday_local.year, yesterday_local.month, yesterday_local.day, 0, 0, 0, tzinfo=TZ_LOCAL)
        end_local = datetime(yesterday_local.year, yesterday_local.month, yesterday_local.day, 23, 59, 59, tzinfo=TZ_LOCAL)
        return self._count_between(start_local, end_local)

    def count_last_hour(self, now: datetime | None = None) -> int:
        """Retorna a contagem de eventos nos últimos 60 minutos."""
        if now is None:
            now = datetime.now(timezone.utc)
        start = now - timedelta(hours=1)
        return self._count_between(start, now)

    def count_this_month(self, now: datetime | None = None) -> int:
        """Retorna a contagem do mês corrente civil local."""
        if now is None:
            now = datetime.now(timezone.utc)
        now_local = now.astimezone(TZ_LOCAL)
        start_local = datetime(now_local.year, now_local.month, 1, 0, 0, 0, tzinfo=TZ_LOCAL)
        if now_local.month == 12:
            end_local = datetime(now_local.year + 1, 1, 1, 0, 0, 0, tzinfo=TZ_LOCAL) - timedelta(seconds=1)
        else:
            end_local = datetime(now_local.year, now_local.month + 1, 1, 0, 0, 0, tzinfo=TZ_LOCAL) - timedelta(seconds=1)
        return self._count_between(start_local, end_local)

    def count_month(self, month: int, now: datetime | None = None) -> int:
        """Retorna a contagem de um mês específico (1 a 12) do ano corrente (ou anterior se ultrapassar o mês atual)."""
        if now is None:
            now = datetime.now(timezone.utc)
        now_local = now.astimezone(TZ_LOCAL)
        year = now_local.year
        if month > now_local.month:
            year -= 1

        start_local = datetime(year, month, 1, 0, 0, 0, tzinfo=TZ_LOCAL)
        if month == 12:
            end_local = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=TZ_LOCAL) - timedelta(seconds=1)
        else:
            end_local = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=TZ_LOCAL) - timedelta(seconds=1)
        return self._count_between(start_local, end_local)

    WEEKDAYS_PT = {
        0: "Segunda-feira",
        1: "Terça-feira",
        2: "Quarta-feira",
        3: "Quinta-feira",
        4: "Sexta-feira",
        5: "Sábado",
        6: "Domingo"
    }

    def busiest_weekday(self) -> tuple[str, int] | None:
        """Retorna o dia da semana mais movimentado acumulado (nome, total_eventos)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp FROM street_presence_events WHERE state = 'on'")
            rows = cursor.fetchall()

        if not rows:
            return None

        counts = {day: 0 for day in self.WEEKDAYS_PT.values()}
        for row in rows:
            ts_str = row[0]
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            dt = datetime.fromisoformat(ts_str).astimezone(TZ_LOCAL)
            weekday_name = self.WEEKDAYS_PT[dt.weekday()]
            counts[weekday_name] += 1

        busiest = max(counts.items(), key=lambda x: x[1])
        if busiest[1] == 0:
            return None
        return busiest

    def busiest_day_of_month(self, now: datetime | None = None) -> tuple[str, int] | None:
        """Calcula e retorna o dia civil local mais movimentado do mês corrente (data_formatada_pt, total)."""
        if now is None:
            now = datetime.now(timezone.utc)
        now_local = now.astimezone(TZ_LOCAL)
        start_local = datetime(now_local.year, now_local.month, 1, 0, 0, 0, tzinfo=TZ_LOCAL)
        if now_local.month == 12:
            end_local = datetime(now_local.year + 1, 1, 1, 0, 0, 0, tzinfo=TZ_LOCAL) - timedelta(seconds=1)
        else:
            end_local = datetime(now_local.year, now_local.month + 1, 1, 0, 0, 0, tzinfo=TZ_LOCAL) - timedelta(seconds=1)

        start_str = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_str = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT timestamp FROM street_presence_events WHERE state = 'on' AND timestamp >= ? AND timestamp <= ?",
                (start_str, end_str),
            )
            rows = cursor.fetchall()

        if not rows:
            return None

        day_counts: dict[str, int] = {}
        for row in rows:
            ts_str = row[0]
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            dt_local = datetime.fromisoformat(ts_str).astimezone(TZ_LOCAL)
            day_key = dt_local.strftime("%Y-%m-%d")
            day_counts[day_key] = day_counts.get(day_key, 0) + 1

        if not day_counts:
            return None

        busiest_day_key, count = max(day_counts.items(), key=lambda x: x[1])
        dt_busiest = datetime.strptime(busiest_day_key, "%Y-%m-%d")
        months_pt = {
            1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril", 5: "maio", 6: "junho",
            7: "julho", 8: "agosto", 9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro"
        }
        formatted_day = f"{dt_busiest.day} de {months_pt[dt_busiest.month]}"
        return formatted_day, count

    async def sync_history_from_ha(self, ha_client: Any) -> int:
        """
        Sincroniza retroativamente os últimos 10 dias de histórico do sensor no HA.
        Retorna o número de novos eventos de fato gravados localmente.
        """
        log.info("StreetPresenceStore: Iniciando sincronização retroativa com HA...")
        now = datetime.now(timezone.utc)
        start_time = now - timedelta(days=10)

        try:
            history_data = await ha_client.get_history(
                entity_id="binary_sensor.presenca_porta_vidro_presence",
                start_time=start_time,
                end_time=now
            )
            if not history_data or not isinstance(history_data, list):
                log.info("StreetPresenceStore: Nenhum histórico retornado pelo Home Assistant.")
                return 0

            events_list = history_data[0] if len(history_data) > 0 else []
            if not isinstance(events_list, list):
                return 0

            imported_count = 0
            log.info("StreetPresenceStore: Processando %d estados recebidos...", len(events_list))
            
            for state_entry in events_list:
                state = state_entry.get("state")
                if state == "on":
                    last_changed = state_entry.get("last_changed")
                    if last_changed:
                        if last_changed.endswith("Z"):
                            last_changed = last_changed[:-1] + "+00:00"
                        dt = datetime.fromisoformat(last_changed)
                        if self.register_event(dt, "on"):
                            imported_count += 1

            log.info("StreetPresenceStore: Sincronização concluída. %d novos eventos importados.", imported_count)
            return imported_count

        except Exception as e:
            log.exception("StreetPresenceStore: Erro ao sincronizar histórico com HA: %s", e)
            return 0
