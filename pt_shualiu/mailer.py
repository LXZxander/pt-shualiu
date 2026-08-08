"""163 邮件摘要（复用 seedhub 的 SMTP_SSL 模式，授权码走 .env）。"""
import smtplib
from email.mime.text import MIMEText
from email.header import Header

def build_summary(site_reports, stats, warnings=None) -> str:
    lines = ["PT刷流日报", "=" * 20, ""]
    for site, rep in site_reports.items():
        lines.append(f"【{site}】")
        for a in rep.assessment:
            status = "已通过" if a.passed else "未通过"
            mark = "✅" if a.passed else "❌"
            # name 取解析出的真实指标名（Task 5），不硬编码；BT 合成达标项 passed=True
            # 且 current==required，直接以 passed 旗标渲染，无需解析字符串差值
            lines.append(f"  {mark} {a.name}: {a.current} / {a.required}（{status}）")
        st = stats.get(site, {})
        lines.append(f"  在保种子: {st.get('in_seed', 0)}")
        # Fix round 3（I5）：设计 §4.7 要求当日新增/删除 + H&R 状态
        hr_oblig = sum(1 for r in rep.hr if r.remaining_seed_sec > 0)
        lines.append(f"  H&R 义务: {hr_oblig} 条")
        lines.append(f"  今日新增: {st.get('added', 0)}  今日删除: {st.get('deleted', 0)}")
        lines.append("")
    if warnings:
        lines.append("告警：")
        for w in warnings:
            lines.append(f"  ⚠️ {w}")
        lines.append("")
    return "\n".join(lines)

def send_mail(subject, body, cfg):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg.mail_user
    msg["To"] = cfg.mail_to
    with smtplib.SMTP_SSL(cfg.mail_host, cfg.mail_port, timeout=30) as s:
        s.login(cfg.mail_user, cfg.mail_auth_code)
        s.send_message(msg)
