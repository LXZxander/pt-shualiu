import os
from pathlib import Path
from pt_shualiu.config import load_config, SiteConfig

def test_site_config_repr_hides_secrets():
    # Minor：cookie / rss_url（含 passkey）不得出现在 repr 里（日志/REPL 泄漏面）
    sc = SiteConfig(name="carpt", base_url="https://carpt.net", cookie="secret-cookie",
                    rss_url="https://carpt.net/rss?passkey=deadbeef1234",
                    seeding_hours=24, tracker_domain="tracker.carpt.net")
    r = repr(sc)
    assert "secret-cookie" not in r
    assert "deadbeef1234" not in r
    assert "carpt" in r  # 非敏感字段仍可见

def test_load_config(tmp_path):
    env = tmp_path / "test.env"
    env.write_text(
        "CARPT_COOKIE=a\nBTSCHOOL_COOKIE=b\n"
        "CARPT_RSS_URL=https://carpt.net/rss?passkey=x\n"
        "BTSCHOOL_RSS_URL=https://pt.btschool.club/rss?passkey=y\n"
        "QBIT_URL=http://127.0.0.1:9091\nQBIT_USER=u\nQBIT_PASS=p\n"
        "MAIL_USER=me@163.com\nMAIL_AUTH_CODE=code\nMAIL_TO=me@163.com\n"
    )
    c = load_config(env)
    assert len(c.sites) == 2
    assert c.sites[0].name == "carpt" and c.sites[0].seeding_hours == 24
    assert c.sites[1].name == "btschool" and c.sites[1].seeding_hours == 20
    assert c.rate_high_bytes == 500 * 1024
    assert c.rate_low_bytes == 200 * 1024
    assert c.disk_min_free_bytes == 3 * 1024**3
