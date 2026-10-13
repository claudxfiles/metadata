@echo off
REM Ejecuta la app en un entorno limpio para que los diálogos estándar funcionen.
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
for /f "tokens=1 delims==" %%V in ('set DSH_ 2^>nul') do set "%%V="
set "SESSIONNAME="
if exist "%~dp0venv\Scripts\python.exe" (
  start "" "%~dp0venv\Scripts\python.exe" "%~dp0rds_dms_migrator.py" %*
) else (
  start "" python "%~dp0rds_dms_migrator.py" %*
)
