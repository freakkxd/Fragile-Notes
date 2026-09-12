import { useEffect, useMemo, useState } from 'react'
import { useVault } from './store/vault'
import Ribbon from './components/Ribbon'
import FileTree from './components/FileTree'
import Editor from './components/Editor'
import Preview from './components/Preview'
import TabBar from './components/TabBar'
import CommandPalette from './components/CommandPalette'
import StatusBar from './components/StatusBar'
import GraphView from './components/GraphView'
import CanvasView from './components/CanvasView'

type ViewId = 'editor' | 'graph' | 'canvas' | 'search' | 'calendar' | 'tasks' | 'ai_chat'

export default function App() {
  const { files, activePath, content, mode, tabs, leftCollapsed, rightCollapsed, commandOpen, status, loadVault, openFile, setContent, save, setMode, setCommandOpen } = useVault()
  const [view, setView] = useState<ViewId>('editor')

  useEffect(() => { loadVault() }, [loadVault])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'p') { e.preventDefault(); setCommandOpen(true) }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [save, setCommandOpen])

  const words = useMemo(() => content.trim() ? content.trim().split(/\s+/).length : 0, [content])

  const commands = useMemo(() => [
    { id: 'save', label: 'Сохранить заметку (Ctrl+S)', action: () => save() },
    { id: 'edit', label: 'Режим: Редактирование', action: () => setMode('edit') },
    { id: 'preview', label: 'Режим: Превью', action: () => setMode('preview') },
    { id: 'split', label: 'Режим: Split', action: () => setMode('split') },
    { id: 'graph', label: 'Открыть Граф', action: () => setView('graph') },
    { id: 'canvas', label: 'Открыть Canvas', action: () => setView('canvas') },
    { id: 'reload', label: 'Перезагрузить волт', action: () => loadVault() },
    ...files.map(f => ({ id: `open:${f}`, label: `Открыть ${f}`, action: () => { setView('editor'); openFile(f) } }))
  ], [files, save, setMode, loadVault, openFile])

  const handleNav = (id: string) => {
    if (id === 'graph' || id === 'canvas' || id === 'search') setView(id as ViewId)
    else if (id === 'ai_chat') setView('ai_chat')
    else setView('editor')
  }

  return (
    <div className="app">
      <Ribbon onNav={handleNav} />
      <aside className={`left-panel ${leftCollapsed ? 'collapsed' : ''}`}>
        <div className="panel-header">Vault — {files.length} файлов</div>
        <FileTree files={files} current={activePath} onOpen={(p) => { setView('editor'); openFile(p) }} />
      </aside>

      <main className="center">
        <div className="toolbar">
          <button onClick={() => setMode(mode === 'edit' ? 'preview' : mode === 'preview' ? 'split' : 'edit')} title="Переключить Edit/Preview/Split">{mode}</button>
          <button className="primary" onClick={save}>Сохранить</button>
          <button onClick={() => setCommandOpen(true)}>⌘ Палитра</button>
          <span className="path">{activePath || '— нет открытой заметки —'}</span>
        </div>

        <TabBar
          tabs={tabs}
          active={activePath}
          onSwitch={(p) => openFile(p)}
          onClose={(p) => {
            const next = tabs.filter(t => t.path !== p)
            // naive close: clear active if closed
            if (activePath === p) {
              const fallback = next[0]?.path ?? null
              if (fallback) openFile(fallback)
              else useVault.setState({ activePath: null, content: '', tabs: next })
            } else useVault.setState({ tabs: next })
          }}
        />

        {view === 'editor' && (
          <div className={`editor-area ${mode}`}>
            {(mode === 'edit' || mode === 'split') && <Editor value={content} onChange={setContent} />}
            {(mode === 'preview' || mode === 'split') && <Preview markdown={content} />}
          </div>
        )}
        {view === 'graph' && <GraphView files={files} />}
        {view === 'canvas' && <CanvasView />}
        {view === 'search' && <div style={{ padding: 16 }}><h3>Поиск</h3><p>FTS5 через C++ core / Tauri <code>fts_search</code> — вставь запрос в палитре.</p></div>}
        {view === 'ai_chat' && <div style={{ padding: 16 }}><h3>AI Чат</h3><p>LLM offline — подключи <code>~/LLM/models/*.gguf</code> (llama.cpp) </p></div>}

        <StatusBar status={status} words={words} />
      </main>

      <aside className={`right-panel ${rightCollapsed ? 'collapsed' : ''}`}>
        <div className="panel-header">Навигация</div>
        <button onClick={() => setView('graph')}>🕸 Граф</button>
        <button onClick={() => setView('canvas')}>🎨 Canvas</button>
        <button onClick={() => setView('search')}>🔍 Поиск (FTS)</button>
        <button onClick={() => handleNav('ai_chat')}>🤖 AI Чат</button>
        <button onClick={() => handleNav('calendar')}>🗓 Календарь</button>
        <div className="panel-header" style={{ marginTop: 8 }}>Контент</div>
        <div style={{ fontSize: 12, opacity: .6, padding: '4px 2px' }}>Backlinks, Outline, Tags — парсятся C++ core (wikilinks [[ ]], #tags, frontmatter). Tauri IPC + zod.</div>
        <div className="panel-header">Tasks</div>
        <div style={{ fontSize: 12, opacity: .7 }}>Задачи из `- [ ]` — агрегируются `tasks` C++ модулем.</div>
      </aside>

      <CommandPalette open={commandOpen} onClose={() => setCommandOpen(false)} commands={commands} />
    </div>
  )
}
