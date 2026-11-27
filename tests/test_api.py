from __future__ import annotations


def test_health(api_client):
    r = api_client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["events_total"] == 3


def test_methods_list(api_client):
    r = api_client.get("/api/methods")
    assert r.status_code == 200
    methods = r.json()
    assert "GetGeneralDatas" in methods
    assert "SortByPickingSKU" in methods


def test_method_stats(api_client):
    r = api_client.get("/api/stats/methods")
    assert r.status_code == 200
    stats = r.json()
    by_method = {s["method"]: s for s in stats}
    assert by_method["GetGeneralDatas"]["paired"] == 1
    assert by_method["SortByPickingSKU"]["no_request"] == 1


def test_pairs_default(api_client):
    r = api_client.get("/api/pairs")
    assert r.status_code == 200
    pairs = r.json()
    assert len(pairs) == 2


def test_pairs_filter_by_method(api_client):
    r = api_client.get("/api/pairs?method=GetGeneralDatas")
    assert r.status_code == 200
    pairs = r.json()
    assert len(pairs) == 1
    assert pairs[0]["method"] == "GetGeneralDatas"


def test_pairs_filter_by_status(api_client):
    r = api_client.get("/api/pairs?pair_status=no_request")
    assert r.status_code == 200
    pairs = r.json()
    assert len(pairs) == 1
    assert pairs[0]["request_id"] == 1792569381298


def test_pairs_count(api_client):
    r = api_client.get("/api/pairs/count")
    assert r.status_code == 200
    assert r.json()["count"] == 2


def test_pairs_min_ms(api_client):
    r = api_client.get("/api/pairs?min_ms=1000")
    assert r.status_code == 200
    pairs = r.json()
    assert len(pairs) == 1
    assert pairs[0]["processing_ms"] == 6343


def test_orphans(api_client):
    r = api_client.get("/api/orphans")
    assert r.status_code == 200
    orphans = r.json()
    assert len(orphans) == 1
    assert orphans[0]["pair_status"] == "no_request"


def test_slow(api_client):
    r = api_client.get("/api/slow?limit=10")
    assert r.status_code == 200
    slow = r.json()
    assert slow[0]["processing_ms"] == 6343


def test_files(api_client):
    r = api_client.get("/api/files")
    assert r.status_code == 200
    files = r.json()
    assert len(files) == 1
    assert files[0]["event_count"] == 3
    assert files[0]["status"] == "ok"


def test_export_csv(api_client):
    r = api_client.get("/api/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    lines = r.text.strip().split("\n")
    # Заголовок + 2 строки данных
    assert len(lines) == 3
    assert "request_id" in lines[0]
    assert ";" in lines[0]  # разделитель


def test_export_endpoint_csv(api_client):
    r = api_client.get("/api/export?format=csv&limit=100")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "request_id" in r.content.decode("utf-8-sig").splitlines()[0]


def test_export_endpoint_json_array(api_client):
    import json

    r = api_client.get("/api/export?format=json&limit=100")
    assert r.status_code == 200
    rows = json.loads(r.content)
    assert len(rows) == 2
    assert rows[0]["request_id"] is not None


def test_export_endpoint_jsonl(api_client):
    import json

    r = api_client.get("/api/export?format=jsonl&limit=100")
    assert r.status_code == 200
    rows = [json.loads(line) for line in r.text.splitlines()]
    assert len(rows) == 2


def test_export_json_limit_rejected_before_stream(api_client):
    r = api_client.get("/api/export?format=json&limit=50001")
    assert r.status_code == 400
    assert "50 000" in r.json()["detail"]


def test_export_body_search_filter(api_client):
    r = api_client.get("/api/export?format=json&body_search=SortByPickingSKU")
    assert r.status_code == 200
    import json
    rows = json.loads(r.content)
    assert rows


def test_hourly_stats(api_client):
    r = api_client.get("/api/stats/hourly?hours=24")
    assert r.status_code == 200
    # Данные есть (в sample.xml события в 2026-10-21, но это ок для теста структуры)
    assert isinstance(r.json(), list)