import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

const cli = join(homedir(), "xunji", "xunji.py");
const dayPattern = "^\\d{4}-\\d{2}-\\d{2}$";
const dateSchema = { type: "string", pattern: dayPattern, description: "中国时间 YYYY-MM-DD" };

function run(args, input) {
  return new Promise((resolve) => {
    const child = execFile("/usr/bin/python3", ["-B", cli, ...args], {
      encoding: "utf8",
      timeout: 25000,
      maxBuffer: 512 * 1024,
    }, (error, stdout, stderr) => {
      if (error) {
        resolve({ ok: false, text: stderr.trim().slice(0, 500) || "训记工具执行失败或超时。" });
      } else {
        resolve({ ok: true, text: stdout.trim() });
      }
    });
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

function toolResult(result) {
  return { content: [{ type: "text", text: result.text }], isError: !result.ok };
}

export default {
  id: "life-tracker",
  name: "训记训练助手",
  register(api) {
    api.registerTool({
      name: "life_tracker_training_read",
      description: "按中国时间查询指定日期的训记训练。只在用户明确要求时调用。",
      parameters: {
        type: "object",
        properties: { date: dateSchema },
        required: ["date"],
        additionalProperties: false,
      },
      async execute(_id, params) {
        return toolResult(await run(["read", params.date]));
      },
    });

    api.registerTool({
      name: "life_tracker_strength_preview",
      description: "将用户报告的真实力量训练保存为本地预览。快走、跑步等有氧运动请使用 life_tracker_cardio_preview。返回预览 ID 及全部组次，展示给用户。需要核实的内容放进 uncertainties。正式写回由用户通过服务器终端手动确认。",
      parameters: {
        type: "object",
        properties: {
          date: dateSchema,
          title: { type: "string" },
          movements: {
            type: "array",
            minItems: 1,
            maxItems: 15,
            items: {
              type: "object",
              properties: {
                name: { type: "string", description: "训记官方中文动作名" },
                sets: {
                  type: "array",
                  minItems: 1,
                  maxItems: 20,
                  items: {
                    type: "object",
                    properties: {
                      weight: { type: "number", minimum: 0 },
                      unit: { enum: ["kg", "lb"] },
                      reps: { type: "integer", minimum: 0 },
                      rpe: { type: "number", minimum: 6, maximum: 10, multipleOf: 0.5 },
                      done: { type: "boolean" },
                    },
                    required: ["weight", "unit", "reps"],
                    additionalProperties: false,
                  },
                },
              },
              required: ["name", "sets"],
              additionalProperties: false,
            },
          },
          symptoms: { type: "array", items: { type: "string" } },
          uncertainties: { type: "array", items: { type: "string" } },
        },
        required: ["date", "title", "movements"],
        additionalProperties: false,
      },
      async execute(_id, params) {
        return toolResult(await run(["preview", "-"], JSON.stringify(params)));
      },
    });

    api.registerTool({
      name: "life_tracker_cardio_preview",
      description: "生成训记原生有氧草稿，快走使用 walking。按原始记录填距离、时长等指标，省略缺失指标。向用户展示草稿和预览 ID。正式写回由用户通过服务器终端手动确认。",
      parameters: {
        type: "object",
        properties: {
          date: dateSchema,
          title: { type: "string" },
          cardio: {
            type: "object",
            properties: {
              preset: { type: "string", enum: ["general", "running", "walking", "cycling", "swimming", "jumpRope", "hiit"] },
              duration_s: { type: "integer", minimum: 1 },
              distance_km: { type: "number" },
              pace: { type: "string", description: "分:秒，例如 14:17" },
              kcal: { type: "number" },
              avg_bpm: { type: "number" },
            },
            required: ["preset", "duration_s"],
            additionalProperties: false,
          },
          uncertainties: { type: "array", items: { type: "string" } },
        },
        required: ["date", "title", "cardio"],
        additionalProperties: false,
      },
      async execute(_id, params) {
        return toolResult(await run(["preview", "-"], JSON.stringify(params)));
      },
    });

    api.registerCommand({
      name: "xunji",
      description: "按日期查询训记训练，例如 /xunji 2026-11-09",
      acceptsArgs: true,
      requireAuth: true,
      async handler(ctx) {
        const day = ctx.args?.trim();
        if (!day || !new RegExp(dayPattern).test(day)) {
          return { text: "请输入 /xunji YYYY-MM-DD。" };
        }
        const result = await run(["read", day]);
        if (!result.ok) {
          return { text: result.text };
        }
        const records = JSON.parse(result.text);
        const titles = records.map((record) => record.title || "未命名训练").slice(0, 20);
        return { text: [`${day} 共 ${records.length} 条训练。`, ...titles].join("\n") };
      },
    });
  },
};
