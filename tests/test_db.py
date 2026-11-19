from __future__ import annotations

from db import JournalDB


def test_schema_created(db_path):
    with JournalDB(db_path, read_only=False) as db:
        tables = [r[0] for r in db.con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()]
    assert "tsd_events" in tables
    assert "ingest_files" in tables


def test_views_created(db_path):
    with JournalDB(db_path, read_only=False) as db:
        views = [r[0] for r in db.con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_type='VIEW'"
        ).fetchall()]
    assert "v_tsd_pairs" in views
    assert "v_method_stats" in views
    assert "v_method_hourly" in views


def test_import_file_counts(db_path, sample_xml):
    with JournalDB(db_path, read_only=False) as db:
        result = db.import_file(sample_xml)
    assert result["status"] == "imported"
    assert result["events"] == 3  # 1 запрос + 2 ответа


def test_import_idempotent(db_path, sample_xml):
    with JournalDB(db_path, read_only=False) as db:
        r1 = db.import_file(sample_xml)
        r2 = db.import_file(sample_xml)  # повторный запуск
    assert r1["status"] == "imported"
    assert r2["status"] == "skipped"

    # Количество событий не удвоилось
    with JournalDB(db_path, read_only=True) as db:
        total = db.con.execute("SELECT count(*) FROM tsd_events").fetchone()[0]
    assert total == 3


def test_import_force_reimports(db_path, sample_xml):
    with JournalDB(db_path, read_only=False) as db:
        db.import_file(sample_xml)
        r = db.import_file(sample_xml, force=True)
    assert r["status"] == "imported"


def test_pairs_view_links_request_and_response(db_path, sample_xml):
    with JournalDB(db_path, read_only=False) as db:
        db.import_file(sample_xml)
        pairs = db.con.execute("""
            SELECT request_id, method, pair_status, processing_ms
            FROM v_tsd_pairs
            ORDER BY request_id
        """).fetchall()

    # В sample.xml: ID=1792562909815 имеет и запрос, и ответ -> ok
    # ID=1792569381298 имеет только ответ -> no_request
    assert len(pairs) == 2

    by_id = {row[0]: row for row in pairs}
    assert by_id[1792562909815][2] == "ok"
    assert by_id[1792562909815][3] == 40  # processing_ms
    assert by_id[1792569381298][2] == "no_request"
    assert by_id[1792569381298][3] == 6343


def test_method_stats_view(db_path, sample_xml):
    with JournalDB(db_path, read_only=False) as db:
        db.import_file(sample_xml)
        stats = db.con.execute("SELECT method, paired, no_request FROM v_method_stats").fetchall()

    by_method = {row[0]: row for row in stats}
    assert by_method["GetGeneralDatas"][1] == 1      # paired
    assert by_method["SortByPickingSKU"][2] == 1     # no_request


def test_read_only_mode(db_path, sample_xml):
    # Сначала заполняем БД
    with JournalDB(db_path, read_only=False) as db:
        db.import_file(sample_xml)

    # В read_only режиме нельзя писать
    with JournalDB(db_path, read_only=True) as db:
        total = db.con.execute("SELECT count(*) FROM tsd_events").fetchone()[0]
        assert total == 3

        try:
            db.import_file(sample_xml)
        except RuntimeError as e:
            assert "read_only" in str(e)
        else:
            raise AssertionError("Ожидалось RuntimeError")


def test_concurrent_reads(db_path, sample_xml):
    """Несколько read_only-соединений могут читать параллельно."""
    with JournalDB(db_path, read_only=False) as db:
        db.import_file(sample_xml)

    readers = []
    for _ in range(5):
        with JournalDB(db_path, read_only=True) as db:
            total = db.con.execute("SELECT count(*) FROM tsd_events").fetchone()[0]
            readers.append(total)

    assert all(t == 3 for t in readers)