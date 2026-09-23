@echo off
cd /d "%~dp0"
python run.py desktop
if errorlevel 1 pause
