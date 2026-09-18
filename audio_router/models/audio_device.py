"""音频设备信息模型"""
from dataclasses import dataclass


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
        """完整显示名称（含 host API 标签）"""
        api_label = self._get_short_hostapi_label()
        return f"{self.display_name} [{api_label}]"

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
        """是否为蓝牙设备"""
        return self.device_type == 'bluetooth' or 'bthhfenum' in self.name.lower() \
            or 'hands-free' in self.name.lower() or 'hands free' in self.name.lower() \
            or 'bluetooth' in self.name.lower()
