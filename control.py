"""숨비 관제 서버: 게이트웨이가 보낸 특징값·경보를 웹 관제 화면과 요원 앱으로 전달하고, 경보마다 RAG 요약을 붙인다.

두 가지 방식으로 돈다.
- 로컬:  python control.py                 → RAG 요약을 이 PC(로컬 LLM)에서 직접 만든다. 인터넷 없이 시연할 때.
- 클라우드(Render): python control.py --rag worker
         → 요약은 노트북의 rag_worker.py(망 내 AI 역할)가 가져가 만들고 결과만 올린다. 어르신 기록은 클라우드에 두지 않는다.

119 자동 신고(시연): 낙상·호흡 이상 뒤 '무응답' 또는 '도움 요청'이면 15초 카운트다운 뒤 자동 신고. 관제·요원 앱에서 [취소]·[즉시 신고] 가능.
화재는 기다리지 않고 바로 신고. 실제 119로 발신하지 않는다(화면에 '시연' 표시). 출동 여부는 119 상황실이 정한다.

주요 주소: /api/features · /api/event(게이트웨이) · /api/stream(웹 화면 SSE) · /api/alerts(앱 조회)
          /api/alert/{id}/cancel · /api/alert/{id}/report_now(관제·앱) · /api/devices(앱 푸시 토큰 등록)
          /api/rag/pending · /api/rag/{id}(작업기) · /api/sim(시연 버튼) · /healthz
환경변수: SUMBI_KEY(쓰기 요청에 필요한 키, 비우면 검사 안 함), EXPO_ACCESS_TOKEN(선택), AUTO119_SECONDS(기본 15)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

HERE = Path(__file__).parent
KEY = os.environ.get("SUMBI_KEY", "")
AUTO119_S = int(os.environ.get("AUTO119_SECONDS", "15"))
MODE = {"rag": "local"}

subscribers: list[asyncio.Queue] = []
LOOP: asyncio.AbstractEventLoop | None = None
LOCK = threading.RLock()
STATE = {"sim": {"state": "sit_breath", "bpm": 15, "after_fall": "lying_breath", "fire": 0, "state_seq": 0, "seq": 0},
         "alerts": {}, "source": "", "sent_bytes": 0, "rag": "loading", "devices": {}, "pushes": []}
RAG = {"mod": None, "index": None, "residents": None}


# ------------------------------------------------------------------ 공통
def check_key(req: Request):
    q = (req.query_params.get("key") or "").replace(" ", "+")   # 주소창에서 '+'가 공백으로 바뀌는 경우
    if KEY and req.headers.get("x-sumbi-key") != KEY and q != KEY:
        raise HTTPException(401, "key")


def broadcast(msg: dict):
    if LOOP is None:
        return
    data = json.dumps(msg, ensure_ascii=False)
    for q in list(subscribers):
        LOOP.call_soon_threadsafe(q.put_nowait, data)


def push_alert(a: dict):
    with LOCK:
        a = json.loads(json.dumps(a, ensure_ascii=False))
    broadcast({"type": "alert", "alert": a, "now": time.time()})


def add_tl(a: dict, phase: str, label: str):
    a["timeline"].append({"phase": phase, "at": time.time(), "label": label})


# ------------------------------------------------------------------ 푸시(Expo)
def send_push(title: str, body: str, data: dict):
    tokens = list(STATE["devices"].keys())
    if not tokens:
        return
    msgs = [{"to": t, "title": title, "body": body, "data": data, "sound": "default", "priority": "high",
             "channelId": "emergency", "ttl": 60} for t in tokens]
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if os.environ.get("EXPO_ACCESS_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["EXPO_ACCESS_TOKEN"]

    def go():
        t0 = time.time()
        try:
            req = urllib.request.Request("https://exp.host/--/api/v2/push/send", data=json.dumps(msgs).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                res = json.loads(r.read().decode("utf-8"))
            STATE["pushes"].append({"at": t0, "ms": round((time.time() - t0) * 1000), "result": res})
            for tok, d in zip(tokens, res.get("data", [])):      # 앱을 지웠거나 토큰이 바뀐 폰은 목록에서 뺀다
                if (d.get("details") or {}).get("error") == "DeviceNotRegistered":
                    STATE["devices"].pop(tok, None)
        except Exception as e:
            STATE["pushes"].append({"at": t0, "error": str(e)})
        STATE["pushes"] = STATE["pushes"][-20:]

    threading.Thread(target=go, daemon=True).start()


# ------------------------------------------------------------------ RAG(로컬 모드)
def warm_rag():
    sys.path.insert(0, str(HERE.parent / "rag"))
    import sumbi_rag as R
    RAG["mod"] = R
    RAG["index"], RAG["residents"] = R.Index(), R.load_residents()
    R.embed(["준비"])                                     # 검색 모델도 미리 올린다
    R.chat_json('JSON으로 {"ok": true}만 답하라.', "준비")  # 생성 모델을 GPU에 올려 둔다
    STATE["rag"] = "ready"
    broadcast({"type": "status", "rag": "ready"})


def rag_event(a: dict) -> dict:
    return {"id": a["id"], "resident": a["resident"], "time": time.strftime("%H:%M", time.localtime(a["detected_at"])),
            "type": a["kind"], "room": a.get("room", "침실"), "breathing": a.get("breathing", "있음"),
            "response": a.get("response", "확인 생략"), "transcript": a.get("transcript", ""),
            "same_day_falls": a.get("same_day_falls", 1)}


def set_summary(aid: str, r: dict):
    with LOCK:
        a = STATE["alerts"].get(aid)
        if not a:
            return
        if a.get("auto119"):                       # 자동 신고가 걸린 경보는 P1 유지
            r["final_priority"] = "P1"
        a["priority"] = r["final_priority"]
        a["summary"] = {k: r[k] for k in ("llm_priority", "rule_floor", "rule_reasons", "output", "check", "latency_s")}
        a["summary"]["context"] = [{"id": c["id"], "kind": c["kind"], "text": c["text"]} for c in r["context"]]
        add_tl(a, "summary", f"AI 근거 요약 완료({r['latency_s']}초)")
    push_alert(a)


def summarize_local(aid: str):
    while RAG["index"] is None:
        time.sleep(0.3)
    R = RAG["mod"]
    r = R.summarize(RAG["index"], RAG["residents"], rag_event(STATE["alerts"][aid]), use_rag=True)
    set_summary(aid, r)


def request_summary(aid: str):
    if MODE["rag"] == "local":
        threading.Thread(target=summarize_local, args=(aid,), daemon=True).start()
    else:
        STATE["alerts"][aid]["rag_pending"] = True     # 노트북 작업기가 가져간다


# ------------------------------------------------------------------ 119 자동 신고(시연)
def start_auto119(aid: str, reason: str):
    with LOCK:
        a = STATE["alerts"][aid]
        if a.get("auto119"):
            return
        a["priority"] = "P1"
        a["auto119"] = {"status": "countdown", "deadline": time.time() + AUTO119_S, "seconds": AUTO119_S, "reason": reason}
        add_tl(a, "auto119", f"119 자동 신고 대기 {AUTO119_S}초 ({reason}, 관제 취소 가능)")
    push_alert(a)
    send_push(f"P1 {a.get('kind_ko', '')} · {a.get('response', '')}",
              f"{a.get('name', '')} · {AUTO119_S}초 뒤 119 자동 신고(시연). 앱에서 취소 가능", {"id": aid})

    def timer():
        time.sleep(AUTO119_S)
        report_119(aid, "자동")

    threading.Thread(target=timer, daemon=True).start()


def report_119(aid: str, how: str):
    with LOCK:
        a = STATE["alerts"].get(aid)
        if not a or a.get("auto119", {}).get("status") in ("reported", "cancelled"):
            return
        a["priority"] = "P1"
        a["auto119"] = {**a.get("auto119", {}), "status": "reported", "reported_at": time.time(), "how": how}
        add_tl(a, "reported", f"119 {how} 신고 완료(시연: 실제 발신 없음) — 출동 여부는 119 상황실이 결정")
    push_alert(a)
    send_push(f"119 {how} 신고(시연)", f"{a.get('name', '')} · {a.get('kind_ko', '')}", {"id": aid})


# ------------------------------------------------------------------ 앱
@asynccontextmanager
async def lifespan(app):
    global LOOP
    LOOP = asyncio.get_running_loop()
    if MODE["rag"] == "local":
        threading.Thread(target=warm_rag, daemon=True).start()
    else:
        STATE["rag"] = "worker"
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def home():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True, "rag": STATE["rag"], "devices": len(STATE["devices"]), "alerts": len(STATE["alerts"])}


@app.get("/api/stream")
async def stream(request: Request):
    q: asyncio.Queue = asyncio.Queue()
    subscribers.append(q)

    async def gen():
        try:
            hello = {"type": "hello", "alerts": list(STATE["alerts"].values()), "source": STATE["source"],
                     "rag": STATE["rag"], "now": time.time(), "keyless": not KEY}
            yield f"data: {json.dumps(hello, ensure_ascii=False)}\n\n"
            while not await request.is_disconnected():
                try:
                    yield f"data: {await asyncio.wait_for(q.get(), timeout=10)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/features")
async def features(req: Request):
    check_key(req)
    body = await req.body()
    f = json.loads(body)
    STATE["sent_bytes"] += len(body)
    STATE["source"] = f.get("source", "")
    f.update({"type": "features", "sent_bytes": STATE["sent_bytes"]})
    STATE["last_features"] = f
    STATE["wave"] = (STATE.get("wave", []) + f.get("wave", []))[-150:]
    broadcast(f)
    return {"ok": True}


@app.post("/api/event")
async def event(req: Request):
    check_key(req)
    body = await req.body()
    e = json.loads(body)
    STATE["sent_bytes"] += len(body)
    with LOCK:
        a = STATE["alerts"].setdefault(e["id"], {"id": e["id"], "timeline": []})
        a.update({k: v for k, v in e.items() if k not in ("timeline", "label")})
        add_tl(a, e["phase"], e.get("label", ""))
    push_alert(a)
    ph, resp = e["phase"], e.get("response")
    if ph == "detected":
        send_push(f"확인 중 · {a.get('kind_ko', '')}", f"{a.get('name', '')} · 음성 확인 진행 중", {"id": a["id"]})
    if ph == "fire":
        report_119(a["id"], "자동")
    if ph == "confirmed" and resp in ("무응답", "도움 요청"):
        start_auto119(a["id"], resp)
    if ph in ("confirmed", "fire"):
        request_summary(a["id"])
    return {"ok": True, "server_at": time.time()}


@app.get("/api/alerts")
def get_alerts():
    with LOCK:
        alerts = sorted(STATE["alerts"].values(), key=lambda a: -a.get("detected_at", 0))
        return {"now": time.time(), "alerts": alerts, "sent_bytes": STATE["sent_bytes"], "rag": STATE["rag"],
                "features": STATE.get("last_features", {}), "wave": STATE.get("wave", []), "keyless": not KEY}


@app.post("/api/alert/{aid}/cancel")
async def cancel(aid: str, req: Request):
    check_key(req)
    body = await req.body()
    who = json.loads(body).get("who", "관제") if body else "관제"
    with LOCK:
        a = STATE["alerts"].get(aid)
        if not a or a.get("auto119", {}).get("status") != "countdown":
            return {"ok": False}
        a["auto119"]["status"] = "cancelled"
        add_tl(a, "cancelled", f"119 자동 신고 취소({who}에서 직접 확인)")
    push_alert(a)
    return {"ok": True}


@app.post("/api/alert/{aid}/report_now")
async def report_now(aid: str, req: Request):
    check_key(req)
    report_119(aid, "즉시")
    return {"ok": True}


@app.post("/api/devices")
async def devices(req: Request):
    check_key(req)
    d = await req.json()
    if str(d.get("token", "")).startswith(("ExponentPushToken", "ExpoPushToken")):
        STATE["devices"][d["token"]] = {"platform": d.get("platform"), "at": time.time()}
        return {"ok": True, "devices": len(STATE["devices"])}
    raise HTTPException(400, "token")


@app.get("/api/pushes")
def pushes(req: Request):
    check_key(req)
    return {"pushes": STATE["pushes"], "devices": len(STATE["devices"])}


@app.get("/api/rag/pending")
def rag_pending(req: Request):
    check_key(req)
    with LOCK:
        out = []
        for a in STATE["alerts"].values():
            if a.pop("rag_pending", False):
                out.append(rag_event(a))
        return {"events": out}


@app.post("/api/rag/{aid}")
async def rag_result(aid: str, req: Request):
    check_key(req)
    set_summary(aid, await req.json())
    return {"ok": True}


@app.get("/api/sim")
def get_sim():
    return STATE["sim"]


@app.post("/api/sim")
async def set_sim(req: Request):
    check_key(req)
    s = await req.json()
    if s.get("fire"):
        STATE["sim"]["fire"] += 1
    if any(k in s for k in ("state", "bpm", "after_fall")):
        STATE["sim"]["state_seq"] += 1
    STATE["sim"].update({k: v for k, v in s.items() if k != "fire"})
    STATE["sim"]["seq"] += 1
    return STATE["sim"]


@app.post("/api/reset")
def reset(req: Request):
    check_key(req)
    STATE["alerts"].clear()
    STATE["sent_bytes"] = 0
    broadcast({"type": "reset"})
    return {"ok": True}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--rag", default="local", choices=["local", "worker"])
    a = ap.parse_args()
    MODE["rag"] = a.rag
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
