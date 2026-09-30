"""Tippex: minimal PDF text editor for macOS. Usage: tippex file.pdf"""
import base64, json, subprocess, sys, threading, urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pymupdf

__version__ = "0.0.3"

SRC = None  # the PDF being edited; set in main()
ORIGINAL = None  # its bytes; edits always apply to this, so saving over SRC is safe
OUT = None  # chosen via the Save dialog on first save
ZOOM = 1.5

# Correction-fluid brush painting a white stroke.
LOGO = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<path d="M2.5 28c4-3 6-1.2 10-4" fill="none" stroke="#9aa4b2" stroke-width="5.5" stroke-linecap="round"/>
<path d="M2.5 28c4-3 6-1.2 10-4" fill="none" stroke="#fff" stroke-width="3.5" stroke-linecap="round"/>
<g transform="translate(12.8 23.8) rotate(45)">
<path d="M-.8 -2.6h1.6l-.45 2.6h-.7z" fill="#7d8796"/>
<path d="M-2.5 -7.6h5L1.1 -2.4h-2.2z" fill="#e6e9ee" stroke="#9aa4b2" stroke-width=".6" stroke-linejoin="round"/>
<rect x="-2.5" y="-23" width="5" height="15.6" rx=".8" fill="#1f3b73"/>
<path d="M-2.5 -9.8h5M-2.5 -11.6h5" stroke="#4a6bb0" stroke-width=".8"/>
<rect x="-2.5" y="-25.4" width="5" height="3" rx="1.4" fill="#e6e9ee"/>
<rect x="1.7" y="-22.4" width="1.5" height="8.5" rx=".75" fill="#c9ced6"/>
</g></svg>"""
FAVICON = "data:image/svg+xml," + urllib.parse.quote(LOGO)


def lines(page):
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if spans:
                yield line["bbox"], "".join(s["text"] for s in line["spans"]), spans[0]


# Standard PDF fonts by family: regular, bold, italic, bold-italic.
FAMILIES = {
    "sans": ("helv", "hebo", "heit", "hebi"),
    "serif": ("tiro", "tibo", "tiit", "tibi"),
    "mono": ("cour", "cobo", "coit", "cobi"),
}
CSS_FAMILIES = {"sans": "Helvetica,Arial,sans-serif", "serif": "Times,'Times New Roman',serif", "mono": "Courier,Menlo,monospace"}


def font_style(span):
    """Guess (family, bold, italic) for a span from its font flags and name."""
    name, flags = span["font"].lower(), span["flags"]
    bold = bool(flags & 16) or any(w in name for w in ("bold", "black", "heavy"))
    italic = bool(flags & 2) or any(w in name for w in ("italic", "oblique"))
    if flags & 8 or any(w in name for w in ("courier", "mono", "consol", "menlo")):
        family = "mono"
    elif "sans" not in name and (flags & 4 or any(w in name for w in (
            "times", "serif", "georgia", "garamond", "cambria", "roman", "palatino", "baskerville"))):
        family = "serif"
    else:
        family = "sans"
    return family, bold, italic


def base_name(font):
    return font.split("+")[-1]  # drop the "ABCDEF+" subset prefix


class Fonts:
    """Draws replacement text in the PDF's own font where that's safe, else the
    closest standard font.

    Embedded fonts are usually subsets without a usable character map, so we learn
    which glyph draws each character from the text already in the document, and only
    reuse a font when every character we need has been seen drawn in it."""

    def __init__(self, doc):
        self.doc = doc
        self.glyphs = {}  # font name -> {character: glyph id}
        for page in doc:
            for span in page.get_texttrace():
                seen = self.glyphs.setdefault(base_name(span["font"]), {})
                for uni, gid, *_ in span["chars"]:
                    if 0 < uni != 0xFFFD and gid > 0:
                        seen.setdefault(chr(uni), gid)

    def encode(self, page, span, text):
        """Returns (font xref, encoded text) to draw text with one of the document's
        own fonts, or None if that can't be done reliably."""
        name = base_name(span["font"])
        seen = self.glyphs.get(name, {})
        if page.rotation or not text or not all(c in seen for c in text):
            return None
        if self.doc.xref_get_key(page.xref, "Resources")[0] == "null":
            return None  # resources inherited from the page tree; adding ours here would hide them
        for xref, _, ftype, basefont, *_ in page.get_fonts(full=True):
            if base_name(basefont) != name:
                continue
            encoding = self.doc.xref_get_key(xref, "Encoding")
            if ftype == "Type0" and encoding == ("name", "/Identity-H") and self.glyph_ids_are_codes(xref):
                return xref, b"".join(seen[c].to_bytes(2, "big") for c in text)
            if ftype in ("TrueType", "Type1") and encoding in (("name", "/WinAnsiEncoding"), ("name", "/MacRomanEncoding")):
                try:
                    return xref, text.encode("cp1252" if "WinAnsi" in encoding[1] else "mac_roman")
                except UnicodeEncodeError:
                    return None
        return None

    def glyph_ids_are_codes(self, xref):
        kind, value = self.doc.xref_get_key(xref, "DescendantFonts")
        if kind != "array":
            return False
        descendant = int(value.strip("[]").split()[0])
        return (self.doc.xref_get_key(descendant, "Subtype") == ("name", "/CIDFontType2")
                and self.doc.xref_get_key(descendant, "CIDToGIDMap")[1] in ("null", "/Identity"))


