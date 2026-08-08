"""qBittorrent Web API v4.6.x 封装。登录拿 cookie 后所有请求复用 session。"""
import json
import time

import requests


class QbitClient:
    # add_torrent 回查 hash 时的重试：qBittorrent 异步获取 .torrent 元数据，
    # torrents/add 成功返回后条目未必立即出现在 torrents/info 里。
    # Fix round 3（I3）：5×1s=5s 窗口在 qbit 负载高时不足（实拍 "added torrent not found"）。
    # 改为指数退避：ADD_RETRY_DELAY * 2**attempt = 1,2,4,8,16 ≈ 31s 总窗口 + 最后一次轮询。
    ADD_RETRIES = 6
    ADD_RETRY_DELAY = 1.0

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
        # ⚠️ 冒烟实测修正（2026-08-07）：旧实现按 category 里 add_time 最新一条回查 hash，
        # 但 qBittorrent 4.6.x 的 torrents/info 字段是 added_on（add_time 恒缺省 → max 恒取
        # 分类里第一条），且同秒内连加多批时按时间戳无法区分新条目 → 同批第 2 个起返回
        # 错误 hash，state 库记错 → 达标后"无上传"基线丢失、种子永不自动删。
        # 改为：POST 前先快照现有 hash 集合，POST 后只认"新出现"的条目，不依赖时间戳字段。
        before = {t["hash"] for t in self.session.get(
            f"{self.base}/api/v2/torrents/info", timeout=15).json()}
        data = {"urls": url, "paused": "true" if paused else "false", "priority": priority}
        if save_path:
            data["savepath"] = save_path
        if category:
            data["category"] = category
        if tags:
            data["tags"] = ",".join(tags)
        r = self.session.post(f"{self.base}/api/v2/torrents/add", data=data, timeout=30)
        r.raise_for_status()
        # 返回 hash：优先取"本次新增"且落在目标分类的条目；无分类才退回按 URL 尾段名回查。
        # qBittorrent 异步抓取 .torrent 元数据，条目可能延迟出现，故轮询重试。
        name = self._name_from_url(url)
        for attempt in range(self.ADD_RETRIES):
            info = self.session.get(f"{self.base}/api/v2/torrents/info", timeout=15).json()
            fresh = [t for t in info if t["hash"] not in before]
            if fresh:
                # Fix round 3（I3）：同批新增多条（并发外部加种混入 fresh）时取目标分类里
                # added_on 最新的一条——旧实现返回 fresh[0] 可能是外部种的 hash，state 库记错。
                # 无目标分类时退回取新增里最新总体。
                pool = fresh
                if category:
                    nc = [t for t in fresh if t.get("category") == category]
                    if nc:
                        pool = nc
                return max(pool, key=lambda t: t.get("added_on", 0))["hash"]
            for t in reversed(info):
                if t.get("name") == name:
                    return t["hash"]
            if attempt < self.ADD_RETRIES - 1:
                time.sleep(self.ADD_RETRY_DELAY * (2 ** attempt))
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
        # v4.6.3 实测：键为 up_limit/dl_limit，单位 bytes/s；json 必须是合法 JSON
        # （str(dict) 的单引号形式会被 qBittorrent 静默忽略）
        self._login()
        body = {"up_limit": up_bytes, "dl_limit": down_bytes}
        self.session.post(f"{self.base}/api/v2/app/setPreferences",
                          data={"json": json.dumps(body)}, timeout=15)

    def set_priority(self, hashes: list[str], priority: int):
        self._login()
        self.session.post(f"{self.base}/api/v2/torrents/setPriority",
                          data={"hashes": "|".join(hashes), "priority": priority}, timeout=15)

    def sync_maindata(self) -> dict:
        self._login()
        # ⚠️ 控制器修正（2026-08-07 实测）：qbit /sync/maindata 的 free_space_on_disk /
        # up_info_speed 等全局字段都在 server_state 子对象里。必须解包返回 server_state，
        # 否则 daemon 磁盘闸门读到 0、限速读到 0、保种磁盘压力分支永不触发。
        return self.session.get(f"{self.base}/api/v2/sync/maindata",
                                timeout=20).json().get("server_state", {})

    def pause(self, hashes: list[str]):
        self._login()
        self.session.post(f"{self.base}/api/v2/torrents/pause",
                          data={"hashes": "|".join(hashes)}, timeout=15)

    def resume(self, hashes: list[str]):
        self._login()
        self.session.post(f"{self.base}/api/v2/torrents/resume",
                          data={"hashes": "|".join(hashes)}, timeout=15)
