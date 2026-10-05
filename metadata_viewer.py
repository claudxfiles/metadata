# -*- coding: utf-8 -*-
"""
Metadata Viewer - lee prompt / parametros / LoRAs de imagenes generadas por IA.

Soporta:
  - PNG de ComfyUI            (grafo 'prompt' / 'workflow')
  - A1111 / Forge            (string 'parameters' en PNG, o EXIF UserComment en JPG/WEBP)
  - Civitai generator        (grafo con 'urn:air:...' + extraMetadata)
  - Video MP4 / WEBM / MKV   (tag 'comment' del contenedor, como lo escribe
                              VHS_VideoCombine de ComfyUI; requiere PyAV)

Extra:
  - Resuelve los urn:air: a nombres reales de Civitai (boton, requiere internet)
  - Abre / copia / descarga los LoRAs y checkpoints

Uso: doble clic en "Metadata Viewer.bat", o arrastra una imagen sobre el .bat.
"""
import sys, os, io, json, re, queue, threading, webbrowser, hashlib, difflib
import urllib.request, urllib.error, urllib.parse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from PIL import Image, ImageTk

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "_civitai_cache.json")
TOKEN_FILE = os.path.join(HERE, "_civitai_token.txt")


def load_token():
    t = os.environ.get("CIVITAI_TOKEN", "").strip()
    if t:
        return t
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def save_token(t):
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(t.strip())


TOKEN = load_token()


def _headers():
    h = {"User-Agent": "metadata-viewer"}
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    return h


def _with_token(url):
    if not TOKEN:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "token=" + TOKEN

# ---------------------------------------------------------------- AIR urn

AIR_RE = re.compile(r"urn:air:([a-z0-9.\-]*):([a-z]+):civitai:(\d+)@(\d+)", re.I)


def air_parse(s):
    """'urn:air:sdxl:lora:civitai:122359@135867' -> dict o None"""
    m = AIR_RE.search(str(s))
    if not m:
        return None
    base, kind, mid, vid = m.groups()
    return {
        "urn": m.group(0),
        "base": base, "kind": kind.lower(),
        "model_id": int(mid), "version_id": int(vid),
        "page_url": f"https://civitai.com/models/{mid}?modelVersionId={vid}",
        "dl_url": f"https://civitai.com/api/download/models/{vid}",
        "name": None,
    }


def air_label(a):
    n = a.get("name")
    if n:
        return n
    return "%s · civitai %s@%s" % (a.get("kind", "?"), a.get("model_id", "?"),
                                   a.get("version_id", "?"))

# ---------------------------------------------------------------- indice local

MODEL_ROOTS = [r"D:\Program Files\ComfyUI\models",
               r"E:\Program Files\ComfyUI\models"]
MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".sft", ".bin")


def build_local_index(roots=MODEL_ROOTS, progress=None):
    """Recorre models/ de D: y E: una vez. Rapido: NO hashea, solo nombres + sidecars."""
    idx = {"by_name": {}, "by_stem": {}, "by_vid": {}, "files": [], "roots": []}
    for root in roots:
        if not os.path.isdir(root):
            continue
        idx["roots"].append(root)
        for dp, _dn, fns in os.walk(root):
            models_here = [f for f in fns if f.lower().endswith(MODEL_EXT)]
            for f in models_here:
                full = os.path.join(dp, f)
                low = f.lower()
                stem = os.path.splitext(low)[0]
                idx["by_name"].setdefault(low, []).append(full)
                idx["by_stem"].setdefault(stem, []).append(full)
                idx["files"].append(full)
                # sidecar de civitai-helper / stability matrix
                for side in (os.path.splitext(f)[0] + ".civitai.info",
                             os.path.splitext(f)[0] + ".json"):
                    sp = os.path.join(dp, side)
                    if os.path.isfile(sp):
                        try:
                            with open(sp, encoding="utf-8") as fh:
                                sd = json.load(fh)
                            vid = sd.get("modelVersionId") or sd.get("id")
                            if isinstance(vid, int) or (isinstance(vid, str) and vid.isdigit()):
                                idx["by_vid"].setdefault(int(vid), []).append(full)
                        except Exception:
                            pass
            if progress:
                progress(len(idx["files"]))
    return idx


_WORD = re.compile(r"[a-z0-9]+")


def _words(s):
    return set(_WORD.findall((s or "").lower()))


_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _norm_compact(s):
    """nombre -> solo [a-z0-9], sin extension ni separadores.

    'Realistic Snapshot Krea2.safetensors' y 'RealisticSnapshotKrea2.safetensors'
    colapsan al mismo string, asi se detecta que es el mismo archivo.
    """
    s = os.path.splitext(str(s or ""))[0].lower()
    return re.sub(r"[^a-z0-9]+", "", s)


def _words_camel(s):
    """como _words pero ademas parte camelCase: 'RealisticSnapshotKrea2' -> {realistic, snapshot, krea2}."""
    s = os.path.splitext(str(s or ""))[0]
    s = _CAMEL.sub(" ", re.sub(r"[_\-.]+", " ", s))
    return set(_WORD.findall(s.lower()))


# tokens demasiado genericos en esta coleccion como para puntuar por si solos
_MATCH_STOP = {"lora", "loras", "xl", "sdxl", "sd15", "sd3", "pony", "style",
               "styles", "safetensors", "the", "and", "of", "v1", "v2", "v3",
               "v4", "illustrious", "noob", "krea", "krea2", "flux", "flux2",
               "wan", "epoch", "epoc", "bf16", "fp16", "fp8", "final", "ckpt"}


def _fuzzy_local(name, idx):
    """(estado, ruta|None) comparando un nombre suelto de LoRA contra el indice local.

    'ok'    = mismo archivo salvo separadores/mayusculas
    'maybe' = coincidencia fuerte por tokens / subcadena / ratio
    'no'    = nada convincente
    """
    base = os.path.basename(str(name or "").replace("\\", "/"))
    tc = _norm_compact(base)
    if not tc or len(tc) < 4:
        return "no", None

    cache = idx.get("_compact")
    if cache is None or len(cache) != len(idx.get("files", [])):
        cache = idx["_compact"] = [
            (p, _norm_compact(os.path.basename(p)), _words_camel(os.path.basename(p)))
            for p in idx.get("files", [])
        ]

    # 1) igual tras normalizar
    for p, pc, _w in cache:
        if pc and pc == tc:
            return "ok", p

    tw_all = _words_camel(base)
    tw = (tw_all - _MATCH_STOP) or tw_all
    cov_toks = [t for t in tw if len(t) >= 4]

    best, best_p = 0.0, None
    for p, pc, w in cache:
        if not pc:
            continue
        ws = (w - _MATCH_STOP) or w
        inter = tw & ws
        union = tw | ws
        jac = len(inter) / len(union) if union else 0.0
        seq = difflib.SequenceMatcher(None, tc, pc).ratio()
        cov = (sum(1 for t in cov_toks if t in pc) / len(cov_toks)) if cov_toks else 0.0

        score = 0.0
        if jac >= 0.5:
            score = max(score, jac)
        if cov >= 1.0 and len(cov_toks) >= 2:
            score = max(score, 0.85)
        if seq >= 0.72 or (seq >= 0.58 and inter):
            score = max(score, seq)

        if score > best:
            best, best_p = score, p

    if best_p and best >= 0.55:
        return "maybe", best_p
    return "no", None


