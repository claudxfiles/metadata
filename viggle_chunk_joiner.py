# -*- coding: utf-8 -*-
"""
Viggle Chunk Joiner - une en un solo video los clips generados por pasadas
(chunks) de un workflow Viggle-Animate-H3 / MiniMax-H3 con ventanas solapadas.

- Si ninguna costura tiene solape: empalme directo (concat, stream copy, sin
  recodificar, instantaneo).
- Si hay solape (frames compartidos entre pasada y pasada, ej. 22 frames del
  esquema chunk_frames=124/overlap_frames=22): crossfade en video + audio
  sobre esos frames exactos, con la duracion calculada de fps/overlap.
- Opcional: mux de un audio externo (ej. el video "fuente" original) en vez
  del audio de los clips.

Uso: doble clic en "Viggle Chunk Joiner.bat", agregar clips EN ORDEN, ajustar
solape por costura si aplica, Unir.
"""
import os, sys, json, queue, threading, subprocess, shutil, time, datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ---------------------------------------------------------------- ffmpeg

_FFMPEG_FALLBACK = r"C:\Users\claud\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"
_FFPROBE_FALLBACK = r"C:\Users\claud\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffprobe.exe"


def _find_exe(name, fallback):
    p = shutil.which(name)
    if p:
        return p
    return fallback if os.path.isfile(fallback) else name


FFMPEG = _find_exe("ffmpeg", _FFMPEG_FALLBACK)
FFPROBE = _find_exe("ffprobe", _FFPROBE_FALLBACK)

NONE_TRANSITION = "ninguna (corte directo)"
TRANSITIONS = [NONE_TRANSITION, "fade", "fadeblack", "dissolve", "wipeleft", "slideleft", "circlecrop", "smoothleft"]


def probe(path):
    """Devuelve duration(s), fps, codec, w, h, audio(bool)."""
    info = {"duration": 0.0, "fps": 0.0, "codec": None, "w": None, "h": None, "audio": False, "error": None}
    try:
        out = subprocess.check_output(
            [FFPROBE, "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,r_frame_rate,width,height",
             "-of", "json", path],
            creationflags=CREATE_NO_WINDOW, stderr=subprocess.STDOUT)
        d = json.loads(out.decode("utf-8", "replace"))
        info["duration"] = float((d.get("format") or {}).get("duration") or 0)
        for s in d.get("streams", []):
            if s.get("codec_type") == "video" and info["codec"] is None:
                info["codec"] = s.get("codec_name")
                info["w"], info["h"] = s.get("width"), s.get("height")
                fr = s.get("r_frame_rate") or "0/1"
                try:
                    n, den = fr.split("/")
                    info["fps"] = round(float(n) / float(den), 5) if float(den) else 0.0
                except Exception:
                    info["fps"] = 0.0
            elif s.get("codec_type") == "audio":
                info["audio"] = True
    except Exception as e:
        info["error"] = str(e)
    return info


def fmt_dur(s):
    try:
        s = float(s)
    except Exception:
        return "?"
    m, sec = divmod(s, 60)
    return f"{int(m)}:{sec:05.2f}"


# ---------------------------------------------------------------- construccion ffmpeg

class JoinError(Exception):
    pass


def build_concat_copy(clips, audio_src, audio_start, out_path, list_path):
    """Empalme directo sin recodificar (requiere mismo codec/res/fps)."""
    with open(list_path, "w", encoding="utf-8") as f:
        for c in clips:
            f.write("file '%s'\n" % os.path.abspath(c).replace("'", "'\\''"))
    if audio_src:
        # concat solo video (sin audio) y luego mux del audio externo
        audio_in = (["-ss", str(audio_start)] if audio_start else []) + ["-i", audio_src]
        cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
               *audio_in, "-map", "0:v", "-map", "1:a", "-c:v", "copy",
               "-c:a", "aac", "-b:a", "192k", "-shortest",
               "-progress", "pipe:1", "-nostats", out_path]
    else:
        cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
               "-c", "copy", "-progress", "pipe:1", "-nostats", out_path]
    return cmd


