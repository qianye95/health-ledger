"""Shared interpretation of daily health observations."""
from collections import Counter, defaultdict
from datetime import date, datetime
import json
from math import isfinite
from statistics import mean

from ledger import SOURCES, TZ, USER


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
        logged, metrics = intake_metrics(food, {})
        if logged:
            food_days.add(day)
        for key, target in (("calories_kcal", food_kcal), ("protein_g", food_protein)):
            value = metrics[key]["actual"]
            if value is not None:
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
