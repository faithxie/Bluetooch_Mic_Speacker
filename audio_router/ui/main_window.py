"""
主窗口 - 深色重设计版
按「蓝牙音频路由-界面重设计」设计稿实现：

  ┌ 顶栏：图标 + 标题/副标题 + 运行状态胶囊
  ├ 控制卡：大号「开始路由」按钮 + 「跟随系统默认设备」开关
  ├ 信号链路：输入面板 → 输出面板（并排，中间蓝色箭头）
  ├ 实时监测：输入/输出渐变电平条 + 延迟/缓冲/丢失统计
  ├ 防啸叫 · 回音消除：移频 / 自适应陷波 / 噪声门（开关+滑杆）
  ├ 音量：麦克风增益 / 扬声器音量（设计稿未画，功能保留故收紧为一小块）
  └ 底栏：滚动提示 · WASAPI v2.0

交互控件（开关/滑杆/电平条）为自绘 Canvas 部件，
以贴近设计稿的圆角、配色与手感。
"""
import math
import os
import threading
from typing import List
from tkinter import ttk, messagebox

import tkinter as tk

from ..models.audio_device import AudioDeviceInfo
from ..services.device_manager import DeviceManager
from ..services.audio_router import AudioRouterEngine
from ..services.paths import data_file, asset_path, is_frozen


# ============================================================
# 设计稿色板
# ============================================================
COLOR_PAGE = "#0b0f15"        # 页面背景（近黑，带蓝调）
COLOR_PANEL = "#141b24"       # 面板底
COLOR_FIELD = "#0d131b"       # 输入控件（下拉框）底
COLOR_BORDER = "#223041"      # 面板描边
COLOR_TRACK = "#1d2733"       # 电平条 / 滑杆轨道
COLOR_PILL = "#18202b"        # 胶囊 / 徽章底
COLOR_BADGE_BG = "#17293f"    # 「系统默认」徽章底

COLOR_ACCENT = "#2f7df6"      # 主蓝
COLOR_ACCENT_HI = "#4a90ff"
COLOR_TEXT = "#e9eef5"        # 主文字
COLOR_TEXT_2 = "#93a0b0"      # 次级文字
COLOR_TEXT_3 = "#5f6b7a"      # 弱化文字

COLOR_GREEN = "#41d98d"       # 成功 / 运行中
COLOR_GREEN_BG = "#10261b"    # 绿色横幅底
COLOR_ORANGE = "#f0a13c"      # 警示
COLOR_RED = "#f0503c"         # 危险（停止按钮 / 高电平）
COLOR_METER_YELLOW = "#f5c542"
COLOR_METER_GREEN = "#2fd06b"

FONT = "Microsoft YaHei UI"
MONO = "Consolas"


def _lerp_color(c1: str, c2: str, t: float) -> str:
    """两个 #rrggbb 颜色按 t∈[0,1] 线性插值"""
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


