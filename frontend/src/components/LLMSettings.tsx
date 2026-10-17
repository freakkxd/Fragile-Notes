import { useEffect, useRef, useState } from 'react'
import * as llm from '../lib/llm'
import {
  SettingsMode, SettingsSection, MODE_LABELS, SECTION_LABELS,
  loadMode, saveMode, snapshotConfig, isDirty, isLocalKind, redactedDiagnostics,
  ollamaDiscoverErrorText,
} from './settings/types'
import { SectionCard, SettingRow, StatusBadge, SecretReferenceField, inputStyle, Button, Select } from './settings/controls'
import { KNOWN_PROVIDER_KINDS, PERPLEXITY_PRESETS } from '../lib/llm'

// Helper to open external URL (OAuth login)
async function openExternal(url: string) {
  const w = window as unknown as { __TAURI__?: { shell?: { open: (u:string)=>void }, invoke?: (cmd:string,args?:unknown)=>Promise<unknown> } }
  try {
    if (w.__TAURI__?.shell?.open) { w.__TAURI__.shell.open(url); return }
    if (w.__TAURI__?.invoke) { await w.__TAURI__.invoke('plugin:shell|open', { path: url }); return }
  } catch {}
  window.open(url, '_blank')
}

type TaskProfileExt = llm.TaskProfile & { generation?: Record<string, string> }
function genOf(t: llm.TaskProfile, key: string, fallback: string): string {
  const g = (t as TaskProfileExt).generation
  return (g && g[key]) ?? fallback
}
function setGen(t: llm.TaskProfile, key: string, value: string): llm.TaskProfile {
  const prev = ((t as TaskProfileExt).generation ?? {}) as Record<string, string>
  if (!value.trim()) {
    const next = { ...prev }
    delete next[key]
    return { ...t, generation: next } as llm.TaskProfile
  }
  return { ...t, generation: { ...prev, [key]: value } } as llm.TaskProfile
}

