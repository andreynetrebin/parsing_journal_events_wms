from __future__ import annotations

import argparse
import csv
import errno
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from db import JournalDB, MartDB, get_db_paths

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("tsd")

load_dotenv()

FULL_REBUILD_INTERVAL_DAYS = 7  # Полный пересчёт раз в неделю


EXPORT_SQL = """
    SELECT
        request_id     AS "Идентификатор_запроса",
        method         AS "Метод",
        device         AS "Устройство",
        terminal       AS "Терминал",
        request_ts     AS "Время_запроса",
        response_ts    AS "Время_ответа",
        processing_ms  AS "ВремяОбработкиСообщения_мс",
        elapsed_ms     AS "Разница_меток_времени_мс",
        pair_status    AS "Статус_пары",
        user_name      AS "UserName",
        connection_id  AS "Connection",
        session_id     AS "Session",
        request_file   AS "Файл_запроса",
        response_file  AS "Файл_ответа"
    FROM tsd_pairs_mart
    ORDER BY COALESCE(request_ts, response_ts), request_id
"""


def print_table(cursor, max_rows: int = 200) -> None:
    headers = [d[0] for d in cursor.description]
    rows = [["" if v is None else str(v) for v in row]
            for row in cursor.fetchmany(max_rows)]
    widths = [max([len(h)] + [len(r[i]) for r in rows])
              for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))
    if not rows:
        print("(нет данных)")


def xml_files(logs_dir: Path, pattern: str | None) -> list[Path]:
    return sorted(
        p for p in logs_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".xml"
        and (not pattern or p.match(pattern))
    )


def is_current_hour_file(path: Path) -> bool:
    """
    Проверяет, является ли файл логом за текущий час.
    Формат имени: 20261028_1500.xml (ГГГГММДД_ЧЧММ.xml, где ММ=00)
    """
    try:
        stem = path.stem
        file_dt = datetime.strptime(stem, "%Y%m%d_%H%M")
        now = datetime.now()
        return (file_dt.year == now.year and
                file_dt.month == now.month and
                file_dt.day == now.day and
                file_dt.hour == now.hour)
    except ValueError:
        return False


def rebuild_mart(db_paths: dict, force_full: bool = False) -> bool:
    """
    Умный пересчёт витрины.
    Автоматически выбирает режим:
      - инкрементальный (5-15 сек) — если данные изменились недавно
      - полный (5-10 мин) — раз в 7 дней или по флагу --full
    Возвращает True если пересчёт был выполнен.
    """
    raw_path = db_paths["raw"]
    mart_path = db_paths["mart"]

    # Определяем режим пересчёта
    need_full = force_full
    reason = "принудительный (--full)" if force_full else ""

    try:
        with MartDB(mart_path, read_only=True) as mart_db:
            last_ts, last_mode = mart_db.get_last_rebuild_info()

        if last_ts and not force_full:
            last_dt = datetime.fromisoformat(last_ts)
            days_since = (datetime.now() - last_dt).days
            if days_since >= FULL_REBUILD_INTERVAL_DAYS:
                need_full = True
                reason = f"прошло {days_since} дней с последнего полного пересчёта (лимит {FULL_REBUILD_INTERVAL_DAYS})"
            else:
                reason = f"последний пересчёт был {days_since} дн. назад в режиме '{last_mode}'"
        elif not last_ts:
            need_full = True
            reason = "нет истории пересчётов"
    except Exception as e:
        log.warning(f"⚠️ Не удалось проверить режим пересчёта: {e}")
        need_full = True
        reason = "ошибка проверки"

    if need_full:
        log.info(f"📋 Режим: ПОЛНЫЙ пересчёт ({reason})")
        return _do_full_rebuild(db_paths)
    else:
        log.info(f"📋 Режим: ИНКРЕМЕНТАЛЬНЫЙ пересчёт ({reason})")
        return _do_incremental_rebuild(db_paths)


def _do_full_rebuild(db_paths: dict) -> bool:
    """Полный пересчёт + очистка старых данных."""
    # Очистка старых данных из сырой базы
    log.info("🧹 Очистка старых данных в сырой базе (лимит 14 дней)...")
    with JournalDB(db_paths["raw"], read_only=False) as raw_db:
        deleted = raw_db.cleanup_old_events(days=14)
        if deleted > 0:
            log.info(f"🗑 Удалено {deleted:,} устаревших событий")
        else:
            log.info("✅ Старых событий для удаления не найдено")

    # Полный пересчёт витрины
    start_time = time.time()
    log.info(f"🔄 Начало полного пересчёта витрины:")
    log.info(f"   из : {db_paths['raw']}")
    log.info(f"   в  : {db_paths['mart']}")

    try:
        with MartDB(db_paths["mart"], read_only=False) as mart_db:
            result = mart_db.rebuild_from_raw(db_paths["raw"])
            elapsed = time.time() - start_time

            # Обновляем duration в логе
            mart_db.con.execute("""
                INSERT OR REPLACE INTO rebuild_log 
                (id, rebuild_ts, mode, pairs_count, errors_count, duration_sec)
                VALUES (1, current_timestamp, 'full', ?, ?, ?)
            """, [result['pairs'], result['errors'], elapsed])

            log.info(f"✅ Полный пересчёт завершен:")
            log.info(f"   пар    : {result['pairs']:,}")
            log.info(f"   ошибок : {result['errors']:,}")
            log.info(f"   время  : {elapsed:.1f} сек")
            return True
    except Exception as e:
        log.error(f"❌ Ошибка при полном пересчёте: {e}")
        raise


