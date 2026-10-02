# -*- coding: utf-8 -*-
"""图像读取的共用工具（Windows 中文路径 + 不整图解码）。

集中放在 core 是有意的：`tageditor/ops`（重命名、导出）与 `tageditor/image`
（编辑器、打标）都要用，放在任一侧都会造成跨包依赖。

## 为什么需要 imread_any：`cv2.imread` 读不了非 ASCII 路径

在 Windows 上实测（OpenCV 4.13.0）：

    cv2.imread('…\\中文图片.png', cv2.IMREAD_UNCHANGED)  ->  None
    cv2.imread('…\\ascii.png',  cv2.IMREAD_UNCHANGED)  ->  正常

而且 `cv2.imwrite` 更隐蔽：写非 ASCII 路径**返回 True 却根本没写文件**。
本项目的 `safe_filename()` 是**刻意保留中文**的，界面也全中文 ——
中文名图片是常态，于是「超清放大 / 背景移除」直接 400，
「批量透明转色底」更糟：判定不出 alpha 就当没有，最后报「完成，转换 0 张」，
**日志里一行都没有**。

`cv2.imdecode(np.fromfile(path, np.uint8), flags)` 实测可正常解码中文路径，
这是 OpenCV 官方推荐的 Windows 非 ASCII 路径写法。本模块统一提供它，
不要在业务代码里再直接调 `cv2.imread` / `cv2.imwrite`。

## 为什么要 probe/尺寸探测：别为读一个头部整图解码

`cv2.imread` 会解码整张图。仅仅为了判断「有没有 alpha 通道」或「宽高多少」
而在 4K 图上付 `h*w*4` 字节的内存与数百 ms，是不可接受的 ——
批量重命名会对每张图来一次，批量转色底甚至会在返回 SSE 之前把整个目录解码一遍
（几千张图时用户点下去要等几分钟才看到第一个进度）。PIL 的 `.size` / `.mode`
只读文件头，且同样支持中文路径。
"""
import struct

# PIL 里表示「带 alpha」的模式
_ALPHA_MODES = ('RGBA', 'LA', 'PA', 'RGBa', 'La', 'PA')


def imread_any(path, flags=None):
    """读图片，支持非 ASCII 路径。失败返回 None（与 `cv2.imread` 的失败语义一致）。

    `np.fromfile` 对目录/不存在的路径抛 `OSError` 而不是返回 None，
    故这里统一 try/except → None，调用方照旧只判 `img is None`。
    """
    import cv2
    import numpy as np
    if flags is None:
        flags = cv2.IMREAD_UNCHANGED
    try:
        buf = np.fromfile(path, dtype=np.uint8)
    except (OSError, ValueError):
        return None
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, flags)


def probe_size(path):
    """纯头部解析宽高（不解码像素）。识别不了返回 (0, 0)。

    从 `ops/file_ops._get_image_size` 的原实现搬来（PNG / JPEG / GIF / WEBP 四类），
    保留其中对**畸形 JPEG 段长度**的防护：`seg_len < 2` 时 `seek` 负偏移在 Python 里
    是「往回退」而非报错，会让解析器在同一位置打转直到文件尾。
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(32)

        # PNG: bytes 16-24 为 IHDR 的 width/height（大端 32 位）
        if head.startswith(b'\x89PNG\r\n\x1a\n'):
            if len(head) >= 24:
                return struct.unpack('>I', head[16:20])[0], struct.unpack('>I', head[20:24])[0]

        # JPEG: 逐段扫描 SOFx 标记取尺寸
        if head.startswith(b'\xff\xd8'):
            with open(path, 'rb') as f:
                f.read(2)  # 跳过 SOI
                while True:
                    marker = f.read(2)
                    if len(marker) < 2 or marker[0] != 0xff:
                        break
                    code = marker[1]
                    if 0xc0 <= code <= 0xcf and code not in (0xc4, 0xc8, 0xcc):
                        seg = f.read(7)  # 长度(2) + 精度(1) + 高(2) + 宽(2)
                        if len(seg) >= 7:
                            return struct.unpack('>H', seg[5:7])[0], struct.unpack('>H', seg[3:5])[0]
                    elif code in (0xd8, 0xd9):  # SOI/EOI
                        break
                    else:
                        seg_len_bytes = f.read(2)
                        if len(seg_len_bytes) < 2:
                            break
                        seg_len = struct.unpack('>H', seg_len_bytes)[0]
                        if seg_len < 2:
                            break
                        f.seek(seg_len - 2, 1)

        # GIF: bytes 6-10 为逻辑屏宽高（小端 16 位）
        if head[:6] in (b'GIF87a', b'GIF89a'):
            if len(head) >= 10:
                return struct.unpack('<H', head[6:8])[0], struct.unpack('<H', head[8:10])[0]

        # WEBP: RIFF + WebP，按 VP8/VP8L/VP8X chunk 解析
        if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
            chunk = head[12:16]
            if chunk == b'VP8 ' and len(head) >= 30:  # 有损
                return (struct.unpack('<H', head[26:28])[0] & 0x3fff,
                        struct.unpack('<H', head[28:30])[0] & 0x3fff)
            if chunk == b'VP8L' and len(head) >= 25:  # 无损
                bits = head[21:25]
                b0 = bits[0]
                w = 1 + (((b0 & 0x3f) << 8) | bits[1])
                h = 1 + (((bits[2] & 0x0f) << 10) | (bits[3] << 2) | (bits[2] >> 6))
                return w, h
            if chunk == b'VP8X' and len(head) >= 30:  # 扩展（含 alpha/动画）
                return (1 + (head[24] | (head[25] << 8) | (head[26] << 16)),
                        1 + (head[27] | (head[28] << 8) | (head[29] << 16)))
    except Exception:
        pass
    return 0, 0


def image_size(path):
    """宽高 (w, h)，**优先只读头部**，实在不行才整图解码兜底。识别不了返回 (0, 0)。

    顺序是有意的：
      1. PIL `.size` —— 只读文件头、支持中文路径、格式覆盖最广
      2. 纯头部解析 —— 无 PIL 或 PIL 认不出时
      3. `cv2.imread` —— 最后的兜底（对畸形/非常规文件最宽容）

    第 3 步**不要删**：它返回 (0, 0) 时调用方会把 `0x0` 写进新文件名，
    那是「静默的错误数据」，比读得慢严重得多。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
            if w and h:
                return int(w), int(h)
    except Exception:
        pass

    w, h = probe_size(path)
    if w and h:
        return w, h

    try:
        import cv2
        img = imread_any(path, cv2.IMREAD_REDUCED_COLOR_8)
        if img is not None and img.ndim >= 2:
            # REDUCED_COLOR_8 是 1/8 尺寸，还原回去
            return img.shape[1] * 8, img.shape[0] * 8
    except Exception:
        pass
    return 0, 0


