type Props = { status: string; words: number }
export default function StatusBar({ status, words }: Props) {
  return <div className="statusbar"><span>{status}</span><span>{words} слов</span><span>Fragile Notes v0.4.1 Tauri+C++</span></div>
}
