from pt_shualiu.mailer import build_summary
from pt_shualiu.sites import MyhrReport, AssessmentMetric, HrRecord

def test_build_summary_contains_metrics():
    rep = MyhrReport(
        assessment=[AssessmentMetric("上传增量", "50 GB", "76.19 MB", False),
                    AssessmentMetric("下载增量", "50 GB", "0.00 KB", False)],
        hr=[HrRecord("carpt", 1, "X", 3600, None, "考察中")])
    s = build_summary({"carpt": rep}, {"carpt": {"in_seed": 5}})
    assert "上传增量" in s and "76.19 MB" in s and "未通过" in s
    assert "in_seed" not in s or "5" in s


def test_build_summary_added_deleted_and_hr_obligation():
    # Fix round 3（I5）：邮件须含当日新增/删除与 H&R 义务（设计 §4.7）
    rep = MyhrReport(
        assessment=[],
        hr=[HrRecord("carpt", 1, "X", 3600, None, "考察中"),
            HrRecord("carpt", 2, "Y", 0, None, "考察中")])
    s = build_summary({"carpt": rep},
                      {"carpt": {"in_seed": 5, "added": 3, "deleted": 1}})
    assert "今日新增: 3" in s and "今日删除: 1" in s
    assert "H&R 义务: 1 条" in s  # 仅 remaining>0 计入义务


def test_build_summary_warnings_section():
    # Fix round 3（I5）：告警（cookie 失效 / 站点异常 / 磁盘告急）渲染为独立段落
    s = build_summary({}, {}, warnings=["站点 btschool myhr 获取失败：timeout",
                                        "磁盘剩余 2.10GB 低于阈值 3GB"])
    assert "告警" in s and "btschool" in s and "磁盘剩余" in s
    s2 = build_summary({}, {})  # 无告警 → 不渲染告警段
    assert "告警" not in s2

def test_send_mail_uses_ssl(tmp_path, monkeypatch):
    # 用 fake smtplib 断言走 465 SSL：只替换 SMTP_SSL，若实现退化为非 SSL 的 SMTP，
    # FakeSMTP 不会被调用 → sent["ssl"] 缺失 → 断言失败（与计划代码 SMTP_SSL 一致，无 context 参数）。
    import pt_shualiu.mailer as m
    sent = {}
    class FakeSMTP:
        def __init__(self, *a, **k): sent["ssl"] = True
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, msg): sent["msg"] = msg
    monkeypatch.setattr(m.smtplib, "SMTP_SSL", FakeSMTP)
    from pt_shualiu.config import Config
    cfg = Config(sites=[], qbit_url="", qbit_user="", qbit_pass="",
                 mail_host="smtp.163.com", mail_port=465,
                 mail_user="me@163.com", mail_auth_code="code", mail_to="me@163.com")
    m.send_mail("subject", "body", cfg)
    assert sent["ssl"] and sent["login"] == ("me@163.com", "code")
