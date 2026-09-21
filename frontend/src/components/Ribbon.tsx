import { useVault } from '../store/vault'

type Props = { onNav: (id: string) => void }

export default function Ribbon({ onNav }: Props) {
  const toggleLeft = useVault((s) => s.toggleLeft)
  const toggleRight = useVault((s) => s.toggleRight)
  const setCommandOpen = useVault((s) => s.setCommandOpen)
  return (
    <div className="ribbon">
      <button title="Файлы (Ctrl+B)" onClick={toggleLeft}>📁</button>
      <button title="Поиск" onClick={() => onNav('search')}>🔍</button>
      <button title="Граф" onClick={() => onNav('graph')}>🕸</button>
      <button title="Canvas" onClick={() => onNav('canvas')}>🎨</button>
      <div className="sep" />
      <button title="Задачи" onClick={() => onNav('tasks')}>✓</button>
      <button title="Календарь" onClick={() => onNav('calendar')}>🗓</button>
      <button title="AI Чат" onClick={() => onNav('ai_chat')}>🤖</button>
      <button title="Нейросети — llama.cpp + GPT/Gemini/Claude" onClick={() => onNav('llm')}>🧠</button>
      <div className="spacer" />
      <button title="Палитра (Ctrl+P)" onClick={() => setCommandOpen(true)}>⌘</button>
      <button title="Правый сайдбар" onClick={toggleRight}>⚙</button>
    </div>
  )
}
