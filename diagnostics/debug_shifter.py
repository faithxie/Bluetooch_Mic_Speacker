"""调查 TrueFrequencyShifter 的频谱异常：质心从 1008Hz 跳到 4904Hz"""
import sys, os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audio_router.services.howling_suppressor_v2 import TrueFrequencyShifter
from scipy import signal as sp

SR = 48000
EPS = 1e-12


def make_speech(f0=120.0, dur=1.0, sr=SR):
    n = int(sr * dur)
    g = np.zeros(n)
    for k in range(0, n, int(sr / f0)):
        g[k] = 1.0
    out = g
    for fc, bw in ((700, 130), (1220, 160), (2500, 220), (3400, 250)):
        r = np.exp(-np.pi * bw / sr)
        out = sp.lfilter([1 - r], [1, -2 * r * np.cos(2 * np.pi * fc / sr), r * r], out)
    return (out / (np.max(np.abs(out)) + EPS) * 0.6).astype(np.float32)


x = make_speech(dur=1.0)
print(f"输入时长 {len(x)} 样本, 峰值 {np.max(np.abs(x)):.4f}")

fs = TrueFrequencyShifter(shift_hz=4.0, sample_rate=SR, channels=1)

# 逐块处理，检查每块的输出峰值和能量
block = 512
print(f"\n{'块号':>5} {'输入峰值':>10} {'输出峰值':>10} {'输出RMS':>10}")
outs = []
for bi, i in enumerate(range(0, len(x) - block, block)):
    seg = x[i:i + block].reshape(-1, 1)
    out = fs.process(seg)
    outs.append(out[:, 0].copy())
    if bi < 8 or bi % 20 == 0:
        print(f"{bi:5d} {np.max(np.abs(seg)):10.4f} "
              f"{np.max(np.abs(out)):10.4f} {np.sqrt(np.mean(out**2)):10.5f}")

y = np.concatenate(outs)
print(f"\n全局: 输入峰值 {np.max(np.abs(x[:len(y)])):.4f} 输出峰值 {np.max(np.abs(y)):.4f}")

# 检查前 20 个样本（看初始瞬态）
print(f"\n前 10 个输出样本: {y[:10]}")
print(f"第 500-510 样本: {y[500:510]}")

# 频谱
spec_in = np.abs(np.fft.rfft(x[:len(y)] * np.hanning(len(y))))
spec_out = np.abs(np.fft.rfft(y * np.hanning(len(y))))
freqs = np.fft.rfftfreq(len(y), 1 / SR)

def centroid(s):
    return np.sum(freqs * s) / (np.sum(s) + EPS)

print(f"\n频谱质心: 输入 {centroid(spec_in):.1f} Hz, 输出 {centroid(spec_out):.1f} Hz")

# 各频段能量占比
print(f"\n{'频段':>14} {'输入能量%':>10} {'输出能量%':>10}")
tot_in = np.sum(spec_in**2) + EPS
tot_out = np.sum(spec_out**2) + EPS
for lo, hi in ((0, 500), (500, 2000), (2000, 5000), (5000, 10000), (10000, 24000)):
    m = (freqs >= lo) & (freqs < hi)
    pi = np.sum(spec_in[m]**2) / tot_in * 100
    po = np.sum(spec_out[m]**2) / tot_out * 100
    print(f"{lo:6d}-{hi:6d} {pi:10.2f} {po:10.2f}")

print("\n诊断：")
print("  若高频段能量占比大幅上升 → 块边界不连续产生了宽带瞬态(咔哒声)")
print("  根因怀疑：重叠相加的尾部处理逻辑有误，导致样本丢失/错位")
