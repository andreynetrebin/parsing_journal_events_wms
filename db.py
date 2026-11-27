from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import duckdb

EVENT_COLUMNS = (
    "source_file", "source_row", "event_ts", "direction", "event_name",
    "method", "device", "request_id", "processing_ms",
    "user_id", "user_name", "connection_id", "session_id",
    "terminal", "object_id", "comment_raw", "body_json", "metadata",
    "action",
)

BATCH_ROWS = 2000
BATCH_BYTES = 8 * 1024 * 1024


def get_db_paths():
    """
    Возвращает пути к двум базам данных:
      - raw  : сырые события (только импорт)
      - mart : витрина (только чтение для API + пересчет)
    """
    base_path = Path(os.getenv(
        "TSD_DB_PATH",
        str(Path(__file__).resolve().parent / "data" / "wms.duckdb")
    ))
    if not base_path.is_absolute():
        base_path = Path(__file__).resolve().parent / base_path
    return {
        "raw":  base_path.parent / "wms_raw.duckdb",
        "mart": base_path,
    }


# ============================================================
# DDL для сырой базы (импорт)
# ============================================================
RAW_DDL = [
    """
    CREATE TABLE IF NOT EXISTS tsd_events (
        source_file    VARCHAR NOT NULL,
        source_row     INTEGER NOT NULL,
        event_ts       TIMESTAMP,
        direction      VARCHAR NOT NULL,
        event_name     VARCHAR,
        method         VARCHAR,
        device         VARCHAR,
        request_id     BIGINT,
        processing_ms  INTEGER,
        user_id        VARCHAR,
        user_name      VARCHAR,
        connection_id  VARCHAR,
        session_id     VARCHAR,
        terminal       VARCHAR,
        object_id      VARCHAR,
        comment_raw    VARCHAR,
        body_json      JSON,
        metadata       VARCHAR,
        action         VARCHAR,        
        ingested_at    TIMESTAMP DEFAULT current_timestamp
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_request_direction
    ON tsd_events(request_id, direction)
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_files (
        file_name   VARCHAR PRIMARY KEY,
        sha256      VARCHAR NOT NULL,
        size_bytes  BIGINT,
        event_count BIGINT,
        status      VARCHAR,
        imported_at TIMESTAMP DEFAULT current_timestamp
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_ingested_at 
    ON tsd_events(ingested_at)
    """,
]


# ============================================================
# DDL для витрины
# ============================================================
MART_DDL = [
    """
    CREATE TABLE IF NOT EXISTS tsd_pairs_mart (
        request_id    BIGINT,
        method        TEXT,
        device        TEXT,
        terminal      TEXT,
        request_ts    TIMESTAMP,
        response_ts   TIMESTAMP,
        processing_ms INTEGER,
        elapsed_ms    INTEGER,
        pair_status   TEXT,
        user_name     TEXT,
        session_id    TEXT,
        connection_id TEXT,
        request_file  TEXT,
        response_file TEXT,
        request_body_json   JSON,       -- ДОБАВЛЕНО
        response_body_json  JSON,        -- ДОБАВЛЕНО        
        action              TEXT            -- <-- ДОБАВЛЕНО        
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mart_request_id ON tsd_pairs_mart(request_id);
    CREATE INDEX IF NOT EXISTS idx_mart_method     ON tsd_pairs_mart(method);
    CREATE INDEX IF NOT EXISTS idx_mart_device     ON tsd_pairs_mart(device);
    CREATE INDEX IF NOT EXISTS idx_mart_pair_status ON tsd_pairs_mart(pair_status);
    """,
    """
    CREATE TABLE IF NOT EXISTS tsd_errors (
        event_ts      TIMESTAMP,
        method        VARCHAR,
        device        VARCHAR,
        terminal      VARCHAR,
        user_name     VARCHAR,
        session_id    VARCHAR,
        connection_id VARCHAR,
        comment_raw   VARCHAR,
        source_file   VARCHAR
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_errors_event_ts ON tsd_errors(event_ts);
    CREATE INDEX IF NOT EXISTS idx_errors_method   ON tsd_errors(method);
    CREATE INDEX IF NOT EXISTS idx_errors_device   ON tsd_errors(device);
    """,
    # Таблица метаданных пересчёта (всегда одна строка с id=1)
    """
    CREATE TABLE IF NOT EXISTS rebuild_log (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        rebuild_ts TIMESTAMP DEFAULT current_timestamp,
        mode VARCHAR,
        pairs_count BIGINT,
        errors_count BIGINT,
        duration_sec REAL
    )
    """,
]


