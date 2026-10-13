@echo off
REM Busca el Python de ComfyUI y deja el resultado en _diagnostico.txt (solo lectura, no cambia nada).
set "OUT=%~dp0_diagnostico.txt"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$o=@(); $o+='== Unidades'; $o+=(Get-PSDrive -PSProvider FileSystem | ForEach-Object { $_.Root + '  libre(GB)=' + [math]::Round($_.Free/1GB) });" ^
 "$o+='== Candidatos ComfyUI'; foreach($d in (Get-PSDrive -PSProvider FileSystem).Root){ foreach($s in 'Program Files\ComfyUI','ComfyUI','ComfyUI_windows_portable','Program Files\ComfyUI_windows_portable'){ $p=Join-Path $d $s; if(Test-Path $p){ $o+=('DIR ' + $p); Get-ChildItem $p -Recurse -Depth 3 -Filter pythonw.exe -ErrorAction SilentlyContinue | ForEach-Object { $o+=('  PYW ' + $_.FullName) } } } };" ^
 "$o+='== Python en PATH'; $o+=(where.exe python pythonw 2>&1 | Out-String);" ^
 "$o | Set-Content -Encoding UTF8 $env:OUT"
echo Listo. Resultado en _diagnostico.txt
timeout /t 3 >nul