def match_local(res, info, idx):
    """Devuelve (estado, ruta|None). estado: 'ok' | 'maybe' | 'no'"""
    vid = res["version_id"]
    if vid in idx["by_vid"]:
        return "ok", idx["by_vid"][vid][0]

    files = (info or {}).get("files") or []
    # nombre de archivo exacto que da Civitai
    for fm in files:
        nm = (fm.get("name") or "").lower()
        if nm and nm in idx["by_name"]:
            return "ok", idx["by_name"][nm][0]
        stem = os.path.splitext(nm)[0]
        if stem and stem in idx["by_stem"]:
            return "ok", idx["by_stem"][stem][0]
    # AutoV2 (10 hex) incrustado en el nombre local
    for fm in files:
        av2 = (fm.get("hashes", {}) or {}).get("AutoV2", "").lower()
        if av2 and len(av2) >= 8:
            for p in idx["files"]:
                if av2 in os.path.basename(p).lower():
                    return "ok", p
    # fuzzy por palabras del nombre del modelo
    target = _words(info.get("name", "") if info else "") | _words(air_label(res))
    target -= {"lora", "xl", "sdxl", "pony", "v1", "v2", "v3", "safetensors",
               "the", "and", "of", "style", "illustrious", "noob"}
    best, score = None, 0
    if len(target) >= 2:
        for p in idx["files"]:
            w = _words(os.path.basename(p))
            ov = len(target & w)
            if ov > score:
                best, score = p, ov
    if best and score >= max(2, len(target) // 2):
        return "maybe", best
    # fuzzy tolerante a separadores / camelCase sobre el nombre del modelo/archivo
    _nm = (info or {}).get("name", "") or ""
    for cand in ([f.get("name") for f in files]
                 + [p.strip() for p in _nm.split("—")]
                 + [air_label(res)]):
        if not cand:
            continue
        st, p = _fuzzy_local(cand, idx)
        if st != "no":
            return st, p
    return "no", None


def sha256_file(path, cb=None):
    h = hashlib.sha256()
    sz = os.path.getsize(path) or 1
    got = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            got += len(b)
            if cb:
                cb(got / sz)
    return h.hexdigest()

# ---------------------------------------------------------------- civitai lookup

def _load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(c):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=1, ensure_ascii=False)
    except Exception:
        pass


def resolve_by_hash(h, cache):
    """AutoV2/SHA256 -> mismo dict que resolve_name (cacheado con clave 'h:<hash>')."""
    key = "h:" + h.lower()
    if key in cache:
        return cache[key]
    url = "https://civitai.com/api/v1/model-versions/by-hash/" + h
    try:
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        vid = d.get("id")
        name = "%s — %s" % (d.get("model", {}).get("name", "?"), d.get("name", "?"))
        files = [{"name": f.get("name"), "hashes": f.get("hashes", {}),
                  "primary": f.get("primary")} for f in d.get("files", [])]
        cache[key] = {"name": name, "version_id": vid,
                      "model_id": d.get("modelId"),
                      "type": d.get("model", {}).get("type"), "files": files}
        if vid:
            cache[str(vid)] = cache[key]
    except Exception as ex:
        cache[key] = {"name": None, "error": str(ex)}
    return cache[key]


def resolve_name(version_id, cache, model_id=None):
    key = str(version_id)
    if key in cache:
        return cache[key]
    url = f"https://civitai.com/api/v1/model-versions/{version_id}"
    try:
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        name = "%s — %s" % (d.get("model", {}).get("name", "?"), d.get("name", "?"))
        dl = None
        files = []
        for f in d.get("files", []):
            if f.get("primary"):
                dl = f.get("downloadUrl")
            files.append({"name": f.get("name"), "hashes": f.get("hashes", {}),
                          "sizeKB": f.get("sizeKB"), "primary": f.get("primary")})
        cache[key] = {"name": name, "download": dl,
                      "base": d.get("baseModel"),
                      "type": d.get("model", {}).get("type"),
                      "trained": d.get("trainedWords", []),
                      "files": files}
    except urllib.error.HTTPError as ex:
        if ex.code == 404 and model_id:
            cache[key] = {"name": "(borrado de Civitai) — civarchive.com/models/%s" % model_id,
                          "civarchive": "https://civarchive.com/models/%s" % model_id,
                          "error": "404"}
        else:
            cache[key] = {"name": None, "error": "HTTP %s" % ex.code}
    except Exception as ex:
        cache[key] = {"name": None, "error": str(ex)}
    return cache[key]

# ------------------------------------------------- resolver por nombre de archivo
# LoRAs de ComfyUI que sólo vienen como nombre suelto (sin urn:air:). Se buscan
# en Civitai y en HuggingFace comparando el nombre del archivo.

_QUERY_NOISE = re.compile(
    r"\b(bf16|fp16|fp8|e4m3fn|e5m2|scaled|pruned|comfy|comfyui|rank|\d{2,4}p|"
    r"v\d+(?:\.\d+)*|steps?|\dstep|distill|lora|resized|avg|turbo|native|int8|"
    r"convrot|merged|epoch|epoc|high|low|noise)\b", re.I)


def _clean_query(stem):
    q = _QUERY_NOISE.sub(" ", re.sub(r"[_\-.]+", " ", stem))
    return " ".join(q.split())


def _civitai_search_file(stem):
    q = _clean_query(stem) or stem
    url = ("https://civitai.com/api/v1/models?limit=20&types=LORA&query="
           + urllib.parse.quote(q))
    try:
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except Exception:
        return None
    want = stem.lower()
    wantw = _words(want)
    best = None
    for m in d.get("items", []):
        for v in m.get("modelVersions", []):
            for fm in v.get("files", []):
                nm = os.path.splitext(fm.get("name") or "")[0].lower()
                if not nm:
                    continue
                if nm == want:
                    sc = 1000
                elif want in nm or nm in want:
                    sc = 500 + len(_words(nm) & wantw)
                else:
                    sc = len(_words(nm) & wantw)
                if best is None or sc > best[0]:
                    best = (sc, m, v)
    if not best or best[0] < 3:
        return None
    sc, m, v = best
    dl = next((f.get("downloadUrl") for f in v.get("files", []) if f.get("primary")),
              None) or "https://civitai.com/api/download/models/%s" % v.get("id")
    return {"name": "%s — %s" % (m.get("name", "?"), v.get("name", "?")),
            "version_id": v.get("id"), "model_id": m.get("id"),
            "download": dl, "base": v.get("baseModel"),
            "page_url": "https://civitai.com/models/%s?modelVersionId=%s"
                        % (m.get("id"), v.get("id")),
            "source": "civitai", "match": sc,
            "files": [{"name": f.get("name"), "hashes": f.get("hashes", {}),
                       "primary": f.get("primary")} for f in v.get("files", [])]}


