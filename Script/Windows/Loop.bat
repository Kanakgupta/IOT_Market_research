@echo off
title Wireless Market Research - Update Loop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Loop.ps1" %*
set "exitCode=%ERRORLEVEL%"
if not "%exitCode%"=="0" pause
exit /b %exitCode%