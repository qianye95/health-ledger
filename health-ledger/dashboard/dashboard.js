"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const dayMs = 86400000;
const fmt = (value, digits = 1) => Number.isFinite(value)
  ? new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value)
  : "—";
const signed = (value, digits) => Number.isFinite(value) ? (value > 0 ? "+" : "") + fmt(value, digits) : "—";
const shortDay = (day) => day?.slice(5).replace("-", "/") ?? "—";
const dayIndex = (day) => Date.parse(day + "T00:00:00Z") / dayMs;
const dayAt = (index) => new Date(index * dayMs).toISOString().slice(0, 10);
const average = (points) => points.length ? points.reduce((sum, p) => sum + p.value, 0) / points.length : null;
const sleepLabel = (minutes) => Number.isFinite(minutes)
  ? `${Math.floor(Math.round(minutes) / 60)} 小时 ${Math.round(minutes) % 60} 分`
  : "暂无有效数据";

function element(tag, text, className) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text != null) item.textContent = text;
  return item;
}

function metric(label, value, unit, note) {
  const item = element("article", null, "metric");
  const number = element("strong", value, "value");
  if (unit) number.append(element("span", unit, "unit"));
  item.append(element("p", label), number, element("p", note));
  return item;
}

function intakeCard(label, { actual, goal, estimated_goal }, unit) {
  const card = element("article", null, "plan-card intake-card");
  const heading = element("div", null, "intake-title");
  const percent = actual != null && goal != null ? actual / goal * 100 : null;
  const overGoal = percent > 100;
  card.classList.toggle("is-over-goal", overGoal);
  heading.append(element("h3", label), element("span", percent == null ? "" : `${fmt(percent, 0)}%`, "intake-percent"));
  const values = element("div", null, "intake-values");
  values.append(element("strong", actual == null ? "—" : `${fmt(actual, 1)} ${unit}`),
    element("span", goal == null ? "暂无目标" : `${estimated_goal ? "折算目标" : "目标"} ${fmt(goal, 0)} ${unit}`));
  card.append(heading, values);
  if (percent != null) {
    const meter = element("progress");
    meter.max = 100;
    meter.value = Math.min(percent, 100);
    meter.setAttribute("aria-label", `${label}目标完成度 ${fmt(percent, 0)}%`);
    card.append(meter);
    const difference = goal - actual;
    const status = overGoal
      ? element("p", `超出 ${fmt(-difference, 1)} ${unit}`, "intake-excess")
      : element("p", `剩余 ${fmt(difference, 1)} ${unit}`);
    card.append(status);
  } else {
    card.append(element("div", null, "intake-track"));
    card.append(element("p", actual == null ? "暂无可统计的摄入" : "设置目标后显示完成度"));
  }
  if (estimated_goal) card.append(element("p", "按训记的碳水、蛋白质和脂肪目标折算。", "energy-note"));
  return card;
}

function mealCard(meal, emptyMessage) {
  const card = element("article", null, "panel meal-card");
  const heading = element("div", null, "meal-heading");
  heading.append(element("h3", meal.name));
  if (meal.carbs_g != null) {
    heading.append(element("strong", `碳水合计 ${fmt(meal.carbs_g)} g`, "meal-total"));
  }
  card.append(heading);
  if (!meal.foods.length) {
    card.append(element("p", emptyMessage, "meal-empty"));
    return card;
  }
  const list = element("ul", null, "food-list");
  for (const food of meal.foods) {
    const row = element("li", null, "food-row");
    const details = element("div", null, "food-heading");
    details.append(element("span", food.name, "food-name"));
    if (food.amount != null) {
      const amount = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(food.amount);
      details.append(element("span", `${amount} ${food.unit}`, "food-amount"));
    }
    const nutrients = element("dl", null, "food-nutrients");
    for (const [label, value, reference] of [
      ["碳水", food.carbs_g], ["蛋白质", food.protein_g], ["脂肪", food.fat_g],
      ["膳食纤维", food.fiber_g, food.fiber_reference],
    ]) {
      const cell = element("div");
      const text = value == null ? "暂无数据" : `${fmt(value)} g`;
      const description = element("dd", reference ? null : text);
      if (reference) {
        const link = element("a", `约 ${text}`);
        link.href = reference.url;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.title = reference.title;
        link.setAttribute("aria-label", `约 ${text}，参考来源 ${reference.title}`);
        description.append(link);
      }
      cell.append(element("dt", label), description);
      nutrients.append(cell);
    }
    row.append(details, nutrients);
    list.append(row);
  }
  card.append(list);
  return card;
}

