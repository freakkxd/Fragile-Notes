# -*- mode: python ; coding: utf-8 -*-
# FragileNotes.spec — PyInstaller для Windows (генерируется build.ps1, можно править вручную)
block_cipher = None

a = Analysis(
    ['../../main.py'],
    pathex=['../..'],
    binaries=[],
    datas=[
        ('../../fragilenotes', 'fragilenotes'),
        ('../fragile-notes.svg', '.'),
    ],
    hiddenimports=['gi', 'gi.repository.Gtk', 'gi.repository.Adw', 'gi.repository.GLib', 'gi.repository.Gio', 'yaml', 'cryptography'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FragileNotes',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='../fragile-notes.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='FragileNotes',
)
