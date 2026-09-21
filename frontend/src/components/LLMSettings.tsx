import { useEffect, useState } from 'react'
import * as llm from '../lib/llm'

// Helper to open external URL (OAuth login)
async function openExternal(url: string) {
  const w = window as unknown as { __TAURI__?: { shell?: { open: (u:string)=>void }, invoke?: (cmd:string,args?:unknown)=>Promise<unknown> } }
  try {
    if (w.__TAURI__?.shell?.open) { w.__TAURI__.shell.open(url); return }
    if (w.__TAURI__?.invoke) { await w.__TAURI__.invoke('plugin:shell|open', { path: url }); return }
  } catch {}
  window.open(url, '_blank')
}

export default function LLMSettings() {
  const [tab, setTab] = useState<'local'|'cloud'|'pipeline'>('local')
  const [cfg, setCfg] = useState<llm.LlmConfig | null>(null)
  const [models, setModels] = useState<llm.ModelInfo[]>([])
  const [status, setStatus] = useState('загрузка...')
  const [cloudStatus, setCloudStatus] = useState<Record<string,string>>({})
  const [localTest, setLocalTest] = useState('—')
  const [pipelineInput, setPipelineInput] = useState('Тестовая заметка #идея: сделать Vault умнее')
  const [pipelineOut, setPipelineOut] = useState('')

  const load = async () => {
    try {
      const c = await llm.llmGetConfig()
      setCfg(c)
      setStatus('конфиг загружен')
      const m = await llm.llmScanModels(c.local.models_dir)
      setModels(m)
    } catch (e) { setStatus(String(e)) }
  }
  useEffect(()=>{ load() },[])

  const save = async () => {
    if(!cfg) return
    await llm.llmSaveConfig(cfg)
    setStatus('сохранено ✓')
    setTimeout(()=>setStatus(''),2000)
  }

  const scan = async () => {
    if(!cfg) return
    const m = await llm.llmScanModels(cfg.local.models_dir)
    setModels(m)
    setStatus(`найдено ${m.length} моделей`)
  }

  const setActive = async (path: string) => {
    await llm.llmSetActiveModel(path)
    if(cfg) setCfg({...cfg, local:{...cfg.local, active_model: path}})
    setStatus(`активна: ${path.split('/').pop()}`)
  }

  const testLocal = async () => {
    setLocalTest('проверка...')
    const r = await llm.llmTestProvider('local')
    setLocalTest(r.slice(0,300))
  }

  const testProvider = async (id: string) => {
    setCloudStatus(s=>({...s,[id]:'проверка...'}))
    const r = await llm.llmTestProvider(id)
    setCloudStatus(s=>({...s,[id]: r.slice(0,200)}))
  }

  if(!cfg) return <div style={{padding:16, color:'var(--muted)'}}>Загрузка LLM конфига… {status}</div>

  return (
    <div style={{display:'flex', flexDirection:'column', height:'100%', background:'var(--bg)'}}>
      {/* tabs */}
      <div style={{display:'flex', gap:6, padding:'8px 10px', borderBottom:'1px solid var(--border)', flexWrap:'wrap'}}>
        <button onClick={()=>setTab('local')} style={{padding:'6px 12px', borderRadius:8, border:'1px solid var(--border)', background: tab==='local'?'var(--accent)':'var(--panel-2)', color: tab==='local'?'#0b0b0b':'var(--fg)', fontWeight:700}}>🖥️ Локально llama.cpp</button>
        <button onClick={()=>setTab('cloud')} style={{padding:'6px 12px', borderRadius:8, border:'1px solid var(--border)', background: tab==='cloud'?'var(--accent)':'var(--panel-2)', color: tab==='cloud'?'#0b0b0b':'var(--fg)', fontWeight:700}}>☁️ API (GPT/Gemini/Claude)</button>
        <button onClick={()=>setTab('pipeline')} style={{padding:'6px 12px', borderRadius:8, border:'1px solid var(--border)', background: tab==='pipeline'?'var(--accent)':'var(--panel-2)', color: tab==='pipeline'?'#0b0b0b':'var(--fg)', fontWeight:700}}>⛓️ Пайплайны</button>
        <span style={{marginLeft:'auto', fontSize:12, color:'var(--muted)', alignSelf:'center'}}>{status}</span>
        <button onClick={save} style={{padding:'6px 12px', background:'var(--accent)', border:0, borderRadius:8, fontWeight:700, cursor:'pointer'}}>Сохранить</button>
      </div>

      <div style={{flex:1, overflow:'auto', padding:12, display:'flex', flexDirection:'column', gap:14}}>

      {tab==='local' && (
        <>
          {/* llama.cpp binary + models dir */}
          <div className="card" style={{display:'flex', flexDirection:'column', gap:8}}>
            <h4 style={{margin:0}}>llama.cpp — бинарь и модели</h4>
            <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
              <label style={{flex:1, minWidth:240}}>Бинарь <input value={cfg.local.binary_path} onChange={e=>setCfg({...cfg, local:{...cfg.local, binary_path:e.target.value}})} placeholder="llama-server / /usr/bin/llama-server" style={{width:'100%', marginTop:4, padding:'6px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)'}} /></label>
              <label style={{flex:1, minWidth:240}}>Модели папка <input value={cfg.local.models_dir} onChange={e=>setCfg({...cfg, local:{...cfg.local, models_dir:e.target.value}})} style={{width:'100%', marginTop:4, padding:'6px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)'}} /></label>
              <label style={{width:110}}>Порт <input type="number" value={cfg.local.port} onChange={e=>setCfg({...cfg, local:{...cfg.local, port: Number(e.target.value)}})} style={{width:'100%', marginTop:4, padding:'6px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)'}} /></label>
            </div>
            <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
              <button onClick={scan} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8}}>🔍 Сканировать</button>
              <button onClick={testLocal} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8}}>Проверить сервер</button>
              <span style={{fontSize:12, color:'var(--muted)'}}>{localTest}</span>
            </div>
            <div style={{display:'flex', gap:8, alignItems:'center'}}>
              <label style={{display:'flex', gap:6, alignItems:'center', fontSize:12}}><input type="checkbox" checked={cfg.local.auto_start} onChange={e=>setCfg({...cfg, local:{...cfg.local, auto_start:e.target.checked}})} /> автостарт</label>
              <label style={{display:'flex', gap:6, alignItems:'center', fontSize:12}}><input type="checkbox" checked={cfg.local.use_mmap} onChange={e=>setCfg({...cfg, local:{...cfg.local, use_mmap:e.target.checked}})} /> mmap</label>
            </div>
            <div style={{fontSize:11, color:'var(--muted)'}}>Активная модель: <code>{cfg.local.active_model || '— не выбрана —'}</code></div>
          </div>

          {/* models list */}
          <div className="card">
            <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}><h4 style={{margin:0}}>Модели (.gguf) — парсинг и выбор</h4><span style={{fontSize:12, color:'var(--muted)'}}>{models.length} шт.</span></div>
            <div style={{display:'flex', flexDirection:'column', gap:6, marginTop:8, maxHeight:360, overflow:'auto'}}>
              {models.length===0 && <div style={{fontSize:12, color:'var(--muted)'}}>Пусто — положи .gguf в {cfg.local.models_dir} или установи ниже</div>}
              {models.map(m=>(
                <div key={m.path} style={{display:'flex', gap:8, alignItems:'center', padding:'8px 10px', background:'rgba(255,255,255,.02)', border:`1px solid ${cfg.local.active_model===m.path?'var(--accent)':'var(--border)'}`, borderRadius:10}}>
                  <div style={{flex:1, minWidth:0}}>
                    <div style={{fontWeight:700, fontSize:13, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis'}}>{m.name}</div>
                    <div style={{fontSize:11, color:'var(--muted)', display:'flex', gap:8, flexWrap:'wrap'}}><span>{m.quant}</span><span>{m.size_mb}МБ</span><span title="угаданная задача">{m.task}</span><span style={{opacity:.6}}>{m.path}</span></div>
                  </div>
                  <select value={m.task} onChange={e=>{
                    const v=e.target.value
                    // persist task as extra in local? For MVP just show
                    setModels(ms=> ms.map(x=> x.path===m.path? {...x, task:v}:x))
                  }} style={{padding:'4px 6px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12}}>
                    <option value="general">general</option><option value="chat">chat</option><option value="coder">coder</option><option value="embedding">embedding</option><option value="vision">vision</option><option value="enrich">enrich</option>
                  </select>
                  <button onClick={()=>setActive(m.path)} style={{padding:'6px 10px', background: cfg.local.active_model===m.path?'var(--accent)':'var(--panel-2)', color: cfg.local.active_model===m.path?'#0b0b0b':'var(--fg)', border:'1px solid var(--border)', borderRadius:8, fontSize:12, fontWeight:700}}>{cfg.local.active_model===m.path?'✓ Активна':'Сделать активной'}</button>
                </div>
              ))}
            </div>
          </div>

          {/* install model */}
          <div className="card" style={{display:'flex', flexDirection:'column', gap:8}}>
            <h4 style={{margin:0}}>Установка модели (конкретное место для задачи)</h4>
            <div style={{fontSize:11, color:'var(--muted)'}}>Вставь HuggingFace URL на .gguf (напр. <code>https://huggingface.co/bartowski/Qwen2-7B.../resolve/main/model.gguf</code>), выбери задачу-папку, скачается в <code>Models/&lt;task&gt;/</code></div>
            <InstallModel onInstalled={scan} modelsDir={cfg.local.models_dir} />
          </div>

          {/* llama.cpp flexible tuning */}
          <div className="card" style={{display:'flex', flexDirection:'column', gap:10}}>
            <h4 style={{margin:0}}>Гибкая настройка llama.cpp</h4>
            <div style={{display:'grid', gridTemplateColumns:'1fr 1fr', gap:10}}>
              <LabeledSlider label={`n_ctx ${cfg.local.n_ctx}`} min={512} max={32768} step={512} value={cfg.local.n_ctx} onChange={v=>setCfg({...cfg, local:{...cfg.local, n_ctx:v}})} />
              <LabeledSlider label={`n_threads ${cfg.local.n_threads||'auto'}`} min={0} max={32} step={1} value={cfg.local.n_threads} onChange={v=>setCfg({...cfg, local:{...cfg.local, n_threads:v}})} />
              <LabeledSlider label={`n_gpu_layers ${cfg.local.n_gpu_layers}`} min={0} max={99} step={1} value={cfg.local.n_gpu_layers} onChange={v=>setCfg({...cfg, local:{...cfg.local, n_gpu_layers:v}})} />
              <LabeledSlider label={`temp ${cfg.local.temp.toFixed(2)}`} min={0} max={2} step={0.05} value={cfg.local.temp} onChange={v=>setCfg({...cfg, local:{...cfg.local, temp:v}})} />
              <LabeledSlider label={`top_p ${cfg.local.top_p.toFixed(2)}`} min={0} max={1} step={0.05} value={cfg.local.top_p} onChange={v=>setCfg({...cfg, local:{...cfg.local, top_p:v}})} />
              <LabeledSlider label={`top_k ${cfg.local.top_k}`} min={0} max={100} step={1} value={cfg.local.top_k} onChange={v=>setCfg({...cfg, local:{...cfg.local, top_k:v}})} />
              <LabeledSlider label={`repeat ${cfg.local.repeat_penalty.toFixed(2)}`} min={1} max={2} step={0.05} value={cfg.local.repeat_penalty} onChange={v=>setCfg({...cfg, local:{...cfg.local, repeat_penalty:v}})} />
              <label>extra_args <input value={cfg.local.extra_args} onChange={e=>setCfg({...cfg, local:{...cfg.local, extra_args:e.target.value}})} placeholder="--mlock --flash-attn" style={{width:'100%', marginTop:4, padding:'6px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)', fontSize:12}} /></label>
            </div>
            <div style={{fontSize:11, color:'var(--muted)'}}>Команда запуска: <code>{cfg.local.binary_path} --model {cfg.local.active_model||'MODEL'} --ctx-size {cfg.local.n_ctx} --threads {cfg.local.n_threads||'auto'} --n-gpu-layers {cfg.local.n_gpu_layers} --temp {cfg.local.temp} --port {cfg.local.port} {cfg.local.extra_args}</code></div>
          </div>
        </>
      )}

      {tab==='cloud' && (
        <div style={{display:'flex', flexDirection:'column', gap:12}}>
          {cfg.providers.filter(p=> p.kind!=='local_llama_cpp').map(p=>(
            <ProviderCard key={p.id} prov={p} status={cloudStatus[p.id] ?? ''} onTest={()=>testProvider(p.id)} onChange={(patch)=> setCfg(c=> c ? {...c, providers: c.providers.map(x=> x.id===p.id? {...x, ...patch}:x)}:c)} />
          ))}
          <div style={{fontSize:11, color:'var(--muted)', padding:6}}>🔑 Ключи хранятся обфусцировано (base64) в <code>{'vault/.fragile/llm.json'}</code> или <code>~/.config/Fragile-Notes/llm.json</code>. Позже — <code>aes-gcm/pbkdf2</code>. Поддерживается <b>API ключ</b> и <b>Вход в аккаунт (OAuth)</b> — кнопка ниже открывает провайдера.</div>
        </div>
      )}

      {tab==='pipeline' && (
        <PipelineTab pipelines={cfg.pipelines} providers={cfg.providers} pipelineInput={pipelineInput} setPipelineInput={setPipelineInput} pipelineOut={pipelineOut} setPipelineOut={setPipelineOut} onSavePipeline={async (pl)=>{
          await llm.llmSavePipeline(pl)
          const c=await llm.llmGetConfig(); setCfg(c)
          setStatus('пайплайн сохранён ✓')
        }} onDeletePipeline={async(id)=>{
          await llm.llmDeletePipeline(id)
          const c=await llm.llmGetConfig(); setCfg(c)
        }} onRun={async(id, inp)=>{
          const out=await llm.llmPipelineRun(id, inp)
          setPipelineOut(out.slice(0,4000))
        }} />
      )}
      </div>
    </div>
  )
}

function LabeledSlider({label, min,max,step,value,onChange}:{label:string,min:number,max:number,step:number,value:number,onChange:(v:number)=>void}) {
  return <label style={{fontSize:12}}>{label}<input type="range" min={min} max={max} step={step} value={value} onChange={e=>onChange(Number(e.target.value))} style={{width:'100%'}} /></label>
}

function InstallModel({onInstalled, modelsDir}:{onInstalled:()=>void, modelsDir:string}) {
  const [url,setUrl]=useState('')
  const [task,setTask]=useState('general')
  const [log,setLog]=useState('—')
  const dl=async()=>{
    if(!url.trim()) return
    setLog('установка...')
    try {
      const p=await llm.llmDownloadModel(url, task)
      setLog(`готово: ${p} (placeholder — для реальных HF файлов настрой curl/aria2)`)
      onInstalled()
    } catch(e){ setLog(String(e)) }
  }
  return <div style={{display:'flex', flexDirection:'column', gap:6}}>
    <div style={{display:'flex', gap:6, flexWrap:'wrap'}}>
      <input value={url} onChange={e=>setUrl(e.target.value)} placeholder="https://huggingface.co/.../model.gguf" style={{flex:1, minWidth:260, padding:'6px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)', fontSize:12}} />
      <select value={task} onChange={e=>setTask(e.target.value)} style={{padding:'6px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8}}>
        <option value="general">general</option><option value="chat">chat</option><option value="coder">coder</option><option value="embedding">embedding</option><option value="enrich">enrich</option>
      </select>
      <button onClick={dl} style={{padding:'6px 12px', background:'var(--accent)', border:0, borderRadius:8, fontWeight:700}}>Установить</button>
    </div>
    <div style={{fontSize:11, color:'var(--muted)'}}>Цель: <code>{modelsDir}/{task}/</code> — {log}</div>
  </div>
}

function ProviderCard({prov, status, onTest, onChange}:{prov: llm.Provider, status:string, onTest:()=>void, onChange:(p:Partial<llm.Provider>)=>void}) {
  const loginUrl = prov.kind==='open_ai' ? 'https://platform.openai.com/api-keys' : prov.kind==='gemini' ? 'https://aistudio.google.com/app/apikey' : prov.kind==='claude' ? 'https://console.anthropic.com/settings/keys' : prov.kind==='custom_open_ai' ? prov.api_url : 'https://ollama.com'
  const [showKey,setShowKey]=useState(false)
  return <div className="card" style={{display:'flex', flexDirection:'column', gap:8, borderLeft:`3px solid ${prov.enabled?'var(--accent)':'var(--border)'}`}}>
    <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
      <strong style={{flex:1}}>{prov.name} <span style={{fontWeight:400, color:'var(--muted)', fontSize:12}}>({prov.kind})</span></strong>
      <label style={{display:'flex', gap:6, alignItems:'center', fontSize:12}}><input type="checkbox" checked={prov.enabled} onChange={e=>onChange({enabled:e.target.checked})} /> вкл</label>
      <select value={prov.auth_method} onChange={e=>onChange({auth_method: e.target.value as llm.Provider['auth_method']})} style={{padding:'4px 6px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12}}>
        <option value="api_key">API ключ</option><option value="o_auth">Вход в аккаунт (OAuth)</option><option value="none">Без auth</option>
      </select>
    </div>

    {prov.auth_method==='api_key' ? (
      <div style={{display:'flex', gap:6, alignItems:'center', flexWrap:'wrap'}}>
        <input type={showKey? 'text':'password'} value={prov.api_key} onChange={e=>onChange({api_key:e.target.value})} placeholder="sk-... / AIza... / sk-ant-..." style={{flex:1, minWidth:220, padding:'6px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)', fontSize:12}} />
        <button onClick={()=>setShowKey(s=>!s)} style={{padding:'6px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>{showKey?'Скрыть':'Показать'}</button>
      </div>
    ) : prov.auth_method==='o_auth' ? (
      <button onClick={()=>openExternal(loginUrl)} style={{padding:'8px 12px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12, fontWeight:600}}>🔐 Войти в аккаунт — {prov.name} ↗</button>
    ) : <div style={{fontSize:12, color:'var(--muted)'}}>Без ключа — локальный / Ollama</div>}

    <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
      <label style={{flex:1, minWidth:200, fontSize:12}}>Модель <input value={prov.model} onChange={e=>onChange({model:e.target.value})} placeholder={prov.kind==='open_ai'?'gpt-4o': prov.kind==='gemini'?'gemini-2.0-flash': prov.kind==='claude'?'claude-3-5-sonnet-latest':'llama3.1'} style={{width:'100%', marginTop:4, padding:'6px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)', fontSize:12}} /></label>
      <label style={{flex:1, minWidth:200, fontSize:12}}>API URL <input value={prov.api_url} onChange={e=>onChange({api_url:e.target.value})} placeholder="https://api.openai.com/v1" style={{width:'100%', marginTop:4, padding:'6px 8px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)', fontSize:12}} /></label>
    </div>

    <div style={{display:'flex', gap:8, alignItems:'center'}}>
      <button onClick={onTest} style={{padding:'6px 10px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8, fontSize:12}}>Тест</button>
      <span style={{fontSize:11, color:'var(--muted)'}}>{status || '—'}</span>
      <button onClick={()=>openExternal(loginUrl)} style={{marginLeft:'auto', fontSize:11, color:'var(--muted)', background:'transparent', border:0, textDecoration:'underline', cursor:'pointer'}}>Открыть кабинет ↗</button>
    </div>
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
            <button onClick={()=>onDeletePipeline(pl.id)} style={{padding:'4px 8px', background:'rgba(255,80,80,.12)', border:'1px solid rgba(255,80,80,.3)', borderRadius:6, fontSize:12}}>Удалить</button>
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
          <label style={{fontSize:12}}>Триггер <select value={editing.trigger} onChange={e=>setEditing({...editing, trigger:e.target.value})} style={{marginLeft:6, padding:'4px 6px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6}}><option value="manual">manual</option><option value="on_save">on_save</option><option value="scheduled">scheduled</option></select></label>
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
              }} style={{padding:'4px 6px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12}}>
                {providers.map(p=> <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
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
            <select value={newStep.provider_id as string} onChange={e=>setNewStep({...newStep, provider_id:e.target.value})} style={{padding:'4px 6px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12}}>
              {providers.map(p=> <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
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

