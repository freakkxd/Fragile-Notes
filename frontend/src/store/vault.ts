import { create } from 'zustand'
import * as bridge from '../lib/bridge'

type Tab = { path: string; dirty: boolean }

type VaultState = {
  vaultRoot: string
  files: string[]
  tabs: Tab[]
  activePath: string | null
  content: string
  mode: 'edit' | 'preview' | 'split'
  leftCollapsed: boolean
  rightCollapsed: boolean
  commandOpen: boolean
  status: string
  loadVault: () => Promise<void>
  openFile: (path: string) => Promise<void>
  setContent: (c: string) => void
  save: () => Promise<void>
  setMode: (m: VaultState['mode']) => void
  toggleLeft: () => void
  toggleRight: () => void
  setCommandOpen: (v: boolean) => void
}

export const useVault = create<VaultState>((set, get) => ({
  vaultRoot: '~/Documents/FragileNotesVault',
  files: [],
  tabs: [],
  activePath: null,
  content: '',
  mode: 'split',
  leftCollapsed: false,
  rightCollapsed: false,
  commandOpen: false,
  status: 'Готово',
  loadVault: async () => {
    const files = await bridge.listNotes()
    set({ files, status: `Заметок: ${files.length}` })
  },
  openFile: async (path) => {
    const content = await bridge.readNote(path)
    const tabs = get().tabs
    const exists = tabs.find((t) => t.path === path)
    const nextTabs = exists ? tabs : [...tabs, { path, dirty: false }]
    set({ activePath: path, content, tabs: nextTabs, status: path })
  },
  setContent: (c) =>
    set((s) => {
      return { content: c, tabs: s.tabs.map((t) => (t.path === s.activePath ? { ...t, dirty: true } : t)) }
    }),
  save: async () => {
    const { activePath, content } = get()
    if (!activePath) return
    await bridge.writeNote(activePath, content)
    set((s) => ({ tabs: s.tabs.map((t) => (t.path === activePath ? { ...t, dirty: false } : t)), status: `Сохранено ${activePath}` }))
  },
  setMode: (mode) => set({ mode }),
  toggleLeft: () => set((s) => ({ leftCollapsed: !s.leftCollapsed })),
  toggleRight: () => set((s) => ({ rightCollapsed: !s.rightCollapsed })),
  setCommandOpen: (v) => set({ commandOpen: v }),
}))
