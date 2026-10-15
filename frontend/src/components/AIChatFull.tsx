import { useEffect, useRef, useState } from 'react'
import * as llm from '../lib/llm'
import { isLocalKind, ollamaDiscoverErrorText } from './settings/types'

type Msg = { role: 'user' | 'assistant'; content: string }

/** Pure helpers below are exported for unit tests; component behavior is unchanged. */

export function classifyError(e: unknown): string {
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

/** First enabled local provider, else first enabled provider, else null. */
export function defaultProviderForChat(providers: llm.Provider[]): llm.Provider | null {
  const enabled = providers.filter((p) => p.enabled)
  return enabled.find((p) => isLocalKind(p.kind)) ?? enabled[0] ?? null
}

/** Configured model default for a provider; user may override afterwards. */
export function defaultModelForProvider(p: llm.Provider | null): string {
  if (!p) return ''
  return p.default_model || p.model || ''
}

/** Non-empty explanation when send must be blocked; '' means allowed. */
export function chatPrivacyBlocked(provider: llm.Provider | null, privacy: string | undefined): string {
  if (provider && privacy === 'local_only' && !isLocalKind(provider.kind)) {
    return `Отправка заблокирована: профиль task-chat — local_only, а провайдер «${provider.name}» облачный. Выберите локальный провайдер или смените политику в настройках.`
  }
  if (!provider) {
    return 'Нет включённого провайдера. Включите провайдера в настройках нейросетей.'
  }
  return ''
}

/** Local registry records eligible for chat: Present state of this provider.
 *  The select value is always RegistryModel.id — never a filesystem path.
 *  Deterministic order (filename, then id): backend registry order is
 *  unspecified, so the UI must not depend on list position. */
export function presentLocalModels(registry: llm.RegistryModel[], providerId: string): llm.RegistryModel[] {
  return registry
    .filter((r) => r.state === 'Present' && r.provider_id === providerId)
    .sort((a, b) => (a.filename < b.filename ? -1 : a.filename > b.filename ? 1 : 0)
      || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
}

/** Local legacy providers return raw JSON; cloud returns normalized text. */
export function extractChatText(raw: unknown): string {
  let txt = String(raw)
  try { const j = JSON.parse(txt); txt = j.choices?.[0]?.message?.content || txt } catch {}
  return txt
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
  const [registry, setRegistry] = useState<llm.RegistryModel[]>([])
  // Config lifecycle: never render a provider-shaped model control before
  // the config is resolved (initial null must not look like cloud).
  const [configState, setConfigState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [configError, setConfigError] = useState('')
  const [registryLoaded, setRegistryLoaded] = useState(false)
  // Ollama-native discovery, separate from the GGUF registry. Guarded by a
  // monotonic generation so a late response never publishes state for another
  // provider, and a failed refresh never leaves a stale model marked confirmed.
  const [ollamaModels, setOllamaModels] = useState<string[]>([])
  const [ollamaState, setOllamaState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [ollamaError, setOllamaError] = useState('')
  const discoverGen = useRef(0)
  const stale = useRef(false)
  const mounted = useRef(true)
  // Monotonic generation: every load()/retry is a new generation; only the
  // latest one may publish state. mounted guards unmount, generation guards
  // staleness between overlapping attempts on a live component.
  const loadGen = useRef(0)

  const load = async () => {
    const gen = (loadGen.current += 1)
    const isCurrent = () => mounted.current && loadGen.current === gen
    setConfigState('loading')
    setConfigError('')
    setBlocked('')
    try {
      const res = await llm.llmGetConfigSafe()
      if (!isCurrent()) return
      if (!res.config) {
        // config:null carries typed, user-facing issues — surface a safe
        // message, never the internal placeholder or raw payload/paths.
        const issue = res.issues.find((i) => i.code === 'invalid_root' || i.code === 'backend_unavailable') ?? res.issues[0]
        throw new Error(issue?.message || 'Конфигурация недоступна. Проверьте backend.')
      }
      const enabled = res.config.providers.filter((p) => p.enabled)
      setProviders(enabled)
      const first = defaultProviderForChat(enabled)
      if (first) {
        setProviderId(first.id)
        setModel(defaultModelForProvider(first))
      }
      const chat = res.config.task_profiles.find((t) => t.id === 'task-chat')
      setPrivacy(chat?.privacy ?? undefined)
      setConfigState('ready')
    } catch (e) {
      if (!isCurrent()) return
      setConfigState('error')
      setConfigError(e instanceof Error ? e.message : String(e))
    }
    try {
      const list = await llm.llmModelsList()
      if (!isCurrent()) return
      setRegistry(list)
    } catch {
      if (!isCurrent()) return
      setRegistry([])
    } finally {
      if (isCurrent()) setRegistryLoaded(true)
    }
  }

  useEffect(() => {
    mounted.current = true
    load()
    return () => { mounted.current = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const selected = providers.find((p) => p.id === providerId) ?? null
  const isOllama = selected?.kind === 'ollama'
  const isLocal = !!selected && isLocalKind(selected.kind)
  const eligible = selected && isLocal && !isOllama ? presentLocalModels(registry, selected.id) : []

  const discoverOllama = async (id: string) => {
    const gen = (discoverGen.current += 1)
    const isCurrent = () => mounted.current && discoverGen.current === gen
    setOllamaState('loading')
    setOllamaError('')
    setOllamaModels([])
    try {
      const res = await llm.llmDiscoverModels(id)
      if (!isCurrent()) return
      if (res.ok) {
        const models = res.models ?? []
        setOllamaModels(models)
        setOllamaState('ready')
        // Select the first model if the current value is not in the list.
        setModel((cur) => (models.length > 0 && !models.includes(cur) ? models[0] : cur))
      } else {
        setOllamaState('error')
        setOllamaError(ollamaDiscoverErrorText(res.error?.code))
      }
    } catch {
      if (!isCurrent()) return
      setOllamaState('error')
      setOllamaError(ollamaDiscoverErrorText(undefined))
    }
  }

  // Discover whenever the selected provider is Ollama. Non-Ollama selection
  // invalidates any in-flight discovery and clears the native model state.
  useEffect(() => {
    if (configState !== 'ready') return
    if (selected?.kind === 'ollama') {
      discoverOllama(selected.id)
    } else {
      discoverGen.current += 1
      setOllamaState('idle')
      setOllamaModels([])
      setOllamaError('')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providerId, configState])

  // Prefer the first verified registry record once it arrives (GGUF registry
  // applies only to local_llama_cpp; Ollama uses native discovery).
  useEffect(() => {
    if (!selected || !isLocalKind(selected.kind) || selected.kind === 'ollama') return
    const list = presentLocalModels(registry, selected.id)
    if (list.length > 0 && !list.some((r) => r.id === model)) {
      setModel(list[0].id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [registry, providerId])

  const onProvider = (id: string) => {
    setProviderId(id)
    const p = providers.find((x) => x.id === id) ?? null
    if (p?.kind === 'ollama') {
      // Native discovery fills the list; avoid a flash of a registry path.
      setModel('')
    } else if (p && isLocalKind(p.kind)) {
      const list = presentLocalModels(registry, p.id)
      setModel(list.length > 0 ? list[0].id : '')
    } else {
      setModel(defaultModelForProvider(p))
    }
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
    if (configState !== 'ready') {
      setBlocked(configState === 'loading' ? 'Дождитесь загрузки конфигурации.' : 'Конфигурация недоступна. Нажмите «Повторить» выше.')
      return
    }
    // UI privacy pre-check (backend guard remains authoritative).
    const blockReason = chatPrivacyBlocked(selected, privacy)
    if (blockReason) {
      setBlocked(blockReason)
      return
    }
    if (isOllama) {
      if (ollamaState === 'loading' || ollamaState === 'idle') {
        setBlocked('Дождитесь загрузки списка моделей Ollama.')
        return
      }
      if (ollamaState === 'error') {
        setBlocked(ollamaError || 'Модели Ollama недоступны. Обновите список.')
        return
      }
      if (ollamaModels.length === 0) {
        setBlocked('В Ollama нет моделей. Загрузите модель (`ollama pull <name>`) и обновите список.')
        return
      }
      if (!ollamaModels.includes(model)) {
        setBlocked('Выберите модель из списка Ollama.')
        return
      }
    } else if (isLocal) {
      if (!registryLoaded) {
        setBlocked('Дождитесь загрузки моделей.')
        return
      }
      if (eligible.length === 0) {
        setBlocked('Нет установленных моделей: откройте настройки → Модели и нажмите «Сканировать реестр».')
        return
      }
      if (!eligible.some((r) => r.id === model)) {
        setBlocked('Выбранная модель отсутствует в реестре. Выберите доступную модель или пересканируйте реестр.')
        return
      }
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
      setMsgs((m) => [...m, { role: 'assistant', content: extractChatText(res) }])
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
        <select value={providerId} onChange={(e)=>onProvider(e.target.value)} aria-label="Провайдер" disabled={configState !== 'ready'}
          style={{ padding:'4px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12, maxWidth:220 }}>
          {configState === 'loading' && <option value="">Загрузка…</option>}
          {configState !== 'loading' && providers.length === 0 && <option value="">Нет включённых провайдеров</option>}
          {providers.map((p) => <option key={p.id} value={p.id}>{p.name} ({isLocalKind(p.kind) ? 'local' : 'cloud'})</option>)}
        </select>
        {configState !== 'ready' ? (
          <span style={{ fontSize:11, color:'var(--muted)' }}>{configState === 'loading' ? 'Загрузка…' : '—'}</span>
        ) : isOllama ? (
          ollamaState === 'loading' || ollamaState === 'idle' ? (
            <span style={{ fontSize:11, color:'var(--muted)' }}>Загрузка моделей Ollama…</span>
          ) : ollamaState === 'error' ? (
            <span style={{ fontSize:11, color:'var(--muted)' }}>
              {ollamaError}{' '}
              <button onClick={()=>selected && discoverOllama(selected.id)} style={{ padding:'2px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:11 }}>↻ Обновить</button>
            </span>
          ) : ollamaModels.length === 0 ? (
            <span style={{ fontSize:11, color:'var(--muted)' }}>
              Нет моделей — выполните <code>ollama pull &lt;name&gt;</code>.{' '}
              <button onClick={()=>selected && discoverOllama(selected.id)} style={{ padding:'2px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:11 }}>↻ Обновить</button>
            </span>
          ) : (
            <>
              <select value={model} onChange={(e)=>setModel(e.target.value)} aria-label="Модель"
                style={{ padding:'4px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12, maxWidth:260 }}>
                {ollamaModels.map((mid) => <option key={mid} value={mid}>{mid}</option>)}
              </select>
              <button onClick={()=>selected && discoverOllama(selected.id)} style={{ padding:'4px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12 }}>↻</button>
            </>
          )
        ) : isLocal ? (
          eligible.length > 0 ? (            <select value={model} onChange={(e)=>setModel(e.target.value)} aria-label="Модель"
              style={{ padding:'4px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12, maxWidth:260 }}>
              {eligible.map((r) => <option key={r.id} value={r.id}>{r.filename}</option>)}
            </select>
          ) : (
            <span style={{ fontSize:11, color:'var(--muted)' }}>{registryLoaded ? 'Нет установленных моделей — откройте настройки → Модели и нажмите «Сканировать реестр».' : 'Загрузка моделей…'}</span>
          )
        ) : (
          <input value={model} onChange={(e)=>setModel(e.target.value)} placeholder="модель (пусто = default провайдера)" aria-label="Модель"
            style={{ padding:'4px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:6, color:'var(--fg)', fontSize:12, width:200 }} />
        )}
        <button onClick={check} style={{ marginLeft:'auto', padding:'4px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12 }}>Статус</button>
        <span style={{ fontSize:11, color:'var(--muted)' }}>{status}</span>
      </div>
      {blocked && <div role="alert" style={{ margin:'8px 12px 0', padding:'8px 10px', background:'rgba(255,180,80,.08)', border:'1px solid var(--border)', borderRadius:8, fontSize:12 }}>{blocked}</div>}
      {configState === 'error' && (
        <div role="alert" style={{ margin:'8px 12px 0', padding:'8px 10px', background:'rgba(255,80,80,.08)', border:'1px solid rgba(255,80,80,.4)', borderRadius:8, fontSize:12 }}>
          Не удалось загрузить конфигурацию{configError ? `: ${configError.slice(0, 200)}` : '. Проверьте backend.'}{' '}
          <button onClick={load} style={{ padding:'4px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12 }}>↻ Повторить</button>
        </div>
      )}
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
