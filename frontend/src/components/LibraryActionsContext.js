import { createContext } from 'react'

/**
 * Context for LibraryBrowser ↔ EditorHeader / EditorView communication.
 *
 * LibraryBrowser writes callbacks + status; EditorHeader reads them to render
 * action buttons and status indicators. LibraryTab reads browsing state for
 * LLM user_state context. EditorView writes navigation requests (below) that
 * LibraryBrowser consumes to drive folder/document reveal from chat citations.
 */
export const LibraryActionsContext = createContext({
  onRefresh: null,
  onReprocessAll: null,
  reprocessingAll: false,
  statusSummary: null,
  hasFailed: false,
  onNewFolder: null,
  onUploadFiles: null,
  // Library browsing state for LLM status context
  selectedDocId: null,
  selectedDocTitle: null,
  currentFolderPath: null,
  indexingStatus: null,
  // Imperative navigation requests from chat citations. Each carries a requestId
  // so LibraryBrowser's effect re-fires even when the target id repeats. null
  // means "no pending request".
  revealFolderRequest: null,    // { folderId, requestId }
  revealDocumentRequest: null,  // { docId, requestId }
})
