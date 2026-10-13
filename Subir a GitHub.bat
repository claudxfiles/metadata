@echo off
REM Instala Git si falta y sube las apps a https://github.com/claudxfiles/app_ (solo los archivos listados).
REM Resultado en _subida.log
cd /d "%~dp0"
call :run > "%~dp0_subida.log" 2>&1
exit /b

:run
set "PATH=%PATH%;C:\Program Files\Git\cmd"
where git >nul 2>&1 && goto havegit
echo Instalando Git para Windows con winget...
winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
set "PATH=%PATH%;C:\Program Files\Git\cmd"
where git >nul 2>&1 || (echo ERROR: Git no quedo disponible. Cierra y vuelve a ejecutar este archivo. & exit /b 1)
:havegit
git --version
if not exist ".git" git init
git add .gitignore README.md CONOCIMIENTO-APPS.md COMO-CONSTRUIR-ESTAS-APPS.md Setup.bat "Metadata Viewer.bat" "Prompt Gallery.bat" "Civitai Scraper.bat" "Viggle Chunk Joiner.bat" metadata_viewer.py prompt_gallery.py civitai_scraper.py viggle_chunk_joiner.py strip_meta.py
echo.
echo Archivos que se van a subir:
git status --short
echo.
git -c user.name=claudxfiles -c user.email=claudxfiles@users.noreply.github.com commit -m "Apps Tkinter de metadata para ComfyUI: viewer, gallery, scraper, joiner" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_012bC1nkLjYJKsNF3iqtMoPC"
git branch -M main
git remote get-url origin >nul 2>&1 || git remote add origin https://github.com/claudxfiles/app_.git
git push -u origin main
echo.
echo FIN
exit /b 0
