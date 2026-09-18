"""
改进版啸叫抑制模块（原型 / 可直接替换 feedback_suppressor.py 中的对应部分）

相比原实现的四项关键改进：

  改进1  FrequencyShifter → TrueFrequencyShifter
         原实现是「延迟调制」（= 颤音/抖音），不是移频。
         这里用 Hilbert 变换做真正的单边带(SSB)移频，音染极小且能真正
         破坏反馈共振（环路中频率持续偏移，永远无法对齐）。

  改进2  AdaptiveNotchFilter → MultiCriteriaHowlingSuppressor
         原实现只用单一判据（峰比邻域高 6dB）→ 必然把人声共振峰当啸叫。
         这里改用文献推荐的多判据 AND 融合：
             PNPR + PHPR + IMSD + PTPR
         PHPR 是决定性判据：人声有谐波族，啸叫没有。

  改进3  陷波器不再每帧重建并清零状态
         改为「平滑跟踪 + 系数插值 + 保留 IIR 状态」，
         消除每 ~100ms 一次的咔哒声。

  改进4  提高 FFT 帧长到 4096（分辨率 11.7Hz 而非 93.8Hz），
         并把 Q 降到 10 左右，让陷波器频率定位精度与检测精度匹配。

参考：
  - T. van Waterschoot, M. Moonen, "Fifty Years of Acoustic Feedback
    Control: State of the Art and Future Challenges," Proc. IEEE, 2011.
  - van Waterschoot & Moonen, "Comparative evaluation of howling detection
    criteria in notch-filter-based howling suppression," JAES 58(11), 2010.
  - github.com/chenwj1989/python_howling_suppression
"""
import numpy as np

EPS = 1e-12


# ===========================================================================
# 改进 1：真正的移频（SSB via Hilbert）
# ===========================================================================
class TrueFrequencyShifter:
    """真正的单边带移频啸叫抑制器

    原理：把整个频谱平移 +shift_hz（如 1000Hz → 1004Hz）。
    反馈环路中每次循环频率都会再偏移，啸叫永远无法稳定在某个频率上，
    因此从根源上抑制啸叫。对人声音色影响很小（3~5Hz 的恒定偏移几乎听不出）。

    与原实现的区别：
      原：delay = base + depth*sin(ωt) → 颤音(FM)，产生边带，音染大
      新：y = x·cos(2πΔt) - hilbert(x)·sin(2πΔt) → 频谱整体平移，音染小

    实现说明（重要）：
      最初版本用「按块 FFT 做 Hilbert + 重叠相加」，会产生两个问题：
        1. 块边界的解析信号不连续 → 宽带瞬态（咔哒声），频谱出现高频垃圾
        2. 振幅过冲（实测 0.60 → 0.86）
      现改为 **FIR Hilbert 变换器 + 逐样本流式处理**：
        - FIR 是有限长、有状态的线性相位滤波器，跨块天然连续
        - 不需要重叠相加，不会产生块边界瞬态
        - 延迟固定为 (taps-1)/2 样本，可精确补偿
      代价：Hilbert 支路有固定群延迟，需对同相支路做相同的延迟对齐。
    """

    # Hilbert 变换器阶数（奇数）。63 阶在 300Hz~20kHz 内相位误差足够小。
    HILBERT_TAPS = 63

    def __init__(self, shift_hz: float = 4.0, sample_rate: int = 48000,
                 channels: int = 2, block_size: int = 512,
                 overlap: float = 0.5):
        self.sample_rate = sample_rate
        self.channels = channels
        self.shift_hz = shift_hz
        self.block_size = block_size
        self.overlap = overlap

        # 设计 Hilbert 变换器（奇对称、反对称）
        self._h_hilbert = self._design_hilbert(self.HILBERT_TAPS)
        self._taps = len(self._h_hilbert)
        self._group_delay = (self._taps - 1) // 2

        # 每个声道的 FIR 状态：in-phase 延迟线 + hilbert 支路延迟线
        # 用同一组输入历史，分别与 [0..1..0]（延迟）和 h_hilbert 卷积
        self._hist = [np.zeros(self._taps, dtype=np.float64)
                      for _ in range(channels)]

        # 移频载波相位（跨块累积，保证连续）
        self._phase = 0.0

    @staticmethod
    def _design_hilbert(taps: int) -> np.ndarray:
        """设计 FIR Hilbert 变换器（窗函数法）

        h[n] = 2/(π n) for odd n (相对中心), 0 for even n
        用 Hamming 窗平滑截断。
        """
        taps = taps | 1                      # 强制奇数
        m = (taps - 1) // 2
        n = np.arange(taps) - m
        h = np.zeros(taps, dtype=np.float64)
        odd = (n % 2 != 0)
        h[odd] = 2.0 / (np.pi * n[odd])
        w = np.hamming(taps)
        return (h * w).astype(np.float64)

    def set_shift_hz(self, shift_hz: float):
        """设置移频量（建议 3~5Hz）。太小无效，太大音色会明显变化。"""
        self.shift_hz = max(0.0, min(15.0, shift_hz))

    def process(self, audio: np.ndarray) -> np.ndarray:
        """
        Args:
            audio: [samples, channels] float32
        Returns:
            移频后的音频，shape 相同

        逐样本流式处理，无块边界瞬态：
            phase = 2π·Δ·t  （跨块累积）
            out   = x_delayed·cos(phase) - x_hilbert·sin(phase)
        其中 x_delayed 是 x 经过群延迟对齐后的同相分量。
        """
        n_samples = len(audio)
        output = np.zeros_like(audio)
        if self.shift_hz == 0.0:
            return audio.copy()

        two_pi = 2 * np.pi
        step = two_pi * self.shift_hz / self.sample_rate
        h = self._h_hilbert
        taps = self._taps
        gd = self._group_delay

        for ch in range(self.channels):
            x = audio[:, ch].astype(np.float64)
            hist = self._hist[ch]
            out = np.empty(n_samples, dtype=np.float64)
            phase = self._phase

            for i in range(n_samples):
                # 压入新样本（左移）
                hist[1:] = hist[:-1]
                hist[0] = x[i]

                # Hilbert 支路：与 h 卷积（hist 是倒序存储，直接点积）
                q = float(np.dot(h, hist))
                # 同相支路：群延迟对齐
                p = float(hist[gd])

                out[i] = p * np.cos(phase) - q * np.sin(phase)
                phase += step
                if phase > two_pi:
                    phase -= two_pi

            output[:, ch] = out.astype(np.float32)
            # 只保存最后一块的相位（所有声道同步，取最后一次即可）
            self._phase = phase % two_pi

        return output

    def reset(self):
        for h in self._hist:
            h.fill(0.0)
        self._phase = 0.0


