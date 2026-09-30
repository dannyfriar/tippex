# Tippex

The simplest possible PDF text editor, for macOS.

## Install

```
uv tool install tippex
```

or `pipx install tippex`, or `uv pip install tippex` into an environment of your choice. Upgrade with `uv tool upgrade tippex`.

## Use

```
tippex path/to/file.pdf
tippex --version
```

A browser tab opens showing the PDF. Pick a tool from the toolbar:

- **Edit text**: click any line and type over it.
- **Add text**: click anywhere on a page to add new text, sized and styled like the nearest line.
- **White-out**: drag a box over anything (text, a signature, an image) to white it out. The content underneath is removed from the PDF, not just covered. Hover a box and click × to remove it.
- **Find & Replace** (⌘F): replace text across the whole document, within each line.
- **Undo / Redo** (⌘Z or Ctrl+Z, ⇧⌘Z): steps back through line edits, added text, white-out boxes and replacements.

Edited text is drawn in the PDF's own font whenever every character you typed already appears in that font in the document; otherwise Tippex uses the closest standard font (Helvetica, Times or Courier, keeping bold and italic).

The first time you press **Save…** (or ⌘S), the macOS Save dialog asks where to put the file, suggesting `file-tippexed.pdf` next to the original. After that, **Save** (⌘S) writes to the same file, and **Save As…** (⇧⌘S) picks a new one. Once saved, a banner under the toolbar shows where the file is, with a link to show it in Finder. Press Ctrl+C in Terminal to quit.

## Limitations

- New characters the document never uses in that font fall back to a standard font.
- Editing is one line at a time; long replacements don't wrap.
- Scanned PDFs have no text layer, so there is nothing to edit.
