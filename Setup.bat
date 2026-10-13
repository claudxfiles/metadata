@echo off
REM Prepara un Python local para estas apps cuando no hay venv de ComfyUI.
REM Crea .\venv e instala Pillow, requests y av (PyAV). No toca nada fuera de esta carpeta,
REM salvo instalar Python 3.12 con winget si no hay ninguno.
setlocal
cd /d "%~dp0"
set "CAND=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
set "PY="
py -3 -c "import tkinter" >nul 2>&1 && set "PY=py -3"
if defined PY goto have
python -c "import tkinter" >nul 2>&1 && set "PY=python"
if defined PY goto have
if exist "%CAND%" set PY="%CAND%"
if defined PY goto have

echo No hay Python en este PC. Instalando Python 3.12 con winget (unos 25 MB)...
winget install -e --id Python.Python.3.12
if exist "%CAND%" set PY="%CAND%"
if defined PY goto have
echo.
echo No pude ubicar Python tras la instalacion. Cierra esta ventana y ejecuta Setup.bat de nuevo.
pause
exit /b 1

:have
echo Usando: %PY%
if not exist "venv\Scripts\python.exe" %PY% -m venv venv
if not exist "venv\Scripts\python.exe" goto fail
echo Instalando Pillow, requests y av...
"venv\Scripts\python.exe" -m pip install Pillow requests av
if errorlevel 1 goto fail
"venv\Scripts\python.exe" -c "import tkinter, PIL, requests, av; print('TODO OK: tkinter, Pillow', PIL.__version__, ', requests', requests.__version__, ', av', av.__version__)"
if errorlevel 1 goto fail
echo.
echo Listo. Ya puedes abrir los .bat de las apps.
pause
exit /b 0

:fail
echo.
echo Algo fallo. Copia el texto de esta ventana y pasaselo a Claude.
pause
exit /b 1
