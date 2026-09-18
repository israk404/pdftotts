# 📹 PDFtoTTS

Turn any **16:9 landscape PDF** into a **narrated video** with **word-level highlighting** that sweeps across the exact page design.

No editing software. No manual timing. Just point it at a PDF and click **Generate**.

![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)
![FFmpeg](https://img.shields.io/badge/FFmpeg-required-green)
![License: MIT](https://img.shields.io/badge/license-MIT-yellow)

---

## ✨ What it does

- 🎙️ **Microsoft Edge neural voices** — natural TTS, free, no API key
- 🖍️ **Word-by-word highlight** — a marker sweeps across each spoken word, positioned using the PDF's own text coordinates
- 📄 **Page design preserved** — tables, headings, colours, borders, images, everything
- ⏱️ **Automatic page transitions** in sync with the narration
- 💧 **Optional watermark** with custom text, position, font, size, and colour
- 🔢 **Spoken symbol control** — choose whether `%`, `$`, `&`, `@`, `+`, `=` are read aloud or silently dropped
- 🎯 **Preview mode** — render just the first page (~30 s) before committing to the full video
- 💾 **Cached TTS** — reruns reuse completed chunks, so the second run is instant
- 🎛️ **Full GUI** — two-column layout, no CLI required

---

## 📋 Requirements

| Tool | Why | Install |
|---|---|---|
| **Python 3.8+** | Runs the app | [python.org](https://www.python.org/downloads/) |
| **FFmpeg** | Renders the MP4 | [ffmpeg.org](https://ffmpeg.org/download.html) — must be on `PATH` |
| **edge-tts** | Neural voice synthesis | `pip install edge-tts` |
| **PyMuPDF** | Reads the PDF, extracts word positions | `pip install pymupdf` |

**One-line install:**

```bash
pip install edge-tts pymupdf
```

---

## 🚀 Quick start

1. **Prepare your PDF**
   - Set page size to **33.87 cm × 19.05 cm** (exactly 16:9)
   - Set orientation to **Landscape**
   - Export as PDF

   > Non-16:9 PDFs work, but produce black letterbox bars.

2. **Run the script**
   ```bash
   python pdftotts.py
   ```

3. **In the app**
   - Browse to your PDF
   - Pick an output folder
   - Choose a voice (default: `en-US-AriaNeural`)
   - Pick a highlight colour preset (default: **Marker yellow**)
   - Click **▶ GENERATE VIDEO**

4. **Wait.** TTS is network-bound; the first run on a large PDF can take a few minutes. The progress log shows what's happening.

5. **Done.** The MP4 lands in your output folder, which opens automatically.

---

## 🖼️ How it works

```
PDF
 │  PyMuPDF renders each page → PNG (1920×1080, aspect preserved)
 │  PyMuPDF extracts every word's bounding box in canvas coordinates
 ▼
Per-page PNG + word bboxes
 │  edge-tts narrates the text word by word (WordBoundary events)
 ▼
Audio + per-word timings
 │  Greedy aligner matches each TTS word to its PDF bbox
 ▼
Timed word highlights
 │  ASS subtitle file draws one semi-transparent rectangle per word,
 │  positioned at its exact bbox, timed to its audio duration
 ▼
MP4 (page PNGs + ASS overlay + narration)
```

No OCR — the PDF must have a real text layer. Scanned documents will report "No extractable text found."

---

## 🎛️ Settings explained

### Voice
- **Voice** — 20+ English neural voices. `en-US-AriaNeural` (female, warm) and `en-US-GuyNeural` (male, clear) are good defaults.
- **Speed** — 0.5 to 2.0. `0.95` is a natural study pace.

### Spoken Symbols
Tick symbols that should be read aloud. Unticked symbols are dropped silently.

### Playback
- **Page transition delay** — how long before the next page's first word the page flips. Default `200 ms`.
- **Heading afterglow** — how long a heading's highlight lingers after being spoken. Default `400 ms`.
- **End tail** — pause after the last spoken word. Default `1500 ms`.
- **Timing offset** — global nudge if highlights consistently lead or lag. Usually `0`.

### Colours
- **Letterbox background** — bar colour around non-16:9 pages.
- **Word highlight** — marker colour. Presets: Marker yellow, Warm amber, Neon cyan, Soft green, Pink highlighter, Bright white.

### Watermark
Enable/disable, text, position (six presets), font, size, colour. Default text is empty.

---

## 💡 Tips

- **Preview first.** Any time you change voice, speed, or colours, run **Preview mode** for a 30-second check.
- **Use 16:9 PDFs.** A4 portrait works but produces large black bars.
- **Clear Cache** in the toolbar deletes the `_project` folder if you want to force TTS regeneration.
- **Caching is per-chunk.** If TTS fails partway, re-run — completed chunks are reused.

---

## 🐛 Troubleshooting

| Symptom | Fix |
|---|---|
| `No extractable text found` | The PDF is a scan. Run OCR first (`ocrmypdf in.pdf out.pdf`). |
| Video has huge black bars | PDF isn't 16:9. Re-export at 33.87 × 19.05 cm landscape. |
| `FFmpeg not found` | Install FFmpeg and add it to your `PATH`. |
| Audio cut off at the end | Open an issue with the log, especially the `Event timeline` and `Combined` lines. |
| Voice sounds robotic | Try `en-US-AriaNeural`, `en-US-JennyNeural`, or `en-GB-SoniaNeural`. |

---

## 📂 Output structure

```
<output_folder>/
├── YourDocument.mp4                 ← the final video
└── YourDocument_project/
    ├── page_0000.png                ← rendered page images
    ├── audio/chunk_0000.mp3         ← cached TTS
    ├── timestamps/chunk_0000_words.json
    ├── narration.wav                ← combined audio (temporary)
    ├── highlights.ass               ← subtitle/overlay file
    └── project.json                 ← cache manifest
```

You can safely delete the entire `_project` folder at any time.

---

## 🤝 Contributing

Pull requests welcome. If you find a bug:

1. Reproduce with **Preview mode** on a public PDF if possible.
2. Copy the full **Progress Log**.
3. Open an issue with the log and a short description.

---

## 📜 License

MIT — free to use, modify, and share. See `LICENSE` for details.

---

## 🙏 Acknowledgements

- [**edge-tts**](https://github.com/rany2/edge-tts) — free Microsoft Edge neural voices
- [**PyMuPDF**](https://pymupdf.readthedocs.io/) — PDF rendering and text extraction
- [**FFmpeg**](https://ffmpeg.org/) — video/audio encoding
- [**libass**](https://github.com/libass/libass) — the ASS subtitle renderer
