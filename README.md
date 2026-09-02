项目架构目录
Crypto_Trading_Bot/
├── config/
│   └── settings.yaml          # 核心配置：阈值/品种/报警映射/TTS设置
├── secrets/
│   ├── push.env.example       # Key 模板说明（不含真值）
│   └── .env                   # 你的只读 key（gitignore，仅本人可读写）★不入库
├── src/
│   ├── alert.py               # 系统通知(Windows toast)+播 MP3+写日志
│   ├── gate_client.py         # 只读 Gate 客户端（多账户，白名单 GET）
│   ├── keystore.py            # 账户密钥管理
│   ├── ledger.py              # L3 每日账本/回撤计算
│   ├── notify.py              # 通知逻辑组件
│   ├── position_printer.py    # 持仓信息格式化打印
│   ├── risk.py                # L1-L4 风险计算核心（已按真实账户校准）
│   ├── risk_board.py          # 一次性中文风险看板
│   ├── setup_keys.py          # 录入脚本本体
│   ├── signal_oi.py           # OI×Price 四分类语义信号逻辑
│   └── watch.py               # 主循环（常驻监控）
├── data/                      # ★不入库
│   ├── equity/                # 权益曲线数据 (主账户.json)
│   ├── ledger/                # 每日盈亏账本 (按日期分文件)
│   ├── oi_baseline.json       # 趋势层本地 OI 基线
│   └── snapshot.json          # 账户/行情实时快照
├── logs/                      # ★不入库
│   ├── alerts.log             # 报警历史
│   └── positions.log          # 持仓状态追踪日志
├── sounds/                    # 报警音效文件 (MP3) ★不入库
├── GateQuantTrader/           # 量化交易模块
├── GateStrategyAnalyst/       # 策略分析模块
├── enter_keys.bat             # 录入 key（双击，交互式）
├── run.bat                    # 双击启动常驻监控
├── GateRiskWatcher 风险看板.bat # 双击启动一次性看板
├── HANDOFF.md                 # 项目交接与维护指南
├── 项目大纲.txt                # 项目规划/功能大纲
├── requirements.txt
└── .gitignore
# GateRiskWatcher —  永续合约只读风险哨兵 （第一代）

> 只用**只读** API key，物理上不可能下单。报警/弹窗/铃音全部自己生成。

---

## 零、已按真实账户校准（重要，接手人必读）

本项目在真实只读 key 下实测过，确认以下事实并已写入代码：

- **账户模式 = 混合（逐仓 + 全仓）**：实测账户中 SUI 为逐仓(isolated)、DOGE 为全仓(cross)，`get_account()` 返回的 `margin_mode` 字段为 `0`（全仓标识）。系统用 `account_safety` 按 `isolated_position_margin` 与 `cross_margin_balance` 的相对大小推断每仓模式，而非只读 `margin_mode` 原始字段。
  资金散布在 `isolated_position_margin` 与 `cross_*` 之间，需同时看两者。
- **多空由 `mode` 字段判断**（`dual_long` / `dual_short`），**不能只看 size 正负**（dual 下 size 恒为正）。
- **强平价 Gate 直接给** `liq_price`，零公式风险。
- **ticker 无 `funding_time` 字段**；资金费按 Gate 固定 UTC `00:00/08:00/16:00` 结算推算。
- **ADL 自动减仓名次** `adl_ranking`：Gate API 返回 0-4 的整数档位（本账户 DOGE 仓位实测为 `4`）。
  ⚠️ 口径说明（保守）：该字段与交易所 App 里「5 格亮灯」ADL 指示灯是**两套不同口径**——亮灯格数按同方向
  持仓分段可视化，而 `adl_ranking` 是 API 独立档位，二者不一定相等（实测 API=4 时 App 仅亮 2 格）。
  **系统不再据此弹窗告警**，仅在持仓定时打印中展示原始档位供你自行对照 App。高杠杆前排仓仍以 App 指示灯为准判断实际危险度。
- 账户级安全垫用 `total`(总保证金) 与 `maintenance_margin`(总维持保证金)：可用保证金率 = `(total - maintenance_margin)/total`。

代码里这些假设集中在 `risk.py`（`account_safety` / `funding_countdown` / `position_panel` / `adl_risk`），改账户类型（如改全仓）时优先看这里。

---

## 一、它做什么（4 层）

