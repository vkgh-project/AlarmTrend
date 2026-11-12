from pathlib import Path

root = Path(SPECPATH).parent

a = Analysis(
    [str(root / 'windows_launcher.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / 'alarmtrend' / 'templates'), 'alarmtrend/templates'),
        (str(root / 'alarmtrend' / 'static'), 'alarmtrend/static'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AlarmTrend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AlarmTrend',
)
