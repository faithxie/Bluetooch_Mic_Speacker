"""
运行期路径解析

区分「源码运行」与「PyInstaller 打包运行」两种场景：

* 源码运行：
    * 只读资源（图标等）         ->  <项目根>/audio_router/assets/
    * 可写配置（设备偏好、日志） ->  <项目根>/.device_prefs.json

* 打包运行（sys.frozen）：
    * _MEIPASS 是 PyInstaller 解包出来的临时只读目录，进程退出即删除，
      绝不可以在里面写文件（写了下次启动就丢）。
    * 因此把可写数据放到 %APPDATA%\\BTSPK\\ 下，
      这也符合 Windows 应用惯例，且不会因为 exe 放在 C:\\Program Files
      这种无写权限目录而整段失败。
"""
import os
import sys

APP_NAME = 'BTSPK'


def is_frozen() -> bool:
    """当前是否运行在 PyInstaller 打包出的 exe 中"""
    return bool(getattr(sys, 'frozen', False))


def project_root() -> str:
    """项目根目录（源码运行）。

    __file__ = <root>/audio_router/services/paths.py
      dirname ×1 -> services/   ×2 -> audio_router/   ×3 -> <root>
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resource_dir() -> str:
    """只读资源根目录（assets 的上一级）。

    打包后 PyInstaller 把 datas 解到 _MEIPASS；
    此时 <_MEIPASS>/audio_router/assets 依然成立，
    因为 spec 中按包内相对路径收集了 assets。
    """
    if is_frozen():
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            return meipass
        # onefile 下理论上不会有这个分支，兜底取 exe 所在目录
        return os.path.dirname(os.path.abspath(sys.executable))
    return project_root()


def asset_path(*parts: str) -> str:
    """拼接 assets 下的资源路径，例：asset_path('icon.ico')"""
    return os.path.join(resource_dir(), 'audio_router', 'assets', *parts)


def data_dir() -> str:
    """可写数据目录。打包后为 %APPDATA%\\BTSPK，源码运行为项目根。"""
    if is_frozen():
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        path = os.path.join(base, APP_NAME)
    else:
        path = project_root()
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        # 极端情况下（无 HOME / 无权限）退回到 exe 同目录
        path = os.path.dirname(os.path.abspath(sys.executable if is_frozen() else __file__))
    return path


def data_file(filename: str) -> str:
    """可写数据文件完整路径"""
    return os.path.join(data_dir(), filename)
