"""Private dashboard for the cloud health ledger."""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import socket
import subprocess
from statistics import mean
from threading import Lock
from time import monotonic
from urllib.parse import parse_qs, urlsplit

from ledger import HERE, SOURCES, STATE, TZ, USER, dates, report_file
from weekly import age_on, aggregate, heart_minutes, is_cardio, lift_changes, load_period, number, sleep_minutes, timestamp

BASELINE = date(2026, 10, 1)
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}
WINDOWS = {"7": 7, "30": 30, "60": 60, "all": None}
# Zepp cloud workout codes. Keep ambiguous codes visible as other activities.
ZEPP_SPORTS = {
    1: ("run", "户外跑步"), 6: ("walk", "健走"), 7: ("run", "越野跑"),
    9: ("bike", "户外骑行"), 10: ("bike", "室内骑行"), 11: ("run", "跑步机"),
    12: ("elliptical", "椭圆机"), 14: ("swim", "泳池游泳"), 15: ("swim", "公开水域游泳"),
    21: ("jump", "跳绳"), 22: ("hike", "徒步"), 23: ("row", "划船机"),
    47: ("bike", "山地骑行"), 52: ("strength", "力量训练"), 108: ("stairs", "爬楼"),
    224: ("hike", "徒步"), 225: ("walk", "负重健走"),
}
CARDIO_TERMS = {
    "stairs": ("爬楼", "楼梯", "登阶"), "hike": ("徒步", "爬坡", "登山"),
    "walk": ("走", "步行"), "run": ("跑",), "bike": ("骑", "单车"),
    "swim": ("游泳",), "elliptical": ("椭圆",), "row": ("划船",), "jump": ("跳绳",),
}
MEAL_NAMES = {"morning": "早餐", "noon": "午餐", "night": "晚餐", "snack": "加餐"}
MEAL_TYPES = {"breakfast": "morning", "lunch": "noon", "dinner": "night", "snacks": "snack",
              "extra": "snack", "morning-added": "snack", "noon-added": "snack", "night-added": "snack"}
# Reference values in grams of fiber per 100 g, matched to specific Xunji foods.
FIBER_REFERENCES = {
    "/shiwu/fdb003a5": (88.1, "Ingredion 菊粉规格表",
                       "https://www.ingredion.com/content/dam/ingredion/technical-documents/na/"
                       "TIC%20Pretested%20Inulin%20LV%20110%20Powder%20-%2038910903%20-%20TDS.pdf"),
    "/shiwu/fd8a67de": (2.4, "USDA 熟甜玉米营养数据（FDC 169999）",
                       "https://whatyoueat.io/foods/169999"),
}


def average(values):
    return round(mean(values), 2) if values else None


def profile_versions():
    path = STATE / "profile.json"
    return json.loads(path.read_text()) if path.is_file() else []


def profile_on(day, versions):
    plan = max((p for p in versions if p["effective_from"] <= day),
               key=lambda p: p["effective_from"], default={})
    fields = ("phase", "height_cm", "target_weight_kg", "estimated_body_fat_pct",
              "start_weight_kg", "start_waist_cm", "target_waist_cm",
              "strength_sessions_per_week", "cardio_sessions_per_week", "neat_minutes_per_day")
    public = {k: plan.get(k) for k in fields}
    public["age"] = age_on(plan["birth_date"], day) if plan.get("birth_date") else None
    public["daily_intake_targets"] = plan.get("daily_intake_targets") or {}
    return public


def food_detail(food):
    amount = number(food.get("amount"))
    unit = food.get("unit") or ""
    nutrition = food.get("ntr") or {}
    grams = None
    if amount is not None and amount >= 0:
        if unit in ("g", "克"):
            grams = amount
        elif unit == "kg":
            grams = amount * 1000
        else:
            conversion = next((item for item in (nutrition.get("foodUnit") or [])
                               if item.get("unit") == unit), {})
            weight = number(conversion.get("gram"))
            count = number(conversion.get("count", 1))
            if weight is not None and weight > 0 and count is not None and count > 0:
                grams = amount * weight / count
    def per_portion(value):
        return value * grams / 100 if value is not None and value >= 0 and grams is not None else None

    fiber = number(nutrition.get("fiber"))
    reference = FIBER_REFERENCES.get(food.get("uniquekey")) if fiber is None and grams is not None else None
    if reference:
        fiber, title, url = reference
    return {
        "name": food.get("name") or "未命名食物", "amount": amount, "unit": unit,
        "carbs_g": per_portion(number(nutrition.get("carb"))),
        "protein_g": per_portion(number(nutrition.get("protein"))),
        "fat_g": per_portion(number(nutrition.get("fat"))),
        "fiber_g": per_portion(fiber),
        "fiber_reference": {"title": title, "url": url} if reference else None,
    }


