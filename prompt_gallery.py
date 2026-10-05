# -*- coding: utf-8 -*-
"""
Prompt Gallery - busca entre tus imagenes y videos generados por IA por el
texto del prompt o por LoRA, y los muestra en una grilla de miniaturas.

Uso:
  - Doble clic en "Prompt Gallery.bat" y elegir carpeta, o arrastrar una
    carpeta sobre el .bat.
  - Buscar: palabras separadas por espacio (deben estar todas). Con "-" delante
    se excluye (palabra completa):   beach sunset -male
    Busca en el prompt positivo, los nombres de LoRA y el nombre del archivo.
  - Clic en un LoRA de la lista izquierda para filtrar por el.
  - Clic en una miniatura = ver prompt / LoRAs / parametros. Doble clic = abrir.

Notas:
  - Reutiliza los parsers de metadata_viewer.py (misma carpeta): ComfyUI,
    A1111/Forge, Civitai y video (PyAV). No usa red.
  - Indice en _prompt_gallery_cache.json junto al script (clave = ruta,
    se invalida por mtime + tamano). Solo guarda texto, no miniaturas.
  - Las miniaturas se generan por pagina (PAGE) en un hilo, no al indexar.
"""
import os, sys, json, re, queue, threading, subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import metadata_viewer as mv          # parsers (extract, extract_video, air_label)

CACHE_FILE = os.path.join(HERE, "_prompt_gallery_cache.json")
VIEWER = os.path.join(HERE, "metadata_viewer.py")
DEFAULT_DIR = r"D:\Program Files\ComfyUI\output"
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
MEDIA_EXT = IMG_EXT + tuple(mv.VIDEO_EXT)
THUMB = 168          # lado de la miniatura en px
PAGE = 60            # miniaturas por pagina
MAX_PHOTOS = 1500    # miniaturas en memoria antes de vaciar el cache


# ------------------------------------------------ logica pura (sin Tk)
def iter_media(root):
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            if fn.lower().endswith(MEDIA_EXT):
                yield os.path.join(dp, fn)


def lora_label(lo):
    n = lo[0]
    if isinstance(n, dict):
        try:
            return str(mv.air_label(n))
        except Exception:
            return str(n.get("urn") or n.get("version_id") or "?")
    n = os.path.basename(str(n).replace("\\", "/"))
    return os.path.splitext(n)[0]


def make_record(path, st):
    rec = {"path": path, "mtime": st.st_mtime, "size": st.st_size,
           "positive": "", "negative": "", "loras": [], "params": {},
           "source": "", "dim": ""}
    try:
        d = mv.extract(path)
    except Exception as e:
        rec["source"] = "(no se pudo leer: %s)" % e
        return rec
    try:
        rec["positive"] = str(d.get("positive") or "")
        rec["negative"] = str(d.get("negative") or "")
        rec["source"] = str(d.get("source") or "")
        for lo in d.get("loras") or []:
            try:
                rec["loras"].append([lora_label(lo), str(lo[1])])
            except Exception:
                pass
        p = d.get("params")
        if isinstance(p, dict):
            rec["params"] = {str(k): str(v) for k, v in p.items()}
        sz = d.get("size")
        if sz:
            rec["dim"] = "%dx%d" % (sz[0], sz[1])
    finally:
        im = d.get("image")
        if im is not None:
            try:
                im.close()
            except Exception:
                pass
    return rec


def has_meta(rec):
    return bool(rec.get("positive") or rec.get("loras"))


def haystack(rec):
    return " ".join([rec.get("positive", ""), os.path.basename(rec["path"]),
                     " ".join(l[0] for l in rec.get("loras", []))]).lower()


def parse_query(q):
    inc, exc = [], []
    for t in q.lower().split():
        if t.startswith("-") and len(t) > 1:
            exc.append(re.compile(r"(?<!\w)%s(?!\w)" % re.escape(t[1:])))
        elif t != "-":
            inc.append(t)
    return inc, exc