export default function LLMSettings() {
  const [mode, setMode] = useState<SettingsMode>(() => loadMode())
  const [section, setSection] = useState<SettingsSection>('general')
  const [cfg, setCfg] = useState<llm.LlmConfig | null>(null)
  const [savedSnapshot, setSavedSnapshot] = useState('')
  const [models, setModels] = useState<llm.ModelInfo[]>([])
  const [registry, setRegistry] = useState<llm.RegistryModel[]>([])
  const [registryStatus, setRegistryStatus] = useState('')
  const [status, setStatus] = useState('загрузка...')
  const [issues, setIssues] = useState<llm.LlmConfigIssue[]>([])
  const [recovered, setRecovered] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)
  // Gate 1: backend report classification + in-memory recovery token (never persisted)
  const [reportStatus, setReportStatus] = useState<llm.ConfigLoadStatus | null>(null)
  const [reportReason, setReportReason] = useState<llm.ConfigDegradedReason | null>(null)
  const [activePath, setActivePath] = useState('')
  const [migratedFrom, setMigratedFrom] = useState<number | null>(null)
  const [recoverToken, setRecoverToken] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [cloudStatus, setCloudStatus] = useState<Record<string,string>>({})
  const [discover, setDiscover] = useState<Record<string, { state: 'idle'|'loading'|'ready'|'error'; models: string[]; error: string }>>({})
  const [keyring, setKeyring] = useState<Record<string, boolean | undefined>>({})
  const [localTest, setLocalTest] = useState('—')
  const [pipelineInput, setPipelineInput] = useState('Тестовая заметка #идея: сделать Vault умнее')
  const [pipelineOut, setPipelineOut] = useState('')
  const [selectedProvider, setSelectedProvider] = useState<string | null>(null)
  const [simpleProvider, setSimpleProvider] = useState<string>('')
  const [simpleModel, setSimpleModel] = useState<string>('')
  const [scopeFilter, setScopeFilter] = useState<'all'|'local'|'cloud'>('all')
  const [saving, setSaving] = useState(false)
  // OpenRouter browser-connect transactions, keyed by provider id. The
  // frontend only ever holds a tx id + safe status; the code/verifier/key stay
  // in the backend.
  const [orOauth, setOrOauth] = useState<Record<string, { txId?: string; status?: llm.OpenRouterOauthState; destination?: string; error?: string }>>({})
  const orPoll = useRef<Record<string, number>>({})
  // OAuth poll lifecycle: a per-provider monotonic generation guards every
  // publish/reschedule path so a stale in-flight status response (from an
  // earlier transaction, a reconnect, a cancel or an unmount) can never
  // overwrite the current state or reschedule itself.
  const orPollGen = useRef<Record<string, number>>({})
  const [showIssueDetails, setShowIssueDetails] = useState(false)
  const statusTimer = useRef<number | null>(null)
  // Discovery lifecycle: a monotonic generation guards every publish path so
  // a late response never lands after an endpoint edit, provider removal,
  // newer discovery request, or unmount.
  const discoverGen = useRef(0)
  const mountedRef = useRef(true)

  const flash = (msg: string) => {
    setStatus(msg)
    if (statusTimer.current) window.clearTimeout(statusTimer.current)
    statusTimer.current = window.setTimeout(() => setStatus(''), 2500)
  }

  const load = async () => {
    setLoadFailed(false)
    setStatus('загрузка...')
    setSaveError('')
    setRecoverToken(null)
    setConfirming(false)
    try {
      const res = await llm.llmGetConfigSafe()
      setIssues(res.issues)
      setRecovered(res.recovered)
      setReportStatus(res.reportStatus ?? null)
      setReportReason(res.reportReason ?? null)
      setActivePath(res.activePath ?? '')
      setMigratedFrom(res.migratedFrom ?? null)
      if (!res.config) {
        setCfg(null)
        setLoadFailed(true)
        setStatus('конфиг недоступен')
        return
      }
      setCfg(res.config)
      setSavedSnapshot(snapshotConfig(res.config))
      if (!simpleProvider && res.config.providers.length > 0) {
        const first = res.config.providers.find(p => p.enabled) ?? res.config.providers[0]
        setSimpleProvider(first.id)
        setSimpleModel(first.default_model || first.model || '')
      }
      setStatus(res.recovered
        ? `конфиг загружен с предупреждениями (${res.issues.length})`
        : 'конфиг загружен')
      try {
        const m = await llm.llmScanModels(res.config.local.models_dir)
        setModels(m)
      } catch { /* models optional for settings usability */ }
      // keyring states (presence only, never values)
      const states: Record<string, boolean | undefined> = {}
      for (const p of res.config.providers) {
        if (p.auth_method === 'api_key' || (p.auth && p.auth.method === 'api_key')) {
          try { states[p.id] = await llm.llmHasProviderCredential(p.id) } catch { states[p.id] = undefined }
        }
      }
      setKeyring(states)
    } catch {
      setCfg(null)
      setLoadFailed(true)
      setStatus('конфиг недоступен')
    }
  }
  useEffect(()=>{ load() },[])
  // Unmount: stop publishing and invalidate any in-flight discovery.
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      discoverGen.current += 1
      // Stop any OpenRouter status polling; the backend tx keeps its own TTL.
      Object.values(orPoll.current).forEach((t) => window.clearTimeout(t))
      orPoll.current = {}
    }
  }, [])

  const changeMode = (m: SettingsMode) => { setMode(m); saveMode(m) }

  const dirty = cfg ? isDirty(savedSnapshot, cfg) : false
  const degraded = reportStatus === 'degraded'
  const saveBlocked = degraded && !recoverToken
  const save = async () => {
    if(!cfg || saving) return
    setSaveError('')
    if (degraded) {
      // Gate 1: no silent destructive write. Token comes only from
      // explicit recovery start; without it the backend rejects too.
      if (!recoverToken) {
        setSaveError('Сохранение заблокировано: конфигурация повреждена. Нажмите «Начать восстановление».')
        return
      }
      setConfirming(true)
      return
    }
    setSaving(true)
    try {
      await llm.llmSaveConfig(cfg)
      setSavedSnapshot(snapshotConfig(cfg))
      flash('сохранено ✓')
    } catch { setStatus('не удалось сохранить: повторите попытку') }
    finally { setSaving(false) }
  }
  const startRecovery = async () => {
    setSaveError('')
    try {
      const r = await llm.llmStartConfigRecovery()
      setRecoverToken(r.token)
      setConfirming(true)
    } catch {
      setSaveError('Не удалось начать восстановление: повторите попытку.')
    }
  }
  const doConfirmedSave = async () => {
    if (!cfg || !recoverToken || saving) return
    setSaving(true)
    try {
      await llm.llmSaveConfigConfirmed(cfg, recoverToken)
      setRecoverToken(null)
      setConfirming(false)
      await load()
      flash('восстановлено и сохранено ✓')
    } catch {
      setSaveError('Подтверждённая запись не удалась: оригинал не тронут. Перезагрузите и повторите.')
    }
    finally { setSaving(false) }
  }
  const cancelConfirm = () => { setConfirming(false); setRecoverToken(null) }
  const cancelEdit = async () => { await load() }

  const scan = async () => {
    if(!cfg) return
    try {
      const m = await llm.llmScanModels(cfg.local.models_dir)
      setModels(m)
      flash(`найдено ${m.length} моделей`)
    } catch { setStatus('не удалось сканировать модели') }
  }

  // Registry-backed scan/list (GGUF-verified states). Backend defaults only.
  const loadRegistry = async () => {
    setRegistryStatus('сканирование реестра…')
    try {
      await llm.llmModelsScan()
      const list = await llm.llmModelsList()
      setRegistry(list)
      const present = list.filter((r) => r.state === 'Present').length
      setRegistryStatus(`реестр: ${present} Present из ${list.length}`)
    } catch {
      setRegistryStatus('реестр недоступен')
    }
  }
  useEffect(()=>{ if(section==='models') loadRegistry() },[section])

  const setActive = async (path: string) => {
    try {
      await llm.llmSetActiveModel(path)
      if(cfg) setCfg({...cfg, local:{...cfg.local, active_model: path}})
      flash(`активна: ${path.split('/').pop()}`)
    } catch { setStatus('не удалось выбрать модель') }
  }

  const testLocal = async () => {
    setLocalTest('проверка...')
    try {
      const r = await llm.llmTestProvider('local')
      setLocalTest(r.slice(0,300))
    } catch { setLocalTest('ошибка проверки') }
  }

  const testProvider = async (id: string) => {
    setCloudStatus(s=>({...s,[id]:'проверка...'}))
    try {
      const r = await llm.llmTestProvider(id)
      setCloudStatus(s=>({...s,[id]: r.slice(0,200)}))
    } catch { setCloudStatus(s=>({...s,[id]:'ошибка проверки'})) }
  }

  // Ollama-native discovery (GET /api/tags). Never substitutes an endpoint.
  const discoverModels = async (id: string) => {
    const gen = (discoverGen.current += 1)
    const isCurrent = () => mountedRef.current && discoverGen.current === gen
    setDiscover(s=>({...s,[id]:{ state:'loading', models:[], error:'' }}))
    try {
      const res = await llm.llmDiscoverModels(id)
      if (!isCurrent()) return
      if (res.ok) {
        setDiscover(s=>({...s,[id]:{ state:'ready', models: res.models ?? [], error:'' }}))
      } else {
        setDiscover(s=>({...s,[id]:{ state:'error', models:[], error: ollamaDiscoverErrorText(res.error?.code) }}))
      }
    } catch {
      if (!isCurrent()) return
      setDiscover(s=>({...s,[id]:{ state:'error', models:[], error: ollamaDiscoverErrorText(undefined) }}))
    }
  }
  // Invalidate any in-flight/finished discovery for a provider: bump the
  // generation and drop its result so a stale model list can never be shown
  // or selected (endpoint edit, provider removal).
  const invalidateDiscovery = (id: string) => {
    discoverGen.current += 1
    setDiscover(s => { const n = { ...s }; delete n[id]; return n })
  }

  const refreshKeyring = async (id: string) => {
    try { setKeyring(s => ({...s, [id]: undefined})); const v = await llm.llmHasProviderCredential(id); setKeyring(s => ({...s, [id]: v})) }
    catch { setKeyring(s => ({...s, [id]: undefined})) }
  }

  const patchOrOauth = (pid: string, patch: Partial<{ txId?: string; status?: llm.OpenRouterOauthState; destination?: string; error?: string }>) => {
    setOrOauth(s => ({ ...s, [pid]: { ...(s[pid] ?? {}), ...patch } }))
  }

  // Poll a transaction to a terminal state. Stops on connected/cancelled/
  // failed/expired or after a safety deadline (the backend TTL is the real cap).
  // Every publish and reschedule is guarded by `gen` so a superseded poll can
  // never land state for a stale transaction.
  const pollOrOauth = (pid: string, txId: string, deadline: number, gen: number) => {
    const isCurrent = () => mountedRef.current && orPollGen.current[pid] === gen
    const tick = async () => {
      if (!isCurrent()) return
      try {
        const st = await llm.llmOpenRouterOauthStatus(txId)
        if (!isCurrent()) return
        patchOrOauth(pid, { txId, status: st.status, destination: st.destination, error: st.error ?? undefined })
        if (st.status === 'connected') {
          await refreshKeyring(pid)
          return
        }
        if (st.status === 'pending' || st.status === 'processing') {
          if (Date.now() < deadline) {
            orPoll.current[pid] = window.setTimeout(tick, 1500)
          } else {
            void cancelOrOauth(pid, txId)
          }
        }
      } catch (e) {
        if (!isCurrent()) return
        patchOrOauth(pid, { status: 'failed', error: e instanceof Error ? e.message : String(e) })
      }
    }
    orPoll.current[pid] = window.setTimeout(tick, 1200)
  }

  const startOrOauth = async (pid: string) => {
    // Bump the generation synchronously so a rapid second start (or a cancel)
    // invalidates this attempt before its first await resolves.
    const gen = (orPollGen.current[pid] = (orPollGen.current[pid] ?? 0) + 1)
    const prev = orPoll.current[pid]
    if (prev) { window.clearTimeout(prev); delete orPoll.current[pid] }
    if (!mountedRef.current) return
    try {
      const s = await llm.llmOpenRouterOauthStart(pid)
      if (!mountedRef.current || orPollGen.current[pid] !== gen) return
      patchOrOauth(pid, { txId: s.tx_id, status: 'pending', error: undefined, destination: undefined })
      await openExternal(s.auth_url)
      if (!mountedRef.current || orPollGen.current[pid] !== gen) return
      pollOrOauth(pid, s.tx_id, Date.now() + 6 * 60 * 1000, gen)
    } catch (e) {
      if (!mountedRef.current || orPollGen.current[pid] !== gen) return
      patchOrOauth(pid, { status: 'failed', error: e instanceof Error ? e.message : String(e) })
    }
  }

  const cancelOrOauth = async (pid: string, txIdOverride?: string) => {
    // Invalidate any in-flight poll for this pid so a late status response
    // cannot resurrect the cancelled transaction.
    orPollGen.current[pid] = (orPollGen.current[pid] ?? 0) + 1
    const t = orPoll.current[pid]
    if (t) { window.clearTimeout(t); delete orPoll.current[pid] }
    const txId = txIdOverride ?? orOauth[pid]?.txId
    if (txId) { try { await llm.llmOpenRouterOauthCancel(txId) } catch { /* best effort */ } }
    patchOrOauth(pid, { status: 'cancelled', txId: undefined })
  }

  const patchProvider = (id: string, patch: Partial<llm.Provider>) => {
    setCfg(c => c ? {...c, providers: c.providers.map(x => x.id===id ? {...x, ...patch} : x)} : c)
  }

  const addProvider = (kind: string) => {
    if(!cfg) return
    const id = 'custom-' + Math.random().toString(36).slice(2,6)
    const p: llm.Provider = {
      id, name: 'Новый провайдер', kind: kind as llm.Provider['kind'],
      enabled: false, endpoint: '', api_url: '', auth_method: 'none',
      api_key: '', model: '', default_model: '', extra: {},
    }
    setCfg({...cfg, providers: [...cfg.providers, p]})
    setSelectedProvider(id)
  }

  const removeProvider = (id: string) => {
    if(!cfg) return
    if(!window.confirm(`Удалить провайдера ${id}? Это изменит конфиг после сохранения.`)) return
    invalidateDiscovery(id)
    setCfg({...cfg, providers: cfg.providers.filter(p => p.id !== id)})
    if (selectedProvider === id) setSelectedProvider(null)
    if (simpleProvider === id) {
      const rest = cfg.providers.filter(p => p.id !== id)
      const next = rest.find(p => p.enabled) ?? rest[0]
      setSimpleProvider(next ? next.id : '')
      setSimpleModel(next ? (next.default_model || next.model || '') : '')
    }
  }

  // ---- recovery / loading states (Phase 2 compat, all modes) ----
  if(!cfg) return (
    <div style={{padding:16, display:'flex', flexDirection:'column', gap:10}}>
      <div style={{fontWeight:700}}>Настройки нейросетей недоступны</div>
      <div style={{fontSize:12, color:'var(--muted)'}}>
        {loadFailed
          ? 'Не удалось загрузить конфигурацию. Проверьте backend и повторите.'
          : 'Загрузка LLM конфига…'}
      </div>
      {issues.length > 0 && (
        <div role="alert" style={{fontSize:12, color:'var(--fg)', background:'rgba(255,180,80,.08)', border:'1px solid var(--border)', borderRadius:8, padding:8}}>
          {issues.map((it, i) => <div key={i}>• {it.message}</div>)}
        </div>
      )}
      <div><button onClick={load} style={{padding:'6px 12px', background:'var(--accent)', border:0, borderRadius:8, fontWeight:700, cursor:'pointer'}}>↻ Повторить</button></div>
    </div>
  )

  const visibleProviders = cfg.providers.filter(p =>
    scopeFilter === 'all' ? true : scopeFilter === 'local' ? isLocalKind(p.kind) : !isLocalKind(p.kind))
  const simpleProv = cfg.providers.find(p => p.id === simpleProvider) ?? null
  const sections: SettingsSection[] = ['general','providers','models','runtime','privacy','pipelines','diagnostics']

  return (
    <div style={{display:'flex', flexDirection:'column', height:'100%', background:'var(--bg)'}}>
      {/* header: search placeholder + mode switch */}
      <div style={{display:'flex', gap:8, padding:'8px 10px', borderBottom:'1px solid var(--border)', alignItems:'center', flexWrap:'wrap'}}>
        <strong style={{fontSize:13}}>Настройки</strong>
        <span style={{fontSize:11, color:'var(--muted)'}}>Палитра: Ctrl+P</span>
        <div role="group" aria-label="Режим настроек" style={{marginLeft:'auto', display:'flex', gap:4}}>
          {(['simple','default','expert'] as SettingsMode[]).map(m => (
            <button key={m} onClick={()=>changeMode(m)} aria-pressed={mode===m}
              style={{padding:'4px 10px', borderRadius:8, border:'1px solid var(--border)', background: mode===m?'var(--accent)':'var(--panel-2)', color: mode===m?'#0b0b0b':'var(--fg)', fontSize:12, fontWeight: mode===m?700:400}}>
              {MODE_LABELS[m]}
            </button>
          ))}
        </div>
      </div>

      {recovered && issues.length > 0 && !degraded && (
        <div role="alert" style={{margin:'8px 10px 0', padding:'8px 10px', background:'rgba(255,180,80,.08)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>
          <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
            <span><b>Часть конфигурации пропущена</b> <span style={{color:'var(--muted)'}}>({issues.length}) — рабочие настройки доступны.</span></span>
            <span style={{marginLeft:'auto', display:'flex', gap:6}}>
              <button onClick={()=>setShowIssueDetails(v=>!v)} aria-expanded={showIssueDetails} style={{padding:'4px 10px', background:'transparent', border:'1px solid var(--border)', borderRadius:6, fontSize:12}}>Подробнее</button>
              <button onClick={load} style={{padding:'4px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12}}>↻ Перезагрузить</button>
            </span>
          </div>
          {showIssueDetails && (
            <div style={{marginTop:6, display:'flex', flexDirection:'column', gap:2}}>
              {issues.map((it, i) => <div key={i}>• {it.message}</div>)}
            </div>
          )}
        </div>
      )}

      {reportStatus === 'missing' && (
        <div role="status" style={{margin:'8px 10px 0', padding:'8px 10px', background:'rgba(120,180,255,.07)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>
          Первый запуск: файл конфигурации отсутствует и будет создан при сохранении. Текущие настройки — безопасные значения по умолчанию.
        </div>
      )}

      {reportStatus === 'migrated' && (
        <div role="status" style={{margin:'8px 10px 0', padding:'8px 10px', background:'rgba(120,180,255,.07)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>
          Конфигурация обновлена со схемы v{migratedFrom ?? 1} (в памяти, оригинал не тронут). При сохранении будет создан бэкап <code>llm.json.bak-v{migratedFrom ?? 1}-&lt;UTC&gt;</code>.
        </div>
      )}

      {degraded && (
        <div role="alert" style={{margin:'8px 10px 0', padding:'10px 12px', background:'rgba(255,80,80,.08)', border:'1px solid rgba(255,80,80,.4)', borderRadius:8, fontSize:12}}>
          <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
            <span><b>Конфигурация повреждена: {REASON_RU[reportReason ?? 'corrupt']}</b></span>
            <span style={{marginLeft:'auto', display:'flex', gap:6}}>
              <button onClick={()=>setShowIssueDetails(v=>!v)} aria-expanded={showIssueDetails} style={{padding:'4px 10px', background:'transparent', border:'1px solid var(--border)', borderRadius:6, fontSize:12}}>Подробнее</button>
              {!recoverToken
                ? <button onClick={startRecovery} style={{padding:'4px 10px', background:'var(--accent)', border:0, borderRadius:6, fontSize:12, fontWeight:700}}>Начать восстановление</button>
                : <button onClick={()=>setConfirming(true)} style={{padding:'4px 10px', background:'var(--accent)', border:0, borderRadius:6, fontSize:12, fontWeight:700}}>Продолжить восстановление</button>}
              <button onClick={load} style={{padding:'4px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12}}>↻ Перезагрузить</button>
            </span>
          </div>
          <div style={{marginTop:4, color:'var(--muted)'}}>Сохранение заблокировано до явного подтверждения. Оригинал не тронут. {activePath ? <>Активный путь: <code>{activePath}</code></> : null}</div>
          {showIssueDetails && (
            <div style={{marginTop:6, display:'flex', flexDirection:'column', gap:2}}>
              {issues.length === 0 && <div>• Детали недоступны без изменения оригинала.</div>}
              {issues.map((it, i) => <div key={i}>• {it.message}</div>)}
            </div>
          )}
        </div>
      )}

      {saveError && (
        <div role="alert" style={{margin:'8px 10px 0', padding:'8px 10px', background:'rgba(255,80,80,.08)', border:'1px solid rgba(255,80,80,.4)', borderRadius:8, fontSize:12}}>
          {saveError}
        </div>
      )}

      {confirming && (
        <div role="alertdialog" aria-modal="true" aria-label="Подтверждение восстановления" style={{position:'fixed', inset:0, background:'rgba(0,0,0,.55)', display:'flex', alignItems:'center', justifyContent:'center', zIndex:50}}>
          <div style={{maxWidth:520, width:'90%', background:'var(--panel)', border:'1px solid rgba(255,80,80,.5)', borderRadius:12, padding:16, display:'flex', flexDirection:'column', gap:10}}>
            <div style={{fontWeight:700, fontSize:14}}>Подтвердить destructive-перезапись?</div>
            <div style={{fontSize:12, color:'var(--fg)'}}>
              Оригинал будет сначала скопирован в <code>llm.json.corrupt-&lt;UTC&gt;</code> рядом с конфигом, затем создан обычный бэкап, и только потом выполнена запись.
              Если копирование не удастся — запись будет отклонена, оригинал останется нетронутым.
            </div>
            {issues.length > 0 && (
              <div style={{fontSize:12, maxHeight:140, overflow:'auto', display:'flex', flexDirection:'column', gap:2}}>
                {issues.map((it, i) => <div key={i}>• {it.message}</div>)}
              </div>
            )}
            <div style={{display:'flex', gap:8, justifyContent:'flex-end'}}>
              <button onClick={cancelConfirm} disabled={saving} style={{padding:'6px 12px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>Отмена</button>
              <button onClick={doConfirmedSave} disabled={saving} style={{padding:'6px 12px', background:'rgba(255,80,80,.85)', color:'#fff', border:0, borderRadius:8, fontWeight:700, fontSize:12}}>{saving ? 'Запись…' : 'Я понимаю, перезаписать'}</button>
            </div>
          </div>
        </div>
      )}

      <div style={{display:'flex', flex:1, minHeight:0}}>
        {/* sidebar */}
        <nav aria-label="Разделы настроек" style={{width:150, flexShrink:0, borderRight:'1px solid var(--border)', padding:8, display:'flex', flexDirection:'column', gap:2}}>
          {sections.map(s => (
            <button key={s} onClick={()=>setSection(s)} aria-current={section===s ? 'page' : undefined}
              style={{textAlign:'left', padding:'6px 10px', borderRadius:8, border:0, background: section===s?'var(--panel-2)':'transparent', color: section===s?'var(--fg)':'var(--muted)', fontSize:12, fontWeight: section===s?700:400, cursor:'pointer'}}>
              {SECTION_LABELS[s]}
            </button>
          ))}
        </nav>

        {/* content */}
        <div style={{flex:1, overflow:'auto', padding:12, display:'flex', flexDirection:'column', gap:12, maxWidth:860}}>
          {section==='general' && (
            <SectionCard title="Быстрая настройка">
              <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
                <SettingRow label="Провайдер">
<Select value={simpleProvider} onChange={e=>{
                    const id = e.target.value
                    setSimpleProvider(id)
                    const p = cfg.providers.find(x => x.id===id)
                    if (p) setSimpleModel(p.default_model || p.model || '')
                  }} style={{width:'100%'}}>
                    {visibleProviders.map(p => <option key={p.id} value={p.id}>{p.name} ({p.kind})</option>)}
                  </Select>
                </SettingRow>
                <SettingRow label="Модель">
                  <input value={simpleModel} onChange={e=>setSimpleModel(e.target.value)} placeholder="имя модели" style={inputStyle} />
                </SettingRow>
              </div>
              <div style={{display:'flex', gap:8, flexWrap:'wrap', alignItems:'center'}}>
                <SettingRow label="Область">
                  <select value={scopeFilter} onChange={e=>setScopeFilter(e.target.value as typeof scopeFilter)} style={inputStyle} aria-label="Фильтр провайдеров по области">
                    <option value="all">Все</option>
                    <option value="local">Локальные</option>
                    <option value="cloud">Облачные</option>
</Select>
                </SettingRow>
                <div style={{fontSize:12, color:'var(--muted)'}}>
                  {simpleProv ? <>Область: <b>{isLocalKind(simpleProv.kind) ? 'Локально' : 'Облако'}</b> · {simpleProv.enabled ? 'включён' : 'выключен'}</> : 'Провайдер не выбран'}
                </div>
              </div>
              {simpleProv && <StatusBadge state={simpleProv.enabled ? 'unknown' : 'off'} />}
              <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
                <button onClick={()=>{
                  if(simpleProv){ setSimpleModel(simpleProv.default_model || simpleProv.model || ''); testProvider(simpleProv.id) }
                }} style={{padding:'6px 12px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>Проверить подключение</button>
                <span style={{fontSize:11, color:'var(--muted)', alignSelf:'center'}}>{simpleProv ? (cloudStatus[simpleProv.id] ?? '') : ''}</span>
              </div>
              <div style={{fontSize:11, color:'var(--muted)'}}>Локальные данные не покидают машину. Облачные провайдеры требуют ключа в keyring и явного согласия.</div>
            </SectionCard>
          )}

          {section==='providers' && (
            <>
              <div style={{display:'flex', gap:8, alignItems:'center'}}>
                <strong style={{fontSize:13}}>Провайдеры ({visibleProviders.length})</strong>
                <select value={scopeFilter} onChange={e=>setScopeFilter(e.target.value as typeof scopeFilter)} style={{...inputStyle, width:'auto', marginTop:0}} aria-label="Фильтр">
                  <option value="all">Все</option><option value="local">Локальные</option><option value="cloud">Облачные</option>
                </Select>
                {mode !== 'simple' && (
                  <select defaultValue="" onChange={e=>{ if(e.target.value) addProvider(e.target.value); e.target.value='' }} style={{...inputStyle, width:'auto', marginTop:0}} aria-label="Добавить провайдера">
                    <option value="" disabled>+ Добавить</option>
                    {KNOWN_PROVIDER_KINDS.map(k => <option key={k} value={k}>{k}</option>)}
</Select>
                )}
              </div>
              {visibleProviders.length === 0 && <div style={{fontSize:12, color:'var(--muted)'}}>Нет провайдеров в этой области.</div>}
              {visibleProviders.map(p => (
                <div key={p.id} className="card" style={{borderLeft:`3px solid ${p.enabled?'var(--accent)':'var(--border)'}`}}>
                  <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
                    <strong style={{flex:1, fontSize:13}}>{p.name} <span style={{fontWeight:400, color:'var(--muted)', fontSize:11}}>({p.kind})</span></strong>
                    {p.enabled ? <StatusBadge state="unknown" /> : <StatusBadge state="off" />}
                    <button onClick={()=>setSelectedProvider(s => s===p.id?null:p.id)} aria-expanded={selectedProvider===p.id}
                      style={{padding:'4px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12}}>
                      {selectedProvider===p.id?'Скрыть':'Настроить ›'}
                    </button>
                  </div>
                  {selectedProvider===p.id && (
                    <div style={{display:'flex', flexDirection:'column', gap:8, marginTop:8}}>
                      <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
                        <SettingRow label="Название"><input value={p.name} onChange={e=>patchProvider(p.id,{name:e.target.value})} style={inputStyle} /></SettingRow>
                        <SettingRow label="Включён">
                          <label style={{display:'flex', gap:6, alignItems:'center', fontSize:12, marginTop:4}}>
                            <input type="checkbox" checked={p.enabled} onChange={e=>patchProvider(p.id,{enabled:e.target.checked})} /> вкл
                          </label>
                        </SettingRow>
                      </div>
                      {(mode==='default' || mode==='expert') && (
                      <SettingRow label="Endpoint" hint="Политика: cloud — только https, local — только loopback">
                        <input value={p.endpoint || p.api_url || ''} onChange={e=>{ patchProvider(p.id,{endpoint:e.target.value}); invalidateDiscovery(p.id) }} placeholder={apiUrlPlaceholder(p.kind)} style={inputStyle} />
                      </SettingRow>
                      )}
                      {mode==='expert' && (
                        <>
                          <SettingRow label="Identity"><span style={{fontSize:11, color:'var(--muted)'}}>id: <code>{p.id}</code></span></SettingRow>
                          <SettingRow label="Scope & privacy">
                            <span style={{fontSize:11, color:'var(--muted)'}}>Область: <b>{isLocalKind(p.kind) ? 'local' : 'cloud'}</b> · local-only задачи блокируют cloud</span>
                          </SettingRow>
                          <SettingRow label="Model mapping">
                            <input value={p.default_model || p.model || ''} onChange={e=>patchProvider(p.id,{default_model:e.target.value})} placeholder="remote model id" style={inputStyle} />
                          </SettingRow>
                          <SettingRow label="Capabilities">
                            <span style={{fontSize:11, color:'var(--muted)'}}>Chat · capabilities берутся из каталога моделей, не угадываются</span>
                          </SettingRow>
                        </>
                      )}
                      {!isLocalKind(p.kind) ? (
                        <SecretReferenceField hasSecret={keyring[p.id]}
                          onReplace={async (s)=>{ await llm.llmSetProviderCredential(p.id, s); await refreshKeyring(p.id) }}
                          onDelete={async ()=>{ await llm.llmDeleteProviderCredential(p.id); await refreshKeyring(p.id); patchOrOauth(p.id, { status: undefined, txId: undefined, error: undefined }) }} />
                      ) : (
                        <div style={{fontSize:11, color:'var(--muted)'}}>Локальный провайдер — ключ не нужен.</div>
                      )}
                      {p.kind === 'open_router' && (() => {
                        const st = orOauth[p.id] ?? {}
                        const waiting = st.status === 'pending' || st.status === 'processing'
                        return (
                          <div style={{display:'flex', flexDirection:'column', gap:6, padding:'8px 10px', border:'1px solid var(--border)', borderRadius:8}}>
                            <div style={{fontSize:12, fontWeight:600}}>Вход через браузер (OAuth)</div>
                            <div style={{fontSize:11, color:'var(--muted)'}}>
                              Откроется официальная страница авторизации OpenRouter. Ключ создаётся там и сохраняется в системном хранилище — приложение его не отображает и не пересылает.
                            </div>
                            <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
                              <button type="button" onClick={()=>startOrOauth(p.id)} disabled={waiting}
                                style={{padding:'6px 10px', background:'var(--accent)', color:'#fff', border:'1px solid var(--border)', borderRadius:8, fontSize:12, opacity: waiting ? 0.6 : 1}}>
                                {waiting ? 'Ожидание авторизации…' : 'Подключить через браузер'}
                              </button>
                              {waiting && (
                                <button type="button" onClick={()=>cancelOrOauth(p.id)} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>Отмена</button>
                              )}
                              {st.status === 'connected' && <span style={{fontSize:11, color:'var(--accent)'}}>Подключено</span>}
                              {(st.status === 'cancelled' || st.status === 'expired') && <span style={{fontSize:11, color:'var(--muted)'}}>Не подключено</span>}
                              {st.status === 'failed' && <span style={{fontSize:11, color:'#e57373'}}>Ошибка: {st.error ?? 'не удалось'}</span>}
                            </div>
                            {waiting && st.destination && (
                              <div style={{fontSize:11, color:'var(--muted)'}}>Назначение: <code>{st.destination}</code></div>
                            )}
                          </div>
                        )
                      })()}
                      {p.kind === 'perplexity' && (() => {
                        const current = p.default_model || p.model || ''
                        return (
                          <div style={{display:'flex', flexDirection:'column', gap:6, padding:'8px 10px', border:'1px solid var(--border)', borderRadius:8}}>
                            <div style={{fontSize:12, fontWeight:600}}>Perplexity Agent API</div>
                            <div style={{fontSize:11, color:'var(--muted)'}}>
                              Веб-ответы с источниками. Ключ API покупается отдельно в Perplexity API Console и не входит в подписку Pro/Max — оплата API независима от подписки.
                            </div>
                            <div style={{fontSize:11, color:'var(--muted)'}}>
                              Кнопка «Тест» проверяет доступность API (endpoint); сам ключ подтверждается при первом запросе.
                            </div>
                            <div style={{fontSize:11, color:'var(--muted)'}}>Пресет (поле <code>preset</code>):</div>
                            <div style={{display:'flex', gap:12, flexWrap:'wrap'}}>
                              {PERPLEXITY_PRESETS.map(pr => (
                                <label key={pr} style={{display:'flex', gap:6, alignItems:'center', fontSize:12}}>
                                  <input type="radio" name={`pplx-preset-${p.id}`} checked={current===pr}
                                    onChange={()=>patchProvider(p.id,{default_model:pr})} />
                                  <code>{pr}</code>
                                </label>
                              ))}
                            </div>
                            {mode==='expert' && (
                              <div style={{fontSize:11, color:'var(--muted)'}}>
                                Либо укажите id модели (например <code>openai/gpt-6-luna</code>) в поле «Model mapping» — значение с «/» уходит в поле <code>model</code>.
                              </div>
                            )}
                          </div>
                        )
                      })()}
                      {p.kind === 'ollama' && (() => {
                        const d = discover[p.id] ?? { state: 'idle' as const, models: [] as string[], error: '' }
                        const currentDefault = p.default_model || p.model || ''
                        return (
                          <div style={{display:'flex', flexDirection:'column', gap:6}}>
                            <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
                              <button onClick={()=>discoverModels(p.id)} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>Получить модели Ollama</button>
                              <span style={{fontSize:11, color:'var(--muted)'}}>
                                {d.state==='loading' ? 'загрузка…'
                                  : d.state==='ready' ? `найдено: ${d.models.length}`
                                  : d.state==='error' ? d.error : ''}
                              </span>
                            </div>
                            {d.state==='ready' && d.models.length===0 && (
                              <span style={{fontSize:11, color:'var(--muted)'}}>Список пуст — выполните <code>ollama pull &lt;name&gt;</code>.</span>
                            )}
                            {d.state==='ready' && d.models.length>0 && (
                              <div style={{display:'flex', flexDirection:'column', gap:4}}>
                                {d.models.map(m => (
                                  <label key={m} style={{display:'flex', gap:6, alignItems:'center', fontSize:12}}>
                                    <input type="radio" name={`ollama-default-${p.id}`} checked={currentDefault===m}
                                      onChange={()=>patchProvider(p.id,{default_model:m})} />
                                    <code>{m}</code>
                                  </label>
                                ))}
                              </div>
                            )}
                          </div>
                        )
                      })()}
                      <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
                        <button onClick={()=>testProvider(p.id)} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>Тест</button>
                        <span style={{fontSize:11, color:'var(--muted)'}}>{cloudStatus[p.id] ?? ''}</span>
                        {mode!=='simple' && (
                          <button onClick={()=>removeProvider(p.id)} style={{marginLeft:'auto', padding:'6px 10px', background:'rgba(255,80,80,.12)', border:'1px solid rgba(255,80,80,.3)', borderRadius:8, fontSize:12}}>Удалить</button>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </>
          )}

          {section==='models' && (
            <SectionCard title={`Модели — реестр (${registry.length})`}>
              <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
                <button onClick={loadRegistry} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>🔍 Сканировать реестр</button>
                <span style={{fontSize:12, color:'var(--muted)'}}>{registryStatus}</span>
              </div>
              {registry.length===0 && <div style={{fontSize:12, color:'var(--muted)'}}>Нет записей — положите .gguf в папку моделей и нажмите «Сканировать реестр».</div>}
              <div style={{display:'flex', flexDirection:'column', gap:6, maxHeight:360, overflow:'auto'}}>
                {registry.map(r=>(
                  <div key={r.id} style={{display:'flex', gap:8, alignItems:'center', padding:'8px 10px', background:'rgba(255,255,255,.02)', border:`1px solid ${r.state==='Present'?'var(--accent)':'var(--border)'}`, borderRadius:10}}>
                    <div style={{flex:1, minWidth:0}}>
                      <div style={{fontWeight:700, fontSize:13, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis'}}>{r.filename}</div>
                      <div style={{fontSize:11, color:'var(--muted)', display:'flex', gap:8, flexWrap:'wrap'}}>
                        <span>{(r.size_bytes/1024/1024).toFixed(0)}МБ</span>
                        <StateBadge state={r.state} />
                        {r.diagnostics.length > 0 && <span>{JSON.stringify(r.diagnostics[0]).slice(0,200)}</span>}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </SectionCard>
          )}

          {section==='models' && (
            <SectionCard title={`Модели (.gguf) — ${models.length} шт. — legacy, без проверки`}>
              {models.length===0 && <div style={{fontSize:12, color:'var(--muted)'}}>Пусто — положи .gguf в {cfg.local.models_dir}</div>}
              <div style={{display:'flex', flexDirection:'column', gap:6, maxHeight:360, overflow:'auto'}}>
                {models.map(m=>(
                  <div key={m.path} style={{display:'flex', gap:8, alignItems:'center', padding:'8px 10px', background:'rgba(255,255,255,.02)', border:`1px solid ${cfg.local.active_model===m.path?'var(--accent)':'var(--border)'}`, borderRadius:10}}>
                    <div style={{flex:1, minWidth:0}}>
                      <div style={{fontWeight:700, fontSize:13, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis'}}>{m.name}</div>
                      <div style={{fontSize:11, color:'var(--muted)', display:'flex', gap:8, flexWrap:'wrap'}}><span>{m.quant}</span><span>{m.size_mb}МБ</span><span>{m.task}</span></div>
                    </div>
                    <button onClick={()=>setActive(m.path)} style={{padding:'6px 10px', background: cfg.local.active_model===m.path?'var(--accent)':'var(--panel-2)', color: cfg.local.active_model===m.path?'#0b0b0b':'var(--fg)', border:'1px solid var(--border)', borderRadius:8, fontSize:12, fontWeight:700}}>{cfg.local.active_model===m.path?'✓ Активна':'Сделать активной'}</button>
                  </div>
                ))}
              </div>
              {mode!=='simple' && <div style={{fontSize:11, color:'var(--muted)'}}>Активная модель: <code>{cfg.local.active_model || '— не выбрана —'}</code></div>}
            </SectionCard>
          )}

          {section==='runtime' && (
            <SectionCard title="llama.cpp — бинарь и рантайм">
              <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
                <SettingRow label="Бинарь"><input value={cfg.local.binary_path} onChange={e=>setCfg({...cfg, local:{...cfg.local, binary_path:e.target.value}})} placeholder="llama-server / /usr/bin/llama-server" style={inputStyle} /></SettingRow>
                <SettingRow label="Модели папка"><input value={cfg.local.models_dir} onChange={e=>setCfg({...cfg, local:{...cfg.local, models_dir:e.target.value}})} style={inputStyle} /></SettingRow>
                <SettingRow label="Порт"><input type="number" value={cfg.local.port} onChange={e=>setCfg({...cfg, local:{...cfg.local, port: Number(e.target.value)}})} style={inputStyle} /></SettingRow>
              </div>
              <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
                <button onClick={scan} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>🔍 Сканировать</button>
                <button onClick={testLocal} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>Проверить сервер</button>
                <span style={{fontSize:12, color:'var(--muted)'}}>{localTest}</span>
              </div>
              {mode==='expert' && (
                <div style={{display:'grid', gridTemplateColumns:'1fr 1fr', gap:10}}>
                  <LabeledSlider label={`n_ctx ${cfg.local.n_ctx}`} min={512} max={32768} step={512} value={cfg.local.n_ctx} onChange={v=>setCfg({...cfg, local:{...cfg.local, n_ctx:v}})} />
                  <LabeledSlider label={`n_threads ${cfg.local.n_threads||'auto'}`} min={0} max={32} step={1} value={cfg.local.n_threads} onChange={v=>setCfg({...cfg, local:{...cfg.local, n_threads:v}})} />
                  <LabeledSlider label={`n_gpu_layers ${cfg.local.n_gpu_layers}`} min={0} max={99} step={1} value={cfg.local.n_gpu_layers} onChange={v=>setCfg({...cfg, local:{...cfg.local, n_gpu_layers:v}})} />
                  <LabeledSlider label={`temp ${cfg.local.temp.toFixed(2)}`} min={0} max={2} step={0.05} value={cfg.local.temp} onChange={v=>setCfg({...cfg, local:{...cfg.local, temp:v}})} />
                  <LabeledSlider label={`top_p ${cfg.local.top_p.toFixed(2)}`} min={0} max={1} step={0.05} value={cfg.local.top_p} onChange={v=>setCfg({...cfg, local:{...cfg.local, top_p:v}})} />
                  <LabeledSlider label={`top_k ${cfg.local.top_k}`} min={0} max={100} step={1} value={cfg.local.top_k} onChange={v=>setCfg({...cfg, local:{...cfg.local, top_k:v}})} />
                  <LabeledSlider label={`repeat ${cfg.local.repeat_penalty.toFixed(2)}`} min={1} max={2} step={0.05} value={cfg.local.repeat_penalty} onChange={v=>setCfg({...cfg, local:{...cfg.local, repeat:v}})} />
                </div>
              )}
              <InstallModel modelsDir={cfg.local.models_dir} />
            </SectionCard>
          )}

          {section==='privacy' && (
            <SectionCard title="Приватность — профили задач">
              {cfg.task_profiles.length===0 && <div style={{fontSize:12, color:'var(--muted)'}}>Нет профилей задач.</div>}
              {cfg.task_profiles.map(t => (
                <div key={t.id} style={{display:'flex', gap:8, flexWrap:'wrap', alignItems:'center', padding:'8px 10px', background:'rgba(255,255,255,.02)', border:'1px solid var(--border)', borderRadius:10}}>
                  <strong style={{fontSize:12}}>{t.name}</strong>
                  <span style={{fontSize:11, color:'var(--muted)'}}>model: {t.model_ref || '—'}</span>
                  <label style={{fontSize:11, display:'flex', gap:4, alignItems:'center'}}>privacy
                    <select value={t.privacy ?? ''} onChange={e=>setCfg({...cfg, task_profiles: cfg.task_profiles.map(x => x.id===t.id ? {...x, privacy: e.target.value as llm.TaskProfile['privacy']} : x)})} style={{padding:'4px 6px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:11, color:'var(--fg)'}}>
                      <option value="">—</option><option value="local_only">local_only</option><option value="cloud_allowed">cloud_allowed</option>
  </Select>
                  </label>
                  {mode==='expert' && (
                    <>
                      <label title="0.0 – 2.0, пусто = default backend" style={{fontSize:11}}>temperature <input value={genOf(t,'temperature','')} onChange={e=>setCfg({...cfg, task_profiles: cfg.task_profiles.map(x => x.id===t.id ? setGen(x,'temperature',e.target.value) : x)})} placeholder="0.7" style={{width:64, padding:'4px 6px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:6, color:'var(--fg)', fontSize:11}} /></label>
                      <label title="0.0 – 1.0, пусто = default backend" style={{fontSize:11}}>top_p <input value={genOf(t,'top_p','')} onChange={e=>setCfg({...cfg, task_profiles: cfg.task_profiles.map(x => x.id===t.id ? setGen(x,'top_p',e.target.value) : x)})} placeholder="0.9" style={{width:64, padding:'4px 6px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:6, color:'var(--fg)', fontSize:11}} /></label>
                      <label title="1 – 1000000, пусто = default backend" style={{fontSize:11}}>max_tokens <input value={genOf(t,'max_tokens','')} onChange={e=>setCfg({...cfg, task_profiles: cfg.task_profiles.map(x => x.id===t.id ? setGen(x,'max_tokens',e.target.value) : x)})} placeholder="2048" style={{width:80, padding:'4px 6px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:6, color:'var(--fg)', fontSize:11}} /></label>
                    </>
                  )}
                </div>
              ))}
              <div style={{fontSize:11, color:'var(--muted)'}}>local-only блокирует облачные провайдеры. Пустое generation-значение = default backend.</div>
            </SectionCard>
          )}

          {section==='diagnostics' && (
            <SectionCard title="Диагностика">
              <div style={{fontSize:12}}>Статус: {status || '—'}</div>
              {issues.length > 0 && <div style={{fontSize:12}}>Проблемы загрузки: {issues.length}</div>}
              <details>
                <summary style={{fontSize:12, cursor:'pointer'}}>Посмотреть redacted JSON</summary>
                <pre style={{whiteSpace:'pre-wrap', background:'rgba(0,0,0,.25)', padding:10, borderRadius:8, border:'1px solid var(--border)', fontSize:11, maxHeight:300, overflow:'auto'}}>{JSON.stringify(redactedView(), null, 2)}</pre>
              </details>
              <div><button onClick={()=>{
                const txt = JSON.stringify(redactedView(), null, 2)
                if (navigator.clipboard) navigator.clipboard.writeText(txt).then(()=>flash('diagnostics скопированы')).catch(()=>flash('не удалось скопировать'))
                else flash('clipboard недоступен')
              }} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>Скопировать diagnostics без секретов</button></div>
            </SectionCard>
          )}

          {section==='pipelines' && (
            <SectionCard title="Пайплайны">
              <PipelineTab pipelines={cfg.pipelines} providers={cfg.providers} pipelineInput={pipelineInput} setPipelineInput={setPipelineInput} pipelineOut={pipelineOut} setPipelineOut={setPipelineOut} onSavePipeline={async (pl)=>{
                await llm.llmSavePipeline(pl)
                await load()
                flash('пайплайн сохранён ✓')
              }} onDeletePipeline={async(id)=>{
                await llm.llmDeletePipeline(id)
                await load()
              }} onRun={async(id, inp)=>{
                try {
                  const out=await llm.llmPipelineRun(id, inp)
                  setPipelineOut(out.slice(0,4000))
                } catch { setPipelineOut('ошибка запуска пайплайна') }
              }} />
            </SectionCard>
          )}
        </div>
      </div>

      {/* sticky footer */}
      {dirty && (
        <div style={{display:'flex', gap:8, alignItems:'center', padding:'8px 12px', borderTop:'1px solid var(--border)', background:'var(--panel)'}} role="status">
          <span style={{fontSize:12, color:'var(--accent-warn)'}}>● Есть несохранённые изменения</span>
          <span style={{marginLeft:'auto', display:'flex', gap:8}}>
            <button onClick={cancelEdit} disabled={saving} style={{padding:'6px 12px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12, opacity: saving?0.5:1}}>Отмена</button>
            <button onClick={save} disabled={saving || saveBlocked} title={saveBlocked ? 'Сохранение заблокировано: начните восстановление' : undefined} style={{padding:'6px 12px', background:'var(--accent)', border:0, borderRadius:8, fontWeight:700, fontSize:12, opacity: (saving || saveBlocked)?0.5:1}}>{saving ? 'Сохранение…' : 'Сохранить'}</button>
          </span>
        </div>
      )}
      {!dirty && (
        <div style={{display:'flex', gap:8, alignItems:'center', padding:'6px 12px', borderTop:'1px solid var(--border)'}}>
          <span style={{fontSize:11, color:'var(--muted)'}}>{status}</span>
          <span style={{marginLeft:'auto'}}><button onClick={save} disabled={saveBlocked} title={saveBlocked ? 'Сохранение заблокировано: начните восстановление' : undefined} style={{padding:'4px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:11, opacity: saveBlocked?0.5:1}}>Сохранить</button></span>
        </div>
      )}
    </div>
  )

  function redactedView() {
    return redactedDiagnostics(cfg as llm.LlmConfig)
  }
}

const REASON_RU: Record<string, string> = {
  corrupt: 'файл повреждён',
  unreadable: 'файл недоступен для чтения',
  migration_failed: 'миграция не удалась',
  unsupported_version: 'неподдерживаемая версия схемы',
  vault_missing: 'каталог из FRAGILE_VAULT отсутствует',
  keyring_failed: 'сбой переноса секрета в хранилище',
  invalid_entry: 'часть записей некорректна',
}

function apiUrlPlaceholder(kind: string): string {
  switch (kind) {
    case 'open_ai': return 'https://api.openai.com/v1'
    case 'gemini': return 'https://generativelanguage.googleapis.com'
    case 'claude': return 'https://api.anthropic.com'
    case 'deep_seek': return 'https://api.deepseek.com/v1'
    case 'open_router': return 'https://openrouter.ai/api/v1'
    case 'perplexity': return 'https://api.perplexity.ai'
    case 'ollama': return 'http://127.0.0.1:11434'
    case 'local_llama_cpp': return 'http://127.0.0.1:8010'
    default: return 'https://…'
  }
}

function StateBadge({state}:{state: llm.RegistryModelState}) {
  const ok = state === 'Present'
  return <span title={ok ? 'Проверена, доступна для чата' : 'Недоступна для чата: требуется сканирование/проверка'} style={{padding:'2px 8px', borderRadius:20, fontSize:11, fontWeight:700, background: ok?'var(--accent)':'transparent', color: ok?'#0b0b0b':'var(--muted)', border:'1px solid var(--border)'}}>{state}</span>
}

function LabeledSlider({label, min,max,step,value,onChange}:{label:string,min:number,max:number,step:number,value:number,onChange:(v:number)=>void}) {
  return <label style={{fontSize:12}}>{label}<input type="range" min={min} max={max} step={step} value={value} onChange={e=>onChange(Number(e.target.value))} style={{width:'100%'}} /></label>
}

function InstallModel({modelsDir}:{modelsDir:string}) {
  return <div style={{display:'flex', flexDirection:'column', gap:6}}>
    <div style={{display:'flex', gap:6, flexWrap:'wrap', alignItems:'center'}}>
      <button disabled title="Загрузка через менеджер — скоро: дождитесь подключения download manager, частичная установка отключена" style={{padding:'6px 12px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontWeight:700, opacity:.5, cursor:'not-allowed'}}>Загрузка через менеджер — скоро</button>
    </div>
    <div style={{fontSize:11, color:'var(--muted)'}}>Папка моделей: <code>{modelsDir}</code> — положите .gguf вручную и нажмите «Сканировать реестр» выше.</div>
  </div>
}

function PipelineTab({pipelines, providers, pipelineInput, setPipelineInput, pipelineOut, setPipelineOut, onSavePipeline, onDeletePipeline, onRun}:{pipelines: llm.Pipeline[], providers: llm.Provider[], pipelineInput:string, setPipelineInput:(s:string)=>void, pipelineOut:string, setPipelineOut:(s:string)=>void, onSavePipeline:(p:llm.Pipeline)=>void, onDeletePipeline:(id:string)=>void, onRun:(id:string, inp:string)=>void}) {
  const [editing, setEditing] = useState<llm.Pipeline | null>(null)
  const [newStep, setNewStep] = useState<Partial<llm.PipelineStep>>({provider_id:'local', prompt_template:'{{content}}', input_from:'note_content', output_to:'chat', enabled:true})

  const startNew = () => {
    const id='pipe-'+Math.random().toString(36).slice(2,6)
    setEditing({id, name:'Новый пайплайн', description:'', enabled:true, trigger:'manual', steps:[]})
  }

  return <div style={{display:'flex', flexDirection:'column', gap:12}}>
    <div style={{display:'flex', gap:8, alignItems:'center'}}>
      <h4 style={{margin:0}}>Пайплайны — гибкая настройка с помощью нейросетей и для нейросетей</h4>
      <button onClick={startNew} style={{marginLeft:'auto', padding:'6px 10px', background:'var(--accent)', border:0, borderRadius:8, fontWeight:700}}>+ Создать</button>
    </div>

    <div style={{display:'flex', flexDirection:'column', gap:8}}>
      {pipelines.map(pl=>(
        <div key={pl.id} className="card" style={{borderLeft:`3px solid ${pl.enabled?'var(--accent)':'var(--border)'}`}}>
          <div style={{display:'flex', gap:8, alignItems:'center'}}>
            <strong>{pl.name}</strong><span style={{fontSize:11, color:'var(--muted)'}}>({pl.trigger}) {pl.enabled?'●':''}</span>
            <span style={{fontSize:11, color:'var(--muted)', marginLeft:8}}>{pl.steps.length} шаг(а)</span>
            <button onClick={()=>setEditing(pl)} style={{marginLeft:'auto', padding:'4px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12}}>✎</button>
            <button onClick={()=>onDeletePipeline(pl.id)} style={{padding:'4px 8px', background:'rgba(255,80,80,.15)', border:'1px solid rgba(255,80,80,.3)', borderRadius:6, fontSize:12}}>Удалить</button>
          </div>
          <div style={{fontSize:12, color:'var(--muted)', marginTop:4}}>{pl.description}</div>
          <div style={{display:'flex', gap:6, flexWrap:'wrap', marginTop:8}}>
            {pl.steps.map(s=> <span key={s.id} style={{padding:'4px 8px', background:'rgba(255,255,255,.04)', border:'1px solid var(--border)', borderRadius:20, fontSize:11}}>{s.name} → {providers.find(p=>p.id===s.provider_id)?.name ?? s.provider_id} · {s.model || 'default'} {s.enabled?'✓':''}</span>)}
          </div>
          <div style={{display:'flex', gap:6, marginTop:8}}>
            <button onClick={()=>onRun(pl.id, pipelineInput)} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>▶ Запустить на тексте ниже</button>
          </div>
        </div>
      ))}
    </div>

    <div className="card" style={{display:'flex', flexDirection:'column', gap:8}}>
      <h4 style={{margin:0}}>Тестовый вход → выход пайплайна</h4>
      <textarea value={pipelineInput} onChange={e=>setPipelineInput(e.target.value)} placeholder="Входной текст для пайплайна — заметка, вопрос, {{content}}" style={{minHeight:70, padding:8, background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)', fontSize:12}} />
      <div style={{fontSize:11, color:'var(--muted)'}}>Выход:</div>
      <pre style={{whiteSpace:'pre-wrap', background:'rgba(0,0,0,.25)', padding:10, borderRadius:8, border:'1px solid var(--border)', fontSize:12, minHeight:60, color:'var(--fg)'}}>{pipelineOut || '— запусти пайплайн —'}</pre>
    </div>

    {editing && (
      <div className="card" style={{border:'1px solid var(--accent)', display:'flex', flexDirection:'column', gap:8}}>
        <h4 style={{margin:0}}>Редактирование: {editing.name}</h4>
        <input value={editing.name} onChange={e=>setEditing({...editing, name:e.target.value})} placeholder="Название" style={{padding:'6px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)'}} />
        <input value={editing.description} onChange={e=>setEditing({...editing, description:e.target.value})} placeholder="Описание — чем пользуется юзер" style={{padding:'6px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)', fontSize:12}} />
        <div style={{display:'flex', gap:8, alignItems:'center'}}>
          <label style={{fontSize:12}}>Триггер <select value={editing.trigger} onChange={e=>setEditing({...editing, trigger:e.target.value})} style={{marginLeft:6, padding:'4px 6px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, color:'var(--fg)'}}><option value="manual">manual</option><option value="on_save">on_save</option><option value="scheduled">scheduled</option></Select></label>
          <label style={{fontSize:12, display:'flex', gap:6, alignItems:'center'}}><input type="checkbox" checked={editing.enabled} onChange={e=>setEditing({...editing, enabled:e.target.checked})} /> вкл</label>
        </div>

        <div style={{display:'flex', flexDirection:'column', gap:6}}>
          <strong style={{fontSize:12}}>Шаги (каждый — нейросеть для задачи)</strong>
          {editing.steps.map((s,idx)=>(
            <div key={s.id} style={{display:'flex', gap:6, alignItems:'center', padding:8, background:'rgba(255,255,255,.02)', border:'1px solid var(--border)', borderRadius:8, flexWrap:'wrap'}}>
              <input value={s.name} onChange={e=>{
                const ns=[...editing.steps]; ns[idx]={...s, name:e.target.value}; setEditing({...editing, steps:ns})
              }} style={{padding:'4px 6px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:6, color:'var(--fg)', fontSize:12, minWidth:120}} />
              <select value={s.provider_id} onChange={e=>{
                const ns=[...editing.steps]; ns[idx]={...s, provider_id:e.target.value}; setEditing({...editing, steps:ns})
              }} style={{padding:'4px 6px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12, color:'var(--fg)'}}>
                {providers.map(p=> <option key={p.id} value={p.id}>{p.name}</option>)}
              </Select>
              <input value={s.model} onChange={e=>{
                const ns=[...editing.steps]; ns[idx]={...s, model:e.target.value}; setEditing({...editing, steps:ns})
              }} placeholder="модель (пусто=default)" style={{padding:'4px 6px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:6, color:'var(--fg)', fontSize:12, width:140}} />
              <button onClick={()=>{
                const ns=editing.steps.filter((_,i)=>i!==idx); setEditing({...editing, steps:ns})
              }} style={{padding:'4px 8px', background:'rgba(255,80,80,.15)', border:'1px solid rgba(255,80,80,.3)', borderRadius:6, fontSize:12}}>✕</button>
              <textarea value={s.prompt_template} onChange={e=>{
                const ns=[...editing.steps]; ns[idx]={...s, prompt_template:e.target.value}; setEditing({...editing, steps:ns})
              }} placeholder="prompt с {{content}} {{rag}}" style={{width:'100%', minHeight:50, marginTop:4, padding:6, background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:6, color:'var(--fg)', fontSize:11}} />
            </div>
          ))}
          <div style={{display:'flex', gap:6, alignItems:'center', flexWrap:'wrap'}}>
            <input value={newStep.name as string ?? ''} onChange={e=>setNewStep({...newStep, name:e.target.value})} placeholder="имя шага" style={{padding:'4px 6px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:6, color:'var(--fg)', fontSize:12}} />
            <select value={newStep.provider_id as string} onChange={e=>setNewStep({...newStep, provider_id:e.target.value})} style={{padding:'4px 6px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12, color:'var(--fg)'}}>
              {providers.map(p=> <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
            <button onClick={()=>{
              if(!newStep.name) return
              const id='s'+Math.random().toString(36).slice(2,5)
              const step: llm.PipelineStep = {id, name: newStep.name as string, kind:'llm', provider_id: (newStep.provider_id as string) || 'local', model: (newStep.model as string) || '', model_ref: (newStep.model as string) || '', prompt_template: (newStep.prompt_template as string) || '{{content}}', input_from:'note_content', input_refs:['note_content'], output_to:'chat', enabled:true}
              setEditing({...editing, steps:[...editing.steps, step]})
              setNewStep({provider_id:'local', prompt_template:'{{content}}', input_from:'note_content', output_to:'chat', enabled:true})
            }} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>+ Добавить шаг</button>
          </div>
        </div>

        <div style={{display:'flex', gap:8}}>
          <button onClick={()=>{ onSavePipeline(editing); setEditing(null)}} style={{padding:'8px 14px', background:'var(--accent)', border:0, borderRadius:8, fontWeight:700}}>Сохранить пайплайн</button>
          <button onClick={()=>setEditing(null)} style={{padding:'8px 14px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8}}>Отмена</button>
        </div>
      </div>
    )}
  </div>
}