- **L1 保命**：强平距离实时计算（Gate 直接给 `liq_price`）、账户级可用保证金率、资金费倒计时+方向、ADL 自动减仓名次报警。
- **L2 态势**：统一持仓面板（净敞口/未实现盈亏/占用保证金）、标记价-指数价背离、**未平仓量(OI) 异动三层检测**（瞬时环比 + 大市值专属 + 趋势累计）、**OI×Price 四分类语义信号**（去杠杆/空头踩踏/资金流入/资金压制）、资金费率绝对值异常、插针/瞬时爆拉检测。
- **L3 复盘**：每日盈亏账（已实现+未实现+资金费净支出）、权益曲线最大回撤、强平历史（落 `data/`）。
- **L4 决策**：情景模拟（给定跌幅算强平价/亏损/保证金率），数据落 `data/snapshot.json` 供你或 Hermes Bot 读取。

---

## 二、安全边界（务必读）

- 只用 GET：代码里只有 `list_*` / `get_*` 只读方法，`gate_client.py` 的 `_READONLY_METHODS` 白名单拒绝任何写方法。
- key **不入库、不明文**：运行 `enter_keys.bat` 交互输入，写入 `secrets/.env`（已 gitignore，权限锁为仅本人可读写）。
- key 由 **Gate 鉴权失败自动提醒**（返回 401 即判定过期/失效）：系统当场弹 Windows 系统通知 + 响铃，告诉你"哪个账号过期了"。你双击 `enter_keys.bat` 重填、关掉旧 `run.bat` 窗口、重开 `run.bat` 即可，**无需任何倒计时配置**。
- 任何日志/输出里 key 一律打码（`****`）。
- **它永远不能帮你下单、平仓、改杠杆**——只喊风险。

---

## 三、首次使用

```bat
cd "F:\Program Files\Crypto_Trading_Bot"

REM 1) 建独立环境并装依赖（只要一次）
uv venv .venv --python 3.11
uv pip install -r requirements.txt

REM 2) 录入你的只读 key（双击 enter_keys.bat，在弹窗里自己输入，）
REM    多账户及平台按提示追加（account_2 对应 GATE_API_KEY_2 / GATE_API_SECRET_2）

REM 3) 看实时风险看板（一次性，人话）
.venv\Scripts\python.exe src\risk_board.py
REM    ↑ 也可以直接双击项目根目录的「GateRiskWatcher 风险看板.bat」，效果相同，看完按任意键关窗。
REM    ↑ 启动时会按 UTC 日自动补齐每日账本（单次最多 7 天，跨多次开机续补至 180 天），可能需要多转几秒。
REM    ↑ 补齐与实时看板数据【并发】拉取：过程显示 🔴红灯进度条（如 `█░░ 1/7 天 2026-02-27`），完成后变 🟢绿灯报进度。

REM 4) 正式常驻监控（关闭窗口即停）
run.bat
```

> 注意：双击 `run.bat` / `enter_keys.bat` 的 cmd 环境里 `uv` 可能不在 PATH（尤其非 hermes 终端）。
> 若报 "uv 不是命令"，先用 hermes 终端跑一次 `uv venv` + `uv pip install`（venv 建好后可双击）。

多账户：录入时 `account_2` 对应 `GATE_API_KEY_2/GATE_API_SECRET_2`，系统**按实际存在的 key 自动决定监控几个账户**（`config/settings.yaml` 的 `accounts` 只是显示名）。

---

## 四、配置 `config/settings.yaml`

