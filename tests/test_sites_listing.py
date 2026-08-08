import responses
from pathlib import Path
from datetime import datetime, timezone
from pt_shualiu.sites import (parse_listing_rows, PROMO_FREE, PROMO_DOWNLOAD_OK,
                              download_fraction, Site)
from pt_shualiu.config import SiteConfig

FIX = Path(__file__).parent / "fixtures"

def test_parse_carpt_listing_promo_and_expire():
    rows = parse_listing_rows((FIX / "listing_carpt.html").read_text(), site="carpt")
    assert 192933 in rows
    r = rows[192933]
    assert r.promo in PROMO_FREE
    # 页面时间为 +0800，解析后统一转 UTC
    assert r.free_expire_dt == datetime(2026, 8, 14, 12, 13, 30, tzinfo=timezone.utc)
    assert r.pub_dt == datetime(2026, 8, 7, 12, 13, 30, tzinfo=timezone.utc)
    assert r.leecher == 32 and r.seeder == 1
    # 真实行 id=192933 大小为 7.42 GB（brief 示例的 100.00 MB 与实际捕获不符，按真实值断言）
    assert r.size_bytes == int(7.42 * 1024**3)
    assert r.name.startswith("Deep Water")

def test_parse_btschool_listing_units():
    rows = parse_listing_rows((FIX / "listing_btschool.html").read_text(), site="btschool")
    r = next(iter(rows.values()))
    assert r.promo in PROMO_FREE
    assert r.free_expire_dt is None  # BT 无剩余时间 tooltip
    assert r.size_bytes == int(9.68 * 1024**3)
    assert r.site == "btschool"

def test_promo_sets_disjoint():
    assert not (PROMO_FREE & PROMO_DOWNLOAD_OK)

def test_promo_bucket_assignment():
    # 用户确认：仅免费/2x免费入免费轨；打折种(50%/50%+2x/30%)、2x、无标记入下载轨
    assert {"pro_free", "pro_free2up"} <= PROMO_FREE
    assert {"pro_2up", "", "pro_50pctdown", "pro_50pctdown2up", "pro_30pctdown"} <= PROMO_DOWNLOAD_OK

def test_download_fraction():
    # 打折种下载按比例计入 50GB 考核指标；免费种 0 计入；2x/无标记 100% 计入
    assert download_fraction("pro_free") == 0.0
    assert download_fraction("pro_free2up") == 0.0
    assert download_fraction("pro_50pctdown") == 0.5
    assert download_fraction("pro_50pctdown2up") == 0.5
    assert download_fraction("pro_30pctdown") == 0.3
    assert download_fraction("pro_2up") == 1.0
    assert download_fraction("") == 1.0
    assert download_fraction("pro_bogus") == 1.0  # 未知促销默认全量计入

def _site_cfg():
    return SiteConfig(name="carpt", base_url="https://carpt.net", cookie="cookie",
                      rss_url="https://carpt.net/torrentrss.php?passkey=x",
                      seeding_hours=24, tracker_domain="tracker.carpt.net")

@responses.activate
def test_site_fetch_listing_and_rss():
    responses.get("https://carpt.net/torrents.php?page=1",
                  body=(FIX / "listing_carpt.html").read_text(), status=200)
    responses.get("https://carpt.net/torrentrss.php?passkey=x",
                  body=(FIX / "rss_carpt.xml").read_text(), status=200)
    site = Site(_site_cfg())
    rows = site.fetch_listing_rows(pages=1)
    assert 192933 in rows and rows[192933].site == "carpt"
    assert rows[192933].download_url == "https://carpt.net/download.php?id=192933"
    cands = site.fetch_rss()
    assert cands[0].site == "carpt" and cands[0].site_id == 192933
