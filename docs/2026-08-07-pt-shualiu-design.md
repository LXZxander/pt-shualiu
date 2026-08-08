# PT刷流工具 设计文档

日期：2026-08-07
状态：已与用户逐条确认

## 1. 背景与目标

用户是 CarPT（https://carpt.net）和 BT school（https://pt.btschool.club）两个 PT 站的新用户，正处于**新手考核期**，需要在 `~/pt_shualiu/` 搭建一个自动化刷流+保种工具。

### 考核要求（从两站 myhr.php 实测）

| 指标 | CarPT | BT school |
|---|---|---|
| 上传增量 | 50 GB（当前 76 MB） | 50 GB（当前 12 GB） |
| 下载增量 | 50 GB（当前 0 KB） | 50 GB（当前 0 KB） |
| 魔力值 | 5000（当前 ~14） | 6000（当前 ~346） |
| 做种积分 | 3000（当前 ~2，CarPT 独有） | — |
| 考核截止 | 2026-09-06 | ~22 天后 |
| 保种时长 | 24 小时（1 天） | 20 小时 |

> 注（2026-08-07 实测）：BT school 的 myhr 页面无 `指标N` 行，只以"还需要 X"列出未达标指标；实际名称是 **上传量 / 下载量 / 魔力值**（非上表的"上传增量/下载增量"）。myhr 解析器以实际页名为准；mailer 按解析出的 name 渲染，不得硬编码表名。

- 两站考核期**分享率均为"无限"**，下载非免费种不影响分享率，安全。
- 两站均已有 H&R 记录（此前已下载的种子在保种中），H&R 上限 CarPT 20 条。

### 关键事实（实测验证）

1. **RSS 不含免费状态**；`free=1` 参数在两站均被忽略（free=0/free=1 返回完全相同内容）。
2. **torrents.php 列表页每行完整**：`pro_free`/`pro_free2up` 等促销标记（含免费剩余时间 tooltip）、大小、精确发布时间（title 属性）、种子数/下载数、`download.php?id=` 链接。两站结构一致（NexusPHP）。
3. **免费种下载不计入考核"下载增量"**：用户今天在 CarPT 已下载约 1.4GB（免费），指标2 仍为 0.00 KB。→ 50GB 下载量必须靠**非免费种**。
4. CarPT 促销规则：90% 新种免费、3% 为 50%下载、2% 为 2x上传、其余组合；免费限时 7 天、2x 永久；>20GB 种子自动免费；发布满 1 个月自动永久 2x。
5. qBittorrent v4.6.3，WebUI 端口 9091，API 需先 `auth/login` 拿 cookie；磁盘剩余 19.2GB。
6. 现有 18 个种子**全部是 PT 种**，可按 tracker 域名区分站点：BT school = `pt.btschool.club`（11 个）、CarPT = `tracker.carpt.net`（7 个）；其中 2 个仍在下（Mermaid），16 个已完成。用户要求这些**已有任务也纳入管理**，完成的（保种期满 + 无上传）同样自动删除。
7. **站点区分**：qBit 的 `torrents/trackers` 返回 announce URL，按域名即可确定站点，新老种子通用。

## 2. 策略：双轨制

### 免费轨（主力）
抢"新 + 小 + 免费 + 下载人数多"的种子 → 快速下载完成 → 做种积累**上传 + 魔力值 + 做种积分 + 保种时间**。同时挂满二三十个（磁盘自然限制），最大化同时保种数（做种积分/魔力按"种子数 × 时间"累积）。

### 下载轨（机会型）
**时不时**抓"非免费 + 容易刷流 + 不算特别大"的种子：
- 下载 100% 计入 50GB 下载指标；
- 同时有上传价值（leechers>0），不专门下大种（大种无收益，且占磁盘）；
- 软上限 ≤2GB（用户确认）；
- 同时最多 1~2 个在下载，只在免费轨有余量、磁盘≥3GB 时抓。

## 3. 架构

Python 3 常驻服务，systemd 托管（开机自启、崩溃重启）。借鉴 ptool：**用 qBittorrent 分类隔离**（`pt_free` / `pt_download`）管理新增种子；**管理范围 = tracker 属于两站的全部种子**（含已有 18 个），站点由 tracker 域名判定。非 PT 种子一律不碰。

