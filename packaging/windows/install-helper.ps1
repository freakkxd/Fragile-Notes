# install-helper.ps1 — вызывается Inno Setup после копирования файлов
# Делает всё за юзера из коробки: проверка Python, pip install, vault, ярлыки
param([switch]$Install)

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $AppDir) { $AppDir = "C:\Program Files\Fragile Notes" }
Set-Location $AppDir

function Ok($m){ Write-Host "✓ $m" -ForegroundColor Green }
function Warn($m){ Write-Host "⚠ $m" -ForegroundColor Yellow }
function Fail($m){ Write-Host "✗ $m" -ForegroundColor Red }

if ($Install) {
  Write-Host "== Fragile Notes — All-in-One install ==" -ForegroundColor Cyan

  # 1. Python
  $py = $null
  foreach ($c in @("python","python3","py")) { if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break } }
  if (-not $py) {
    Warn "Python 3.11+ не найден"
    Write-Host "Скачай Python 3.11 с https://www.python.org/downloads/ и перезапусти инсталлер"
    # Попытка скачать автоматом (если интернет есть)
    try {
      $url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
      $tmp = "$env:TEMP\python-installer.exe"
      Write-Host "Скачиваю Python..."
      Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
      Start-Process -FilePath $tmp -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1" -Wait
      $py = "python"
      Ok "Python установлен"
    } catch { Fail "Не удалось скачать Python: $_"; exit 1 }
  } else { Ok "Python $py $(& $py --version 2>&1)" }

  # Проверка версии
  $ver = & $py -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
  if ([version]$ver -lt [version]"3.11") { Fail "Требуется Python >=3.11 (найден $ver)"; exit 1 }

  # 2. GTK check (MSYS2)
  $gtkOk = $false
  try { & $py -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Adw; print('ok')" 2>$null | Out-Null; if ($LASTEXITCODE -eq 0){$gtkOk=$true} } catch {}
  if ($gtkOk) { Ok "GTK4 + libadwaita OK" }
  else {
    Warn "GTK4/libadwaita не найден — нужен MSYS2"
    Write-Host "  Установи MSYS2 https://www.msys2.org/ → UCRT64: pacman -S mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-libadwaita mingw-w64-ucrt-x86_64-python-gobject"
    Write-Host "  Или: pip install gvsbuild && gvsbuild build gtk4 libadwaita"
    Write-Host "  Продолжаю — без GTK приложение не запустится, но pip deps поставятся"
  }

  # 3. pip install -e . (создаст fragile-notes.exe в Scripts)
  Write-Host "`n→ pip install" -ForegroundColor Cyan
  & $py -m pip install --upgrade pip 2>&1 | Out-Null
  & $py -m pip install -e . 2>&1 | Tee-Object -FilePath "$AppDir\install.log" | Out-Null
  if ($LASTEXITCODE -eq 0) { Ok "pip install -e . OK" } else { Fail "pip install failed — смотри $AppDir\install.log"; Get-Content "$AppDir\install.log" | Select-Object -Last 20 | Write-Host }

  # 4. Создаем обертку fragile-notes.exe в {app} если pip поставил в Scripts
  $scripts = & $py -c "import sysconfig; print(sysconfig.get_path('scripts'))" 2>$null
  $srcExe = Join-Path $scripts "fragile-notes.exe"
  $dstExe = Join-Path $AppDir "fragile-notes.exe"
  if (Test-Path $srcExe) { Copy-Item $srcExe $dstExe -Force; Ok "Скопирован $dstExe" }
  # Fallback — батник-обертка
  if (-not (Test-Path $dstExe)) {
    "@echo off`n`"$py`" -m main %*" | Out-File -FilePath $dstExe -Encoding ascii -Force
    # На самом деле делаем .bat и .exe через copy
    $bat = Join-Path $AppDir "fragile-notes.bat"
    "@echo off`n`"$py`" `"$AppDir\main.py`" %*" | Out-File $bat -Encoding ascii
    Ok "Создан $bat (fallback)"
    $dstExe = $bat
  }

  # 5. Vault
  $Vault = "$env:USERPROFILE\desktop"
  if (-not (Test-Path $Vault)) {
    New-Item -ItemType Directory -Force -Path "$Vault\01 Home","$Vault\02 Daily","$Vault\_System" | Out-Null
    "# Home" | Out-File "$Vault\01 Home\Home.md" -Encoding utf8
    Ok "Vault создан $Vault"
  } else { Ok "Vault $Vault" }

  # 6. Проверка bootstrap
  if (Test-Path "$AppDir\scripts\bootstrap.ps1") {
    & powershell -ExecutionPolicy Bypass -File "$AppDir\scripts\bootstrap.ps1" 2>&1 | Out-Null
  }

  Write-Host "`nГотово! Запусти Fragile Notes из меню Пуск." -ForegroundColor Cyan
}