- `poll_account_seconds` / `poll_market_seconds`：轮询间隔（秒）。别低于 5s 以免限频。
- `risk.liq_distance_critical_pct`：强平距离低于此值 → **critical** 报警（默认 2%）。
- `risk.liq_distance_warning_pct`：低于此值 → warning（默认 5%）。
- `risk.margin_ratio_warning_pct`：账户级可用保证金率低于此 → warning（默认 30%）。
- `funding.settle_warning_seconds`：距资金费结算剩余秒数且你是付方 → warning。
- `anomaly.oi_change_pct`：小市值币未平仓量(20s)环比变化超此 → warning（默认 8%）。
- `anomaly.big_oi_threshold`：OI 张数 > 此值视为「大市值资产」（默认 1 亿张≈1亿美元）。动态判定，无需维护名单。命中时弹窗加 ⚠ + 专属音效 + 巨鲸级别说明。
- `anomaly.big_oi_warn_pct`：大市值币 20s 环比阈值（默认 1.5%，比小币 8% 灵敏，让大币瞬时异动也能抓）。
- `anomaly.oi_trend_window_minutes`：趋势层本地基线窗口（默认 60 分钟）。
- `anomaly.oi_trend_pct`：大币小币同阈值的累计变化（默认 30%）。窗口内 OI 累计超此 → 弹「OI 趋势异动」（抓急涨急跌，补大币慢速盲区）。
- `anomaly.oi_signal_oi_pct` / `oi_signal_price_pct`：OI×Price 四分类信号阈值（默认 OI 5% / 价格 0.3%，需双向同时达阈值才归类）。
- `anomaly.price_spike_pct`：白名单内币种标记价环比插针/爆拉阈值（默认 1.5%）。
- `anomaly.funding_abnormal_rate_pct`：资金费率绝对值异常阈值（默认 0.05%）。
- `anomaly.signal_labels`：四分类信号中文字段映射（去杠杆/空头踩踏/资金流入/资金压制），可在配置里改措辞。
- `accounts`：给每个 key 起显示名。
- `daily_ledger_backfill_days`：账本补齐总窗口（默认 180）。从未有账本时从该天数前补到昨天；覆盖看板全周期。
- `daily_ledger_backfill_per_run_days`：单次运行最多拉几天（默认 7）。防一次性 180 天过载/超时；跨多次运行/多次开机自动续补，追平到昨天才停。非 24/7 用户每天开机啃 7 天，约 26 次攒满 180 天。
- 看板 `GateRiskWatcher 风险看板.bat` 启动即触发补齐（非驻留、拉完按键关）；补齐异常不影响看板显示。**常驻 run.bat 不做账本补齐**（避免后台静默抢补、看不到进度），只管实时告警与快照。

---

## 五、报警与你的 MP3

把你自己生成的铃音放到 `sounds/`：

```
sounds/critical.mp3   # 强平临界（最危险）
sounds/warning.mp3    # 资金费/背离/OI/ADL 异动（小币 OI 异动也用此音）
sounds/info.mp3       # 每日账本/信息
sounds/big_oi.mp3          # 大币瞬时异动(20s环比>1.5%)，专属音效
sounds/big_oi_trend.mp3    # 大币趋势异动(60min累计>30%)，专属音效
```

缺哪个文件，该级别就不发声，只弹 Windows 系统通知（常驻右下角操作中心、可手动关闭）+ 写 `logs/alerts.log`。
映射在 `config/settings.yaml` 的 `alert_sounds` 段改（`big_oi` / `big_oi_trend` 为自定义键，`fire()` 支持 `sound=` 参数覆盖 level 默认音效）。

报警抑制：同一 warning/info 在 `alert.suppress_seconds`（默认 300s）内不重复响；critical 不受抑制。

### 三层音频系统（2026-08-26 升级）

原来的告警只有"静态 MP3 响铃"。现已升级为三层、串行、自清理的音频架构（实现见 `src/alert.py` 的 `AlertDispatcher` + `SequentialAudioWorker`，对外接口不变）：

| 层 | 角色 | 技术 | 触发 |
|----|------|------|------|
| **Layer 1 信号层** | 夺命响铃，"抓注意力" | 现有静态 MP3（`sounds/` 下你生成的） | 每次告警都响（按 `sound=` 选音效键） |
| **Layer 2 信息层** | 把动态告警文本**念出来** | `edge-tts` 云端神经语音（需联网） | `settings.tts.enabled=true` 时，按 `tts.layers` 选级别 |
| **Layer 3 生存层** | Layer 2 失败时的**本地兜底** | `pyttsx3`（Windows SAPI5，无需联网） | Layer 2 超时/断网/DNS 失败时**自动无缝回退**，绝不静默 |

**关键保证：**
- **串行防打断（Rule 1）**：所有音频（Layer1/2/3）走同一个 thread-safe FIFO 队列 + 专用播放线程，**逐条听完再放下一句**，高频告警不会互相打断/跳过/截断（市场再震荡也不丢字）。单段播放有 30s 硬上限，坏文件也不会卡死整条队列。
- **读后即焚（Temp-Generate-Burn）**：Layer 2/3 每次都用 `tempfile` 生成唯一临时文件，播放结束后立即 `os.remove()`。目标：**磁盘近零占用**，临时文件只在播放期间存在。
- **错误隔离（Rule 3）**：edge-tts 的任何异常（404/超时/DNS）只会触发 Layer 3 回退，**绝不拖垮告警调度主流程**。

