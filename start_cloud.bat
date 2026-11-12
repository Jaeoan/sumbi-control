@echo off
chcp 65001 >nul
rem 클라우드(Render) 관제에 붙는 노트북 쪽 실행: 게이트웨이(집) + AI 요약 작업기(망 내 AI)
rem 사용: start_cloud.bat [mic^|none] [sim^|COM5]            (.env의 SUMBI_SERVER·SUMBI_KEY 사용)
rem   또는 start_cloud.bat ^<Render 주소^> ^<SUMBI_KEY^> [mic^|none] [sim^|COM5]
rem .env는 저장소에 올리지 않는다(.gitignore)
cd /d %~dp0
if exist .env for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
set "A1=%~1"
if /i "%A1:~0,4%"=="http" (
  set "SUMBI_SERVER=%~1"
  set "SUMBI_KEY=%~2"
  set "VOICE=%~3"
  set "SOURCE=%~4"
) else (
  set "VOICE=%~1"
  set "SOURCE=%~2"
)
if "%SUMBI_SERVER%"=="" (echo .env에 SUMBI_SERVER·SUMBI_KEY를 적거나, start_cloud.bat ^<Render 주소^> ^<SUMBI_KEY^> 로 실행 & exit /b 1)
if "%VOICE%"=="" set VOICE=mic
if "%SOURCE%"=="" set SOURCE=sim
set "SERVER=%SUMBI_SERVER%"
set PYTHONIOENCODING=utf-8

rem 무료 요금제는 잠들어 있을 수 있어 먼저 깨운다(최대 약 1분)
echo 관제 서버 깨우는 중: %SERVER%
curl -s -m 90 %SERVER%/healthz
echo.
start "SUMBI rag worker" cmd /k python -u rag_worker.py --server %SERVER%
start "SUMBI gateway" cmd /k python -u gateway.py --server %SERVER% --voice %VOICE% --source %SOURCE%
start "" "%SERVER%/?key=%SUMBI_KEY%"