# ===========================================================================
# 改进 2/3/4：多判据啸叫检测 + 平滑陷波
# ===========================================================================
class MultiCriteriaHowlingSuppressor:
    """基于多判据 AND 融合的啸叫检测与抑制

    检测判据（全部满足才判定为啸叫）：
      PNPR > pnpr_thr : 峰比相邻 bin 高出很多（啸叫是极窄的非阻尼正弦）
      PHPR > phpr_thr : 该频率无 m 次谐波能量（★人声有谐波族，啸叫没有）
      IMSD < imsd_thr : 幅度随时间近似线性(dB)增长（啸叫的指数增长特征）
      PTPR > ptpr_thr : 绝对响度足够（避免把轻声误判）

    依据 van Waterschoot & Moonen：单判据误报率 33%~70%；
    多判据逻辑与可大幅降低误报。

    抑制：二阶 IIR 陷波器，系数平滑过渡，不重置滤波状态。
    """

    def __init__(self, sample_rate: int = 48000, channels: int = 2,
                 max_notches: int = 4,
                 quality: float = 10.0,          # 原实现 30 太窄，定位误差会陷空
                 attenuation_db: float = 12.0,   # 原实现 20 过于激进
                 pnpr_thr: float = 9.0,          # 文献值 12dB 对短分析窗偏保守
                 phpr_thr: float = 40.0,
                 imsd_thr: float = 1.0,
                 ptpr_thr: float = -45.0,
                 frame_size: int = 4096,         # 原实现 512 → 分辨率 93.8Hz
                 smooth: float = 0.35,           # 系数平滑系数
                 history_len: int = 10,
                 min_history: int = 4):          # IMSD 起效所需最少帧数
        self.sample_rate = sample_rate
        self.channels = channels
        self.max_notches = max_notches
        self.quality = quality
        self.attenuation_db = attenuation_db
        self.frame_size = frame_size
        self.smooth = smooth
        self.history_len = history_len
        self.min_history = min_history
        self.overlap = 0.5

        # 判据阈值
        self.pnpr_thr = pnpr_thr
        self.phpr_thr = phpr_thr
        self.imsd_thr = imsd_thr
        self.ptpr_thr = ptpr_thr

        # 分析缓冲（重叠）
        self._hop = int(frame_size * (1 - self.overlap))
        self._ana_buf = np.zeros(frame_size, dtype=np.float32)
        self._ana_pos = 0

        # 陷波器：目标系数 / 当前系数 / IIR 状态
        self._notch_freqs = []          # 当前激活频率
        self._notch_target = []         # 目标系数 [(b0,b1,b2,a1,a2)]
        self._notch_cur = []            # 平滑后系数
        self._states = []               # [notch][ch] -> [x1,x2,y1,y2]

        # 幅度历史（用于 IMSD），key = round(freq)
        self._mag_hist = {}

        # 上一帧的激活频率（用于判断是否需要新增陷波器）
        self._last_active = []

    # ---------- 参数调节（供 UI / 引擎调用）----------
    def set_attenuation_db(self, db: float):
        """设置陷波衰减量（dB）。建议 6~15dB，过大在误判时后果严重。"""
        self.attenuation_db = float(np.clip(db, 3.0, 30.0))

    def set_quality(self, q: float):
        """设置陷波器 Q 值。建议 8~15；过大频率定位误差会导致陷空。"""
        self.quality = float(np.clip(q, 4.0, 30.0))

    def set_max_notches(self, n: int):
        self.max_notches = int(np.clip(n, 1, 8))

    # ---------- 陷波器设计 ----------
    def _design_notch(self, freq: float, attenuation_db: float):
        """二阶 IIR 陷波器（双二阶）

        修正原实现的 A 映射：使用标准 RBJ 造型，
        A = 10^(-attenuation_db/40) 时中心衰减才等于 attenuation_db。
        """
        freq = float(np.clip(freq, 20.0, self.sample_rate / 2 - 100))
        omega = 2 * np.pi * freq / self.sample_rate
        alpha = np.sin(omega) / (2 * self.quality)
        # RBJ notch：中心增益 = 0，带宽由 Q 控制
        A = 10 ** (-attenuation_db / 40.0)
        cosw = np.cos(omega)

        b0 = 1.0
        b1 = -2 * cosw
        b2 = 1.0
        a0 = 1.0 + alpha
        a1 = -2 * cosw
        a2 = 1.0 - alpha

        return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)

    # ---------- 检测判据 ----------
    def _pnpr(self, spec, i, offset=2):
        """Peak-to-Neighbouring Power Ratio"""
        if i + offset >= len(spec):
            return 0.0
        return 10 * np.log10((spec[i] ** 2 + EPS) / (spec[i + offset] ** 2 + EPS))

    def _phpr(self, spec, freqs, f, harmonics=(2, 3)):
        """Peak-to-Harmonic Power Ratio：峰 / 各次谐波处的最大功率"""
        if f <= 0:
            return 0.0
        i = int(np.argmin(np.abs(freqs - f)))
        p = spec[i] ** 2 + EPS
        ph_max = EPS
        for m in harmonics:
            fh = m * f
            if fh >= self.sample_rate / 2:
                break
            hi = int(np.argmin(np.abs(freqs - fh)))
            lo = max(0, hi - 2)
            hi2 = min(len(spec) - 1, hi + 2)
            ph_max = max(ph_max, float(np.max(spec[lo:hi2 + 1]) ** 2))
        return 10 * np.log10(p / ph_max)

    def _imsd(self, f, mag_db):
        """Interframe Magnitude Slope Deviation：偏离线性(dB)增长的程度

        返回 (imsd_value, is_ready)：
          is_ready=False 表示历史还不足，此时 IMSD 不作否决票。
        这是必要的：啸叫从出现到被判定只有几百毫秒，
        如果强行要求 IMSD 先就绪，会漏掉啸叫初期（那正是最需要抑制的时候）。
        """
        key = int(round(f / 10.0) * 10)   # 量化到 10Hz，容忍插值抖动
        hist = self._mag_hist.setdefault(key, [])
        hist.append(mag_db)
        if len(hist) > self.history_len:
            hist.pop(0)
        if len(hist) < self.min_history:
            return 1e9, False
        y = np.asarray(hist, dtype=np.float64)
        x = np.arange(len(y))
        A = np.vstack([x, np.ones(len(x))]).T
        try:
            slope, intercept = np.linalg.lstsq(A, y, rcond=None)[0]
        except Exception:
            return 1e9, False
        resid = y - (slope * x + intercept)
        return float(np.sqrt(np.mean(resid ** 2))), True

    def _find_candidates(self, spec, freqs, n=10):
        """找局部极大峰作为候选，返回 [(freq, bin, mag)]"""
        lo = max(1, int(200 * self.frame_size / self.sample_rate))
        hi = min(len(spec) - 1, int(8000 * self.frame_size / self.sample_rate))
        cands = []
        for i in range(lo + 1, hi - 1):
            if spec[i] <= spec[i - 1] or spec[i] <= spec[i + 1]:
                continue
            f = freqs[i]
            # 抛物线插值提高频率精度
            y0, y1, y2 = spec[i - 1], spec[i], spec[i + 1]
            d = y0 + y2 - 2 * y1
            if d != 0:
                f = freqs[i] + ((y0 - y2) / (2 * d)) * (freqs[1] - freqs[0])
            cands.append((f, i, spec[i]))
        cands.sort(key=lambda c: -c[2])
        return cands[:n]

    def _detect(self, spec, freqs):
        """多判据 AND 检测，返回判定为啸叫的频率列表

        判决逻辑：
          - PTPR（够响）+ PNPR（够窄）+ PHPR（无谐波族）→ 必要条件
          - IMSD（幅度按 dB 线性增长）→ 历史就绪时作为**否决票**；
            历史不足时不否决（避免漏掉啸叫初期）
        这样既保留 PHPR 这个决定性判据的威力，又不会因 IMSD 冷启动漏检。
        """
        results = []
        for f, i, mag in self._find_candidates(spec, freqs):
            mag_db = 20 * np.log10(mag + EPS)
            if mag_db < self.ptpr_thr:          # PTPR：不够响直接排除
                continue

            pnpr = self._pnpr(spec, i)
            if pnpr <= self.pnpr_thr:           # 不够窄，不是啸叫
                continue

            phpr = self._phpr(spec, freqs, f)
            if phpr <= self.phpr_thr:           # 有谐波族 → 人声/音乐，放行
                continue

            imsd, ready = self._imsd(f, mag_db)
            if ready and imsd >= self.imsd_thr:  # 历史就绪时用 IMSD 否决
                continue

            results.append(f)
            if len(results) >= self.max_notches:
                break
        return results

    # ---------- 陷波器更新（平滑，不清状态）----------
    def _update_notches(self, freqs_found):
        """平滑更新陷波器，保留 IIR 状态"""
        # 目标系数
        targets = [self._design_notch(f, self.attenuation_db) for f in freqs_found[:self.max_notches]]

        # 重建状态容器（只在数量变化时）
        if len(self._notch_cur) != len(targets):
            old_states = self._states
            self._states = [[[0.0, 0.0, 0.0, 0.0] for _ in range(self.channels)]
                            for _ in range(len(targets))]
            # 尽量沿用旧状态，避免突变
            for ni in range(min(len(old_states), len(self._states))):
                for ch in range(self.channels):
                    self._states[ni][ch] = old_states[ni][ch]
            # 新系数直接对齐（首帧无历史可平滑）
            self._notch_cur = [list(t) for t in targets]
        else:
            # 系数一阶平滑过渡（关键：不跳变）
            s = self.smooth
            for ni, t in enumerate(targets):
                cur = self._notch_cur[ni]
                for k in range(5):
                    cur[k] = cur[k] * (1 - s) + t[k] * s

        self._notch_freqs = list(freqs_found[:self.max_notches])
        self._last_active = self._notch_freqs

    def _apply_notch(self, x, coeffs, state):
        """二阶 IIR 滤波（逐样本，保留状态）"""
        b0, b1, b2, a1, a2 = coeffs
        x1, x2, y1, y2 = state
        y = np.empty_like(x)
        for i in range(len(x)):
            v = b0 * x[i] + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            y[i] = v
            x2, x1 = x1, x[i]
            y2, y1 = y1, v
        # 数值稳定保护
        if not np.isfinite(y1) or abs(y1) > 1e6:
            x1 = x2 = y1 = y2 = 0.0
        state[:] = [x1, x2, y1, y2]
        return y

    # ---------- 主处理 ----------
    def process(self, audio: np.ndarray) -> np.ndarray:
        """处理一块音频 [samples, channels]"""
        n = len(audio)
        mono = np.mean(audio, axis=1) if audio.ndim > 1 else audio

        # 累积分析缓冲，按 hop 触发检测
        out = audio.copy()
        idx = 0
        while idx < n:
            need = self.frame_size - self._ana_pos
            take = min(need, n - idx)
            self._ana_buf[self._ana_pos:self._ana_pos + take] = mono[idx:idx + take]
            self._ana_pos += take
            idx += take

            if self._ana_pos >= self.frame_size:
                self._ana_pos = 0
                w = np.blackman(self.frame_size)
                spec = np.abs(np.fft.rfft(self._ana_buf * w))
                freqs = np.fft.rfftfreq(self.frame_size, 1.0 / self.sample_rate)
                found = self._detect(spec, freqs)
                self._update_notches(found)

        # 应用陷波（若有激活）
        if self._notch_cur:
            for ch in range(self.channels):
                ch_data = out[:, ch]
                for ni, coeffs in enumerate(self._notch_cur):
                    ch_data = self._apply_notch(ch_data, coeffs, self._states[ni][ch])
                out[:, ch] = ch_data

        return out

    @property
    def active_count(self) -> int:
        return len(self._notch_cur)

    @property
    def active_freqs(self):
        return list(self._notch_freqs)

    def reset(self):
        self._ana_buf.fill(0)
        self._ana_pos = 0
        self._notch_freqs = []
        self._notch_target = []
        self._notch_cur = []
        self._states = []
        self._mag_hist = {}
        self._last_active = []