def _do_incremental_rebuild(db_paths: dict) -> bool:
    """Инкрементальный пересчёт (только новые данные)."""
    start_time = time.time()
    log.info(f"🔄 Начало инкрементального пересчёта витрины:")
    log.info(f"   из : {db_paths['raw']}")
    log.info(f"   в  : {db_paths['mart']}")

    try:
        with MartDB(db_paths["mart"], read_only=False) as mart_db:
            result = mart_db.incremental_rebuild_from_raw(db_paths["raw"], duration_sec=0)
            elapsed = time.time() - start_time

            if result.get("skipped"):
                log.info("✅ Новых данных нет, пересчёт пропущен (0 сек)")
                return False

            log.info(f"✅ Инкрементальный пересчёт завершен:")
            log.info(f"   новых пар обработано : {result.get('new_pairs', 0):,}")
            log.info(f"   всего пар в витрине  : {result['pairs']:,}")
            log.info(f"   ошибок в витрине     : {result['errors']:,}")
            log.info(f"   время                : {elapsed:.1f} сек")
            return True
    except Exception as e:
        log.error(f"❌ Ошибка при инкрементальном пересчёте: {e}")
        log.info("💡 Попробуйте выполнить полный пересчёт: python main.py rebuild-mart --full")
        raise

def cmd_ingest(args) -> None:
    """Импорт XML в сырую базу (wms_raw.duckdb)."""
    db_paths = get_db_paths()
    logs_dir = Path(args.logs_dir)

    if not logs_dir.exists():
        logs_dir.mkdir(parents=True, exist_ok=True)
        log.warning("Создана директория %s. Положите туда XML и повторите запуск.", logs_dir)
        return

    files = xml_files(logs_dir, args.pattern)
    if not files:
        log.warning("В %s нет XML-файлов.", logs_dir)
        # Даже если нет файлов, проверяем необходимость пересчета (например, после очистки)
        if args.auto_rebuild:
            rebuild_mart(db_paths, force=args.force_rebuild)
        return

    ok = skipped = failed = deleted = 0

    # Работаем ТОЛЬКО с сырой базой — витрина не блокируется
    with JournalDB(db_paths["raw"], read_only=False) as db:
        for path in files:
            log.info("Файл: %s (%.2f МБ)", path.name, path.stat().st_size / 1024 / 1024)
            try:
                result = db.import_file(path, force=args.force)
            except ET.ParseError as exc:
                failed += 1
                log.error("Повреждённый XML %s: %s", path.name, exc)
                continue
            except OSError as exc:
                if exc.errno == errno.EBUSY:
                    skipped += 1
                    log.warning("⏳ Файл занят другой программой, пропускаем: %s", path.name)
                    continue
                else:
                    failed += 1
                    log.exception("Ошибка OS при чтении %s: %s", path.name, exc)
                    continue
            except Exception as exc:
                failed += 1
                log.exception("Ошибка импорта %s: %s", path.name, exc)
                continue

            if result["status"] == "imported":
                ok += 1
                log.info("  Импортировано ТСД-событий: %s", result["events"])
                if args.delete and not is_current_hour_file(path):
                    try:
                        path.unlink()
                        deleted += 1
                        log.info("  🗑 Файл удалён: %s", path.name)
                    except OSError as e:
                        log.warning("  ⚠ Не удалось удалить %s: %s", path.name, e)
            elif result["status"] == "duplicate":
                skipped += 1
                log.info("  Пропуск: тот же контент уже импортирован как %s", result.get("other"))
                if args.delete and not is_current_hour_file(path):
                    try:
                        path.unlink()
                        deleted += 1
                        log.info("  🗑 Дубликат удалён: %s", path.name)
                    except OSError as e:
                        log.warning("  ⚠ Не удалось удалить дубликат %s: %s", path.name, e)
            else:
                skipped += 1
                log.info("  Пропуск: файл уже импортирован")
                if args.delete and not is_current_hour_file(path):
                    try:
                        path.unlink()
                        deleted += 1
                        log.info("  🗑 Уже импортированный файл удалён: %s", path.name)
                    except OSError as e:
                        log.warning("  ⚠ Не удалось удалить %s: %s", path.name, e)

        log.info("Итого: импортировано=%s, пропущено=%s, ошибок=%s, удалено=%s",
                 ok, skipped, failed, deleted)

        print("\n=== События в сырой БД ===")
        print_table(db.con.execute("""
            SELECT count(*) AS events,
                   count(*) FILTER (WHERE direction = 'request')  AS requests,
                   count(*) FILTER (WHERE direction = 'response') AS responses,
                   count(*) FILTER (WHERE direction = 'error')    AS errors,
                   count(*) FILTER (WHERE request_id IS NULL)     AS without_id
            FROM tsd_events
        """))

    # Автоматический пересчёт витрины
    if args.auto_rebuild:
        if ok > 0:
            log.info("\n🔄 Запуск пересчёта витрины (импортированы новые данные)...")
            rebuild_mart(db_paths, force_full=args.force_rebuild)
        else:
            log.info("\n⏭️ Пересчёт витрины пропущен (нет новых данных)")


