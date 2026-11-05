@echo off
rem Double-click to start the local web server and open the site in your browser.
rem Optional arguments: -Port 9000   -NoBrowser
title Wireless Intelligence - local server
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start_Server.ps1" %*
pause
