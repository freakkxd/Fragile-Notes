import { useState } from 'react'
type Props = { onSearch: (q: string) => Promise<string[]> }
export default function SearchView({ onSearch }: Props) {
  const [q, setQ] = useState('')
  const [res, setRes] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const doSearch = async () => {
    if (!q.trim()) return
    setLoading(true)
    const r = await onSearch(q)
    setRes(r)
    setLoading(false)
  }
  return (
    <div className="search-view">
      <h3 style={{ margin:'4px 0 6px' }}>Поиск — FTS5</h3>
      <div className="search-bar">
        <input placeholder=" tag:#todo path:Daily hello — FTS5" value={q} onChange={(e)=>setQ(e.target.value)} onKeyDown={(e)=> e.key==='Enter' && doSearch()} />
        <button onClick={doSearch}>{loading?'…':'Найти'}</button>
      </div>
      {!res.length && !loading && <div className="empty">Введи запрос — ищет по содержимому C++ FTS5 + fallback scan. Синтаксис: <code>tag:</code> <code>path:</code></div>}
      {res.length>0 && <div className="card"><h4>Результаты {res.length}</h4><ul style={{ margin:0, paddingLeft:16 }}>{res.map(r=> <li key={r} style={{ padding:'4px 0', fontSize:13 }}>{r}</li>)}</ul></div>}
    </div>
  )
}