def standard_font(span):
    family, bold, italic = font_style(span)
    return FAMILIES[family][bold + 2 * italic]


def draw_with_page_font(page, font_xref, encoded, span):
    """Append a content stream drawing pre-encoded text with an existing font object.
    The font may live in a nested resource dictionary (macOS PDFs do this), so it's
    registered on the page itself under our own name."""
    doc = page.parent
    resname = f"Tippex{font_xref}"
    doc.xref_set_key(page.xref, f"Resources/Font/{resname}", f"{font_xref} 0 R")
    x, y = pymupdf.Point(span["origin"]) * ~page.transformation_matrix
    r, g, b = rgb(span["color"])
    stream = f"q BT {r:.3f} {g:.3f} {b:.3f} rg /{resname} {span['size']:.2f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm <{encoded.hex()}> Tj ET Q"
    page.wrap_contents()
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, stream.encode())
    kind, value = doc.xref_get_key(page.xref, "Contents")
    contents = value[:-1] + f" {xref} 0 R]" if kind == "array" else f"[{value} {xref} 0 R]"
    doc.xref_set_key(page.xref, "Contents", contents)


def rgb(color):
    return ((color >> 16) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255)


def build_page():
    doc = pymupdf.open(stream=ORIGINAL)
    html = []
    for pno, page in enumerate(doc):
        png = base64.b64encode(page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM)).tobytes("png")).decode()
        inputs = []
        for i, (bbox, text, span) in enumerate(lines(page)):
            x0, y0, x1, y1 = (v * ZOOM for v in bbox)
            family, bold, italic = font_style(span)
            # Preview in the document's own font if it's installed on this Mac.
            own = base_name(span["font"]).split("-")[0].replace(" Regular", "").replace('"', "")
            inputs.append(
                f'<input class="line" data-p="{pno}" data-i="{i}" data-size="{span["size"]:.2f}" data-fam="{family}" '
                f'value="{escape(text)}" data-orig="{escape(text)}" style="left:{x0}px;top:{y0}px;width:{x1 - x0 + 40}px;'
                f'height:{y1 - y0}px;font-size:{span["size"] * ZOOM * 0.9}px;font-family:&quot;{escape(own)}&quot;,{CSS_FAMILIES[family]};'
                f'font-weight:{700 if bold else 400};font-style:{"italic" if italic else "normal"}">'
            )
        html.append(f'<div class="page" data-p="{pno}"><img src="data:image/png;base64,{png}" draggable="false">{"".join(inputs)}</div>')
    return (TEMPLATE.replace("{{LOGO}}", LOGO).replace("{{FAVICON}}", FAVICON).replace("{{VERSION}}", __version__)
            .replace("{{ZOOM}}", str(ZOOM)).replace("{{CSS_FAMILIES}}", json.dumps(CSS_FAMILIES))
            .replace("{{NAME}}", escape(SRC.name)).replace("{{PATH}}", escape(str(SRC))).replace("{{PAGES}}", "".join(html)))


def escape(s):
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


