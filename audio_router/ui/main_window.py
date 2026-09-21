"""
主窗口 - 音频路由控制面板
使用 Tkinter 构建专业的音频路由界面
"""
import tkinter as tk
from tkinter import ttk, messagebox
import json
import os
import threading
import time
from typing import List, Optional

from ..models.audio_device import AudioDeviceInfo
from ..services.device_manager import DeviceManager
from ..services.audio_router import AudioRouterEngine


class MainWindow:
    """音频路由主窗口"""

    # 颜色主题
    COLOR_BG = "#1e1e2e"
    COLOR_PANEL = "#2a2a3e"
    COLOR_ACCENT = "#4f8ff7"
    COLOR_ACCENT_HOVER = "#6ba0ff"
    COLOR_SUCCESS = "#22c55e"
    COLOR_DANGER = "#ef4444"
    COLOR_WARNING = "#f59e0b"
    COLOR_TEXT = "#e4e4e7"
    COLOR_TEXT_SECONDARY = "#a1a1aa"
    COLOR_METER_BG = "#3f3f5a"
    COLOR_METER_FILL = "#4f8ff7"
    COLOR_METER_PEAK = "#22c55e"
    COLOR_BORDER = "#3f3f5a"

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
        self.root.configure(bg=self.COLOR_BG)

        # 窗口尺寸：按屏幕可用高度自适应
        # 界面内容总高约 1020px，而 1080p 屏幕减去标题栏/任务栏后放不下
        # → 采用「固定顶部（标题+开始按钮）+ 中间可滚动 + 底部提示」布局。
        # 这里让窗口尽量高但不超出屏幕，保证中间滚动区有足够可视高度。
        scr_w = self.root.winfo_screenwidth()
        scr_h = self.root.winfo_screenheight()

        # 估算屏幕可用高度：减去标题栏(~40) 与任务栏(~60) 的余量。
        # Tk 没有跨平台获取工作区的接口，用固定余量是业界常规做法。
        avail_h = scr_h - 110
        win_h = int(min(900, max(520, avail_h)))
        win_w = int(min(660, max(560, scr_w - 240)))

        # 水平居中、垂直略偏上（视觉更自然）
        pos_x = max(0, (scr_w - win_w) // 2)
        pos_y = max(0, (avail_h - win_h) // 2 + 20)
        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        # 最小尺寸放宽，确保小屏也能把窗口缩到可用大小
        self.root.minsize(520, 400)
        self.root.configure(bg=self.COLOR_BG)

        # 设置样式
        self._setup_styles()

        # 提示文字的初始换行宽度与注册表（供 _on_window_resize 统一调整）
        self._hint_wrap = max(320, win_w - 110)
        self._hint_labels = []

        # 构建界面
        self._build_ui()

        # 初始化设备列表（启动时允许恢复上次偏好，不弹提示框）
        self._refresh_devices_impl(user_action=False)

        # 启动 UI 刷新定时器
        self._update_ui()

        # 初始化与「开关」联动的控件状态
        # （移频/噪声门/侧链默认关闭 → 对应滑块起步即为禁用态）
        self._sync_control_states()

        # 窗口关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 窗口尺寸变化时调整提示文字换行宽度（减少需要滚动的距离）
        self.root.bind('<Configure>', self._on_window_resize)

    def _sync_control_states(self):
        """根据各开关的初始值同步关联滑块的启用状态

        避免出现「开关是关的、滑块却可拖动」的误导状态。
        """
        self.freq_shift_slider.config(
            state='normal' if self.freq_shift_var.get() else 'disabled')
        self.noise_gate_check.select() if self.noise_gate_var.get() \
            else self.noise_gate_check.deselect()
        self.gate_threshold_slider.config(
            state='normal' if self.noise_gate_var.get() else 'disabled')
        self.notch_slider.config(
            state='normal' if self.notch_var.get() else 'disabled')
        self.sidechain_slider.config(
            state='normal' if self.sidechain_var.get() else 'disabled')

        # 把 UI 初值推送到引擎，确保「界面显示」与「引擎实际值」一致
        self.engine.set_input_gain(self.input_gain_var.get() / 100.0)
        self.engine.set_volume(self.volume_var.get() / 100.0)
        self.engine.set_freq_shift_enabled(self.freq_shift_var.get())
        self.engine.set_freq_shift_amount(self.freq_shift_amount_var.get())
        self.engine.set_noise_gate_enabled(self.noise_gate_var.get())
        self.engine.set_sidechain_enabled(self.sidechain_var.get())
        self.engine.set_notch_enabled(self.notch_var.get())
        self.engine.set_notch_attenuation_db(self.notch_attenuation_var.get())

    # ---------- 样式设置 ----------

    def _setup_styles(self):
        """设置 ttk 样式"""
        style = ttk.Style()
        style.theme_use('clam')

        # 全局样式
        style.configure('TFrame', background=self.COLOR_BG)
        style.configure('Panel.TFrame', background=self.COLOR_PANEL)

        # 标签
        style.configure('TLabel', background=self.COLOR_BG, foreground=self.COLOR_TEXT, font=('Segoe UI', 10))
        style.configure('Panel.TLabel', background=self.COLOR_PANEL, foreground=self.COLOR_TEXT, font=('Segoe UI', 10))
        style.configure('Title.TLabel', background=self.COLOR_BG, foreground=self.COLOR_TEXT, font=('Segoe UI', 16, 'bold'))
        style.configure('Subtitle.TLabel', background=self.COLOR_BG, foreground=self.COLOR_TEXT_SECONDARY, font=('Segoe UI', 9))
        style.configure('PanelTitle.TLabel', background=self.COLOR_PANEL, foreground=self.COLOR_ACCENT, font=('Segoe UI', 11, 'bold'))
        style.configure('Status.TLabel', background=self.COLOR_PANEL, foreground=self.COLOR_TEXT_SECONDARY, font=('Segoe UI', 9))
        style.configure('Value.TLabel', background=self.COLOR_PANEL, foreground=self.COLOR_TEXT, font=('Segoe UI', 10, 'bold'))

        # 下拉框
        style.configure('TCombobox',
                        fieldbackground=self.COLOR_BG,
                        background=self.COLOR_BG,
                        foreground=self.COLOR_TEXT,
                        borderwidth=0,
                        arrowcolor=self.COLOR_ACCENT)
        style.map('TCombobox',
                  fieldbackground=[('readonly', self.COLOR_BG)],
                  foreground=[('readonly', self.COLOR_TEXT)])

        # 按钮
        style.configure('TButton',
                        background=self.COLOR_PANEL,
                        foreground=self.COLOR_TEXT,
                        borderwidth=0,
                        padding=(16, 8),
                        font=('Segoe UI', 10, 'bold'))
        style.map('TButton',
                  background=[('active', self.COLOR_ACCENT), ('pressed', self.COLOR_ACCENT_HOVER)])

        # 主按钮（启动/停止）
        style.configure('Primary.TButton',
                        background=self.COLOR_ACCENT,
                        foreground='white',
                        borderwidth=0,
                        padding=(20, 12),
                        font=('Segoe UI', 12, 'bold'))
        style.map('Primary.TButton',
                  background=[('active', self.COLOR_ACCENT_HOVER), ('pressed', '#3d7ae8')])

        # 停止按钮
        style.configure('Danger.TButton',
                        background=self.COLOR_DANGER,
                        foreground='white',
                        borderwidth=0,
                        padding=(20, 12),
                        font=('Segoe UI', 12, 'bold'))
        style.map('Danger.TButton',
                  background=[('active', '#f87171'), ('pressed', '#dc2626')])

        # 滑块
        style.configure('Horizontal.TScale',
                        background=self.COLOR_PANEL,
                        troughcolor=self.COLOR_METER_BG,
                        borderwidth=0)

        # 紧凑滑块：tk.Scale 自带约 40px 的轨道高度，
        # 在「防啸叫」这种一行一个滑块的密集区域会堆积出大量空白。
        # ttk.Scale 高度约 20px，配合同行数值标签能省下约一半竖向空间。
        style.configure('Compact.Horizontal.TScale',
                        background=self.COLOR_PANEL,
                        troughcolor=self.COLOR_METER_BG,
                        borderwidth=0,
                        lightcolor=self.COLOR_METER_FILL,
                        darkcolor=self.COLOR_METER_BG)
        style.map('Compact.Horizontal.TScale',
                  background=[('active', self.COLOR_PANEL)])

    # ---------- 界面构建 ----------

    def _compact_slider(self, parent, var, lo, hi, command, text,
                        label_w=8, value_w=8, resolution=None,
                        accent=None, fmt='%s'):
        """创建「说明标签 + 紧凑滑块 + 数值」的横向一行

        与旧写法相比：
          - tk.Scale 用 width=10 / sliderlength=14 压扁，高度约 40px → 约 18px
          - 三列宽度固定，多行滑块左右对齐，观感更整齐
          - 继续用 tk.Scale（而非 ttk.Scale），保证 troughcolor 暗色样式生效

        :param text:    左侧说明文字
        :param label_w: 左侧说明文字的字符宽度
        :param value_w: 右侧数值的字符宽度
        :param fmt:     数值格式化，如 '%d %%' / '%.1f Hz'
        :return: (滑块, 数值标签)
        """
        row = tk.Frame(parent, bg=self.COLOR_PANEL)
        row.pack(fill=tk.X, pady=(2, 0))

        # 数值标签先 pack 到右侧，保证它始终贴右不被滑块挤压
        value = tk.Label(row, text=fmt % var.get(),
                         bg=self.COLOR_PANEL,
                         fg=accent or self.COLOR_TEXT_SECONDARY,
                         font=('Consolas', 9), width=value_w, anchor=tk.E)
        value.pack(side=tk.RIGHT)

        # 左侧说明
        tk.Label(row, text=text, bg=self.COLOR_PANEL,
                 fg=self.COLOR_TEXT_SECONDARY,
                 font=('Segoe UI', 9), width=label_w,
                 anchor=tk.W).pack(side=tk.LEFT)

        kw = dict(from_=lo, to=hi, orient=tk.HORIZONTAL,
                  variable=var, command=command,
                  bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
                  troughcolor=self.COLOR_METER_BG,
                  activebackground=accent or '#f59e0b',
                  highlightthickness=0, bd=0,
                  showvalue=False,
                  width=10, sliderlength=14,
                  font=('Segoe UI', 9))
        if resolution is not None:
            kw['resolution'] = resolution   # tk.Scale 原生支持步进
        scale = tk.Scale(row, **kw)
        scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        return scale, value

    def _hint(self, parent, text):
        """创建一个「帮助提示」标签（统一样式，自动按窗口宽度换行）

        统一封装的好处：
          1. 所有提示文字样式一致
          2. wraplength 随窗口宽度自适应 —— 窗口变宽时文字少占行数，
             减少整个界面需要滚动的距离
        """
        lbl = tk.Label(parent, text=text,
                       bg=self.COLOR_PANEL, fg=self.COLOR_TEXT_SECONDARY,
                       font=('Segoe UI', 8),
                       anchor=tk.W, justify=tk.LEFT,
                       wraplength=self._hint_wrap)
        self._hint_labels.append(lbl)
        return lbl

    def _on_window_resize(self, event=None):
        """窗口尺寸变化时更新提示文字的换行宽度"""
        if event is not None and event.widget is not self.root:
            return
        # 内容区宽度 = 窗口宽度 - 左右内边距(20+20) - 滚动条/面板内边距估算
        new_wrap = max(320, self.root.winfo_width() - 110)
        if abs(new_wrap - self._hint_wrap) < 20:
            return                      # 变化很小，不做无谓刷新
        self._hint_wrap = new_wrap
        for lbl in self._hint_labels:
            try:
                lbl.configure(wraplength=new_wrap)
            except tk.TclError:
                pass

    def _build_ui(self):
        """构建主界面

        布局分三层（这是为了适配 1080p 等放不下全部内容的屏幕）：
            ┌────────────────────────────┐
            │ 固定：标题区 + 开始/停止按钮 │  ← 始终可见，不被裁切
            ├────────────────────────────┤
            │ 可滚动：设备/增益/防啸叫/   │  ← 中间内容超出时出现滚动条，
            │         电平/状态            │     支持鼠标滚轮与触控板
            ├────────────────────────────┤
            │ 固定：底部提示              │
            └────────────────────────────┘

        说明：把「开始路由」按钮移到顶部固定区，是因为它是最重要的操作 ——
        绝不能因为窗口不够高而看不到。
        """
        # ---- 固定顶部：标题 + 主控按钮 ----
        top = ttk.Frame(self.root, style='TFrame', padding=(20, 12, 20, 8))
        top.pack(fill=tk.X, side=tk.TOP)

        self._build_header(top)
        self._build_control_section(top)

        # ---- 可滚动中间区 ----
        # 注意 pack 顺序：先把随窗口伸缩的滚动区放进去（expand=True），
        # 再放底部提示。若顺序反了，底部提示会被挤到中间。
        self._build_scroll_area()

        # 所有内容区都挂到 self._scroll_content 上
        main = self._scroll_content

        # ---- 输入设备区 ----
        self._build_input_section(main)

        # ---- 输出设备区 ----
        self._build_output_section(main)

        # ---- 电平表区（紧跟设备区，方便边说话边看电平）----
        self._build_meter_section(main)

        # ---- 状态信息区 ----
        self._build_status_section(main)

        # ---- 防啸叫控制区 ----
        self._build_antifeedback_section(main)

        # ---- 音量控制区（麦克风增益 / 扬声器音量）置底 ----
        self._build_volume_section(main)

        # ---- 固定底部提示（必须在滚动区之后 pack）----
        self._build_footer_hint()

    def _build_footer_hint(self):
        """固定底部提示条：告知中间区域可滚动

        没有这个提示，用户看到内容被截断会以为「程序显示不全」，
        而不是「需要往下滚」。
        """
        self.footer_hint = tk.Label(
            self.root,
            text="↕  可滚动 · 滚轮 / PgUp / PgDn / Home / End",
            bg=self.COLOR_BG, fg=self.COLOR_TEXT_SECONDARY,
            font=('Segoe UI', 8), anchor=tk.CENTER)
        self.footer_hint.pack(fill=tk.X, side=tk.BOTTOM, pady=(2, 5))

    def _build_scroll_area(self):
        """构建可滚动的内容区

        实现要点（Tkinter 滚动区的经典做法）：
          1. Canvas 作为视口，内嵌一个 Frame 承载真实内容
          2. 通过 create_window 把内容 Frame 贴到 Canvas 上
          3. 内容尺寸变化时同步更新 Canvas 的 scrollregion
          4. Canvas 宽度变化时，把内容 Frame 宽度拉到与 Canvas 一致
             （否则内容会保持自身请求宽度，右侧出现空白）
        """
        # 外层容器：Canvas + 垂直滚动条
        outer = tk.Frame(self.root, bg=self.COLOR_BG)
        outer.pack(fill=tk.BOTH, expand=True, side=tk.TOP)

        self._canvas = tk.Canvas(outer, bg=self.COLOR_BG,
                                 highlightthickness=0, bd=0)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL,
                                        command=self._canvas.yview)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._scrollbar_visible = True

        self._canvas.configure(yscrollcommand=self._on_scrollbar_set)

        # 内容 Frame（所有区块的父容器）
        self._scroll_content = ttk.Frame(self._canvas, style='TFrame',
                                         padding=(20, 4, 20, 16))
        self._content_window = self._canvas.create_window(
            (0, 0), window=self._scroll_content, anchor='nw')

        # 内容尺寸变化 → 更新滚动范围
        self._scroll_content.bind('<Configure>', self._on_content_configure)
        # Canvas 尺寸变化 → 内容宽度跟随
        self._canvas.bind('<Configure>', self._on_canvas_configure)

        # 鼠标滚轮绑定（Windows / macOS）
        self._bind_mousewheel()

    def _on_scrollbar_set(self, first, last):
        """滚动条位置更新；内容未超出时隐藏滚动条

        用 pack 的副作用做显隐，不能 pack_forget 后重新 pack ——
        那样会把滚动条排到 Canvas 左边（破坏左右布局）。
        正确做法：保持 pack 顺序，只用 pack(side=RIGHT) 重新声明是不可靠的，
        因此这里改为在 _on_content_configure 里统一控制显隐。
        """
        self._scrollbar.set(first, last)
        need = not (float(first) <= 0.0 and float(last) >= 1.0)
        if need and not self._scrollbar_visible:
            # 重新 pack 时必须显式保证在 Canvas 右侧
            self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            self._scrollbar_visible = True
        elif not need and self._scrollbar_visible:
            self._scrollbar.pack_forget()
            self._scrollbar_visible = False

    def _on_content_configure(self, event=None):
        """内容尺寸变化时更新 Canvas 的可滚动范围"""
        self._canvas.configure(scrollregion=self._canvas.bbox('all'))

    def _on_canvas_configure(self, event):
        """Canvas 宽度变化时，让内容 Frame 宽度与之一致"""
        self._canvas.itemconfigure(self._content_window, width=event.width)

    def _bind_mousewheel(self):
        """绑定鼠标滚轮

        注意：Tkinter 在 Windows 用 <MouseWheel>（event.delta 为 ±120 的倍数），
        在 Linux 用 <Button-4>/<Button-5>，这里都做兼容。
        另外只在指针位于滚动区内时才响应，避免干扰其他控件。
        """
        def _on_wheel(event):
            # 内容未超出视口时不做任何事
            bbox = self._canvas.bbox('all')
            if not bbox or bbox[3] <= self._canvas.winfo_height():
                return
            if event.delta:
                self._canvas.yview_scroll(int(-event.delta / 120), 'units')
            return 'break'

        def _on_wheel_up(event):
            self._canvas.yview_scroll(-1, 'units')
            return 'break'

        def _on_wheel_down(event):
            self._canvas.yview_scroll(1, 'units')
            return 'break'

        # 绑定到整个窗口，指针在滚动区上方即生效
        self.root.bind_all('<MouseWheel>', _on_wheel)
        self.root.bind_all('<Button-4>', _on_wheel_up)
        self.root.bind_all('<Button-5>', _on_wheel_down)

        # 触控板 / 键盘翻页（更顺手的补充操作）
        self.root.bind_all('<Prior>', lambda e: self._canvas.yview_scroll(-1, 'pages'))
        self.root.bind_all('<Next>', lambda e: self._canvas.yview_scroll(1, 'pages'))
        self.root.bind_all('<Home>', lambda e: self._canvas.yview_moveto(0.0))
        self.root.bind_all('<End>', lambda e: self._canvas.yview_moveto(1.0))

    def _build_header(self, parent):
        """标题区（位于固定顶部区域）"""
        header = ttk.Frame(parent, style='TFrame')
        header.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(header, text="🎧 蓝牙音频路由", style='Title.TLabel').pack(anchor=tk.W)
        ttk.Label(header, text="蓝牙耳机麦克风 → 扬声器 / HDMI 实时监听",
                  style='Subtitle.TLabel').pack(anchor=tk.W, pady=(1, 0))

    def _build_input_section(self, parent):
        """输入设备选择区"""
        # 注意：绝不能对设备面板调用 pack_propagate(False)。
        # 该调用会把面板尺寸冻结在「创建瞬间」的大小（空面板=1px），
        # 之后加进来的下拉框永远撑不开它 —— 设备选择区会直接消失。
        panel = tk.Frame(parent, bg=self.COLOR_PANEL,
                         highlightthickness=1, highlightbackground=self.COLOR_BORDER)
        panel.pack(fill=tk.X, pady=(0, 6))

        # 内边距
        inner = tk.Frame(panel, bg=self.COLOR_PANEL, padx=16, pady=8)
        inner.pack(fill=tk.X)

        # 标题行
        title_row = tk.Frame(inner, bg=self.COLOR_PANEL)
        title_row.pack(fill=tk.X)

        tk.Label(title_row, text="🎤 输入设备 (麦克风)",
                 bg=self.COLOR_PANEL, fg=self.COLOR_ACCENT,
                 font=('Segoe UI', 11, 'bold')).pack(side=tk.LEFT)

        refresh_btn = tk.Button(title_row, text="🔄 刷新", command=self._refresh_devices,
                                bg=self.COLOR_PANEL, fg=self.COLOR_TEXT_SECONDARY,
                                font=('Segoe UI', 9), bd=0, cursor='hand2',
                                activebackground=self.COLOR_PANEL, activeforeground=self.COLOR_ACCENT)
        refresh_btn.pack(side=tk.RIGHT)

        # 设备下拉框
        self.input_combo = ttk.Combobox(inner, state='readonly', height=10)
        self.input_combo.pack(fill=tk.X, pady=(8, 0))
        self.input_combo.bind('<<ComboboxSelected>>', self._on_input_selected)

        # 设备信息
        self.input_info = tk.Label(inner, text="请选择输入设备",
                                   bg=self.COLOR_PANEL, fg=self.COLOR_TEXT_SECONDARY,
                                   font=('Segoe UI', 9), anchor=tk.W, justify=tk.LEFT)
        self.input_info.pack(fill=tk.X, pady=(6, 0))

    def _build_output_section(self, parent):
        """输出设备选择区"""
        # 同输入区：不用 pack_propagate(False)，避免面板被冻结成 1px
        panel = tk.Frame(parent, bg=self.COLOR_PANEL,
                         highlightthickness=1, highlightbackground=self.COLOR_BORDER)
        panel.pack(fill=tk.X, pady=(0, 6))

        inner = tk.Frame(panel, bg=self.COLOR_PANEL, padx=16, pady=8)
        inner.pack(fill=tk.X)

        # 标题行
        title_row = tk.Frame(inner, bg=self.COLOR_PANEL)
        title_row.pack(fill=tk.X)

        tk.Label(title_row, text="🔊 输出设备 (扬声器 / HDMI)",
                 bg=self.COLOR_PANEL, fg=self.COLOR_ACCENT,
                 font=('Segoe UI', 11, 'bold')).pack(side=tk.LEFT)

        # 设备下拉框
        self.output_combo = ttk.Combobox(inner, state='readonly', height=10)
        self.output_combo.pack(fill=tk.X, pady=(8, 0))
        self.output_combo.bind('<<ComboboxSelected>>', self._on_output_selected)

        # 设备信息
        self.output_info = tk.Label(inner, text="请选择输出设备",
                                    bg=self.COLOR_PANEL, fg=self.COLOR_TEXT_SECONDARY,
                                    font=('Segoe UI', 9), anchor=tk.W, justify=tk.LEFT)
        self.output_info.pack(fill=tk.X, pady=(6, 0))

        # 醒目提示：当前声音输出到哪里
        self.output_target_label = tk.Label(inner,
                                            text="💡 声音将从：未选择",
                                            bg='#0e4429', fg='#4ade80',
                                            font=('Segoe UI', 10, 'bold'),
                                            anchor=tk.W, padx=10, pady=6)
        self.output_target_label.pack(fill=tk.X, pady=(8, 0))

    def _build_volume_section(self, parent):
        """音量/增益控制区"""
        panel = tk.Frame(parent, bg=self.COLOR_PANEL,
                         highlightthickness=1, highlightbackground=self.COLOR_BORDER)
        panel.pack(fill=tk.X, pady=(0, 6))

        inner = tk.Frame(panel, bg=self.COLOR_PANEL, padx=16, pady=8)
        inner.pack(fill=tk.X)

        # 标题
        tk.Label(inner, text="🎤 麦克风增益（输入放大）",
                 bg=self.COLOR_PANEL, fg=self.COLOR_ACCENT,
                 font=('Segoe UI', 11, 'bold')).pack(anchor=tk.W)

        # 输入增益滑块 + 数值
        # 【改动】默认 200% → 100%。蓝牙麦克风灵敏度通常已足够，
        # 额外放大在外放场景等同于主动把系统推向啸叫临界点。
        gain_row = tk.Frame(inner, bg=self.COLOR_PANEL)
        gain_row.pack(fill=tk.X, pady=(5, 0))

        self.input_gain_var = tk.DoubleVar(value=100.0)  # 默认 100%（不加放大）
        self.input_gain_slider = tk.Scale(
            gain_row, from_=50, to=500, orient=tk.HORIZONTAL,
            variable=self.input_gain_var,
            command=self._on_input_gain_change,
            bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
            troughcolor=self.COLOR_METER_BG,
            activebackground=self.COLOR_ACCENT,
            highlightthickness=0,
            bd=0,
            length=300,
            font=('Segoe UI', 9),
            showvalue=False
        )
        self.input_gain_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))

        self.input_gain_label = tk.Label(gain_row, text="100%",
                                         bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
                                         font=('Segoe UI', 11, 'bold'), width=6)
        self.input_gain_label.pack(side=tk.RIGHT)

        # 增益提示：避免用户把增益当作「音量」无脑拉满
        self.input_gain_hint = self._hint(
            inner,
            "增益 80~120% 为宜，过高易引发啸叫；要更响请调下方「扬声器音量」。")
        self.input_gain_hint.pack(fill=tk.X, pady=(6, 0))

        # 分隔线
        tk.Frame(inner, bg=self.COLOR_BORDER, height=1).pack(fill=tk.X, pady=8)

        # 输出音量标题
        tk.Label(inner, text="🔊 扬声器音量（输出）",
                 bg=self.COLOR_PANEL, fg=self.COLOR_ACCENT,
                 font=('Segoe UI', 11, 'bold')).pack(anchor=tk.W)

        # 音量滑块 + 数值
        vol_row = tk.Frame(inner, bg=self.COLOR_PANEL)
        vol_row.pack(fill=tk.X, pady=(5, 0))

        self.volume_var = tk.DoubleVar(value=100.0)
        self.volume_slider = tk.Scale(
            vol_row, from_=0, to=200, orient=tk.HORIZONTAL,
            variable=self.volume_var,
            command=self._on_volume_change,
            bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
            troughcolor=self.COLOR_METER_BG,
            activebackground=self.COLOR_ACCENT,
            highlightthickness=0,
            bd=0,
            length=300,
            font=('Segoe UI', 9),
            showvalue=False
        )
        self.volume_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))

        self.volume_label = tk.Label(vol_row, text="100%",
                                     bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
                                     font=('Segoe UI', 11, 'bold'), width=6)
        self.volume_label.pack(side=tk.RIGHT)

    def _build_antifeedback_section(self, parent):
        """防啸叫/回音消除控制区（4 级防护）"""
        panel = tk.Frame(parent, bg=self.COLOR_PANEL,
                         highlightthickness=1, highlightbackground=self.COLOR_BORDER)
        panel.pack(fill=tk.X, pady=(0, 6))

        inner = tk.Frame(panel, bg=self.COLOR_PANEL, padx=16, pady=8)
        inner.pack(fill=tk.X)

        # 标题行
        title_row = tk.Frame(inner, bg=self.COLOR_PANEL)
        title_row.pack(fill=tk.X)

        tk.Label(title_row, text="🛡️  防啸叫 / 回音消除",
                 bg=self.COLOR_PANEL, fg='#f59e0b',
                 font=('Segoe UI', 11, 'bold')).pack(side=tk.LEFT)

        # 状态指示器
        self.gate_status_label = tk.Label(title_row, text="  🔇 门关闭",
                                          bg=self.COLOR_PANEL, fg='#6b7280',
                                          font=('Segoe UI', 9, 'bold'))
        self.gate_status_label.pack(side=tk.RIGHT)

        # --- 1. 频谱移频 ---
        # 【改动】默认由「开启」改为「关闭」。
        # 真移频虽然音染很小，但会让整体音高固定偏移 4Hz，
        # 对音乐/较真的人声仍有可感差异 → 改为由用户按需开启。
        fs_row = tk.Frame(inner, bg=self.COLOR_PANEL)
        fs_row.pack(fill=tk.X, pady=(6, 0))

        self.freq_shift_var = tk.BooleanVar(value=False)
        self.freq_shift_check = tk.Checkbutton(
            fs_row, text="频谱移频（把整体音高偏移几 Hz，破坏反馈共振）",
            variable=self.freq_shift_var,
            command=self._on_freq_shift_toggle,
            bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
            selectcolor=self.COLOR_BG,
            activebackground=self.COLOR_PANEL,
            activeforeground=self.COLOR_TEXT,
            font=('Segoe UI', 9, 'bold'),
            bd=0, highlightthickness=0
        )
        self.freq_shift_check.pack(anchor=tk.W)

        # 移频量滑块（紧凑行：说明 + 滑块 + 数值 同一行）
        # 【改动】默认 5Hz → 4Hz（文献与工程共识区间 3~5Hz）
        self.freq_shift_amount_var = tk.DoubleVar(value=4.0)
        self.freq_shift_slider, self.freq_shift_amount_label = \
            self._compact_slider(inner, self.freq_shift_amount_var, 2, 12,
                                 self._on_freq_shift_amount_change,
                                 text="移频量", resolution=0.5,
                                 fmt='%.1f Hz', value_w=8)

        # 分隔线
        tk.Frame(inner, bg=self.COLOR_BORDER, height=1).pack(fill=tk.X, pady=7)

        # --- 2. 自适应陷波 ---
        notch_row = tk.Frame(inner, bg=self.COLOR_PANEL)
        notch_row.pack(fill=tk.X)

        self.notch_var = tk.BooleanVar(value=True)
        self.notch_check = tk.Checkbutton(
            notch_row, text="自适应陷波（自动检测并消除啸叫频率）",
            variable=self.notch_var,
            command=self._on_notch_toggle,
            bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
            selectcolor=self.COLOR_BG,
            activebackground=self.COLOR_PANEL,
            activeforeground=self.COLOR_TEXT,
            font=('Segoe UI', 9),
            bd=0, highlightthickness=0
        )
        self.notch_check.pack(anchor=tk.W)

        # 陷波强度（紧凑行）
        # 【改动】默认 20dB → 10dB；范围 6~35 → 3~30（与引擎 set_notch_attenuation_db 的 clamp 对齐）
        # 原因：衰减越深，一旦检测误判对人声的破坏越不可逆（直接把共振峰挖掉）。
        self.notch_attenuation_var = tk.DoubleVar(value=10.0)
        self.notch_slider, self.notch_attenuation_label = \
            self._compact_slider(inner, self.notch_attenuation_var, 3, 30,
                                 self._on_notch_attenuation_change,
                                 text="衰减强度", resolution=1.0,
                                 fmt='%d dB', value_w=8)

        # 实时陷波频率（用于验证是否误伤自己的人声：说话时这里应保持为空）
        self.notch_freq_label = tk.Label(
            inner, text="当前陷波：(无)",
            bg=self.COLOR_PANEL, fg=self.COLOR_TEXT_SECONDARY,
            font=('Consolas', 8), anchor=tk.W, justify=tk.LEFT,
            wraplength=self._hint_wrap)
        self._hint_labels.append(self.notch_freq_label)
        self.notch_freq_label.pack(fill=tk.X, pady=(3, 0))

        # 分隔线
        tk.Frame(inner, bg=self.COLOR_BORDER, height=1).pack(fill=tk.X, pady=7)

        # --- 3. 噪声门 ---
        # 【改动】默认由「开启」改为「关闭」。
        # 噪声门按电平判定，语音的字间停顿/气声容易被误关门，
        # 产生「吞字、喘息」的听感。需要时由用户手动开启。
        gate_row = tk.Frame(inner, bg=self.COLOR_PANEL)
        gate_row.pack(fill=tk.X)

        self.noise_gate_var = tk.BooleanVar(value=False)
        self.noise_gate_check = tk.Checkbutton(
            gate_row, text="噪声门（不说话时自动静音）",
            variable=self.noise_gate_var,
            command=self._on_noise_gate_toggle,
            bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
            selectcolor=self.COLOR_BG,
            activebackground=self.COLOR_PANEL,
            activeforeground=self.COLOR_TEXT,
            font=('Segoe UI', 9),
            bd=0, highlightthickness=0
        )
        self.noise_gate_check.pack(anchor=tk.W)

        # 噪声门阈值滑块（紧凑行）
        self.gate_threshold_var = tk.DoubleVar(value=1.5)
        self.gate_threshold_slider, self.gate_threshold_label = \
            self._compact_slider(inner, self.gate_threshold_var, 0.5, 10.0,
                                 self._on_gate_threshold_change,
                                 text="灵敏度", resolution=0.1,
                                 value_w=8)
        # 噪声门用文字档位表达比裸数字更直观
        self.gate_threshold_label.configure(text="中")

        # 分隔线
        tk.Frame(inner, bg=self.COLOR_BORDER, height=1).pack(fill=tk.X, pady=7)

        # --- 4. 侧链抑制 ---
        # 【改动】默认由「开启」改为「关闭」。
        # 它用「上一块输出电平」压低输入增益，形成随语音起伏的自动增益，
        # 听感就是「音量一抽一抽」；且它只能降低啸叫增长速率，
        # 真正的抑制靠陷波/移频，所以改为默认关闭。
        sc_row = tk.Frame(inner, bg=self.COLOR_PANEL)
        sc_row.pack(fill=tk.X)

        self.sidechain_var = tk.BooleanVar(value=False)
        self.sidechain_check = tk.Checkbutton(
            sc_row, text="侧链抑制（扬声器有声时压低麦克风）",
            variable=self.sidechain_var,
            command=self._on_sidechain_toggle,
            bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
            selectcolor=self.COLOR_BG,
            activebackground=self.COLOR_PANEL,
            activeforeground=self.COLOR_TEXT,
            font=('Segoe UI', 9),
            bd=0, highlightthickness=0
        )
        self.sidechain_check.pack(anchor=tk.W)

        # 侧链强度滑块（紧凑行）
        self.sidechain_amount_var = tk.DoubleVar(value=60.0)
        self.sidechain_slider, self.sidechain_amount_label = \
            self._compact_slider(inner, self.sidechain_amount_var, 10, 90,
                                 self._on_sidechain_amount_change,
                                 text="抑制强度", resolution=1.0,
                                 fmt='%d %%', value_w=8)

        # 四个子项的调参要点合并为一行，替代原先各自一条说明（省下约 3 行高度）
        tk.Frame(inner, bg=self.COLOR_BORDER, height=1).pack(fill=tk.X, pady=7)
        self._hint(
            inner,
            "推荐值：移频 3~5Hz（真移频非颤音） · 陷波 6~12dB（只消孤立窄峰） · "
            "噪声门 8/120/150ms（字间停顿不吞字） · 侧链仅压制增长速率，非必需。"
        ).pack(fill=tk.X, pady=(0, 1))

    def _build_meter_section(self, parent):
        """电平表区"""
        panel = tk.Frame(parent, bg=self.COLOR_PANEL,
                         highlightthickness=1, highlightbackground=self.COLOR_BORDER)
        panel.pack(fill=tk.X, pady=(0, 6))

        inner = tk.Frame(panel, bg=self.COLOR_PANEL, padx=16, pady=8)
        inner.pack(fill=tk.X)

        # 标题
        tk.Label(inner, text="📊 实时电平",
                 bg=self.COLOR_PANEL, fg=self.COLOR_ACCENT,
                 font=('Segoe UI', 11, 'bold')).pack(anchor=tk.W)

        # 输入电平
        in_row = tk.Frame(inner, bg=self.COLOR_PANEL)
        in_row.pack(fill=tk.X, pady=(6, 4))

        tk.Label(in_row, text="输入", bg=self.COLOR_PANEL, fg=self.COLOR_TEXT_SECONDARY,
                 font=('Segoe UI', 9), width=6, anchor=tk.W).pack(side=tk.LEFT)

        self.input_meter_canvas = tk.Canvas(in_row, height=16, bg=self.COLOR_METER_BG,
                                            highlightthickness=0, bd=0)
        self.input_meter_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        self.input_meter_value = tk.Label(in_row, text="-∞ dB",
                                          bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
                                          font=('Consolas', 9), width=8, anchor=tk.E)
        self.input_meter_value.pack(side=tk.RIGHT, padx=(8, 0))

        # 输出电平
        out_row = tk.Frame(inner, bg=self.COLOR_PANEL)
        out_row.pack(fill=tk.X, pady=(4, 0))

        tk.Label(out_row, text="输出", bg=self.COLOR_PANEL, fg=self.COLOR_TEXT_SECONDARY,
                 font=('Segoe UI', 9), width=6, anchor=tk.W).pack(side=tk.LEFT)

        self.output_meter_canvas = tk.Canvas(out_row, height=16, bg=self.COLOR_METER_BG,
                                             highlightthickness=0, bd=0)
        self.output_meter_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        self.output_meter_value = tk.Label(out_row, text="-∞ dB",
                                           bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
                                           font=('Consolas', 9), width=8, anchor=tk.E)
        self.output_meter_value.pack(side=tk.RIGHT, padx=(8, 0))

    def _build_status_section(self, parent):
        """状态信息区"""
        panel = tk.Frame(parent, bg=self.COLOR_PANEL,
                         highlightthickness=1, highlightbackground=self.COLOR_BORDER)
        panel.pack(fill=tk.X, pady=(0, 2))

        inner = tk.Frame(panel, bg=self.COLOR_PANEL, padx=16, pady=8)
        inner.pack(fill=tk.X)

        # 标题
        tk.Label(inner, text="📈 运行状态",
                 bg=self.COLOR_PANEL, fg=self.COLOR_ACCENT,
                 font=('Segoe UI', 11, 'bold')).pack(anchor=tk.W)

        # 状态网格
        grid = tk.Frame(inner, bg=self.COLOR_PANEL)
        grid.pack(fill=tk.X, pady=(6, 0))

        # 第1行：状态 + 延迟
        self.status_label = tk.Label(grid, text="● 未运行",
                                     bg=self.COLOR_PANEL, fg=self.COLOR_TEXT_SECONDARY,
                                     font=('Segoe UI', 10, 'bold'))
        self.status_label.grid(row=0, column=0, sticky=tk.W)

        self.latency_label = tk.Label(grid, text="延迟: -- ms",
                                      bg=self.COLOR_PANEL, fg=self.COLOR_TEXT_SECONDARY,
                                      font=('Segoe UI', 9))
        self.latency_label.grid(row=0, column=1, sticky=tk.E)

        # 第2行：缓冲 + 欠载
        self.buffer_label = tk.Label(grid, text="缓冲: --",
                                     bg=self.COLOR_PANEL, fg=self.COLOR_TEXT_SECONDARY,
                                     font=('Segoe UI', 9))
        self.buffer_label.grid(row=1, column=0, sticky=tk.W, pady=(4, 0))

        self.xrun_label = tk.Label(grid, text="欠载: 0",
                                   bg=self.COLOR_PANEL, fg=self.COLOR_TEXT_SECONDARY,
                                   font=('Segoe UI', 9))
        self.xrun_label.grid(row=1, column=1, sticky=tk.E, pady=(4, 0))

        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

    def _build_control_section(self, parent):
        """控制按钮区（位于固定顶部区域，始终可见）

        把「开始路由」放在顶部固定区，是为了保证它永远不被窗口高度裁掉 ——
        这是整个界面最关键的按钮。
        """
        btn_frame = tk.Frame(parent, bg=self.COLOR_BG)
        btn_frame.pack(fill=tk.X, pady=(0, 0))

        self.start_button = tk.Button(
            btn_frame, text="▶  开始路由",
            command=self._toggle_router,
            bg=self.COLOR_ACCENT, fg='white',
            font=('Segoe UI', 12, 'bold'),
            bd=0, cursor='hand2',
            activebackground=self.COLOR_ACCENT_HOVER,
            activeforeground='white',
            height=1,
            relief=tk.FLAT
        )
        self.start_button.pack(fill=tk.X)

        # 跟随系统默认设备开关。
        # 之前程序完全无视 Windows 的默认设备设置：输入硬性优先蓝牙麦克风、
        # 输出优先 HDMI / 内置扬声器，并且会记住上次手动选择。
        # 用户改了系统默认却不起作用，就是因为这三条规则在挡。
        # 勾选此项后，程序会改为跟随 Windows 的默认输入/输出设备。
        follow_row = tk.Frame(parent, bg=self.COLOR_BG)
        follow_row.pack(fill=tk.X, pady=(6, 0))
        self.follow_default_var = tk.BooleanVar(value=bool(self._prefs.get('follow_system_default', False)))
        tk.Checkbutton(
            follow_row, text="跟随系统默认设备（Windows 声音设置里改了就跟着变）",
            variable=self.follow_default_var, command=self._on_follow_default_toggle,
            bg=self.COLOR_BG, fg=self.COLOR_TEXT_SECONDARY,
            selectcolor=self.COLOR_PANEL,
            activebackground=self.COLOR_BG, activeforeground=self.COLOR_TEXT,
            font=('Segoe UI', 9), bd=0, highlightthickness=0, cursor='hand2'
        ).pack(anchor=tk.W)

    # ---------- 设备管理 ----------

    def _refresh_devices(self):
        """刷新设备列表

        参数 on_user_action 区分「启动时首次枚举」与「用户点刷新」：
        启动时可以恢复上次偏好；用户主动点刷新时，应当反映系统当前状态
        （否则用户改了系统默认设备，一刷新又被旧偏好覆盖回去，看起来像没反应）。
        """
        return self._refresh_devices_impl(user_action=True)

    def _refresh_devices_impl(self, user_action: bool = True):
        try:
            # 刷新前记住当前选择，便于刷新后对比提示
            prev_in = self.input_combo.get()
            prev_out = self.output_combo.get()

            # 获取按优先级排序且过滤可用的设备
            all_input = DeviceManager.get_input_devices(sort_by_priority=True)
            all_output = DeviceManager.get_output_devices(sort_by_priority=True)

            # 过滤出真正可用的设备（避免显示打不开的设备）
            self._input_devices = DeviceManager.filter_usable_devices(all_input, is_input=True)
            self._output_devices = DeviceManager.filter_usable_devices(all_output, is_input=False)

            # 如果过滤后为空（可能测试有问题），回退到全部设备
            if not self._input_devices:
                self._input_devices = all_input
            if not self._output_devices:
                self._output_devices = all_output

            # 更新输入下拉框（显示完整名称含 API 标签）
            input_names = []
            for d in self._input_devices:
                name = d.full_display_name
                # 添加类型标签
                if d.is_bluetooth:
                    name += "  🎧"
                elif d.device_type == 'virtual':
                    name += "  💻"
                input_names.append(name)
            self.input_combo['values'] = input_names

            # 更新输出下拉框
            output_names = []
            for d in self._output_devices:
                name = d.full_display_name
                if d.is_bluetooth:
                    name += "  🎧"
                elif d.is_display_audio:
                    # HDMI / DisplayPort 显示器音频（名字里通常没有 "hdmi"）
                    name += "  📺"
                elif d.device_type == 'virtual':
                    name += "  💻"
                output_names.append(name)
            self.output_combo['values'] = output_names

            # 智能选择默认设备
            self._auto_select_devices(user_action=user_action)

            # 提示本次刷新带来的变化（让用户确认刷新确实起作用了）
            if user_action:
                new_in = self.input_combo.get()
                new_out = self.output_combo.get()
                changes = []
                if new_in != prev_in:
                    changes.append(f"输入 → {new_in}")
                if new_out != prev_out:
                    changes.append(f"输出 → {new_out}")
                if not changes:
                    changes.append("设备列表已更新，当前选择未变")

                # 顺带汇报系统默认设备，便于用户核对
                try:
                    sd_in = DeviceManager.find_system_default_input()
                    sd_out = DeviceManager.find_system_default_output()
                    default_txt = (
                        f"\n\nWindows 当前默认：\n"
                        f"  输入：{sd_in.display_name if sd_in else '未知'}\n"
                        f"  输出：{sd_out.display_name if sd_out else '未知'}")
                except Exception:
                    default_txt = ""

                messagebox.showinfo(
                    "刷新完成",
                    "已重新扫描系统音频设备。\n\n" + "\n".join(changes) + default_txt)

        except Exception as e:
            messagebox.showerror("Error", f"Failed to refresh devices: {str(e)}")

    def _auto_select_devices(self, user_action: bool = False):
        """智能选择默认设备

        选择优先级：
          0) 若用户勾选「跟随系统默认设备」→ 直接用 Windows 默认输入/输出
          1) 输入：优先蓝牙麦克风（本工具的主用场景）
          2) 输出：启动时优先恢复上次手动选择 → HDMI → 内置扬声器 → 第一个
                   （用户点刷新时不恢复偏好，改为反映系统当前默认，
                     否则改系统设置后刷新会被旧偏好盖回去）
        """
        # ---- 0) 跟随系统默认 ----
        if getattr(self, 'follow_default_var', None) and self.follow_default_var.get():
            if self._apply_system_default(fallback=True):
                return

        # ---- 1) 输入设备 ----
        # 用户点刷新时优先跟随系统默认输入；启动时优先蓝牙麦克风（本工具主场景）。
        # 之前无脑 fallback 到 index 0，会选中排在最前的 WASAPI 虚拟声卡
        # （CABLE Output），这不是用户想要的麦克风。
        in_candidates = []
        if user_action:
            sd_in = DeviceManager.find_system_default_input()
            if sd_in:
                in_candidates.append(sd_in.index)
        # 蓝牙麦克风（启动时的主选，也是刷新的次选）
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
            # 退而求其次：优先选「非虚拟」的物理麦克风，避免落到虚拟声卡上
            for i, d in enumerate(self._input_devices):
                if d.device_type != 'virtual':
                    self.input_combo.current(i)
                    self._on_input_selected(None)
                    picked = True
                    break
        if not picked and self._input_devices:
            self.input_combo.current(0)
            self._on_input_selected(None)

        # ---- 2) 输出设备 ----
        # 仅在启动时（非用户刷新）恢复上次偏好
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
            # 用户点刷新：优先跟随系统默认输出（改了就跟着变）
            sd_out = DeviceManager.find_system_default_output()
            if sd_out:
                for i, d in enumerate(self._output_devices):
                    if d.index == sd_out.index:
                        self.output_combo.current(i)
                        self._on_output_selected(None)
                        return

        # 优先选 HDMI / DisplayPort 显示器音频
        hdmi_out = DeviceManager.find_hdmi_output()
        if hdmi_out:
            for i, d in enumerate(self._output_devices):
                if d.index == hdmi_out.index:
                    self.output_combo.current(i)
                    self._on_output_selected(None)
                    return
        # 找内置扬声器
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
        """输入设备选中"""
        idx = self.input_combo.current()
        if idx >= 0 and idx < len(self._input_devices):
            device = self._input_devices[idx]
            api_name = device.hostapi_name or f"API {device.hostapi}"
            tag = "  ★ 系统默认" if device.is_system_default else ""
            self.input_info.config(
                text=(f"{device.display_name}{tag}\n"
                      f"声道: {device.max_input_channels}  |  "
                      f"标称采样率: {int(device.default_samplerate)} Hz  |  {api_name}")
            )
            if not self.engine.is_running:
                self.engine.set_input_device(device)
                # 显示引擎实际选定的采样率（可能与标称不同，
                # 例如蓝牙 WASAPI 端点标称 16k、MME 端点标称 44.1k）
                try:
                    picked = self.engine._input_samplerate
                except Exception:
                    picked = int(device.default_samplerate)
                self.input_info.config(
                    text=(f"{device.display_name}{tag}\n"
                          f"声道: {device.max_input_channels}  |  "
                          f"实际使用: {picked} Hz  |  {api_name}")
                )

    def _on_output_selected(self, event):
        """输出设备选中"""
        idx = self.output_combo.current()
        if idx >= 0 and idx < len(self._output_devices):
            device = self._output_devices[idx]
            api_name = device.hostapi_name or f"API {device.hostapi}"
            tag = "  ★ 系统默认" if device.is_system_default else ""
            self.output_info.config(
                text=(f"{device.display_name}{tag}\n"
                      f"声道: {device.max_output_channels}  |  "
                      f"标称采样率: {int(device.default_samplerate)} Hz  |  {api_name}")
            )
            # 更新醒目提示
            short_name = device.display_name
            if len(short_name) > 30:
                short_name = short_name[:28] + "..."
            # 判定顺序很重要：蓝牙耳机也可能被识别为 display_audio/virtual，
            # 但用户最需要知道的是「它是不是蓝牙耳机」，所以蓝牙优先。
            if device.is_bluetooth:
                tag = "🎧 蓝牙"
            elif device.is_display_audio:
                tag = "📺 HDMI/显示器"
            elif device.device_type == 'virtual':
                tag = "💻 虚拟声卡"
            else:
                tag = "🔊 扬声器"
            self.output_target_label.config(
                text=f"💡 声音将从：{short_name}  （{tag}）"
                     + ("   ★系统默认" if device.is_system_default else ""))
            # 记住用户选择，供下次启动自动恢复。
            # 但「跟随系统默认」模式下不记，否则会把系统默认固化成手动偏好。
            if not (getattr(self, 'follow_default_var', None)
                    and self.follow_default_var.get()):
                self._save_preferred_output(device)
            if not self.engine.is_running:
                self.engine.set_output_device(device)

    # ---------- 设备偏好记忆 ----------

    def _pref_file(self) -> str:
        """偏好文件路径（项目根目录，避免污染用户目录）

        注意层级：__file__ = <root>/audio_router/ui/main_window.py
          dirname ×1 -> ui/   ×2 -> audio_router/   ×3 -> 项目根
        旧实现只上溯两级，把偏好文件错放进了 audio_router/ 包目录里。
        """
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, '.device_prefs.json')

    def _load_prefs(self) -> dict:
        """读取全部偏好（容错：任何异常都返回空字典）"""
        try:
            if os.path.exists(self._pref_file()):
                with open(self._pref_file(), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _update_prefs(self, **kwargs):
        """合并写入偏好项"""
        try:
            prefs = self._load_prefs()
            prefs.update(kwargs)
            with open(self._pref_file(), 'w', encoding='utf-8') as f:
                json.dump(prefs, f, ensure_ascii=False, indent=2)
            self._prefs = prefs
        except Exception:
            pass  # 偏好保存失败不影响主流程

    def _save_preferred_output(self, device):
        """记录用户手动选择的输出设备名，下次优先恢复"""
        self._update_prefs(output_name=device.name,
                           output_hostapi=device.hostapi_name)

    def _load_preferred_output(self):
        """读取上次选择的输出设备名，返回 (name, hostapi) 或 None"""
        name = self._prefs.get('output_name')
        if name:
            return name, self._prefs.get('output_hostapi', '')
        return None

    def _on_follow_default_toggle(self):
        """「跟随系统默认设备」开关变化"""
        follow = bool(self.follow_default_var.get())
        self._update_prefs(follow_system_default=follow)
        if follow:
            # 立刻按系统默认重选一次，让用户马上看到效果
            self._apply_system_default(fallback=False)

    def _apply_system_default(self, fallback: bool = True) -> bool:
        """按 Windows 默认设备重选输入/输出，返回是否成功选中了至少一项"""
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
        """麦克风增益滑块变化"""
        gain = float(value)
        self.input_gain_label.config(text=f"{int(gain)}%")
        self.engine.set_input_gain(gain / 100.0)

    def _on_volume_change(self, value):
        """音量滑块变化"""
        vol = float(value)
        self.volume_label.config(text=f"{int(vol)}%")
        self.engine.set_volume(vol / 100.0)

    # ---------- 防啸叫控制 ----------

    def _on_noise_gate_toggle(self):
        """噪声门开关"""
        enabled = self.noise_gate_var.get()
        self.engine.set_noise_gate_enabled(enabled)
        self.gate_threshold_slider.config(state='normal' if enabled else 'disabled')

    def _on_gate_threshold_change(self, value):
        """噪声门阈值滑块变化（UI值 0.5~10.0 对应实际阈值 0.005~0.1）"""
        val = float(value)
        # 映射到实际阈值：0.5 -> 0.005, 10.0 -> 0.1
        threshold = val / 100.0
        self.engine.set_noise_gate_threshold(threshold)
        # 更新标签文字
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

    def _on_sidechain_toggle(self):
        """侧链抑制开关"""
        enabled = self.sidechain_var.get()
        self.engine.set_sidechain_enabled(enabled)
        self.sidechain_slider.config(state='normal' if enabled else 'disabled')

    def _on_sidechain_amount_change(self, value):
        """侧链抑制强度滑块变化"""
        val = float(value)
        self.sidechain_amount_label.config(text=f"{int(val)}%")
        self.engine.set_sidechain_amount(val / 100.0)

    # ---------- 频谱移频控制 ----------

    def _on_freq_shift_toggle(self):
        """频谱移频开关"""
        enabled = self.freq_shift_var.get()
        self.engine.set_freq_shift_enabled(enabled)
        self.freq_shift_slider.config(state='normal' if enabled else 'disabled')

    def _on_freq_shift_amount_change(self, value):
        """移频量滑块变化"""
        val = float(value)
        self.freq_shift_amount_label.config(text=f"{val:.1f} Hz")
        self.engine.set_freq_shift_amount(val)

    # ---------- 自适应陷波控制 ----------

    def _on_notch_toggle(self):
        """自适应陷波开关"""
        enabled = self.notch_var.get()
        self.engine.set_notch_enabled(enabled)
        self.notch_slider.config(state='normal' if enabled else 'disabled')

    def _on_notch_attenuation_change(self, value):
        """陷波衰减强度滑块变化"""
        val = float(value)
        self.notch_attenuation_label.config(text=f"{int(val)} dB")
        self.engine.set_notch_attenuation_db(val)

    # ---------- 路由控制 ----------

    def _toggle_router(self):
        """切换路由启停"""
        if self.engine.is_running:
            self._stop_router()
        else:
            self._start_router()

    def _start_router(self):
        """启动路由（必须在主线程执行，PortAudio 要求流在创建它的线程中启动）"""
        # 验证设备选择
        input_idx = self.input_combo.current()
        output_idx = self.output_combo.current()

        if input_idx < 0:
            messagebox.showwarning("提示", "请选择输入设备（麦克风）")
            return
        if output_idx < 0:
            messagebox.showwarning("提示", "请选择输出设备（扬声器）")
            return

        # 更新引擎设备
        self.engine.set_input_device(self._input_devices[input_idx])
        self.engine.set_output_device(self._output_devices[output_idx])

        # 禁用下拉框和刷新按钮
        self.input_combo.config(state='disabled')
        self.output_combo.config(state='disabled')

        # 按钮变为停止
        self.start_button.config(text="■  停止路由", bg=self.COLOR_DANGER,
                                 activebackground="#f87171")

        # 注意：PortAudio/WASAPI 流必须在创建它们的同一线程中启动
        # 所以直接在 UI 线程启动（启动操作很快，不会明显卡 UI）
        import traceback
        try:
            self.engine.start()
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            print(f"\n[START ERROR] {error_msg}")
            print(traceback.format_exc())
            self._on_start_error(error_msg)

    def _on_start_error(self, error_msg, detail=""):
        """启动失败回调"""
        # 显示详细错误信息
        messagebox.showerror("Start Failed", error_msg)
        self._reset_ui_to_stopped()

    def _stop_router(self):
        """停止路由"""
        threading.Thread(target=self._do_stop, daemon=True).start()

    def _do_stop(self):
        """实际停止（后台线程）"""
        try:
            self.engine.stop()
        finally:
            self.root.after(0, self._reset_ui_to_stopped)

    def _reset_ui_to_stopped(self):
        """重置 UI 到停止状态"""
        self.input_combo.config(state='readonly')
        self.output_combo.config(state='readonly')
        self.start_button.config(text="▶  开始路由", bg=self.COLOR_ACCENT,
                                 activebackground=self.COLOR_ACCENT_HOVER)
        self.status_label.config(text="● 未运行", fg=self.COLOR_TEXT_SECONDARY)
        self._ensure_meter_state()
        self._meter_display = {'in': 0.0, 'out': 0.0}
        self._meter_peak = {'in': 0.0, 'out': 0.0}
        self._meter_peak_hold = {'in': 0, 'out': 0}
        self._draw_meter(self.input_meter_canvas, 0.0)
        self._draw_meter(self.output_meter_canvas, 0.0)
        self.input_meter_value.config(text="-∞ dB")
        self.output_meter_value.config(text="-∞ dB")
        self.latency_label.config(text="延迟: -- ms")
        self.buffer_label.config(text="缓冲: --")
        self.notch_freq_label.config(text="当前陷波：(无)", fg=self.COLOR_TEXT_SECONDARY)

    # ---------- 引擎回调 ----------

    def _on_engine_status(self, status: str):
        """引擎状态变化回调（可能在非UI线程）"""
        self.root.after(0, lambda: self._update_status_text(status))

    def _on_engine_error(self, error: str):
        """引擎错误回调（可能在非UI线程）"""
        self.root.after(0, lambda: messagebox.showerror("错误", error))

    def _update_status_text(self, status: str):
        """更新状态文字"""
        if status == 'started':
            self.status_label.config(text="● 运行中", fg=self.COLOR_SUCCESS)
        elif status == 'stopped':
            self.status_label.config(text="● 已停止", fg=self.COLOR_TEXT_SECONDARY)

    # ---------- UI 刷新 ----------

    def _update_ui(self):
        """定时刷新 UI 显示（电平、状态等）"""
        if not self._ui_running:
            return

        if self.engine.is_running:
            # 更新电平表
            # 注意：raw level 是线性 RMS。正常说话时蓝牙麦克风只有 0.002~0.03，
            # 若直接按 level 比例画条，200px 的条只会亮 0~6px —— 看起来就是「没反应」。
            # 所以这里用 dB 映射 + 噪声地板，再做快起慢落平滑和峰值保持。
            in_level = self.engine.input_level
            out_level = self.engine.output_level
            in_ratio = self._level_to_ratio(in_level)
            out_ratio = self._level_to_ratio(out_level)

            self._meter_smooth('in', in_ratio)
            self._meter_smooth('out', out_ratio)

            self._draw_meter(self.input_meter_canvas, self._meter_display['in'],
                             peak=self._meter_peak['in'])
            self._draw_meter(self.output_meter_canvas, self._meter_display['out'],
                             peak=self._meter_peak['out'])

            # 更新 dB 值
            in_db = self._level_to_db(in_level)
            out_db = self._level_to_db(out_level)
            self.input_meter_value.config(text=f"{in_db} dB")
            self.output_meter_value.config(text=f"{out_db} dB")

            # 更新延迟和缓冲
            latency = self.engine.estimated_latency_ms
            self.latency_label.config(text=f"延迟: {latency:.0f} ms")

            buf_pct = self.engine.buffer_occupancy * 100
            self.buffer_label.config(text=f"缓冲: {buf_pct:.0f}%")

            # 更新欠载计数
            xruns = self.engine.xruns
            xrun_text = f"欠载: {xruns}"
            xrun_color = self.COLOR_WARNING if xruns > 0 else self.COLOR_TEXT_SECONDARY
            self.xrun_label.config(text=xrun_text, fg=xrun_color)

            # 更新噪声门状态
            gate_gain = self.engine.gate_gain
            if self.noise_gate_var.get():
                if gate_gain > 0.8:
                    self.gate_status_label.config(text="  🟢 门打开", fg='#22c55e')
                elif gate_gain > 0.2:
                    self.gate_status_label.config(text="  🟡 过渡中", fg='#f59e0b')
                else:
                    self.gate_status_label.config(text="  🔇 门关闭", fg='#6b7280')
            else:
                self.gate_status_label.config(text="  ⏸️ 已停用", fg='#6b7280')

            # 更新陷波频率显示
            # 这是判断「算法有没有误伤自己声音」的最直接依据：
            # 正常说话时这里应该始终显示「(无)」；只有真啸叫时才会出现频率。
            self._update_notch_freq_display()

        # 30ms 后再次刷新（约 30 FPS）
        self.root.after(30, self._update_ui)

    def _update_notch_freq_display(self):
        """刷新当前激活的陷波频率"""
        try:
            freqs = self.engine.get_active_notch_freqs()
        except Exception:
            freqs = []

        if not self.notch_var.get():
            self.notch_freq_label.config(text="当前陷波：(陷波已停用)", fg=self.COLOR_TEXT_SECONDARY)
            return

        if not freqs:
            self.notch_freq_label.config(text="当前陷波：(无)  ✓ 未检测到啸叫",
                                         fg=self.COLOR_SUCCESS)
            return

        text = "  ".join(f"{f:.0f}Hz" for f in freqs)
        self.notch_freq_label.config(
            text=f"当前陷波 ({len(freqs)} 个)：{text}",
            fg=self.COLOR_WARNING)

    # ---------- 电平表：dB 映射 / 弹道 / 峰值保持 ----------

    # 噪声地板与满刻度：低于 -70dBFS 视为无声，0dBFS 为满格。
    # 地板取 -70 而非 -60：蓝牙 HFP 麦克风电平天然偏低（正常说话常在
    # -45 ~ -65 dBFS），若地板是 -60，说话时电平条仍会基本不动。
    METER_FLOOR_DB = -70.0
    METER_CEIL_DB = 0.0
    # 快起慢落：上升立即跟随（防漏掉字首），下降按每帧比例衰减
    METER_ATTACK = 1.0
    METER_RELEASE = 0.25
    # 峰值保持衰减（每帧）
    METER_PEAK_DECAY = 0.012

    def _ensure_meter_state(self):
        """惰性初始化电平表状态（在 __init__ 之外做，避免改动构造函数）"""
        if not hasattr(self, '_meter_display'):
            self._meter_display = {'in': 0.0, 'out': 0.0}
            self._meter_peak = {'in': 0.0, 'out': 0.0}
            self._meter_peak_hold = {'in': 0, 'out': 0}

    def _level_to_ratio(self, level: float) -> float:
        """线性 RMS → 0.0~1.0 的显示比例（dB 映射）"""
        if level <= 1e-7:
            return 0.0
        import math
        db = 20.0 * math.log10(level)
        span = self.METER_CEIL_DB - self.METER_FLOOR_DB
        ratio = (db - self.METER_FLOOR_DB) / span
        return max(0.0, min(1.0, ratio))

    def _meter_smooth(self, key: str, target: float):
        """对电平做快起慢落平滑，并维护峰值保持"""
        self._ensure_meter_state()
        cur = self._meter_display[key]

        if target >= cur:
            cur = cur + (target - cur) * self.METER_ATTACK
        else:
            cur = cur + (target - cur) * self.METER_RELEASE
            if cur < 0.001:
                cur = 0.0
        self._meter_display[key] = cur

        # 峰值保持：更新则重置保持计时，之后缓慢下落
        if cur >= self._meter_peak[key]:
            self._meter_peak[key] = cur
            self._meter_peak_hold[key] = 12  # 约 0.36s
        else:
            if self._meter_peak_hold[key] > 0:
                self._meter_peak_hold[key] -= 1
            else:
                self._meter_peak[key] = max(cur, self._meter_peak[key] - self.METER_PEAK_DECAY)

    def _draw_meter(self, canvas: tk.Canvas, ratio: float, peak: float = 0.0):
        """绘制电平表（ratio 已是 0~1 的显示比例）"""
        canvas.update_idletasks()
        width = canvas.winfo_width()
        height = canvas.winfo_height()

        if width <= 1:
            return

        canvas.delete("all")

        # 背景
        canvas.create_rectangle(0, 0, width, height, fill=self.COLOR_METER_BG, outline="")

        # 电平填充（按 dB 映射，低电平也能看见明显长度）
        fill_width = int(min(max(ratio, 0.0), 1.0) * width)
        if fill_width > 0:
            # 渐变色：绿 -> 黄 -> 红（阈值对齐 dB 刻度，约 -12dB / -3dB）
            if ratio < 0.8:
                fill_color = self.COLOR_METER_PEAK
            elif ratio < 0.95:
                fill_color = self.COLOR_WARNING
            else:
                fill_color = self.COLOR_DANGER
            canvas.create_rectangle(0, 0, fill_width, height, fill=fill_color, outline="")

        # 峰值保持刻线
        if peak > 0.01:
            px = int(min(peak, 1.0) * (width - 2))
            px = max(1, min(width - 2, px))
            canvas.create_rectangle(px, 0, px + 2, height,
                                    fill=self.COLOR_TEXT, outline="")

    def _level_to_db(self, level: float) -> str:
        """电平转 dB 显示"""
        if level < 0.0001:
            return "-∞"
        import math
        db = 20 * math.log10(level)
        return f"{db:+.1f}"

    # ---------- 窗口事件 ----------

    def _on_close(self):
        """窗口关闭"""
        self._ui_running = False
        try:
            self.engine.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        """运行主循环"""
        self.root.mainloop()