def cmd_rebuild_mart(args) -> None:
    """Ручной пересчёт витрины."""
    db_paths = get_db_paths()
    rebuild_mart(db_paths, force_full=args.full)


def cmd_cleanup(args) -> None:
    """Очистка старых данных из сырой базы."""
    db_paths = get_db_paths()
    days = args.days

    log.info(f"🧹 Очистка данных старше {days} дней...")
    with JournalDB(db_paths["raw"], read_only=False) as raw_db:
        deleted_count = raw_db.cleanup_old_events(days=days)
        if deleted_count > 0:
            log.info(f"🗑 Удалено {deleted_count:,} устаревших событий")
        else:
            log.info("✅ Старых событий для удаления не найдено")

    # После очистки обязательно пересчитываем витрину
    if args.auto_rebuild:
        log.info("\n🔄 Пересчет витрины после очистки...")
        rebuild_mart(db_paths, force=True)


def cmd_export(args) -> None:
    """Экспорт пар из витрины в CSV."""
    db_paths = get_db_paths()
    out = Path(args.out) if args.out else (
            BASE_DIR / "exports" / f"tsd_pairs_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    with MartDB(db_paths["mart"], read_only=True) as db, \
            out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        cursor = db.con.execute(EXPORT_SQL)
        writer.writerow([d[0] for d in cursor.description])
        total = 0
        while True:
            rows = cursor.fetchmany(5000)
            if not rows:
                break
            writer.writerows(rows)
            total += len(rows)

    log.info("Экспортировано строк: %s -> %s", total, out.resolve())


def cmd_report(args) -> None:
    """Статистика по методам (из витрины)."""
    db_paths = get_db_paths()
    with MartDB(db_paths["mart"], read_only=True) as db:
        print("=== Статистика по методам ===")
        print_table(db.con.execute("SELECT * FROM v_method_stats"), max_rows=args.limit)


def cmd_sql(args) -> None:
    """Произвольный SELECT к витрине."""
    db_paths = get_db_paths()
    with MartDB(db_paths["mart"], read_only=True) as db:
        print_table(db.con.execute(args.query), max_rows=args.limit)


def cmd_status(args) -> None:
    """Реестр импортированных файлов (из сырой базы)."""
    db_paths = get_db_paths()
    with JournalDB(db_paths["raw"], read_only=True) as db:
        print("=== Импортированные файлы ===")
        print_table(db.con.execute("""
            SELECT file_name, size_bytes, event_count, status, imported_at
            FROM ingest_files ORDER BY file_name
        """))
        print(f"\nСырая БД : {db_paths['raw'].resolve()}")
        print(f"Витрина  : {db_paths['mart'].resolve()}")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    logs_dir = os.getenv("TSD_LOGS_DIR", str(BASE_DIR / "logs"))
    db_path = os.getenv("TSD_DB_PATH", str(BASE_DIR / "data" / "wms.duckdb"))

    common.add_argument("--logs-dir", default=logs_dir)
    common.add_argument("--db", default=db_path)
    common.add_argument("--auto-rebuild", action="store_true", default=True,
                        help="автоматически пересчитывать витрину после импорта (по умолчанию включено)")
    common.add_argument("--no-auto-rebuild", action="store_false", dest="auto_rebuild",
                        help="не пересчитывать витрину автоматически")
    common.add_argument("--force-rebuild", action="store_true",
                        help="принудительный пересчет витрины даже если данные не изменились")

    p = argparse.ArgumentParser(description="Парсинг журнала событий ТСД в DuckDB")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("ingest", parents=[common], help="импортировать XML из logs/")
    s.add_argument("--force", action="store_true", help="переимпортировать даже уже загруженные")
    s.add_argument("--pattern", help="фильтр имён, например EventLog_*.xml")
    s.add_argument("--delete", action="store_true", help="удалять XML-файл после успешного импорта")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("rebuild-mart", parents=[common], help="пересчитать витрину")
    s.add_argument("--full", action="store_true",
                   help="принудительный полный пересчёт (вместо инкрементального)")
    s.set_defaults(func=cmd_rebuild_mart)

    s = sub.add_parser("cleanup", parents=[common], help="очистить старые данные")
    s.add_argument("--days", type=int, default=14, help="хранить данные не старше N дней (по умолчанию 14)")
    s.set_defaults(func=cmd_cleanup)

    s = sub.add_parser("export", parents=[common], help="выгрузить пары в CSV")
    s.add_argument("--out")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("report", parents=[common], help="статистика по методам")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("sql", parents=[common], help="произвольный SELECT")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_sql)

    s = sub.add_parser("status", parents=[common], help="реестр импорта")
    s.set_defaults(func=cmd_status)

    return p


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()