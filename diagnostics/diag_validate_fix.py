"""
验证「多判据 AND 融合」方案能否解决误伤问题
对比：
  方案 A（现有）：仅 PNPR 单判据，阈值 6dB
  方案 B（改进）：PNPR + PHPR + IMSD 三判据逻辑与

学术依据：van Waterschoot & Moonen, AES 126th Convention, Preprint 7752
  单判据误报率：PTPR 70%, PAPR 63%, PHPR 37%, PNPR 33%, IPMP 54%, IMSD 40%
  多判据逻辑与可显著降低误报率
"""
import sys, os
import numpy as np
from scipy import signal as sp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SR = 48000
FRAME = 512


def make_speech(f0=120.0, dur=2.0, sr=SR):
    """声门脉冲 + 共振峰，模拟真实人声"""
    n = int(sr * dur)
    glottal = np.zeros(n)
    period = int(sr / f0)
    for k in range(0, n, period):
        glottal[k] = 1.0
    out = glottal
    for fc, bw in ((700, 130), (1220, 160), (2500, 220), (3400, 250)):
        r = np.exp(-np.pi * bw / sr)
        theta = 2 * np.pi * fc / sr
        out = sp.lfilter([1 - r], [1, -2 * r * np.cos(theta), r * r], out)
    out = out / (np.max(np.abs(out)) + 1e-9) * 0.7
    return out.astype(np.float32)


def make_howling(freq=1200.0, dur=2.0, sr=SR, growth_db_per_s=8.0):
    """模拟啸叫：单频正弦，能量随时间指数增长(即 dB 线性增长)"""
    n = int(sr * dur)
    t = np.arange(n) / sr
    amp = 0.02 * 10 ** (growth_db_per_s * t / 20.0)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class Detector:
    """多判据啸叫检测器"""

    def __init__(self, sr=SR, frame=FRAME, threshold=6.0,
                 phpr_thr=40.0, pnpr_thr=12.0, imsd_thr=1.0,
                 use_phpr=True, use_pnpr=True, use_imsd=True,
                 history=8):
        self.sr = sr
        self.frame = frame
        self.threshold = threshold
        self.phpr_thr = phpr_thr
        self.pnpr_thr = pnpr_thr
        self.imsd_thr = imsd_thr
        self.use_phpr = use_phpr
        self.use_pnpr = use_pnpr
        self.use_imsd = use_imsd
        self.history = history
        # 记录候选频率在历史帧中的幅度(dB)
        self._track = {}

    def _spectrum(self, seg):
        w = np.blackman(len(seg))
        return np.abs(np.fft.rfft(seg * w))

    def _candidates_peak_ratio(self, spec, freqs, n_cand=10):
        """候选：局部极大，按幅度取前 n_cand"""
        cands = []
        lo = max(1, int(200 * self.frame / self.sr))
        hi = min(len(spec) - 1, int(8000 * self.frame / self.sr))
        for i in range(lo + 1, hi - 1):
            if spec[i] <= spec[i - 1] or spec[i] <= spec[i + 1]:
                continue
            f = freqs[i]
            if i > 0 and i < len(spec) - 1:
                y0, y1, y2 = spec[i - 1], spec[i], spec[i + 1]
                d = y0 + y2 - 2 * y1
                if d != 0:
                    f = freqs[i] + ((y0 - y2) / (2 * d)) * (freqs[1] - freqs[0])
            cands.append((f, spec[i], i))
        cands.sort(key=lambda x: -x[1])
        return cands[:n_cand]

    def _pnpr(self, spec, i):
        """Peak-to-Neighbouring Power Ratio：峰 / 相邻 bin"""
        if i + 2 >= len(spec):
            return 0.0
        p = spec[i] ** 2
        nb = max(spec[i + 2] ** 2, 1e-12)
        return 10 * np.log10(p / nb)

    def _phpr(self, spec, freqs, f, m=2):
        """Peak-to-Harmonic Power Ratio：峰 / m 次谐波处的功率
        啸叫无谐波结构 -> PHPR 高；语音有谐波 -> PHPR 低
        """
        fh = m * f
        if fh >= self.sr / 2:
            return 0.0
        pi = np.argmin(np.abs(freqs - f))
        hi = np.argmin(np.abs(freqs - fh))
        # 取谐波附近 ±1 bin 的最大值，避免因分辨率错过
        lo_i = max(0, hi - 1)
        hi_i = min(len(spec) - 1, hi + 1)
        p = spec[pi] ** 2
        ph = max(spec[lo_i:hi_i + 1].max() ** 2, 1e-12)
        return 10 * np.log10(p / ph)

    def _imsd(self, f, mag_db):
        """Interframe Magnitude Slope Deviation
        啸叫幅度按 dB 线性增长；语音不会。
        返回 |实际斜率 - 最优线性斜率| 的偏差(dB)；小=像啸叫
        """
        key = round(f)
        hist = self._track.setdefault(key, [])
        hist.append(mag_db)
        if len(hist) > self.history:
            hist.pop(0)
        if len(hist) < 5:
            return 999.0
        y = np.array(hist)
        x = np.arange(len(y))
        # 最小二乘拟合直线，计算残差(RMS)
        A = np.vstack([x, np.ones(len(x))]).T
        try:
            slope, intercept = np.linalg.lstsq(A, y, rcond=None)[0]
        except Exception:
            return 999.0
        resid = y - (slope * x + intercept)
        return float(np.sqrt(np.mean(resid ** 2)))

    def detect(self, seg):
        spec = self._spectrum(seg)
        freqs = np.fft.rfftfreq(len(seg), 1.0 / self.sr)
        cands = self._candidates_peak_ratio(spec, freqs)
        results = []
        for f, mag, i in cands:
            mag_db = 20 * np.log10(mag + 1e-12)
            # 邻域中位数比较（现有逻辑）
            nb = spec[max(0, i - 10):min(len(spec), i + 11)]
            med = np.median(nb)
            db_diff = 20 * np.log10(mag / med) if med > 1e-8 else 0.0

            flags = {}
            flags['PNPR'] = self._pnpr(spec, i) > self.pnpr_thr
            flags['PHPR'] = self._phpr(spec, freqs, f) > self.phpr_thr
            flags['IMSD'] = self._imsd(f, mag_db) < self.imsd_thr
            # 现有单判据
            flags['PNPR_only(old)'] = db_diff > self.threshold

            active = []
            if self.use_pnpr:
                active.append(flags['PNPR'])
            if self.use_phpr:
                active.append(flags['PHPR'])
            if self.use_imsd:
                active.append(flags['IMSD'])
            flags['AND_decision'] = all(active) if active else False
            results.append((f, db_diff, flags))
        return results


