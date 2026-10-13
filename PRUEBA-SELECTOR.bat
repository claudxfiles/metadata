@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
for /f "tokens=1 delims==" %%V in ('set DSH_ 2^>nul') do set "%%V="
set "SESSIONNAME="
"%~dp0venv\Scripts\python.exe" -c "import tkinter as tk; from tkinter import filedialog; r=tk.Tk(); r.withdraw(); print('Prueba selector CSV'); p=filedialog.askopenfilename(title='Selecciona un CSV', filetypes=[('Archivos CSV','*.csv')]); print('RESULTADO:', p); r.destroy()"
echo Selector termino: %ERRORLEVEL%
pause
