# PT刷流工具 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 CarPT 和 BT school 两个 PT 站搭建 systemd 常驻的自动刷流+保种服务，满足新手考核（上传/下载/魔力/做种积分），自动抓免费小种刷上传、机会性抓非免费小种凑下载量，保种期满后自动清理。

**Architecture:** Python 3 常驻服务，主循环按不同周期执行四个子任务：选种（RSS+列表页→qbit 添加）、保种生命周期（qbit+myhr→删除）、限速状态机（上传/下载独立 500↔200）、每日邮件摘要。站点区分用 qbit tracker 域名；管理范围 = tracker 属于两站的全部种子；SQLite 存状态。

**Tech Stack:** Python 3.10+（服务器 3.12）、`requests`、`python-dotenv`、SQLite（stdlib）、pytest + `responses`（测试）、systemd。qBittorrent Web API v4.6.3（HTTP 直连，无第三方库）。

## Global Constraints

- 站点：`carpt`（https://carpt.net，保种 24h，tracker 域名 `tracker.carpt.net`）、`btschool`（https://pt.btschool.club，保种 20h，tracker 域名 `pt.btschool.club`）
- 两站均为 NexusPHP，RSS/列表页/myhr 结构已验证一致；`free=1` RSS 参数被忽略，**促销状态一律从列表页 `pro_*` class 判定**
- 促销枚举：`pro_free`=免费、`pro_free2up`=免费+2x、`pro_50pctdown`=下载50%、`pro_50pctdown2up`、`pro_30pctdown`=下载30%、`pro_2up`=2x上传、无标记=正常
- 免费轨促销 ∈ `{pro_free, pro_free2up}`；下载轨促销 ∈ `{pro_2up, 无标记}`（下载100%计入）
- **管理范围 = tracker 属于两站的全部种子**（含已有 18 个：btschool 11 + carpt 7）；非 PT 种子不碰
- 新增种子分类 `pt_free`/`pt_download`，保存路径 `~/pt_shualiu/downloads`
- 磁盘剩余 <3GB 不新增，告急先删"达标且无上传"（先大的）
- 保种达标以网站 myhr 为准（还需做种时间归零/不再出现在考察中列表）；达标后 10 分钟无上传删除；`completion_on + 保种时长 + 30 分钟` 网站仍未刷新则直接删除
- 限速：上传 500 KB/s → 跑满(≥95%)连续 60 分钟 → 200 KB/s 保持 60 分钟 → 回 500；下载 500 KB/s → 跑满连续 120 分钟 → 200 KB/s 保持 60 分钟 → 回 500
- 免费轨：≤500MB（偏好 <400MB）、发布时间 <12h、leecher>0、免费剩余时间 > 预计下载+1h
- 下载轨：≤2GB、leecher>0、同时下载中 ≤2 个、优先级低于免费轨
- qbit 全局限速用 `POST /api/v2/app/setPreferences`，body 表单字段 `json={...}`；单位字节/秒（实现时先读当前值→设→读回→恢复原值 验证一次单位）
- 凭证（站点 cookie、qbit 密码、163 授权码）只放 `.env`（gitignore），代码不硬编码
- 每日 09:00 邮件摘要到 163
- 调度：限速 60s、选种 5min、保种 10min、邮件 09:00

---

### Task 1: 项目脚手架 + config.py + state.py

**Files:**
- Create: `~/pt_shualiu/.gitignore`
- Create: `~/pt_shualiu/.env.example`
- Create: `~/pt_shualiu/pt_shualiu/__init__.py`
- Create: `~/pt_shualiu/pt_shualiu/config.py`
- Create: `~/pt_shualiu/pt_shualiu/state.py`
- Create: `~/pt_shualiu/requirements.txt`
- Create: `~/pt_shualiu/requirements-dev.txt`
- Test: `~/pt_shualiu/tests/test_config.py`, `~/pt_shualiu/tests/test_state.py`, `~/pt_shualiu/tests/conftest.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: `pt_shualiu.config.Config` / `load_config()`；`pt_shualiu.state.State` 全部方法；供 Task 2-10 使用

- [ ] **Step 1: 初始化仓库与目录**

```bash
mkdir -p ~/pt_shualiu/{pt_shualiu,tests,downloads}
cd ~/pt_shualiu
git init -q
python3 -m venv .venv
.venv/bin/pip install -q -U pip
```

`~/pt_shualiu/.gitignore`:
```
.venv/
__pycache__/
*.pyc
.env
*.db
downloads/
```

`~/pt_shualiu/.env.example`（真实值填到 `.env`，.env 不入 git）:
```
# ==== 站点凭证 ====
CARPT_COOKIE=c_secure_uid=...; c_secure_pass=...
BTSCHOOL_COOKIE=sl-session=...; c_secure_uid=...; c_secure_pass=...
# ==== qBittorrent ====
QBIT_URL=http://127.0.0.1:9091
QBIT_USER=your_qbit_user
QBIT_PASS=...
# ==== 163 邮箱 ====
MAIL_HOST=smtp.163.com
MAIL_PORT=465
MAIL_USER=you@example.com
MAIL_AUTH_CODE=...
MAIL_TO=you@example.com
```

`~/pt_shualiu/requirements.txt`:
```
requests==2.32.*
python-dotenv==1.0.*
```
`~/pt_shualiu/requirements-dev.txt`:
```
-r requirements.txt
pytest==8.*
responses==0.25.*
```

安装：`cd ~/pt_shualiu && .venv/bin/pip install -q -r requirements-dev.txt`

- [ ] **Step 2: 写 config.py**

```python
"""配置加载：.env 放凭证与阈值，返回 Config 数据类。"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

@dataclass
class SiteConfig:
    name: str                 # "carpt" | "btschool"
    base_url: str
    cookie: str
    rss_url: str
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
```

注：`CARPT_RSS_URL`/`BTSCHOOL_RSS_URL` 两个 rss_url 也放 `.env`（含 passkey，属敏感信息）。在 `.env.example` 追加：
```
CARPT_RSS_URL=https://carpt.net/torrentrss.php?passkey=...
BTSCHOOL_RSS_URL=https://pt.btschool.club/torrentrss.php?passkey=...
```

- [ ] **Step 3: 写 config 测试**

`tests/test_config.py`:
```python
import os
from pathlib import Path
from pt_shualiu.config import load_config

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
```

- [ ] **Step 4: 跑测试确认失败**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_config.py -v`
Expected: FAIL（`pt_shualiu.config` 尚未创建 config 函数前 `load_config` 不存在——先建好目录与 `__init__.py` 空文件）

- [ ] **Step 5: 写 state.py（SQLite 状态存储）**

```python
"""SQLite 状态：种子登记、去重历史、限速状态、每日统计。"""
import sqlite3
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS torrents (
    hash TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    category TEXT,
    name TEXT,
    added_at REAL,
    completion_on INTEGER DEFAULT 0,
    last_uploaded INTEGER DEFAULT 0,
    last_upload_change REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS history (
    site TEXT, site_id INTEGER, PRIMARY KEY (site, site_id)
);
CREATE TABLE IF NOT EXISTS ratelimit (
    direction TEXT PRIMARY KEY,
    mode TEXT DEFAULT 'high',
    limit_bytes INTEGER,
    cap_minutes INTEGER DEFAULT 0,
    low_remaining_min INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily_stats (
    d TEXT, site TEXT, action TEXT, n INTEGER DEFAULT 0,
    PRIMARY KEY (d, site, action)
);
"""

