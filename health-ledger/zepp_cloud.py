"""Fetch Zepp band data on Linux, renewing the session once after HTTP 401."""
import argparse
from datetime import date, datetime, timedelta, timezone
from getpass import getpass
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

from huami_token.constants import HEADERS
from huami_token.zepp import ZeppSession
from loguru import logger
import requests
from zepp_export import ZeppAuthError, ZeppClient

ROOT = Path(__file__).resolve().parent / ".state"
SECRETS = Path.home() / ".codex/secrets/health-zepp.json"
TOKEN = ROOT / "zepp-token.json"
BASE = "https://api-mifit-us2.zepp.com"
TZ = ZoneInfo("Asia/Shanghai")


class WorkoutAuthError(Exception):
    pass


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def private(path):
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("unsafe credential mode")
    return json.loads(path.read_text())


def login(credentials):
    session = ZeppSession(credentials["email"], credentials["password"])
    session.login()
    return {"token": session.app_token, "user_id": str(session.user_id)}


def pull(token, start, end, *, allow_empty=False):
    client = ZeppClient(token=token["token"], user_id=token["user_id"], base_url=BASE)
    rows = client._fetch_band_data(start, end)
    if not isinstance(rows, list) or (not rows and not allow_empty):
        raise ValueError("empty Zepp export")
    return rows


def pull_workouts(token, start, end):
    # The history route returns every sport, including non-running workouts.
    headers = {**HEADERS.ZEPP_DEVICES.value, "apptoken": token["token"]}
    params = {"source": "run.mi.com", "userid": token["user_id"], "count": 100}
    first = datetime.fromisoformat(start).replace(tzinfo=TZ).timestamp()
    workouts = {}
    previous_cursor = None
    for _ in range(20):
        response = requests.get(f"{BASE}/v1/sport/run/history.json", params=params,
                                headers={**headers, "x-request-id": str(uuid4())}, allow_redirects=False)
        if response.status_code in (401, 403):
            raise WorkoutAuthError()
        if response.status_code != 200:
            raise ValueError("Zepp workout HTTP error")
        payload = response.json()
        if payload.get("code") != 1:
            raise ValueError("Zepp workout API error")
        data = payload["data"]
        rows = data["summary"]
        if isinstance(rows, str):
            rows = json.loads(rows)
        if isinstance(rows, dict):
            rows = rows["data"]
        if not isinstance(rows, list):
            raise ValueError("Zepp workout format changed")
        for row in rows:
            track = str(row["trackid"])
            started, ended = int(track), int(row["end_time"])
            if not 0 < ended - started <= 7 * 86400:
                raise ValueError("invalid Zepp workout time")
            day = datetime.fromtimestamp(started, TZ).date().isoformat()
            if start <= day <= end:
                workouts.setdefault(track, {
                    "date": day, "id": track, "sport_type": int(row["type"]),
                    "title": str(row.get("sport_title") or "").strip(),
                    "start": started, "end": ended,
                    "distance_m": float(row.get("dis") or 0),
                    "avg_heart_rate": float(row.get("avg_heart_rate") or 0),
                })
        cursor = int(data["next"])
        if cursor <= 0 or cursor < first:
            return sorted(workouts.values(), key=lambda row: row["start"])
        if not rows or (previous_cursor is not None and cursor >= previous_cursor):
            raise ValueError("Zepp workout pagination did not advance")
        params["trackid"] = cursor
        previous_cursor = cursor
    raise ValueError("Zepp workout pagination exceeded limit")


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--workouts", action="store_true")
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("start", nargs="?", type=date.fromisoformat)
    parser.add_argument("end", nargs="?", type=date.fromisoformat)
    args = parser.parse_args()
    os.umask(0o077)
    logger.remove()
    default_request = requests.sessions.Session.request

    def with_timeout(session, method, url, **kwargs):
        kwargs.setdefault("timeout", (10, 25))
        return default_request(session, method, url, **kwargs)

    requests.sessions.Session.request = with_timeout
    if args.setup:
        if not sys.stdin.isatty():
            parser.error("Zepp 授权需要交互式终端")
        email = input("Zepp 登录邮箱: ").strip()
        password = getpass("Zepp 密码（输入不会显示）: ")
        if not email or not password:
            parser.error("请填写邮箱和密码")
        credentials = {"email": email, "password": password}
        try:
            token = login(credentials)
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            pull(token, (today - timedelta(days=2)).isoformat(), today.isoformat())
        except Exception as error:
            sys.exit(f"Zepp 授权或当天只读验证失败（{type(error).__name__}），请核对账号和手表同步状态。")
        save(SECRETS, credentials)
        save(TOKEN, token)
        print("Zepp 云端授权和只读验证通过。")
        return
    if not all((args.output, args.start, args.end)) or args.start > args.end:
        parser.error("需要输出文件、开始日期和结束日期")
    if not SECRETS.is_file():
        sys.exit(78)
    stage = "授权"
    try:
        credentials = private(SECRETS)
        token = private(TOKEN) if TOKEN.is_file() else login(credentials)
        stage = "拉取"
        fetch = pull_workouts if args.workouts else lambda *dates: pull(*dates, allow_empty=True)
        start, end = args.start.isoformat(), args.end.isoformat()
        try:
            rows = fetch(token, start, end)
        except (ZeppAuthError, WorkoutAuthError):
            stage = "续授权"
            token = login(credentials)
            stage = "续授权后拉取"
            rows = fetch(token, start, end)
        save(TOKEN, token)
        save(args.output, {"fetched_at": datetime.now(timezone.utc).isoformat(), "timezone": "Asia/Shanghai",
                           "source": "zepp-cloud", "requested_from": args.start.isoformat(),
                           "requested_to": args.end.isoformat(), "records": rows})
    except Exception as error:
        print(f"Zepp {stage}失败（{type(error).__name__}）", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()
