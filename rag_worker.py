"""망 내 AI 역할(노트북): 클라우드 관제(Render)의 요약 요청을 가져가 로컬 LLM·RAG로 요약을 만들고, 결과만 올린다.

어르신 돌봄 기록과 지침은 이 PC 안에만 있고, 클라우드로 가는 것은 요약 결과뿐이다(제안서 10쪽 'RAG 요약은 통신사 망 안에서').
사용: set SUMBI_KEY=... && python rag_worker.py --server https://<render 주소>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "rag"))
import sumbi_rag as R  # noqa: E402


def call(server: str, path: str, obj: dict | None = None) -> dict:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8") if obj is not None else None
    req = urllib.request.Request(server + path, data=data, headers={"Content-Type": "application/json",
                                                                     "x-sumbi-key": os.environ.get("SUMBI_KEY", "")})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    a = ap.parse_args()
    server = a.server.rstrip("/")
    index, residents = R.Index(), R.load_residents()
    R.embed(["준비"])
    R.chat_json('JSON으로 {"ok": true}만 답하라.', "준비")
    print(f"[작업기] 준비 완료 · 관제: {server}", flush=True)
    while True:
        try:
            for ev in call(server, "/api/rag/pending").get("events", []):
                r = R.summarize(index, residents, ev, use_rag=True)
                keep = {k: r[k] for k in ("final_priority", "llm_priority", "rule_floor", "rule_reasons", "output", "check", "latency_s", "context")}
                call(server, f"/api/rag/{ev['id']}", keep)
                print(f"[작업기] {ev['id']} 요약 {r['latency_s']}초 → {r['final_priority']}", flush=True)
        except Exception as e:
            print("[작업기] 대기:", e, file=sys.stderr, flush=True)
            time.sleep(2)
        time.sleep(1)


if __name__ == "__main__":
    main()
