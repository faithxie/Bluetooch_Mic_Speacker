"""
【已废弃 / DEPRECATED】旧版啸叫抑制算法模块

⚠️ 本模块已不再被程序任何地方引用，仅作历史记录保留。
   请勿在新代码中使用，也不要重新接入引擎。

废弃原因（详见项目根目录《啸叫问题诊断与解决方案报告.md》）：
  1. FrequencyShifter 名为「频谱移频」，实为「延迟调制」= 颤音(FM)。
     实测输入 1000Hz 时，主峰仍在 ~1000Hz，能量散布到 ±4Hz 边带，
     并未把频谱平移 → 听感上是人声发抖、有金属感。
  2. AdaptiveNotchFilter 只用单一判据（峰比邻域高 6dB）判定啸叫，
     实测在 187 帧人声上误触发 66 次，把元音共振峰 F1/F2/F3
     （~730/1193/2508Hz）当成啸叫挖掉 → 这正是「说话声音都变了」的根因。
  3. 陷波器每帧重建并清零 IIR 状态 → 每 ~107ms 产生一次咔哒声。

替代实现：audio_router/services/howling_suppressor_v2.py
  - TrueFrequencyShifter          → 真移频（FIR Hilbert / SSB）
  - MultiCriteriaHowlingSuppressor → 多判据 AND 检测（PNPR+PHPR+IMSD+PTPR）

原说明（仅供追溯）：
包含两种核心算法：
1. 颤音调制（Vibrato / Frequency Modulation）- 破坏反馈共振，最有效
2. 自适应陷波（Adaptive Notch Filter）- 自动检测并消除啸叫频率
"""

import numpy as np