def _hf_search_file(stem):
    q = _clean_query(stem) or stem
    ua = {"User-Agent": "metadata-viewer"}
    try:
        req = urllib.request.Request(
            "https://huggingface.co/api/models?search=%s&limit=10"
            % urllib.parse.quote(q), headers=ua)
        with urllib.request.urlopen(req, timeout=20) as r:
            models = json.load(r)
    except Exception:
        return None
    want = stem.lower()
    for m in models:
        rid = m.get("id") or m.get("modelId")
        if not rid:
            continue
        try:
            req = urllib.request.Request(
                "https://huggingface.co/api/models/" + rid, headers=ua)
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)
        except Exception:
            continue
        for sib in d.get("siblings", []):
            fn = sib.get("rfilename", "")
            if not fn.lower().endswith((".safetensors", ".pt", ".pth", ".gguf")):
                continue
            b = os.path.splitext(os.path.basename(fn))[0].lower()
            if b == want or (len(want) > 12 and (want in b or b in want)):
                return {"name": os.path.basename(fn),
                        "version_id": -(abs(hash(rid + "/" + fn)) % 1_000_000_000),
                        "model_id": 0,
                        "download": "https://huggingface.co/%s/resolve/main/%s"
                                    % (rid, urllib.parse.quote(fn)),
                        "page_url": "https://huggingface.co/" + rid,
                        "source": "hf", "files": []}
    return None


def resolve_by_filename(fname, cache):
    """Sólo-nombre -> dict con 'name' (+ version_id/download/page_url si lo halla)."""
    stem = os.path.splitext(os.path.basename(str(fname).replace("\\", "/")))[0]
    key = "f:" + stem.lower()
    if key in cache:
        return cache[key]
    res = _civitai_search_file(stem) or _hf_search_file(stem)
    cache[key] = res or {"name": None, "error": "no hallado en Civitai ni HF"}
    vid = cache[key].get("version_id")
    if vid:
        cache[str(vid)] = cache[key]
    _save_cache(cache)
    return cache[key]

# ---------------------------------------------------------------- parsers

def _exif_usercomment(im):
    try:
        uc = im.getexif().get_ifd(0x8769).get(0x9286)
    except Exception:
        return None
    if not uc:
        return None
    if isinstance(uc, str):
        return uc
    if isinstance(uc, bytes):
        if uc.startswith(b"UNICODE\x00\x00"):
            body = uc[9:]
            for enc in ("utf-16-le", "utf-16-be"):
                try:
                    return body.decode(enc).rstrip("\x00")
                except Exception:
                    pass
            return body.decode("utf-16-le", "replace").rstrip("\x00")
        if uc.startswith(b"ASCII\x00\x00\x00"):
            return uc[8:].decode("ascii", "replace").rstrip("\x00")
        return uc.decode("utf-8", "replace").rstrip("\x00")
    return str(uc)


def _raw_fields(im):
    fields = {}
    for k, v in im.info.items():
        if k in ("exif", "icc_profile", "dpi", "chromaticity", "gamma", "srgb"):
            continue
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        if isinstance(v, str):
            fields[k] = v
    uc = _exif_usercomment(im)
    if uc and "parameters" not in fields:
        fields["parameters"] = uc
    return fields


A1111_LORA = re.compile(r"<lora:([^:>]+)(?::([\d.]+))?(?::([\d.]+))?>", re.I)


def _parse_a1111(text):
    neg, rest = "", ""
    pos = text
    m = re.search(r"\nNegative prompt:\s*", text)
    if m:
        pos, rest = text[:m.start()], text[m.end():]
    else:
        # sin "Negative prompt:" -> los parametros empiezan en la linea
        # "Steps:" / "Sampler:" / "CFG scale:" / "Seed:" / "Schedule type:"
        pm = re.search(
            r"\n(?=(?:Steps|Sampler|CFG scale|Seed|Schedule type)\s*:)", text)
        if pm:
            pos, rest = text[:pm.start()], text[pm.start():].lstrip("\n")
    params = {}
    if rest:
        lines = rest.split("\n")
        for i, ln in enumerate(lines):
            if re.match(r"\s*(Steps|Sampler|CFG scale|Seed)\s*:", ln):
                neg = "\n".join(lines[:i]).strip()
                params_line = "\n".join(lines[i:])
                break
        else:
            neg, params_line = rest.strip(), ""
        if params_line:
            depth, cur, parts = 0, "", []
            for ch in params_line:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                if ch == "," and depth == 0:
                    parts.append(cur); cur = ""
                else:
                    cur += ch
            if cur.strip():
                parts.append(cur)
            fixed = []
            for p in parts:
                fixed.extend(re.split(r"\s+(?=Version:\s)", p, maxsplit=1))
            for p in fixed:
                if ":" in p:
                    k, _, val = p.partition(":")
                    params[k.strip()] = val.strip().rstrip("�").strip()
    loras = [(n.strip(), w1 or w2 or "1") for n, w1, w2 in A1111_LORA.findall(pos)]
    # hashes AutoV2 por lora, del campo "Hashes: {...}"
    lora_hashes = {}
    hj = params.get("Hashes", "")
    if hj:
        try:
            hd = json.loads(hj[hj.index("{"):hj.rindex("}") + 1])
            for k, v in hd.items():
                if k.lower().startswith(("lora:", "lyco:", "lycoris:")):
                    nm = k.split(":", 1)[1].replace("\\\\", "\\").strip()
                    lora_hashes[nm] = str(v).lower()
                    lora_hashes[os.path.basename(nm.replace("\\", "/")).lower()] = str(v).lower()
        except Exception:
            pass
    return {"positive": A1111_LORA.sub("", pos).strip() or pos.strip(),
            "negative": neg, "params": params, "loras": loras,
            "lora_hashes": lora_hashes,
            "resources": [], "source": "A1111 / Forge (EXIF / parameters)"}


def _clean_embedding_urns(text):
    """'embedding:urn:air:sd1:embedding:civitai:222256@250708, foo' -> 'embedding:222256@250708, foo'"""
    def repl(m):
        a = air_parse(m.group(0))
        return "embedding:%d@%d" % (a["model_id"], a["version_id"]) if a else m.group(0)
    return re.sub(r"embedding:urn:air:[^\s,]+", repl, text)


