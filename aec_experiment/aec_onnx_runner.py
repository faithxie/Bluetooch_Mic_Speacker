# -*- coding: utf-8 -*-
"""
Pure-NumPy/ONNX streaming runner for `aec7_ep0185.onnx`.

No compiled C++ extension needed. Implements the exact upstream convention:

    48 kHz, hop 480 (10 ms), win 960 (20 ms), n_fft 960 -> 481 bins,
    sqrt-Hann window = sqrt(hann(960)),  mono float32,
    STFT is done on a 960-sample sliding buffer with hop_length == win_length,
    so each call yields exactly ONE frame.

Model I/O (14 in / 14 out):
    mic_frame (1,2,481)  far_frame (1,2,481)   <- dim1 = [real, imag]
    + 12 cache tensors, all initialised to zeros, fed back verbatim each frame.

Adapted from the upstream `AEC_ONNX_USAGE.md` reference loop, but written
against the *14-input* `aec7_ep0185.onnx` model (the doc describes an older
9-input model), and stripped of the torch dependency.
"""

import os
import sys

import numpy as np

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover
    print("[FATAL] onnxruntime 未安装。请先执行:")
    print("        ./venv/Scripts/python.exe -m pip install onnxruntime")
    sys.exit(1)


# --------------------------------------------------------------------------
# STFT configuration (must match the model exactly)
# --------------------------------------------------------------------------
SAMPLE_RATE = 48000
HOP_LEN = 480          # 10 ms
WIN_LEN = 960          # 20 ms
N_FFT = 960            # -> N_FFT//2 + 1 = 481 bins

# sqrt-Hann. np.hanning(M) is the symmetric window; torch.hann_window(M) is
# the periodic one. We build the periodic variant explicitly so the analysis
# window matches what the model was trained with:
#     torch.hann_window(960) = 0.5 - 0.5*cos(2*pi*n/960),  n = 0..959
_n = np.arange(WIN_LEN, dtype=np.float64)
_WINDOW = np.sqrt(0.5 - 0.5 * np.cos(2.0 * np.pi * _n / WIN_LEN))
_WINDOW = _WINDOW.astype(np.float32)

# --------------------------------------------------------------------------
# Cache tensors: name -> shape, in the exact order the model declares them
# --------------------------------------------------------------------------
CACHE_SHAPES = {
    "res_enc_conv": (1, 135680),
    "res_enc_tfa": (1, 248),
    "mic_enc_conv": (1, 135680),
    "mic_enc_tfa": (1, 248),
    "deep_enc_tfa": (1, 336),
    "dec_conv": (1, 13440),
    "dec_tfa": (1, 496),
    "inter": (1, 7680),
    "res_prev1": (1, 1, 1, 320),
    "res_prev2": (1, 1, 1, 320),
    "mic_prev1": (1, 1, 1, 320),
    "mic_prev2": (1, 1, 1, 320),
}

CACHE_NAMES = list(CACHE_SHAPES.keys())


def _default_model_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "aec7_ep0185.onnx")


