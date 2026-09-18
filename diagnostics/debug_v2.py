"""调试：为什么纯啸叫信号没被检测到"""
import numpy as np
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audio_router.services.howling_suppressor_v2 import MultiCriteriaHowlingSuppressor, EPS

SR = 48000
FRAME = 4096


def make_howl(f=1200.0, dur=2.0):
    n = int(SR * dur)
    t = np.arange(n) / SR
    amp = 0.02 * 10 ** (8.0 * t / 20.0)
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


sup = MultiCriteriaHowlingSuppressor(sample_rate=SR, channels=1)
howl = make_howl()

# 直接对一帧做检测，打印每个判据的数值
pos = int(1.0 * SR)          # 取 1 秒处（幅度已增长）
seg = howl[pos:pos + FRAME]
w = np.blackman(FRAME)
spec = np.abs(np.fft.rfft(seg * w))
freqs = np.fft.rfftfreq(FRAME, 1.0 / SR)

print(f"信号峰值幅度: {np.max(np.abs(seg)):.4f}")
print(f"帧长: {FRAME}, 分辨率: {SR/FRAME:.1f} Hz\n")

cands = sup._find_candidates(spec, freqs, n=5)
print(f"{'频率(Hz)':>10} {'幅度(dB)':>10} {'PNPR':>8} {'PHPR':>8} {'IMSD':>10} {'就绪':>6} {'判定':>6}")
for f, i, mag in cands:
    mag_db = 20 * np.log10(mag + EPS)
    pnpr = sup._pnpr(spec, i)
    phpr = sup._phpr(spec, freqs, f)
    # 注意：_imsd 返回 (值, 是否就绪)。历史不足时就绪=False，
    # 此时 IMSD 不作否决票（改为「就绪时才否决」是本版的关键修正）。
    imsd, ready = sup._imsd(f, mag_db)
    ok = (mag_db >= sup.ptpr_thr
          and pnpr > sup.pnpr_thr
          and phpr > sup.phpr_thr
          and not (ready and imsd >= sup.imsd_thr))
    imsd_txt = f"{imsd:10.3f}" if ready else f"{'--':>10}"
    print(f"{f:10.1f} {mag_db:10.1f} {pnpr:8.2f} {phpr:8.2f} {imsd_txt} "
          f"{('是' if ready else '否'):>6} {'啸叫' if ok else '-':>6}")

print(f"\n阈值: PNPR>{sup.pnpr_thr}  PHPR>{sup.phpr_thr}  IMSD<{sup.imsd_thr}  PTPR>{sup.ptpr_thr}")
print("\n诊断：")
print("  - IMSD 需要 ≥4 帧历史才能计算；历史不足时返回 (1e9, ready=False)")
print("  - 本版已修正：历史不足时 IMSD 不参与否决，避免漏掉啸叫初期")
print("  - 候选频率每帧抖动时历史会对不上，故 _imsd 内部把 key 量化到 10Hz")