function renderIntake(intake) {
  const time = intake.observed_at ? new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(new Date(intake.observed_at)) : null;
  const status = time ? `最近同步 ${time}` : "等待当天同步";
  const recording = intake.logged ? "按训记已记录的食物统计" : time ? "训记当天暂无饮食记录" : "今日饮食尚未入账";
  $("#intake-caption").textContent = `${intake.day} · ${status} · ${recording}`;
  $("#intake-grid").replaceChildren(
    intakeCard("热量", intake.metrics.calories_kcal, "kcal"),
    intakeCard("碳水化合物", intake.metrics.carbs_g, "g"),
    intakeCard("蛋白质", intake.metrics.protein_g, "g"),
    intakeCard("脂肪", intake.metrics.fat_g, "g"),
  );
  const emptyMessage = time ? "暂无记录" : "等待同步";
  $("#meal-grid").replaceChildren(...intake.meals.map((meal) => mealCard(meal, emptyMessage)));
}

const definitions = {
  weight: { label: "体重", color: "#186b53", unit: "kg", scale: 1, digits: 1, minimum: 1 },
  sleep: { label: "睡眠", color: "#3f638e", unit: "小时", scale: 60, digits: 1, minimum: 1.5 },
  waist: { label: "腰围", color: "#186b53", unit: "cm", scale: 1, digits: 1, minimum: 3 },
  resting: { label: "静息心率", color: "#3f638e", unit: "次/分", scale: 1, digits: 0, minimum: 6 },
  steps: { label: "步数", color: "#996629", unit: "步", scale: 1, digits: 0, minimum: 1500, bar: true },
};

function axis(values, minimum, zero) {
  const low = Math.min(...values);
  const high = Math.max(...values);
  const width = Math.max(high - low, minimum);
  const center = (low + high) / 2;
  const rough = width * 1.15 / 4;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = ([1, 2, 2.5, 5, 10].find((n) => n * power >= rough) ?? 10) * power;
  return { min: zero ? 0 : Math.floor((center - width * .575) / step) * step,
    max: Math.ceil((center + width * .575) / step) * step, step };
}

