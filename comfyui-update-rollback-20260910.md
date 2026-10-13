# ComfyUI update rollback note — 2026-09-10

## Objetivo
Actualizar ComfyUI core de v0.34.2 a v0.35.0 para obtener el nodo core `BlockSparseAttention`
(mergeado 2026-09-06, PR #16072; necesita comfy-kitchen >= 0.2.33).

## RESULTADO (2026-09-10)
- **HECHO.** v0.34.2 (`169fcf35`) -> **v0.35.0 (`40c4fcdf`)**. comfy-cli `update comfy --version 0.35.0`.
- Cambios uncommitted previos (placeholders borrados + yamls SD1.5/SD2) quedaron en `stash@{0} (cac92ed1)` — basura, no restaurar.
- Paquetes: comfy-kitchen 0.2.31->**0.2.33**, frontend 1.52.7->1.51.10 (pin oficial de 0.35.0), workflow-templates 0.11.50->0.11.57, comfy-aimdo 0.4.15->0.5.3. **torch NO se tocó** (sigue nightly cu130).
- `BlockSparseAttention` core: OK. Los ~40 custom nodes: todos importaron sin error.
- Parche r2v shim: re-aplicado OK (`comfyui-r2v-shim-20260910.patch`).
- Parche TE-Speed hook: `patch_model.py` NO aplica en 0.35.0 (cambió el block loop). **Porté el hook a mano** en `comfy/ldm/minimax/model.py`:
  - metodo `_run_blocks(..., transformer_options, layout=None, start=0, end=None)` (firma con `layout` extra vs original)
  - rama `("block_loop", 0)` en el block loop del forward, pasando `layout`/`cache_ranges`/`block_count`
  - `patch_model.py --check` da falso negativo ("hooks MISSING") por la firma distinta; el nodo `TESpeedMiniMaxH3` sólo checa `hasattr(inner,"_run_blocks")` -> funciona igual.
  - backup pristine 0.35.0 en `comfy/ldm/minimax/model.py.te_speed.bak` (creado esta sesión). `--revert` restaura ESE (0.35.0 limpio), no re-aplica el hook.
- Nota Blackwell: `BlockSparseAttention` en modo `sol-attn` necesita kernel `ck.sol_attn` compilado; en RTX 5090 (SM120) puede caer a densa (log, no error).

## Estado ANTES del update (para rollback)
- Ruta: `D:\Program Files\ComfyUI`
- git commit: `169fcf35a2fc163fec31338b816503ddac0d3fcf`
- tag: `v0.34.2`  (HEAD detached, sin branch)
- comfy-kitchen: 0.2.31
- comfyui-frontend-package: 1.52.7
- torch: 2.15.0.dev20260812+cu130  (nightly Blackwell — NO tocar)
- venv: `D:\Program Files\ComfyUI\venv\Scripts\python.exe`

## Parches manuales al core presentes ANTES del update (git diff)
1. `comfy/ldm/minimax/model.py` (+49/-15)
   - Pack: **TE-Speed-MiniMaxH3-OSS** (`custom_nodes/TE-Speed-MiniMaxH3-OSS/`)
   - Anade metodo `_run_blocks()` + hook `("block_loop", 0)` en `MiniMaxH3Model.forward`
   - Backup automatico: `comfy/ldm/minimax/model.py.te_speed.bak` (2026-08-11)
   - Re-aplicar: `python custom_nodes/TE-Speed-MiniMaxH3-OSS/patch_model.py --comfy-ui "D:/Program Files/ComfyUI"`
   - Revert:     `... patch_model.py --revert`   | Check: `... --check`
2. `comfy_extras/nodes_minimax_h3.py` (+10)
   - Shim en `MiniMaxH3ReferenceToVideo.execute`: envuelve ref_images/ref_videos/ref_video_audios/ref_audios
     sueltos (frontend viejo autogrow) en dict `{"ref_image_0": ...}` etc.
   - Sin backup dedicado. Diff guardado abajo.

## Rollback completo
```
mcp stop_comfyui
cd "D:/Program Files/ComfyUI"
git stash list                 # el update deja los parches aca
mcp switch_comfyui_version 0.34.2   (confirm)
git checkout 169fcf35a2fc163fec31338b816503ddac0d3fcf   # si el tag no basta
# re-aplicar parches:
python custom_nodes/TE-Speed-MiniMaxH3-OSS/patch_model.py --comfy-ui "D:/Program Files/ComfyUI"
git stash pop                   # o re-aplicar el shim r2v a mano (diff abajo)
mcp launch_comfyui ; mcp server_info
```

## Diff del shim r2v (por si el stash se pierde)
En `comfy_extras/nodes_minimax_h3.py`, dentro de `MiniMaxH3ReferenceToVideo.execute`,
justo antes de `latent, frame_count = _empty_av_latent(width, height, length)`:
```python
        # Older frontends serialize a single autogrow connection under the flat
        # container name, delivering the raw value instead of a name->value dict.
        if torch.is_tensor(ref_images):
            ref_images = {"ref_image_0": ref_images}
        if torch.is_tensor(ref_videos):
            ref_videos = {"ref_video_0": ref_videos}
        if isinstance(ref_video_audios, dict) and "waveform" in ref_video_audios:
            ref_video_audios = {"ref_video_audio_0": ref_video_audios}
        if isinstance(ref_audios, dict) and "waveform" in ref_audios:
            ref_audios = {"ref_audio_0": ref_audios}
```
