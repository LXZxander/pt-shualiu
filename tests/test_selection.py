from datetime import datetime, timedelta, timezone
from pt_shualiu.selection import select
from pt_shualiu.sites import Candidate, ListingRow, PROMO_DOWNLOAD_OK
from pt_shualiu.config import Config, SiteConfig

NOW = datetime(2026, 8, 7, 14, 0, 0, tzinfo=timezone.utc)

def _cfg():
    return Config(sites=[], qbit_url="", qbit_user="", qbit_pass="")

def _cand(sid, size, age_h, promo=None, leecher=5, seeder=1):
    if promo is not None:
        row = ListingRow(site="carpt", site_id=sid, size_bytes=size,
                         pub_dt=NOW - timedelta(hours=age_h), seeder=seeder, leecher=leecher,
                         promo=promo, free_expire_dt=NOW + timedelta(days=2),
                         download_url=f"https://carpt.net/download.php?id={sid}")
    else:
        row = None
    return (Candidate(site="carpt", site_id=sid, name=f"M{sid}", size_bytes=size,
                      pub_dt=NOW - timedelta(hours=age_h),
                      download_url=f"https://carpt.net/dl/{sid}", guid=f"g{sid}"), row)

def test_free_track_filters():
    cands = [
        _cand(1, 100*1024**2, 1, "pro_free"),             # 免费 新 小 → 选
        _cand(2, 600*1024**2, 1, "pro_free"),             # 超过500MB → 不选
        _cand(3, 100*1024**2, 13, "pro_free"),            # 超过12h → 不选
        _cand(4, 100*1024**2, 1, "pro_50pctdown"),        # 非免费 → 不选
        _cand(5, 100*1024**2, 1, "pro_free", leecher=0),  # leecher=0 但 seeder=1 → 非死种 → 选（设计：leecher>0 或 seeder>0）
        _cand(8, 100*1024**2, 1, "pro_free", leecher=0, seeder=0),  # 死种（seeder==0 且 leecher==0）→ 不选
        _cand(6, 100*1024**2, 1, "pro_free"),             # 已在SQLite历史 → 不选
        _cand(7, 100*1024**2, 1, "pro_free"),             # 已在qbit(同名老种) → 不选
    ]
    listing = {c[0].site_id: c[1] for c in cands if c[1]}
    res = select([c for c, _ in cands], listing,
                 history_ids={6}, known_names={"M7"},
                 disk_free_bytes=20*1024**3, cfg=_cfg(), now=NOW)
    assert {x.site_id for x in res.free} == {1, 5}

def test_free_expire_soon_skipped():
    row = ListingRow(site="carpt", site_id=1, size_bytes=100*1024**2,
                     pub_dt=NOW - timedelta(hours=1), seeder=1, leecher=5,
                     promo="pro_free", free_expire_dt=NOW + timedelta(minutes=30),
                     download_url="")
    cand = Candidate(site="carpt", site_id=1, name="M1", size_bytes=100*1024**2,
                     pub_dt=NOW - timedelta(hours=1), download_url="u", guid="g")
    res = select([cand], {1: row}, set(), set(), 20*1024**3, _cfg(), NOW)
    assert res.free == []  # 免费30分钟后到期 → 跳过

def test_download_track_picks_2up_and_normal():
    rows = [
        ListingRow(site="carpt", site_id=10, size_bytes=1*1024**3, pub_dt=NOW-timedelta(hours=2),
                   seeder=1, leecher=8, promo="pro_2up", free_expire_dt=None, download_url="d"),
        ListingRow(site="carpt", site_id=11, size_bytes=1*1024**3, pub_dt=NOW-timedelta(hours=2),
                   seeder=1, leecher=8, promo="", free_expire_dt=None, download_url="d"),
        ListingRow(site="carpt", site_id=12, size_bytes=1*1024**3, pub_dt=NOW-timedelta(hours=2),
                   seeder=1, leecher=8, promo="pro_free", free_expire_dt=None, download_url="d"),
        ListingRow(site="carpt", site_id=13, size_bytes=3*1024**3, pub_dt=NOW-timedelta(hours=2),
                   seeder=1, leecher=8, promo="pro_2up", free_expire_dt=None, download_url="d"),
    ]
    res = select([], {r.site_id: r for r in rows}, set(), set(), 20*1024**3, _cfg(), NOW,
                 download_concurrent=0)
    # 每轮最多加 1 个（设计 §4.4）；12=free 排除、13 超 2GB 排除
    assert len(res.download) == 1
    assert {x.site_id for x in res.download} <= {10, 11}
    assert all(x.promo in PROMO_DOWNLOAD_OK for x in res.download)