def _walk_graph(graph):
    nodes = graph
    loras, model_air, model = [], None, None
    sampler = None
    resources = []

    for nid, n in nodes.items():
        if not isinstance(n, dict):
            continue
        ins = n.get("inputs", {})
        ct = n.get("class_type", "")
        for key in ("lora_name", "ckpt_name", "model_name", "unet_name",
                    "upscale_model", "vae_name"):
            val = ins.get(key)
            if isinstance(val, str):
                a = air_parse(val)
                if a:
                    a["strength"] = ins.get("strength_model", ins.get("strength"))
                    resources.append(a)
                    if key == "lora_name":
                        loras.append((a, a.get("strength") or "1"))
                    if key == "ckpt_name" or (key == "model_name" and ct.startswith("Checkpoint")):
                        model_air = a
                elif key == "lora_name":
                    loras.append((val, str(ins.get("strength_model",
                                                  ins.get("strength", "1")))))
                elif key in ("ckpt_name", "unet_name") and not model:
                    model = val
        # rgthree "Power Lora Loader" / "easy loraStack" / "*LoraStack*":
        # los loras van en sub-dicts {"on","lora","strength"} o en claves lora_N_name
        if ("Lora Loader" in ct or "loraStack" in ct or "LoraStack" in ct
                or "LoraLoaderStacked" in ct):
            if ins.get("toggle") is False:
                pass
            else:
                for k, v in ins.items():
                    nm = st = None
                    if isinstance(v, dict) and "lora" in v:          # rgthree
                        if v.get("on") is False:
                            continue
                        nm, st = v.get("lora"), v.get("strength")
                    elif isinstance(v, str) and re.match(r"lora_\d+_name$", k):
                        num = re.search(r"\d+", k).group()
                        nm = v
                        st = ins.get("lora_%s_strength" % num,
                                     ins.get("model_str_%s" % num))
                    if not isinstance(nm, str) or nm.strip().lower() in ("", "none"):
                        continue
                    a = air_parse(nm)
                    if a:
                        a["strength"] = st
                        resources.append(a)
                        loras.append((a, st if st is not None else "1"))
                    else:
                        loras.append((nm, str(st if st is not None else "1")))

        # urn:air: incrustados en cualquier texto (embeddings en el prompt, etc.)
        for v in ins.values():
            if isinstance(v, str) and "urn:air:" in v:
                for mm in re.finditer(r"(?:embedding:)?urn:air:[^\s,\"]+", v):
                    a = air_parse(mm.group(0))
                    if a:
                        resources.append(a)

        if ("KSampler" in ct or "SamplerCustom" in ct
                or ("sampler_name" in ins and {"seed", "steps", "cfg"} & set(ins))):
            sampler = n

    def resolve_text(ref):
        seen = set()
        while isinstance(ref, list) and ref and ref[0] not in seen:
            seen.add(ref[0])
            nn = nodes.get(str(ref[0])) or nodes.get(ref[0])
            if not isinstance(nn, dict):
                return ""
            ii = nn.get("inputs", {})
            t = (ii.get("text") or ii.get("text_g") or ii.get("prompt")
                 or ii.get("string"))
            if isinstance(t, str):
                return t
            if isinstance(t, list):
                ref = t
                continue
            for v in ii.values():
                if isinstance(v, str) and len(v) > 8:
                    return v
            return ""
        return ""

    pos = neg = ""
    params = {}
    if sampler:
        si = sampler.get("inputs", {})
        pos = resolve_text(si.get("positive"))
        neg = resolve_text(si.get("negative"))
        for k in ("seed", "noise_seed", "steps", "cfg", "sampler_name",
                  "scheduler", "denoise"):
            if k in si and not isinstance(si[k], list):
                params[k] = si[k]

    # extraMetadata de Civitai (JSON escapado) -> prompt limpio
    src = "ComfyUI (grafo prompt)"
    em = graph.get("extraMetadata")
    if isinstance(em, str):
        try:
            em = json.loads(em)
        except Exception:
            em = None
    if isinstance(em, dict):
        src = "Civitai generator"
        pos = em.get("prompt", pos)
        neg = em.get("negativePrompt", neg)
        for a, b in (("steps", "steps"), ("cfgScale", "cfg"),
                     ("sampler", "sampler_name")):
            if em.get(a) is not None:
                params[b] = em[a]
        if em.get("workflowId"):
            params["workflow"] = em["workflowId"]

    for nid, n in nodes.items():
        if isinstance(n, dict):
            ii = n.get("inputs", {})
            if ("width" in ii and "height" in ii
                    and not isinstance(ii["width"], list) and ii["width"]):
                params.setdefault("width", ii["width"])
                params.setdefault("height", ii["height"])

    if model_air:
        params["model"] = air_label(model_air)
    elif model:
        params["model"] = model

    # embeddings citados en el texto
    for t in (pos, neg):
        for m in re.finditer(r"embedding:urn:air:[^\s,]+", t or ""):
            a = air_parse(m.group(0))
            if a and a["urn"] not in [r["urn"] for r in resources]:
                resources.append(a)

    pos = _clean_embedding_urns(pos or "")
    neg = _clean_embedding_urns(neg or "")

    # dedup resources por version_id
    seen, uniq = set(), []
    for r in resources:
        if r["version_id"] in seen:
            continue
        seen.add(r["version_id"])
        uniq.append(r)

    # dedup loras (mismo nombre+peso, de nodos repetidos HIGH/LOW en el grafo)
    lseen, luniq = set(), []
    for lo in loras:
        key = ((lo[0] if isinstance(lo[0], str) else lo[0].get("urn")), str(lo[1]))
        if key in lseen:
            continue
        lseen.add(key)
        luniq.append(lo)

    return {"positive": pos, "negative": neg, "params": params,
            "loras": luniq, "resources": uniq, "source": src}


def _salvage_civitai(t):
    """Grafo Civitai roto/cortado (EXIF): rescata prompt + recursos por regex."""
    if "urn:air:" not in t and "extraMetadata" not in t:
        return None
    pos = neg = ""
    params = {}
    m = re.search(r'"extraMetadata"\s*:\s*"', t)
    if m:
        body = t[m.end():]
        # es un JSON escapado dentro de un string; recorta al ultimo '}' plausible
        depth, out, esc, instr = 0, [], False, False
        for ch in body:
            out.append(ch)
            if esc:
                esc = False; continue
            if ch == "\\":
                esc = True; continue
            if ch == '"' and depth == 0:
                # fin del valor string de extraMetadata
                out.pop()
                break
        raw = "".join(out)
        try:
            em = json.loads(raw.encode().decode("unicode_escape"))
        except Exception:
            try:
                em = json.loads(raw.replace('\\u0022', '"').replace('\\"', '"'))
            except Exception:
                em = None
        if isinstance(em, dict):
            pos = em.get("prompt", "") or ""
            neg = em.get("negativePrompt", "") or ""
            for a, b in (("steps", "steps"), ("cfgScale", "cfg"),
                         ("sampler", "sampler_name")):
                if em.get(a) is not None:
                    params[b] = em[a]
            if em.get("workflowId"):
                params["workflow"] = em["workflowId"]
            strengths = {}
            for r in em.get("resources", []):
                if isinstance(r, dict) and "modelVersionId" in r:
                    strengths[int(r["modelVersionId"])] = r.get("strength")
    else:
        strengths = {}

    resources, seen = [], set()
    for mm in re.finditer(r"(?:embedding:)?urn:air:[^\s,\"\\]+", t):
        a = air_parse(mm.group(0))
        if a and a["version_id"] not in seen:
            seen.add(a["version_id"])
            a["strength"] = strengths.get(a["version_id"])
            resources.append(a)
    loras = [(a, a.get("strength") if a.get("strength") is not None else "1")
             for a in resources if a["kind"] in ("lora", "lycoris", "locon")]
    ckpt = next((a for a in resources if a["kind"] == "checkpoint"), None)
    if ckpt:
        params["model"] = air_label(ckpt)
    return {"positive": pos, "negative": neg, "params": params,
            "loras": loras, "resources": resources,
            "source": "Civitai generator (rescatado de EXIF)"}


def _try_graph(s):
    """s puede ser un grafo ComfyUI/Civitai en JSON -> _walk_graph, o None."""
    if not isinstance(s, str):
        return None
    t = s.strip().rstrip("�\x00 ")
    if not t.startswith("{"):
        return None
    if '"class_type"' not in t and "urn:air:" not in t:
        return None
    # 1) intento directo, tolerando basura/cortes al final
    for cand in (t, t + "}", t + "}}", t[:t.rfind("}") + 1] if "}" in t else t):
        try:
            g = json.loads(cand)
            if isinstance(g, dict) and any(
                    isinstance(v, dict) and "class_type" in v for v in g.values()):
                return _walk_graph(g)
        except Exception:
            pass
    # 2) rescate por regex (EXIF cortado)
    return _salvage_civitai(t)


VIDEO_EXT = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi")


def _video_poster(container):
    """primer frame decodificado -> PIL.Image, o None."""
    try:
        for frame in container.decode(video=0):
            return frame.to_image()
    except Exception:
        return None
    return None


