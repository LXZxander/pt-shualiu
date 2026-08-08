from datetime import datetime, timezone, timedelta
from pt_shualiu.seeding import SeedingManager
from pt_shualiu.config import Config
from pt_shualiu.state import State
from pt_shualiu.sites import MyhrReport, HrRecord, MyhrParseError

NOW = datetime(2026, 8, 7, 20, 0, 0, tzinfo=timezone.utc)

class FakeQbit:
    def __init__(self, torrents, free_space=20*1024**3):
        self.torrents = torrents; self.deleted = []
        self.free_space = free_space
    def all_pt_torrents(self, domains): return list(self.torrents)
    def delete_torrents(self, hashes, delete_files=True): self.deleted.extend(hashes)
    def sync_maindata(self): return {"free_space_on_disk": self.free_space}

class FakeSite:
    def __init__(self, hr): self.hr = hr
    def fetch_myhr(self): return MyhrReport(assessment=[], hr=self.hr)

def _cfg():
    return Config(sites=[], qbit_url="", qbit_user="", qbit_pass="")

def _torrent(hash_, completed_at=None, site="carpt", uploaded=1000, state="uploading", category="pt_free", size=100*1024**2, tags=None):
    completion_on = completed_at.timestamp() if completed_at else 0
    return {"hash": hash_, "site": site, "category": category, "progress": 1.0,
            "completion_on": completion_on, "uploaded": uploaded,
            "state": state, "name": f"N-{hash_}", "size": size, "tags": tags or []}

def _mgr(qbit, hr_list, tmp_path):
    state = State(tmp_path / "t.db")
    return SeedingManager(qbit, {"carpt": FakeSite(hr_list)}, state, _cfg())

def _seed_upload_snapshot(mgr, hash_, uploaded, changed_ago_min):
    # 预置上传快照：标记该种 uploaded 已"一段时间未变"
    mgr.state.update_torrent(hash_, last_uploaded=uploaded,
                             last_upload_change=NOW.timestamp() - changed_ago_min * 60)

def test_ingest_existing_registers(tmp_path):
    q = FakeQbit([_torrent("h1", NOW - timedelta(hours=2))])
    s = _mgr(q, [], tmp_path)
    s.ingest_existing()
    assert "h1" in s.known

def test_ingest_preserves_state_on_restart(tmp_path):
    # ⚠️ 控制器修正（Task 1 minor 指针）：重启 re-ingest 已存在行必须走 update_torrent，
    # 不得 INSERT OR REPLACE 清空 last_uploaded/last_upload_change——否则"达标+10分钟无上传"
    # 判定每次重启都会重置计时。
    q = FakeQbit([_torrent("h1", NOW - timedelta(hours=2), uploaded=1000)])
    s = _mgr(q, [], tmp_path)
    s.ingest_existing()                                  # 首次摄入（新行 upsert）
    _seed_upload_snapshot(s, "h1", 1000, changed_ago_min=20)
    s.ingest_existing()                                  # 模拟重启再次摄入
    st = s.state.get_torrent("h1")
    assert st["last_uploaded"] == 1000
    assert st["last_upload_change"] == NOW.timestamp() - 20 * 60  # 未被重置
    assert st["added_at"] is not None and st["added_at"] > 0

def test_site_met_deletes_when_no_upload(tmp_path):
    # myhr 里该种还需做种时间=0 → 达标；上传 20 分钟未变 → 删
    q = FakeQbit([_torrent("h1", NOW - timedelta(hours=30), uploaded=1000)])
    hr = [HrRecord(site="carpt", site_id=192915, name="N-h1",
                   remaining_seed_sec=0, complete_dt=None, status="考察中")]
    mgr = _mgr(q, hr, tmp_path)
    mgr.ingest_existing()
    _seed_upload_snapshot(mgr, "h1", 1000, changed_ago_min=20)
    res = mgr.check(NOW)
    assert any(d.hash == "h1" and d.reason == "达标且10分钟无上传" for d in res)

def test_not_met_keeps(tmp_path):
    q = FakeQbit([_torrent("h1", NOW - timedelta(hours=5), uploaded=1000)])
    hr = [HrRecord(site="carpt", site_id=192915, name="N-h1",
                   remaining_seed_sec=24*3600, complete_dt=None, status="考察中")]
    mgr = _mgr(q, hr, tmp_path)
    mgr.ingest_existing()
    assert mgr.check(NOW) == []

