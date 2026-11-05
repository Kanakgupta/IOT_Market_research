@echo off
rem Double-click to run the Wireless Market Research Agent (full run, ~4 minutes).
rem Optional arguments: -Quick  (smoke test)   -Open  (open the report when done)
title Wireless Market Research Agent
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run_Agent.ps1" %*
echo.
pause
