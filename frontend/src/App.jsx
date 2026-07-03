import { Routes, Route, useParams } from 'react-router-dom'
import { ToastContainer } from './components/Toast'
import ProjectsView from './views/ProjectsView'
import EditorView from './views/EditorView'

/** Wrapper that forces EditorView remount when project id changes. */
function EditorRoute() {
  const { id } = useParams()
  return <EditorView key={id} />
}

function App() {
  return (
    <div className="h-screen w-screen flex flex-col overflow-hidden bg-white dark:bg-gray-900 font-sans">
      <Routes>
        <Route path="/" element={<ProjectsView />} />
        <Route path="/editor/:id" element={<EditorRoute />} />
      </Routes>
      <ToastContainer />
    </div>
  )
}

export default App