def test_site_lag_override(tmp_path):
    # completion + window(24h) + 30min 已过，但 myhr 仍显示未达标 → 直接删
    q = FakeQbit([_torrent("h1", NOW - timedelta(hours=30), uploaded=1000)])
    hr = [HrRecord(site="carpt", site_id=192915, name="N-h1",
                   remaining_seed_sec=3600, complete_dt=None, status="考察中")]  # 网站没刷新
    mgr = _mgr(q, hr, tmp_path)
    mgr.ingest_existing()
    res = mgr.check(NOW)
    assert any(d.hash == "h1" and d.reason == "站点30分钟未刷新兜底" for d in res)

def test_site_lag_override_missing_completion(tmp_path):
    # completion_on 缺失(0) + hr 剩余>0 → 兜底必须 fail-closed（不得误删有义务种子）
    q = FakeQbit([_torrent("h1", None, uploaded=1000)])
    hr = [HrRecord(site="carpt", site_id=192915, name="N-h1",
                   remaining_seed_sec=3600, complete_dt=None, status="考察中")]
    mgr = _mgr(q, hr, tmp_path)
    mgr.ingest_existing()
    res = mgr.check(NOW)
    assert not any(d.reason == "站点30分钟未刷新兜底" for d in res)

def test_disk_pressure_met_first(tmp_path):
    # ⚠️ Fix round 3（I6）：磁盘压力候选集收紧为"达标且无上传"。h_met 预置上传快照使其
    # 为真·无上传（主循环即判"达标且10分钟无上传"删），未达标大种排最后——若磁盘压力忽略
    # 主循环已判删所释放的空间、直接删未达标大种（制造 H&R），此测试能区分。
    small = _torrent("h_met", NOW - timedelta(hours=30), uploaded=1000, size=600*1024**2)
    big = _torrent("h_notmet", NOW - timedelta(hours=5), uploaded=1000, size=2*1024**3)
    q = FakeQbit([small, big], free_space=int(2.5*1024**3))
    hr = [
        HrRecord(site="carpt", site_id=192915, name="N-h_met",
                 remaining_seed_sec=0, complete_dt=None, status="考察中"),
        HrRecord(site="carpt", site_id=192916, name="N-h_notmet",
                 remaining_seed_sec=86400, complete_dt=None, status="考察中"),
    ]
    mgr = _mgr(q, hr, tmp_path)
    mgr.ingest_existing()
    _seed_upload_snapshot(mgr, "h_met", 1000, changed_ago_min=20)  # 真·无上传
    res = mgr.check(NOW)
    assert any(d.hash == "h_met" for d in res)          # 达标种被删（主循环或磁盘压力）
    assert not any(d.hash == "h_notmet" for d in res)    # 未达标大种不得删（磁盘满 > H&R 例外兜底仅在达标种不够时）

def test_disk_pressure_unknown_site_fail_closed(tmp_path):
    # myhr 抓取失败站的种子义务未知 → _is_met 必须 fail-closed（按未达标排最后）。
    # 磁盘<3GB 时应先删已知达标+无上传种，未知义务种排最后（本例删达标种即达标，未知种不动）。
    # 旧实现（未知站 return True）会先删 2GB 未知种，此测试能区分。
    met = _torrent("h_met", NOW - timedelta(hours=30), site="carpt", uploaded=1000, size=600*1024**2)
    unk = _torrent("h_unk", NOW - timedelta(hours=30), site="btschool", uploaded=1000, size=2*1024**3)
    q = FakeQbit([met, unk], free_space=int(2.5*1024**3))
    hr = [HrRecord(site="carpt", site_id=192915, name="N-h_met",
                   remaining_seed_sec=0, complete_dt=None, status="考察中")]
    state = State(tmp_path / "t.db")
    mgr = SeedingManager(q, {"carpt": FakeSite(hr), "btschool": RaisingSite([])}, state, _cfg())
    mgr.ingest_existing()
    _seed_upload_snapshot(mgr, "h_met", 1000, changed_ago_min=20)  # 真·无上传
    res = mgr.check(NOW)
    assert any(d.hash == "h_met" for d in res)          # 已知达标种被删
    assert not any(d.hash == "h_unk" for d in res)       # 未知义务种不得删

class RaisingSite(FakeSite):
    def fetch_myhr(self):
        raise RuntimeError("myhr timeout")

