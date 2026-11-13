---
name: teach
description: Run an interactive tutoring session on a concept from the knowledge graph, grounded in the learner's PDF books, ending with a quiz, a mastery update and a journal entry. Use when the user says "teach me X", "quiz me", "what should I learn next", "review", or "study session".
---

# Teach skill

Follow `.github/copilot-instructions.md` ("How to teach"). Concrete procedure:

1. **Pick the topic**
   - If the user named a topic, find its slug: `.\kg.cmd search "<topic>" --kind note --limit 5`.
     If no note exists, create one with `.\kg.cmd new` (choose domain + minimal direct prereqs).
   - If not, run `.\kg.cmd review` (due items first) and `.\kg.cmd next` and offer 3 choices via ask_user.
2. **Check readiness** — open the note; for each prereq with mastery < 3 ask whether to do a quick refresher first.
3. **Gather sources** — `.\kg.cmd search "<key terms>" --kind book --limit 8`. Open the most relevant book pages
   only through search snippets; cite as `book-slug p.N`. If nothing is indexed, teach from general knowledge and
   say so.
4. **Teach in small loops** (explain → example → question). Wait for the learner's answer before continuing.
   Prefer runnable code (Python/C/Rust) or worked math. Keep each message short.
5. **Quiz** — 3 questions: recall, application, transfer. Grade answers and explain mistakes.
6. **Update the graph**
   - Propose a mastery level (0–5 scale) and confirm with the learner, then `.\kg.cmd rate <slug> <n>`.
   - Fill `## Key ideas`, add new `## Check yourself` questions, append `sources`.
   - `.\kg.cmd check` (fix any errors) then `.\kg.cmd build`.
7. **Journal** — append to `journal/<today>.md`:
   ```
   ## <HH:MM> <Concept title>
   - Covered: …
   - Mastery: <old> → <new>
   - Open questions: …
   - Next: <slug from kg next>
   ```
