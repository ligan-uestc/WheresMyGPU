# WheresMyGPU — 多机 GPU 空闲监控与自动提醒

持续连接多台 GPU 服务器，定时执行 `nvidia-smi`，读取每张显卡的**显存占用、计算利用率、
显存带宽利用率和正在运行的进程**；当任意一张卡空闲或低负载时，**自动给你发邮件**。

带一个完整的图形界面：调阈值、增删服务器、配邮件都有对应的输入区域。
启动之后就不需要再管它 —— 它会一直按设定间隔轮询。

---

## 目录

- [图形界面（推荐）](#图形界面推荐)
- [命令行方式](#命令行方式)
- [它是怎么工作的](#它是怎么工作的)
- [判定阈值（重要）](#判定阈值重要)
- [邮件提醒怎么配](#邮件提醒怎么配)
- [全部命令](#全部命令)
- [配置文件详解](#配置文件详解)
- [开机自启 / 后台常驻](#开机自启--后台常驻)
- [输出文件](#输出文件)
- [没有真实服务器时先试用](#没有真实服务器时先试用)
- [常见问题](#常见问题)
- [项目结构与测试](#项目结构与测试)

---

## 图形界面（推荐）

双击 **`run_gui.bat`** 就会打开界面（不会多出一个黑色命令行窗口）。
等价命令：

```bat
python -m wheresmygpu --gui
```

界面一共 5 个标签页：

| 标签页 | 能做什么 |
| --- | --- |
| **监控状态** | 开始 / 停止监控、立即检查一次；实时表格里**绿色 = 空闲**；双击任意一行可以看到这张卡「为什么不算空闲」——逐条列出满足和没满足的条件 |
| **服务器** | **添加 / 编辑 / 删除 / 上下排序**服务器。主机、端口、用户、密码都有对应输入框；登录方式可选密码 / 密码文件 / 密钥文件；还有「测试连接」按钮，点一下就知道能不能连上、读到几张卡 |
| **阈值设置** | 所有判定阈值都有对应输入框（剩余显存、利用率、显存带宽、温度、进程……），每项旁边可以勾选启用 / 停用；底部有**实时规则预览**，改一个数字立刻看到规则变成什么样，配置有坑时直接红字警告 |
| **邮件提醒** | 填 SMTP 服务器、端口、加密方式、账号、授权码、收件人；有常见邮箱的**服务商预设**（选一下自动填好服务器和端口）；有**「发送测试邮件」**按钮，立刻验证能不能发出去 |
| **运行日志** | 程序日志实时滚动，方便排查 |

改动**要点右下角「保存配置」才会写进 `config.yaml`**。保存前会先自检一遍，保证写出来的
文件监控程序一定读得回来；旧文件自动备份成 `config.yaml.bak`。

> 界面和命令行读写的是**同一份 `config.yaml`**，可以混着用：
> 用界面调阈值、加服务器，用命令行挂后台常驻。

---

## 命令行方式

不想用界面时，全流程也可以纯命令行完成：

```bat
cd /d D:\WheresMyGPU

:: 1) 生成配置文件（只需一次）
python -m wheresmygpu --init

:: 2) 编辑 config.yaml，填入两台服务器的 SSH 信息

:: 3) 验证：能不能连上、能不能读到 GPU、阈值是否合理
python -m wheresmygpu --check

:: 4) 验证：邮件能不能真的发出去（会真的发一封测试邮件）
python -m wheresmygpu --test-notify

:: 5) 开始持续监控（前台运行，Ctrl+C 停止）
python -m wheresmygpu
```

也可以直接双击 **`run.bat`**，它等价于第 5 步。

先不接真实服务器、只想看看效果？见 [没有真实服务器时先试用](#没有真实服务器时先试用)。

---

## 已在真实 GPU 服务器上验证通过

连续跑 3 轮，行为完全符合预期：

```
第 1 轮: 提醒 0 条     ← 建立"连续满足"计数
第 2 轮: 提醒 4 条     ← 连续 2 次确认空闲，触发提醒（桌面通知 + 提示音）
第 3 轮: 提醒 0 条     ← 冷却生效，不重复刷屏
```

期间还真实观察到了显卡被抢占：服务器 1 的 GPU 0/2/4/5 在第 1 轮时空闲，
第 3 轮已被占用（22.8G 显存、1 个进程），程序如实反映了这一变化。

> **两个实测发现（重要）**
>
> 1. **空闲卡的剩余显存是 23.6 ~ 23.8G，不是 24G。** 驱动自身要占一点显存，
>    所以"剩余显存 > 24G"这类阈值在 3090 上**永远不可能成立**。
>    默认值 20G 才是可用的写法。
> 2. **你的 `~/.ssh/id_rsa` 不被服务器接受**，而且这个文件会让 paramiko 2.8.1
>    把它当成 DSA 密钥再解析一遍，进而在新版 `cryptography` 上抛出
>    `q must be exactly 160, 224, or 256 bits long`，**在 SSH 握手线程里直接
>    中断认证，导致密码登录根本没机会执行**。程序已修正为显式加载
>    Ed25519 / ECDSA / RSA 密钥并跳过已废弃的 DSA，同时默认走密码登录。

---

## 它是怎么工作的

```
                ┌──────────────┐
   每 N 分钟 ──▶ │  并行轮询两台  │ ──SSH──▶ nvidia-smi --query-gpu=...
                │   服务器      │ ──SSH──▶ nvidia-smi --query-compute-apps=...
                └──────┬───────┘ ──SSH──▶ ps（补全进程的属主和运行时长）
                       │
                       ▼
              ┌─────────────────┐
              │ 阈值判定（逻辑与）│  剩余显存 / GPU 利用率 / 显存利用率 / 温度 / 进程
              └────────┬────────┘
                       │ 连续满足 N 次（防误报）
                       ▼
              ┌─────────────────┐
              │ 冷却去重（防刷屏）│  同一张卡 N 分钟内只提醒一次
              └────────┬────────┘
                       ▼
        桌面通知 / 飞书 / 钉钉 / 企微 / 邮件 / Webhook / 提示音 / 日志
```

同时每一轮都会落盘：`history.jsonl` + `history.csv`（便于事后统计谁在用卡），
以及 `state.json`（记录冷却和连续次数，**重启后不会重复提醒**）。

设计上的几个要点：

| 机制 | 作用 |
| --- | --- |
| **`sustained_checks`（连续满足次数）** | `nvidia-smi` 的利用率是瞬时值。任务在两次 kernel 之间可能瞬时显示 0%，只采一次极易误报。默认要求连续 2 轮都满足才提醒。 |
| **`cooldown_minutes`（冷却）** | 同一张卡在冷却期内不再重复提醒；冷却结束后如果它仍然空闲，会再提醒一次（相当于"还在空着"的提醒）。 |
| **状态持久化** | 程序重启、电脑重启后不会把已经提醒过的卡再提醒一遍。 |
| **SSH 连接复用** | 长连接 + keepalive，不用每轮重新握手。连接失效会自动重连。 |
| **双 SSH 后端** | 优先用 paramiko（支持密码/密钥），连不上或协商失败自动回退到系统 `ssh`。 |
| **并行轮询** | 多台服务器同时查询，总耗时约等于最慢的一台。 |
| **单台失败不影响其他** | 某台连不上只记录失败并（按配置）提醒，其余服务器照常监控。 |

---

## 判定阈值（重要）

当下面**所有**条件同时满足时，才认为这张卡"空闲可用"（逻辑与，可用 `null` 关掉某一项）：

| 配置项 | 含义 | 默认 |
| --- | --- | --- |
| `min_free_memory_gb` | 剩余显存下限（GB） | `20.0` |
| `min_free_memory_pct` | 剩余显存下限（百分比，跨型号更通用） | `null`（关闭） |
| `max_gpu_utilization` | GPU 计算利用率上限（%） | `5.0` |
| `max_memory_utilization` | 显存带宽利用率上限（%） | `10.0` |
| `max_temperature_c` | 温度上限（℃） | `null`（关闭） |
| `require_no_processes` | 是否要求完全没有进程 | `false` |
| `max_process_memory_gb` | 进程占用显存上限（GB） | `null`（关闭） |

> ### ⚠️ 关于「剩余显存大于 24G」这个例子
>
> **RTX 3090 的总显存就是 24GB（24576 MiB）**，所以「剩余显存 > 24G」这张卡
> 永远不可能满足 —— 那要求整张卡 100% 完全空着，连驱动都不占显存。
> 规则会一直沉默，你会以为程序坏了。
>
> 这就是为什么默认值是 **`20.0`**（约等于"基本没人用"）。
>
> 程序会在发现这种不可能满足的阈值时**主动告警**：
> ```
> ⚠️ 阈值警告: min_free_memory_gb=24 >= total VRAM 24G of NVIDIA GeForce RTX 3090:
>    needs an entirely empty card, so it will (practically) never fire
> ```
> `python -m wheresmygpu --check` 和运行日志里都会提示。
>
> 如果换成了 A100 80G、H100 等卡，再按实际容量调整即可；或者直接用
> `min_free_memory_pct: 85` 这种按比例的说法，换卡也不用改配置。

---

## 邮件提醒怎么配

提醒方式就是**邮件**：只要有显卡空闲，程序就给你发一封邮件。
配置在界面的「邮件提醒」标签页里填最省事，也可以直接改 `config.yaml`：

```yaml
notify:
  channels:
    - type: email
      smtp_host: ""    # SMTP 服务器
      smtp_port: 465            # SSL 用 465，STARTTLS 用 587
      use_ssl: true             # 465 端口填 true
      starttls: false           # 587 端口填 true
      username: ""      # 登录账号（完整邮箱）
      password: ""       # 注意不是网页登录密码
      to: ""            # 收件人；多个用英文逗号分隔
      from: ""          # 可省略，默认用 username

    # 本地再留一份提醒记录（不打扰，建议保留）
    - type: logfile
      path: logs/alerts.log
```

### 关键：密码要填「授权码」，不是登录密码

QQ / 163 / 126 / Gmail 这类邮箱，直接用登录密码会认证失败。要先去邮箱网页版开启
SMTP 服务并生成一串**授权码**：

| 邮箱 | 怎么拿授权码 | SMTP 服务器 | 端口 |
| --- | --- | --- | --- |
| QQ 邮箱 | 设置 → 账户 → POP3/IMAP/SMTP → 开启 SMTP → 生成授权码 | `smtp.qq.com` | 465 (SSL) |
| 腾讯企业邮 | 邮箱设置 → 客户端设置 | `smtp.exmail.qq.com` | 465 (SSL) |
| 163 / 126 | 设置 → POP3/SMTP/IMAP → 开启 → 新增授权密码 | `smtp.163.com` | 465 (SSL) |
| Gmail | 需要「应用专用密码」（要先开两步验证） | `smtp.gmail.com` | 465 (SSL) |
| Outlook | 账户 → 安全 → 应用密码 | `smtp.office365.com` | 587 (STARTTLS) |

界面里选一下「邮箱服务商」就会自动填好服务器和端口，剩下的只要填账号、授权码、收件人，
然后点 **「发送测试邮件」** 立刻验证。

命令行等价操作：

```bat
python -m wheresmygpu --test-notify
```

它会给所有已配置通道各发一条测试消息并报告成功/失败：

```
正在测试 2 个提醒通道…

  [✓] email            email                      2.31s
  [✓] logfile          logfile -> D:\WheresMyGPU\logs\alerts.log 0.00s

成功 2/2 个通道。
```

### 邮件长什么样

```
主题: 🎮 GPU 空闲可用了：服务器 2 · GPU 3
正文:
  服务器: 服务器 2
  GPU: #3 NVIDIA GeForce RTX 3090

  剩余显存: 23.6 GB / 24 GB (已用 0.0 GB, 0%)
  GPU 利用率: 0%   显存利用率: 0%
  温度: 41°C   功耗: 27W / 350W
  进程数: 0

  判定阈值: free VRAM >= 20G, GPU util <= 5%, ...
  连续满足 2 次检查（约 10m）
  检测时间: 2026-10-23 00:59:56
```

### 一轮一封：多张卡会汇总成一封邮件

一次检查里如果有多个 GPU 同时空闲，**只会发一封邮件**，而不是每张卡各发一封。
16 张卡一起空出来，收到的是这样一封 20 多行的汇总：

```
主题: 🎮 16 张 GPU 空闲可用了 · 2 台服务器
正文:
  本轮检查命中 16 项，已汇总成一封邮件（2026-10-23 11:33:30）
  涉及服务器: 服务器 1、服务器 2

  【服务器 1】8 张
    · GPU #0  RTX 3090 — 剩余 23.6G/24G · 利用率 0% · 显存 0% · 进程 0 · 41°C
    · GPU #1  RTX 3090 — 剩余 23.6G/24G · 利用率 0% · 显存 0% · 进程 0 · 41°C
    …
  【服务器 2】8 张
    · GPU #0  RTX 3090 — 剩余 23.6G/24G · 利用率 0% · 显存 0% · 进程 0 · 41°C
    …

  判定阈值: free VRAM >= 20G, GPU util <= 5%
```

规则：

- **只有一张卡空闲**时，仍然发原来那种完整的单卡邮件（含温度、功耗、进程明细）。
- **多张卡空闲**时，按服务器分组列成清单，每张卡一行；阈值只写一次。
  某张卡上有进程的话，进程明细会跟在它那一行下面。
- 同一轮里「有卡空出来」和「有卡被占用」算两类，各发一封。
- 「服务器连不上 / 已恢复」同理，多台一起出问题时合成一封。

合并只影响「发几封」，不影响「要不要发」：**去抖（连续几次才算）和冷却时间
仍然是每张卡各自独立计算的**。

### 其它可选的提醒方式

邮件是默认且推荐的。如果还想同时收到别的（比如推到手机），可以在界面上勾选，
或在 `config.yaml` 里加进来 —— **可以同时启用多个**：

| `type` | 说明 | 需要什么 |
| --- | --- | --- |
| **`email`** | **SMTP 邮件（默认，推荐）** | 邮箱 + 授权码 |
| `logfile` | 写入独立的告警文件 | 无（默认开，本地留档） |
| `windows_toast` | Windows 10/11 原生桌面通知 | 无，不用装东西 |
| `sound` | 提示音 | 无 |
| `console` | 终端彩色打印 | 无 |
| `jsonl` | 每个事件一行 JSON，便于二次处理 | 无 |
| `feishu` | 飞书 / Lark 群机器人 | 群机器人 webhook 地址 |
| `dingtalk` | 钉钉群机器人 | webhook + 可选关键词 |
| `wecom` | 企业微信群机器人 | webhook 地址 |
| `serverchan` | Server 酱（推到微信） | send_key |
| `bark` | Bark（推到 iOS） | key |
| `telegram` | Telegram Bot | bot_token + chat_id |
| `slack` / `discord` | Slack / Discord Webhook | webhook 地址 |
| `webhook` | 任意 HTTP POST，可自定义 JSON 模板 | URL |
| `command` | 兜底：执行任意本地命令 | 命令；事件通过 `WHERESMYGPU_*` 环境变量传入 |

`python -m wheresmygpu --channels` 可以随时列出全部可用通道。

配置示例（挑你要的加上即可）：

```yaml
notify:
  channels:
    - type: email
      smtp_host: ""
      smtp_port: 465
      use_ssl: true
      username: ""
      password: ""
      to: ""

    # 电脑前也能立刻注意到
    - type: windows_toast
    - type: sound

    # 飞书：群设置 → 群机器人 → 添加自定义机器人 → 复制 webhook
    - type: feishu
      webhook: https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
```

---

## 全部命令

```
python -m wheresmygpu [选项]

  （不加选项）        持续监控，直到 Ctrl+C
  --init              生成 config.yaml 模板（--force 可覆盖）
  --check             校验配置、SSH 连通性、nvidia-smi 可用性、阈值是否合理
  --test-notify       逐个测试所有已配置的提醒通道
  --once              只跑一轮，打印表格后退出（会正常发提醒）
  --once --dry-run    只评估和打印，不发提醒、不写状态（调阈值时用）
  --status            打印上一次监控的缓存状态（不连服务器）
  --channels          列出所有可用的提醒方式
  --config PATH       指定配置文件
  --interval 5m       临时覆盖轮询间隔（支持 300 / 5m / 1h）
  --log-level DEBUG   临时覆盖日志级别
  --version           版本号
```

外层的 `run.bat` 就是 `python -m wheresmygpu`，会自动找到 Python 解释器。

---

## 配置文件详解

完整带注释的模板见 **`config.example.yaml`**。分段说明：

### `app` — 运行参数

```yaml
app:
  interval_seconds: 300      # 轮询间隔（秒）。5 分钟 = 300
  jitter_seconds: 0          # 随机抖动，避免所有机器同一秒被唤醒
  log_dir: logs
  state_file: state.json
  history_file: history.jsonl
  history_csv: history.csv   # 设为 null 可关闭
  log_level: INFO            # DEBUG / INFO / WARNING / ERROR
  console: true              # 是否同时把日志打到控制台
```

> 路径写相对路径时，是相对于**配置文件所在目录**。

### `ssh` — 连接参数

```yaml
ssh:
  transport: auto            # auto | paramiko | system_ssh
  connect_timeout: 20
  command_timeout: 30
  keepalive: 30
  max_retries: 2             # 单轮内每条命令的重试次数
  retry_backoff: 3.0
  reuse_connection: true     # 复用长连接（推荐）
  nvidia_smi_paths:          # 远程 nvidia-smi 的候选位置，按顺序探测
    - nvidia-smi
    - /usr/bin/nvidia-smi
    - /usr/local/nvidia/bin/nvidia-smi
```

`transport` 说明：
- `auto`（默认）：先试 paramiko，失败自动回退到系统 `ssh`。
- `paramiko`：支持密码和密钥登录，能复用长连接。
- `system_ssh`：调用系统 OpenSSH 客户端，**只能用密钥**（不支持密码）。

### `servers` — 服务器列表

```yaml
servers:
  - name: server-1          # 唯一标识，用于状态文件和日志
    label: 服务器 1          # 显示名（可中文）
    host: ""
    port: 22
    user: ""
    enabled: true
    # 三选一（或组合）：
    password: ${WHERESMYGPU_PW_1}   # 推荐：从环境变量读
    # key_file: ~/.ssh/id_rsa
    # password_file: ~/.ssh/gpu1.pass
    alert:                       # 可选：这台机器单独覆盖全局阈值
      min_free_memory_gb: 18
```

想监控三台以上，继续往列表里加就行，没有数量限制。

### `alert` — 阈值与提醒策略

见 [判定阈值](#判定阈值重要)，另外几个策略项：

```yaml
alert:
  sustained_checks: 2            # 连续满足几次才提醒
  cooldown_minutes: 60           # 同一张卡的提醒冷却（分钟）
  notify_on_busy: false          # 已提醒的空闲卡又被占用时，是否再通知
  notify_on_recovery: true       # 服务器恢复连接时通知
  notify_on_unreachable: true    # 服务器连不上时通知
  unreachable_after_failures: 3  # 连续失败几次才判定为「连不上」
```

### 环境变量

配置里任何字符串都能用 `${VAR}` 或 `${VAR:-默认值}`：

```yaml
password: ${WHERESMYGPU_PW_1}        # 未设置则报错，提示你配置
password: ${WHERESMYGPU_PW_1:-}      # 未设置则留空（回退到密钥登录）
```

---

## 开机自启 / 后台常驻

### 方式一：开机自动启动（推荐）

以**管理员身份**打开 PowerShell：

```powershell
cd D:\WheresMyGPU
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
```

它会在「任务计划程序」里注册一个登录时启动的任务，并把日志写到 `logs\`。

卸载：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -Remove
```

### 方式二：手动后台运行（不弹窗口）

双击 **`start_hidden.vbs`**，它会在后台静默启动，日志照常写入 `logs\`。

停止：打开任务管理器结束 `python.exe`，或运行 `stop_monitor.bat`。

### 方式三：就开着个命令行窗口

直接双击 `run.bat`，最小化即可。Ctrl+C 停止。

---

## 输出文件

| 文件 | 内容 |
| --- | --- |
| `logs/wheresmygpu-YYYYMMDD.log` | 完整运行日志，自动轮转（单文件 8MB，保留 5 份） |
| `logs/alerts.log` | 只含提醒事件，便于快速翻看 |
| `history.jsonl` | 每一轮每张卡一条 JSON（字段最全，含进程明细） |
| `history.csv` | 同一份数据的 CSV（Excel 可直接打开，UTF-8 BOM） |
| `state.json` | 冷却/连续次数/失败次数，重启不丢 |

`history.csv` 字段：`timestamp, iso_time, server, server_label, ok, gpu_index, gpu_name,
mem_total_mb, mem_used_mb, mem_free_mb, mem_used_pct, gpu_util_pct, mem_util_pct,
temperature_c, power_w, power_limit_w, proc_count, proc_memory_mb, processes, is_free, error`

想统计"这张卡这周被谁占了多少时间"，直接拿 `history.csv` 做透视表即可。

---

## 没有真实服务器时先试用

程序自带一个**模拟 GPU 服务器**（一个真的会响应 SSH 的本地服务），
可以在没有真实机器、或还没配好密码时验证整套流程。

```bat
:: 终端 1：起两个模拟服务器（2222 一开始两张卡都忙，25 秒后释放；2223 有一张空闲）
python -m wheresmygpu.mock_server --port 2222 --gpus 2 --busy 2 --idle-after 25
python -m wheresmygpu.mock_server --port 2223 --gpus 2 --busy 1

:: 终端 2：用自带的演示配置监控它们
python -m wheresmygpu --config demo.yaml --interval 10s
```

你会看到它检测到空闲显卡并弹出提醒。`demo.yaml` 里配的是两个模拟服务器，
和真实场景完全一致，只是把主机换成了 `127.0.0.1`。

常用参数：

```
--gpus N          模拟几张卡
--busy N          让前 N 张卡一开始处于忙碌状态
--idle-after SEC  多少秒后把忙碌的卡释放为空闲（用来演示"任务结束"）
--write-demo-config PATH   顺便生成一份指向本模拟服务器的配置
```

---

## 常见问题

**Q：`--check` 报 `Permission denied (publickey,password)`**
服务器不接受你的密钥。三种解决办法：
1. 在 `config.yaml` 里填 `password`（或设环境变量 `WHERESMYGPU_PW_1`）；
2. 把公钥装到服务器：`ssh-copy-id -p 端口 ubuntu@主机`；
3. 用 `password_file` 指向一个存密码的文件。

**Q：报 `nvidia-smi not found in any of [...]`**
远程 `nvidia-smi` 不在常规位置。先 `ssh` 上去执行 `which nvidia-smi`，
把结果加到 `ssh.nvidia_smi_paths` 列表里。

**Q：报 `nvidia-smi listed no GPUs`**
容器没有挂载 GPU，或驱动没装好。在服务器上直接跑 `nvidia-smi` 确认。

**Q：一直不提醒，但卡明明是空的？**
按顺序排查：
1. `python -m wheresmygpu --once` 看表格里的 `STATE` 是不是 `FREE`；
2. 不是的话，看日志里 `未满足:` 后面的条件，那就是没达标的项；
3. 检查 `sustained_checks` —— 默认要连续 2 轮都空闲才提醒，也就是约 10 分钟；
4. 检查 `cooldown_minutes` —— 冷却期内不会重复提醒；
5. 用 `--check` 看有没有"阈值警告"（比如 24G 那种不可能满足的阈值）。

**Q：不想收到"服务器连不上"的提醒**
```yaml
alert:
  notify_on_unreachable: false
```

**Q：想调成"只要有 1 张卡空着就立刻告诉我"**
```yaml
alert:
  min_free_memory_gb: 20
  max_gpu_utilization: 5
  sustained_checks: 1     # 采到一次就提醒
  cooldown_minutes: 5     # 冷却短一点
```

**Q：桌面通知没出现？**
Windows 的「专注助手 / 勿扰模式」会拦截通知。另外首次通知可能被系统折叠到
通知中心，点开右下角通知图标即可看到。`--test-notify` 可以单独验证。

**Q：想调整轮询间隔**
改 `app.interval_seconds`，或临时用 `--interval 2m`。最小 10 秒。

---

## 项目结构与测试

```
D:\WheresMyGPU\
├─ wheresmygpu\                  主程序包
│  ├─ gui.py                 图形界面（Tkinter，5 个标签页）
│  ├─ cli.py                 命令行入口与各子命令
│  ├─ config.py              配置加载 / 校验 / 环境变量展开
│  ├─ config_writer.py       配置回写 YAML（界面保存用，带 .bak 备份）
│  ├─ ssh_runner.py          SSH 传输层（paramiko + 系统 ssh 双后端，带重试与重连）
│  ├─ nvidia.py              nvidia-smi 查询构造与解析（GPU + 进程 + ps 补全）
│  ├─ analyzer.py            阈值判定，逐条给出"满足/未满足"的原因
│  ├─ monitor.py             轮询主循环、状态机、冷却去重、表格渲染
│  ├─ notify.py              16 种提醒通道（含邮件）
│  ├─ state.py               状态持久化（原子写）
│  ├─ history.py             JSONL / CSV 历史记录
│  └─ mock_server.py         模拟 GPU 服务器（演示与测试用）
├─ tests\
│  ├─ test_wheresmygpu.py       52 个单元测试
│  ├─ test_gui.py            24 个界面 / 配置回写测试
│  └─ test_end_to_end.py     10 个端到端测试（真跑 SSH）
├─ run_gui.bat               打开图形界面（推荐入口）
├─ run.bat                   不开界面，直接持续监控
├─ stop_monitor.bat          停止后台监控
├─ start_hidden.vbs          后台静默启动（无窗口）
├─ install_autostart.ps1     注册开机自启
├─ config.example.yaml       配置模板（含全部注释）
├─ config.yaml               实际使用的配置（界面保存的就是它）
├─ demo.yaml                 演示配置（指向模拟服务器）
└─ requirements.txt
```

跑测试：

```bat
python -m unittest discover -s tests -t . -v
```

86 个测试，覆盖：

- **解析**：CSV 解析（含 `[N/A]`、缺列、单卡回退）、UUID 进程归属、`ps` 补全
- **阈值逻辑**：逐条条件判定，含「24G 的卡永远不满足 24G 规则」的回归用例
- **配置**：校验、环境变量展开、界面保存后能原样读回（含中文、多收件人、密码）
- **状态机**：冷却、连续次数、连不上 / 恢复
- **界面**：5 个标签页齐全、阈值输入框正确回填、改阈值后能存回磁盘、
  非法值被拒绝、邮件开关与收件人解析、服务商预设自动填服务器和端口
- **端到端**：测试里真的起一个 SSH 服务器，走完
  socket → 认证 → exec → 解析 → 判定 → 提醒，再断言提醒内容正确

依赖只有三个（`paramiko` / `requests` / `PyYAML`）。图形界面用的 Tkinter、
桌面通知和提示音用的都是 Windows / Python 自带能力，不需要额外装任何东西。