def test_download_track_fraction_prefers_full_count():
    # download_fraction 高者优先：1.0（2up/无标记）应在 0.5（50pctdown）之前
    rows = [
        ListingRow(site="carpt", site_id=20, size_bytes=1*1024**3, pub_dt=NOW-timedelta(hours=2),
                   seeder=1, leecher=8, promo="pro_2up", free_expire_dt=None, download_url="d"),
        ListingRow(site="carpt", site_id=21, size_bytes=1*1024**3, pub_dt=NOW-timedelta(hours=2),
                   seeder=1, leecher=9, promo="pro_50pctdown", free_expire_dt=None, download_url="d"),
    ]
    res = select([], {r.site_id: r for r in rows}, set(), set(), 20*1024**3, _cfg(), NOW,
                 download_concurrent=0)
    assert [x.site_id for x in res.download] == [20]  # 20 分数高，优先于 leecher 更多的 21

def test_download_track_uses_rss_auth_url():
    # 冒烟实测修正回归：列表页裸 download_url 无鉴权，qbit 抓回 HTML 无法解析（下载轨全挂）。
    # 同 id 的 RSS 候选带 passkey/downhash → 下载轨必须优先用 RSS 链接。
    row = ListingRow(site="carpt", site_id=10, size_bytes=1*1024**3, pub_dt=NOW-timedelta(hours=2),
                     seeder=1, leecher=8, promo="pro_2up", free_expire_dt=None,
                     download_url="https://carpt.net/download.php?id=10")
    cand = Candidate(site="carpt", site_id=10, name="M10", size_bytes=1*1024**3,
                     pub_dt=NOW - timedelta(hours=2),
                     download_url="https://carpt.net/download.php?downhash=TOKEN", guid="g")
    res = select([cand], {10: row}, set(), set(), 20*1024**3, _cfg(), NOW)
    assert len(res.download) == 1
    assert res.download[0].download_url == "https://carpt.net/download.php?downhash=TOKEN"

def test_download_track_btschool_passkey_synth():
    # RSS 里没有该种时，对 btschool 用 RSS URL 里的全局 passkey 现拼带鉴权链接。
    row = ListingRow(site="btschool", site_id=42, size_bytes=1*1024**3,
                     pub_dt=NOW-timedelta(hours=2), seeder=1, leecher=8, promo="",
                     free_expire_dt=None, download_url="https://pt.btschool.club/download.php?id=42")
    cfg = Config(sites=[SiteConfig(name="btschool", base_url="https://pt.btschool.club",
                                   cookie="", rss_url="https://pt.btschool.club/torrentrss.php?passkey=abc123deadbeef",
                                   seeding_hours=20, tracker_domain="pt.btschool.club")],
                 qbit_url="", qbit_user="", qbit_pass="")
    res = select([], {42: row}, set(), set(), 20*1024**3, cfg, NOW)
    assert len(res.download) == 1
    assert res.download[0].download_url == "https://pt.btschool.club/download.php?id=42&passkey=abc123deadbeef"

def test_download_track_concurrent_cap():
    # 同时下载中的下载轨种子 ≥2 → 本轮不再加（设计 §4.4）
    rows = [
        ListingRow(site="carpt", site_id=30, size_bytes=1*1024**3, pub_dt=NOW-timedelta(hours=2),
                   seeder=1, leecher=8, promo="pro_2up", free_expire_dt=None, download_url="d"),
    ]
    res = select([], {30: rows[0]}, set(), set(), 20*1024**3, _cfg(), NOW,
                 download_concurrent=2)
    assert res.download == []
