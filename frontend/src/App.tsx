import { useEffect, useMemo, useState, lazy, Suspense } from 'react'
import { useVault } from './store/vault'
import * as bridge from './lib/bridge'
import { checkForUpdates, openReleasePage } from './lib/updater'
import Ribbon from './components/Ribbon'
import FileTree from './components/FileTree'
import Editor from './components/Editor'
import Preview from './components/Preview'
import TabBar from './components/TabBar'
import CommandPalette from './components/CommandPalette'
import StatusBar from './components/StatusBar'
// Heavy views — lazy to save 100-150kB initial bundle + memory фоном
const GraphView = lazy(() => import('./components/GraphView'))
const CanvasView = lazy(() => import('./components/CanvasView'))
const SearchView = lazy(() => import('./components/SearchView'))
const WebClipper = lazy(() => import('./components/WebClipper'))
const AIChatFull = lazy(() => import('./components/AIChatFull'))
const TaskManager = lazy(() => import('./components/tasks/TaskManager'))
const LLMSettings = lazy(() => import('./components/LLMSettings'))

type ViewId = 'editor' | 'graph' | 'canvas' | 'search' | 'ai_chat' | 'tasks' | 'llm'

export default function App() {
  const { files, activePath, content, mode, tabs, leftCollapsed, rightCollapsed, commandOpen, status, loadVault, openFile, setContent, save, setMode, setCommandOpen } = useVault()
  const [view, setView] = useState<ViewId>('editor')
  const [activeRibbon, setActiveRibbon] = useState<string>('files')
  const [updateInfo, setUpdateInfo] = useState<{ latest: string; url: string } | null>(null)

  useEffect(() => { loadVault() }, [loadVault])

  // Автообновление на любом устройстве — без ручных триггеров
  useEffect(() => {
    let cancelled = false
    let installing = false
    const check = async () => {
      if (installing) return
      // 1. Пробуем Tauri auto-install (скачает и поставит молча)
      try {
        const { autoInstallIfAvailable } = await import('./lib/updater')
        if (await autoInstallIfAvailable()) {
          installing = true
          return // будет relaunch
        }
      } catch {}
      // 2. Fallback — баннер с GitHub
      const res = await checkForUpdates(true)
      if (!cancelled && res?.hasUpdate) setUpdateInfo({ latest: res.latest, url: res.url })
    }
    check()
    const id = setInterval(check, 15 * 60 * 1000) // каждые 15 мин фоном — экономит CPU/RAM + сеть (было 5мин)
    const onFocus = () => check()
    const onVisibility = () => { if (document.visibilityState === 'visible') check() }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)
    return () => { cancelled = true; clearInterval(id); window.removeEventListener('focus', onFocus); document.removeEventListener('visibilitychange', onVisibility) }
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'p') { e.preventDefault(); setCommandOpen(true) }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save() }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') { e.preventDefault(); useVault.getState().toggleLeft() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [save, setCommandOpen])

  const words = useMemo(() => content.trim() ? content.trim().split(/\s+/).filter(Boolean).length : 0, [content])
  const chars = content.length

  const commands = useMemo(() => [
    { id: 'save', label: 'Сохранить заметку', hint: 'Ctrl+S', action: () => save() },
    { id: 'edit', label: 'Режим: Редактирование', hint: 'edit', action: () => setMode('edit') },
    { id: 'preview', label: 'Режим: Превью', hint: 'preview', action: () => setMode('preview') },
    { id: 'split', label: 'Режим: Split', hint: 'split', action: () => setMode('split') },
    { id: 'graph', label: 'Открыть Граф', hint: 'graph', action: () => { setView('graph'); setActiveRibbon('graph') } },
    { id: 'canvas', label: 'Открыть Canvas', hint: 'canvas', action: () => { setView('canvas'); setActiveRibbon('canvas') } },
    { id: 'search', label: 'Поиск в волте', hint: 'FTS', action: () => setView('search') },
    { id: 'llm', label: 'Настройки нейросетей — llama.cpp + GPT/Gemini/Claude', hint: 'LLM', action: () => { setView('llm'); setActiveRibbon('llm') } },
    { id: 'reload', label: 'Перезагрузить волт', hint: 'reload', action: () => loadVault() },
    ...files.map(f => ({ id: `open:${f}`, label: `Открыть ${f}`, hint: f.split('/').slice(-2).join('/'), action: () => { setView('editor'); setActiveRibbon('files'); openFile(f) } }))
  ], [files, save, setMode, loadVault, openFile])

  const handleNav = (id: string) => {
    setActiveRibbon(id)
    if (id === 'graph' || id === 'canvas' || id === 'search' || id === 'tasks' || id === 'llm') setView(id as ViewId)
    else if (id === 'ai_chat') setView('ai_chat')
    else setView('editor')
  }

  const createNote = async () => {
    const name = `Заметка ${new Date().toISOString().slice(0,10)} ${Math.random().toString(36).slice(2,6)}.md`
    await bridge.writeNote(name, `# ${name.replace('.md','')}\n\nНовая заметка — начни с [[ссылки]] или #тега\n`)
    await loadVault()
    openFile(name)
    setView('editor')
  }

  return (
    <div className="app">
      <Ribbon onNav={handleNav} />

      {!leftCollapsed && (
        <aside className="left-panel">
          <div className="panel-header">Vault — {files.length} <span className="count">{mode}</span></div>
          <div style={{ padding: '6px 8px', display:'flex', gap:6 }}>
            <button className="badge" style={{ flex:1, cursor:'pointer', background:'var(--accent)', color:'#0b0b0b', borderColor:'var(--accent)' }} onClick={createNote}>+ Новая</button>
            <button className="badge" style={{ cursor:'pointer' }} onClick={loadVault}>⟳</button>
          </div>
          <FileTree files={files} current={activePath} onOpen={(p) => { setView('editor'); setActiveRibbon('files'); openFile(p) }} onNew={createNote} />
        </aside>
      )}

      <main className="center">
        {updateInfo && (
          <div style={{ padding:'6px 10px', background:'var(--accent)', color:'#0b0b0b', display:'flex', gap:8, alignItems:'center', fontSize:13, fontWeight:600 }}>
            <span>⬆ Доступно обновление {updateInfo.latest}</span>
            <button onClick={()=> openReleasePage(updateInfo.url)} style={{ background:'#0b0b0b', color:'var(--accent)', border:0, padding:'4px 10px', borderRadius:6, cursor:'pointer' }}>Скачать</button>
            <button onClick={()=> setUpdateInfo(null)} style={{ background:'transparent', border:0, color:'#0b0b0b', cursor:'pointer', marginLeft:'auto' }}>✕</button>
          </div>
        )}
        <div className="toolbar">
          <button className="ghost" onClick={()=> useVault.getState().toggleLeft()} title="Скрыть левую панель (Ctrl+B)">◧</button>
          <button onClick={() => setMode(mode === 'edit' ? 'preview' : mode === 'preview' ? 'split' : 'edit')} title="Edit/Preview/Split">{mode==='edit'?'✎ Edit':mode==='preview'?'👁 Preview':'⬌ Split'}</button>
          <button className="primary" onClick={save}>Сохранить</button>
          <button onClick={() => setCommandOpen(true)}>⌘ Палитра</button>
          <span className="badge" style={{ marginLeft:8 }}>{activeRibbon}</span>
          <span className="path">{activePath || '— нет открытой заметки —'}</span>
          <button className="ghost" onClick={()=> useVault.getState().toggleRight()} title="Правая панель">◨</button>
        </div>

        <TabBar
          tabs={tabs}
          active={activePath}
          onSwitch={(p) => openFile(p)}
          onClose={(p) => {
            const next = tabs.filter(t => t.path !== p)
            if (activePath === p) {
              const fallback = next[0]?.path ?? null
              if (fallback) openFile(fallback)
              else useVault.setState({ activePath: null, content: '', tabs: next })
            } else useVault.setState({ tabs: next })
          }}
        />

        {view === 'editor' && (
          <div className={`editor-area ${mode}`}>
            {(mode === 'edit' || mode === 'split') && <Editor value={content} onChange={setContent} onSave={save} />}
            {(mode === 'preview' || mode === 'split') && <Preview markdown={content} />}
          </div>
        )}
        {view === 'graph' && <Suspense fallback={<div style={{padding:12, opacity:.6}}>Загрузка графа…</div>}><GraphView files={files} /></Suspense>}
        {view === 'canvas' && <Suspense fallback={<div style={{padding:12, opacity:.6}}>Загрузка canvas…</div>}><CanvasView /></Suspense>}
        {view === 'search' && <Suspense fallback={<div style={{padding:12, opacity:.6}}>Загрузка поиска…</div>}><SearchView onSearch={bridge.searchNotes} /></Suspense>}
        {view === 'ai_chat' && <Suspense fallback={<div style={{padding:12, opacity:.6}}>Загрузка AI чата…</div>}><AIChatFull /></Suspense>}
        {view === 'tasks' && <Suspense fallback={<div style={{padding:12, opacity:.6}}>Загрузка задач…</div>}><TaskManager /></Suspense>}
        {view === 'llm' && <Suspense fallback={<div style={{padding:12, opacity:.6}}>Загрузка LLM…</div>}><LLMSettings /></Suspense>}

        <StatusBar status={status} words={words} chars={chars} />
      </main>

      {!rightCollapsed && (
        <aside className="right-panel">
          <div className="panel-header">Навигация <span className="count">{files.length}</span></div>
          <Suspense fallback={<div style={{opacity:.5, fontSize:12}}>Загрузка…</div>}><WebClipper /></Suspense>
          <button onClick={() => { setView('tasks'); setActiveRibbon('tasks') }} style={{ background: activeRibbon==='tasks'?'var(--accent)':'var(--panel-2)', color: activeRibbon==='tasks'?'#0b0b12':'var(--fg)' }}>✓ Task Manager v2</button>
          <button onClick={() => { setView('graph'); setActiveRibbon('graph') }}>🕸 Граф связей</button>
          <button onClick={() => { setView('canvas'); setActiveRibbon('canvas') }}>🎨 Canvas доска</button>
          <button onClick={() => setView('search')}>🔍 Поиск — FTS5</button>
          <button onClick={() => handleNav('ai_chat')}>🤖 AI Чат</button>
          <button onClick={() => { setView('llm'); setActiveRibbon('llm') }} style={{ background: activeRibbon==='llm'?'var(--accent)':'var(--panel-2)', color: activeRibbon==='llm'?'#0b0b12':'var(--fg)' }}>🧠 Нейросети — llama.cpp + API</button>
          <div className="card">
            <h4>Контент</h4>
            <div style={{ fontSize:12, color:'var(--muted)', lineHeight:1.6 }}>Backlinks • Outline • Tags — парсятся `C++` (`[[ ]]`, `#tags`, `frontmatter`).<br/>Tauri IPC + `zod` валидация.</div>
          </div>
          <div className="card">
            <h4>Tasks</h4>
            <div style={{ fontSize:12, color:'var(--muted)' }}>Задачи из <code>- [ ]</code> — агрегируются `tasks` модулем. Скоро `Kanban`/`Calendar`.</div>
          </div>
          <div className="card" style={{ opacity:.8 }}>
            <h4>Совет</h4>
            <div style={{ fontSize:12, color:'var(--muted)' }}><b>Ctrl+P</b> палитра • <b>Ctrl+S</b> сохранить • <b>Ctrl+B</b> боковая • `[[` ссылка • `#тег`</div>
          </div>
        </aside>
      )}

      <CommandPalette open={commandOpen} onClose={() => setCommandOpen(false)} commands={commands} />
    </div>
  )
}
