/**
 * Inline formatting (bold / italic / code) for the CodeMirror editors.
 *
 * The core is a pure function that computes one atomic change (a single undo
 * step) for toggling a style around the selection or the word at the cursor.
 * The exported keymap builder turns it into CodeMirror key bindings. Marker
 * syntax is data-driven so Markdown and LaTeX share the same toggle logic.
 *
 * Toggle rules:
 * - A selection with the markers adjacent outside, or with the markers
 *   included in the selection, is unwrapped; any other non-empty selection is
 *   wrapped, keeping surrounding whitespace outside the markers.
 * - A marker match only counts when it is not part of a longer marker run:
 *   one '*' of '**bold**' must not toggle italic (see `guard`).
 * - Empty selection: an adjacent empty marker pair (e.g. `**|**`) is removed;
 *   otherwise the word at the cursor acts as the selection; when there is no
 *   word, an empty pair is inserted with the cursor between the markers.
 */

// Cap for word extension. CJK prose has no spaces, so the run between two
// punctuation marks can span a whole sentence; toggling such a run when the
// user only meant to format a nearby word would be surprising, so long runs
// fall back to inserting an empty marker pair.
const MAX_WORD_EXTEND = 20

// `pairs`: marker pairs to detect when toggling; the first pair is also the
// one emitted when wrapping (so `_italic_` input toggles off to `*` output).
// `guard`: characters that, when bordering a marker match, mark it as part of
// a longer marker run. `wordBreak`: characters that end a word for cursor
// word extension (whitespace, other inline markers, common punctuation).
const LANGUAGES = {
  markdown: {
    wordBreak: /[\s*_`~[\]()<>.,;:!?，。、；：！？（）【】《》“”‘’…—]/,
    bold: { pairs: [['**', '**'], ['__', '__']], guard: '*_' },
    italic: { pairs: [['*', '*'], ['_', '_']], guard: '*_' },
    code: { pairs: [['`', '`']], guard: '`' },
  },
  latex: {
    wordBreak: /[\s{}\\$&#%_^~[\].,;:!?，。、；：！？（）【】《》“”‘’…—]/,
    bold: { pairs: [['\\textbf{', '}']], guard: '' },
    italic: { pairs: [['\\textit{', '}'], ['\\emph{', '}']], guard: '' },
    code: { pairs: [['\\texttt{', '}']], guard: '' },
  },
}

/** True when the marker match [from, to) borders a longer marker run. */
function insideLongerRun(text, from, to, style) {
  const before = from > 0 ? text[from - 1] : ''
  const after = to < text.length ? text[to] : ''
  return Boolean(before && style.guard.includes(before)) || Boolean(after && style.guard.includes(after))
}

/** Expand pos to the maximal run of non-wordBreak characters around it. */
function expandWordAt(text, pos, wordBreak) {
  let from = pos
  let to = pos
  while (from > 0 && !wordBreak.test(text[from - 1])) from -= 1
  while (to < text.length && !wordBreak.test(text[to])) to += 1
  return { from, to }
}

/** Toggle `style` around the non-empty range [from, to) of `text`. */
function toggleRange(text, from, to, style) {
  const selected = text.slice(from, to)

  // Markers adjacent outside the selection: remove them, keep content selected.
  for (const [open, close] of style.pairs) {
    const openFrom = from - open.length
    if (openFrom >= 0 && text.startsWith(open, openFrom) && text.startsWith(close, to)
      && !insideLongerRun(text, openFrom, to + close.length, style)) {
      return { from: openFrom, to: to + close.length, insert: selected, anchor: openFrom, head: openFrom + selected.length }
    }
  }

  // Markers included in the selection: strip them, keep the inner text selected.
  // The char right inside each matched marker must not extend it into a longer
  // marker run, or e.g. italic on a selected '**bold**' would strip one '*'
  // per side and corrupt the bold into italic.
  for (const [open, close] of style.pairs) {
    const innerStart = open.length
    const innerEnd = selected.length - close.length
    if (innerEnd < innerStart) continue
    if (!selected.startsWith(open) || !selected.endsWith(close)) continue
    const extendsRun = innerEnd > innerStart
      && (style.guard.includes(selected[innerStart]) || style.guard.includes(selected[innerEnd - 1]))
    if (!extendsRun && !insideLongerRun(text, from, to, style)) {
      const inner = selected.slice(innerStart, innerEnd)
      return { from, to, insert: inner, anchor: from, head: from + inner.length }
    }
  }

  // Wrap, keeping surrounding whitespace outside the markers.
  const leading = selected.match(/^\s*/)[0].length
  const trailing = selected.match(/\s*$/)[0].length
  let coreFrom = from + leading
  let coreTo = to - trailing
  if (coreTo <= coreFrom) { coreFrom = from; coreTo = to }
  const core = text.slice(coreFrom, coreTo)
  const [open, close] = style.pairs[0]
  const insert = selected.slice(0, coreFrom - from) + open + core + close + selected.slice(coreTo - from)
  return { from, to, insert, anchor: coreFrom + open.length, head: coreFrom + open.length + core.length }
}

/**
 * Compute the change that toggles `styleName` in `language` for the selection
 * [selFrom, selTo) of `text`. Returns the atomic change as
 * { from, to, insert, anchor, head }, or null when the style is unknown.
 */
export function computeFormatChange(text, selFrom, selTo, styleName, language) {
  const lang = LANGUAGES[language]
  const style = lang?.[styleName]
  if (!style) return null
  const [open, close] = style.pairs[0]

  if (selFrom !== selTo) return toggleRange(text, selFrom, selTo, style)

  // Empty selection: remove an adjacent empty marker pair, e.g. `**|**`.
  for (const [pairOpen, pairClose] of style.pairs) {
    const openFrom = selFrom - pairOpen.length
    if (openFrom >= 0 && text.startsWith(pairOpen, openFrom) && text.startsWith(pairClose, selFrom)
      && !insideLongerRun(text, openFrom, selFrom + pairClose.length, style)) {
      return { from: openFrom, to: selFrom + pairClose.length, insert: '', anchor: openFrom, head: openFrom }
    }
  }

  // Otherwise toggle the word at the cursor.
  const { from, to } = expandWordAt(text, selFrom, lang.wordBreak)
  if (to > from && to - from <= MAX_WORD_EXTEND) {
    return toggleRange(text, from, to, style)
  }

  // No word to extend (whitespace, punctuation, or an over-long CJK run):
  // insert an empty pair and let the user type between the markers.
  return { from: selFrom, to: selFrom, insert: open + close, anchor: selFrom + open.length, head: selFrom + open.length }
}

/**
 * Build the format key bindings. `getLanguage` is invoked on every keypress so
 * the bindings can be captured once at editor mount yet always format with
 * the language of the file currently open.
 */
export function buildFormatKeymap(getLanguage) {
  const toggle = (styleName) => (view) => {
    const sel = view.state.selection.main
    const change = computeFormatChange(view.state.doc.toString(), sel.from, sel.to, styleName, getLanguage())
    if (!change) return false
    view.dispatch({
      changes: { from: change.from, to: change.to, insert: change.insert },
      selection: { anchor: change.anchor, head: change.head },
      userEvent: 'input.format',
    })
    return true
  }
  return [
    { key: 'Mod-b', run: toggle('bold'), preventDefault: true },
    { key: 'Mod-i', run: toggle('italic'), preventDefault: true },
    { key: 'Mod-`', run: toggle('code'), preventDefault: true },
  ]
}
