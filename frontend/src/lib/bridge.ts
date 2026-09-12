import { z } from 'zod'

// Runtime validation — never use `as T` without check
const VaultFileSchema = z.string()
const VaultListSchema = z.array(z.string())
const NoteContentSchema = z.string()

type TauriInvoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>

// Detect Tauri runtime, fallback to legacy Python WebView bridge
function getTauriInvoke(): TauriInvoke | null {
  const w = window as unknown as { __TAURI__?: { invoke: TauriInvoke } }
  return w.__TAURI__?.invoke ?? null
}

function getLegacyBridge(): {
  listNotes: () => Promise<string[]>
  readNote: (path: string) => Promise<string>
  writeNote: (path: string, content: string) => Promise<void>
} | null {
  const w = window as unknown as {
    fragileBridge?: {
      listNotes: () => Promise<string[]>
      readNote: (p: string) => Promise<string>
      writeNote: (p: string, c: string) => Promise<void>
    }
  }
  return w.fragileBridge ?? null
}

export async function listNotes(): Promise<string[]> {
  const tauri = getTauriInvoke()
  if (tauri) {
    const raw = await tauri('list_notes')
    return VaultListSchema.parse(raw)
  }
  const leg = getLegacyBridge()
  if (leg) {
    const raw = await leg.listNotes()
    return VaultListSchema.parse(raw)
  }
  // dev fallback: mock vault
  return []
}

export async function readNote(path: string): Promise<string> {
  const p = VaultFileSchema.parse(path)
  const tauri = getTauriInvoke()
  if (tauri) {
    const raw = await tauri('read_note', { path: p })
    return NoteContentSchema.parse(raw)
  }
  const leg = getLegacyBridge()
  if (leg) return NoteContentSchema.parse(await leg.readNote(p))
  return `# ${p}\n\nBridge not connected — dev preview`
}

export async function writeNote(path: string, content: string): Promise<void> {
  const p = VaultFileSchema.parse(path)
  const c = NoteContentSchema.parse(content)
  const tauri = getTauriInvoke()
  if (tauri) {
    await tauri('write_note', { path: p, content: c })
    return
  }
  const leg = getLegacyBridge()
  if (leg) {
    await leg.writeNote(p, c)
    return
  }
  console.warn('[bridge] writeNote: no backend, dropped', p)
}

export async function searchNotes(query: string): Promise<string[]> {
  const q = z.string().parse(query)
  const tauri = getTauriInvoke()
  if (tauri) {
    const raw = await tauri('fts_search', { query: q })
    return VaultListSchema.parse(raw)
  }
  return []
}
