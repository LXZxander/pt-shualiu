"""qbit.py 的 HTTP 层测试：用 responses 模拟 qBittorrent Web API。"""
import json
from urllib.parse import parse_qs

import responses
import pytest
from pt_shualiu.qbit import QbitClient


@pytest.fixture
def client():
    return QbitClient("http://127.0.0.1:9091", "user", "pass")


@responses.activate
def test_login(client):
    responses.post(f"{client.base}/api/v2/auth/login",
                   body="Ok.", status=200,
                   headers={"Set-Cookie": "SID=test-session"})
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
    # 按 category+add_time 取最新一条（URL 尾段与真实种名不一致，不能按名回查）
    responses.post(f"{client.base}/api/v2/auth/login", body="Ok.", status=200)
    responses.post(f"{client.base}/api/v2/torrents/add", body="Ok.", status=200)
    responses.get(f"{client.base}/api/v2/torrents/info",
                  json=[
                      {"hash": "h_old", "name": "Some Older Torrent",
                       "category": "pt_free", "add_time": 1000, "progress": 1.0},
                      {"hash": "h1", "name": "download.php?id=1",
                       "category": "pt_free", "add_time": 2000, "progress": 0},
                  ], status=200)
    h = client.add_torrent("https://carpt.net/download.php?id=1",
                           save_path="/tmp/dl", category="pt_free",
                           paused=True, priority=3, tags=["site:carpt:1"])
    assert h == "h1"


@responses.activate
def test_add_torrent_returns_new_hash_when_category_populated(client):
    # 冒烟实测修正回归：真实 qbit 4.6.x 的 torrents/info 用 added_on（无 add_time 字段）。
    # 旧实现按 category+add_time 最新一条回查，add_time 恒缺省 → 返回分类里第一条（h_old），
    # 同批第 2 个起的 hash 全记错。新实现快照比对只认"本次新增"的 h1。
    responses.post(f"{client.base}/api/v2/auth/login", body="Ok.", status=200)
    responses.post(f"{client.base}/api/v2/torrents/add", body="Ok.", status=200)
    calls = {"n": 0}

    def info_callback(request):
        calls["n"] += 1
        if calls["n"] == 1:  # pre-GET：分类里已有老种
            return (200, {}, json.dumps([
                {"hash": "h_old", "name": "Older Torrent",
                 "category": "pt_free", "added_on": 1000, "progress": 1.0},
            ]))
        # POST 后：新种 h1 出现在分类里（h_old 仍排最前）
        return (200, {}, json.dumps([
            {"hash": "h_old", "name": "Older Torrent",
             "category": "pt_free", "added_on": 1000, "progress": 1.0},
            {"hash": "h1", "name": "download.php?id=1",
             "category": "pt_free", "added_on": 2000, "progress": 0},
        ]))

    responses.add_callback(
        responses.GET, f"{client.base}/api/v2/torrents/info",
        callback=info_callback)
    client.ADD_RETRY_DELAY = 0
    h = client.add_torrent("https://carpt.net/download.php?id=1",
                           category="pt_free", paused=True, priority=3,
                           tags=["site:carpt:1"])
    assert h == "h1"  # 必须返回新种 hash，而不是分类里第一条 h_old
    assert calls["n"] == 2  # pre-GET + 1 次 POST 后轮询


@responses.activate
def test_add_torrent_retry_when_info_delayed(client):
    # qBittorrent 异步抓取 .torrent 元数据：torrents/add 返回后条目可能延迟出现。
    # 第一次 info 轮询为空，第二次才出现新种 —— 应触发重试并最终返回正确 hash。
    responses.post(f"{client.base}/api/v2/auth/login", body="Ok.", status=200)
    responses.post(f"{client.base}/api/v2/torrents/add", body="Ok.", status=200)
    info_calls = {"n": 0}

    def info_callback(request):
        info_calls["n"] += 1
        if info_calls["n"] == 1:
            return (200, {}, json.dumps([]))
        return (200, {}, json.dumps([
            {"hash": "h1", "name": "download.php?id=1",
             "category": "pt_free", "add_time": 2000, "progress": 0},
        ]))

    responses.add_callback(
        responses.GET, f"{client.base}/api/v2/torrents/info",
        callback=info_callback)
    client.ADD_RETRY_DELAY = 0  # 测试不等待
    h = client.add_torrent("https://carpt.net/download.php?id=1",
                           category="pt_free", paused=True)
    assert h == "h1"
    assert info_calls["n"] == 2  # 确实发生了两次轮询


@responses.activate
def test_add_torrent_picks_newest_added_on_matching_category(client):
    # I3：同批出现多条新增（外部并发加种混入 fresh）时，旧实现返回 fresh[0] 可能是外部种的
    # hash → state 库记错 → 达标后"无上传"基线丢失。必须取目标分类里 added_on 最新的一条；
    # 外部种（不同 category）即使排最前也不得选中。
    responses.post(f"{client.base}/api/v2/auth/login", body="Ok.", status=200)
    responses.post(f"{client.base}/api/v2/torrents/add", body="Ok.", status=200)
    calls = {"n": 0}

    def info_callback(request):
        calls["n"] += 1
        if calls["n"] == 1:  # pre-GET：空库
            return (200, {}, json.dumps([]))
        return (200, {}, json.dumps([
            {"hash": "h_ext", "name": "External Torrent", "category": "other",
             "added_on": 9999, "progress": 0},
            {"hash": "h_ours_old", "name": "download.php?id=1", "category": "pt_free",
             "added_on": 1001, "progress": 0},
            {"hash": "h_ours_new", "name": "download.php?id=1", "category": "pt_free",
             "added_on": 2002, "progress": 0},
        ]))

    responses.add_callback(
        responses.GET, f"{client.base}/api/v2/torrents/info",
        callback=info_callback)
    client.ADD_RETRY_DELAY = 0
    h = client.add_torrent("https://carpt.net/download.php?id=1",
                           category="pt_free", paused=True)
    assert h == "h_ours_new"  # 目标分类里 added_on 最新，而非 fresh[0]=h_ext


@responses.activate
def test_set_global_limits(client):
    responses.post(f"{client.base}/api/v2/auth/login", body="Ok.", status=200)
    responses.post(f"{client.base}/api/v2/app/setPreferences", status=200)
    client.set_global_limits(500 * 1024, 500 * 1024)
    req = responses.calls[-1].request
    body = req.body if isinstance(req.body, str) else req.body.decode()
    assert "json=" in body and "up_limit" in body and "dl_limit" in body
    # 必须发送合法 JSON（str(dict) 的单引号形式会被 qBittorrent 静默忽略）
    payload = json.loads(parse_qs(body)["json"][0])
    assert payload == {"up_limit": 500 * 1024, "dl_limit": 500 * 1024}


@responses.activate
def test_sync_maindata_unwraps_server_state(client):
    # ⚠️ 控制器修正：qbit /sync/maindata 的 free_space_on_disk/up_info_speed 等全局字段
    # 都在 server_state 子对象里；sync_maindata() 必须解包返回 server_state，
    # 否则 daemon 磁盘闸门读到 0、限速读到 0、保种磁盘压力分支永不触发。
    responses.post(f"{client.base}/api/v2/auth/login", body="Ok.", status=200)
    responses.get(f"{client.base}/api/v2/sync/maindata",
                  json={"rid": 1,
                        "server_state": {"free_space_on_disk": 18514370560,
                                         "up_info_speed": 0, "dl_info_speed": 0}},
                  status=200)
    md = client.sync_maindata()
    assert md["free_space_on_disk"] == 18514370560
    assert "rid" not in md  # 已解包 server_state
