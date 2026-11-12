"""CSI 입력원.

- SimCSI: 보드 없이 시연할 때 쓰는 시뮬레이터. ESP32 csi_recv와 같은 모양(부반송파별 복소수, 초당 fs개)을 낸다.
  사람 상태(빈 방·앉아서 호흡·걷기·낙상·누워 호흡·호흡 없음)에 따라 진폭이 달라진다. 실측이 아니므로 화면에 '시뮬레이션 입력'으로 표시한다.
- SerialCSI: ESP32(Espressif esp-csi `get-started/csi_recv`)가 USB 직렬로 내보내는 `CSI_DATA,...,"[...]"` 줄을 읽는다.
  보드가 오면 이것으로 바꾼다(아직 실기 미검증).
"""
from __future__ import annotations

import re
import threading
import time

import numpy as np

N_SC = 52   # HT20 데이터 부반송파 수(가드 제외)
FS = 50     # 초당 패킷 수


class SimCSI:
    STATES = ("empty", "sit_breath", "walk", "fall", "lying_breath", "lying_no_breath")

    def __init__(self, fs: int = FS, seed: int = 7):
        self.fs = fs
        self.rng = np.random.default_rng(seed)
        self.base = 20 + 10 * self.rng.random(N_SC)        # 방의 다중 경로가 만든 부반송파별 기본 진폭
        self.phase_k = self.rng.uniform(0, 2 * np.pi, N_SC)
        self.sens_k = self.rng.uniform(0.3, 1.0, N_SC)      # 부반송파마다 사람 움직임에 대한 민감도
        self.offset = self.rng.normal(0, 2.0, N_SC)         # 사람 위치·자세에 따른 진폭 이동
        self.walk = np.zeros(N_SC)
        self.state, self.breath_bpm, self.after_fall = "sit_breath", 15.0, "lying_breath"
        self.t, self.state_since = 0.0, 0.0
        self.lock = threading.Lock()
        self.label = "시뮬레이션 입력"

    def set_state(self, state: str, bpm: float | None = None, after_fall: str | None = None):
        with self.lock:
            if state not in self.STATES:
                return
            if state != self.state:
                if state in ("lying_breath", "lying_no_breath", "fall"):
                    pass                                            # 낙상 뒤 자세 이동은 fall에서 처리
                elif state == "empty":
                    self.offset = np.zeros(N_SC)
                else:
                    self.offset = self.rng.normal(0, 2.0, N_SC)
                self.state, self.state_since = state, self.t
            if bpm:
                self.breath_bpm = float(bpm)
            if after_fall:
                self.after_fall = after_fall

    def next(self) -> np.ndarray:
        with self.lock:
            self.t += 1 / self.fs
            s, el = self.state, self.t - self.state_since
            amp = self.base + self.offset
            if s == "fall":
                if el < 1.2:   # 넘어지는 순간: 크고 짧은 변동
                    amp = amp * (1 + 0.6 * self.sens_k * np.sin(2 * np.pi * 2.5 * el + self.phase_k) * np.exp(-el)
                                 + 0.15 * self.rng.normal(0, 1, N_SC))
                else:          # 바닥에 누운 자세로 바뀜(다중 경로 이동)
                    self.offset = self.offset + self.rng.normal(0, 3.0, N_SC)
                    self.state, self.state_since, s = self.after_fall, self.t, self.after_fall
                    amp = self.base + self.offset
            if s in ("sit_breath", "lying_breath"):
                m = 0.025 if s == "sit_breath" else 0.035
                amp = amp * (1 + m * self.sens_k * np.sin(2 * np.pi * self.breath_bpm / 60 * self.t + self.phase_k))
            elif s == "walk":
                self.walk = 0.92 * self.walk + 0.08 * self.rng.normal(0, 1, N_SC)
                amp = amp * (1 + 0.9 * self.sens_k * self.walk)
            amp = np.maximum(amp + self.rng.normal(0, 0.25, N_SC), 0.1)
            return amp * np.exp(1j * self.rng.uniform(-np.pi, np.pi, N_SC))  # 위상은 쓰지 않는다(ESP32도 불안정)

    def stream(self, stop: threading.Event):
        period = 1 / self.fs
        nxt = time.perf_counter()
        while not stop.is_set():
            yield self.next()
            nxt += period
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)


class SerialCSI:
    """ESP32 csi_recv 직렬 출력 읽기. 줄 끝의 "[i0,r0,i1,r1,...]"(부반송파마다 허수·실수 int8)를 복소수로 바꾼다."""
    LIST_RE = re.compile(r"\[([-\d,\s]+)\]")

    def __init__(self, port: str, baud: int = 921600):
        import serial  # pip install pyserial
        self.ser = serial.Serial(port, baud, timeout=1)
        self.label = f"ESP32 실측 ({port})"
        self.fs = FS

    def stream(self, stop: threading.Event):
        while not stop.is_set():
            line = self.ser.readline().decode("ascii", "ignore")
            if not line.startswith("CSI_DATA"):
                continue
            m = self.LIST_RE.search(line)
            if not m:
                continue
            v = np.array([int(x) for x in m.group(1).split(",") if x.strip()], dtype=np.float32)
            if len(v) < 2 * N_SC:
                continue
            c = v[1::2] + 1j * v[0::2]                      # 실수 + j·허수
            mag = np.abs(c)
            keep = np.nonzero(mag > 0)[0]                   # 가드·널 부반송파(0) 제외
            if len(keep) < N_SC:
                continue
            yield c[keep[:N_SC]]
