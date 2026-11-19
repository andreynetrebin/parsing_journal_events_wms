from __future__ import annotations

from parser import (
    _build_record,
    _milliseconds,
    _read_fields,
    iter_tsd_events,
    REQUEST_PREFIX,
    RESPONSE_PREFIX,
)
import xml.etree.ElementTree as ET


def test_milliseconds_simple():
    assert _milliseconds("40") == 40
    assert _milliseconds("100") == 100


def test_milliseconds_with_spaces():
    # Обычный пробел
    assert _milliseconds("6 343") == 6343
    # Неразрывный пробел (U+00A0)
    assert _milliseconds("6\u00a0343") == 6343
    # Узкий неразрывный пробел (U+202F)
    assert _milliseconds("1\u202f234\u202f567") == 1234567


def test_milliseconds_invalid():
    assert _milliseconds(None) is None
    assert _milliseconds("") is None
    assert _milliseconds("abc") is None


def test_build_record_request():
    fields = {
        "Date": "2026-10-21T09:08:29",
        "Event": "ЗапросТСД.94:FB:29:D6:D9:7A.GetGeneralDatas",
        "EventPresentation": "ЗапросТСД.94:FB:29:D6:D9:7A.GetGeneralDatas",
        "User": "user-1",
        "UserName": "RF",
        "Connection": "346831",
        "Session": "16421437",
        "Comment": "Идентификатор запроса - 1792562909815",
        "Data": "ef69190d-c9c1-11eb-ac86-000af7e0ad15",
        "DataPresentation": "94:FB:29:D6:D9:7A",
    }
    rec = _build_record(fields, 1)
    assert rec is not None
    assert rec["direction"] == "request"
    assert rec["method"] == "GetGeneralDatas"
    assert rec["device"] == "94:FB:29:D6:D9:7A"
    assert rec["request_id"] == 1792562909815
    assert rec["processing_ms"] is None  # у запроса времени нет
    assert rec["user_name"] == "RF"
    assert rec["session_id"] == "16421437"


def test_build_record_response_with_nbsp():
    fields = {
        "Date": "2026-10-21T09:08:30",
        "Event": "ОтветТСД.78:B8:D6:31:BA:A3.SortByPickingSKU",
        "EventPresentation": "ОтветТСД.78:B8:D6:31:BA:A3.SortByPickingSKU",
        "User": "user-2",
        "UserName": "OP",
        "Connection": "425257",
        "Session": "16626942",
        "Comment": "Идентификатор запроса - 1792569381298\nВремяОбработкиСообщения - 6\u00a0343мс.",
    }
    rec = _build_record(fields, 2)
    assert rec is not None
    assert rec["direction"] == "response"
    assert rec["method"] == "SortByPickingSKU"
    assert rec["processing_ms"] == 6343
    assert rec["request_id"] == 1792569381298


def test_build_record_skips_non_tsd():
    fields = {
        "Date": "2026-10-21T09:09:00",
        "Event": "_$Data$_.Update",
        "EventPresentation": "Данные. Изменение",
        "Comment": "",
    }
    assert _build_record(fields, 3) is None


def test_build_record_uses_event_presentation_when_event_empty():
    fields = {
        "Date": "2026-10-21T09:08:29",
        "Event": "",
        "EventPresentation": "ЗапросТСД.AA:BB:CC.MethodX",
        "Comment": "Идентификатор запроса - 42",
    }
    rec = _build_record(fields, 1)
    assert rec is not None
    assert rec["method"] == "MethodX"
    assert rec["event_name"].startswith(REQUEST_PREFIX)


def test_iter_tsd_events(sample_xml):
    events = list(iter_tsd_events(sample_xml))
    # В sample.xml: 1 запрос, 2 ответа, 1 не-ТСД событие = 3 ТСД-события
    assert len(events) == 3

    directions = [e["direction"] for e in events]
    assert directions.count("request") == 1
    assert directions.count("response") == 2

    # Все события из одного файла
    assert all(e["source_file"] == sample_xml.name for e in events)

    # У ответа с неразрывным пробелом время корректно
    slow = [e for e in events if e["processing_ms"] == 6343]
    assert len(slow) == 1
    assert slow[0]["method"] == "SortByPickingSKU"