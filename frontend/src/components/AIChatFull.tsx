import { useState } from 'react'

type Msg = { role: 'user' | 'assistant'; content: string }

export default function AIChatFull() {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [status, setStatus] = useState('LLM: проверка...')

  const check = async () => {
    const invoke = (window as unknown as { __TAURI__?: { invoke: (c:string, a:unknown)=>Promise<string> } }).__TAURI__?.invoke
    if (!invoke) { setStatus('Tauri not available'); return }
    try {
      const res = await invoke('llm_status', { profile: 'day' })
      setStatus(String(res).slice(0,120))
    } catch (e) { setStatus(String(e)) }
  }

  const send = async () => {
    if (!input.trim()) return
    const u: Msg = { role: 'user', content: input }
    setMsgs((m) => [...m, u])
    setInput('')
    const invoke = (window as unknown as { __TAURI__?: { invoke: (c:string, a:unknown)=>Promise<string> } }).__TAURI__?.invoke
    if (!invoke) return
    try {
      const res = await invoke('llm_chat', { profile: 'day', messages: [...msgs, u].map(m=>({role:m.role, content:m.content})) })
      // llm_chat returns JSON string from llama-server
      let txt = String(res)
      try { const j = JSON.parse(txt); txt = j.choices?.[0]?.message?.content || txt } catch {}
      setMsgs((m) => [...m, { role: 'assistant', content: txt }])
    } catch (e) { setMsgs((m)=>[...m, {role:'assistant', content:String(e)}]) }
  }

  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100%' }}>
      <div style={{ padding:'8px 12px', borderBottom:'1px solid var(--border)', display:'flex', gap:8, alignItems:'center' }}>
        <span style={{ fontWeight:700 }}>AI Чат — Qwen3-14B</span>
        <button onClick={check} style={{ marginLeft:'auto', padding:'4px 8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:6, fontSize:12 }}>Статус</button>
        <span style={{ fontSize:11, color:'var(--muted)' }}>{status}</span>
      </div>
      <div style={{ flex:1, overflow:'auto', padding:12, display:'flex', flexDirection:'column', gap:8 }}>
        {msgs.map((m,i)=> (
          <div key={i} style={{ alignSelf: m.role==='user'?'flex-end':'flex-start', maxWidth:'80%', padding:'8px 12px', borderRadius:12, background: m.role==='user'?'rgba(130,168,255,.14)':'var(--panel-2)', border:'1px solid var(--border)', fontSize:13 }}>{m.content}</div>
        ))}
      </div>
      <div style={{ padding:10, borderTop:'1px solid var(--border)', display:'flex', gap:8 }}>
        <input value={input} onChange={(e)=>setInput(e.target.value)} onKeyDown={(e)=> e.key==='Enter' && send()} placeholder="Спроси vault — RAG подтянет 05 Sort + FTS" style={{ flex:1, padding:'10px 12px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:10, color:'var(--fg)' }} />
        <button onClick={send} style={{ padding:'10px 14px', background:'var(--accent)', border:0, borderRadius:10, fontWeight:700 }}>Отправить</button>
      </div>
    </div>
  )
}
