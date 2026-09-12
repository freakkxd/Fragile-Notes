export default function Editor({ value, onChange }){
  return (
    <textarea
      className="md-editor"
      value={value}
      onChange={e=>onChange(e.target.value)}
      spellCheck={false}
      placeholder="Пиши заметку в Markdown — читаем любой текстовый файл"
    />
  )
}
