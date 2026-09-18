"""
端到端验证：模拟完整的「麦克风 → 引擎处理 → 扬声器」链路
验证目标：
  1. 人声通过引擎后不被误伤（陷波器不误触发）
  2. 啸叫信号能被检出并抑制
  3. 输出无 NaN / 无异常
  4. 打印各阶段实际生效的参数

不打开真实音频设备，直接调用回调和处理链。
"""
import sys, os
import numpy as np
from scipy import signal as sp

# 允许从 diagnostics/ 子目录直接运行：把**项目根**加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio_router.services.audio_router import AudioRouterEngine
from audio_router.services.howling_suppressor_v2 import (
    TrueFrequencyShifter, MultiCriteriaHowlingSuppressor)

SR = 48000
EPS = 1e-12


def make_speech(f0=120.0, dur=2.0, sr=SR):
    n = int(sr * dur)
    g = np.zeros(n)
    for k in range(0, n, int(sr / f0)):
        g[k] = 1.0
    out = g
    for fc, bw in ((700, 130), (1220, 160), (2500, 220), (3400, 250)):
        r = np.exp(-np.pi * bw / sr)
        out = sp.lfilter([1 - r], [1, -2 * r * np.cos(2 * np.pi * fc / sr), r * r], out)
    return (out / (np.max(np.abs(out)) + EPS) * 0.6).astype(np.float32)


def make_howl(f=1200.0, dur=2.0, sr=SR):
    n = int(sr * dur)
    t = np.arange(n) / sr
    amp = 0.02 * 10 ** (8.0 * t / 20.0)
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


print("=" * 74)
print("端到端链路验证")
print("=" * 74)

engine = AudioRouterEngine()
engine._output_samplerate = SR
engine._input_samplerate = SR
engine._output_channels = 2
engine._input_channels = 1
engine._need_resample = False
engine._resample_ratio = 1.0

print("\n【引擎默认配置】")
print(f"  输入增益      : {engine._input_gain*100:.0f}%")
print(f"  输出音量      : {engine._volume*100:.0f}%")
print(f"  噪声门        : {'开' if engine._noise_gate_enabled else '关'} "
      f"(attack {engine._gate_attack_ms}ms / release {engine._gate_release_ms}ms / hold {engine._noise_gate_hold_ms}ms)")
print(f"  侧链抑制      : {'开' if engine._sidechain_enabled else '关'}")
print(f"  真移频        : {'开' if engine._freq_shift_enabled else '关'} "
      f"({engine._freq_shift_amount}Hz)")
print(f"  多判据陷波    : {'开' if engine._notch_enabled else '关'} "
      f"(衰减 {engine._notch_attenuation_db}dB)")

# 手动构造抑制器（绕开设备启动）
def build_suppressors(eng):
    if eng._freq_shift_enabled:
        eng._freq_shifter = TrueFrequencyShifter(
            shift_hz=eng._freq_shift_amount, sample_rate=SR,
            channels=eng._output_channels)
    else:
        eng._freq_shifter = None
    if eng._notch_enabled:
        eng._notch_filter = MultiCriteriaHowlingSuppressor(
            sample_rate=SR, channels=eng._output_channels,
            max_notches=4, quality=10.0,
            attenuation_db=eng._notch_attenuation_db)
    else:
        eng._notch_filter = None


def run_chain(signal, label, enable_notch=True, enable_shift=False):
    """把信号按引擎的处理链走一遍（输入回调 → 缓冲 → 输出回调 的等效流程）"""
    eng = AudioRouterEngine()
    eng._output_samplerate = SR
    eng._input_samplerate = SR
    eng._output_channels = 2
    eng._input_channels = 1
    eng._need_resample = False
    eng._notch_enabled = enable_notch
    eng._freq_shift_enabled = enable_shift
    eng._is_running = True
    build_suppressors(eng)

    block = 512
    out_frames = []
    notch_active_frames = 0
    n_blocks = 0
    for i in range(0, len(signal) - block, block):
        seg = signal[i:i + block]
        mono = seg.reshape(-1, 1).astype(np.float32)
        # 等效输入回调的输出（增益/门/侧链 → 立体声复制）
        data = np.tile(mono, (1, 2))
        if eng._input_gain != 1.0:
            data = np.clip(data * eng._input_gain, -1, 1)
        # 输出回调处理
        out = data.copy()
        if eng._notch_filter is not None:
            out = eng._notch_filter.process(out)
            if eng._notch_filter.active_count > 0:
                notch_active_frames += 1
        if eng._freq_shifter is not None:
            out = eng._freq_shifter.process(out)
        out_frames.append(out[:, 0].copy())
        n_blocks += 1

    y = np.concatenate(out_frames)
    x = signal[:len(y)]

    ok = np.all(np.isfinite(y))
    print(f"\n  {label}")
    print(f"    输出有限性      : {'OK 无 NaN/Inf' if ok else 'FAIL 含 NaN/Inf'}")
    print(f"    陷波器激活帧数  : {notch_active_frames} / {n_blocks} 帧")
    print(f"    输入 RMS        : {np.sqrt(np.mean(x**2)):.5f}")
    print(f"    输出 RMS        : {np.sqrt(np.mean(y**2)):.5f}")
    return y, x, notch_active_frames, n_blocks


# --- 场景 1：纯人声，陷波开（不应误伤）---
y_speech, x_speech, nf, nb = run_chain(
    make_speech(), "场景1：真实人声 + 多判据陷波（理想：0 帧激活）",
    enable_notch=True, enable_shift=False)

