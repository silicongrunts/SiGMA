/**
 * Editor font registry. Single source of truth for the selectable monospace
 * fonts in the editor appearance dialog.
 *
 * Each entry maps a stable `id` (stored in localStorage and matched by the
 * whitelist in utils/storage.js) to a `label` shown in the UI and a `css`
 * font-family stack passed to CodeMirror.
 *
 * The fonts themselves are bundled via @fontsource imports in main.jsx.
 * Keep the id list in sync with EDITOR_FONT_IDS in utils/storage.js.
 */

export const FONT_OPTIONS = [
  { id: 'jetbrains-mono', label: 'JetBrains Mono', css: "'JetBrains Mono', ui-monospace, monospace" },
  { id: 'fira-code',      label: 'Fira Code',      css: "'Fira Code', ui-monospace, monospace" },
  { id: 'cascadia-code',  label: 'Cascadia Code',  css: "'Cascadia Code', ui-monospace, monospace" },
  { id: 'source-code-pro',label: 'Source Code Pro',css: "'Source Code Pro', ui-monospace, monospace" },
  { id: 'roboto-mono',    label: 'Roboto Mono',    css: "'Roboto Mono', ui-monospace, monospace" },
  { id: 'system',         label: 'System',         css: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' },
]

const DEFAULT_CSS = FONT_OPTIONS[0].css

/** Resolve a font id to its CSS font-family stack. Falls back to the default. */
export function getFontCss(id) {
  return FONT_OPTIONS.find((f) => f.id === id)?.css ?? DEFAULT_CSS
}
