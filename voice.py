"""음성 확인: 게이트웨이 스피커로 묻고, 마이크로 듣고, 응답을 정상·도움 요청·무응답으로 나눈다.

- 말하기: Windows 내장 한국어 음성(Microsoft Heami, System.Speech) — 인터넷 없이 동작.
- 듣기: sounddevice로 녹음 → 소리가 없으면 Whisper를 돌리지 않고 '무응답'.
- 받아쓰기: Whisper small(로컬). 처음 한 번 모델을 메모리에 올린다.
- 분류: 규칙. 애매하면 '도움 요청'으로 올린다(불확실하면 상향, 제안서 8쪽).
- mode: "mic"(실제) / "none"(항상 무응답) / "ok"(항상 정상) / "help"(항상 도움 요청) — 마이크 없이 시험할 때.
"""
from __future__ import annotations

import os
import re
import subprocess
import time

import numpy as np

SR = 16000
HELP_WORDS = ["아파", "아프", "도와", "살려", "못 일어", "못일어", "일어날 수가 없", "119", "구급", "안 괜찮", "안괜찮", "괜찮지 않", "숨이", "어지러"]
OK_WORDS = ["괜찮", "괜찬", "문제없", "문제 없", "이상 없", "멀쩡"]


def say(text: str):
    """한국어 음성으로 말한다(끝날 때까지 기다림). 글자는 환경변수로 넘겨 명령 주입을 막는다. SUMBI_MUTE=1이면 말하지 않음(시험용)."""
    if os.environ.get("SUMBI_MUTE") == "1":
        time.sleep(min(4.0, 0.12 * len(text)))
        return
    env = dict(os.environ, SUMBI_TTS=text)
    ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "try { $s.SelectVoice('Microsoft Heami Desktop') } catch {}; $s.Rate=0; $s.Volume=100; $s.Speak($env:SUMBI_TTS)")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Listener:
    def __init__(self, model_name: str = "small"):
        self.model_name = model_name
        self.model = None

    def load(self):
        if self.model is None:
            import torch
            import whisper
            self.model = whisper.load_model(self.model_name, device="cuda" if torch.cuda.is_available() else "cpu")
        return self

    def record(self, seconds: float) -> np.ndarray:
        import sounddevice as sd
        a = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="float32")
        sd.wait()
        return a[:, 0]

    @staticmethod
    def has_voice(a: np.ndarray) -> bool:
        frames = a[: len(a) // 400 * 400].reshape(-1, 400)           # 25ms 단위
        rms = np.sqrt((frames ** 2).mean(axis=1))
        noise = np.percentile(rms, 20)
        return bool((rms > max(0.01, noise * 4)).sum() >= 8)          # 0.2초 이상 소리

    def transcribe(self, a: np.ndarray) -> str:
        self.load()
        import torch
        r = self.model.transcribe(a, language="ko", fp16=torch.cuda.is_available(), condition_on_previous_text=False)
        return r.get("text", "").strip()


def classify(text: str | None) -> str:
    if not text:
        return "무응답"
    t = re.sub(r"\s+", " ", text)
    if any(w in t for w in HELP_WORDS):
        return "도움 요청"
    if any(w in t for w in OK_WORDS):
        return "정상"
    return "도움 요청"   # 알아듣기 어려운 응답은 위급 쪽으로


def confirm(name: str, mode: str = "mic", listener: Listener | None = None, log=print) -> dict:
    """최대 2번 묻고 듣는다(약 25~30초). 반환: response, transcript, asked_at, answered_at"""
    t0 = time.time()
    prompts = [f"{name}, 괜찮으세요? 괜찮으시면 괜찮아요 라고 대답해 주세요.", f"{name}, 제 말 들리세요? 대답해 주세요."]
    for i, p in enumerate(prompts):
        log(f"[음성] 질문 {i + 1}: {p}")
        say(p)
        if mode != "mic":
            time.sleep(2)
            fixed = {"none": None, "ok": "괜찮아요", "help": "아파요, 못 일어나겠어요"}[mode]
            if fixed is None and i == 0:
                continue
            return {"response": classify(fixed), "transcript": fixed or "", "asked_at": t0, "answered_at": time.time()}
        a = listener.record(8.0)
        if not Listener.has_voice(a):
            log("[음성] 소리 없음")
            continue
        text = listener.transcribe(a)
        log(f"[음성] 받아쓰기: {text}")
        resp = classify(text)
        if resp == "정상":
            say("네, 알겠습니다. 내일 아침에 다시 안부를 여쭙겠습니다.")
        else:
            say("지금 관제 센터에 바로 알리겠습니다. 조금만 기다려 주세요.")
        return {"response": resp, "transcript": text, "asked_at": t0, "answered_at": time.time()}
    say("응답이 없어 관제 센터에 바로 알리겠습니다.")
    return {"response": "무응답", "transcript": "", "asked_at": t0, "answered_at": time.time()}
