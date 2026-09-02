# GateRiskWatcher — Gate.io USDT Perpetuals Read-Only Risk Sentinel

> Uses **read-only** API keys only; placing/closing/adjusting orders is physically impossible.
> All alerts, toasts, and sounds are generated locally.

---

## 0. Calibrated Against Real Accounts (Important for Maintainers)

This project has been tested against live read-only keys. The following facts were observed and encoded in code:

- **Account mode = mixed (isolated + cross)**: In the tested account, SUI is isolated and DOGE is cross. `get_account()` returns `margin_mode = 0` (cross indicator). The system infers per-position mode via `account_safety()` by comparing `isolated_position_margin` against `cross_margin_balance`, rather than trusting the raw `margin_mode` field. Capital is spread across both; both must be monitored.
- **Long/short direction is determined by `mode`** (`dual_long` / `dual_short`). **Do not judge from `size` sign alone** — under dual mode, `size` is always positive.
- **Liquidation price is provided directly by Gate** as `liq_price`; no formula risk.
- **`funding_time` is missing from ticker**; funding countdown is computed from Gate's fixed UTC settlement schedule: `00:00 / 08:00 / 16:00`.
- **ADL auto-deleveraging rank** `adl_ranking`: Gate API returns an integer from 0–4 (tested account DOGE position returned `4`).
  ⚠️ **Calibration note (conservative)**: This field uses a different definition from the "5-bar indicator" shown in the Gate app. The app lights bars by long/short position segment visualization, while `adl_ranking` is an independent API tier — they may not match (tested API=4 while app showed only 2 bars). **The system no longer alerts on this field**; it is displayed raw in the periodic position print for your own cross-reference with the app. Before high-leverage positions, use the app indicator as the primary danger gauge.
- Account-level safety buffer uses `total` (total margin) and `maintenance_margin` (total maintenance margin): available margin ratio = `(total - maintenance_margin) / total`.

These assumptions are concentrated in `risk.py` (`account_safety` / `funding_countdown` / `position_panel` / `adl_risk`). When changing account types (e.g. to cross-only), review here first.

---

## 1. What It Does (4 Layers)

