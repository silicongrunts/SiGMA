// Memory usage tag helpers shared by the markdown renderer and chat chips.
//
// The backend keeps <memory_usage file="x.md">note</memory_usage> tags in
// stored assistant messages (the model re-reads its own past citations for
// behavioral consistency); the UI strips them from display and surfaces a
// compact chip instead. Also handles an in-flight trailing tag while the
// reply is still streaming (open tag not yet closed).

// The tag name may be followed by attributes or stand bare — a malformed
// no-attribute tag must be hidden from display just like a well-formed one.
const COMPLETE_TAG_RE = /<memory_usage(?:\s[^>]*)?>(.*?)<\/memory_usage>\s*/gs
// Mirrors the backend's recording regex: the file attribute must come right
// after the tag name (extra attributes after it are tolerated). Tags with
// a missing or reordered file attribute are stripped from display but never
// recorded, so they must not produce a chip the usage log cannot back.
const RECORDABLE_TAG_RE = /<memory_usage\s+file=(?:"([^"]*)"|'([^']*)')[^>]*>([\s\S]*?)<\/memory_usage>/g
// An open tag that never closed: everything after it is the note still
// streaming in (complete tags, if any later, were already removed above),
// so it all goes — mirrors the backend's display semantics.
const OPEN_TAIL_RE = /<memory_usage(?:\s[^>]*)?>[\s\S]*$/
// Trailing fragment whose opening tag has not closed with ">" yet — hides
// raw markup while the tag's attributes are still streaming in.
const PARTIAL_TAIL_RE = /<memory_usage(?:\s[^>]*)?$/
// A closing tag with no opener (or whose pair was removed above) — stray
// markup, dropped.
const STRAY_CLOSE_RE = /<\/memory_usage>\s*/g

// Mirrors the backend's per-reply recording cap: only the first tags of a
// reply are logged, so the chip must not claim more than that.
const MAX_USAGES_PER_TURN = 3

export function stripMemoryUsageTags(text) {
    if (!text) return text
    return text
        .replace(COMPLETE_TAG_RE, '')
        .replace(OPEN_TAIL_RE, '')
        .replace(STRAY_CLOSE_RE, '')
        .replace(PARTIAL_TAIL_RE, '')
}

export function extractMemoryUsages(text) {
    if (!text) return []
    const usages = []
    for (const match of text.matchAll(RECORDABLE_TAG_RE)) {
        const file = (match[1] ?? match[2] ?? '').trim()
        const note = (match[3] || '').replace(/\s+/g, ' ').trim()
        // Mirror the backend's post-regex exclusions: a whitespace-only name
        // and the MEMORY.md index are never recorded, so they must not show
        // a chip either. Whether the file actually exists is checked
        // server-side only.
        const base = file.slice(file.lastIndexOf('/') + 1)
        if (file && base !== 'MEMORY.md' && note) usages.push({ file, note })
        if (usages.length >= MAX_USAGES_PER_TURN) break
    }
    return usages
}
