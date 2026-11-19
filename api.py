"""FastAPI: дашборд для анализа ТСД (ТЗ)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
import os
import json as json_lib
from datetime import datetime
from db import MartDB, get_db_paths

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "data" / "wms.duckdb"

_DB_PATH: Path = DEFAULT_DB


def normalize_date(date_str: str | None, is_end: bool = False) -> str | None:
    """
    Если передана только дата (YYYY-MM-DD), автоматически добавляет время.
    Для start_date добавляет 00:00:00, для end_date добавляет 23:59:59.
    Если время уже указано, оставляет как есть.
    """
    if not date_str:
        return None
    date_str = str(date_str).strip()
    if len(date_str) == 10:  # Формат "YYYY-MM-DD"
        return f"{date_str} 23:59:59" if is_end else f"{date_str} 00:00:00"
    return date_str


def _open() -> MartDB:
    """Открывает витрину в режиме read_only. Сырая база не затрагивается."""
    db_paths = get_db_paths()
    return MartDB(db_paths["mart"], read_only=True)


def _rows(cursor) -> list[dict[str, Any]]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _build_action_filter(action: list[str] | None, where: list, params: list) -> None:
    """Добавляет фильтр по action в where и params."""
    if action:
        placeholders = ", ".join(["?"] * len(action))
        where.append(f"action IN ({placeholders})")
        params.extend(action)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not _DB_PATH.exists():
        raise RuntimeError(f"БД не найдена: {_DB_PATH}. Сначала запустите `python main.py ingest`.")
    yield


app = FastAPI(title="WMS TSD Dashboard", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    with _open() as db:
        # 1. Общее количество обработанных пар в витрине
        total = db.con.execute("SELECT count(*) FROM tsd_pairs_mart").fetchone()[0]

        # 2. Максимальная дата события (берем максимум из request_ts)
        max_event_row = db.con.execute("SELECT max(request_ts) FROM tsd_pairs_mart").fetchone()
        max_event_ts = max_event_row[0] if max_event_row and max_event_row[0] else None

        # 3. Время последней активности (max response_ts) как прокси для last_import_at.
        last_activity_row = db.con.execute("SELECT max(response_ts) FROM tsd_pairs_mart").fetchone()
        last_import_at = last_activity_row[0] if last_activity_row and last_activity_row[0] else None

    return {
        "status": "ok",
        "db": str(_DB_PATH),
        "events_total": total,
        "max_event_ts": str(max_event_ts) if max_event_ts else None,
        "last_import_at": str(last_import_at) if last_import_at else None,
    }


# ===== Сводная статистика для дашборда =====
@app.get("/api/dashboard/summary")
def dashboard_summary(
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    action: list[str] | None = Query(default=None),
    start_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
):
    where = []
    params: list[Any] = []

    if start_date:
        where.append("COALESCE(response_ts, request_ts) >= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(start_date, is_end=False))
    if end_date:
        where.append("COALESCE(response_ts, request_ts) <= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(end_date, is_end=True))

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    _build_action_filter(action, where, params)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with _open() as db:
        row = db.con.execute(f"""
            SELECT
                count(*) AS total_pairs,
                count(*) FILTER (WHERE pair_status = 'ok') AS ok_pairs,
                count(*) FILTER (WHERE pair_status = 'no_response') AS no_response,
                count(*) FILTER (WHERE pair_status = 'no_request') AS no_request,
                round(avg(processing_ms), 1) AS avg_ms,
                median(processing_ms) AS p50_ms,
                quantile_cont(processing_ms, 0.95) AS p95_ms,
                max(processing_ms) AS max_ms
            FROM tsd_pairs_mart
            {where_sql}
        """, params).fetchone()
    return {
        "total_pairs": row[0], "ok_pairs": row[1], "no_response": row[2], "no_request": row[3],
        "avg_ms": row[4], "p50_ms": row[5], "p95_ms": row[6], "max_ms": row[7]
    }


# ===== Топ самых медленных методов в разрезе ТСД =====
@app.get("/api/dashboard/slow-methods")
def slow_methods_by_device(
    limit: int = Query(100, ge=1, le=500),
    start_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    action: list[str] | None = Query(default=None),
):
    where = ["pair_status = 'ok'"]
    params: list[Any] = []

    if start_date:
        where.append("COALESCE(response_ts, request_ts) >= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(start_date, is_end=False))
    if end_date:
        where.append("COALESCE(response_ts, request_ts) <= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(end_date, is_end=True))

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    _build_action_filter(action, where, params)

    where_sql = " AND ".join(where)

    with _open() as db:
        return _rows(db.con.execute(f"""
            SELECT device, method,
                   count(*) FILTER (WHERE pair_status = 'ok') AS paired,
                   count(processing_ms) AS timed,
                   round(avg(processing_ms), 1) AS avg_ms,
                   max(processing_ms) AS max_ms,
                   sum(processing_ms) AS total_ms
            FROM tsd_pairs_mart
            WHERE {where_sql}
            GROUP BY device, method
            ORDER BY total_ms DESC NULLS LAST
            LIMIT {int(limit)}
        """, params))


# ===== График всплесков времени обработки =====
@app.get("/api/dashboard/hourly")
def hourly_stats(
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    action: list[str] | None = Query(default=None),
    hours: int = Query(72, ge=1, le=720),
):
    where = [f"hour >= date_trunc('hour', current_timestamp) - INTERVAL '{hours}' HOUR"]
    params: list[Any] = []

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    _build_action_filter(action, where, params)

    where_sql = " AND ".join(where)
    with _open() as db:
        return _rows(db.con.execute(f"""
            SELECT * FROM v_method_hourly WHERE {where_sql} ORDER BY hour DESC, method
        """, params))


@app.get("/api/dashboard/minutely")
def minutely_stats(
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    action: list[str] | None = Query(default=None),
    start_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    minutes: int = Query(1440, ge=1, le=10080, description="Минуты"),
):
    where = []
    params: list[Any] = []

    if start_date and end_date:
        where.append("minute_ts >= CAST(? AS TIMESTAMP)")
        params.append(start_date)
        where.append("minute_ts <= CAST(? AS TIMESTAMP)")
        params.append(end_date)
    else:
        where.append(f"minute_ts >= date_trunc('minute', current_timestamp) - INTERVAL '{minutes}' MINUTE")

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    _build_action_filter(action, where, params)

    where_sql = " AND ".join(where)

    with _open() as db:
        return _rows(db.con.execute(f"""
            SELECT * FROM v_method_minutely
            WHERE {where_sql}
            ORDER BY minute_ts ASC, method
        """, params))


@app.get("/api/dashboard/slow-requests")
def slow_requests(
    threshold_ms: int = Query(0, ge=0),
    request_id: int | None = Query(None),
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    action: list[str] | None = Query(default=None),
    start_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    limit: int = Query(500, ge=1, le=5000),
):
    where = ["processing_ms >= ?"]
    params: list[Any] = [threshold_ms]

    if request_id:
        where.append("request_id = ?")
        params.append(request_id)

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    _build_action_filter(action, where, params)

    if start_date:
        where.append("COALESCE(response_ts, request_ts) >= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(start_date, is_end=False))
    if end_date:
        where.append("COALESCE(response_ts, request_ts) <= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(end_date, is_end=True))

    where_sql = " AND ".join(where)
    with _open() as db:
        return _rows(db.con.execute(f"""
            SELECT request_id, method, device, terminal, processing_ms,
                   request_ts, response_ts, user_name, session_id, pair_status, action
            FROM tsd_pairs_mart
            WHERE {where_sql} AND pair_status = 'ok'
            ORDER BY processing_ms DESC
            LIMIT {int(limit)}
        """, params))


# ===== Отдельный реестр запросов, оставшихся без ответа =====
@app.get("/api/dashboard/orphans")
def orphans(
    request_id: int | None = Query(None),
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    action: list[str] | None = Query(default=None),
    start_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    limit: int = Query(500, ge=1, le=5000),
):
    where = ["pair_status <> 'ok'"]
    params: list[Any] = []

    if request_id:
        where.append("request_id = ?")
        params.append(request_id)

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    _build_action_filter(action, where, params)

    if start_date:
        where.append("COALESCE(response_ts, request_ts) >= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(start_date, is_end=False))
    if end_date:
        where.append("COALESCE(response_ts, request_ts) <= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(end_date, is_end=True))

    where_sql = " AND ".join(where)
    with _open() as db:
        return _rows(db.con.execute(f"""
            SELECT request_id, method, device, terminal, pair_status,
                   COALESCE(response_ts, request_ts) AS event_ts,
                   user_name, session_id, request_file, response_file, action
            FROM tsd_pairs_mart
            WHERE {where_sql}
            ORDER BY COALESCE(response_ts, request_ts) DESC
            LIMIT {int(limit)}
        """, params))


# ===== Детали события =====
@app.get("/api/events/{request_id}")
def event_details(request_id: int):
    with _open() as db:
        row = db.con.execute("""
            SELECT
                request_id, method, device, terminal, user_name, session_id, connection_id,
                request_ts, response_ts, processing_ms,
                request_body_json, response_body_json, request_file, response_file,
                action
            FROM tsd_pairs_mart
            WHERE request_id = ?
        """, [request_id]).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail=f"Событие {request_id} не найдено")

        # Индексы: 0=request_id, 1=method, 2=device, 3=terminal, 4=user_name,
        # 5=session_id, 6=connection_id, 7=request_ts, 8=response_ts, 9=processing_ms,
        # 10=request_body_json, 11=response_body_json, 12=request_file, 13=response_file,
        # 14=action
        action_value = row[14] if len(row) > 14 else None

        # Формируем структуру, ожидаемую фронтендом, только из данных витрины
        req_event = None
        if row[7]:  # request_ts
            req_event = {
                "direction": "request", "event_ts": str(row[7]), "method": row[1],
                "device": row[2], "terminal": row[3], "user_name": row[4],
                "session_id": row[5], "connection_id": row[6],
                "body_json": row[10], "source_file": row[12],
                "action": action_value
            }

        resp_event = None
        if row[8]:  # response_ts
            resp_event = {
                "direction": "response", "event_ts": str(row[8]), "method": row[1],
                "device": row[2], "terminal": row[3], "user_name": row[4],
                "session_id": row[5], "connection_id": row[6],
                "body_json": row[11], "processing_ms": row[9], "source_file": row[13],
                "action": action_value
            }

        return {
            "request_id": row[0],
            "action": action_value,
            "request": req_event,
            "response": resp_event,
            "events_count": (1 if req_event else 0) + (1 if resp_event else 0)
        }


@app.get("/api/pairs")
def pairs(
    request_id: int | None = Query(None),
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    action: list[str] | None = Query(default=None),
    pair_status: str | None = Query(None),
    user_name: str | None = Query(None),
    session_id: str | None = Query(None),
    min_ms: int | None = Query(None),
    max_ms: int | None = Query(None),
    start_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    where = []
    params: list[Any] = []

    if request_id:
        where.append("request_id = ?"); params.append(request_id)

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    _build_action_filter(action, where, params)

    if pair_status:
        where.append("pair_status = ?"); params.append(pair_status)
    if user_name:
        where.append("user_name = ?"); params.append(user_name)
    if session_id:
        where.append("session_id = ?"); params.append(session_id)
    if min_ms is not None:
        where.append("processing_ms >= ?"); params.append(min_ms)
    if max_ms is not None:
        where.append("processing_ms <= ?"); params.append(max_ms)

    if start_date:
        where.append("COALESCE(response_ts, request_ts) >= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(start_date, is_end=False))
    if end_date:
        where.append("COALESCE(response_ts, request_ts) <= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(end_date, is_end=True))

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT request_id, method, device, terminal, action,
               request_ts, response_ts, processing_ms, elapsed_ms,
               pair_status, user_name, session_id, connection_id
        FROM tsd_pairs_mart
        {where_sql}
        ORDER BY COALESCE(response_ts, request_ts) DESC NULLS LAST
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    with _open() as db:
        return _rows(db.con.execute(sql, params))


@app.get("/api/pairs/server")
def pairs_server(
    start: int = Query(0, ge=0),
    length: int = Query(25, ge=1, le=1000),
    search: str = Query(''),
    order_column: int = Query(3),
    order_dir: str = Query('desc'),
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    action: list[str] | None = Query(default=None),
    pair_status: str | None = Query(None),
    user_name: str | None = Query(None),
    session_id: str | None = Query(None),
    min_ms: int | None = Query(None),
    max_ms: int | None = Query(None),
    request_id: int | None = Query(None),
    start_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    body_search: str | None = Query(None, description="Поиск по телу запроса"),
):
    where = []
    params: list[Any] = []

    if request_id:
        where.append("request_id = ?"); params.append(request_id)
    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})"); params.extend(method)
    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})"); params.extend(device)
    if action:
        placeholders = ", ".join(["?"] * len(action))
        where.append(f"action IN ({placeholders})"); params.extend(action)
    if pair_status:
        where.append("pair_status = ?"); params.append(pair_status)
    if user_name:
        where.append("user_name = ?"); params.append(user_name)
    if session_id:
        where.append("session_id = ?"); params.append(session_id)
    if min_ms is not None:
        where.append("processing_ms >= ?"); params.append(min_ms)
    if max_ms is not None:
        where.append("processing_ms <= ?"); params.append(max_ms)
    if start_date:
        where.append("COALESCE(response_ts, request_ts) >= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(start_date, is_end=False))
    if end_date:
        where.append("COALESCE(response_ts, request_ts) <= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(end_date, is_end=True))

    # ПОИСК ПО ТЕЛУ ЗАПРОСА
    if body_search:
        where.append("CAST(request_body_json AS VARCHAR) LIKE ?")
        params.append(f"%{body_search}%")

    if search:
        search_clause = "(CAST(request_id AS VARCHAR) LIKE ? OR method LIKE ? OR device LIKE ? OR terminal LIKE ? OR user_name LIKE ? OR action LIKE ?)"
        where.append(search_clause)
        for _ in range(6):
            params.append(f"%{search}%")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    column_map = {
        0: 'request_id', 1: 'method', 2: 'device',
        3: 'COALESCE(response_ts, request_ts)', 4: 'response_ts',
        5: 'processing_ms', 6: 'user_name', 7: 'session_id', 8: 'pair_status',
        9: 'action',
    }
    order_col = column_map.get(order_column, 'COALESCE(response_ts, request_ts)')
    order_direction = 'ASC' if order_dir == 'asc' else 'DESC'

    with _open() as db:
        total = db.con.execute(f"SELECT count(*) FROM tsd_pairs_mart {where_sql}", params).fetchone()[0]
        sql = f"""
            SELECT request_id, method, device, terminal, action,
                   request_ts, response_ts, processing_ms, elapsed_ms,
                   pair_status, user_name, session_id, connection_id
            FROM tsd_pairs_mart
            {where_sql}
            ORDER BY {order_col} {order_direction}
            LIMIT ? OFFSET ?
        """
        params.extend([length, start])
        rows = _rows(db.con.execute(sql, params))

    return {"draw": 1, "recordsTotal": total, "recordsFiltered": total, "data": rows}



@app.get("/api/dashboard/errors/server")
def errors_server(
    start: int = Query(0, ge=0),
    length: int = Query(25, ge=1, le=1000),
    search: str = Query('', description="Глобальный поиск"),
    order_column: int = Query(0, description="Индекс колонки сортировки"),
    order_dir: str = Query('desc', description="Направление сортировки"),
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    start_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
):
    where = []
    params: list[Any] = []

    if start_date:
        where.append("event_ts >= CAST(? AS TIMESTAMP)")
        params.append(start_date)
    if end_date:
        where.append("event_ts <= CAST(? AS TIMESTAMP)")
        params.append(end_date)

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    if search:
        search_clause = "(method LIKE ? OR device LIKE ? OR terminal LIKE ? OR user_name LIKE ? OR comment_raw LIKE ?)"
        where.append(search_clause)
        for _ in range(5):
            params.append(f"%{search}%")

    where_sql = " AND ".join(where) if where else "1=1"

    column_map = {
        0: 'event_ts', 1: 'method', 2: 'device',
        3: 'terminal', 4: 'user_name', 5: 'comment_raw',
    }
    order_col = column_map.get(order_column, 'event_ts')
    order_direction = 'ASC' if order_dir == 'asc' else 'DESC'

    with _open() as db:
        total = db.con.execute(f"SELECT count(*) FROM tsd_errors WHERE {where_sql}", params).fetchone()[0]

        sql = f"""
            SELECT event_ts, method, device, terminal, user_name, comment_raw
            FROM tsd_errors
            WHERE {where_sql}
            ORDER BY {order_col} {order_direction}
            LIMIT ? OFFSET ?
        """
        params.extend([length, start])
        rows = _rows(db.con.execute(sql, params))

    return {
        "draw": 1,
        "recordsTotal": total,
        "recordsFiltered": total,
        "data": rows,
    }


@app.get("/api/pairs/count")
def pairs_count(
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    action: list[str] | None = Query(default=None),
    pair_status: str | None = Query(None),
    min_ms: int | None = Query(None),
):
    where, params = [], []

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    _build_action_filter(action, where, params)

    if pair_status:
        where.append("pair_status = ?"); params.append(pair_status)
    if min_ms is not None:
        where.append("processing_ms >= ?"); params.append(min_ms)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with _open() as db:
        row = db.con.execute(f"SELECT count(*) AS cnt FROM tsd_pairs_mart {where_sql}", params).fetchone()
    return {"count": row[0]}


@app.get("/api/methods")
def methods():
    with _open() as db:
        return [r[0] for r in db.con.execute(
            "SELECT DISTINCT method FROM tsd_pairs_mart WHERE method IS NOT NULL ORDER BY method"
        ).fetchall()]


@app.get("/api/devices")
def devices():
    with _open() as db:
        return [r[0] for r in db.con.execute(
            "SELECT DISTINCT device FROM tsd_pairs_mart WHERE device IS NOT NULL ORDER BY device"
        ).fetchall()]


@app.get("/api/actions")
def get_actions(method: list[str] | None = Query(default=None, description="Фильтр по методу")):
    """
    - Без параметра method: возвращает объект {method: [action, ...]} (полная карта).
    - С параметром method: возвращает плоский список действий для этих методов.
    """
    with _open() as db:
        if method and len(method) > 0:
            placeholders = ", ".join(["?"] * len(method))
            rows = db.con.execute(f"""
                SELECT DISTINCT action
                FROM tsd_pairs_mart
                WHERE method IN ({placeholders}) AND action IS NOT NULL
                ORDER BY action
            """, list(method)).fetchall()
            return [r[0] for r in rows if r[0]]
        else:
            # Полная карта method -> [actions]
            rows = db.con.execute("""
                SELECT method, action
                FROM tsd_pairs_mart
                WHERE action IS NOT NULL
                GROUP BY method, action
                ORDER BY method, action
            """).fetchall()
            mapping: dict[str, list[str]] = {}
            for m, a in rows:
                mapping.setdefault(m, []).append(a)
            return mapping



@app.get("/api/stats/methods")
def method_stats(
    start_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    action: list[str] | None = Query(default=None),
):
    where = []
    params: list[Any] = []

    if start_date:
        where.append("COALESCE(response_ts, request_ts) >= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(start_date, is_end=False))
    if end_date:
        where.append("COALESCE(response_ts, request_ts) <= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(end_date, is_end=True))

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    _build_action_filter(action, where, params)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with _open() as db:
        return _rows(db.con.execute(f"""
            SELECT method,
                   count(*) FILTER (WHERE pair_status = 'ok') AS paired,
                   count(*) FILTER (WHERE pair_status = 'no_response') AS no_response,
                   count(*) FILTER (WHERE pair_status = 'no_request') AS no_request,
                   count(processing_ms) AS timed,
                   round(avg(processing_ms), 1) AS avg_ms,
                   quantile_cont(processing_ms, 0.5) AS p50_ms,
                   round(quantile_cont(processing_ms, 0.95), 2) AS p95_ms,
                   round(quantile_cont(processing_ms, 0.99), 2) AS p99_ms,
                   max(processing_ms) AS max_ms,
                   sum(processing_ms) AS total_ms
            FROM tsd_pairs_mart
            {where_sql}
            GROUP BY method
            ORDER BY total_ms DESC NULLS LAST
        """, params))


@app.get("/api/stats/errors-by-device")
def errors_by_device_stats(
    start_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    device: list[str] | None = Query(default=None),
    method: list[str] | None = Query(default=None),
):
    where = []
    params: list[Any] = []

    if start_date:
        where.append("event_ts >= CAST(? AS TIMESTAMP)")
        params.append(start_date)
    if end_date:
        where.append("event_ts <= CAST(? AS TIMESTAMP)")
        params.append(end_date)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    where_sql = " AND ".join(where) if where else "1=1"

    with _open() as db:
        return _rows(db.con.execute(f"""
            SELECT
                device,
                method,
                count(*) AS error_count,
                count(DISTINCT terminal) AS unique_terminals,
                count(DISTINCT user_name) AS unique_users,
                min(event_ts) AS first_error,
                max(event_ts) AS last_error,
                (SELECT substring(comment_raw, 1, 200) 
                 FROM tsd_errors e2 
                 WHERE e2.device = e1.device 
                   AND e2.method = e1.method
                 ORDER BY e2.event_ts DESC
                 LIMIT 1) AS top_error_sample
            FROM tsd_errors e1
            WHERE {where_sql}
            GROUP BY device, method
            ORDER BY error_count DESC
        """, params))


@app.get("/api/files")
def files():
    with _open() as db:
        return _rows(db.con.execute(
            "SELECT file_name, size_bytes, event_count, status, imported_at "
            "FROM ingest_files ORDER BY file_name"
        ))


@app.get("/api/slow")
def slow(limit: int = Query(100, ge=1, le=1000)):
    with _open() as db:
        return _rows(db.con.execute(f"""
            SELECT request_id, method, processing_ms, terminal,
                   session_id, user_name, response_ts, action
            FROM tsd_pairs_mart
            WHERE processing_ms IS NOT NULL
            ORDER BY processing_ms DESC
            LIMIT {int(limit)}
        """))


@app.get("/api/export.csv")
def export_csv(
    method: list[str] | None = Query(default=None),
    device: list[str] | None = Query(default=None),
    action: list[str] | None = Query(default=None),
    pair_status: str | None = Query(None),
    min_ms: int | None = Query(None),
):
    where, params = [], []

    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)

    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)

    _build_action_filter(action, where, params)

    if pair_status:
        where.append("pair_status = ?"); params.append(pair_status)
    if min_ms is not None:
        where.append("processing_ms >= ?"); params.append(min_ms)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT * FROM tsd_pairs_mart {where_sql} ORDER BY COALESCE(response_ts, request_ts) DESC"

    def generate():
        with _open() as db:
            cur = db.con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            yield ";".join(cols) + "\n"
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for row in rows:
                    yield ";".join("" if v is None else str(v).replace(";", ",") for v in row) + "\n"

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tsd_pairs.csv"},
    )


@app.get("/api/export")
def export_data(
        format: str = Query("csv", pattern="^(csv|jsonl|json)$", description="Формат: csv, jsonl или json"),
        method: list[str] | None = Query(default=None),
        device: list[str] | None = Query(default=None),
        action: list[str] | None = Query(default=None),
        pair_status: str | None = Query(None),
        min_ms: int | None = Query(None),
        start_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
        end_date: str | None = Query(None, description="YYYY-MM-DD HH:MM:SS"),
        limit: int = Query(10000, ge=1, le=500000, description="Максимум записей"),
):
    """
    Потоковый экспорт данных из витрины в CSV/JSONL/JSON.
    Включает тела запроса и ответа (request_body_json, response_body_json) и поле action.
    """
    where = []
    params: list[Any] = []

    if start_date:
        where.append("COALESCE(request_ts, response_ts) >= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(start_date, is_end=False))
    if end_date:
        where.append("COALESCE(request_ts, response_ts) <= CAST(? AS TIMESTAMP)")
        params.append(normalize_date(end_date, is_end=True))
    if method:
        placeholders = ", ".join(["?"] * len(method))
        where.append(f"method IN ({placeholders})")
        params.extend(method)
    if device:
        placeholders = ", ".join(["?"] * len(device))
        where.append(f"device IN ({placeholders})")
        params.extend(device)
    _build_action_filter(action, where, params)
    if pair_status:
        where.append("pair_status = ?")
        params.append(pair_status)
    if min_ms is not None:
        where.append("processing_ms >= ?")
        params.append(min_ms)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # Запрос с телами запроса и ответа + action
    sql = f"""
        SELECT
            request_id, method, device, terminal, action,
            request_ts, response_ts, processing_ms, elapsed_ms,
            pair_status, user_name, session_id, connection_id,
            request_file, response_file,
            request_body_json, response_body_json
        FROM tsd_pairs_mart
        {where_sql}
        ORDER BY COALESCE(request_ts, response_ts) DESC
        LIMIT {int(limit)}
    """

    # Формируем имя файла
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filters = []
    if method and len(method) == 1:
        filters.append(method[0])
    elif method:
        filters.append(f"{len(method)}_methods")
    if action and len(action) == 1:
        filters.append(action[0])
    elif action:
        filters.append(f"{len(action)}_actions")
    if pair_status:
        filters.append(pair_status)
    if min_ms:
        filters.append(f">{min_ms}ms")
    filter_str = "_".join(filters) if filters else "all"

    if format == "csv":
        filename = f"tsd_pairs_{filter_str}_{timestamp}.csv"
        media_type = "text/csv; charset=utf-8"
    elif format == "jsonl":
        filename = f"tsd_pairs_{filter_str}_{timestamp}.jsonl"
        media_type = "application/x-ndjson"
    else:  # json
        filename = f"tsd_pairs_{filter_str}_{timestamp}.json"
        media_type = "application/json"

    def generate():
        with _open() as db:
            cursor = db.con.execute(sql, params)
            columns = [d[0] for d in cursor.description]

            if format == "csv":
                # BOM для корректного открытия в Excel
                yield "\ufeff"
                # Заголовки
                yield ";".join(columns) + "\n"
                # Данные потоково
                while True:
                    rows = cursor.fetchmany(5000)
                    if not rows:
                        break
                    for row in rows:
                        # Преобразуем значения в строки, экранируя спецсимволы
                        values = []
                        for v in row:
                            if v is None:
                                values.append("")
                            else:
                                s = str(v)
                                # Экранирование для CSV: если есть ; " или перенос — оборачиваем в кавычки
                                if any(c in s for c in [';', '"', '\n', '\r']):
                                    s = '"' + s.replace('"', '""') + '"'
                                values.append(s)
                        yield ";".join(values) + "\n"

            elif format == "jsonl":
                # JSON Lines — каждая строка отдельный JSON объект
                while True:
                    rows = cursor.fetchmany(5000)
                    if not rows:
                        break
                    for row in rows:
                        record = dict(zip(columns, row))
                        # Преобразуем datetime в строку
                        for key, value in record.items():
                            if hasattr(value, 'isoformat'):
                                record[key] = value.isoformat()
                        yield json_lib.dumps(record, ensure_ascii=False, default=str) + "\n"

            else:  # json array
                # Собираем все данные в память (только для небольших выборок)
                all_rows = cursor.fetchall()
                data = []
                for row in all_rows:
                    record = dict(zip(columns, row))
                    for key, value in record.items():
                        if hasattr(value, 'isoformat'):
                            record[key] = value.isoformat()
                    data.append(record)
                yield json_lib.dumps(data, ensure_ascii=False, indent=2, default=str)

    return StreamingResponse(
        generate(),
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Total-Rows": str(limit),
        },
    )


# ===== Статика =====
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.exists(STATIC_DIR):
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def read_root():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"error": "Файл static/index.html не найден."}


def run(host: str = "0.0.0.0", port: int = 8000, db_path: str | Path | None = None):
    global _DB_PATH
    if db_path:
        _DB_PATH = Path(db_path)
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()