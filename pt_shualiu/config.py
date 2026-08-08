"""配置加载：.env 放凭证与阈值，返回 Config 数据类。"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

@dataclass
class SiteConfig:
    name: str                 # "carpt" | "btschool"
    base_url: str
    cookie: str = field(repr=False)      # 不随 repr 泄露（日志/REPL 面）
    rss_url: str = field(repr=False)     # 含 passkey，不随 repr 泄露
    seeding_hours: int        # 24 | 20
    tracker_domain: str       # "tracker.carpt.net" | "pt.btschool.club"

@dataclass
class Config:
    sites: list[SiteConfig]
    qbit_url: str
    qbit_user: str
    qbit_pass: str
    save_path: str = str(Path.home() / "pt_shualiu" / "downloads")
    cat_free: str = "pt_free"
    cat_download: str = "pt_download"
    # 阈值
    disk_min_free_bytes: int = 3 * 1024**3
    free_max_size_bytes: int = 500 * 1024**2
    free_prefer_size_bytes: int = 400 * 1024**2
    free_max_age_hours: int = 12
    download_max_size_bytes: int = 2 * 1024**3
    free_expire_buffer_min: int = 60
    no_upload_min: int = 10
    site_lag_override_min: int = 30
    # 限速
    rate_limit_kbs: int = 500
    rate_low_kbs: int = 200
    up_trigger_min: int = 60
    down_trigger_min: int = 120
    low_hold_min: int = 60
    cap_fraction: float = 0.95
    # 调度
    poll_selection_s: int = 300
    poll_seeding_s: int = 600
    poll_rate_s: int = 60
    mail_send_time: str = "09:00"
    # 邮件
    mail_host: str = "smtp.163.com"
    mail_port: int = 465
    mail_user: str = ""
    mail_auth_code: str = ""
    mail_to: str = ""
    # 站点 rss_url 的 passkey 部分由 .env 提供
    env_path: Path = field(default=Path(".env"), repr=False)

    @property
    def rate_high_bytes(self) -> int:
        return self.rate_limit_kbs * 1024
    @property
    def rate_low_bytes(self) -> int:
        return self.rate_low_kbs * 1024

def load_config(env_path: Path = Path(".env")) -> Config:
    load_dotenv(env_path)
    sites = [
        SiteConfig(
            name="carpt",
            base_url="https://carpt.net",
            cookie=os.environ["CARPT_COOKIE"],
            rss_url=os.environ["CARPT_RSS_URL"],
            seeding_hours=24,
            tracker_domain="tracker.carpt.net",
        ),
        SiteConfig(
            name="btschool",
            base_url="https://pt.btschool.club",
            cookie=os.environ["BTSCHOOL_COOKIE"],
            rss_url=os.environ["BTSCHOOL_RSS_URL"],
            seeding_hours=20,
            tracker_domain="pt.btschool.club",
        ),
    ]
    return Config(
        sites=sites,
        qbit_url=os.environ["QBIT_URL"],
        qbit_user=os.environ["QBIT_USER"],
        qbit_pass=os.environ["QBIT_PASS"],
        mail_user=os.environ.get("MAIL_USER", ""),
        mail_auth_code=os.environ.get("MAIL_AUTH_CODE", ""),
        mail_to=os.environ.get("MAIL_TO", ""),
        env_path=env_path,
    )
