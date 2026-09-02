#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alert.py — 报警：弹窗(toast/tkinter) + 三层音频通知 + 写日志

三层音频架构（本次升级核心）：
  Layer 1  信号层 (Signal)    : 现有静态 MP3（critical/warning/info/big_oi...），"夺命响铃"——抓注意力。
  Layer 2  信息层 (Narrator)  : edge-tts（云端神经语音）把动态告警文本念出来，需联网。
  Layer 3  生存层 (Lifeboat)  : 本地 pyttsx3 (Windows SAPI5) 兜底。当 edge-tts 失败（超时/DNS/断网）
                                时立即无缝回退，用本地引擎念同一段文本，绝不静默。

资源生命周期（"生成-读-烧" Temp-Generate-Burn）：
  - edge-tts 每次请求用 tempfile 生成唯一临时 mp3（tempfile.NamedTemporaryFile(suffix=".mp3")）。
  - SequentialAudioWorker 按 FIFO 顺序播放；某段播放**结束**后立刻 os.remove() 该临时文件（"读后即焚"）。
  - 目标：磁盘近零占用，文件只在播放期间存在。

运行约束（严格合规）：
  Rule 1 串行音频队列（防打断）：高频告警不得打断/跳过/截断正在播放的音频。用 thread-safe 的
         queue.Queue + 专用 Playback Worker 线程，逐条听完再放下一句，市场再震荡也不丢字。
  Rule 2 原子执行：每个告警事件原子地 (1) 触发 MP3 -> (2) 尝试 Edge-TTS -> (3) 失败回退 pyttsx3 ->
         (4) 把得到的音频路径入队播放。
  Rule 3 错误隔离：edge-tts 请求的错误（404/超时/DNS）绝不拖垮 AlertDispatcher，只触发 Layer 3 回退。

对外契约（不改变，watch.py / risk_board.py 零改动）：
  AlertManager(settings)  -> 兼容别名，= AlertDispatcher
  .fire(level, title, msg, key=None, sound=None)   # 进告警；sound 选 Layer1 静态音键
  .flush_digest()                              # 周期把 warning/info 合并成摘要邮件