def intake_metrics(records, targets):
    details = [r.get("foods") or {} for r in records]
    logged = any(detail.get("records") for detail in details) or any(
        (number(r.get("item_count")) or 0) > 0 for r in records
    )
    totals = (records[0].get("totals") or {}) if logged and len(records) == 1 else {}
    limits = (details[0].get("limits") or {}) if len(details) == 1 else {}
    macros = {key: number(limits.get(key)) for key in ("carb", "protein", "fat")}
    energy = number(limits.get("cal"))
    estimated_energy = energy is None and all(value is not None and value > 0 for value in macros.values())
    if estimated_energy:
        energy = 4 * (macros["carb"] + macros["protein"]) + 9 * macros["fat"]
    source_targets = {
        "calories_kcal": energy, "carbs_g": macros["carb"], "protein_g": macros["protein"], "fat_g": macros["fat"],
    }
    metrics = {}
    for key, field in (("calories_kcal", "totalCal"), ("carbs_g", "totalCarb"),
                       ("protein_g", "totalProtein"), ("fat_g", "totalFat")):
        actual, goal = number(totals.get(field)), source_targets[key]
        source_goal = goal is not None and goal > 0
        if not source_goal:
            goal = number(targets.get(key))
        metrics[key] = {
            "actual": actual if actual is not None and actual >= 0 else None,
            "goal": goal if goal is not None and goal > 0 else None,
            "estimated_goal": source_goal and key == "calories_kcal" and estimated_energy,
        }
    return logged, metrics


def today_intake(db, day, targets):
    row = db.execute("""SELECT payload,observed_at FROM observations
        WHERE user_id=? AND source='xunji-food' AND day=?""", (USER, day)).fetchone()
    records = json.loads(row[0]) if row else []
    logged, metrics = intake_metrics(records, targets)
    meals = {key: [] for key in MEAL_NAMES}
    for record in records:
        for food in (record.get("foods") or {}).get("records") or []:
            kind = str(food.get("meal_type") or "").lower()
            kind = MEAL_TYPES.get(kind, kind)
            meals.setdefault(kind if kind in MEAL_NAMES else "other", []).append(food_detail(food))
    return {
        "day": day, "observed_at": row[1] if row else None, "logged": logged, "metrics": metrics,
        "meals": [{"name": MEAL_NAMES.get(key, "其他"), "foods": items,
                   "carbs_g": sum(food["carbs_g"] for food in items)
                   if items and all(food["carbs_g"] is not None for food in items) else None}
                  for key, items in meals.items()],
    }


def intake_goal_summary(intake, food):
    if any(metric["goal"] is not None for metric in intake["metrics"].values()):
        return {"day": intake["day"], "metrics": intake["metrics"]}
    for day in sorted(food, reverse=True):
        _, metrics = intake_metrics(food[day], {})
        if any(metric["goal"] is not None for metric in metrics.values()):
            return {"day": day, "metrics": metrics}
    return {"day": None, "metrics": intake["metrics"]}


def points_by_day(rows, kind):
    result = []
    for day, items in sorted(rows.items()):
        values = [number(item.get("value")) for item in items if item.get("type") == kind]
        values = [v for v in values if v is not None and v > 0]
        if values:
            result.append({"day": day, "value": round(mean(values), 2), "measurements": len(values)})
    return result


def sensor_points(rows, metric):
    result = []
    for day, row in sorted(rows.items()):
        summary = row.get("summary") or {}
        if metric == "sleep":
            value = sleep_minutes(summary, day)
        elif metric == "resting":
            value = number((summary.get("slp") or {}).get("rhr"))
        else:
            value = number((summary.get("stp") or {}).get("ttl"))
        if value is not None and (value > 0 or (metric == "steps" and value == 0)):
            result.append({"day": day, "value": value})
    return result


