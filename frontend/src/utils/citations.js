// Chat citations. Up to MAX_CITATIONS entries can be pending at once; they
// travel to the backend joined as ONE string inside the single <citation>
// block (see ai_service._build_message_content), keeping the persisted
// message format unchanged for history compatibility. Two entry kinds share
// the pending list: editor text selections and library documents/folders.
export const MAX_CITATIONS = 5

/** Join pending citations into the single citation string sent with a chat
 *  message. Entries are numbered so the model (and the citation viewer) can
 *  tell the quoted passages apart. */
export function joinCitationTexts(citations) {
  return citations
    .map((c, i) => `[${i + 1}]\n${c.fullText}`)
    .join('\n\n---\n\n')
}

/** Build a citation entry for a library document or folder. The id is the
 *  authoritative pointer — the model reads content on demand via
 *  library_get / library_ls — while title and path add human-readable
 *  context. `folderPath` is the containing folder ("A / B"), "" at root. */
export function buildLibraryCitation(item, folderPath) {
  const kind = item.is_folder ? 'library folder' : 'library document'
  const path = folderPath ? `${folderPath} / ${item.title}` : item.title
  const link = item.is_folder
    ? `sigma://library/folder/${item.id}`
    : `sigma://library/doc/${item.id}`
  return {
    text: item.title,
    fullText: `${kind}\ntitle: ${item.title}\npath: ${path}\nid: ${item.id} (${link})`,
  }
}
