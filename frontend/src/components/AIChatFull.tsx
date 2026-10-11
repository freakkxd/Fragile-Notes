import { useEffect, useRef, useState } from 'react'
import * as llm from '../lib/llm'
import { isLocalKind } from './settings/types'

type Msg = { role: 'user' | 'assistant'; content: string }

function classifyError(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  const low = raw.toLowerCase()
  if (low.includes('no api_key in keyring') || low.includes('missing credential') || low.includes('credentials_missing')) {
    return 'Нет ключа: сохраните API-ключ провайдера в настройках (ключ хранится только в системном хранилище).'
  }
  if (low.includes('unauthorized') || low.includes('invalid_key') || low.includes('invalid key')) {
    return 'Ключ отклонён провайдером (unauthorized). Проверьте ключ в настройках.'
  }
  if (low.includes('privacy') || low.includes('local-only') || low.includes('cloud deny')) {
    return 'Заблокировано политикой приватности: задача local-only, облако запрещено.'
  }
  if (low.includes('offline') || low.includes('timeout') || low.includes('network')) {
    return 'Сеть/таймаут: провайдер недоступен. Проверьте соединение и endpoint.'
  }
  if (low.includes('provider not found') || low.includes('model')) {
    return `Провайдер/модель недоступны: ${raw.slice(0, 200)}`
  }
  return raw.slice(0, 500)
}

export default function AIChatFull() {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [status, setStatus] = useState('LLM: проверка...')
  const [providers, setProviders] = useState<llm.Provider[]>([])
  const [providerId, setProviderId] = useState('')
  const [model, setModel] = useState('')
  const [privacy, setPrivacy] = useState<string | undefined>(undefined)
  const [busy, setBusy] = useState(false)
  const [blocked, setBlocked] = useState('')
  const stale = useRef(false)

  useEffect(() => {
    let live = true
    llm.llmGetConfigSafe().then((res) => {
      if (!live || !res.config) return
      const enabled = res.config.providers.filter((p) => p.enabled)
      setProviders(enabled)
      const first = enabled.find((p) => isLocalKind(p.kind)) ?? enabled[0]
      if (first) {
        setProviderId(first.id)
        setModel(first.default_model || first.model || '')
      }
      const chat = res.config.task_profiles.find((t) => t.id === 'task-chat')
      setPrivacy(chat?.privacy ?? undefined)
    }).catch(() => {})
    return () => { live = false }
  }, [])

  const selected = providers.find((p) => p.id === providerId) ?? null

  const onProvider = (id: string) => {
    setProviderId(id)
    const p = providers.find((x) => x.id === id)
    if (p) setModel(p.default_model || p.model || '')
    setBlocked('')
  }

  const check = async () => {
    const invoke = (window as unknown as { __TAURI__?: { invoke: (c:string, a:unknown)=>Promise<string> } }).__TAURI__?.invoke
    if (!invoke) { setStatus('Tauri not available'); return }
    try {
      const res = await invoke('llm_status', { profile: 'day' })
      setStatus(String(res).slice(0,120))
    } catch (e) { setStatus(String(e)) }
  }

  const stop = () => {
    stale.current = true
    setBusy(false)
    setMsgs((m) => [...m, { role: 'assistant', content: 'Остановлено пользователем.' }])
  }

  const send = async () => {
    if (!input.trim() || busy) return
    setBlocked('')
    // UI privacy pre-check (backend guard remains authoritative).
    if (selected && privacy === 'local_only' && !isLocalKind(selected.kind)) {
      setBlocked(`Отправка заблокирована: профиль task-chat — local_only, а провайдер «${selected.name}» облачный. Выберите локальный провайдер или смените политику в настройках.`)
      return
    }
    if (!selected) {
      setBlocked('Нет включённого провайдера. Включите провайдера в настройках нейросетей.')
      return
    }
    const u: Msg = { role: 'user', content: input }
    const history = [...msgs, u]
    setMsgs(history)
    setInput('')
    stale.current = false
    setBusy(true)
    try {
      const res = await llm.llmChatUniversal(providerId, model, history.map(m=>({role:m.role, content:m.content})))
      if (stale.current) return
      // Local legacy providers return raw JSON; cloud returns normalized text.
      let txt = String(res)
      try { const j = JSON.parse(txt); txt = j.choices?.[0]?.message?.content || txt } catch {}
      setMsgs((m) => [...m, { role: 'assistant', content: txt }])
    } catch (e) {
      if (stale.current) return
      setMsgs((m)=>[...m, {role:'assistant', content: classifyError(e)}])
    } finally {
      if (!stale.current) setBusy(false)
      else stale.current = false
    }
  }

  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100%' }}>
      <div style={{ padding:'8px 12px', borderBottom:'1px solid var(--border)', display:'flex', gap:8, alignItems:'center', flexWrap:'wrap' }}>
        <span style={{ fontWeight:700 }}>AI Чат{selected ? ` — ${selected.name}` : ''}</span>
        <select value={providerId} onChange={(e)=>onProvider(e.target.value)} aria-label="Провайдер"
          style={{ padding:'4px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12, maxWidth:220 }}>
          {providers.length === 0 && <option value="">Нет включённых провайдеров</option>}
          {providers.map((p) => <option key={p.id} value={p.id}>{p.name} ({isLocalKind(p.kind) ? 'local' : 'cloud'})</option>)}
        </select>
        <input value={model} onChange={(e)=>setModel(e.target.value)} placeholder="модель (пусто = default провайдера)" aria-label="Модель"
          style={{ padding:'4px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:6, color:'var(--fg)', fontSize:12, width:200 }} />
        <button onClick={check} style={{ marginLeft:'auto', padding:'4px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12 }}>Статус</button>
        <span style={{ fontSize:11, color:'var(--muted)' }}>{status}</span>
      </div>
      {blocked && <div role="alert" style={{ margin:'8px 12px 0', padding:'8px 10px', background:'rgba(255,180,80,.08)', border:'1px solid var(--border)', borderRadius:8, fontSize:12 }}>{blocked}</div>}
      <div style={{ flex:1, overflow:'auto', padding:12, display:'flex', flexDirection:'column', gap:8 }}>
        {msgs.map((m,i)=> (
          <div key={i} style={{ alignSelf: m.role==='user'?'flex-end':'flex-start', maxWidth:'80%', padding:'8px 12px', borderRadius:12, background: m.role==='user'?'rgba(130,168,255,.14)':'var(--panel-2)', border:'1px solid var(--border)', fontSize:13 }}>{m.content}</div>
        ))}
      </div>
      <div style={{ padding:10, borderTop:'1px solid var(--border)', display:'flex', gap:8 }}>
        <input value={input} onChange={(e)=>setInput(e.target.value)} onKeyDown={(e)=> e.key==='Enter' && send()} placeholder="Сообщение провайдеру…" style={{ flex:1, padding:'10px 12px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:10, color:'var(--fg)', fontSize:12 }} />
        {busy
          ? <button onClick={stop} style={{ padding:'10px 14px', background:'rgba(255,80,80,.85)', color:'#fff', border:0, borderRadius:10, fontWeight:700 }}>Стоп</button>
          : <button onClick={send} style={{ padding:'10px 14px', background:'var(--accent)', border:0, borderRadius:10, fontWeight:700 }}>Отправить</button>}
      </div>
    </div>
  )
}