def sources(db, today):
    cover = {source: (first, last, count, observed)
             for source, first, last, count, observed in db.execute("""
        SELECT source,min(day),max(day),count(*),max(observed_at)
        FROM observations WHERE user_id=? AND day BETWEEN ? AND ? GROUP BY source
    """, (USER, BASELINE.isoformat(), today))}
    runs = {}
    for source, status, detail, completed in db.execute("""
        SELECT source,status,detail,completed_at FROM sync_runs
        WHERE user_id=? AND id IN (
            SELECT max(id) FROM sync_runs WHERE user_id=? GROUP BY source
        )
    """, (USER, USER)):
        runs[source] = {"status": status, "detail": detail, "checked_at": completed}
    return [{"source": source, "first": cover.get(source, (None,)*4)[0],
             "last": cover.get(source, (None,)*4)[1], "days": cover.get(source, (None,)*4)[2],
             "observed_at": cover.get(source, (None,)*4)[3], "run": runs.get(source)}
            for source in SOURCES]


def report_delivery(db, now):
    today = now.date()
    daily = db.execute("SELECT sent_at FROM daily_deliveries WHERE user_id=? AND day=?",
                       (USER, today.isoformat())).fetchone()
    saturday = today - timedelta(days=(today.weekday() - 5) % 7)
    friday = (saturday - timedelta(days=1)).isoformat()
    weekly = db.execute("SELECT sent_at FROM deliveries WHERE user_id=? AND week_end=?",
                        (USER, friday)).fetchone()

    reports = []
    for kind, schedule, scheduled_day, period, receipt in (
        ("daily", "每天 11:00", today, today.isoformat(), daily),
        ("weekly", "每周六 11:00", saturday, friday, weekly),
    ):
        status = "off"
        scheduled_today = scheduled_day == today
        saved = report_file(kind, period).is_file()
        if receipt and (scheduled_today or datetime.fromisoformat(receipt[0]).astimezone(TZ).date() == today):
            status = "sent"
        elif not receipt and (scheduled_today or saved):
            status = "waiting" if scheduled_today and now.hour < 11 else "unconfirmed"
            try:
                result = subprocess.run(
                    ["/usr/bin/systemctl", "show", f"health-ledger-{kind}.service",
                     "-p", "ActiveState", "-p", "Result", "-p", "StateChangeTimestamp"],
                    capture_output=True, text=True, check=True, timeout=1,
                    env={"PATH": "/usr/bin:/bin", "TZ": "Asia/Shanghai", "LC_ALL": "C"},
                )
                unit = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
                if unit.get("ActiveState") == "activating":
                    status = "running"
                elif (scheduled_day.isoformat() in unit.get("StateChangeTimestamp", "")
                      and unit.get("Result") not in (None, "", "success")):
                    status = "failed"
            except (OSError, subprocess.SubprocessError):
                pass
        reports.append({"kind": kind, "schedule": schedule, "status": status,
                        "sent_at": receipt[0] if status == "sent" else None,
                        "retryable": saved and not receipt and status in ("failed", "unconfirmed")})
    return reports


def assess(current, previous, plan, workouts):
    guidance = []
    strength = sum(event["kind"] == "strength" for event in workouts)
    goal = plan.get("strength_sessions_per_week")
    if goal and strength >= goal:
        guidance.append({"title": "力量频次已达标", "text": f"近 7 个完整日记录了 {strength} 场力量训练，达到每周 {goal} 场的计划。可继续沿用目前的训练频次。"})
    elif goal and current["coverage"]["xunji-training"] == 7:
        guidance.append({"title": "核对力量训练安排", "text": f"近 7 个完整日记录 {strength} 场力量训练，计划 {goal} 场。先核对是否有漏记，再安排下一次训练。"})
    cardio_goal = plan.get("cardio_sessions_per_week")
    cardio = sum(event["kind"] == "cardio" for event in workouts)
    if cardio_goal and cardio < cardio_goal:
        guidance.append({"title": "核对专门有氧", "text": f"近 7 个完整日同步了 {cardio} 场有氧，计划每周 {cardio_goal} 场。可核对是否有尚未同步的训练。"})
    sleep, prior = current["sleep_minutes"], previous["sleep_minutes"]
    if min(len(sleep), len(prior)) >= 4:
        delta = round(mean(sleep.values()) - mean(prior.values()))
        if delta <= -30:
            guidance.append({"title": "本周睡眠均值减少", "text": f"近 7 个完整日的平均睡眠比前 7 日少 {abs(delta)} 分钟。下次训练留意主观恢复和动作表现，再决定是否加量。"})
        elif delta >= 30:
            guidance.append({"title": "本周睡眠均值增加", "text": f"近 7 个完整日的平均睡眠比前 7 日多 {delta} 分钟。继续观察下一周能否维持。"})
    if len(guidance) < 3 and current["food_logged_days"] < 4:
        guidance.append({"title": "饮食记录覆盖有限", "text": f"近 7 个完整日只有 {current['food_logged_days']} 天有饮食记录。需要更完整的记录，才能判断整周摄入有没有变化。"})
    return guidance[:3]