class State:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---- torrents ----
    def upsert_torrent(self, hash_, site, category, name, added_at):
        self._conn.execute(
            "INSERT OR REPLACE INTO torrents(hash,site,category,name,added_at) VALUES(?,?,?,?,?)",
            (hash_, site, category, name, added_at))
        self._conn.commit()

    def update_torrent(self, hash_, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE torrents SET {cols} WHERE hash=?", (*fields.values(), hash_))
        self._conn.commit()

    def get_torrent(self, hash_):
        r = self._conn.execute("SELECT * FROM torrents WHERE hash=?", (hash_,)).fetchone()
        if not r:
            return None
        cols = [d[0] for d in self._conn.execute("SELECT * FROM torrents").description]
        return dict(zip(cols, r))

    def all_torrents(self):
        cols = [d[0] for d in self._conn.execute("SELECT * FROM torrents").description]
        return [dict(zip(cols, r)) for r in self._conn.execute("SELECT * FROM torrents").fetchall()]

    def remove_torrent(self, hash_):
        self._conn.execute("DELETE FROM torrents WHERE hash=?", (hash_,))
        self._conn.commit()

    # ---- history（站点种子 id 去重） ----
    def add_history(self, site, site_id):
        self._conn.execute("INSERT OR IGNORE INTO history(site,site_id) VALUES(?,?)", (site, site_id))
        self._conn.commit()

    def is_history(self, site, site_id):
        return self._conn.execute(
            "SELECT 1 FROM history WHERE site=? AND site_id=?", (site, site_id)).fetchone() is not None

    def history_ids(self, site):
        return {r[0] for r in self._conn.execute(
            "SELECT site_id FROM history WHERE site=?", (site,)).fetchall()}

    # ---- ratelimit ----
    def get_rate_state(self, direction):
        r = self._conn.execute("SELECT * FROM ratelimit WHERE direction=?", (direction,)).fetchone()
        if not r:
            return {"mode": "high", "limit_bytes": None, "cap_minutes": 0, "low_remaining_min": 0}
        cols = [d[0] for d in self._conn.execute("SELECT * FROM ratelimit").description]
        return dict(zip(cols, r))

    def set_rate_state(self, direction, mode, limit_bytes, cap_minutes, low_remaining_min):
        self._conn.execute(
            "INSERT OR REPLACE INTO ratelimit(direction,mode,limit_bytes,cap_minutes,low_remaining_min)"
            " VALUES(?,?,?,?,?)",
            (direction, mode, limit_bytes, cap_minutes, low_remaining_min))
        self._conn.commit()

    # ---- daily_stats（计数累计） ----
    def incr_stat(self, site, action, n=1):
        d = date.today().isoformat()
        self._conn.execute(
            "INSERT INTO daily_stats(d,site,action,n) VALUES(?,?,?,?) "
            "ON CONFLICT(d,site,action) DO UPDATE SET n=n+?",
            (d, site, action, n, n))
        self._conn.commit()

    def stat(self, site, action):
        d = date.today().isoformat()
        r = self._conn.execute("SELECT n FROM daily_stats WHERE d=? AND site=? AND action=?",
                               (d, site, action)).fetchone()
        return r[0] if r else 0

    def close(self):
        self._conn.close()
```

- [ ] **Step 6: 写 state 测试**

`tests/test_state.py`:
```python
from pt_shualiu.state import State

def test_torrent_crud(tmp_path):
    s = State(tmp_path / "t.db")
    s.upsert_torrent("abc123", "carpt", "pt_free", "Some Movie", 1000.0)
    t = s.get_torrent("abc123")
    assert t["site"] == "carpt" and t["category"] == "pt_free"
    s.update_torrent("abc123", completion_on=2000)
    assert s.get_torrent("abc123")["completion_on"] == 2000
    s.remove_torrent("abc123")
    assert s.get_torrent("abc123") is None

def test_history_dedup(tmp_path):
    s = State(tmp_path / "t.db")
    s.add_history("carpt", 123)
    assert s.is_history("carpt", 123)
    assert not s.is_history("carpt", 124)
    s.add_history("carpt", 123)  # 幂等
    assert s.is_history("carpt", 123)

def test_rate_state_roundtrip(tmp_path):
    s = State(tmp_path / "t.db")
    s.set_rate_state("up", "low", 200*1024, 0, 30)
    assert s.get_rate_state("up")["mode"] == "low"
    assert s.get_rate_state("up")["low_remaining_min"] == 30

def test_history_ids(tmp_path):
    s = State(tmp_path / "t.db")
    s.add_history("carpt", 1); s.add_history("carpt", 2); s.add_history("btschool", 99)
    assert s.history_ids("carpt") == {1, 2}
    assert s.history_ids("btschool") == {99}

def test_daily_stats_incr(tmp_path):
    s = State(tmp_path / "t.db")
    s.incr_stat("carpt", "added", 3)
    s.incr_stat("carpt", "added", 2)
    assert s.stat("carpt", "added") == 5
    assert s.stat("btschool", "added") == 0
```

- [ ] **Step 7: 跑测试确认通过**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
cd ~/pt_shualiu
git add .gitignore .env.example requirements*.txt pt_shualiu/ tests/
git commit -q -m "feat: 脚手架 + config 加载 + SQLite 状态存储"
```

---

### Task 2: qbit.py — qBittorrent API 封装

**Files:**
- Create: `~/pt_shualiu/pt_shualiu/qbit.py`
- Test: `~/pt_shualiu/tests/test_qbit.py`

**Interfaces:**
- Consumes: `Config`（qbit_url/qbit_user/qbit_pass）
- Produces: `QbitClient` 类，方法：`login()`, `add_torrent(url, save_path, category, paused, priority, tags) -> hash`, `torrents_info() -> list[dict]`, `trackers(hash) -> list[str]`, `site_of_torrent(hash, domains: dict) -> str|None`, `all_pt_torrents(domains: dict) -> list[dict]`, `delete_torrents(hashes, delete_files=True)`, `set_global_limits(up_bytes, down_bytes)`, `set_priority(hashes, priority)`, `sync_maindata() -> dict`, `pause(hashes)`, `resume(hashes)`

- [ ] **Step 1: 写失败测试**

`tests/test_qbit.py`（用 `responses` mock HTTP）:
```python
import responses
import pytest
from pt_shualiu.qbit import QbitClient

@pytest.fixture
def client():
    return QbitClient("http://127.0.0.1:9091", "user", "pass")

@responses.activate
def test_login(client):
    responses.post(f"{client.base}/api/v2/auth/login",
                   body="Ok.", status=200)
    client.login()
    assert "SID" in client.session.cookies  # qbit 返回 cookie 名视版本而定

@responses.activate
def test_login_fail(client):
    responses.post(f"{client.base}/api/v2/auth/login",
                   body="Fails.", status=200)
    with pytest.raises(RuntimeError):
        client.login()

@responses.activate
def test_add_torrent(client):
    responses.post(f"{client.base}/api/v2/auth/login", body="Ok.", status=200)
    responses.post(f"{client.base}/api/v2/torrents/add", body="Ok.", status=200)
    responses.get(f"{client.base}/api/v2/torrents/info",
                  json=[{"hash": "h1", "name": "X", "category": "", "progress": 0}], status=200)
    h = client.add_torrent("https://carpt.net/download.php?id=1",
                           save_path="/tmp/dl", category="pt_free",
                           paused=True, priority=3, tags=["site:carpt:1"])
    assert h == "h1"

@responses.activate
def test_set_global_limits(client):
    responses.post(f"{client.base}/api/v2/auth/login", body="Ok.", status=200)
    responses.post(f"{client.base}/api/v2/app/setPreferences", status=200)
    client.set_global_limits(500*1024, 500*1024)
    req = responses.calls[-1].request
    assert "json=" in req.body.decode() and "upload_limit" in req.body.decode()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_qbit.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 qbit.py**

```python
"""qBittorrent Web API v4.6.x 封装。登录拿 cookie 后所有请求复用 session。"""
import requests
from urllib.parse import urljoin

class QbitClient:
    def __init__(self, base_url: str, user: str, password: str):
        self.base = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "pt-shualiu/0.1"})

    def _login(self):
        r = self.session.post(f"{self.base}/api/v2/auth/login",
                              data={"username": self.user, "password": self.password}, timeout=15)
        r.raise_for_status()
        if r.text.strip() != "Ok.":
            raise RuntimeError(f"qBittorrent login failed: {r.text.strip()!r}")

    def login(self):
        self._login()

    def add_torrent(self, url: str, save_path: str = None, category: str = None,
                    paused: bool = False, priority: int = 2, tags: list[str] = None) -> str:
        self._login()
        data = {"urls": url, "paused": "true" if paused else "false", "priority": priority}
        if save_path:
            data["savepath"] = save_path
        if category:
            data["category"] = category
        if tags:
            data["tags"] = ",".join(tags)
        r = self.session.post(f"{self.base}/api/v2/torrents/add", data=data, timeout=30)
        r.raise_for_status()
        # 返回 hash：重新查最近添加的同名种
        info = self.session.get(f"{self.base}/api/v2/torrents/info", timeout=15).json()
        name = self._name_from_url(url)
        for t in reversed(info):
            if t.get("name") == name:
                return t["hash"]
        raise RuntimeError("added torrent not found in qBittorrent list")

    @staticmethod
    def _name_from_url(url: str) -> str:
        # .torrent 文件名来自 URL 末尾或 fallback
        return url.rstrip("/").split("/")[-1]

    def torrents_info(self) -> list[dict]:
        self._login()
        return self.session.get(f"{self.base}/api/v2/torrents/info", timeout=20).json()

    def trackers(self, hash_: str) -> list[str]:
        self._login()
        r = self.session.get(f"{self.base}/api/v2/torrents/trackers",
                             params={"hash": hash_}, timeout=15).json()
        return [t["url"] for t in r if t["url"] not in ("** [DHT] **", "** [PeX] **", "** [LSD] **")]

    def site_of_torrent(self, hash_: str, domains: dict) -> str | None:
        for tr in self.trackers(hash_):
            for site, domain in domains.items():
                if domain in tr:
                    return site
        return None

    def all_pt_torrents(self, domains: dict) -> list[dict]:
        out = []
        for t in self.torrents_info():
            site = self.site_of_torrent(t["hash"], domains)
            if site:
                t["site"] = site
                out.append(t)
        return out

    def delete_torrents(self, hashes: list[str], delete_files: bool = True):
        self._login()
        self.session.post(f"{self.base}/api/v2/torrents/delete",
                          data={"hashes": "|".join(hashes),
                                "deleteFiles": "true" if delete_files else "false"}, timeout=30)

    def set_global_limits(self, up_bytes: int, down_bytes: int):
        self._login()
        body = {"upload_limit": up_bytes, "download_limit": down_bytes}
        self.session.post(f"{self.base}/api/v2/app/setPreferences",
                          data={"json": str(body)}, timeout=15)

    def set_priority(self, hashes: list[str], priority: int):
        self._login()
        self.session.post(f"{self.base}/api/v2/torrents/setPriority",
                          data={"hashes": "|".join(hashes), "priority": priority}, timeout=15)

    def sync_maindata(self) -> dict:
        self._login()
        return self.session.get(f"{self.base}/api/v2/sync/maindata", timeout=20).json()

    def pause(self, hashes: list[str]):
        self._login()
        self.session.post(f"{self.base}/api/v2/torrents/pause",
                          data={"hashes": "|".join(hashes)}, timeout=15)

    def resume(self, hashes: list[str]):
        self._login()
        self.session.post(f"{self.base}/api/v2/torrents/resume",
                          data={"hashes": "|".join(hashes)}, timeout=15)
```

> 注意：`add_torrent` 里"按名字回查 hash"是本实现的简化。若真实添加后名字与 URL 尾段不一致，实现时改为：从 RSS 候选直接带 `hash` 不现实（torrent 未下载无 hash）；改用 qbit 添加后 `torrents/info` 里按 `category`+`add_time` 最新一条取。实现阶段以实测为准，保证返回真实 hash。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_qbit.py -v`
Expected: PASS

- [ ] **Step 5: 实测验证全局限速单位（关键）**