- **L1 Lifesaving**: Real-time liquidation distance (Gate's `liq_price`), account-level available margin ratio, funding countdown + direction, ADL auto-deleveraging rank display.
- **L2 Situational**: Unified position panel (net exposure / unrealized P&L / margin occupied), mark-price vs index-price deviation, **Open Interest (OI) anomaly three-layer detection** (instantaneous MoM + large-cap exclusive + trend cumulative), **OI×Price four-category semantic signal** (deleveraging / short squeeze / inflow / suppression), funding rate absolute anomaly, spike / flash-pump detection.
- **L3 Review**: Daily P&L ledger (realized + unrealized + net funding expense), equity curve max drawdown, liquidation history (stored in `data/`).
- **L4 Decision**: Scenario simulation (given drawdown, calculate liquidation price / loss / margin ratio); output to `data/snapshot.json` for you or Hermes Bot to read.

---

## 2. Security Boundaries (Must Read)

- **GET-only**: Only `list_*` / `get_*` read methods exist. `gate_client.py`'s `_READONLY_METHODS` whitelist rejects any write methods.
- **Keys not in repo, not in plaintext**: Run `enter_keys.bat` for interactive input, stored in `secrets/.env` (gitignored, permission-locked to owner only). ★ Do not commit.
- **Key expiry auto-alert**: When Gate returns 401, the system immediately fires a Windows toast + sound telling you **which account expired**. Double-click `enter_keys.bat` to re-enter, close the old `run.bat` window, reopen it. **No countdown configuration needed.**
- **All log/output masks keys** as `****`.
- **It can never place/close/adjust leverage for you** — it only shouts about risk.

---

## 3. First Use

```bat
cd "F:\Program Files\Crypto_Trading_Bot"

REM 1) Create isolated environment and install deps (once only)
uv venv .venv --python 3.11
uv pip install -r requirements.txt

REM 2) Enter your read-only keys (double-click enter_keys.bat, input in the popup)
REM    Multi-account: account_2 maps to GATE_API_KEY_2 / GATE_API_SECRET_2

REM 3) View live risk dashboard (one-shot, plain language)
.venv\Scripts\python.exe src\risk_board.py
REM    ↑ Or double-click "GateRiskWatcher 风险看板.bat" in project root. Press any key to close.
REM    ↑ On startup, UTC-daily ledger backfill runs automatically (max 7 days per run, resumes across reboots up to 180 days). May take a few seconds.
REM    ↑ Backfill and live dashboard data fetch in parallel: 🔴 red progress bar (e.g. █░░ 1/7 days 2026-02-27), turns 🟢 green when complete.

REM 4) Persistent monitoring (stops when window closes)
run.bat
```

> **Note**: In a plain `cmd` double-click environment, `uv` may not be on PATH. If you see "uv is not recognized", run `uv venv` + `uv pip install` once in the Hermes terminal first; after that, double-click works.

Multi-account: `account_2` in `config/settings.yaml` maps to `GATE_API_KEY_2` / `GATE_API_SECRET_2` in `.env`. The system **automatically decides how many accounts to monitor** based on how many keys actually exist.

---

## 4. Configuring `config/settings.yaml`

- `poll_account_seconds` / `poll_market_seconds`: Polling interval (seconds). Don't go below 5s to avoid rate limits.
- `risk.liq_distance_critical_pct`: Liquidation distance below this → **critical** alert (default 2%).
- `risk.liq_distance_warning_pct`: Below this → warning (default 5%).
- `risk.margin_ratio_warning_pct`: Account-level available margin ratio below this → warning (default 30%).
- `funding.settle_warning_seconds`: Seconds before funding settlement where you are the payer → warning.
- `anomaly.oi_change_pct`: Small-cap OI (20s) MoM change above this → warning (default 8%).
- `anomaly.big_oi_threshold`: OI contracts above this count as "large-cap asset" (default 100M contracts ≈ $100M notional). Dynamically determined; no manual list needed. Triggers ⚠ + dedicated sound + whale-level description.
- `anomaly.big_oi_warn_pct`: Large-cap 20s MoM threshold (default 1.5%, more sensitive than small caps' 8%).
- `anomaly.oi_trend_window_minutes`: Trend-layer local baseline window (default 60 minutes).
- `anomaly.oi_trend_pct`: Cumulative change threshold for large and small caps (default 30%). Cumulative OI change within the window above this → "OI trend anomaly" alert (catches slow pumps/dumps missed by the instantaneous layer).
- `anomaly.oi_signal_oi_pct` / `oi_signal_price_pct`: OI×Price four-category signal thresholds (default OI 5% / price 0.3%; both sides must breach to classify).
- `anomaly.price_spike_pct`: Mark-price MoM spike/pump threshold for whitelisted symbols (default 1.5%).
- `anomaly.funding_abnormal_rate_pct`: Funding rate absolute anomaly threshold (default 0.05%).
- `anomaly.signal_labels`: Chinese label mapping for the four OI×Price categories (deleveraging / short squeeze / inflow / suppression); editable in config.
- `accounts`: Display names for each key.
- `daily_ledger_backfill_days`: Ledger backfill window (default 180). When no ledger exists, backfills from this many days ago through yesterday. Covers the full dashboard cycle.
- `daily_ledger_backfill_per_run_days`: Max days per single run (default 7). Prevents one-shot 180-day overload/timeout; auto-resumes across reboots until current. Non-24/7 users eat 7 days per boot, ~26 runs to fill 180 days.
- Dashboard `GateRiskWatcher 风险看板.bat` triggers backfill on launch (not resident; closes after completion). **Persistent `run.bat` does NOT backfill** (avoids silent background stealing; you see progress). It only handles real-time alerts and snapshots.

---

## 5. Alerts and Your MP3s

Place your own alert tones in `sounds/`:

```
sounds/critical.mp3   # Liquidation imminent (most dangerous)
sounds/warning.mp3    # Funding / deviation / OI / ADL anomaly (small-cap OI also uses this)
sounds/info.mp3       # Daily ledger / informational
sounds/big_oi.mp3          # Large-cap instantaneous anomaly (20s MoM > 1.5%), dedicated tone
sounds/big_oi_trend.mp3    # Large-cap trend anomaly (60min cumulative > 30%), dedicated tone
```

If a file is missing, that level stays silent — Windows toast still fires (resident in the notification center, dismissible) and `logs/alerts.log` still records everything.
Mapping is configured in `config/settings.yaml`'s `alert_sounds` section (`big_oi` / `big_oi_trend` are custom keys; `fire()` supports `sound=` parameter to override the level default tone).

Alert suppression: Same warning/info within `alert.suppress_seconds` (default 300s) does not re-fire; critical is never suppressed.

### Three-Layer Audio System (2026-08-26 upgrade)

The original alerting was "static MP3 ring". Now upgraded to three-layer, serial, self-cleaning audio architecture (implemented in `src/alert.py`'s `AlertDispatcher` + `SequentialAudioWorker`, external interface unchanged):

| Layer | Role | Tech | Trigger |
|-------|------|------|---------|
| **Layer 1 Signal** | Grab attention, loud ring | Static MP3 (`sounds/` — your own) | Every alert (selected by `sound=` key) |
| **Layer 2 Info** | Speak the alert text dynamically | `edge-tts` cloud neural voice (requires Internet) | `settings.tts.enabled=true`; per `tts.layers` |
| **Layer 3 Survival** | Local fallback when Layer 2 fails | `pyttsx3` (Windows SAPI5, no Internet) | Layer 2 timeout / offline / DNS failure; seamless fallback, never silent |

**Guarantees:**
- **Serial / no interruption (Rule 1)**: All audio (L1/2/3) goes through a single thread-safe FIFO queue + dedicated playback thread. Each message plays to completion before the next. High-frequency alerts do not interrupt or skip each other. Single playback has a 30s hard cap; a corrupt file will not deadlock the queue.
- **Temp-Generate-Burn**: L2/L3 generate a unique temp file via `tempfile` each time; deleted immediately after playback ends. Target: near-zero disk footprint.
- **Error isolation (Rule 3)**: Any `edge-tts` exception (404 / timeout / DNS) only triggers Layer 3 fallback. It **never drags down the alert scheduler main flow**.

**Enable voice alerts** (off by default, preserves original behavior): in `config/settings.yaml`'s `tts:` block set `enabled: true`. Optional `voice` (e.g. `zh-CN-XiaoxiaoNeural` female / `zh-CN-YunxiNeural` male), `rate`, `timeout_seconds` (synthesis timeout, fallback to Layer 3 immediately), `fallback_pyttsx3` (recommended `true`). Example:

```yaml
tts:
  enabled: true
  layers: ["critical", "warning", "info"]
  voice: "zh-CN-XiaoxiaoNeural"
  timeout_seconds: 5.0
  fallback_pyttsx3: true
```

Dependencies: `edge-tts`, `pyttsx3` (already in `requirements.txt`; reinstall with `uv pip install --python .venv -r requirements.txt`).

- **API rate-limit auto-backoff**: When Gate returns 429 / `TOO_MANY_REQUEST` / `frequency limit`, the system does not blindly retry — it **doubles that account's polling interval** (caps at `BACKOFF_MAX=120s`) and fires a "Rate limited, slowing down" toast showing the new interval. After cooldown (`BACKOFF_COOLDOWN=60s`), it gradually returns to `poll_account_seconds` and notifies "Polling恢复正常". Auth failure (INVALID_KEY / 401) and rate-limit are **handled separately**: the former triggers key-expiry logic; the latter only backs off without false alerts.

---

## 6. Data on Disk

- `data/snapshot.json`: Full account / margin / position / market snapshot every cycle (all L1/L2 fields).
- `data/oi_baseline.json`: Trend-layer local OI baseline (local-only, survives restarts, no exchange calls). Rewritten once per `oi_trend_window_minutes` with the "window start total market OI" for cumulative change computation.
- `data/ledger/<account>_<date>.json`: Daily P&L ledger (L3).
- `data/equity/<account>.json`: Equity curve (for drawdown).
- `logs/alerts.log`: All alert history.

---

## 7. Directory Structure

```
Crypto_Trading_Bot/
  config/settings.yaml      # Thresholds / symbols / alert mapping (you change this)
  config/.env.example       # Key template (no real values)
  secrets/.env              # Your read-only keys (gitignored, owner-only) ★ DO NOT COMMIT
  src/
    enter_keys.bat          # Key entry (double-click, interactive)
    setup_keys.py           # Key entry script (interactive)
    keystore.py             # Decides account list from keys present (formerly secrets.py, renamed to avoid stdlib conflict) ★ DO NOT COMMIT
    gate_client.py          # Read-only Gate client (multi-account, whitelisted GET)
    risk.py                 # L1-L4 risk calculation (pure functions, calibrated to real accounts)
    alert.py                # Windows toast + MP3 playback + log writer (by level; non-Windows falls back to tkinter)
    ledger.py               # L3 daily ledger / drawdown
    watch.py                # Main loop (serial 4 layers, persistent monitoring; no ledger backfill)
    risk_board.py           # One-shot Chinese risk dashboard (concurrent backfill + live data, red/green progress)
  sounds/                   # Your MP3s (★ DO NOT COMMIT)
  data/                     # Snapshots / ledger (★ DO NOT COMMIT)
  logs/                     # Alert history (★ DO NOT COMMIT)
  run.bat                   # Double-click persistent monitoring
  GateRiskWatcher 风险看板.bat  # Double-click one-shot risk dashboard (= manual `src\risk_board.py`, read-only self-check, press any key to close)
  requirements.txt
  .gitignore
```

---

## 8. Notes for Maintainers / Other Users / AI Models

- The system is self-contained and decoupled from Hermes. The read-only contract lives in `gate_client.py`'s `_READONLY_METHODS` whitelist — no extension may add write methods.
- To integrate with a Hermes risk-advisor Bot: let the Bot read `data/snapshot.json` (purely read-only); no changes to this project are needed.
- When changing exchanges / account types (e.g. cross-only, adding OKX): prioritize `risk.py`'s `account_safety` / `position_panel` / `funding_countdown`, and verify fields with real read-only keys via `risk_board.py`.
- Calibration records are in this document's Section 0.

---

## 🚀 Core Feature Updates (2026-08-25)

### 🛡️ Smart Alert Filtering

To prevent log spam from whole-market funding settlements, the system now filters alerts based on **positions and a user whitelist**.

**Core logic:**
```
Allow_Alert = { Active Positions (size > 0) } ∪ { User_Whitelist }
```

**Configuration:**
If you want to monitor specific symbols even without open positions, edit `config/settings.yaml`:

1. Find the `funding:` block.
2. Add symbols to `monitoring_whitelist: []`.

**Example:**
```yaml
funding:
  settle_warning_seconds: 1800
  monitoring_whitelist: ['SOL_USDT', 'PEPE_USDT', 'WIF_USDT']
  rate_alert_pct: 0.05
```

**Notes:**
- **Strict format**: Symbol names must be quoted with single or double quotes.
- **Separator**: Multiple symbols must be comma-separated with English commas `,`.
- **Auto-disable**: When you close a position and the symbol is not on the whitelist, funding alerts for that symbol stop automatically.

---

## 📊 OI Anomaly Three-Layer Detection + Large-Cap Dedicated Sound (2026-08-25 Enhancement)

### Background
Original OI anomaly used a single "20s MoM > 8%" layer. Testing showed large caps (BTC OI ≈ 587M contracts) would need a $47M instantaneous notional change to breach 8% — effectively **deaf** to large caps; old alerts also lacked current price / 24h change, making them information-sparse. This refactor introduces three layers + richer info.

### Three-Layer OI Detection

| Layer | Window | Large-cap threshold | Small-cap threshold | Catches | Alert title |
|-------|--------|---------------------|---------------------|---------|-------------|
| Instantaneous | 20s MoM | **1.5%** | 8% | Squeeze / forced-liquidation precursors | `⚠ OI Anomaly X_USDT` |
| Trend | 60min cumulative | **30%** (same as small) | 30% | Fast pumps/dumps (covers large-cap slow-move blindspot) | `OI Trend Anomaly X_USDT` |
| OI×Price 4-category | 20s bidirectional | OI 5% + Price 0.3% | same | Semantic signal (deleveraging / short squeeze / inflow / suppression) | `Short squeeze / suppression / ... X_USDT` |

**Large-cap determination**: Dynamic by absolute OI `> big_oi_threshold` (default 100M contracts). Currently 14 of 937 contracts qualify (BTC/ETH/DOGE/SHIB/ICP/XAG/XAU/FIL/SUI/TLM/DOGS/ZK/BICO/SKHYNIX). Keeping 100M threshold means ETH/SHIB also get ⚠ + dedicated sound.

**All OI alerts include**: current price + 24h change %. Trend layer additionally includes window cumulative change %. Large-cap instantaneous alerts add `【Large-cap asset】OI net change ~$X万 in 20s — whale / extreme-market level`.

### Design Tradeoffs (Known Blindspots)

- **Slow 5-day bull run stays silent**: e.g. BTC 64000→80000 (hourly <30% cumulative) does not trigger the trend layer. To catch slow runs, extend `oi_trend_window_minutes` and/or lower `oi_trend_pct` to ~20% (cost: BTC may fire trend anomaly 1–3× daily during volatile weeks).
- Trend layer may fire 1–3× daily for large caps during choppy weeks; raise `oi_trend_pct` or extend the window if annoying.

### Large-Cap Dedicated Sound
`alert_sounds` adds `big_oi` / `big_oi_trend` keys. `fire()` supports `sound=` parameter to override the level default tone — large-cap instantaneous uses `big_oi`, trend uses `big_oi_trend`, small caps still use `warning`. If mp3 is missing, only the toast fires; no error. See Section 5 for details.

### Empirical Verification (real read-only keys)
- Three-layer threshold pure-function validation passed (large-cap instantaneous +1.5% triggers, small-cap +7% no / +9% triggers, trend +30% triggers / +25% no).
- No crashes, no rate-limit violations under persistent live-key operation.
- `data/oi_baseline.json` generates correctly (full-market OI, survives restarts).
- `fire(sound='big_oi')` silently skips when mp3 is missing; does not crash main flow.

### Related files
- `src/watch.py`: Three-layer detection main logic + large-cap ⚠/sound/whale description
- `src/risk.py`: `oi_change_pct` (instantaneous), `oi_trend_pct` (trend cumulative), `scenario_full`, `funding_rate_abnormal`
- `src/signal_oi.py`: OI×Price four-category pure functions (`oi_price_signal` / `interpret_to_alert`)
- `src/alert.py`: `fire()` `sound=` parameter override
- `config/settings.yaml`: All `anomaly.*` thresholds + `alert_sounds` two new keys