def overlaps(first, second):
    a, b, c, d = first["start"], first["end"], second["start"], second["end"]
    return (None not in (a, b, c, d) and a < b and c < d
            and min(b, d) - max(a, c) >= 0.6 * max(b - a, d - c))


def merged_workouts(data, history):
    xunji = [
        {"day": row["date"], "kind": "strength", "sport": "strength", "title": row["title"],
         "start": row["start"], "end": row["end"], "source": "训记"}
        for row in aggregate(data, history, {})["sessions"]
    ]
    zepp = []
    for day in history:
        for row in data["xunji-training"].get(day, []):
            if not is_cardio(row):
                continue
            names = "、".join(move["name"] for move in row["movements"] if move.get("name"))
            title = row.get("title") or names or "有氧训练"
            sport = next((key for key, words in CARDIO_TERMS.items()
                          if any(word in f"{title} {names}" for word in words)), None)
            xunji.append({"day": day, "kind": "cardio", "sport": sport, "title": title,
                          "start": timestamp(row.get("start")), "end": timestamp(row.get("end")), "source": "训记"})
        for row in data["zepp-workout"].get(day, []):
            code = row["sport_type"]
            sport, title = ZEPP_SPORTS.get(code, (None, f"Zepp 运动（类型 {code}）"))
            kind = "strength" if sport == "strength" else "cardio" if sport else "other"
            zepp.append({"day": day, "kind": kind, "sport": sport, "title": row["title"] or title,
                         "start": row["start"], "end": row["end"], "source": "Zepp",
                         "distance_m": row["distance_m"], "avg_heart_rate": row["avg_heart_rate"]})
    xunji_matches, zepp_matches = defaultdict(list), defaultdict(list)
    for i, first in enumerate(xunji):
        for j, second in enumerate(zepp):
            if first["sport"] and first["sport"] == second["sport"] and overlaps(first, second):
                xunji_matches[i].append(j)
                zepp_matches[j].append(i)
    matched = set()
    for i, candidates in xunji_matches.items():
        if len(candidates) == 1 and len(zepp_matches[candidates[0]]) == 1:
            j = candidates[0]
            watch = zepp[j]
            xunji[i].update(source="训记 + Zepp", distance_m=watch["distance_m"], avg_heart_rate=watch["avg_heart_rate"])
            matched.add(j)
    records = xunji + [event for j, event in enumerate(zepp) if j not in matched]
    return sorted(records, key=lambda event: (event["day"], event["start"] or 0))


def month_bounds(today):
    first = today.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    return first, next_month - timedelta(days=1)


def month_grid(first, last, month_days):
    return {"month": first.isoformat()[:7],
            "days": [None] * first.weekday() + month_days + [None] * (6 - last.weekday())}


def intake_calendar(food, today, versions):
    first, last = month_bounds(today)
    month_days = []
    for day in dates(first, last):
        logged, metrics = intake_metrics(food.get(day, []), profile_on(day, versions)["daily_intake_targets"])
        month_days.append({"day": day, "logged": logged, **metrics["calories_kcal"]})
    return month_grid(first, last, month_days)


def training_calendar(data, today, workouts):
    first, last = month_bounds(today)
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    training = data["xunji-training"]
    events = defaultdict(list)
    for event in workouts:
        events[event["day"]].append(event)
    totals = {"strength": 0, "cardio": 0}
    for day in dates(monday, today):
        for event in events[day]:
            if event["kind"] in totals:
                totals[event["kind"]] += 1
    month_days = [{"day": day, "synced": day in training and day in data["zepp-workout"],
                   "events": events[day]} for day in dates(first, last)]
    return {
        **month_grid(first, last, month_days),
        "week_start": monday.isoformat(), "week_end": sunday.isoformat(), "week_totals": totals,
    }


