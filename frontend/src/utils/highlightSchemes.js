/**
 * Syntax highlight scheme registry for the CodeMirror editor.
 *
 * Each scheme provides a `light` and a `dark` CodeMirror extension. Both define
 * ONLY token foreground colors via syntaxHighlighting() — they never touch the
 * editor background, which is controlled solely by dark mode
 * (lightEditorTheme / darkEditorTheme in components/Editor.jsx).
 *
 * Token color sources:
 *  - `default`: CodeMirror's built-in `defaultHighlightStyle` (light) and
 *    `@codemirror/theme-one-dark`'s `oneDarkHighlightStyle` (dark). Both are
 *    the canonical reference implementations.
 *  - `github`, `solarized`: both light and dark palettes come from the
 *    `@uiw/codemirror-theme-*` packages (MIT), which mirror the official
 *    GitHub Primer / Solarized palettes.
 *  - `monokai` dark: from `@uiw/codemirror-theme-monokai` (MIT). No official
 *    light variant exists, so the light palette is hand-written and clearly
 *    marked below as non-authoritative.
 *  - `dracula` dark: the `@uiw/codemirror-theme-dracula` package ships only 8
 *    code-token rules and omits the markdown structural tags (heading/strong/
 *    emphasis/...). We append those rules using the official Dracula palette
 *    (https://draculatheme.com/spec) so markdown live-preview works. No
 *    official light variant exists, so the light palette is hand-written.
 *
 * Keep the id list in sync with EDITOR_SCHEME_IDS in utils/storage.js.
 */
import { syntaxHighlighting, defaultHighlightStyle, HighlightStyle } from '@codemirror/language'
import { oneDarkHighlightStyle } from '@codemirror/theme-one-dark'
import { tags as t } from '@lezer/highlight'
import { githubLightStyle, githubDarkStyle } from '@uiw/codemirror-theme-github'
import { solarizedLightStyle, solarizedDarkStyle } from '@uiw/codemirror-theme-solarized'
import { monokaiDarkStyle } from '@uiw/codemirror-theme-monokai'
import { draculaDarkStyle } from '@uiw/codemirror-theme-dracula'

/**
 * Markdown structural tag rules. These control the live-preview of markdown
 * (heading bold+underline, **bold**, *italic*, ~~strike~~, [link]). They carry
 * no color — only font shape — so they are theme-independent and reused across
 * schemes whose upstream palettes omit them.
 */
const MARKDOWN_STRUCTURAL_RULES = [
  { tag: t.heading,       fontWeight: 'bold', textDecoration: 'underline' },
  { tag: t.strong,        fontWeight: 'bold' },
  { tag: t.emphasis,      fontStyle: 'italic' },
  { tag: t.strikethrough, textDecoration: 'line-through' },
  { tag: t.link,          textDecoration: 'underline' },
]

/**
 * Build a syntaxHighlighting extension from a { tag, color, ... } spec list.
 * The specs may be a mix of our own TagStyle objects and ones imported from
 * @uiw packages.
 */
function build(specs) {
  return syntaxHighlighting(HighlightStyle.define(specs))
}

/**
 * Sanitize an imported @uiw style array: drop any entry whose `fontStyle` is
 * not a recognized keyword. Works around a data bug in
 * @uiw/codemirror-theme-solarized where `typeName` carries a hex color in the
 * `fontStyle` field. Invalid values are silently ignored by CodeMirror anyway,
 * but dropping them here keeps the spec clean and avoids relying on that
 * silent-ignore behavior.
 */
function cleanUiwStyle(style) {
  return style.filter((s) => s.fontStyle === undefined || s.fontStyle === 'italic' || s.fontStyle === 'normal')
}

// ── GitHub (authoritative, from @uiw) ───────────────────────────────────
const githubLight = build(cleanUiwStyle(githubLightStyle))
const githubDark  = build(cleanUiwStyle(githubDarkStyle))

// ── Solarized (authoritative, from @uiw; typeName fontStyle bug cleaned) ─
const solarizedLight = build(cleanUiwStyle(solarizedLightStyle))
const solarizedDark  = build(cleanUiwStyle(solarizedDarkStyle))

// ── Monokai ──────────────────────────────────────────────────────────────
// Dark: authoritative from @uiw. Light: hand-written (no official Monokai
// light palette exists).
const monokaiDark = build(cleanUiwStyle(monokaiDarkStyle))
const monokaiLight = build([
  { tag: t.comment, color: '#7c7c7c', fontStyle: 'italic' },
  { tag: t.variableName, color: '#272822' },
  { tag: [t.number, t.bool], color: '#ae81ff' },
  { tag: t.string, color: '#a6e22e' },
  { tag: t.keyword, color: '#f92672' },
  { tag: [t.function(t.variableName), t.function(t.propertyName)], color: '#66d9ef' },
  { tag: t.typeName, color: '#66d9ef' },
  { tag: t.propertyName, color: '#fd971f' },
  { tag: t.operator, color: '#f92672' },
  { tag: t.punctuation, color: '#272822' },
  ...MARKDOWN_STRUCTURAL_RULES,
])

