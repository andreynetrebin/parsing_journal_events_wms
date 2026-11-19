"""Потоковый разбор v8e:EventLog в плоские записи ТСД."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Iterator

REQUEST_PREFIX = "ЗапросТСД"
RESPONSE_PREFIX = "ОтветТСД"
ERROR_PREFIX = "ОшибкаТСД"

# Объединяем все допустимые префиксы для быстрой проверки
VALID_PREFIXES = (REQUEST_PREFIX, RESPONSE_PREFIX, ERROR_PREFIX)

RE_REQUEST_ID = re.compile(
    r"Идентификатор\s+запроса\s*[:=\-]?\s*(\d+)", re.IGNORECASE
)
RE_PROCESSING = re.compile(
    r"ВремяОбработкиСообщения\s*[:=\-]?\s*"
    r"([0-9][0-9 \t\xa0\u202f\u2009\r\n]*)\s*мс\.?",
    re.IGNORECASE,
)


def split_comment_to_json_and_metadata(comment: str) -> tuple[str | None, str]:
    """
    Разделяет Comment на JSON-тело и метаданные.
    Если JSON нет (как в случае с ошибками), вернёт None и весь текст как metadata.
    """
    if not comment:
        return None, ""

    first_brace = comment.find('{')
    last_brace = comment.rfind('}')

    if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
        return None, comment.strip()

    json_part = comment[first_brace:last_brace + 1].strip()
    metadata_part = comment[last_brace + 1:].strip()

    try:
        json.loads(json_part)
        return json_part, metadata_part
    except json.JSONDecodeError:
        return None, comment.strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _milliseconds(raw: str | None) -> int | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else None


def _read_fields(elem: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for child in elem:
        name = _local(child.tag)
        if child.text and name not in result:
            result[name] = child.text
    return result


def _build_record(fields: dict[str, str], source_row: int) -> dict | None:
    date_raw = (fields.get("Date") or "").strip()
    if not date_raw:
        return None

    try:
        event_ts = datetime.fromisoformat(date_raw)
    except ValueError:
        event_ts = None

    code = (fields.get("Event") or "").strip()
    presentation = (fields.get("EventPresentation") or "").strip()

    # Ищем первое совпадение с любым из допустимых префиксов
    event_name = next(
        (v for v in (code, presentation) if v.startswith(VALID_PREFIXES)),
        "",
    )
    if not event_name:
        return None  # Это не событие ТСД, пропускаем

    # Определяем направление и префикс
    if event_name.startswith(REQUEST_PREFIX):
        direction = "request"
        prefix = REQUEST_PREFIX
    elif event_name.startswith(RESPONSE_PREFIX):
        direction = "response"
        prefix = RESPONSE_PREFIX
    elif event_name.startswith(ERROR_PREFIX):
        direction = "error"
        prefix = ERROR_PREFIX
    else:
        return None

    # Извлекаем устройство и метод (например: "78:B8:D6:F:86:69.GetGeneralDatas")
    tail = event_name[len(prefix):].lstrip(".")
    device, dot, method = tail.rpartition(".")
    if not dot:
        device, method = "", tail

    comment_raw = fields.get("Comment") or ""
    body_json, metadata = split_comment_to_json_and_metadata(comment_raw)

    # Для ошибок request_id и processing_ms не извлекутся (вернут None), что корректно
    m_id = RE_REQUEST_ID.search(metadata)
    request_id = int(m_id.group(1)) if m_id else None

    m_time = RE_PROCESSING.search(metadata) if direction == "response" else None
    processing_ms = _milliseconds(m_time.group(1)) if m_time else None

    def s(key: str) -> str | None:
        return (fields.get(key) or "").strip() or None

    return {
        "source_row": source_row,
        "event_ts": event_ts,
        "direction": direction,
        "event_name": event_name,
        "method": method or None,
        "device": device or None,
        "request_id": request_id,
        "processing_ms": processing_ms,
        "user_id": s("User"),
        "user_name": s("UserName"),
        "connection_id": s("Connection"),
        "session_id": s("Session"),
        "terminal": s("DataPresentation"),
        "object_id": s("Data"),
        "comment_raw": comment_raw,
        "body_json": body_json,
        "metadata": metadata,
    }


def iter_tsd_events(path: Path) -> Iterator[dict]:
    row_number = 0
    for _, elem in ET.iterparse(str(path), events=("end",)):
        if _local(elem.tag) != "Event":
            continue

        record = _build_record(_read_fields(elem), row_number + 1)
        if record is None:
            elem.clear()
            continue

        row_number += 1
        record["source_file"] = path.name

        # === ИЗВЛЕЧЕНИЕ ACTION ИЗ body_json ===
        action_value = None
        body_json = record.get("body_json")

        if body_json:
            try:
                # Пытаемся распарсить JSON и получить значение ключа "Action"
                body_dict = json.loads(body_json)
                action_value = body_dict.get("Action")
            except (json.JSONDecodeError, TypeError, AttributeError):
                # Если JSON невалидный или body_json отсутствует, оставляем None
                action_value = None

        record["action"] = action_value
        # ======================================

        yield record
        elem.clear()