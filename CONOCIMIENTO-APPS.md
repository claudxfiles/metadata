# Conocimiento de apps propias y adaptadas — guía consolidada

Documento único para pasarle a otro agente. Escrito a partir del código real (2026-10-03).

## Índice

- **Parte A — Apps Python/Tkinter de `Downloads\metadata`** (creadas acá): Metadata Viewer, Civitai Scraper, Viggle Chunk Joiner, strip_meta.
- **Parte B — Ref Map** (app Electron de terceros, parcheada a inglés + agent-kit de prompts).
- **Parte C — Director Studio** (repo open source clonado y adaptado localmente).
- **Lecciones transferibles** (al final).

> **Secretos — no compartir nunca:** `Downloads\metadata\_civitai_token.txt`, `director\.env`, `Director-Studio\backend\.env`, `director\gmail_credentials.json`. Este documento no contiene ninguno.

---

# PARTE A — Apps de `Downloads\metadata`

## 1. Qué hay en la carpeta

| Archivo | Qué es | Líneas |
|---|---|---|
| `metadata_viewer.py` + `Metadata Viewer.bat` | GUI que lee prompt / parámetros / LoRAs de imágenes y videos generados por IA (ComfyUI, A1111/Forge, Civitai). Resuelve nombres en Civitai/HF, verifica si el modelo existe en disco y lo descarga. | ~1600 |
| `civitai_scraper.py` + `Civitai Scraper.bat` | GUI que busca imágenes en civitai.com (por usuario / tag / modelo / versionId), las muestra en grilla con checkbox y descarga las elegidas (+ `.txt` con el prompt si existe). Sesiones guardables. | ~820 |
| `viggle_chunk_joiner.py` + `Viggle Chunk Joiner.bat` | GUI sobre ffmpeg que une clips generados por pasadas (chunks) con solape: concat sin recodificar, recorte del solape o crossfade; mux de audio externo; planificador de pasadas. | ~760 |
| `strip_meta.py` | Script de consola: borra los chunks de texto (`prompt`/`workflow`/`parameters`) de PNGs de ComfyUI, in-place. | 42 |
| `_civitai_token.txt` | Token API de Civitai compartido por las dos apps Civitai. **Secreto: no copiar ni compartir.** | – |
| `_civitai_cache.json` | Caché de resoluciones Civitai/HF (versionId, hash, nombre de archivo). | – |
| `_civitai_downloaded_ids.log` | IDs de imágenes ya descargadas (dedupe global del scraper). | – |
| `civitai_sessions/*.json` | Sesiones guardadas del scraper. | – |
| `*.md`, `*.patch`, `*.diff` | Handoffs y parches de ComfyUI (no son parte de las apps). | – |

---

## 2. Filosofía / decisiones de diseño

1. **Un solo archivo `.py` por app, sin dependencias nuevas.** Solo stdlib + lo que ya trae el venv de ComfyUI (`Pillow`, `requests`, `av`/PyAV). Nada de `pip install` extra, nada de frameworks web.
2. **Se ejecuta con el Python del venv de ComfyUI**, no con un Python del sistema:
   `D:\Program Files\ComfyUI\venv\Scripts\pythonw.exe` (con `w` = sin consola).
3. **Lanzador `.bat` de doble clic** al lado de cada `.py`. Soporta arrastrar archivos encima (`%*`).
4. **GUI con Tkinter + ttk**, tema oscuro estilo Discord, interfaz y comentarios **en español**.
5. **Nunca bloquear el hilo de la UI**: todo lo de red/disco/ffmpeg va en hilos daemon que se comunican por una `queue.Queue` (ver §4).
6. **Estado persistente en archivos junto al script** (`HERE = os.path.dirname(os.path.abspath(__file__))`): token, caché, logs, sesiones. Nada en `%APPDATA%` ni en el registro.
7. **Tolerante a fallos**: cada parser devuelve `None` si no aplica y se prueba el siguiente; los errores de red se cachean o se muestran en la barra de estado, nunca crashean la app.
8. **Descargas seguras**: escribir a `archivo.part` y `os.replace()` al terminar; borrar el `.part` si falla o se cancela.
9. **Rutas del usuario hardcodeadas como constantes al inicio del archivo** (raíces de modelos D: y E:, fallback de ffmpeg). Si se porta a otra máquina, cambiar solo esas constantes.

---

## 3. Plantilla del lanzador `.bat`

```bat
@echo off
REM Doble clic para abrir, o arrastra un archivo sobre este .bat.
set "PYW=D:\Program Files\ComfyUI\venv\Scripts\pythonw.exe"
set "PYTHONUTF8=1"
start "" "%PYW%" "%~dp0mi_app.py" %*
```