# ============================================================
# Views для витрины (все на локальных таблицах mart-базы)
# ============================================================
MART_VIEWS = [
    """
    CREATE OR REPLACE VIEW v_tsd_orphans AS
    SELECT * FROM tsd_pairs_mart WHERE pair_status <> 'ok'
    """,
    """
    CREATE OR REPLACE VIEW v_slow_methods_by_device AS
    SELECT
        device, method,
        count(*) FILTER (WHERE pair_status = 'ok') AS paired,
        count(processing_ms)                       AS timed,
        round(avg(processing_ms), 1)               AS avg_ms,
        max(processing_ms)                         AS max_ms,
        sum(processing_ms)                         AS total_ms
    FROM tsd_pairs_mart
    WHERE pair_status = 'ok'
    GROUP BY device, method
    """,
    """
    CREATE OR REPLACE VIEW v_method_hourly AS
    SELECT
        date_trunc('hour', COALESCE(request_ts, response_ts)) AS hour,
        method,
        count(*)                                      AS messages,
        count(*) FILTER (WHERE pair_status = 'ok')     AS paired,
        count(processing_ms)                          AS timed,
        round(avg(processing_ms), 1)                  AS avg_ms,
        quantile_cont(processing_ms, 0.95)            AS p95_ms,
        max(processing_ms)                            AS max_ms
    FROM tsd_pairs_mart
    WHERE COALESCE(request_ts, response_ts) IS NOT NULL
    GROUP BY 1, 2
    ORDER BY 1, 2
    """,
    """
    CREATE OR REPLACE VIEW v_method_minutely AS
    SELECT
        date_trunc('minute', COALESCE(request_ts, response_ts)) AS minute_ts,
        method, device,
        count(*)                                      AS messages,
        count(*) FILTER (WHERE pair_status = 'ok')     AS paired,
        count(processing_ms)                          AS timed,
        round(avg(processing_ms), 1)                  AS avg_ms,
        quantile_cont(processing_ms, 0.95)            AS p95_ms,
        max(processing_ms)                            AS max_ms
    FROM tsd_pairs_mart
    WHERE COALESCE(request_ts, response_ts) IS NOT NULL
    GROUP BY 1, 2, 3
    ORDER BY 1 DESC, 2, 3
    """,
    """
    CREATE OR REPLACE VIEW v_method_stats AS
    SELECT
        method,
        count(*) FILTER (WHERE pair_status = 'ok')          AS paired,
        count(*) FILTER (WHERE pair_status = 'no_response') AS no_response,
        count(*) FILTER (WHERE pair_status = 'no_request')  AS no_request,
        count(processing_ms)                               AS timed,
        round(avg(processing_ms), 1)                       AS avg_ms,
        median(processing_ms)                              AS p50_ms,
        quantile_cont(processing_ms, 0.95)                 AS p95_ms,
        quantile_cont(processing_ms, 0.99)                 AS p99_ms,
        max(processing_ms)                                 AS max_ms,
        sum(processing_ms)                                 AS total_ms
    FROM tsd_pairs_mart
    GROUP BY method
    ORDER BY total_ms DESC NULLS LAST
    """,
    """
    CREATE OR REPLACE VIEW v_tsd_errors_view AS
    SELECT * FROM tsd_errors ORDER BY event_ts DESC
    """,
]


