/**
 * ExploreTab - Research module
 * Left: ChatPanel | Right: BrowserVNC
 */
import { useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import ChatPanel from './ChatPanel'
import BrowserVNC from './BrowserVNC'
import { ResizablePanels } from './ResizablePanels'

export default function ExploreTab({ projectId }) {
  const { t } = useTranslation()
  const getUserState = useCallback(() => ({ active_tab: 'explore' }), [])

  return (
    <ResizablePanels initialSizes={['20%', '1']}>
      <ChatPanel
        projectId={projectId}
        placeholder={t('chat.explorePlaceholder')}
        getUserState={getUserState}
      />
      <BrowserVNC projectId={projectId} />
    </ResizablePanels>
  )
}
