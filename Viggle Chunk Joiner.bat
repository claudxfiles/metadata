@echo off
REM Doble clic para abrir.
set "PYW=D:\Program Files\ComfyUI\venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%~dp0venv\Scripts\pythonw.exe"
if not exist "%PYW%" (echo No hay Python para esta app. Ejecuta Setup.bat primero.& pause & exit /b 1)
set "PYTHONUTF8=1"
start "" "%PYW%" "%~dp0viggle_chunk_joiner.py" %*
