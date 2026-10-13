# -*- coding: utf-8 -*-
"""CSV → Amazon RDS/DMS: revisión por lotes, SQL y mappings."""
import csv
import hashlib
import json
import os
import queue
import re
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BG, BG2, FG, MUT, ACC = "#1e1f22", "#2b2d31", "#e3e5e8", "#949ba4", "#5865f2"


def select_csv_files():
    """Selector Tk nativo. El .bat limpia variables DSH que interfieren con ventanas del sistema."""
    return filedialog.askopenfilenames(
        parent=None,
        title="Selecciona archivos CSV",
        filetypes=(("Archivos CSV", "*.csv"), ("Todos los archivos", "*.*")),
    )


def safe_identifier(value):
    value = re.sub(r"[^A-Za-z0-9_]", "_", str(value).strip()) or "columna"
    if value[0].isdigit(): value = "c_" + value
    return value[:60]


def unique_headers(headers):
    used, out = set(), []
    for i, raw in enumerate(headers or []):
        base = safe_identifier(raw or f"columna_{i+1}")
        name, n = base, 2
        while name.lower() in used:
            suffix = f"_{n}"; name, n = base[:60-len(suffix)] + suffix, n + 1
        used.add(name.lower()); out.append(name)
    return out


def infer_type(samples, dialect):
    values = [str(v).strip() for v in samples if v is not None and str(v).strip()]
    if not values: return "TEXT" if dialect == "postgres" else "LONGTEXT"
    low = [v.lower() for v in values]
    if all(v in {"true", "false", "yes", "no", "si", "sí"} for v in low):
        return "BOOLEAN" if dialect == "postgres" else "TINYINT(1)"
    if all(re.fullmatch(r"-?(0|[1-9]\d*)", v) for v in values): return "BIGINT"
    if all(re.fullmatch(r"-?(0|[1-9]\d*)\.\d+", v) for v in values):
        return "DOUBLE PRECISION" if dialect == "postgres" else "DOUBLE"
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in values): return "DATE"
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T].+", v) for v in values):
        return "TIMESTAMP" if dialect == "postgres" else "DATETIME"
    max_len = max(map(len, values))
    if max_len <= 255 and not any("\n" in v for v in values): return f"VARCHAR({max_len})"
    return "TEXT" if dialect == "postgres" else "LONGTEXT"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()


def profile_csv(path, sample_limit=1000):
    """Perfila todo el CSV, valida el ancho de filas y retiene solo muestras limitadas."""
    last_error = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=encoding, newline="") as stream:
                sample = stream.read(65536); stream.seek(0)
                try: dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error: dialect = csv.excel
                reader = csv.reader(stream, dialect, strict=True)
                headers = next(reader, [])
                if not headers: raise ValueError("El archivo está vacío o no tiene encabezado.")
                names = unique_headers(headers)
                samples, nulls, max_lengths = [[] for _ in names], [0 for _ in names], [0 for _ in names]
                malformed, total_rows = [], 0
                for row in reader:
                    if not any(str(cell).strip() for cell in row): continue
                    total_rows += 1
                    if len(row) != len(names) and len(malformed) < 100:
                        malformed.append({"line": reader.line_num, "expected": len(names), "actual": len(row)})
                    for idx in range(len(names)):
                        value = row[idx] if idx < len(row) else ""
                        nulls[idx] += not bool(value.strip()); max_lengths[idx] = max(max_lengths[idx], len(value))
                        if len(samples[idx]) < sample_limit: samples[idx].append(value)
                columns, warnings = [], []
                original_names = [str(h).strip() for h in headers]
                for idx, name in enumerate(names):
                    typ = infer_type(samples[idx], "postgres")
                    vals = [str(v).strip() for v in samples[idx] if str(v).strip()]
                    if vals and all(re.fullmatch(r"0\d+", v) for v in vals): warnings.append(f"{name}: tiene ceros iniciales; revisar tipo texto.")
                    if any(re.fullmatch(r"-?\d+,\d+", v) for v in vals): warnings.append(f"{name}: parece usar coma decimal; revisar tipo.")
                    if max_lengths[idx] > 255 and typ.startswith("VARCHAR"): warnings.append(f"{name}: revisar longitud máxima.")
                    columns.append({"name": name, "type": typ, "nulls_sample": nulls[idx], "max_len_sample": max_lengths[idx]})
                if not any(h.lower() in {"id", "uuid", "key", "codigo", "code"} for h in original_names):
                    warnings.append("No se detectó una columna con nombre típico de clave; revisar claves y duplicados.")
                stat = os.stat(path)
                return {"path": os.path.abspath(path), "file": os.path.basename(path),
                        "table": safe_identifier(os.path.splitext(os.path.basename(path))[0]).lower(),
                        "encoding": encoding, "delimiter": dialect.delimiter, "quotechar": dialect.quotechar,
                        "doublequote": dialect.doublequote, "escapechar": dialect.escapechar,
                        "skipinitialspace": dialect.skipinitialspace, "rows_sampled": total_rows,
                        "columns": columns, "malformed_rows": malformed, "malformed_count_sample": len(malformed),
                        "warnings": warnings, "source_size_bytes": stat.st_size, "source_sha256": sha256_file(path)}
        except UnicodeDecodeError as exc: last_error = exc
        except csv.Error as exc: raise ValueError(f"CSV malformado cerca de la línea {locals().get('reader').line_num}: {exc}")
    raise ValueError(f"No se pudo leer {os.path.basename(path)}: {last_error}")


