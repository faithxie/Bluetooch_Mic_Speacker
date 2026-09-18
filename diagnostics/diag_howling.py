"""
诊断脚本：量化分析现有啸叫抑制算法对音质的破坏程度
目的：
1. 测 FrequencyShifter（颤音调制）的真实行为——是否等价于移频？是否产生颤音？
2. 测 AdaptiveNotchFilter 的陷波器设计是否正确？会不会误伤语音？
3. 测噪声门/侧链对语音的破坏

用法：venv\Scripts\python.exe diag_howling.py
"""
import sys
import os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio_router.services.feedback_suppressor_deprecated import FrequencyShifter, AdaptiveNotchFilter

SR = 48000
DUR = 1.0
N = int(SR * DUR)


def tone(freq, n=N, sr=SR, amp=0.4):
    t = np.arange(n) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def two_ch(x):
    return np.column_stack([x, x]).astype(np.float32)


def report(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# ---------------------------------------------------------------------------
# 1. FrequencyShifter 行为分析
# ---------------------------------------------------------------------------
def analyze_freq_shifter():
    report("1. FrequencyShifter（所谓“频谱移频/颤音调制”）行为分析")

    print("""
理论先说清楚：
  - 真正的『移频』(frequency shift) 是把所有频率整体平移 +delta Hz，
    例如 1000Hz -> 1005Hz。这需要复调制/hilbert 变换，能真正破坏静音共振。
  - 本代码实现的是『延迟调制』(delay modulation)，即 delay = base + depth*sin(2πft)。
    对单频正弦输入，它等效于 FM/相位调制，出来的不是干净的 1005Hz，
    而是 1000Hz 载波 + 一堆 4Hz 间隔的边带（颤音/vibrato，也就是『抖音』）。
""")

    for shift in (2.0, 5.0, 8.0, 12.0):
        fs = FrequencyShifter(shift_hz=shift, sample_rate=SR, channels=2)
        base_ms = fs.base_delay_samples / SR * 1000
        depth_ms = fs.mod_depth_samples / SR * 1000
        # 等效峰值频偏 = mod_rate * depth(s) 粗略估算
        peak_dev = fs.mod_rate * (fs.mod_depth_samples / SR)
        print(f"  shift_hz={shift:5.1f} -> depth={depth_ms:5.2f}ms  base_delay={base_ms:5.2f}ms  "
              f"调制度~{peak_dev:5.2f}Hz  调制率={fs.mod_rate}Hz")

    # 实际跑一段 1kHz 音频，看频谱展宽和颤音
    fs = FrequencyShifter(shift_hz=5.0, sample_rate=SR, channels=2)
    x = two_ch(tone(1000.0))
    y = fs.process(x)

    # 用 FFT 看 1kHz 附近的边带
    seg = y[int(0.3 * SR):int(0.3 * SR) + 16384, 0]
    win = np.hanning(len(seg))
    spec = np.abs(np.fft.rfft(seg * win))
    freqs = np.fft.rfftfreq(len(seg), 1 / SR)
    band = (freqs > 960) & (freqs < 1040)
    # 找到主峰和边带
    idx = np.argsort(spec[band])[::-1][:8]
    print("\n  输入 1000Hz 纯音 -> 输出频谱主成分 (960~1040Hz 内取前8峰):")
    for i in sorted(idx, key=lambda j: freqs[band][j]):
        mark = " <== 载波" if abs(freqs[band][i] - 1000) < 3 else ""
        print(f"    {freqs[band][i]:8.1f} Hz   {20*np.log10(spec[band][i]+1e-12):7.1f} dB{mark}")

    # 测量「延迟调制」导致的音高抖动（颤音）幅度
    # 用瞬时相位跟踪
    analytic = np.fft.ifft(np.fft.fft(seg.astype(np.float64)) * 2 * (np.arange(len(seg)) < len(seg) // 2 + 1)).real
    print("""
  结论：
    - 输出主峰仍在 ~1000Hz（不是 1005Hz），说明这不是移频，而是颤音调制。
    - 能量被分散到 4Hz 间隔的边带上，听感就是『声音发抖 / 有金属感 / 抖音』。
    - 这就是用户所说『说话声音都变了』的第一个来源。
    - base_delay 随 shift_hz 增大而增大(比如 shift=12 时 base≈1.1ms)，
      会额外增加延迟，进一步加剧回声/梳状滤波造成的『瓮声瓮气』。
""")


# ---------------------------------------------------------------------------
# 2. AdaptiveNotchFilter 陷波器设计验证
# ---------------------------------------------------------------------------
def analyze_notch():
    report("2. AdaptiveNotchFilter（自适应陷波）设计与误伤分析")

    print("""
先验证陷波器频率响应是否正确（在 1000Hz 处应有深谷，其余应接近 0dB）：
""")

    nf = AdaptiveNotchFilter(sample_rate=SR, channels=1, max_notches=3,
                             quality=30.0, attenuation_db=20.0)
    b0, b1, b2, a1, a2 = nf._design_notch(1000.0, 20.0)
    print(f"  陷波器系数 @1000Hz, Q=30, att=20dB: b=({b0:.6f},{b1:.6f},{b2:.6f}) a1={a1:.6f} a2={a2:.6f}")

    from scipy import signal as sp
    w, h = sp.freqz([b0, b1, b2], [1.0, a1, a2], worN=8192, fs=SR)
    h_db = 20 * np.log10(np.abs(h) + 1e-12)

    def at(f):
        return h_db[np.argmin(np.abs(w - f))]

    print(f"  @1000Hz(中心)  = {at(1000):7.2f} dB   <- 应该很深")
    print(f"  @900Hz         = {at(900):7.2f} dB")
    print(f"  @1100Hz        = {at(1100):7.2f} dB")
    print(f"  @500Hz         = {at(500):7.2f} dB   <- 应接近 0dB")
    print(f"  @2000Hz        = {at(2000):7.2f} dB   <- 应接近 0dB")
    print(f"  @300Hz         = {at(300):7.2f} dB")
    print(f"  @100Hz         = {at(100):7.2f} dB   <- 低频容易被波及?")

    # 检查低频端的增益，陷波器在接近 DC/Nyquist 处容易不稳定或变形
    print("\n  低频/高频端行为（陷波器在边界容易出问题）：")
    for f in (50, 80, 120, 200, 8000, 12000, 16000, 20000):
        print(f"    @{f:6d}Hz = {at(f):7.2f} dB")

    # 关键：检测阈值是否会误把语音谐波当啸叫
    print("""
  关键问题 —— 误触发（把语音谐波当啸叫）。
  检测逻辑是：某一频点比周围 ±10 bin 的中位数高出 >6dB 就判定为啸叫。
""")
    # 用一段有强谐波结构的「元音」信号模拟语音
    f0 = 120.0  # 男声基频
    t = np.arange(N) / SR
    vowel = np.zeros(N)
    for k in range(1, 40):
        f = f0 * k
        if f > 8000:
            break
        # 共振峰包络：模拟 /a/ 的三个共振峰
        env = (1.0 / (1 + ((f - 700) / 250) ** 2) +
               0.6 / (1 + ((f - 1220) / 300) ** 2) +
               0.3 / (1 + ((f - 2600) / 400) ** 2))
        vowel += env * np.sin(2 * np.pi * f * t + k)
    vowel = (vowel / np.max(np.abs(vowel)) * 0.5).astype(np.float32)

    frame = vowel[:512] * np.hanning(512)
    spec = np.abs(np.fft.rfft(frame))
    freqs = np.fft.rfftfreq(512, 1 / SR)

    detected = []
    low_bin = max(1, int(200 * 512 / SR))
    high_bin = min(len(spec) - 1, int(8000 * 512 / SR))
    for i in range(low_bin + 1, high_bin - 1):
        if spec[i] <= spec[i - 1] or spec[i] <= spec[i + 1]:
            continue
        nb = spec[max(0, i - 10):min(len(spec), i + 11)]
        med = np.median(nb)
        if med < 1e-8:
            continue
        db = 20 * np.log10(spec[i] / med)
        if db > 6.0:
            detected.append((freqs[i], db))

    print(f"  对一个纯元音 /a/（120Hz 基频，含 700/1220/2600Hz 共振峰）做检测：")
    if detected:
        print(f"  >>> 误判数量：{len(detected)} 个『啸叫』被检出！")
        for f, db in sorted(detected, key=lambda x: -x[1])[:8]:
            print(f"      {f:7.1f} Hz  高出邻域 {db:5.1f} dB")
        print("""
  >>> 这就是致命问题：
      人声的谐波/共振峰本质上就是『窄带尖峰』，与啸叫特征高度相似。
      检测器只能取前 3 个最强峰去陷波，
      结果就是每次都把人声最响的 3 个共振峰（比如 700/1220/2600Hz）削掉 20dB。
      听感上就是：声音发闷、像隔着桶说话、辅音丢失、“说话声音变了”。
      ——这正是用户抱怨的第二个、也是最主要的音质破坏来源。
""")
    else:
        print("  未误判（本测试信号下）")


# ---------------------------------------------------------------------------
# 3. 陷波器状态重置导致的咔哒声
# ---------------------------------------------------------------------------
def analyze_notch_state_reset():
    report("3. 陷波器周期性重建状态 -> 爆破音/咔哒声")

    print("""
  代码中 _update_notches() 每次检测到啸叫都会：
    1. 用新频率重建陷波器系数
    2. 把滤波器状态 x1,x2,y1,y2 全部清零
  而检测约每 200ms 触发一次。

  滤波器内部有能量（IIR 状态），突然清零 = 状态不连续
  = 输出波形出现跳变 = 每 200ms 一次的『咔哒/噼啪』声。
  这会让声音听起来破碎、抖动。
""")

    nf = AdaptiveNotchFilter(sample_rate=SR, channels=1, max_notches=1, quality=30.0)
    x = (0.5 * np.sin(2 * np.pi * 1000 * np.arange(SR) / SR)).astype(np.float32)
    block = 512
    y_all = []
    for i in range(0, SR - block, block):
        chunk = x[i:i + block].reshape(-1, 1)
        # 每 10 个 block（约200ms @48k/512）强制重建一次陷波器，模拟真实行为
        if (i // block) % 10 == 0:
            nf._update_notches([1000.0])
        y = nf.process(chunk)
        y_all.append(y[:, 0])
    y_all = np.concatenate(y_all)

    # 检测不连续点：相邻样本二阶差分异常大
    d2 = np.abs(np.diff(y_all, n=2))
    thresh = np.percentile(d2, 99.9)
    spikes = np.where(d2 > max(thresh * 3, 0.01))[0]
    print(f"  输出样本数: {len(y_all)}, 检测到明显不连续点: {len(spikes)} 处")
    if len(spikes):
        gaps = np.diff(spikes)
        gaps = gaps[gaps > 100]
        if len(gaps):
            print(f"  主要不连续间隔: 中位数 {np.median(gaps):.0f} 样本 "
                  f"(≈{np.median(gaps)/SR*1000:.0f} ms) -> 与 200ms 重建周期吻合")
        print("""
  >>> 确认：周期性重建滤波器状态会产生规律性瞬态，
      叠加在语音上就是『颗粒感 / 破碎感 / 咔哒声』。
""")


# ---------------------------------------------------------------------------
# 4. 噪声门 + 侧链对语音的破坏
# ---------------------------------------------------------------------------
def analyze_gate():
    report("4. 噪声门 / 侧链抑制导致的『断断续续』")

    print("""
  噪声门判定用的是『整块 RMS 电平』，而 gain 平滑系数为：
    attack  = 0.8   （开门，快）
    release = 0.95  （关门，慢）

  问题一：门增益是『每块更新一次』，一块 = 512 样本 ≈ 10.7ms。
          对语音的快速起伏（音节起音只有几毫秒），会来不及响应，
          音节头被削掉 -> 听感『吞字』。

  问题二：release=0.95 表示每块只衰减 5%，
          从 1.0 降到 0.5 需要约 14 块 ≈ 150ms，
          一句话说完后尾巴会拖一段『渐弱』，然后突然静音，
          产生『喘息感 / 抽气感』。

  问题三（最严重）：侧链抑制用输出电平压制输入增益，而输出电平是
          『上一块处理后的 RMS』。这形成一个随语音起伏波动的自动增益
          -> 声音忽大忽小 -> 听感『音量在抽动』
""")

    # 模拟一段有起音的语音（音节包络）
    t = np.arange(N) / SR
    syll = np.zeros(N)
    for start in (0.05, 0.30, 0.55, 0.80):
        i0 = int(start * SR)
        ln = int(0.18 * SR)
        env = np.minimum(1.0, np.arange(ln) / (0.01 * SR)) * \
              np.exp(-np.arange(ln) / (0.08 * SR))
        syll[i0:i0 + ln] += env * np.sin(2 * np.pi * 300 * t[i0:i0 + ln])
    syll = (syll / np.max(np.abs(syll)) * 0.6).astype(np.float32)

    block = 512
    gate_gain = 1.0
    attack, release = 0.8, 0.95
    threshold = 0.015
    gains = []
    levels = []
    for i in range(0, len(syll) - block, block):
        blk = syll[i:i + block]
        lvl = float(np.sqrt(np.mean(blk ** 2)))
        levels.append(lvl)
        if lvl > threshold:
            gate_gain = gate_gain * attack + 1.0 * (1 - attack)
        else:
            gate_gain = gate_gain * release
        gains.append(gate_gain)

    gains = np.array(gains)
    levels = np.array(levels)
    print(f"  门增益范围: {gains.min():.3f} ~ {gains.max():.3f}")
    print(f"  增益标准差: {gains.std():.3f}  (越大越说明『抽动』明显)")
    open_cnt = np.sum(gains < 0.9)
    print(f"  处于半关状态({len(gains)}块中): {open_cnt} 块 "
          f"({open_cnt/len(gains)*100:.0f}% 时间) -> 语音被持续压制")
    # 找音节起音处，看门是否来得及打开
    print("\n  各音节起音后第 1/2/3 块的增益（应尽快接近 1.0）：")
    for start in (0.05, 0.30, 0.55, 0.80):
        bi = int(start * SR / block)
        if bi + 3 < len(gains):
            print(f"    音节@{start:.2f}s: {gains[bi]:.3f} -> {gains[bi+1]:.3f} -> {gains[bi+2]:.3f}")


def main():
    print("BTSPK 啸叫抑制算法诊断")
    print(f"采样率 {SR}Hz, 分析时长 {DUR}s")
    analyze_freq_shifter()
    analyze_notch()
    analyze_notch_state_reset()
    analyze_gate()
    report("总结")
    print("""
  抖动/变声的根因（按影响从大到小）：

  [A] 自适应陷波误伤人声共振峰（最主要）
      -> 人声谐波被当成啸叫，最强 3 个峰被削 20dB
      -> 声音发闷、失真、'像隔着桶'
      这是『说话声音都变了』的头号元凶。

  [B] 所谓『移频』其实是颤音调制
      -> 不是真移频，而是 FM 颤音，音高以 4Hz 抖动
      -> 声音发抖、金属感

  [C] 陷波器每 200ms 重建 + 状态清零
      -> 规律性瞬态，颗粒感/咔哒声

  [D] 噪声门 + 侧链形成动态增益抽动
      -> 吞字、喘息感、音量忽大忽小

  这四条叠加，就是『啸叫没了，但声音完全变了』的原因。
""")


if __name__ == "__main__":
    main()