- `PYTHONUTF8=1` evita errores de encoding con nombres/prompts con tildes o emojis.
- `%~dp0` = carpeta del `.bat`, así funciona aunque se mueva la carpeta.
- `start ""` lo despega de la consola (la ventana negra se cierra sola).
- En el `.py`, el archivo arrastrado llega por `sys.argv`:
  ```python
  if __name__ == "__main__":
      arg = next((a for a in sys.argv[1:] if os.path.isfile(a)), None)
      App(arg).mainloop()
  ```

---

## 4. Esqueleto de una app (patrón común a las tres)

```python
# -*- coding: utf-8 -*-
"""
Nombre App - qué hace en una línea.

Soporta / Uso / Notas de la API (estado verificado y fecha).
"""
import os, sys, json, queue, threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "_civitai_token.txt")

# ------------------------------------------------ lógica pura (sin Tk)
def hacer_algo(path):
    ...                    # funciones testeables, devuelven dicts simples

# ------------------------------------------------ GUI
BG = "#1e1f22"; BG2 = "#2b2d31"; FG = "#e3e5e8"; ACC = "#5865f2"; MUT = "#949ba4"
GOOD = "#3ba55d"; BAD = "#e06c6c"; WARN = "#d9a441"

class App(tk.Tk):
    def __init__(self, initial=None):
        super().__init__()
        self.title("Nombre App"); self.geometry("1040x720"); self.configure(bg=BG)
        self._q = queue.Queue()
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=BG2)
        st.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        st.configure("Muted.TLabel", background=BG2, foreground=MUT, font=("Segoe UI", 9))
        st.configure("TButton", font=("Segoe UI", 9), padding=6)
        st.map("TButton", background=[("active", ACC)])
        # ... widgets ...
        self.after(120, self._pump)

    # hilos empujan (kind, payload) a la cola; SOLO el hilo UI toca widgets
    def _pump(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "status":   self.status.configure(text=payload)
                elif kind == "progress": self.pb.configure(value=payload)
                elif kind == "error":  messagebox.showerror("Error", payload)
                # ... más tipos según la app ...
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _start(self, fn):
        def wrap():
            try:
                fn()
            except Exception as e:
                self._q.put(("error", str(e)))
        threading.Thread(target=wrap, daemon=True).start()

if __name__ == "__main__":
    App().mainloop()
```

Reglas del patrón:
- **Tkinter no es thread-safe**: los workers nunca llaman métodos de widgets; solo `self._q.put(...)`.
- Mensajes típicos: `status`, `progress`, `error`, `done`, `log`, `thumb`, `row`, `open` (abrir URL en navegador), `batch_done`.
- Bandera `self._busy` para deshabilitar botones mientras corre un worker; `self._cancel` (bool) que el worker revisa dentro del loop para cancelar.
- Progreso: `ttk.Progressbar(mode="determinate", maximum=1000)` y se envía la fracción × 1000.
- Widgets usados: `ttk.Notebook` (pestañas), `ttk.Treeview` (tablas con `tag_configure` para colorear ok/maybe/no), `tk.Text` con tags (`"h"` encabezado acento, `"mut"` gris) en `state="disabled"` para solo-lectura, grilla de `ttk.Frame` + `ImageTk.PhotoImage` para miniaturas.
- **Guardar referencias a `PhotoImage`** (`self._thumb`, `self._photo_cache[id]`) o Tk las borra por GC.
- Portapapeles: `self.clipboard_clear(); self.clipboard_append(texto)`; feedback cambiando el título 1.2 s.
- Abrir carpeta/archivo resultado: `os.startfile(path)`.

---

## 5. Conocimiento específico por app

### 5.1 Metadata Viewer — dónde vive la metadata y cómo se parsea

Pipeline de `extract(path)` (cada paso devuelve `None` si no aplica y cae al siguiente):

1. **Video** (`.mp4 .m4v .mov .webm .mkv .avi`) → `extract_video` con **PyAV** (`import av` dentro de la función, error amigable si falta). Lee `container.metadata`; ComfyUI (`VHS_VideoCombine`) mete un JSON `{"prompt":..., "workflow":...}` en el tag `comment` (en webm/mkv las claves vienen en MAYÚSCULAS → normalizar a minúsculas). Poster = primer frame decodificado. Se agregan codec/fps/frames/duración a PARÁMETROS.
2. **Imagen** → `Image.open(path).info` (chunks tEXt/iTXt del PNG) + **EXIF UserComment** (`getexif().get_ifd(0x8769).get(0x9286)`), que en JPG/WEBP de A1111/Civitai viene con prefijo `UNICODE\0\0` (UTF-16 LE/BE) o `ASCII\0\0\0`.
3. Si hay campo `prompt` → es el **grafo API de ComfyUI** → `_walk_graph(json.loads(...))`.
4. Si hay `parameters` que empieza con `{` → `_try_graph`: intenta `json.loads` tolerando cortes (`+ "}"`, `+ "}}"`, recortar al último `}`); si falla, `_salvage_civitai` rescata por regex (EXIF cortado por límite de tamaño).
5. Si no, `parameters` es texto **A1111/Forge** → `_parse_a1111`.