**开启语音念报**（默认关，保持原行为）：在 `config/settings.yaml` 的 `tts:` 段把 `enabled: true`。可选 `voice`（如 `zh-CN-XiaoxiaoNeural` 女声 / `zh-CN-YunxiNeural` 男声）、`rate`、`timeout_seconds`（合成超时，到点立刻回退 Layer 3）、`fallback_pyttsx3`（建议保持 `true`）。例：
```yaml
tts:
  enabled: true
  layers: ["critical", "warning", "info"]
  voice: "zh-CN-XiaoxiaoNeural"
  timeout_seconds: 5.0
  fallback_pyttsx3: true
```
> 依赖：`edge-tts`、`pyttsx3`（已加入 `requirements.txt`，重装依赖即可：`uv pip install --python .venv -r requirements.txt`）。


- **API 限流自动降速**：当 Gate 返回限流（429 / `TOO_MANY_REQUEST` / `frequency limit` 等，即拉取过快）时，系统**不会反复硬撞**——会自动把该账户轮询间隔翻倍（封顶 `BACKOFF_MAX=120s`），并弹一条"API 限流，已自动降速"通知说明当前间隔；限流解除后经过冷却（`BACKOFF_COOLDOWN=60s`）逐步把间隔降回 `poll_account_seconds`，并通知"轮询已恢复正常"。鉴权失败（INVALID_KEY/401）与限流**分开处理**：前者按 key 过期逻辑提示重填，后者只降速不误报。

---

## 六、数据落盘

- `data/snapshot.json`：每次拉满的账户/资金/持仓/行情快照（L1/L2 全字段）。
- `data/oi_baseline.json`：趋势层本地 OI 基线（纯本地落盘、跨重启有效、不触交易所）。每 `oi_trend_window_minutes` 窗口重写一次「窗口起点全市场 OI」，用于算累计变化。
- `data/ledger/<账户>_<日期>.json`：每日盈亏账（L3）。
- `data/equity/<账户>.json`：权益曲线（用于回撤）。
- `logs/alerts.log`：所有报警历史。

---

## 七、目录结构

```
Crypto_Trading_Bot/
  config/settings.yaml      # 阈值/品种/报警映射（你改这里）
  config/.env.example       # key 模板说明（不含真值）
  secrets/.env              # 你的只读 key（gitignore，仅本人可读写）★不入库
  src/
    enter_keys.bat          # 录入 key（双击，自己输入）
    setup_keys.py           # 录入脚本本体（交互）
    keystore.py              # 按存在的 key 决定账户列表（原名 secrets.py，因与标准库同名会遮蔽 Layer2 edge-tts 已改名）★不入库
    gate_client.py          # 只读 Gate 客户端（多账户，白名单 GET）
    risk.py                 # L1-L4 风险计算（纯函数，已按真实账户校准）
    alert.py                # 系统通知(Windows toast)+播 MP3+写日志（按级别；非 Windows 回退 tkinter 弹窗）
    ledger.py               # L3 每日账本/回撤
    watch.py                # 主循环（串 4 层，常驻监控；不含账本补齐）
    risk_board.py           # 一次性中文风险看板（并发拉补足+实时数据，带红绿灯进度，看完按键关）
  sounds/                   # 你的 MP3（★不入库）
  data/                     # 快照/账本（★不入库）
  logs/                     # 报警历史（★不入库）
  run.bat                   # 双击常驻监控
  GateRiskWatcher 风险看板.bat  # 双击一次性风险看板（= 手动跑 src\risk_board.py，只读自检，看完按任意键关窗）
  requirements.txt
  .gitignore
```

---

## 八、给接管人（其他用户 / AI 模型）的说明

- 本系统自包含、与 Hermes 解耦。只读契约在 `gate_client.py` 的 `_READONLY_METHODS` 白名单——任何扩展都不准往里加写方法。
- 接 Hermes risk-advisor Bot：只需让 Bot 读 `data/snapshot.json`（纯只读），无需改本项目代码。
- 改交易所/账户类型（如改全仓、加 OKX）：优先改 `risk.py` 的 `account_safety` / `position_panel` / `funding_countdown`，并在真实只读 key 下用 `risk_board.py` 验证字段。
- 实测校准记录见本文件「零、已按真实账户校准」。



## 🚀 核心特性更新 (2026-08-25)

### 🛡️ 智能报警过滤机制 (Smart Alert Filtering)

为了解决全市场资金费结算导致的日志爆炸问题，系统现已实现基于**持仓与白名单**的过滤逻辑。

**核心逻辑：**
系统不再对所有结算事件进行报警，报警范围仅限于：
$$\text{Allow\_Alert} = \{ \text{Active Positions (size > 0)} \} \cup \{ \text{User\_Whitelist} \}$$

**配置方法：**
如果你希望在没有持仓的情况下，依然能够监控特定币种的资金费结算，请修改 `config/settings.yaml`。

