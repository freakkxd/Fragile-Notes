import { useState } from 'react'

type Props = { onClipped?: (res: string) => void }

export default function WebClipper({ onClipped }: Props) {
  const [url, setUrl] = useState('')
  const [title, setTitle] = useState('')
  const [status, setStatus] = useState('')

  const clip = async () => {
    const html = document.documentElement.outerHTML.slice(0, 8000)
    const sel = window.getSelection()?.toString().slice(0, 4000) || ''
    const invoke = (window as unknown as { __TAURI__?: { invoke: (cmd: string, args: unknown) => Promise<unknown> } }).__TAURI__?.invoke
    setStatus('Сохраняю...')
    try {
      if (invoke) {
        const res = await invoke('web_clip', { url: url || location.href, title: title || document.title, html, selection: sel })
        setStatus(String(res))
        onClipped?.(String(res))
      } else {
        // fallback: save via writeNote to Sources/web-clips
        const { writeNote } = await import('../lib/bridge')
        const slug = (title || 'clip').toLowerCase().replace(/[^\w]+/g, '-').slice(0, 40)
        const path = `_System/ArchiveOrganism/Sources/web-clips/${new Date().toISOString().slice(0,10)}-${slug}.md`
        await writeNote(path, `---\ntitle: "${title}"\nurl: "${url}"\n---\n\n${sel || html.slice(0,2000)}`)
        setStatus(`saved ${path}`)
      }
    } catch (e) {
      setStatus(String(e))
    }
  }

  const collect = async () => {
    const invoke = (window as unknown as { __TAURI__?: { invoke: (cmd: string, args?: unknown) => Promise<unknown> } }).__TAURI__?.invoke
    if (!invoke) { setStatus('Tauri not available'); return }
    setStatus('Сбор...')
    try {
      const res = await invoke('collect_sources', {})
      setStatus(String(res))
    } catch (e) { setStatus(String(e)) }
  }

  const enrich = async () => {
    const invoke = (window as unknown as { __TAURI__?: { invoke: (cmd: string, args?: unknown) => Promise<unknown> } }).__TAURI__?.invoke
    if (!invoke) return
    setStatus('Обогащаю...')
    try {
      const res = await invoke('enrich_notes', { limit: 3, profile: 'day' })
      setStatus(String(res))
    } catch (e) { setStatus(String(e)) }
  }

  return (
    <div className="card">
      <h4>Web Clipper — нативно</h4>
      <div style={{ display:'flex', flexDirection:'column', gap:8 }}>
        <input placeholder="URL (или пусто = текущий)" value={url} onChange={(e)=>setUrl(e.target.value)} style={{ padding:'8px 10px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)' }} />
        <input placeholder="Title" value={title} onChange={(e)=>setTitle(e.target.value)} style={{ padding:'8px 10px', background:'rgba(0,0,0,.25)', border:'1px solid var(--border)', borderRadius:8, color:'var(--fg)' }} />
        <div style={{ display:'flex', gap:6 }}>
          <button onClick={clip} style={{ flex:1, padding:'8px', background:'var(--accent)', border:0, borderRadius:8, fontWeight:700 }}>✂ Clip</button>
          <button onClick={collect} style={{ flex:1, padding:'8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8 }}>📥 Сбор</button>
          <button onClick={enrich} style={{ flex:1, padding:'8px', background:'var(--panel-2)', border:'1px solid var(--border)', borderRadius:8 }}>✨ Обогатить</button>
        </div>
        {status && <div style={{ fontSize:11, color:'var(--muted)', wordBreak:'break-all' }}>{status}</div>}
        <div style={{ fontSize:11, color:'var(--muted)' }}>Клип сохраняет в <code>Sources/web-clips</code> → `collect` переносит в <code>05 Sort</code> как `ao_sort_status: undecided` (как `wrapWebClips`). `Enrich` вызывает `Qwen3-14B`.</div>
      </div>
    </div>
  )
}
