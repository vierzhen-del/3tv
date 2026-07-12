@echo off
rem 3tv 로컬 실행 (Galaxy Book fallback)
rem 사용법: run_local.bat us   또는   run_local.bat kr
rem Windows 작업 스케줄러 등록:
rem   us 세션 - 평일 05:55, 인수 "us"
rem   kr 세션 - 평일 07:55, 인수 "kr"
rem 사전 준비: 리포 루트에 .env 작성 (README 참고), py -3.11 + ffmpeg 설치

setlocal
cd /d "%~dp0.."
set PYTHONPATH=src

if "%1"=="" (
    echo 사용법: run_local.bat [us^|kr]
    exit /b 1
)

py -3.11 -m threetv.main --session %1
endlocal
