"""站点统一适配器：RSS / 列表页 / myhr。两站均为 NexusPHP，结构一致。"""
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from dataclasses import dataclass

import requests

from .config import SiteConfig

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


# 促销枚举：下载计费方式决定选种轨（Task 6）。用户确认：打折种归入下载轨。
# PROMO_FREE 仅限真正免费（免费 / 2x 免费）；折扣下载（50%/50%+2x/30%）与
# pro_2up、无标记（""，下载正常计入）一样按正常下载轨计费。
PROMO_FREE = frozenset({"pro_free", "pro_free2up"})
PROMO_DOWNLOAD_OK = frozenset({"pro_2up", "", "pro_50pctdown",
                               "pro_50pctdown2up", "pro_30pctdown"})

# 打折种下载按比例计入 50GB 考核指标。Task 6 选种 / 守护与邮件统计用
# effective_size = size_bytes * download_fraction(promo) 计考核流量，磁盘占用仍按满额 size_bytes。
# "" = 无促销标记，下载 100% 计入；未知促销默认全量计入（安全兜底）。
PROMO_DOWNLOAD_FRACTION = {
    "pro_free": 0.0, "pro_free2up": 0.0,
    "pro_50pctdown": 0.5, "pro_50pctdown2up": 0.5, "pro_30pctdown": 0.3,
    "pro_2up": 1.0, "": 1.0,
}


def download_fraction(promo: str) -> float:
    """该促销下，下载流量按多少比例计入考核指标。未知促销默认全量。"""
    return PROMO_DOWNLOAD_FRACTION.get(promo, 1.0)


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


NAME_RE = re.compile(r'<a title="([^"]*)"\s+href="details\.php\?id=\d+')
# 发布日取"rowfollow nowrap"格内的 <span title>（该格是发布日；剩余时间的 span 在 torrentname 格内，
# 不会被误取）。两种引号风格都兼容（真实两站均为双引号，留单引号防御）。
TIME_ATTR_RE = re.compile(r"<td class=['\"]rowfollow nowrap['\"]>\s*<span title=['\"]([\d\- :]+)['\"]>")
SIZE_TD_RE = re.compile(r"<td class='rowfollow'>\s*([\d.]+)\s*<br />\s*(TB|GB|MB|KB)\s*</td>")
SIZE_TD_RE_BT = re.compile(r'<td class="rowfollow">\s*([\d.]+)\s*<br />\s*(TB|GB|MB|KB)\s*</td>')
# 做种/下载数：数字在 #seeders/#leechers 锚点后的 <a> 文本里，允许被 <font> 或 <b> 包裹，
# 也允许无包裹（真实 BT/CarPT 的下载数格就无 <font>）。
LEECH_RE = re.compile(r"#leechers[^>]*>(?:\s*<[^>]*>)*?\s*(\d+)\s*(?:</font>)?\s*</a>", re.S)
SEED_RE = re.compile(r"#seeders[^>]*>(?:\s*<[^>]*>)*?\s*(\d+)\s*(?:</font>)?\s*</a>", re.S)
EXPIRE_RE = re.compile(r'剩余时间：<span title="(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})">')
PROMO_CLASS_RE = re.compile(r'class="(pro_[a-z0-9]+)"')
DOWNLOAD_LINK_RE = re.compile(r'href="download\.php\?id=(\d+)"')
TORRENTNAME_TABLE_RE = re.compile(r'<table class=["\']torrentname["\']')

_UNIT = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def _local_dt_to_utc(s: str) -> datetime:
    # 站点时间为 +0800；兼容 CarPT(无秒)/BT(带秒) 两种格式
    s = s.strip()
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)