**`_walk_graph` (grafo ComfyUI/Civitai):**
- Recorre nodos `{id: {"class_type", "inputs"}}`. Busca recursos en inputs `lora_name, ckpt_name, model_name, unet_name, upscale_model, vae_name`.
- Soporta stacks de LoRA: rgthree *Power Lora Loader* (sub-dicts `{"on","lora","strength"}`, respetar `on: False`), `easy loraStack` / `*LoraStack*` (claves `lora_N_name` + `lora_N_strength`/`model_str_N`), ignora `"None"`.
- Sampler = nodo con `KSampler`/`SamplerCustom` en `class_type`, o con `sampler_name` + seed/steps/cfg.
- Prompt: seguir la referencia `["nodeId", slot]` de `positive`/`negative` recursivamente hasta un input `text`/`text_g`/`prompt`/`string` (con `seen` para evitar ciclos).
- Civitai mete `extraMetadata` (JSON **escapado dentro de un string**) con `prompt`, `negativePrompt`, `steps`, `cfgScale`, `sampler`, `resources[{modelVersionId, strength}]` → tiene prioridad.
- Dedup de recursos por `version_id` y de LoRAs por (nombre, peso) — workflows Wan tienen nodos HIGH/LOW repetidos.

**URN AIR de Civitai:** `urn:air:<base>:<tipo>:civitai:<modelId>@<versionId>`
```python
AIR_RE = re.compile(r"urn:air:([a-z0-9.\-]*):([a-z]+):civitai:(\d+)@(\d+)", re.I)
# página:   https://civitai.com/models/{mid}?modelVersionId={vid}
# descarga: https://civitai.com/api/download/models/{vid}
```

**A1111/Forge:** el texto es `positivo\nNegative prompt: negativo\nSteps: 30, Sampler: ..., Hashes: {...}`. Detalles que rompen parsers ingenuos:
- Puede no haber `Negative prompt:` → cortar en la primera línea que empiece con `Steps|Sampler|CFG scale|Seed|Schedule type:`.
- Separar parámetros por comas **solo a profundidad 0 de llaves** (el valor `Hashes: {...}` contiene comas).
- `Version:` a veces viene pegado sin coma → `re.split(r"\s+(?=Version:\s)")`.
- LoRAs inline: `<lora:nombre:peso>` (regex `A1111_LORA`); se quitan del prompt mostrado.
- `Hashes` trae AutoV2 por LoRA (`"lora:nombre": "abc123..."`) → sirve para resolver por hash.

**Resolución de nombres (red), siempre cacheada en `_civitai_cache.json`:**
| Entrada | Endpoint | Clave caché |
|---|---|---|
| versionId | `GET https://civitai.com/api/v1/model-versions/{vid}` | `"<vid>"` |
| hash AutoV2/SHA256 | `GET https://civitai.com/api/v1/model-versions/by-hash/{hash}` | `"h:<hash>"` |
| solo nombre de archivo | `GET /api/v1/models?types=LORA&query=<limpio>` y luego HF `GET https://huggingface.co/api/models?search=` + `/api/models/{id}` (`siblings[].rfilename`) | `"f:<stem>"` |

- Antes de buscar por nombre, limpiar ruido (`bf16, fp8, v1.2, rank, lora, high, low, epoch…`) con `_QUERY_NOISE`.
- Puntaje de match: nombre exacto 1000, subcadena 500+, si no intersección de palabras; mínimo 3.
- **404 en Civitai = modelo borrado** → apuntar a `https://civarchive.com/models/{modelId}` (Civitai purgó mucho contenido).
- Auth: header `Authorization: Bearer <token>` para la API; para descargas además `?token=<token>` en la URL. Si la descarga responde `text/html` o `application/json` → falta login/token.
- El nombre final del archivo descargado sale de `Content-Disposition: filename="..."`.

