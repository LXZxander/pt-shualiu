from pathlib import Path
import pytest
from pt_shualiu.sites import parse_myhr, parse_remaining_sec, MyhrParseError

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


def test_carpt_passed_metric_detection():
    # 指标4（做种积分增量）为构造的已达标项：结果以 <span style="color: green"> 包裹"通过"，
    # passed 判定必须先去标签再匹配通过/达标/已达标。
    r = parse_myhr((FIX / "myhr_carpt.html").read_text(), site="carpt")
    assert len(r.assessment) == 4
    p4 = r.assessment[3]
    assert p4.name == "做种积分增量"
    assert p4.required == "3000 " and p4.current == "2"
    assert p4.passed is True
    assert sum(1 for a in r.assessment if a.passed) == 1  # 仅此一条通过


def test_btschool_passed_metrics_synthesized():
    # BT"还需要"行只列未达标项；缺行即已达标 → 合成 passed=True，保证恒为 3 条。
    html = ("离新人考核结束还有 <span title='2026-08-30 21:06:42'>22天23时</span><br />"
            "上传量： <span style='color: red'>还需要 37.98 GB</span><br />"
            "魔力值： <span style='color: red'>还需要 5653.1</span><br />")
    r = parse_myhr(html, site="btschool")
    assert len(r.assessment) == 3
    by_name = {a.name: a for a in r.assessment}
    assert by_name["上传量"].passed is False
    assert by_name["魔力值"].passed is False
    dl = by_name["下载量"]  # 无"还需要"行 → 合成已达标
    assert dl.required == "50 GB" and dl.current == "50 GB" and dl.passed is True
    # 全达标（一个"还需要"行都没有）→ 三项全部合成 passed=True
    r2 = parse_myhr("<h1>H&R记录</h1><html>全部通过</html>", site="btschool")
    assert len(r2.assessment) == 3
    assert all(a.passed for a in r2.assessment)


def test_parse_myhr_login_page_raises():
    # CRITICAL: Cookie 失效 / 布局变更时 myhr.php 返回 HTTP 200 登录页。页面不含任何 myhr
    # 内容标记，若解析成空 hr + 全部达标 → 有义务种被当"无义务"删（H&R 风险）。必须抛
    # MyhrParseError，让 check()/run_mail 走 per-site 容错跳过该站（fail-closed）。
    html = (FIX / "myhr_login.html").read_text()
    with pytest.raises(MyhrParseError):
        parse_myhr(html, site="carpt")


def test_parse_myhr_valid_fixtures_do_not_raise():
    # 正控：真实捕获的两站 myhr 页（含有效 H&R 表标记）不因校验误伤
    assert parse_myhr((FIX / "myhr_carpt.html").read_text(), site="carpt").hr
    assert parse_myhr((FIX / "myhr_btschool.html").read_text(), site="btschool").hr


def test_parse_btschool_myhr():
    r = parse_myhr((FIX / "myhr_btschool.html").read_text(), site="btschool")
    assert len(r.hr) == 1
    assert r.hr[0].remaining_seed_sec == 20 * 3600  # 20:00:00 → 72000s
    # BT 考核指标区是"还需要 X"格式（无 指标N 行）；required 按站点规则 50GB/50GB/6000，
    # current = required - remaining。真实指标名：上传量/下载量/魔力值（非"上传增量"）。
    assert len(r.assessment) == 3
    by_name = {a.name: a for a in r.assessment}
    up = by_name["上传量"]
    assert up.required == "50 GB" and up.current == "12.02 GB" and up.passed is False
    dl = by_name["下载量"]
    assert dl.required == "50 GB" and dl.current == "0 GB" and dl.passed is False
    bonus = by_name["魔力值"]
    assert bonus.required == "6000" and bonus.current == "346.9" and bonus.passed is False


def test_remaining_done_is_zero():
    # 还需做种时间 "00:00:00" 应解析为 0
    assert parse_remaining_sec("00:00:00") == 0
    assert parse_remaining_sec("1天00:00:00") == 86400
    assert parse_remaining_sec("20:00:00") == 72000