function chart(card) {
  const canvas = $("canvas", card);
  const ctx = canvas.getContext("2d");
  const tip = $(".tip", card);
  const kind = card.dataset.chart;
  const spec = definitions[kind];
  const latestLabel = kind === "sleep" ? null : element("div", null, "latest-point");
  if (latestLabel) {
    latestLabel.hidden = true;
    canvas.parentElement.append(latestLabel);
  }
  let rows = [], trend = [], start = 0, end = 0, focus = null, frame = null;

  function draw() {
    const box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const ratio = Math.min(devicePixelRatio || 1, 2);
    canvas.width = Math.round(box.width * ratio);
    canvas.height = Math.round(box.height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const width = box.width, height = box.height;
    ctx.clearRect(0, 0, width, height);
    ctx.font = "12px -apple-system, sans-serif";
    ctx.fillStyle = "#52645b";
    if (!rows.length) {
      ctx.textAlign = "center";
      ctx.fillText("这个范围内暂无实测", width / 2, height / 2);
      tip.hidden = true;
      if (latestLabel) latestLabel.hidden = true;
      canvas.setAttribute("aria-label", `${spec.label}走势，这个范围内暂无实测`);
      frame = null;
      return;
    }
    const padding = { top: 16, bottom: 29, left: kind === "steps" ? 44 : 37, right: 16 };
    const innerWidth = Math.max(1, width - padding.left - padding.right);
    const innerHeight = Math.max(1, height - padding.top - padding.bottom);
    const count = Math.max(1, end - start);
    const span = spec.bar ? count + 1 : count;
    const x = (i) => padding.left + ((i - start) + (spec.bar ? .5 : 0)) / span * innerWidth;
    const limits = axis([...rows, ...trend].map((p) => p.value / spec.scale), spec.minimum, spec.bar);
    const y = (v) => padding.top + innerHeight - (v / spec.scale - limits.min) / (limits.max - limits.min) * innerHeight;
    frame = { padding, innerWidth, span };

    ctx.lineWidth = 1;
    ctx.strokeStyle = "#e2e9e3";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let value = limits.min; value <= limits.max + limits.step / 100; value += limits.step) {
      const py = y(value * spec.scale);
      ctx.beginPath();
      ctx.moveTo(padding.left, py);
      ctx.lineTo(width - padding.right, py);
      ctx.stroke();
      const tick = Math.round(value * 100) / 100;
      const label = Math.abs(tick) >= 1000 ? `${fmt(tick / 1000, tick % 1000 ? 1 : 0)}k` : String(tick);
      ctx.fillText(label, padding.left - 7, py);
    }
    const ticks = Math.min(count, width > 430 ? 5 : 3);
    ctx.textBaseline = "top";
    for (let i = 0; i <= ticks; i++) {
      const d = start + Math.round(count * i / ticks);
      if (d > end) continue;
      ctx.textAlign = i === 0 ? "left" : i === ticks ? "right" : "center";
      ctx.fillText(shortDay(dayAt(d)), x(d), height - 20);
    }

    function line(points, color, dashed, dots) {
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 2;
      ctx.setLineDash(dashed ? [5, 4] : []);
      let previous = null;
      ctx.beginPath();
      for (const p of points) {
        const d = dayIndex(p.day);
        if (previous == null || (kind === "sleep" && d - previous > 1)) ctx.moveTo(x(d), y(p.value));
        else ctx.lineTo(x(d), y(p.value));
        previous = d;
      }
      ctx.stroke();
      ctx.setLineDash([]);
      if (dots) {
        for (const p of points) {
          ctx.beginPath();
          ctx.arc(x(dayIndex(p.day)), y(p.value), kind === "waist" ? 4 : rows.length > 35 ? 2 : 3, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }

    if (spec.bar) {
      const barWidth = Math.min(14, innerWidth / span * .7);
      for (const p of rows) {
        ctx.fillStyle = dayIndex(p.day) === end ? "#c5a881" : spec.color;
        ctx.fillRect(x(dayIndex(p.day)) - barWidth / 2, y(p.value), barWidth, y(0) - y(p.value));
      }
    } else {
      line(rows, spec.color, false, true);
      if (trend.length) line(trend, "#8b6737", true, false);
    }

    if (latestLabel) {
      const latest = rows.at(-1);
      const day = dayIndex(latest.day);
      const pointX = x(day), pointY = y(latest.value);
      const partial = spec.bar && day === end ? " · 今日部分" : "";
      latestLabel.textContent = `${shortDay(latest.day)} · ${fmt(latest.value / spec.scale, spec.digits)} ${spec.unit}${partial}`;
      latestLabel.hidden = focus != null;
      if (!latestLabel.hidden) {
        const left = Math.max(0, Math.min(pointX - latestLabel.offsetWidth / 2, width - latestLabel.offsetWidth));
        const top = pointY - latestLabel.offsetHeight - 8;
        latestLabel.style.left = `${left}px`;
        latestLabel.style.top = `${top >= 0 ? top : pointY + 8}px`;
        canvas.setAttribute("aria-label", `${spec.label}走势，最新数据 ${latestLabel.textContent}，可用方向键查看日期`);
      }
      ctx.beginPath();
      ctx.arc(pointX, pointY, 4, 0, Math.PI * 2);
      ctx.fillStyle = spec.color;
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    tip.hidden = focus == null;
    if (focus != null) {
      const day = dayAt(focus);
      const point = rows.find((p) => p.day === day);
      const smooth = trend.find((p) => p.day === day);
      ctx.strokeStyle = "#80958b";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x(focus), padding.top);
      ctx.lineTo(x(focus), height - padding.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      const actual = point ? `${fmt(point.value / spec.scale, spec.digits)} ${spec.unit}` : "无实测";
      const partial = spec.bar && focus === end && point ? " · 今日部分数据" : "";
      tip.textContent = `${shortDay(day)} · ${actual}${smooth ? ` · 7 日均值 ${fmt(smooth.value, 1)} kg` : ""}${partial}`;
      canvas.setAttribute("aria-label", `${spec.label}走势，${tip.textContent}`);
    }
  }

  new ResizeObserver(draw).observe(canvas);
  canvas.addEventListener("pointermove", (event) => {
    if (!frame) return;
    const point = (event.offsetX - frame.padding.left) / frame.innerWidth * frame.span;
    focus = Math.max(start, Math.min(end, start + Math.round(point - (spec.bar ? .5 : 0))));
    draw();
  });
  canvas.addEventListener("pointerleave", () => {
    focus = null;
    draw();
  });
  canvas.addEventListener("keydown", (event) => {
    if (!frame || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Home") focus = start;
    else if (event.key === "End") focus = end;
    else focus = Math.max(start, Math.min(end, (focus ?? end) + (event.key === "ArrowLeft" ? -1 : 1)));
    draw();
  });
  canvas.addEventListener("blur", () => {
    focus = null;
    draw();
  });

  return (payload) => {
    rows = payload.series[kind];
    trend = kind === "weight" ? payload.series.trend : [];
    start = dayIndex(payload.start);
    end = dayIndex(payload.end);
    focus = null;
    const compared = kind === "steps" ? rows.filter((p) => p.day !== payload.end) : rows;
    const value = average(compared);
    const summary = $("[data-summary]", card);
    if (!rows.length) summary.textContent = "暂无实测";
    else if (kind === "weight" && rows.length > 1) {
      summary.textContent = `区间首末 ${signed(rows.at(-1).value - rows[0].value, 1)} kg`;
    } else if (kind === "waist") {
      summary.textContent = `${rows.length} 个测量日`;
    } else {
      summary.textContent = compared.length ? `均值 ${fmt(value / spec.scale, spec.digits)} ${spec.unit} · ${compared.length} 天` : "暂无完整日";
    }
    draw();
  };
}

const charts = [...document.querySelectorAll("[data-chart]")].map(chart);
const sourceTitles = { "xunji-training": "训记 · 训练", "xunji-body": "训记 · 身体测量",
  "xunji-food": "训记 · 饮食", zepp: "Zepp · 睡眠与活动", "zepp-workout": "Zepp · 单次运动" };

const trainingLabels = { strength: "力量", cardio: "有氧", other: "其他" };

function renderIntakeGoals(summary, today) {
  const { day, metrics } = summary;
  const amount = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
  const goalItems = [
    ["每日热量", "calories_kcal", "kcal"], ["碳水", "carbs_g", "g"],
    ["蛋白质", "protein_g", "g"], ["脂肪", "fat_g", "g"],
  ];
  $("#intake-daily-goals").replaceChildren(...goalItems.map(([label, key, unit]) => {
    const item = element("div");
    const goal = metrics[key].goal;
    const value = element("dd", goal == null ? "未设置" : amount.format(goal));
    if (goal != null) value.append(element("span", ` ${unit}`, "intake-goal-unit"));
    item.append(element("dt", label), value);
    return item;
  }));
  const energy = metrics.calories_kcal;
  let basis = "暂无每日热量目标。";
  let formula = "";
  if (energy.estimated_goal) {
    const [carbs, protein, fat] = goalItems.slice(1).map(([, key]) => amount.format(metrics[key].goal));
    basis = "热量折算按碳水和蛋白质每克 4 kcal，脂肪每克 9 kcal。";
    formula = `${carbs} × 4 ＋ ${protein} × 4 ＋ ${fat} × 9 ＝ ${amount.format(energy.goal)} kcal`;
  } else if (energy.goal != null) {
    basis = "热量采用已设置的每日目标。";
  }
  const source = day && day < today ? `目标来源 · ${shortDay(day)} 训记记录` : "";
  $("#intake-goal-basis").textContent = basis;
  $("#intake-goal-formula").textContent = formula;
  $("#intake-goal-formula").hidden = !formula;
  $("#intake-goal-source").textContent = source;
  $("#intake-goal-source").hidden = !source;
}

function renderIntakeCalendar(calendar, today) {
  const [year, month] = calendar.month.split("-").map(Number);
  $("#intake-month").textContent = `${year} 年 ${month} 月 · 训记已记录摄入`;
  const amount = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
  $("#intake-days").replaceChildren(...calendar.days.map((entry) => {
    const cell = element("li", null, "week-day intake-day");
    if (!entry) {
      cell.classList.add("outside-month");
      cell.setAttribute("aria-hidden", "true");
      return cell;
    }
    const { day, actual, goal, logged, estimated_goal } = entry;
    const date = element("time", String(Number(day.slice(-2))), "week-date");
    date.dateTime = day;
    date.setAttribute("aria-label", day);
    if (day === today) date.setAttribute("aria-current", "date");
    cell.append(date);
    if (day > today) return cell;
    if (actual == null) {
      cell.append(element("p", logged ? "热量缺失" : "无记录", "intake-day-empty"));
      return cell;
    }
    cell.append(element("strong", amount.format(actual), "intake-day-actual"));
    if (goal == null) {
      cell.append(element("p", "目标缺失", "intake-day-empty"));
      return cell;
    }
    const difference = actual - goal;
    const direction = difference > 0 ? "超标" : difference < 0 ? "低于目标" : "达到目标";
    const magnitude = Math.abs(difference);
    const value = magnitude > 0 && magnitude < 0.05 ? "<0.1" : amount.format(magnitude);
    const signed = (difference > 0 ? "+" : difference < 0 ? "−" : "") + value;
    const delta = element("span", signed, "intake-day-delta");
    delta.setAttribute("aria-label", `${direction} ${value} kcal，${estimated_goal ? "折算目标" : "目标"} ${amount.format(goal)} kcal`);
    delta.title = `${estimated_goal ? "折算目标" : "目标"} ${amount.format(goal)} kcal`;
    cell.append(delta);
    if (difference > 0) {
      cell.classList.add("over-target");
      cell.append(element("span", "超标", "intake-day-flag"));
    }
    return cell;
  }));
}

function renderTrainingCalendar(calendar, today, plan) {
  const [year, month] = calendar.month.split("-").map(Number);
  $("#calendar-caption").textContent = `${year} 年 ${month} 月 · 本周 ${shortDay(calendar.week_start)}–${shortDay(calendar.week_end)}`;
  $("#week-summary").replaceChildren(...Object.entries(calendar.week_totals).map(([kind, count]) => {
    const item = element("div", null, "week-count");
    const goal = plan[`${kind}_sessions_per_week`];
    item.append(element("span", `本周${trainingLabels[kind]}训练`),
      element("strong", goal == null ? `${count} 场` : `${count} / ${goal}`));
    return item;
  }));
  $("#week-days").replaceChildren(...calendar.days.map((entry, index) => {
    const cell = element("li", null, "week-day");
    if (!entry) {
      cell.classList.add("outside-month");
      cell.setAttribute("aria-hidden", "true");
      return cell;
    }
    const { day, synced, events } = entry;
    const date = element("time", String(Number(day.slice(-2))), "week-date");
    date.dateTime = day;
    date.setAttribute("aria-label", `周${"一二三四五六日"[index % 7]} ${shortDay(day)}`);
    if (day === today) date.setAttribute("aria-current", "date");
    cell.append(date);
    if (events.length) {
      const list = element("ul", null, "week-events");
      list.setAttribute("role", "list");
      for (const { kind, title, source, distance_m, avg_heart_rate } of events) {
        const event = element("li", null, `week-event ${kind}`);
        const metrics = [];
        if (distance_m > 0) metrics.push(`${fmt(distance_m / 1000, 2)} km`);
        if (avg_heart_rate > 0) metrics.push(`平均心率 ${fmt(avg_heart_rate, 0)} 次/分`);
        event.append(element("span", trainingLabels[kind]), element("strong", title),
          element("small", source));
        if (metrics.length) event.append(element("small", metrics.join(" · "), "workout-metrics"));
        list.append(event);
      }
      cell.append(list);
    } else if (day <= today) {
      cell.append(element("p", synced ? "暂无记录" : "待同步", "week-empty"));
    }
    return cell;
  }));
}

const inJin = (kg) => kg == null ? null : kg * 2;
const measurementText = (value) => new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value);

function fatLossCard(label, unit, start, reading, goal, goalLabel) {
  const card = element("article", null, "panel fat-loss-card");
  const heading = element("div", null, "fat-loss-heading");
  heading.append(element("h3", label));
  const current = reading?.value;
  const percent = [start, current, goal].every(Number.isFinite) && start > goal
    ? Math.max(0, Math.min(100, (start - current) / (start - goal) * 100))
    : null;
  if (percent != null) heading.append(element("strong", `${measurementText(percent)}%`));
  const values = element("dl", null, "fat-loss-values");
  for (const [name, value] of [["起点", start], ["最新实测", current], [goalLabel, goal]]) {
    const entry = element("div");
    entry.append(element("dt", name), element("dd", value == null ? "—" : `${measurementText(value)} ${unit}`));
    values.append(entry);
  }
  card.append(heading, values);
  if (percent != null) {
    const bar = element("progress");
    bar.max = 100;
    bar.value = percent;
    bar.setAttribute("aria-label", `${label}减脂进度`);
    card.append(bar);
  }
  const notes = [];
  if (Number.isFinite(start) && Number.isFinite(current)) {
    const change = start - current;
    if (change) notes.push(`较起点${change > 0 ? "减少" : "增加"} ${measurementText(Math.abs(change))} ${unit}`);
  }
  if (Number.isFinite(current) && Number.isFinite(goal)) {
    notes.push(current > goal ? `距目标 ${measurementText(current - goal)} ${unit}` : "已达目标");
  }
  if (reading?.day) notes.push(`${shortDay(reading.day)} 实测`);
  card.append(element("p", notes.join(" · ") || "等待最新测量和目标数据"));
  return card;
}

function render(payload) {
  const { current: c, plan: p, plan_period: period } = payload;
  renderIntake(payload.intake);
  renderIntakeGoals(payload.intake_goal_summary, payload.end);
  renderIntakeCalendar(payload.intake_calendar, payload.end);
  const base = c.baseline_weight;
  const weight = inJin(c.weight?.value);
  const target = inJin(p.target_weight_kg);
  const delta = weight != null && base ? weight - inJin(base.value) : null;
  const gap = weight != null && target != null ? weight - target : null;
  const sleepDelta = c.prior_sleep_avg == null || c.sleep_avg == null ? null : Math.round(c.sleep_avg - c.prior_sleep_avg);
  const profile = [p.phase, p.height_cm ? `${fmt(p.height_cm, 0)} cm` : "", p.age == null ? "" : `${p.age} 岁`,
    p.estimated_body_fat_pct == null ? "" : `参考体脂 ${fmt(p.estimated_body_fat_pct, 0)}%（估算）`].filter(Boolean);
  $("#subtitle").textContent = profile.join(" · ") || "减脂计划与身体数据";
  $("#metrics").replaceChildren(
    metric(c.weight?.measurements > 1 ? "最近称重日均值" : "最新体重", weight == null ? "—" : measurementText(weight), "斤",
      c.weight ? `${shortDay(c.weight.day)} 实测 · 较 ${shortDay(base?.day)} ${signed(delta, 1)} 斤` : "暂无实测"),
    metric("目标体重", target == null ? "—" : measurementText(target), "斤",
      gap == null ? "等待当前体重与目标" : gap > 0 ? `距目标 ${measurementText(gap)} 斤` : "最新实测已到目标"),
    metric("最近实测腰围", fmt(c.waist?.value, 0), "cm", c.waist ? `沿用 ${shortDay(c.waist.day)} 的测量` : "暂无实测"),
    metric("近 7 日平均睡眠", fmt(c.sleep_avg == null ? null : c.sleep_avg / 60), "小时",
      `${c.sleep_days}/7 晚有效${sleepDelta == null ? "" : ` · 较前周 ${signed(sleepDelta, 0)} 分`}`),
  );
  $("#fat-loss-progress").replaceChildren(
    fatLossCard("体重", "斤", inJin(p.start_weight_kg), c.weight && { ...c.weight, value: weight }, target, "目标"),
    fatLossCard("腰围", "cm", p.start_waist_cm, c.waist, p.target_waist_cm, "暂定目标"),
  );
  $("#range-caption").textContent = `${payload.start} 至 ${payload.end} · 跟踪起点 ${payload.baseline}`;
  $("#review-caption").textContent = `近 7 个完整日 · ${shortDay(period.start)}–${shortDay(period.end)}`;
  renderTrainingCalendar(payload.training_calendar, payload.end, p);

  const sessionPanel = $("#sessions");
  sessionPanel.replaceChildren();
  for (const item of payload.sessions) {
    const row = element("div", null, "session");
    const details = element("div");
    details.append(element("strong", item.title), element("p", `${item.working_sets} 个已完成正式组`));
    const heart = item.heart;
    row.append(element("span", shortDay(item.date)), details, element("span",
      heart?.minutes ? `均值 ${fmt(heart.avg_bpm, 0)} 次/分 · ${heart.minutes}/${heart.window_minutes} 分钟` : "心率未匹配", "right"));
    sessionPanel.append(row);
  }
  if (!payload.sessions.length) sessionPanel.append(element("p", "这段时间暂无可统计的力量训练。"));
  const comparison = $("#comparison");
  comparison.replaceChildren();
  if (payload.comparison.length) {
    comparison.append(element("p", "相同次数的最高负重，前周 → 本周"));
    for (const lift of payload.comparison) {
      comparison.append(element("p", `${lift.name} · ${lift.reps} 次 · ${fmt(lift.previous, 1)} → ${fmt(lift.current, 1)} kg`));
    }
  }
  const recovery = $("#recovery");
  recovery.className = "recovery";
  recovery.replaceChildren(element("p", `睡眠 ${sleepLabel(c.sleep_avg)} · ${c.sleep_days}/7 晚有效`),
    element("p", `静息心率均值 ${fmt(c.resting_avg)} 次/分 · ${c.resting_days}/7 天有效`));
  const guidance = $("#guidance");
  guidance.replaceChildren();
  for (const finding of payload.guidance) {
    const item = element("div", null, "guidance");
    item.append(element("h4", finding.title), element("p", finding.text));
    guidance.append(item);
  }
  const sources = $("#source-grid");
  sources.replaceChildren();
  for (const item of payload.sources) {
    const card = element("article", null, "source");
    const stamp = item.observed_at
      ? new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(item.observed_at))
      : "待采集";
    card.append(element("h3", sourceTitles[item.source]), element("p", item.last ? `入账日期截至 ${item.last}` : "尚无入账日期"),
      element("p", `最近入账 ${stamp}`));
    if (item.run && item.run.status !== "ok") card.append(element("p", item.run.detail));
    sources.append(card);
  }
  $("#dashboard").hidden = false;
  for (const redraw of charts) redraw(payload);
  $("#status").textContent = "已读取云端总账 · 北京时间";
}

let activeRange = "all";
let requestId = 0;
const pages = [...document.querySelectorAll("[data-page]")];
const pageLinks = [...document.querySelectorAll("#pages a")];

function showPage() {
  const hash = location.hash.slice(1);
  const page = pages.find((panel) => panel.dataset.page === hash) ?? pages[0];
  for (const panel of pages) panel.hidden = panel !== page;
  for (const link of pageLinks) {
    if (link.hash === `#${page.dataset.page}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

async function refresh() {
  const id = ++requestId;
  try {
    const response = await fetch(`/api/dashboard?range=${activeRange}`, { cache: "no-store" });
    if (!response.ok) throw new Error("数据暂时不可读取");
    const data = await response.json();
    if (requestId !== id) return;
    $("#error").hidden = true;
    render(data);
  } catch {
    if (requestId !== id) return;
    $("#error").textContent = "暂时读不到云端总账，请检查连接后刷新。";
    $("#error").hidden = false;
    $("#status").textContent = "未能刷新";
  }
}

window.addEventListener("hashchange", () => {
  showPage();
  window.scrollTo(0, 0);
});
showPage();
for (const button of document.querySelectorAll("[data-range]")) {
  button.addEventListener("click", () => {
    activeRange = button.dataset.range;
    for (const other of document.querySelectorAll("[data-range]")) other.setAttribute("aria-pressed", String(other === button));
    refresh();
  });
}
$("#refresh").addEventListener("click", refresh);
$("#sync-xunji").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const feedback = $("#sync-feedback");
  button.disabled = true;
  feedback.hidden = false;
  feedback.textContent = "正在同步近 7 天训记数据，请稍候…";
  try {
    const response = await fetch("/api/sync/xunji", {
      method: "POST",
      headers: { "X-Ledger-Action": "sync-xunji" },
    });
    const result = await response.json();
    feedback.textContent = result.message;
    if (response.ok || response.status === 502) await refresh();
  } catch {
    feedback.textContent = "暂时无法确认同步结果，请查看数据同步页。";
  } finally {
    button.disabled = false;
  }
});
setInterval(() => { if (!document.hidden) refresh(); }, 60000);
refresh();
