"""숨비 게이트웨이(집 안): CSI → 1차 감지 → 음성 확인 → 관제 서버 전송. 원본 CSI는 밖으로 보내지 않는다.

사용:
  python gateway.py                                  # 시뮬레이션 입력 + 실제 마이크
  python gateway.py --voice none                     # 마이크 없이(항상 무응답)
  python gateway.py --source COM5                    # ESP32 실측(보드 연결 후)
  python gateway.py --server http://<관제 PC IP>:8000 # 관제 서버가 다른 PC(5G 핫스팟 너머)일 때
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

from csi_source import N_SC, SerialCSI, SimCSI
from features import Detector
from voice import Listener, confirm, say

HERE = Path(__file__).parent
KIND_KO = {"fall": "낙상 의심", "breath_absent": "호흡 신호 없음", "fire": "화재 감지"}


class Gateway:
    def __init__(self, a):
        self.a = a
        self.server = a.server.rstrip("/")
        self.src = SimCSI() if a.source == "sim" else SerialCSI(a.source)
        self.det = Detector(self.src.fs, N_SC)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.busy = False
        self.n = 0
        self.falls_today = 0
        self.last_feat: dict = {}
        self.rtt_ms: float | None = None
        self.sim_seq, self.state_seq, self.fire_seen = -1, -1, 0
        res = json.loads((HERE.parent / "rag" / "data" / "residents.json").read_text(encoding="utf-8"))["residents"]
        self.res = {r["id"]: r for r in res}[a.resident]
        self.listener = Listener().load() if a.voice == "mic" else None

    # ------------------------------------------------------------ 통신
    def post(self, path: str, obj: dict) -> dict | None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.server + path, data=data, headers={"Content-Type": "application/json",
                                                                              "x-sumbi-key": os.environ.get("SUMBI_KEY", "")})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                out = json.loads(r.read().decode("utf-8"))
            self.rtt_ms = round((time.perf_counter() - t0) * 1000, 1)
            return out
        except Exception as e:  # 관제가 잠시 끊겨도 감지는 계속
            print("[전송 실패]", path, e, file=sys.stderr)
            return None

    def get(self, path: str) -> dict | None:
        try:
            with urllib.request.urlopen(self.server + path, timeout=3) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    # ------------------------------------------------------------ 루프
    def packets(self):
        for c in self.src.stream(self.stop):
            with self.lock:
                self.det.push(c)

    def poll_sim(self):
        if not isinstance(self.src, SimCSI):
            return
        s = self.get("/api/sim")
        if not s or s["seq"] == self.sim_seq:
            return
        self.sim_seq = s["seq"]
        if s.get("fire", 0) > self.fire_seen:
            self.fire_seen = s["fire"]
            if self.sim_seq > 0:
                threading.Thread(target=self.handle, args=("fire",), daemon=True).start()
        if s.get("state_seq", 0) == self.state_seq:
            return
        self.state_seq = s.get("state_seq", 0)
        st = s.get("state")
        if st == "fall":
            self.src.set_state("fall", after_fall=s.get("after_fall", "lying_breath"))
        elif st:
            self.src.set_state(st, bpm=s.get("bpm"))

    def sender(self):
        """특징값 전송은 따로: 네트워크가 느려도 1초 감지 주기가 밀리지 않게(최신 것만 보냄)"""
        while not self.stop.is_set():
            f = self.outq.get()
            while not self.outq.empty():
                f = self.outq.get_nowait()
            self.post("/api/features", f)

    def sim_poller(self):
        while not self.stop.is_set():
            self.poll_sim()
            time.sleep(0.5)

    def run(self):
        import queue
        self.outq = queue.Queue()
        for fn in (self.packets, self.sender, self.sim_poller):
            threading.Thread(target=fn, daemon=True).start()
        print(f"[게이트웨이] 입력: {self.src.label} · 관제: {self.server} · 대상자: {self.a.resident} · 음성: {self.a.voice}", flush=True)
        t0 = time.time()
        nxt = time.time() + 1
        while not self.stop.is_set():
            time.sleep(max(0, nxt - time.time()))
            nxt += 1
            with self.lock:
                feat, events = self.det.step(time.time() - t0)
            feat.update({"source": self.src.label, "rtt_ms": self.rtt_ms, "resident": self.a.resident})
            self.last_feat = feat
            self.outq.put(feat)
            for e in events:
                print(f"[감지] {e}", flush=True)
                if not self.busy:
                    threading.Thread(target=self.handle, args=(e["type"],), daemon=True).start()

    # ------------------------------------------------------------ 경보 처리
    def handle(self, kind: str):
        if kind != "fire":
            self.busy = True
        try:
            self.n += 1
            aid = f"A{self.n:03d}"
            if kind == "fall":
                self.falls_today += 1
            r = self.res
            base = {"id": aid, "kind": kind, "kind_ko": KIND_KO[kind], "resident": r["id"],
                    "name": ",".join(r["profile"].split(",")[:2]), "room": self.a.room, "detected_at": time.time(),
                    "same_day_falls": max(1, self.falls_today)}
            if kind == "fire":   # 확인 없이 즉시(제안서 4쪽)
                self.post("/api/event", {**base, "phase": "fire", "priority": "P1", "response": "확인 생략",
                                         "breathing": "있음" if self.last_feat.get("breathing") else "없음",
                                         "label": "화재 감지 → 119 자동 신고(시연) · 확인 생략"})
                return
            self.post("/api/event", {**base, "phase": "detected", "priority": "확인 중",
                                     "breathing": "있음" if self.last_feat.get("breathing") else "없음",
                                     "label": "게이트웨이 1차 감지 → 관제에 '확인 중' 표시"})
            res = confirm(self.a.call_name, self.a.voice, self.listener)
            # 넘어지는 순간의 큰 변동이 호흡 계산 구간(20초)에서 빠진 뒤의 값으로 보고한다
            while time.time() - base["detected_at"] < 15:
                time.sleep(0.5)
            breathing = "있음" if self.last_feat.get("breathing") else "없음"
            self.post("/api/event", {"id": aid, "phase": "confirmed", "response": res["response"],
                                     "transcript": res["transcript"], "breathing": breathing,
                                     "confirm_s": round(res["answered_at"] - base["detected_at"], 1),
                                     "label": f"음성 확인: {res['response']}" + (f" (“{res['transcript']}”)" if res["transcript"] else "")})
        finally:
            if kind != "fire":
                self.busy = False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--source", default="sim", help="sim 또는 ESP32 직렬 포트(COM5 등)")
    ap.add_argument("--voice", default="mic", choices=["mic", "none", "ok", "help"])
    ap.add_argument("--resident", default="R-001")
    ap.add_argument("--call-name", default="어르신")
    ap.add_argument("--room", default="침실")
    gw = Gateway(ap.parse_args())
    try:
        gw.run()
    except KeyboardInterrupt:
        gw.stop.set()
