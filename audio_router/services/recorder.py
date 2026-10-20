"""
本地 WAV 录音

设计要点
--------
音频回调运行在 PortAudio 的实时线程里，**绝对不能在里面做磁盘 IO**：
一次卡顿就会导致整个输出流欠载（听感是咔哒/断续）。
所以这里采用「回调只入队，独立线程写盘」的生产者-消费者结构：

    _input_callback ──feed()──► queue(maxsize=N) ──► writer thread ──► .wav

队列满了就丢帧并计数（dropped），宁可录音有缺口，也不能拖垮实时路由；
UI 上会把丢帧数显示出来，用户能立刻发现磁盘太慢。

录音内容是「经过麦克风增益 / 噪声门处理、即将送往扬声器的那一路信号」，
不含输出端的移频与陷波（那两道是给扬声器回授用的，录进文件只会让声音变怪）。
"""
import os
import queue
import threading
import time
import wave
from datetime import datetime
from typing import Optional

import numpy as np

# 队列深度：每块约 10~20ms，128 块 ≈ 1.3~2.6 秒的缓冲
# 足够吸收磁盘瞬时卡顿（杀毒扫描、机械盘寻道），又不会积压太久
QUEUE_BLOCKS = 128


def default_recordings_dir() -> str:
    """录音默认目录：%USERPROFILE%\\Music\\BTSPK 录音

    放在「音乐」而不是 %APPDATA%，是因为录音是用户要拿走的产物，
    AppData 是隐藏目录，找起来很费劲。
    """
    music = os.path.join(os.path.expanduser("~"), "Music")
    if not os.path.isdir(music):
        # 某些精简系统没有 Music 文件夹，退回到用户目录
        music = os.path.expanduser("~")
    path = os.path.join(music, "BTSPK 录音")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        path = os.path.expanduser("~")
    return path


class WavRecorder:
    """把 float32 音频块写成 16bit PCM WAV"""

    def __init__(self, directory: str, samplerate: int, channels: int,
                 on_error=None):
        self._dir = directory
        self._samplerate = max(8000, int(samplerate))
        self._channels = max(1, int(channels))
        self._on_error = on_error

        self._q: "queue.Queue" = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        self._wave: Optional[wave.Wave_write] = None
        self._path: Optional[str] = None
        self._t0: float = 0.0
        self._elapsed: float = 0.0
        self._frames_written: int = 0
        self._dropped: int = 0
        self._error: Optional[str] = None
        self._closed = False

    # ---------------------------------------------------------- 生命周期

    def start(self) -> str:
        """创建文件并启动写盘线程，返回文件路径。失败抛异常。"""
        os.makedirs(self._dir, exist_ok=True)
        self._path = self._make_path()

        wf = wave.open(self._path, "wb")
        wf.setnchannels(self._channels)
        wf.setsampwidth(2)                 # 16 bit PCM
        wf.setframerate(self._samplerate)
        self._wave = wf

        self._stop_evt.clear()
        self._closed = False
        self._frames_written = 0
        self._dropped = 0
        self._t0 = time.perf_counter()

        self._thread = threading.Thread(target=self._writer_loop,
                                        name="BTSPK-Recorder", daemon=True)
        self._thread.start()
        return self._path

    def _make_path(self) -> str:
        """BTSPK_20261020_101530.wav；同一秒重复启动时加序号避免覆盖"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(self._dir, f"BTSPK_{stamp}")
        path = base + ".wav"
        n = 2
        while os.path.exists(path):
            path = f"{base}_{n}.wav"
            n += 1
        return path

    def stop(self, timeout: float = 5.0) -> Optional[str]:
        """停止录音，等写盘线程把队列写完再返回文件路径"""
        if self._wave is None and self._thread is None:
            return self._path

        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._close_wave()
        self._elapsed = time.perf_counter() - self._t0 if self._t0 else 0.0
        return self._path

    def _close_wave(self):
        if self._wave is not None and not self._closed:
            try:
                self._wave.close()
            except Exception:
                pass
            self._closed = True
        self._wave = None

    # ---------------------------------------------------------- 生产者

    def feed(self, block: np.ndarray):
        """音频回调里调用：只入队，绝不阻塞、绝不写盘。"""
        if self._closed or block is None or len(block) == 0:
            return
        try:
            # 必须拷贝：PortAudio 会复用 indata 缓冲区
            self._q.put_nowait(np.array(block, dtype=np.float32, copy=True))
        except queue.Full:
            self._dropped += 1
        except Exception:
            self._dropped += 1

    # ---------------------------------------------------------- 消费者

    def _writer_loop(self):
        buf = []
        buf_frames = 0
        # 攒到约 100ms 再一次写盘，减少系统调用次数
        flush_frames = max(1024, int(self._samplerate * 0.1))

        while True:
            stopping = self._stop_evt.is_set()
            try:
                block = self._q.get(timeout=0.1)
                buf.append(block)
                buf_frames += len(block)
            except queue.Empty:
                block = None

            if buf_frames >= flush_frames or (stopping and block is None):
                self._flush(buf)
                buf = []
                buf_frames = 0

            if stopping and self._q.empty() and block is None:
                break

        self._close_wave()
        self._elapsed = time.perf_counter() - self._t0 if self._t0 else 0.0

    def _flush(self, buf):
        if not buf or self._wave is None:
            return
        try:
            data = np.concatenate(buf, axis=0) if len(buf) > 1 else buf[0]
            if data.ndim == 1:
                data = data.reshape(-1, 1)
            # float32 [-1,1] -> int16，乘 32767 并四舍五入，避免截断噪声
            pcm = np.clip(data, -1.0, 1.0) * 32767.0
            pcm = np.rint(pcm).astype(np.int16)
            self._wave.writeframes(pcm.tobytes())
            self._frames_written += len(pcm)
            self._elapsed = time.perf_counter() - self._t0
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            if self._on_error:
                try:
                    self._on_error(f"录音写入失败：{self._error}")
                except Exception:
                    pass
            # 写盘失败后不再继续（避免刷屏），但保留已写部分
            self._close_wave()

    # ---------------------------------------------------------- 状态

    @property
    def path(self) -> Optional[str]:
        return self._path

    @property
    def elapsed(self) -> float:
        """已录制时长（秒）

        按「已写入的帧数 / 采样率」计算，而不是墙钟时间：
        两者在实时场景下几乎一致，但以帧数为准更能反映文件里真正有多少音频
        （音频回调被系统调度延迟时，墙钟会虚高）。
        """
        if self._samplerate > 0 and self._frames_written > 0:
            return self._frames_written / float(self._samplerate)
        return self._elapsed

    @property
    def dropped(self) -> int:
        """因磁盘跟不上而丢弃的音频块数"""
        return self._dropped

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def file_size(self) -> int:
        """当前文件字节数（用于 UI 显示实时大小）"""
        if not self._path:
            return 0
        try:
            return os.path.getsize(self._path)
        except Exception:
            return 0