def test_myhr_partial_failure_keeps_other_site(tmp_path):
    # 一站 myhr 抛异常 → check() 不抛、该站种子跳过（不做删除）；另一站照常判定
    q = FakeQbit([
        _torrent("h_carpt", NOW - timedelta(hours=30), site="carpt", uploaded=1000),
        _torrent("h_btschool", NOW - timedelta(hours=30), site="btschool", uploaded=1000),
    ])
    state = State(tmp_path / "t.db")
    hr = [HrRecord(site="carpt", site_id=192915, name="N-h_carpt",
                   remaining_seed_sec=0, complete_dt=None, status="考察中")]
    mgr = SeedingManager(q, {"carpt": FakeSite(hr), "btschool": RaisingSite([])}, state, _cfg())
    mgr.ingest_existing()
    _seed_upload_snapshot(mgr, "h_carpt", 1000, changed_ago_min=20)
    res = mgr.check(NOW)  # 不得抛异常
    assert any(d.hash == "h_carpt" and d.reason == "达标且10分钟无上传" for d in res)
    assert not any(d.hash == "h_btschool" for d in res)

def test_check_dry_run_does_not_write_state(tmp_path):
    # Fix round 2 Finding 2：dry-run 的保种检查只读——种子 uploaded 与快照不一致时，
    # 不得 UPDATE 现有行；无行种子也不得 CREATE 行（预览不应有副作用）。
    # 该分支 dry-run 返回 False → 新库 dry-run 报零删除，与真实运行所需一致。
    q = FakeQbit([_torrent("h1", NOW - timedelta(hours=30), uploaded=1000)])
    hr = [HrRecord(site="carpt", site_id=192915, name="N-h1",
                   remaining_seed_sec=0, complete_dt=None, status="考察中")]
    mgr = _mgr(q, hr, tmp_path)
    mgr.ingest_existing()                              # 行已存在，last_uploaded 默认 0
    st0 = mgr.state.get_torrent("h1")
    assert st0 is not None and st0["last_uploaded"] == 0
    res = mgr.check(NOW, dry_run=True)                 # uploaded=1000 与快照 0 不一致
    assert not any(d.hash == "h1" for d in res)
    st = mgr.state.get_torrent("h1")
    assert st["last_uploaded"] == 0                    # 未被更新

    # 无行种子：dry-run 不得 CREATE 行（全新 DB 上）
    q2 = FakeQbit([_torrent("h2", NOW - timedelta(hours=30), uploaded=1000)])
    mgr2 = SeedingManager(q2, {"carpt": FakeSite(hr)}, State(tmp_path / "t2.db"), _cfg())
    mgr2.check(NOW, dry_run=True)
    assert mgr2.state.get_torrent("h2") is None

def test_check_non_dry_run_writes_snapshot(tmp_path):
    # Fix round 2 正控：非 dry-run 在 uploaded 与快照不一致时必须落库（建立上传快照基线）。
    # 行由 ingest_existing 先创建（update_torrent 是 UPDATE 不建行）。
    q = FakeQbit([_torrent("h1", NOW - timedelta(hours=30), uploaded=1000)])
    hr = [HrRecord(site="carpt", site_id=192915, name="N-h1",
                   remaining_seed_sec=0, complete_dt=None, status="考察中")]
    mgr = _mgr(q, hr, tmp_path)
    mgr.ingest_existing()
    res = mgr.check(NOW)                               # 快照 0 != 1000 → 落库更新
    st = mgr.state.get_torrent("h1")
    assert st is not None and st["last_uploaded"] == 1000
    assert not any(d.hash == "h1" for d in res)        # 刚建快照 → 本轮不判删


# ---- Fix round 3：I2 site_id 标签匹配 / CRITICAL MyhrParseError fail-closed / I6 磁盘压力限无上传 ----

def test_hr_matches_site_id_tag_not_name(tmp_path):
    # I2：qbit 种子名与 myhr 名不一致（重名加 " (1)" / 名称截断 / 编码差异）时，必须按 daemon
    # 打的 site:{site}:{id} 标签匹配 site_id——否则按名漏匹配 → 误判"无义务" → 有义务种被删（H&R 风险）。
    # completion 取 5h 前（< window+lag，避免站点兜底分支误触发，专注验证 site_id 匹配）。
    q = FakeQbit([_torrent("h1", NOW - timedelta(hours=5), uploaded=1000, tags=["site:carpt:123"])])
    hr = [HrRecord(site="carpt", site_id=123, name="Real Site Name",
                   remaining_seed_sec=86400, complete_dt=None, status="考察中")]
    mgr = _mgr(q, hr, tmp_path)
    mgr.ingest_existing()
    _seed_upload_snapshot(mgr, "h1", 1000, changed_ago_min=20)  # 否则旧实现按名漏匹配会删
    res = mgr.check(NOW)
    assert not any(d.hash == "h1" for d in res)  # 有义务（site_id 匹配）→ 不得删


