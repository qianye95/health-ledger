"""Single-user CLI for reading and recording workouts in 训记."""
import argparse
from contextlib import closing
from datetime import date
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
KEY_FILE = Path.home() / ".codex/secrets/xunji.env"
BASE_URL = "https://trains.xunjiapp.cn"
READ_PATH = "/api_trains_for_llm_v2"
WRITE_PATH = "/api_upsert_trains_for_llm_v2"
SCHEMA = "train_open_api_v2"
FULL_READ_INTERVAL = 30
RPE_VALUES = {6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 10}
CARDIO_PRESETS = {
    "general": "有氧训练", "running": "跑步", "walking": "步行",
    "cycling": "骑行", "swimming": "游泳", "jumpRope": "跳绳", "hiit": "HIIT",
}


class Failure(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def key_from_file():
    try:
        with KEY_FILE.open(encoding="utf-8") as file:
            info = os.fstat(file.fileno())
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise Failure(f"请将 {KEY_FILE} 设为当前用户所有且权限为 600。")
            for line in file:
                name, _, value = line.strip().partition("=")
                if name == "XUNJI_TRAIN_KEY" and re.fullmatch(r"[!-~]+", value):
                    return value
    except FileNotFoundError:
        pass
    raise Failure(f"请在 {KEY_FILE} 配置 XUNJI_TRAIN_KEY。")


def api(path, fields):
    body = json.dumps({"schema_version": SCHEMA, **fields}).encode()
    request = Request(BASE_URL + path, data=body, headers={
        "Authorization": "Bearer " + key_from_file(),
        "Content-Type": "application/json",
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            response_body = response.read()
            encoding = response.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip" or response_body.startswith(b"\x1f\x8b"):
                response_body = gzip.decompress(response_body)
            result = json.loads(response_body)
    except HTTPError as error:
        raise Failure(f"训记请求返回 HTTP {error.code}。") from None
    except (URLError, TimeoutError, ValueError, gzip.BadGzipFile, EOFError) as error:
        raise Failure(f"训记请求未取得有效响应（{type(error).__name__}）。") from None
    if not isinstance(result, dict):
        raise Failure("训记返回格式无法识别。")
    if result.get("success") is False or (
        result.get("success") is not True and result.get("res") is None
    ):
        details = json.dumps(result, ensure_ascii=False).lower()
        for marker, message in (
            ("apikey invalid", "训记 Key 无效，请在 App 更新。"),
            ("apikey missing", "训记未识别到 Key，请在 App 核对。"),
            ("仅vip可用", "该接口需要训记 VIP 权限。"),
            ("too frequent", "训记限频，请等待后再试。"),
        ):
            if marker in details:
                raise Failure(message)
        raise Failure("训记未接受请求，请核对 App 内接口说明和账号权限。")
    return result.get("res")


def checked_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise Failure("日期须为 YYYY-MM-DD。")
    date.fromisoformat(value)
    return value


def trains_in(result):
    if isinstance(result, dict):
        result = result.get("trains")
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise Failure("训记响应缺少可识别的训练列表。")
    return result


def read_day(db, day, *, fresh=False):
    checked_date(day)
    cached = db.execute("SELECT * FROM reads WHERE day = ?", (day,)).fetchone()
    if cached and time.time() - cached["requested_at"] < FULL_READ_INTERVAL:
        if not fresh and cached["payload"] is not None:
            return json.loads(cached["payload"])
        remaining = math.ceil(FULL_READ_INTERVAL - (time.time() - cached["requested_at"]))
        raise Failure(f"同日读取限频，请约 {remaining} 秒后再回查。")
    result = trains_in(api(READ_PATH, {"datestr": day, "include_full_data": True}))
    db.execute("INSERT OR REPLACE INTO reads VALUES (?, ?, ?)",
               (day, time.time(), json.dumps(result, ensure_ascii=False)))
    db.commit()
    return result


def fields(obj, allowed):
    if not isinstance(obj, dict) or obj.keys() - set(allowed.split()):
        raise Failure(f"字段不符合草稿格式。可用字段为 {allowed}。")


def label(value):
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise Failure("标题、动作名和备注须为单行文本。")
    return value.strip()


def checked_rpe(value):
    if type(value) not in (int, float) or value not in RPE_VALUES:
        raise Failure("RPE 须为 6 到 10 之间、步长为 0.5 的数字。")
    return int(value) if value == int(value) else value


def reject_cardio_placeholder(movement):
    name = movement["name"]
    if name in {"快走", "步行", "跑步机步行"} or name.endswith("_有氧训练"):
        raise Failure(f"「{name}」请用原生有氧草稿记录。")


def cardio_movement(cardio):
    fields(cardio, "preset duration_s distance_km pace kcal avg_bpm")
    preset, duration = cardio.get("preset"), cardio.get("duration_s")
    if not isinstance(preset, str) or preset not in CARDIO_PRESETS:
        raise Failure("有氧类型无效。")
    if type(duration) is not int or duration <= 0:
        raise Failure("有氧时长须为正整数秒。")
    metrics = {}
    for source, target in (("distance_km", "distance"), ("kcal", "kcal"), ("avg_bpm", "bpm")):
        if source in cardio:
            value = cardio[source]
            if type(value) not in (int, float) or not 0 < value < math.inf:
                raise Failure(f"{source} 须为正数。")
            metrics[target] = str(value)
    if "pace" in cardio:
        pace = cardio["pace"]
        if not isinstance(pace, str) or not re.fullmatch(r"[0-9]+:[0-5][0-9]", pace):
            raise Failure("配速须为 分:秒，例如 14:17。")
        metrics["pace"] = pace
    return {"name": CARDIO_PRESETS[preset], "cardio": True, "recordPreset": preset,
            "duration_s": duration, "metrics": metrics}


def draft_from(raw):
    fields(raw, "date title movements cardio symptoms uncertainties")
    day = checked_date(raw.get("date"))
    title = label(raw.get("title"))
    if "休息" in title:
        raise Failure("请使用实际训练标题。")
    notes = {}
    for name in ("symptoms", "uncertainties"):
        values = raw.get(name, [])
        if not isinstance(values, list):
            raise Failure(f"{name} 须为文本列表。")
        notes[name] = [label(item) for item in values]
    if "cardio" in raw:
        if "movements" in raw:
            raise Failure("原生有氧草稿只能提交一个有氧项目。")
        return {"datestr": day, "title": title, "movements": [cardio_movement(raw["cardio"])], **notes}
    catalog = set(json.loads((ROOT / "movements.json").read_text(encoding="utf-8")))
    movements = raw.get("movements")
    if not isinstance(movements, list) or not 1 <= len(movements) <= 15:
        raise Failure("每条训练须有 1–15 个动作。")
    normalized = []
    for movement in movements:
        fields(movement, "name sets")
        name = label(movement.get("name"))
        if name not in catalog:
            raise Failure(f"动作「{name}」未匹配训记目录。")
        reject_cardio_placeholder({"name": name})
        sets = movement.get("sets")
        if not isinstance(sets, list) or not 1 <= len(sets) <= 20:
            raise Failure("每个动作须有 1–20 组。")
        groups = []
        for group in sets:
            fields(group, "weight unit reps done rpe")
            weight, reps = group.get("weight"), group.get("reps")
            unit, done = group.get("unit"), group.get("done", True)
            if type(weight) not in (int, float) or not 0 <= weight < math.inf:
                raise Failure("重量须为非负数字。")
            if type(reps) is not int or reps < 0 or (done and reps == 0):
                raise Failure("次数须为非负整数，已完成组须至少一次。")
            if unit not in ("kg", "lb") or type(done) is not bool:
                raise Failure("单位须为 kg 或 lb，done 须为布尔值。")
            entry = {"weight": str(weight), "unit": unit, "reps": str(reps), "done": done}
            if "rpe" in group:
                rpe = group["rpe"]
                entry["rpe"] = checked_rpe(rpe)
            groups.append(entry)
        normalized.append({"name": name, "sets": groups})
    return {"datestr": day, "title": title, "movements": normalized, **notes}


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def save_preview(db, raw):
    draft = draft_from(raw)
    payload = json.dumps(draft, ensure_ascii=False, sort_keys=True)
    preview_id = hashlib.sha256(payload.encode()).hexdigest()
    db.execute("INSERT OR IGNORE INTO drafts (id, payload) VALUES (?, ?)",
               (preview_id, payload))
    db.commit()
    return preview_id


def find_draft(db, preview_id):
    row = db.execute("SELECT * FROM drafts WHERE id = ?", (preview_id,)).fetchone()
    if row is None:
        raise Failure("找不到该预览，请先生成草稿。")
    return row


def verify(db, row):
    if row["status"] == "verified":
        return
    if row["status"] != "sent":
        raise Failure("当前草稿尚无可回查的训记记录 ID。")
    day = json.loads(row["payload"])["datestr"]
    result = read_day(db, day, fresh=True)
    if not any(str(item.get("localid")) == row["external_id"] for item in result):
        raise Failure("训记已接受写入，但当日列表尚未找到对应 ID，请稍后再次回查。")
    db.execute("UPDATE drafts SET status = 'verified' WHERE id = ?", (row["id"],))
    db.commit()


def confirm(preview_id):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Failure("请在本机终端打开 commit 并手动确认。")
    phrase = f"确认写入训记 {preview_id[-8:]}"
    return input(f"确认后将新增一条正式训练记录。请输入「{phrase}」\n> ") == phrase


def commit(db, row):
    if row["status"] in ("sent", "verified"):
        verify(db, row)
        return
    if row["status"] == "accepted":
        raise Failure("训记已报告写入成功但未返回可识别的 ID。请在 App 核对。")
    draft = json.loads(row["payload"])
    show(draft)
    if draft["uncertainties"]:
        raise Failure("请核实草稿里的不确定项，再生成新预览。")
    include_full_data = False
    for movement in draft["movements"]:
        if movement.get("cardio") is True:
            continue
        reject_cardio_placeholder(movement)
        for group in movement["sets"]:
            if "rpe" in group:
                group["rpe"] = str(checked_rpe(group["rpe"]))
                include_full_data = True
    payload = {key: draft[key] for key in ("datestr", "title", "movements")}
    if not confirm(row["id"]):
        raise Failure("确认短语不匹配。")
    result = api(WRITE_PATH, {
        "client_request_id": row["id"],
        "dry_run": False,
        "include_full_data": include_full_data,
        "res": [payload],
    })
    # Once the API reports success, never submit this draft again.
    try:
        records = trains_in(result)
    except Failure:
        records = []
    external_id = records[0].get("localid") if len(records) == 1 else None
    if type(external_id) not in (str, int) or not str(external_id).strip():
        external_id = None
    status = "sent" if external_id is not None else "accepted"
    db.execute("UPDATE drafts SET status = ?, external_id = ? WHERE id = ?",
               (status, str(external_id) if external_id is not None else None, row["id"]))
    db.execute("UPDATE reads SET payload = NULL WHERE day = ?", (draft["datestr"],))
    db.commit()
    if external_id is None:
        raise Failure("训记已接受写入但未返回可识别的 ID。请在 App 核对。")
    verify(db, find_draft(db, row["id"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("read", help="读取指定日期").add_argument("date")
    commands.add_parser("preview", help="从 JSON 生成草稿").add_argument("file")
    for name in ("status", "commit", "verify"):
        commands.add_parser(name).add_argument("preview_id")
    args = parser.parse_args()
    os.umask(0o077)
    state = Path(os.environ.get("XUNJI_STATE_DIR", ROOT / ".state"))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_uid != os.getuid() or stat.S_IMODE(state.stat().st_mode) != 0o700:
        raise Failure("状态目录须归当前用户所有且权限为 700。")
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with closing(sqlite3.connect(state / "training.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            db.executescript("""
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'preview', external_id TEXT
                );
                CREATE TABLE IF NOT EXISTS reads (
                    day TEXT PRIMARY KEY, requested_at REAL NOT NULL, payload TEXT
                );
            """)
            if args.command == "read":
                show(read_day(db, args.date))
                return
            if args.command == "preview":
                raw = json.load(sys.stdin) if args.file == "-" else json.loads(
                    Path(args.file).read_text(encoding="utf-8")
                )
                preview_id = save_preview(db, raw)
            else:
                preview_id = args.preview_id
                row = find_draft(db, preview_id)
                if args.command == "commit":
                    commit(db, row)
                elif args.command == "verify":
                    verify(db, row)
            row = dict(find_draft(db, preview_id))
            show({"preview_id": row["id"], "status": row["status"],
                  "external_id": row["external_id"], "draft": json.loads(row["payload"])})


if __name__ == "__main__":
    try:
        main()
    except (Failure, OSError, ValueError, EOFError, sqlite3.Error) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
