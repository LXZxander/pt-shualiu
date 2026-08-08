# PT刷流（pt-shualiu）

自动化 PT 站刷流工具：RSS 自动加种、促销识别、新人考核追踪、H&R 义务守护、智能限速、每日邮件日报。专为通过 PT 站新人考核设计，也适用于日常保种/冲上传量。

> ⚠️ 本项目仅用于**个人**合法使用——在自己账号、在自己可控的 qBittorrent 上自动化养号。请遵守各 PT 站点的规则与考核要求，风险自负。

---

## 功能特性

- **RSS 自动加种**：订阅站点的 `torrentrss.php`，轮询最新发布，自动抓取选中的种子交给 qBittorrent 下载
- **促销识别**：解析列表页/RSS，识别免费 / 2x / 50% / 30% 等促销状态，优先选择上传量多的种子
- **新人考核双轨追踪**：解析站点"考核"页（我的HR/指标区），记录上传增量、下载增量、做种积分等指标是否达标
- **H&R 义务守护**：解析 HR 明细页，记录每个种子要求的保种时长，防止被删种导致违规
- **智能限速（状态机）**：上传/下载独立限速。高优先级高速冲量，达到触发条件后自动降速限流，防超量
- **磁盘压力管理**：根据磁盘剩余空间与种子活动情况，自动决定保种或删除（已达标且长时间无上传的种子）
- **每日邮件日报**：每天定时（默认北京时间 09:00）发送站点考核进度 + 在保种子数 + 今日新增/删除 + 告警的摘要邮件
- **断线/重启恢复**：state 持久化到 SQLite，重启后自动恢复限速状态与统计

## 架构

```
pt_shualiu/
├── pt_shualiu/
│   ├── config.py      # 配置加载（.env → Config）
│   ├── state.py       # SQLite 状态持久化（种子记录、统计、限速状态）
│   ├── qbit.py        # qBittorrent WebUI API 客户端
│   ├── sites.py       # 站点 RSS/列表页/考核页解析
│   ├── selection.py   # 种子选择策略（双轨 + 下载量控制）
│   ├── seeding.py     # 保种管理 + 删除决策（H&R/磁盘压力）
│   ├── ratelimit.py   # 上传/下载限速状态机
│   ├── mailer.py      # 163 邮件日报
│   └── daemon.py      # 主守护循环（定时任务编排）
├── tests/             # pytest 测试 + 站点页面 fixtures
├── docs/              # 设计文档 & 实现计划
├── status.sh          # 一键查看运行状态
├── pt_shualiu.service # systemd 单元文件示例
└── .env.example       # 环境变量模板
```

数据流：`站点 RSS/列表 → selection 选种 → qbit 加种 → seeding 保种/删种 → ratelimit 限速 → mailer 日报`，全程状态落 SQLite。

---

## 部署教程

### 1. 环境要求

