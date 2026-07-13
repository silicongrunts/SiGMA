/**
 * mathGuard — protect math fragments from being mangled by the markdown parser.
 *
 * Problem: marked (with `breaks: true`) turns single newlines into `<br>`, and
 * inline `\n` inside a multi-line display-math block (`$$\nA = ...\n$$`) gets a
 * `<br>` between the opening `$$` and the body. KaTeX auto-render only
 * concatenates *consecutive sibling text nodes*, so the `<br>` splits the
 * delimiter across nodes and the formula never matches. marked can also eat
 * `_`/`*` inside math as emphasis, or escape `&`/`<`.
 *
 * Solution: lift math out of the raw markdown before marked runs, substitute a
 * plain-text placeholder that marked treats as ordinary text (no markdown chars
 * → no interpretation, no `<br>` because the placeholder has no newline), then
 * stitch the original math back into the rendered HTML string. The browser HTML
 * parser collapses the literal `\n` inside the restored text to whitespace, so
 * each math block becomes ONE text node — exactly what auto-render needs.
 *
 * Used by both ChatShared.MarkdownContent and Preview's markdown path so the
 * two render math identically.
 */

// Order matters: `$$...$$` (display) first so a `$$` pair is not mis-read as
// two single `$...$`. Display math allows newlines; inline math does not.
const MATH_RE = /\$\$[\s\S]*?\$\$|\$[^\$\n]+\$/g

// Pure ASCII letters/digits only — no `_`, `*`, `#`, `[`, `<`, `&`, newline…
// so marked never interprets it as markdown, never inserts a `<br>` inside it,
// and DOMPurify leaves it alone (plain text). The random hex makes accidental
// collisions with user text effectively impossible.
const PH_PREFIX = 'xMathToken'
const PH_SUFFIX = 'xEnd'
const PH_RE = /xMathToken([0-9a-f]+)xEnd/g

function randomHex(n) {
  let s = ''
  const chars = '0123456789abcdef'
  for (let i = 0; i < n; i++) s += chars[Math.floor(Math.random() * 16)]
  return s
}

/**
 * Strip math fragments from raw markdown, returning:
 *  - `text`:  placeholder-filled text (what marked should parse).
 *  - `map`:   id → original math fragment, for restoreMath.
 *  - `spans`: per placeholder, its offsets in BOTH the placeholder text and
 *             the original markdown. Used by Preview's block-map lexer to map
 *             placeholder-text offsets back to real source line numbers (a
 *             multi-line `$$...$$` collapses to a single-line placeholder, so
 *             placeholder offsets ≠ source offsets). `phStart/phEnd` are in
 *             the placeholder text; `srcStart`/`srcEnd` are in `markdown`
 *             (srcEnd exclusive, covering the whole `$$...$$` block).
 *
 * Callers that only need `{ text, map }` simply ignore `spans`.
 */
export function extractMath(markdown) {
  if (!markdown || typeof markdown !== 'string') return { text: markdown ?? '', map: null, spans: [] }
  const map = new Map()
  const spans = []
  const re = new RegExp(MATH_RE.source, 'g')
  let counter = 0
  let out = ''
  let cursor = 0      // read cursor in original markdown
  let phCursor = 0    // write cursor in placeholder text
  let m
  while ((m = re.exec(markdown)) !== null) {
    const srcStart = m.index
    const srcEnd = srcStart + m[0].length
    // copy the verbatim gap before this math block
    const gap = markdown.slice(cursor, srcStart)
    out += gap
    phCursor += gap.length
    // emit placeholder
    const id = randomHex(6) + counter.toString(16)
    const ph = `${PH_PREFIX}${id}${PH_SUFFIX}`
    map.set(id, m[0])
    spans.push({ phStart: phCursor, phEnd: phCursor + ph.length, srcStart, srcEnd })
    out += ph
    phCursor += ph.length
    cursor = srcEnd
    counter++
    re.lastIndex = srcEnd
  }
  // trailing tail
  if (counter === 0) return { text: markdown, map: null, spans: [] }
  out += markdown.slice(cursor)
  return { text: out, map, spans }
}

/**
 * Inverse of extractMath: substitute the original math fragments back into the
 * HTML string produced by marked.parse. Placeholders are replaced with the raw
 * math text (including its literal newlines, which the HTML parser collapses to
 * whitespace — keeping each block a single text node for KaTeX auto-render).
 */
export function restoreMath(html, map) {
  if (!html || !map) return html
  return html.replace(PH_RE, (full, id) => map.get(id) ?? full)
}