def _video_graph_fields(tags):
    """De las tags del contenedor saca prompt / workflow / parameters,
    como los escribe VHS_VideoCombine (ComfyUI) u otras herramientas."""
    fields = {}
    low = {k.lower(): v for k, v in tags.items()}          # webm/mkv: MAYUS
    comment = low.get("comment") or low.get("description") or ""
    if isinstance(comment, str) and comment.strip().startswith("{"):
        try:
            j = json.loads(comment)
        except Exception:
            j = None
        if isinstance(j, dict) and ("prompt" in j or "workflow" in j):
            for key in ("prompt", "workflow"):
                v = j.get(key)
                if v is None:
                    continue
                fields[key] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            return fields
    for k in ("prompt", "workflow", "parameters"):
        if isinstance(low.get(k), str):
            fields[k] = low[k]
    if not fields and isinstance(comment, str) and comment.strip():
        fields["parameters"] = comment            # grafo suelto o texto A1111
    return fields


def extract_video(path):
    try:
        import av
    except Exception:
        raise RuntimeError("Falta PyAV para leer videos.  Instala con:  pip install av")
    container = av.open(path)
    try:
        tags = dict(container.metadata)
        vs = next((s for s in container.streams if s.type == "video"), None)
        w = h = 0
        vtech = {}
        if vs is not None:
            cc = vs.codec_context
            w, h = cc.width or 0, cc.height or 0
            vtech["codec"] = cc.name
            try:
                if vs.average_rate:
                    vtech["fps"] = round(float(vs.average_rate), 3)
            except Exception:
                pass
            if vs.frames:
                vtech["frames"] = vs.frames
        if container.duration:
            vtech["duration"] = "%.2fs" % (container.duration / av.time_base)
        poster = _video_poster(container)
        fields = _video_graph_fields(tags)
        raw = {k: v for k, v in tags.items()}
    finally:
        container.close()

    if poster is not None and (not w or not h):
        w, h = poster.size

    out = {"file": path,
           "format": (os.path.splitext(path)[1].lstrip(".").upper() or "VIDEO"),
           "size": (w, h), "raw": raw, "image": poster,
           "workflow": fields.get("workflow")}

    data = None
    if "prompt" in fields:
        try:
            data = _walk_graph(json.loads(fields["prompt"]))
        except Exception:
            data = None
    if data is None and "parameters" in fields:
        data = _try_graph(fields["parameters"])
    if data is None and "parameters" in fields:
        data = _parse_a1111(fields["parameters"])
    if data is None:
        data = {"positive": "", "negative": "", "params": {}, "loras": [],
                "resources": [], "source": "(video sin metadata reconocible)"}

    merged = dict(vtech)                       # info tecnica al frente de PARAMETROS
    merged.update(data.get("params", {}))
    data["params"] = merged
    if not data["source"].startswith("("):
        data["source"] += "  ·  video"
    out.update(data)
    return out


def extract(path):
    if path.lower().endswith(VIDEO_EXT):
        return extract_video(path)
    im = Image.open(path)
    fields = _raw_fields(im)
    out = {"file": path, "format": im.format, "size": im.size,
           "raw": fields, "image": im, "workflow": fields.get("workflow")}
    data = None
    if "prompt" in fields:
        try:
            data = _walk_graph(json.loads(fields["prompt"]))
        except Exception:
            data = None
    # Civitai mete el grafo entero en EXIF UserComment / 'parameters'
    if data is None and "parameters" in fields:
        data = _try_graph(fields["parameters"])
    if data is None and "parameters" in fields:
        data = _parse_a1111(fields["parameters"])
    if data is None:
        data = {"positive": "", "negative": "", "params": {}, "loras": [],
                "resources": [], "source": "(sin metadata reconocible)"}
    out.update(data)
    return out

# ---------------------------------------------------------------- GUI

BG = "#1e1f22"; BG2 = "#2b2d31"; FG = "#e3e5e8"; ACC = "#5865f2"; MUT = "#949ba4"
GOOD = "#3ba55d"


