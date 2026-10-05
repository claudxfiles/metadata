# Cómo se construyen las apps de `Downloads\metadata`

Guía de conocimiento para que otro agente pueda **mantener, extender o crear nuevas herramientas** con el mismo estilo que las existentes. Escrita a partir del código real (2026-10-03).

---

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
