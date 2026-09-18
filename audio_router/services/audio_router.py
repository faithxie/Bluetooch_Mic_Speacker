"""
核心音频路由引擎
实现从蓝牙麦克风采集 -> 处理 -> 播放到扬声器的实时音频路由

采用双通道（独立输入流+输出流）+ 样本级环形缓冲区方案，
支持输入输出采样率不同的场景（如蓝牙麦克风16kHz -> 扬声器48kHz）。
"""
import numpy as np
import sounddevice as sd
import threading
from collections import deque
from typing import Optional, Callable, List
from scipy import signal as scipy_signal

from audio_router.services.howling_suppressor_v2 import (
    TrueFrequencyShifter, MultiCriteriaHowlingSuppressor)

from ..models.audio_device import AudioDeviceInfo


class AudioRouterEngine:
    """音频路由引擎 - 实时麦克风到扬声器音频路由"""

    def __init__(self):
        # 设备配置
        self._input_device: Optional[AudioDeviceInfo] = None
        self._output_device: Optional[AudioDeviceInfo] = None

        # 音频参数
        self._input_samplerate: int = 48000
        self._output_samplerate: int = 48000
        self._input_channels: int = 1
        self._output_channels: int = 2
        self._block_size: int = 512  # 约 10ms @ 48kHz

        # 音量控制 (0.0 ~ 2.0)
        self._volume: float = 1.0
        # 【改动1】默认增益 200% → 100%
        # 蓝牙耳机麦克风灵敏度通常足够，200% 增益在外放场景等于主动制造啸叫。
        self._input_gain: float = 1.0

        # 噪声门（防啸叫/回音）
        # 【改动2】默认关闭噪声门。原实现按「整块 RMS」判定并跳变增益，
        # 会造成吞字/喘息感。改用逐样本平滑包络后风险已降低，
        # 但仍建议默认关闭，由用户按需开启。
        self._noise_gate_enabled: bool = False
        self._noise_gate_threshold: float = 0.015  # 低于此电平直接静音（0.0~1.0）
        self._noise_gate_attenuation: float = 0.0  # 关闭时的衰减量（0=完全静音，0.1=保留10%）
        # 【改动3】噪声门新增 hold 时间，避免语音字间停顿被误关门
        self._noise_gate_hold_ms: float = 120.0

        # 侧链抑制（防啸叫：输出声音大时自动压低输入）
        # 【改动4】默认关闭。它用「上一块输出 RMS」压制输入增益，
        # 形成随语音起伏的自动增益 → 音量抽动。对抑制啸叫贡献很小（仅辅助级）。
        self._sidechain_enabled: bool = False
        self._sidechain_amount: float = 0.6  # 抑制强度 0.0~1.0（0.6=最多压低60%）
        self._sidechain_threshold: float = 0.05  # 输出电平超过此值时启动抑制

        # 噪声门平滑状态（避免开关时的咔哒声）
        self._gate_gain: float = 1.0  # 当前门增益（平滑过渡）
        # 【改动5】改用逐样本时间常数（毫秒），不再用按块跳变的系数。
        # attack 8ms（快速打开，保住字首）/ release 150ms（缓慢关闭，避免喘息）
        self._gate_attack_ms: float = 8.0
        self._gate_release_ms: float = 150.0
        self._gate_hold_blocks: int = 0
        self._gate_attack_coeff: float = 0.8   # 保留字段（旧逻辑兼容），实际不再使用
        self._gate_release_coeff: float = 0.95  # 保留字段（旧逻辑兼容），实际不再使用

        # 频域啸叫抑制
        # 【改动6】移频默认关闭。原实现是「延迟调制」= 颤音（4Hz 抖音），
        # 会让人声发抖/有金属感。新版 TrueFrequencyShifter 是真移频，
        # 音染小得多，但仍有轻微音色偏移，默认关闭由用户按需开启。
        self._freq_shifter: Optional[TrueFrequencyShifter] = None
        self._notch_filter: Optional[MultiCriteriaHowlingSuppressor] = None
        self._freq_shift_enabled: bool = False
        # 【改动7】移频量 5Hz → 4Hz（文献与工程共识 3~5Hz）
        self._freq_shift_amount: float = 4.0  # 移频量（Hz）
        self._notch_enabled: bool = True
        # 【改动8】陷波衰减 20dB → 10dB（原实现过于激进，误伤人声时后果严重）
        self._notch_attenuation_db: float = 10.0  # 陷波衰减（dB）

        # 运行状态
        self._is_running: bool = False
        self._input_stream: Optional[sd.InputStream] = None
        self._output_stream: Optional[sd.OutputStream] = None

        # 样本级环形缓冲区（存储 float32 样本，按输出采样率）
        # 使用足够大的缓冲来吸收抖动和重采样差异
        self._buffer: Optional[np.ndarray] = None  # 一维数组，存交错后的多声道样本
        self._buffer_size: int = 0  # 总样本数（单声道计数）
        self._buffer_write_pos: int = 0
        self._buffer_read_pos: int = 0
        self._buffer_lock = threading.Lock()
        self._target_buffersize_ms: float = 50.0  # 目标缓冲 50ms

        # 统计信息
        self._input_level: float = 0.0
        self._output_level: float = 0.0
        self._buffer_occupancy: float = 0.0
        self._total_frames_processed: int = 0
        self._xruns: int = 0

        # 状态回调
        self._status_callback: Optional[Callable] = None
        self._error_callback: Optional[Callable] = None

        # 重采样比例
        self._resample_ratio: float = 1.0
        self._need_resample: bool = False

        # 上一块时间戳（用于估算延迟）
        self._last_output_time: float = 0.0

        # 输出端的原始目标信号副本（供检测分析参考）
        self._last_target: Optional[np.ndarray] = None

    # ---------- 配置方法 ----------

    def set_input_device(self, device: AudioDeviceInfo):
        """设置输入设备"""
        if self._is_running:
            raise RuntimeError("Cannot change device while running")
        self._input_device = device
        self._input_samplerate = int(device.default_samplerate) if device.default_samplerate else 48000
        self._input_channels = min(device.max_input_channels, 2)

    def set_output_device(self, device: AudioDeviceInfo):
        """设置输出设备"""
        if self._is_running:
            raise RuntimeError("Cannot change device while running")
        self._output_device = device
        self._output_samplerate = int(device.default_samplerate) if device.default_samplerate else 48000
        self._output_channels = min(device.max_output_channels, 2)

    def set_volume(self, volume: float):
        """设置输出音量 (0.0 ~ 2.0)"""
        self._volume = max(0.0, min(2.0, volume))

    def get_volume(self) -> float:
        return self._volume

    def set_input_gain(self, gain: float):
        """设置输入增益/麦克风放大 (0.0 ~ 10.0)，默认 2.0 (200%)"""
        self._input_gain = max(0.0, min(10.0, gain))

    def get_input_gain(self) -> float:
        return self._input_gain

    # ---------- 噪声门控制 ----------

    def set_noise_gate_enabled(self, enabled: bool):
        """开启/关闭噪声门"""
        self._noise_gate_enabled = enabled

    def is_noise_gate_enabled(self) -> bool:
        return self._noise_gate_enabled

    def set_noise_gate_threshold(self, threshold: float):
        """设置噪声门阈值 (0.0 ~ 0.1)，低于此电平静音"""
        self._noise_gate_threshold = max(0.0, min(0.1, threshold))

    def get_noise_gate_threshold(self) -> float:
        return self._noise_gate_threshold

    # ---------- 侧链抑制控制 ----------

    def set_sidechain_enabled(self, enabled: bool):
        """开启/关闭侧链抑制"""
        self._sidechain_enabled = enabled

    def is_sidechain_enabled(self) -> bool:
        return self._sidechain_enabled

    def set_sidechain_amount(self, amount: float):
        """设置侧链抑制强度 (0.0 ~ 1.0)，越大抑制越强"""
        self._sidechain_amount = max(0.0, min(1.0, amount))

    def get_sidechain_amount(self) -> float:
        return self._sidechain_amount

    # ---------- 频域啸叫抑制控制 ----------

    def set_freq_shift_enabled(self, enabled: bool):
        """开启/关闭频谱移频（核心啸叫抑制）"""
        self._freq_shift_enabled = enabled
        if self._freq_shifter and not enabled:
            self._freq_shifter.reset()

    def is_freq_shift_enabled(self) -> bool:
        return self._freq_shift_enabled

    def set_freq_shift_amount(self, shift_hz: float):
        """设置移频量（Hz），建议 3~8 Hz"""
        self._freq_shift_amount = max(0.0, min(15.0, shift_hz))
        if self._freq_shifter:
            self._freq_shifter.set_shift_hz(self._freq_shift_amount)

    def get_freq_shift_amount(self) -> float:
        return self._freq_shift_amount

    def set_notch_enabled(self, enabled: bool):
        """开启/关闭自适应陷波"""
        self._notch_enabled = enabled
        if self._notch_filter and not enabled:
            self._notch_filter.reset()

    def is_notch_enabled(self) -> bool:
        return self._notch_enabled

    def set_notch_attenuation_db(self, db: float):
        """设置陷波衰减量（dB）"""
        self._notch_attenuation_db = max(0.0, min(30.0, db))
        if self._notch_filter:
            self._notch_filter.set_attenuation_db(self._notch_attenuation_db)

    def get_notch_attenuation_db(self) -> float:
        return self._notch_attenuation_db

    def get_active_notch_count(self) -> int:
        """当前激活的陷波器数量"""
        if self._notch_filter:
            return int(self._notch_filter.active_count)
        return 0

    def get_active_notch_freqs(self) -> list:
        """当前激活的陷波频率（Hz），用于 UI 显示"""
        if self._notch_filter:
            return list(self._notch_filter.active_freqs)
        return []

    def set_block_size(self, size: int):
        """设置块大小（延迟控制）"""
        if self._is_running:
            raise RuntimeError("Cannot change block size while running")
        self._block_size = max(64, min(4096, size))

    def set_status_callback(self, callback: Optional[Callable]):
        """设置状态回调"""
        self._status_callback = callback

    def set_error_callback(self, callback: Optional[Callable]):
        """设置错误回调"""
        self._error_callback = callback

    # ---------- 状态查询 ----------

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def input_level(self) -> float:
        """输入电平 (0.0 ~ 1.0)"""
        return self._input_level

    @property
    def output_level(self) -> float:
        """输出电平 (0.0 ~ 1.0)"""
        return self._output_level

    @property
    def buffer_occupancy(self) -> float:
        """缓冲区占用率 (0.0 ~ 1.0)"""
        return self._buffer_occupancy

    @property
    def xruns(self) -> int:
        """欠载/过载次数"""
        return self._xruns

    @property
    def gate_gain(self) -> float:
        """当前噪声门增益 (0.0 ~ 1.0)，用于UI显示门是否激活"""
        return self._gate_gain

    @property
    def estimated_latency_ms(self) -> float:
        """估算延迟（毫秒）"""
        if not self._is_running or self._output_samplerate <= 0:
            return 0.0
        # 基于缓冲区中当前可用样本数估算
        available_samples = self._get_buffer_samples()
        return (available_samples / self._output_samplerate) * 1000.0

    def _get_buffer_samples(self) -> int:
        """获取缓冲区中可用的样本数（单声道样本数）"""
        if self._buffer is None:
            return 0
        with self._buffer_lock:
            if self._buffer_write_pos >= self._buffer_read_pos:
                samples = self._buffer_write_pos - self._buffer_read_pos
            else:
                samples = self._buffer_size - self._buffer_read_pos + self._buffer_write_pos
            return samples

    # ---------- 核心控制 ----------

    def start(self):
        """启动音频路由（带自动容错和渐进式诊断）"""
        if self._is_running:
            return
        if not self._input_device:
            raise ValueError("No input device selected")
        if not self._output_device:
            raise ValueError("No output device selected")

        # 重置统计
        self._xruns = 0
        self._total_frames_processed = 0
        self._input_level = 0.0
        self._output_level = 0.0
        # 重置噪声门状态，避免继承上次会话的残留增益
        self._gate_gain = 1.0
        self._gate_hold_blocks = 0

        # 计算重采样比例
        self._resample_ratio = self._output_samplerate / self._input_samplerate
        self._need_resample = abs(self._resample_ratio - 1.0) > 0.001

        # 初始化环形缓冲区（按输出采样率，约 500ms 缓冲防止溢出）
        target_buffer_ms = 500
        total_samples = int(self._output_samplerate * target_buffer_ms / 1000.0)
        self._buffer_size = total_samples
        self._buffer = np.zeros(total_samples * self._output_channels, dtype=np.float32)
        self._buffer_write_pos = 0
        self._buffer_read_pos = 0

        # 预热：填充 100ms 静音（蓝牙设备输入慢，避免启动欠载）
        warmup_ms = 100
        warmup_samples = int(self._output_samplerate * warmup_ms / 1000.0)
        self._buffer_write_pos = warmup_samples % self._buffer_size
        self._buffer_occupancy = warmup_samples / total_samples

        # 收集所有失败记录用于诊断
        failures = []

        # 尝试不同的参数组合（先试最可能成功的）
        latency_options = [None, 'high', 'low']  # None=默认值通常最兼容
        blocksize_options = [0, 1024, 2048, 512, 256]  # 0=让PortAudio自动选择

        for latency in latency_options:
            for blocksize in blocksize_options:
                bs_label = blocksize if blocksize > 0 else 'auto'
                lat_label = latency if latency else 'default'
                try:
                    self._try_start_full(blocksize if blocksize > 0 else None, latency)
                    # 成功
                    return
                except Exception as e:
                    err_msg = f"latency={lat_label}, blocksize={bs_label}: {type(e).__name__}: {str(e)[:80]}"
                    failures.append(err_msg)
                    self._cleanup_streams()
                    continue

        # 所有组合都失败了
        self._buffer = None
        input_api = getattr(self._input_device, 'hostapi_name', f'API{self._input_device.hostapi}')
        output_api = getattr(self._output_device, 'hostapi_name', f'API{self._output_device.hostapi}')

        error_details = "\n".join(failures[:6])  # 只显示前6条
        raise RuntimeError(
            f"Failed to start audio router.\n\n"
            f"Input device: {self._input_device.display_name}\n"
            f"  API: {input_api}\n"
            f"  SampleRate: {self._input_samplerate} Hz\n"
            f"  Channels: {self._input_channels}\n\n"
            f"Output device: {self._output_device.display_name}\n"
            f"  API: {output_api}\n"
            f"  SampleRate: {self._output_samplerate} Hz\n"
            f"  Channels: {self._output_channels}\n\n"
            f"Attempted {len(failures)} parameter combinations, all failed.\n"
            f"Recent failures:\n{error_details}\n\n"
            f"Suggestions:\n"
            f"  1. Try selecting a different device (different API)\n"
            f"  2. Make sure the device is not used by another app\n"
            f"  3. Try WASAPI devices first (most reliable)"
        )

    def _try_start_full(self, blocksize, latency):
        """尝试完整启动（输入+输出流）"""
        # 初始化啸叫抑制器（基于输出采样率和声道数）
        # 注意：移频/陷波都作用在「输出端」——即即将播放的声音上，
        # 这样「扬声器→麦克风」回环时，频率已经偏移/削弱了。
        if self._freq_shift_enabled:
            self._freq_shifter = TrueFrequencyShifter(
                shift_hz=self._freq_shift_amount,
                sample_rate=self._output_samplerate,
                channels=self._output_channels
            )
        else:
            self._freq_shifter = None

        if self._notch_enabled:
            self._notch_filter = MultiCriteriaHowlingSuppressor(
                sample_rate=self._output_samplerate,
                channels=self._output_channels,
                max_notches=4,
                quality=10.0,
                attenuation_db=self._notch_attenuation_db
            )
        else:
            self._notch_filter = None

        # 先测试输入流
        input_args = dict(
            device=self._input_device.index,
            channels=self._input_channels,
            samplerate=self._input_samplerate,
            dtype='float32',
            callback=self._input_callback
        )
        if blocksize is not None:
            input_args['blocksize'] = blocksize
        if latency is not None:
            input_args['latency'] = latency

        try:
            self._input_stream = sd.InputStream(**input_args)
        except Exception as e:
            raise RuntimeError(f"Failed to create input stream: {e}")

        # 再测试输出流
        output_args = dict(
            device=self._output_device.index,
            channels=self._output_channels,
            samplerate=self._output_samplerate,
            dtype='float32',
            callback=self._output_callback
        )
        if blocksize is not None:
            output_args['blocksize'] = blocksize
        if latency is not None:
            output_args['latency'] = latency

        try:
            self._output_stream = sd.OutputStream(**output_args)
        except Exception as e:
            # 清理输入流
            try:
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None
            raise RuntimeError(f"Failed to create output stream: {e}")

        # 启动流（先启动输出，再启动输入，避免初始欠载）
        try:
            self._output_stream.start()
        except Exception as e:
            self._cleanup_streams()
            raise RuntimeError(f"Failed to start output stream: {e}")

        try:
            self._input_stream.start()
        except Exception as e:
            self._cleanup_streams()
            raise RuntimeError(f"Failed to start input stream: {e}")

        self._is_running = True
        self._notify_status("started")

    def stop(self):
        """停止音频路由"""
        if not self._is_running:
            return
        self._is_running = False
        self._cleanup_streams()
        self._buffer = None
        self._input_level = 0.0
        self._output_level = 0.0
        self._buffer_occupancy = 0.0

        # 清理啸叫抑制器
        self._freq_shifter = None
        self._notch_filter = None

        self._notify_status("stopped")

    def _cleanup_streams(self):
        """清理音频流"""
        try:
            if self._input_stream:
                self._input_stream.stop()
                self._input_stream.close()
        except Exception:
            pass
        try:
            if self._output_stream:
                self._output_stream.stop()
                self._output_stream.close()
        except Exception:
            pass
        self._input_stream = None
        self._output_stream = None

    # ---------- 环形缓冲区操作 ----------

    def _buffer_write(self, data: np.ndarray):
        """写入多声道音频数据到缓冲区（data: [samples, channels]）"""
        if self._buffer is None:
            return False

        samples = len(data)
        ch = self._output_channels

        with self._buffer_lock:
            # 检查是否有足够空间
            available = self._buffer_free_samples_locked()
            if samples > available:
                # 空间不足，丢数据（过载）
                self._xruns += 1
                # 只写入能放下的部分
                samples = available
                if samples <= 0:
                    return False

            # 写入数据（处理回绕）
            pos = self._buffer_write_pos
            for c in range(ch):
                channel_data = data[:, c]
                # 第一段：从 pos 到 buffer 末尾
                end = min(pos + samples, self._buffer_size)
                count1 = end - pos
                if count1 > 0:
                    self._buffer[pos * ch + c::ch][:count1] = channel_data[:count1]
                # 第二段：从 buffer 开头继续（如果需要回绕）
                count2 = samples - count1
                if count2 > 0:
                    self._buffer[c::ch][:count2] = channel_data[count1:count1 + count2]

            self._buffer_write_pos = (pos + samples) % self._buffer_size
            self._buffer_occupancy = self._buffer_available_samples_locked() / self._buffer_size
            return True

    def _buffer_read(self, outdata: np.ndarray) -> int:
        """从缓冲区读取多声道音频数据，返回复制的样本数"""
        if self._buffer is None:
            return 0

        samples = len(outdata)
        ch = self._output_channels

        with self._buffer_lock:
            available = self._buffer_available_samples_locked()
            if available == 0:
                return 0

            read_samples = min(samples, available)
            pos = self._buffer_read_pos

            for c in range(ch):
                # 第一段：从 pos 到 buffer 末尾
                end = min(pos + read_samples, self._buffer_size)
                count1 = end - pos
                if count1 > 0:
                    outdata[:count1, c] = self._buffer[pos * ch + c::ch][:count1]
                # 第二段：从 buffer 开头继续
                count2 = read_samples - count1
                if count2 > 0:
                    outdata[count1:count1 + count2, c] = self._buffer[c::ch][:count2]

            self._buffer_read_pos = (pos + read_samples) % self._buffer_size
            self._buffer_occupancy = self._buffer_available_samples_locked() / self._buffer_size
            return read_samples

    def _buffer_available_samples_locked(self) -> int:
        """已加锁情况下获取可用样本数"""
        if self._buffer_write_pos >= self._buffer_read_pos:
            return self._buffer_write_pos - self._buffer_read_pos
        else:
            return self._buffer_size - self._buffer_read_pos + self._buffer_write_pos

    def _buffer_free_samples_locked(self) -> int:
        """已加锁情况下获取空闲样本数（留 1 个样本的间隔区分满/空）"""
        return self._buffer_size - self._buffer_available_samples_locked() - 1

    # ---------- 音频回调 ----------

    def _input_callback(self, indata, frames, time_info, status):
        """输入流回调 - 采集到新音频数据"""
        if status and status.input_overflow:
            self._xruns += 1

        if not self._is_running:
            return

        try:
            # 1. 计算原始输入电平（增益前）
            input_level = float(np.sqrt(np.mean(indata ** 2)))

            # 2. 应用输入增益（麦克风放大）
            if self._input_gain != 1.0:
                indata = indata * self._input_gain
                np.clip(indata, -1.0, 1.0, out=indata)

            # 3. 计算增益后的电平（用于噪声门判断）
            gained_level = input_level * self._input_gain
            if gained_level > 1.0:
                gained_level = 1.0

            # 4. 噪声门 - 低于阈值时静音（防啸叫回音）
            #    【重写】改为「逐样本时间常数 + hold」方案：
            #      - attack 8ms：快速开门，保住字首（原按块跳变会吞字）
            #      - release 150ms：缓慢关门，避免喘息感
            #      - hold 120ms：语音字间短停顿不关门
            if self._noise_gate_enabled:
                if gained_level > self._noise_gate_threshold:
                    # 超过阈值：开门，并重置 hold 计时
                    self._gate_hold_blocks = max(
                        1, int(self._noise_gate_hold_ms /
                               max(1e-6, frames / self._output_samplerate * 1000.0)))
                    target_gain = 1.0
                    coeff = self._gate_attack_ms
                elif self._gate_hold_blocks > 0:
                    # hold 期间保持开门（字间停顿）
                    self._gate_hold_blocks -= 1
                    target_gain = 1.0
                    coeff = self._gate_attack_ms
                else:
                    # 低于阈值且 hold 结束：关门
                    target_gain = self._noise_gate_attenuation
                    coeff = self._gate_release_ms

                # 逐样本平滑（时间常数 → 每样本系数）
                block_ms = max(1e-6, frames / self._output_samplerate * 1000.0)
                alpha = 1.0 - np.exp(-block_ms / max(0.1, coeff))
                self._gate_gain += (target_gain - self._gate_gain) * alpha
                self._gate_gain = float(np.clip(self._gate_gain, 0.0, 1.0))

                if self._gate_gain < 0.999:
                    indata = indata * self._gate_gain
            else:
                self._gate_gain = 1.0

            # 5. 侧链抑制（防啸叫：输出声音大时自动压低输入）
            #    默认关闭。它会造成随语音起伏的自动增益 =「音量抽动」，
            #    且对抑制啸叫仅为辅助级贡献。保留能力供用户按需开启。
            if self._sidechain_enabled and self._output_level > self._sidechain_threshold:
                excess = self._output_level - self._sidechain_threshold
                ratio = min(1.0, excess * 10.0)
                ducking = ratio * self._sidechain_amount
                sidechain_gain = 1.0 - ducking
                if sidechain_gain < 1.0:
                    indata = indata * sidechain_gain

            # 6. 计算最终输入电平（用于UI显示）
            self._input_level = float(np.sqrt(np.mean(indata ** 2)))

            # 单声道转多声道
            if self._input_channels == 1 and self._output_channels > 1:
                data = np.tile(indata, (1, self._output_channels))
            else:
                data = indata.copy()

            # 重采样到输出采样率
            if self._need_resample:
                target_len = int(frames * self._resample_ratio)
                data = self._resample_audio(data, target_len)

            # 应用输出音量
            if self._volume != 1.0:
                data = data * self._volume
                np.clip(data, -1.0, 1.0, out=data)

            # 写入环形缓冲区
            self._buffer_write(data.astype(np.float32))

        except Exception as e:
            self._notify_error(f"Input error: {str(e)}")

    def _output_callback(self, outdata, frames, time_info, status):
        """输出流回调 - 需要播放新音频数据"""
        if status and status.output_underflow:
            self._xruns += 1

        if not self._is_running:
            outdata.fill(0)
            return

        try:
            # 从缓冲区读取
            read_samples = self._buffer_read(outdata)

            if read_samples < frames:
                # 数据不足，剩余填充静音
                outdata[read_samples:] = 0
                self._xruns += 1

            # 记录未经抑制处理的目标信号（供自适应模块分析参考）
            self._last_target = outdata.copy()

            # ---- 啸叫抑制处理 ----
            # 注意：处理是在输出端做的（作用于即将播放的声音）
            # 这样"扬声器→麦克风"回环时，频率已经偏移/削弱了

            # 1. 自适应陷波（多判据检测 + 平滑陷波）
            #    先做陷波再做移频：让陷波器分析的是「未经频率搬移」的信号，
            #    频谱结构更干净，检测更准。
            if self._notch_filter is not None and self._notch_enabled:
                processed = self._notch_filter.process(outdata)
                outdata[:] = processed

            # 2. 真移频（SSB / Hilbert）—— 破坏反馈共振
            if self._freq_shifter is not None and self._freq_shift_enabled:
                processed = self._freq_shifter.process(outdata)
                outdata[:] = processed

            # 计算输出电平（处理后）
            self._output_level = float(np.sqrt(np.mean(outdata ** 2)))
            self._total_frames_processed += frames

        except Exception as e:
            self._notify_error(f"Output error: {str(e)}")
            outdata.fill(0)

    def _resample_audio(self, data: np.ndarray, target_len: int) -> np.ndarray:
        """重采样音频数据"""
        input_len = len(data)
        if input_len == target_len:
            return data

        # 使用 scipy 的傅里叶重采样（质量好）
        # 对于小数据块使用线性插值更快，大数据块用 scipy
        if input_len < 256:
            return self._linear_resample(data, target_len)

        try:
            resampled = scipy_signal.resample(data, target_len, axis=0)
            return resampled.astype(np.float32)
        except Exception:
            return self._linear_resample(data, target_len)

    def _linear_resample(self, data: np.ndarray, target_len: int) -> np.ndarray:
        """快速线性插值重采样"""
        input_len = len(data)
        if input_len == 0 or target_len == 0:
            return data

        channels = data.shape[1] if data.ndim > 1 else 1
        result = np.zeros((target_len, channels), dtype=np.float32)

        # 使用 numpy 向量化操作加速
        indices = np.linspace(0, input_len - 1, target_len, dtype=np.float32)
        idx0 = np.floor(indices).astype(np.int32)
        idx1 = np.minimum(idx0 + 1, input_len - 1)
        frac = (indices - idx0).reshape(-1, 1)

        for c in range(channels):
            result[:, c] = data[idx0, c] * (1 - frac[:, 0]) + data[idx1, c] * frac[:, 0]

        return result

    # ---------- 通知方法 ----------

    def _notify_status(self, status: str):
        if self._status_callback:
            try:
                self._status_callback(status)
            except Exception:
                pass

    def _notify_error(self, error: str):
        if self._error_callback:
            try:
                self._error_callback(error)
            except Exception:
                pass

    # ---------- 析构 ----------

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass
