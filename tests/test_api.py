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


def test_hourly_stats(api_client):
    r = api_client.get("/api/stats/hourly?hours=24")
    assert r.status_code == 200
    # Данные есть (в sample.xml события в 2026-10-21, но это ок для теста структуры)
    assert isinstance(r.json(), list)