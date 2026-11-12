@echo off
chcp 65001 >nul
rem 숨비 시연 실행: 관제 서버 + 게이트웨이 + 브라우저
rem 사용: start_demo.bat          (마이크로 대답 인식)
rem       start_demo.bat none     (마이크 없이, 항상 무응답)
rem       start_demo.bat mic COM5 (ESP32 실측 입력, 보드 연결 후)
cd /d %~dp0
set VOICE=%1
if "%VOICE%"=="" set VOICE=mic
set SOURCE=%2
if "%SOURCE%"=="" set SOURCE=sim
set PYTHONIOENCODING=utf-8

start "SUMBI control" cmd /k python control.py --port 8000
timeout /t 8 /nobreak >nul
start "SUMBI gateway" cmd /k python -u gateway.py --server http://127.0.0.1:8000 --voice %VOICE% --source %SOURCE%
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8000
