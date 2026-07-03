---
name: library-organization
description: Use when adding, importing, moving, renaming, deleting, or organizing Library documents/folders. Prevents root clutter, duplicate folders, weak taxonomies, and unverified saves.
---

# Before changing Library structure

- Before changing structure, build a sufficient map of the Library: list root, then list any candidate folders whose names, descriptions, or search results could contain the destination.
- Search existing titles and folders before creating new ones when the intended name may already exist.
- Ask the user only when the destination, retention policy, or taxonomy would materially change the result and cannot be inferred.
- Do not create folders for a single temporary note unless the user asked for that structure or more related items are expected.

# Folder design

- Reuse existing folders when they reasonably fit.
- Create a new folder only for a durable category: project, topic, corpus, source type, experiment, writing output, or review stage.
- Use clear noun phrases. Avoid names such as `misc`, `temp`, `new`, `stuff`, `references2`, `papers old`, or date-only folders unless the project already uses that convention.
- Keep siblings at the same abstraction level. Do not mix broad topics, individual papers, and workflow states in one folder level.
- Prefer 2-3 meaningful levels over deep nesting. If a path would exceed 4 levels, reconsider the taxonomy.

# Adding materials

- Put new documents into the most specific existing suitable folder.
- For browser pages, save with `library_new(content_type="tab")` only after confirming the page is the relevant source, not a search result page, login page, navigation page, or noisy index, and make sure **all intended content is loaded and expanded**.
- Do not use `library_new(content_type="text")` for generated notes unless the user explicitly asks to save them to Library.
- For files, use `library_new(content_type="file")` when the file is already available by path. Use a title that identifies the material, not just the filename if the filename is opaque.
- After adding, note that AI metadata extraction and RAG indexing are asynchronous. Do not assume the document is immediately semantically searchable.

# Moving and renaming

- Before `library_mv`, list the destination folder and check for title conflicts.
- Move related batches together only when all items share the same destination and category.
- Rename only to improve identification, consistency, or collision avoidance. Preserve meaningful original titles for papers and primary sources.
- For document metadata edits, use exact `old_string`/`new_string` replacement only after reading enough content to make `old_string` unique.

# Deleting

- Treat deletion as destructive. Delete only when the user explicitly asked, the item is clearly duplicate/unwanted, or cleanup is part of the requested task.
- Remember that deleting a folder deletes its contents recursively.
- Prefer moving questionable items into a clearer folder over deleting them.