def validate_batch(profiles):
    issues, tables = [], {}
    for profile in profiles:
        label, table = profile.get("file", "CSV"), profile.get("table", "")
        prior = tables.get(table.lower())
        if prior: issues.append({"severity": "ERROR", "file": label, "message": f"Nombre de tabla '{table}' colisiona con {prior}."})
        else: tables[table.lower()] = label
        if not profile.get("columns"): issues.append({"severity": "ERROR", "file": label, "message": "No se detectaron columnas."})
        if profile.get("malformed_rows"):
            first = profile["malformed_rows"][0]
            issues.append({"severity": "ERROR", "file": label, "message": f"{profile.get('malformed_count_sample')} filas con ancho incorrecto; primera línea {first['line']} (esperadas {first['expected']}, encontradas {first['actual']})."})
        if not profile.get("rows_sampled"): issues.append({"severity": "ERROR", "file": label, "message": "El archivo no contiene filas de datos."})
        for warning in profile.get("warnings", []): issues.append({"severity": "AVISO", "file": label, "message": warning})
    return issues


def allowed_sql_type(value):
    value = str(value).strip().upper()
    if value in {"BIGINT", "BOOLEAN", "TINYINT(1)", "DOUBLE PRECISION", "DOUBLE", "DATE", "TIMESTAMP", "DATETIME", "TEXT", "LONGTEXT"}: return value
    match = re.fullmatch(r"VARCHAR\(([1-9]\d{0,3})\)", value)
    if match and int(match.group(1)) <= 4000: return value
    raise ValueError(f"Tipo SQL no permitido: {value!r}")


def quote_ident(identifier, dialect):
    q = "`" if dialect == "mysql" else '"'
    return q + identifier.replace(q, q + q) + q


def generate_ddl(profiles, dialect="postgres", schema="public"):
    statements = []
    for profile in profiles:
        table = quote_ident(profile["table"], dialect)
        qualified = f"{quote_ident(schema, dialect)}.{table}" if dialect == "postgres" and schema else table
        cols = [f"  {quote_ident(c['name'], dialect)} {allowed_sql_type(c.get('type', 'TEXT'))} NULL" for c in profile["columns"]]
        statements.append(f"CREATE TABLE IF NOT EXISTS {qualified} (\n" + ",\n".join(cols) + "\n);")
    return "\n\n".join(statements) + ("\n" if statements else "")