```bash
cd ~/pt_shualiu
.venv/bin/python - <<'EOF'
from pt_shualiu.qbit import QbitClient
from dotenv import load_dotenv
import os
load_dotenv()
c = QbitClient(os.environ["QBIT_URL"], os.environ["QBIT_USER"], os.environ["QBIT_PASS"])
# 读当前
cur = c.session.get(f"{c.base}/api/v2/app/preferences", timeout=15).json()
print("before:", cur.get("upload_limit"), cur.get("download_limit"))
c.set_global_limits(200*1024, 200*1024)
after = c.session.get(f"{c.base}/api/v2/app/preferences", timeout=15).json()
print("after set 204800:", after.get("upload_limit"), after.get("download_limit"))
# 断言字节/秒：若显示 204800 则为 bytes/s；若显示 200 则为 KiB/s（需换算）
c.set_global_limits(0, 0)  # 恢复（0 = 不限速）
EOF
```
Expected: 打印 after 的两个值。**根据结果在 config 换算**：若值为 204800 直接传 bytes；若为 200，则 `set_global_limits` 里把入参除以 1024 再传。记录结论到本文件（不可留 TODO）。

- [ ] **Step 6: 提交**

```bash
cd ~/pt_shualiu
git add pt_shualiu/qbit.py tests/test_qbit.py
git commit -q -m "feat: qBittorrent API 封装（登录/加种/删种/限速/tracker 分站）"
```

---

### Task 3: sites.py — RSS 解析

**Files:**
- Create: `~/pt_shualiu/pt_shualiu/sites.py`
- Create: `~/pt_shualiu/tests/fixtures/rss_carpt.xml`, `~/pt_shualiu/tests/fixtures/rss_btschool.xml`
- Test: `~/pt_shualiu/tests/test_sites_rss.py`

**Interfaces:**
- Consumes: `SiteConfig`；`requests`/`responses`
- Produces: `parse_rss(xml_text) -> list[Candidate]`；`Candidate = (site, site_id:int, name, size_bytes:int, pub_dt:datetime(UTC), download_url:str, guid:str)`；`Site.fetch_rss()` 复用它

- [ ] **Step 1: 建 RSS fixture（取自真实抓取，已脱敏保留结构）**

`tests/fixtures/rss_carpt.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<title>CarPT Torrents</title>
<item>
<title><![CDATA[[电影/Movies]Test Movie 2026 Bluray 1080p x265 [100.00 MB][anonymous]]]></title>
<link><![CDATA[https://carpt.net/details.php?id=192933]]></link>
<description><![CDATA[]]></description>
<author>anonymous@carpt.net (anonymous)</author>
<enclosure url="https://carpt.net/download.php?downhash=abc123" length="104857600" type="application/x-bittorrent" />
<guid isPermaLink="false">aabbccddeeff00112233445566778899aabbccdd</guid>
<pubDate>Fri, 07 Aug 2026 21:40:24 +0800</pubDate>
</item>
</channel></rss>
```

`tests/fixtures/rss_btschool.xml`: 同结构，`<link>` 为 `https://pt.btschool.club/details.php?id=313678`，enclosure url 含 `&amp;passkey=`，length=575550647，pubDate 同格式。

- [ ] **Step 2: 写失败测试**

`tests/test_sites_rss.py`:
```python
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
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_sites_rss.py -v`
Expected: FAIL

- [ ] **Step 4: 实现 parse_rss（放 sites.py）**

```python
"""站点统一适配器：RSS / 列表页 / myhr。两站均为 NexusPHP，结构一致。"""
import re
from datetime import datetime, timezone
from html import unescape
from dataclasses import dataclass

ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
TITLE_RE = re.compile(r"<title><!\[CDATA\[(.*?)\]\]></title>", re.S)
LINK_RE = re.compile(r"<link><!\[CDATA\[(.*?)\]\]></link>", re.S)
LINK_PLAIN_RE = re.compile(r"<link>(.*?)</link>", re.S)
ENCLOSURE_RE = re.compile(r'<enclosure url="([^"]+)"\s+length="(\d+)"')
GUID_RE = re.compile(r'<guid isPermaLink="false">([0-9a-f]{40})</guid>')
PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)
ID_RE = re.compile(r"details\.php\?id=(\d+)")

@dataclass
class Candidate:
    site: str
    site_id: int
    name: str
    size_bytes: int
    pub_dt: datetime
    download_url: str
    guid: str

def _parse_pubdate(s: str) -> datetime:
    # "Fri, 07 Aug 2026 21:40:24 +0800"
    dt = datetime.strptime(s.strip(), "%a, %d %b %Y %H:%M:%S %z")
    return dt.astimezone(timezone.utc)

def parse_rss(xml_text: str, site: str = "") -> list[Candidate]:
    out = []
    for it in ITEM_RE.findall(xml_text):
        m_title = TITLE_RE.search(it)
        m_link = LINK_RE.search(it) or LINK_PLAIN_RE.search(it)
        m_enc = ENCLOSURE_RE.search(it)
        m_guid = GUID_RE.search(it)
        m_pub = PUBDATE_RE.search(it)
        if not (m_title and m_link and m_enc and m_guid and m_pub):
            continue
        link = unescape(m_link.group(1))
        m_id = ID_RE.search(link)
        if not m_id:
            continue
        out.append(Candidate(
            site=site,
            site_id=int(m_id.group(1)),
            name=unescape(m_title.group(1)),
            size_bytes=int(m_enc.group(2)),
            pub_dt=_parse_pubdate(m_pub.group(1)),
            download_url=unescape(m_enc.group(1)),
            guid=m_guid.group(1),
        ))
    return out
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_sites_rss.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
cd ~/pt_shualiu
git add pt_shualiu/sites.py tests/fixtures/rss_*.xml tests/test_sites_rss.py
git commit -q -m "feat: RSS 解析器（id/大小/UTC时间/下载链接）"
```

---

### Task 4: sites.py — 列表页解析 + 促销枚举

**Files:**
- Modify: `~/pt_shualiu/pt_shualiu/sites.py`
- Create: `~/pt_shualiu/tests/fixtures/listing_carpt.html`, `~/pt_shualiu/tests/fixtures/listing_btschool.html`
- Test: `~/pt_shualiu/tests/test_sites_listing.py`

**Interfaces:**
- Consumes: `parse_rss` 的 `Candidate`；`SiteConfig`
- Produces: `parse_listing_rows(html_text, site="") -> dict[int, ListingRow]`；`ListingRow = (site, site_id, size_bytes, pub_dt, seeder, leecher, promo, free_expire_dt, download_url, name)`；`PROMO_FREE` / `PROMO_DOWNLOAD_OK` 常量；`Site.fetch_rss()` / `Site.fetch_listing_rows(pages)`

- [ ] **Step 1: 建列表页 fixture（取真实捕获行，含各类促销标记）**

`tests/fixtures/listing_carpt.html` 至少包含 5 行：1 行 `pro_free`（带"剩余时间" tooltip）、1 行 `pro_free2up`、1 行 `pro_50pctdown`、1 行 `pro_2up`、1 行无促销标记。行结构参考真实捕获（已存 `/tmp/carpt_free.html`，实现时从中截取完整 `<tr>...</tr>`）：
```html
<table class="torrents"><tr>
<td class="rowfollow nowrap" valign="middle" style='padding: 0px'><a href="?cat=401"><img class="c_movie" .../></a></td>
<td class="rowfollow" width="100%" align="left"><table class="torrentname" width="100%"><tr><td class="embedded"><a title="Test Movie 2026 Bluray 1080p x265 [100.00 MB]" href="details.php?id=192933&amp;hit=1"><b>Test Movie 2026</b></a> <img class="pro_free" src="pic/trans.gif" alt="Free" title="免费" onmouseover="domTT_activate(this, event, 'content', '&lt;b&gt;&lt;font class=&quot;free&quot;&gt;免费&lt;/font&gt;&lt;/b&gt;剩余时间：&lt;b&gt;&lt;span title=&quot;2026-08-14 20:13:30&quot;&gt;6天22时&lt;/span&gt;&lt;/b&gt;', ...);" /> <font color='#0000FF'>剩余时间：<span title="2026-08-14 20:13:30">6天22时</span></font><br />官方</td>
<td width="20" class="embedded" style="text-align: right; " valign="middle"><a href="download.php?id=192933"><img class="download" .../></a></td>
</tr></table></td>
<td class="rowfollow"><a href="comment.php?action=add&amp;pid=192933&amp;type=torrent" title="添加评论">0</a></td>
<td class="rowfollow nowrap"><span title="2026-08-07 20:13:30">1时<br />33分钟</span></td>
<td class="rowfollow">100.00<br />MB</td>
<td class="rowfollow" align="center"><b><a href="details.php?id=192933&amp;hit=1&amp;dllist=1#seeders"><font color="#ee0000">1</font></a></b></td>
<td class="rowfollow"><b><a href="details.php?id=192933&amp;hit=1&amp;dllist=1#leechers">32</a></b></td>
<td class="rowfollow">0</td>
<td class="rowfollow"><i>匿名</i></td>
</tr></table>
```

`tests/fixtures/listing_btschool.html`：同上但 BT 的 pro_free 无"剩余时间" tooltip，`<span title="2026-08-07 20:04:34">2时<br />3分</span>`、`<td class="rowfollow">9.68<br />GB</td>`。实现时从 `/tmp/bt_listing.html` 截取。

- [ ] **Step 2: 写失败测试**

`tests/test_sites_listing.py`:
```python
import responses
from pathlib import Path
from datetime import datetime, timezone
from pt_shualiu.sites import (parse_listing_rows, PROMO_FREE, PROMO_DOWNLOAD_OK,
                              Site)
from pt_shualiu.config import SiteConfig

FIX = Path(__file__).parent / "fixtures"

def test_parse_carpt_listing_promo_and_expire():
    rows = parse_listing_rows((FIX / "listing_carpt.html").read_text(), site="carpt")
    assert 192933 in rows
    r = rows[192933]
    assert r.promo in PROMO_FREE
    # 页面时间为 +0800，解析后统一转 UTC
    assert r.free_expire_dt == datetime(2026, 8, 14, 12, 13, 30, tzinfo=timezone.utc)
    assert r.leecher == 32 and r.seeder == 1
    assert r.size_bytes == 100 * 1024**2
    assert r.name.startswith("Test Movie")

def test_parse_btschool_listing_units():
    rows = parse_listing_rows((FIX / "listing_btschool.html").read_text(), site="btschool")
    r = next(iter(rows.values()))
    assert r.promo in PROMO_FREE
    assert r.free_expire_dt is None  # BT 无剩余时间 tooltip
    assert r.size_bytes == int(9.68 * 1024**3)
    assert r.site == "btschool"

def test_promo_sets_disjoint():
    assert not (PROMO_FREE & PROMO_DOWNLOAD_OK)

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
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_sites_listing.py -v`
Expected: FAIL

