"""Shared WeChat transport and persisted delivery attempts."""
import json
from pathlib import Path
import subprocess
from uuid import uuid4

from ledger import DASHBOARD_URL, USER, DeliveryUnknown, SourceError, now


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
    if not dry_run:
        params = {"channel": "openclaw-weixin", "accountId": account, "to": recipient,
                  "message": body, "idempotencyKey": str(uuid4())}
        command = [str(cli), "gateway", "call", "send", "--json", "--timeout", "45000",
                   "--params", json.dumps(params, ensure_ascii=False)]
    if dry_run:
        command.append("--dry-run")
    try:
        result = subprocess.run(command, capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        raise DeliveryUnknown("微信投递状态未知") from None
    if result.returncode:
        output = result.stdout + result.stderr
        if b"prepare failed" in output:
            raise SourceError("微信拒绝投递，请先在微信中激活机器人会话")
        if b"timeout" in output.lower() or b"timed out" in output.lower():
            raise DeliveryUnknown("微信投递状态未知")
        raise SourceError("微信投递失败")

    if not dry_run:
        try:
            receipt = json.loads(result.stdout)
            message_id = receipt.get("messageId")
        except (ValueError, AttributeError):
            raise DeliveryUnknown("网关响应无法确认投递") from None
        if not isinstance(message_id, str) or not message_id.strip():
            raise DeliveryUnknown("网关未返回有效消息编号")


def deliver_report(db, kind, period, body, *, resend=False):
    table, column = {"daily": ("daily_deliveries", "day"), "weekly": ("deliveries", "week_end")}[kind]
    if not resend and db.execute(f"SELECT 1 FROM {table} WHERE user_id=? AND {column}=?",
                                 (USER, period)).fetchone():
        print("报告已发送")
        return
    if kind == "weekly":
        if not DASHBOARD_URL:
            raise SourceError("请配置 HEALTH_DASHBOARD_URL")
        body = f"{body.rstrip()}\n\n身体总账\n{DASHBOARD_URL}"
    with db:
        attempt = db.execute("""INSERT INTO report_attempts (user_id,kind,period,attempted_at,status,detail)
                                VALUES (?,?,?,?,'sending','')""", (USER, kind, period, now())).lastrowid
    try:
        send_wechat(body)
    except (OSError, ValueError, SourceError) as error:
        status = "unknown" if isinstance(error, DeliveryUnknown) else "failed"
        detail = str(error) if isinstance(error, SourceError) else "无法读取投递配置或启动网关命令"
        with db:
            db.execute("UPDATE report_attempts SET status=?,detail=? WHERE id=?", (status, detail, attempt))
        raise SourceError(detail) from None
    with db:
        db.execute(f"""INSERT INTO {table} VALUES (?,?,?)
                       ON CONFLICT(user_id, {column}) DO UPDATE SET sent_at=excluded.sent_at""", (USER, period, now()))
        db.execute("UPDATE report_attempts SET status='accepted' WHERE id=?", (attempt,))
    print("网关已接受报告")