# A long-lived helper that shows the native Save panel. Starting AppKit takes ~2s,
# so we pay that once at launch instead of on every Save click.
SAVE_PANEL_JXA = """
ObjC.import('AppKit');
const app = $.NSApplication.sharedApplication;
app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
$.NSSavePanel.savePanel;
const stdin = $.NSFileHandle.fileHandleWithStandardInput;
const stdout = $.NSFileHandle.fileHandleWithStandardOutput;
while (true) {
  const data = stdin.availableData;
  if (data.length == 0) break;
  const req = JSON.parse($.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js);
  const panel = $.NSSavePanel.savePanel;
  panel.setMessage($('Save edited PDF as:'));
  panel.setAllowedFileTypes($(['pdf']));
  panel.setDirectoryURL($.NSURL.fileURLWithPath($(req.dir)));
  panel.setNameFieldStringValue($(req.name));
  app.activateIgnoringOtherApps(true);
  const path = panel.runModal == $.NSModalResponseOK ? panel.URL.path.js : '';
  app.hide(null);
  stdout.writeData($(JSON.stringify({path}) + '\\n').dataUsingEncoding($.NSUTF8StringEncoding));
}
"""
_save_panel = None


def start_save_panel():
    global _save_panel
    _save_panel = subprocess.Popen(
        ["osascript", "-l", "JavaScript", "-e", SAVE_PANEL_JXA],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )


def ask_save_path():
    """Show the native macOS Save dialog. Returns a Path, or None if cancelled."""
    if _save_panel is None or _save_panel.poll() is not None:
        start_save_panel()
    default = OUT or SRC.with_name(SRC.stem + "-tippexed.pdf")
    _save_panel.stdin.write(json.dumps({"dir": str(default.parent), "name": default.name}) + "\n")
    _save_panel.stdin.flush()
    reply = _save_panel.stdout.readline()
    if not reply:
        raise RuntimeError("the Save dialog closed unexpectedly")
    path = json.loads(reply)["path"]
    return Path(path) if path else None