def build_concat_reencode(clips, has_audio, audio_src, audio_start, fps_out, out_path):
    """Empalme directo con recodificacion (codecs/res distintos)."""
    n = len(clips)
    cmd = [FFMPEG, "-y"]
    for c in clips:
        cmd += ["-i", c]
    use_audio = has_audio and not audio_src
    if audio_src:
        if audio_start:
            cmd += ["-ss", str(audio_start)]
        cmd += ["-i", audio_src]
    parts = "".join(f"[{i}:v]" for i in range(n))
    if use_audio:
        parts_a = "".join(f"[{i}:a]" for i in range(n))
        filt = f"{parts}concat=n={n}:v=1:a=0[vv];{parts_a}concat=n={n}:v=0:a=1[aa]"
    else:
        filt = f"{parts}concat=n={n}:v=1:a=0[vv]"
    cmd += ["-filter_complex", filt, "-map", "[vv]"]
    if audio_src:
        cmd += ["-map", f"{n}:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    elif use_audio:
        cmd += ["-map", "[aa]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-r", str(fps_out), "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p",
            "-progress", "pipe:1", "-nostats", out_path]
    return cmd


def build_trim_concat(clips, durations, overlaps, fps_out, has_audio, audio_src, audio_start, out_path):
    """Sin transicion: recorta de cada clip (salvo el primero) los frames que
    duplica del anterior y concatena en corte directo, sin blend."""
    n = len(clips)
    use_audio = has_audio and not audio_src
    cmd = [FFMPEG, "-y"]
    for c in clips:
        cmd += ["-i", c]
    if audio_src:
        if audio_start:
            cmd += ["-ss", str(audio_start)]
        cmd += ["-i", audio_src]

    seg_v = []
    seg_a = []
    warnings = []
    total = durations[0]
    for i in range(n):
        if i == 0:
            seg_v.append(f"[0:v]")
            if use_audio:
                seg_a.append(f"[0:a]")
            continue
        ov = max(0, overlaps[i])
        d = ov / fps_out if fps_out else 0.0
        cap = max(0.0, durations[i] * 0.9)
        if d > cap:
            warnings.append(f"costura {i}: solape pedido {d:.2f}s excede el clip, recortado a {cap:.2f}s")
            d = cap
        if d > 0:
            seg_v.append(f"[{i}:v]trim=start={d:.4f}:end={durations[i]:.4f},setpts=PTS-STARTPTS[tv{i}]")
            if use_audio:
                seg_a.append(f"[{i}:a]atrim=start={d:.4f}:end={durations[i]:.4f},asetpts=PTS-STARTPTS[ta{i}]")
            total += durations[i] - d
        else:
            seg_v.append(f"[{i}:v]null[tv{i}]")
            if use_audio:
                seg_a.append(f"[{i}:a]anull[ta{i}]")
            total += durations[i]

    # etiquetas de entrada de video para el concat final
    trimmed_v = ["0:v"] + [f"tv{i}" for i in range(1, n)]
    filt = ";".join(seg_v[1:])
    concat_in_v = "".join(f"[{t}]" for t in trimmed_v)
    filt = (filt + ";" if filt else "") + f"{concat_in_v}concat=n={n}:v=1:a=0[vv]"
    if use_audio:
        trimmed_a = ["0:a"] + [f"ta{i}" for i in range(1, n)]
        filt_a = ";".join(seg_a[1:])
        concat_in_a = "".join(f"[{t}]" for t in trimmed_a)
        filt = filt + ";" + (filt_a + ";" if filt_a else "") + f"{concat_in_a}concat=n={n}:v=0:a=1[aa]"

    cmd += ["-filter_complex", filt, "-map", "[vv]"]
    if audio_src:
        cmd += ["-map", f"{n}:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    elif use_audio:
        cmd += ["-map", "[aa]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-r", str(fps_out), "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p",
            "-progress", "pipe:1", "-nostats", out_path]
    return cmd, total, warnings


def build_crossfade(clips, durations, overlaps, fps_out, transition, has_audio, audio_src, audio_start, out_path):
    """Crossfade en las costuras usando los frames de solape indicados por fila."""
    n = len(clips)
    with_audio_filter = has_audio and not audio_src
    cmd = [FFMPEG, "-y"]
    for c in clips:
        cmd += ["-i", c]
    if audio_src:
        if audio_start:
            cmd += ["-ss", str(audio_start)]
        cmd += ["-i", audio_src]

    parts = []
    cur_v = "0:v"
    cur_a = "0:a"
    cum = durations[0]
    warnings = []
    for i in range(1, n):
        ov = max(0, overlaps[i])
        d = ov / fps_out if fps_out else 0.0
        cap = max(0.05, min(durations[i - 1], durations[i]) * 0.9)
        if d <= 0:
            d = min(0.2, cap)
            warnings.append(f"costura {i}: solape 0 -> crossfade minimo forzado ({d:.2f}s)")
        elif d > cap:
            warnings.append(f"costura {i}: solape pedido {d:.2f}s excede el clip, recortado a {cap:.2f}s")
            d = cap
        offset = max(0.0, cum - d)
        outv = f"v{i}"
        parts.append(f"[{cur_v}][{i}:v]xfade=transition={transition}:duration={d:.4f}:offset={offset:.4f}[{outv}]")
        cur_v = outv
        if with_audio_filter:
            outa = f"a{i}"
            parts.append(f"[{cur_a}][{i}:a]acrossfade=d={d:.4f}[{outa}]")
            cur_a = outa
        cum = cum + durations[i] - d

    filt = ";".join(parts)
    cmd += ["-filter_complex", filt, "-map", f"[{cur_v}]"]
    if audio_src:
        cmd += ["-map", f"{n}:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    elif with_audio_filter:
        cmd += ["-map", f"[{cur_a}]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-r", str(fps_out), "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p",
            "-progress", "pipe:1", "-nostats", out_path]
    return cmd, cum, warnings


# ---------------------------------------------------------------- GUI

BG = "#1e1f22"; BG2 = "#2b2d31"; FG = "#e3e5e8"; ACC = "#5865f2"; MUT = "#949ba4"
GOOD = "#3ba55d"; BAD = "#e06c6c"; WARN = "#d9a441"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Viggle Chunk Joiner")
        self.geometry("980x680")
        self.configure(bg=BG)
        self.clips = []      # list of dict: path, duration, fps, codec, w, h, audio, overlap
        self.proc = None
        self._q = queue.Queue()
        self._build_style()
        self._build_ui()
        self.after(80, self._poll_queue)

    # ---------------- style ----------------
    def _build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=BG, foreground=FG, font=("Segoe UI", 9))
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        st.configure("Muted.TLabel", background=BG, foreground=MUT, font=("Segoe UI", 9))
        st.configure("TButton", font=("Segoe UI", 9), padding=6)
        st.configure("Accent.TButton", font=("Segoe UI Semibold", 9))
        st.map("Accent.TButton", background=[("!disabled", ACC)])
        st.configure("TEntry", fieldbackground=BG2, foreground=FG, insertcolor=FG)
        st.configure("TCombobox", fieldbackground=BG2, foreground=FG)
        st.configure("Treeview", background=BG2, foreground=FG, fieldbackground=BG2,
                     rowheight=24, font=("Segoe UI", 9))
        st.configure("Treeview.Heading", background=BG, foreground=MUT, font=("Segoe UI Semibold", 9))
        st.map("Treeview", background=[("selected", ACC)])
        st.configure("Horizontal.TProgressbar", background=ACC, troughcolor=BG2)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=BG2, foreground=MUT, padding=(14, 6), font=("Segoe UI", 9))
        st.map("TNotebook.Tab", background=[("selected", BG)], foreground=[("selected", FG)])

    # ---------------- layout ----------------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        tab1 = ttk.Frame(self.nb)
        tab2 = ttk.Frame(self.nb)
        self.nb.add(tab1, text="Unir clips")
        self.nb.add(tab2, text="Planificador de pasadas")

        top = ttk.Frame(tab1); top.pack(fill="x", **pad)
        ttk.Label(top, text="Clips (en orden de union)", font=("Segoe UI Semibold", 11)).pack(side="left")

        cols = ("idx", "file", "dur", "fps", "res", "audio", "overlap")
        self.tree = ttk.Treeview(tab1, columns=cols, show="headings", height=8, selectmode="browse")
        heads = {"idx": "#", "file": "Archivo", "dur": "Duracion", "fps": "FPS", "res": "Res",
                 "audio": "Audio", "overlap": "Solape c/ anterior (frames)"}
        widths = {"idx": 30, "file": 340, "dur": 80, "fps": 60, "res": 100, "audio": 60, "overlap": 190}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor="center" if c != "file" else "w")
        self.tree.pack(fill="x", padx=10)
        self.tree.bind("<Double-1>", self._edit_overlap)

        btns = ttk.Frame(tab1); btns.pack(fill="x", **pad)
        ttk.Button(btns, text="+ Agregar clips", command=self._add_clips).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Quitar", command=self._remove_clip).pack(side="left", padx=6)
        ttk.Button(btns, text="Subir", command=lambda: self._move(-1)).pack(side="left", padx=6)
        ttk.Button(btns, text="Bajar", command=lambda: self._move(1)).pack(side="left", padx=6)
        ttk.Label(btns, text="   solape por defecto:").pack(side="left", padx=(20, 4))
        self.default_overlap = tk.StringVar(value="22")
        ttk.Entry(btns, textvariable=self.default_overlap, width=5).pack(side="left")
        ttk.Button(btns, text="Aplicar a todas las costuras", command=self._apply_default_overlap).pack(side="left", padx=6)

        opts = ttk.Frame(tab1); opts.pack(fill="x", **pad)
        ttk.Label(opts, text="FPS salida:").grid(row=0, column=0, sticky="w")
        self.fps_var = tk.StringVar(value="24")
        ttk.Entry(opts, textvariable=self.fps_var, width=6).grid(row=0, column=1, sticky="w", padx=(4, 20))

        ttk.Label(opts, text="Transicion:").grid(row=0, column=2, sticky="w")
        self.trans_var = tk.StringVar(value="fade")
        ttk.Combobox(opts, textvariable=self.trans_var, values=TRANSITIONS, width=12,
                     state="readonly").grid(row=0, column=3, sticky="w", padx=(4, 20))

        ttk.Label(opts, text="Audio origen (opcional, reemplaza el de los clips):").grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.audio_var = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.audio_var, width=55).grid(row=1, column=2, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(opts, text="Elegir...", command=self._pick_audio).grid(row=1, column=4, sticky="w", padx=6, pady=(8, 0))
        ttk.Button(opts, text="Quitar", command=lambda: self.audio_var.set("")).grid(row=1, column=5, sticky="w", pady=(8, 0))

        ttk.Label(opts, text="Audio inicio (s):").grid(row=1, column=6, sticky="w", padx=(20, 0), pady=(8, 0))
        self.audio_start_var = tk.StringVar(value="0")
        ttk.Entry(opts, textvariable=self.audio_start_var, width=8).grid(row=1, column=7, sticky="w", padx=(4, 0), pady=(8, 0))

        ttk.Label(opts, text="Salida:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.out_var = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.out_var, width=55).grid(row=2, column=2, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(opts, text="Elegir...", command=self._pick_out).grid(row=2, column=4, sticky="w", padx=6, pady=(8, 0))

        run = ttk.Frame(tab1); run.pack(fill="x", **pad)
        self.join_btn = ttk.Button(run, text="Unir", style="Accent.TButton", command=self._start_join)
        self.join_btn.pack(side="left")
        self.open_btn = ttk.Button(run, text="Abrir carpeta", command=self._open_folder, state="disabled")
        self.open_btn.pack(side="left", padx=8)
        self.play_btn = ttk.Button(run, text="Reproducir", command=self._play, state="disabled")
        self.play_btn.pack(side="left")

        self.progress = ttk.Progressbar(tab1, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", padx=10, pady=(0, 6))
        self.status_lbl = ttk.Label(tab1, text="Listo.", style="Muted.TLabel")
        self.status_lbl.pack(fill="x", padx=10)

        self.log = tk.Text(tab1, height=14, bg=BG2, fg=FG, insertbackground=FG,
                            relief="flat", font=("Consolas", 9), padx=8, pady=8)
        self.log.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.log.tag_configure("err", foreground=BAD)
        self.log.tag_configure("ok", foreground=GOOD)
        self.log.tag_configure("warn", foreground=WARN)

        self._build_planner_tab(tab2, pad)

    # ---------------- pestana planificador ----------------
    def _build_planner_tab(self, tab2, pad):
        info = ttk.Frame(tab2); info.pack(fill="x", **pad)
        ttk.Label(info, text="Planificador de pasadas (grilla H3 17k+5)",
                  font=("Segoe UI Semibold", 11)).pack(anchor="w")
        ttk.Label(info, style="Muted.TLabel",
                  text="Calcula skip_first_frames / frame_load_cap / length para las pasadas del workflow "
                       "Viggle-Animate-H3, antes de generar los clips en ComfyUI.").pack(anchor="w", pady=(2, 0))

        src = ttk.Frame(tab2); src.pack(fill="x", **pad)
        ttk.Label(src, text="Video fuente (opcional, autocompleta frames totales / fps):").grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.plan_src_var = tk.StringVar(value="")
        ttk.Entry(src, textvariable=self.plan_src_var, width=55).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(src, text="Elegir...", command=self._plan_pick_src).grid(row=1, column=2, sticky="w", padx=6, pady=(4, 0))

        form = ttk.Frame(tab2); form.pack(fill="x", **pad)
        ttk.Label(form, text="FPS (force_rate):").grid(row=0, column=0, sticky="w")
        self.plan_fps_var = tk.StringVar(value="24")
        ttk.Entry(form, textvariable=self.plan_fps_var, width=8).grid(row=0, column=1, sticky="w", padx=(4, 20))

        ttk.Label(form, text="Frames totales @ fps:").grid(row=0, column=2, sticky="w")
        self.plan_total_var = tk.StringVar(value="708")
        ttk.Entry(form, textvariable=self.plan_total_var, width=8).grid(row=0, column=3, sticky="w", padx=(4, 20))

        ttk.Label(form, text="length / chunk_frames:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.plan_len_var = tk.StringVar(value="124")
        ttk.Entry(form, textvariable=self.plan_len_var, width=8).grid(row=1, column=1, sticky="w", padx=(4, 20), pady=(8, 0))

        ttk.Label(form, text="overlap_frames:").grid(row=1, column=2, sticky="w", pady=(8, 0))
        self.plan_overlap_var = tk.StringVar(value="22")
        ttk.Entry(form, textvariable=self.plan_overlap_var, width=8).grid(row=1, column=3, sticky="w", padx=(4, 20), pady=(8, 0))

        self.plan_grid_lbl = ttk.Label(form, style="Muted.TLabel", text="")
        self.plan_grid_lbl.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))

        btnf = ttk.Frame(tab2); btnf.pack(fill="x", **pad)
        ttk.Button(btnf, text="Calcular pasadas", style="Accent.TButton", command=self._plan_calc).pack(side="left")
        ttk.Button(btnf, text="Copiar tabla (Markdown)", command=self._plan_copy).pack(side="left", padx=8)

        cols = ("pasada", "skip", "cap", "length", "cubre")
        self.plan_tree = ttk.Treeview(tab2, columns=cols, show="headings", height=12, selectmode="browse")
        heads = {"pasada": "Pasada", "skip": "skip_first_frames (nodo 3)", "cap": "frame_load_cap (nodo 3)",
                 "length": "length (nodo 1)", "cubre": "Cubre frames"}
        widths = {"pasada": 60, "skip": 190, "cap": 170, "length": 110, "cubre": 220}
        for c in cols:
            self.plan_tree.heading(c, text=heads[c])
            self.plan_tree.column(c, width=widths[c], anchor="center" if c != "cubre" else "w")
        self.plan_tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.plan_status = ttk.Label(tab2, text="", style="Muted.TLabel")
        self.plan_status.pack(fill="x", padx=10, pady=(0, 10))

        self._plan_rows = []
        self._plan_calc()

    def _plan_pick_src(self):
        p = filedialog.askopenfilename(title="Video fuente",
                                        filetypes=[("Video", "*.mp4;*.mov;*.mkv;*.webm"), ("Todos", "*.*")])
        if not p:
            return
        self.plan_src_var.set(p)
        info = probe(p)
        if info.get("error"):
            messagebox.showerror("Planificador", info["error"])
            return
        fps = float(self.plan_fps_var.get() or info["fps"] or 24)
        total = int(round(info["duration"] * fps))
        self.plan_total_var.set(str(total))
        self._log_planner_note(f"{os.path.basename(p)}: {info['duration']:.2f}s nativo -> {total} frames @ {fps} fps")

    def _log_planner_note(self, msg):
        self.plan_status.config(text=msg)

    _H3_GRID_STEP = 17
    _H3_GRID_BASE = 5

    def _plan_calc(self):
        try:
            fps = float(self.plan_fps_var.get())
            total = int(self.plan_total_var.get())
            length = int(self.plan_len_var.get())
            overlap = int(self.plan_overlap_var.get())
        except ValueError:
            messagebox.showerror("Planificador", "Revisa que fps/total/length/overlap sean numeros.")
            return
        if length <= overlap:
            messagebox.showerror("Planificador", "length debe ser mayor que overlap_frames.")
            return
        if total <= 0 or length <= 0:
            messagebox.showerror("Planificador", "total y length deben ser mayores a 0.")
            return

        rem = (length - self._H3_GRID_BASE) % self._H3_GRID_STEP
        if rem != 0:
            lower = length - rem
            upper = lower + self._H3_GRID_STEP
            self.plan_grid_lbl.config(
                text=f"length={length} no esta en la grilla H3 17k+5 (...{lower}, {upper}...); "
                     f"el node lo redondea igual hacia arriba al siguiente valor de la grilla.",
                foreground=WARN)
        else:
            self.plan_grid_lbl.config(text=f"length={length} = 17*{(length - self._H3_GRID_BASE)//self._H3_GRID_STEP}+5, en grilla H3. OK.",
                                       foreground=GOOD)

        step = length - overlap
        rows = []
        skip = 0
        idx = 1
        while True:
            avail = total - skip
            covered = min(length, avail)
            cubre = f"{skip}–{skip + covered - 1}"
            if covered < length:
                cubre += f" ({covered} frames reales)"
            rows.append((idx, skip, length, length, cubre))
            if covered >= avail:
                break
            skip += step
            idx += 1

        self._plan_rows = rows
        self.plan_tree.delete(*self.plan_tree.get_children())
        for r in rows:
            self.plan_tree.insert("", "end", values=r)
        self._log_planner_note(f"{len(rows)} pasada(s), paso {step} frames, solape {overlap} frames "
                                f"({overlap / fps:.3f}s a {fps} fps).")

    def _plan_copy(self):
        if not self._plan_rows:
            return
        lines = ["| Pasada | skip_first_frames (nodo 3) | frame_load_cap (nodo 3) | length (nodo 1) | Cubre frames |",
                 "|---|---|---|---|---|"]
        for r in self._plan_rows:
            lines.append("| {} | {} | {} | {} | {} |".format(*r))
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))
        self._log_planner_note("Tabla copiada al portapapeles (Markdown).")

    # ---------------- clip list ----------------
    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, c in enumerate(self.clips):
            ov = "-" if i == 0 else str(c["overlap"])
            self.tree.insert("", "end", iid=str(i), values=(
                i + 1, os.path.basename(c["path"]), fmt_dur(c["duration"]),
                round(c["fps"], 2) if c["fps"] else "?",
                f'{c["w"]}x{c["h"]}' if c["w"] else "?",
                "si" if c["audio"] else "no", ov))

    def _add_clips(self):
        paths = filedialog.askopenfilenames(
            title="Elegir clips (selecciona en el orden que quieres unirlos)",
            filetypes=[("Video", "*.mp4;*.mov;*.mkv;*.webm"), ("Todos", "*.*")])
        if not paths:
            return
        for p in paths:
            info = probe(p)
            if info.get("error"):
                self._log(f"No se pudo leer {os.path.basename(p)}: {info['error']}", "err")
                continue
            c = dict(info)
            c["path"] = p
            c["overlap"] = int(self.default_overlap.get() or 0) if self.clips else 0
            self.clips.append(c)
        if self.clips and self.clips[0]["fps"]:
            self.fps_var.set(str(round(self.clips[0]["fps"], 3)).rstrip("0").rstrip("."))
        if not self.out_var.get() and self.clips:
            d = os.path.dirname(self.clips[0]["path"])
            ts = datetime.datetime.now().strftime("%H%M%S")
            self.out_var.set(os.path.join(d, f"joined_{ts}.mp4"))
        self._refresh_tree()

    def _remove_clip(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        del self.clips[idx]
        self._refresh_tree()

    def _move(self, delta):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        j = idx + delta
        if 0 <= j < len(self.clips):
            self.clips[idx], self.clips[j] = self.clips[j], self.clips[idx]
            self._refresh_tree()
            self.tree.selection_set(str(j))

    def _apply_default_overlap(self):
        try:
            v = int(self.default_overlap.get())
        except ValueError:
            messagebox.showerror("Solape", "Pon un numero entero de frames.")
            return
        for i in range(1, len(self.clips)):
            self.clips[i]["overlap"] = v
        self._refresh_tree()

    def _edit_overlap(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row or col != "#7":  # columna "overlap"
            return
        idx = int(row)
        if idx == 0:
            return
        x, y, w, h = self.tree.bbox(row, col)
        var = tk.StringVar(value=str(self.clips[idx]["overlap"]))
        ent = tk.Entry(self.tree, textvariable=var, bg=BG2, fg=FG, insertbackground=FG,
                       relief="flat", justify="center")
        ent.place(x=x, y=y, width=w, height=h)
        ent.focus_set()
        ent.select_range(0, "end")

        def commit(_evt=None):
            try:
                self.clips[idx]["overlap"] = max(0, int(var.get()))
            except ValueError:
                pass
            ent.destroy()
            self._refresh_tree()

        ent.bind("<Return>", commit)
        ent.bind("<FocusOut>", commit)
        ent.bind("<Escape>", lambda e: ent.destroy())

    # ---------------- file pickers ----------------
    def _pick_audio(self):
        p = filedialog.askopenfilename(title="Audio / video origen",
                                        filetypes=[("Video/Audio", "*.mp4;*.mov;*.mkv;*.wav;*.aac;*.m4a"), ("Todos", "*.*")])
        if p:
            self.audio_var.set(p)

    def _pick_out(self):
        p = filedialog.asksaveasfilename(title="Guardar como", defaultextension=".mp4",
                                          filetypes=[("MP4", "*.mp4")])
        if p:
            self.out_var.set(p)

    # ---------------- log / progress ----------------
    def _log(self, msg, tag=None):
        self.log.insert("end", msg + "\n", tag or ())
        self.log.see("end")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._log(*payload)
                elif kind == "progress":
                    self.progress["value"] = payload
                elif kind == "status":
                    self.status_lbl.config(text=payload)
                elif kind == "done":
                    ok, out_path = payload
                    self.join_btn.config(state="normal")
                    if ok:
                        self.progress["value"] = 1000
                        self.open_btn.config(state="normal")
                        self.play_btn.config(state="normal")
                        self._last_out = out_path
                        self._log(f"Listo -> {out_path}", "ok")
                        self.status_lbl.config(text="Terminado.")
                    else:
                        self.status_lbl.config(text="Fallo, revisa el log.")
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    # ---------------- accion principal ----------------
    def _start_join(self):
        audio_src_check = self.audio_var.get().strip()
        if len(self.clips) < 1 or (len(self.clips) < 2 and not audio_src_check):
            messagebox.showwarning("Viggle Chunk Joiner", "Agrega al menos 2 clips (o 1 clip + audio origen para solo agregar musica).")
            return
        out_path = self.out_var.get().strip()
        if not out_path:
            messagebox.showwarning("Viggle Chunk Joiner", "Elige un archivo de salida.")
            return
        try:
            fps_out = float(self.fps_var.get())
        except ValueError:
            messagebox.showerror("Viggle Chunk Joiner", "FPS de salida invalido.")
            return

        self.join_btn.config(state="disabled")
        self.open_btn.config(state="disabled")
        self.play_btn.config(state="disabled")
        self.progress["value"] = 0
        self.log.delete("1.0", "end")

        clips = [c["path"] for c in self.clips]
        durations = [c["duration"] for c in self.clips]
        overlaps = [c["overlap"] for c in self.clips]
        has_audio = all(c["audio"] for c in self.clips)
        audio_src = self.audio_var.get().strip() or None
        try:
            audio_start = max(0.0, float(self.audio_start_var.get().strip() or "0"))
        except ValueError:
            messagebox.showerror("Viggle Chunk Joiner", "Audio inicio invalido.")
            return
        transition = self.trans_var.get()

        threading.Thread(target=self._run_join,
                          args=(clips, durations, overlaps, has_audio, audio_src, audio_start, fps_out, transition, out_path),
                          daemon=True).start()

    def _run_join(self, clips, durations, overlaps, has_audio, audio_src, audio_start, fps_out, transition, out_path):
        try:
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            any_overlap = any(o > 0 for o in overlaps[1:])

            # verificar consistencia de codec/res/fps para saber si el copy directo aplica
            infos = [probe(c) for c in clips]
            consistent = len({(i["codec"], i["w"], i["h"], round(i["fps"], 2)) for i in infos}) == 1

            total_est = sum(durations)
            if not any_overlap:
                if consistent:
                    list_path = os.path.join(os.path.dirname(out_path) or ".", "_join_list.txt")
                    cmd = build_concat_copy(clips, audio_src, audio_start, out_path, list_path)
                    self._q.put(("log", ("Empalme directo (stream copy, sin recodificar)...", None)))
                else:
                    self._q.put(("log", ("Clips con codec/resolucion distintos -> recodificando para el empalme.", "warn")))
                    cmd = build_concat_reencode(clips, has_audio, audio_src, audio_start, fps_out, out_path)
            elif transition == NONE_TRANSITION:
                cmd, total_est, warns = build_trim_concat(clips, durations, overlaps, fps_out,
                                                           has_audio, audio_src, audio_start, out_path)
                for w in warns:
                    self._q.put(("log", (w, "warn")))
                self._q.put(("log", ("Sin transicion: recortando solape y cortando directo...", None)))
            else:
                cmd, total_est, warns = build_crossfade(clips, durations, overlaps, fps_out, transition,
                                                         has_audio, audio_src, audio_start, out_path)
                for w in warns:
                    self._q.put(("log", (w, "warn")))
                self._q.put(("log", (f"Crossfade en {sum(1 for o in overlaps[1:] if o>0)} costura(s)...", None)))

            self._q.put(("log", (" ".join(f'"{a}"' if " " in a else a for a in cmd), None)))
            self._exec(cmd, total_est)
            self._q.put(("done", (True, out_path)))
        except Exception as e:
            self._q.put(("log", (f"ERROR: {e}", "err")))
            self._q.put(("done", (False, None)))

    def _exec(self, cmd, total_est):
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, bufsize=1, creationflags=CREATE_NO_WINDOW)

        def read_stderr():
            for line in self.proc.stderr:
                line = line.rstrip()
                if line:
                    self._q.put(("log", (line, None)))

        def read_stdout():
            out_time = 0.0
            for line in self.proc.stdout:
                line = line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        out_time = int(line.split("=", 1)[1]) / 1_000_000.0
                    except ValueError:
                        continue
                    if total_est > 0:
                        self._q.put(("progress", min(1000, int(out_time / total_est * 1000))))
                        self._q.put(("status", f"{fmt_dur(out_time)} / {fmt_dur(total_est)}"))
                elif line == "progress=end":
                    self._q.put(("progress", 1000))

        t1 = threading.Thread(target=read_stderr, daemon=True)
        t2 = threading.Thread(target=read_stdout, daemon=True)
        t1.start(); t2.start()
        self.proc.wait()
        t1.join(timeout=2); t2.join(timeout=2)
        if self.proc.returncode != 0:
            raise JoinError(f"ffmpeg salio con codigo {self.proc.returncode}")

    def _open_folder(self):
        if getattr(self, "_last_out", None):
            os.startfile(os.path.dirname(self._last_out))

    def _play(self):
        if getattr(self, "_last_out", None):
            os.startfile(self._last_out)


if __name__ == "__main__":
    app = App()
    app.mainloop()