def dashboard(db, view, today, custom_dates=None):
    today = max(today, BASELINE)
    last_complete = today - timedelta(days=1)
    period_start = max(BASELINE, last_complete - timedelta(days=6))
    previous_start = max(BASELINE, last_complete - timedelta(days=13))
    today_str = today.isoformat()
    all_data = load_period(db, BASELINE.isoformat(), today_str)
    versions = profile_versions()
    plan = profile_on(today_str, versions)
    points = heart_minutes({d: row for d, row in all_data["zepp"].items() if d >= previous_start.isoformat()})
    current = aggregate(all_data, dates(period_start, last_complete), points)
    previous = aggregate(all_data, dates(previous_start, period_start - timedelta(days=1)), points)
    workout_start = min(period_start, today.replace(day=1), today - timedelta(days=today.weekday()))
    workouts = merged_workouts(all_data, dates(workout_start, today))

    measures = {
        "weight": points_by_day(all_data["xunji-body"], "weight"),
        "waist": points_by_day(all_data["xunji-body"], "weist"),
        "sleep": sensor_points(all_data["zepp"], "sleep"),
        "resting": sensor_points(all_data["zepp"], "resting"),
        "steps": sensor_points(all_data["zepp"], "steps"),
    }
    if custom_dates is not None:
        start, end = custom_dates
    else:
        days = WINDOWS[view]
        start = max(BASELINE, today - timedelta(days=days - 1)) if days else BASELINE
        end = today
    first, last = start.isoformat(), end.isoformat()
    weight = measures["weight"]
    weight_now = weight[-1] if weight else None
    weight_then = weight[0] if weight else None
    waist = measures["waist"]
    recent_sleep = current["sleep_minutes"]
    weight_by_day = {p["day"]: p["value"] for p in weight}
    trend = []
    for day in dates(start, end):
        point_day = date.fromisoformat(day)
        recent = dates(max(BASELINE, point_day - timedelta(days=6)), point_day)
        values = [weight_by_day[d] for d in recent if d in weight_by_day]
        if len(values) >= 4 and day in weight_by_day:
            trend.append({"day": day, "value": average(values), "measurements": len(values)})
    sessions = [{"date": row["date"], "title": row["title"], "heart": row["heart"],
                 "working_sets": sum(len(groups) for groups in row["lifts"].values())}
                for row in reversed(current["sessions"])]
    comparison = []
    seen = set()
    for name, reps, before, after in lift_changes(current, previous):
        if name not in seen:
            comparison.append({"name": name, "reps": reps, "previous": before, "current": after})
            seen.add(name)
    comparable_sleep = average(list(previous["sleep_minutes"].values())) if min(len(recent_sleep), len(previous["sleep_minutes"])) >= 4 else None
    intake = today_intake(db, today_str, plan["daily_intake_targets"])
    return {
        "baseline": BASELINE.isoformat(), "start": first, "end": last, "today": today_str, "plan": plan,
        "training_calendar": training_calendar(all_data, today, workouts),
        "intake": intake,
        "intake_goal_summary": intake_goal_summary(intake, all_data["xunji-food"]),
        "intake_calendar": intake_calendar(all_data["xunji-food"], today, versions),
        "current": {"weight": weight_now, "baseline_weight": weight_then, "waist": waist[-1] if waist else None,
                    "sleep_avg": average(list(recent_sleep.values())), "sleep_days": len(recent_sleep),
                    "prior_sleep_avg": comparable_sleep,
                    "resting_avg": average(list(current["resting_heart_rate"].values())),
                    "resting_days": len(current["resting_heart_rate"]),
                    "steps_avg": average(list(current["steps"].values())), "steps_days": len(current["steps"])},
        "plan_period": {"start": period_start.isoformat(), "end": last_complete.isoformat(),
                        "strength": len(sessions), "cardio": current["cardio_sessions"],
                        "training_days": current["coverage"]["xunji-training"],
                        "food_days": current["food_logged_days"], "zepp_days": current["coverage"]["zepp"]},
        "series": {**{k: [p for p in v if first <= p["day"] <= last] for k, v in measures.items()}, "trend": trend},
        "sessions": sessions, "comparison": comparison[:4],
        "guidance": assess(current, previous, plan, [event for event in workouts
                           if period_start.isoformat() <= event["day"] <= last_complete.isoformat()]),
        "sources": sources(db, today_str),
    }


