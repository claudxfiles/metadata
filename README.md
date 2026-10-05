# app_

Herramientas de escritorio (Python + Tkinter, Windows) para trabajar con imágenes y videos generados por IA (ComfyUI, A1111/Forge, Civitai).

| App | Qué hace |
|---|---|
| **Metadata Viewer** | Lee prompt, parámetros y LoRAs de una imagen o video; resuelve nombres en Civitai/HF, revisa si el modelo está en disco y lo descarga. |
| **Prompt Gallery** | Indexa una carpeta y busca por texto del prompt o por LoRA, con grilla de miniaturas. Reutiliza los parsers de `metadata_viewer.py`. |
| **Civitai Scraper** | Busca imágenes en civitai.com por usuario, tag, modelo o versionId y descarga las elegidas. |
| **Viggle Chunk Joiner** | Une clips generados por pasadas con ffmpeg (concat, recorte de solape o crossfade). Requiere ffmpeg. |
| `strip_meta.py` | Borra la metadata de PNGs de ComfyUI, in-place. |

## Uso

Doble clic en el `.bat` de cada app. Los lanzadores usan el Python del venv de ComfyUI (`D:\Program Files\ComfyUI\venv`) si existe; si no, un `venv` local en esta carpeta.

Sin ComfyUI: ejecutar `Setup.bat` una vez. Instala Python 3.12 con winget si falta, crea `venv` e instala Pillow, requests y av.

## Token de Civitai

Las apps de Civitai leen el token de `_civitai_token.txt` junto al script (o de la variable `CIVITAI_TOKEN`). Ese archivo está en `.gitignore`; no subirlo.

## Documentación

- `CONOCIMIENTO-APPS.md`: cómo están construidas estas apps, más notas sobre Ref Map y Director Studio.
- `COMO-CONSTRUIR-ESTAS-APPS.md`: patrones para mantenerlas o crear nuevas.