- Linux 服务器（或任何可常驻运行的机器）
- Python 3.10+
- [qBittorrent](https://www.qbittorrent.org/)（`qbittorrent-nox`），需开启 WebUI
- 至少一个 PT 站账号

### 2. 安装 qBittorrent

```bash
# Debian/Ubuntu 示例
sudo apt install qbittorrent-nox
# 设置 WebUI 账号密码（重要：默认 localhost 只允许本机）
```

### 3. 克隆项目并安装依赖

```bash
git clone <本仓库地址> pt_shualiu
cd pt_shualiu

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # 运行依赖
.venv/bin/pip install -r requirements-dev.txt      # （可选）测试依赖
```

### 4. 配置 `.env`

```bash
cp .env.example .env
vim .env
```

#### 4.1 站点 Cookie —— 怎么获取？

登录你的 PT 站点（浏览器），打开开发者工具（F12）：

1. 切到 **Network（网络）** 标签，刷新页面
2. 找到任意一个请求（比如首页），在 **Request Headers** 里找到 `Cookie:`
3. 把 `Cookie:` 冒号后的**整串值**（不含 `Cookie:` 本身）复制到 `.env` 的 `CARPT_COOKIE` / `BTSCHOOL_COOKIE`

```
CARPT_COOKIE=c_secure_uid=xxx; c_secure_pass=xxx; ...
BTSCHOOL_COOKIE=sl-session=xxx; c_secure_uid=xxx; ...
```

Cookie 有有效期，失效后需要重新登录获取并更新 `.env`。

#### 4.2 站点 RSS（含 passkey）—— 怎么获取？

PT 站一般都有"RSS 订阅"功能：

1. 站点主页找 **RSS 订阅 / 我的订阅** 入口
2. 选择想要的分类，生成 RSS 地址——地址里通常带 `passkey=一串字符`
3. 把**完整 RSS 地址**复制到 `.env`：

```
CARPT_RSS_URL=https://carpt.net/torrentrss.php?passkey=你的passkey
BTSCHOOL_RSS_URL=https://pt.btschool.club/torrentrss.php?passkey=你的passkey
```

> **passkey 是账号敏感凭证**，泄露后他人可冒用你的账号下载。不要提交到任何仓库。

#### 4.3 163 邮箱授权码 —— 怎么获取？

每日日报需要 SMTP 发送。163 邮箱的登录密码不能直接用，需要**授权码**：

1. 登录 163 邮箱网页版 → 设置 → **客户端授权密码**（POP3/SMTP/IMAP）
2. 开启 SMTP 服务，按提示用手机验证生成**授权码**（16 位字母数字）
3. 填入 `.env`：

```
MAIL_HOST=smtp.163.com
MAIL_PORT=465
MAIL_USER=你的邮箱@163.com
MAIL_AUTH_CODE=你的16位授权码
MAIL_TO=收件邮箱@163.com
```

> 授权码等同邮箱密码，**绝不能**进仓库/代码。只在 `.env` 里。

#### 4.4 qBittorrent WebUI

```
QBIT_URL=http://127.0.0.1:9091
QBIT_USER=你的WebUI用户名
QBIT_PASS=你的WebUI密码
```

若 qBittorrent 不在本机，把 `QBIT_URL` 换成 `http://服务器IP:9091` 并保证网络可达。

### 5. 试运行（不改变任何真实状态）

```bash
.venv/bin/python -m pt_shualiu.daemon --check      # 检查配置/连接/页面解析
.venv/bin/python -m pt_shualiu.daemon --once --dry-run   # 空跑一轮，只读不写
```

确认输出正常后，再正式启动。

### 6. 以 systemd 服务常驻

编辑 `pt_shualiu.service`，把 `User`、`WorkingDirectory`、`ExecStart`、`EnvironmentFile` 改成你的实际路径，然后：

```bash
sudo cp pt_shualiu.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pt_shualiu
journalctl -u pt_shualiu -f          # 查看日志
```

### 7. 查看运行状态

```bash
./status.sh
```

会显示：各站点考核进度、在保种子数、磁盘占用、限速状态、进程健康度等。

---

## 测试

```bash
.venv/bin/pytest -v
```

测试使用本地 fixtures（含模拟的站点页面），不访问任何真实站点/邮箱。

## 注意事项

- **只用个人账号，遵守站点规则**：过考核≠滥用，别把站刷死
- Cookie / passkey / 授权码都是敏感信息，`.env` 已被 `.gitignore` 排除，请勿删除该配置
- 首次部署建议先 `--dry-run` 空跑一两天，观察选种与限速是否符合预期
- 服务器时区建议设 `Asia/Shanghai`（或按 `pt_shualiu.service` 里 `Environment=TZ=Asia/Shanghai` 设置），邮件门限按北京时间触发

## 目录：文档

- `docs/2026-08-07-pt-shualiu-design.md` — 设计文档（需求、架构、数据流）
- `docs/2026-08-07-pt-shualiu-plan.md` — 实现计划（TDD 任务分解与执行计划）
