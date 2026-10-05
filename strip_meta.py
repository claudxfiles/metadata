"""Quita metadata (prompt/workflow/parameters) de PNGs generados por ComfyUI.

Uso:
    "D:/Program Files/ComfyUI/venv/Scripts/python.exe" strip_meta.py [carpeta]

Sin argumento usa D:/Program Files/ComfyUI/output.
Reescribe cada PNG in-place sin chunks de texto. Preserva pixeles.
Para no incrustar metadata de entrada: arrancar ComfyUI con --disable-metadata.
"""
import os
import sys
import pathlib
from PIL import Image
from PIL.PngImagePlugin import PngInfo

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else r"D:/Program Files/ComfyUI/output")
KEYS = ("prompt", "workflow", "parameters")

pngs = sorted(root.rglob("*.png"))
print(f"{len(pngs)} PNG en {root}")

limpiados = 0
for p in pngs:
    try:
        im = Image.open(p)
        im.load()
        if not any(k in im.info for k in KEYS):
            continue
        tmp = str(p) + ".tmp.png"
        im.save(tmp, format="PNG", pnginfo=PngInfo())
        os.replace(tmp, p)
        limpiados += 1
        print("limpiado:", p.relative_to(root))
    except Exception as e:
        print(f"ERROR {p}: {e}")

restantes = [
    str(p.relative_to(root))
    for p in root.rglob("*.png")
    if any(k in Image.open(p).info for k in KEYS)
]
print(f"\nlimpiados: {limpiados}   restantes con metadata: {restantes or 'NINGUNO'}")