def _torrent_rows(html_text: str) -> list[str]:
    """把列表页切成完整外层种子行。

    NexusPHP 的种子行是嵌套结构：外层 <tr> 内含有 torrentname 内层 <tr>...</tr>，
    非贪婪的 <tr>(.*?)</tr> 会在内层 </tr> 处截断。这里用栈一次性配对每个 <tr> 与
    其对应的 </tr>；块内含且仅含一个 torrentname 表的即一个完整种子行
    （整页 wrapper <tr> 含多个 torrentname 表，会被排除）。容错：多余的 </tr>
    （页面侧栏等区域可能存在）在栈空时忽略。
    """
    tags = [(m.start(), m.group()) for m in re.finditer(r"<tr\b[^>]*>|</tr>", html_text)]
    stack: list[int] = []
    match: dict[int, int] = {}
    for idx, (_pos, tag) in enumerate(tags):
        if tag.startswith("</"):
            if stack:
                match[stack.pop()] = idx
        else:
            stack.append(idx)
    rows = []
    for oi, ci in match.items():
        block = html_text[tags[oi][0]:tags[ci][0] + 5]
        if len(TORRENTNAME_TABLE_RE.findall(block)) == 1:
            rows.append(block)
    return rows


def parse_listing_rows(html_text: str, site: str = "") -> dict[int, ListingRow]:
    rows = {}
    for blk in _torrent_rows(html_text):
        m_id = ID_RE.search(blk)
        if not m_id:
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


# ---- myhr（考核进度）解析，Task 5 ----

class MyhrParseError(Exception):
    """myhr.php 响应无法识别为有效 myhr 页（Cookie 失效返回 200 登录页 / 布局变更 / 网关错误页）。

    抛给上层（seeding.check / daemon.run_mail / status.sh）走 per-site 容错跳过该站，
    保证 Cookie 失效时该站种子 fail-closed（不做删除），而不是被当成"全部达标"批量删（H&R 风险）。
    """


# 有效 myhr 页的内容标记。实测（2026-08-07）：
#   - CarPT 真页：`<table width='100%' id='hr-table'>`（H&R 表）+ 列头 "H&R ID" + 考核块 "指标N："
#   - BTSchool 真页（fixture 取自真实捕获）：`<h1>H&R记录</h1>` + 列头 "HR编号" + 考核块 "还需要 X"
#   - 两站均含 "新人考核"。
# 登录页/错误页（HTTP 200）实测均不含以上任一标记（CarPT login.php 与 Cookie 失效的 myhr.php
# 都只含 login form + 用户名/密码，title 为"登录 - Powered by NexusPHP"）。用任一标记命中即视为有效。
_MYHR_MARKER_RE = re.compile(r"hr-table|H&R记录|H&R ID|HR编号|新人考核|指标\d|还需要")
ASSESS_RE = re.compile(r"指标\d+：([^,]+),\s*要求：([^,]+),\s*当前：([^,]+),\s*结果：\s*([^！]+)！")
# BT 站 myhr 页无"指标N"行：考核指标在页顶"新人考核"块里，按
# "上传量：还需要 37.98 GB / 下载量：还需要 50.00 GB / 魔力值：还需要 5653.1" 展示，
# 只列出尚未达标项（remaining>0）。required 不在页面上，按 BT 新人考核站点规则
# 硬编码（上传 50GB / 下载 50GB / 魔力 6000）；current = required - remaining。
BT_ASSESS_RE = re.compile(
    r"(上传量|下载量|魔力值)：\s*<span[^>]*>还需要\s*([\d.]+)\s*(GB|MB|KB)?</span>")
# BT 固定 3 项指标，顺序即输出顺序。页面不打印 required，按站点规则硬编码。
_BT_ASSESS_METRICS = (
    ("上传量", "50 GB"),
    ("下载量", "50 GB"),
    ("魔力值", "6000"),
)
_BT_ASSESS_REQUIRED = dict(_BT_ASSESS_METRICS)
HR_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
# 真实页面单/双引号混用（CarPT 末列"操作"为双引号），两种都要匹配。
CELL_RE = re.compile(r"<td class=['\"]rowfollow[^'\"]*['\"](?:[^>]*)>(.*?)</td>", re.S)
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


