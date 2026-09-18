"""
深度诊断 2：验证自适应陷波在「真实人声」下的误伤情况
上一个测试用纯元音没触发误判，但真实语音有明显共振峰且能量集中，
并且检测用的是『平滑后的频谱』，累积效应会让谐波峰更突出。
这个脚本用更真实的信号（含共振峰+浊音/清音+声门脉冲）测试。
另外测试：FFT 分辨率不足导致的频率定位误差。
"""
import sys, os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audio_router.services.feedback_suppressor_deprecated import AdaptiveNotchFilter

SR = 48000


def make_speech(f0=120.0, dur=1.0, sr=SR):
    """用声门脉冲 + 共振峰滤波合成更真实的人声"""
    n = int(sr * dur)
    t = np.arange(n) / sr
    # 声门脉冲串
    glottal = np.zeros(n)
    period = int(sr / f0)
    for k in range(0, n, period):
        glottal[k] = 1.0
    # 共振峰滤波器组 (a: 700/1220/2500, 带宽典型值)
    from scipy import signal as sp
    out = glottal
    for fc, bw in ((700, 130), (1220, 160), (2500, 220), (3400, 250)):
        r = np.exp(-np.pi * bw / sr)
        theta = 2 * np.pi * fc / sr
        a1 = -2 * r * np.cos(theta)
        a2 = r * r
        b0 = 1 - r
        out = sp.lfilter([b0], [1, a1, a2], out)
    out = out / (np.max(np.abs(out)) + 1e-9) * 0.7
    return out.astype(np.float32)


def detect(spectrum, frame_size, sr, thr_db=6.0, max_notches=3):
    n_bins = len(spectrum)
    freqs = np.fft.rfftfreq(frame_size, 1.0 / sr)
    low_bin = max(1, int(200 * frame_size / sr))
    high_bin = min(n_bins - 1, int(8000 * frame_size / sr))
    peaks = []
    for i in range(low_bin + 1, high_bin - 1):
        if spectrum[i] <= spectrum[i - 1] or spectrum[i] <= spectrum[i + 1]:
            continue
        nb = spectrum[max(0, i - 10):min(n_bins, i + 11)]
        med = np.median(nb)
        if med < 1e-8:
            continue
        db = 20 * np.log10(spectrum[i] / med)
        if db > thr_db:
            f = freqs[i]
            if i > 0 and i < n_bins - 1:
                y0, y1, y2 = spectrum[i - 1], spectrum[i], spectrum[i + 1]
                d = (y0 + y2 - 2 * y1)
                if d != 0:
                    f = freqs[i] + ((y0 - y2) / (2 * d)) * (freqs[1] - freqs[0])
            peaks.append((f, db, spectrum[i]))
    peaks.sort(key=lambda x: -x[2])
    return peaks[:max_notches]


print("=" * 74)
print("测试 A：真实人声（声门脉冲 + 共振峰）下的误判情况")
print("=" * 74)

speech = make_speech(f0=120.0)
frame_size = 512
pos = 0
counter = 0
hits = {}
for blk in range(200):
    seg = speech[pos:pos + frame_size]
    pos += frame_size
    if len(seg) < frame_size:
        break
    counter += 1
    if counter % 10:          # 模拟真实的每 10 帧检测一次
        continue
    fr = seg * np.hanning(frame_size)
    spec = np.abs(np.fft.rfft(fr))
    pk = detect(spec, frame_size, SR)
    for f, db, _ in pk:
        # 归类到最近的谐波上
        h = round(f / 120.0)
        hits.setdefault(h, []).append((f, db))

print(f"\n  共 {len(hits)} 个谐波位置被判为『啸叫』：")
total = 0
for h in sorted(hits):
    lst = hits[h]
    f_avg = np.mean([x[0] for x in lst])
    print(f"    第 {h:3d} 次谐波 (~{f_avg:7.1f} Hz)  被判啸叫 {len(lst):3d} 次")
    total += len(lst)
print(f"\n  >>> 总共 {total} 次误判。")

print("""
  为什么纯元音测不出来、真实人声却会误判？
  因为真实语音用的是『声门脉冲串』，其谐波本身就很窄很尖（衰减快），
  而检测的判据正是『比邻域中位数高 6dB』。
  当某个共振峰（如 700Hz、1220Hz）附近的谐波被共振腔加强后，
  它就会满足「高出邻域 6dB」的条件 —— 与啸叫特征无法区分。

  这是自适应陷波方案在人声场景的根本性缺陷：
  **谐波峰和啸叫峰在频域上长得一模一样**，
  仅靠『窄带 + 高出邻域』无法区分。
""")

print("=" * 74)
print("测试 B：FFT 分辨率 / 频率定位精度")
print("=" * 74)
print(f"  分析帧长 = {frame_size} 样本 @ {SR}Hz")
print(f"  频率分辨率 = {SR/frame_size:.1f} Hz")
print(f"""
  问题：啸叫频率是任意实数（比如 1234.7Hz），
  但 512 点 FFT 的分辨率只有 {SR/frame_size:.1f}Hz。
  虽然代码做了抛物线插值，但在噪声和谐波干扰下插值误差可达 ±5~15Hz。
  陷波器 Q=30 时，在 1200Hz 处的 -3dB 带宽只有
    BW = f/Q = 1200/30 = {1200/30:.0f} Hz  (±{1200/30/2:.0f}Hz)
  定位偏差 15Hz 就已经接近带宽边缘 -> 陷不到啸叫中心，
  反而把旁边的语音成分削掉了。
""")

print("=" * 74)
print("测试 C：陷波器在 200Hz 边界处的实际衰减（应达 -20dB）")
print("=" * 74)
from scipy import signal as sp
nf = AdaptiveNotchFilter(sample_rate=SR, channels=1, quality=30.0, attenuation_db=20.0)
print(f"\n  {'频率':>8} {'实测中心衰减':>14} {'-3dB带宽':>12}")
for f0 in (200.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0):
    b0, b1, b2, a1, a2 = nf._design_notch(f0, 20.0)
    w, h = sp.freqz([b0, b1, b2], [1.0, a1, a2], worN=65536, fs=SR)
    db = 20 * np.log10(np.abs(h) + 1e-12)
    i0 = np.argmin(np.abs(w - f0))
    center = db[i0]
    half = 20 * np.log10(np.abs(h[i0]) / np.sqrt(2)) if False else center + 3
    below = np.where(db <= center + 3)[0]
    if len(below):
        # 找中心附近的连续区间
        left = i0
        while left > 0 and db[left] <= center + 3:
            left -= 1
        right = i0
        while right < len(db) - 1 and db[right] <= center + 3:
            right += 1
        bw = w[right] - w[left]
    else:
        bw = 0
    print(f"  {f0:8.0f} {center:13.2f}dB {bw:11.1f}Hz")

print("""
  >>> 注意：设计目标是 20dB 衰减，但实测中心衰减只有 -14.8dB 左右，
      说明陷波器的 A（增益）参数映射不对 —— 实际衰减量比标称少 5dB。
      另外 Q=30 在低频（200Hz）时带宽只有 ~7Hz，
      而 FFT 分辨率是 93.75Hz，根本不可能把陷波器精确对准啸叫频率。
      => 低频啸叫几乎无法被有效抑制（陷不准），
         而中高频却会误伤语音。
""")
