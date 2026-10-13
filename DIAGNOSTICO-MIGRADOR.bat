@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
echo === Diagnostico del Migrador CSV ===
"%~dp0venv\Scripts\python.exe" --version
"%~dp0venv\Scripts\python.exe" -m py_compile "%~dp0rds_dms_migrator.py"
if errorlevel 1 goto end
echo Sintaxis correcta.
echo Selector WinForms de archivos para probar; elige un CSV o cancela.
powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0selector_archivos.ps1" -Title "Prueba selector CSV" -Filter "CSV (*.csv)|*.csv|Todos (*.*)|*.*"
echo Codigo selector: %ERRORLEVEL%
echo Abriendo la app; prueba Agregar CSV.
"%~dp0venv\Scripts\python.exe" "%~dp0rds_dms_migrator.py"
echo Codigo de app: %ERRORLEVEL%
:end
echo.
echo Copia los resultados de esta ventana si hay problemas.
pause