- [ ] **Step 4: 实现 parse_listing_rows**

```python
import requests
from datetime import timedelta
from .config import SiteConfig

PROMO_FREE = frozenset({"pro_free", "pro_free2up"})
PROMO_DOWNLOAD_OK = frozenset({"pro_2up", ""})   # "" = 无标记，下载正常计入

@dataclass
class ListingRow:
    site: str
    site_id: int
    size_bytes: int
    pub_dt: datetime
    seeder: int
    leecher: int
    promo: str
    free_expire_dt: datetime | None
    download_url: str | None
    name: str = ""          # 取自 <a title="...">，供同名去重

ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
NAME_RE = re.compile(r'<a title="([^"]*)"\s+href="details\.php\?id=\d+')
TIME_ATTR_RE = re.compile(r'<span title="(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})">')
SIZE_TD_RE = re.compile(r"<td class='rowfollow'>\s*([\d.]+)\s*<br />\s*(TB|GB|MB|KB)\s*</td>")
SIZE_TD_RE_BT = re.compile(r'<td class="rowfollow">\s*([\d.]+)\s*<br />\s*(TB|GB|MB|KB)\s*</td>')
LEECH_RE = re.compile(r"#leechers[^>]*>.*?<font[^>]*>(\d+)</font>", re.S)
SEED_RE = re.compile(r"#seeders[^>]*>.*?<font[^>]*>(\d+)</font>", re.S)
EXPIRE_RE = re.compile(r'剩余时间：<span title="(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})">')
PROMO_CLASS_RE = re.compile(r'class="(pro_[a-z0-9]+)"')
DOWNLOAD_LINK_RE = re.compile(r'href="download\.php\?id=(\d+)"')

_UNIT = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

def _local_dt_to_utc(s: str) -> datetime:
    # 站点时间为 +0800；兼容 CarPT(无秒)/BT(带秒) 两种格式
    s = s.strip()
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)

def parse_listing_rows(html_text: str, site: str = "") -> dict[int, ListingRow]:
    rows = {}
    for blk in ROW_RE.findall(html_text):
        m_id = ID_RE.search(blk)
        if not m_id or "torrentname" not in blk:
            continue  # 只处理种子行
        sid = int(m_id.group(1))
        m_promo = PROMO_CLASS_RE.search(blk)
        m_size = SIZE_TD_RE.search(blk) or SIZE_TD_RE_BT.search(blk)
        m_time = TIME_ATTR_RE.search(blk)
        m_seed = SEED_RE.search(blk)
        m_leech = LEECH_RE.search(blk)
        m_dl = DOWNLOAD_LINK_RE.search(blk)
        m_exp = EXPIRE_RE.search(blk)
        m_name = NAME_RE.search(blk)
        if not (m_size and m_time):
            continue
        rows[sid] = ListingRow(
            site=site,
            site_id=sid,
            size_bytes=int(float(m_size.group(1)) * _UNIT[m_size.group(2)]),
            pub_dt=_local_dt_to_utc(m_time.group(1)),
            seeder=int(m_seed.group(1)) if m_seed else 0,
            leecher=int(m_leech.group(1)) if m_leech else 0,
            promo=m_promo.group(1) if m_promo else "",
            free_expire_dt=_local_dt_to_utc(m_exp.group(1)) if m_exp else None,
            download_url=f"/download.php?id={sid}" if m_dl else None,
            name=unescape(m_name.group(1)) if m_name else "",
        )
    return rows

class Site:
    """站点适配器：RSS / 列表页统一 HTTP 入口（fetch_myhr 在 Task 5 补充）。"""
    def __init__(self, cfg: SiteConfig, session: requests.Session = None):
        self.name = cfg.name
        self.base_url = cfg.base_url
        self.cookie = cfg.cookie
        self.rss_url = cfg.rss_url
        self.seeding_hours = cfg.seeding_hours
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) pt-shualiu/0.1",
            "Cookie": cfg.cookie})

    def _get(self, url: str) -> str:
        r = self.session.get(url, timeout=20)
        r.raise_for_status()
        return r.text

    def fetch_rss(self) -> list[Candidate]:
        return parse_rss(self._get(self.rss_url), site=self.name)

    def fetch_listing_rows(self, pages: int = 2) -> dict[int, ListingRow]:
        rows = {}
        for p in range(1, pages + 1):
            rows.update(parse_listing_rows(
                self._get(f"{self.base_url}/torrents.php?page={p}"), site=self.name))
        for sid, r in rows.items():
            if r.download_url and r.download_url.startswith("/"):
                r.download_url = self.base_url + r.download_url
        return rows
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_sites_listing.py -v`
Expected: PASS

- [ ] **Step 6: 用真实页面复核枚举完整性**

```bash
cd ~/pt_shualiu
.venv/bin/python - <<'EOF'
from pt_shualiu.sites import parse_listing_rows
html = open('/tmp/carpt_free.html').read()
rows = parse_listing_rows(html)
from collections import Counter
print("rows:", len(rows))
print("promos:", Counter(r.promo for r in rows.values()))
EOF
```
Expected: promos 覆盖 `pro_free / pro_free2up / pro_50pctdown / pro_50pctdown2up / pro_30pctdown / pro_2up / ''`。若出现未覆盖的 `pro_*` 值，补充到 PROMO_FREE/PROMO_DOWNLOAD_OK 判断。

- [ ] **Step 7: 提交**

```bash
cd ~/pt_shualiu
git add pt_shualiu/sites.py tests/fixtures/listing_*.html tests/test_sites_listing.py
git commit -q -m "feat: 列表页解析 + 促销枚举（免费/2x/50%/30%/正常）"
```

---

### Task 5: sites.py — myhr 解析

**Files:**
- Modify: `~/pt_shualiu/pt_shualiu/sites.py`
- Create: `~/pt_shualiu/tests/fixtures/myhr_carpt.html`, `~/pt_shualiu/tests/fixtures/myhr_btschool.html`
- Test: `~/pt_shualiu/tests/test_sites_myhr.py`

**Interfaces:**
- Consumes: `parse_rss` / `parse_listing_rows` 的 `ListingRow`；`SiteConfig`
- Produces: `parse_myhr(html_text, site) -> MyhrReport`；`MyhrReport = (assessment: list[AssessmentMetric], hr: list[HrRecord])`；`AssessmentMetric=(name, required, current, passed:bool)`；`HrRecord=(site, site_id:int, name, remaining_seed_sec:int, complete_dt:datetime|None, status:str)`；`Site.fetch_myhr()`

- [ ] **Step 1: 建 myhr fixture**

`tests/fixtures/myhr_carpt.html`（真实 `/tmp/carpt_myhr.html` 精简为 2 行，保留列头与指标块）：
```html
名称：新人考核 时间：2026-08-07 20:21:07 ~ 2026-09-06 20:21:07
指标1：上传增量, 要求：50 GB, 当前：76.19 MB, 结果： 未通过！
指标2：下载增量, 要求：50 GB, 当前：0.00 KB, 结果： 未通过！
指标3：魔力增量, 要求：5000 , 当前：14, 结果： 未通过！
指标4：做种积分增量, 要求：3000 , 当前：2, 结果： 未通过！
<table width='100%' id='hr-table'><tr>
<td class='colhead' align='center'>H&R ID</td><td class='colhead' align='center'>种子名称</td>
<td class='colhead' align='center'>还需做种时间</td><td class='colhead' align='center'>下载完成时间</td></tr>
<tr>
<td class='rowfollow nowrap' align='center'>7898981</td>
<td class='rowfollow' align='left'><a href='details.php?id=192915'>Whispers of Southern Song 2026 S01E13</a></td>
<td class='rowfollow nowrap' align='center'>0.00 KB</td><td class='rowfollow nowrap' align='center'>378.45 MB</td>
<td class='rowfollow nowrap' align='center'><font color="#ff0000">0.000</font></td>
<td class='rowfollow nowrap' align='center'>1天00:00:00</td>
<td class='rowfollow nowrap' align='center'>2026-08-07 21:39</td>
<td class='rowfollow nowrap' align='center' >9天23:52:26</td></tr>
</table>
```

`tests/fixtures/myhr_btschool.html`：同样精简，需做种时间列格式为 `20:00:00`（时分秒），完成时间含秒 `2026-08-07 20:54:12`。

- [ ] **Step 2: 写失败测试**

`tests/test_sites_myhr.py`:
```python
from pathlib import Path
from pt_shualiu.sites import parse_myhr

FIX = Path(__file__).parent / "fixtures"

def test_parse_carpt_myhr():
    r = parse_myhr((FIX / "myhr_carpt.html").read_text(), site="carpt")
    assert len(r.assessment) == 4
    assert r.assessment[1].name == "下载增量"
    assert r.assessment[1].passed is False
    assert len(r.hr) == 1
    rec = r.hr[0]
    assert rec.site_id == 192915
    assert rec.remaining_seed_sec == 24 * 3600      # 1天 → 86400s

def test_parse_btschool_myhr():
    r = parse_myhr((FIX / "myhr_btschool.html").read_text(), site="btschool")
    assert len(r.hr) == 1
    assert r.hr[0].remaining_seed_sec == 20 * 3600  # 20:00:00 → 72000s

def test_remaining_done_is_zero():
    # 还需做种时间 "00:00:00" 应解析为 0
    assert parse_remaining_sec("00:00:00") == 0
    assert parse_remaining_sec("1天00:00:00") == 86400
    assert parse_remaining_sec("20:00:00") == 72000
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_sites_myhr.py -v`
Expected: FAIL

