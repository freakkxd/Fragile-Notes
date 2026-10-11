import { afterEach } from 'vitest'

type InvokeFn = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>
type Fixture = unknown | ((cmd: string, args?: Record<string, unknown>) => unknown)

function ensureWindow(): Record<string, unknown> {
  const g = globalThis as unknown as { window?: Record<string, unknown> }
  if (!g.window) g.window = {}
  return g.window
}

/**
 * Install a mocked `window.__TAURI__.invoke` for tests.
 *
 * `fixtures` maps Tauri command names to canned results (values or
 * functions of cmd/args). Unknown commands reject — tests fail loudly
 * instead of silently passing on missing fixtures.
 *
 * Never touches the real Tauri backend, keyring, config, or vault.
 * Call `resetTauriMock()` (auto-run after each test) to remove it.
 */
export function installTauriMock(fixtures: Record<string, Fixture>): void {
  const w = ensureWindow()
  const tauri = (w.__TAURI__ ?? {}) as Record<string, unknown>
  const calls: Array<{ cmd: string; args?: Record<string, unknown> }> = []
  const invoke: InvokeFn = async (cmd, args) => {
    calls.push({ cmd, args })
    if (!(cmd in fixtures)) throw new Error(`no mock fixture for Tauri command: ${cmd}`)
    const f = fixtures[cmd]
    return typeof f === 'function' ? (f as (cmd: string, args?: Record<string, unknown>) => unknown)(cmd, args) : f
  }
  tauri.invoke = invoke
  tauri.__mockCalls = calls
  w.__TAURI__ = tauri
}

export function tauriMockCalls(): Array<{ cmd: string; args?: Record<string, unknown> }> {
  const g = globalThis as unknown as { window?: { __TAURI__?: { __mockCalls?: Array<{ cmd: string; args?: Record<string, unknown> }> } } }
  return g.window?.__TAURI__?.__mockCalls ?? []
}

export function resetTauriMock(): void {
  const g = globalThis as unknown as { window?: Record<string, unknown> }
  if (g.window && '__TAURI__' in g.window) delete g.window.__TAURI__
}

afterEach(() => {
  resetTauriMock()
})