class Handler(BaseHTTPRequestHandler):
    sync_lock = Lock()
    next_sync = 0
    retry_lock = Lock()
    next_retry = 0

    def log_message(self, format, *args):
        return

    def respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        host = self.headers.get("Host", "").split(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            self.respond(403, b"Forbidden", "text/plain")
            return
        url = urlsplit(self.path)
        if url.path in STATIC:
            filename, content_type = STATIC[url.path]
            self.respond(200, (HERE / "dashboard" / filename).read_bytes(), content_type)
        elif url.path == "/api/dashboard":
            query = parse_qs(url.query)
            view, = query.get("range", ["all"])[:1]
            today = datetime.now(TZ).date()
            custom_dates = None
            if view == "custom":
                try:
                    start = date.fromisoformat(query["start"][0])
                    end = date.fromisoformat(query["end"][0])
                    if not BASELINE <= start <= end <= today:
                        raise ValueError("Outside supported dates")
                    custom_dates = (start, end)
                except (KeyError, ValueError):
                    self.respond(400, b'{"error":"Invalid custom dates"}', "application/json")
                    return
            elif view not in WINDOWS:
                self.respond(400, b'{"error":"Invalid date range"}', "application/json")
                return
            try:
                uri = (STATE / "ledger.sqlite3").as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
                    db.execute("BEGIN")
                    result = dashboard(db, view, today, custom_dates)
                    result["report_delivery"] = report_delivery(db, datetime.now(TZ))
                payload = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
            except (OSError, ValueError, TypeError, sqlite3.Error):
                self.respond(503, '{"error":"总账暂时不可读取，请稍后刷新。"}'.encode(), "application/json")
                return
            self.respond(200, payload, "application/json; charset=utf-8")
        else:
            self.respond(404, b"Not found", "text/plain")

    def do_POST(self):
        if self.path in ("/api/reports/daily/retry", "/api/reports/weekly/retry"):
            self.retry_report(self.path.split("/")[3])
            return
        if self.path != "/api/sync/xunji":
            self.respond(404, b"Not found", "text/plain")
            return
        host = self.headers.get("Host", "").split(":", 1)[0]
        if host not in ("127.0.0.1", "localhost") or self.headers.get("X-Ledger-Action") != "sync-xunji":
            self.respond(403, b"Forbidden", "text/plain")
            return
        if not self.sync_lock.acquire(blocking=False):
            self.sync_result(409, "训记同步正在进行，请稍候。")
            return
        try:
            if monotonic() < Handler.next_sync:
                self.sync_result(429, "请求过于频繁，请稍后再试。")
                return
            Handler.next_sync = monotonic() + 60
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(250)
                    client.connect("/run/health-ledger-xunji.sock")
                    with client.makefile("rb") as result:
                        completed = result.read(4096).rstrip().endswith("训记同步完成".encode())
            except OSError:
                self.sync_result(503, "暂时无法启动或确认同步，请查看数据同步页。")
                return
            if completed:
                self.sync_result(200, "训记同步完成。")
            else:
                self.sync_result(502, "部分训记数据同步失败，已入账的数据可以刷新查看。")
        finally:
            self.sync_lock.release()

    def sync_result(self, status, message):
        self.respond(status, json.dumps({"message": message}, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def retry_report(self, kind):
        host = self.headers.get("Host", "").split(":", 1)[0]
        if host not in ("127.0.0.1", "localhost") or self.headers.get("X-Ledger-Action") != "retry-report":
            self.sync_result(403, "该请求无法执行。")
            return
        if not self.retry_lock.acquire(blocking=False):
            self.sync_result(409, "报告正在推送，请稍候。")
            return
        try:
            uri = (STATE / "ledger.sqlite3").as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
                reports = report_delivery(db, datetime.now(TZ))
            if not any(report["kind"] == kind and report["retryable"] for report in reports):
                self.sync_result(409, "当前没有可重新推送的报告。")
                return
            if monotonic() < Handler.next_retry:
                self.sync_result(429, "请稍候再试。")
                return
            Handler.next_retry = monotonic() + 5
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(70)
                client.connect("/run/health-ledger-report-retry.sock")
                client.sendall(f"{kind}\n".encode())
                with client.makefile("rb") as response:
                    completed = response.read(4096).rstrip().endswith("报告推送完成".encode())
            if completed:
                self.sync_result(200, "微信推送成功。")
            else:
                self.sync_result(502, "重推未成功。请确认已给微信机器人发过消息，并在总账任务结束后重试。")
        except (OSError, sqlite3.Error):
            self.sync_result(503, "暂时无法确认推送结果，请刷新看板查看状态。")
        finally:
            self.retry_lock.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18790)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
