"""主循环：选种/保种/限速/邮件 四周期调度。"""
import argparse, logging, signal, time
from datetime import datetime, timezone
from pathlib import Path
from .config import load_config
from .qbit import QbitClient
from .sites import Site
from .state import State
from .selection import select
from .seeding import SeedingManager, _site_tag_id
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
        # ⚠️ 控制器修正：self.cfg.sites 是 SiteConfig（无 fetch_rss/fetch_listing_rows），
        # 必须遍历 self.sites（{name: Site}）才有 fetch_* 方法；与 run_mail 的 sites 用法一致。
        for site in self.sites.values():
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
                # Fix round 3：qbit torrents/info 的 tags 是逗号分隔字符串（实测 "site:carpt:192915"），
                # 旧实现 `for tag in tags` 会逐字符迭代 → 永远匹配不上。用 _site_tag_id 解析。
                tag_id = _site_tag_id(t)
                if tag_id is not None:
                    known_ids.add(tag_id)
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
                    self.state.incr_stat(site.name, "added")   # Fix round 3（I5）：邮件当日新增
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
                    self.state.incr_stat(site.name, "added")   # Fix round 3（I5）：邮件当日新增
                    log.info("added download %s id=%s hash=%s", site.name, r.site_id, h[:12])
                except Exception as e:
                    log.warning("add download failed: %s", e)

    def run_seeding(self, now):
        # ⚠️ Fix round 2：dry-run 时 check 只读判定，不写状态库（不建立上传快照基线）
        decisions = self.seeding.check(now, dry_run=self.dry_run)
        for d in decisions:
            if self.dry_run:
                log.info("[dry-run] delete %s (%s)", d.hash[:12], d.reason)
                continue
            try:
                self.qbit.delete_torrents([d.hash])
                self.state.remove_torrent(d.hash)
                if d.site:
                    self.state.incr_stat(d.site, "deleted")   # Fix round 3（I5）：邮件当日删除
                log.info("deleted %s (%s)", d.hash[:12], d.reason)
            except Exception as e:
                log.warning("delete failed %s: %s", d.hash[:12], e)

    def run_mail(self, now):
        # ⚠️ 控制器修正（2026-08-07 实测）：btschool myhr 需 ~22.7s，一站慢/失败必须只跳过该站，
        # 否则整封邮件失败。与 seeding.check() 的每站容错同源。
        # Fix round 3（I5）：收集告警（站点 myhr 失败=Cookie 失效/站点异常、磁盘告急）入邮件 §告警。
        warnings = []
        reports = {}
        for name, site in self.sites.items():
            try:
                reports[name] = site.fetch_myhr()
            except Exception as e:
                log.warning("mail: site %s myhr failed: %s", name, e)
                warnings.append(f"站点 {name} myhr 获取失败（Cookie 失效或站点异常）：{e}")
        stats = {}
        for t in self.state.all_torrents():
            stats.setdefault(t["site"], {"in_seed": 0})["in_seed"] += 1
        for sc in self.cfg.sites:
            st = stats.setdefault(sc.name, {"in_seed": 0})
            st["added"] = self.state.stat(sc.name, "added")
            st["deleted"] = self.state.stat(sc.name, "deleted")
        try:
            free = self.qbit.sync_maindata().get("free_space_on_disk", 0)
            if free < self.cfg.disk_min_free_bytes:
                warnings.append(
                    f"磁盘剩余 {free / 1024**3:.2f}GB 低于阈值 {self.cfg.disk_min_free_bytes / 1024**3:.0f}GB")
        except Exception as e:
            log.warning("mail: disk check failed: %s", e)
            warnings.append(f"磁盘状态读取失败：{e}")
        body = build_summary(reports, stats, warnings=warnings)
        if self.dry_run:
            log.info("[dry-run] mail:\n%s", body)
            return
        send_mail("PT刷流日报", body, self.cfg)

    def loop_once(self, now):
        if time.time() - self.last_selection >= self.cfg.poll_selection_s:
            self.run_selection(now); self.last_selection = time.time()
        if time.time() - self.last_seeding >= self.cfg.poll_seeding_s:
            self.run_seeding(now); self.last_seeding = time.time()
        # ⚠️ Fix round 1：dry-run 不触碰限速/状态。否则 --once --dry-run 会对真实 qbit
        # 执行 set_global_limits 并写 SQLite ratelimit 行——预览不应有副作用。sync_maindata
        # 也在门控内：dry-run 连读都不读（只在真正驱动限速时才取）。
        if not self.dry_run:
            md = self.qbit.sync_maindata()
            self.rates.run_once(md.get("up_info_speed", 0), md.get("dl_info_speed", 0), now)
        # ⚠️ Fix round 2：邮件 09:00 门限由精确相等改为 >=——循环周期 60s + 可变工作量
        # （btschool myhr ~22.7s/站）会让采样网格跨过 09:00 分钟，精确相等当日漏发。
        # HH:MM 与日期按本地时区算（守本地钟；本机 UTC 但语义应一致）。astimezone 一次，
        # 门限与 run_mail 复用（run_mail 本身不用时间，传 now 保持参数贯通）。
        local = now.astimezone()
        hhmm = local.strftime("%H:%M")
        if hhmm >= self.cfg.mail_send_time and self.last_mail != local.date().isoformat():
            self.run_mail(now); self.last_mail = local.date().isoformat()

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

# 入口点：`python -m pt_shualiu.daemon` 由 systemd ExecStart / 手动 --check/--once 调起
if __name__ == "__main__":
    main()