# --- 场景 2：纯啸叫，陷波开（应检出）---
y_howl, x_howl, nh, nb2 = run_chain(
    make_howl(), "场景2：纯啸叫 + 多判据陷波（理想：多数帧激活）",
    enable_notch=True, enable_shift=False)

# --- 场景 3：人声 + 真移频（看音色影响）---
y_shift, _, _, _ = run_chain(
    make_speech(), "场景3：人声 + 真移频 4Hz（看是否有明显失真）",
    enable_notch=False, enable_shift=True)

# 频谱对比：移频对音色的影响
# 注意：FIR 移频器有固定群延迟，需对齐后再比较，否则长度/相位错位会污染频谱指标
def spectrum_centroid(sig):
    spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
    freqs = np.fft.rfftfreq(len(sig), 1 / SR)
    return np.sum(freqs * spec) / (np.sum(spec) + EPS)


def band_energy(sig):
    spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig)))) ** 2
    freqs = np.fft.rfftfreq(len(sig), 1 / SR)
    tot = np.sum(spec) + EPS
    out = {}
    for lo, hi in ((0, 500), (500, 2000), (2000, 5000), (5000, 24000)):
        m = (freqs >= lo) & (freqs < hi)
        out[f"{lo}-{hi}"] = np.sum(spec[m]) / tot * 100
    return out


# 用群延迟对齐：取信号的有效重叠区间比较
gd = TrueFrequencyShifter.HILBERT_TAPS // 2
n_cmp = len(y_shift) - gd
x_ref = x_speech[gd:gd + n_cmp]
y_ref = y_shift[gd:gd + n_cmp]

print("\n" + "=" * 74)
print("结论")
print("=" * 74)
print(f"""
  [1] 人声误伤：陷波器在 {nb} 帧人声上激活 {nf} 帧
      → {'通过 ✓（不误伤人声）' if nf <= 1 else '仍有误判，需调阈值'}

  [2] 啸叫检出：{nb2} 帧中激活 {nh} 帧
      → {'通过 ✓（能检出啸叫）' if nh > nb2 * 0.3 else '检出偏弱'}

  [3] 输出有限性：{'通过 ✓' if np.all(np.isfinite(y_speech)) and np.all(np.isfinite(y_howl)) else '失败 ✗'}
""")

c_in = spectrum_centroid(x_ref)
c_out = spectrum_centroid(y_ref)
print(f"  [4] 真移频音色影响（已按群延迟 {gd} 样本对齐后比较）")
print(f"      带宽/过冲类指标见下（频谱质心不适用于此处的说明见文末）")
print(f"      峰值：原始 {np.max(np.abs(x_ref)):.4f} → 移频后 {np.max(np.abs(y_ref)):.4f} "
      f"(不应过冲)")
bi = band_energy(x_ref)
bo = band_energy(y_ref)
print(f"\n      各频段能量占比（输入 % → 输出 %）：")
for k in bi:
    print(f"        {k:>12} Hz : {bi[k]:6.2f} % → {bo[k]:6.2f} %")

hf_growth = bo["5000-24000"] - bi["5000-24000"]
print(f"\n      高频段(5k-24k)能量变化：{hf_growth:+.3f} % "
      f"→ {'通过 ✓ 无块边界瞬态' if abs(hf_growth) < 0.5 else '存在高频伪影 ✗'}")
over = np.max(np.abs(y_ref)) / (np.max(np.abs(x_ref)) + EPS)
print(f"      振幅过冲比：{over:.4f} → "
      f"{'通过 ✓ 无过冲' if over < 1.15 else '存在过冲 ✗'}")

# --- 移频量精度：用单频正弦测量，这才是能反映「是否真移频」的权威指标 ---
# 宽带语音的频谱质心对时间对齐极度敏感（不同步 1% 就会漂移上百 Hz），
# 因此不能用它对 4Hz 的全局平移下结论。改用单频正弦，直接看峰移到哪。
_tone_in = (0.5 * np.sin(2 * np.pi * 1000 * np.arange(SR) / SR)).astype(np.float32)
_fs_probe = TrueFrequencyShifter(shift_hz=4.0, sample_rate=SR, channels=1)
_tone_out = _fs_probe.process(_tone_in.reshape(-1, 1))[:, 0]
_seg = _tone_out[SR // 2: SR // 2 + 65536]
_spec = np.abs(np.fft.rfft(_seg * np.hanning(len(_seg))))
_freqs = np.fft.rfftfreq(len(_seg), 1 / SR)
_band = (_freqs > 985) & (_freqs < 1025)
_peak = _freqs[_band][np.argmax(_spec[_band])]
print(f"\n      ★ 移频量精度（单频 1000Hz 输入，shift=4Hz）：")
print(f"          输出主峰 {_peak:.2f} Hz（目标 1004.00 Hz，误差 {_peak - 1004.0:+.2f} Hz）"
      f" → {'通过 ✓ 真移频' if abs(_peak - 1004.0) < 2.0 else '未达到目标移频量 ✗'}")
print("""
  说明：真移频是线性时不变操作，理论上只把频谱整体平移 ΔHz，
        不改变各频段能量的相对分布，也不应产生过冲。
        单频探针是验证「移频量是否准确」的权威方法；
        宽带语音的频谱质心受时间对齐影响极大，不用于此判断。
        上面若各项通过，说明移频器是干净可用的。
""")