def matches(rec, inc, exc, lora=None, only_meta=False):
    if only_meta and not has_meta(rec):
        return False
    if lora and lora not in [l[0] for l in rec.get("loras", [])]:
        return False
    hay = rec.get("_hay")
    if hay is None:
        hay = rec["_hay"] = haystack(rec)
    return all(t in hay for t in inc) and not any(r.search(hay) for r in exc)


def lora_counts(recs):
    c = {}
    for r in recs:
        for name in {l[0] for l in r.get("loras", [])}:
            c[name] = c.get(name, 0) + 1
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d["files"] if d.get("v") == 1 else {}
    except Exception:
        return {}


def save_cache(files):
    clean = {p: {k: v for k, v in r.items() if not k.startswith("_")}
             for p, r in files.items()}
    tmp = CACHE_FILE + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "files": clean}, f, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)


def load_thumb(path):
    if path.lower().endswith(tuple(mv.VIDEO_EXT)):
        im = mv.extract_video(path).get("image")
        if im is None:
            return None
    else:
        im = Image.open(path)
        try:
            im.draft("RGB", (THUMB * 2, THUMB * 2))   # JPEG: decodifica reducido
        except Exception:
            pass
    im.thumbnail((THUMB, THUMB))
    out = im.convert("RGB")
    try:
        im.close()
    except Exception:
        pass
    return out


# ------------------------------------------------ GUI
BG = "#1e1f22"; BG2 = "#2b2d31"; FG = "#e3e5e8"; ACC = "#5865f2"; MUT = "#949ba4"
GOOD = "#3ba55d"; BAD = "#e06c6c"; WARN = "#d9a441"
SORTS = ("Más nuevas", "Más antiguas", "Nombre")
ALL = "(todas)"


