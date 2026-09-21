"""
蓝牙音频路由 - 主入口
蓝牙耳机麦克风 → 电脑扬声器 实时监听软件

源码运行：python main.py
打包运行：BTSPK.exe（见 build_exe.bat）
"""
import sys
import os
import traceback
from datetime import datetime


def _log(msg: str):
    """输出日志。

    打包成 --windowed（无控制台）后 stdout 可能为 None，
    直接 print 会抛 AttributeError，因此这里做兼容处理，
    并额外落盘到数据目录下的 BTSPK.log，方便排障。
    """
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        if sys.stdout is not None:
            print(line)
            sys.stdout.flush()
    except Exception:
        pass
    try:
        if getattr(sys, 'frozen', False):
            base = os.environ.get('APPDATA') or os.path.expanduser('~')
            log_path = os.path.join(base, 'BTSPK', 'BTSPK.log')
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass


def _global_excepthook(exc_type, exc_value, exc_tb):
    """全局异常捕获：记录但尽量不退出（音频线程里的异常不应拖垮 UI）"""
    _log('=' * 60)
    _log('UNHANDLED ERROR')
    _log(''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    _log('=' * 60)


sys.excepthook = _global_excepthook

# 将项目根目录加入路径（打包后 PyInstaller 已处理好，这里对源码运行生效）
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_router.ui import MainWindow


def _show_fatal_error(text: str):
    """致命错误提示。

    有控制台就 input() 停住让用户看清；
    打包成窗口程序没有控制台，改用系统弹窗。
    """
    _log(text)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('BTSPK 启动失败', text)
        root.destroy()
    except Exception:
        try:
            input('Press Enter to exit...')
        except Exception:
            pass


def main():
    _log('Bluetooth Audio Router starting...')
    try:
        app = MainWindow()
        _log('App window created.')
        app.run()
        _log('App exited normally.')
    except Exception as e:
        _log(f'FATAL ERROR: {e}')
        _log(traceback.format_exc())
        _show_fatal_error(
            f'程序启动失败：\n\n{type(e).__name__}: {e}\n\n'
            f'详细信息见日志：%APPDATA%\\BTSPK\\BTSPK.log'
        )


if __name__ == '__main__':
    main()
