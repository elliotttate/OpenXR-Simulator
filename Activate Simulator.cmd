@echo off
setlocal
title OpenXR Simulator - Activate
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0activate_simulator.ps1" %*
set "exitCode=%errorlevel%"
if not "%exitCode%"=="0" pause
exit /b %exitCode%