// ── Dracula ──────────────────────────────────────────────────────────────
// Dark: @uiw ships only code-token rules; we append the markdown structural
// rules using official Dracula palette values (Pink #ff79c6 for headings,
// foreground #f8f8f2 for emphasis/strong which only need font shape anyway).
// Light: hand-written (Dracula is dark-only by design).
const draculaDark = build([
  ...cleanUiwStyle(draculaDarkStyle),
  { tag: t.heading, color: '#ff79c6', fontWeight: 'bold', textDecoration: 'underline' },
  { tag: t.strong, color: '#f8f8f2', fontWeight: 'bold' },
  { tag: t.emphasis, color: '#f8f8f2', fontStyle: 'italic' },
  { tag: t.strikethrough, color: '#f8f8f2', textDecoration: 'line-through' },
  { tag: t.link, color: '#8be9fd', textDecoration: 'underline' },
])
const draculaLight = build([
  { tag: t.comment, color: '#6272a4', fontStyle: 'italic' },
  { tag: t.variableName, color: '#383a42' },
  { tag: [t.number, t.bool], color: '#bd93f9' },
  { tag: t.string, color: '#50fa7b' },
  { tag: t.keyword, color: '#8b080b' },
  { tag: [t.function(t.variableName), t.function(t.propertyName)], color: '#a04080' },
  { tag: t.typeName, color: '#bd93f9' },
  { tag: t.propertyName, color: '#6272a4' },
  { tag: t.operator, color: '#ff79c6' },
  { tag: t.punctuation, color: '#6272a4' },
  ...MARKDOWN_STRUCTURAL_RULES,
])

export const HIGHLIGHT_SCHEMES = [
  {
    id: 'default',
    label: 'Default',
    // The built-in styles are already full HighlightStyle extensions.
    light: syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    dark: syntaxHighlighting(oneDarkHighlightStyle, { fallback: true }),
  },
  { id: 'github',    label: 'GitHub',     light: githubLight,    dark: githubDark },
  { id: 'solarized', label: 'Solarized',  light: solarizedLight, dark: solarizedDark },
  { id: 'dracula',   label: 'Dracula',    light: draculaLight,   dark: draculaDark },
  { id: 'monokai',   label: 'Monokai',    light: monokaiLight,   dark: monokaiDark },
]

const DEFAULT_SCHEME = HIGHLIGHT_SCHEMES[0]

/**
 * Resolve a scheme id to its CodeMirror extension for the current background
 * mode. `isDark` selects which of the two palettes is returned; the scheme
 * itself never controls the editor background.
 */
export function getSchemeExtension(id, isDark) {
  const scheme = HIGHLIGHT_SCHEMES.find((s) => s.id === id) ?? DEFAULT_SCHEME
  return isDark ? scheme.dark : scheme.light
}

/**
 * A small palette preview used by the appearance dialog: returns the
 * foreground colors of a few representative token kinds for the given scheme
 * and mode, so the UI can render a color swatch without instantiating a
 * HighlightStyle parser.
 *
 * The @uiw package styles do not expose a clean 1:1 mapping for these four
 * token kinds (e.g. they fold variableName into a shared "name" entry, and
 * the @uiw entries often carry uppercase hex while the hand-written light
 * variants use lowercase), so the swatch values are curated here rather than
 * mechanically extracted. The JSDoc on each scheme above is the authoritative
 * source for where each palette comes from.
 */
export function getSchemePreviewColors(id, isDark) {
  const map = {
    default:    isDark ? { keyword: '#c678dd', string: '#98c379', comment: '#7f848e', name: '#e06c75' }
                       : { keyword: '#708',    string: '#a11',    comment: '#940',    name: '#00f' },
    github:     isDark ? { keyword: '#ff7b72', string: '#a5d6ff', comment: '#8b949e', name: '#d2a8ff' }
                       : { keyword: '#d73a49', string: '#032f62', comment: '#6a737d', name: '#6f42c1' },
    solarized:  isDark ? { keyword: '#859900', string: '#2aa198', comment: '#586e75', name: '#268bd2' }
                       : { keyword: '#859900', string: '#2aa198', comment: '#93a1a1', name: '#268bd2' },
    dracula:    isDark ? { keyword: '#ff79c6', string: '#f1fa8c', comment: '#6272a4', name: '#50fa7b' }
                       : { keyword: '#8b080b', string: '#50fa7b', comment: '#6272a4', name: '#a04080' },
    monokai:    isDark ? { keyword: '#f92672', string: '#e6db74', comment: '#88846f', name: '#a6e22e' }
                       : { keyword: '#f92672', string: '#a6e22e', comment: '#7c7c7c', name: '#66d9ef' },
  }
  return map[id] ?? map.default
}
