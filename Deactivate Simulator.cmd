@echo off
setlocal
title OpenXR Simulator - Deactivate
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deactivate_simulator.ps1" %*
set "exitCode=%errorlevel%"
if not "%exitCode%"=="0" pause
exit /b %exitCode%