def dms_mapping(profiles, schema="public"):
    return {"rules": [{"rule-type": "selection", "rule-id": str(i), "rule-name": f"include_{p['table']}",
                       "object-locator": {"schema-name": schema, "table-name": p["table"]}, "rule-action": "include"}
                      for i, p in enumerate(profiles, 1)]}


def load_to_postgres(profile, host, port, database, user, password, schema, progress=None, sslmode="verify-full", sslrootcert=None):
    try:
        import psycopg
        from psycopg import sql
    except ImportError: raise RuntimeError("Falta psycopg. Ejecuta: python -m pip install 'psycopg[binary]'")
    if profile.get("malformed_rows"): raise ValueError("Carga bloqueada: hay filas con distinto número de columnas.")
    path = profile["path"]
    if profile.get("source_sha256") and sha256_file(path) != profile["source_sha256"]:
        raise ValueError("El CSV cambió desde el perfilado. Vuelve a agregarlo para validar el archivo actualizado.")
    options = {"host": host, "port": int(port), "dbname": database, "user": user, "password": password, "connect_timeout": 10, "sslmode": sslmode}
    if sslrootcert: options["sslrootcert"] = sslrootcert
    conn = psycopg.connect(**options)
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            cols = profile["columns"]
            defs = [sql.SQL("{} {} NULL").format(sql.Identifier(c["name"]), sql.SQL(allowed_sql_type(c["type"]))) for c in cols]
            cur.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(sql.Identifier(schema), sql.Identifier(profile["table"]), sql.SQL(", ").join(defs)))
            with open(path, "r", encoding=profile["encoding"], newline="") as stream:
                dialect = csv.excel; dialect.delimiter = profile["delimiter"]; dialect.quotechar = profile.get("quotechar", '"')
                dialect.doublequote = profile.get("doublequote", True); dialect.escapechar = profile.get("escapechar"); dialect.skipinitialspace = profile.get("skipinitialspace", False)
                reader = csv.reader(stream, dialect, strict=True); header = next(reader, [])
                if len(header) != len(cols): raise ValueError("El encabezado ya no coincide con el perfilado.")
                insert = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(sql.Identifier(schema), sql.Identifier(profile["table"]),
                    sql.SQL(",").join(map(sql.Identifier, [c["name"] for c in cols])), sql.SQL(",").join(sql.Placeholder() for _ in cols))
                batch, count = [], 0
                for row in reader:
                    if not any(str(x).strip() for x in row): continue
                    if len(row) != len(cols): raise ValueError(f"Fila {reader.line_num}: se esperaban {len(cols)} columnas y se encontraron {len(row)}.")
                    batch.append(tuple(v if v != "" else None for v in row)); count += 1
                    if len(batch) >= 1000: cur.executemany(insert, batch); batch.clear()
                    if progress and count % 1000 == 0: progress(count)
                if batch: cur.executemany(insert, batch)
        conn.commit(); return count
    except Exception:
        conn.rollback(); raise
    finally: conn.close()


