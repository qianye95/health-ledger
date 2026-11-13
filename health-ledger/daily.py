"""Render and deliver the daily dashboard summary."""
from datetime import date, timedelta

from dashboard import dashboard, profile_on, profile_versions, today_intake
from ledger import DASHBOARD_URL, STATE, USER, SourceError, atomic, now
from weekly import send_wechat


def amount(value):
    return f"{value:,.1f}".removesuffix(".0")


def progress_line(label, reading, start, goal, day, unit, scale):
    parts = [f"{label} 暂无实测"]
    if reading:
        reading_date = f"（{reading['day'][5:]} 实测）" if reading["day"] != day else ""
        parts[0] = f"{label} {amount(reading['value'] * scale)} {unit}{reading_date}"
    if goal is not None:
        parts.append(f"目标 {amount(goal * scale)} {unit}")
    if reading and start is not None:
        change = round((start - reading["value"]) * scale, 1)
        direction = "已减" if change >= 0 else "较起点增加"
        parts.append(f"{direction} {amount(abs(change))} {unit}")
    return " · ".join(parts)


def intake_line(metrics, label, key, unit, *, compare=False):
    actual, goal = (metrics[key][field] for field in ("actual", "goal"))
    if actual is None:
        return None
    show = (lambda value: f"{value:,.0f}") if compare else amount
    if goal is None:
        return f"{label} {show(actual)} {unit}"
    line = f"{label} {show(actual)} / {show(goal)} {unit}"
    if compare:
        delta = round(actual - goal)
        if delta:
            direction = "多" if delta > 0 else "少"
            line += f" · 比目标{direction} {abs(delta):,} {unit}"
        else:
            line += " · 达到目标"
    return line


def report_text(payload, yesterday):
    day = payload["end"]
    report_date = date.fromisoformat(day)
    weekday = "一二三四五六日"[report_date.weekday()]
    current = payload["current"]
    plan = payload["plan"]
    title = f"身体日报｜{report_date.month} 月 {report_date.day} 日 · 周{weekday}"
    progress = ["【减脂进度】"]
    for label, key, start_key, goal_key, unit, scale in (
        ("体重", "weight", "start_weight_kg", "target_weight_kg", "斤", 2),
        ("腰围", "waist", "start_waist_cm", "target_waist_cm", "cm", 1),
    ):
        progress.append(progress_line(label, current[key], plan.get(start_key), plan[goal_key], day, unit, scale))

    intake_day = date.fromisoformat(yesterday["day"])
    diet = [f"【昨日饮食】{intake_day.month} 月 {intake_day.day} 日"]
    metrics = yesterday["metrics"]
    energy = intake_line(metrics, "热量", "calories_kcal", "kcal", compare=True)
    protein = intake_line(metrics, "蛋白质", "protein_g", "g", compare=True)
    macros = " · ".join(filter(None, (
        intake_line(metrics, "碳水", "carbs_g", "g"),
        intake_line(metrics, "脂肪", "fat_g", "g"),
    )))
    diet.extend(list(filter(None, (energy, protein, macros))) or ["暂无已同步的摄入数据"])

    week = payload["training_calendar"]["week_totals"]
    counts = []
    for kind, label in (("strength", "力量"), ("cardio", "有氧")):
        goal = plan[f"{kind}_sessions_per_week"]
        counts.append(f"{label} {week[kind]}" + (f" / {goal} 场" if goal is not None else " 场"))
    recovery = ["【训练与恢复】", f"本周{' · '.join(counts)}"]

    sleep = next((point for point in payload["series"]["sleep"] if point["day"] == day), None)
    if sleep:
        hours, minutes = divmod(round(sleep["value"]), 60)
        recovery.append(f"昨夜睡眠 {hours} 小时 {minutes} 分钟")
    else:
        recovery.append("昨夜睡眠 · 暂无当天同步数据")

    # Desktop WeChat may collapse single newlines within a paragraph.
    sections = [[title], progress, diet, recovery]
    if DASHBOARD_URL:
        sections.append(["查看身体总账", DASHBOARD_URL])
    return "\n\n\n".join("\n\n".join(section) for section in sections) + "\n"


def generate(db, today, *, send=False, dry_run=False, resend=False):
    if (send or dry_run or resend) and not DASHBOARD_URL:
        raise SourceError("请配置 HEALTH_DASHBOARD_URL")
    day = today.isoformat()
    if send and not resend and db.execute(
        "SELECT 1 FROM daily_deliveries WHERE user_id=? AND day=?", (USER, day)
    ).fetchone():
        print("今日日报已发送")
        return
    yesterday = (today - timedelta(days=1)).isoformat()
    prior_plan = profile_on(yesterday, profile_versions())
    intake = today_intake(db, yesterday, prior_plan["daily_intake_targets"])
    report = report_text(dashboard(db, "7", today), intake)
    folder = STATE / "reports" / USER / day
    folder.mkdir(parents=True, exist_ok=True)
    atomic(folder / "daily.md", report.encode())
    print(f"日报已保存，日期 {day}")
    if dry_run:
        send_wechat(report, dry_run=True)
        print("微信投递参数验证通过")
    if send or resend:
        send_wechat(report)
        with db:
            db.execute("""INSERT INTO daily_deliveries VALUES (?,?,?)
                          ON CONFLICT(user_id, day) DO UPDATE SET sent_at=excluded.sent_at""", (USER, day, now()))
        print("日报已投递到绑定微信")