def _fingerprint(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest(), path.stat().st_size


# ============================================================
# JournalDB — сырая база (только импорт)
# ============================================================
class JournalDB:
    """Работает с wms_raw.duckdb. Только запись при импорте."""

    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self.con = duckdb.connect(str(self.path), read_only=read_only)
        self.con.execute("SET preserve_insertion_order=false;")

        # Keep each API worker's DuckDB footprint bounded. Spill files are
        # written to a per-connection directory and removed by DuckDB on close.
        threads = max(1, int(os.getenv("TSD_DUCKDB_THREADS", "1")))
        memory_limit = os.getenv("TSD_DUCKDB_MEMORY_LIMIT", "1GB").replace("'", "''")
        max_temp_size = os.getenv("TSD_DUCKDB_MAX_TEMP_SIZE", "10GB").replace("'", "''")
        self._temp_dir = Path(tempfile.mkdtemp(prefix="tsd-duckdb-"))
        escaped_temp_dir = str(self._temp_dir).replace("'", "''")
        self.con.execute(f"SET threads TO {threads}")
        self.con.execute(f"SET memory_limit = '{memory_limit}'")
        self.con.execute(f"SET max_temp_directory_size = '{max_temp_size}'")
        self.con.execute(f"SET temp_directory = '{escaped_temp_dir}'")

        if not read_only:
            try:
                self.con.execute("SELECT 1 FROM tsd_events LIMIT 1")
                schema_exists = True
            except Exception:
                schema_exists = False

            if not schema_exists:
                for stmt in RAW_DDL:
                    self.con.execute(stmt)



    def cleanup_old_events(self, days: int = 14) -> int:
        """
        Удаляет события и записи импорта старше указанного количества дней.
        Возвращает количество удаленных событий.
        """
        # Считаем количество до удаления для лога
        count_before = self.con.execute("SELECT count(*) FROM tsd_events").fetchone()[0]

        # Удаляем старые события
        self.con.execute(f"""
            DELETE FROM tsd_events
            WHERE event_ts < CURRENT_TIMESTAMP - INTERVAL '{days} days'
        """)

        # (Опционально) Чистим таблицу метаданных импорта от старых записей
        self.con.execute(f"""
            DELETE FROM ingest_files
            WHERE imported_at < CURRENT_TIMESTAMP - INTERVAL '{days} days'
        """)

        count_after = self.con.execute("SELECT count(*) FROM tsd_events").fetchone()[0]
        return count_before - count_after


    def __enter__(self) -> "JournalDB":
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.con.close()
        finally:
            if hasattr(self, "_temp_dir"):
                shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _flush(self, rows: list[tuple]) -> int:
        if not rows:
            return 0
        cols = ", ".join(EVENT_COLUMNS)
        placeholders = ", ".join(["?"] * len(EVENT_COLUMNS))
        self.con.executemany(
            f"INSERT INTO tsd_events ({cols}) VALUES ({placeholders})", rows
        )
        return len(rows)

    def import_file(self, path: Path, force: bool = False) -> dict:
        if self.read_only:
            raise RuntimeError("import_file() недоступен в read_only режиме")

        from parser import iter_tsd_events

        path = Path(path)
        name = path.name
        sha256, size = _fingerprint(path)
        result = {"file": name, "status": "skipped", "events": 0}

        if not force:
            old = self.con.execute(
                "SELECT sha256, size_bytes, status FROM ingest_files WHERE file_name = ?",
                [name],
            ).fetchone()
            if old and old[0] == sha256 and old[1] == size and old[2] == "ok":
                return result

            same = self.con.execute(
                "SELECT file_name FROM ingest_files WHERE sha256 = ? AND status = 'ok'",
                [sha256],
            ).fetchone()
            if same:
                result["status"] = "duplicate"
                result["other"] = same[0]
                return result

        self.con.execute("BEGIN TRANSACTION")
        try:
            self.con.execute("DELETE FROM tsd_events WHERE source_file = ?", [name])
            self.con.execute("DELETE FROM ingest_files WHERE file_name = ?", [name])

            batch: list[tuple] = []
            batch_bytes = 0
            total = 0

            for record in iter_tsd_events(path):
                row = tuple(record[c] for c in EVENT_COLUMNS)
                batch.append(row)
                batch_bytes += sum(len(str(v)) for v in row if v)

                if len(batch) >= BATCH_ROWS or batch_bytes >= BATCH_BYTES:
                    total += self._flush(batch)
                    batch, batch_bytes = [], 0

            total += self._flush(batch)

            self.con.execute(
                """INSERT INTO ingest_files
                   (file_name, sha256, size_bytes, event_count, status)
                   VALUES (?, ?, ?, ?, 'ok')""",
                [name, sha256, size, total],
            )
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

        result.update(status="imported", events=total)
        return result


# ============================================================
# MartDB — витрина (чтение для API + пересчет из raw)
# ============================================================
class MartDB:
    """Работает с wms.duckdb. Читает API, пересчитывается из raw через ATTACH."""

    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self.con = duckdb.connect(str(self.path), read_only=read_only)
        self.con.execute("SET preserve_insertion_order=false;")
        self.con.execute("SET threads TO 4;")

        if not read_only:
            # Всегда выполняем DDL — IF NOT EXISTS гарантирует безопасность
            for stmt in MART_DDL:
                self.con.execute(stmt)
            for stmt in MART_VIEWS:
                self.con.execute(stmt)

    def __enter__(self) -> "MartDB":
        return self

    def __exit__(self, *exc) -> None:
        self.con.close()


    def rebuild_from_raw(self, raw_db_path: str | Path) -> dict:
        """
        Полный пересчёт витрины с КОРРЕКТНОЙ попарной привязкой запрос-ответ.
        Использует ROW_NUMBER() для связи N-го запроса с N-м ответом внутри одного request_id и device.
        """
        raw_path = str(Path(raw_db_path).resolve())
        self.con.execute(f"ATTACH '{raw_path}' AS raw_db (READ_ONLY)")
        try:
            # 1. Пересчет витрины пар
            self.con.execute("DELETE FROM tsd_pairs_mart")
            self.con.execute("""
                INSERT INTO tsd_pairs_mart
                WITH numbered AS (
                    SELECT 
                        request_id, method, device, terminal, user_name, session_id, connection_id,
                        direction, event_ts, processing_ms, body_json, source_file, action,
                        ROW_NUMBER() OVER (
                            PARTITION BY request_id, device, direction 
                            ORDER BY event_ts
                        ) AS rn
                    FROM raw_db.tsd_events
                    WHERE request_id IS NOT NULL
                ),
                requests AS (
                    SELECT * FROM numbered WHERE direction = 'request'
                ),
                responses AS (
                    SELECT * FROM numbered WHERE direction = 'response'
                )
                SELECT
                    r.request_id, r.method, r.device, r.terminal,
                    r.event_ts AS request_ts,
                    resp.event_ts AS response_ts,
                    resp.processing_ms,
                    DATEDIFF('ms', r.event_ts, resp.event_ts) AS elapsed_ms,
                    CASE WHEN resp.event_ts IS NOT NULL THEN 'ok'
                         ELSE 'no_response' END AS pair_status,
                    r.user_name, r.session_id, r.connection_id,
                    r.source_file AS request_file,
                    resp.source_file AS response_file,
                    r.body_json AS request_body_json,
                    resp.body_json AS response_body_json,
                    r.action
                FROM requests r
                LEFT JOIN responses resp
                    ON r.request_id = resp.request_id
                   AND r.device = resp.device
                   AND r.rn = resp.rn
            """)
            pairs_count = self.con.execute("SELECT count(*) FROM tsd_pairs_mart").fetchone()[0]

            # 2. Синхронизация таблицы ошибок (явно указываем 9 колонок)
            self.con.execute("DELETE FROM tsd_errors")
            self.con.execute("""
                INSERT INTO tsd_errors 
                    (event_ts, method, device, terminal, user_name, session_id, connection_id, comment_raw, source_file)
                SELECT 
                    event_ts, method, device, terminal, user_name, session_id, connection_id, comment_raw, source_file
                FROM raw_db.tsd_events
                WHERE direction = 'error'
            """)
            errors_count = self.con.execute("SELECT count(*) FROM tsd_errors").fetchone()[0]

            # 3. Логируем пересчёт
            self.con.execute("""
                INSERT OR REPLACE INTO rebuild_log 
                (id, rebuild_ts, mode, pairs_count, errors_count, duration_sec)
                VALUES (1, current_timestamp, 'full', ?, ?, 0)
            """, [pairs_count, errors_count])

            return {"pairs": pairs_count, "errors": errors_count}
        finally:
            self.con.execute("DETACH raw_db")


    def get_last_rebuild_info(self) -> tuple[str | None, str | None]:
        """
        Возвращает (rebuild_ts, mode) последнего пересчёта.
        """
        try:
            row = self.con.execute("""
                SELECT rebuild_ts, mode FROM rebuild_log WHERE id = 1
            """).fetchone()
            if row:
                return str(row[0]), row[1]
            return None, None
        except Exception:
            return None, None

    def incremental_rebuild_from_raw(self, raw_db_path: str | Path, duration_sec: float = 0) -> dict:
        """
        Инкрементальный пересчёт с КОРРЕКТНОЙ попарной привязкой.
        """
        raw_path = str(Path(raw_db_path).resolve())

        last_rebuild_ts, _ = self.get_last_rebuild_info()
        if not last_rebuild_ts:
            raise RuntimeError("Нет записи о последнем пересчёте в rebuild_log. Выполните полный пересчёт.")

        self.con.execute(f"ATTACH '{raw_path}' AS raw_db (READ_ONLY)")
        try:
            # 1. Находим request_id из новых файлов
            self.con.execute("DROP TABLE IF EXISTS new_request_ids")
            self.con.execute("""
                CREATE TEMP TABLE new_request_ids AS
                SELECT DISTINCT request_id 
                FROM raw_db.tsd_events 
                WHERE source_file IN (
                    SELECT file_name FROM raw_db.ingest_files 
                    WHERE imported_at > CAST(? AS TIMESTAMP)
                )
                AND request_id IS NOT NULL
            """, [last_rebuild_ts])

            new_count = self.con.execute("SELECT count(*) FROM new_request_ids").fetchone()[0]
            if new_count == 0:
                self.con.execute("DROP TABLE IF EXISTS new_request_ids")
                return {"pairs": 0, "errors": 0, "mode": "incremental", "skipped": True, "new_pairs": 0}

            # 2. Удаляем старые пары для этих request_id
            self.con.execute("""
                DELETE FROM tsd_pairs_mart 
                WHERE request_id IN (SELECT request_id FROM new_request_ids)
            """)

            # 3. Вставляем новые пары с корректной привязкой через ROW_NUMBER()
            self.con.execute("""
                INSERT INTO tsd_pairs_mart
                WITH numbered AS (
                    SELECT 
                        request_id, method, device, terminal, user_name, session_id, connection_id,
                        direction, event_ts, processing_ms, body_json, source_file, action,
                        ROW_NUMBER() OVER (
                            PARTITION BY request_id, device, direction 
                            ORDER BY event_ts
                        ) AS rn
                    FROM raw_db.tsd_events
                    WHERE request_id IN (SELECT request_id FROM new_request_ids)
                ),
                requests AS (
                    SELECT * FROM numbered WHERE direction = 'request'
                ),
                responses AS (
                    SELECT * FROM numbered WHERE direction = 'response'
                )
                SELECT
                    r.request_id, r.method, r.device, r.terminal,
                    r.event_ts AS request_ts,
                    resp.event_ts AS response_ts,
                    resp.processing_ms,
                    DATEDIFF('ms', r.event_ts, resp.event_ts) AS elapsed_ms,
                    CASE WHEN resp.event_ts IS NOT NULL THEN 'ok'
                         ELSE 'no_response' END AS pair_status,
                    r.user_name, r.session_id, r.connection_id,
                    r.source_file AS request_file,
                    resp.source_file AS response_file,
                    r.body_json AS request_body_json,
                    resp.body_json AS response_body_json,
                    r.action
                FROM requests r
                LEFT JOIN responses resp
                    ON r.request_id = resp.request_id
                   AND r.device = resp.device
                   AND r.rn = resp.rn
            """)

            pairs_count = self.con.execute("SELECT count(*) FROM tsd_pairs_mart").fetchone()[0]

            # 4. Инкрементальное обновление ошибок
            self.con.execute("DROP TABLE IF EXISTS new_error_files")
            self.con.execute("""
                CREATE TEMP TABLE new_error_files AS
                SELECT DISTINCT source_file
                FROM raw_db.tsd_events
                WHERE direction = 'error'
                  AND ingested_at > CAST(? AS TIMESTAMP)
            """, [last_rebuild_ts])

            self.con.execute("""
                DELETE FROM tsd_errors 
                WHERE source_file IN (SELECT source_file FROM new_error_files)
            """)

            self.con.execute("""
                INSERT INTO tsd_errors 
                    (event_ts, method, device, terminal, user_name, session_id, connection_id, comment_raw, source_file)
                SELECT 
                    event_ts, method, device, terminal, user_name, session_id, connection_id, comment_raw, source_file
                FROM raw_db.tsd_events
                WHERE direction = 'error'
                  AND source_file IN (SELECT source_file FROM new_error_files)
            """)

            errors_count = self.con.execute("SELECT count(*) FROM tsd_errors").fetchone()[0]

            # 5. Логируем пересчёт
            self.con.execute("""
                INSERT OR REPLACE INTO rebuild_log 
                (id, rebuild_ts, mode, pairs_count, errors_count, duration_sec)
                VALUES (1, current_timestamp, 'incremental', ?, ?, ?)
            """, [pairs_count, errors_count, duration_sec])

            self.con.execute("DROP TABLE IF EXISTS new_request_ids")
            self.con.execute("DROP TABLE IF EXISTS new_error_files")

            return {"pairs": pairs_count, "errors": errors_count, "mode": "incremental", "new_pairs": new_count}
        finally:
            self.con.execute("DETACH raw_db")