def run_case(name, signal, label):
    print(f"\n  --- {name} ---")
    det = Detector()
    pos = 0
    n_old = 0      # 现有单判据判定为啸叫的次数
    n_new = 0      # 多判据 AND 判定为啸叫的次数
    blocks = 0
    while pos + FRAME <= len(signal):
        seg = signal[pos:pos + FRAME]
        pos += FRAME
        blocks += 1
        if blocks % 10:
            continue
        res = det.detect(seg)
        for f, db, flags in res[:3]:
            if flags['PNPR_only(old)']:
                n_old += 1
            if flags['AND_decision']:
                n_new += 1
    print(f"    {label}: 现有单判据判定 {n_old} 次 | 多判据AND判定 {n_new} 次")


print("=" * 76)
print("多判据 AND 融合 有效性验证")
print("=" * 76)

speech = make_speech(f0=120.0, dur=3.0)
howl = make_howling(freq=1200.0, dur=3.0)

run_case("真实人声（不应判为啸叫 = 理想值 0）", speech, "误判")
run_case("真实啸叫（应判为啸叫 = 理想值高）", howl, "命中")

print("""
============================================================================
结论
============================================================================
""")

# 详细看一个语音帧的判据数值
det = Detector()
seg = speech[10000:10000 + FRAME]
res = det.detect(seg)
print("  对某一语音帧，各候选峰的判据数值：")
print(f"  {'频率(Hz)':>10} {'PNPR(dB)':>10} {'PHPR(dB)':>10} {'IMSD(dB)':>10}  "
      f"{'旧判据':>8} {'新AND':>8}")
for f, db, flags in res[:6]:
    print(f"  {f:10.1f} {flags.get('PNPR',0):10.1f} {flags.get('PHPR',0):10.1f} "
          f"{flags.get('IMSD',0):10.1f}  "
          f"{'啸叫' if flags['PNPR_only(old)'] else '  -':>8} "
          f"{'啸叫' if flags['AND_decision'] else '  -':>8}")

print("""
  判据说明与阈值（参考 van Waterschoot & Moonen）：
    PNPR > 12 dB : 峰比相邻 bin 高出很多（啸叫是极窄的非阻尼正弦）
    PHPR > 40 dB : 该频率处没有 m 次谐波能量（语音一定有谐波，啸叫没有）
    IMSD < 1 dB  : 幅度随时间呈线性(dB)增长（啸叫的指数增长特征）

  关键：PHPR 是区分「人声谐波」与「啸叫」的决定性判据。
  因为人声的第 N 次谐波必然伴随 2N、3N 次谐波，
  而啸叫是孤立单频，没有谐波族。
  现有实现完全没有用这个判据，所以必然误伤人声。
""")
