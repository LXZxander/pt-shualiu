"""双轨选种：免费轨（刷上传/魔力/做种）+ 下载轨（机会型凑下载量）。

去重说明（设计文档"按 info_hash 比对"的等价实现）：RSS 不含 info_hash，
用两层替代——(1) SQLite history 的 (site, site_id)（本工具加过的种，含 qbit
新加种 tag `site:{site}:{id}` 解析出的 id，由 daemon 并入）；(2) qbit 里同名
种子 known_names（覆盖已有 18 个无 tag 老种）。两站各走各的 select 调用，
history_ids/known_names 均为单站点集合，无跨站 id 冲突。
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from .sites import Candidate, ListingRow, PROMO_FREE, PROMO_DOWNLOAD_OK, download_fraction

# ⚠️ 冒烟实测修正（2026-08-07）：列表页合成的 download_url 是裸
# `/download.php?id=X`（无鉴权），qbit 抓回 HTML 登录/错误页 → bencoded 解析失败、
# "added torrent not found"（下载轨全部加种失败）。下载轨必须用 RSS enclosure 的
# 带鉴权链接（passkey/downhash）；RSS 里没有时，对任一 rss_url 含 hex passkey 的站点
# 用该全局 passkey 现拼一个（btschool 是这种；CarPT 用 per-torrent 的 downhash JWT，
# 仅 RSS 有 → 下载轨若不在 RSS 里无法构造，fallback 返回原样交给调用方按加种失败处理）。
_PASSKEY_RE = re.compile(r"passkey=([0-9a-fA-F]+)")


def _download_url_for(sid: int, row: ListingRow, candidates: list, cfg) -> str:
    """给下载轨种子配一个带站点鉴权的下载链接。"""
    for c in candidates:
        if c.site_id == sid and c.download_url:
            return c.download_url
    for sc in cfg.sites:
        if sc.name == row.site:
            m = _PASSKEY_RE.search(sc.rss_url)
            if m:
                return f"{sc.base_url}/download.php?id={sid}&passkey={m.group(1)}"
    return row.download_url

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
            # 冒烟实测修正：列表页裸 URL 无鉴权 → 换成 RSS/全局 passkey 的带鉴权链接
            r.download_url = _download_url_for(sid, r, candidates, cfg)
            download.append(r)
    # 下载轨排序：download_fraction 高的优先（全量计入指标的 2up/无标记=1.0 优先于
    # 打折种 0.5/0.3——同样磁盘占用下给 50GB 下载指标贡献更大，即"选种排序用有效大小"），
    # 同分数再按 leecher 降序（上传价值）。
    download.sort(key=lambda r: (download_fraction(r.promo), r.leecher), reverse=True)
    return Selection(free[:3], download[:1])