**"¿Lo tengo?" (índice local sin hashear):**
- `os.walk` de `D:\Program Files\ComfyUI\models` y `E:\Program Files\ComfyUI\models` (extensiones `.safetensors .ckpt .pt .pth .gguf .sft .bin`). **No hashear al indexar** (lento en HDD; un plugin que lo hacía colgaba ComfyUI).
- Índices: `by_name`, `by_stem`, `by_vid` (leído de sidecars `.civitai.info` / `.json` de civitai-helper/Stability Matrix).
- Orden de match: versionId por sidecar → nombre exacto que da Civitai → AutoV2 incrustado en el nombre → fuzzy.
- Fuzzy (`_fuzzy_local`): normalizar a `[a-z0-9]` (colapsa `Realistic Snapshot` vs `RealisticSnapshot`), partir camelCase, quitar stopwords genéricas de la colección (`lora, xl, flux, wan, krea, bf16…`), y combinar Jaccard ≥ 0.5, cobertura total de tokens ≥ 4 letras, `difflib.SequenceMatcher` ≥ 0.72. Resultado `ok` / `maybe` / `no` → colores verde/ámbar/rojo en la tabla.
- "Verificar hash" = SHA256 en bloques de 1 MB con callback de progreso, solo bajo demanda.

**UX que conviene conservar:** sin selección, "Descargar" baja solo LoRAs (evita bajar sin querer un checkpoint de 6 GB); si una descarga falla, abrir la página para descarga manual; "Guardar workflow .json" para arrastrarlo al canvas de ComfyUI; mensaje explícito cuando no hay metadata (re-guardado, screenshot o red social la borran).

### 5.2 Civitai Scraper — quirks de la API de imágenes

Documentados en el docstring (verificado 2026-09-06), importantes:
- `GET /api/v1/images` devuelve **`meta: null` en casi todas las imágenes** (bug conocido de Civitai) → casi nunca hay prompt; se guarda la imagen igual y se omite el `.txt`. Para captions hay que captionar localmente (JoyCaption en ComfyUI).
- `/images` **no filtra por tag** → modo tag = `GET /models?tag=...` y luego `/images?modelVersionId=` por cada versión.
- `period` aplicarlo **solo en `/images`**; en `/models` filtra por actividad del modelo y devuelve casi vacío.
- Sorts válidos: `/images` → `Most Reactions | Most Comments | Newest`; `/models` → `Highest Rated | Most Downloaded | Newest`.
- Paginación por cursor: seguir `data["metadata"]["nextPage"]` (y pasar `params=None` después de la primera llamada porque la URL ya los trae).
- `api_get` con reintentos: 429/5xx → backoff `min(2**intento, 30)` s, hasta 6 intentos; 401 → error claro de token. Un `STATUS_HOOK` global deja que la GUI muestre los reintentos en vivo.

Diseño:
- Las fuentes son **generadores** (`build_source_iter`) → "Cargar más" sigue consumiendo el mismo iterador.
- Filtros en cliente: tamaño mínimo, IDs ya vistos, IDs ya descargados alguna vez (`_civitai_downloaded_ids.log`, dedupe global entre carpetas), y exclusión por tags/prompt con match de palabra completa (`(?<!\w)term(?!\w)` para que `male` no excluya `female`).
- Imágenes se guardan como `{id}.jpg` (quality 95, convertir RGBA/P → RGB) + `{id}.txt` con el prompt sin `<lora:...>`; log local `downloaded_urls.log` en la carpeta destino.
- Sesiones: JSON con todo el estado de la UI + items con su check; al restaurar se reconstruye el iterador y se saltan los IDs vistos.

### 5.3 Viggle Chunk Joiner — ffmpeg desde Python

- Localizar ffmpeg: `shutil.which("ffmpeg")` o ruta fallback de WinGet (Gyan.FFmpeg).
- `subprocess` con `creationflags=0x08000000` (`CREATE_NO_WINDOW`) para que no aparezcan consolas en una app `pythonw`.
- `ffprobe -show_entries format=duration:stream=codec_type,codec_name,r_frame_rate,width,height -of json` → `r_frame_rate` viene como fracción `"30000/1001"`.
- Árbol de decisión:
  - sin solape y todos los clips iguales (codec, w, h, fps) → **concat demuxer** con `-c copy` (instantáneo, sin pérdida);
  - sin solape pero distintos → `filter_complex concat` recodificando;
  - con solape y transición "ninguna" → `trim`/`atrim` de los frames duplicados + concat;
  - con solape y transición → cadena de `xfade` (video) + `acrossfade` (audio), `offset = duración acumulada − duración del solape`.
- Solape en frames → segundos: `overlap / fps`; limitar al 90 % del clip y avisar en el log.
- Audio externo opcional: entrada extra con `-ss inicio`, `-map N:a -shortest`.
- Salida: `libx264 -crf 14 -pix_fmt yuv420p`, AAC 192k.
- **Progreso real**: `-progress pipe:1 -nostats` y leer `out_time_ms=` de stdout en un hilo (stderr en otro hilo para el log) → fracción contra la duración estimada.
- Planificador: grilla de longitudes válidas de MiniMax-H3 = `17k + 5` frames; paso = `length − overlap`; genera la tabla `skip_first_frames / frame_load_cap` por pasada y la copia como Markdown.

### 5.4 strip_meta.py

