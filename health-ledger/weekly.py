"""Build a reproducible weekly health review from normalized day partitions."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from statistics import mean

from ledger import SOURCES, STATE, TZ, USER, atomic, dates, now
from delivery import deliver_report
from measurements import age_on, aggregate, heart_minutes, lift_changes, load_period, merged_workouts, number


def waist_history(db, end, *, min_gap=0):
    result = []
    for day, raw in db.execute("""
        SELECT day,payload FROM observations WHERE user_id=? AND source='xunji-body' AND day<=? ORDER BY day DESC
    """, (USER, end)):
        values = [number(row.get("value")) for row in json.loads(raw) if row.get("type") == "weist"]
        values = [v for v in values if v is not None and v > 0]
        if values and (not result or (date.fromisoformat(result[0]["day"]) - date.fromisoformat(day)).days >= min_gap):
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


def report_text(end, start, current, previous, weeks, waist, profile, latest, energy):
    titles = {"xunji-training": "训记训练", "xunji-body": "训记体重／腰围",
              "xunji-food": "训记饮食", "zepp": "Zepp 日汇总", "zepp-workout": "Zepp 单次运动"}
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
    lines.extend(energy)
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
            lines.append(f"有氧计划每周 {profile['cardio_sessions_per_week']} 场，合并后的实际场次见热量缺口评估。")
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


def generate(db, end, send):
    from energy_review import energy_review

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
    workouts = merged_workouts(data, dates(start - timedelta(days=7), end))
    age = age_on(profile["birth_date"], end_day) if profile.get("birth_date") else number(profile.get("age"))
    energy = energy_review(current, previous, weeks, waist_history(db, end_day, min_gap=7), profile, age, end, workouts)
    report = report_text(end_day, start_day, current, previous, weeks, waist, profile, latest, energy)
    folder = STATE / "reports" / USER / end_day
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"start": start_day, "end": end_day, "generated_at": now(), "current": current,
            "previous": previous, "weight_weeks": weeks, "waist": waist,
            "profile": profile, "sync": latest, "energy_review": energy}
    atomic(folder / "report.json", json.dumps(meta, ensure_ascii=False, indent=2).encode())
    atomic(folder / "report.md", report.encode())
    print(f"周报已保存，统计期 {start_day} 至 {end_day}")
    if send:
        deliver_report(db, "weekly", end_day, report)
