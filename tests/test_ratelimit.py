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