"""

import os
import sys
import time
import queue
import glob
import threading
import tempfile
import subprocess
import traceback
from typing import Optional

# ============ 延迟 import，避免无 GUI/无音频环境直接崩 ============
try:
    import pygame
    _PYGAME_OK = True
except Exception:
    _PYGAME_OK = False

try:
    import tkinter as tk
    from tkinter import messagebox
    _TK_OK = True
except Exception:
    _TK_OK = False

# 系统通知（Windows 11 Action Center toast）：常驻右下角、可手动关闭、不阻塞主循环。
try:
    from win11toast import toast as _win_toast
    _TOAST_OK = True
except Exception:
    _TOAST_OK = False

# win11toast 默认把 on_click/on_dismissed/on_failed 设为 print，会在通知关闭时
# 往 stdout 打印 (ToastDismissalReason.TIMED_OUT: 2,) 刷屏 run.bat 终端。传空函数关掉。
def _noop(*a, **k):
    return None

# ===== Layer 2 / Layer 3 语音引擎（延迟 import + 各自容错）=====
try:
    import edge_tts
    _EDGE_OK = True
except Exception:
    _EDGE_OK = False

try:
    import pyttsx3
    _PYTTSX3_OK = True
except Exception:
    _PYTTSX3_OK = False

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 延迟 import 推送模块（与交易所接口完全隔离）
try:
    from notify import NotifierHub
    _NOTIFY_OK = True
except Exception:
    _NOTIFY_OK = False


# =============================================================================
# SequentialAudioWorker —— 串行音频队列 + "读后即焚" 清理
# =============================================================================
class SequentialAudioWorker:
    """
    专用播放线程：消费一个 thread-safe 的 FIFO 队列，逐条播放，绝不互相打断。

    队列元素为 dict：
      {
        "kind": "mp3_static" | "mp3_temp",   # mp3_static=Layer1 永久文件；mp3_temp=Layer2/3 临时文件
        "path": <音频文件路径>,
        "burn": True/False,                  # True=播放结束后 os.remove()（临时文件用）
        "label": <用于日志的简短标识>,
      }

    设计要点：
      - 单线程顺序消费 -> 天然满足 Rule 1（防打断/防截断）。
      - 临时文件在 "播放结束（无论正常/异常）" 后统一清理 -> 满足 Temp-Generate-Burn。
      - 单个任务异常被吞掉，绝不终止 worker 线程 -> 满足 Rule 3（错误隔离）。
    """

    def __init__(self, name: str = "audio-worker", tts_cfg: Optional[dict] = None, popup_coordinator=None):
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._stop = threading.Event()
        self._started = False
        self._lock = threading.Lock()
        self._popup_coordinator = popup_coordinator  # 语音任务据此同步弹窗
        # TTS 参数（合成在 worker 线程内进行）
        t = tts_cfg or {}
        self._tts_voice = t.get("voice", "zh-CN-XiaoxiaoNeural")
        self._tts_rate = t.get("rate", "+0%")
        self._tts_timeout = _num(t.get("timeout_seconds", 5.0))
        self._tts_fallback = bool(t.get("fallback_pyttsx3", True))
        self._tts_pyttsx3_rate = int(_num(t.get("pyttsx3_rate", 200)))

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        """停止 worker：先放一个毒丸，等线程把队列里剩余任务(含其临时文件)都消费/清理完再退出。
        不丢弃未处理的 speech 任务 —— 否则正在合成的临时文件会泄漏。drain 模式确保读后即焚完整。"""
        self._stop.set()
        try:
            self._queue.put_nowait({"kind": "_poison", "path": None, "burn": False, "label": "stop"})
        except Exception:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)
        # 极度兜底：若线程真的没退出(超时)，手动清掉队列里残留的临时文件，避免磁盘泄漏
        try:
            while not self._queue.empty():
                it = self._queue.get_nowait()
                if it.get("kind") == "speech":
                    # speech 任务尚未合成，无临时文件，跳过
                    continue
                if it.get("burn") and it.get("path"):
                    try:
                        os.remove(it["path"])
                    except Exception:
                        pass
        except Exception:
            pass

    def enqueue(self, item: dict):
        """入队一个播放任务。队列满/异常时静默丢弃（不影响告警主流程）。"""
        if not _PYGAME_OK:
            # 没 pygame 就没有音频后端；但临时文件若被生成了也要清掉，避免磁盘残留。
            self._maybe_burn_on_enqueue_fail(item)
            return
        try:
            self._queue.put_nowait(item)
        except Exception:
            self._maybe_burn_on_enqueue_fail(item)

    @staticmethod
    def _maybe_burn_on_enqueue_fail(item: dict):
        if item.get("burn") and item.get("path") and item.get("kind") == "mp3_temp":
            try:
                os.remove(item["path"])
            except Exception:
                pass

    def _run(self):
        """worker 主循环：逐条取出 -> 处理 -> 播放 -> 读后即焚。异常全部隔离。
        本线程独占所有音频操作（含 Layer2/3 语音合成），因为 Windows SAPI(pyttsx3) 的 COM
        对象线程绑定——只能在与创建它的同一线程里 runAndWait，否则会死锁。把合成放进 worker
        线程，既解决了死锁，又天然保证严格串行（Rule 1 防打断）。"""
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item.get("kind") == "_poison":
                break
            try:
                self._handle_one(item)
            except Exception:
                # Rule 3：单条失败绝不终止 worker
                pass
            finally:
                try:
                    self._queue.task_done()
                except Exception:
                    pass

    def _handle_one(self, item: dict):
        """处理一个任务：若是 'speech' 先在 worker 线程内完成 L2->L3 合成得到音频路径，
        再播放，最后读后即焚。若是直接音频文件，直接播放+烧。
        burn=True 的临时文件无论播放成功/异常都保证被删除（finally），绝不泄漏。"""
        kind = item.get("kind")
        if kind == "speech":
            # 弹窗与语音同步：进入 speech 任务时立刻让弹窗协调器弹出（toast 近瞬时），
            # 随后才合成+播放本段语音 -> 语音与窗口同刻出现（修"语音出了窗口还没出"）。
            pop = item.get("popup")
            if pop and self._popup_coordinator is not None:
                try:
                    self._popup_coordinator.show(pop.get("level", "info"),
                                                 pop.get("title", ""), pop.get("msg", ""))
                except Exception:
                    pass
            path = self._synthesize(item.get("text", ""), item.get("level", "info"))
            burn = True
            if not path:
                return  # 两层都失败：静默放弃该次语音（Layer1+日志+弹窗已保留信息）
        else:
            path = item.get("path")
            burn = item.get("burn", False)

        # ---- 播放 + 读后即焚（统一路径，burn 用 finally 兜底）----
        try:
            if path and os.path.exists(path):
                self._play_one(path)
        finally:
            if burn and path:
                # SDL/pygame 在某些环境播放结束后仍短暂持有文件句柄，
                # 立即 os.remove 会触发 PermissionError；重试几次 + 短延迟兜底，确保零残留。
                for _ in range(10):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                        break
                    except (PermissionError, OSError):
                        time.sleep(0.1)
                # 终极兜底：若仍删不掉（句柄被 SDL 延迟释放），丢给后台延迟清扫线程
                if os.path.exists(path):
                    _deferred_burn(path)

    def _synthesize(self, text: str, level: str) -> Optional[str]:
        """在 worker 线程内执行 Layer2(edge-tts) -> Layer3(pyttsx3) 合成，返回临时音频路径。"""
        # Layer 2：云端神经语音
        if _EDGE_OK:
            try:
                tmp = synthesize_edge_tts(text, voice=self._tts_voice,
                                          timeout=self._tts_timeout, rate=self._tts_rate)
                if tmp:
                    return tmp
            except Exception:
                pass
        # Layer 3：本地 SAPI5 兜底（子进程 + 硬超时，绝不阻塞 worker）
        if self._tts_fallback and _PYTTSX3_OK:
            try:
                return synthesize_pyttsx3_to_file(
                    text, rate=self._tts_pyttsx3_rate, volume=1.0,
                    timeout=self._tts_timeout + 5.0,   # 比 edge 超时更宽，给本地合成留足
                )
            except Exception:
                pass
        return None

    def _play_one(self, path: str):
        if not _PYGAME_OK:
            return
        # 硬上限：单段音频最长播放 N 秒，防止坏文件/异常导致 get_busy() 死循环卡住整条队列
        # （Rule 1 防打断 + 零磁盘残留的兜底保险）。告警音频一般 <10s，30s 足够宽。
        MAX_PLAY_SECONDS = 30.0
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            start = time.time()
            while pygame.mixer.music.get_busy():
                if self._stop.is_set() or (time.time() - start) > MAX_PLAY_SECONDS:
                    try:
                        pygame.mixer.music.stop()
                    except Exception:
                        pass
                    break
                time.sleep(0.05)
        except Exception:
            # 播放异常（文件损坏/解码失败）-> 上层负责清理，不向上抛
            raise
        finally:
            # SDL_mixer 播放结束后仍持有文件句柄，必须显式 unload() 释放，
            # 否则后续 os.remove 在 Windows 上会静默失败（句柄未释放 = 磁盘残留）。
            try:
                pygame.mixer.music.unload()
            except Exception:
                pass


# =============================================================================
# 语音合成 helper（Layer 2 / Layer 3），与 AlertDispatcher 解耦、可单测
# =============================================================================
def synthesize_edge_tts(text: str, voice: str, timeout: float, rate: str = "+0%") -> Optional[str]:
    """
    Layer 2：用 edge-tts 把文本合成为临时 mp3，返回临时文件路径；失败返回 None。
    生成的临时文件由调用方负责"读后即焚"（worker 的 burn=True）。
    """
    if not _EDGE_OK:
        return None
    if not text or not text.strip():
        return None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".mp3", prefix="tts_edge_")
        os.close(fd)  # 只借名字，给 edge-tts 写
        try:
            comm = edge_tts.Communicate(text=text, voice=voice, rate=rate)
            # 用事件循环跑异步；超时由 asyncio.wait_for 兜住
            import asyncio
            async def _save():
                with open(tmp, "wb") as f:
                    async for chunk in comm.stream():
                        if chunk["type"] == "audio":
                            f.write(chunk["data"])
            asyncio.run(asyncio.wait_for(_save(), timeout=timeout))
        except Exception:
            # 任何失败（网络/DNS/404/超时）-> 清理半成品临时文件，返回 None
            try:
                os.remove(tmp)
            except Exception:
                pass
            return None
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            return tmp
        try:
            os.remove(tmp)
        except Exception:
            pass
        return None
    except Exception:
        return None


# -----------------------------------------------------------------------------

def _synthesize_pyttsx3_subprocess(text: str, rate: int, volume: float, out_path: str, timeout: float) -> bool:
    """在独立子进程里跑 pyttsx3 合成，硬超时兜底。
    原因：pyttsx3 的 SAPI runAndWait 在主进程线程里偶发阻塞（COM 死锁/挂起），
    会卡住我们的音频 worker 线程乃至泄漏临时文件。丢到子进程 + timeout，
    既能彻底隔离 COM 风险，又能保证"合成超时即放弃"（绝不阻塞告警队列）。"""
    code = (
        "import sys, os\n"
        "try:\n"
        "    import pyttsx3\n"
        "    eng = pyttsx3.init()\n"
        "    vs = eng.getProperty('voices') or []\n"
        "    for v in vs:\n"
        "        vid = (v.id or '').lower()\n"
        "        if 'chinese' in vid or 'zh' in vid or 'china' in vid:\n"
        "            eng.setProperty('voice', v.id); break\n"
        f"    eng.setProperty('rate', int({int(rate)}))\n"
        f"    eng.setProperty('volume', float({float(volume)}))\n"
        f"    eng.save_to_file({text!r}, {out_path!r})\n"
        "    eng.runAndWait()\n"
        "    sys.exit(0 if os.path.exists(%r) and os.path.getsize(%r) > 0 else 2)\n"
        "except Exception as e:\n"
        "    sys.exit(3)\n"
    ) % (out_path, out_path)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except Exception:
        # 超时 / 子进程异常 -> 视为失败，返回 False（触发"放弃该次语音"，不阻塞）
        try:
            proc.kill()
        except Exception:
            pass
        return False


def synthesize_pyttsx3_to_file(text: str, rate: int = 200, volume: float = 1.0,
                               timeout: float = 10.0) -> Optional[str]:
    """
    Layer 3：用本地 pyttsx3 (SAPI5) 把文本合成为临时 .wav 文件，返回路径；失败返回 None。
    之所以落临时文件而不是直接 speak，是为了让所有音频（Layer1 MP3 / Layer2 / Layer3）都走
    同一个 FIFO 队列 -> 严格不互相打断（Rule 1），且统一"读后即焚"（临时 wav 播完即删）。
    无网络依赖，是断网时的生存兜底。合成在子进程 + 硬超时中进行，绝不阻塞音频 worker。
    """
    if not text or not text.strip():
        return None
    if not _PYTTSX3_OK:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="tts_py_")
    os.close(fd)
    try:
        ok = _synthesize_pyttsx3_subprocess(text, rate, volume, tmp, timeout)
    except Exception:
        ok = False
    if ok and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        return tmp
    try:
        os.remove(tmp)
    except Exception:
        pass
    return None


# =============================================================================
# PopupCoordinator —— 弹窗协调器（解决"上一个弹窗模态卡住下一个"）
# =============================================================================
class PopupCoordinator:
    """
    统一管理两类弹窗：
      - toast：Windows 系统通知（非模态、常驻右下角、可手动关、不阻塞，首选）。
      - tkinter modal：toast 失败时的兜底；用一个持久 Tk 窗口 + queue，
        在"唯一 GUI 线程"里串行弹出，避免多条告警各自建 Tk() 互相模态阻塞。
    关键契约：show() 永远是"近瞬时、绝不阻塞调用线程"——所以语音和弹窗能同步出现，
    且上一个没关也不会卡住下一个（toast 天然；modal 进队列串排）。
    """
    def __init__(self, popup_enabled: bool = True):
        self._enabled = popup_enabled
        self._use_toast = _TOAST_OK
        self._lock = threading.Lock()
        # tkinter modal 队列 + 守护线程
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._tk_thread = None
        self._tk_root = None
        self._tk_running = threading.Event()
        if self._enabled and not self._use_toast and _TK_OK:
            self._start_tk_loop()

    def _start_tk_loop(self):
        def _loop():
            try:
                root = tk.Tk()
                root.withdraw()
                self._tk_root = root
                self._tk_running.set()
            except Exception:
                self._tk_running.set()
                return
            while self._tk_running.is_set():
                try:
                    item = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    break
                try:
                    lvl = item.get("level", "info")
                    if lvl == "critical":
                        messagebox.showerror(item.get("title", ""), item.get("msg", ""))
                    elif lvl == "warning":
                        messagebox.showwarning(item.get("title", ""), item.get("msg", ""))
                    else:
                        messagebox.showinfo(item.get("title", ""), item.get("msg", ""))
                except Exception:
                    pass
                try:
                    self._q.task_done()
                except Exception:
                    pass
            try:
                self._tk_root.destroy()
            except Exception:
                pass
        t = threading.Thread(target=_loop, name="popup-tk-loop", daemon=True)
        t.start()

    def show(self, level: str, title: str, msg: str):
        if not self._enabled:
            return
        if self._use_toast:
            try:
                _win_toast(title, msg, app_id="GateRiskWatcher 风险哨兵",
                           duration="long",
                           on_click=_noop, on_dismissed=_noop, on_failed=_noop)
                return
            except Exception:
                # toast 偶发失败 -> 落 tkinter 队列（若已启动）
                if _TK_OK and self._tk_thread is None:
                    self._start_tk_loop()
        if _TK_OK:
            try:
                self._q.put_nowait({"level": level, "title": title, "msg": msg})
            except Exception:
                pass

    def stop(self):
        self._tk_running.clear()
        try:
            self._q.put_nowait(None)
        except Exception:
            pass


# =============================================================================
# AlertDispatcher —— 升级后的告警调度器（原 AlertManager 改名 + 三层音频）
# =============================================================================
class AlertDispatcher:
    def __init__(self, settings: dict):
        self.settings = settings or {}
        self.cfg = self.settings.get("alert") or {}
        self.sounds_cfg = self.settings.get("alert_sounds") or {}
        self.tts_cfg = self.settings.get("tts") or {}
        self.suppress_seconds = _num(self.cfg.get("suppress_seconds", 300))

        # ---- TTS 开关与参数（从 settings.tts 读取，缺省行为保守）----
        self.tts_enabled = bool(self.tts_cfg.get("enabled", False))   # 默认关，用户开
        self.tts_layers = self.tts_cfg.get("layers", ["warning", "critical", "info"])  # 默认三层都念

        self._lock = threading.Lock()
        self._last_fire: dict[str, float] = {}

        # ---- 弹窗协调器（toast 首选 / tkinter 兜底串行）----
        self.popup = PopupCoordinator(popup_enabled=bool(self.cfg.get("popup", True)))

        # ---- pygame 音频后端 ----
        if _PYGAME_OK:
            try:
                pygame.mixer.init()
            except Exception:
                pass

        # ---- 串行音频 worker（TTS 参数交给 worker，合成在 worker 线程内做；
        #      语音任务由 worker 在播放前同步触发弹窗协调器，使语音与窗口同刻出现）----
        self.worker = SequentialAudioWorker(name="alert-audio-worker",
                                            tts_cfg=self.tts_cfg, popup_coordinator=self.popup)
        self.worker.start()

        # ---- 离线推送（邮件/Telegram 预留）：与交易所接口完全隔离，失败不影响本地告警 ----
        self.notifier = None
        if _NOTIFY_OK:
            try:
                push_cfg = self._load_push_cfg()
                if push_cfg:
                    ec = _num(self.settings.get("notify", {}).get("email_cooldown_seconds", 60))
                    cc = _num(self.settings.get("notify", {}).get("critical_cooldown_seconds", 300))
                    dg = _num(self.settings.get("notify", {}).get("digest_minutes", 15))
                    self.notifier = NotifierHub(push_cfg, email_cooldown=ec, critical_cooldown=cc)
                    self._digest_minutes = dg
                    self._last_digest = time.time()
            except Exception:
                self.notifier = None

        # 日志
        self.log_path = os.path.join(ROOT, "logs", "alerts.log")
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

    # ---------- 推送配置（只读 secrets/push.env，绝不读交易 key）----------
    @staticmethod
    def _load_push_cfg() -> dict:
        try:
            from notify import _load_push_cfg as _lp
            return _lp()
        except Exception:
            return {}

    def flush_digest(self):
        """由 watch 主循环周期调用，把 warning/info 合并成摘要邮件发出。"""
        if self.notifier is None:
            return
        now = time.time()
        if now - self._last_digest < self._digest_minutes * 60.0:
            return
        self._last_digest = now
        try:
            self.notifier.flush_digest()
        except Exception:
            pass

    # ---------- 路径解析 ----------
    def _sound_path(self, sound: str) -> Optional[str]:
        # sound 既可以是 level 名(critical/warning/info)，也可以是 alert_sounds 里的自定义键(如 big_oi)
        rel = self.sounds_cfg.get(sound)
        if not rel:
            return None
        p = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
        return p if os.path.exists(p) else None

    # ---------- 原子告警入口（Rule 2）----------
    def fire(self, level: str, title: str, msg: str, key: Optional[str] = None,
             sound: Optional[str] = None):
        """
        level: critical/warning/info
        key:   同一告警去重键（如 'liq:BTC_USDT'），用于抑制期判断。
               critical 忽略抑制；warning/info 在 suppress_seconds 内不重复。
        sound: 可选，覆盖 level 默认音效（如大币异动传 'big_oi'/'big_oi_trend'，
               对应 settings.alert_sounds 里的自定义键；缺失文件则只弹窗不发声）。
        """
        # ---- 抑制期判定（线程安全）----
        with self._lock:
            now = time.time()
            if key and key.startswith("err-auth:"):
                last = self._last_fire.get(key)
                if last and (now - last) < 21600:  # 6 小时
                    return
                self._last_fire[key] = now
            elif level != "critical" and key:
                last = self._last_fire.get(key)
                if last and (now - last) < self.suppress_seconds:
                    return
                self._last_fire[key] = now

        # ---- (always) 日志 + Layer1 即时弹窗(toast) + Layer1 静态音 ----
        self._log(level, title, msg)
        # L1 弹窗：toast 首选，近瞬时、不阻塞、不排队在前面的语音后；
        # 这样即使当前正在念上一句，新告警的窗口也立刻出现（修"窗口被上一条占用"）。
        self.popup.show(level, title, msg)
        self._fire_layer1(sound if sound else level)

        # ---- (conditional) Layer2/3 语音念文本（含同步弹窗，见 worker._handle_one）----
        self._fire_speech_layers(level, f"{title}。{msg}", popup={"level": level, "title": title, "msg": msg})

        # ---- 离线推送（与交易所接口完全隔离，失败静默）----
        if self.notifier is not None:
            try:
                self.notifier.send_immediate(level, title, msg)
            except Exception:
                pass

    # ---------- Layer 1：静态 MP3（信号层）----------
    def _fire_layer1(self, sound_key: str):
        if not self.cfg.get("sound", True):
            return
        if not _PYGAME_OK:
            return
        p = self._sound_path(sound_key)
        if not p:
            return
        # 入队为永久文件（burn=False），worker 只播放、不删除
        self.worker.enqueue({
            "kind": "mp3_static",
            "path": p,
            "burn": False,
            "label": f"layer1:{sound_key}",
        })

    # ---------- Layer 2 + Layer 3：TTS 念文本（信息层 -> 生存层回退）----------
    def _fire_speech_layers(self, level: str, text: str, popup: Optional[dict] = None):
        if not self.tts_enabled:
            return
        if level not in self.tts_layers:
            return
        if not text or not text.strip():
            return
        # 只入队一个 'speech' 任务；合成(L2->L3)与播放都在 worker 线程内串行完成。
        # worker 在播放本段语音前会调弹窗协调器同步弹出（见 _handle_one），使语音与窗口同刻出现。
        # 既不会阻塞来自 watch 的告警线程，又因 pyttsx3 的 COM 线程绑定而必须在 worker 线程合成，
        # 同时天然保证严格串行（Rule 1）与读后即焚（零磁盘残留）。异常在 worker 内隔离（Rule 3）。
        self.worker.enqueue({
            "kind": "speech",
            "text": text,
            "level": level,
            "popup": popup,                 # 语音播放前由 worker 同步弹窗
            "label": f"speech:{level}",
        })

    def _log(self, level: str, title: str, msg: str):
        if not self.cfg.get("log", True):
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}][{level.upper()}]{title} | {msg}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


# 向后兼容别名：watch.py / risk_board.py 仍 import AlertManager，无需改动。
AlertManager = AlertDispatcher


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# -----------------------------------------------------------------------------
# 临时文件终极兜底清扫：极少数情况下 SDL 句柄延迟释放，即时 os.remove 失败；
# 这里用一条"晚点再删"的守护线程 + 进程退出时的 atexit 清扫，确保零磁盘残留。
# -----------------------------------------------------------------------------
def _deferred_burn(path: str, delay: float = 1.0, tries: int = 30):
    """后台守护线程：延迟 delay 秒后重试删除临时文件，最多 tries 次。"""
    def _run():
        for _ in range(tries):
            time.sleep(delay)
            try:
                if os.path.exists(path):
                    os.remove(path)
                return
            except Exception:
                pass
    t = threading.Thread(target=_run, name="audio-burn-deferred", daemon=True)
    t.start()


def _sweep_orphan_tempfiles():
    """进程退出时清扫本模块可能遗留在 %TEMP% 的 tts_/burn_ 临时文件。"""
    try:
        import tempfile as _tf
        td = _tf.gettempdir()
        for name in ("tts_edge_", "tts_py_", "burn_"):
            for f in glob.glob(os.path.join(td, name + "*")):
                try:
                    os.remove(f)
                except Exception:
                    pass
    except Exception:
        pass


import atexit as _atexit
_atexit.register(_sweep_orphan_tempfiles)
