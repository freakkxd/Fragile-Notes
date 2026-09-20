import { useEffect, useState } from 'react'

type Task = { id: string; title: string; status: string; task_type: string; due: string; project: string; path: string }

type Surface = 'start' | 'today' | 'day' | 'workspace' | 'board' | 'completed'

const invoke = (cmd: string, args?: unknown) => {
  const w = window as unknown as { __TAURI__?: { invoke: (c: string, a?: unknown) => Promise<string> } }
  return w.__TAURI__?.invoke(cmd, args as Record<string, unknown>) || Promise.reject('Tauri not available')
}

function useTasks(filter: string) {
  const [tasks, setTasks] = useState<Task[]>([])
  const load = async () => {
    try {
      const raw = await invoke('tasks_list', { filter })
      setTasks(JSON.parse(String(raw)) as Task[])
    } catch { setTasks([]) }
  }
  useEffect(() => { load() }, [filter])
  return { tasks, reload: load }
}

function TaskRow({ t, onDone }: { t: Task; onDone: () => void }) {
  const done = async () => {
    await invoke('tasks_update_status', { id: t.id, status: 'done' }).catch(()=>{})
    onDone()
  }
  return (
    <div style={{ display:'flex', alignItems:'center', gap:8, padding:'8px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8 }}>
      <span style={{ fontSize:12, opacity:.6 }}>{t.task_type==='bill'?'💳': t.task_type==='routine'?'🔁':'✅'}</span>
      <span style={{ flex:1, fontSize:13 }}>{t.title} {t.due && <span style={{ opacity:.5, fontSize:11 }}>· {t.due}</span>}</span>
      <span style={{ fontSize:11, background:'var(--panel)', border:'1px solid var(--border)', padding:'2px 6px', borderRadius:999 }}>{t.project || '—'}</span>
      <button onClick={done} disabled={t.status==='done'} style={{ padding:'4px 8px', background: t.status==='done'?'var(--muted)':'var(--accent)', color:'#0b0b12', border:0, borderRadius:6, fontSize:12, cursor:'pointer' }}>{t.status==='done'?'✓':'Выполнено'}</button>
    </div>
  )
}

export default function TaskManager() {
  const [surface, setSurface] = useState<Surface>('today')
  const [newTitle, setNewTitle] = useState('')
  const [project, setProject] = useState('')
  const filter = surface==='today'?'today': surface==='board'?'board': surface==='completed'?'completed': 'workspace'
  const { tasks, reload } = useTasks(filter)
  const todayCount = tasks.filter(t=> t.status!=='done').length

  const create = async () => {
    if (!newTitle.trim()) return
    await invoke('tasks_create', { title: newTitle, project, due: '' }).catch(()=>{})
    setNewTitle('')
    reload()
  }

  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100%' }}>
      <div style={{ display:'flex', gap:6, padding:'10px 12px', borderBottom:'1px solid var(--border)', background:'var(--panel)', flexWrap:'wrap' }}>
        {(['start','today','day','workspace','board','completed'] as Surface[]).map(s=> (
          <button key={s} onClick={()=> setSurface(s)} style={{ padding:'6px 10px', background: surface===s?'var(--accent)':'var(--panel-2)', color: surface===s?'#0b0b12':'var(--fg)', border:'1px solid var(--border)', borderRadius:8, fontSize:12, cursor:'pointer', fontWeight: surface===s?700:400 }}>
            {s==='start'?'Start': s==='today'?'Today': s==='day'?'Day': s==='workspace'?'Workspace': s==='board'?'Board':'Completed'}
          </button>
        ))}
        <span style={{ marginLeft:'auto', fontSize:11, color:'var(--muted)' }}>{todayCount} активных</span>
      </div>

      {surface==='start' && (
        <div style={{ padding:16, display:'flex', flexDirection:'column', gap:12 }}>
          <div style={{ padding:14, background:'linear-gradient(135deg, rgba(130,168,255,.14), rgba(139,213,202,.10))', border:'1px solid var(--border)', borderRadius:12 }}>
            <div style={{ fontWeight:800 }}>Где дальше?</div>
            <div style={{ fontSize:12, color:'var(--muted)', marginTop:4 }}>KPI: {todayCount} задач на сегодня — открой Today</div>
            <div style={{ display:'flex', gap:8, marginTop:10 }}>
              <button onClick={()=> setSurface('today')} style={{ padding:'7px 12px', background:'var(--accent)', border:0, borderRadius:8, fontWeight:700 }}>Today</button>
              <button onClick={()=> setSurface('workspace')} style={{ padding:'7px 12px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8 }}>Workspace</button>
              <button onClick={()=> setSurface('board')} style={{ padding:'7px 12px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8 }}>Board</button>
            </div>
          </div>
          <div style={{ display:'flex', gap:8 }}>
            <input value={newTitle} onChange={(e)=>setNewTitle(e.target.value)} placeholder="Новая задача — Enter" onKeyDown={(e)=> e.key==='Enter' && create()} style={{ flex:1, padding:'10px 12px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:10, color:'var(--fg)' }} />
            <input value={project} onChange={(e)=>setProject(e.target.value)} placeholder="Проект" style={{ width:120, padding:'10px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:10 }} />
            <button onClick={create} style={{ padding:'10px 14px', background:'var(--accent)', border:0, borderRadius:10, fontWeight:700 }}>Создать</button>
          </div>
        </div>
      )}

      {surface==='today' && (
        <div style={{ padding:12, display:'flex', flexDirection:'column', gap:8, overflow:'auto' }}>
          <div style={{ fontSize:11, letterSpacing:'.06em', color:'var(--muted)', fontWeight:700 }}>TODAY — {new Date().toISOString().slice(0,10)}</div>
          {tasks.length===0 && <div style={{ padding:20, textAlign:'center', color:'var(--muted)', border:'1px dashed var(--border)', borderRadius:12 }}>Нет задач на сегодня — создай в Start</div>}
          {tasks.map(t=> <TaskRow key={t.id} t={t} onDone={reload} />)}
        </div>
      )}

      {surface==='day' && (
        <div style={{ padding:12 }}>
          <div style={{ padding:12, background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:12 }}>
            <div style={{ fontWeight:700 }}>Day — живой день</div>
            <div style={{ fontSize:12, color:'var(--muted)', marginTop:4 }}>Контекст `02 Daily/{new Date().toISOString().slice(0,10)}.md` + задачи. Утром: Daily note → Today, вечером: Completed.</div>
          </div>
          <div style={{ marginTop:12, display:'flex', flexDirection:'column', gap:8 }}>
            {tasks.slice(0,6).map(t=> <TaskRow key={t.id} t={t} onDone={reload} />)}
          </div>
        </div>
      )}

      {surface==='workspace' && (
        <div style={{ padding:12, overflow:'auto' }}>
          <div style={{ display:'flex', gap:8, marginBottom:10 }}>
            <input placeholder="Фильтр по проекту" onChange={(e)=> setProject(e.target.value)} value={project} style={{ flex:1, padding:'8px 10px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8 }} />
            <button onClick={reload} style={{ padding:'8px 12px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8 }}>Обновить</button>
          </div>
          {tasks.filter(t=> !project || t.project.includes(project)).map(t=> <div key={t.id} style={{ marginBottom:6 }}><TaskRow t={t} onDone={reload} /></div>)}
        </div>
      )}

      {surface==='board' && (
        <div style={{ display:'flex', gap:12, padding:12, overflow:'auto', flex:1 }}>
          {(['todo','doing','done'] as const).map(col=> (
            <div key={col} style={{ flex:1, minWidth:220, background:'rgba(255,255,255,.02)', border:'1px solid var(--border)', borderRadius:12, padding:10 }}>
              <div style={{ fontWeight:700, fontSize:11, letterSpacing:'.06em', textTransform:'uppercase', color:'var(--muted)', borderBottom:'1px solid var(--border)', paddingBottom:6, marginBottom:8 }}>{col} — {tasks.filter(t=> t.status===col).length}</div>
              <div style={{ display:'flex', flexDirection:'column', gap:6 }}>
                {tasks.filter(t=> t.status===col).map(t=> <TaskRow key={t.id} t={t} onDone={reload} />)}
              </div>
            </div>
          ))}
        </div>
      )}

      {surface==='completed' && (
        <div style={{ padding:12, overflow:'auto' }}>
          <div style={{ display:'flex', gap:8, marginBottom:10 }}>
            <span style={{ fontSize:12, color:'var(--muted)' }}>Ретро — terminal `done/cancelled`</span>
            <button onClick={async()=> { await invoke('tasks_archive',{}).catch(()=>{}); reload() }} style={{ marginLeft:'auto', padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12 }}>Архивировать</button>
          </div>
          {tasks.map(t=> (
            <div key={t.id} style={{ padding:'8px 10px', background:'rgba(120,210,150,.08)', border:'1px solid rgba(120,210,150,.22)', borderRadius:8, marginBottom:6, fontSize:13 }}>
              <b>{t.title}</b> <span style={{ opacity:.6, fontSize:11 }}>· {t.path}</span>
            </div>
          ))}
          {!tasks.length && <div style={{ color:'var(--muted)', textAlign:'center', padding:20 }}>Нет завершённых</div>}
        </div>
      )}
    </div>
  )
}
