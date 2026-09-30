# Tippex

The simplest possible PDF text editor, for macOS.

## Install

```
curl -fsSL https://raw.githubusercontent.com/dannyfriar/tippex/main/install.sh | sh
```

While the repo is private, clone over SSH and run the installer from the clone:

```
git clone git@github.com:dannyfriar/tippex.git ~/.tippex && ~/.tippex/install.sh
```

## Use

```
tippex path/to/file.pdf
```

A browser tab opens showing the PDF. Click any line of text, type over it, and press **Save**. The result is written next to the original as `file-tippexed.pdf`; the original is never modified. Press Ctrl+C in Terminal to quit.

The first run creates a local `.venv` and installs [PyMuPDF](https://pymupdf.readthedocs.io/).

## Limitations

- Edited lines are redrawn in Helvetica (bold if the original was bold) at the original size and color.
- Editing is one line at a time; long replacements don't wrap.
- Scanned PDFs have no text layer, so there is nothing to edit.
