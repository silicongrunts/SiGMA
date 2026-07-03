/**
 * LibraryTab - Resource library module
 * Left: ChatPanel | Right: LibraryBrowser
 */
import { useCallback, useContext } from 'react'
import { useTranslation } from 'react-i18next'
import ChatPanel from './ChatPanel'
import LibraryBrowser from './LibraryBrowser'
import { ResizablePanels } from './ResizablePanels'
import { LibraryActionsContext } from './LibraryActionsContext'

export default function LibraryTab({ projectId }) {
  const { t } = useTranslation()
  const libraryCtx = useContext(LibraryActionsContext)

  const getUserState = useCallback(() => {
    const userState = { active_tab: 'library' }
    if (libraryCtx.selectedDocId) {
      userState.viewing_document = {
        id: libraryCtx.selectedDocId,
        title: libraryCtx.selectedDocTitle,
      }
    }
    if (libraryCtx.currentFolderPath && libraryCtx.currentFolderPath !== 'Library') {
      userState.folder_path = libraryCtx.currentFolderPath
    }
    if (libraryCtx.indexingStatus) {
      const s = libraryCtx.indexingStatus
      const active = s.pending + s.processing + s.indexing + (s.cancelling || 0)
      if (active > 0 || s.failed > 0) {
        userState.indexing_status = libraryCtx.indexingStatus
      }
    }
    return userState
  }, [libraryCtx.selectedDocId, libraryCtx.selectedDocTitle,
      libraryCtx.currentFolderPath, libraryCtx.indexingStatus])

  return (
    <ResizablePanels initialSizes={['20%', '1']} className="h-full">
      <ChatPanel
        projectId={projectId}
        placeholder={t('chat.libraryPlaceholder')}
        getUserState={getUserState}
      />
      <div className="h-full overflow-hidden">
        <LibraryBrowser projectId={projectId} />
      </div>
    </ResizablePanels>
  )
}