```
~/pt_shualiu/
├── pt_shualiu/
│   ├── __init__.py
│   ├── config.py        # 配置加载（.env 存凭证，gitignore）
│   ├── qbit.py          # qBittorrent API 封装
│   ├── sites.py         # 两站统一适配器（RSS/列表页/myrh/促销解析）
│   ├── selection.py     # 双轨选种决策
│   ├── seeding.py       # 保种生命周期 + 删除规则
│   ├── ratelimit.py     # 限速状态机
│   ├── mailer.py        # 163 邮件摘要
│   ├── state.py         # SQLite 状态存储
│   └── daemon.py        # 主循环（调度）
├── status.sh            # 手动查看进度
├── pt_shualiu.service   # systemd 单元
├── .env                 # 凭证（gitignore）
└── downloads/           # 种子保存路径（qbit 分类 pt_free/pt_download 指向此处）
```

### 依赖
- Python 3.10+，venv
- `requests`（站点抓取）
- `python-dotenv`（.env 加载）
- 无第三方 qbit 库，直接用 HTTP API（`requests` 即可）

## 4. 组件设计

### 4.1 config.py
- 从 `.env` 读凭证：两站 cookie、qBit 账号密码、163 授权码/账号/收件人。
- 从 `pt_shualiu.toml`（或 config 常量）读策略参数：阈值、限速、调度周期。
- 凭证**不落 git**（.env gitignore）；设计文档只引用环境变量名。

### 4.2 qbit.py
封装 Web API（login cookie 复用）：
- `login()`：POST `/api/v2/auth/login` → 保存 cookie
- `add_torrent(url_or_path, save_path, category, paused, priority)`：POST `/api/v2/torrents/add`
- `torrents_info(category=...)` / `torrents_info()`：全部或按分类
- `trackers(hash)`：取种子 announce URL；`site_of_torrent(hash)` 按域名判定站点（carpt.net → carpt；pt.btschool.club → btschool；其余 → None 非 PT）
- `all_pt_torrents()`：遍历全部种子，按 tracker 域名筛出两站种子（含已有 18 个），供 seeding.py 全量纳管
- `delete_torrents(hashes, delete_files=True)`
- `set_global_limits(up_bytes, down_bytes)`：`torrents/setPreferences`（body 表单字段 `json={...}`，字节/秒）—— 复用已知坑：直接发 application/json 会被拒
- `set_per_torrent_priority(hashes, priority)`：免费轨=高优先，下载轨=普通
- `pause/resume` 按 hash
- `sync_maindata()`：读实时速度、free_space_on_disk、种子进度
- `reannounce()` 备用

### 4.3 sites.py
统一站点接口（CarPT / BT school 同为 NexusPHP，结构已验证一致）：

```python
class Site:
    name          # "carpt" | "btschool"
    base_url      # https://carpt.net | https://pt.btschool.club
    cookie
    rss_url       # 含 passkey
    seeding_hours # 24 | 20
```

- `fetch_rss()` → 候选列表：`id, title, size_bytes, pub_dt, download_url`
  - size 取 `enclosure length`（字节），pubDate 解析（+0800 → UTC）
- `fetch_listing_pages(n=2)` → 每行解析：
  - `id`、大小、发布时间、seeder/leecher、`download.php?id=`
  - **促销类型**：解析 `<img class="pro_xxx">` → 枚举 {free, free2up, 2up, 50pct, 50pct2up, none}
    - `pro_free`=免费、`pro_free2up`=免费+2x、其余待实现时核实 CarPT/BT 具体 class（实现阶段抓真实页面确认全部枚举）
  - 免费剩余时间（tooltip 里 `剩余时间：<span title="YYYY-MM-DD HH:MM:SS">`）
- `fetch_myhr()` → 
  - 考核进度：各指标 name/required/current/result
  - H&R 明细：`种子名称, 上传量, 下载量, 分享率, 还需做种时间, 完成时间, 剩余考察时间, 状态(考察中/已达标)`
  - 供 seeding.py 判断达标 & daemon 邮件摘要取数