- [ ] **Step 4: 实现 parse_myhr**

```python
ASSESS_RE = re.compile(r"指标\d+：([^,]+),\s*要求：([^,]+),\s*当前：([^,]+),\s*结果：\s*([^！]+)！")
HR_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<td class='rowfollow[^']*'(?:[^>]*)>(.*?)</td>", re.S)
HRNAME_RE = re.compile(r"<a href='details\.php\?id=(\d+)'>([^<]+)</a>")

@dataclass
class AssessmentMetric:
    name: str
    required: str
    current: str
    passed: bool

@dataclass
class HrRecord:
    site: str
    site_id: int
    name: str
    remaining_seed_sec: int
    complete_dt: datetime | None
    status: str

@dataclass
class MyhrReport:
    assessment: list[AssessmentMetric]
    hr: list[HrRecord]

def parse_remaining_sec(s: str) -> int:
    s = s.strip()
    if "天" in s:
        days, rest = s.split("天", 1)
        h, m, sec = (int(x) for x in rest.split(":"))
        return int(days) * 86400 + h * 3600 + m * 60 + sec
    h, m, sec = (int(x) for x in s.split(":"))
    return h * 3600 + m * 60 + sec

_REMAIN_RE = re.compile(r"^\d+天?\d{2}:\d{2}:\d{2}$")
_COMPLETE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?$")

def _find_remaining(cells) -> int:
    for c in cells:
        if _REMAIN_RE.match(c):
            return parse_remaining_sec(c)
    return 0

def _find_complete(cells):
    for c in cells:
        if _COMPLETE_RE.match(c):
            return _local_dt_to_utc(c)
    return None

def parse_myhr(html_text: str, site: str) -> MyhrReport:
    assessment = [AssessmentMetric(name=m.group(1), required=m.group(2),
                                   current=m.group(3), passed=(m.group(4).strip() == "通过"))
                  for m in ASSESS_RE.finditer(html_text)]
    hr = []
    for blk in HR_ROW_RE.findall(html_text):
        m_n = HRNAME_RE.search(blk)
        if not m_n:
            continue
        sid = int(m_n.group(1))
        name = re.sub(r"\s+", " ", unescape(m_n.group(2))).strip()
        cells = [re.sub(r"<[^>]+>", " ", c).replace("\n", " ").strip()
                 for c in CELL_RE.findall(blk)]
        hr.append(HrRecord(site=site, site_id=sid, name=name,
                           remaining_seed_sec=_find_remaining(cells),
                           complete_dt=_find_complete(cells), status="考察中"))
    return MyhrReport(assessment=assessment, hr=hr)
```

> 注：`_find_remaining` 取"首个匹配 `N天HH:MM:SS`/`HH:MM:SS` 的单元格"——CarPT 的"还需做种时间"列必然排在"剩余时间(9天23:52:26)"列之前，首匹配即所需列，不受 CarPT(8列)/BT(9列，含"20000魔力值免罪"列)列数差异影响；`_find_complete` 同理按时间格式定位。用真实 `/tmp/bt_myhr.html` 复核两站列序。

Task 5 结尾给 `Site` 补 `fetch_myhr`：
```python
    def fetch_myhr(self) -> MyhrReport:
        return parse_myhr(self._get(f"{self.base_url}/myhr.php"), site=self.name)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_sites_myhr.py -v`
Expected: PASS

- [ ] **Step 6: 用真实 myhr 页面复核**

```bash
cd ~/pt_shualiu
.venv/bin/python - <<'EOF'
from pt_shualiu.sites import parse_myhr
for path, site in [('/tmp/carpt_myhr.html','carpt'), ('/tmp/bt_myhr.html','btschool')]:
    r = parse_myhr(open(path).read(), site=site)
    print(site, "assessment:", [(a.name, a.current, a.passed) for a in r.assessment])
    print(site, "hr rows:", [(x.site_id, x.name[:20], x.remaining_seed_sec) for x in r.hr])
EOF
```
Expected: carpt 4 条指标 + 5 行 hr；btschool 3 条指标 + 11 行 hr，时间解析正确。

- [ ] **Step 7: 提交**

```bash
cd ~/pt_shualiu
git add pt_shualiu/sites.py tests/fixtures/myhr_*.html tests/test_sites_myhr.py
git commit -q -m "feat: myhr 解析（考核指标 + H&R 明细/剩余保种秒数）"
```

---

### Task 6: selection.py — 双轨选种

**Files:**
- Create: `~/pt_shualiu/pt_shualiu/selection.py`
- Test: `~/pt_shualiu/tests/test_selection.py`

**Interfaces:**
- Consumes: `Candidate`、`ListingRow`、`Config`；`PROMO_FREE`、`PROMO_DOWNLOAD_OK`、`download_fraction(promo)->float`
- Produces: `select(candidates, listing_by_id, history_ids:set, known_names:set, disk_free_bytes, cfg, now, download_concurrent=0) -> Selection`；`Selection=(free:list[Candidate], download:list[ListingRow])`

- [ ] **Step 1: 写失败测试**