def test_hr_name_fallback_when_no_tag(tmp_path):
    # I2 正控：无 tag 老种仍按名称匹配
    q = FakeQbit([_torrent("h1", NOW - timedelta(hours=5), uploaded=1000)])
    hr = [HrRecord(site="carpt", site_id=192915, name="N-h1",
                   remaining_seed_sec=3600, complete_dt=None, status="考察中")]
    mgr = _mgr(q, hr, tmp_path)
    mgr.ingest_existing()
    _seed_upload_snapshot(mgr, "h1", 1000, changed_ago_min=20)
    res = mgr.check(NOW)
    assert not any(d.hash == "h1" for d in res)  # 名称匹配到义务 → 不删


def test_hr_site_id_tag_string_form(tmp_path):
    # I2 现实形态：qbit torrents/info 的 tags 是逗号分隔字符串（实测 "site:carpt:123"），
    # 不是 list。_find_hr 必须能解析字符串形态，否则按名漏匹配 → 有义务种被误删。
    t = _torrent("h1", NOW - timedelta(hours=5), uploaded=1000, tags="site:carpt:123")
    hr = [HrRecord(site="carpt", site_id=123, name="Real Site Name",
                   remaining_seed_sec=86400, complete_dt=None, status="考察中")]
    mgr = _mgr(FakeQbit([t]), hr, tmp_path)
    mgr.ingest_existing()
    _seed_upload_snapshot(mgr, "h1", 1000, changed_ago_min=20)
    res = mgr.check(NOW)
    assert not any(d.hash == "h1" for d in res)


def test_hr_site_id_met_still_deletable(tmp_path):
    # I2 正控：site_id 匹配到已达标（remaining=0）记录 → 按达标+无上传删
    q = FakeQbit([_torrent("h1", NOW - timedelta(hours=30), uploaded=1000, tags=["site:carpt:123"])])
    hr = [HrRecord(site="carpt", site_id=123, name="Real Site Name",
                   remaining_seed_sec=0, complete_dt=None, status="考察中")]
    mgr = _mgr(q, hr, tmp_path)
    mgr.ingest_existing()
    _seed_upload_snapshot(mgr, "h1", 1000, changed_ago_min=20)
    res = mgr.check(NOW)
    assert any(d.hash == "h1" and d.reason == "达标且10分钟无上传" for d in res)


class InvalidMyhrSite(FakeSite):
    def fetch_myhr(self):
        raise MyhrParseError("myhr 页面缺少有效标记（疑似 Cookie 失效）")

def test_check_parse_invalid_skips_site_no_deletion(tmp_path):
    # CRITICAL：parse_myhr 对登录/错误页抛 MyhrParseError → check() 必须跳过该站（fail-closed），
    # 不得把"解析失败"当成"全部达标"删掉该站有义务种。
    q = FakeQbit([_torrent("h1", NOW - timedelta(hours=30), uploaded=1000)])
    state = State(tmp_path / "t.db")
    mgr = SeedingManager(q, {"carpt": InvalidMyhrSite([])}, state, _cfg())
    mgr.ingest_existing()
    _seed_upload_snapshot(mgr, "h1", 1000, changed_ago_min=20)
    res = mgr.check(NOW)  # 不得抛异常
    assert res == []       # 该站种子一个都不删


def test_disk_pressure_skips_actively_uploading_met(tmp_path):
    # I6：磁盘<3GB 时，正在上传的达标种不得被删（旧实现磁盘压力会删任意达标种，白丢 50GB
    # 考核流量）；仅"达标且无上传"的达标种可删。h_idle 无上传（快照一致且超 10 分钟）、
    # h_upload 仍在传（快照不一致）→ 只删 h_idle。
    idle = _torrent("h_idle", NOW - timedelta(hours=30), uploaded=1000, size=600*1024**2)
    upload = _torrent("h_upload", NOW - timedelta(hours=30), uploaded=2000, size=1*1024**3)
    q = FakeQbit([idle, upload], free_space=int(2.5*1024**3))
    hr = [
        HrRecord(site="carpt", site_id=1, name="N-h_idle",
                 remaining_seed_sec=0, complete_dt=None, status="考察中"),
        HrRecord(site="carpt", site_id=2, name="N-h_upload",
                 remaining_seed_sec=0, complete_dt=None, status="考察中"),
    ]
    mgr = _mgr(q, hr, tmp_path)
    mgr.ingest_existing()
    _seed_upload_snapshot(mgr, "h_idle", 1000, changed_ago_min=20)
    _seed_upload_snapshot(mgr, "h_upload", 1000, changed_ago_min=20)  # current=2000 → 仍在传
    res = mgr.check(NOW)
    assert any(d.hash == "h_idle" for d in res)      # 无上传达标种被删
    assert not any(d.hash == "h_upload" for d in res)  # 正在上传的达标种不得删
