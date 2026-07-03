---
name: library-search
description: Use when searching, reading, citing, or verifying Library materials. Guides keyword vs semantic search, scoped search, pagination, original-content verification, and evidence handoffs.
---

# Choose search mode

- Use `library_search(mode="keyword")` for exact terms: titles, author names, system names, abbreviations, identifiers, quoted phrases, equations, dataset names, URLs, and distinctive terminology.
- Use `library_search(mode="semantic")` for meaning-based discovery: concepts, mechanisms, related work, paraphrases, fuzzy topics, and questions where exact wording is unknown.
- Use both modes when recall matters. Start semantic for concepts, then keyword-search names and terms discovered from semantic hits.
- Restrict with `parent_id` when the relevant folder is known. Use `library_ls` first when folder structure is unknown.

# Interpret results

- Keyword mode returns document-level matches across title, description, and content. It is paginated at 50 results per page; request the next page only when the current page plausibly contains useful but incomplete coverage.
- Semantic mode returns top relevant chunks from hybrid retrieval and may return multiple chunks from the same document. Scores rank candidates; they are not evidence by themselves.
- Search snippets are only locators. Do not cite or rely on a snippet for a key claim until `library_get` has read the relevant field or surrounding content.
- A document may be unavailable or incomplete while processing, indexing, failed, or pending.

# Query discipline

- Use short, information-rich queries. Avoid long multi-clause prompts in keyword mode.
- For keyword mode, try exact variants: singular/plural, hyphenation, capitalization-insensitive names, acronym/full name, Chinese/English terms, and spelling variants.
- For semantic mode, write one natural-language description of the information need. If results are broad, narrow by mechanism, domain, method, time period, or expected terminology. Make the query explicit and domain-specific; avoid generic prompts or vague phrasing.
- When no result is found, report the exact modes, folders, and query variants tried. Do not infer absence from one failed search.

# Reading documents

- Use `library_get` with `field=["description","keywords"]` to triage a candidate cheaply.
- Use `library_get(field="content", offset=..., limit=...)` to read the relevant region. Use line numbers from search matches as the starting point.
- Read adjacent lines when a claim depends on definitions, assumptions, limitations, methods, results, or comparisons.
- For large documents, read only the needed regions first; expand only when the local context is insufficient.
- When comparing documents, keep a compact evidence matrix: document ID, title, claim, location, and confidence.
- For broad Library exploration likely to require more than 10 tool calls, delegate to an `explore` agent with scope, search modes, stop conditions, and evidence format.
- For deep study of 6+ long documents, use one fork-mode subagent per document; ask each to read the full document unless the user explicitly requested specific sections only.
