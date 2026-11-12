"""게이트웨이 1차 감지: CSI 진폭 → 특징값(1초마다) → 사건(낙상 의심·호흡 신호 없음).

원본 CSI는 이 안에서만 쓰고 버린다(최근 32초만 메모리에 두고 밀어냄). 관제로 나가는 것은 1초에 수백 바이트의 특징값뿐이다.
임곗값은 시작할 때 '조용한 방 10초 보정'으로 정한다(현장마다 다르므로).

- 움직임 지수: 최근 1초 부반송파별 (표준편차 ÷ 평균)의 평균, 보정값 대비 배수.
- 호흡: 최근 20초. 부반송파별 호흡 대역(0.13~0.6Hz, 분당 8~36회) 세기 ÷ 잡음 대역(1~3Hz) 세기 = 신호 대 잡음비.
  잡음비가 큰 부반송파 8개를 대역 통과한 뒤 첫 주성분의 주파수 최댓값 = 분당 호흡수.
- 낙상 의심: 앞 4초 이상 조용하다가, 3초 이내의 크고 짧은 변동(보정값의 9배 이상) 뒤 5초 이상 조용.
- 호흡 신호 없음: 사람이 있었고, 조용한데 호흡 신호가 20초 이상 잡히지 않음.
"""
from __future__ import annotations

from collections import deque

import numpy as np
from scipy.signal import butter, sosfiltfilt

BREATH_BAND = (0.13, 0.6)
NOISE_BAND = (1.0, 3.0)


class Detector:
    def __init__(self, fs: int, n_sc: int):
        self.fs, self.n_sc = fs, n_sc
        self.buf = deque(maxlen=fs * 32)
        self.sos = butter(3, BREATH_BAND, btype="band", fs=fs, output="sos")
        self.raw_bytes = 0                       # 원본으로 처리한 양(ESP32 기준: 부반송파당 2바이트)
        self.calib: list[float] = []
        self.base_motion: float | None = None
        self.hist: deque = deque(maxlen=30)      # (t, 움직임 배수)
        self.state = "보정 중"
        self.fall_run_end: float | None = None
        self.no_breath_since: float | None = None
        self.present = False
        self.last_event_t = -1e9

    def push(self, csi: np.ndarray):
        self.buf.append(np.abs(csi).astype(np.float32))
        self.raw_bytes += 2 * self.n_sc

    def _breath(self, A: np.ndarray):
        fs = self.fs
        n = min(len(A), fs * 20)
        if n < fs * 12:
            return None, 0.0, []
        X = A[-n:] - A[-n:].mean(axis=0)
        win = np.hanning(n)[:, None]
        P = np.abs(np.fft.rfft(X * win, axis=0)) ** 2
        f = np.fft.rfftfreq(n, 1 / fs)
        b = (f >= BREATH_BAND[0]) & (f <= BREATH_BAND[1])
        r = (f >= NOISE_BAND[0]) & (f <= NOISE_BAND[1])
        snr_k = P[b].mean(axis=0) / (P[r].mean(axis=0) + 1e-12)
        top = np.argsort(snr_k)[-8:]
        snr = float(np.median(snr_k[top]))
        Y = sosfiltfilt(self.sos, X[:, top], axis=0)
        u, sv, _ = np.linalg.svd(Y - Y.mean(axis=0), full_matrices=False)
        pc = u[:, 0] * sv[0]
        spec = np.abs(np.fft.rfft(pc * np.hanning(n), n=8 * n)) ** 2
        f8 = np.fft.rfftfreq(8 * n, 1 / fs)
        bb = (f8 >= BREATH_BAND[0]) & (f8 <= BREATH_BAND[1])
        bpm = float(f8[bb][np.argmax(spec[bb])] * 60)
        w = pc[-fs:] / (np.std(pc) + 1e-9)
        return bpm, snr, [round(float(x), 3) for x in w[:: fs // 5]]

    def step(self, t: float) -> tuple[dict, list[dict]]:
        A = np.asarray(self.buf)
        fs = self.fs
        if len(A) < fs:
            return {"t": t, "state": self.state}, []
        last = A[-fs:]
        motion = float(np.mean(np.std(last, axis=0) / (np.mean(last, axis=0) + 1e-6)))

        if self.base_motion is None:                 # 보정 10초
            self.calib.append(motion)
            if len(self.calib) >= 10:
                self.base_motion = float(np.median(self.calib))
                self.state = "감시 중"
            return {"t": t, "state": self.state}, []

        ratio = motion / self.base_motion
        moving, quiet = ratio > 6, ratio < 2.2
        bpm, snr, wave = self._breath(A)
        breathing = bool(bpm is not None and snr > 60 and not moving)   # 시뮬레이션 기준: 호흡 160~500, 잡음·자세 전환 2~45
        if breathing or moving:
            self.present = True
        self.hist.append((t, ratio))

        events: list[dict] = []
        # ---- 낙상 의심
        H = list(self.hist)
        if not moving:
            hi = [i for i, (_, m) in enumerate(H) if m > 6]
            if hi:
                end = hi[-1]
                start = end
                while start > 0 and H[start - 1][1] > 6:
                    start -= 1
                before = H[max(0, start - 6):start]
                isolated = len(before) >= 4 and all(m < 6 for _, m in before)
                peak = max(m for _, m in H[start:end + 1])
                run_end_t = H[end][0]
                if end - start + 1 <= 3 and peak > 9 and isolated and run_end_t > self.last_event_t:
                    self.fall_run_end = run_end_t
        if self.fall_run_end is not None:
            after = [m for tt, m in H if tt > self.fall_run_end]
            if any(m >= 2.2 for m in after):
                self.fall_run_end = None if any(m > 6 for m in after) else self.fall_run_end
            if self.fall_run_end is not None and len(after) >= 5 and all(m < 2.2 for m in after[-5:]):
                events.append({"type": "fall", "breathing": breathing})
                self.last_event_t, self.fall_run_end = t, None

        # ---- 호흡 신호 없음
        if self.present and quiet and not breathing and bpm is not None:
            self.no_breath_since = self.no_breath_since or t
            if t - self.no_breath_since >= 20 and t - self.last_event_t > 40:
                events.append({"type": "breath_absent", "breathing": False})
                self.last_event_t = t
        else:
            self.no_breath_since = None

        self.state = "움직임" if moving else "호흡 감지" if breathing else "호흡 신호 약함"
        feat = {"t": t, "state": self.state, "motion_ratio": round(ratio, 2), "bpm": round(bpm, 1) if (bpm and breathing) else None,
                "snr": round(snr, 1), "breathing": breathing, "wave": wave, "raw_bytes": self.raw_bytes}
        return feat, events
