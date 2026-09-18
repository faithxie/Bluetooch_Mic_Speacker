"""
蓝牙音频路由 - 主入口
蓝牙耳机麦克风 → 电脑扬声器 实时监听软件
"""
import sys
import os
import traceback

# 全局异常捕获（打印到控制台）
def _global_excepthook(exc_type, exc_value, exc_tb):
    print("\n" + "=" * 70)
    print("  UNHANDLED ERROR")
    print("=" * 70)
    traceback.print_exception(exc_type, exc_value, exc_tb)
    print("=" * 70 + "\n")
    # 尝试继续运行，不退出

sys.excepthook = _global_excepthook

# 将项目根目录加入路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_router.ui import MainWindow


def main():
    print("Bluetooth Audio Router starting...")
    try:
        app = MainWindow()
        print("App window created.")
        app.run()
        print("App exited normally.")
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        traceback.print_exc()
        input("Press Enter to exit...")


if __name__ == '__main__':
    main()
