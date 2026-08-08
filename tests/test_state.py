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
