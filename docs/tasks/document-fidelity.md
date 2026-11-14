# Document fidelity — reading attached files as more than their text

**Status: PARKED 2026-11-14.** Nothing built. Raised by Mike once Word,
Excel and PowerPoint attachments worked (`share-and-files.md`). The trigger
is a felt symptom: Marlow missing something in a real file that was there on
the page.

## What Marlow gets today

PDFs and images go to Read as they are, and Read sees a PDF's pages as text
and as images. Office files are converted to Markdown in a sandbox
(`server/convert.py`):

- **Word:** text and tables. Pictures, charts, tracked changes and comments
  are dropped, so "what did the reviewer change?" has no answer.
- **Excel:** each cell's value as last saved (`data_only=True`), not its
  formula, and no charts. Right for "what's the total?", wrong for "why is
  this number off?".
- **PowerPoint:** each slide's text and speaker notes. A deck's meaning is
  often in its layout, charts and pictures, which are gone.

Markdown is still the right default. For a memo or a report it is cleaner
and cheaper than any page image.

## Ways to go further, when it is needed

1. **Render to PDF beside the text.** Headless LibreOffice in the same
   sandbox turns the file into a PDF, and Read sees the pages as Marlow
   would. Offer it beside the Markdown, not instead of it, and maybe only
   for decks and image-heavy documents. Cost: a large dependency on every
   box, a slower conversion, and several times the tokens a page.
2. **More from the libraries already in use:** formulas next to values,
   comments and tracked changes as Markdown notes, image placeholders that
   say a picture was there. Cheap, and it covers the "why is this number
   off" and "what changed" cases without layout.
3. **Ride on the CLI.** If `claude -p` gains a native reader for Office
   files, as Read has for PDF, pass the original and drop the conversion
   for that kind. Check the CLI's release notes before building 1.
4. **An MCP server** that reads documents, if a good one exists by then. It
   would run under the same rules as any other: `boundaries.AGENT_CONFIG_PATHS`,
   and egress.

Whatever is chosen, the original file stays the record: it is what the
transcript keeps, and a better reader later can go back to it.
