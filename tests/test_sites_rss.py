from pathlib import Path
from datetime import datetime, timezone
from pt_shualiu.sites import parse_rss

FIX = Path(__file__).parent / "fixtures"

def test_parse_carpt_rss():
    items = parse_rss((FIX / "rss_carpt.xml").read_text())
    assert len(items) == 1
    it = items[0]
    assert it.site_id == 192933
    assert it.size_bytes == 104857600
    assert it.pub_dt == datetime(2026, 8, 7, 13, 40, 24, tzinfo=timezone.utc)  # 21:40+0800 → 13:40 UTC
    assert it.download_url == "https://carpt.net/download.php?downhash=abc123"

def test_parse_btschool_rss_unescape():
    items = parse_rss((FIX / "rss_btschool.xml").read_text())
    assert items[0].site_id == 313678
    assert "&amp;" not in items[0].download_url  # HTML 实体已反转义
