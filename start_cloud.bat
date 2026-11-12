@echo off
chcp 65001 >nul
rem 클라우드(Render) 관제에 붙는 노트북 쪽 실행: 게이트웨이(집) + AI 요약 작업기(망 내 AI)
rem 사용: start_cloud.bat https://sumbi-control.onrender.com 키값 [mic^|none] [sim^|COM5]
rem 키값 = Render 대시보드 > sumbi-control > Environment > SUMBI_KEY
cd /d %~dp0
if "%~1"=="" (echo 사용: start_cloud.bat ^<Render 주소^> ^<SUMBI_KEY^> [mic^|none] [sim^|COM5] & exit /b 1)
set SERVER=%~1
set SUMBI_KEY=%~2
set VOICE=%3
if "%VOICE%"=="" set VOICE=mic
set SOURCE=%4
if "%SOURCE%"=="" set SOURCE=sim
set PYTHONIOENCODING=utf-8

rem 무료 요금제는 잠들어 있을 수 있어 먼저 깨운다(최대 약 1분)
curl -s -m 90 %SERVER%/healthz
echo.
start "SUMBI rag worker" cmd /k python -u rag_worker.py --server %SERVER%
start "SUMBI gateway" cmd /k python -u gateway.py --server %SERVER% --voice %VOICE% --source %SOURCE%
start "" "%SERVER%/?key=%SUMBI_KEY%"
