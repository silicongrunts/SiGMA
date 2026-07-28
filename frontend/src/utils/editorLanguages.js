import { LanguageDescription } from '@codemirror/language'
import { languages as cmLanguages } from '@codemirror/language-data'
import { latex } from 'codemirror-lang-latex'

/**
 * Unified entry point mapping a file path to its CodeMirror language support.
 *
 * Language recognition is delegated to the official @codemirror/language-data
 * lookup table (covers dozens of languages; adding a language needs no code
 * change). Loading is asynchronous: the first time a language is opened its
 * @codemirror/lang-* package is dynamically imported; desc.support then caches
 * the result so reopening the same language is a synchronous hit.
 *
 * LaTeX is a deliberate exception: there is no official @codemirror/lang-latex,
 * and the stex mode shipped via language-data is a regex-based legacy mode with
 * no completion or syntax tree. We instead use the community-maintained
 * codemirror-lang-latex, which is built on the Overleaf grammar and ships
 * command/environment completion and linting — a core capability for SiGMA's
 * writing workflows. That package returns a LanguageSupport synchronously, so
 * it is wrapped as a preloaded LanguageDescription (.load() resolves at once)
 * and flows through the same load path as the rest.
 *
 * Caller contract:
 *   - {@link getLanguageDescription} returns a LanguageDescription (or null)
 *     synchronously, used to switch the language extension immediately on
 *     file open.
 *   - {@link loadLanguage} calls .load() on the returned description (idempotent)
 *     and the resolved LanguageSupport is reconfigured into the editor.
 */

// LaTeX is placed first in the array so matchFilename prefers it and overrides
// the legacy stex entry that language-data ships. The latex() instance is a
// single shared LanguageSupport; once cached on the description it is reused.
const latexDescription = LanguageDescription.of({
  name: 'latex',
  alias: ['LaTeX', 'tex'],
  extensions: ['tex', 'ltx'],
  support: latex(),
})

const languageDescriptions = [latexDescription, ...cmLanguages]

/**
 * Look up a language description by file path, synchronously.
 * @param {string} path file path or filename
 * @returns {LanguageDescription | null} the matched description, or null
 */
export function getLanguageDescription(path) {
  if (!path) return null
  return LanguageDescription.matchFilename(languageDescriptions, path)
}

/**
 * Load language support (idempotent: a given description loads at most once).
 * @param {LanguageDescription} desc
 * @returns {Promise<import('@codemirror/language').LanguageSupport>}
 */
export async function loadLanguage(desc) {
  return desc.load()
}
