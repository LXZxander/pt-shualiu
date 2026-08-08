#!/usr/bin/env bash
# 查看 PT刷流 当前进度
cd "$(dirname "$0")"
set -e
.venv/bin/python - <<'EOF'
import logging; logging.disable(logging.CRITICAL)
from pt_shualiu.config import load_config
from pt_shualiu.sites import Site
from pt_shualiu.qbit import QbitClient
cfg = load_config()
q = QbitClient(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
print("=== 考核进度 ===")
for s in cfg.sites:
    try:
        rep = Site(s).fetch_myhr()
        for a in rep.assessment:
            mark = "✅" if a.passed else "❌"
            print(f"  [{s.name}] {mark} {a.name}: {a.current} / {a.required}")
    except Exception as e:
        print(f"  [{s.name}] 站点读取失败: {e}")
print("=== qbit 状态 ===")
md = q.sync_maindata()
print("  磁盘剩余: %.1f GB  下载速度: %.0f KB/s  上传速度: %.0f KB/s"
      % (md.get("free_space_on_disk",0)/1024**3, md.get("dl_info_speed",0)/1024, md.get("up_info_speed",0)/1024))
EOF
