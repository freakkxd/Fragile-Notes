# build.ps1 — Сборка Windows exe + инсталлера Fragile Notes
# Требует: Python 3.11+, Node (опционально), Inno Setup 6 (опционально для EXE инсталлера)
# Запуск: powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1
# Или:    packaging/windows/build.ps1 -PortableOnly

param(
  [switch]$PortableOnly,
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot/../..").Path
Set-Location $Root

Write-Host "== Fragile Notes — Windows build ==" -ForegroundColor Cyan
Write-Host "Root: $Root"

# 1. Проверка Python
try { $pyVer = & $Python --version 2>&1 } catch { Write-Error "Python не найден: $Python"; exit 1 }
Write-Host "✓ $pyVer" -ForegroundColor Green
$ver = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"; 
if ([version]$ver -lt [version]"3.11") { Write-Error "Требуется Python >=3.11 (найден $ver)"; exit 1 }

# 2. Проверка GTK (MSYS2)
$gtkOk = $false
try { & $Python -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Adw; print('ok')" 2>$null | Out-Null; $gtkOk = $true } catch {}
if ($gtkOk) { Write-Host "✓ GTK4 + libadwaita (PyGObject) OK" -ForegroundColor Green }
else {
  Write-Host "⚠ GTK4/libadwaita не найден — на Windows нужен MSYS2" -ForegroundColor Yellow
  Write-Host "  Установи MSYS2: https://www.msys2.org/"
  Write-Host "  В MSYS2 UCRT64: pacman -S mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-libadwaita mingw-w64-ucrt-x86_64-python-gobject"
  Write-Host "  Или используй gvsbuild: pip install gvsbuild && gvsbuild build gtk4 libadwaita"
  Write-Host "  Сборка продолжится, но exe может не запуститься без GTK"
}

# 3. Установка зависимостей
Write-Host "`n→ pip install -e . + pyinstaller" -ForegroundColor Cyan
& $Python -m pip install --upgrade pip | Out-Null
& $Python -m pip install -e . | Out-Null
& $Python -m pip install pyinstaller  | Out-Null

# 4. Конвертация SVG → ICO для Windows (если rsvg-convert или Inkscape доступен)
$IconIco = "$Root/packaging/fragile-notes.ico"
$IconSvg = "$Root/packaging/fragile-notes.svg"
if (-not (Test-Path $IconIco)) {
  if (Get-Command rsvg-convert -ErrorAction SilentlyContinue) {
    & rsvg-convert -w 256 -h 256 "$IconSvg" -o "$env:TEMP/fragile.png"; 
    # png → ico через Python Pillow
    & $Python -c "from PIL import Image; im=Image.open(r'$env:TEMP/fragile.png'); im.save(r'$IconIco', sizes=[(256,256),(48,48),(32,32),(16,16)])" 2>$null
    if (Test-Path $IconIco) { Write-Host "✓ Иконка $IconIco создана" -ForegroundColor Green }
  } else {
    Write-Host "⚠ rsvg-convert не найден, использую SVG как есть (Inno Setup конвертирует сам)" -ForegroundColor Yellow
  }
}
$IconParam = if (Test-Path $IconIco) { $IconIco } else { $IconSvg }

# 5. PyInstaller сборка (one-folder portable)
Write-Host "`n→ PyInstaller (one-folder) — это 30-60 сек" -ForegroundColor Cyan
$Spec = "$Root/packaging/windows/FragileNotes.spec"
# Генерируем spec на лету если нет
if (-not (Test-Path $Spec)) {
  Write-Host "  Генерация spec..."
  & $Python -m PyInstaller --name="FragileNotes" --windowed --onedir --icon="$IconParam" --add-data="fragilenotes;fragilenotes" --add-data="packaging/fragile-notes.svg;." --hidden-import="gi" --hidden-import="gi.repository.Gtk" --hidden-import="gi.repository.Adw" --hidden-import="yaml" --hidden-import="cryptography" main.py --distpath="dist/windows" --workpath="build/windows" --specpath="packaging/windows" --noconfirm 2>&1 | Out-Null
} else {
  & $Python -m PyInstaller --noconfirm --distpath="dist/windows" --workpath="build/windows" "$Spec" 2>&1 | Out-Null
}
# Альтернатива — прямой вызов без spec (если spec не сгенерился)
if (-not (Test-Path "dist/windows/FragileNotes/FragileNotes.exe")) {
  Write-Host "  Прямая сборка..."
  & $Python -m PyInstaller --noconfirm --windowed --name="FragileNotes" --icon="$IconParam" --hidden-import="gi" --hidden-import="gi.repository.Gtk" --hidden-import="gi.repository.Adw" main.py --distpath="dist/windows" --workpath="build/windows" 2>&1 | Out-Null
}

if (Test-Path "dist/windows/FragileNotes/FragileNotes.exe") {
  Write-Host "✓ Portable собран: dist/windows/FragileNotes/FragileNotes.exe" -ForegroundColor Green
  $size = (Get-ChildItem "dist/windows/FragileNotes" -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
  Write-Host "  Размер: $([math]::Round($size,1)) MB"
} else {
  Write-Error "✗ PyInstaller не собрал exe — проверь логи build/windows/"
  exit 1
}

# 6. Portable ZIP
$Zip = "dist/FragileNotes-Portable-v0.1.1.zip"
if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path "dist/windows/FragileNotes/*" -DestinationPath $Zip -Force
Write-Host "✓ ZIP: $Zip" -ForegroundColor Green

if ($PortableOnly) { Write-Host "`nГотово (portable only). Для EXE инсталлера запусти без -PortableOnly и установи Inno Setup 6." -ForegroundColor Cyan; exit 0 }

# 7. Inno Setup EXE (требует iscc)
$Iscc = $null
foreach ($p in @("C:\Program Files (x86)\Inno Setup 6\ISCC.exe", "C:\Program Files\Inno Setup 6\ISCC.exe")) { if (Test-Path $p) { $Iscc = $p; break } }
if (-not $Iscc) { $Iscc = Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source }
if ($Iscc) {
  Write-Host "`n→ Inno Setup: $Iscc" -ForegroundColor Cyan
  & $Iscc "packaging/windows/installer.iss"
  if ($LASTEXITCODE -eq 0 -and (Test-Path "dist/FragileNotes-Setup-v0.1.1.exe")) {
    Write-Host "✓ Installer: dist/FragileNotes-Setup-v0.1.1.exe" -ForegroundColor Green
  } else {
    Write-Host "⚠ Inno Setup завершился с кодом $LASTEXITCODE" -ForegroundColor Yellow
  }
} else {
  Write-Host "`n⚠ Inno Setup не найден — пропускаю EXE инсталлер" -ForegroundColor Yellow
  Write-Host "  Скачай: https://jrsoftware.org/isdl.php"
  Write-Host "  Затем: iscc packaging/windows/installer.iss"
}

Write-Host "`n== Готово ==" -ForegroundColor Cyan
Write-Host "Portable: dist/FragileNotes-Portable-v0.1.1.zip"
Write-Host "Installer: dist/FragileNotes-Setup-v0.1.1.exe (если Inno установлен)"
Write-Host "Запуск portable: dist/windows/FragileNotes/FragileNotes.exe"