class FrequencyShifter:
    """
    颤音调制啸叫抑制器（Vibrato-based Feedback Suppressor）
    原理：用低频正弦波调制延迟时间（±0.5ms 左右），
    等效于对信号进行频率调制(FM)，产生大量边带。
    这样每次"扬声器→麦克风→扬声器"循环，啸叫频率的能量
    都会被分散到边带，无法形成稳定的共振，从根源上消除啸叫。

    优点：实现简单、延迟低（<5ms）、CPU 占用小、对音质影响小
    这是专业啸叫抑制器的常用方案之一。
    """

    def __init__(self, shift_hz: float = 5.0, sample_rate: int = 48000,
                 channels: int = 2, depth_ms: float = 0.5, mod_rate: float = 4.0):
        """
        Args:
            shift_hz: 等效移频量（Hz），实际通过调制深度间接控制
                      这个参数保留给外部 API 用，越大抑制越强
            sample_rate: 采样率
            channels: 声道数
            depth_ms: 延迟调制深度（毫秒），越大频率偏移越大
            mod_rate: 调制频率（Hz），即颤音的速度
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.depth_ms = depth_ms
        self.mod_rate = mod_rate

        # 基础延迟（样本数）- 至少是调制深度的 2 倍
        self.base_delay_samples = int(depth_ms * sample_rate / 1000 * 2) + 1
        self.mod_depth_samples = int(depth_ms * sample_rate / 1000)

        # 延迟线缓冲区（每个声道独立）
        self._buffer_size = self.base_delay_samples + self.mod_depth_samples + 1024
        self._delay_buffers = [np.zeros(self._buffer_size, dtype=np.float32)
                               for _ in range(channels)]
        self._write_pos = 0

        # 调制相位
        self._mod_phase = 0.0
        self._mod_phase_step = 2 * np.pi * mod_rate / sample_rate

        # 根据 shift_hz 调整调制深度（简单映射：shift_hz 越大 -> 深度越大）
        self.set_shift_hz(shift_hz)

    def set_shift_hz(self, shift_hz: float):
        """设置等效移频强度（0~15 Hz 映射到调制深度）"""
        shift_hz = max(0.0, min(15.0, shift_hz))
        # 映射：0Hz -> 0.1ms(几乎无), 5Hz -> 0.5ms, 15Hz -> 1.5ms
        depth_ms = 0.1 + (shift_hz / 15.0) * 1.4
        self.depth_ms = depth_ms
        self.mod_depth_samples = int(depth_ms * self.sample_rate / 1000)
        self.base_delay_samples = self.mod_depth_samples + 16
        self._buffer_size = self.base_delay_samples + self.mod_depth_samples + 1024

        # 重新分配缓冲区（如果大小变了）
        for ch in range(self.channels):
            if len(self._delay_buffers[ch]) != self._buffer_size:
                old_buf = self._delay_buffers[ch]
                new_buf = np.zeros(self._buffer_size, dtype=np.float32)
                # 复制旧数据的尾部
                copy_len = min(len(old_buf), self._buffer_size)
                new_buf[:copy_len] = old_buf[-copy_len:]
                self._delay_buffers[ch] = new_buf

    def _get_delayed_sample(self, ch: int, delay_samples: float) -> float:
        """获取延迟后的样本（线性插值）"""
        buf = self._delay_buffers[ch]
        buf_len = len(buf)

        # 整数和小数部分
        d_int = int(delay_samples)
        d_frac = delay_samples - d_int

        # 两个读取位置
        pos1 = (self._write_pos - d_int) % buf_len
        pos2 = (self._write_pos - d_int - 1) % buf_len

        # 线性插值
        return buf[pos1] * (1 - d_frac) + buf[pos2] * d_frac

    def process(self, audio: np.ndarray) -> np.ndarray:
        """
        处理一块音频数据
        Args:
            audio: [samples, channels] float32
        Returns:
            处理后的音频，shape 相同
        """
        if self.mod_depth_samples < 1:
            return audio.copy()

        samples = len(audio)
        output = np.zeros_like(audio)

        for ch in range(self.channels):
            ch_data = audio[:, ch]
            buf = self._delay_buffers[ch]
            buf_len = len(buf)
            out_ch = output[:, ch]

            for i in range(samples):
                # 写入延迟线
                buf[self._write_pos] = ch_data[i]

                # 计算当前调制延迟
                # 延迟 = 基础延迟 + 调制深度 * sin(相位)
                mod_val = np.sin(self._mod_phase)
                delay = self.base_delay_samples + mod_val * self.mod_depth_samples

                # 读取延迟后的样本（线性插值）
                d_int = int(delay)
                d_frac = delay - d_int

                pos1 = (self._write_pos - d_int) % buf_len
                pos2 = (self._write_pos - d_int - 1) % buf_len

                out_ch[i] = buf[pos1] * (1 - d_frac) + buf[pos2] * d_frac

                # 更新写指针和相位
                self._write_pos = (self._write_pos + 1) % buf_len
                self._mod_phase += self._mod_phase_step
                if self._mod_phase > 2 * np.pi:
                    self._mod_phase -= 2 * np.pi

        return output

    def reset(self):
        """重置"""
        for ch in range(self.channels):
            self._delay_buffers[ch].fill(0)
        self._write_pos = 0
        self._mod_phase = 0.0


class AdaptiveNotchFilter:
    """
    自适应陷波滤波器 - 自动检测并消除啸叫频率
    原理：实时分析输出频谱，找到能量异常集中的尖峰（啸叫特征），
    自动在那个频率上放置一个窄带陷波器把它压下去。

    使用二阶 IIR 陷波滤波器 + 频域啸叫检测
    """

    def __init__(self, sample_rate: int = 48000, channels: int = 2,
                 max_notches: int = 3, quality: float = 30.0,
                 detection_threshold_db: float = 6.0,
                 attenuation_db: float = 20.0):
        """
        Args:
            sample_rate: 采样率
            channels: 声道数
            max_notches: 最多同时激活的陷波器数量
            quality: 陷波器 Q 值（越大带宽越窄）
            detection_threshold_db: 尖峰超过周围多少 dB 判定为啸叫
            attenuation_db: 陷波衰减量（dB）
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.max_notches = max_notches
        self.quality = quality
        self.detection_threshold_db = detection_threshold_db
        self.attenuation_db = attenuation_db

        # 当前激活的陷波频率列表
        self._active_notches = []  # [(freq, b0, b1, b2, a1, a2)]

        # 每个声道的滤波器状态（双二阶滤波器状态）
        self._filter_states = []  # [ch][notch_idx][x1, x2, y1, y2]

        # 平滑频谱（用于检测啸叫尖峰）
        self._smoothed_spectrum = None
        self._spectrum_smooth_coeff = 0.9  # 频谱平滑系数

        # 分析帧缓冲区
        self._analyze_buffer = [np.zeros(512, dtype=np.float32) for _ in range(channels)]
        self._analyze_pos = 0
        self._analyze_frame_size = 512

        # 检测计数器（避免频繁切换陷波频率）
        self._detection_counter = 0
        self._detection_interval = 10  # 每 10 帧检测一次（约 200ms）

    def set_attenuation_db(self, db: float):
        """设置陷波衰减量（dB）"""
        self.attenuation_db = max(0.0, min(40.0, db))

    def set_quality(self, q: float):
        """设置 Q 值（带宽）"""
        self.quality = max(5.0, min(100.0, q))

    def _design_notch(self, freq: float, attenuation_db: float):
        """
        设计二阶 IIR 陷波滤波器（双二阶形式）
        Returns: (b0, b1, b2, a1, a2)
        """
        omega = 2 * np.pi * freq / self.sample_rate
        alpha = np.sin(omega) / (2 * self.quality)
        A = 10 ** (attenuation_db / 40.0)  # 增益

        b0 = 1.0
        b1 = -2 * np.cos(omega)
        b2 = 1.0
        a0 = 1.0 + alpha / A
        a1 = -2 * np.cos(omega)
        a2 = 1.0 - alpha / A

        # 归一化
        b0 /= a0
        b1 /= a0
        b2 /= a0
        a1 /= a0
        a2 /= a0

        return (b0, b1, b2, a1, a2)

    def _detect_feedback_peaks(self, spectrum: np.ndarray) -> list:
        """
        检测频谱中的啸叫尖峰
        Returns: 啸叫频率列表（Hz），按能量从高到低排序
        """
        n_bins = len(spectrum)
        freqs = np.fft.rfftfreq(self._analyze_frame_size, 1.0 / self.sample_rate)

        # 只关注 200Hz ~ 8kHz 范围（啸叫通常在这个范围）
        low_bin = int(200 * self._analyze_frame_size / self.sample_rate)
        high_bin = int(8000 * self._analyze_frame_size / self.sample_rate)
        low_bin = max(1, low_bin)
        high_bin = min(n_bins - 1, high_bin)

        peaks = []

        for i in range(low_bin + 1, high_bin - 1):
            # 局部极大值
            if spectrum[i] <= spectrum[i-1] or spectrum[i] <= spectrum[i+1]:
                continue

            # 计算与周围邻域的 dB 差
            neighborhood = spectrum[max(0, i-10):min(n_bins, i+11)]
            local_median = np.median(neighborhood)
            if local_median < 1e-8:
                continue

            db_diff = 20 * np.log10(spectrum[i] / local_median)

            if db_diff > self.detection_threshold_db:
                # 这是一个啸叫尖峰
                peak_freq = freqs[i]
                # 频率插值（更精确）
                if i > 0 and i < n_bins - 1:
                    y0 = spectrum[i-1]
                    y1 = spectrum[i]
                    y2 = spectrum[i+1]
                    if y0 + y2 - 2*y1 != 0:
                        offset = (y0 - y2) / (2 * (y0 + y2 - 2*y1))
                        peak_freq = freqs[i] + offset * (freqs[1] - freqs[0])

                peaks.append((peak_freq, db_diff, spectrum[i]))

        # 按能量排序，取前 N 个
        peaks.sort(key=lambda x: -x[2])
        return [p[0] for p in peaks[:self.max_notches]]

    def _apply_filter(self, x: np.ndarray, notch_idx: int, ch: int) -> np.ndarray:
        """应用一个二阶 IIR 滤波器"""
        if notch_idx >= len(self._active_notches):
            return x

        b0, b1, b2, a1, a2 = self._active_notches[notch_idx]
        state = self._filter_states[ch][notch_idx]
        x1, x2, y1, y2 = state

        y = np.zeros_like(x)
        for i in range(len(x)):
            y[i] = b0 * x[i] + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2 = x1
            x1 = x[i]
            y2 = y1
            y1 = y[i]

        self._filter_states[ch][notch_idx] = [x1, x2, y1, y2]
        return y

    def _update_notches(self, detected_freqs: list):
        """更新激活的陷波器"""
        if not detected_freqs:
            self._active_notches = []
            return

        # 简单策略：直接用检测到的频率重建陷波器
        # （实际产品中会用更平滑的策略，但对于我们的场景足够了）
        new_notches = []
        for freq in detected_freqs[:self.max_notches]:
            coeffs = self._design_notch(freq, self.attenuation_db)
            new_notches.append(coeffs)

        self._active_notches = new_notches

        # 重置滤波器状态
        self._filter_states = []
        for ch in range(self.channels):
            ch_states = []
            for _ in range(len(new_notches)):
                ch_states.append([0.0, 0.0, 0.0, 0.0])
            self._filter_states.append(ch_states)

    def process(self, audio: np.ndarray) -> np.ndarray:
        """
        处理一块音频数据
        Args:
            audio: [samples, channels] float32
        Returns:
            处理后的音频，shape 相同
        """
        if len(self._active_notches) == 0:
            # 没有激活的陷波器，但仍需要收集数据用于检测
            self._analyze_and_detect(audio)
            return audio.copy()

        output = np.zeros_like(audio)

        for ch in range(self.channels):
            ch_data = audio[:, ch].copy()

            # 级联所有陷波器
            for notch_idx in range(len(self._active_notches)):
                ch_data = self._apply_filter(ch_data, notch_idx, ch)

            output[:, ch] = ch_data

        # 同时做啸叫检测
        self._analyze_and_detect(audio)

        return output

    def _analyze_and_detect(self, audio: np.ndarray):
        """累积音频数据并周期性检测啸叫"""
        samples = len(audio)

        # 混合所有声道（单声道分析即可）
        mono = np.mean(audio, axis=1) if self.channels > 1 else audio[:, 0]

        # 累积到分析缓冲区
        remaining = samples
        offset = 0

        while remaining > 0:
            space = self._analyze_frame_size - self._analyze_pos
            copy_size = min(remaining, space)

            for ch in range(self.channels):
                self._analyze_buffer[ch][self._analyze_pos:self._analyze_pos + copy_size] = \
                    mono[offset:offset + copy_size]

            self._analyze_pos += copy_size
            offset += copy_size
            remaining -= copy_size

            # 缓冲区满了，做一次 FFT 分析
            if self._analyze_pos >= self._analyze_frame_size:
                self._analyze_pos = 0
                self._detection_counter += 1

                if self._detection_counter >= self._detection_interval:
                    self._detection_counter = 0

                    # 用第一个声道（或混合）分析
                    frame = self._analyze_buffer[0] * np.hanning(self._analyze_frame_size)
                    spectrum = np.abs(np.fft.rfft(frame))

                    # 平滑
                    if self._smoothed_spectrum is None or len(self._smoothed_spectrum) != len(spectrum):
                        self._smoothed_spectrum = spectrum.copy()
                    else:
                        self._smoothed_spectrum = (
                            self._smoothed_spectrum * self._spectrum_smooth_coeff +
                            spectrum * (1 - self._spectrum_smooth_coeff)
                        )

                    # 检测啸叫
                    peaks = self._detect_feedback_peaks(self._smoothed_spectrum)
                    if peaks:
                        self._update_notches(peaks)

    def get_active_frequencies(self) -> list:
        """获取当前激活的陷波频率（用于UI显示）"""
        # 我们没有存频率，只存了系数，这里简化返回数量
        return [f"{i+1}" for i in range(len(self._active_notches))]

    def reset(self):
        """重置"""
        self._active_notches = []
        self._filter_states = []
        self._smoothed_spectrum = None
        self._analyze_pos = 0
        self._detection_counter = 0
        for ch in range(self.channels):
            self._analyze_buffer[ch].fill(0)