- Reabrir PNG y guardar con `pnginfo=PngInfo()` vacío → elimina `prompt`/`workflow`/`parameters` conservando píxeles. Escribir a `.tmp.png` + `os.replace`. Verificar al final cuántos quedan con metadata.
- Para que ComfyUI no la escriba desde el inicio: arrancarlo con `--disable-metadata`.

---

## 6. Cómo crear una app nueva con este estilo (checklist)

1. Crear `mi_app.py` en la carpeta con: docstring (qué hace, qué soporta, cómo se usa, quirks verificados con fecha), constantes (`HERE`, rutas, colores), **funciones puras primero**, clase `App(tk.Tk)` después.
2. Crear `Mi App.bat` con la plantilla de §3.
3. Toda operación lenta → `self._start(worker)`; el worker solo hace `self._q.put(...)`.
4. Si usa Civitai, reutilizar `_civitai_token.txt` (mismo token para todas) y respetar el caché/backoff.
5. Si escribe archivos grandes → `.part` + `os.replace`, cancelable.
6. Probar con el Python del venv: `"D:\Program Files\ComfyUI\venv\Scripts\python.exe" mi_app.py` (con `python.exe`, no `pythonw.exe`, para ver tracebacks mientras se desarrolla).
7. Verificar sintaxis rápido: `python -m py_compile mi_app.py`.
8. No instalar paquetes nuevos en el venv de ComfyUI sin preguntar (puede romper ComfyUI). Si algo es opcional (como PyAV), importarlo dentro de la función y dar un error claro si falta.

## 7. Errores conocidos / cosas a no repetir

- No hashear modelos en bloque al indexar (HDD lento, cuelga).
- No asumir que el JSON del EXIF está completo: Civitai lo corta; siempre tener plan B por regex.
- No confiar en `meta` de la API de imágenes de Civitai.
- No tocar widgets Tk desde hilos.
- No mostrar ni loguear el token; en descargas va en la URL, así que no imprimir URLs completas de descarga en logs compartibles.
- Nombres de archivo de salida: sanear con `re.sub(r"[^\w.\- ]", "_", nombre)[:80-120]`.

---

# PARTE B — Ref Map

## 1.1 Qué es

- Instalador `Ref-Map-Setup.exe`, app Electron instalada en `%LOCALAPPDATA%\Programs\Ref Map`.
- Moodboard/canvas de referencias + barra de prompt que **optimiza el prompt para un modelo concreto** (Veo 3, Kling, Wan, MiniMax H3, Flux, Midjourney…) usando un LLM (claude-haiku-4-5 / gpt-4o-mini / llama-3.3-70b), dictado por voz con Whisper, y visión (imagen → tags).
- Solo viene en portugués, sin opción de idioma.