class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title("CSV → Amazon RDS / DMS"); self.geometry("1180x790"); self.minsize(980, 680); self.configure(bg=BG)
        self.q = queue.Queue(); self.profiles = []; self._style(); self._build(); self.after(120, self._pump)
        self.report_callback_exception = self._report_tk_error
        self.after(400, self._show_first_run_hint)

    def _show_first_run_hint(self):
        if not self.profiles:
            messagebox.showinfo("Cómo agregar tus CSV", "Pulsa + Agregar CSV. El explorador integrado abre en metadata y muestra ejemplo_clientes.csv; marca el CSV y pulsa Agregar seleccionados. También puedes pegar la ruta de otra carpeta en la parte superior.")

    def _report_tk_error(self, exc, value, tb):
        detail = "".join(traceback.format_exception(exc, value, tb)); self._write_crash_log(detail)
        messagebox.showerror("Error de la aplicación", f"Error. Detalles: {os.path.join(HERE, 'rds_dms_migrator_error.log')}\n\n{value}")

    def _write_crash_log(self, text):
        try:
            with open(os.path.join(HERE, "rds_dms_migrator_error.log"), "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now().isoformat(timespec='seconds')} ---\n{text}\n")
        except OSError: pass

    def _style(self):
        style = ttk.Style(self); style.theme_use("clam")
        style.configure("TFrame", background=BG); style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI", 18, "bold")); style.configure("TButton", font=("Segoe UI", 9), padding=7)
        style.configure("Treeview", background=BG2, fieldbackground=BG2, foreground=FG, rowheight=25); style.configure("Treeview.Heading", background="#383a40", foreground=FG)

    def _build(self):
        top = ttk.Frame(self, padding=(18, 14)); top.pack(fill="x")
        ttk.Label(top, text="Migración de CSV a Amazon RDS", style="Title.TLabel").pack(anchor="w")
        ttk.Label(top, text="Selecciona fuentes, valida el lote y exporta un paquete revisable para la migración.", foreground=MUT).pack(anchor="w", pady=(4, 0))
        ttk.Label(top, text="También puedes arrastrar CSV desde el Explorador y soltarlos sobre esta ventana.", foreground=MUT).pack(anchor="w", pady=(2, 0))
        bar = ttk.Frame(self, padding=(18, 4)); bar.pack(fill="x")
        ttk.Button(bar, text="＋ Agregar CSV", command=self.add_csv).pack(side="left"); ttk.Button(bar, text="Quitar selección", command=self.remove_selected).pack(side="left", padx=7)
        ttk.Button(bar, text="Validar lote", command=self.run_validation).pack(side="left", padx=7)
        ttk.Label(bar, text="Motor:").pack(side="left", padx=(16, 4)); self.dialect = tk.StringVar(value="postgres")
        ttk.Combobox(bar, textvariable=self.dialect, values=("postgres", "mysql"), state="readonly", width=12).pack(side="left")
        ttk.Label(bar, text="Esquema:").pack(side="left", padx=(12, 4)); self.schema = tk.StringVar(value="public"); ttk.Entry(bar, textvariable=self.schema, width=18).pack(side="left")
        self.tree = ttk.Treeview(self, columns=("archivo", "tabla", "filas", "columnas", "estado"), show="headings", height=10)
        for col, title, width in (("archivo", "CSV", 280), ("tabla", "Tabla destino", 170), ("filas", "Filas", 90), ("columnas", "Columnas", 85), ("estado", "Prevalidación", 430)):
            self.tree.heading(col, text=title); self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=18, pady=8)
        self.tabs = ttk.Notebook(self); self.tabs.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        self.sql_text = self._text_tab("SQL DDL"); self.dms_text = self._text_tab("AWS DMS mappings"); self.report_text = self._text_tab("Informe de validación"); self.guide_text = self._text_tab("Flujo recomendado")
        self.guide_text.insert("1.0", "1. Añade CSV manualmente desde la lista de archivos.\n2. Valida lote y revisa los tipos sugeridos.\n3. Exporta SQL/mappings y revisa antes de ejecutar en RDS.\n4. CSV local no es endpoint relacional nativo de DMS.\n\nLa carga directa opcional usa PostgreSQL, TLS verify-full y modo append.")
        conn = ttk.LabelFrame(self, text="Carga opcional a PostgreSQL", padding=7); conn.pack(fill="x", padx=18, pady=4)
        self.conn_vars = {}
        for label, key, default, show in (("Host", "host", "", ""), ("Puerto", "port", "5432", ""), ("Base", "database", "", ""), ("Usuario", "user", "", ""), ("Contraseña", "password", "", "●")):
            ttk.Label(conn, text=label).pack(side="left", padx=(4, 3)); var = tk.StringVar(value=default); self.conn_vars[key] = var
            ttk.Entry(conn, textvariable=var, width=14, show=show).pack(side="left", padx=(0, 4))
        ttk.Label(conn, text="CA TLS:").pack(side="left", padx=(3, 3)); self.ca_path = tk.StringVar(value="")
        ttk.Entry(conn, textvariable=self.ca_path, width=20).pack(side="left"); ttk.Button(conn, text="…", command=self.pick_ca).pack(side="left", padx=3)
        ttk.Button(conn, text="Cargar seleccionado", command=self.load_selected).pack(side="left", padx=5)
        foot = ttk.Frame(self, padding=(18, 7)); foot.pack(fill="x"); self.status = ttk.Label(foot, text="Listo. Los CSV se procesan localmente.", foreground=MUT)
        self.status.pack(side="left", fill="x", expand=True); ttk.Button(foot, text="Exportar paquete", command=self.export).pack(side="right")
        # Alternativa robusta: arrastrar CSV desde Explorer y soltarlos sobre la ventana.
        self.bind("<Drop>", self._on_drop, add="+")
        self.bind("<DragEnter>", lambda event: "copy", add="+")
        self.bind("<DragOver>", lambda event: "copy", add="+")
        self._enable_file_drop()

    def _text_tab(self, name):
        frame = ttk.Frame(self.tabs); self.tabs.add(frame, text=name)
        text = tk.Text(frame, bg=BG2, fg=FG, insertbackground=FG, relief="flat", wrap="none", font=("Consolas", 9))
        sy = ttk.Scrollbar(frame, command=text.yview); sx = ttk.Scrollbar(frame, orient="horizontal", command=text.xview); text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        text.grid(row=0, column=0, sticky="nsew"); sy.grid(row=0, column=1, sticky="ns"); sx.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1); return text

    def _enable_file_drop(self):
        try:
            from tkinterdnd2 import DND_FILES
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            # TkinterDnD es opcional: usa también rutas pegadas en la interfaz.
            self.status.configure(text="Consejo: pega la ruta de una carpeta en el campo de carpeta del selector.")

    def _on_drop(self, event):
        try:
            paths = tuple(self.tk.splitlist(event.data))
            csv_paths = tuple(path for path in paths if os.path.isfile(path) and path.lower().endswith(".csv"))
            if csv_paths:
                self._start_profile_paths(csv_paths)
            else:
                messagebox.showinfo("Archivos", "Suelta uno o varios archivos .csv.")
        except Exception as exc:
            self._write_crash_log(traceback.format_exc())
            messagebox.showerror("Error al abrir archivos", str(exc))
        return "copy"

    def add_csv(self):
        self.open_file_browser()

    def open_file_browser(self):
        """Browser integrado: evita el selector de archivos nativo inestable."""
        win = tk.Toplevel(self)
        win.title("Seleccionar archivos CSV")
        win.geometry("900x650")
        win.minsize(760, 520)
        win.update_idletasks()
        win.transient(self)
        win.grab_set()
        # Empezar directamente donde está el CSV de prueba y la app.
        start_folder = HERE
        current = tk.StringVar(value=start_folder)
        selected = set()
        top = ttk.Frame(win, padding=8); top.pack(fill="x")
        ttk.Label(top, text="Carpeta:").pack(side="left")
        path_entry = ttk.Entry(top, textvariable=current); path_entry.pack(side="left", fill="x", expand=True, padx=6)
        listing = ttk.Treeview(win, columns=("tipo",), show="tree headings", selectmode="extended")
        listing.heading("#0", text="Nombre"); listing.heading("tipo", text="Tipo")
        listing.column("#0", width=520); listing.column("tipo", width=120)
        listing.pack(fill="both", expand=True, padx=8, pady=6)
        status = ttk.Label(win, text="Aquí está ejemplo_clientes.csv. Marca uno o varios CSV y pulsa Agregar seleccionados."); status.pack(fill="x", padx=8)
        def populate(folder=None):
            folder = os.path.abspath(folder or current.get())
            if not os.path.isdir(folder):
                status.configure(text="La ruta no es una carpeta válida."); return
            current.set(folder)
            for iid in listing.get_children(): listing.delete(iid)
            try:
                entries = sorted(os.scandir(folder), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            except OSError as exc:
                status.configure(text=f"No se puede abrir la carpeta: {exc}"); return
            parent = os.path.dirname(folder)
            if parent != folder: listing.insert("", "end", iid="..", text=".. (carpeta superior)", values=("Carpeta",))
            for i, entry in enumerate(entries):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        listing.insert("", "end", iid=f"d{i}", text=entry.name, values=("Carpeta",))
                    elif entry.name.lower().endswith(".csv"):
                        listing.insert("", "end", iid=f"f{i}", text=entry.name, values=("Archivo CSV",))
                except OSError: continue
        def navigate(event=None):
            choices = listing.selection()
            if not choices: return
            item = choices[0]
            if item == "..":
                populate(os.path.dirname(current.get()))
                return
            name = listing.item(item, "text")
            candidate = os.path.join(current.get(), name)
            if os.path.isdir(candidate):
                populate(candidate)
            elif candidate.lower().endswith(".csv"):
                selected.add(candidate)
                status.configure(text=f"Seleccionado: {name}. Pulsa Agregar seleccionados para continuar.")
        def add_selected():
            for item in listing.selection():
                if item.startswith("f"):
                    candidate = os.path.join(current.get(), listing.item(item, "text"))
                    if os.path.isfile(candidate): selected.add(candidate)
            if selected:
                paths = tuple(sorted(selected))
                win.destroy()
                self._start_profile_paths(paths)
            else: status.configure(text="Marca uno o varios archivos CSV primero.")
        listing.bind("<Double-Button-1>", navigate)
        listing.bind("<Return>", navigate)
        path_entry.bind("<Return>", lambda event: populate())
        path_entry.bind("<Control-v>", lambda event: win.after(100, populate))
        path_entry.bind("<Control-V>", lambda event: win.after(100, populate))
        buttons = ttk.Frame(win, padding=8); buttons.pack(fill="x")
        ttk.Button(buttons, text="Ir a carpeta", command=populate).pack(side="left")
        ttk.Button(buttons, text="Abrir carpeta CSV…", command=lambda: self._browse_folder(populate)).pack(side="left", padx=6)
        ttk.Button(buttons, text="Seleccionar todo", command=lambda: [selected.add(os.path.join(current.get(), listing.item(i, "text"))) for i in listing.get_children() if i.startswith("f")]).pack(side="left", padx=6)
        ttk.Button(buttons, text="Cancelar", command=win.destroy).pack(side="right", padx=5)
        ttk.Button(buttons, text="Agregar seleccionados", command=add_selected).pack(side="right")
        populate()

    def _start_profile_paths(self, paths):
        self.status.configure(text=f"Perfilando {len(paths)} archivo(s)…")
        self._profile_thread = threading.Thread(target=self._profile_worker, args=(tuple(paths),), daemon=True)
        self._profile_thread.start()

    def _profile_worker(self, paths):
        try:
            for index, path in enumerate(paths, 1):
                self.q.put(("status", f"Perfilando {index}/{len(paths)}: {os.path.basename(path)}"))
                try: self.q.put(("profile", profile_csv(path)))
                except Exception as exc: self.q.put(("error", f"{os.path.basename(path)}: {exc}"))
            self.q.put(("refresh", None))
        except Exception:
            self._write_crash_log(traceback.format_exc())
            self.q.put(("error", "Falló el perfilado. Consulta rds_dms_migrator_error.log."))

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "profile":
                    if not any(p["path"] == payload["path"] for p in self.profiles): self.profiles.append(payload)
                elif kind == "error": messagebox.showerror("Validación/carga", payload)
                elif kind == "status": self.status.configure(text=payload)
                elif kind == "refresh": self._refresh()
                elif kind == "loaded": self.status.configure(text=payload); messagebox.showinfo("Carga completa", payload)
        except queue.Empty: pass
        except Exception as exc:
            self._write_crash_log(traceback.format_exc())
            messagebox.showerror("Error al actualizar", f"Vista: {exc}\nRevisa rds_dms_migrator_error.log")
        self.after(120, self._pump)

    def _refresh(self):
        for row in self.tree.get_children(): self.tree.delete(row)
        issues = validate_batch(self.profiles); by_file = {}
        for issue in issues: by_file.setdefault(issue["file"], []).append(issue)
        for profile in self.profiles:
            found = by_file.get(profile["file"], []); state = " | ".join(i["message"] for i in found[:2]) if found else "OK — revisar tipos y clave antes de migrar"
            self.tree.insert("", "end", iid=profile["path"], values=(profile["file"], profile["table"], profile["rows_sampled"], len(profile["columns"]), state))
        self._update_artifacts(issues)
        self.status.configure(text=f"{len(self.profiles)} archivo(s) | {sum(i['severity']=='ERROR' for i in issues)} error(es) | {sum(i['severity']=='AVISO' for i in issues)} aviso(s)")

    def _update_artifacts(self, issues=None):
        issues = validate_batch(self.profiles) if issues is None else issues
        for profile in self.profiles:
            for col in profile["columns"]: col["type"] = infer_type_from_profile(col, profile, self.dialect.get())
        self.sql_text.delete("1.0", "end"); self.sql_text.insert("1.0", generate_ddl(self.profiles, self.dialect.get(), self.schema.get().strip() or "public"))
        self.dms_text.delete("1.0", "end"); self.dms_text.insert("1.0", json.dumps(dms_mapping(self.profiles, self.schema.get().strip() or "public"), ensure_ascii=False, indent=2))
        self.report_text.delete("1.0", "end")
        if not issues: self.report_text.insert("end", "Sin errores estructurales detectados. Revisa tipos, claves y reglas de negocio.\n")
        for issue in issues: self.report_text.insert("end", f"[{issue['severity']}] {issue['file']}: {issue['message']}\n")

    def run_validation(self):
        if not self.profiles: messagebox.showinfo("Validación", "Añade primero uno o más CSV."); return
        self._refresh(); errors = [i for i in validate_batch(self.profiles) if i["severity"] == "ERROR"]
        if errors: messagebox.showwarning("Validación del lote", f"Hay {len(errors)} error(es). Revisa la pestaña Informe de validación.")
        else: messagebox.showinfo("Validación del lote", "No se detectaron errores estructurales. Revisa avisos y negocio.")

    def pick_ca(self):
        try:
            path = filedialog.askopenfilename(parent=self, title="Certificado CA de RDS", filetypes=(("Certificados", "*.pem *.crt"), ("Todos", "*.*")))
            if path: self.ca_path.set(path)
        except Exception as exc:
            self._write_crash_log(traceback.format_exc())
            messagebox.showerror("Selector de certificados", str(exc))

    def remove_selected(self):
        selected = set(self.tree.selection()); self.profiles = [p for p in self.profiles if p["path"] not in selected]; self._refresh()

    def export(self):
        if not self.profiles: messagebox.showwarning("Sin CSV", "Agrega al menos un CSV primero."); return
        self._refresh(); issues = validate_batch(self.profiles); errors = [i for i in issues if i["severity"] == "ERROR"]
        if errors: messagebox.showerror("Exportación bloqueada", f"Hay {len(errors)} error(es). Corrígelos antes de exportar."); return
        folder = filedialog.askdirectory(parent=self, title="Carpeta para exportar paquete")
        if not folder: return
        names = ("rds_schema.sql", "dms_table_mappings.json", "migration_manifest.json", "validation_report.json")
        existing = [n for n in names if os.path.exists(os.path.join(folder, n))]
        if existing and not messagebox.askyesno("Confirmar sobrescritura", "Ya existen: " + ", ".join(existing) + ". ¿Sobrescribir?"): return
        schema, engine = self.schema.get().strip() or "public", self.dialect.get()
        manifest = {"created_at": datetime.now().isoformat(timespec="seconds"), "target_engine": engine, "schema": schema,
                    "sources": [{k: v for k, v in p.items() if k != "path"} for p in self.profiles],
                    "warning": "DMS requiere endpoints/tarea configurados en AWS. CSV local no es endpoint relacional nativo."}
        artifacts = {"rds_schema.sql": generate_ddl(self.profiles, engine, schema),
                     "dms_table_mappings.json": json.dumps(dms_mapping(self.profiles, schema), ensure_ascii=False, indent=2),
                     "migration_manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2),
                     "validation_report.json": json.dumps({"issues": issues, "files_checked": len(self.profiles)}, ensure_ascii=False, indent=2)}
        try:
            for name, content in artifacts.items():
                with open(os.path.join(folder, name), "w", encoding="utf-8") as stream: stream.write(content)
        except OSError as exc: messagebox.showerror("Error al exportar", str(exc)); return
        self.status.configure(text=f"Paquete exportado: {folder}"); messagebox.showinfo("Exportación lista", "Paquete exportado con informe de validación.")

    def load_selected(self):
        selected = self.tree.selection()
        if not selected: messagebox.showwarning("Selecciona un CSV", "Selecciona una fuente en la tabla."); return
        profile = next((p for p in self.profiles if p["path"] == selected[0]), None)
        if not profile: return
        errors = [i for i in validate_batch(self.profiles) if i["severity"] == "ERROR" and i["file"] == profile["file"]]
        if errors: messagebox.showerror("Carga bloqueada", "Corrige errores estructurales antes de cargar."); return
        vals = {key: var.get() for key, var in self.conn_vars.items()}
        if not all(vals[k] for k in ("host", "database", "user", "password")):
            messagebox.showwarning("Conexión incompleta", "Completa host, base, usuario y contraseña."); return
        ca = self.ca_path.get().strip()
        if not ca: messagebox.showwarning("TLS requerido", "Selecciona el certificado CA de RDS."); return
        if not messagebox.askyesno("Confirmar carga", f"Se insertarán {profile['rows_sampled']} filas en {self.schema.get()}.{profile['table']}. Append puede duplicar datos. ¿Continuar?"): return
        self.status.configure(text="Conectando y cargando…")
        secret = vals.pop("password"); schema = self.schema.get().strip() or "public"; self.conn_vars["password"].set("")
        def worker(secret_value):
            try:
                count = load_to_postgres(profile, **vals, password=secret_value, schema=schema, sslmode="verify-full", sslrootcert=ca)
                self.q.put(("loaded", f"Carga completada: {profile['file']} → {profile['table']} ({count} filas)."))
            except Exception as exc: self.q.put(("error", str(exc)))
        threading.Thread(target=worker, args=(secret,), daemon=True).start()


def infer_type_from_profile(col, profile, dialect):
    try:
        with open(profile["path"], "r", encoding=profile["encoding"], newline="") as stream:
            d = csv.excel; d.delimiter = profile["delimiter"]; d.quotechar = profile.get("quotechar", '"')
            d.doublequote = profile.get("doublequote", True); d.escapechar = profile.get("escapechar"); d.skipinitialspace = profile.get("skipinitialspace", False)
            reader = csv.reader(stream, d, strict=True); next(reader, None)
            idx = next(i for i, c in enumerate(profile["columns"]) if c["name"] == col["name"])
            values = []
            for row in reader:
                if idx < len(row) and len(values) < 1000: values.append(row[idx])
                if len(values) >= 1000: break
        return allowed_sql_type(infer_type(values, dialect))
    except Exception: return "TEXT" if dialect == "postgres" else "LONGTEXT"


if __name__ == "__main__":
    try: App().mainloop()
    except Exception:
        details = traceback.format_exc()
        try:
            with open(os.path.join(HERE, "rds_dms_migrator_error.log"), "a", encoding="utf-8") as f: f.write(details + "\n")
        except OSError: pass
        try: messagebox.showerror("Error al iniciar", f"La app no pudo abrirse. Consulta rds_dms_migrator_error.log en metadata.\n\n{details[-1800:]}")
        except Exception: pass
