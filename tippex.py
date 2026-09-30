"""Tippex: minimal PDF text editor. Usage: ./tippex file.pdf"""
import base64, json, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pymupdf

SRC = Path(sys.argv[1]).expanduser().resolve()
OUT = SRC.with_name(SRC.stem + "-tippexed.pdf")
ZOOM = 1.5


def lines(page):
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if spans:
                yield line["bbox"], "".join(s["text"] for s in line["spans"]), spans[0]


def build_page():
    doc = pymupdf.open(SRC)
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
    return TEMPLATE.replace("{{NAME}}", escape(SRC.name)).replace("{{PAGES}}", "".join(html))


def escape(s):
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def save(edits):
    doc = pymupdf.open(SRC)
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
    doc.save(OUT, garbage=3, deflate=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.reply(build_page().encode(), "text/html")

    def do_POST(self):
        edits = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            save(edits)
            self.reply(f"Saved to {OUT}".encode(), "text/plain")
        except Exception as e:
            self.reply(f"Error: {e}".encode(), "text/plain")

    def reply(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


TEMPLATE = """<!doctype html><meta charset="utf-8"><title>Tippex – {{NAME}}</title>
<style>
body{margin:0;background:#888;font-family:-apple-system,sans-serif}
header{position:sticky;top:0;z-index:9;background:#222;color:#fff;padding:10px 16px;display:flex;gap:12px;align-items:center}
button{font-size:15px;padding:6px 16px}
.page{position:relative;width:max-content;margin:20px auto;box-shadow:0 2px 10px #0006}
.page img{display:block}
.page input{position:absolute;box-sizing:border-box;border:1px solid transparent;background:transparent;color:transparent;padding:0;font-family:Helvetica,Arial,sans-serif;outline:none}
.page input:hover{border-color:#39f8}
.page input:focus,.page input.changed{background:#fff;color:#000;border-color:#39f}
</style>
<header><b>Tippex</b><span>{{NAME}}</span><span>Click any text to edit it.</span><button id="save">Save</button><span id="msg"></span></header>
{{PAGES}}
<script>
document.querySelectorAll('.page input').forEach(el =>
  el.addEventListener('input', () => el.classList.toggle('changed', el.value !== el.dataset.orig)));
document.getElementById('save').onclick = async () => {
  const edits = [...document.querySelectorAll('input.changed')].map(el => ({p: +el.dataset.p, i: +el.dataset.i, text: el.value}));
  if (!edits.length) return msg.textContent = 'No changes.';
  msg.textContent = 'Saving...';
  msg.textContent = await (await fetch('/', {method: 'POST', body: JSON.stringify(edits)})).text();
};
</script>"""

if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Editing {SRC.name} at {url}\nEdits are saved to {OUT.name}. Press Ctrl+C to quit.")
    threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
