/**
 * Автообновление на любом устройстве — без ручных триггеров.
 * 1) Tauri plugin updater (подписанный, скачивает и ставит молча) — primary
 * 2) Fallback: GitHub Releases API + баннер если plugin недоступен (dev mode)
 */

const REPO = 'freakkxd/Fragile-Notes'
const CURRENT = '0.5.3'

function compareVersions(a: string, b: string): number {
  const pa = a.replace(/^v/, '').split('.').map(Number)
  const pb = b.replace(/^v/, '').split('.').map(Number)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const da = pa[i] || 0
    const db = pb[i] || 0
    if (da !== db) return da - db
  }
  return 0
}

// Fallback GitHub API
async function checkGitHub(silent = true) {
  try {
    const res = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`, {
      headers: { Accept: 'application/vnd.github.v3+json' },
    })
    if (!res.ok) return null
    const data = await res.json()
    const latest: string = data.tag_name || ''
    const url: string = data.html_url || `https://github.com/${REPO}/releases/latest`
    const notes: string = data.body || ''
    if (!latest) return null
    return { hasUpdate: compareVersions(latest, CURRENT) > 0, latest, url, notes }
  } catch {
    if (!silent) console.warn('[updater] GitHub check failed')
    return null
  }
}

export async function checkForUpdates(silent = true): Promise<{ hasUpdate: boolean; latest: string; url: string; notes: string } | null> {
  // 1. Пробуем Tauri plugin (авто-скачивание)
  try {
    const w = window as unknown as { __TAURI__?: unknown }
    if (w.__TAURI__) {
      // @tauri-apps/api/updater — Tauri 1.x
      const mod = await import('@tauri-apps/api/updater').catch(() => null)
      if (mod && typeof mod.checkUpdate === 'function') {
        const update = await mod.checkUpdate()
        if (update?.shouldUpdate) {
          return { hasUpdate: true, latest: update.manifest?.version || 'latest', url: update.manifest?.body || '', notes: update.manifest?.body || '' }
        }
        // plugin сказал нет обновления — fallback к GitHub чтобы показать баннер если есть более новая версия без updater.json
        const gh = await checkGitHub(true)
        if (gh?.hasUpdate) return gh
        return null
      }
    }
  } catch (e) {
    if (!silent) console.warn('[updater] Tauri plugin check failed', e)
  }
  // 2. Fallback GitHub API
  return checkGitHub(silent)
}

export async function installUpdateAndRelaunch() {
  try {
    const w = window as unknown as { __TAURI__?: unknown }
    if (w.__TAURI__) {
      const mod = await import('@tauri-apps/api/updater').catch(() => null)
      const processMod = await import('@tauri-apps/api/process').catch(() => null)
      if (mod?.installUpdate) {
        await mod.installUpdate()
        if (processMod?.relaunch) await processMod.relaunch()
        return true
      }
    }
  } catch (e) {
    console.error('[updater] install failed', e)
  }
  return false
}

export function openReleasePage(url: string) {
  const w = window as unknown as {
    __TAURI__?: { shell?: { open: (u: string) => void }; invoke?: (cmd: string, args?: unknown) => Promise<unknown> }
  }
  if (w.__TAURI__?.shell?.open) w.__TAURI__.shell.open(url)
  else if (w.__TAURI__?.invoke) w.__TAURI__.invoke('plugin:shell|open', { path: url }).catch(() => window.open(url, '_blank'))
  else window.open(url, '_blank')
}

// Автоматическая установка без клика — вызывается из App.tsx
export async function autoInstallIfAvailable(): Promise<boolean> {
  try {
    const w = window as unknown as { __TAURI__?: unknown }
    if (!w.__TAURI__) return false
    const mod = await import('@tauri-apps/api/updater').catch(() => null)
    if (!mod?.checkUpdate) return false
    const update = await mod.checkUpdate()
    if (update?.shouldUpdate) {
      await mod.installUpdate()
      const proc = await import('@tauri-apps/api/process').catch(() => null)
      if (proc?.relaunch) await proc.relaunch()
      return true
    }
  } catch {}
  return false
}
