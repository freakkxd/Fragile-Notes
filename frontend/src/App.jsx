import { useState, useEffect } from 'react'
import Editor from './components/Editor.jsx'
import Preview from './components/Preview.jsx'
import FileTree from './components/FileTree.jsx'

const TEXT_EXTS = ['.md','.txt','.html','.css','.js','.ts','.json','.py','.cpp','.h','.rs','.go','.sh','.xml','.csv','.log','.yaml','.yml','.toml','.ini']

export default function App(){
  const [vault, setVault] = useState([])
  const [current, setCurrent] = useState(null)
  const [content, setContent] = useState('')
  const [mode, setMode] = useState('split')

  useEffect(()=>{
    if(window.fragileBridge){
      window.fragileBridge.listNotes().then(setVault)
    }
  },[])

  const openFile = async (path) => {
    setCurrent(path)
    if(window.fragileBridge){
      const txt = await window.fragileBridge.readNote(path)
      setContent(txt)
    } else {
      setContent(`# ${path}\n\nBridge not connected — preview mode`)
    }
  }

  const save = async () => {
    if(current && window.fragileBridge){
      await window.fragileBridge.writeNote(current, content)
    }
  }

  return (
    <div className="app">
      <aside className="left-panel">
        <div className="panel-header">Заметки — корень</div>
        <FileTree files={vault} onOpen={openFile} current={current} />
      </aside>
      <main className="center">
        <div className="toolbar">
          <button onClick={()=>setMode(mode==='edit'?'preview': mode==='preview'?'split':'edit')}>{mode}</button>
          <button onClick={save}>Сохранить</button>
          <span className="path">{current || '—'}</span>
        </div>
        <div className={`editor-area ${mode}`}>
          {(mode==='edit'||mode==='split') && <Editor value={content} onChange={setContent} />}
          {(mode==='preview'||mode==='split') && <Preview markdown={content} currentPath={current} />}
        </div>
      </main>
      <aside className="right-panel">
        <div className="panel-header">Фичи</div>
        <button onClick={()=>window.dispatchEvent(new CustomEvent('fragile:nav',{detail:'graph'}))}>🕸 Граф</button>
        <button onClick={()=>window.dispatchEvent(new CustomEvent('fragile:nav',{detail:'canvas'}))}>🎨 Canvas</button>
        <button onClick={()=>window.dispatchEvent(new CustomEvent('fragile:nav',{detail:'ai_chat'}))}>🤖 AI Чат</button>
        <button onClick={()=>window.dispatchEvent(new CustomEvent('fragile:nav',{detail:'calendar'}))}>🗓 Календарь</button>
      </aside>
    </div>
  )
}
