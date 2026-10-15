# Domain docs: single-context

Layout: single-context.

- Glossary: `GLOSSARY.md` at repo root (create lazily when first term resolves).
- ADRs: `docs/adr/<NNNN>-<slug>.md` (create lazily when first hard-to-reverse decision lands).
- No `GLOSSARY-MAP.md`; no per-package glossaries.

Consumer rules:

- Use glossary vocabulary in tickets/specs; respect ADRs in touched area.
- Challenge terms that conflict with `GLOSSARY.md`; sharpen fuzzy language; update `GLOSSARY.md` inline when terms resolve (see `domain-modeling` skill).
- Offer ADRs sparingly (hard-to-reverse + surprising + real trade-off).
