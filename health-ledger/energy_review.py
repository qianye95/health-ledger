"""Weekly comparison of recorded intake, activity and measurement trends."""
from datetime import date, timedelta
from statistics import mean


def estimated_expenditure(weight, height, age, sex):
    resting = 10 * weight + 6.25 * height - 5 * age
    offsets = {"男性公式": 5, "女性公式": -161}
    known = {"male": "男性公式", "female": "女性公式", "男": "男性公式", "女": "女性公式"}.get(sex)
    # Use low-activity scenarios, not a calibration from steps or watch calories.
    return {label: ((resting + offset) * 1.4, (resting + offset) * 1.5)
            for label, offset in offsets.items() if known is None or label == known}


def timed_workouts(workouts):
    return [(event["end"] - event["start"]) / 60 for event in workouts
            if event["kind"] in ("strength", "cardio") and event["start"] is not None
            and event["end"] is not None and event["end"] > event["start"]]


def activity_line(label, period, workouts):
    steps = period["steps"]
    steps_text = f"日均 {mean(steps.values()):,.0f} 步（{len(steps)}/7 天）" if steps else "步数暂无数据"
    strength = sum(event["kind"] == "strength" for event in workouts)
    cardio = sum(event["kind"] == "cardio" for event in workouts)
    watch_days = period["coverage"].get("zepp-workout", 0)
    durations = timed_workouts(workouts)
    time_text = f"，其中 {len(durations)} 场合计 {sum(durations):,.0f} 分钟" if durations else ""
    return f"{label}{steps_text}，已记录力量 {strength} 场、有氧 {cardio} 场{time_text}（Zepp 运动同步 {watch_days}/7 天）。"


def assessment_lines(confirmed, persistent, weight_delta, waist_delta, activity_down, plausible_gap):
    if not confirmed:
        return [
            "判断：证据不足，暂时无法确认实际热量缺口。至少需要连续两周每天有热量记录，且每周至少有 5 天称重。",
            "接下来：再连续记录两周，补齐正餐、零食、饮料和用油，尽量在相同条件下称重和量腰围。",
        ]
    if (weight_delta < -0.25 or (waist_delta is not None and waist_delta <= -1)) and not persistent:
        return [
            "判断：周均体重或近期腰围出现下降，趋势支持可能有缺口。短期水分变化仍会影响体重。",
            "接下来：继续记录两周，用周均体重和同条件腰围复核趋势。",
        ]
    if not plausible_gap:
        return ["判断：按当前摄入记录和消耗情景，缺口尚不明确。先保持记录，结合未来两周的体重和腰围再评估。"]

    intake = "饮食可能低估。优先核对烹调油、酱料、饮料、零食和外食份量。每天有条目也可能漏记一部分。"
    expenditure = "每日消耗可能高估。步数与训练场次只能说明活动情况，个体总消耗仍可能低于模型。"
    water = "水分和胃肠内容物短期波动可能掩盖变化。现有数据无法确认水肿。"
    if activity_down:
        hypotheses = [expenditure, intake, water]
        first = "本周日均步数或训练时长明显下降，优先核对消耗估算。"
    elif persistent:
        hypotheses = [intake, expenditure, water]
        first = "体重多周缺少下降迹象，优先核对摄入记录和消耗估算。两者目前无法区分。"
    else:
        hypotheses = [water, intake, expenditure]
        first = "目前时间较短，优先考虑短期波动。"
    return [
        f"判断：账面有缺口，实测暂未显示明确下降。{first}",
        *[f"{index}. {text}" for index, text in enumerate(hypotheses, 1)],
        "接下来：先复核记录并观察两周，再决定是否调整热量目标。",
    ]


def energy_review(current, previous, weeks, waist, profile, age, end, workouts):
    start = end - timedelta(days=6)
    this_week = [event for event in workouts if event["day"] >= start.isoformat()]
    prior_week = [event for event in workouts if event["day"] < start.isoformat()]
    lines = ["", "## 热量缺口评估", ""]
    intake = current["recorded_kcal"]
    prior_intake = previous["recorded_kcal"]
    for label, values in (("本周", intake), ("上周", prior_intake)):
        value = f"记录日均 {mean(values.values()):,.0f} kcal" if values else "暂无摄入热量"
        lines.append(f"{label}{value}（{len(values)}/7 天）。")
    lines += [activity_line("本周", current, this_week), activity_line("上周", previous, prior_week)]

    weight = current["weights"]
    prior_weight = previous["weights"]
    weight_delta = mean(weight.values()) - mean(prior_weight.values()) if min(len(weight), len(prior_weight)) >= 5 else None
    if weight_delta is not None:
        lines.append(f"周均体重较上周 {weight_delta:+.2f} kg。")
    waist_delta = None
    if len(waist) == 2:
        newest, older = waist
        measured, earlier = date.fromisoformat(newest["day"]), date.fromisoformat(older["day"])
        if (end - measured).days <= 7 and 7 <= (measured - earlier).days <= 21:
            waist_delta = newest["cm"] - older["cm"]
            lines.append(f"近期腰围 {waist_delta:+.1f} cm（{older['day']} 到 {newest['day']} 实测）。")
    if waist_delta is None:
        lines.append("近期腰围可比测量不足。")

    height = profile.get("height_cm")
    plausible_gap = False
    prior_gap = False
    if weight and height and age is not None:
        expenditure = estimated_expenditure(mean(weight.values()), height, age, profile.get("sex") or profile.get("gender"))
        differences = []
        lines.append("维持热量按 Mifflin–St Jeor 公式及低活动量系数 1.4–1.5 估算。步数和训练记录用于核对实际活动变化。")
        for label, (low, high) in expenditure.items():
            line = f"- {label} · 维持约 {low:,.0f} 至 {high:,.0f} kcal/日"
            if len(intake) == 7:
                recorded = mean(intake.values())
                line += f" · 账面缺口 {low - recorded:+,.0f} 至 {high - recorded:+,.0f} kcal/日"
                differences.append(low - recorded)
            lines.append(line + "。")
        if len(intake) == 7:
            lines.append("缺口按估算消耗减去日均记录摄入，正值表示估算有缺口。")
            plausible_gap = all(value >= 200 for value in differences)
        if len(prior_intake) == 7:
            prior_gap = all(low - mean(prior_intake.values()) >= 200 for low, _ in expenditure.values())
        else:
            lines.append("本周摄入缺天，暂不计算整周日均缺口。")

    recent = [weights for _, weights in weeks[-3:]]
    three_weeks = len(recent) == 3 and all(len(weights) >= 5 for weights in recent)
    persistent = three_weeks and prior_gap and all(
        mean(after.values()) - mean(before.values()) >= -0.25 for before, after in zip(recent, recent[1:])
    ) and (waist_delta is None or waist_delta > -1)
    steps, prior_steps = current["steps"], previous["steps"]
    steps_down = min(len(steps), len(prior_steps)) >= 5 and mean(steps.values()) < 0.8 * mean(prior_steps.values())
    workout_minutes, prior_minutes = sum(timed_workouts(this_week)), sum(timed_workouts(prior_week))
    watch_coverage = min(period["coverage"].get("zepp-workout", 0) for period in (current, previous))
    training_down = watch_coverage == 7 and prior_minutes - workout_minutes >= 30 and workout_minutes < 0.75 * prior_minutes
    complete = len(intake) == len(prior_intake) == 7 and weight_delta is not None
    lines.extend(("", *assessment_lines(complete, persistent, weight_delta, waist_delta,
                                       steps_down or training_down, plausible_gap)))
    return lines
