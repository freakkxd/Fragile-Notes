import { useEffect, useRef, useState } from 'react'
import * as llm from '../lib/llm'
import { isLocalKind, ollamaDiscoverErrorText } from './settings/types'
import ComposerMenu from './chat/ComposerMenu'

type Msg = { role: 'user' | 'assistant'; content: string }

/** Pure helpers below are exported for unit tests; component behavior is unchanged. */

export function classifyError(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  const low = raw.toLowerCase()
  if (low.includes('consent_required')) {
    return 'Требуется согласие на отправку в облако: повторите отправку и подтвердите запрос согласия.'
  }
  if (low.includes('task profile not found')) {
    return 'Профиль задачи не найден в конфигурации. Проверьте Настройки → Приватность.'
  }
  if (low.includes('no api_key in keyring') || low.includes('missing credential') || low.includes('credentials_missing')) {
    return 'Нет ключа: сохраните API-ключ провайдера в настройках (ключ хранится только в системном хранилище).'
  }
  if (low.includes('unauthorized') || low.includes('invalid_key') || low.includes('invalid key')) {
    return 'Ключ отклонён провайдером (unauthorized). Проверьте ключ в настройках.'
  }
  if (low.includes('privacy') || low.includes('local-only') || low.includes('cloud deny')) {
    return 'Заблокировано политикой приватности: задача local-only, облако запрещено.'
  }
  if (low.includes('is disabled')) {
    return 'Провайдер выключен в настройках нейросетей. Включите его или выберите другого.'
  }
  if (low.includes('offline') || low.includes('timeout') || low.includes('network')) {
    return 'Сеть/таймаут: провайдер недоступен. Проверьте соединение и endpoint.'
  }
  if (low.includes('provider not found') || low.includes('model')) {
    return `Провайдер/модель недоступны: ${raw.slice(0, 200)}`
  }
  return raw.slice(0, 250)
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
  // AskOnce consent gate: when the backend reports `required`, we surface a
  // real pre-send dialog instead of a static error. The draft is preserved and
  // no message/request is created until the user confirms.
  const [pendingConsent, setPendingConsent] = useState<{ providerId: string; providerName: string; model: string; text: string; destination: string } | null>(null)
  const [consentChecking, setConsentChecking] = useState(false)
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
  // Monotonic request sequence: every send() and stop() bumps it. Only the
  // send whose captured id still matches may publish state or clear busy, so
  // a late response after stop / a newer send is dropped deterministically.
  const reqSeq = useRef(0)
  const activeReqId = useRef<string | null>(null)
  const mounted = useRef(true)
  // Monotonic generation: every load()/retry is a new generation; only the
  // latest one may publish state. mounted guards unmount, generation guards
  // staleness between overlapping attempts on a live component.
  const loadGen = useRef(0)
  // Composer presentation state: collapsed until focused / has content.
  // The card itself uses natural height (container decides the space), only
  // the textarea grows — no fixed pixel height that can clip its controls.
  const [focused, setFocused] = useState(false)
  const taRef = useRef<HTMLTextAreaElement>(null)

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
    // Unmount invalidates any in-flight chat so its continuations no-op.
    return () => {
      mounted.current = false
      reqSeq.current += 1
      const rid = activeReqId.current
      activeReqId.current = null
      if (rid) { void llm.llmChatCancel(rid).catch(() => { /* best effort */ }) }
    }
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

  // Auto-grow the textarea up to the reference cap (160px), min one line.
  useEffect(() => {
    const el = taRef.current
    if (!el) return
    el.style.height = 'auto'
    const next = Math.min(160, Math.max(24, el.scrollHeight))
    el.style.height = `${next}px`
  }, [input])

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
    // Invalidate the in-flight request (late response/error is dropped) and
    // ask the backend to abort the client-side wait. Honest message: the
    // provider may already have processed the request server-side.
    reqSeq.current += 1
    const rid = activeReqId.current
    activeReqId.current = null
    setBusy(false)
    setMsgs((m) => [...m, { role: 'assistant', content: 'Остановлено пользователем.' }])
    if (rid) { void llm.llmChatCancel(rid).catch(() => { /* best effort */ }) }
  }

  // Actual dispatch. Only ever called once per user-confirmed message, so a
  // cancelled consent dialog cannot create a duplicate message/request.
  const doSend = async (text: string, pid: string, mdl: string) => {
    const u: Msg = { role: 'user', content: text }
    const history = [...msgs, u]
    const myId = (reqSeq.current += 1)
    const rid = (typeof crypto !== 'undefined' && 'randomUUID' in crypto)
      ? (crypto as Crypto).randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}-${myId}`
    const requestId = rid
    activeReqId.current = requestId
    const isCurrent = () => mounted.current && reqSeq.current === myId
    setMsgs(history)
    setInput('')
    setBusy(true)
    try {
      const res = await llm.llmChatUniversal(pid, mdl, history.map(m=>({role:m.role, content:m.content})), requestId)
      if (!isCurrent()) return
      // Local legacy providers return raw JSON; cloud returns normalized text.
      setMsgs((m) => [...m, { role: 'assistant', content: extractChatText(res) }])
    } catch (e) {
      if (!isCurrent()) return
      setMsgs((m)=>[...m, {role:'assistant', content: classifyError(e)}])
    } finally {
      if (isCurrent()) { setBusy(false); activeReqId.current = null }
    }
  }

  const send = async () => {
    if (!input.trim() || busy || consentChecking) return
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
    // Cloud AskOnce gate: query the backend consent binding before dispatch.
    // A `required` result opens the dialog; nothing is sent or cleared.
    if (selected && !isLocalKind(selected.kind)) {
      setConsentChecking(true)
      try {
        const st = await llm.llmCloudConsentStatus(providerId, 'task-chat')
        if (!mounted.current) return
        if (st.required) {
          setPendingConsent({ providerId, providerName: selected.name, model, text: input, destination: st.destination })
          return
        }
      } catch (e) {
        // Never silently bypass the consent check on a backend error.
        setBlocked('Не удалось проверить согласие на отправку в облако: ' + (e instanceof Error ? e.message : String(e)))
        return
      } finally {
        setConsentChecking(false)
      }
    }
    await doSend(input, providerId, model)
  }

  // Confirm consent: re-verify the provider/model/draft snapshot is still
  // current, grant the binding, then perform the single dispatch.
  const confirmConsent = async () => {
    const pc = pendingConsent
    if (!pc) return
    if (pc.providerId !== providerId || pc.model !== model || pc.text !== input) {
      setPendingConsent(null)
      setBlocked('Провайдер, модель или текст изменились — отправьте сообщение заново.')
      return
    }
    try {
      await llm.llmCloudConsentGrant(pc.providerId, 'task-chat')
    } catch (e) {
      setBlocked('Не удалось сохранить согласие: ' + (e instanceof Error ? e.message : String(e)))
      return
    }
    if (!mounted.current) return
    setPendingConsent(null)
    await doSend(pc.text, pc.providerId, pc.model)
  }

  // Cancel keeps the draft untouched and sends nothing.
  const cancelConsent = () => setPendingConsent(null)

  // Collapsed/expanded is a max-width + chrome change; height stays natural
  // so every control is always inside the visible card.
  const expanded = focused || input.trim().length > 0

  // Provider/model pickers. Data is exclusively project config + real
  // discovery; cloud keeps the existing free-text model input because the
  // project exposes no remote model list.
  const modelControl = (() => {
    if (configState !== 'ready') {
      return <span className="ai-chip">{configState === 'loading' ? 'Загрузка…' : '—'}</span>
    }
    if (isOllama) {
      if (ollamaState === 'loading' || ollamaState === 'idle') {
        return <span className="ai-chip">Загрузка моделей Ollama…</span>
      }
      if (ollamaState === 'error') {
        return (
          <span className="ai-chip ai-chip-error">
            {ollamaError}{' '}
            <button type="button" className="ai-mini-btn" onClick={() => selected && discoverOllama(selected.id)}>↻ Обновить</button>
          </span>
        )
      }
      if (ollamaModels.length === 0) {
        return (
          <span className="ai-chip">
            Нет моделей — выполните <code>ollama pull &lt;name&gt;</code>.{' '}
            <button type="button" className="ai-mini-btn" onClick={() => selected && discoverOllama(selected.id)}>↻ Обновить</button>
          </span>
        )
      }
      return (
        <>
          <ComposerMenu ariaLabel="Модель" options={ollamaModels.map((mid) => ({ id: mid, label: mid }))} value={model} onSelect={setModel}>
            <span className="ai-menu-label">{model || 'Модель'}</span>
          </ComposerMenu>
          <button type="button" className="ai-mini-btn" aria-label="↻ Обновить" onClick={() => selected && discoverOllama(selected.id)}>↻</button>
        </>
      )
    }
    if (isLocal) {
      if (!registryLoaded) return <span className="ai-chip">Загрузка моделей…</span>
      if (eligible.length === 0) {
        return <span className="ai-chip">Нет установленных моделей — откройте настройки → Модели и нажмите «Сканировать реестр».</span>
      }
      return (
        <ComposerMenu ariaLabel="Модель" options={eligible.map((r) => ({ id: r.id, label: r.filename }))} value={model} onSelect={setModel}>
          <span className="ai-menu-label">{eligible.find((r) => r.id === model)?.filename ?? (model || 'Модель')}</span>
        </ComposerMenu>
      )
    }
    // Cloud: the project exposes no real remote model list — keep the field.
    return (
      <input className="ai-model-input" value={model} onChange={(e) => setModel(e.target.value)} placeholder="модель (пусто = default провайдера)" aria-label="Модель" />
    )
  })()

  return (
    <div style={{ display:'flex', flexDirection:'column', flex:1, minHeight:0 }}>
      <div className="ai-chat-head">
        <span style={{ fontWeight:700 }}>AI Чат{selected ? ` — ${selected.name}` : ''}</span>
        <span style={{ flex:1 }} />
        <button onClick={check} className="ai-ghost-btn">Статус</button>
        <span style={{ fontSize:11, color:'var(--muted)' }}>{status}</span>
      </div>
      {blocked && <div role="alert" style={{ margin:'8px 12px 0', padding:'8px 10px', background:'rgba(255,180,80,.08)', border:'1px solid var(--border)', borderRadius:8, fontSize:12 }}>{blocked}</div>}
      {configState === 'error' && (
        <div role="alert" style={{ margin:'8px 12px 0', padding:'8px 10px', background:'rgba(255,80,80,.08)', border:'1px solid rgba(255,80,80,.4)', borderRadius:8, fontSize:12 }}>
          Не удалось загрузить конфигурацию{configError ? `: ${configError.slice(0, 200)}` : '. Проверьте backend.'}{' '}
          <button onClick={load} style={{ padding:'4px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12 }}>↻ Повторить</button>
        </div>
      )}
      <div style={{ flex:1, minHeight:0, overflow:'auto', padding:12, display:'flex', flexDirection:'column', gap:8 }}>
        {msgs.map((m,i)=> (
          <div key={i} style={{ alignSelf: m.role==='user'?'flex-end':'flex-start', maxWidth:'80%', padding:'8px 12px', borderRadius:12, background: m.role==='user'?'rgba(130,168,255,.14)':'var(--panel-2)', border:'1px solid var(--border)', fontSize:13 }}>{m.content}</div>
        ))}
      </div>
      <div className={`ai-composer${expanded ? ' is-expanded' : ' is-collapsed'}`}>
        <div className="ai-card">
          <textarea
            ref={taRef}
            className="ai-input"
            value={input}
            rows={1}
            placeholder="Сообщение провайдеру…"
            aria-label="Сообщение провайдеру"
            onChange={(e) => setInput(e.target.value)}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                send()
              }
            }}
          />
          <div className="ai-actions">
            <div className="ai-left">
              <ComposerMenu
                ariaLabel="Провайдер"
                options={providers.map((p) => ({ id: p.id, label: `${p.name} (${isLocalKind(p.kind) ? 'local' : 'cloud'})` }))}
                value={providerId}
                onSelect={onProvider}
                disabled={configState !== 'ready' || providers.length === 0}
              >
                <span className="ai-menu-label">
                  {configState === 'loading'
                    ? 'Загрузка…'
                    : providers.length === 0
                      ? (configState === 'ready' ? 'Нет провайдеров' : '—')
                      : selected?.name ?? 'Провайдер'}
                </span>
              </ComposerMenu>
              {modelControl}
            </div>
            <div className="ai-right">
              {busy
                ? <button type="button" className="ai-send ai-stop" aria-label="Стоп" onClick={stop}><span aria-hidden="true">■</span></button>
                : <button type="button" className="ai-send" aria-label="Отправить" onClick={send} disabled={!input.trim()}><span aria-hidden="true">↑</span></button>}
            </div>
          </div>
        </div>
      </div>
      {pendingConsent && (
        <div role="dialog" aria-modal="true" aria-label="Согласие на отправку в облако" style={{ position:'fixed', inset:0, background:'rgba(0,0,0,.6)', display:'flex', alignItems:'center', justifyContent:'center', zIndex:1000 }}>
          <div style={{ background:'var(--panel)', border:'1px solid var(--border)', borderRadius:12, padding:16, maxWidth:460, width:'90%', fontSize:13, display:'flex', flexDirection:'column', gap:10 }}>
            <div style={{ fontWeight:700 }}>Отправка в облако — требуется согласие</div>
            <div>Провайдер: <b>{pendingConsent.providerName}</b></div>
            <div>Назначение: <code>{pendingConsent.destination}</code></div>
            <div style={{ color:'var(--muted)' }}>
              В облако будет отправлена история диалога, включая это сообщение. Запрос не будет выполнен до явного подтверждения.
              Согласие привязано к провайдеру, назначению и профилю task-chat и действует для этой сессии.
            </div>
            <div style={{ display:'flex', justifyContent:'flex-end', gap:8 }}>
              <button type="button" onClick={cancelConsent} style={{ padding:'6px 12px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6 }}>Отмена</button>
              <button type="button" onClick={confirmConsent} style={{ padding:'6px 12px', background:'var(--accent)', color:'#fff', border:'1px solid var(--border)', borderRadius:6 }}>Разрешить и отправить</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