1. 定位到 `funding:` 配置块。
2. 在 `monitoring_whitelist: []` 数组中添加你关心的币种。

**示例：**
```yaml
funding:
  settle_warning_seconds: 1800
  # 即使不持有以下币种，也会在结算前 30 分钟触发警告
  monitoring_whitelist: ['SOL_USDT', 'PEPE_USDT', 'WIF_USDT']
  rate_alert_pct: 0.05
```

**注意事项：**
* **格式严谨**：币种名称必须使用**单引号或双引号**括起来。
* **分隔符**：多个币种之间必须使用**英文逗号** `,` 分页。
* **自动失效**：当你平掉某个币种的仓位，且该币种也不在白名单中时，系统会自动停止对其进行资金费预警。

---

## 📊 OI 异动三层检测 + 大币专属音效 (2026-08-25 后续增强)

### 背景
原 OI 异动只有「20s 环比 > 8%」单层检测。实测发现大币（BTC OI≈5.87亿张）要让 20s 环比达 8% 需瞬时净变 4700 万美元（黑天鹅级），等于对大币**失聪**；且旧弹窗不带当前价/24h 涨跌，信息稀疏。本轮重构为三层 + 信息增强。

### 三层 OI 检测
| 层 | 窗口 | 大币阈值 | 小币阈值 | 抓什么 | 弹窗标题 |
|---|---|---|---|---|---|
| 瞬时层 | 20s 环比 | **1.5%** | 8% | 踩踏/逼空前兆 | `⚠ OI 异动 X_USDT` |
| 趋势层 | 60min 累计 | **30%**（同小币） | 30% | 急涨急跌（补大币慢速盲区） | `OI 趋势异动 X_USDT` |
| OI×Price 四分类 | 20s 双向 | OI 5% + 价格 0.3% | 同 | 语义信号（去杠杆/空头踩踏/资金流入/资金压制） | `空头踩踏/资金压制/... X_USDT` |

**大币判定**：动态按 OI 绝对值 `> big_oi_threshold`（默认 1亿张）。实测当前全市场 937 个合约中 14 个算大币（BTC/ETH/DOGE/SHIB/ICP/XAG/XAU/FIL/SUI/TLM/DOGS/ZK/BICO/SKHYNIX）。保持 1 亿张阈值则 ETH/SHIB 也带 ⚠ + 专属音效。

**所有 OI 弹窗均带**：当前价 + 24h 涨跌%（趋势层另带窗口内累计变化%）。大币瞬时异动额外附 `【大市值资产】20s 内 OI 净变约 $X万，属巨鲸/极端行情级别`。

### 设计取舍（已知盲区）
- **5 天慢牛不弹**：如 BTC 64000→80000（每小时 <30% 累计），趋势层不触发。若要抓慢牛需把 `oi_trend_window_minutes` 拉长 + `oi_trend_pct` 降到 20% 量级（代价：BTC 可能每天弹 1 次趋势异动）。
- 趋势层对大币在波动周可能每天弹 1-3 次，嫌烦可调高 `oi_trend_pct` 或拉长窗口。

### 大币专属音效
`alert_sounds` 新增 `big_oi` / `big_oi_trend` 两键。`fire()` 支持 `sound=` 参数覆盖 level 默认音效——大币瞬时传 `big_oi`、趋势传 `big_oi_trend`，小币仍用 `warning`。mp3 缺失时只弹窗不发声、不报错。详见第五节。

### 实测验证（真实只读 key）
- 三层阈值纯函数验证正确（大币瞬时 +1.5% 触发、小币 +7% 不/+9% 触发、趋势 +30% 触发/+25% 不触发）。
- 真实 key 常驻运行无崩溃、无限频。
- `data/oi_baseline.json` 正常生成（含全市场 OI，跨重启有效）。
- `fire(sound='big_oi')` 在 mp3 缺失时静默跳过、不崩主流程。

### 相关文件
- `src/watch.py`：三层检测主逻辑 + 大币 ⚠/音效/巨鲸说明
- `src/risk.py`：`oi_change_pct`（瞬时）、`oi_trend_pct`（趋势累计）、`scenario_full`、`funding_rate_abnormal`
- `src/signal_oi.py`：OI×Price 四分类纯函数（`oi_price_signal` / `interpret_to_alert`）
- `src/alert.py`：`fire()` 加 `sound=` 参数覆盖音效
- `config/settings.yaml`：`anomaly.*` 全部阈值 + `alert_sounds` 两新键

