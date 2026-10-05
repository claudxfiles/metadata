"""
Civitai Scraper GUI — buscar, filtrar (previsualizar en grilla) y descargar
imagenes de civitai.com en la cantidad que se pida.

Reemplaza scraper1.py / scraper2.py / descargar_refs_civitai.bat:
- Antes: scripts de consola ciegos (bajaban todo sin ver nada antes) o un
  .bat con URLs hardcodeadas.
- Ahora: busca por usuario / tag / nombre de modelo / modelVersionId,
  muestra miniaturas en grilla con checkbox, elegis cuales bajar y cuantas.

ESTADO API (heredado de scraper2, verificado 2026-09-06):
- /api/v1/images devuelve meta:null en casi todas las imagenes (bug civitai
  #1297/#1037) -> no hay prompt para la mayoria; se guarda igual y se omite
  el .txt si no hay prompt.
- /images NO soporta filtro por tag directo -> modo "tag" resuelve
  /models?tag= y junta las modelVersions.
- sort en /images: "Most Reactions" | "Most Comments" | "Newest"
- sort en /models: "Highest Rated" | "Most Downloaded" | "Newest"
- 429 -> backoff exponencial.

Token: mismo archivo que Metadata Viewer (_civitai_token.txt, junto a este
script) -> se configura una vez y sirve para las dos herramientas.
"""

import os
import re
import sys
import json
import threading
import queue
import webbrowser
from io import BytesIO

import requests
from PIL import Image, ImageTk, UnidentifiedImageError
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "_civitai_token.txt")
GLOBAL_SEEN_FILE = os.path.join(HERE, "_civitai_downloaded_ids.log")
SESSIONS_DIR = os.path.join(HERE, "civitai_sessions")
BASE = "https://civitai.com/api/v1"
REQUEST_TIMEOUT = 60
TAG_RE = re.compile(r"<.*?>")

THUMB = 180
COLS = 5

STATUS_HOOK = None  # la GUI lo setea para ver reintentos en vivo (civitai caído a ratos)

DEFAULT_EXCLUDE_TAGS = ["Anime", "Manga", "Body Horror", "Gore", "Political",
                         "Anthro", "Furry", "Hentai", "Real Person", "Disturbing",
                         "No Humans", "Male"]
DEFAULT_EXCLUDE_PRESELECTED = {"anime", "manga", "body horror", "gore", "political", "anthro", "furry",
                                "no humans", "male"}


def load_token():
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def save_token(t):
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(t.strip())


TOKEN = load_token()


def load_seen_ids():
    """IDs ya descargados alguna vez, sin importar carpeta destino."""
    try:
        with open(GLOBAL_SEEN_FILE, "r", encoding="utf-8") as f:
            return {int(line.strip()) for line in f if line.strip().isdigit()}
    except OSError:
        return set()