- 网络：UA 模拟浏览器 + 适度限速（不触发站点封禁）；失败指数退避

### 4.4 selection.py — 双轨选种

输入：RSS 候选 + 列表页促销集 + qbit 现有种子 + SQLite 历史 + 磁盘状态。
过滤（任一不满足即排除）：
- 不在 qbit 现有种子（按 info_hash）
- 不在 SQLite 已处理历史（避免重复抓同一 id）
- 磁盘剩余 ≥ 3GB
- 免费轨候选：非免费种一律排除

**免费轨**（每轮最多加 3 个，最新优先）：
- 促销 ∈ {free, free2up}
- size ≤ 500MB（偏好 <400MB）
- 发布时间 < 12 小时
- leecher > 0 或（seeder>0）；跳过死种（seeder==0 且 leecher==0）
- 免费剩余时间 > 预计下载时间 + 1h 缓冲（预计 = size / 期望下载速度）
- 排序：pubDate 最新优先

**下载轨**（每轮最多加 1 个，且同时下载中的下载轨种子 ≤2）：
- 促销 ∈ {2up, none, 50pctdown, 50pctdown2up, 30pctdown}（下载 100%/50%/30% 计入 50GB 指标；**避开** free/free2up。2026-08-07 用户确认：打折种归入下载轨）
- **下载按比例计入指标**（2026-08-07 用户要求"按比例统计大小"）：有效指标大小 = size × download_fraction，其中 50pctdown/50pctdown2up = 0.5、30pctdown = 0.3、2up/无标记 = 1.0、free/free2up = 0.0。选种排序与统计口径均用有效大小；磁盘占用仍按全量 size 计。
- size ≤ 2GB
- leecher > 0
- 优先级低于免费轨：仅当免费轨本轮无更优候选且磁盘充足时添加

### 4.5 seeding.py — 保种生命周期（每 10 分钟）

每种子在 SQLite 记录：hash、site、category、added_at、completion_on（qbit 提供）、上传字节快照历史。

**纳管范围 = 全部 PT 种子**（`all_pt_torrents()` 按 tracker 域名判定）。启动时全量摄入：qbit 中已有的 16 个已完成 + 2 个下载中的种子一并登记（记录各自的 completion_on），此后与新增种子走同一套保种/删除生命周期——用户要求的"已有任务完成的也删掉"由此实现。已有种子的 site 由 tracker 判定；新加种子用其站点 RSS 判定。

删除规则（按顺序判断）：
1. **达标判断以网站为准**：myhr 中该种"还需做种时间"归零 或 状态"已达标"；若种不在 myhr（无 H&R 义务），视为达标。
2. 达标后：最近 **10 分钟上传增量 == 0** → 删除（含文件）；仍在传 → 保留（继续赚上传）。
3. **站点延迟兜底**：qbit `completion_on + 保种时长 + 30 分钟` 已过而网站仍未显示达标 → 直接删除（不等网站）。
4. **磁盘紧张**（<3GB）：优先删除"达标且无上传"的种子（先删大的）；仍不足则暂停下载轨；不再新增。
5. 免费种在**下载完成前免费到期** → 暂停并删除（借鉴 ptool，避免白下）。

上传增量判定：每次检查记录 `uploaded` 字节快照 + 上次变化时间；`now - last_change ≥ 10min` 且未达标后 → 无上传。

### 4.6 ratelimit.py — 限速状态机（每 1 分钟）

上传/下载**独立**状态机，通过 qbit 全局限速设置（字节/秒）：

| 方向 | 上限(HIGH) | 触发条件 | 降速(LOW) | 恢复 |
|---|---|---|---|---|
| 上传 | 500 KB/s | 实际速度 ≥95% 上限 连续 **60** 分钟 | 200 KB/s | 保持 200 满 **60** 分钟 → 回 500 |
| 下载 | 500 KB/s | 实际速度 ≥95% 上限 连续 **120** 分钟 | 200 KB/s | 保持 200 满 **60** 分钟 → 回 500 |

