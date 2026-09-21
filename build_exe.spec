# -*- mode: python ; coding: utf-8 -*-
"""
BTSPK 打包配置（PyInstaller spec）

打包命令：
    venv\\Scripts\\pyinstaller.exe --noconfirm --clean build_exe.spec
或者直接双击：
    build_exe.bat

关键点说明
----------
1. sounddevice 是「纯 Python 模块 + 一个捆绑的 PortAudio DLL」的组合：
   - sounddevice.py        普通模块，PyInstaller 能自动发现
   - _sounddevice.py       cffi 生成的胶水层，需显式 hiddenimport
   - _sounddevice_data/    存 libportaudio64bit.dll，运行时按相对路径
                           <_sounddevice_data>/portaudio-binaries/xxx.dll 加载。
                           这一步是动态拼路径 + dlopen，静态分析看不到，
                           必须用 collect_data_files 手工收集，否则 exe 一启动
                           就报 "PortAudio library not found"。
   - cffi / _cffi_backend  加载 DLL 的底层依赖，一并收进来更稳。

2. scipy / numpy 由 PyInstaller 官方 hooks 处理，无需手工干预。

3. --windowed 不弹黑框，但代价是 stdout 为 None，
   所以 main.py 里对 print 做了兼容并把日志落到 %APPDATA%\\BTSPK\\BTSPK.log。

4. onefile 体积小、分发方便，代价是每次启动要解包（约 1~3 秒）。
   想启动更快可把 ONE_FILE 改成 False，产出 onedir 目录版。
"""

import os
from PyInstaller.utils.hooks import collect_submodules

ONE_FILE = True          # False -> 生成 dist/BTSPK/ 目录版（启动更快）
APP_NAME = 'BTSPK'
ICON_FILE = os.path.join('audio_router', 'assets', 'icon.ico')

# ---------------------------------------------------------------- 数据文件
datas = []

# ① sounddevice 捆绑的 PortAudio DLL —— 缺了它 exe 直接跑不起来。
#    注意：完全交给本地 hooks/hook-sounddevice.py 处理（经 hookspath 生效），
#    它只收 win64 两个变体。不要在这里调 collect_data_files('_sounddevice_data')，
#    否则会把 mac dylib / arm64 / 32bit 全部重新塞回来，瘦身就白做了。

# ② 应用自己的资源目录（当前为空，占位以保证以后加图标/音源文件时自动打包）
assets_dir = os.path.join('audio_router', 'assets')
if os.path.isdir(assets_dir):
    for name in os.listdir(assets_dir):
        full = os.path.join(assets_dir, name)
        if os.path.isfile(full):
            datas.append((full, os.path.join('audio_router', 'assets')))

# ---------------------------------------------------------------- 隐藏导入
hiddenimports = [
    '_sounddevice',       # cffi 胶水层，静态分析找不到
    '_cffi_backend',      # cffi 的 C 扩展后端
    'cffi',
    'sounddevice',

    # ---- scipy.stats._sobol 的坑（实测踩到，务必保留）----
    # 它是 Cython 编译出的 .pyd，在 init 阶段用「运行时字符串」去
    # import importlib.resources（见 .pyd 内的字符串表），
    # PyInstaller 的静态分析看不到这个导入，于是 exe 启动即
    # ModuleNotFoundError: No module named 'importlib.resources'。
    # 而 scipy.signal -> scipy.stats 是硬依赖链，必然被拉起。
    'importlib.resources',
    'importlib.resources.abc',
    'importlib.abc',
    'importlib.metadata',
    'importlib.readers',
    'importlib.util',
    'importlib._adapters',
    'importlib._common',
    # scipy.stats 自身的子模块也容易被漏掉
    'scipy.stats',
    'scipy.stats._sobol',
    'scipy.stats._qmc',
    'scipy.signal',
    'scipy.signal._peak_finding',
]
# 保险起见，把 audio_router 所有子模块都收进来
hiddenimports += collect_submodules('audio_router')

# ---------------------------------------------------------------- 排除项
excludes = [
    # 开发期才用的东西，打进去只会让体积暴涨
    'matplotlib', 'pandas', 'IPython', 'jupyter', 'notebook',
    'pytest', 'pip', 'wheel',
    'PIL', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx',
    # 本项目用不到的可选后端
    'onnxruntime',
    # 注意：不要排除 setuptools！
    # Python 3.10 下 importlib.metadata 的部分路径会回落到
    # setuptools 提供的 backport，排掉会引发难查的 ImportError。
]

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=['hooks'],    # 本地 hooks/ 优先于官方 hook，实现 PortAudio 瘦身
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe_kwargs = dict(
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 程序，不弹控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
if os.path.isfile(ICON_FILE):
    exe_kwargs['icon'] = ICON_FILE

if ONE_FILE:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
        **exe_kwargs,
    )
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_kwargs)
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        strip=False, upx=True, upx_exclude=[], name=APP_NAME,
    )
