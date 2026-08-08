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
