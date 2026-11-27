"""Fail-closed transport boundary for image-free observation experiments."""
from pathlib import PurePosixPath

class NoScreenshotError(RuntimeError):
    pass

def _sample_carries_image(sample):
    """UDS Sample 的图像证据：解出的字节数、识别出的格式/尺寸、响应里的 image 键。"""
    if getattr(sample, 'image_bytes', 0) or getattr(sample, 'image_fmt', None):
        return True
    if getattr(sample, 'image_w', None) or getattr(sample, 'image_h', None):
        return True
    return 'image' in (getattr(sample, 'response_keys', None) or ())


class NoScreenshotRequest:
    """Allow known non-image operations; reject implicit screenshots before dispatch."""
    _UI = frozenset({'key', 'swipe', 'tap', 'type', 'app_launch', 'app_terminate', 'open_url'})
    _READ = frozenset({'app_list', 'app_foreground', 'file_get', 'file_put'})

    def __init__(self, request):
        self.request = request
        self.attempted = 0
        self.completed = 0
        self.rejected = 0
        self.commands = []

    def __call__(self, message, **kwargs):
        self.attempted += 1
        command = message.get('t')
        self.commands.append(command)
        allowed = command in self._READ or (command in self._UI and message.get('screen') is False)
        if 'screen' in message and message['screen'] is not False:
            allowed = False
        if command in {'file_get', 'file_put'}:
            suffix = PurePosixPath(str(message.get('path', ''))).suffix
            allowed = allowed and suffix in ({'.json', '.plist'} if command == 'file_get' else {'.json', '.plist', '.dylib'})
        if not allowed:
            self.rejected += 1
            raise NoScreenshotError(f'forbidden image-free request: {command}')
        result = self.request(message, **kwargs)
        # UDS request 返回 (response, sample)。sample 恒非 None（它是 round-trip 计时
        # 记录），但它同时记着这次往返到底有没有解出图像；只查 result[0] 会漏掉
        # parse_image 关掉、响应里 image 键被剥掉、而字节仍然过了线的情况。
        sample = result[1] if isinstance(result, tuple) and len(result) > 1 else None
        if sample is not None and _sample_carries_image(sample):
            self.rejected += 1
            raise NoScreenshotError('image-bearing transport sample in image-free collection')
        response = result[0] if isinstance(result, tuple) else result
        def has_image(value):
            if isinstance(value, dict):
                return 'image' in value or any(has_image(v) for v in value.values())
            return isinstance(value, list) and any(has_image(v) for v in value)
        if has_image(response):
            self.rejected += 1
            raise NoScreenshotError('image-bearing response in image-free collection')
        self.completed += 1
        return result

    def receipt(self):
        return {'attempted': self.attempted, 'completed': self.completed,
                'rejected': self.rejected, 'commands': list(self.commands)}


def checked_lock_state(read):
    result = read()
    if type(result) is not bool:
        raise NoScreenshotError('lock state must be a measured boolean')
    return result