`tests/test_selection.py`（用 Task 3/4 的 `Candidate`/`ListingRow` 构造）:
```python
from datetime import datetime, timedelta, timezone
from pt_shualiu.selection import select
from pt_shualiu.sites import Candidate, ListingRow
from pt_shualiu.config import Config

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

def test_download_track_concurrent_cap():
    # 同时下载中的下载轨种子 ≥2 → 本轮不再加（设计 §4.4）
    rows = [
        ListingRow(site="carpt", site_id=30, size_bytes=1*1024**3, pub_dt=NOW-timedelta(hours=2),
                   seeder=1, leecher=8, promo="pro_2up", free_expire_dt=None, download_url="d"),
    ]
    res = select([], {30: rows[0]}, set(), set(), 20*1024**3, _cfg(), NOW,
                 download_concurrent=2)
    assert res.download == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_selection.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 selection.py**

```python
"""双轨选种：免费轨（刷上传/魔力/做种）+ 下载轨（机会型凑下载量）。

去重说明（设计文档"按 info_hash 比对"的等价实现）：RSS 不含 info_hash，
用两层替代——(1) SQLite history 的 (site, site_id)（本工具加过的种，含 qbit
新加种 tag `site:{site}:{id}` 解析出的 id，由 daemon 并入）；(2) qbit 里同名
种子 known_names（覆盖已有 18 个无 tag 老种）。两站各走各的 select 调用，
history_ids/known_names 均为单站点集合，无跨站 id 冲突。
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from .sites import Candidate, ListingRow, PROMO_FREE, PROMO_DOWNLOAD_OK, download_fraction

@dataclass
class Selection:
    free: list
    download: list

def _age_hours(cand, now):
    return (now - cand.pub_dt).total_seconds() / 3600.0

def select(candidates, listing_by_id, history_ids, known_names, disk_free_bytes,
           cfg, now, download_concurrent=0):
    if disk_free_bytes < cfg.disk_min_free_bytes:
        return Selection([], [])
    free, download = [], []
    # 免费轨：RSS 候选 + 列表页促销校验
    for c in sorted(candidates, key=lambda x: x.pub_dt, reverse=True):
        if c.site_id in history_ids or c.name in known_names:
            continue
        row = listing_by_id.get(c.site_id)
        if not row or row.promo not in PROMO_FREE:
            continue
        if c.size_bytes > cfg.free_max_size_bytes:
            continue
        if _age_hours(c, now) > cfg.free_max_age_hours:
            continue
        if row.seeder <= 0 and row.leecher <= 0:
            continue  # 跳过死种；leecher>0（上传价值）或 seeder>0（保种价值）任一即接受
        est_min = (c.size_bytes / cfg.rate_high_bytes) / 60.0
        if row.free_expire_dt and (row.free_expire_dt - now).total_seconds() / 60.0 < est_min + cfg.free_expire_buffer_min:
            continue
        free.append(c)
    # 下载轨：列表页里促销=2up/正常/打折；受在途数约束。无年龄上限——下载轨的价值
    # 在累计 50GB 下载指标（非免费种），非上传；CarPT 90% 新种免费，全量计入的非免费种
    # 往往是免费到期后的"老种"，设年龄上限会饿死下载配额池（设计 §4.4 过滤器清单无年龄）。
    if download_concurrent < 2:
        for sid, r in listing_by_id.items():
            if sid in history_ids or r.name in known_names:
                continue
            if r.promo not in PROMO_DOWNLOAD_OK:
                continue
            if r.size_bytes > cfg.download_max_size_bytes:
                continue
            if r.leecher <= 0:
                continue
            download.append(r)
    # 下载轨排序：download_fraction 高的优先（全量计入指标的 2up/无标记=1.0 优先于
    # 打折种 0.5/0.3——同样磁盘占用下给 50GB 下载指标贡献更大，即"选种排序用有效大小"），
    # 同分数再按 leecher 降序（上传价值）。
    download.sort(key=lambda r: (download_fraction(r.promo), r.leecher), reverse=True)
    return Selection(free[:3], download[:1])
```

`Selection` 用 `dataclass` 定义（`free: list`, `download: list`）。`download_concurrent` 由调用方传当前正在下载的下载轨种子数（Task 10 从 qbit 状态取）。

> 语义澄清（控制器 2026-08-07 修正，记录于 ledger）：(1) **下载轨每轮最多加 1 个**（`download[:1]`），同时下载中下载轨种子 ≤2（`download_concurrent < 2` 闸门）——设计文档 §4.4 明确"每轮最多加 1 个"，不与测试的 {10,11} 混淆；(2) **下载轨独立运行**，不以"免费轨本轮无候选"为前置——CarPT 90% 新种免费，若下载轨只在免费轨空时才跑，50GB 下载指标永远凑不满，考核必挂；"优先级低于免费轨"由 qbit 每种优先级实现（免费轨=高优先、下载轨=普通，Task 2 已封装 `set_per_torrent_priority`），不在选种层闸门。(3) **按比例统计**（用户"按比例统计大小"要求）：选种排序用 `download_fraction`（本任务）；mailer/daemon 统计口径由调用方 `download_fraction(r.promo) * r.size_bytes` 得有效指标大小（Task 9/10 指针），磁盘占用仍按全量 `size_bytes`。
>
> ⚠️ **review fix round 1（控制器裁决，设计文档为准）**：reviewer 判 2 个 Important 均为 brief 与设计 §4.4 冲突——(a) 免费轨 leecher 条件从严格 `leecher>0` 改为设计原文 `leecher>0 或 seeder>0`（仅 `seeder==0 且 leecher==0` 的死种跳过）；(b) 移除下载轨 24h 年龄上限（设计过滤器清单无年龄；下载轨价值在凑下载配额不在上传，年龄上限饿死配额池）。测试随之改：free 轨道接受 seeder-only 候选（{1,5}）、新增死种候选 8 验证排除、新增 `test_download_track_concurrent_cap`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_selection.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd ~/pt_shualiu
git add pt_shualiu/selection.py tests/test_selection.py
git commit -q -m "feat: 双轨选种（免费≤500MB<12h + 机会型下载轨≤2GB）"
```

---

### Task 7: seeding.py — 保种生命周期 + 删除规则

**Files:**
- Create: `~/pt_shualiu/pt_shualiu/seeding.py`
- Test: `~/pt_shualiu/tests/test_seeding.py`

**Interfaces:**
- Consumes: `QbitClient`、`State`、`Config`、`Site.fetch_myhr()`、`HrRecord`
- Produces: `SeedingManager`，方法：`ingest_existing()`、`check(now) -> list[DeleteDecision]`；`DeleteDecision=(hash, reason:str)`；`no_upload_for(torrent, state) -> bool`

- [ ] **Step 1: 写失败测试（用 fake qbit/site）**

`tests/test_seeding.py`:
```python
from datetime import datetime, timezone, timedelta
from pt_shualiu.seeding import SeedingManager
from pt_shualiu.config import Config
from pt_shualiu.state import State
from pt_shualiu.sites import MyhrReport, HrRecord

NOW = datetime(2026, 8, 7, 20, 0, 0, tzinfo=timezone.utc)

class FakeQbit:
    def __init__(self, torrents): self.torrents = torrents; self.deleted = []
    def all_pt_torrents(self, domains): return list(self.torrents)
    def delete_torrents(self, hashes, delete_files=True): self.deleted.extend(hashes)
    def sync_maindata(self): return {"free_space_on_disk": 20*1024**3}

class FakeSite:
    def __init__(self, hr): self.hr = hr
    def fetch_myhr(self): return MyhrReport(assessment=[], hr=self.hr)

def _cfg():
    return Config(sites=[], qbit_url="", qbit_user="", qbit_pass="")

def _torrent(hash_, completed_at, site="carpt", uploaded=1000, state="uploading", category="pt_free"):
    return {"hash": hash_, "site": site, "category": category, "progress": 1.0,
            "completion_on": completed_at.timestamp(), "uploaded": uploaded,
            "state": state, "name": f"N-{hash_}", "size": 100*1024**2}

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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_seeding.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 seeding.py**

```python
"""保种生命周期：全量纳管 PT 种子，按网站 myhr 判断达标，达标后 10 分钟无上传删除。"""
from datetime import datetime
from dataclasses import dataclass
from .sites import HrRecord

@dataclass
class DeleteDecision:
    hash: str
    reason: str

class SeedingManager:
    def __init__(self, qbit, sites: dict, state, cfg):
        self.qbit = qbit
        self.sites = sites          # {name: Site}
        self.state = state
        self.cfg = cfg
        self.known = {}             # hash -> torrent dict（含 site）

    def ingest_existing(self):
        domains = {s.name: s.tracker_domain for s in self.cfg.sites}
        for t in self.qbit.all_pt_torrents(domains):
            self.known[t["hash"]] = t
            if self.state:
                # ⚠️ 控制器修正（Task 1 minor 指针）：upsert_torrent 是 INSERT OR REPLACE，
                # 会清空 completion_on/last_uploaded/last_upload_change/added_at。已存在的行
                # 必须走 update_torrent（保字段），仅新行 upsert——否则每次重启都会重置已完成
                # 种子的保种状态，导致"达标且10分钟无上传"判定失效。
                if self.state.get_torrent(t["hash"]):
                    self.state.update_torrent(t["hash"], site=t.get("site", ""),
                                              category=t.get("category", ""),
                                              name=t.get("name", ""))
                else:
                    self.state.upsert_torrent(t["hash"], t.get("site", ""),
                                              t.get("category", ""), t.get("name", ""),
                                              added_at=datetime.now().timestamp())

    def check(self, now):
        domains = {s.name: s.tracker_domain for s in self.cfg.sites}
        for t in self.qbit.all_pt_torrents(domains):
            self.known[t["hash"]] = t
        decisions = []
        myhr_by_site = {name: site.fetch_myhr() for name, site in self.sites.items()}
        for hash_, t in list(self.known.items()):
            site = t.get("site")
            if not site or site not in myhr_by_site:
                continue
            if t.get("progress", 1.0) < 1.0:
                continue  # 未下载完成，不处理
            rec = self._find_hr(myhr_by_site[site].hr, t)
            if rec and rec.remaining_seed_sec > 0:
                # 站点延迟兜底：completion + window + 30min 已过
                deadline = (t.get("completion_on", 0) or 0) + self._window_sec(site) * 1.0 + self.cfg.site_lag_override_min * 60
                if deadline and now.timestamp() > deadline:
                    decisions.append(DeleteDecision(hash_, "站点30分钟未刷新兜底"))
                continue
            # 达标：网站已归零 / 种不在 myhr（无义务）
            if self._no_upload_for(hash_, t, now):
                decisions.append(DeleteDecision(hash_, "达标且10分钟无上传"))
        # 磁盘紧张：达标+无上传之外，先删最大的达标种
        self._disk_pressure(now, decisions)
        return decisions

    def _window_sec(self, site_name):
        for s in self.cfg.sites:
            if s.name == site_name:
                return s.seeding_hours * 3600
        return 24 * 3600

    def _find_hr(self, hr_list, t):
        # 按名称匹配 H&R 记录（两站 myhr 名称与 qbit 种子名一致）
        for r in hr_list:
            if r.name and r.name == t.get("name"):
                return r
        return None

    def _no_upload_for(self, hash_, t, now):
        # 用上传字节快照：state 里记录 last_uploaded 与 last_upload_change
        if not self.state:
            return False
        cur = t.get("uploaded", 0)
        st = self.state.get_torrent(hash_)
        if not st or st["last_uploaded"] != cur:
            self.state.update_torrent(hash_, last_uploaded=cur,
                                      last_upload_change=now.timestamp())
            return False
        last_change = st["last_upload_change"] or now.timestamp()
        return (now.timestamp() - last_change) >= self.cfg.no_upload_min * 60

    def _disk_pressure(self, now, decisions):
        if not self.qbit:
            return
        free = self.qbit.sync_maindata().get("free_space_on_disk", 1 << 60)
        if free >= self.cfg.disk_min_free_bytes:
            return
        # 达标但还因"正在上传"而保留的，按大小降序删
        done = [t for h, t in self.known.items()
                if t.get("progress", 1.0) >= 1.0 and not any(d.hash == h for d in decisions)]
        done.sort(key=lambda t: t.get("size", 0), reverse=True)
        for t in done:
            if free >= self.cfg.disk_min_free_bytes:
                break
            decisions.append(DeleteDecision(t["hash"], "磁盘不足"))
            free += t.get("size", 0)
```

> 注：`_no_upload_for` 依赖 `state` 的上传快照。首次见到某 hash 时仅记录快照返回 False（不删），下一轮上传量未变且间隔 ≥10 分钟才删——测试用 `_seed_upload_snapshot` 预置快照来模拟"已无上传一段时间"。真实运行时 state 为 `State` 实例（Task 1 提供），`check()` 每 10 分钟跑一次自然满足间隔。
> `check()` 返回 decisions 后，由 daemon 调用 `self.qbit.delete_torrents(...)` 实际删除并写 `history`（站点 id 去重）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_seeding.py -v`
Expected: PASS（测试用真实 `State` 预置上传快照，覆盖 `_no_upload_for` 逻辑）

- [ ] **Step 5: 提交**

```bash
cd ~/pt_shualiu
git add pt_shualiu/seeding.py tests/test_seeding.py
git commit -q -m "feat: 保种生命周期（myhr达标/10分钟无上传/30分钟站点兜底/磁盘优先）"
```

---

### Task 8: ratelimit.py — 限速状态机

**Files:**
- Create: `~/pt_shualiu/pt_shualiu/ratelimit.py`
- Test: `~/pt_shualiu/tests/test_ratelimit.py`

**Interfaces:**
- Consumes: `QbitClient`、`Config`、`State`
- Produces: `DirectionController`（`update(speed, now) -> int(新limit字节)`、`to_state()/restore(state)`）、`RateManager`（`run_once(up_speed, down_speed, now)`）

- [ ] **Step 1: 写失败测试**

`tests/test_ratelimit.py`:
```python
from datetime import datetime, timedelta, timezone
from pt_shualiu.ratelimit import DirectionController

def _mk(direction="up"):
    return DirectionController(direction=direction, limit_high=500*1024,
                               limit_low=200*1024, trigger_min=60 if direction=="up" else 120,
                               low_hold_min=60, cap_fraction=0.95)

def test_high_at_cap_triggers_down_after_trigger():
    c = _mk("up")
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    limit = c.limit_high
    for i in range(60):
        limit = c.update(495*1024, now + timedelta(minutes=i))
    assert limit == 200*1024            # 60 分钟跑满 → 降速
    assert c.mode == "low"

def test_low_recover_after_hold():
    c = _mk("up")
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    for i in range(60):
        c.update(495*1024, now + timedelta(minutes=i))  # 进入 low
    limit = c.limit_high
    for i in range(60):
        limit = c.update(190*1024, now + timedelta(minutes=60+i))  # low 期保持
    assert limit == 500*1024            # 60 分钟后恢复
    assert c.mode == "high"

def test_high_resets_when_below_cap():
    c = _mk("up")
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    c.update(100*1024, now)
    c.update(495*1024, now + timedelta(minutes=1))   # 只跑了 1 分钟
    c.update(100*1024, now + timedelta(minutes=2))   # 中断 → 计数清零
    limit = c.limit_high
    for i in range(59):
        limit = c.update(495*1024, now + timedelta(minutes=3+i))
    assert limit == 500*1024            # 重新连续 59 分钟，未触发（还差 1 分钟）

def test_down_requires_120():
    c = _mk("down")
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    limit = c.limit_high
    for i in range(60):
        limit = c.update(495*1024, now + timedelta(minutes=i))
    assert limit == 500*1024            # 下载 60 分钟不触发
    for i in range(60, 120):
        limit = c.update(495*1024, now + timedelta(minutes=i))
    assert limit == 200*1024            # 下载 120 分钟触发
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_ratelimit.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 ratelimit.py**

```python
"""上传/下载独立限速状态机：HIGH(500) 跑满触发 → LOW(200) 保持后恢复。"""
from dataclasses import dataclass, asdict

@dataclass
class DirectionController:
    direction: str            # "up" | "down"
    limit_high: int           # 500*1024 bytes/s
    limit_low: int            # 200*1024 bytes/s
    trigger_min: int          # up=60, down=120
    low_hold_min: int         # 60
    cap_fraction: float       # 0.95
    mode: str = "high"
    cap_minutes: int = 0
    low_remaining_min: int = 0

    def update(self, speed: int, now) -> int:
        if self.mode == "high":
            if speed >= self.limit_high * self.cap_fraction:
                self.cap_minutes += 1
                if self.cap_minutes >= self.trigger_min:
                    self.mode = "low"
                    self.low_remaining_min = self.low_hold_min
                    self.cap_minutes = 0
                    return self.limit_low
            else:
                self.cap_minutes = 0
            return self.limit_high
        else:  # low：计时器递减，归零恢复
            self.low_remaining_min -= 1
            if self.low_remaining_min <= 0:
                self.mode = "high"
                return self.limit_high
            return self.limit_low

    def to_state(self):
        return asdict(self)

    def restore(self, state: dict):
        self.mode = state.get("mode", "high")
        self.cap_minutes = state.get("cap_minutes", 0)
        self.low_remaining_min = state.get("low_remaining_min", 0)

class RateManager:
    def __init__(self, qbit, cfg, state):
        self.qbit = qbit
        self.cfg = cfg
        self.state = state
        self.up = DirectionController("up", cfg.rate_high_bytes, cfg.rate_low_bytes,
                                      cfg.up_trigger_min, cfg.low_hold_min, cfg.cap_fraction)
        self.down = DirectionController("down", cfg.rate_high_bytes, cfg.rate_low_bytes,
                                        cfg.down_trigger_min, cfg.low_hold_min, cfg.cap_fraction)

    def run_once(self, up_speed, down_speed, now):
        up_limit = self.up.update(up_speed, now)
        down_limit = self.down.update(down_speed, now)
        self.qbit.set_global_limits(up_limit, down_limit)
        if self.state:
            self.state.set_rate_state("up", self.up.mode, up_limit,
                                      self.up.cap_minutes, self.up.low_remaining_min)
            self.state.set_rate_state("down", self.down.mode, down_limit,
                                      self.down.cap_minutes, self.down.low_remaining_min)
        return up_limit, down_limit
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_ratelimit.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd ~/pt_shualiu
git add pt_shualiu/ratelimit.py tests/test_ratelimit.py
git commit -q -m "feat: 限速状态机（上传60min/下载120min触发降速，1h恢复）"
```

---

### Task 9: mailer.py — 163 邮件摘要

**Files:**
- Create: `~/pt_shualiu/pt_shualiu/mailer.py`
- Test: `~/pt_shualiu/tests/test_mailer.py`

**Interfaces:**
- Consumes: `Config`（mail_*）、各 `Site.fetch_myhr()` 结果
- Produces: `build_summary(site_reports: dict[str, MyhrReport], stats: dict) -> str`、`send_mail(subject, body, cfg)`

- [ ] **Step 1: 写失败测试**

`tests/test_mailer.py`:
```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_mailer.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 mailer.py**

```python
"""163 邮件摘要（复用 seedhub 的 SMTP_SSL 模式，授权码走 .env）。"""
import smtplib
from email.mime.text import MIMEText
from email.header import Header

def build_summary(site_reports, stats) -> str:
    lines = ["PT刷流日报", "=" * 20, ""]
    for site, rep in site_reports.items():
        lines.append(f"【{site}】")
        for a in rep.assessment:
            status = "已通过" if a.passed else "未通过"
            mark = "✅" if a.passed else "❌"
            # name 取解析出的真实指标名（Task 5），不硬编码；BT 合成达标项 passed=True
            # 且 current==required，直接以 passed 旗标渲染，无需解析字符串差值
            lines.append(f"  {mark} {a.name}: {a.current} / {a.required}（{status}）")
        lines.append(f"  在保种子: {stats.get(site, {}).get('in_seed', 0)}")
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/test_mailer.py -v`
Expected: PASS

- [ ] **Step 5: 真实发一封测试邮件（可选，验证授权码）**

```bash
cd ~/pt_shualiu
.venv/bin/python - <<'EOF'
from dotenv import load_dotenv; load_dotenv()
from pt_shualiu.config import load_config
from pt_shualiu.mailer import send_mail
c = load_config()
send_mail("pt-shualiu 测试", "你好，这是一封测试邮件。", c)
print("sent ok")
EOF
```
Expected: 打印 sent ok，163 邮箱收到。失败则检查授权码/SSL。

- [ ] **Step 6: 提交**

```bash
cd ~/pt_shualiu
git add pt_shualiu/mailer.py tests/test_mailer.py
git commit -q -m "feat: 163 邮件摘要"
```

---

### Task 10: daemon.py + status.sh + systemd 单元

**Files:**
- Create: `~/pt_shualiu/pt_shualiu/daemon.py`
- Create: `~/pt_shualiu/status.sh`
- Create: `~/pt_shualiu/pt_shualiu.service`
- Test: `~/pt_shualiu/tests/test_daemon.py`（调度与 dry-run 逻辑）

**Interfaces:**
- Consumes: 全部模块
- Produces: `main(argv)`（`--dry-run` / `--once` / `--check`），可被 systemd 调起

- [ ] **Step 1: 写 daemon.py 主循环**

```python
"""主循环：选种/保种/限速/邮件 四周期调度。"""
import argparse, logging, signal, time
from datetime import datetime, timezone
from pathlib import Path
from .config import load_config
from .qbit import QbitClient
from .sites import Site
from .state import State
from .selection import select
from .seeding import SeedingManager
from .ratelimit import RateManager
from .mailer import send_mail, build_summary

log = logging.getLogger("pt_shualiu")

class App:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.qbit = QbitClient(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
        self.state = State(Path(__file__).parent.parent / "pt_shualiu.db")
        self.sites = {s.name: Site(s) for s in cfg.sites}
        self.seeding = SeedingManager(self.qbit, self.sites, self.state, cfg)
        self.rates = RateManager(self.qbit, cfg, self.state)
        self.last_selection = 0.0
        self.last_seeding = 0.0
        self.last_mail = None

    def startup_check(self):
        self.qbit.login()
        self.seeding.ingest_existing()
        self._restore_rates()   # ⚠️ 控制器修正：设计 §4.6"状态存 SQLite（重启不丢）"
        log.info("startup: qbit ok, known=%d", len(self.seeding.known))

    def _restore_rates(self):
        # RateManager 构造了全新 DirectionController，须从 SQLite 恢复 mode/cap_minutes/
        # low_remaining_min——否则重启后 HIGH 期连续计数归零、LOW 期计时器复位，降速/恢复
        # 语义被破坏（如 200KB/s 已保持 50 分钟，重启后又要重新 60 分钟）。
        for d in ("up", "down"):
            st = self.state.get_rate_state(d)
            ctrl = self.rates.up if d == "up" else self.rates.down
            ctrl.restore(st)

    def run_selection(self, now):
        domains = {s.name: s.tracker_domain for s in self.cfg.sites}
        qbt = self.qbit.all_pt_torrents(domains)   # 每项带 site 字段 + tags
        for site in self.cfg.sites:
            try:
                cands = site.fetch_rss()
                listing = site.fetch_listing_rows(pages=2)
            except Exception as e:
                log.warning("site %s fetch failed: %s", site.name, e)
                continue
            # 本站点去重集：SQLite 历史 + qbit 中本站种子的 tag(site:id) 与同名
            known_ids = set(self.state.history_ids(site.name))
            known_names = set()
            for t in qbt:
                if t.get("site") != site.name:
                    continue
                known_names.add(t.get("name", ""))
                for tag in t.get("tags", []) or []:
                    prefix = f"site:{site.name}:"
                    if tag.startswith(prefix):
                        known_ids.add(int(tag.split(":")[2]))
            download_concurrent = sum(
                1 for t in qbt
                if t.get("category") == self.cfg.cat_download and t.get("progress", 1.0) < 1.0)
            disk_free = self.qbit.sync_maindata().get("free_space_on_disk", 0)
            res = select(cands, listing, known_ids, known_names,
                         disk_free, self.cfg, now, download_concurrent=download_concurrent)
            for c in res.free:
                if self.dry_run:
                    log.info("[dry-run] add free %s id=%s %sMB", site.name, c.site_id, c.size_bytes//1048576)
                    continue
                try:
                    h = self.qbit.add_torrent(c.download_url, save_path=self.cfg.save_path,
                                              category=self.cfg.cat_free, paused=False,
                                              priority=3, tags=[f"site:{site.name}:{c.site_id}"])
                    self.state.upsert_torrent(h, site.name, self.cfg.cat_free, c.name, time.time())
                    self.state.add_history(site.name, c.site_id)
                    log.info("added free %s id=%s hash=%s", site.name, c.site_id, h[:12])
                except Exception as e:
                    log.warning("add free failed: %s", e)
            for r in res.download:
                if self.dry_run:
                    log.info("[dry-run] add download %s id=%s %sMB", site.name, r.site_id, r.size_bytes//1048576)
                    continue
                try:
                    h = self.qbit.add_torrent(r.download_url, save_path=self.cfg.save_path,
                                              category=self.cfg.cat_download, paused=False,
                                              priority=2, tags=[f"site:{site.name}:{r.site_id}"])
                    self.state.upsert_torrent(h, site.name, self.cfg.cat_download, f"id-{r.site_id}", time.time())
                    self.state.add_history(site.name, r.site_id)
                    log.info("added download %s id=%s hash=%s", site.name, r.site_id, h[:12])
                except Exception as e:
                    log.warning("add download failed: %s", e)

    def run_seeding(self, now):
        decisions = self.seeding.check(now)
        for d in decisions:
            if self.dry_run:
                log.info("[dry-run] delete %s (%s)", d.hash[:12], d.reason)
                continue
            try:
                self.qbit.delete_torrents([d.hash])
                self.state.remove_torrent(d.hash)
                log.info("deleted %s (%s)", d.hash[:12], d.reason)
            except Exception as e:
                log.warning("delete failed %s: %s", d.hash[:12], e)

    def run_mail(self, now):
        reports = {name: site.fetch_myhr() for name, site in self.sites.items()}
        stats = {}
        for t in self.state.all_torrents():
            stats.setdefault(t["site"], {"in_seed": 0})["in_seed"] += 1
        body = build_summary(reports, stats)
        if self.dry_run:
            log.info("[dry-run] mail:\n%s", body)
            return
        send_mail("PT刷流日报", body, self.cfg)

    def loop_once(self, now):
        if time.time() - self.last_selection >= self.cfg.poll_selection_s:
            self.run_selection(now); self.last_selection = time.time()
        if time.time() - self.last_seeding >= self.cfg.poll_seeding_s:
            self.run_seeding(now); self.last_seeding = time.time()
        md = self.qbit.sync_maindata()
        self.rates.run_once(md.get("up_info_speed", 0), md.get("dl_info_speed", 0), now)
        hhmm = now.strftime("%H:%M")
        if hhmm == self.cfg.mail_send_time and self.last_mail != now.date().isoformat():
            self.run_mail(now); self.last_mail = now.date().isoformat()

    def run(self):
        self.startup_check()
        stop = False
        def _sig(*_): nonlocal stop; stop = True
        signal.signal(signal.SIGTERM, _sig); signal.signal(signal.SIGINT, _sig)
        while not stop:
            now = datetime.now(timezone.utc)
            try:
                self.loop_once(now)
            except Exception as e:
                log.exception("loop error: %s", e)
            time.sleep(self.cfg.poll_rate_s)

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    app = App(cfg, dry_run=args.dry_run)
    if args.check:
        app.startup_check()
        print("ok, known:", len(app.seeding.known))
        return
    if args.once:
        app.loop_once(datetime.now(timezone.utc))
        return
    app.run()
```

> 注：`download_concurrent` 与 tag 去重已在 `run_selection` 内联实现；`known_names` 覆盖已有 18 个无 tag 老种的同名去重。qbit `torrents/info` 返回的每项自带 `tags` 数组（v4.6.3 支持）。

- [ ] **Step 2: 写测试（dry-run 与 check 可跑）**

`tests/test_daemon.py`:
```python
import pytest
from pt_shualiu.daemon import main

def test_check_dry_run_without_creds(tmp_path, monkeypatch):
    # 无 .env 时应报错而非静默
    monkeypatch.chdir(tmp_path)
    with pytest.raises(KeyError):
        main(["--check"])
```

- [ ] **Step 3: 跑测试确认通过 + 真实 dry-run**

```bash
cd ~/pt_shualiu
.venv/bin/pytest tests/ -v                      # 全量单测
.venv/bin/python -m pt_shualiu.daemon --check   # 有 .env 后：连 qbit+抓两站，打印 known
.venv/bin/python -m pt_shualiu.daemon --once --dry-run   # 只报告会加/会删，不执行
```
Expected: `--check` 打印 ok；`--once --dry-run` 打印本轮候选但不加种不删种。

- [ ] **Step 4: 写 status.sh**

`status.sh`:
```bash
#!/usr/bin/env bash
# 查看 PT刷流 当前进度
cd "$(dirname "$0")"
set -e
.venv/bin/python - <<'EOF'
import logging; logging.disable(logging.CRITICAL)
from pt_shualiu.config import load_config
from pt_shualiu.sites import Site
from pt_shualiu.qbit import QbitClient
cfg = load_config()
q = QbitClient(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
print("=== 考核进度 ===")
for s in cfg.sites:
    try:
        rep = Site(s).fetch_myhr()
        for a in rep.assessment:
            mark = "✅" if a.passed else "❌"
            print(f"  [{s.name}] {mark} {a.name}: {a.current} / {a.required}")
    except Exception as e:
        print(f"  [{s.name}] 站点读取失败: {e}")
print("=== qbit 状态 ===")
md = q.sync_maindata()
print("  磁盘剩余: %.1f GB  下载速度: %.0f KB/s  上传速度: %.0f KB/s"
      % (md.get("free_space_on_disk",0)/1024**3, md.get("dl_info_speed",0)/1024, md.get("up_info_speed",0)/1024))
EOF
```
`chmod +x status.sh`

- [ ] **Step 5: 写 systemd 单元**

`pt_shualiu.service`:
```ini
[Unit]
Description=PT Shualiu daemon
After=network-online.target qbittorrent-nox.service
Wants=network-online.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/path/to/pt_shualiu
ExecStart=/path/to/pt_shualiu/.venv/bin/python -m pt_shualiu.daemon
Restart=on-failure
RestartSec=10
EnvironmentFile=/path/to/pt_shualiu/.env

[Install]
WantedBy=multi-user.target
```

（把部署步骤留到 Task 11。）

- [ ] **Step 6: 提交**

```bash
cd ~/pt_shualiu
git add pt_shualiu/daemon.py status.sh pt_shualiu.service tests/test_daemon.py
chmod +x status.sh
git commit -q -m "feat: daemon 主循环 + status.sh + systemd 单元"
```

---

### Task 11: 真实站点集成冒烟 + 部署上线

**Files:**
- Modify: 无新文件（部署操作）

**Interfaces:**
- Consumes: 全部模块 + `.env`

- [ ] **Step 1: 全量单测**

Run: `cd ~/pt_shualiu && .venv/bin/pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 2: dry-run 冒烟（真实站点）**

```bash
cd ~/pt_shualiu
.venv/bin/python -m pt_shualiu.daemon --once --dry-run 2>&1 | tail -20
```
Expected: 打印本轮会选哪些免费种/下载轨种、会删哪些种，不含"added/delete 实际执行"。人工核对选种符合预期（免费、≤500MB、<12h）。

- [ ] **Step 3: 真实冒烟（一小轮，实际加种）**

```bash
cd ~/pt_shualiu
timeout 120 .venv/bin/python -m pt_shualiu.daemon --once 2>&1 | tail -30
```
Expected: 日志出现 `added free ... hash=...`（或 `added download`）。随后：
```bash
curl -s -b /tmp/qbcookies "http://127.0.0.1:9091/api/v2/torrents/info" --max-time 10 | python3 -c "import sys,json; print(len(json.load(sys.stdin)),'torrents')"
```
确认新种已进 `pt_free`/`pt_download` 分类且带 tag `site:xxx:id`。若加种失败，检查下载链接可访问性（cookie）与 save_path 权限。

- [ ] **Step 4: 部署 systemd**

```bash
sudo cp ~/pt_shualiu/pt_shualiu.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pt_shualiu
sudo systemctl status pt_shualiu --no-pager | head -15
```
Expected: active (running)。看日志：
```bash
journalctl -u pt_shualiu -n 30 --no-pager
```
Expected: 正常循环（限速状态、选种结果、保种检查）。

- [ ] **Step 5: 观察一小时后确认限速状态机**

```bash
journalctl -u pt_shualiu --since "60 min ago" | grep -iE "limit|降速|200|500" | tail
```
以及确认上传/下载确有流量：
```bash
./status.sh
```
Expected: status.sh 显示考核进度、磁盘剩余、实时速度。

- [ ] **Step 6: 提交剩余文件并收尾**

```bash
cd ~/pt_shualiu
git add -A
git commit -q -m "chore: 部署配置与冒烟验证"
echo "done"
```
（`.env`、`downloads/`、`*.db` 已被 gitignore，不会误提交凭证。）

## 验收清单（对照 spec）

- [ ] 两站 RSS 候选解析正确（id/大小/UTC 时间）
- [ ] 列表页促销枚举完整（6 种 + 无标记），免费/2x/50%/30% 判定正确
- [ ] myhr 指标与 H&R 明细解析正确（1天/20:00:00 两种格式）
- [ ] 免费轨只选 免费+≤500MB+<12h+leecher>0+免费未快到期
- [ ] 下载轨只选 2up/正常 + ≤2GB + leecher>0
- [ ] 已有 18 个 PT 种子被纳管，达标+10 分钟无上传自动删除
- [ ] 30 分钟站点未刷新兜底删除生效
- [ ] 磁盘 <3GB 停止新增并优先删大种
- [ ] 上传 500→200 需连续 60 分钟、下载需连续 120 分钟、恢复 60 分钟
- [ ] 全局限速字节/秒单位验证通过（Task 2 Step 5 结论已落实）
- [ ] 每日 09:00 邮件摘要发出（含两站考核 4 指标）
- [ ] systemd 常驻，重启自动拉起；`status.sh` 可查进度
