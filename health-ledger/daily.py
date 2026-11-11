"""Render and deliver the daily dashboard summary."""
from dashboard import dashboard
from ledger import DASHBOARD_URL, STATE, USER, SourceError, atomic, now
from weekly import send_wechat


def measured(reading, unit, scale=1):
    if reading is None:
        return "暂无实测"
    value = f"{reading['value'] * scale:.1f}".removesuffix(".0")
    return f"{value} {unit}（{reading['day'][5:]} 实测）"


def report_text(payload):
    day = payload["end"]
    current = payload["current"]
    plan = payload["plan"]
    lines = [f"身体日报 · {day}", "", "减脂进度"]
    for label, key, goal_key, unit, scale in (
        ("体重", "weight", "target_weight_kg", "斤", 2),
        ("腰围", "waist", "target_waist_cm", "cm", 1),
    ):
        reading = measured(current[key], unit, scale)
        goal = plan[goal_key]
        suffix = f" · 目标 {goal * scale:g} {unit}" if goal is not None else ""
        lines.append(f"{label} {reading}{suffix}")

    week = payload["training_calendar"]["week_totals"]
    counts = []
    for kind, label in (("strength", "力量"), ("cardio", "有氧")):
        goal = plan[f"{kind}_sessions_per_week"]
        counts.append(f"{label} {week[kind]}" + (f" / {goal} 场" if goal is not None else " 场"))
    lines.extend(("", f"本周训练 · {'，'.join(counts)}"))

    sleep = next((point for point in payload["series"]["sleep"] if point["day"] == day), None)
    if sleep:
        hours, minutes = divmod(round(sleep["value"]), 60)
        lines.append(f"昨夜睡眠 · {hours} 小时 {minutes} 分钟")
    else:
        lines.append("昨夜睡眠 · 暂无当天同步数据")

    intake = payload["intake"]
    amounts = []
    for key, label, unit in (("calories_kcal", "热量", "kcal"), ("carbs_g", "碳水", "g"),
                             ("protein_g", "蛋白质", "g"), ("fat_g", "脂肪", "g")):
        value = intake["metrics"][key]["actual"]
        if value is not None:
            amounts.append(f"{label} {value:.1f} {unit}")
    if amounts:
        lines.extend(("", "今日已记录摄入", " · ".join(amounts)))
    else:
        lines.extend(("", "今日摄入 · 暂无已同步记录"))
    if DASHBOARD_URL:
        lines.extend(("", "打开身体总账", DASHBOARD_URL))
    return "\n".join(lines) + "\n"


def generate(db, today, *, send=False, dry_run=False):
    if (send or dry_run) and not DASHBOARD_URL:
        raise SourceError("请配置 HEALTH_DASHBOARD_URL")
    day = today.isoformat()
    if send and db.execute("SELECT 1 FROM daily_deliveries WHERE user_id=? AND day=?", (USER, day)).fetchone():
        print("今日日报已发送")
        return
    report = report_text(dashboard(db, "7", today))
    folder = STATE / "reports" / USER / day
    folder.mkdir(parents=True, exist_ok=True)
    atomic(folder / "daily.md", report.encode())
    print(f"日报已保存，日期 {day}")
    if dry_run:
        send_wechat(report, dry_run=True)
        print("微信投递参数验证通过")
    if send:
        send_wechat(report)
        with db:
            db.execute("INSERT INTO daily_deliveries VALUES (?,?,?)", (USER, day, now()))
        print("日报已投递到绑定微信")
