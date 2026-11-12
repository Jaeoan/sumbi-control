"""사용: python tools/phone_e2e.py <출력 폴더>  (Ollama 로컬 RAG 필요, 포트 8030)
가상 휴대폰 시험: 앱(ElderScreen)이 보내는 것과 같은 요청으로 관제 서버의 처리를 확인한다.
① 넘어짐 → 질문 2회 → 무응답 → 119 카운트다운 → AI 요약 → 자동 신고
② [도와주세요](sos) → 바로 도움 요청 → P1
관제 화면 캡처(휴대폰 입력 표시)도 남긴다."""
import json, math, os, subprocess, sys, time, urllib.request
from pathlib import Path

LIVE = Path(__file__).resolve().parents[1]  # demo/live
OUT = Path(sys.argv[1]).resolve(); OUT.mkdir(parents=True, exist_ok=True)
PORT = 8030; B = f"http://127.0.0.1:{PORT}"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
env = dict(os.environ, SUMBI_MUTE="1", PYTHONIOENCODING="utf-8", SUMBI_KEY="")
log = open(OUT / "e2e.log", "w", encoding="utf-8")


def get(p): return json.load(urllib.request.urlopen(B + p, timeout=5))
def post(p, d):
    r = urllib.request.Request(B + p, data=json.dumps(d, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=5))
def shot(name):
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1290,1150", "--force-device-scale-factor=2",
                    f"--screenshot={OUT / (name + '.png')}", f"{B}/?snapshot=1"], capture_output=True, timeout=60)


srv = subprocess.Popen([sys.executable, "control.py", "--rag", "local", "--port", str(PORT)], cwd=LIVE, env=env, stdout=log, stderr=log)
stop = False
try:
    for _ in range(120):
        try:
            if get("/healthz").get("rag") == "ready": break
        except Exception: pass
        time.sleep(1)
    # 휴대폰 특징값(1초마다): 가만히 놓인 휴대폰 → 넘어짐 순간 큰 값
    def feats(n, spike=False, steps=132):
        for i in range(n):
            w = [round(0.05 * math.sin(i + k), 2) for k in range(5)]
            if spike and i == n - 1: w = [0.1, -2.4, 2.5, 1.2, 0.0]
            post("/api/features", {"source": "휴대폰 센서(가속도·마이크)", "resident": "R-001", "state": "지켜보는 중", "motion_ratio": 1.1,
                                   "bpm": None, "breathing": None, "steps": steps, "wave": w, "raw_bytes": 50 * 12 * (i + 1), "rtt_ms": 40})
            time.sleep(0.2)
    feats(30)
    now = get("/api/alerts")["now"]
    # ① 넘어짐
    aid = "MTEST1"
    post("/api/event", {"id": aid, "phase": "detected", "kind": "fall", "kind_ko": "낙상 의심", "resident": "R-001", "name": "김○순(가명), 84세 여성",
                        "room": "침실", "detected_at": now, "same_day_falls": 1, "priority": "확인 중", "breathing": "측정 안 함", "source": "휴대폰",
                        "label": "휴대폰 센서 1차 감지(충격 3.4g, 자유 낙하 0.34초, 기울기 88°) → 관제에 '확인 중' 표시"})
    feats(3, spike=True)
    post("/api/event", {"id": aid, "phase": "asking", "asked": 1, "label": "음성 확인 질문 1: “어르신, 괜찮으세요?”"})
    time.sleep(6)
    post("/api/event", {"id": aid, "phase": "asking", "asked": 2, "label": "음성 확인 질문 2: “어르신, 제 말 들리세요?”"})
    time.sleep(6)
    post("/api/event", {"id": aid, "phase": "confirmed", "response": "무응답", "transcript": "", "breathing": "측정 안 함", "label": "음성 확인: 무응답"})
    t0 = time.time(); shot_cd = False
    while time.time() - t0 < 40:
        a = next(x for x in get("/api/alerts")["alerts"] if x["id"] == aid)
        st = (a.get("auto119") or {}).get("status")
        if st == "countdown" and a.get("summary") and not shot_cd:
            shot("phone_countdown"); shot_cd = True
        if st == "reported": break
        time.sleep(0.5)
    a = next(x for x in get("/api/alerts")["alerts"] if x["id"] == aid)
    print("FALL priority", a.get("priority"), "auto119", (a.get("auto119") or {}).get("status"),
          "summary_ok", bool(a.get("summary", {}).get("output")), "cites", a.get("summary", {}).get("check", {}).get("cited"), file=log, flush=True)
    print("FALL summary:", (a.get("summary", {}).get("output") or {}).get("summary"), file=log, flush=True)
    print("FALL timeline:", [(round(x["at"] - a["detected_at"], 1), x["label"]) for x in a["timeline"]], file=log, flush=True)
    # ② 본인 긴급 호출
    sid = "MTEST2"
    post("/api/event", {"id": sid, "phase": "detected", "kind": "sos", "kind_ko": "긴급 호출(본인)", "resident": "R-001", "name": "김○순(가명), 84세 여성",
                        "room": "침실", "detected_at": get("/api/alerts")["now"], "same_day_falls": 1, "priority": "확인 중", "breathing": "측정 안 함",
                        "source": "휴대폰", "label": "어르신이 직접 [도와주세요]를 누름 → 관제에 바로 알림"})
    post("/api/event", {"id": sid, "phase": "confirmed", "response": "도움 요청", "transcript": "(화면) 도와주세요", "breathing": "측정 안 함",
                        "label": "음성 확인: 도움 요청 (“(화면) 도와주세요”)"})
    t0 = time.time()
    while time.time() - t0 < 30:
        s = next(x for x in get("/api/alerts")["alerts"] if x["id"] == sid)
        if s.get("summary"): break
        time.sleep(0.5)
    print("SOS priority", s.get("priority"), "auto119", (s.get("auto119") or {}).get("status"), "final", s.get("summary", {}).get("final_priority"),
          "floor", s.get("summary", {}).get("rule_reasons"), file=log, flush=True)
    print("SOS summary:", (s.get("summary", {}).get("output") or {}).get("summary"), file=log, flush=True)
    shot("phone_after")
finally:
    srv.terminate()
