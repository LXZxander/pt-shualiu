"""保种生命周期：全量纳管 PT 种子，按网站 myhr 判断达标，达标后 10 分钟无上传删除。"""
import logging
from datetime import datetime
from dataclasses import dataclass
from .sites import HrRecord

logger = logging.getLogger(__name__)

@dataclass
class DeleteDecision:
    hash: str
    reason: str
    site: str = ""   # 删除所属站点，供 daemon 记 daily_stats("deleted")


def _site_tag_id(t):
    """从 qbit torrent dict 的 tags 提取 `site:{site}:{id}` 里的 id。

    qbit torrents/info 的 tags 是逗号分隔字符串（实测如 "site:carpt:192915"），
    不是 list；防御兼容 list（测试夹具用 list）。tag 站点与 t.get('site') 不一致
    （或 torrent 无 site 字段时）按 tag 自身站点解析。非本工具 tag 返回 None。
    """
    tags = t.get("tags") or ""
    if isinstance(tags, str):
        tags = [x for x in tags.split(",") if x]
    elif not isinstance(tags, list):
        return None
    t_site = t.get("site")
    for tag in tags:
        if not isinstance(tag, str) or not tag.startswith("site:"):
            continue
        parts = tag.split(":")
        if len(parts) != 3:
            continue
        tag_site, id_str = parts[1], parts[2]
        if t_site is not None and tag_site != t_site:
            continue
        try:
            return int(id_str)
        except ValueError:
            return None
    return None

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
                # completion_on 按设计 §4.5 落库：现有行 update 补写；新行 upsert 后紧接 update
                # 写 completion_on（upsert_torrent 签名不含该字段，不改 state.py）。
                completion_on = t.get("completion_on", 0) or 0
                if self.state.get_torrent(t["hash"]):
                    self.state.update_torrent(t["hash"], site=t.get("site", ""),
                                              category=t.get("category", ""),
                                              name=t.get("name", ""),
                                              completion_on=completion_on)
                else:
                    self.state.upsert_torrent(t["hash"], t.get("site", ""),
                                              t.get("category", ""), t.get("name", ""),
                                              added_at=datetime.now().timestamp())
                    self.state.update_torrent(t["hash"], completion_on=completion_on)

    def check(self, now, dry_run=False):
        domains = {s.name: s.tracker_domain for s in self.cfg.sites}
        # 每次从 qbit 快照重建 known，避免已删种子残留导致 _disk_pressure 按残留 size 误判
        self.known = {t["hash"]: t for t in self.qbit.all_pt_torrents(domains)}
        decisions = []
        # 逐站取 myhr：一站慢/失败只跳过该站，另一站照常判定。失败站不在 myhr_by_site 里，
        # 其种子自然被下方 `site not in myhr_by_site` 分支跳过（不做删除）——避免一站超时拖垮全站。
        myhr_by_site = {}
        for name, site in self.sites.items():
            try:
                myhr_by_site[name] = site.fetch_myhr()
            except Exception:
                logger.warning("myhr 获取失败，跳过站点 %s", name)
        for hash_, t in list(self.known.items()):
            site = t.get("site")
            if not site or site not in myhr_by_site:
                continue
            if t.get("progress", 1.0) < 1.0:
                continue  # 未下载完成，不处理
            rec = self._find_hr(myhr_by_site[site].hr, t)
            if rec and rec.remaining_seed_sec > 0:
                # 站点延迟兜底：completion + window + 30min 已过。
                # completion_on 缺失时 fail-closed（保留，等网站达标后再按无上传规则删）——
                # 防止 completion_on=0 → deadline≈1970 → 恒真 → 有义务未达标种首轮被误删。
                deadline = (t.get("completion_on", 0) or 0) + self._window_sec(site) * 1.0 + self.cfg.site_lag_override_min * 60
                if t.get("completion_on") and now.timestamp() > deadline:
                    decisions.append(DeleteDecision(hash_, "站点30分钟未刷新兜底", site))
                continue
            # 达标：网站已归零 / 种不在 myhr（无义务）
            if self._no_upload_for(hash_, t, now, dry_run=dry_run):
                decisions.append(DeleteDecision(hash_, "达标且10分钟无上传", site))
        # 磁盘紧张：先删"达标且无上传"的达标种（同主循环快照判定，正在上传的达标种不删，
        # 免得白丢 50GB 考核流量）；不足时再兜底删未达标种（磁盘满 > H&R）
        self._disk_pressure(now, decisions, myhr_by_site, dry_run=dry_run)
        return decisions

    def _window_sec(self, site_name):
        for s in self.cfg.sites:
            if s.name == site_name:
                return s.seeding_hours * 3600
        return 24 * 3600

    def _find_hr(self, hr_list, t):
        # 优先按 daemon 加的 tag `site:{site}:{id}` 匹配 HrRecord.site_id：qbit 重名加 " (1)"、
        # myhr 名称截断/编码差异都会让按名匹配漏配 → 漏配被当"无义务"→ 有义务种被删（H&R 风险）。
        # 名称匹配仅作 fallback（覆盖无 tag 老种）。
        tag_id = _site_tag_id(t)
        if tag_id is not None:
            for r in hr_list:
                if r.site_id == tag_id:
                    return r
        for r in hr_list:
            if r.name and r.name == t.get("name"):
                return r
        return None

    def _is_met(self, t, myhr_by_site):
        # 达标：网站已归零 / 种不在 myhr（无义务）。与 check() 主循环判定保持一致。
        site = t.get("site")
        if not site or site not in myhr_by_site:
            # ⚠️ 控制器收紧（re-review Minor）：myhr 抓取失败站/未知站的义务未知 → 按未达标
            # 处理（磁盘压力最后才删），与 check() 主循环对这些种的 fail-closed 跳过一致，
            # 避免磁盘满时误删有义务种制造 H&R。
            return False
        rec = self._find_hr(myhr_by_site[site].hr, t)
        if rec and rec.remaining_seed_sec > 0:
            return False
        return True

    def _no_upload_for(self, hash_, t, now, dry_run=False):
        # 用上传字节快照：state 里记录 last_uploaded 与 last_upload_change
        if not self.state:
            return False
        cur = t.get("uploaded", 0)
        st = self.state.get_torrent(hash_)
        if not st or st["last_uploaded"] != cur:
            # ⚠️ Fix round 2：dry-run 只读判定不落库——预览无法建立上传快照基线，也不应
            # CREATE/UPDATE torrents 行。该分支本会返回 False（快照不一致，不能判"无上传"），
            # dry-run 同样返回 False（新库 dry-run 报零删除，与真实运行所需一致）。
            if not dry_run:
                self.state.update_torrent(hash_, last_uploaded=cur,
                                          last_upload_change=now.timestamp())
            return False
        last_change = st["last_upload_change"] or now.timestamp()
        return (now.timestamp() - last_change) >= self.cfg.no_upload_min * 60

    def _disk_pressure(self, now, decisions, myhr_by_site, dry_run=False):
        if not self.qbit:
            return
        free = self.qbit.sync_maindata().get("free_space_on_disk", 1 << 60)
        if free >= self.cfg.disk_min_free_bytes:
            return
        decided = {d.hash for d in decisions}
        # 先算上主循环本轮已判删（达标且10分钟无上传 / 站点兜底）将释放的空间，再决定还要删多少，
        # 否则磁盘压力会忽略这些待删种、把未达标大种也误删。
        effective_free = free + sum(t.get("size", 0) for h, t in self.known.items() if h in decided)
        if effective_free >= self.cfg.disk_min_free_bytes:
            return
        # Fix round 3（I6）：磁盘压力候选集收紧为"达标且无上传"（与主循环同款快照判定）——
        # 旧实现会删任意达标种，正在上传的达标种被删 = 白丢 50GB 考核流量。已判删的达标种
        # 自然不在候选里；未达标/义务未知种排最后，仅当达标种不足时才兜底删（磁盘满 > H&R）。
        tier_met_idle = [t for h, t in self.known.items()
                         if t.get("progress", 1.0) >= 1.0 and h not in decided
                         and self._is_met(t, myhr_by_site)
                         and self._no_upload_for(h, t, now, dry_run=dry_run)]
        tier_met_idle.sort(key=lambda t: -t.get("size", 0))
        tier_notmet = [t for h, t in self.known.items()
                       if t.get("progress", 1.0) >= 1.0 and h not in decided
                       and not self._is_met(t, myhr_by_site)]
        tier_notmet.sort(key=lambda t: -t.get("size", 0))
        for t in tier_met_idle + tier_notmet:
            if effective_free >= self.cfg.disk_min_free_bytes:
                break
            decisions.append(DeleteDecision(t["hash"], "磁盘不足", t.get("site", "")))
            effective_free += t.get("size", 0)
