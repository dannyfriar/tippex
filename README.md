# Tippex

The simplest possible PDF text editor, for macOS.

## Install

```
uv tool install tippex
```

or `pipx install tippex`, or `uv pip install tippex` into an environment of your choice.

Without uv or pipx:

```
curl -fsSL https://raw.githubusercontent.com/dannyfriar/tippex/main/install.sh | sh
```

## Use

```
tippex path/to/file.pdf
tippex --version
```

A browser tab opens showing the PDF. Click any line of text and type over it.

The first time you press **Save…** (or ⌘S), the macOS Save dialog asks where to put the file, suggesting `file-tippexed.pdf` next to the original. After that, **Save** (⌘S) writes to the same file, and **Save As…** (⇧⌘S) picks a new one. Once saved, a banner under the toolbar shows where the file is, with a link to show it in Finder. Press Ctrl+C in Terminal to quit.

The first run creates a local `.venv` and installs [PyMuPDF](https://pymupdf.readthedocs.io/).

## Limitations

- Edited lines are redrawn in Helvetica (bold if the original was bold) at the original size and color.
- Editing is one line at a time; long replacements don't wrap.
- Scanned PDFs have no text layer, so there is nothing to edit.
