# -*- coding: utf-8 -*-
"""
Capture genuine speech via the VB-Audio Virtual Cable loopback.

Chain:
    Windows SAPI TTS  ->  CABLE Input (playback)  ->  CABLE Output (record)

This yields real, non-synthetic 48 kHz speech. The synthesized harmonic
generators used elsewhere in this folder are convenient but degenerate: a
perfectly periodic harmonic stack is not representative of speech, and the
AEC model behaves very differently on it.

Output: speech_probe_48k.npy  (float32 mono, 48 kHz)

Usage:
    cd D:/code/BTSPK
    ./venv/Scripts/python.exe aec_experiment/capture_speech.py
"""

import os
import subprocess
import sys
import time

import numpy as np
import sounddevice as sd

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "speech_probe_48k.npy")

CABLE_IN_NAME = "CABLE Input (VB-Audio Virtual Cable)"
CABLE_OUT_NAME = "CABLE Output (VB-Audio Virtual Cable)"

SENTENCES = [
    "The quick brown fox jumps over the lazy dog.",
    "Speech carries many harmonics, and they change from moment to moment.",
    "Acoustic echo cancellation needs a clean far end reference signal.",
    "One, two, three, four, five, six, seven, eight, nine, ten.",
    "Howling happens when the loop gain reaches one and the phase lines up.",
]


def find_device(name, kind):
    """Return the first device index whose name matches and supports `kind`."""
    want_in = (kind == "in")
    for idx, dev in enumerate(sd.query_devices()):
        if name.lower() not in dev["name"].lower():
            continue
        if want_in and dev["max_input_channels"] > 0:
            # prefer 48 kHz-capable devices
            try:
                sd.check_input_settings(device=idx, samplerate=48000,
                                        channels=1, dtype="float32")
                return idx, 48000
            except Exception:
                continue
        if not want_in and dev["max_output_channels"] > 0:
            try:
                sd.check_output_settings(device=idx, samplerate=48000,
                                         channels=1, dtype="float32")
                return idx, 48000
            except Exception:
                continue
    return None, None


def speak(text, out_dev):
    """Send text to the given playback device via PowerShell SAPI."""
    safe = text.replace("'", "''")
    ps = (
        "$ErrorActionPreference='Stop';"
        "$sp = New-Object -ComObject SAPI.SpVoice;"
        "$devs = $sp.GetAudioOutputs();"
        # pick the wave-out device whose name contains 'CABLE'
        "$t = $null; foreach($d in $devs){ if($d.GetDescription() -like '*CABLE*'){ $t=$d; break } };"
        "if($t -ne $null){ $sp.AudioOutput = $t };"
        "$sp.Speak('%s')" % safe
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, timeout=60)


def main():
    in_dev, in_sr = find_device(CABLE_OUT_NAME, "in")
    out_dev, out_sr = find_device(CABLE_IN_NAME, "out")
    if in_dev is None or out_dev is None:
        print("[FAIL] 找不到 VB-Audio Virtual Cable 设备")
        print("       input :", in_dev)
        print("       output:", out_dev)
        print("       请确认已安装 VB-Audio Virtual Cable 且未被独占。")
        return 1
    print("录音设备 #%d @ %d Hz" % (in_dev, in_sr))
    print("播放设备 #%d @ %d Hz" % (out_dev, out_sr))
    print()

    # Silence any pre-existing audio on the cable by draining first.
    time.sleep(0.3)

    stop = {"v": False}
    captured = []

    def cb(indata, frames, time_info, status):
        if status:
            pass
        captured.append(indata[:, 0].copy())

    stream = sd.InputStream(device=in_dev, samplerate=48000, channels=1,
                            dtype="float32", blocksize=480, callback=cb)
    stream.start()

    try:
        for i, s in enumerate(SENTENCES):
            print("  [%d/%d] %s" % (i + 1, len(SENTENCES), s))
            speak(s, out_dev)
            time.sleep(0.35)
    finally:
        time.sleep(0.4)
        stream.stop()
        stream.close()

    if not captured:
        print("[FAIL] 没有采集到任何音频")
        return 1

    audio = np.concatenate(captured).astype(np.float32)
    dur = audio.size / 48000.0
    peak = float(np.abs(audio).max())
    rms = float(np.sqrt((audio ** 2).mean()))
    print()
    print("采集到 %.2f 秒，峰值 %.4f，RMS %.5f" % (dur, peak, rms))

    if peak < 1e-4:
        print("[FAIL] 电平过低，TTS 可能没有播到 CABLE Input。")
        print("       请检查 Windows 默认播放设备 / SAPI 输出设备。")
        return 1

    # Normalise to a comfortable level, keeping the original dynamics.
    audio = (audio / peak * 0.5).astype(np.float32)
    np.save(OUT_PATH, audio)
    print("已保存 -> %s" % OUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
