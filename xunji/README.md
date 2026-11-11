# Codex × 训记

用 Codex 将口述训练整理成结构化草稿。本地命令行校验动作名、保存预览，并在你手动确认后新增一条力量训练。训记返回记录 ID 后按日期回查。

## 配置与只读验证

在训记 App 申请训练数据 Open API Key。用终端创建凭据文件，并在编辑器内把真实 Key 填入 `XUNJI_TRAIN_KEY=` 后面，整行无需引号。保持文件权限为 600。

```sh
mkdir -p ~/.codex/secrets
(umask 077; touch ~/.codex/secrets/xunji.env)
chmod 600 ~/.codex/secrets/xunji.env
nano ~/.codex/secrets/xunji.env
```

在 `human3.0` 目录运行，日期换成你已经有训练记录的一天。

```sh
python3 xunji/xunji.py read 2026-11-10
```

读取采用完整模式，同日缓存 30 秒。首次只读成功后再试写回。

## 口述、预览与回查

告诉 Codex 实际训练的日期、动作、重量、单位、各组次数。Codex 可调用 `preview -`，将整理出的 JSON 从标准输入交给工具，并把预览展示给你。口述存在歧义时，由 Codex 向你核实。以下内容只用于演示预览。

```sh
python3 xunji/xunji.py preview - <<'JSON'
{
  "date": "2026-11-10",
  "title": "上肢力量",
  "movements": [
    {
      "name": "杠铃卧推",
      "sets": [
        {"weight": 40, "unit": "kg", "reps": 10, "rpe": 8},
        {"weight": 40, "unit": "kg", "reps": 9}
      ]
    }
  ]
}
JSON
```

确认真实训练内容后，在本机终端执行 `commit`，将 `PREVIEW_ID` 换成预览给出的完整 ID。核对终端展示的内容后，手动输入对应的确认短语。

```sh
python3 xunji/xunji.py commit PREVIEW_ID
python3 xunji/xunji.py status PREVIEW_ID
python3 xunji/xunji.py verify PREVIEW_ID
```

- `preview` 已保存草稿。修改内容会生成新的 ID。存在 `uncertainties` 时须核实并生成新预览。
- `sent` 训记已返回 ID。若回查限频，稍等后执行 `verify`。
- `verified` 当日列表已找到对应 ID，可在 App 检查训练详情。
- `accepted` 训记报告成功但没有可识别的 ID。请在 App 核对，工具会阻止对该预览再次写入。

如果写入请求超时，先在 App 核对。重试时使用同一预览和请求 ID。症状和不确定项保存在本地草稿。首版写回动作、重量、次数、单位、完成状态以及已提供的 RPE。RPE 支持 6 至 10，步长为 0.5。

本地数据位于 `xunji/.state/`。动作目录取自训记公开目录，共 1,213 条，来源 blob 为 `0486bbd2caf159e5462336cf5367bea7878402bc`。[训记动作名目录](https://github.com/Foveluy/Xunji-movements)