class App(tk.Tk):
    def __init__(self, initial=None):
        super().__init__()
        self.title("Prompt Gallery"); self.geometry("1360x820"); self.configure(bg=BG)
        self._q = queue.Queue()
        self._busy = False; self._cancel = False
        self._gen = 0                 # generacion de busqueda (descarta thumbs viejas)
        self.records = []; self.results = []; self.shown = 0
        self.cells = []; self.cell_by_path = {}; self._photos = {}
        self.sel = None; self.lora = None; self._cols = 0

        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=BG2)
        st.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        st.configure("Muted.TLabel", background=BG, foreground=MUT, font=("Segoe UI", 9))
        st.configure("TButton", font=("Segoe UI", 9), padding=6)
        st.map("TButton", background=[("active", ACC)])
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.configure("TEntry", fieldbackground=BG2, foreground=FG, insertcolor=FG)
        st.configure("TCombobox", fieldbackground=BG2, background=BG2, foreground=FG)
        st.map("TCombobox", fieldbackground=[("readonly", BG2)], foreground=[("readonly", FG)])
        st.configure("Treeview", background=BG2, fieldbackground=BG2, foreground=FG,
                     borderwidth=0, rowheight=22)
        st.configure("Treeview.Heading", background=BG, foreground=MUT)
        st.map("Treeview", background=[("selected", ACC)])
        st.configure("Horizontal.TProgressbar", troughcolor=BG2, background=ACC)
        self._blank = tk.PhotoImage(width=THUMB, height=THUMB)

        # --- barra superior
        top = ttk.Frame(self); top.pack(fill="x", padx=10, pady=(10, 6))
        self.btn_dir = ttk.Button(top, text="Carpeta…", command=self.pick)
        self.btn_dir.pack(side="left")
        self.btn_cancel = ttk.Button(top, text="Cancelar", command=self._do_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(6, 12))
        ttk.Label(top, text="Buscar:").pack(side="left")
        self.q_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.q_var, width=44)
        ent.pack(side="left", padx=6)
        ent.bind("<Return>", lambda e: self.apply())
        ttk.Button(top, text="Buscar", command=self.apply).pack(side="left")
        ttk.Button(top, text="Limpiar", command=self.clear).pack(side="left", padx=6)
        self.sort_var = tk.StringVar(value=SORTS[0])
        cb = ttk.Combobox(top, textvariable=self.sort_var, values=SORTS, width=13, state="readonly")
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.apply())
        self.meta_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Solo con metadata", variable=self.meta_var,
                        command=self.apply).pack(side="left", padx=6)
        self.dir_lbl = ttk.Label(self, text="(sin carpeta)", style="Muted.TLabel")
        self.dir_lbl.pack(fill="x", padx=12)

        # --- cuerpo: LoRAs | grilla | detalle
        body = ttk.Frame(self); body.pack(fill="both", expand=True, padx=10, pady=6)
        left = ttk.Frame(body); left.pack(side="left", fill="y")
        self.tree = ttk.Treeview(left, columns=("n",), height=20, selectmode="browse")
        self.tree.heading("#0", text="LoRA"); self.tree.heading("n", text="#")
        self.tree.column("#0", width=210); self.tree.column("n", width=44, anchor="e")
        self.tree.pack(side="left", fill="y")
        sb = ttk.Scrollbar(left, command=self.tree.yview); sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self._lora_pick)

        right = ttk.Frame(body, width=380); right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self.info_lbl = ttk.Label(right, text="", style="Muted.TLabel", wraplength=370, justify="left")
        self.info_lbl.pack(fill="x", pady=(0, 4))
        self.txt = tk.Text(right, bg=BG2, fg=FG, wrap="word", relief="flat",
                           font=("Segoe UI", 10), padx=8, pady=8, state="disabled")
        self.txt.tag_configure("h", foreground=ACC, font=("Segoe UI", 10, "bold"))
        self.txt.tag_configure("mut", foreground=MUT)
        self.txt.pack(fill="both", expand=True)
        br = ttk.Frame(right); br.pack(fill="x", pady=(6, 0))
        ttk.Button(br, text="Copiar prompt", command=lambda: self.copy("positive")).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(br, text="Copiar negativo", command=lambda: self.copy("negative")).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(br, text="Abrir archivo", command=self.open_file).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(br, text="Abrir carpeta", command=self.open_dir).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(br, text="Ver en Metadata Viewer", command=self.open_viewer).grid(row=2, column=0, columnspan=2, sticky="ew", padx=2, pady=2)
        br.columnconfigure(0, weight=1); br.columnconfigure(1, weight=1)

        mid = ttk.Frame(body); mid.pack(side="left", fill="both", expand=True, padx=8)
        self.canvas = tk.Canvas(mid, bg=BG, highlightthickness=0)
        vs = ttk.Scrollbar(mid, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y"); self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        # --- barra inferior
        bot = ttk.Frame(self); bot.pack(fill="x", padx=10, pady=(0, 10))
        self.btn_more = ttk.Button(bot, text="Cargar más", command=self.more, state="disabled")
        self.btn_more.pack(side="left")
        self.status = ttk.Label(bot, text="Elige una carpeta para indexar.", style="Muted.TLabel")
        self.status.pack(side="left", padx=10)
        self.pb = ttk.Progressbar(bot, mode="determinate", maximum=1000, length=220)
        self.pb.pack(side="right")

        self.after(120, self._pump)
        start = initial or (DEFAULT_DIR if os.path.isdir(DEFAULT_DIR) else None)
        if start:
            self.after(200, lambda: self.scan(start))

    # ---------------------------------------------------- cola / hilos
    def _pump(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "status":
                    self.status.configure(text=payload)
                elif kind == "progress":
                    self.pb.configure(value=payload)
                elif kind == "error":
                    self._set_busy(False)
                    messagebox.showerror("Prompt Gallery", payload)
                elif kind == "indexed":
                    self._indexed(*payload)
                elif kind == "thumb":
                    self._thumb(*payload)
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

    def _set_busy(self, b):
        self._busy = b
        self.btn_dir.configure(state="disabled" if b else "normal")
        self.btn_cancel.configure(state="normal" if b else "disabled")

    def _do_cancel(self):
        self._cancel = True

    # ---------------------------------------------------- indexado
    def pick(self):
        d = filedialog.askdirectory(title="Carpeta con imágenes / videos")
        if d:
            self.scan(os.path.normpath(d))

    def scan(self, root):
        if self._busy:
            return
        if not os.path.isdir(root):
            messagebox.showwarning("Prompt Gallery", "No existe la carpeta:\n%s" % root)
            return
        self._cancel = False
        self._set_busy(True)
        self.dir_lbl.configure(text=root)
        self._start(lambda: self._scan(root))

    def _scan(self, root):
        self._q.put(("status", "Listando archivos…"))
        paths = list(iter_media(root))
        files = load_cache()
        recs, new, n = [], 0, len(paths)
        for i, p in enumerate(paths):
            if self._cancel:
                break
            try:
                st = os.stat(p)
            except OSError:
                continue
            c = files.get(p)
            if c and c.get("mtime") == st.st_mtime and c.get("size") == st.st_size:
                rec = c
            else:
                rec = make_record(p, st)
                files[p] = rec
                new += 1
            recs.append(rec)
            if i % 20 == 0:
                self._q.put(("status", "Indexando %d / %d…" % (i + 1, n)))
                self._q.put(("progress", (i + 1) * 1000 // max(n, 1)))
        if new:
            save_cache(files)
        self._q.put(("progress", 0))
        self._q.put(("indexed", (recs, new, self._cancel)))

    def _indexed(self, recs, new, cancelled):
        self._set_busy(False)
        self.records = recs
        self.lora = None
        self.tree.delete(*self.tree.get_children())
        self.tree.insert("", "end", iid="__all__", text=ALL, values=(len(recs),))
        self._lora_names = {}
        for i, (name, cnt) in enumerate(lora_counts(recs)):
            iid = "l%d" % i
            self._lora_names[iid] = name
            self.tree.insert("", "end", iid=iid, text=name, values=(cnt,))
        self._index_note = "%d archivos, %d con metadata, %d nuevos en el índice%s" % (
            len(recs), sum(1 for r in recs if has_meta(r)), new,
            " (cancelado)" if cancelled else "")
        self.apply()

    # ---------------------------------------------------- busqueda / grilla
    def _lora_pick(self, _=None):
        s = self.tree.selection()
        name = self._lora_names.get(s[0]) if s else None
        if name != self.lora:
            self.lora = name
            self.apply()

    def clear(self):
        self.q_var.set("")
        self.lora = None
        self.tree.selection_remove(*self.tree.selection())
        self.apply()

    def apply(self):
        inc, exc = parse_query(self.q_var.get())
        res = [r for r in self.records
               if matches(r, inc, exc, self.lora, self.meta_var.get())]
        s = self.sort_var.get()
        if s == SORTS[2]:
            res.sort(key=lambda r: os.path.basename(r["path"]).lower())
        else:
            res.sort(key=lambda r: r["mtime"], reverse=(s == SORTS[0]))
        self._gen += 1
        self.results = res; self.shown = 0; self.sel = None
        for c in self.cells:
            c.destroy()
        self.cells = []; self.cell_by_path = {}
        if len(self._photos) > MAX_PHOTOS:
            self._photos = {}
        self.canvas.yview_moveto(0)
        self._show_detail(None)
        self.more()

    def more(self):
        start, end = self.shown, min(self.shown + PAGE, len(self.results))
        cols = self._ncols()
        todo = []
        for i in range(start, end):
            rec = self.results[i]
            cell = tk.Frame(self.inner, bg=BG2, highlightthickness=2,
                            highlightbackground=BG2, cursor="hand2")
            ph = self._photos.get(rec["path"])
            img = tk.Label(cell, image=ph or self._blank, bg=BG2, width=THUMB, height=THUMB)
            img.pack(padx=3, pady=(3, 0))
            name = os.path.basename(rec["path"])
            cap = tk.Label(cell, text=name if len(name) <= 24 else name[:21] + "…",
                           bg=BG2, fg=FG if has_meta(rec) else MUT, font=("Segoe UI", 8))
            cap.pack(pady=(1, 3))
            for w in (cell, img, cap):
                w.bind("<Button-1>", lambda e, k=i: self.select(k))
                w.bind("<Double-Button-1>", lambda e, k=i: (self.select(k), self.open_file()))
            cell._img = img
            cell.grid(row=i // cols, column=i % cols, padx=4, pady=4)
            self.cells.append(cell)
            self.cell_by_path[rec["path"]] = cell
            if ph is None:
                todo.append(rec["path"])
        self.shown = end
        self._cols = cols
        rest = len(self.results) - end
        self.btn_more.configure(state="normal" if rest else "disabled",
                                text="Cargar más (%d)" % rest if rest else "Cargar más")
        self.status.configure(text="%d resultados · mostrando %d   |   %s" % (
            len(self.results), end, getattr(self, "_index_note", "")))
        if todo:
            gen = self._gen
            self._start(lambda: self._thumbs(gen, todo))

    def _thumbs(self, gen, paths):
        for p in paths:
            if gen != self._gen:
                return
            try:
                im = load_thumb(p)
            except Exception:
                im = None
            self._q.put(("thumb", (gen, p, im)))

    def _thumb(self, gen, path, im):
        if im is None:
            return
        ph = self._photos[path] = ImageTk.PhotoImage(im)   # guardar ref (GC de Tk)
        cell = self.cell_by_path.get(path)
        if gen == self._gen and cell is not None:
            cell._img.configure(image=ph)

    def _ncols(self):
        return max(1, self.canvas.winfo_width() // (THUMB + 22))

    def _on_resize(self, _=None):
        cols = self._ncols()
        if cols != self._cols and self.cells:
            self._cols = cols
            for i, c in enumerate(self.cells):
                c.grid(row=i // cols, column=i % cols, padx=4, pady=4)

    def _wheel(self, e):
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    # ---------------------------------------------------- detalle
    def select(self, i):
        if self.sel is not None and self.sel < len(self.cells):
            self.cells[self.sel].configure(highlightbackground=BG2)
        self.sel = i
        self.cells[i].configure(highlightbackground=ACC)
        self._show_detail(self.results[i])

    def _cur(self):
        return self.results[self.sel] if self.sel is not None else None

    def _show_detail(self, rec):
        t = self.txt
        t.configure(state="normal"); t.delete("1.0", "end")
        if rec is None:
            self.info_lbl.configure(text="")
            t.insert("end", "Clic en una miniatura para ver su prompt.", "mut")
        else:
            self.info_lbl.configure(text="%s\n%s  %s" % (rec["path"], rec.get("dim", ""), rec.get("source", "")))
            if not has_meta(rec):
                t.insert("end", "Sin metadata reconocible (re-guardado, screenshot o red social la borran).\n", "mut")
            if rec.get("positive"):
                t.insert("end", "PROMPT\n", "h"); t.insert("end", rec["positive"] + "\n\n")
            if rec.get("negative"):
                t.insert("end", "NEGATIVO\n", "h"); t.insert("end", rec["negative"] + "\n\n")
            if rec.get("loras"):
                t.insert("end", "LORAS\n", "h")
                for n, w in rec["loras"]:
                    t.insert("end", "%s  " % n); t.insert("end", "%s\n" % w, "mut")
                t.insert("end", "\n")
            if rec.get("params"):
                t.insert("end", "PARÁMETROS\n", "h")
                for k, v in rec["params"].items():
                    t.insert("end", "%s: " % k, "mut"); t.insert("end", "%s\n" % v)
        t.configure(state="disabled")

    def copy(self, what):
        rec = self._cur()
        if not rec or not rec.get(what):
            return
        self.clipboard_clear(); self.clipboard_append(rec[what])
        self.title("Prompt Gallery — copiado")
        self.after(1200, lambda: self.title("Prompt Gallery"))

    def open_file(self):
        rec = self._cur()
        if rec:
            os.startfile(rec["path"])

    def open_dir(self):
        rec = self._cur()
        if rec:
            os.startfile(os.path.dirname(rec["path"]))

    def open_viewer(self):
        rec = self._cur()
        if rec:
            subprocess.Popen([sys.executable, VIEWER, rec["path"]], creationflags=0x08000000)


if __name__ == "__main__":
    arg = next((a for a in sys.argv[1:] if os.path.isdir(a)), None)
    App(arg).mainloop()
