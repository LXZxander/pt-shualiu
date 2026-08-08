from datetime import datetime, timezone

import pytest
from pt_shualiu.config import Config
from pt_shualiu.daemon import App, main

# 其他测试（如 test_config）会 load_dotenv 把凭证键写进 os.environ 且不还原；
# 本测试要验证"无 .env 时报错"，须先清掉这些键，否则 load_config 会读到被污染的
# 环境变量、连上真实 qbit 而抛 RuntimeError（full-suite 顺序执行时必现）。
_CRED_KEYS = ["CARPT_COOKIE", "BTSCHOOL_COOKIE", "CARPT_RSS_URL", "BTSCHOOL_RSS_URL",
              "QBIT_URL", "QBIT_USER", "QBIT_PASS", "MAIL_USER", "MAIL_AUTH_CODE", "MAIL_TO"]

# 本地时区 08:00（mail_send_time=09:00 之前），确保 loop_once 的邮件分支不触发。
# 用 naive datetime：now.astimezone() 把 naive 当本地时间，测试不依赖机器时区。
_NOW = datetime(2026, 1, 1, 8, 0)

def _app(dry_run):
    """App.__new__ 构造（绕过 __init__ 的网络/DB 副作用），只留 loop_once 需要的字段。"""
    app = App.__new__(App)
    app.cfg = Config(sites=[], qbit_url="", qbit_user="", qbit_pass="")
    app.dry_run = dry_run
    app.last_selection = float("inf")   # 跳过选种分支
    app.last_seeding = float("inf")     # 跳过保种分支
    app.last_mail = None
    app.run_mail = lambda now: None     # 默认惰性，防邮件分支误触真实 run_mail/state
    return app

def test_check_dry_run_without_creds(tmp_path, monkeypatch):
    # 无 .env 时应报错而非静默
    monkeypatch.chdir(tmp_path)
    for k in _CRED_KEYS:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(KeyError):
        main(["--check"])

def test_dry_run_loop_once_does_not_touch_rates():
    # Fix round 1：dry-run 不得调用 sync_maindata / rates.run_once——
    # 否则会对真实 qbit 执行 set_global_limits 并写 ratelimit 行（预览不应有副作用）。
    app = _app(dry_run=True)
    class FakeQbit:
        def __init__(self): self.calls = []
        def sync_maindata(self):
            self.calls.append("sync_maindata")
            return {"up_info_speed": 0, "dl_info_speed": 0}
    class FakeRates:
        def __init__(self): self.calls = []
        def run_once(self, up, down, now):
            self.calls.append("run_once")
    app.qbit = FakeQbit()
    app.rates = FakeRates()
    app.loop_once(_NOW)
    assert app.qbit.calls == []
    assert app.rates.calls == []

def test_loop_once_drives_rates_when_not_dry_run():
    # 正控：非 dry-run 必须照常驱动限速（防把整个限速步骤误删）。
    app = _app(dry_run=False)
    class FakeQbit:
        def __init__(self): self.calls = []
        def sync_maindata(self):
            self.calls.append("sync_maindata")
            return {"up_info_speed": 10, "dl_info_speed": 20}
    class FakeRates:
        def __init__(self): self.calls = []
        def run_once(self, up, down, now):
            self.calls.append((up, down))
    app.qbit = FakeQbit()
    app.rates = FakeRates()
    app.loop_once(_NOW)
    assert app.qbit.calls == ["sync_maindata"]
    assert app.rates.calls == [(10, 20)]

def test_mail_catches_straddled_clock_and_once_per_day():
    # Fix round 2 Finding 1：循环采样网格跨过 09:00 时，精确相等门限会当日漏发邮件。
    # 改为 >= 门限：08:59 采样未到 → 09:01 采样（已跨过）触发；同日 09:59 不再重发
    # （date guard）；次日 09:00 再触发。全部用 naive 本地时刻，测试不依赖机器时区。
    app = _app(dry_run=True)
    calls = []
    app.run_mail = lambda now: calls.append(now)
    app.loop_once(datetime(2026, 1, 1, 8, 59, 50))     # 未到 09:00
    assert calls == []
    app.loop_once(datetime(2026, 1, 1, 9, 1, 50))      # 已跨过 09:00 → 触发
    assert len(calls) == 1 and app.last_mail == "2026-01-01"
    app.loop_once(datetime(2026, 1, 1, 9, 59, 0))      # 同日 → date guard 不重发
    assert len(calls) == 1
    app.loop_once(datetime(2026, 1, 2, 9, 0, 10))      # 次日 → 再触发
    assert len(calls) == 2 and app.last_mail == "2026-01-02"
