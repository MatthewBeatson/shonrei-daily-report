@echo off
rem Starts Household Budget and restarts it if it ever stops.
rem Put a shortcut to this file in your Startup folder (see docs/HOME-SETUP.md).
cd /d "%~dp0.."
title Household Budget
if not exist data mkdir data
:loop
echo [%date% %time%] starting >> data\server.log
node --disable-warning=ExperimentalWarning src\server.js >> data\server.log 2>&1
echo [%date% %time%] stopped, restarting in 10s >> data\server.log
timeout /t 10 /nobreak >nul
goto loop
