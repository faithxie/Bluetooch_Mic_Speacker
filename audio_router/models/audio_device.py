"""音频设备信息模型"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AudioDeviceInfo:
    """音频设备信息"""
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float
    hostapi: int
    hostapi_name: str = ""
    # 是否为 Windows 当前的默认输入/输出设备。
    # 用于在 UI 上标注「系统默认」，并支持「跟随系统默认」的选择策略。
    is_system_default: bool = False
    # 蓝牙判定缓存：None=未计算，True/False=已计算
    _bluetooth_peer: Optional[bool] = field(default=None, repr=False, compare=False)

    @property
    def is_input(self) -> bool:
        """是否为输入设备（麦克风）"""
        return self.max_input_channels > 0

    @property
    def is_output(self) -> bool:
        """是否为输出设备（扬声器）"""
        return self.max_output_channels > 0

    @property
    def display_name(self) -> str:
        """显示名称（简化设备名）"""
        name = self.name.strip()
        # 去掉 Windows 常见的驱动路径后缀
        if '(@System32' in name:
            name = name.split('(@System32')[0].strip()
        if ';' in name and name.endswith(')'):
            # 处理类似 "Hands-Free%0;(设备名)" 的格式
            parts = name.split(';')
            if len(parts) >= 2:
                name = parts[-1].rstrip(')').strip()
        return name

    @property
    def full_display_name(self) -> str:
        """完整显示名称（含 host API 标签与系统默认标记）"""
        api_label = self._get_short_hostapi_label()
        name = f"{self.display_name} [{api_label}]"
        if self.is_system_default:
            name = "★ " + name
        return name

    def _get_short_hostapi_label(self) -> str:
        """获取 host API 简短标签"""
        name = self.hostapi_name.lower() if self.hostapi_name else ''
        if 'wasapi' in name:
            return 'WASAPI'
        elif 'wdm-ks' in name or 'wdmks' in name:
            return 'WDM-KS'
        elif 'directsound' in name or 'direct sound' in name:
            return 'DSound'
        elif 'mme' in name:
            return 'MME'
        elif 'asio' in name:
            return 'ASIO'
        elif self.hostapi_name:
            return self.hostapi_name[:8]
        return f'API{self.hostapi}'

    @property
    def device_type(self) -> str:
        """设备类型推测"""
        name_lower = self.name.lower()
        if 'bluetooth' in name_lower or 'bth' in name_lower or 'hands-free' in name_lower or 'hands free' in name_lower:
            return 'bluetooth'
        if 'usb' in name_lower:
            return 'usb'
        if 'realtek' in name_lower:
            return 'built-in'
        if 'vb-audio' in name_lower or 'virtual' in name_lower:
            return 'virtual'
        if self.is_display_audio:
            return 'hdmi'
        return 'unknown'

    # 显示器音频（HDMI / DisplayPort）在 Windows 上的常见命名。
    # 关键：设备名里通常**没有** "hdmi" 字样，而是以显卡厂商的音频驱动名出现。
    DISPLAY_AUDIO_KEYWORDS = (
        # 显卡厂商标识
        'intel(r) display audio', 'intel display audio', '英特尔(r) 显示器音频',
        '英特尔显示器音频', '显示器音频', 'display audio',
        'nvidia high definition audio', 'nvidia audio',
        'amd high definition audio', 'amd audio',
        'high definition audio device', 'hd audio driver',
        # 直连标识
        'hdmi', 'displayport', 'display port',
        # 常见接收端设备名
        'led proj', 'projector', 'tv', 'television', 'monitor',
    )

    @property
    def is_display_audio(self) -> bool:
        """是否为显示器音频输出（HDMI / DisplayPort）。

        用于把「LED Proj (英特尔(R) 显示器音频)」这类设备识别成 HDMI 输出 ——
        Windows 上它们几乎不会在名字里带 "hdmi"。
        """
        name_lower = self.name.lower()
        for kw in self.DISPLAY_AUDIO_KEYWORDS:
            if kw in name_lower:
                return True
        return False

    @property
    def is_bluetooth(self) -> bool:
        """是否为蓝牙设备

        难点：Windows 给已配对蓝牙音频设备取的名字往往只是
        「耳机 (dubuds G108青春版)」 —— 既没有 "bluetooth" 也没有
        "hands-free"。蓝牙身份只存在于设备实例路径里，PortAudio 拿不到。

        可靠的旁证：同一台物理蓝牙耳机一定还会以 HFP 免提端点出现，
        名字形如「耳机 (@System32\\drivers\\bthhfenum.sys,#2;%1 Hands-Free%0;(XXX))」。
        因此用「括号内的设备名」做关联：只要存在一个名字里含 bthhfenum /
        Hands-Free 且括号内设备名相同的端点，就认定本设备也是蓝牙。
        """
        name_lower = self.name.lower()
        if self.device_type == 'bluetooth':
            return True
        if any(k in name_lower for k in
               ('bthhfenum', 'hands-free', 'hands free', 'bluetooth', '蓝牙')):
            return True
        # 通过「同类蓝牙 HFP 端点」反查（结果缓存在实例上，避免重复扫描）
        if self._bluetooth_peer is None:
            try:
                self._bluetooth_peer = self._has_bluetooth_hfp_peer()
            except Exception:
                self._bluetooth_peer = False
        return self._bluetooth_peer

    @property
    def _device_key(self) -> str:
        """提取用于跨端点关联的设备标识。

        例：
          "耳机 (dubuds G108青春版)"                      -> "dubuds g108青春版"
          "耳机 (@System32\\...bthhfenum.sys,#2;%1 Hands-Free%0;(dubuds G108青春版))"
                                                          -> "dubuds g108青春版"
          "麦克风阵列 (Realtek(R) Audio)"                  -> "realtek(r) audio"
        """
        import re
        name = self.name
        # 括号里最后一个 (...) 的内容通常是真实设备名
        groups = re.findall(r'\(([^()]*)\)', name)
        key = groups[-1] if groups else name
        # HFP 端点的括号里可能还带 "Hands-Free%0;" 前缀，取分号后部分
        if ';' in key:
            key = key.split(';')[-1]
        key = key.strip().lower()
        # 去掉可能的通用尾注
        for junk in ('hands-free', 'hands free', '%0'):
            key = key.replace(junk, '')
        return key.strip()

    def _has_bluetooth_hfp_peer(self) -> bool:
        """扫描全部设备，看是否存在同名的蓝牙免提(HFP)端点"""
        if not self._device_key:
            return False
        try:
            import sounddevice as sd
            for d in sd.query_devices():
                n = (d.get('name') or '').lower()
                if not any(k in n for k in ('bthhfenum', 'hands-free', 'hands free')):
                    continue
                # 复用同样的 key 提取逻辑
                import re
                groups = re.findall(r'\(([^()]*)\)', d['name'])
                peer_key = groups[-1] if groups else d['name']
                if ';' in peer_key:
                    peer_key = peer_key.split(';')[-1]
                peer_key = peer_key.strip().lower()
                for junk in ('hands-free', 'hands free', '%0'):
                    peer_key = peer_key.replace(junk, '')
                if peer_key.strip() == self._device_key:
                    return True
        except Exception:
            pass
        return False
