import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App.jsx'
import './index.css'
import 'katex/dist/katex.min.css'
import 'pdfjs-dist/web/pdf_viewer.css'

// Editor fonts (OFL-1.1, bundled locally — see THIRD_PARTY_FONT_LICENSES.md).
// Only regular and medium weights are loaded to keep bundle size down.
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import '@fontsource/fira-code/400.css'
import '@fontsource/fira-code/500.css'
import '@fontsource/cascadia-code/400.css'
import '@fontsource/cascadia-code/500.css'
import '@fontsource/source-code-pro/400.css'
import '@fontsource/source-code-pro/500.css'
import '@fontsource/roboto-mono/400.css'
import '@fontsource/roboto-mono/500.css'

import './i18n'
import { initTheme } from './hooks/useTheme'
import { initLanguage } from './hooks/useLanguage'
import { initEditorAppearance } from './hooks/useEditorAppearance'
import { storage } from './utils/storage'

// Apply the saved theme, language, and editor appearance before React mounts
// to avoid a flash of the wrong values.
storage.initialize()
initTheme()
initLanguage()
initEditorAppearance()

// Initialize PDF.js worker (local, no CDN)
import * as pdfjsLib from 'pdfjs-dist'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.js?url'
pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