class AecOnnxRunner:
    """Streaming AEC over a 14-input ONNX model. Pure Python, no torch."""

    def __init__(self, model_path=None, num_threads=1, verbose=False):
        self.model_path = model_path or _default_model_path()
        if not os.path.isfile(self.model_path):
            raise FileNotFoundError("找不到模型文件: %s" % self.model_path)

        so = ort.SessionOptions()
        so.intra_op_num_threads = num_threads
        so.inter_op_num_threads = num_threads
        if not verbose:
            so.log_severity_level = 3
        self.sess = ort.InferenceSession(
            self.model_path, so, providers=["CPUExecutionProvider"])

        # Validate that our assumptions match the actual model contract.
        got_in = [i.name for i in self.sess.get_inputs()]
        got_out = [o.name for o in self.sess.get_outputs()]
        expect_in = ["mic_frame", "far_frame"] + CACHE_NAMES
        missing = [n for n in expect_in if n not in got_in]
        if missing:
            raise RuntimeError(
                "模型输入与本运行器不匹配，缺少: %s\n实际输入: %s"
                % (missing, got_in))
        if len(got_out) != 14:
            raise RuntimeError(
                "期望 14 个输出，实际 %d 个: %s" % (len(got_out), got_out))

        # Output order: [enhanced_frame] + updated caches (names suffixed _o).
        # NOTE: `deep_enc_conv_o` is emitted with shape (1, 0) but has NO
        # matching input, so it is a dead tensor. We therefore map outputs back
        # to caches BY NAME rather than by position, and ignore anything that
        # does not correspond to a declared input cache.
        self.out_enhanced = got_out[0]
        in_names = set(got_in)
        self.out_to_cache = []          # list of (output_index, cache_name)
        for idx, name in enumerate(got_out):
            if idx == 0:
                continue
            base = name[:-2] if name.endswith("_o") else name
            if base in in_names and base in CACHE_SHAPES:
                self.out_to_cache.append((idx, base))
        if len(self.out_to_cache) != len(CACHE_SHAPES):
            raise RuntimeError(
                "缓存输出映射不完整：期望 %d 个，匹配到 %d 个 (%s)"
                % (len(CACHE_SHAPES), len(self.out_to_cache), self.out_to_cache))

        self.reset()

    # ------------------------------------------------------------------
    def reset(self):
        """Zero all caches and the sliding buffers."""
        self.caches = {
            name: np.zeros(shape, dtype=np.float32)
            for name, shape in CACHE_SHAPES.items()
        }
        self.mic_buffer = np.zeros(WIN_LEN, dtype=np.float32)
        self.far_buffer = np.zeros(WIN_LEN, dtype=np.float32)
        # WOLA overlap-add tail. Because hop == win/2 the previous frame's
        # second half overlaps the current frame's first half, so we carry it
        # forward instead of discarding it. This yields gain-exact
        # reconstruction at the cost of one hop (10 ms) of output latency.
        self._ola_tail = np.zeros(HOP_LEN, dtype=np.float32)
        self.frames_done = 0

    # ------------------------------------------------------------------
    @staticmethod
    def _stft_single(x960):
        """One frame of a sqrt-Hann STFT.

        x960 : (960,) real float32 (unwindowed sliding-buffer contents)
        returns (2, 481) float32 as [real, imag].

        Matches torch.stft(..., n_fft=960, hop_length=960, win_length=960,
        window=sqrt(hann(960)), center=False, return_complex=True) which,
        on a 960-sample input, yields exactly one frame. torch.stft is
        unnormalised and numpy's rfft is likewise unnormalised, so no extra
        scaling is applied.
        """
        spec = np.fft.rfft(x960 * _WINDOW, n=N_FFT)      # (481,) complex
        return np.stack([spec.real, spec.imag], axis=0).astype(np.float32)

    # ------------------------------------------------------------------
    def process(self, mic_chunk, far_chunk):
        """Process one 480-sample frame.

        mic_chunk : array-like of 480 float32 (mic, contains echo + near-end)
        far_chunk : array-like of 480 float32 (far-end reference / speaker loopback)
        returns   : np.ndarray (480,) float32, echo-cancelled mic

        Synthesis uses weighted overlap-add (WOLA): the model output frame is
        multiplied by the same sqrt-Hann synthesis window and accumulated.
        With hop = win/2, sqrt-Hann analysis + sqrt-Hann synthesis sums to
        exactly unity across the overlap, so the round trip is gain-exact.
        (Writing only the newest hop instead leaves an uncompensated
        window weight of 0.7078, i.e. a constant -3.00 dB loss.)
        """
        mic = np.asarray(mic_chunk, dtype=np.float32).reshape(-1)
        far = np.asarray(far_chunk, dtype=np.float32).reshape(-1)
        if mic.size != HOP_LEN or far.size != HOP_LEN:
            raise ValueError(
                "每一帧必须是 %d 个样本，收到 mic=%d far=%d"
                % (HOP_LEN, mic.size, far.size))

        # --- Step A: update the 960-sample sliding buffers ---
        self.mic_buffer = np.concatenate([self.mic_buffer[HOP_LEN:], mic])
        self.far_buffer = np.concatenate([self.far_buffer[HOP_LEN:], far])

        # --- Step B/C: single-frame STFT, packed as [R, I] -> (1, 2, 481) ---
        mic_frame = self._stft_single(self.mic_buffer)[np.newaxis, :, :]
        far_frame = self._stft_single(self.far_buffer)[np.newaxis, :, :]

        # --- Step D: inference ---
        feeds = {"mic_frame": mic_frame, "far_frame": far_frame}
        for name in CACHE_NAMES:
            feeds[name] = self.caches[name]

        outputs = self.sess.run(None, feeds)

        # --- Step E: the model emits the ENHANCED STFT directly ---
        enhanced = np.asarray(outputs[0], dtype=np.float32).reshape(2, 481)

        # --- Step F: iFFT + WOLA synthesis ---
        #
        # hop = 480, win = 960, analysis window w = sqrt-Hann. Because
        #     w[k]**2 + w[k+480]**2 == 1   (exact, verified to 1e-6)
        # applying the SAME sqrt-Hann as a synthesis window and overlap-adding
        # reconstructs the signal with unity gain and ~1e-7 error.
        #
        # Frame n analyses the buffer covering global samples
        #     (n-1)*480 .. n*480 + 479
        # so y[:480] belongs to the OLDER hop and y[480:] to the NEWER hop.
        # The hop that becomes complete after frame n is the older one, hence
        # the output lags the input by exactly one hop (480 samples = 10 ms).
        spec = enhanced[0] + 1j * enhanced[1]
        y = np.fft.irfft(spec, n=N_FFT)                  # (960,)

        out = (self._ola_tail + y[:HOP_LEN] * _WINDOW[:HOP_LEN])
        self._ola_tail = (y[HOP_LEN:] * _WINDOW[HOP_LEN:]).astype(np.float32)
        out = out.astype(np.float32)

        # --- Step G: roll the caches forward (by name, skipping dead outputs) ---
        for idx, name in self.out_to_cache:
            arr = np.asarray(outputs[idx], dtype=np.float32)
            if arr.size == 0:
                continue
            if arr.shape != CACHE_SHAPES[name]:
                arr = arr.reshape(CACHE_SHAPES[name])
            self.caches[name] = arr

        self.frames_done += 1
        return out

    # ------------------------------------------------------------------
    def process_stream(self, mic, far):
        """Run a long signal through frame by frame.

        IMPORTANT — output latency: the WOLA synthesis described in
        `process()` emits the *older* hop, so the frame produced by call n
        corresponds to input samples from call n-1. This method hides that
        by discarding the first returned frame and flushing one extra zero
        frame at the end, so that `out[i]` is time-aligned with `mic[i]`.

        Returns a float32 array of the same length as the usable input.
        """
        mic = np.asarray(mic, dtype=np.float32).reshape(-1)
        far = np.asarray(far, dtype=np.float32).reshape(-1)
        n = min(mic.size, far.size)
        n_frames = n // HOP_LEN

        # Prepend one silent frame so the very first real frame is emitted
        # together with its completing partner; append one more to flush the
        # final hop out of the overlap buffer.
        pad_mic = np.concatenate([np.zeros(HOP_LEN, np.float32), mic])
        pad_far = np.concatenate([np.zeros(HOP_LEN, np.float32), far])
        total_frames = n_frames + 2

        raw = []
        for i in range(total_frames):
            s = i * HOP_LEN
            cm = pad_mic[s:s + HOP_LEN]
            cf = pad_far[s:s + HOP_LEN]
            if cm.size < HOP_LEN:
                cm = np.pad(cm, (0, HOP_LEN - cm.size))
            if cf.size < HOP_LEN:
                cf = np.pad(cf, (0, HOP_LEN - cf.size))
            raw.append(self.process(cm, cf))

        raw = np.concatenate(raw) if raw else np.zeros(0, np.float32)
        # The priming frame plus the WOLA tail mean the raw stream lags the
        # input by TWO hops (verified: shifting by 2*HOP_LEN gives corr 1.000).
        # Drop both so that out[i] is time-aligned with mic[i].
        drop = 2 * HOP_LEN
        out = raw[drop:drop + n_frames * HOP_LEN]
        if out.size < n_frames * HOP_LEN:
            out = np.pad(out, (0, n_frames * HOP_LEN - out.size))
        return out.astype(np.float32)

    # ------------------------------------------------------------------
    def process_stream_delayed_far(self, mic, far, delay_samples):
        """Feed `far` delayed by N samples relative to `mic`.

        Models the real acoustic path, where the reference we hand to the AEC
        leads the echo appearing in the mic. Returns output aligned to `mic`.
        """
        far = np.asarray(far, dtype=np.float32).reshape(-1)
        pad = np.zeros(delay_samples, dtype=np.float32)
        far_delayed = np.concatenate([pad, far])
        return self.process_stream(mic, far_delayed)
