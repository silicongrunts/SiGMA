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

/**
 * Mark inline `$...$` formulas that truly overflow the column so each one gets
 * its own horizontal scrollbar, while leaving short/wrappable formulas as plain
 * baseline-aligned inline content (no spurious scrollbar, no height shift).
 *
 * Why this exists: KaTeX renders `.katex` inline. A single very wide inline
 * formula (e.g. a long fraction or `\text{...}` run with no break point) would
 * otherwise push past the column boundary. The CSS rule `.katex { display:
 * inline-block; max-width: 100% }` already constrains the box so nothing spills
 * out, and most formulas simply wrap inside it. But the few that CANNOT wrap
 * need a scrollbar so the user can still reach the clipped part.
 *
 * Why this is a measured JS step and not `overflow-x:auto` on every `.katex`:
 * KaTeX's own layout reports `scrollWidth > clientWidth` by ~2px for some
 * glyphs (notably `\sqrt` whose rule extends past the measured box). A blanket
 * `overflow-x:auto` therefore shows a phantom scrollbar on short square-root
 * formulas. We instead detect the real overflow: an inline-block only reaches
 * its `max-width:100%` cap (== the available line width) when its content is
 * too wide to shrink-to-fit. So a formula needs a scrollbar precisely when its
 * rendered width has hit the cap AND its scrollWidth exceeds that capped width.
 * Short formulas never reach the cap, so the sqrt artifact never triggers.
 *
 * Display math (`$$...$$`, wrapped in `.katex-display`) already scrolls via its
 * own CSS rule and is skipped here.
 *
 * Run this AFTER `renderMathInElement` so the `.katex` nodes exist.
 */

// Block-level ancestors a formula may sit in. Climbing to one of these (rather
// than the immediate parent) matters when the formula is wrapped in an inline
// element like <strong>/<em>/<a> — its width is only a line fragment, not the
// column, so measuring against it would misjudge whether the formula overflows.
const BLOCK_ANCESTORS = 'p, li, td, th, blockquote, div, section, article'
const MATH_SCROLL_CLASS = 'sigma-math-scroll'

export function applyMathOverflow(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return
  const formulas = root.querySelectorAll('.katex')
  formulas.forEach((katex) => {
    // Reset first; re-measure on every call so width changes (resize, streaming
    // re-render) re-evaluate correctly.
    katex.classList.remove(MATH_SCROLL_CLASS)
    // Skip display math — handled by the dedicated .katex-display rule.
    if (katex.closest('.katex-display')) return
    const container = katex.closest(BLOCK_ANCESTORS)
    if (!container) return
    const available = container.getBoundingClientRect().width
    if (!Number.isFinite(available) || available <= 0) return
    const rendered = katex.getBoundingClientRect().width
    // "Constrained" = the box hit its max-width cap (rendered ≈ available),
    // meaning shrink-to-fit could not accommodate the content.
    const constrained = Math.abs(rendered - available) < 4
    // Real overflow only when constrained AND natural content exceeds the box.
    // The +2 tolerance absorbs the KaTeX sqrt sub-pixel artifact for the rare
    // case where a sqrt formula happens to sit right at the column edge.
    if (constrained && katex.scrollWidth > katex.clientWidth + 2) {
      katex.classList.add(MATH_SCROLL_CLASS)
    }
  })
}
