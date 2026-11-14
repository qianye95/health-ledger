"""Private health ledger for one person. Sources are read-only."""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "xunji"))
import xunji

TZ = ZoneInfo("Asia/Shanghai")
USER = "owner"
STATE = Path(os.environ.get("HEALTH_LEDGER_STATE", HERE / ".state"))
DASHBOARD_URL = os.environ.get("HEALTH_DASHBOARD_URL", "").strip()
SOURCES = ("xunji-training", "xunji-body", "xunji-food", "zepp", "zepp-workout")
XUNJI_ENV = Path.home() / ".codex/secrets/xunji.env"
SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    user_id TEXT NOT NULL, source TEXT NOT NULL, day TEXT NOT NULL,
    payload TEXT NOT NULL, observed_at TEXT NOT NULL, snapshot TEXT NOT NULL,
    PRIMARY KEY (user_id, source, day)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, source TEXT NOT NULL,
    start_day TEXT NOT NULL, end_day TEXT NOT NULL, completed_at TEXT NOT NULL,
    status TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    user_id TEXT NOT NULL, week_end TEXT NOT NULL, sent_at TEXT NOT NULL,
    PRIMARY KEY(user_id, week_end)
);
CREATE TABLE IF NOT EXISTS daily_deliveries (
    user_id TEXT NOT NULL, day TEXT NOT NULL, sent_at TEXT NOT NULL,
    PRIMARY KEY(user_id, day)
);
CREATE TABLE IF NOT EXISTS report_attempts (
    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
    period TEXT NOT NULL, attempted_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zepp_backfill (
    user_id TEXT NOT NULL, start_day TEXT NOT NULL, end_day TEXT NOT NULL,
    completed_at TEXT NOT NULL, days_returned INTEGER NOT NULL,
    PRIMARY KEY (user_id, start_day, end_day)
);
"""


class SourceError(Exception):
    pass


class DeliveryUnknown(SourceError):
    pass


class PartialSync(SourceError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def report_file(kind, period):
    return STATE / "reports" / USER / period / ("daily.md" if kind == "daily" else "report.md")


def retry_saved_report(db, kind, today):
    if kind not in ("daily", "weekly"):
        raise SourceError("无效的报告类型")
    saturday = today - timedelta(days=(today.weekday() - 5) % 7)
    period = (today if kind == "daily" else saturday - timedelta(days=1)).isoformat()
    path = report_file(kind, period)
    if not path.is_file():
        raise SourceError("没有可重发的报告")
    from delivery import deliver_report
    deliver_report(db, kind, period, path.read_text())


def dates(start, end):
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def iso_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise SourceError("响应缺少有效日期")
    return date.fromisoformat(value).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def snapshot(source, payload, start, end, fetched):
    folder = STATE / "raw" / USER / source
    folder.mkdir(parents=True, exist_ok=True)
    raw = encoded({"start": start, "end": end, "fetched_at": fetched, "payload": payload}).encode()
    path = folder / (hashlib.sha256(raw).hexdigest() + ".json.gz")
    if not path.exists():
        atomic(path, gzip.compress(raw))
    return str(path.relative_to(STATE))


def ingest(db, source, payload, parts, start, end, *, observed=None, preserve_empty=False):
    fetched = now()
    observed = observed or fetched
    ref = snapshot(source, payload, start, end, observed)
    with db:
        previous = dict(db.execute("""SELECT day,payload FROM observations
                                      WHERE user_id=? AND source=? AND day BETWEEN ? AND ?""",
                                   (USER, source, start, end))) if preserve_empty else {}
        unconfirmed = [day for day, rows in parts.items() if not rows and previous.get(day, "[]") != "[]"]
        db.executemany("""
            INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, source, day) DO UPDATE SET
                payload=excluded.payload, observed_at=excluded.observed_at, snapshot=excluded.snapshot
            WHERE excluded.observed_at >= observations.observed_at
              AND NOT (? AND excluded.payload = '[]' AND observations.payload <> '[]')
        """, [(USER, source, day, encoded(row), observed, ref, preserve_empty) for day, row in parts.items()])
        if unconfirmed:
            log_run(db, source, start, end, "partial", "上游未返回记录，保留原数据：" + "、".join(unconfirmed))
        else:
            log_run(db, source, start, end, "ok", f"{len(parts)} 个日期")
    return not unconfirmed


def log_run(db, source, start, end, status, detail):
    db.execute("INSERT INTO sync_runs (user_id,source,start_day,end_day,completed_at,status,detail) VALUES (?,?,?,?,?,?,?)",
               (USER, source, start, end, now(), status, detail))


def secret(name):
    if not XUNJI_ENV.is_file():
        raise SourceError("等待配置凭据")
    info = XUNJI_ENV.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o600:
        raise SourceError("凭据文件需要当前服务用户所有，权限 600")
    for line in XUNJI_ENV.read_text().splitlines():
        field, _, value = line.partition("=")
        if field == name and re.fullmatch(r"[!-~]+", value):
            return value
    raise SourceError(f"等待配置 {name}")


def fetch_xunji(url, key, params):
    request = Request(url, data=encoded(params).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + secret(key),
    })
    try:
        with build_opener(xunji.NoRedirect()).open(request, timeout=20) as response:
            data = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip" or data.startswith(b"\x1f\x8b"):
                data = gzip.decompress(data)
            result = json.loads(data)
    except HTTPError as e:
        raise SourceError(f"训记 HTTP {e.code}") from None
    except (URLError, OSError, EOFError, ValueError) as e:
        raise SourceError(f"训记传输或解析失败（{type(e).__name__}）") from None
    if not isinstance(result, dict) or result.get("success") is False or "res" not in result:
        # Report only recognizable error categories. Remote payloads may contain account data.
        msg = encoded(result).lower()
        for marker, detail in (("too frequent", "训记限频"), ("apikey invalid", "Key 无效"),
                               ("apikey missing", "Key 缺失"), ("仅vip可用", "接口需要 VIP 权限")):
            if marker in msg:
                raise SourceError(detail)
        raise SourceError("训记拒绝请求或缺少结果")
    return result


def group_rows(items, days):
    if not isinstance(items, list):
        raise SourceError("响应缺少可识别的记录列表")
    parts = {day: [] for day in days}
    for item in items:
        if not isinstance(item, dict):
            raise SourceError("响应中的记录格式有误")
        day = iso_day(item.get("datestr") or item.get("date"))
        if day not in parts:
            raise SourceError(f"上游返回请求范围外的日期 {day}")
        parts[day].append(item)
    return parts


def fetch_range(source, start, end, days):
    common = {"start_date": start, "end_date": end}
    if source == "xunji-body":
        url = "https://api.xunjiapp.cn/open/body/query_gzip"
        params = {**common, "types": ["weight", "weist"], "include_records": True,
                  "include_latest": False, "limit": 500, "offset": 0}
        pages, rows = [], []
        while True:
            response = fetch_xunji(url, "XUNJI_BODY_KEY", params)
            page = response["res"]
            if not isinstance(page, dict) or not isinstance(page.get("records"), list):
                raise SourceError("训记身体数据缺少 records")
            items = page["records"]
            pages.append(response)
            rows.extend(items)
            if len(items) < params["limit"]:
                return pages, group_rows(rows, days)
            if len(pages) >= 20:
                raise SourceError("身体数据分页超出本次上限")
            params["offset"] += len(items)
            time.sleep(15)
    response = fetch_xunji("https://eatings.xunjiapp.cn/open/food/query_gzip", "XUNJI_FOOD_KEY",
                           {**common, "include_detail": True})
    result = response["res"]
    if isinstance(result, dict):
        for field in ("records", "days", "entries", "eatings"):
            if isinstance(result.get(field), list):
                result = result[field]
                break
    try:
        parts = group_rows(result, days)
    except (SourceError, ValueError):
        snapshot(source, response, start, end, now())
        raise SourceError("饮食已获取，回包结构待核对，未更新总账") from None
    return response, parts


def import_zepp(db, path, *, requested=None):
    payload = json.loads(path.read_bytes())
    rows = payload.get("records")
    if payload.get("timezone") != "Asia/Shanghai" or not isinstance(rows, list) or (not rows and requested is None):
        raise SourceError("Zepp 导出文件没有可识别的北京时间日记录")
    if requested and (payload.get("requested_from"), payload.get("requested_to")) != requested:
        raise SourceError("Zepp 导出查询范围与请求不一致")
    stamp = datetime.fromisoformat(payload["fetched_at"])
    if stamp.tzinfo is None:
        raise SourceError("Zepp 导出时间缺少时区")
    parts = {}
    for row in rows:
        day = iso_day(row["date"])
        if day in parts:
            raise SourceError("Zepp 导出含重复日期")
        if requested and not requested[0] <= day <= requested[1]:
            continue
        parts[day] = row
    first, last = requested or (min(parts), max(parts))
    ingest(db, "zepp", payload, parts, first, last,
           observed=stamp.astimezone(timezone.utc).isoformat(timespec="microseconds"))
    return len(parts)


def fetch_zepp(db, start, end, *, workouts=False):
    command = ([str(HERE / ".venv/bin/python3"), str(HERE / "zepp_cloud.py"), "--workouts"]
               if workouts else [str(STATE / "zepp-sync")])
    if not Path(command[0]).is_file():
        raise SourceError("等待 Zepp 云端采集")
    inbox = STATE / "incoming"
    inbox.mkdir(exist_ok=True)
    path = inbox / ("zepp-workout.json" if workouts else "zepp.json")
    path.unlink(missing_ok=True)
    try:
        result = subprocess.run([*command, str(path), start, end],
                                capture_output=True, timeout=180, check=False)
        if result.returncode == 78:
            raise SourceError("等待 Zepp 云端授权")
        if result.returncode or not path.is_file():
            detail = result.stderr.decode("utf-8", "replace").strip()
            if not re.fullmatch(r"Zepp [^\n]{1,80}失败（[A-Za-z]+）", detail):
                detail = "Zepp 云端采集失败，请核对授权和网络"
            raise SourceError(detail)
        if not workouts:
            return import_zepp(db, path, requested=(start, end))
        payload = json.loads(path.read_bytes())
        if (payload["requested_from"], payload["requested_to"]) != (start, end):
            raise SourceError("Zepp 运动导出范围与请求不一致")
        parts = group_rows(payload["records"], dates(date.fromisoformat(start), date.fromisoformat(end)))
        if not ingest(db, "zepp-workout", payload, parts, start, end,
                      observed=payload["fetched_at"], preserve_empty=True):
            raise PartialSync("Zepp 部分运动日期未返回，已保留原数据")
        return sum(map(len, parts.values()))
    except subprocess.TimeoutExpired:
        raise SourceError("Zepp 云端采集超时") from None
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise SourceError(f"Zepp 回包处理失败（{type(error).__name__}）") from None
    finally:
        path.unlink(missing_ok=True)


def monthly_windows(start, end):
    while start <= end:
        next_month = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        stop = min(end, next_month - timedelta(days=1))
        yield start.isoformat(), stop.isoformat()
        start = next_month


def backfill_zepp(db, since, end):
    windows = list(monthly_windows(since, end))
    for start, stop in reversed(windows):
        if db.execute("SELECT 1 FROM zepp_backfill WHERE user_id=? AND start_day=? AND end_day=?",
                      (USER, start, stop)).fetchone():
            continue
        try:
            count = fetch_zepp(db, start, stop)
        except SourceError as error:
            with db:
                log_run(db, "zepp", start, stop, "error", str(error))
            raise SourceError(f"Zepp 回溯停在 {start} 至 {stop}：{error}。已完成区间会保留。") from None
        with db:
            db.execute("INSERT INTO zepp_backfill VALUES (?,?,?,?,?)", (USER, start, stop, now(), count))
        print(f"Zepp {start} 至 {stop}：返回 {count} 天", flush=True)
        time.sleep(2)
    print("Zepp 历史回溯完成。")


XUNJI_SOURCES = ("xunji-training", "xunji-body", "xunji-food")


def sync(db, start, end, *, zepp_end=None, body_end=None, xunji_end=None, sources=SOURCES):
    days = dates(start, end)
    failed = []
    for source in sources:
        status = "error"
        if source == "xunji-training":
            source_days = dates(start, xunji_end) if xunji_end else days
            failed_days = []
            for day in source_days:
                try:
                    payload = fetch_xunji("https://trains.xunjiapp.cn" + xunji.READ_PATH, "XUNJI_TRAIN_KEY",
                                          {"schema_version": xunji.SCHEMA, "datestr": day, "include_full_data": True})
                    ingest(db, source, payload, {day: xunji.trains_in(payload["res"])}, day, day)
                except (SourceError, xunji.Failure, ValueError) as error:
                    with db:
                        log_run(db, source, day, day, "error", str(error))
                    failed_days.append(day)
            if failed_days:
                failed.append(source)
                status = "partial" if len(failed_days) < len(source_days) else "error"
                with db:
                    log_run(db, source, source_days[0], source_days[-1], status,
                            "失败日期：" + "、".join(failed_days))
            continue
        source_end = {"zepp": zepp_end, "zepp-workout": zepp_end,
                      "xunji-body": xunji_end or body_end, "xunji-food": xunji_end}.get(source) or end
        source_days = days if source_end == end else dates(start, source_end)
        last = source_days[-1]
        try:
            if source in ("zepp", "zepp-workout"):
                fetch_zepp(db, days[0], last, workouts=source == "zepp-workout")
            else:
                payload, parts = fetch_range(source, days[0], last, source_days)
                if not ingest(db, source, payload, parts, days[0], last, preserve_empty=True):
                    failed.append(source)
        except PartialSync:
            failed.append(source)
        except (SourceError, ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
            detail = str(error) if isinstance(error, SourceError) else f"导出格式或读取失败（{type(error).__name__}）"
            if detail.startswith("等待"):
                status = "pending"
            with db:
                log_run(db, source, days[0], last, status, detail)
            failed.append(source)
    if failed:
        print("需核对：" + "、".join(dict.fromkeys(failed)))
    return not failed


def status(db):
    for source in SOURCES:
        run = db.execute("""
            SELECT status,detail,completed_at FROM sync_runs
            WHERE user_id=? AND source=? ORDER BY id DESC LIMIT 1
        """, (USER, source)).fetchone()
        bounds = db.execute("""
            SELECT count(*),min(day),max(day) FROM observations WHERE user_id=? AND source=?
        """, (USER, source)).fetchone()
        count, start, end = bounds
        print(f"{source}  {count} 日" + (f"  {start} 至 {end}" if count else "")
              + (f"  最近任务 {run[0]} {run[1]}" if run else ""))
    count, empty = db.execute("""SELECT count(*),count(CASE WHEN days_returned=0 THEN 1 END)
        FROM zepp_backfill WHERE user_id=?""", (USER,)).fetchone()
    if count:
        print(f"Zepp 历史已查询 {count} 个分段，其中 {empty} 段没有返回记录。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sync_cmd = commands.add_parser("sync")
    sync_cmd.add_argument("--days", type=int, default=7)
    sync_cmd.add_argument("--end", type=date.fromisoformat)
    sync_cmd.add_argument("--include-today-zepp", action="store_true")
    sync_cmd.add_argument("--include-today-body", action="store_true")
    sync_cmd.add_argument("--include-today-xunji", action="store_true")
    commands.add_parser("sync-xunji", help="补拉近七个自然日的训记数据")
    commands.add_parser("status")
    commands.add_parser("sync-food-today", help="只同步今天的训记饮食")
    workout_sync = commands.add_parser("sync-zepp-workouts", help="只同步 Zepp 单次运动")
    workout_sync.add_argument("--since", required=True, type=date.fromisoformat)
    commands.add_parser("import-zepp").add_argument("path", type=Path)
    history = commands.add_parser("backfill-zepp")
    history.add_argument("--since", required=True, type=date.fromisoformat)
    history.add_argument("--end", type=date.fromisoformat)
    report = commands.add_parser("report")
    report.add_argument("--end", type=date.fromisoformat)
    report.add_argument("--send", action="store_true")
    daily = commands.add_parser("daily-report", help="生成并可发送每日身体简报")
    delivery = daily.add_mutually_exclusive_group()
    delivery.add_argument("--send", action="store_true")
    delivery.add_argument("--dry-run", action="store_true", help="核对微信投递参数，不发送")
    delivery.add_argument("--resend", action="store_true", help="重新推送今天的日报")
    commands.add_parser("retry-saved-report", help="通过本机网关重发已保存的报告")
    saved = commands.add_parser("send-saved-report", help="发送已保存的报告")
    saved.add_argument("kind", choices=("daily", "weekly"))
    args = parser.parse_args()
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    if STATE.stat().st_uid != os.getuid() or STATE.stat().st_mode & 0o077:
        raise SourceError("总账目录需由运行用户拥有，且权限为 700")
    with (STATE / "ledger.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if args.command == "retry-saved-report" else 0))
        except BlockingIOError:
            raise SourceError("总账任务正在运行，请稍后重试") from None
        with closing(sqlite3.connect(STATE / "ledger.sqlite3")) as db:
            db.executescript(SCHEMA)
            if args.command == "status":
                status(db)
            elif args.command == "sync-food-today":
                day = datetime.now(TZ).date().isoformat()
                try:
                    payload, parts = fetch_range("xunji-food", day, day, [day])
                    complete = ingest(db, "xunji-food", payload, parts, day, day, preserve_empty=True)
                except (SourceError, ValueError, OSError) as error:
                    detail = str(error) if isinstance(error, SourceError) else type(error).__name__
                    with db:
                        log_run(db, "xunji-food", day, day, "error", detail)
                    raise SourceError(f"今日饮食同步失败：{detail}") from None
                if not complete:
                    raise PartialSync("今日饮食未返回，已保留原数据")
                print(f"今日饮食同步成功，日期 {day}")
            elif args.command == "sync-zepp-workouts":
                today = datetime.now(TZ).date()
                if args.since > today:
                    parser.error("运动同步起点不可晚于今天")
                start, end = args.since.isoformat(), today.isoformat()
                try:
                    count = fetch_zepp(db, start, end, workouts=True)
                except SourceError as error:
                    with db:
                        log_run(db, "zepp-workout", start, end, "error", str(error))
                    raise
                print(f"Zepp 单次运动同步成功，共 {count} 场")
            elif args.command == "sync":
                today = datetime.now(TZ).date()
                end = args.end or today - timedelta(days=1)
                if not 1 <= args.days <= 31:
                    parser.error("--days 需要在 1 至 31 之间")
                if (args.include_today_zepp or args.include_today_body or args.include_today_xunji) and args.end:
                    parser.error("包含当天的选项和 --end 不能同时使用")
                if not sync(db, end - timedelta(days=args.days - 1), end,
                            zepp_end=today if args.include_today_zepp else None,
                            body_end=today if args.include_today_body else None,
                            xunji_end=today if args.include_today_xunji else None):
                    raise SourceError("部分来源同步失败")
            elif args.command == "sync-xunji":
                today = datetime.now(TZ).date()
                if not sync(db, today - timedelta(days=6), today, sources=XUNJI_SOURCES):
                    raise SourceError("部分训记数据同步失败")
                print("训记同步完成")
            elif args.command == "import-zepp":
                import_zepp(db, args.path)
            elif args.command == "backfill-zepp":
                today = datetime.now(TZ).date()
                end = args.end or today
                if not args.since <= end <= today:
                    parser.error("回溯日期须有效且不晚于今天")
                backfill_zepp(db, args.since, end)
            elif args.command == "daily-report":
                from daily import generate
                generate(db, datetime.now(TZ).date(), send=args.send, dry_run=args.dry_run, resend=args.resend)
            elif args.command == "retry-saved-report":
                retry_saved_report(db, sys.stdin.readline(16).strip(), datetime.now(TZ).date())
                print("报告推送完成")
            elif args.command == "send-saved-report":
                retry_saved_report(db, args.kind, datetime.now(TZ).date())
            else:
                from weekly import generate
                today = datetime.now(TZ).date()
                yesterday = today - timedelta(days=1)
                end = args.end or yesterday - timedelta(days=(yesterday.weekday() - 4) % 7)
                if end >= today:
                    parser.error("周报截止日须为已结束日期")
                generate(db, end, args.send)


if __name__ == "__main__":
    sys.modules["ledger"] = sys.modules[__name__]
    try:
        main()
    except (SourceError, OSError, ValueError, sqlite3.Error) as error:
        sys.exit(str(error))