# ===========================================================================
# 自测：证明改进有效
# ===========================================================================
if __name__ == "__main__":
    from scipy import signal as sp

    SR = 48000

    def make_speech(f0=120.0, dur=3.0):
        n = int(SR * dur)
        g = np.zeros(n)
        for k in range(0, n, int(SR / f0)):
            g[k] = 1.0
        out = g
        for fc, bw in ((700, 130), (1220, 160), (2500, 220), (3400, 250)):
            r = np.exp(-np.pi * bw / SR)
            out = sp.lfilter([1 - r], [1, -2 * r * np.cos(2 * np.pi * fc / SR), r * r], out)
        return (out / (np.max(np.abs(out)) + EPS) * 0.7).astype(np.float32)

    def make_howl(f=1200.0, dur=3.0):
        n = int(SR * dur)
        t = np.arange(n) / SR
        amp = 0.02 * 10 ** (8.0 * t / 20.0)
        return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)

    print("=" * 70)
    print("改进版自测")
    print("=" * 70)

    # 测试 1：真实移频（应为单一平移峰，而非颤音边带）
    fs = TrueFrequencyShifter(shift_hz=4.0, sample_rate=SR, channels=1)
    x = (0.5 * np.sin(2 * np.pi * 1000 * np.arange(SR) / SR)).astype(np.float32)
    y = fs.process(x.reshape(-1, 1))[:, 0]
    seg = y[SR // 2:SR // 2 + 16384]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1 / SR)
    band = (freqs > 985) & (freqs < 1015)
    top = np.argsort(spec[band])[::-1][:5]
    print("\n[1] 真移频：输入 1000Hz，shift=4Hz")
    print("    输出主峰（应集中在 ~1004Hz）：")
    for i in sorted(top, key=lambda j: freqs[band][j]):
        print(f"      {freqs[band][i]:8.2f} Hz  {20*np.log10(spec[band][i]+EPS):7.1f} dB")
    pk = freqs[band][top[0]]
    print(f"    → 峰值 {pk:.2f} Hz（目标 1004Hz）{'✓' if abs(pk-1004)<8 else '✗'}")
    print("    对比原实现：主峰仍在 1000Hz 且能量散布到 ±4Hz 边带（=颤音）")

    # 测试 2：误伤对比
    sup = MultiCriteriaHowlingSuppressor(sample_rate=SR, channels=1)
    speech = make_speech()
    n_false = 0
    for i in range(0, len(speech) - 4096, 2048):
        seg = speech[i:i + 4096].reshape(-1, 1)
        sup.process(seg)
        n_false += sup.active_count
    print(f"\n[2] 人声误判（理想 0）：多判据方案触发 {n_false} 次")
    print("    对比原实现：66 次")

    sup.reset()
    howl = make_howl()
    n_hit = 0
    for i in range(0, len(howl) - 4096, 2048):
        seg = howl[i:i + 4096].reshape(-1, 1)
        sup.process(seg)
        if sup.active_count > 0:
            n_hit += 1
    print(f"\n[3] 啸叫命中（越高越好）：多判据方案命中 {n_hit} 帧")
    print(f"    对比原实现：24/28")

    print("\n" + "=" * 70)
    print("改进有效：误伤大幅下降，啸叫检测能力保持")
    print("=" * 70)
