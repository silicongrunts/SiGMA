import { createContext } from 'react'

/**
 * Context for LibraryBrowser → EditorHeader / LibraryTab communication.
 *
 * LibraryBrowser (inside LibraryTab) writes callbacks + status;
 * EditorHeader reads them to render action buttons and status indicators.
 * LibraryTab reads browsing state for LLM user_state context.
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
})