# brief 原正则 `^\d+天?\d{2}:\d{2}:\d{2}$` 有缺陷：裸 `HH:MM:SS`（无"天"）时贪心 `\d+`
# 吞掉两位小时后 `\d{2}` 会撞上冒号导致失配，_find_remaining 漏掉"还需做种时间"列。
# 改写成精确两分支：`N天HH:MM:SS` 或 `HH:MM:SS`（小时 1~2 位，真实两站小时恒为 2 位）。
_REMAIN_RE = re.compile(r"^\d+天\d{2}:\d{2}:\d{2}$|^\d{1,2}:\d{2}:\d{2}$")
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


_PASS_WORDINGS = ("通过", "达标", "已达标", "已通过")


def _is_passed(raw: str) -> bool:
    """结果判定：去 HTML 标签 + strip 后，命中通过/达标/已达标 才算通过（未通过/未达标 不算）。"""
    txt = re.sub(r"<[^>]+>", "", raw).strip()
    return txt in _PASS_WORDINGS


def _parse_btschool_assessment(html_text: str) -> list[AssessmentMetric]:
    """解析 BT 页顶"新人考核"块（"还需要 X"格式）。

    "还需要" 行只列尚未达标项；已达标指标在页面上没有对应行。为保证 BT 恒定输出
    3 条（设计约定），缺项的指标按 required 值合成 passed=True 记录
    （current=required，语义"已达标"）。
    """
    parsed = {}
    for m in BT_ASSESS_RE.finditer(html_text):
        name = m.group(1)
        remain = float(m.group(2))
        unit = m.group(3) or ""
        req = _BT_ASSESS_REQUIRED[name]
        cur = float(req.split()[0]) - remain
        parsed[name] = AssessmentMetric(name=name, required=req,
                                        current=f"{cur:g} {unit}".strip(),
                                        passed=(remain <= 0))
    out = []
    for name, req in _BT_ASSESS_METRICS:
        if name in parsed:
            out.append(parsed[name])
        else:
            out.append(AssessmentMetric(name=name, required=req,
                                        current=req, passed=True))
    return out


def _parse_assessment(html_text: str) -> list[AssessmentMetric]:
    """CarPT 走 指标N 行；BT 页无该行（ASSESS_RE 无匹配）时回落到"还需要"块。"""
    found = [AssessmentMetric(name=m.group(1), required=m.group(2),
                              current=m.group(3), passed=_is_passed(m.group(4)))
             for m in ASSESS_RE.finditer(html_text)]
    if found:
        return found
    return _parse_btschool_assessment(html_text)


def parse_myhr(html_text: str, site: str) -> MyhrReport:
    # CRITICAL fail-closed：Cookie 失效时 myhr.php 返回 HTTP 200 的登录页（实测两站皆如此），
    # 若无校验会解析成空 hr + 全部达标 → 该站所有有义务种被当"无义务"删。必须先验证页面
    # 含有效 myhr 内容标记，否则抛 MyhrParseError 让调用方跳过该站。
    if not _MYHR_MARKER_RE.search(html_text):
        raise MyhrParseError(
            f"myhr 页面缺少有效标记（疑似 Cookie 失效/布局变更/错误页），site={site}")
    assessment = _parse_assessment(html_text)
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


class Site:
    """站点适配器：RSS / 列表页 / myhr 统一 HTTP 入口。"""
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
        # ⚠️ 控制器修正（2026-08-07 实测）：btschool myhr 需 ~22.7s，20s 硬超时会让
        # daemon 每轮 seeding.check() 整体失败、连带禁用另一站的保种管理。放宽到 60s。
        r = self.session.get(url, timeout=60)
        r.raise_for_status()
        return r.text

    def fetch_rss(self) -> list[Candidate]:
        return parse_rss(self._get(self.rss_url), site=self.name)

    def fetch_myhr(self) -> MyhrReport:
        return parse_myhr(self._get(f"{self.base_url}/myhr.php"), site=self.name)

    def fetch_listing_rows(self, pages: int = 2) -> dict[int, ListingRow]:
        rows = {}
        for p in range(1, pages + 1):
            rows.update(parse_listing_rows(
                self._get(f"{self.base_url}/torrents.php?page={p}"), site=self.name))
        for sid, r in rows.items():
            if r.download_url and r.download_url.startswith("/"):
                r.download_url = self.base_url + r.download_url
        return rows
