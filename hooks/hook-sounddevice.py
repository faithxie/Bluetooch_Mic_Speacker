# ------------------------------------------------------------------
# 本地覆盖版 sounddevice hook（优先于 hooks-contrib 里的同名 hook）
#
# 背景：官方 hook 用 glob("libportaudio*.*") 把 macOS dylib、
# arm64、32bit 版 PortAudio 全打进去。本项目确定只在
# Windows x64 上分发，运行时也只按名加载 libportaudio64bit.dll，
# 因此这里只收 win64 两个变体，省约 4MB。
#
# 使用：build_exe.spec 里 Analysis(..., hookspath=['hooks'])
# ------------------------------------------------------------------
import pathlib

from PyInstaller.utils.hooks import get_module_file_attribute, logger

binaries = []
datas = []

# PyPI 的 Windows/macOS wheel 把 PortAudio 放在
# site-packages/_sounddevice_data/portaudio-binaries/ 下
module_dir = pathlib.Path(get_module_file_attribute('sounddevice')).parent
data_dir = module_dir / '_sounddevice_data' / 'portaudio-binaries'
if data_dir.is_dir():
    destdir = str(data_dir.relative_to(module_dir))
    for lib_file in data_dir.glob('libportaudio64bit*.dll'):
        binaries += [(str(lib_file), destdir)]
    readme_file = data_dir / 'README.md'
    if readme_file.is_file():
        datas += [(str(readme_file), destdir)]

if not binaries:
    logger.warning('portaudio win64 shared library not found - sounddevice will likely fail!')
