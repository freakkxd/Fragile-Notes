export default function FileTree({ files, onOpen, current }){
  if(!files.length) return <div className="empty">vault пуст — создай заметку</div>
  return (
    <ul className="file-tree">
      {files.map(f=>(
        <li key={f} className={current===f?'active':''} onClick={()=>onOpen(f)}>{f.split('/').pop()}</li>
      ))}
    </ul>
  )
}