class App(tk.Tk):
    def __init__(self, initial=None):
        super().__init__()
        self.title("Metadata Viewer")
        self.geometry("1040x720")
        self.configure(bg=BG)
        self.minsize(820, 520)
        self._thumb = None
        self.cur = None
        self.cache = _load_cache()
        self._q = queue.Queue()
        self._workers = 0
        self._cancel = False
        self._local_idx = None

        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=BG2)
        st.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        st.configure("Muted.TLabel", background=BG2, foreground=MUT, font=("Segoe UI", 9))
        st.configure("TButton", font=("Segoe UI", 9), padding=6)
        st.configure("Accent.TButton", font=("Segoe UI Semibold", 9))
        st.map("TButton", background=[("active", ACC)])

        top = ttk.Frame(self, padding=(12, 10))
        top.pack(fill="x")
        ttk.Button(top, text="  Abrir…  ", style="Accent.TButton",
                   command=self.pick).pack(side="left")
        self.path_var = tk.StringVar()
        e = tk.Entry(top, textvariable=self.path_var, bg=BG2, fg=FG,
                     insertbackground=FG, relief="flat", font=("Segoe UI", 9))
        e.pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        e.bind("<Return>", lambda _=None: self.load(self.path_var.get().strip('" ')))
        ttk.Button(top, text="Pegar", command=self.paste).pack(side="left")

        body = ttk.Frame(self, padding=(12, 0, 12, 12))
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, style="Card.TFrame", padding=10)
        left.pack(side="left", fill="y")
        self.thumb_lbl = ttk.Label(left, style="Muted.TLabel",
                                   text="\n\n  arrastra una imagen o video\n  sobre el .bat\n\n"
                                        "  o Abrir…\n\n")
        self.thumb_lbl.pack()
        self.info_lbl = ttk.Label(left, style="Muted.TLabel", text="", justify="left")
        self.info_lbl.pack(anchor="w", pady=(8, 0))

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        self.src_lbl = ttk.Label(right, text="", foreground=ACC,
                                 font=("Segoe UI Semibold", 10))
        self.src_lbl.pack(anchor="w", pady=(0, 6))

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)

        # -- tab: prompt
        f1 = ttk.Frame(nb)
        nb.add(f1, text="Prompt / Parametros")
        self.txt = tk.Text(f1, bg=BG2, fg=FG, relief="flat", wrap="word",
                           insertbackground=FG, font=("Consolas", 10), padx=12, pady=12)
        self.txt.pack(fill="both", expand=True)
        self.txt.tag_configure("h", foreground=ACC,
                               font=("Segoe UI Semibold", 11), spacing1=10, spacing3=4)
        self.txt.tag_configure("mut", foreground=MUT)
        self.txt.configure(state="disabled")

        # -- tab: recursos
        f2 = ttk.Frame(nb)
        nb.add(f2, text="Recursos / LoRAs")
        cols = ("kind", "res", "str", "local", "name")
        self.tree = ttk.Treeview(f2, columns=cols, show="headings", height=10)
        for c, w, t in (("kind", 74, "tipo"), ("res", 120, "model@ver"),
                        ("str", 48, "peso"), ("local", 62, "local"),
                        ("name", 430, "nombre (resuelto)")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("ok", foreground=GOOD)
        self.tree.tag_configure("maybe", foreground="#d9a441")
        self.tree.tag_configure("no", foreground="#e06c6c")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree.bind("<Double-1>", self._dbl)

        rb = ttk.Frame(f2)
        rb.pack(fill="x", padx=4, pady=(0, 6))
        ttk.Button(rb, text="Resolver nombres (Civitai)",
                   command=self.resolve_all).pack(side="left")
        ttk.Button(rb, text="¿Lo tengo? (D:+E:)",
                   command=self.check_local).pack(side="left", padx=6)
        ttk.Button(rb, text="Verificar hash",
                   command=self.verify_hash).pack(side="left")
        ttk.Button(rb, text="Abrir pagina", command=self._open_selected).pack(side="left", padx=6)
        ttk.Button(rb, text="Copiar links descarga",
                   command=self.copy_dls).pack(side="left")
        ttk.Button(rb, text="Descargar seleccion…",
                   command=self.download_sel).pack(side="left", padx=6)
        self.b_cancel = ttk.Button(rb, text="Cancelar", command=self._cancel_dl)
        self.b_cancel.pack(side="left")
        self.b_cancel.state(["disabled"])
        ttk.Button(rb, text="Token…", command=self.set_token).pack(side="right")

        prow = ttk.Frame(f2)
        prow.pack(fill="x", padx=6, pady=(2, 0))
        self.pb = ttk.Progressbar(prow, mode="determinate", maximum=1000)
        self.pb.pack(fill="x")
        self.status = ttk.Label(f2, text="", style="Muted.TLabel")
        self.status.pack(anchor="w", padx=6)

        # -- bottom buttons (globales)
        btns = ttk.Frame(right, padding=(0, 8, 0, 0))
        btns.pack(fill="x")
        self.b_pos = ttk.Button(btns, text="Copiar prompt", command=lambda: self.copy("positive"))
        self.b_pos.pack(side="left")
        self.b_neg = ttk.Button(btns, text="Copiar negativo", command=lambda: self.copy("negative"))
        self.b_neg.pack(side="left", padx=6)
        self.b_all = ttk.Button(btns, text="Copiar TODO (raw)", command=lambda: self.copy("raw"))
        self.b_all.pack(side="left")
        self.b_wf = ttk.Button(btns, text="Guardar workflow .json", command=self.save_wf)
        self.b_wf.pack(side="left", padx=6)

        self.bind("<Control-v>", lambda _=None: self.paste())
        if initial:
            self.after(80, lambda: self.load(initial))

    # ---- load / render
    def pick(self):
        p = filedialog.askopenfilename(
            title="Elegir imagen o video",
            filetypes=[("Imagenes / Videos",
                        "*.png *.jpg *.jpeg *.webp *.avif "
                        "*.mp4 *.m4v *.mov *.webm *.mkv *.avi"),
                       ("Todo", "*.*")])
        if p:
            self.load(p)

    def paste(self):
        try:
            s = self.clipboard_get().strip().strip('"')
        except Exception:
            return
        if os.path.isfile(s):
            self.load(s)

    def load(self, path):
        if not path or not os.path.isfile(path):
            messagebox.showwarning("Metadata Viewer", "No existe:\n%s" % path)
            return
        try:
            d = extract(path)
        except Exception as ex:
            messagebox.showerror("Metadata Viewer", "No se pudo leer:\n%s" % ex)
            return
        self.cur = d
        self.path_var.set(path)

        try:
            im = d["image"].copy()
            im.thumbnail((300, 470))
            self._thumb = ImageTk.PhotoImage(im)
            self.thumb_lbl.configure(image=self._thumb, text="")
        except Exception:
            self.thumb_lbl.configure(image="", text="(sin preview)")
        self.info_lbl.configure(
            text="%s\n%dx%d  %s" % (os.path.basename(path),
                                    d["size"][0], d["size"][1], d["format"]))
        self.src_lbl.configure(text=d["source"])

        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")

        def sec(t, c, muted=False):
            self.txt.insert("end", t + "\n", "h")
            self.txt.insert("end", (c or "(vacio)") + "\n", ("mut",) if muted else ())

        sec("PROMPT POSITIVO", d["positive"])
        sec("PROMPT NEGATIVO", d["negative"])
        if d["params"]:
            sec("PARAMETROS", "\n".join("%-13s %s" % (k + ":", v)
                                        for k, v in d["params"].items()))
        extra = [k for k in d["raw"] if k not in ("parameters", "prompt", "workflow")]
        if extra:
            sec("OTROS CAMPOS", ", ".join(extra), muted=True)
        if not d["positive"] and not d["params"]:
            self.txt.insert("end", "\nSin metadata reconocible (borrada al re-guardar / "
                                   "screenshot / subir a red social).\n", "mut")
        self.txt.configure(state="disabled")

        self.b_pos.state(["!disabled"] if d["positive"] else ["disabled"])
        self.b_neg.state(["!disabled"] if d["negative"] else ["disabled"])
        self.b_wf.state(["!disabled"] if d["workflow"] else ["disabled"])

        # tabla recursos
        self.tree.delete(*self.tree.get_children())
        for r in d["resources"]:
            if not isinstance(r, dict):
                continue
            s = r.get("strength")
            self.tree.insert("", "end", iid=str(r["version_id"]),
                             values=(r["kind"],
                                     "%d@%d" % (r["model_id"], r["version_id"]),
                                     "" if s in (None, "") else s, "",
                                     r.get("name") or "(doble clic = abrir · Resolver nombres)"))
        # loras que NO son air (nombres locales A1111)
        self._a1111_loras = []
        self._name_lora_str = {}
        for i, lo in enumerate(d["loras"]):
            if isinstance(lo, tuple) and isinstance(lo[0], str):
                iid = "a1111:%d" % i
                self._a1111_loras.append((iid, lo[0]))
                self._name_lora_str[iid] = lo[1]
                self.tree.insert("", "end", iid=iid,
                                 values=("lora", "(nombre)", lo[1], "", lo[0]))
        n = len([r for r in d["resources"] if isinstance(r, dict)])
        self.status.configure(text="%d recurso(s) Civitai" % n if n else
                              "sin recursos urn:air: (metadata local o A1111)")

    # ---- resources
    def _sel_resources(self):
        out = []
        for iid in self.tree.selection() or self.tree.get_children():
            for r in self.cur["resources"]:
                if isinstance(r, dict) and str(r["version_id"]) == iid:
                    out.append(r)
        return out

    def _open_selected(self, _=None):
        for r in self._sel_resources():
            webbrowser.open(r["page_url"])

    def _dbl(self, _=None):
        paths = getattr(self, "_a1111_paths", {})
        for iid in self.tree.selection():
            if iid.startswith("a1111:"):
                p = paths.get(iid)
                if p and os.path.isfile(p):
                    try:
                        os.startfile(os.path.dirname(p))
                    except Exception:
                        pass
                continue
        for r in self._sel_resources():
            p = r.get("local_path")
            if p and os.path.isfile(p):
                try:
                    os.startfile(os.path.dirname(p))  # abre la carpeta (Windows)
                except Exception:
                    webbrowser.open(r["page_url"])
            else:
                webbrowser.open(r["page_url"])

    def copy_dls(self):
        rs = self._sel_resources()
        if not rs:
            return
        txt = "\n".join(r["dl_url"] for r in rs)
        self.clipboard_clear(); self.clipboard_append(txt)
        self.status.configure(text="copiado %d link(s) de descarga" % len(rs))

    # -- worker plumbing: hilos empujan mensajes a una cola, el hilo UI la vacia
    def _pump(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "name":
                    vid, name = payload
                    try:
                        self.tree.set(vid, "name", name)
                    except Exception:
                        pass
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "progress":
                    self.pb.configure(value=max(0, min(1000, int(payload * 1000))))
                elif kind == "dlend":
                    self.b_cancel.state(["disabled"])
                elif kind == "row":
                    vid, col, val, tag = payload
                    try:
                        self.tree.set(vid, col, val)
                        if tag:
                            self.tree.item(vid, tags=(tag,))
                    except Exception:
                        pass
                elif kind == "promote":
                    iid, res = payload
                    try:
                        self.tree.delete(iid)
                    except Exception:
                        pass
                    if isinstance(self.cur, dict):
                        self.cur.setdefault("resources", []).append(res)
                    vid = str(res["version_id"])
                    s = res.get("strength")
                    ref = ("%d@%d" % (res["model_id"], res["version_id"])
                           if res.get("model_id") else (res.get("source") or "hf") + " ✓")
                    vals = (res["kind"], ref, "" if s in (None, "") else s, "",
                            res.get("name") or "")
                    try:
                        if self.tree.exists(vid):
                            self.tree.item(vid, values=vals, tags=("maybe",))
                        else:
                            self.tree.insert("", "end", iid=vid, values=vals,
                                             tags=("maybe",))
                    except Exception:
                        pass
                elif kind == "open":
                    webbrowser.open(payload)
        except queue.Empty:
            pass
        if self._workers > 0:
            self.after(120, self._pump)

    def _start(self, fn):
        self._workers += 1

        def wrap():
            try:
                fn()
            finally:
                self._workers -= 1
        threading.Thread(target=wrap, daemon=True).start()
        self.after(120, self._pump)

    def resolve_all(self):
        rs = [r for r in (self.cur or {}).get("resources", []) if isinstance(r, dict)]
        names = [(iid, nm) for (iid, nm) in getattr(self, "_a1111_loras", [])
                 if self.tree.exists(iid)]
        if not rs and not names:
            self.status.configure(text="no hay recursos que resolver")
            return
        self.status.configure(text="resolviendo en civitai.com / huggingface …")

        def work():
            ok = 0
            for i, r in enumerate(rs, 1):
                info = resolve_name(r["version_id"], self.cache, r["model_id"])
                if info.get("name"):
                    if not info.get("error"):
                        ok += 1
                    r["name"] = info["name"]
                    if info.get("download"):
                        r["dl_url"] = info["download"]
                    if info.get("civarchive"):
                        r["page_url"] = info["civarchive"]
                    self._q.put(("name", (str(r["version_id"]), info["name"])))
                self._q.put(("status", "resolviendo urn:air… %d/%d" % (i, len(rs))))
            # LoRAs que sólo vienen como nombre de archivo: buscar en Civitai/HF
            for j, (iid, nm) in enumerate(names, 1):
                self._q.put(("status", "buscando por nombre… %d/%d  %s"
                             % (j, len(names), os.path.basename(nm))))
                info = resolve_by_filename(nm, self.cache)
                if not info.get("name"):
                    self._q.put(("row", (iid, "name",
                                         "(no hallado en Civitai/HF)", "no")))
                    continue
                ok += 1
                res = {"kind": "lora",
                       "model_id": info.get("model_id") or 0,
                       "version_id": info["version_id"],
                       "page_url": info["page_url"],
                       "dl_url": info.get("download")
                       or "https://civitai.com/api/download/models/%s" % info["version_id"],
                       "name": info["name"],
                       "strength": getattr(self, "_name_lora_str", {}).get(iid),
                       "base": info.get("base"),
                       "source": info.get("source", "")}
                self._q.put(("promote", (iid, res)))
            _save_cache(self.cache)
            self._q.put(("status", "listo: %d resuelto(s)  ·  cache _civitai_cache.json" % ok))

        self._start(work)

    def set_token(self):
        global TOKEN
        cur = TOKEN
        v = simpledialog.askstring(
            "Token Civitai",
            "Pega tu API token de civitai.com (Settings → API Keys).\n"
            "Se guarda en _civitai_token.txt — no compartas ese archivo.",
            initialvalue=cur, show="*", parent=self)
        if v is None:
            return
        TOKEN = v.strip()
        save_token(TOKEN)
        self.status.configure(text="token %s" % ("guardado ✓" if TOKEN else "borrado"))

    def _cancel_dl(self):
        self._cancel = True
        self.status.configure(text="cancelando…")

    def _match_name(self, name, idx):
        """match de un lora por su nombre (A1111 'Styles\\Kenva' o ComfyUI 'krea2\\foo.safetensors')."""
        base = os.path.basename(name.replace("\\", "/")).lower()
        if base in idx["by_name"]:
            return "ok", idx["by_name"][base][0]
        stem = os.path.splitext(base)[0] if base.endswith(MODEL_EXT) else base
        for ext in MODEL_EXT:
            if stem + ext in idx["by_name"]:
                return "ok", idx["by_name"][stem + ext][0]
        if stem in idx["by_stem"]:
            return "ok", idx["by_stem"][stem][0]
        # fuzzy tolerante a separadores / camelCase / sufijos de version
        return _fuzzy_local(base, idx)

    def check_local(self):
        rs = [r for r in (self.cur or {}).get("resources", []) if isinstance(r, dict)]
        a1 = getattr(self, "_a1111_loras", [])
        if not rs and not a1:
            self.status.configure(text="no hay recursos que buscar")
            return
        self.status.configure(text="indexando D:\\ y E:\\ …")

        def work():
            if self._local_idx is None:
                self._local_idx = build_local_index(
                    progress=lambda n: self._q.put(("status", "indexando… %d modelos" % n)))
            idx = self._local_idx
            self._q.put(("status", "%d modelos indexados en %s — comparando…"
                         % (len(idx["files"]), " + ".join(idx["roots"]) or "(nada)")))
            found = miss = maybe = 0
            for r in rs:
                info = self.cache.get(str(r["version_id"]))
                if not info:                       # resolver primero pa tener files/hashes
                    info = resolve_name(r["version_id"], self.cache, r["model_id"])
                    if info.get("name"):
                        self._q.put(("name", (str(r["version_id"]), info["name"])))
                st, p = match_local(r, info, idx)
                r["local_path"] = p
                if st == "ok":
                    found += 1
                    tag, cell = "ok", "✓ SÍ"
                elif st == "maybe":
                    maybe += 1
                    tag, cell = "maybe", "~ quizá"
                else:
                    miss += 1
                    tag, cell = "no", "✗ falta"
                self._q.put(("row", (str(r["version_id"]), "local", cell, tag)))
                if p:
                    self._q.put(("status", "%s  ->  %s" % (cell, p)))
            lh = self.cur.get("lora_hashes", {}) if isinstance(self.cur, dict) else {}
            for iid, nm in a1:
                st, p = "no", None
                h = lh.get(nm) or lh.get(os.path.basename(nm.replace("\\", "/")).lower())
                if h:
                    info = resolve_by_hash(h, self.cache)
                    if info.get("name"):
                        self._q.put(("row", (iid, "name", info["name"], "")))
                        vid = info.get("version_id")
                        fake = {"version_id": vid or -1, "model_id": info.get("model_id") or 0}
                        st, p = match_local(fake, info, idx)
                if st == "no":
                    st, p = self._match_name(nm, idx)
                self._a1111_paths = getattr(self, "_a1111_paths", {})
                self._a1111_paths[iid] = p
                cell = {"ok": "✓ SÍ", "maybe": "~ quizá", "no": "✗ falta"}[st]
                if st == "ok":
                    found += 1
                elif st == "maybe":
                    maybe += 1
                else:
                    miss += 1
                self._q.put(("row", (iid, "local", cell, st)))
                if p:
                    self._q.put(("status", "%s  ->  %s" % (cell, p)))
            _save_cache(self.cache)
            self._q.put(("status",
                         "listo: %d tenemos · %d quizá · %d faltan  (doble clic fila = abrir)"
                         % (found, maybe, miss)))

        self._start(work)

    def verify_hash(self):
        rs = [r for r in self._sel_resources() if r.get("local_path")]
        if not rs:
            messagebox.showinfo("Verificar hash",
                                "Corre '¿Lo tengo?' primero y selecciona una fila con archivo local.")
            return
        self.status.configure(text="hasheando %d archivo(s)…" % len(rs))

        def work():
            for r in rs:
                info = self.cache.get(str(r["version_id"]), {})
                want = ""
                for fm in info.get("files", []):
                    want = (fm.get("hashes", {}) or {}).get("SHA256", "") or want
                want = want.lower()
                p = r["local_path"]
                self._q.put(("progress", 0))
                got = sha256_file(p, cb=lambda fr: self._q.put(("progress", fr)))
                if not want:
                    res = "sin hash en Civitai; SHA256 local %s…" % got[:12]
                    tag = "maybe"
                elif got == want:
                    res = "✓ hash COINCIDE: %s" % os.path.basename(p)
                    tag = "ok"
                else:
                    res = "✗ hash DISTINTO — el archivo local no es esa versión"
                    tag = "no"
                self._q.put(("row", (str(r["version_id"]), "local",
                                     "✓ hash" if tag == "ok" else
                                     ("~ ?" if tag == "maybe" else "✗ ≠"), tag)))
                self._q.put(("status", res))
                self._q.put(("progress", 0))

        self._start(work)

    def download_sel(self):
        sel = self._sel_resources()
        picked = bool(self.tree.selection())
        # sin seleccion -> solo lora/lycoris (evita bajar el checkpoint de 6GB / embeddings)
        rs = sel if picked else [r for r in sel
                                 if r["kind"] in ("lora", "lycoris", "locon")]
        if not rs:
            messagebox.showinfo(
                "Descargar",
                "Selecciona filas en la tabla (Ctrl/Shift-clic).\n\n"
                "Las filas '(nombre)' son LoRAs sin link — pulsa primero "
                "'Resolver nombres (Civitai)' para buscarlas en Civitai/HF; "
                "si las encuentra podras bajarlas.\n\n"
                "Sin seleccion solo bajo LoRAs; el checkpoint y embeddings hay "
                "que elegirlos a mano.")
            return
        folder = filedialog.askdirectory(title="Carpeta destino (ej: ...\\models\\loras)")
        if not folder:
            return
        self._cancel = False
        self.b_cancel.state(["!disabled"])
        self.pb.configure(value=0)

        def human(n):
            for u in ("B", "KB", "MB", "GB"):
                if n < 1024:
                    return "%.1f %s" % (n, u)
                n /= 1024
            return "%.1f TB" % n

        def work():
            ok = 0
            for idx, r in enumerate(rs, 1):
                if self._cancel:
                    break
                info = self.cache.get(str(r["version_id"]), {})
                if info.get("error") == "404":
                    self._q.put(("status", "%s: borrado de Civitai, abro civarchive"
                                 % r["version_id"]))
                    self._q.put(("open", r["page_url"]))
                    continue
                is_hf = "huggingface.co" in r.get("dl_url", "")
                if is_hf and info.get("name"):
                    fn = re.sub(r"[^\w.\- ]", "_", info["name"])[:120]
                    dst = os.path.join(folder, fn)
                else:
                    base = info.get("name") or "civitai_%d" % r["version_id"]
                    fn = re.sub(r"[^\w.\- ]", "_", base.split("—")[-1].strip())[:80] \
                        or "civitai_%d" % r["version_id"]
                    dst = os.path.join(folder, fn + ".safetensors")
                label = fn[:40]
                try:
                    req = urllib.request.Request(
                        r["dl_url"] if is_hf else _with_token(r["dl_url"]),
                        headers={"User-Agent": "metadata-viewer"} if is_hf else _headers())
                    with urllib.request.urlopen(req, timeout=90) as resp:
                        cd = resp.headers.get("Content-Disposition", "")
                        mm = re.search(r'filename="?([^"\r\n]+)"?', cd)
                        if mm:
                            dst = os.path.join(folder, mm.group(1).strip())
                            label = mm.group(1).strip()[:40]
                        ctype = resp.headers.get("Content-Type", "")
                        if "text/html" in ctype or "application/json" in ctype:
                            raise RuntimeError("login / token requerido")
                        total = int(resp.headers.get("Content-Length") or 0)
                        got = 0
                        tmp = dst + ".part"
                        with open(tmp, "wb") as f:
                            while True:
                                if self._cancel:
                                    raise RuntimeError("cancelado")
                                chunk = resp.read(256 * 1024)
                                if not chunk:
                                    break
                                f.write(chunk)
                                got += len(chunk)
                                if total:
                                    frac = (idx - 1 + got / total) / len(rs)
                                    self._q.put(("progress", frac))
                                    self._q.put(("status", "%d/%d  %s  %s / %s (%d%%)" % (
                                        idx, len(rs), label, human(got), human(total),
                                        got * 100 // total)))
                                else:
                                    self._q.put(("status", "%d/%d  %s  %s…" %
                                                 (idx, len(rs), label, human(got))))
                        os.replace(tmp, dst)
                    ok += 1
                    self._q.put(("progress", idx / len(rs)))
                    self._q.put(("status", "ok %d/%d: %s (%s)" %
                                 (idx, len(rs), os.path.basename(dst), human(got))))
                except Exception as ex:
                    try:
                        os.remove(dst + ".part")
                    except Exception:
                        pass
                    if str(ex) == "cancelado":
                        self._q.put(("status", "cancelado en %s" % label))
                        break
                    self._q.put(("status", "%s fallo (%s) — abro la pagina para bajar manual"
                                 % (label, ex)))
                    self._q.put(("open", r["page_url"]))
            self._q.put(("progress", 0))
            self._q.put(("status", "terminado: %d/%d ok  ·  carpeta: %s"
                         % (ok, len(rs), folder)))
            self._q.put(("dlend", None))

        self._start(work)

    # ---- copy / save
    def copy(self, what):
        if not self.cur:
            return
        val = (json.dumps(self.cur["raw"], indent=1, ensure_ascii=False)
               if what == "raw" else self.cur.get(what, ""))
        if val:
            self.clipboard_clear(); self.clipboard_append(val)
            self.title("Metadata Viewer  -  copiado ✓")
            self.after(1200, lambda: self.title("Metadata Viewer"))

    def save_wf(self):
        if not self.cur or not self.cur.get("workflow"):
            return
        base = os.path.splitext(os.path.basename(self.cur["file"]))[0]
        p = filedialog.asksaveasfilename(defaultextension=".json",
                                         initialfile=base + "_workflow.json",
                                         filetypes=[("JSON", "*.json")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.cur["workflow"])
            messagebox.showinfo("Metadata Viewer",
                                "Guardado. Arrastralo al canvas de ComfyUI.")


if __name__ == "__main__":
    arg = next((a for a in sys.argv[1:] if os.path.isfile(a)), None)
    App(arg).mainloop()
