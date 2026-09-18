"""
PDFtoTTS  v1.0
==============
Turn a 16:9 landscape PDF into a narrated MP4 with word-by-word
highlighting that sweeps across the exact page design.

No editing software. No manual timing. Just point it at a PDF and click
Generate.

Features
--------
• Microsoft Edge neural voices (natural TTS, free, no API key)
• Word-level highlight marker positioned using the PDF's own text coords
• Page design preserved — tables, headings, colours, borders, images
• Automatic page transitions synced to the narration
• Optional watermark with position, font, size, colour
• Preview mode — render just the first page before committing
• Cached TTS — reruns reuse completed chunks, second run is instant
• Two-column GUI, no command line required

Requirements
------------
Python 3.8+, FFmpeg on PATH, edge-tts, PyMuPDF

License: MIT
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, scrolledtext, ttk


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME    = "PDFtoTTS"
APP_VERSION = "1.0"

CANVAS_W = 1920
CANVAS_H = 1080
FPS      = 24
ASPECT   = 16 / 9

DEFAULT_TAIL_MS = 1500

# Characters allowed to survive PDF extraction (whitelist approach).
# Anything not in this set splits the word and is discarded.
_LATIN_RANGE = "\u00C0-\u024F"
_WORD_SAFE_BASE = (
    "-"
    "A-Za-z0-9"
    + _LATIN_RANGE
    + "\u2018\u2019\u201C\u201D"
    + ".,!?;:()'\""
)

# Optional symbols the user can enable from the UI. When enabled, they
# survive PDF extraction and are spoken by the TTS engine.
_OPTIONAL_SYMBOLS = {
    "percent":  ("%",  "50% → 'fifty percent'"),
    "dollar":   ("$",  "$5 → 'five dollars'"),
    "amp":      ("&",  "A & B → 'A and B'"),
    "at":       ("@",  "user@site → 'user at site'"),
    "plus":     ("+",  "C++ → 'C plus plus'"),
    "equals":   ("=",  "x=5 → 'x equals five'"),
}

SETTINGS_PATH = Path.home() / f".{APP_NAME.lower()}_config.json"

DEFAULT_EN_VOICES = [
    "en-US-AriaNeural", "en-US-JennyNeural", "en-US-GuyNeural",
    "en-US-ChristopherNeural", "en-US-DavisNeural", "en-US-AmberNeural",
    "en-US-BrandonNeural", "en-US-CoraNeural", "en-US-ElizabethNeural",
    "en-US-EricNeural", "en-US-MichelleNeural", "en-US-MonicaNeural",
    "en-US-NancyNeural", "en-US-RogerNeural", "en-US-SteffanNeural",
    "en-GB-LibbyNeural", "en-GB-RyanNeural", "en-GB-SoniaNeural",
    "en-AU-NatashaNeural", "en-AU-WilliamNeural",
    "en-CA-ClaraNeural", "en-IN-NeerjaNeural",
]

FONT_CHOICES = ["Arial", "Segoe UI", "Verdana", "Calibri",
                "Trebuchet MS", "Tahoma", "Georgia", "Courier New"]

COLOR_PRESETS = {
    "Marker yellow":    ("#000000", "#FFEB3B"),
    "Warm amber":       ("#1A1A1A", "#FFB300"),
    "Neon cyan":        ("#000000", "#00E5FF"),
    "Soft green":       ("#0B1A0B", "#7FD46C"),
    "Pink highlighter": ("#1A1A2E", "#FF4FA3"),
    "Bright white":     ("#FFFFFF", "#FFD400"),
}

WM_POSITIONS = ["Top-Left", "Top-Center", "Top-Right",
                "Bottom-Left", "Bottom-Center", "Bottom-Right"]

_WM_POS_MAP = {
    "Top-Left":      (7, 0.00, 0.00),
    "Top-Center":    (8, 0.50, 0.00),
    "Top-Right":     (9, 1.00, 0.00),
    "Bottom-Left":   (1, 0.00, 1.00),
    "Bottom-Center": (2, 0.50, 1.00),
    "Bottom-Right":  (3, 1.00, 1.00),
}


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

def check_dependencies():
    missing = []
    try:
        import edge_tts  # noqa
    except ImportError:
        missing.append("edge-tts  →  pip install edge-tts")
    try:
        import pymupdf  # noqa
    except ImportError:
        try:
            import fitz  # noqa
        except ImportError:
            missing.append("PyMuPDF  →  pip install pymupdf")
    if shutil.which("ffmpeg") is None:
        missing.append("FFmpeg  →  https://ffmpeg.org/download.html  (add to PATH)")
    return missing


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def hex_to_ass(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}"


def hex_to_ffmpeg(hex_color: str) -> str:
    return "0x" + hex_color.lstrip("#").upper()


# ---------------------------------------------------------------------------
# FFmpeg filter path escaping
# ---------------------------------------------------------------------------

def escape_filter_path(p: str) -> str:
    """
    Escape a filesystem path for FFmpeg -vf filters like `ass='<path>'`.

    Windows:   C:\\Users\\x\\file.ass  →  C\\:/Users/x/file.ass
    """
    s = str(p).replace("\\", "/")
    s = s.replace(":", "\\:")
    s = s.replace("'", r"'\''")
    return s


# ---------------------------------------------------------------------------
# Text cleanup
# ---------------------------------------------------------------------------

def build_character_sets(enabled_symbols):
    """
    Build the whitelist regexes from base characters + user-enabled symbols.

    enabled_symbols — iterable of keys from _OPTIONAL_SYMBOLS
    """
    extras = "".join(_OPTIONAL_SYMBOLS[k][0] for k in enabled_symbols
                     if k in _OPTIONAL_SYMBOLS)

    word_safe = _WORD_SAFE_BASE + extras

    # Build regex character classes. Escape backslash and dash carefully:
    # we already included '-' as first char in _WORD_SAFE_BASE.
    def cls(chars):
        # Nothing needs escaping inside [...] except \ ] ^ -
        safe = chars.replace("\\", "\\\\").replace("]", "\\]").replace("^", "\\^")
        return safe

    frag_re      = re.compile(f"[{cls(word_safe)}]+")
    tts_safe     = word_safe + r"\s"
    tts_strip_re = re.compile(f"[^{cls(tts_safe)}]+")

    return frag_re, tts_strip_re


def sanitize_for_tts(t: str, strip_re) -> str:
    t = strip_re.sub(" ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def split_pdf_word(w: dict, frag_re) -> list:
    text = w.get("text", "")
    if not text:
        return []

    fragments = [(m.start(), m.end(), m.group(0))
                 for m in frag_re.finditer(text)]

    if not fragments:
        return []

    if len(fragments) == 1 and fragments[0][2] == text:
        return [w]

    L  = len(text)
    x0 = w["x0"]
    W  = w["x1"] - w["x0"]

    out = []
    for s, e, t in fragments:
        out.append({
            "text": t,
            "x0":   x0 + (s / L) * W,
            "x1":   x0 + (e / L) * W,
            "y0":   w["y0"],
            "y1":   w["y1"],
            "page": w["page"],
        })
    return out


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def _load_pymupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        import fitz
        return fitz


def extract_pdf_pages(pdf_path: str, project_dir: str, frag_re,
                       canvas_w: int = CANVAS_W,
                       canvas_h: int = CANVAS_H,
                       log_fn=None) -> list:
    fitz = _load_pymupdf()

    doc = fitz.open(pdf_path)
    pages = []
    warned = False

    for i, page in enumerate(doc):
        pw = page.rect.width
        ph = page.rect.height
        if pw <= 0 or ph <= 0:
            continue

        aspect = pw / ph
        if not warned and abs(aspect - ASPECT) > 0.05 * ASPECT:
            warned = True
            if log_fn:
                log_fn(f"⚠ Page {i+1} aspect {aspect:.2f} ≠ 16:9 "
                       f"({ASPECT:.2f}) — letterbox bars will appear.")
                log_fn("  Tip: export your PDF at 33.87 × 19.05 cm.")

        scale = min(canvas_w / pw, canvas_h / ph)
        ox = (canvas_w - pw * scale) / 2.0
        oy = (canvas_h - ph * scale) / 2.0

        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        png_path = str(Path(project_dir) / f"page_{i:04d}.png")
        pix.save(png_path)

        raw_words = []
        for w in page.get_text("words"):
            text = w[4]
            if not text or not text.strip():
                continue
            raw_words.append({
                "text": text,
                "x0":   w[0] * scale + ox,
                "y0":   w[1] * scale + oy,
                "x1":   w[2] * scale + ox,
                "y1":   w[3] * scale + oy,
                "page": i,
            })

        page_words = []
        for w in raw_words:
            page_words.extend(split_pdf_word(w, frag_re))

        pages.append({"index": i, "png": png_path, "words": page_words})

    doc.close()
    return pages


def mark_headings(pages: list):
    heights = []
    for p in pages:
        for w in p["words"]:
            h = w["y1"] - w["y0"]
            if h > 0:
                heights.append(h)
    if not heights:
        return
    heights.sort()
    median = heights[len(heights) // 2]
    threshold = median * 1.35
    for p in pages:
        for w in p["words"]:
            w["heading"] = (w["y1"] - w["y0"]) > threshold


# ---------------------------------------------------------------------------
# TTS chunking
# ---------------------------------------------------------------------------

_TERMINAL_PUNCT = ".!?,:;…\u2014\u2013"


def _word_for_tts(w: dict, is_last_of_heading: bool) -> str:
    t = w["text"]
    if not is_last_of_heading:
        return t
    if not w.get("heading"):
        return t
    if not t:
        return t
    if t[-1] in _TERMINAL_PUNCT:
        return t
    return t + "."


def build_tts_chunks(pages: list, max_chars: int = 3500):
    all_words = []
    for p in pages:
        all_words.extend(p["words"])

    for i, w in enumerate(all_words):
        is_head = w.get("heading", False)
        is_last = (i == len(all_words) - 1) or (not all_words[i + 1].get("heading", False))
        w["_last_of_heading"] = is_head and is_last

    chunks, cur_words, cur_parts = [], [], ""

    for w in all_words:
        piece = _word_for_tts(w, w.get("_last_of_heading", False))
        candidate_text = (cur_parts + " " + piece).strip() if cur_parts else piece
        if len(candidate_text) > max_chars and cur_words:
            chunks.append({"text": cur_parts.strip(), "words": cur_words})
            cur_parts, cur_words = piece, [w]
        else:
            cur_parts = candidate_text
            cur_words.append(w)

    if cur_words:
        chunks.append({"text": cur_parts.strip(), "words": cur_words})

    return chunks, all_words


# ---------------------------------------------------------------------------
# TTS generation
# ---------------------------------------------------------------------------

async def generate_tts_chunk(text, voice, rate, audio_path, words_path, strip_re):
    import edge_tts

    text = sanitize_for_tts(text, strip_re)

    communicate = edge_tts.Communicate(text, voice, rate=rate,
                                        boundary="WordBoundary")
    word_events = []
    audio_bytes = bytearray()

    try:
        async for event in communicate.stream():
            etype = event.get("type", "")
            if etype == "audio":
                audio_bytes.extend(event["data"])
            elif etype in ("WordBoundary", "SentenceBoundary"):
                word_events.append({
                    "word":        event["text"],
                    "offset_ms":   event["offset"]   / 10000.0,
                    "duration_ms": event["duration"] / 10000.0,
                })
    except Exception as exc:
        raise RuntimeError(f"TTS stream error: {exc}") from exc

    if not audio_bytes:
        raise RuntimeError("TTS returned empty audio — check internet/voice.")

    if not word_events:
        est_ms = max(500.0, len(audio_bytes) * 8 / 48.0)
        tokens = re.findall(r"\w+", text)
        step   = est_ms / max(1, len(tokens))
        for k, tok in enumerate(tokens):
            word_events.append({"word": tok, "offset_ms": k * step,
                                "duration_ms": step})

    with open(audio_path, "wb") as f:
        f.write(bytes(audio_bytes))
    with open(words_path, "w", encoding="utf-8") as f:
        json.dump(word_events, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------

def align_events_to_words(events_per_chunk, chunks, offsets):
    aligned = []
    for ci, (chunk, events) in enumerate(zip(chunks, events_per_chunk)):
        base_ms = offsets[ci]
        chunk_words = chunk["words"]
        pi = 0

        for ev in events:
            w_norm = re.sub(r"\W", "", ev["word"]).lower()
            if not w_norm:
                continue
            if pi >= len(chunk_words):
                break

            best = pi
            for k in range(pi, min(pi + 4, len(chunk_words))):
                pw_norm = re.sub(r"\W", "", chunk_words[k]["text"]).lower()
                if pw_norm == w_norm:
                    best = k
                    break

            pw = chunk_words[best]
            start_ms = base_ms + ev["offset_ms"]
            end_ms   = start_ms + ev["duration_ms"]
            if end_ms - start_ms < 50:
                end_ms = start_ms + 50

            aligned.append({
                "text":     pw["text"],
                "x0":       pw["x0"], "y0": pw["y0"],
                "x1":       pw["x1"], "y1": pw["y1"],
                "page":     pw["page"],
                "start_ms": start_ms,
                "end_ms":   end_ms,
                "heading":  pw.get("heading", False),
            })
            pi = best + 1

    return aligned


# ---------------------------------------------------------------------------
# Page timing
# ---------------------------------------------------------------------------

def compute_page_durations(aligned_words, pages, total_ms, pause_ms=0):
    if not pages:
        return []

    if not aligned_words:
        return [{
            "png":        pages[0]["png"],
            "start_ms":   0.0,
            "end_ms":     float(total_ms),
            "duration_s": max(0.2, total_ms / 1000.0),
        }]

    page_first, page_last = {}, {}
    for w in aligned_words:
        p = w["page"]
        if p not in page_first:
            page_first[p] = w["start_ms"]
        page_last[p] = w["end_ms"]

    order = sorted(page_first.keys())

    switch_times = [0.0]
    for i in range(len(order) - 1):
        cur_last   = page_last[order[i]]
        next_first = page_first[order[i + 1]]
        desired    = next_first - pause_ms
        switch     = max(cur_last, desired, switch_times[-1] + 50.0)
        switch_times.append(switch)

    segments = []
    for i, p in enumerate(order):
        start = switch_times[i]
        end   = switch_times[i + 1] if i + 1 < len(order) else float(total_ms)
        if end <= start:
            end = start + 200.0
        segments.append({
            "png":        pages[p]["png"],
            "start_ms":   start,
            "end_ms":     end,
            "duration_s": max(0.2, (end - start) / 1000.0),
        })
    return segments


# ---------------------------------------------------------------------------
# ASS generation
# ---------------------------------------------------------------------------

def ms_to_ass(ms):
    ms = max(0, int(ms))
    cs = ms // 10
    s  = cs // 100; cs = cs % 100
    m  = s // 60;  s  = s % 60
    h  = m // 60;  m  = m % 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _wm_xy(pos_name, canvas_w, canvas_h, margin=40):
    an, xf, yf = _WM_POS_MAP.get(pos_name, (7, 0.0, 0.0))
    x = margin + xf * (canvas_w - 2 * margin)
    y = margin + yf * (canvas_h - 2 * margin)
    return an, x, y


def make_ass_for_highlights(
    aligned_words,
    highlight_color,
    total_ms,
    heading_pause_ms=400,
    watermark=None,
    canvas_w=CANVAS_W,
    canvas_h=CANVAS_H,
    alpha_hex="70",
    afterglow_alpha="D0",
):
    ass_hl = hex_to_ass(highlight_color)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {canvas_w}
PlayResY: {canvas_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: HL,Arial,20,{ass_hl},{ass_hl},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: WM,Arial,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,1,1,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    afterglow_lines = []
    main_lines      = []
    pad = 2.0

    for w in aligned_words:
        x0 = w["x0"] - pad
        y0 = w["y0"] - pad
        W  = (w["x1"] - w["x0"]) + 2 * pad
        H  = (w["y1"] - w["y0"]) + 2 * pad

        if W <= 0 or H <= 0:
            continue
        if x0 < 0:
            W += x0; x0 = 0
        if y0 < 0:
            H += y0; y0 = 0
        if x0 + W > canvas_w:
            W = canvas_w - x0
        if y0 + H > canvas_h:
            H = canvas_h - y0
        if W <= 0 or H <= 0:
            continue

        start_ms = max(0.0, min(w["start_ms"], total_ms - 50))
        end_ms   = max(start_ms + 50, min(w["end_ms"], total_ms))

        if start_ms >= total_ms:
            continue

        start = ms_to_ass(start_ms)
        end   = ms_to_ass(end_ms)
        draw  = f"m 0 0 l {W:.1f} 0 l {W:.1f} {H:.1f} l 0 {H:.1f} l 0 0"

        main_lines.append(
            f"Dialogue: 1,{start},{end},HL,,0,0,0,,"
            f"{{\\an7\\pos({x0:.1f},{y0:.1f})\\p1\\bord0\\shad0"
            f"\\1c{ass_hl}&\\1a&H{alpha_hex}&}}"
            f"{draw}{{\\p0}}"
        )

        if w.get("heading") and heading_pause_ms > 0:
            ago_end = min(w["end_ms"] + heading_pause_ms, total_ms)
            if ago_end > end_ms:
                ago_str = ms_to_ass(ago_end)
                afterglow_lines.append(
                    f"Dialogue: 0,{end},{ago_str},HL,,0,0,0,,"
                    f"{{\\an7\\pos({x0:.1f},{y0:.1f})\\p1\\bord0\\shad0"
                    f"\\1c{ass_hl}&\\1a&H{afterglow_alpha}&}}"
                    f"{draw}{{\\p0}}"
                )

    wm_line = ""
    if watermark and watermark.get("enabled") and watermark.get("text", "").strip():
        wm = watermark
        an, wx, wy = _wm_xy(wm.get("position", "Bottom-Right"),
                            canvas_w, canvas_h, margin=40)
        wm_colour = hex_to_ass(wm.get("color", "#FFFFFF"))
        wm_font   = wm.get("font", "Arial").replace(",", " ")
        wm_size   = int(wm.get("size", 36))
        wm_text   = wm["text"].replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}")
        end_ass   = ms_to_ass(total_ms + 3000)

        overrides = (
            f"\\an{an}\\pos({wx:.0f},{wy:.0f})"
            f"\\fn{wm_font}\\fs{wm_size}"
            f"\\c{wm_colour}&\\bord1.5\\shad1\\3c&H00000000&"
        )
        wm_line = (f"Dialogue: 2,0:00:00.00,{end_ass},WM,,0,0,0,,"
                   f"{{{overrides}}}{wm_text}")

    body = "\n".join(afterglow_lines + main_lines + ([wm_line] if wm_line else []))
    return header + body + "\n"


# ---------------------------------------------------------------------------
# Audio utilities
# ---------------------------------------------------------------------------

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def get_audio_duration_ms(path):
    if not Path(path).exists():
        return 0.0

    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            fmt = data.get("format", {})
            dur = fmt.get("duration")
            if dur:
                d = float(dur)
                if d > 0.05:
                    return d * 1000.0
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["ffmpeg", "-i", path, "-hide_banner"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        m = _DUR_RE.search(r.stderr or "")
        if m:
            h, mn, s = m.group(1), m.group(2), m.group(3)
            return (int(h) * 3600 + int(mn) * 60 + float(s)) * 1000.0
    except Exception:
        pass

    return 0.0


def combine_audio_chunks(paths, output_path):
    list_file = output_path + ".list.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in paths:
            safe = p.replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    cmd = ["ffmpeg", "-y",
           "-f", "concat", "-safe", "0", "-i", list_file,
           "-acodec", "pcm_s16le",
           output_path]

    r = subprocess.run(cmd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    try:
        os.remove(list_file)
    except OSError:
        pass
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg audio combine failed:\n{r.stderr[-2000:]}")
    return get_audio_duration_ms(output_path)


# ---------------------------------------------------------------------------
# Video rendering
# ---------------------------------------------------------------------------

def unique_output_path(p: str) -> str:
    path = Path(p)
    if not path.exists():
        return str(path)
    stem, suffix = path.stem, path.suffix
    parent = path.parent
    i = 2
    while True:
        cand = parent / f"{stem}_v{i}{suffix}"
        if not cand.exists():
            return str(cand)
        i += 1


def _run_ffmpeg_render(cmd, log_fn):
    if log_fn:
        log_fn("Running FFmpeg render…")
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace")
    stdout, stderr = proc.communicate()
    return proc.returncode, (stdout or ""), (stderr or "")


def render_video(page_durations, ass_path, audio_path, output_path,
                  bg_color, total_ms, log_fn=None):
    if not page_durations:
        raise RuntimeError("No pages to render.")

    concat_file = output_path + ".pages.txt"
    with open(concat_file, "w", encoding="utf-8") as f:
        for pd in page_durations:
            safe = pd["png"].replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
            f.write(f"duration {pd['duration_s']:.3f}\n")
        safe = page_durations[-1]["png"].replace("\\", "/").replace("'", "'\\''")
        f.write(f"file '{safe}'\n")

    ass_escaped = escape_filter_path(ass_path)
    bg_hex      = hex_to_ffmpeg(bg_color)

    def build_cmd(filter_name):
        vf = (
            f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,"
            f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color={bg_hex},"
            f"fps={FPS},format=yuv420p,"
            f"{filter_name}='{ass_escaped}'"
        )
        return ["ffmpeg", "-y",
                "-hide_banner",
                "-f", "concat", "-safe", "0", "-i", concat_file,
                "-i", audio_path,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{total_ms / 1000.0:.3f}",
                "-movflags", "+faststart",
                output_path]

    try:
        cmd = build_cmd("ass")
        rc, out, err = _run_ffmpeg_render(cmd, log_fn)

        if rc != 0 and ("Could not create a libass track" in err
                        or "fopen failed" in err):
            if log_fn:
                log_fn("⚠ ass= filter failed, retrying with subtitles=…")
            cmd = build_cmd("subtitles")
            rc, out, err = _run_ffmpeg_render(cmd, log_fn)

        if rc != 0:
            detail = (err or "").strip()
            if not detail and out.strip():
                detail = out.strip()
            if not detail:
                detail = (f"FFmpeg exited with code {rc} but produced no "
                          f"output. Try running the command below manually.")
            raise RuntimeError(
                f"FFmpeg render failed (exit {rc}):\n"
                f"{detail[-3000:]}\n\n"
                f"Command:\n{' '.join(cmd)}"
            )
    finally:
        try:
            os.remove(concat_file)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Project manifest
# ---------------------------------------------------------------------------

class ProjectManifest:
    def __init__(self, project_dir):
        self.dir  = Path(project_dir)
        self.path = self.dir / "project.json"
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"version": 1, "chunks_done": [], "audio_combined": False,
                "total_duration_ms": 0}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def chunk_done(self, idx):
        return idx in self.data["chunks_done"]

    def mark_chunk(self, idx):
        if idx not in self.data["chunks_done"]:
            self.data["chunks_done"].append(idx)
        self.save()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class GenerationPipeline:
    def __init__(self, config, log_fn, progress_fn, title_fn, cancel_event):
        self.cfg       = config
        self.log       = log_fn
        self.progress  = progress_fn
        self.set_title = title_fn
        self.cancel    = cancel_event

    def run(self):
        cfg = self.cfg
        project_dir = Path(cfg["output_folder"]) / \
                      (Path(cfg["input_file"]).stem + "_project")
        project_dir.mkdir(parents=True, exist_ok=True)
        audio_dir = project_dir / "audio";      audio_dir.mkdir(exist_ok=True)
        ts_dir    = project_dir / "timestamps"; ts_dir.mkdir(exist_ok=True)
        manifest  = ProjectManifest(str(project_dir))

        # ── Build character sets from UI toggles ──────────────────────────
        frag_re, strip_re = build_character_sets(cfg.get("symbols", []))

        # ── 1. PDF extraction ─────────────────────────────────────────────
        self.log("Reading PDF…")
        pages = extract_pdf_pages(cfg["input_file"], str(project_dir),
                                   frag_re, log_fn=self.log)

        total_words_all = sum(len(p["words"]) for p in pages)
        if total_words_all == 0:
            raise RuntimeError(
                "No extractable text found in this PDF.\n"
                "If it's a scanned document, run OCR first."
            )

        mark_headings(pages)
        heading_count = sum(1 for p in pages for w in p["words"]
                            if w.get("heading"))

        if cfg.get("preview_mode"):
            pages = pages[:1]
            self.log("✓ Preview mode — using first page only")

        total_words = sum(len(p["words"]) for p in pages)
        self.log(f"✓ {len(pages)} pages  |  {total_words} words  "
                 f"|  {heading_count} heading words")

        # ── 2. Chunks ─────────────────────────────────────────────────────
        chunks, _ = build_tts_chunks(pages, max_chars=3500)
        self.log(f"✓ {len(chunks)} TTS chunks")

        # ── 3. Narration ──────────────────────────────────────────────────
        self.log("Generating narration…")
        rate_str = f"{int(round((cfg['voice_speed'] - 1.0) * 100)):+d}%"

        async def process_all():
            for ci, chunk in enumerate(chunks):
                if self.cancel.is_set():
                    raise InterruptedError("Cancelled")
                ap = str(audio_dir / f"chunk_{ci:04d}.mp3")
                wp = str(ts_dir    / f"chunk_{ci:04d}_words.json")

                if manifest.chunk_done(ci):
                    if (Path(ap).exists() and Path(ap).stat().st_size > 0
                            and Path(wp).exists()
                            and Path(wp).stat().st_size > 2):
                        self.log(f"  Chunk {ci+1}/{len(chunks)} — cached")
                        continue

                self.log(f"  Chunk {ci+1}/{len(chunks)}: {len(chunk['text'])} chars")
                self.progress(ci / len(chunks))
                self.set_title(f"Narration {int(ci / len(chunks) * 100)}%")

                for attempt in range(6):
                    if self.cancel.is_set():
                        raise InterruptedError("Cancelled")
                    try:
                        await generate_tts_chunk(chunk["text"], cfg["voice"],
                                                  rate_str, ap, wp, strip_re)
                        manifest.mark_chunk(ci)
                        break
                    except Exception as exc:
                        wait = min(2 ** attempt, 60)
                        self.log(f"    ⚠ Attempt {attempt+1}/6: {exc}")
                        if attempt < 5:
                            self.log(f"      Retry in {wait}s…")
                            await asyncio.sleep(wait)
                        else:
                            raise RuntimeError(
                                f"Chunk {ci+1} failed after 6 attempts: {exc}\n"
                                "Restart to resume from cache.")

        asyncio.run(process_all())
        self.progress(1.0)
        self.set_title("Combining audio…")
        self.log("✓ Narration done")

        # ── 4. Load word events ───────────────────────────────────────────
        evs_per_chunk = []
        for ci in range(len(chunks)):
            wp = str(ts_dir / f"chunk_{ci:04d}_words.json")
            try:
                with open(wp, encoding="utf-8") as f:
                    evs = json.load(f)
                if not isinstance(evs, list):
                    evs = []
            except Exception:
                evs = []
            evs_per_chunk.append(evs)

        # ── 5. Offsets (event-based) ──────────────────────────────────────
        offsets = []
        cursor_ms = 0.0
        for evs in evs_per_chunk:
            offsets.append(cursor_ms)
            if evs:
                last_end = max(ev["offset_ms"] + ev["duration_ms"] for ev in evs)
                cursor_ms += last_end + 40.0

        event_timeline_ms = cursor_ms
        self.log(f"✓ Event timeline: {event_timeline_ms/1000:.1f}s")

        # ── 6. Combine audio → WAV ────────────────────────────────────────
        combined = str(project_dir / "narration.wav")
        self.log("Combining audio…")
        file_total_ms = combine_audio_chunks(
            [str(audio_dir / f"chunk_{ci:04d}.mp3") for ci in range(len(chunks))],
            combined)
        manifest.data["audio_combined"]    = True
        manifest.data["total_duration_ms"] = file_total_ms
        manifest.save()
        self.log(f"✓ Combined: {file_total_ms/1000:.1f}s")

        gap_ms = event_timeline_ms - file_total_ms
        if abs(gap_ms) > 2000:
            self.log(f"⚠ Audio is {gap_ms/1000:+.1f}s vs event timeline. "
                     "Highlights may drift on the last chunk.")

        # ── 7. Alignment ──────────────────────────────────────────────────
        self.set_title("Aligning words…")
        self.log("Aligning TTS words to PDF positions…")

        aligned = align_events_to_words(evs_per_chunk, chunks, offsets)
        self.log(f"✓ {len(aligned)} words aligned to bboxes")

        if not aligned:
            raise RuntimeError("No words aligned — cannot render video.")

        nudge_ms = int(cfg.get("timing_offset_ms", 0))
        if nudge_ms:
            for w in aligned:
                w["start_ms"] += nudge_ms
                w["end_ms"]   += nudge_ms
            for w in aligned:
                if w["start_ms"] < 0:
                    w["start_ms"] = 0.0
                if w["end_ms"] < w["start_ms"] + 50:
                    w["end_ms"] = w["start_ms"] + 50
            self.log(f"✓ Applied global timing offset: {nudge_ms:+d} ms")

        # ── 8. Video length ───────────────────────────────────────────────
        last_word_end = max(w["end_ms"] for w in aligned)
        tail_ms       = int(cfg.get("tail_ms", DEFAULT_TAIL_MS))
        total_ms      = last_word_end + tail_ms

        if file_total_ms > 0 and total_ms > file_total_ms:
            self.log(f"  Note: clamped video from "
                     f"{total_ms/1000:.1f}s to audio length "
                     f"{file_total_ms/1000:.1f}s")
            total_ms = file_total_ms

        self.log(f"✓ Content: last word ends at {last_word_end/1000:.1f}s, "
                 f"tail {tail_ms}ms → video {total_ms/1000:.1f}s")

        # ── 9. Page segments ──────────────────────────────────────────────
        page_durs = compute_page_durations(
            aligned, pages, total_ms,
            pause_ms=int(cfg.get("page_pause_ms", 0)))
        self.log(f"✓ {len(page_durs)} page segments")

        # ── 10. ASS overlay ───────────────────────────────────────────────
        self.set_title("Building highlights…")
        self.log("Generating overlay…")
        ass_path = str(project_dir / "highlights.ass")
        wm_cfg = {
            "enabled":  cfg.get("wm_enabled", False),
            "text":     cfg.get("wm_text", ""),
            "position": cfg.get("wm_position", "Bottom-Right"),
            "font":     cfg.get("wm_font", "Arial"),
            "size":     int(cfg.get("wm_size", 36)),
            "color":    cfg.get("wm_color", "#FFFFFF"),
        }
        ass_content = make_ass_for_highlights(
            aligned_words    = aligned,
            highlight_color  = cfg["highlight_color"],
            total_ms         = total_ms,
            heading_pause_ms = int(cfg.get("heading_pause_ms", 400)),
            watermark        = wm_cfg,
        )
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        self.log("✓ Overlay ready")

        # ── 11. Render ────────────────────────────────────────────────────
        self.set_title("Rendering video…")
        suffix   = "_preview.mp4" if cfg.get("preview_mode") else ".mp4"
        base_out = str(Path(cfg["output_folder"]) /
                       (Path(cfg["input_file"]).stem + suffix))
        out_video = unique_output_path(base_out)
        if out_video != base_out:
            self.log(f"⚠ Existing file found — writing to {Path(out_video).name}")

        self.log("Rendering final video…")
        render_video(page_durs, ass_path, combined, out_video,
                     cfg["bg_color"], total_ms, log_fn=self.log)

        manifest.save()

        self.set_title("✓ Done")
        self.log("\n══════════════════════════════════════")
        self.log("✓  VIDEO COMPLETE")
        self.log(f"   {out_video}")
        self.log("══════════════════════════════════════")
        return out_video


# ---------------------------------------------------------------------------
# GUI components
# ---------------------------------------------------------------------------

class ColorButton(tk.Frame):
    def __init__(self, parent, label, initial, **kw):
        super().__init__(parent, bg="#1a1a2e", **kw)
        self._color = initial
        tk.Label(self, text=label, bg="#1a1a2e", fg="#e0e0e0",
                 font=("Segoe UI", 10)).pack(side="left", padx=(0, 6))
        self._sw = tk.Button(self, width=4, relief="flat",
                              cursor="hand2", command=self._pick)
        self._sw.pack(side="left")
        self._lbl = tk.Label(self, text=initial, bg="#1a1a2e",
                              fg="#aaa", font=("Consolas", 9))
        self._lbl.pack(side="left", padx=(4, 0))
        self._refresh()

    def _refresh(self):
        self._sw.configure(bg=self._color, activebackground=self._color)
        self._lbl.configure(text=self._color)

    def _pick(self):
        r = colorchooser.askcolor(color=self._color, title="Pick colour")
        if r and r[1]:
            self._color = r[1].upper()
            self._refresh()

    def get(self): return self._color
    def set(self, c): self._color = c; self._refresh()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION} — PDF → narrated video")
        self.resizable(True, True)
        self.configure(bg="#1a1a2e")
        self.geometry("1220x900")
        self.minsize(1000, 700)
        self._cancel_event = threading.Event()
        self._last_output  = ""
        self._saved        = load_settings()

        self._build_ui()
        self._apply_saved_settings(self._saved)
        self._load_voices_async()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("TFrame",        background="#1a1a2e")
        st.configure("TLabelframe",   background="#1a1a2e", foreground="#7ec8e3")
        st.configure("TLabelframe.Label", background="#1a1a2e",
                      foreground="#7ec8e3", font=("Segoe UI", 9, "bold"))
        st.configure("TLabel",        background="#1a1a2e", foreground="#e0e0e0",
                      font=("Segoe UI", 10))
        st.configure("Header.TLabel", font=("Segoe UI", 14, "bold"),
                      foreground="#7ec8e3")
        st.configure("Sub.TLabel",    background="#1a1a2e", foreground="#7ec8e3",
                      font=("Segoe UI", 9))
        st.configure("TEntry",        fieldbackground="#0f3460",
                      foreground="white", insertcolor="white")
        st.configure("TCombobox",     fieldbackground="#0f3460",
                      foreground="white", selectbackground="#0f3460")
        st.map("TCombobox",           fieldbackground=[("readonly", "#0f3460")])
        st.configure("Gen.TButton",   font=("Segoe UI", 12, "bold"),
                      background="#e94560", foreground="white", padding=10)
        st.map("Gen.TButton",         background=[("active", "#c73652"),
                                                   ("disabled", "#555")])
        st.configure("TButton",       background="#0f3460", foreground="white",
                      padding=6, font=("Segoe UI", 9))
        st.map("TButton",             background=[("active", "#1a5276")])
        st.configure("TProgressbar",  troughcolor="#0f3460",
                      background="#7ec8e3", thickness=16)
        st.configure("TCheckbutton",  background="#1a1a2e", foreground="#e0e0e0")

        ttk.Label(self, text=f"📹  {APP_NAME}",
                  style="Header.TLabel").pack(pady=(10, 2))
        ttk.Label(self,
                  text="Turn a 16:9 landscape PDF into a narrated video "
                       "with word-by-word highlighting",
                  style="Sub.TLabel").pack(pady=(0, 6))

        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x")

        br = ttk.Frame(bottom); br.pack(pady=8)
        self._gen_btn = ttk.Button(br, text="▶  GENERATE VIDEO",
                                    style="Gen.TButton", command=self._start)
        self._gen_btn.grid(row=0, column=0, padx=6)
        self._cancel_btn = ttk.Button(br, text="⏹  Cancel",
                                       command=self._cancel, state="disabled")
        self._cancel_btn.grid(row=0, column=1, padx=6)
        ttk.Button(br, text="📂  Open Folder",
                   command=self._open_folder).grid(row=0, column=2, padx=6)
        ttk.Button(br, text="🗑  Clear Cache",
                   command=self._start_over).grid(row=0, column=3, padx=6)

        self._pbar = ttk.Progressbar(bottom, mode="determinate",
                                      style="TProgressbar")
        self._pbar.pack(fill="x", padx=12, pady=(0, 3))

        self._status_var = tk.StringVar(value="Ready.")
        ttk.Label(bottom, textvariable=self._status_var,
                  foreground="#7ec8e3", background="#1a1a2e").pack()

        lf = ttk.LabelFrame(bottom, text=" Progress Log ", padding=4)
        lf.pack(fill="both", expand=True, padx=12, pady=(4, 8))
        self._log_box = scrolledtext.ScrolledText(
            lf, height=8, bg="#0a0a1a", fg="#b0e0e6",
            font=("Consolas", 9), state="disabled", relief="flat")
        self._log_box.pack(fill="both", expand=True)

        scroll_host = ttk.Frame(self)
        scroll_host.pack(side="top", fill="both", expand=True,
                          padx=12, pady=(4, 4))

        self._scroll_canvas = tk.Canvas(scroll_host, bg="#1a1a2e",
                                          highlightthickness=0, bd=0)
        scroll_bar = ttk.Scrollbar(scroll_host, orient="vertical",
                                     command=self._scroll_canvas.yview)
        self._scroll_canvas.configure(yscrollcommand=scroll_bar.set)
        self._scroll_canvas.pack(side="left", fill="both", expand=True)
        scroll_bar.pack(side="right", fill="y")

        inner = ttk.Frame(self._scroll_canvas)
        inner_id = self._scroll_canvas.create_window(
            (0, 0), window=inner, anchor="nw")

        def _on_inner_configure(_e):
            self._scroll_canvas.configure(
                scrollregion=self._scroll_canvas.bbox("all"))

        def _on_canvas_configure(e):
            self._scroll_canvas.itemconfig(inner_id, width=e.width)

        inner.bind("<Configure>", _on_inner_configure)
        self._scroll_canvas.bind("<Configure>", _on_canvas_configure)

        def _on_wheel(e):
            try:
                delta = -1 if e.delta > 0 else 1
                if e.num == 4:
                    delta = -1
                elif e.num == 5:
                    delta = 1
                self._scroll_canvas.yview_scroll(delta * 3, "units")
            except Exception:
                pass

        self._scroll_canvas.bind_all("<MouseWheel>", _on_wheel)
        self._scroll_canvas.bind_all("<Button-4>", _on_wheel)
        self._scroll_canvas.bind_all("<Button-5>", _on_wheel)

        inner.grid_columnconfigure(0, weight=1, uniform="col")
        inner.grid_columnconfigure(1, weight=1, uniform="col")

        left  = ttk.Frame(inner)
        right = ttk.Frame(inner)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        pad = {"pady": 6}

        # ── LEFT ─────────────────────────────────────────────────────────
        fi = ttk.LabelFrame(left, text=" Input ", padding=8)
        fi.pack(fill="x", **pad)
        fi.grid_columnconfigure(1, weight=1)

        ttk.Label(fi, text="PDF file:").grid(row=0, column=0, sticky="w", pady=3)
        self._input_var = tk.StringVar()
        ttk.Entry(fi, textvariable=self._input_var).grid(
            row=0, column=1, padx=6, sticky="ew")
        ttk.Button(fi, text="Browse…", command=self._browse_input).grid(
            row=0, column=2)

        ttk.Label(fi, text="Output folder:").grid(row=1, column=0, sticky="w", pady=3)
        self._output_var = tk.StringVar()
        ttk.Entry(fi, textvariable=self._output_var).grid(
            row=1, column=1, padx=6, sticky="ew")
        ttk.Button(fi, text="Browse…", command=self._browse_output).grid(
            row=1, column=2)

        ttk.Label(fi,
                  text="Tip: export your PDF at 33.87 × 19.05 cm "
                       "(16:9 landscape) so no letterbox bars appear.",
                  foreground="#888", background="#1a1a2e",
                  font=("Segoe UI", 8), wraplength=440, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        fv = ttk.LabelFrame(left, text=" Voice ", padding=8)
        fv.pack(fill="x", **pad)
        fv.grid_columnconfigure(1, weight=1)

        ttk.Label(fv, text="Voice:").grid(row=0, column=0, sticky="w", pady=3)
        self._voice_var = tk.StringVar(value="en-US-AriaNeural")
        self._voice_cb  = ttk.Combobox(fv, textvariable=self._voice_var,
                                        values=DEFAULT_EN_VOICES, width=30,
                                        state="readonly")
        self._voice_cb.grid(row=0, column=1, padx=6, sticky="ew")
        self._voice_status = ttk.Label(fv, text="Loading voices…",
                                        foreground="#aaa")
        self._voice_status.grid(row=0, column=2, padx=(4, 0), sticky="w")

        ttk.Label(fv, text="Speed (0.5–2.0):").grid(
            row=1, column=0, sticky="w", pady=3)
        self._speed_var = tk.DoubleVar(value=0.95)
        ttk.Spinbox(fv, textvariable=self._speed_var, from_=0.5, to=2.0,
                     increment=0.05, width=8, format="%.2f").grid(
            row=1, column=1, padx=6, sticky="w")

        fs = ttk.LabelFrame(left, text=" Spoken Symbols ", padding=8)
        fs.pack(fill="x", **pad)

        ttk.Label(fs,
                  text="Tick symbols that should be spoken by the voice. "
                       "Unticked symbols are silently dropped.",
                  foreground="#888", background="#1a1a2e",
                  font=("Segoe UI", 8), wraplength=440, justify="left").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self._symbol_vars = {}
        r = 1
        c = 0
        for key, (sym, example) in _OPTIONAL_SYMBOLS.items():
            var = tk.BooleanVar(value=False)
            self._symbol_vars[key] = var
            ttk.Checkbutton(fs, variable=var,
                             text=f"{sym}   {example}").grid(
                row=r, column=c, sticky="w", padx=(0, 20), pady=1)
            c += 1
            if c > 1:
                c = 0
                r += 1

        fp = ttk.LabelFrame(left, text=" Playback ", padding=8)
        fp.pack(fill="x", **pad)

        ttk.Label(fp, text="Page transition delay (ms):").grid(
            row=0, column=0, sticky="w", pady=3)
        self._pause_var = tk.IntVar(value=200)
        ttk.Spinbox(fp, textvariable=self._pause_var, from_=0, to=2000,
                     increment=50, width=6).grid(row=0, column=1, padx=6, sticky="w")

        ttk.Label(fp, text="Heading afterglow (ms):").grid(
            row=1, column=0, sticky="w", pady=3)
        self._heading_pause_var = tk.IntVar(value=400)
        ttk.Spinbox(fp, textvariable=self._heading_pause_var, from_=0, to=2000,
                     increment=50, width=6).grid(row=1, column=1, padx=6, sticky="w")

        ttk.Label(fp, text="End tail (ms):").grid(
            row=2, column=0, sticky="w", pady=3)
        self._tail_var = tk.IntVar(value=DEFAULT_TAIL_MS)
        ttk.Spinbox(fp, textvariable=self._tail_var, from_=0, to=5000,
                     increment=100, width=6).grid(row=2, column=1, padx=6, sticky="w")

        ttk.Label(fp, text="Timing offset (ms):").grid(
            row=3, column=0, sticky="w", pady=3)
        self._timing_offset_var = tk.IntVar(value=0)
        ttk.Spinbox(fp, textvariable=self._timing_offset_var,
                     from_=-2000, to=2000, increment=20, width=6).grid(
            row=3, column=1, padx=6, sticky="w")

        ttk.Label(fp,
                  text="Negative = highlight fires earlier, positive = later.",
                  foreground="#888", background="#1a1a2e",
                  font=("Segoe UI", 8)).grid(row=4, column=0, columnspan=2,
                                              sticky="w", pady=(2, 0))

        self._preview_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(fp, variable=self._preview_var,
                         text="Preview mode (first page only)").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ── RIGHT ────────────────────────────────────────────────────────
        fd = ttk.LabelFrame(right, text=" Colours ", padding=8)
        fd.pack(fill="x", **pad)

        self._bg_btn = ColorButton(fd, "Letterbox background:", "#000000")
        self._bg_btn.grid(row=0, column=0, columnspan=2, sticky="w", pady=4)

        self._hl_btn = ColorButton(fd, "Word highlight:", "#FFEB3B")
        self._hl_btn.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Label(fd, text="Preset:").grid(row=2, column=0, sticky="w", pady=3)
        self._preset_var = tk.StringVar(value="— pick a preset —")
        pcb = ttk.Combobox(fd, textvariable=self._preset_var,
                            values=["— pick a preset —"] + list(COLOR_PRESETS.keys()),
                            width=26, state="readonly")
        pcb.grid(row=2, column=1, padx=6, sticky="w")
        pcb.bind("<<ComboboxSelected>>", self._apply_preset)

        fw = ttk.LabelFrame(right, text=" Watermark ", padding=8)
        fw.pack(fill="x", **pad)
        fw.grid_columnconfigure(3, weight=1)

        self._wm_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(fw, variable=self._wm_enabled_var,
                         text="Enable watermark").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        ttk.Label(fw, text="Text:").grid(row=1, column=0, sticky="w", pady=3)
        self._wm_text_var = tk.StringVar(value="")
        ttk.Entry(fw, textvariable=self._wm_text_var).grid(
            row=1, column=1, columnspan=3, padx=6, sticky="ew")

        ttk.Label(fw, text="Position:").grid(row=2, column=0, sticky="w", pady=3)
        self._wm_pos_var = tk.StringVar(value="Bottom-Right")
        ttk.Combobox(fw, textvariable=self._wm_pos_var, values=WM_POSITIONS,
                      width=14, state="readonly").grid(
            row=2, column=1, padx=6, sticky="w")

        ttk.Label(fw, text="Font:").grid(row=2, column=2, sticky="w",
                                          padx=(12, 0))
        self._wm_font_var = tk.StringVar(value="Arial")
        ttk.Combobox(fw, textvariable=self._wm_font_var, values=FONT_CHOICES,
                      width=12, state="readonly").grid(
            row=2, column=3, padx=6, sticky="w")

        ttk.Label(fw, text="Size:").grid(row=3, column=0, sticky="w", pady=3)
        self._wm_size_var = tk.IntVar(value=36)
        ttk.Spinbox(fw, textvariable=self._wm_size_var, from_=8, to=200,
                     width=6).grid(row=3, column=1, padx=6, sticky="w")

        self._wm_color_btn = ColorButton(fw, "Colour:", "#FFFFFF")
        self._wm_color_btn.grid(row=3, column=2, columnspan=2, sticky="w",
                                 padx=(12, 0))

        fa = ttk.LabelFrame(right, text=f" About {APP_NAME} ", padding=8)
        fa.pack(fill="x", **pad)
        ttk.Label(fa,
                  text=(f"{APP_NAME} v{APP_VERSION}\n\n"
                        "Turn a 16:9 landscape PDF into a narrated MP4 with "
                        "word-level highlighting that sweeps across the exact "
                        "page design.\n\n"
                        "Built with Python, edge-tts, PyMuPDF, FFmpeg, libass.\n"
                        "Licensed under MIT."),
                  foreground="#c0d0e0", background="#1a1a2e",
                  font=("Segoe UI", 9), justify="left",
                  wraplength=440).pack(anchor="w")

    def _collect_settings(self):
        return {
            "voice":            self._voice_var.get(),
            "speed":            self._speed_var.get(),
            "pause_ms":         self._pause_var.get(),
            "heading_pause":    self._heading_pause_var.get(),
            "tail_ms":          self._tail_var.get(),
            "timing_offset_ms": self._timing_offset_var.get(),
            "preview":          self._preview_var.get(),
            "bg_color":         self._bg_btn.get(),
            "highlight_color":  self._hl_btn.get(),
            "wm_enabled":       self._wm_enabled_var.get(),
            "wm_text":          self._wm_text_var.get(),
            "wm_position":      self._wm_pos_var.get(),
            "wm_font":          self._wm_font_var.get(),
            "wm_size":          self._wm_size_var.get(),
            "wm_color":         self._wm_color_btn.get(),
            "symbols":          [k for k, v in self._symbol_vars.items() if v.get()],
        }

    def _apply_saved_settings(self, s):
        if not s:
            return
        try:
            if "speed" in s:            self._speed_var.set(float(s["speed"]))
            if "pause_ms" in s:         self._pause_var.set(int(s["pause_ms"]))
            if "heading_pause" in s:    self._heading_pause_var.set(int(s["heading_pause"]))
            if "tail_ms" in s:          self._tail_var.set(int(s["tail_ms"]))
            if "timing_offset_ms" in s: self._timing_offset_var.set(int(s["timing_offset_ms"]))
            if "preview" in s:          self._preview_var.set(bool(s["preview"]))
            if "bg_color" in s:         self._bg_btn.set(s["bg_color"])
            if "highlight_color" in s:  self._hl_btn.set(s["highlight_color"])
            if "wm_enabled" in s:       self._wm_enabled_var.set(bool(s["wm_enabled"]))
            if "wm_text" in s:          self._wm_text_var.set(s["wm_text"])
            if "wm_position" in s:      self._wm_pos_var.set(s["wm_position"])
            if "wm_font" in s:          self._wm_font_var.set(s["wm_font"])
            if "wm_size" in s:          self._wm_size_var.set(int(s["wm_size"]))
            if "wm_color" in s:         self._wm_color_btn.set(s["wm_color"])
            if "symbols" in s:
                for k in s["symbols"]:
                    if k in self._symbol_vars:
                        self._symbol_vars[k].set(True)
        except Exception:
            pass

    def _on_close(self):
        save_settings(self._collect_settings())
        self.destroy()

    def _apply_preset(self, _=None):
        name = self._preset_var.get()
        if name not in COLOR_PRESETS:
            return
        bg, hl = COLOR_PRESETS[name]
        self._bg_btn.set(bg)
        self._hl_btn.set(hl)

    def _load_voices_async(self):
        threading.Thread(target=self._fetch_voices, daemon=True).start()

    def _fetch_voices(self):
        try:
            import edge_tts
            voices = asyncio.run(edge_tts.list_voices())
            en = sorted([v["ShortName"] for v in voices
                         if v["Locale"].startswith("en-")],
                        key=lambda x: (not x.startswith("en-US"), x))
            self.after(0, lambda: self._update_voices(en))
        except Exception as exc:
            self.after(0, lambda: self._voice_status.configure(
                text=f"Using built-in list ({exc})", foreground="#f0a500"))

    def _update_voices(self, voices):
        self._voice_cb["values"] = voices
        wanted = self._saved.get("voice") if self._saved else None
        if wanted and wanted in voices:
            self._voice_var.set(wanted)
        elif voices:
            self._voice_var.set(voices[0])
        self._voice_status.configure(text=f"✓ {len(voices)} voices",
                                       foreground="#7ec8e3")

    def _browse_input(self):
        path = filedialog.askopenfilename(
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if path:
            self._input_var.set(path)
            if not self._output_var.get():
                self._output_var.set(str(Path(path).parent))

    def _browse_output(self):
        folder = filedialog.askdirectory()
        if folder:
            self._output_var.set(folder)

    def _open_folder(self):
        folder = self._output_var.get()
        if folder and Path(folder).exists():
            os.startfile(folder)
        elif self._last_output and Path(self._last_output).parent.exists():
            os.startfile(str(Path(self._last_output).parent))
        else:
            messagebox.showinfo("Open folder", "No output folder set yet.")

    def _start(self):
        errors = []
        if not self._input_var.get():
            errors.append("No input PDF selected.")
        elif not Path(self._input_var.get()).exists():
            errors.append("Input PDF does not exist.")
        elif not self._input_var.get().lower().endswith(".pdf"):
            errors.append("Input must be a .pdf file.")
        if not self._output_var.get():
            errors.append("No output folder selected.")
        if errors:
            messagebox.showerror("Input Error", "\n".join(errors))
            return

        missing = check_dependencies()
        if missing:
            messagebox.showerror("Missing dependencies",
                                  "Please install:\n\n" + "\n".join(missing))
            return

        save_settings(self._collect_settings())

        self._cancel_event.clear()
        self._gen_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._clear_log()
        self._pbar["value"] = 0

        cfg = {
            "input_file":       self._input_var.get(),
            "output_folder":    self._output_var.get(),
            "voice":            self._voice_var.get(),
            "voice_speed":      self._speed_var.get(),
            "bg_color":         self._bg_btn.get(),
            "highlight_color":  self._hl_btn.get(),
            "page_pause_ms":    self._pause_var.get(),
            "heading_pause_ms": self._heading_pause_var.get(),
            "tail_ms":          self._tail_var.get(),
            "timing_offset_ms": self._timing_offset_var.get(),
            "preview_mode":     self._preview_var.get(),
            "wm_enabled":       self._wm_enabled_var.get(),
            "wm_text":          self._wm_text_var.get(),
            "wm_position":      self._wm_pos_var.get(),
            "wm_font":          self._wm_font_var.get(),
            "wm_size":          self._wm_size_var.get(),
            "wm_color":         self._wm_color_btn.get(),
            "symbols":          [k for k, v in self._symbol_vars.items() if v.get()],
        }
        threading.Thread(target=self._run_pipeline, args=(cfg,),
                          daemon=True).start()

    def _run_pipeline(self, cfg):
        pipeline = GenerationPipeline(cfg, self._append_log, self._set_progress,
                                        self._set_title, self._cancel_event)
        try:
            out = pipeline.run()
            self._last_output = out
            self.after(0, self._on_done)
        except InterruptedError:
            self.after(0, lambda: self._on_error("Cancelled by user."))
        except Exception as exc:
            import traceback
            self.after(0, lambda: self._on_error(
                str(exc) + "\n\n" + traceback.format_exc()))

    def _cancel(self):
        self._cancel_event.set()
        self._append_log("⏹ Cancellation requested…")

    def _start_over(self):
        inp = self._input_var.get(); out = self._output_var.get()
        if not inp or not out:
            messagebox.showinfo("Clear Cache", "Select file and folder first.")
            return
        pd = Path(out) / (Path(inp).stem + "_project")
        if pd.exists():
            if messagebox.askyesno("Clear Cache",
                                    f"Delete cache:\n{pd}\n\nAre you sure?"):
                shutil.rmtree(pd)
                self._append_log("🗑 Cache cleared.")
        else:
            messagebox.showinfo("Clear Cache", "No cache found.")

    def _append_log(self, msg):
        def _do():
            self._log_box.configure(state="normal")
            self._log_box.insert("end", msg + "\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")
            self._status_var.set(msg[:120])
        self.after(0, _do)

    def _set_progress(self, f):
        self.after(0, lambda: self._pbar.__setitem__("value", f * 100))

    def _set_title(self, t):
        self.after(0, lambda: self.title(f"{APP_NAME} — {t}"))

    def _clear_log(self):
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")

    def _on_done(self):
        self._pbar["value"] = 100
        self._gen_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        self.title(f"{APP_NAME} — ✓ Done")
        if self._last_output:
            try:
                os.startfile(str(Path(self._last_output).parent))
            except Exception:
                pass
        messagebox.showinfo("Done!", "Video generated!\nOutput folder opened.")

    def _on_error(self, msg):
        self._gen_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        self.title(f"{APP_NAME} — Error")
        self._append_log(f"\n❌ ERROR:\n{msg}")
        messagebox.showerror("Error", msg[:600])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    App().mainloop()


if __name__ == "__main__":
    main()