# ============================================================
# 自绘部件
# ============================================================
class RoundedPanel(tk.Canvas):
    """圆角面板：Canvas 画圆角矩形打底，inner Frame 承载内容

    高度随内容自适应（inner Configure -> 调 Canvas 高度），
    宽度随父容器拉伸（Canvas Configure -> 拉宽 inner）。
    """

    def __init__(self, master, radius=14, pad=14,
                 bg=COLOR_PANEL, border=COLOR_BORDER, **kw):
        super().__init__(master, bg=COLOR_PAGE, highlightthickness=0, bd=0, **kw)
        self._radius = radius
        self._pad = pad
        self._pbg = bg
        self._pborder = border
        self.inner = tk.Frame(self, bg=bg)
        self._win = self.create_window(pad, pad, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.bind("<Configure>", self._on_panel_configure)

    def _on_inner_configure(self, event):
        want = event.height + 2 * self._pad
        if abs(self.winfo_height() - want) > 1:
            self.configure(height=want)
        self._draw()

    def _on_panel_configure(self, event):
        want = event.width - 2 * self._pad
        if want > 1:
            self.itemconfigure(self._win, width=want)
        self._draw()

    def _draw(self):
        # 只重画背景多边形（tag="bg"）。
        # 绝不能 delete("all") —— 那会把 __init__ 里创建的内容窗口项
        # （inner Frame）一起删掉，面板从此只剩一个空壳、高度也锁死在默认值。
        self.delete("bg")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 4 or h <= 4:
            return
        r = min(self._radius, w // 2, h // 2)
        pts = [r, 0, w - r, 0, w, 0, w, r, w, h - r, w, h,
               w - r, h, r, h, 0, h, 0, h - r, 0, r, 0, 0]
        self.create_polygon(pts, smooth=True, fill=self._pbg,
                            outline=self._pborder, width=1, tags="bg")
        # 把内容窗口提到背景之上（Canvas 按创建顺序叠放，
        # 新画的 polygon 默认盖在先创建的内容窗口上面）
        self.tag_raise(self._win)


class ToggleSwitch(tk.Canvas):
    """iOS 风格拨动开关（自绘）"""

    def __init__(self, master, var: tk.BooleanVar, command=None,
                 width=42, height=22, bg=COLOR_PANEL):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._var = var
        self._cmd = command
        # 注意：绝不能用 self._w 存宽度 —— 那是 Tkinter 内部的控件路径属性，
        # 覆盖后 pack() 拿到的是数字而不是窗口名，直接报
        # TclError: bad argument "42": must be name of window
        self._tw = width
        self._th = height
        self.bind("<Button-1>", self._on_click)
        self.bind("<Configure>", lambda e: self._draw())

    def _on_click(self, _event):
        self._var.set(not self._var.get())
        self._draw()
        if self._cmd:
            self._cmd()

    def refresh(self):
        """外部改 var 后调用，同步外观"""
        self._draw()

    def _draw(self):
        w, h = self._tw, self._th
        self.delete("all")
        r = h / 2
        on = self._var.get()
        track = COLOR_ACCENT if on else "#2a3644"
        self.create_line(r, r, w - r, r, width=h, capstyle="round", fill=track)
        kx = w - r if on else r
        self.create_oval(kx - r + 3, 3, kx + r - 3, h - 3,
                         fill="#ffffff", outline="")


class CanvasSlider(tk.Canvas):
    """细轨道 + 圆形滑钮的自绘滑杆（贴设计稿样式）

    兼容旧代码习惯：
      - .config(state='disabled'/'normal') 置灰禁用
      - command(value) 在用户拖动时回调（传 float）
    """

    def __init__(self, master, var: tk.DoubleVar, lo, hi,
                 command=None, resolution=None, height=22, bg=COLOR_PANEL):
        super().__init__(master, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._var = var
        self._lo = float(lo)
        self._hi = float(hi)
        self._cmd = command
        self._res = resolution
        self._state = "normal"
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._on_event)
        self.bind("<B1-Motion>", self._on_event)

    # ---- 兼容 .config(state=...) ----
    def config(self, **kw):  # noqa: A003 - 有意覆盖
        if "state" in kw:
            self._state = kw.pop("state")
            self._draw()
        super().config(**kw)

    def set_enabled(self, enabled: bool):
        self.config(state="normal" if enabled else "disabled")

    # ---- 交互 ----
    def _on_event(self, event):
        if self._state != "normal":
            return
        w = self.winfo_width()
        pad = 10
        span = max(1, w - 2 * pad)
        frac = min(1.0, max(0.0, (event.x - pad) / span))
        v = self._lo + frac * (self._hi - self._lo)
        if self._res:
            v = round(v / self._res) * self._res
        v = min(self._hi, max(self._lo, v))
        if abs(v - self._var.get()) > 1e-9:
            self._var.set(v)
            self._draw()
            if self._cmd:
                self._cmd(v)

    # ---- 绘制 ----
    def _draw(self):
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 4 or h <= 4:
            return
        self.delete("all")
        mid = h / 2
        pad = 10
        disabled = self._state != "normal"

        frac = (self._var.get() - self._lo) / (self._hi - self._lo)
        frac = min(1.0, max(0.0, frac))
        x = pad + frac * (w - 2 * pad)

        track = COLOR_TRACK
        fill = "#33445a" if disabled else COLOR_ACCENT
        knob = "#6b7787" if disabled else "#ffffff"

        self.create_line(pad, mid, w - pad, mid, width=4,
                         capstyle="round", fill=track)
        if x > pad + 2:
            self.create_line(pad, mid, x, mid, width=4,
                             capstyle="round", fill=fill)
        self.create_oval(x - 7, mid - 7, x + 7, mid + 7,
                         fill=knob, outline=fill, width=2)


# ============================================================
# 主窗口
# ============================================================
class MainWindow:
    """音频路由主窗口（深色重设计版）"""

    # 电平表参数（沿用电平修复版：dB 地板 + 快起慢落 + 峰值保持）
    METER_FLOOR_DB = -70.0
    METER_CEIL_DB = 0.0
    METER_ATTACK = 1.0
    METER_RELEASE = 0.25
    METER_PEAK_DECAY = 0.012

    def __init__(self):
        # 核心引擎
        self.engine = AudioRouterEngine()
        self.engine.set_status_callback(self._on_engine_status)
        self.engine.set_error_callback(self._on_engine_error)

        # 设备列表缓存
        self._input_devices: List[AudioDeviceInfo] = []
        self._output_devices: List[AudioDeviceInfo] = []

        # 偏好（必须早于 _build_ui，因为控件初始值要读它）
        self._prefs: dict = self._load_prefs()

        # UI 刷新标志
        self._ui_running = True

        # 创建窗口
        self.root = tk.Tk()
        self.root.title("蓝牙音频路由 - Bluetooth Audio Router")
        self.root.configure(bg=COLOR_PAGE)

        # 窗口尺寸：按屏幕可用高度自适应（内容一屏为主，超出可滚动）
        scr_w = self.root.winfo_screenwidth()
        scr_h = self.root.winfo_screenheight()
        avail_h = scr_h - 110
        win_h = int(min(920, max(520, avail_h)))
        win_w = int(min(780, max(600, scr_w - 200)))
        pos_x = max(0, (scr_w - win_w) // 2)
        pos_y = max(0, (avail_h - win_h) // 2 + 20)
        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.root.minsize(560, 420)

        # 样式
        self._setup_styles()

        # 构建界面
        self._build_ui()

        # 初始化设备列表（启动时允许恢复上次偏好，不弹提示框）
        self._refresh_devices_impl(user_action=False)

        # 启动 UI 刷新定时器
        self._update_ui()

        # 与「开关」联动的控件状态 + 引擎初值同步
        self._sync_control_states()

        # 窗口关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 样式设置 ----------

    def _setup_styles(self):
        """ttk 样式（clam 主题，深色下拉框 / 滚动条）"""
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("TFrame", background=COLOR_PAGE)
        style.configure("TLabel", background=COLOR_PAGE,
                        foreground=COLOR_TEXT, font=(FONT, 10))

        # 下拉框（深色输入框 + 深色箭头区）
        style.configure("TCombobox",
                        fieldbackground=COLOR_FIELD,
                        background=COLOR_FIELD,
                        foreground=COLOR_TEXT,
                        bordercolor=COLOR_BORDER,
                        lightcolor=COLOR_FIELD,
                        darkcolor=COLOR_FIELD,
                        borderwidth=1,
                        arrowcolor=COLOR_TEXT_2,
                        padding=(8, 5))
        style.map("TCombobox",
                  fieldbackground=[("readonly", COLOR_FIELD)],
                  foreground=[("readonly", COLOR_TEXT)],
                  bordercolor=[("focus", COLOR_ACCENT)])

        # 下拉列表（弹出部分）
        self.root.option_add("*TCombobox*Listbox.background", COLOR_PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", COLOR_TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLOR_ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 9))
        self.root.option_add("*TCombobox*Listbox.relief", "flat")

        # 滚动条（深色）
        style.configure("Vertical.TScrollbar",
                        background=COLOR_TRACK,
                        troughcolor=COLOR_PAGE,
                        bordercolor=COLOR_PAGE,
                        lightcolor=COLOR_TRACK,
                        darkcolor=COLOR_TRACK,
                        arrowcolor=COLOR_TEXT_3)
        style.map("Vertical.TScrollbar",
                  background=[("active", COLOR_BORDER),
                              ("pressed", COLOR_BORDER)])

    # ---------- 界面构建 ----------

    def _build_ui(self):
        """三层布局：固定顶栏 / 可滚动内容 / 固定底栏"""
        # ---- 固定顶部：标题 + 控制卡 ----
        top = tk.Frame(self.root, bg=COLOR_PAGE)
        top.pack(fill=tk.X, side=tk.TOP, padx=24, pady=(16, 0))

        self._build_header(top)
        self._build_control_section(top)

        # ---- 可滚动中间区 ----
        self._build_scroll_area()
        main = self._scroll_content

        # 「信号链路」小节标题
        self._build_section_label(main, "信号链路")

        # 输入 → 输出 并排面板
        self._build_signal_chain(main)

        # 实时监测
        self._build_monitor_panel(main)

        # 防啸叫 · 回音消除
        self._build_antifeedback_panel(main)

        # 音量（设计稿未画，功能保留，收紧为一小块）
        self._build_volume_panel(main)

        # ---- 固定底栏 ----
        self._build_footer()

    def _build_header(self, parent):
        """顶栏：图标 + 标题/副标题 + 状态胶囊"""
        header = tk.Frame(parent, bg=COLOR_PAGE)
        header.pack(fill=tk.X)

        # 蓝色圆角方块图标（Canvas 自绘）
        icon = tk.Canvas(header, width=40, height=40, bg=COLOR_PAGE,
                         highlightthickness=0, bd=0)
        icon.pack(side=tk.LEFT)
        icon.create_polygon([10, 0, 30, 0, 40, 0, 40, 10, 40, 30, 40, 40,
                             30, 40, 10, 40, 0, 40, 0, 30, 0, 10, 0, 0],
                            smooth=True, fill=COLOR_ACCENT, outline="")
        icon.create_text(20, 21, text="🎧", font=(FONT, 15), fill="#ffffff")

        # 标题 + 副标题
        text_col = tk.Frame(header, bg=COLOR_PAGE)
        text_col.pack(side=tk.LEFT, padx=(12, 0))
        tk.Label(text_col, text="蓝牙音频路由", bg=COLOR_PAGE, fg=COLOR_TEXT,
                 font=(FONT, 15, "bold"), anchor=tk.W).pack(anchor=tk.W)
        tk.Label(text_col, text="蓝牙耳机麦克风 → 扬声器 / HDMI 实时监听",
                 bg=COLOR_PAGE, fg=COLOR_TEXT_3,
                 font=(FONT, 9), anchor=tk.W).pack(anchor=tk.W, pady=(1, 0))

        # 状态胶囊（右上角）
        self.status_label = tk.Label(header, text="● 未运行",
                                     bg=COLOR_PILL, fg=COLOR_TEXT_3,
                                     font=(FONT, 9, "bold"), padx=12, pady=4)
        self.status_label.pack(side=tk.RIGHT, pady=(4, 0))

    def _build_control_section(self, parent):
        """控制卡：开始路由大按钮 + 跟随系统默认开关"""
        panel = RoundedPanel(parent, radius=16, pad=16)
        panel.pack(fill=tk.X, pady=(14, 0))
        inner = panel.inner

        self.start_button = tk.Button(
            inner, text="▶  开始路由",
            command=self._toggle_router,
            bg=COLOR_ACCENT, fg="#ffffff",
            font=(FONT, 13, "bold"),
            bd=0, cursor="hand2", relief=tk.FLAT,
            activebackground=COLOR_ACCENT_HI, activeforeground="#ffffff")
        self.start_button.pack(fill=tk.X, ipady=9)

        # 跟随系统默认设备：拨动开关 + 标题 + 说明
        follow_row = tk.Frame(inner, bg=COLOR_PANEL)
        follow_row.pack(fill=tk.X, pady=(12, 0))

        self.follow_default_var = tk.BooleanVar(
            value=bool(self._prefs.get("follow_system_default", False)))
        self.follow_toggle = ToggleSwitch(follow_row, self.follow_default_var,
                                          command=self._on_follow_default_toggle)
        self.follow_toggle.pack(side=tk.LEFT)

        text_col = tk.Frame(follow_row, bg=COLOR_PANEL)
        text_col.pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(text_col, text="跟随系统默认设备", bg=COLOR_PANEL, fg=COLOR_TEXT,
                 font=(FONT, 10, "bold"), anchor=tk.W).pack(anchor=tk.W)
        tk.Label(text_col, text="Windows 声音设置切换默认设备时自动跟随",
                 bg=COLOR_PANEL, fg=COLOR_TEXT_3,
                 font=(FONT, 8), anchor=tk.W).pack(anchor=tk.W)

    def _build_section_label(self, parent, text):
        """小节标题：灰色小字 + 右侧延伸细线"""
        row = tk.Frame(parent, bg=COLOR_PAGE)
        row.pack(fill=tk.X, pady=(14, 6))
        tk.Label(row, text=text, bg=COLOR_PAGE, fg=COLOR_TEXT_3,
                 font=(FONT, 9)).pack(side=tk.LEFT)
        tk.Frame(row, bg=COLOR_TRACK, height=1).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 0), pady=(4, 0))

    def _build_signal_chain(self, parent):
        """信号链路：输入面板 → 箭头 → 输出面板（并排）"""
        row = tk.Frame(parent, bg=COLOR_PAGE)
        row.pack(fill=tk.X)
        row.columnconfigure(0, weight=1, uniform="chain")
        row.columnconfigure(2, weight=1, uniform="chain")

        self._build_input_panel(row).grid(row=0, column=0, sticky="nsew")

        arrow = tk.Label(row, text="→", bg=COLOR_PAGE, fg=COLOR_ACCENT,
                         font=(FONT, 15, "bold"))
        arrow.grid(row=0, column=1, padx=8, pady=(0, 0))

        self._build_output_panel(row).grid(row=0, column=2, sticky="nsew")

    def _build_input_panel(self, parent):
        """输入设备面板"""
        panel = RoundedPanel(parent, radius=14, pad=12)
        inner = panel.inner

        # 标题行 + 刷新按钮
        head = tk.Frame(inner, bg=COLOR_PANEL)
        head.pack(fill=tk.X)
        tk.Label(head, text="🎤 输入 · 麦克风", bg=COLOR_PANEL,
                 fg=COLOR_TEXT, font=(FONT, 10, "bold")).pack(side=tk.LEFT)
        refresh_btn = tk.Button(head, text="↻", command=self._refresh_devices,
                                bg=COLOR_PANEL, fg=COLOR_TEXT_2,
                                font=(FONT, 12), bd=0, cursor="hand2",
                                activebackground=COLOR_PANEL,
                                activeforeground=COLOR_ACCENT, relief=tk.FLAT)
        refresh_btn.pack(side=tk.RIGHT)

        # 设备名 + 「系统默认」徽章
        name_row = tk.Frame(inner, bg=COLOR_PANEL)
        name_row.pack(fill=tk.X, pady=(10, 0))
        self.input_name_label = tk.Label(name_row, text="未选择设备",
                                         bg=COLOR_PANEL, fg=COLOR_TEXT,
                                         font=(FONT, 10, "bold"), anchor=tk.W)
        self.input_name_label.pack(side=tk.LEFT)
        self.input_default_badge = tk.Label(
            name_row, text="系统默认", bg=COLOR_BADGE_BG, fg="#5aa2ff",
            font=(FONT, 8), padx=7, pady=2)
        # 徽章仅在设备是系统默认时显示（见 _on_input_selected）

        # 下拉框
        self.input_combo = ttk.Combobox(inner, state="readonly", height=10)
        self.input_combo.pack(fill=tk.X, pady=(8, 0))
        self.input_combo.bind("<<ComboboxSelected>>", self._on_input_selected)

        # 元信息行：CH 1 · 16.0 kHz · Windows WASAPI
        self.input_meta_label = tk.Label(inner, text="请选择输入设备",
                                         bg=COLOR_PANEL, fg=COLOR_TEXT_3,
                                         font=(MONO, 8), anchor=tk.W)
        self.input_meta_label.pack(fill=tk.X, pady=(6, 0))
        return panel

    def _build_output_panel(self, parent):
        """输出设备面板"""
        panel = RoundedPanel(parent, radius=14, pad=12)
        inner = panel.inner

        head = tk.Frame(inner, bg=COLOR_PANEL)
        head.pack(fill=tk.X)
        tk.Label(head, text="🔊 输出 · 扬声器 / HDMI", bg=COLOR_PANEL,
                 fg=COLOR_TEXT, font=(FONT, 10, "bold")).pack(side=tk.LEFT)

        name_row = tk.Frame(inner, bg=COLOR_PANEL)
        name_row.pack(fill=tk.X, pady=(10, 0))
        self.output_name_label = tk.Label(name_row, text="未选择设备",
                                          bg=COLOR_PANEL, fg=COLOR_TEXT,
                                          font=(FONT, 10, "bold"), anchor=tk.W)
        self.output_name_label.pack(side=tk.LEFT)
        self.output_default_badge = tk.Label(
            name_row, text="系统默认", bg=COLOR_BADGE_BG, fg="#5aa2ff",
            font=(FONT, 8), padx=7, pady=2)

        self.output_combo = ttk.Combobox(inner, state="readonly", height=10)
        self.output_combo.pack(fill=tk.X, pady=(8, 0))
        self.output_combo.bind("<<ComboboxSelected>>", self._on_output_selected)

        self.output_meta_label = tk.Label(inner, text="请选择输出设备",
                                          bg=COLOR_PANEL, fg=COLOR_TEXT_3,
                                          font=(MONO, 8), anchor=tk.W)
        self.output_meta_label.pack(fill=tk.X, pady=(6, 0))

        # 绿色横幅：声音将从哪里播放
        self.output_target_label = tk.Label(inner, text="🔊 声音将从：未选择",
                                            bg=COLOR_GREEN_BG, fg=COLOR_GREEN,
                                            font=(FONT, 9, "bold"),
                                            anchor=tk.W, padx=10, pady=6)
        self.output_target_label.pack(fill=tk.X, pady=(10, 0))
        return panel

    def _build_monitor_panel(self, parent):
        """实时监测面板：电平条 + 延迟/缓冲/丢失统计"""
        panel = RoundedPanel(parent, radius=14, pad=12)
        panel.pack(fill=tk.X, pady=(12, 0))
        inner = panel.inner

        head = tk.Frame(inner, bg=COLOR_PANEL)
        head.pack(fill=tk.X)
        tk.Label(head, text="📈 实时监测", bg=COLOR_PANEL, fg=COLOR_TEXT,
                 font=(FONT, 10, "bold")).pack(side=tk.LEFT)

        stats = tk.Frame(head, bg=COLOR_PANEL)
        stats.pack(side=tk.RIGHT)
        self.latency_label = tk.Label(stats, text="延迟 -- ms", bg=COLOR_PANEL,
                                      fg=COLOR_TEXT_3, font=(MONO, 8))
        self.latency_label.pack(side=tk.LEFT, padx=(0, 12))
        self.buffer_label = tk.Label(stats, text="缓冲 --", bg=COLOR_PANEL,
                                     fg=COLOR_TEXT_3, font=(MONO, 8))
        self.buffer_label.pack(side=tk.LEFT, padx=(0, 12))
        self.xrun_label = tk.Label(stats, text="丢失 0", bg=COLOR_PANEL,
                                   fg=COLOR_TEXT_3, font=(MONO, 8))
        self.xrun_label.pack(side=tk.LEFT)

        # 输入电平行
        in_row = tk.Frame(inner, bg=COLOR_PANEL)
        in_row.pack(fill=tk.X, pady=(10, 4))
        tk.Label(in_row, text="输入", bg=COLOR_PANEL, fg=COLOR_TEXT_2,
                 font=(FONT, 9), width=5, anchor=tk.W).pack(side=tk.LEFT)
        self.input_meter_value = tk.Label(in_row, text="-∞ dB",
                                          bg=COLOR_PANEL, fg=COLOR_TEXT,
                                          font=(MONO, 8), width=8, anchor=tk.E)
        self.input_meter_value.pack(side=tk.RIGHT)
        self.input_meter_canvas = tk.Canvas(in_row, height=10,
                                            bg=COLOR_PANEL,
                                            highlightthickness=0, bd=0)
        self.input_meter_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                     padx=(6, 8))

        # 输出电平行
        out_row = tk.Frame(inner, bg=COLOR_PANEL)
        out_row.pack(fill=tk.X, pady=(4, 0))
        tk.Label(out_row, text="输出", bg=COLOR_PANEL, fg=COLOR_TEXT_2,
                 font=(FONT, 9), width=5, anchor=tk.W).pack(side=tk.LEFT)
        self.output_meter_value = tk.Label(out_row, text="-∞ dB",
                                           bg=COLOR_PANEL, fg=COLOR_TEXT,
                                           font=(MONO, 8), width=8, anchor=tk.E)
        self.output_meter_value.pack(side=tk.RIGHT)
        self.output_meter_canvas = tk.Canvas(out_row, height=10,
                                             bg=COLOR_PANEL,
                                             highlightthickness=0, bd=0)
        self.output_meter_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                      padx=(6, 8))

        # 停止状态下也要画出空轨道（Canvas 首次布局后触发一次）
        for cvs in (self.input_meter_canvas, self.output_meter_canvas):
            cvs.bind("<Configure>",
                     lambda e, c=cvs: (not self.engine.is_running)
                     and self._draw_meter(c, 0.0))

    def _build_antifeedback_panel(self, parent):
        """防啸叫 · 回音消除：移频 / 自适应陷波 / 噪声门"""
        panel = RoundedPanel(parent, radius=14, pad=14)
        panel.pack(fill=tk.X, pady=(12, 0))
        inner = panel.inner

        head = tk.Frame(inner, bg=COLOR_PANEL)
        head.pack(fill=tk.X)
        tk.Label(head, text="🛡 防啸叫 · 回音消除", bg=COLOR_PANEL,
                 fg=COLOR_TEXT, font=(FONT, 10, "bold")).pack(side=tk.LEFT)
        # 抑制门状态胶囊（噪声门开/关/过渡）
        self.gate_status_label = tk.Label(head, text="抑制门 · 关闭",
                                          bg=COLOR_PILL, fg=COLOR_TEXT_3,
                                          font=(FONT, 8, "bold"),
                                          padx=10, pady=3)
        self.gate_status_label.pack(side=tk.RIGHT)

        # ---- 1. 移频 ----
        # 真移频音染很小，但整体音高固定偏移 4Hz，对音乐/较真人声有可感差异
        # → 默认关闭，由用户按需开启。
        self.freq_shift_var = tk.BooleanVar(value=False)
        self._af_group(inner, title="移频",
                       desc="整体音高偏移 Hz，破坏反馈共振条件",
                       var=self.freq_shift_var,
                       command=self._on_freq_shift_toggle)
        self.freq_shift_amount_var = tk.DoubleVar(value=4.0)
        self.freq_shift_slider, self.freq_shift_amount_label = self._slider_row(
            inner, "移频量", self.freq_shift_amount_var, 2, 12,
            self._on_freq_shift_amount_change,
            resolution=0.5, value_text="4.0 Hz")

        tk.Frame(inner, bg=COLOR_TRACK, height=1).pack(fill=tk.X, pady=10)

        # ---- 2. 自适应陷波 ----
        self.notch_var = tk.BooleanVar(value=True)
        self._af_group(inner, title="自适应陷波",
                       desc="自动检测啸叫频点并实时抑制",
                       var=self.notch_var,
                       command=self._on_notch_toggle)
        self.notch_attenuation_var = tk.DoubleVar(value=10.0)
        self.notch_slider, self.notch_attenuation_label = self._slider_row(
            inner, "衰减深度", self.notch_attenuation_var, 3, 30,
            self._on_notch_attenuation_change,
            resolution=1.0, value_text="10 dB")

        # 当前陷波频率（判断「是否误伤自己人声」的直接依据）
        self.notch_freq_label = tk.Label(inner, text="当前陷波: 无",
                                         bg=COLOR_PANEL, fg=COLOR_TEXT_3,
                                         font=(FONT, 8), anchor=tk.W)
        self.notch_freq_label.pack(fill=tk.X, pady=(6, 0))

        tk.Frame(inner, bg=COLOR_TRACK, height=1).pack(fill=tk.X, pady=10)

        # ---- 3. 噪声门 ----
        # 按电平判定，字间停顿/气声易被误关门（吞字、喘息）→ 默认关闭。
        self.noise_gate_var = tk.BooleanVar(value=False)
        self._af_group(inner, title="噪声门",
                       desc="低于门限的信号自动静音",
                       var=self.noise_gate_var,
                       command=self._on_noise_gate_toggle)
        self.gate_threshold_var = tk.DoubleVar(value=1.5)
        self.gate_threshold_slider, self.gate_threshold_label = self._slider_row(
            inner, "灵敏度", self.gate_threshold_var, 0.5, 10.0,
            self._on_gate_threshold_change,
            resolution=0.1, value_text="中")
        # 噪声门用文字档位表达比裸数字更直观
        self.gate_threshold_label.config(font=(FONT, 9))

    def _af_group(self, parent, title, desc, var, command):
        """防啸叫分组头：拨动开关 + 标题 + 说明（说明与标题左对齐）"""
        row = tk.Frame(parent, bg=COLOR_PANEL)
        row.pack(fill=tk.X, pady=(0, 2))
        ToggleSwitch(row, var, command=command).pack(side=tk.LEFT)

        col = tk.Frame(row, bg=COLOR_PANEL)
        col.pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(col, text=title, bg=COLOR_PANEL, fg=COLOR_TEXT,
                 font=(FONT, 10, "bold"), anchor=tk.W).pack(anchor=tk.W)
        tk.Label(col, text=desc, bg=COLOR_PANEL, fg=COLOR_TEXT_3,
                 font=(FONT, 8), anchor=tk.W).pack(anchor=tk.W)

    def _slider_row(self, parent, text, var, lo, hi, command,
                    resolution=None, value_text=""):
        """「标签 + 滑杆 + 数值」一行（设计稿样式）

        数值标签必须在本函数内以 row 为父容器创建。
        之前版本在外部以 inner 为父创建、却在这里 pack —— pack 只能由
        其 master 管理，结果数值全部错位到面板右缘、行内无线。
        """
        row = tk.Frame(parent, bg=COLOR_PANEL)
        row.pack(fill=tk.X, pady=(8, 0))

        value_label = tk.Label(row, text=value_text, bg=COLOR_PANEL,
                               fg=COLOR_TEXT, font=(MONO, 9), width=8,
                               anchor=tk.E)
        value_label.pack(side=tk.RIGHT)

        tk.Label(row, text=text, bg=COLOR_PANEL, fg=COLOR_TEXT_2,
                 font=(FONT, 9), width=8, anchor=tk.W).pack(side=tk.LEFT)

        slider = CanvasSlider(row, var, lo, hi, command=command,
                              resolution=resolution)
        slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))
        return slider, value_label

    def _build_volume_panel(self, parent):
        """音量面板：麦克风增益 / 扬声器音量

        设计稿没有这一块，但两个滑杆是既有功能（且用户此前明确要求置底），
        故按同一套设计语言收紧保留。
        """
        panel = RoundedPanel(parent, radius=14, pad=14)
        panel.pack(fill=tk.X, pady=(12, 0))
        inner = panel.inner

        head = tk.Frame(inner, bg=COLOR_PANEL)
        head.pack(fill=tk.X)
        tk.Label(head, text="🎚 音量", bg=COLOR_PANEL, fg=COLOR_TEXT,
                 font=(FONT, 10, "bold")).pack(side=tk.LEFT)

        # 麦克风增益（默认 100%：蓝牙麦克风灵敏度通常已足够，
        # 额外放大在外放场景会把系统推向啸叫临界点）
        self.input_gain_var = tk.DoubleVar(value=100.0)
        self.input_gain_slider, self.input_gain_label = self._slider_row(
            inner, "麦克风增益", self.input_gain_var, 50, 500,
            self._on_input_gain_change,
            resolution=5.0, value_text="100%")

        # 扬声器音量
        self.volume_var = tk.DoubleVar(value=100.0)
        self.volume_slider, self.volume_label = self._slider_row(
            inner, "扬声器音量", self.volume_var, 0, 200,
            self._on_volume_change,
            resolution=5.0, value_text="100%")

        tk.Label(inner, text="增益 80~120% 为宜，过高易引发啸叫；要更响请调「扬声器音量」",
                 bg=COLOR_PANEL, fg=COLOR_TEXT_3,
                 font=(FONT, 8), anchor=tk.W).pack(fill=tk.X, pady=(8, 0))

    def _build_footer(self):
        """固定底栏：左滚动提示 / 右版本"""
        footer = tk.Frame(self.root, bg=COLOR_PAGE)
        footer.pack(fill=tk.X, side=tk.BOTTOM, padx=24, pady=(2, 8))

        tk.Label(footer, text="🖱 滚动 · 滚轮 / PgUp / PgDn / Home / End",
                 bg=COLOR_PAGE, fg=COLOR_TEXT_3,
                 font=(FONT, 8), anchor=tk.W).pack(side=tk.LEFT)
        tk.Label(footer, text="WASAPI · v2.0",
                 bg=COLOR_PAGE, fg=COLOR_TEXT_3,
                 font=(FONT, 8), anchor=tk.E).pack(side=tk.RIGHT)

    def _build_scroll_area(self):
        """可滚动内容区（Canvas + 内嵌 Frame 的经典做法）"""
        outer = tk.Frame(self.root, bg=COLOR_PAGE)
        outer.pack(fill=tk.BOTH, expand=True, side=tk.TOP, padx=24)

        self._canvas = tk.Canvas(outer, bg=COLOR_PAGE,
                                 highlightthickness=0, bd=0)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL,
                                        command=self._canvas.yview)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._scrollbar_visible = True
        self._canvas.configure(yscrollcommand=self._on_scrollbar_set)

        self._scroll_content = tk.Frame(self._canvas, bg=COLOR_PAGE)
        self._content_window = self._canvas.create_window(
            (0, 0), window=self._scroll_content, anchor="nw")

        self._scroll_content.bind("<Configure>", self._on_content_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._bind_mousewheel()

    def _on_scrollbar_set(self, first, last):
        """滚动条位置更新；内容未超出时隐藏滚动条"""
        self._scrollbar.set(first, last)
        need = not (float(first) <= 0.0 and float(last) >= 1.0)
        if need and not self._scrollbar_visible:
            self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            self._scrollbar_visible = True
        elif not need and self._scrollbar_visible:
            self._scrollbar.pack_forget()
            self._scrollbar_visible = False

    def _on_content_configure(self, event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfigure(self._content_window, width=event.width)

    def _bind_mousewheel(self):
        """鼠标滚轮 / 触控板 / 键盘翻页"""
        def _on_wheel(event):
            bbox = self._canvas.bbox("all")
            if not bbox or bbox[3] <= self._canvas.winfo_height():
                return
            if event.delta:
                self._canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        self.root.bind_all("<MouseWheel>", _on_wheel)
        self.root.bind_all("<Button-4>",
                           lambda e: self._canvas.yview_scroll(-1, "units"))
        self.root.bind_all("<Button-5>",
                           lambda e: self._canvas.yview_scroll(1, "units"))
        self.root.bind_all("<Prior>",
                           lambda e: self._canvas.yview_scroll(-1, "pages"))
        self.root.bind_all("<Next>",
                           lambda e: self._canvas.yview_scroll(1, "pages"))
        self.root.bind_all("<Home>",
                           lambda e: self._canvas.yview_moveto(0.0))
        self.root.bind_all("<End>",
                           lambda e: self._canvas.yview_moveto(1.0))

    def _sync_control_states(self):
        """根据各开关初始值同步滑块禁用态，并把 UI 初值推送到引擎"""
        self.freq_shift_slider.set_enabled(self.freq_shift_var.get())
        self.gate_threshold_slider.set_enabled(self.noise_gate_var.get())
        self.notch_slider.set_enabled(self.notch_var.get())
        for t in (self.follow_toggle,):
            t.refresh()

        self.engine.set_input_gain(self.input_gain_var.get() / 100.0)
        self.engine.set_volume(self.volume_var.get() / 100.0)
        self.engine.set_freq_shift_enabled(self.freq_shift_var.get())
        self.engine.set_freq_shift_amount(self.freq_shift_amount_var.get())
        self.engine.set_noise_gate_enabled(self.noise_gate_var.get())
        self.engine.set_notch_enabled(self.notch_var.get())
        self.engine.set_notch_attenuation_db(self.notch_attenuation_var.get())

    # ---------- 设备管理 ----------

    def _refresh_devices(self):
        """刷新设备列表（用户点击：反映系统当前状态）"""
        return self._refresh_devices_impl(user_action=True)

    def _refresh_devices_impl(self, user_action: bool = True):
        try:
            prev_in = self.input_combo.get()
            prev_out = self.output_combo.get()

            # 【关键】重载 PortAudio，才能发现「程序启动后才连上」的蓝牙耳机。
            # 引擎运行中不能重载（会打断音频流），退化为仅重新查询。
            reloaded = False
            if not self.engine.is_running:
                reloaded = DeviceManager.reload_portaudio()

            all_input = DeviceManager.get_input_devices(sort_by_priority=True)
            all_output = DeviceManager.get_output_devices(sort_by_priority=True)

            self._input_devices = DeviceManager.filter_usable_devices(
                all_input, is_input=True)
            self._output_devices = DeviceManager.filter_usable_devices(
                all_output, is_input=False)

            if not self._input_devices:
                self._input_devices = all_input
            if not self._output_devices:
                self._output_devices = all_output

            # 输入下拉框（完整名 + 类型标签）
            input_names = []
            for d in self._input_devices:
                name = d.full_display_name
                if d.is_bluetooth:
                    name += "  🎧"
                elif d.device_type == "virtual":
                    name += "  💻"
                input_names.append(name)
            self.input_combo["values"] = input_names

            # 输出下拉框
            output_names = []
            for d in self._output_devices:
                name = d.full_display_name
                if d.is_bluetooth:
                    name += "  🎧"
                elif d.is_display_audio:
                    name += "  📺"
                elif d.device_type == "virtual":
                    name += "  💻"
                output_names.append(name)
            self.output_combo["values"] = output_names

            self._auto_select_devices(user_action=user_action)

            if user_action:
                new_in = self.input_combo.get()
                new_out = self.output_combo.get()
                changes = []
                if new_in != prev_in:
                    changes.append(f"输入 → {new_in}")
                if new_out != prev_out:
                    changes.append(f"输出 → {new_out}")
                if not changes:
                    changes.append("当前选择未变")

                bt_in = [d for d in self._input_devices if d.is_bluetooth]
                bt_out = [d for d in self._output_devices if d.is_bluetooth]
                scan_txt = (
                    f"扫描到 {len(self._input_devices)} 个输入 / "
                    f"{len(self._output_devices)} 个输出设备\n"
                    f"其中蓝牙：输入 {len(bt_in)} 个，输出 {len(bt_out)} 个"
                    + ("" if (bt_in or bt_out) else "　⚠️ 未发现蓝牙设备"))

                try:
                    sd_in = DeviceManager.find_system_default_input()
                    sd_out = DeviceManager.find_system_default_output()
                    default_txt = (
                        f"\n\nWindows 当前默认：\n"
                        f"  输入：{sd_in.display_name if sd_in else '未知'}\n"
                        f"  输出：{sd_out.display_name if sd_out else '未知'}")
                except Exception:
                    default_txt = ""

                hint = ""
                if not reloaded and self.engine.is_running:
                    hint = ("\n\n注意：路由运行中，本次未重载音频驱动。\n"
                            "如需发现新连接的蓝牙设备，请先停止路由再刷新。")

                messagebox.showinfo(
                    "刷新完成",
                    "已重新扫描系统音频设备。\n\n"
                    + scan_txt + "\n\n变化：\n" + "\n".join(changes)
                    + default_txt + hint)

        except Exception as e:
            messagebox.showerror("Error", f"Failed to refresh devices: {str(e)}")

    def _auto_select_devices(self, user_action: bool = False):
        """智能选择默认设备

        优先级：
          0) 勾选「跟随系统默认设备」→ 直接用 Windows 默认输入/输出
          1) 输入：刷新时优先系统默认，其次蓝牙麦克风；最后非虚拟物理麦
          2) 输出：启动恢复偏好 → 刷新跟系统默认 → HDMI → 内置扬声器
        """
        # ---- 0) 跟随系统默认 ----
        if getattr(self, "follow_default_var", None) and self.follow_default_var.get():
            if self._apply_system_default(fallback=True):
                return

        # ---- 1) 输入设备 ----
        in_candidates = []
        if user_action:
            sd_in = DeviceManager.find_system_default_input()
            if sd_in:
                in_candidates.append(sd_in.index)
        bt_input = DeviceManager.find_bluetooth_input()
        if bt_input:
            in_candidates.append(bt_input.index)

        picked = False
        for want in in_candidates:
            for i, d in enumerate(self._input_devices):
                if d.index == want:
                    self.input_combo.current(i)
                    self._on_input_selected(None)
                    picked = True
                    break
            if picked:
                break

        if not picked:
            for i, d in enumerate(self._input_devices):
                if d.device_type != "virtual":
                    self.input_combo.current(i)
                    self._on_input_selected(None)
                    picked = True
                    break
        if not picked and self._input_devices:
            self.input_combo.current(0)
            self._on_input_selected(None)

        # ---- 2) 输出设备 ----
        if not user_action:
            pref = self._load_preferred_output()
            if pref:
                pref_name, pref_api = pref
                for i, d in enumerate(self._output_devices):
                    if d.name == pref_name and (not pref_api or d.hostapi_name == pref_api):
                        self.output_combo.current(i)
                        self._on_output_selected(None)
                        return
        else:
            sd_out = DeviceManager.find_system_default_output()
            if sd_out:
                for i, d in enumerate(self._output_devices):
                    if d.index == sd_out.index:
                        self.output_combo.current(i)
                        self._on_output_selected(None)
                        return

        hdmi_out = DeviceManager.find_hdmi_output()
        if hdmi_out:
            for i, d in enumerate(self._output_devices):
                if d.index == hdmi_out.index:
                    self.output_combo.current(i)
                    self._on_output_selected(None)
                    return
        builtin_out = DeviceManager.find_builtin_output()
        if builtin_out:
            for i, d in enumerate(self._output_devices):
                if d.index == builtin_out.index:
                    self.output_combo.current(i)
                    self._on_output_selected(None)
                    return
        if self._output_devices:
            self.output_combo.current(0)
            self._on_output_selected(None)

    def _on_input_selected(self, event):
        """输入设备选中：更新设备名 / 徽章 / 元信息行"""
        idx = self.input_combo.current()
        if idx < 0 or idx >= len(self._input_devices):
            return
        device = self._input_devices[idx]
        api_name = device.hostapi_name or f"API {device.hostapi}"

        self.input_name_label.config(text=device.display_name)
        if device.is_system_default:
            self.input_default_badge.pack(side=tk.LEFT, padx=(8, 0))
        else:
            self.input_default_badge.pack_forget()

        if not self.engine.is_running:
            self.engine.set_input_device(device)
            # 显示引擎实际选定的采样率（可能与标称不同）
            try:
                picked = self.engine._input_samplerate
            except Exception:
                picked = int(device.default_samplerate)
        else:
            picked = int(device.default_samplerate)

        self.input_meta_label.config(
            text=f"CH {device.max_input_channels} · {picked / 1000:.1f} kHz · {api_name}")

    def _on_output_selected(self, event):
        """输出设备选中：更新设备名 / 徽章 / 元信息 / 绿色横幅"""
        idx = self.output_combo.current()
        if idx < 0 or idx >= len(self._output_devices):
            return
        device = self._output_devices[idx]
        api_name = device.hostapi_name or f"API {device.hostapi}"

        self.output_name_label.config(text=device.display_name)
        if device.is_system_default:
            self.output_default_badge.pack(side=tk.LEFT, padx=(8, 0))
        else:
            self.output_default_badge.pack_forget()

        if not self.engine.is_running:
            self.engine.set_output_device(device)
            try:
                picked = self.engine._output_samplerate
            except Exception:
                picked = int(device.default_samplerate)
        else:
            picked = int(device.default_samplerate)

        self.output_meta_label.config(
            text=f"CH {device.max_output_channels} · {picked / 1000:.1f} kHz · {api_name}")

        # 绿色横幅（对齐设计稿：只放设备名，类型由下拉框里的 🎧/📺 标签表达）
        short_name = device.display_name
        if len(short_name) > 24:
            short_name = short_name[:22] + "…"
        self.output_target_label.config(
            text=f"🔊 声音将从：{short_name} 播放")

        # 记住用户选择（跟随系统默认模式下不记，避免固化系统默认）
        if not (getattr(self, "follow_default_var", None)
                and self.follow_default_var.get()):
            self._save_preferred_output(device)
        if not self.engine.is_running:
            self.engine.set_output_device(device)

    # ---------- 设备偏好记忆 ----------

    def _pref_file(self) -> str:
        """偏好文件路径

        源码运行 -> 项目根/.device_prefs.json
        打包运行 -> %APPDATA%\\BTSPK\\.device_prefs.json
        """
        return data_file(".device_prefs.json")

    def _load_prefs(self) -> dict:
        try:
            if os.path.exists(self._pref_file()):
                with open(self._pref_file(), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _update_prefs(self, **kwargs):
        try:
            prefs = self._load_prefs()
            prefs.update(kwargs)
            with open(self._pref_file(), "w", encoding="utf-8") as f:
                json.dump(prefs, f, ensure_ascii=False, indent=2)
            self._prefs = prefs
        except Exception:
            pass  # 偏好保存失败不影响主流程

    def _save_preferred_output(self, device):
        self._update_prefs(output_name=device.name,
                           output_hostapi=device.hostapi_name)

    def _load_preferred_output(self):
        name = self._prefs.get("output_name")
        if name:
            return name, self._prefs.get("output_hostapi", "")
        return None

    def _on_follow_default_toggle(self):
        follow = bool(self.follow_default_var.get())
        self._update_prefs(follow_system_default=follow)
        if follow:
            self._apply_system_default(fallback=False)

    def _apply_system_default(self, fallback: bool = True) -> bool:
        """按 Windows 默认设备重选输入/输出，返回是否选中了至少一项"""
        ok = False

        d_in = DeviceManager.find_system_default_input()
        if d_in:
            for i, d in enumerate(self._input_devices):
                if d.index == d_in.index:
                    self.input_combo.current(i)
                    self._on_input_selected(None)
                    ok = True
                    break
        elif fallback and self._input_devices:
            self.input_combo.current(0)
            self._on_input_selected(None)

        d_out = DeviceManager.find_system_default_output()
        if d_out:
            for i, d in enumerate(self._output_devices):
                if d.index == d_out.index:
                    self.output_combo.current(i)
                    self._on_output_selected(None)
                    ok = True
                    break
        elif fallback and self._output_devices:
            self.output_combo.current(0)
            self._on_output_selected(None)

        return ok

    # ---------- 音量/增益控制 ----------

    def _on_input_gain_change(self, value):
        gain = float(value)
        self.input_gain_label.config(text=f"{int(gain)}%")
        self.engine.set_input_gain(gain / 100.0)

    def _on_volume_change(self, value):
        vol = float(value)
        self.volume_label.config(text=f"{int(vol)}%")
        self.engine.set_volume(vol / 100.0)

    # ---------- 防啸叫控制 ----------

    def _on_noise_gate_toggle(self):
        enabled = self.noise_gate_var.get()
        self.engine.set_noise_gate_enabled(enabled)
        self.gate_threshold_slider.set_enabled(enabled)

    def _on_gate_threshold_change(self, value):
        """灵敏度滑块（UI值 0.5~10.0 对应实际阈值 0.005~0.1）"""
        val = float(value)
        self.engine.set_noise_gate_threshold(val / 100.0)
        if val < 1.0:
            label_text = "很高"
        elif val < 2.0:
            label_text = "高"
        elif val < 4.0:
            label_text = "中"
        elif val < 7.0:
            label_text = "低"
        else:
            label_text = "很低"
        self.gate_threshold_label.config(text=label_text)

    def _on_freq_shift_toggle(self):
        enabled = self.freq_shift_var.get()
        self.engine.set_freq_shift_enabled(enabled)
        self.freq_shift_slider.set_enabled(enabled)

    def _on_freq_shift_amount_change(self, value):
        val = float(value)
        self.freq_shift_amount_label.config(text=f"{val:.1f} Hz")
        self.engine.set_freq_shift_amount(val)

    def _on_notch_toggle(self):
        enabled = self.notch_var.get()
        self.engine.set_notch_enabled(enabled)
        self.notch_slider.set_enabled(enabled)

    def _on_notch_attenuation_change(self, value):
        val = float(value)
        self.notch_attenuation_label.config(text=f"{int(val)} dB")
        self.engine.set_notch_attenuation_db(val)

    # ---------- 路由控制 ----------

    def _toggle_router(self):
        if self.engine.is_running:
            self._stop_router()
        else:
            self._start_router()

    def _start_router(self):
        """启动路由（PortAudio 要求流在创建它的线程中启动，故在 UI 线程执行）"""
        input_idx = self.input_combo.current()
        output_idx = self.output_combo.current()

        if input_idx < 0:
            messagebox.showwarning("提示", "请选择输入设备（麦克风）")
            return
        if output_idx < 0:
            messagebox.showwarning("提示", "请选择输出设备（扬声器）")
            return

        self.engine.set_input_device(self._input_devices[input_idx])
        self.engine.set_output_device(self._output_devices[output_idx])

        self.input_combo.config(state="disabled")
        self.output_combo.config(state="disabled")

        self.start_button.config(text="■  停止路由", bg=COLOR_RED,
                                 activebackground="#f87171")
        self._set_status_pill(running=True)

        import traceback
        try:
            self.engine.start()
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            print(f"\n[START ERROR] {error_msg}")
            traceback.print_exc()
            self._on_start_error(error_msg)

    def _on_start_error(self, error_msg, detail=""):
        messagebox.showerror("Start Failed", error_msg)
        self._reset_ui_to_stopped()

    def _stop_router(self):
        threading.Thread(target=self._do_stop, daemon=True).start()

    def _do_stop(self):
        try:
            self.engine.stop()
        finally:
            self.root.after(0, self._reset_ui_to_stopped)

    def _set_status_pill(self, running: bool):
        """右上角状态胶囊样式"""
        if running:
            self.status_label.config(text="● 运行中", fg=COLOR_GREEN,
                                     bg=COLOR_GREEN_BG)
        else:
            self.status_label.config(text="● 未运行", fg=COLOR_TEXT_3,
                                     bg=COLOR_PILL)

    def _reset_ui_to_stopped(self):
        """重置 UI 到停止状态"""
        self.input_combo.config(state="readonly")
        self.output_combo.config(state="readonly")
        self.start_button.config(text="▶  开始路由", bg=COLOR_ACCENT,
                                 activebackground=COLOR_ACCENT_HI)
        self._set_status_pill(running=False)
        self._ensure_meter_state()
        self._meter_display = {"in": 0.0, "out": 0.0}
        self._meter_peak = {"in": 0.0, "out": 0.0}
        self._meter_peak_hold = {"in": 0, "out": 0}
        self._draw_meter(self.input_meter_canvas, 0.0)
        self._draw_meter(self.output_meter_canvas, 0.0)
        self.input_meter_value.config(text="-∞ dB")
        self.output_meter_value.config(text="-∞ dB")
        self.latency_label.config(text="延迟 -- ms")
        self.buffer_label.config(text="缓冲 --")
        self.xrun_label.config(text="丢失 0", fg=COLOR_TEXT_3)
        self.notch_freq_label.config(text="当前陷波: 无", fg=COLOR_TEXT_3)
        self.gate_status_label.config(text="抑制门 · 关闭", fg=COLOR_TEXT_3,
                                      bg=COLOR_PILL)

    # ---------- 引擎回调 ----------

    def _on_engine_status(self, status: str):
        """引擎状态变化回调（可能在非UI线程）"""
        self.root.after(0, lambda: self._update_status_text(status))

    def _on_engine_error(self, error: str):
        self.root.after(0, lambda: messagebox.showerror("错误", error))

    def _update_status_text(self, status: str):
        if status == "started":
            self._set_status_pill(running=True)
        elif status == "stopped":
            self._set_status_pill(running=False)

    # ---------- UI 刷新 ----------

    def _update_ui(self):
        """定时刷新 UI 显示（电平、状态等，约 30 FPS）"""
        if not self._ui_running:
            return

        if self.engine.is_running:
            in_level = self.engine.input_level
            out_level = self.engine.output_level
            in_ratio = self._level_to_ratio(in_level)
            out_ratio = self._level_to_ratio(out_level)

            self._meter_smooth("in", in_ratio)
            self._meter_smooth("out", out_ratio)

            self._draw_meter(self.input_meter_canvas,
                             self._meter_display["in"],
                             peak=self._meter_peak["in"])
            self._draw_meter(self.output_meter_canvas,
                             self._meter_display["out"],
                             peak=self._meter_peak["out"])

            in_db = self._level_to_db(in_level)
            out_db = self._level_to_db(out_level)
            self.input_meter_value.config(text=f"{in_db} dB")
            self.output_meter_value.config(text=f"{out_db} dB")

            latency = self.engine.estimated_latency_ms
            self.latency_label.config(text=f"延迟 {latency:.0f} ms")

            buf_pct = self.engine.buffer_occupancy * 100
            self.buffer_label.config(text=f"缓冲 {buf_pct:.0f}%")

            xruns = self.engine.xruns
            xrun_color = COLOR_ORANGE if xruns > 0 else COLOR_TEXT_3
            self.xrun_label.config(text=f"丢失 {xruns}", fg=xrun_color)

            # 抑制门状态胶囊
            gate_gain = self.engine.gate_gain
            if self.noise_gate_var.get():
                if gate_gain > 0.8:
                    self.gate_status_label.config(text="抑制门 · 打开",
                                                  fg=COLOR_GREEN,
                                                  bg=COLOR_GREEN_BG)
                elif gate_gain > 0.2:
                    self.gate_status_label.config(text="抑制门 · 过渡",
                                                  fg=COLOR_ORANGE,
                                                  bg=COLOR_PILL)
                else:
                    self.gate_status_label.config(text="抑制门 · 关闭",
                                                  fg=COLOR_TEXT_3,
                                                  bg=COLOR_PILL)
            else:
                self.gate_status_label.config(text="抑制门 · 关闭",
                                              fg=COLOR_TEXT_3, bg=COLOR_PILL)

            # 当前陷波频率
            self._update_notch_freq_display()

        self.root.after(30, self._update_ui)

    def _update_notch_freq_display(self):
        """刷新当前激活的陷波频率（说话时应始终为「无」）"""
        try:
            freqs = self.engine.get_active_notch_freqs()
        except Exception:
            freqs = []

        if not self.notch_var.get():
            self.notch_freq_label.config(text="当前陷波: (陷波已停用)",
                                         fg=COLOR_TEXT_3)
            return

        if not freqs:
            self.notch_freq_label.config(text="当前陷波: 无  ✓ 未检测到啸叫",
                                         fg=COLOR_TEXT_3)
            return

        text = "  ".join(f"{f:.0f}Hz" for f in freqs)
        self.notch_freq_label.config(
            text=f"当前陷波 ({len(freqs)} 个): {text}", fg=COLOR_ORANGE)

    # ---------- 电平表：dB 映射 / 弹道 / 峰值保持 ----------

    def _ensure_meter_state(self):
        if not hasattr(self, "_meter_display"):
            self._meter_display = {"in": 0.0, "out": 0.0}
            self._meter_peak = {"in": 0.0, "out": 0.0}
            self._meter_peak_hold = {"in": 0, "out": 0}

    def _level_to_ratio(self, level: float) -> float:
        """线性 RMS → 0.0~1.0 的显示比例（dB 映射）"""
        if level <= 1e-7:
            return 0.0
        db = 20.0 * math.log10(level)
        span = self.METER_CEIL_DB - self.METER_FLOOR_DB
        ratio = (db - self.METER_FLOOR_DB) / span
        return max(0.0, min(1.0, ratio))

    def _meter_smooth(self, key: str, target: float):
        """快起慢落平滑 + 峰值保持"""
        self._ensure_meter_state()
        cur = self._meter_display[key]

        if target >= cur:
            cur = cur + (target - cur) * self.METER_ATTACK
        else:
            cur = cur + (target - cur) * self.METER_RELEASE
            if cur < 0.001:
                cur = 0.0
        self._meter_display[key] = cur

        if cur >= self._meter_peak[key]:
            self._meter_peak[key] = cur
            self._meter_peak_hold[key] = 12  # 约 0.36s
        else:
            if self._meter_peak_hold[key] > 0:
                self._meter_peak_hold[key] -= 1
            else:
                self._meter_peak[key] = max(
                    cur, self._meter_peak[key] - self.METER_PEAK_DECAY)

    @staticmethod
    def _meter_color(t: float) -> str:
        """电平条渐变色：绿(低) → 黄 → 红(高)"""
        if t < 0.6:
            return _lerp_color(COLOR_METER_GREEN, COLOR_METER_YELLOW, t / 0.6)
        return _lerp_color(COLOR_METER_YELLOW, COLOR_RED, (t - 0.6) / 0.4)

    def _draw_meter(self, canvas: tk.Canvas, ratio: float, peak: float = 0.0):
        """绘制圆角电平条：暗色轨道 + 绿→黄→红渐变填充 + 峰值白线"""
        canvas.update_idletasks()
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 1:
            return

        canvas.delete("all")
        mid = height / 2
        pad = 2
        span = width - 2 * pad

        # 轨道（圆角胶囊）
        canvas.create_line(pad, mid, width - pad, mid, width=height - 2,
                           capstyle="round", fill=COLOR_TRACK)

        # 渐变填充（分段画线，绿色→黄色→红色）
        ratio = min(max(ratio, 0.0), 1.0)
        fill_w = ratio * span
        if fill_w > 1:
            segments = 28
            seg_w = fill_w / segments
            for i in range(segments):
                t = (i + 0.5) / segments
                x0 = pad + i * seg_w
                x1 = pad + (i + 1) * seg_w
                canvas.create_line(x0, mid, max(x1, x0 + 0.5), mid,
                                   width=height - 2, capstyle="butt",
                                   fill=self._meter_color(t))
            # 末端补一段圆头，收口圆润
            canvas.create_line(pad + fill_w, mid, pad + fill_w, mid,
                               width=height - 2, capstyle="round",
                               fill=self._meter_color(ratio))

        # 峰值保持刻线
        if peak > 0.01:
            px = pad + min(peak, 1.0) * span
            px = max(pad, min(width - pad, px))
            canvas.create_line(px, mid, px, mid, width=2,
                               fill="#dfe6ee")

    def _level_to_db(self, level: float) -> str:
        if level < 0.0001:
            return "-∞"
        db = 20 * math.log10(level)
        return f"{db:+.1f}"

    # ---------- 窗口事件 ----------

    def _on_close(self):
        self._ui_running = False
        try:
            self.engine.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()