def headers():
    return {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def api_get(url, params=None, attempts=6):
    import time
    last_err = None
    for attempt in range(attempts):
        try:
            r = requests.get(url, headers=headers(), params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            last_err = e
            r = None
        else:
            if r.status_code in RETRYABLE_STATUS:
                last_err = requests.HTTPError(f"{r.status_code} {r.reason} for url: {url}")
            elif r.status_code == 401:
                raise RuntimeError("401 no autorizado — poné un token valido (boton Token…)")
            else:
                r.raise_for_status()
                return r.json()
        wait = min(2 ** attempt, 30)
        if STATUS_HOOK and attempt < attempts - 1:
            STATUS_HOOK(f"civitai no responde ({last_err}) — reintento {attempt + 1}/{attempts} en {wait}s…")
        time.sleep(wait)
    raise RuntimeError(f"civitai no responde tras {attempts} intentos ({last_err}): {url}")


# ---------------------------------------------------------------- fuentes

def image_pages(params):
    """Itera items de /images paginando via metadata.nextPage."""
    url = f"{BASE}/images"
    while url:
        data = api_get(url, params)
        params = None
        for img in data.get("items", []):
            yield img
        url = (data.get("metadata") or {}).get("nextPage")


def build_exclude_set(raw):
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def _term_hits(term, blob):
    """Match de palabra/frase completa: 'male' no debe pegarle a 'female'."""
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", blob) is not None


def model_excluded(model, exclude_set):
    """Filtra por tags/nombre del modelo (chips tipo Anime/Gore/Furry de civitai)."""
    if not exclude_set:
        return False
    tags = [str(t).lower() for t in (model.get("tags") or [])]
    blob = " ".join(tags) + " " + (model.get("name") or "").lower()
    return any(_term_hits(term, blob) for term in exclude_set)


def image_excluded(img, exclude_set):
    """Red de seguridad a nivel imagen: tags si vienen, y prompt/negativePrompt si hay meta."""
    if not exclude_set:
        return False
    tags = [str(t).lower() for t in (img.get("tags") or [])]
    meta = img.get("meta") or {}
    blob = " ".join(tags) + " " + (meta.get("prompt") or "").lower() + " " + (meta.get("negativePrompt") or "").lower()
    return any(_term_hits(term, blob) for term in exclude_set)


def model_version_ids_by_tag(tag, model_types, sort_models="Most Downloaded", exclude_set=None):
    # ojo: el periodo NO se aplica acá — /models?period= filtra por actividad
    # reciente del MODELO (casi siempre vacío), no de las imagenes. El periodo
    # se aplica solo al pedir /images más abajo (build_source_iter).
    p = {"limit": 100, "tag": tag, "sort": sort_models}
    if model_types and model_types != "All":
        p["types"] = model_types
    url = f"{BASE}/models"
    while url:
        d = api_get(url, p)
        p = None
        for m in d.get("items", []):
            if model_excluded(m, exclude_set):
                continue
            for v in m.get("modelVersions", []):
                yield v["id"]
        url = (d.get("metadata") or {}).get("nextPage")


def search_models(query, model_types=None, limit=20, exclude_set=None):
    # mismo motivo que model_version_ids_by_tag: period acá filtraría por
    # actividad reciente del modelo, no de las imagenes.
    p = {"limit": limit, "query": query}
    if model_types and model_types != "All":
        p["types"] = model_types
    d = api_get(f"{BASE}/models", p)
    items = d.get("items", [])
    if exclude_set:
        items = [m for m in items if not model_excluded(m, exclude_set)]
    return items


def model_version_ids_of_model(model):
    for v in model.get("modelVersions", []):
        yield v["id"]


def build_source_iter(mode, query, nsfw, sort_images, model_types, picked_model=None,
                       exclude_set=None, period=None):
    """Generador único de imagenes crudas segun el modo elegido."""
    img_params = {"sort": sort_images, "nsfw": nsfw}
    if period and period != "AllTime":
        img_params["period"] = period

    if mode == "username":
        yield from image_pages({"limit": 100, "username": query, **img_params})
    elif mode == "version":
        yield from image_pages({"limit": 100, "modelVersionId": int(query), **img_params})
    elif mode == "tag":
        for vid in model_version_ids_by_tag(query, model_types, exclude_set=exclude_set):
            yield from image_pages({"limit": 100, "modelVersionId": vid, **img_params})
    elif mode == "model" and picked_model is not None:
        for vid in model_version_ids_of_model(picked_model):
            yield from image_pages({"limit": 100, "modelVersionId": vid, **img_params})


# ---------------------------------------------------------------- GUI

class Scraper(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Civitai Scraper")
        self.geometry("1100x760")

        self._q = queue.Queue()
        global STATUS_HOOK
        STATUS_HOOK = lambda msg: self._q.put(("status", msg))
        self._iter = None
        self._seen_ids = set()
        self._seen_downloaded = load_seen_ids()  # global, cruza carpetas y sesiones
        self._items = []          # [(img_dict, tk.BooleanVar, thumb_label)]
        self._photo_cache = {}    # img_id -> ImageTk.PhotoImage
        self._models_found = []   # resultados de busqueda por nombre de modelo
        self._picked_model = None
        self._busy = False

        self._build_ui()
        self.after(120, self._pump)

    # -- layout ------------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Modo:").grid(row=0, column=0, sticky="w")
        self.mode = tk.StringVar(value="username")
        modes = [("Usuario", "username"), ("Tag", "tag"),
                 ("Buscar modelo", "model"), ("ModelVersionId", "version")]
        for i, (label, val) in enumerate(modes):
            ttk.Radiobutton(top, text=label, value=val, variable=self.mode,
                             command=self._on_mode_change).grid(row=0, column=1 + i, sticky="w", padx=4)

        ttk.Label(top, text="Busqueda:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.query = tk.StringVar()
        ttk.Entry(top, textvariable=self.query, width=30).grid(
            row=1, column=1, columnspan=2, sticky="we", pady=(6, 0))
        self.btn_search = ttk.Button(top, text="Buscar", command=self.on_search)
        self.btn_search.grid(row=1, column=3, sticky="w", pady=(6, 0))
        ttk.Button(top, text="Token…", command=self.set_token).grid(
            row=1, column=4, sticky="w", pady=(6, 0))
        ttk.Button(top, text="Guardar sesión…", command=self.on_save_session).grid(
            row=1, column=5, sticky="w", pady=(6, 0), padx=(8, 0))
        ttk.Button(top, text="Cargar sesión…", command=self.on_load_session).grid(
            row=1, column=6, sticky="w", pady=(6, 0))
        ttk.Button(top, text="Limpiar grilla", command=self.on_clear_grid).grid(
            row=1, column=7, sticky="w", pady=(6, 0), padx=(8, 0))

        filt = ttk.Frame(self, padding=(8, 0))
        filt.pack(fill="x")

        ttk.Label(filt, text="NSFW:").grid(row=0, column=0, sticky="w")
        self.nsfw = tk.StringVar(value="false")
        ttk.Combobox(filt, textvariable=self.nsfw, width=10, state="readonly",
                     values=["false", "None", "Soft", "Mature", "X"]).grid(row=0, column=1, padx=4)

        ttk.Label(filt, text="Orden:").grid(row=0, column=2, sticky="w")
        self.sort = tk.StringVar(value="Most Reactions")
        ttk.Combobox(filt, textvariable=self.sort, width=16, state="readonly",
                     values=["Most Reactions", "Most Comments", "Newest"]).grid(row=0, column=3, padx=4)

        ttk.Label(filt, text="Tipo modelo:").grid(row=0, column=4, sticky="w")
        self.model_types = tk.StringVar(value="All")
        ttk.Combobox(filt, textvariable=self.model_types, width=12, state="readonly",
                     values=["All", "Checkpoint", "LORA"]).grid(row=0, column=5, padx=4)

        ttk.Label(filt, text="Min ancho:").grid(row=0, column=6, sticky="w")
        self.min_w = tk.IntVar(value=512)
        ttk.Spinbox(filt, from_=0, to=8000, textvariable=self.min_w, width=6).grid(row=0, column=7, padx=4)

        ttk.Label(filt, text="Min alto:").grid(row=0, column=8, sticky="w")
        self.min_h = tk.IntVar(value=512)
        ttk.Spinbox(filt, from_=0, to=8000, textvariable=self.min_h, width=6).grid(row=0, column=9, padx=4)

        ttk.Label(filt, text="Periodo:").grid(row=0, column=10, sticky="w")
        self.period = tk.StringVar(value="AllTime")
        ttk.Combobox(filt, textvariable=self.period, width=10, state="readonly",
                     values=["Day", "Week", "Month", "Year", "AllTime"]).grid(row=0, column=11, padx=4)

        ttk.Label(filt, text="Resultados por tanda:").grid(row=1, column=0, sticky="w", pady=4)
        self.batch = tk.IntVar(value=30)
        ttk.Spinbox(filt, from_=5, to=200, textvariable=self.batch, width=6).grid(row=1, column=1, pady=4)

        self.btn_more = ttk.Button(filt, text="Cargar más", command=self.on_load_more, state="disabled")
        self.btn_more.grid(row=1, column=2, padx=4)

        ttk.Button(filt, text="Seleccionar todo", command=lambda: self._set_all(True)).grid(row=1, column=3, padx=4)
        ttk.Button(filt, text="Deseleccionar todo", command=lambda: self._set_all(False)).grid(row=1, column=4, padx=4)

        ttk.Label(filt, text="Carpeta destino:").grid(row=2, column=0, sticky="w", pady=4)
        self.out_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads", "civitai_scraped"))
        ttk.Entry(filt, textvariable=self.out_dir, width=50).grid(row=2, column=1, columnspan=4, sticky="we", pady=4)
        ttk.Button(filt, text="Elegir…", command=self._pick_dir).grid(row=2, column=5, padx=4)

        ttk.Label(filt, text="Máximo a descargar:").grid(row=2, column=6, sticky="w")
        self.max_dl = tk.IntVar(value=50)
        ttk.Spinbox(filt, from_=1, to=5000, textvariable=self.max_dl, width=7).grid(row=2, column=7, padx=4)

        self.btn_download = ttk.Button(filt, text="Descargar seleccionadas", command=self.on_download)
        self.btn_download.grid(row=2, column=8, columnspan=2, sticky="e", padx=4)

        ttk.Label(filt, text="Excluir:").grid(row=3, column=0, sticky="w", pady=4)
        self.exclude_vars = {t: tk.BooleanVar(value=t.lower() in DEFAULT_EXCLUDE_PRESELECTED)
                              for t in DEFAULT_EXCLUDE_TAGS}
        self._exclude_popup = None
        self.exclude_menubtn = ttk.Button(filt, text="Excluir ▾", command=self._toggle_exclude_popup)
        self.exclude_menubtn.grid(row=3, column=1, sticky="w", pady=4)

        ttk.Label(filt, text="+ otros (coma):").grid(row=3, column=2, sticky="w", pady=4)
        self.exclude_extra = tk.StringVar(value="")
        ttk.Entry(filt, textvariable=self.exclude_extra, width=40).grid(
            row=3, column=3, columnspan=6, sticky="we", pady=4)
        self._update_exclude_label()

        # lista de modelos encontrados (modo "Buscar modelo")
        self.model_list_frame = ttk.Frame(self, padding=(8, 4))
        self.model_list = tk.Listbox(self.model_list_frame, height=5)
        self.model_list.pack(side="left", fill="x", expand=True)
        self.model_list.bind("<<ListboxSelect>>", self._on_model_pick)
        sb = ttk.Scrollbar(self.model_list_frame, orient="vertical", command=self.model_list.yview)
        self.model_list.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        # grilla de miniaturas con scroll
        grid_wrap = ttk.Frame(self)
        grid_wrap.pack(fill="both", expand=True, padx=8, pady=4)
        self.canvas = tk.Canvas(grid_wrap, highlightthickness=0)
        vsb = ttk.Scrollbar(grid_wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", side="left", expand=True)
        self.status = ttk.Label(bottom, text=f"listo · token {'✓' if TOKEN else '✗ (poné uno)'}")
        self.status.pack(side="right", padx=8)

        self._on_mode_change()

    def _on_mode_change(self):
        if self.mode.get() == "model":
            self.model_list_frame.pack(fill="x", before=self.canvas.master)
        else:
            self.model_list_frame.pack_forget()

    def on_clear_grid(self):
        if self._busy:
            return
        self._clear_grid()
        self._seen_ids.clear()
        self._photo_cache.clear()
        self._iter = None
        self.status.configure(text="grilla limpiada")

    def _pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.out_dir.get() or HERE)
        if d:
            self.out_dir.set(d)

    def set_token(self):
        global TOKEN
        v = simpledialog.askstring(
            "Token Civitai",
            "Pega tu API token de civitai.com (Settings → API Keys).\n"
            "Se guarda en _civitai_token.txt (compartido con Metadata Viewer).",
            initialvalue=TOKEN, show="*")
        if v is not None:
            TOKEN = v.strip()
            save_token(TOKEN)
            self.status.configure(text=f"token {'guardado ✓' if TOKEN else 'borrado'}")

    # -- sesiones (guardar/retomar busqueda + selección) ----------------

    def _session_state(self):
        return {
            "mode": self.mode.get(),
            "query": self.query.get(),
            "nsfw": self.nsfw.get(),
            "sort": self.sort.get(),
            "period": self.period.get(),
            "model_types": self.model_types.get(),
            "min_w": self.min_w.get(),
            "min_h": self.min_h.get(),
            "batch": self.batch.get(),
            "out_dir": self.out_dir.get(),
            "max_dl": self.max_dl.get(),
            "exclude_selected": [t for t, v in self.exclude_vars.items() if v.get()],
            "exclude_extra": self.exclude_extra.get(),
            "picked_model": self._picked_model,
            "items": [{"img": img, "checked": var.get()} for img, var in self._items],
        }

    def on_save_session(self):
        if not self._items:
            messagebox.showwarning("Nada para guardar", "Buscá algo primero.")
            return
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        default_name = re.sub(r"[^\w\-]+", "_", (self.query.get().strip() or self.mode.get()))[:40] or "sesion"
        name = simpledialog.askstring("Guardar sesión", "Nombre de la sesión:", initialvalue=default_name)
        if not name:
            return
        name = re.sub(r"[^\w\-]+", "_", name.strip()) or "sesion"
        path = os.path.join(SESSIONS_DIR, f"{name}.json")
        if os.path.exists(path) and not messagebox.askyesno("Sobreescribir", f"'{name}' ya existe. ¿Sobreescribir?"):
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._session_state(), f, ensure_ascii=False)
        self.status.configure(text=f"sesión guardada: {name} ({len(self._items)} imagenes)")

    def on_load_session(self):
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        path = filedialog.askopenfilename(
            title="Cargar sesión", initialdir=SESSIONS_DIR,
            filetypes=[("Sesiones civitai", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Error", f"No se pudo leer la sesión: {e}")
            return
        self._restore_session(data)

    def _restore_session(self, data):
        if self._busy:
            return
        self.mode.set(data.get("mode", "username"))
        self.query.set(data.get("query", ""))
        self.nsfw.set(data.get("nsfw", "false"))
        self.sort.set(data.get("sort", "Most Reactions"))
        self.period.set(data.get("period", "AllTime"))
        self.model_types.set(data.get("model_types", "All"))
        self.min_w.set(data.get("min_w", 512))
        self.min_h.set(data.get("min_h", 512))
        self.batch.set(data.get("batch", 30))
        if data.get("out_dir"):
            self.out_dir.set(data["out_dir"])
        self.max_dl.set(data.get("max_dl", 50))
        sel = set(data.get("exclude_selected", []))
        for t, v in self.exclude_vars.items():
            v.set(t in sel)
        self._update_exclude_label()
        self.exclude_extra.set(data.get("exclude_extra", ""))
        self._picked_model = data.get("picked_model")
        self._on_mode_change()

        self._clear_grid()
        self._seen_ids = set()
        items = data.get("items", [])
        checked_map = {}
        imgs = []
        for it in items:
            img = it.get("img")
            if not img:
                continue
            imgs.append(img)
            checked_map[img["id"]] = it.get("checked", True)
            self._seen_ids.add(img["id"])

        # reconstruye el iterador (misma query/orden) para que "Cargar más"
        # pueda seguir de donde quedó — se saltea lo ya visto via self._seen_ids
        self._iter = build_source_iter(self.mode.get(), self.query.get(), self.nsfw.get(),
                                        self.sort.get(), self.model_types.get(),
                                        picked_model=self._picked_model,
                                        exclude_set=self._get_exclude_set(), period=self.period.get())

        self._busy = True
        self.btn_search.configure(state="disabled")
        self.btn_more.configure(state="disabled")
        self.btn_download.configure(state="disabled")
        self._q.put(("status", f"cargando sesión… {len(imgs)} imagenes"))

        def work():
            for img in imgs:
                thumb = self._download_thumb(img["url"])
                self._q.put(("thumb", (img, thumb, checked_map.get(img["id"], True))))
            self._q.put(("status", f"sesión restaurada: {len(imgs)} imagenes"))
            self._q.put(("batch_done", None))

        self._start(work)

    # -- queue pump ----------------------------------------------------

    def _pump(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "status":
                    self.status.configure(text=payload)
                elif kind == "models":
                    self._fill_model_list(payload)
                elif kind == "thumb":
                    self._place_thumb(*payload)
                elif kind == "batch_done":
                    self._busy = False
                    self.btn_search.configure(state="normal")
                    self.btn_more.configure(state="normal")
                    self.btn_download.configure(state="normal")
                elif kind == "error":
                    self._busy = False
                    self.btn_search.configure(state="normal")
                    self.btn_more.configure(state="normal")
                    self.btn_download.configure(state="normal")
                    messagebox.showerror("Error", payload)
                elif kind == "progress":
                    self.progress.configure(value=payload)
                elif kind == "dl_done":
                    messagebox.showinfo("Listo", payload)
                    try:
                        os.startfile(self.out_dir.get())
                    except Exception:
                        pass
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

    # -- busqueda --------------------------------------------------------

    def on_search(self):
        if self._busy:
            return
        mode = self.mode.get()
        q = self.query.get().strip()
        if mode != "model" and not q:
            messagebox.showwarning("Falta busqueda", "Ingresá un valor de busqueda.")
            return
        # no se limpia la grilla: una busqueda nueva se suma a lo ya cargado
        # (asi no perdés lo que ya juntaste al cambiar de palabra/tag). Para
        # empezar de cero esta el botón "Limpiar".
        self._iter = None
        self._picked_model = None
        self.model_list.delete(0, "end")
        self._models_found = []

        if mode == "model":
            self._busy = True
            self.btn_search.configure(state="disabled")
            self._q.put(("status", f"buscando modelos: {q}…"))

            def work():
                items = search_models(q, self.model_types.get(), exclude_set=self._get_exclude_set())
                self._q.put(("models", items))
                self._q.put(("batch_done", None))
            self._start(work)
            return

        self._iter = build_source_iter(mode, q, self.nsfw.get(), self.sort.get(), self.model_types.get(),
                                        exclude_set=self._get_exclude_set(), period=self.period.get())
        self._fetch_batch()

    def _update_exclude_label(self):
        n = sum(v.get() for v in self.exclude_vars.values())
        self.exclude_menubtn.configure(text=f"Excluir ({n}) ▾" if n else "Excluir ▾")

    def _toggle_exclude_popup(self):
        if self._exclude_popup is not None and self._exclude_popup.winfo_exists():
            self._close_exclude_popup()
        else:
            self._open_exclude_popup()

    def _open_exclude_popup(self):
        btn = self.exclude_menubtn
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height()
        pop = tk.Toplevel(self)
        pop.overrideredirect(True)
        pop.geometry(f"+{x}+{y}")
        frame = tk.Frame(pop, bd=1, relief="solid", background="white")
        frame.pack()
        for t in DEFAULT_EXCLUDE_TAGS:
            tk.Checkbutton(frame, text=t, variable=self.exclude_vars[t], anchor="w",
                           background="white", command=self._update_exclude_label).pack(fill="x", padx=4, pady=1)
        self._exclude_popup = pop
        self.bind_all("<Button-1>", self._maybe_close_exclude_popup, add="+")

    def _maybe_close_exclude_popup(self, event):
        pop = self._exclude_popup
        if pop is None or not pop.winfo_exists():
            return
        widget = event.widget
        if widget is self.exclude_menubtn:
            return  # el propio boton ya maneja el toggle via su command
        try:
            if widget.winfo_toplevel() is pop:
                return  # click en una opcion del popup: seguir abierto
        except Exception:
            pass
        self._close_exclude_popup()

    def _close_exclude_popup(self):
        if self._exclude_popup is not None:
            try:
                self._exclude_popup.destroy()
            except Exception:
                pass
        self._exclude_popup = None
        self.unbind_all("<Button-1>")

    def _get_exclude_set(self):
        picked = {t.lower() for t, v in self.exclude_vars.items() if v.get()}
        return picked | build_exclude_set(self.exclude_extra.get())

    def _fill_model_list(self, items):
        self._models_found = items
        self.model_list.delete(0, "end")
        for m in items:
            nv = len(m.get("modelVersions", []))
            self.model_list.insert("end", f"{m.get('name', '?')}   [{m.get('type', '?')}, {nv} versiones, id={m.get('id')}]")
        self.status.configure(text=f"{len(items)} modelos encontrados — elegí uno de la lista")

    def _on_model_pick(self, _evt):
        sel = self.model_list.curselection()
        if not sel:
            return
        self._picked_model = self._models_found[sel[0]]
        self._iter = None
        self._iter = build_source_iter("model", "", self.nsfw.get(), self.sort.get(),
                                        self.model_types.get(), picked_model=self._picked_model,
                                        exclude_set=self._get_exclude_set(), period=self.period.get())
        self._fetch_batch()

    def on_load_more(self):
        if self._busy or self._iter is None:
            return
        self._fetch_batch()

    def _fetch_batch(self):
        if self._iter is None:
            return
        self._busy = True
        self.btn_search.configure(state="disabled")
        self.btn_more.configure(state="disabled")
        want = self.batch.get()
        min_w, min_h = self.min_w.get(), self.min_h.get()
        exclude_set = self._get_exclude_set()
        self._q.put(("status", "buscando…"))

        def work():
            start_count = len(self._items)  # capturado antes: self._items lo va mutando el hilo principal
            got = []
            fetch_err = None
            try:
                for img in self._iter:
                    if img.get("type") != "image":
                        continue
                    iid = img.get("id")
                    if iid in self._seen_ids or iid in self._seen_downloaded:
                        continue
                    if img.get("width", 0) < min_w or img.get("height", 0) < min_h:
                        continue
                    if image_excluded(img, exclude_set):
                        continue
                    self._seen_ids.add(iid)
                    got.append(img)
                    if len(got) >= want:
                        break
            except StopIteration:
                pass
            except Exception as e:
                fetch_err = e  # civitai caído tras reintentos: mostramos igual lo ya juntado
            for img in got:
                thumb = self._download_thumb(img["url"])
                self._q.put(("thumb", (img, thumb)))
            if fetch_err:
                self._q.put(("status", f"{start_count + len(got)} imagenes — civitai cortó la busqueda: {fetch_err}"))
            else:
                self._q.put(("status", f"{start_count + len(got)} imagenes cargadas"))
            self._q.put(("batch_done", None))

        self._start(work)

    def _download_thumb(self, url):
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            im = Image.open(BytesIO(r.content))
            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")
            im.thumbnail((THUMB, THUMB))
            return im.copy()
        except (requests.RequestException, UnidentifiedImageError):
            return None

    # -- grilla ------------------------------------------------------

    def _clear_grid(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self._items = []

    def _place_thumb(self, img, pil_thumb, checked=True):
        idx = len(self._items)
        r, c = divmod(idx, COLS)
        cell = ttk.Frame(self.grid_frame, padding=4, relief="groove", borderwidth=1)
        cell.grid(row=r, column=c, padx=3, pady=3)

        var = tk.BooleanVar(value=checked)
        if pil_thumb is not None:
            photo = ImageTk.PhotoImage(pil_thumb)
            self._photo_cache[img["id"]] = photo
            lbl = tk.Label(cell, image=photo, cursor="hand2")
            lbl.pack()
            lbl.bind("<Button-1>", lambda e, v=var: v.set(not v.get()))
            lbl.bind("<Double-Button-1>", lambda e, u=img.get("id"): webbrowser.open(
                f"https://civitai.com/images/{u}"))
        else:
            ttk.Label(cell, text="(sin preview)", width=20).pack()

        stats = img.get("stats") or {}
        cap = f"♥{stats.get('heartCount', 0)} · {img.get('width')}x{img.get('height')}"
        ttk.Checkbutton(cell, text=cap, variable=var).pack()

        self._items.append((img, var))

    def _set_all(self, val):
        for _img, var in self._items:
            var.set(val)

    # -- descarga ------------------------------------------------------

    def on_download(self):
        if self._busy:
            return
        selected = [img for img, var in self._items if var.get()]
        if not selected:
            messagebox.showwarning("Nada seleccionado", "Marcá al menos una imagen.")
            return
        max_dl = self.max_dl.get()
        selected = selected[:max_dl]
        out_dir = self.out_dir.get().strip()
        if not out_dir:
            messagebox.showwarning("Falta carpeta", "Elegí carpeta destino.")
            return
        os.makedirs(out_dir, exist_ok=True)

        self._busy = True
        self.btn_download.configure(state="disabled")
        self.progress.configure(maximum=len(selected), value=0)

        def work():
            # log local (auditoria de la carpeta) + registro global de IDs (dedup real)
            log_path = os.path.join(out_dir, "downloaded_urls.log")

            saved, skipped, no_prompt = 0, 0, 0
            with open(log_path, "a", encoding="utf-8") as log_file, \
                 open(GLOBAL_SEEN_FILE, "a", encoding="utf-8") as global_file:
                for i, img in enumerate(selected, 1):
                    url = img["url"]
                    iid = img["id"]
                    if iid in self._seen_downloaded:
                        skipped += 1
                        self._q.put(("progress", i))
                        continue
                    try:
                        r = requests.get(url, timeout=REQUEST_TIMEOUT)
                        r.raise_for_status()
                        pic = Image.open(BytesIO(r.content))
                        if pic.mode in ("RGBA", "P"):
                            pic = pic.convert("RGB")
                        pic.save(os.path.join(out_dir, f"{iid}.jpg"), quality=95)

                        prompt = (img.get("meta") or {}).get("prompt")
                        if prompt:
                            with open(os.path.join(out_dir, f"{iid}.txt"), "w", encoding="utf-8") as mf:
                                mf.write(TAG_RE.sub("", prompt))
                        else:
                            no_prompt += 1

                        log_file.write(url + "\n")
                        log_file.flush()
                        global_file.write(f"{iid}\n")
                        global_file.flush()
                        self._seen_downloaded.add(iid)
                        saved += 1
                    except (requests.RequestException, UnidentifiedImageError) as e:
                        self._q.put(("status", f"error bajando {url}: {e}"))
                    self._q.put(("progress", i))

            msg = f"Guardadas: {saved}\nYa descargadas antes (omitidas): {skipped}\nSin prompt (.txt): {no_prompt}\nCarpeta: {out_dir}"
            self._q.put(("dl_done", msg))
            self._q.put(("batch_done", None))

        self._start(work)


if __name__ == "__main__":
    Scraper().mainloop()