def has_alpha(path):
    """是否含 alpha 通道。返回 True / False / **None（判定不了）**。

    三态是刻意的：调用方（批量转色底）拿到 None 时应当**退回整图解码判定**，
    而不是当 False 跳过 —— 否则一张真的有透明通道的图会被静默漏掉
    （症状正是「完成，转换 0 张」）。

    只看文件头，不解码像素。PIL 的 mode/info 同时覆盖了调色板 PNG 带 tRNS 的情形
    （它会给 `info['transparency']`），比自己解析 chunk 可靠。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.mode in _ALPHA_MODES:
                return True
            if 'transparency' in im.info:
                return True
            fmt = (im.format or '').upper()
            if fmt == 'PNG':
                return False          # PNG 的 alpha 只可能来自 mode/tRNS，上面都排除了
            if fmt in ('JPEG', 'JPG', 'BMP'):
                return False          # 这几类格式本身不支持 alpha
            if fmt == 'GIF':
                return False          # GIF 的透明是「索引透明」，转色底不适用
            return None               # WEBP 等：交给调用方解码判定
    except Exception:
        return None


# 送模型前的图片编码上限（与 config.py 的 _PROMPT_IMAGE_MAX_BYTES / _SIDE 同口径）。
# **两条 VLM 路径必须用同一套上限**：prompt_tool 一直有压缩，而 tagger 的
# VLM 描述路径早先是原图直送 —— 同一台后端、同一类任务，一条压一条不压。
# 按 Qwen-VL 系 28px patch 估：4096² ≈ 21k vision token，1536² ≈ 3.0k，
# 单图就差一万多 token（本地是 prefill 时间，云端是钱），
# 还会因为超服务端 body 上限而整张失败。
VLM_MAX_BYTES = 4194304   # 4MB
VLM_MAX_SIDE = 1536


def encode_for_vlm(data, ext, max_bytes=None, max_side=None, warnings=None):
    """把图片字节编码成可送 VLM 的 (base64_str, mime)。超限则缩到 max_side 并转 JPEG q90。

    `warnings` 是可选列表，压缩/失败时会追加一条中文说明（前端与模型看到同一条）。
    """
    import base64
    import io
    max_bytes = VLM_MAX_BYTES if max_bytes is None else max_bytes
    max_side = VLM_MAX_SIDE if max_side is None else max_side
    ext = (ext or 'png').lstrip('.').lower()
    mime = 'image/jpeg' if ext in ('jpg', 'jpeg') else f'image/{ext}'

    need_shrink = len(data) > max_bytes
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            if max(im.size) > max_side:
                need_shrink = True
    except Exception:
        pass
    if not need_shrink:
        return base64.b64encode(data).decode('ascii'), mime

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert('RGB')
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=90)
        out = buf.getvalue()
        if warnings is not None:
            warnings.append(f'图片已压缩后送模型（{len(data) // 1024}KB → {len(out) // 1024}KB）')
        return base64.b64encode(out).decode('ascii'), 'image/jpeg'
    except Exception as e:
        # 压缩失败时不再无条件按原图发送：need_shrink 为真说明这张图已经超字节上限
        # 或超最长边，原样送出会撞服务端的请求体上限，而报错文案是「请求过大」
        # 这种与图片无关的话，用户查不到原因。
        if warnings is not None:
            warnings.append(
                f'图片压缩失败（{e}），且原图 {len(data) // 1024}KB 可能超过服务端上限'
                f'（配置上限 {max_bytes // 1024 // 1024}MB），本次仍按原图发送')
        return base64.b64encode(data).decode('ascii'), mime
