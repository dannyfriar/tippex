"""Tippex: minimal PDF text editor for macOS. Usage: tippex file.pdf"""
import base64, json, subprocess, sys, threading, urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pymupdf

__version__ = "0.0.2"

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


def build_page():
    doc = pymupdf.open(stream=ORIGINAL)
    html = []
    for pno, page in enumerate(doc):
        png = base64.b64encode(page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM)).tobytes("png")).decode()
        boxes = []
        for i, (bbox, text, span) in enumerate(lines(page)):
            x0, y0, x1, y1 = (v * ZOOM for v in bbox)
            boxes.append(
                f'<input data-p="{pno}" data-i="{i}" value="{escape(text)}" data-orig="{escape(text)}" '
                f'style="left:{x0}px;top:{y0}px;width:{x1 - x0 + 40}px;height:{y1 - y0}px;font-size:{span["size"] * ZOOM * 0.9}px">'
            )
        html.append(f'<div class="page"><img src="data:image/png;base64,{png}">{"".join(boxes)}</div>')
    return TEMPLATE.replace("{{LOGO}}", LOGO).replace("{{FAVICON}}", FAVICON).replace("{{VERSION}}", __version__).replace("{{NAME}}", escape(SRC.name)).replace("{{PATH}}", escape(str(SRC))).replace("{{PAGES}}", "".join(html))


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


def save(edits, path):
    doc = pymupdf.open(stream=ORIGINAL)
    for pno in {e["p"] for e in edits}:
        page = doc[pno]
        page_lines = list(lines(page))
        todo = [(page_lines[e["i"]], e["text"]) for e in edits if e["p"] == pno]
        for (bbox, _, _), _ in todo:
            page.add_redact_annot(bbox, fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        for (bbox, _, span), text in todo:
            c = span["color"]
            color = ((c >> 16) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)
            bold = "bold" in span["font"].lower()
            page.insert_text(span["origin"], text, fontsize=span["size"], fontname="hebo" if bold else "helv", color=color)
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
            save(body["edits"], path)
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
#dest{position:sticky;top:52px;z-index:8;background:#fffbe6;color:#333;padding:8px 16px;font-size:13px;border-bottom:1px solid #e5d98a;word-break:break-all}
#dest a{margin-left:8px;color:#06c}
.page{position:relative;width:max-content;margin:20px auto;box-shadow:0 1px 3px #0002,0 6px 24px #0000001a}
.page img{display:block}
.page input{position:absolute;box-sizing:border-box;border:1px solid transparent;background:transparent;color:transparent;padding:0;font-family:Helvetica,Arial,sans-serif;outline:none}
.page input:hover{border-color:#39f8}
.page input:focus,.page input.changed{background:#fff;color:#000;border-color:#39f}
</style>
<header>
  <span class="brand">{{LOGO}}Tippex<span class="version">v{{VERSION}}</span></span><span class="dim path" title="Editing {{PATH}}">{{PATH}}</span>
  <span class="grow"></span>
  <span id="status" class="dim"></span>
  <button id="save">Save…</button><button id="saveas" hidden>Save As…</button>
</header>
<div id="dest" hidden></div>
{{PAGES}}
<script>
let savedPath = null, dirty = false;
const $ = id => document.getElementById(id);
const setDirty = d => { dirty = d; $('status').textContent = d ? 'Unsaved changes' : (savedPath ? 'All changes saved' : ''); };

document.querySelectorAll('.page input').forEach(el => el.addEventListener('input', () => {
  el.classList.toggle('changed', el.value !== el.dataset.orig);
  setDirty(true);
}));

async function save(saveAs) {
  const edits = [...document.querySelectorAll('input.changed')].map(el => ({p: +el.dataset.p, i: +el.dataset.i, text: el.value}));
  $('status').textContent = savedPath && !saveAs ? 'Saving…' : 'Choose where to save…';
  const r = await (await fetch('/save', {method: 'POST', body: JSON.stringify({edits, saveAs})})).json();
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
$('save').onclick = () => save(false);
$('saveas').onclick = () => save(true);
document.addEventListener('keydown', e => {
  if (e.metaKey && e.key.toLowerCase() === 's') { e.preventDefault(); save(e.shiftKey); }
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
