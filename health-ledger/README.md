# 个人身体数据总账

`ledger.py` 定时只读采集训记与 Zepp，按用户、来源和日期保存 SQLite 快照。默认状态目录为 `.state/`。`dashboard.py` 从 SQLite 读取总览、训练日历、饮食、身体趋势、训练恢复和同步状态。日历合并时间重叠、类型相同且唯一匹配的训练。

## 云端部署

示例 systemd 单元以 `xunji-agent` 运行，项目目录为 `/home/xunji-agent/health-ledger`，同级保留 `xunji/` 目录。按实际目录调整单元。

- 训记训练、身体、饮食 Key 放在服务用户的 `~/.codex/secrets/xunji.env`，权限设为 600。对应变量为 `XUNJI_TRAIN_KEY`、`XUNJI_BODY_KEY`、`XUNJI_FOOD_KEY`。
- 在 `health-ledger/.venv` 中安装 Zepp 采集依赖 `huami-token`、`loguru`、`requests`、`zepp-export`，以及备份依赖 `pycryptodome`。以服务用户运行 `.venv/bin/python zepp_cloud.py --setup` 授权。将 `.state/zepp-sync` 配置为可执行的转发脚本，通过该虚拟环境执行 `zepp_cloud.py` 并转交全部参数。
- `.state/profile.json` 用 JSON 数组存储计划版本，按 `effective_from` 生效。可设置体重和腰围起点与目标，以及 `strength_sessions_per_week`、`cardio_sessions_per_week`。
- 看板默认监听 `127.0.0.1:18790`。跨设备访问时配置带登录认证的 HTTPS 反向代理。
- 微信推送通过 OpenClaw 已绑定的唯一微信账号发送。在服务器的 `/etc/health-ledger/dashboard.env` 中配置 `HEALTH_DASHBOARD_URL=https://dashboard.example.com/#overview`，替换为实际看板地址。手动运行日报或周报命令时，同样需要提供该环境变量。

## 定时任务

示例单元位于 `systemd/`。时间均为北京时间。

| 时间 | 任务 |
| --- | --- |
| 每日 10:00 | 同步近期训记和 Zepp 数据 |
| 06:15–23:45 每半小时 | 补拉当天饮食 |
| 每日 11:00 | 微信日报，附看板链接 |
| 周六 11:00 | 微信周报与加密备份 |

可用 `python3 ledger.py status` 核对数据覆盖与同步状态，或用 `python3 ledger.py daily-report --dry-run` 校验微信投递参数。