def save(changes, path):
    """Apply edits to the original PDF and write it to path. Each page is layered as
    in the editor: edited lines, then white-out boxes on top, then added text."""
    doc = pymupdf.open(stream=ORIGINAL)
    fonts = Fonts(doc)
    for pno, page in enumerate(doc):
        edits = [e for e in changes["edits"] if e["p"] == pno]
        boxes = [b for b in changes["boxes"] if b["p"] == pno]
        added = [a for a in changes["added"] if a["p"] == pno]

        if edits:
            page_lines = list(lines(page))
            todo = []
            for e in edits:
                bbox, _, span = page_lines[e["i"]]
                todo.append((span, e["text"], fonts.encode(page, span, e["text"])))
                page.add_redact_annot(bbox, fill=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
            for span, text, own_font in todo:
                if not text.strip():
                    continue
                if own_font:
                    draw_with_page_font(page, *own_font, span)
                else:
                    page.insert_text(span["origin"], text, fontsize=span["size"], fontname=standard_font(span), color=rgb(span["color"]))

        if boxes:
            # Redacting (not just painting white) also removes the text and image pixels underneath.
            for b in boxes:
                page.add_redact_annot(pymupdf.Rect(b["x0"], b["y0"], b["x1"], b["y1"]), fill=(1, 1, 1))
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)

        for a in added:
            page.insert_text((a["x"], a["y"]), a["text"], fontsize=a["size"], fontname=FAMILIES[a["fam"]][0])
    doc.save(path, garbage=3, deflate=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.reply(build_page().encode(), "text/html")

    def do_POST(self):
        global OUT
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/reveal":
            subprocess.run(["open", "-R", str(OUT)])
            return self.reply(b"{}", "application/json")
        try:
            path = OUT
            if path is None or body["saveAs"]:
                path = ask_save_path()
                if path is None:
                    return self.reply(json.dumps({"cancelled": True}).encode(), "application/json")
            save(body, path)
            OUT = path
            result = {"path": str(path)}
        except Exception as e:
            result = {"error": str(e)}
        self.reply(json.dumps(result).encode(), "application/json")

    def reply(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


TEMPLATE = """<!doctype html><meta charset="utf-8"><title>Tippex – {{NAME}}</title><link rel="icon" href="{{FAVICON}}">
<style>
body{margin:0;background:#f4f5f7;font-family:-apple-system,sans-serif}
header{position:sticky;top:0;z-index:9;background:#222;color:#fff;padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header .grow{flex:1}
.brand{display:flex;align-items:center;gap:8px;font-weight:700;font-size:17px;letter-spacing:.3px}
.brand svg{width:30px;height:30px}
.version{font-weight:400;font-size:11px;color:#aaa;margin-left:-3px;align-self:flex-end;padding-bottom:3px}
.dim{color:#aaa}
.path{font-size:13px;word-break:break-all}
button{font-size:15px;padding:6px 16px}
.tools{display:flex;border-radius:7px;overflow:hidden;border:1px solid #555}
.tools button{background:#333;color:#ddd;border:0;border-right:1px solid #555;font-size:13px;padding:6px 12px;cursor:pointer}
.tools button:last-child{border-right:0}
.tools button.on{background:#4a6bb0;color:#fff}
.tools button:disabled{color:#777;cursor:default}
#find{flex-basis:100%;display:flex;gap:8px;align-items:center;font-size:13px}
#find[hidden]{display:none}
#find input{font-size:13px;padding:5px 8px;width:220px;border-radius:5px;border:1px solid #555}
#find button{font-size:13px;padding:5px 12px}
#dest{position:sticky;top:52px;z-index:8;background:#fffbe6;color:#333;padding:8px 16px;font-size:13px;border-bottom:1px solid #e5d98a;word-break:break-all}
#dest a{margin-left:8px;color:#06c}
.hint{text-align:center;color:#667;font-size:13px;margin:14px 0 0}
.page{position:relative;width:max-content;margin:20px auto;box-shadow:0 1px 3px #0002,0 6px 24px #0000001a;user-select:none}
.page img{display:block}
.page input{position:absolute;box-sizing:border-box;border:1px solid transparent;background:transparent;color:transparent;padding:0;outline:none}
.page input.line{z-index:1}
.page input:hover{border-color:#39f8}
.page input:focus,.page input.changed,.page input.added{background:#fff;color:#000;border-color:#39f}
.page input.added{z-index:3;background:transparent;border-style:dashed}
.page input.added:focus{background:#fff}
.box{position:absolute;z-index:2;background:#fff;outline:1px dashed #39f}
.box button{display:none;position:absolute;top:-10px;right:-10px;width:20px;height:20px;padding:0;border-radius:50%;border:0;background:#e5484d;color:#fff;font-size:14px;line-height:20px;cursor:pointer}
.box:hover button{display:block}
body[data-mode=text] .page{cursor:text}
body[data-mode=white] .page{cursor:crosshair}
body[data-mode=text] .page input.line,body[data-mode=white] .page input{pointer-events:none}
</style>
<header>
  <span class="brand">{{LOGO}}Tippex<span class="version">v{{VERSION}}</span></span><span class="dim path" title="Editing {{PATH}}">{{PATH}}</span>
  <span class="grow"></span>
  <div class="tools">
    <button data-mode="edit" class="on" title="Click any text to change it (Esc undoes a line)">Edit text</button>
    <button data-mode="text" title="Click anywhere on a page to add text">Add text</button>
    <button data-mode="white" title="Drag a box to white out anything">White-out</button>
  </div>
  <div class="tools"><button id="undo" title="Undo (⌘Z or Ctrl+Z)" disabled>↶ Undo</button><button id="redo" title="Redo (⇧⌘Z)" disabled>↷ Redo</button></div>
  <div class="tools"><button id="findbtn" title="Find and replace (⌘F)">Find &amp; Replace</button></div>
  <span id="status" class="dim"></span>
  <button id="save">Save…</button><button id="saveas" hidden>Save As…</button>
  <div id="find" hidden>
    <input id="q" placeholder="Find"><input id="r" placeholder="Replace with">
    <button id="replaceall">Replace all</button><span id="count" class="dim"></span>
    <span class="grow"></span><button id="closefind">Done</button>
  </div>
</header>
<div id="dest" hidden></div>
<p class="hint" id="hint"></p>
{{PAGES}}
<script>
const ZOOM = {{ZOOM}}, FONTS = {{CSS_FAMILIES}};
const HINTS = {
  edit: 'Click any text to change it. ⌘Z or Ctrl+Z undoes, ⇧⌘Z redoes.',
  text: 'Click anywhere on a page to add text. Press Esc to go back to editing.',
  white: 'Drag a box over anything to white it out. Hover a box and click × to remove it.',
};
let savedPath = null, dirty = false, mode = 'edit';
const $ = id => document.getElementById(id);
const setDirty = d => { dirty = d; $('status').textContent = d ? 'Unsaved changes' : (savedPath ? 'All changes saved' : ''); };

function setMode(m) {
  mode = m;
  document.body.dataset.mode = m;
  document.querySelectorAll('.tools button[data-mode]').forEach(b => b.classList.toggle('on', b.dataset.mode === m));
  $('hint').textContent = HINTS[m];
}
document.querySelectorAll('.tools button[data-mode]').forEach(b => b.onclick = () => setMode(b.dataset.mode));
setMode('edit');

// Undo history. Every change is recorded as {undo, redo}.
const done = [], undone = [];
function record(action) { done.push(action); undone.length = 0; setDirty(true); updateUndoButtons(); }
function undo() { const a = done.pop(); if (a) { a.undo(); undone.push(a); setDirty(true); } updateUndoButtons(); }
function redo() { const a = undone.pop(); if (a) { a.redo(); done.push(a); setDirty(true); } updateUndoButtons(); }
function updateUndoButtons() { $('undo').disabled = !done.length; $('redo').disabled = !undone.length; }

// Text boxes are existing lines or added text. Typing in one becomes a single
// undo step when you leave it (or press undo while still in it).
function refresh(el) {
  if (el.classList.contains('line')) el.classList.toggle('changed', el.value !== el.dataset.orig);
  else el.size = Math.max(4, el.value.length + 1);
}
function setValue(el, v) { el.value = el._before = v; refresh(el); }
function commit(el) {
  const before = el._before, after = el.value, pg = el.parentElement;
  el._before = after;
  if (el.classList.contains('added') && !after.trim()) {  // an empty added box disappears
    el.remove();
    if (el._new) return 'discarded';
    record({undo: () => { pg.append(el); setValue(el, before); }, redo: () => el.remove()});
  } else if (el._new) {
    el._new = false;
    record({undo: () => el.remove(), redo: () => { pg.append(el); setValue(el, after); }});
  } else if (before !== after) {
    record({undo: () => setValue(el, before), redo: () => setValue(el, after)});
  }
}
function watch(el) {
  el._before = el.value;
  el.addEventListener('input', () => { refresh(el); setDirty(true); });
  el.addEventListener('blur', () => { if (el.isConnected) commit(el); });
  el.addEventListener('keydown', e => { if (e.key === 'Escape' || e.key === 'Enter') { e.stopPropagation(); el.blur(); } });
}
document.querySelectorAll('input.line').forEach(watch);

function addText(pg, x, y) {
  // Match the size and style of the nearest line on the page.
  let near = null, best = Infinity;
  pg.querySelectorAll('input.line').forEach(l => {
    const d = Math.abs(l.offsetTop + l.offsetHeight / 2 - y);
    if (d < best) { best = d; near = l; }
  });
  const size = near ? +near.dataset.size : 12, fam = near ? near.dataset.fam : 'sans', h = size * 1.2 * ZOOM;
  const el = document.createElement('input');
  el.className = 'added';
  el.size = 4;
  Object.assign(el.dataset, {p: pg.dataset.p, size, fam});
  Object.assign(el.style, {left: x + 'px', top: (y - h / 2) + 'px', height: h + 'px', lineHeight: h + 'px',
                           fontSize: size * ZOOM + 'px', fontFamily: FONTS[fam]});
  el._new = true;
  pg.append(el);
  watch(el);
  setTimeout(() => el.focus());
}

function drawBox(pg, x0, y0) {
  const box = document.createElement('div'), del = document.createElement('button');
  box.className = 'box';
  box.dataset.p = pg.dataset.p;
  del.textContent = '×';
  del.title = 'Remove';
  del.onclick = () => { box.remove(); record({undo: () => pg.append(box), redo: () => box.remove()}); };
  box.append(del);
  pg.append(box);
  const r = pg.getBoundingClientRect();
  const move = e => {
    const x = Math.min(Math.max(e.clientX - r.left, 0), r.width), y = Math.min(Math.max(e.clientY - r.top, 0), r.height);
    Object.assign(box.style, {left: Math.min(x0, x) + 'px', top: Math.min(y0, y) + 'px',
                              width: Math.abs(x - x0) + 'px', height: Math.abs(y - y0) + 'px'});
  };
  const up = () => {
    removeEventListener('mousemove', move);
    removeEventListener('mouseup', up);
    if (box.offsetWidth < 4 || box.offsetHeight < 4) box.remove();
    else record({undo: () => box.remove(), redo: () => pg.append(box)});
  };
  move({clientX: x0 + r.left, clientY: y0 + r.top});
  addEventListener('mousemove', move);
  addEventListener('mouseup', up);
}

document.querySelectorAll('.page').forEach(pg => pg.addEventListener('mousedown', e => {
  if (mode === 'edit' || e.target.closest('input, .box button')) return;
  e.preventDefault();
  const r = pg.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  if (mode === 'text') addText(pg, x, y); else drawBox(pg, x, y);
}));

// Find & replace, within each line of text.
const texts = () => [...document.querySelectorAll('.page input')];
function showFind(show) {
  $('find').hidden = !show;
  if (show) { $('q').focus(); $('q').select(); countMatches(); }
}
function countMatches() {
  const q = $('q').value;
  const n = q ? texts().reduce((n, el) => n + el.value.split(q).length - 1, 0) : 0;
  $('count').textContent = q ? `${n} match${n === 1 ? '' : 'es'}` : '';
}
$('q').oninput = countMatches;
$('findbtn').onclick = () => showFind($('find').hidden);
$('closefind').onclick = () => showFind(false);
$('replaceall').onclick = () => {
  const q = $('q').value;
  if (!q) return;
  let n = 0;
  const changes = [];
  texts().forEach(el => {
    const parts = el.value.split(q);
    if (parts.length < 2) return;
    n += parts.length - 1;
    changes.push([el, el.value, parts.join($('r').value)]);
  });
  changes.forEach(([el, , after]) => setValue(el, after));
  if (changes.length) record({undo: () => changes.forEach(([el, before]) => setValue(el, before)),
                              redo: () => changes.forEach(([el, , after]) => setValue(el, after))});
  $('count').textContent = `Replaced ${n}`;
};
$('find').addEventListener('keydown', e => {
  if (e.key === 'Escape') showFind(false);
  if (e.key === 'Enter') $('replaceall').click();
});

async function save(saveAs) {
  const px = v => parseFloat(v) / ZOOM;
  const edits = [...document.querySelectorAll('input.line.changed')].map(el => ({p: +el.dataset.p, i: +el.dataset.i, text: el.value}));
  const added = [...document.querySelectorAll('input.added')].filter(el => el.value.trim()).map(el => ({
    p: +el.dataset.p, text: el.value, size: +el.dataset.size, fam: el.dataset.fam,
    x: px(el.style.left) + 1 / ZOOM, y: px(el.style.top) + px(el.style.height) / 2 + 0.27 * el.dataset.size,
  }));
  const boxes = [...document.querySelectorAll('.box')].map(b => ({
    p: +b.dataset.p, x0: px(b.style.left), y0: px(b.style.top),
    x1: px(b.style.left) + px(b.style.width), y1: px(b.style.top) + px(b.style.height),
  }));
  $('status').textContent = savedPath && !saveAs ? 'Saving…' : 'Choose where to save…';
  const r = await (await fetch('/save', {method: 'POST', body: JSON.stringify({edits, added, boxes, saveAs})})).json();
  if (r.cancelled) return setDirty(dirty);
  if (r.error) return $('status').textContent = 'Error: ' + r.error;
  savedPath = r.path;
  $('dest').innerHTML = 'Saving to <b></b> <a href="#">Show in Finder</a>';
  $('dest').querySelector('b').textContent = savedPath;
  $('dest').querySelector('a').onclick = e => { e.preventDefault(); fetch('/reveal', {method: 'POST', body: '{}'}); };
  $('dest').hidden = false;
  $('save').textContent = 'Save';
  $('saveas').hidden = false;
  setDirty(false);
}
$('undo').onclick = undo;
$('redo').onclick = redo;
$('save').onclick = () => save(false);
$('saveas').onclick = () => save(true);
document.addEventListener('keydown', e => {
  const key = e.key.toLowerCase();
  if (e.metaKey && key === 's') { e.preventDefault(); save(e.shiftKey); }
  if (e.metaKey && key === 'f') { e.preventDefault(); showFind(true); }
  if ((e.metaKey || e.ctrlKey) && key === 'z' && !e.target.closest('#find')) {
    e.preventDefault();
    const el = document.activeElement;
    if (el && el.closest('.page')) { if (commit(el) === 'discarded') return; el.blur(); }
    e.shiftKey ? redo() : undo();
  }
  if (e.key === 'Escape' && mode !== 'edit') setMode('edit');
});
addEventListener('beforeunload', e => { if (dirty) e.preventDefault(); });
</script>"""

def main():
    global SRC, ORIGINAL
    usage = "Usage: tippex file.pdf\n       tippex --version"
    if sys.argv[1:] in (["-V"], ["--version"]):
        return print(f"tippex {__version__}")
    if sys.argv[1:] in (["-h"], ["--help"]):
        return print(usage)
    if len(sys.argv) != 2:
        sys.exit(usage)
    SRC = Path(sys.argv[1]).expanduser().resolve()
    if not SRC.is_file():
        sys.exit(f"No such file: {SRC}")
    ORIGINAL = SRC.read_bytes()
    start_save_panel()
    server = HTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Editing {SRC.name} at {url}\nPress Ctrl+C to quit.")
    threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
