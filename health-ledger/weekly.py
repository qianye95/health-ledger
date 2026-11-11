"""Build a reproducible weekly health review from normalized day partitions."""
from collections import Counter
from datetime import date, datetime, timedelta
import json
from math import isfinite
from pathlib import Path
from statistics import mean
import subprocess

from ledger import DASHBOARD_URL, SOURCES, STATE, TZ, USER, SourceError, atomic, dates, now


def number(value):
    if type(value) not in (int, float, str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if isfinite(parsed) else None


def load_period(db, start, end):
    data = {source: {} for source in SOURCES}
    for source, day, text in db.execute("""
        SELECT source,day,payload FROM observations
        WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY day
    """, (USER, start, end)):
        data[source][day] = json.loads(text)
    return data


def sleep_minutes(summary, day):
    sleep = summary.get("slp") or {}
    end = sleep.get("ed")
    if not isinstance(end, int) or end <= 0 or datetime.fromtimestamp(end, TZ).date().isoformat() != day:
        return None
    totals = Counter()
    prev = None
    for stage in sleep.get("stage") or []:
        start, stop, mode = (stage.get(k) for k in ("start", "stop", "mode"))
        if not all(type(v) is int for v in (start, stop, mode)):
            return None
        if stop < start or mode not in (4, 5, 7, 8) or (prev is not None and start != prev + 1):
            return None
        totals[mode] += stop - start + 1
        prev = stop
    if not totals or any(totals[mode] != sleep.get(key) for mode, key in ((4, "lt"), (5, "dp"), (7, "wk"))):
        return None
    return totals[4] + totals[5] + totals[8]


def timestamp(value):
    if type(value) not in (int, float) or value <= 0:
        return None
    return value / 1000 if value >= 10**12 else value


def heart_minutes(zepp):
    points = {}
    for day, row in zepp.items():
        midnight = int(datetime.fromisoformat(day).replace(tzinfo=TZ).timestamp())
        for item in row.get("heart_rate") or []:
            minute, bpm = item.get("minute"), item.get("bpm")
            if type(minute) is int and 0 <= minute < 1440 and type(bpm) is int and 0 < bpm <= 250:
                points[midnight // 60 + minute] = bpm
    return points


def pair_heart(training, points):
    start, end = timestamp(training.get("start")), timestamp(training.get("end"))
    if start is None or end is None or not 0 < end - start <= 86400:
        return None
    first, stop = int(start // 60), int((end - 0.001) // 60) + 1
    values = [points[k] for k in range(first, stop) if k in points]
    return {"minutes": len(values), "window_minutes": stop - first,
            "avg_bpm": round(mean(values), 1) if values else None, "max_sample_bpm": max(values, default=None)}


def is_cardio(training):
    movements = training.get("movements") or []
    return bool(movements) and all(movement.get("cardio") is True for movement in movements)


def aggregate(data, days, points):
    weight, steps, rhr, sleep, sessions = {}, {}, {}, {}, []
    food_days, food_kcal, food_protein = set(), {}, {}
    for day in days:
        food = data["xunji-food"].get(day, [])
        logged = [item for item in food if item.get("foods") or (number(item.get("item_count")) or 0) > 0]
        if logged:
            food_days.add(day)
        if len(logged) == 1:
            totals = logged[0].get("totals") or {}
            for field, target in (("totalCal", food_kcal), ("totalProtein", food_protein)):
                value = number(totals.get(field))
                if value is not None and value >= 0:
                    target[day] = value
        values = [number(row.get("value")) for row in data["xunji-body"].get(day, []) if row.get("type") == "weight"]
        values = [v for v in values if v is not None and v > 0]
        if values:
            weight[day] = mean(values)
        summary = data["zepp"].get(day, {}).get("summary") or {}
        step = number((summary.get("stp") or {}).get("ttl"))
        resting = number((summary.get("slp") or {}).get("rhr"))
        asleep = sleep_minutes(summary, day)
        if step is not None and step >= 0:
            steps[day] = step
        if resting is not None and resting > 0:
            rhr[day] = resting
        if asleep is not None:
            sleep[day] = asleep
        for row in data["xunji-training"].get(day, []):
            lifts = {}
            for move in row.get("movements") or []:
                name = move.get("name")
                if move.get("cardio") is True or not name:
                    continue
                groups = []
                for group in move.get("sets") or []:
                    if group.get("done") is not True or group.get("setType") in ("热", "warmup"):
                        continue
                    kg, reps = number(group.get("weight")), number(group.get("reps"))
                    unit = group.get("unit")
                    if kg is not None and kg >= 0 and reps is not None and reps > 0 and unit in ("kg", "lb"):
                        groups.append({"kg": round(kg * (0.45359237 if unit == "lb" else 1), 2), "reps": reps})
                if groups:
                    lifts.setdefault(name, []).extend(groups)
            if lifts:
                sessions.append({"date": day, "title": row.get("title") or "力量训练",
                                 "start": timestamp(row.get("start")), "end": timestamp(row.get("end")),
                                 "lifts": lifts, "heart": pair_heart(row, points)})
    return {"weights": weight, "steps": steps, "resting_heart_rate": rhr, "sleep_minutes": sleep,
            "sessions": sessions, "food_logged_days": len(food_days), "recorded_kcal": food_kcal,
            "recorded_protein_g": food_protein,
            "coverage": {k: sum(d in v for d in days) for k, v in data.items()},
            "cardio_sessions": sum(is_cardio(r) for d in days for r in data["xunji-training"].get(d, []))}


def waist_history(db, end):
    result = []
    for day, raw in db.execute("""
        SELECT day,payload FROM observations WHERE user_id=? AND source='xunji-body' AND day<=? ORDER BY day DESC
    """, (USER, end)):
        values = [number(row.get("value")) for row in json.loads(raw) if row.get("type") == "weist"]
        values = [v for v in values if v is not None and v > 0]
        if values:
            result.append({"day": day, "cm": values[0]})
        if len(result) == 2:
            break
    return result


def latest_runs(db):
    result = {}
    for source in SOURCES:
        last = db.execute("""
            SELECT status,detail,completed_at FROM sync_runs WHERE user_id=? AND source=? ORDER BY id DESC LIMIT 1
        """, (USER, source)).fetchone()
        if last:
            result[source] = {"status": last[0], "detail": last[1], "checked_at": last[2]}
    return result


def average(values):
    return mean(values.values()) if values else None


def describe(values, fmt):
    val = average(values)
    return f"{format(val, fmt)}（{len(values)}/7 天）" if val is not None else "缺数据"


def age_on(birthday, day):
    born, as_of = date.fromisoformat(birthday), date.fromisoformat(day)
    return as_of.year - born.year - ((as_of.month, as_of.day) < (born.month, born.day))


def lift_changes(current, previous):
    def best_by_reps(period):
        result = {}
        for session in period["sessions"]:
            for name, sets in session["lifts"].items():
                for group in sets:
                    key = (name, group["reps"])
                    result[key] = max(result.get(key, 0), group["kg"])
        return result

    latest, prior = best_by_reps(current), best_by_reps(previous)
    return sorted(((name, reps, prior[name, reps], latest[name, reps])
                   for name, reps in latest.keys() & prior.keys()),
                  key=lambda row: (row[0], -row[1]))


def report_text(end, start, current, previous, weeks, waist, profile, latest):
    titles = {"xunji-training": "训记训练", "xunji-body": "训记体重／腰围",
              "xunji-food": "训记饮食", "zepp": "Zepp"}
    lines = [f"# 身体数据周报 {start} 至 {end}", "", f"计划阶段：{profile.get('phase', '待录入')}", "",
             "| 指标 | 本周 | 上周 |", "|---|---:|---:|"]
    for label, key, fmt in (("实测平均体重（kg）", "weights", ".2f"), ("平均睡眠（分钟）", "sleep_minutes", ".0f"),
                            ("日均步数", "steps", ".0f"), ("平均静息心率（次/分）", "resting_heart_rate", ".1f")):
        lines.append(f"| {label} | {describe(current[key], fmt)} | {describe(previous[key], fmt)} |")
    def covered(period, source, value):
        days = period["coverage"][source]
        return f"{value}（覆盖 {days}/7 天）" if days else "缺数据"

    for label, source, value in (
        ("已记录的力量训练（场）", "xunji-training", lambda p: len(p["sessions"])),
        ("训记原生有氧（场）", "xunji-training", lambda p: p["cardio_sessions"]),
        ("有饮食记录（天）", "xunji-food", lambda p: p["food_logged_days"]),
    ):
        lines.append(f"| {label} | {covered(current, source, value(current))} | {covered(previous, source, value(previous))} |")
    lines.append("")
    if current["recorded_kcal"] or current["recorded_protein_g"]:
        lines.append(f"有对应营养汇总的记录中，日均已记录热量 {describe(current['recorded_kcal'], '.0f')} kcal，"
                     f"日均已记录蛋白质 {describe(current['recorded_protein_g'], '.1f')} g。漏记餐食会影响这两个数。")
    if min(len(current["weights"]), len(previous["weights"])) >= 4:
        delta = average(current["weights"]) - average(previous["weights"])
        lines.append(f"本周平均体重比上周 {delta:+.2f} kg。")
    if weeks:
        trend = "、".join(f"{day} {describe(weight, '.2f')}" for day, weight in weeks)
        lines.append(f"近四周平均体重（kg，按周截止日）为 {trend}。")
    if waist:
        recent_waist = waist[0]
        freshness = "本周实测" if recent_waist["day"] >= start else "沿用最近实测，本周没有新测量"
        lines.append(f"当前腰围 {recent_waist['cm']:g} cm，{recent_waist['day']} {freshness}。")
        if len(waist) == 2:
            older = waist[1]
            lines.append(f"两次实测相比 {recent_waist['cm'] - older['cm']:+.1f} cm，上一次为 {older['day']}。")
    else:
        lines.append("腰围暂无实测。")
    if profile:
        lines += ["", "## 计划对照", ""]
        if profile.get("birth_date") and profile.get("height_cm"):
            lines.append(f"身高 {profile['height_cm']:g} cm，{age_on(profile['birth_date'], end)} 岁。")
        if profile.get("estimated_body_fat_pct") is not None:
            lines.append(f"参考体脂率 {profile['estimated_body_fat_pct']:g}%（估算）。")
        if profile.get("target_weight_kg") and current["weights"]:
            lines.append(f"目标体重 {profile['target_weight_kg']:g} kg，本周实测均值距目标 {average(current['weights']) - profile['target_weight_kg']:+.2f} kg。")
        if "strength_sessions_per_week" in profile:
            lines.append(f"力量计划每周 {profile['strength_sessions_per_week']} 场，本周确认记录 {len(current['sessions'])} 场。")
        if "cardio_sessions_per_week" in profile:
            lines.append(f"有氧计划每周 {profile['cardio_sessions_per_week']} 场，本周训记记录 {current['cardio_sessions']} 场。Zepp 单次有氧活动明细待接入。")
        if "neat_minutes_per_day" in profile:
            lines.append(f"NEAT 计划每天 {profile['neat_minutes_per_day']} 分钟。目前只有步数等日常活动指标，完成时长待验证。")
    lines.extend(["", "## 力量训练与心率", ""])
    for row in current["sessions"]:
        lifts = "、".join(f"{name} {len(groups)} 组" for name, groups in row["lifts"].items())
        heart = row["heart"]
        if heart and heart["minutes"]:
            window = f"{heart['minutes']}/{heart['window_minutes']} 分钟"
            info = f"平均心率 {heart['avg_bpm']:g}，最高采样 {heart['max_sample_bpm']}，覆盖 {window}"
        else:
            info = "Zepp 心率未配对"
        lines.append(f"- {row['date']} {row['title']}。{lifts}。{info}。")
    if not current["sessions"]:
        lines.append("这周暂无可统计的力量训练记录。")
    compared = set()
    for name, reps, before, after in lift_changes(current, previous):
        if name in compared:
            continue
        compared.add(name)
        lines.append(f"- {name}同为 {reps:g} 次时，记录的最高重量为上周 {before:g} kg、本周 {after:g} kg。")
        if len(compared) == 4:
            break
    lines += ["", "## 下周关注", ""]
    if min(len(current["weights"]), len(previous["weights"])) < 4:
        lines.append("- 继续按相同条件称重，等本周和对照周有足够实测天数后再判断变化。")
    elif not profile:
        lines.append("- 补齐当前计划和目标后，再用体重、腰围及训练执行度判断是否需要调整。")
    else:
        lines.append("- 下周继续在同样条件下记录体重和主要动作，结合腰围实测复核趋势。")
    if current["food_logged_days"] < 4:
        lines.append("- 饮食记录覆盖有限，本周无法据此判断热量摄入变化。")
    lines += ["", "## 数据覆盖", ""]
    for source, title in titles.items():
        cutoff, run = latest[source]
        detail = f"，截至 {cutoff}" if cutoff else ""
        if run and run["status"] != "ok":
            detail += f"，最近同步状态：{run['detail']}"
        lines.append(f"- {title}本周覆盖 {current['coverage'][source]}/7 天，上周 {previous['coverage'][source]}/7 天{detail}。")
    return "\n".join(lines) + "\n"


def send_wechat(body, *, dry_run=False):
    account_root = Path.home() / ".openclaw/openclaw-weixin/accounts"
    accounts = []
    for path in account_root.glob("*.json"):
        if path.name.endswith((".sync.json", ".context-tokens.json")):
            continue
        uid = json.loads(path.read_text()).get("userId")
        if isinstance(uid, str) and uid.strip():
            accounts.append((path.stem, uid.strip()))
    if len(accounts) != 1:
        raise SourceError("微信绑定账号无法唯一确认")
    account, recipient = accounts[0]
    cli = Path.home() / ".openclaw/bin/openclaw"
    command = [str(cli), "message", "send", "--channel", "openclaw-weixin",
               "--account", account, "--target", recipient, "--message", body, "--json"]
    if dry_run:
        command.append("--dry-run")
    try:
        result = subprocess.run(command, capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        raise SourceError("微信投递状态未知") from None
    if result.returncode:
        raise SourceError("微信投递失败")


def deliver(db, end, body):
    if db.execute("SELECT 1 FROM deliveries WHERE user_id=? AND week_end=?", (USER, end)).fetchone():
        print("本周周报已发送")
        return
    if not DASHBOARD_URL:
        raise SourceError("请配置 HEALTH_DASHBOARD_URL")
    send_wechat(f"{body.rstrip()}\n\n身体总账\n{DASHBOARD_URL}")
    with db:
        db.execute("INSERT INTO deliveries VALUES (?,?,?)", (USER, end, now()))
    print("周报已投递到绑定微信")


def generate(db, end, send):
    start = end - timedelta(days=6)
    baseline = end - timedelta(days=27)
    data = load_period(db, (baseline - timedelta(days=1)).isoformat(), (end + timedelta(days=1)).isoformat())
    points = heart_minutes(data["zepp"])
    current = aggregate(data, dates(start, end), points)
    previous = aggregate(data, dates(start - timedelta(days=7), end - timedelta(days=7)), points)
    weeks = []
    for offset in range(3, -1, -1):
        week_end = end - timedelta(days=7 * offset)
        week = aggregate(data, dates(week_end - timedelta(days=6), week_end), points)
        weeks.append((week_end.isoformat(), week["weights"]))
    waist = waist_history(db, end.isoformat())
    path = STATE / "profile.json"
    versions = json.loads(path.read_text()) if path.is_file() else []
    profile = max((p for p in versions if p["effective_from"] <= end.isoformat()),
                  key=lambda p: p["effective_from"], default={})
    start_day, end_day = start.isoformat(), end.isoformat()
    runs = latest_runs(db)
    cutoffs = dict(db.execute("""
        SELECT source,max(day) FROM observations WHERE user_id=? AND day<=? GROUP BY source
    """, (USER, end_day)))
    latest = {source: (cutoffs.get(source), runs.get(source)) for source in SOURCES}
    report = report_text(end_day, start_day, current, previous, weeks, waist, profile, latest)
    folder = STATE / "reports" / USER / end_day
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"start": start_day, "end": end_day, "generated_at": now(), "current": current,
            "previous": previous, "weight_weeks": weeks, "waist": waist,
            "profile": profile, "sync": latest}
    atomic(folder / "report.json", json.dumps(meta, ensure_ascii=False, indent=2).encode())
    atomic(folder / "report.md", report.encode())
    print(f"周报已保存，统计期 {start_day} 至 {end_day}")
    if send:
        deliver(db, end_day, report)