Kit propio en `C:\Users\claud\RefMap-EN\`:

| Archivo | Rol |
|---|---|
| `apply.ps1` | Aplica todo el parche (idempotente). Correr de nuevo tras reinstalar/actualizar Ref Map. |
| `revert.ps1` | Restaura la app original en portugués. |
| `refmap-en.js` | Traductor en tiempo de ejecución que se inyecta en la UI (diccionario PT→EN + MutationObserver). |
| `patch-main.js` | Reemplazos de strings en el proceso main de Electron + desactiva auto-updates + idioma de Whisper en la nube. |
| `patch-renderer.js` | Parches en el bundle del renderer: idioma de Whisper local y forzar idioma de prompt EN. |
| `agent-kit/` | Motor de prompts de Ref Map extraído como librería JS. |

## 1.2 Técnica: parchear una app Electron empaquetada en asar

Pasos (lo que hace `apply.ps1`):

1. **Cerrar la app** (`Stop-Process "Ref Map"`) y esperar a que muera; si no, los handles bloquean el borrado.
2. **Verificar los fuses de Electron.** Esto solo funciona porque esta app tiene `OnlyLoadAppFromAsar` **desactivado** y la validación de integridad del asar **desactivada**. Si una app los tiene activos, esta técnica no sirve.
3. Extraer `resources\app.asar` → `resources\app\` con `npx --yes @electron/asar extract` (requiere Node en PATH). `app.asar.unpacked` debe estar al lado de `app.asar` durante la extracción.
4. Renombrar `app.asar` → `app.asar.orig`. Electron, al no encontrar el asar, **carga la carpeta `resources\app\`** directamente. El `.orig` sirve para revertir y para re-extraer limpio.
5. Parchear archivos dentro de `resources\app\out\`:
   - `out\renderer\index.html`: inyectar `<script src="./refmap-en.js"></script>` **antes** del `<script type="module">` del bundle y cambiar `lang="pt-BR"` → `lang="en"`.
   - `out\main\index.js`: correr `patch-main.js`.
   - `out\renderer\assets\index-*.js` y `whisperWorker-*.js`: correr `patch-renderer.js`.
6. Relanzar la app.

Detalles que costaron:
- **`apply.ps1` debe ejecutarse desde fuera de `resources\app`**: si la terminal tiene esa carpeta como directorio actual, Windows mantiene un handle abierto y no se puede borrar.
- `Remove-Tree` reintenta hasta 30 veces con 1 s de espera, porque el indexador de Windows Search suele tener handles abiertos.
- Si existe `app.asar.orig` pero no `app.asar`, primero se restaura el `.orig`, así cada ejecución parte de cero (idempotencia).
- Revertir = borrar `resources\app\` + renombrar `app.asar.orig` → `app.asar`.

## 1.3 Traductor en tiempo de ejecución (`refmap-en.js`)

En vez de buscar y reemplazar en el bundle minificado de React (frágil), se traduce **el DOM ya renderizado**:

- `EXACT`: diccionario `{"texto PT exacto": "texto EN"}` (~300 entradas, agrupadas por zona de la UI).
- `PATTERNS`: lista de `[regex, reemplazo | función]` para strings dinámicos con números o nombres.
- Traduce nodos de texto y los atributos `placeholder`, `title`, `aria-label`, `alt` y el `value` de botones `<input>`.
- Pasada inicial con `TreeWalker` sobre `document.body`, luego un `MutationObserver` sobre `documentElement` con `subtree, childList, characterData, attributes` (filtrado a esos atributos) para traducir todo lo que React agregue o cambie después.
- Si un string no está en el diccionario, queda en portugués (fallback seguro, nunca rompe).
- **Agregar traducciones faltantes:** editar `EXACT` (o `PATTERNS`) y volver a correr `apply.ps1`.

## 1.4 Parches del bundle (`patch-main.js`, `patch-renderer.js`)

Patrón: script de Node que lee el archivo, aplica una lista `[original, reemplazo]` con `split().join()` y **reporta cuántos aplicó y cuáles no encontró** (`MISSED:`). Así, cuando una versión nueva cambia el código, se ve de inmediato qué patrones hay que actualizar.

- **Main process:** ~30 strings (diálogo de actualización, menús, progreso de instalación de Ollama, errores de ffmpeg).
- **Desactivar auto-update** (necesario, porque cada actualización restauraba el portugués): justo después de `const electronUpdater = require("electron-updater");` se inyecta código que reemplaza `checkForUpdates`, `checkForUpdatesAndNotify`, `downloadUpdate` y `quitAndInstall` por funciones vacías, y pone `autoDownload` y `autoInstallOnAppQuit` en `false`. Se protege con un guard para no inyectarlo dos veces.
- **Dictado (Whisper):** en la nube, `language: "pt"` → `"es"`; en local, `transcribeLocal(blob, lang = "pt")` y `language: lang || "pt"` → `"es"` mediante regex sobre `index-*.js` y `whisperWorker-*.js` (los nombres llevan hash, por eso se buscan con regex). Constante `LANG` (`"es"`, `"en"` o `""` para autodetectar).
- **Idioma del prompt optimizado:** forzar `setAppLang("en")` y congelar el toggle (`const next = "en";`).

Resultado: UI en inglés, dictado en español, prompts optimizados en inglés y sin auto-update.

## 1.5 agent-kit: el motor de prompts como librería

Ubicación: `C:\Users\claud\RefMap-EN\agent-kit\` (extraído de Ref Map v0.0.28; el texto de los prompts es obra del autor de la app, mantener la atribución si se redistribuye).

| Archivo | Qué es |
|---|---|
| `refmap-prompts.js` | `MODEL_PROMPT_CONFIGS`: 27 modelos con su guía de formato, ejemplos y sintaxis de referencias (copia literal de la app) + el set `IMAGE_MODEL_IDS`. |
| `optimizer.js` | `buildOptimize(text, modelId, {format})` → `{system, user, asJson}` · `postProcess(raw, asJson)` · `listModels()`. |
| `vision.js` | `buildVisionPrompt(lang)` + `parseDescriptionToTags(raw)`: imagen de referencia → tags. |
| `example-agent.js` | Flujo completo con el SDK de Anthropic. |

Por qué funciona bien el optimizador:
- **system** = guía del modelo destino + override `SCOPE`: *el texto del usuario manda sobre el contenido; la guía es un menú, no un checklist*.
- **user** = wrapper: regla VERBATIM (diálogos y texto en pantalla entre comillas no se traducen ni alteran) → objetivo central *"eres un formateador, no un coescritor: misma escena, mejor estructura, no inventes nada"* → fotorrealismo por defecto en modelos de imagen → "devuelve solo el prompt" + autochequeo de alcance → prompt del usuario.
- La salida es en inglés sin importar el idioma de entrada (`outputLanguage: "source"` la mantiene; `"en-strict"` para MiniMax H3).
- `postProcess` quita introducciones ("Here's the optimized prompt:"), markdown y líneas en blanco de más; en modo JSON extrae el objeto y lo re-indenta.

```js
const { buildOptimize, postProcess } = require("./optimizer");
const { system, user, asJson } = buildOptimize("un gato negro camina por un techo de noche", "veo3");
const raw = await llm({ system, messages: [{ role: "user", content: user }], max_tokens: 2048 });
const prompt = postProcess(raw, asJson);
```

Ids: `boogu hidream midjourney nano-banana gpt-image-2 stable-diffusion flux flux2-klein krea-2 zimage gemini-omni veo3 hunyuan minimax-h3 sora-2 grok wan wan-3 qwen-image-2512 seedance ltx-2 runway-gen4 hailuo kling-3 pika pixverse luma`.

Uso real: se portó a TypeScript dentro de la web app del capstone (`OneDrive\Desktop\proyecto activo\web\src\lib\refmap\`) como paso automático antes de cada generación de imagen o video; doc en `proyecto activo/docs/prompt_optimizer_refmap.md` (sin probar en ejecución todavía).

---

# PARTE C — Director Studio

## 2.1 Qué es

Workspace local de preproducción cinematográfica: biblioteca de assets tipada (actores, escenarios, props, layouts, voces), un **Director Agent** conversacional que planifica shots a partir de un guion, prompts H3 de seis secciones y generación vía **ComfyUI** (y opcionalmente la API oficial de MiniMax H3).

- Repo: `https://github.com/ai2764/Director-Studio` (autor ai2764, sep 2026), clonado en `C:\Users\claud\OneDrive\Desktop\director\Director-Studio`.
- Carpeta padre `director\` = workspace del agente filmmaker (`CLAUDE.md`, `AGENTS.md`, `CHATGPT_PROJECT_INSTRUCTIONS.md` = proceso autoritativo, `MODELS_INVENTORY.md`, `SOUL.md`, `USER.md`).

## 2.2 Arquitectura (resumen de `docs/ARCHITECTURE.md`)

| Capa | Tecnología |
|---|---|
| Backend | FastAPI + uvicorn, Pydantic v2 / pydantic-settings (variables `DS_*` en `.env`), httpx, `comfy-cli` + `comfy-mcp==0.10.0` |
| Frontend | Vite 6 + React 19 + TypeScript 5.8, carpetas por feature |
| LLM del Director | Un proveedor a la vez: Ollama, LM Studio o cualquier servidor OpenAI-compatible (`DS_LLM_PROVIDER`; el modelo se elige en la UI, no en `.env`) |
| Ejecución | ComfyUI (vía MCP) para actor / escena / prop / layout / H3 local |

Ideas clave del diseño:
- **Pipelines enchufables:** cada workflow de ComfyUI = un módulo `pipelines/<id>/` que implementa `Pipeline` (`build_prompt` parchea el JSON del workflow, `map_history_outputs` mapea los nodos SaveImage/SaveVideo a claves lógicas, `save_to_library`, `meta_defaults`) y se registra con `register_pipeline(...)` + `include_router`. Para agregar una feature nueva no se toca el núcleo.
- **Workflows en formato API** (`backend/workflows/*.api.json`), no grafos de UI.
- **Jobs genéricos:** upload → queue → poll → guardado en `data/jobs/<id>/{job.json, inputs/, outputs/}`.
- **Assets de primera clase:** `data/library/<kind>/<id>/asset.json` + imágenes.
- **VRAM exclusiva:** antes de cada job de ComfyUI se descarga el LLM (Ollama `keep_alive=0`); el contexto del agente se persiste en `data/projects/<id>/agent/context.json` y se recarga al volver.
- **Doble gate humano:** (1) aprobar el layout del primer frame; (2) aprobar refs ordenadas + prompt H3 antes de encolar el video.
- **H3 puro Ref2AV:** `MiniMaxH3ReferenceToVideo`, máximo 9 refs, layout = Picture 1; prompt en seis secciones (`subject_definitions, summary, retention_analysis, detailed_description, overall_soundscape, non_diegetic_music`); largo en frames `n % 17 == 5`, rango 124–362.

## 2.3 Modificaciones locales (sin commitear, `git diff`)

1. **Rutas de modelos de los workflows → la organización de esta máquina.** Los `.api.json` traían nombres planos que acá no existen; se reapuntaron a la subcarpeta `qwen\` y a las variantes ya descargadas, en vez de bajar modelos nuevos:
   - `qwen_image_edit_2511_fp8mixed` → `qwen\qwen_image_edit_2511_int8_convrot`
   - `qwen_image_2512_fp8_e4m3fn` → `qwen\qwen_image_fp8_e4m3fn`
   - LoRAs Lightning / multiple-angles → `qwen\...` (y Lightning 2512 fp32 → `qwen\Qwen-Image-Lightning-4steps-V1.0`)
   - Archivos tocados: `QwenEdit2511_MultiAngle_SceneRef`, `qwen_actor_asset_workbench`, `qwen_prop_master`, `ref_frame_layout`.
   - En JSON, el separador de Windows va escapado: `"qwen\\archivo.safetensors"`.
2. **Compatibilidad con LM Studio** (`backend/app/core/llm/openai_compatible.py`): el historial interno de tool calls tiene la forma de Ollama (llamadas sin `id`, `arguments` como dict, resultados con `tool_name`). Ollama lo acepta, pero LM Studio y llama.cpp validan el esquema OpenAI de forma estricta y rechazan la petición. Se agregó `_normalize_tool_messages()`, que sintetiza `id`s `call_N` en orden, convierte `arguments` a string JSON, pone `type: "function"`, `content: ""` si es `None` y asigna a cada mensaje `tool` su `tool_call_id`. Se ajustó el test `test_director_native_tools.py`.
3. **Idioma del agente** (`agents/director/chat_orchestrator.py`, system prompt): responde en el idioma del usuario (español), pero **todo texto destinado al modelo de imagen o video** (`generation_prompt`, descripciones de actor/vestuario/escena para `queue_actor_design` / `queue_gpt_ref_frame`) se escribe en inglés.
4. **Markdown en el chat** (`frontend/src/shared/lightMarkdown.tsx`, nuevo, + `DirectorPage.tsx` + `styles.css`): renderizador mínimo propio (negritas, código inline, listas numeradas y con viñetas, párrafos) sin agregar dependencias; reemplaza el texto plano de las burbujas del chat.
5. Ajustes menores en `AssetWorkspace.tsx` y `LibraryOverview.tsx`.
6. **Lanzadores Windows:** `Start.bat` / `Stop.bat` (sin trackear) llaman a `start.ps1` / `kill.ps1` con `pwsh`. `start.ps1` levanta backend (:8790) y frontend Vite (:5173) como procesos desacoplados, con logs y pids en `.run/`, y busca automáticamente un Python que pueda importar `uvicorn` y `app.main` (`DS_PYTHON_EXE`, `backend\.venv`, venv activo, conda, pyenv, PATH).

Al actualizar desde upstream (`git pull`), estos cambios pueden chocar: guardarlos antes con `git stash` o en una rama.

## 2.4 Reglas para el agente que trabaje en `director\`

- Leer `CHATGPT_PROJECT_INSTRUCTIONS.md` antes de cualquier paso de producción: ledger compacto, **un solo Asset ID activo, una generación por respuesta** y no avanzar sin aprobación explícita.
- Generación 100 % local en ComfyUI (`D:\Program Files\ComfyUI`, :8188, RTX 5090): `server_info` primero. No usar Veo, Seedance ni GPT Image salvo que el usuario lo pida.
- Revisar `MODELS_INVENTORY.md` antes de suponer que falta un modelo (varios pesan 20–34 GB) y **preguntar antes de descargar**.
- Los contratos `production-json-contract.md`, `h3-ref2va-contract.md` y `visual-assets.md` se citan como fuente, pero **no existen en disco**: pedírselos al usuario, no inventarlos.
- El MCP de InsForge en `.mcp.json` y `CLAUDE.md.old.md` son restos de otro proyecto (FastField/Viral+): ignorarlos.
- Secretos: `director\.env`, `backend\.env` y `gmail_credentials.json` tienen credenciales. **No compartirlos.**

---

# Lecciones transferibles (Ref Map y Director Studio)

1. **App Electron sin idioma:** revisar los fuses → extraer el asar → cargar la carpeta → traducir el DOM con diccionario + MutationObserver en lugar de editar el bundle minificado.
2. **Parches de bundle:** listas `[original, reemplazo]` que reportan los aciertos y los `MISSED`, scripts idempotentes, siempre con un `revert`.
3. **Si las actualizaciones pisan el parche:** desactivar `electron-updater` desde el propio main.
4. **Lo valioso de una app suele ser su prompt engineering:** extraerlo como librería independiente del LLM (agent-kit).
5. **Repo de terceros con ComfyUI:** adaptar los nombres de modelo de los `.api.json` a los que ya existen en disco, en vez de descargar.
6. **Ollama → servidores OpenAI-compatible:** normalizar el historial de tool calls (ids, arguments como string, `tool_call_id`).
7. **Agente bilingüe:** conversación en el idioma del usuario, prompts para modelos generativos siempre en inglés.
