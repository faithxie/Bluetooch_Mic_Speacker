# -*- coding: utf-8 -*-
"""
Controlled real-speech AEC probe with unambiguous ground truth.

The earlier probes conflated two things: (a) how much the model damages the
near-end, and (b) how much echo it removes. This script separates them by
using TWO DIFFERENT speech sources:

    far_speech  -> played on the speaker, leaks into the mic
    near_speech -> the local talker, must be preserved

    mic = delay(far_speech, D) * atten + near_speech

Because we know both components exactly, we can measure:
    ERLE = 20log10( ||echo|| / ||output - near_speech|| )
i.e. how much of the *echo* survives, while explicitly allowing for the
model altering the near-end.

Segments (each 2 s):
    0-2 s  echo only    (near is zero)  -> clean ERLE
    2-4 s  echo + near  (double talk)
    4-6 s  near only    (far is zero)   -> near-end fidelity
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aec_onnx_runner import AecOnnxRunner, HOP_LEN, SAMPLE_RATE  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "speech_probe_48k.npy")

SEG_SEC = 2.0
N_SEG = int(SEG_SEC * SAMPLE_RATE)
WARM = HOP_LEN * 20          # skip model adaptation


def rms(v):
    v = np.asarray(v, dtype=np.float64)
    return float(np.sqrt(np.mean(v ** 2) + 1e-20))


def db(x, r):
    return 20.0 * np.log10((x + 1e-20) / (r + 1e-20))


def load():
    """Return (far_src, near_src), each 4 s long, from disjoint speech halves."""
    sp = np.load(PROBE).astype(np.float32)
    half = sp.size // 2
    need = 2 * N_SEG          # far must cover the echo-only AND double-talk parts

    def take(src, want):
        if src.size >= want:
            return src[:want]
        reps = int(np.ceil(want / max(src.size, 1)))
        return np.tile(src, reps)[:want]

    f = take(sp[:half], need)
    n = take(sp[half:], need)
    f = (f / (np.abs(f).max() + 1e-9) * 0.5).astype(np.float32)
    n = (n / (np.abs(n).max() + 1e-9) * 0.5).astype(np.float32)
    return f, n


def run(delay_ms, atten_db, refl=True):
    far1, near1 = load()

    far = np.zeros(N_SEG * 3, dtype=np.float32)
    near = np.zeros(N_SEG * 3, dtype=np.float32)

    # 0-2 echo only, 2-4 double talk, 4-6 near only
    far[0:2 * N_SEG] = far1
    near[N_SEG:3 * N_SEG] = near1

    d = int(round(delay_ms * SAMPLE_RATE / 1000.0))
    a = 10.0 ** (-atten_db / 20.0)

    echo = np.zeros_like(far)
    if d < far.size:
        echo[d:] = far[:far.size - d] * a
    if refl:
        rn = int(round(1.6 * SAMPLE_RATE / 1000.0))
        if rn < far.size:
            echo[rn:] += (a * 0.4) * far[:far.size - rn]

    mic = (echo + near).astype(np.float32)

    r = AecOnnxRunner()
    out = r.process_stream(mic, far)

    m = min(out.size, mic.size)
    out, mic, echo_c, near_c = out[:m], mic[:m], echo[:m], near[:m]

    eo = slice(WARM, N_SEG)                            # echo only
    dt_ = slice(N_SEG + WARM, 2 * N_SEG)               # double talk
    no = slice(2 * N_SEG + WARM, 3 * N_SEG)            # near only

    # ---- echo-only: nothing but echo in mic, so residual == output ----
    erle = db(rms(echo_c[eo]), rms(out[eo]))
    # how much of the mic was removed
    mic_red = db(rms(mic[eo]), rms(out[eo]))

    # ---- near-only: near == mic, so measure distortion ----
    near_chg = db(rms(out[no]), rms(near_c[no]))
    near_corr = float(np.corrcoef(out[no], near_c[no])[0, 1])

    # ---- double talk ----
    dt_chg = db(rms(out[dt_]), rms(mic[dt_]))
    # residual echo during double talk (output minus known near-end)
    resid = out[dt_] - near_c[dt_]
    dt_erle = db(rms(echo_c[dt_]), rms(resid))
    dt_corr = float(np.corrcoef(out[dt_], near_c[dt_])[0, 1])

    return {
        "delay_ms": delay_ms, "atten_db": atten_db,
        "erle": erle, "mic_red": mic_red,
        "near_chg": near_chg, "near_corr": near_corr,
        "dt_chg": dt_chg, "dt_erle": dt_erle, "dt_corr": dt_corr,
    }


def show(tag, res):
    print("  %s" % tag)
    print("    回声段   回声 %.5f -> 输出 %.5f   ERLE %6.2f dB   (mic 共降 %.2f dB)"
          % (0, 0, res["erle"], -res["mic_red"]) if False else
          "    回声段   ERLE %6.2f dB    mic 整体降 %6.2f dB"
          % (res["erle"], -res["mic_red"]))
    print("    近端段   变化 %+6.2f dB   相关性 %.3f"
          % (res["near_chg"], res["near_corr"]))
    print("    双讲段   ERLE %6.2f dB    变化 %+6.2f dB   相关性 %.3f"
          % (res["dt_erle"], res["dt_chg"], res["dt_corr"]))


def main():
    if not os.path.isfile(PROBE):
        print("[FAIL] 缺少 %s" % PROBE)
        return 1

    print("=" * 76)
    print("真实语音 AEC 验证（far / near 取自不同语音片段，ground truth 已知）")
    print("=" * 76)
    print()

    print("[基准] 理想回声 far == mic，无延迟无衰减")
    show("delay 0 ms, atten 0 dB", run(0.0, 0.0, refl=False))
    print()

    print("[主扫描] 延迟 × 衰减")
    # The model only starts working once the reference leads the echo by roughly
    # 18-20 ms, so the sweep must reach well beyond that to be informative.
    delays = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0, 50.0)
    attens = (-6.0, -12.0, -20.0)
    res = []
    for d in delays:
        for a in attens:
            rr = run(d, a)
            res.append(rr)
            show("delay %.1f ms, atten %.1f dB" % (d, a), rr)
            print()

    print("=" * 76)
    print("汇总")
    print("=" * 76)
    print("  ERLE / 回声抑制 (dB)   [>15 值得接入]")
    print("    %-12s" % "延迟\\衰减" + "".join("%12s" % ("%g dB" % a) for a in attens))
    for d in delays:
        row = "    %-12s" % ("%.1f ms" % d)
        for a in attens:
            h = [x for x in res if x["delay_ms"] == d and x["atten_db"] == a][0]
            row += "%12.2f" % h["erle"]
        print(row)
    print()
    print("  近端变化 (dB)  [应接近 0]")
    for d in delays:
        row = "    %-12s" % ("%.1f ms" % d)
        for a in attens:
            h = [x for x in res if x["delay_ms"] == d and x["atten_db"] == a][0]
            row += "%12.2f" % h["near_chg"]
        print(row)
    print()
    print("  近端相关性  [应接近 1]")
    for d in delays:
        row = "    %-12s" % ("%.1f ms" % d)
        for a in attens:
            h = [x for x in res if x["delay_ms"] == d and x["atten_db"] == a][0]
            row += "%12.3f" % h["near_corr"]
        print(row)
    print()

    good = [x for x in res if x["erle"] >= 15.0]
    print("  ERLE>=15dB 的场景: %d / %d" % (len(good), len(res)))
    print("  ERLE 中位数: %.2f dB" % float(np.median([x["erle"] for x in res])))
    print("  近端变化中位数: %+.2f dB" % float(np.median([x["near_chg"] for x in res])))
    print("  近端相关性中位数: %.3f" % float(np.median([x["near_corr"] for x in res])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
