---
name: study-book
description: Ingest new PDF books into the knowledge graph and connect them to concepts — sets the book's domain, maps chapters to concept notes via `covers`, and creates missing concept notes. Use when the user adds PDFs, says "ingest my books", "add this book", or "what does book X cover".
---

# Study-book skill

1. Run `.\kg.cmd ingest`. Report each new book slug, page count and any "no text layer" warning
   (scanned PDFs need OCR first, e.g. `ocrmypdf in.pdf out.pdf`, then re-ingest).
2. For each **new** book note in `vault/books/`:
   - Read its `## Chapters` outline. If empty, sample the text with
     `.\kg.cmd search "chapter" --ref <book-slug> --limit 20` to infer structure.
   - Set `domain` to the best fit (`ai`, `security`, `systems`, `math`, `cs`).
   - Map chapters to existing concept slugs (`.\kg.cmd search "<chapter topic>" --kind note`). Add matches to
     `covers` and to each concept's `sources` as `<book-slug> p.<page>`.
   - For important chapters with no matching concept, propose new concept notes (title, domain, prereqs) and
     create them with `.\kg.cmd new` after the learner agrees.
   - If the book suggests a natural order, propose a reading plan (chapters ↔ roadmap stages).
3. `.\kg.cmd check` → fix errors → `.\kg.cmd build`.
4. Summarise: books added, concepts linked, concepts created, suggested first chapter based on `kg next`.

Never copy book text into notes beyond a short quote; write summaries in your own words.
