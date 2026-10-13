# Handoff — ComfyUI-Viggle-Animate-H3

Fecha: 2026-09-10. Estado: **instalación y descargas completas. Falta arrancar ComfyUI y validar el workflow.**

## Qué es

Node pack de Saganaki22 para **reemplazo de personaje en video**. Entrada: un video "conductor" + una imagen de referencia. Salida: el mismo video pero el actor tiene la identidad de la imagen. Movimiento, cámara, timing, fondo y luz vienen del video; la identidad viene de la imagen.

Basado en un finetune "Viggle" de **MiniMax-H3 ref2va**. No usa text encoder — trae conditioning precomputado.

Repo: https://github.com/Saganaki22/ComfyUI-Viggle-Animate-H3 (node pack v1.3.1, Apache 2.0; pesos bajo MiniMax-H3 Community License).

## Lo ya hecho en esta sesión

### Node pack
- Clonado en `D:\Program Files\ComfyUI\custom_nodes\ComfyUI-Viggle-Animate-H3` (`git clone --depth 1`).
- Sin dependencias Python (`dependencies = []` en pyproject).
- Requiere **ComfyUI-KJNodes** para el preview rápido — ya estaba instalado.

### Config
- El `__init__.py` del nodo registra el tipo de carpeta `text_cond` solo apuntando a `D:\...\ComfyUI\models\text_cond`. Para que ComfyUI vea la carpeta en el HDD se añadió esta línea al bloque `comfyui_hdd` de `D:\Program Files\ComfyUI\extra_model_paths.yaml`:
  ```
  text_cond: text_cond/
  ```
  (justo debajo de `vae: vae/`)

### Modelos descargados a E: (todos verificados: tamaño exacto + header safetensors OK)

| Archivo | Ruta en E:\Program Files\ComfyUI\models\ | Bytes | Fuente HF |
|---|---|---|---|
| `minimax_h3_ref2va_viggle_pruned_int8_convrot.safetensors` | `diffusion_models/` | 21 033 720 080 (937 tensores) | `drbaph/Viggle-Animate-ComfyUI` |
| `viggle_animate_dmd_lora.safetensors` | `loras/` | 3 767 420 888 (405 tensores) | `drbaph/Viggle-Animate-ComfyUI` |
| `fixed_embed_fwd_anyframe.safetensors` | `text_cond/` (carpeta nueva) | 3 713 456 | `drbaph/Viggle-Animate-ComfyUI` |

### Modelos que YA estaban en E: y sirven tal cual
| Archivo | Ruta |
|---|---|
| `minimax_h3_video_vae_int8_convrot.safetensors` (3.17 GB) | `vae/` |
| `taeh3.safetensors` (preview rápido vía KJNodes) | `vae_approx/` |

### OJO — no confundir modelos
Ya existía en E: `diffusion_models/minimax_h3/minimax_h3_ref2va_pruned_int8_convrot.safetensors` (21 GB). Ese es **MiniMax-H3 ref2va base, NO el finetune Viggle**. El de Viggle es el que tiene `_viggle_` en el nombre y está en la raíz de `diffusion_models/`.

### Alternativas no bajadas (por si acaso)
- LoRA DMD ligera: `viggle_animate_dmd_lora_r64.safetensors` (0.94 GB) — misma calidad aprox, menos disco. El workflow v1.2.0 apunta por defecto a la de 3.8 GB, por eso se bajó esa.
- Diffusion model full INT8 (47 GB) o bf16 (66.3 GB) — no caben cómodos en VRAM de la 5090, el pruned int8 21 GB va bien.
- VAE fp16 (5.21 GB, `Comfy-Org/MiniMax-H3`) — el int8 ya está y basta.

## Próximos pasos (siguiente sesión)

1. Arrancar ComfyUI (recoge el yaml nuevo + node pack al iniciar). Vía MCP: `mcp__comfy-local__launch_comfyui`, luego `server_info` para confirmar.
2. Cargar `D:\Program Files\ComfyUI\custom_nodes\ComfyUI-Viggle-Animate-H3\example_workflows\viggle-animate-h3_workflow-v1.2.0.json`.
3. Verificar que los dropdowns resuelven solos:
   - `unet_name` -> `minimax_h3_ref2va_viggle_pruned_int8_convrot.safetensors`
   - `lora_name` -> `viggle_animate_dmd_lora.safetensors`
   - `vae_name` -> `minimax_h3_video_vae_int8_convrot.safetensors`
   - `text_cond` -> `fixed_embed_fwd_anyframe.safetensors`
   - preview -> `taeh3.safetensors`
   Si `text_cond` sale vacío: el yaml no se recogió o hay que reiniciar; comprobar con la API de ComfyUI que el folder type `text_cond` lista el archivo.
4. `validate_workflow` sobre el JSON, arreglar lo que falte.
5. Meter video conductor + imagen de referencia y hacer gen de prueba.

## Parámetros de uso

- **Sampler:** Euler + BasicGuider + `ModelSamplingMiniMaxH3` con shift 3.0/3.0.
- **Sigmas** (elegir preset):
  - rápido (3 updates): `1.0, 0.857, 0.6, 0.0`
  - balance (5 updates): `1.0, 0.923, 0.818, 0.667, 0.429, 0.0`
  - calidad (7 updates, puede sobre-afilar): `1.0, 0.947, 0.882, 0.8, 0.692, 0.545, 0.333, 0.0`
- **Frames:** single-shot máx 124 frames (~5.2 s a 24 fps). Salida a la resolución del video conductor.
- **Imagen de referencia:** mejor a 0.4–1.2 megapixels, con pose y fondo parecidos al primer frame del video.
- **Clips largos:** usar `viggle-animate-h3_workflow-chunked-sampler.json` (window 124, overlap 22, cache en RAM) o `viggle-animate-h3_workflow-long-video-advanced.json` (checkpoints a disco, recuperable). Guía: `custom_nodes\ComfyUI-Viggle-Animate-H3\docs\long_video_guide.md`.

## Limitaciones conocidas

- La identidad se degrada cuando el sujeto sale y vuelve a entrar de cuadro, o se aleja mucho de la pose de referencia.
- Lip-sync poco fiable.

## Entorno (recordatorio)

- ComfyUI en `D:\Program Files\ComfyUI` (checkout git de comfyanonymous/ComfyUI, v0.34.2).
- venv propio: `D:\Program Files\ComfyUI\venv\Scripts\python.exe`.
- GPU RTX 5090, ~34 GB VRAM. 64 GB RAM.
- Modelos en HDD `E:\Program Files\ComfyUI\models\` vía `extra_model_paths.yaml` bloque `comfyui_hdd`.
