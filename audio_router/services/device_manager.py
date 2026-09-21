"""音频设备管理模块"""
import sounddevice as sd
from typing import List, Optional, Dict
from ..models.audio_device import AudioDeviceInfo


class DeviceManager:
    """音频设备管理器"""

    # Host API 优先级（Windows上WASAPI最可靠，WDM-KS不支持回调模式需排除）
    HOSTAPI_PRIORITY = ['WASAPI', 'DirectSound', 'MME']

    # 不支持回调模式的 API（WDM-KS 只支持阻塞模式）
    UNSUPPORTED_CALLBACK_APIS = ['Windows WDM-KS', 'WDM-KS', 'ASIO']

    @staticmethod
    def get_hostapi_name(hostapi_index: int) -> str:
        """获取 host API 名称"""
        try:
            hostapis = sd.query_hostapis()
            if 0 <= hostapi_index < len(hostapis):
                return hostapis[hostapi_index]['name']
        except Exception:
            pass
        return f"HostAPI {hostapi_index}"

    @staticmethod
    def get_hostapi_priority(hostapi_index: int) -> int:
        """获取 host API 优先级数值（越小越优先）"""
        name = DeviceManager.get_hostapi_name(hostapi_index)
        for i, priority_name in enumerate(DeviceManager.HOSTAPI_PRIORITY):
            if priority_name.lower() in name.lower():
                return i
        return 99  # 未知 API 优先级最低

    @staticmethod
    def get_all_devices() -> List[AudioDeviceInfo]:
        """获取所有音频设备"""
        devices = sd.query_devices()
        # 预获取 host API 名称列表
        hostapi_names = {}
        try:
            hostapis = sd.query_hostapis()
            for idx, ha in enumerate(hostapis):
                hostapi_names[idx] = ha['name']
        except Exception:
            pass

        # 系统默认设备（PortAudio 视角）。注意：PortAudio 只给出一个设备索引，
        # 但该索引指向 MME 端点；同一个物理设备在 WASAPI/MME/DirectSound 下
        # 是不同索引。因此这里同时记录「默认索引」和「默认设备名」，
        # 后者用于跨 API 匹配同一台物理设备。
        default_in_idx, default_out_idx = -1, -1
        try:
            di, do = sd.default.device
            default_in_idx = di if di is not None else -1
            default_out_idx = do if do is not None else -1
        except Exception:
            pass

        result = []
        for i, d in enumerate(devices):
            info = AudioDeviceInfo(
                index=i,
                name=d['name'],
                max_input_channels=d['max_input_channels'],
                max_output_channels=d['max_output_channels'],
                default_samplerate=d['default_samplerate'],
                hostapi=d['hostapi'],
                hostapi_name=hostapi_names.get(d['hostapi'], '')
            )
            result.append(info)

        # 按名称把「系统默认」标记扩展到同一物理设备的所有 API 端点，
        # 这样 UI 上无论是 WASAPI 还是 MME 变体都能看出它是系统默认。
        def _norm(n: str) -> str:
            return AudioDeviceInfo(
                index=0, name=n, max_input_channels=0, max_output_channels=0,
                default_samplerate=0, hostapi=0).display_name.lower()

        default_in_name = _norm(result[default_in_idx].name) if 0 <= default_in_idx < len(result) else ''
        default_out_name = _norm(result[default_out_idx].name) if 0 <= default_out_idx < len(result) else ''

        for info in result:
            n = _norm(info.name)
            if info.is_input and default_in_name and n == default_in_name:
                info.is_system_default = True
            elif info.is_output and default_out_name and n == default_out_name:
                info.is_system_default = True

        return result

    @staticmethod
    def get_input_devices(sort_by_priority: bool = True, exclude_unsupported: bool = True) -> List[AudioDeviceInfo]:
        """获取所有输入设备（麦克风），默认按 host API 优先级排序并排除不支持回调的API"""
        devices = [d for d in DeviceManager.get_all_devices() if d.is_input]
        if exclude_unsupported:
            devices = [d for d in devices if not DeviceManager._is_unsupported_api(d.hostapi_name)]
        if sort_by_priority:
            devices.sort(key=lambda d: (DeviceManager.get_hostapi_priority(d.hostapi), d.index))
        return devices

    @staticmethod
    def get_output_devices(sort_by_priority: bool = True, exclude_unsupported: bool = True) -> List[AudioDeviceInfo]:
        """获取所有输出设备（扬声器），默认按 host API 优先级排序并排除不支持回调的API"""
        devices = [d for d in DeviceManager.get_all_devices() if d.is_output]
        if exclude_unsupported:
            devices = [d for d in devices if not DeviceManager._is_unsupported_api(d.hostapi_name)]
        if sort_by_priority:
            devices.sort(key=lambda d: (DeviceManager.get_hostapi_priority(d.hostapi), d.index))
        return devices

    @staticmethod
    def _is_unsupported_api(api_name: str) -> bool:
        """判断是否为不支持回调模式的音频 API"""
        if not api_name:
            return False
        name_lower = api_name.lower()
        for unsupported in DeviceManager.UNSUPPORTED_CALLBACK_APIS:
            if unsupported.lower() in name_lower:
                return True
        return False

    @staticmethod
    def get_default_input_device() -> Optional[AudioDeviceInfo]:
        """获取默认输入设备"""
        try:
            idx = sd.default.device[0]
            devices = DeviceManager.get_all_devices()
            for d in devices:
                if d.index == idx:
                    return d
        except Exception:
            pass
        return None

    @staticmethod
    def get_default_output_device() -> Optional[AudioDeviceInfo]:
        """获取默认输出设备"""
        try:
            idx = sd.default.device[1]
            devices = DeviceManager.get_all_devices()
            for d in devices:
                if d.index == idx:
                    return d
        except Exception:
            pass
        return None

    @staticmethod
    def find_system_default_input() -> Optional[AudioDeviceInfo]:
        """查找系统默认输入设备中「实际可用」的那个端点。

        Windows 的默认设备是「物理设备」级别的，而同一个物理设备在
        MME / DirectSound / WASAPI 下是不同的索引。PortAudio 的
        sd.default.device 通常指向 MME 端点，但 MME 端点有时打不开，
        所以这里在「同一物理设备的所有输入端点」里挑一个能真正开流的，
        优先级仍按 host API 排序（WASAPI 优先）。
        """
        try:
            devices = DeviceManager.get_input_devices(sort_by_priority=True)
        except Exception:
            return None

        cands = [d for d in devices if d.is_system_default]
        for d in cands:
            if DeviceManager._test_input_device(d.index, d.default_samplerate,
                                                min(d.max_input_channels, 2)):
                return d
        return cands[0] if cands else None

    @staticmethod
    def find_system_default_output() -> Optional[AudioDeviceInfo]:
        """查找系统默认输出设备中「实际可用」的那个端点（同 find_system_default_input）"""
        try:
            devices = DeviceManager.get_output_devices(sort_by_priority=True)
        except Exception:
            return None

        cands = [d for d in devices if d.is_system_default]
        for d in cands:
            if DeviceManager._test_output_device(d.index, d.default_samplerate,
                                                 min(d.max_output_channels, 2)):
                return d
        return cands[0] if cands else None

    @staticmethod
    def _is_bluetooth_device(name: str) -> bool:
        """判断是否为蓝牙设备"""
        if not name:
            return False
        name_lower = name.lower()
        bluetooth_keywords = [
            'hands-free', 'hands free', 'bthhfenum',
            'bluetooth', '蓝牙',
            'dubuds', 'redmi buds', 'airpods', 'qcy',
            'soundpeats', 'sabbat', 'tronsmart', 'haylou',
            'fiil', 'soundcore', 'jbl', 'sony wh-',
        ]
        for kw in bluetooth_keywords:
            if kw in name_lower:
                return True
        # 名称中包含"耳机"且不是"扬声器"的，大概率是蓝牙耳机
        # （注意：有些蓝牙耳机会在输出设备里显示为"耳机"而不是"扬声器"）
        if '耳机' in name and '扬声器' not in name:
            return True
        return False

    @staticmethod
    def find_bluetooth_input() -> Optional[AudioDeviceInfo]:
        """查找蓝牙耳机麦克风（优先选择WASAPI下的HFP免提模式）"""
        input_devices = DeviceManager.get_input_devices(sort_by_priority=True)

        # 第一轮：找有明确蓝牙标识 + HFP 特征的设备
        for d in input_devices:
            name_lower = d.name.lower()
            if DeviceManager._is_bluetooth_device(d.name) and \
               ('hands-free' in name_lower or 'bthhfenum' in name_lower or 'hands free' in name_lower):
                if DeviceManager._test_input_device(d.index, d.default_samplerate, d.max_input_channels):
                    return d

        # 第二轮：找名称含"耳机"的设备（可能是蓝牙）
        for d in input_devices:
            if '耳机' in d.name:
                if DeviceManager._test_input_device(d.index, d.default_samplerate, d.max_input_channels):
                    return d

        # 第三轮：所有蓝牙设备（不限制HFP）
        for d in input_devices:
            if DeviceManager._is_bluetooth_device(d.name):
                if DeviceManager._test_input_device(d.index, d.default_samplerate, d.max_input_channels):
                    return d

        return None

    @staticmethod
    def find_bluetooth_output() -> Optional[AudioDeviceInfo]:
        """查找蓝牙耳机输出（优先选择WASAPI下的立体声A2DP模式）"""
        output_devices = DeviceManager.get_output_devices(sort_by_priority=True)

        # 第一轮：立体声蓝牙设备（A2DP）
        for d in output_devices:
            name_lower = d.name.lower()
            if DeviceManager._is_bluetooth_device(d.name) and \
               'hands-free' not in name_lower and 'bthhfenum' not in name_lower and \
               d.max_output_channels >= 2:
                if DeviceManager._test_output_device(d.index, d.default_samplerate, d.max_output_channels):
                    return d

        # 第二轮：HFP 模式输出
        for d in output_devices:
            name_lower = d.name.lower()
            if DeviceManager._is_bluetooth_device(d.name) and \
               ('hands-free' in name_lower or 'bthhfenum' in name_lower):
                if DeviceManager._test_output_device(d.index, d.default_samplerate, d.max_output_channels):
                    return d

        # 第三轮：名称含"耳机"的设备
        for d in output_devices:
            if '耳机' in d.name and d.max_output_channels >= 2:
                if DeviceManager._test_output_device(d.index, d.default_samplerate, d.max_output_channels):
                    return d

        return None

    @staticmethod
    def find_hdmi_output() -> Optional[AudioDeviceInfo]:
        """查找 HDMI / DisplayPort 输出设备（显示器音频）。

        注意：Windows 上 HDMI 音频设备名通常**不含** "hdmi"，
        而是以显卡厂商的音频驱动名出现（如「英特尔(R) 显示器音频」）。
        因此委托给 AudioDeviceInfo.is_display_audio 做关键词匹配。
        """
        output_devices = DeviceManager.get_output_devices(sort_by_priority=True)

        # 第一轮：明确的显示器音频设备，且排除虚拟声卡
        for d in output_devices:
            if d.is_display_audio and not DeviceManager._is_virtual_device(d.name):
                if DeviceManager._test_output_device(d.index, d.default_samplerate, d.max_output_channels):
                    return d

        # 第二轮：放宽 —— 只要看起来是显示器音频就接受
        for d in output_devices:
            if d.is_display_audio:
                if DeviceManager._test_output_device(d.index, d.default_samplerate, d.max_output_channels):
                    return d

        return None

    @staticmethod
    def _is_virtual_device(name: str) -> bool:
        """是否为虚拟声卡（VB-Audio / CABLE / 各类 virtual 驱动）"""
        if not name:
            return False
        name_lower = name.lower()
        virtual_keywords = [
            'vb-audio', 'vb audio', 'cable input', 'cable output',
            'virtual cable', 'virtual audio', 'voicemeeter',
            'virtual', 'vac', 'loopback', '声音映射器', 'sound mapper',
            '主声音', 'primary sound',
        ]
        return any(kw in name_lower for kw in virtual_keywords)

    @staticmethod
    def find_builtin_output() -> Optional[AudioDeviceInfo]:
        """查找内置扬声器输出"""
        output_devices = DeviceManager.get_output_devices(sort_by_priority=True)
        # 优先找"扬声器"且非蓝牙、非虚拟的
        for d in output_devices:
            if '扬声器' in d.name and not DeviceManager._is_bluetooth_device(d.name) \
               and 'virtual' not in d.name.lower() and 'vb-audio' not in d.name.lower():
                if DeviceManager._test_output_device(d.index, d.default_samplerate, d.max_output_channels):
                    return d
        # 其次找 realtek 等内置声卡
        for d in output_devices:
            if d.device_type == 'built-in':
                if DeviceManager._test_output_device(d.index, d.default_samplerate, d.max_output_channels):
                    return d
        return None

    @staticmethod
    def get_device_by_index(index: int) -> Optional[AudioDeviceInfo]:
        """根据索引获取设备"""
        devices = DeviceManager.get_all_devices()
        for d in devices:
            if d.index == index:
                return d
        return None

    @staticmethod
    def reload_portaudio() -> bool:
        """重载 PortAudio，使蓝牙等「运行期新接入」的设备能被枚举到。

        为什么需要这一步：
        PortAudio 在首次初始化时建立一份设备表快照，之后 sd.query_devices()
        一直读这份快照。蓝牙耳机若是在程序**启动之后**才连上（非常常见：
        先开程序、再连耳机），旧快照里根本没有它的端点 →
        无论点多少次「刷新」都看不到蓝牙设备。
        terminate + initialize 会重新扫描系统音频端点。

        安全性：调用前必须确保没有任何音频流在运行，否则会打断播放。
        本方法仅改变全局 PortAudio 状态，失败时静默返回 False。
        """
        try:
            sd._terminate()
            sd._initialize()
            # 重新初始化后，默认设备索引可能已变化，需刷新缓存
            sd.default.reset()
            return True
        except Exception:
            return False

    @staticmethod
    def refresh(reload: bool = True) -> List[AudioDeviceInfo]:
        """刷新设备列表。

        reload=True（默认）会先重载 PortAudio，以便发现运行期新接入的
        蓝牙耳机等设备。若当前有音频流在运行，调用方应传 reload=False，
        避免打断正在播放的声音。
        """
        if reload:
            DeviceManager.reload_portaudio()
        return DeviceManager.get_all_devices()

    @staticmethod
    def _test_input_device(device_index: int, samplerate: float, channels: int) -> bool:
        """测试输入设备是否可用（尝试打开流再立即关闭）"""
        try:
            sr = int(samplerate) if samplerate else 48000
            ch = min(channels, 2)
            stream = sd.InputStream(device=device_index, channels=ch,
                                    samplerate=sr, blocksize=64, dtype='float32')
            stream.close()
            return True
        except Exception:
            return False

    @staticmethod
    def _test_output_device(device_index: int, samplerate: float, channels: int) -> bool:
        """测试输出设备是否可用（尝试打开流再立即关闭）"""
        try:
            sr = int(samplerate) if samplerate else 48000
            ch = min(channels, 2)
            stream = sd.OutputStream(device=device_index, channels=ch,
                                     samplerate=sr, blocksize=64, dtype='float32')
            stream.close()
            return True
        except Exception:
            return False

    @staticmethod
    def filter_usable_devices(devices: List[AudioDeviceInfo], is_input: bool) -> List[AudioDeviceInfo]:
        """过滤出真正可用的设备"""
        result = []
        for d in devices:
            if is_input:
                if DeviceManager._test_input_device(d.index, d.default_samplerate, d.max_input_channels):
                    result.append(d)
            else:
                if DeviceManager._test_output_device(d.index, d.default_samplerate, d.max_output_channels):
                    result.append(d)
        return result