- 状态存 SQLite（重启不丢）。
- HIGH 期连续计数；LOW 期只走 60 分钟计时器，不重复触发。

### 4.7 mailer.py
- 每日 09:00 发送 163 邮件（复用 seedhub 的 SMTP 模式）：
  - 两站考核 4 指标 当前值/要求/剩余
  - 在保种子数（免费轨/下载轨）
  - 当日新增、删除、H&R 状态、告警（cookie 失效/站点异常/磁盘告急）
- 授权码从 `.env` 读，不落代码。

### 4.8 daemon.py — 调度
| 周期 | 动作 |
|---|---|
| 每 60s | 限速状态机 + 应用全局限速 |
| 每 5min | 选种循环（RSS+列表页 → 双轨决策 → qbit 添加） |
| 每 10min | 保种生命周期（快照、myhr、删除） |
| 每日 09:00 | 邮件摘要 |
| 启动时 | 全量自检：qbit 可达、站点 cookie 有效、磁盘、限速状态续跑 |

### 4.9 state.py
SQLite `pt_shualiu.db`（gitignore）：
- `torrents`：hash、site、category、added_at、completion_on、上传快照
- `history`：已删除/已处理的种子 id（去重）
- `ratelimit`：up/down 状态机当前状态
- `daily_stats`：邮件摘要取数

## 5. 数据流

```
每5min: RSS(200) + 列表页(前2页) → 促销集/免费集 → 双轨过滤 → qbit add(分类+优先级)
每10min: qbit 查完成/completion_on → myhr 达标 → 10min 无上传/30min 兜底 → delete
每1min: qbit sync 实时速度 → 状态机 → setPreferences 全局限速
每天09:00: 邮件摘要
```

## 6. 配置项汇总

- **每站**：name / base_url / cookie / rss_url / seeding_hours(20,24) / 启用免费轨、下载轨开关
- **qbit**：url / 用户名 / 密码 / 保存路径 `~/pt_shualiu/downloads` / 分类名
- **限速**：500,200 KB/s；上传触发 60min、下载触发 120min、恢复 60min；95% 阈值
- **阈值**：磁盘余量 3GB、免费轨 size≤500MB(偏好400)、下载轨≤2GB、发布时间<12h、无上传 10min、站点未刷新 30min、免费到期缓冲 1h
- **调度**：限速 60s、选种 5min、保种 10min、邮件 09:00
- **邮件**：163 账号、授权码（.env）、收件人

## 7. 边界处理

- 站点限流/封 IP → 指数退避（30s→60s→120s→...上限 10min），持续失败邮件告警
- Cookie 失效（myhr 返回登录页/403）→ 邮件告警 + 该站暂停
- qbit 不可用 → 重试，状态存盘不丢
- 磁盘满 → 先删达标无上传，再停下载轨，不新增
- 免费种未下完就到期 → 暂停删除
- 同一 id 去重（SQLite history + qbit 现有 hash）
- **管理范围 = tracker 属于两站的种子**（新增种走 `pt_free`/`pt_download` 分类，已有 18 个种按 tracker 判定站点）；**非 PT 种子不碰**

## 8. 测试

- **单元**：RSS/列表页/myrh 解析器（真实抓取的 fixture 存 `tests/fixtures/`）、促销类型枚举、选种过滤、限速状态机（模拟速度序列触发 60/120 分钟）
- **集成**：`--dry-run` 连真实站点，只报告"本轮会选哪些种/会删哪些种"不实际执行
- **冒烟**：真实跑一小轮（选 1 个免费种 + 1 个下载轨种，下载→保种→删除全链路）
- 手动：`status.sh` 显示当前进度

## 9. 实施里程碑

1. 脚手架 + config/qbit/state（含 .env 模板、gitignore、systemd 单元）
2. sites.py 解析器（RSS/列表页/myrh）+ 促销枚举核实 + 单元测试
3. selection.py 双轨 + 单元测试
4. seeding.py 保种/删除 + ratelimit.py 状态机 + 单元测试
5. daemon.py 调度 + dry-run 集成验证
6. 冒烟实跑 → 邮件摘要 → 部署 systemd 上线
