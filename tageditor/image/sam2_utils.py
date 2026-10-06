# -*- coding: utf-8 -*-
"""SAM2 交互式分割推理工具（描点 → mask），与 ToonOut 组合成三步式抠图。

## 为什么需要它：ToonOut 不可交互

`birefnet_utils.remove_background` 是**非交互式**的整图分割 —— 整图进、alpha 出，
架构里没有任何 prompt 输入（`BiRefNet.forward(self, x)` 只吃一张图）。所以
「只抠某个人」「把误留的路人排除掉」这类需求在 ToonOut 上**无解**，不是配置能开出来的。

SAM2 正好补上这一半：用户点几下给出目标，模型返回 mask。两者组合的分工是
**SAM2 决定「抠谁」（用户控制），ToonOut 决定「边缘多精细」（模型能力）**：

    alpha_out = ToonOut(img) * gate(SAM2_mask)      # 见 apply_gate

## 设计要点（改之前先读）

1. **embedding 缓存是整个交互体验的关键**。SAM2 分成两半：图像编码（重，~0.3~1s）
   与 prompt 解码（轻，~30ms）。用户每点一个锚点都要重跑一次编码的话，交互是卡死的；
   缓存住 embedding 之后每次点击都是实时的。缓存键必须是
   `(路径, mtime, 大小)` —— 图片被「保存」覆盖后 mtime 会变，自动失效
   （与 `llm_pipeline._cooc_cache`、`translation._tag_groups_cache` 同款口径）。

2. **权重与 config 名必须成对**。`sam2.1_hiera_b+.yaml` 这个 `+` 在 shell/URL 里
   需要转义，但作为 Python 字符串传给 `build_sam2` 是安全的。写错 config 名的表现是
   `KeyError` 而不是「加载了错误的模型」，还算好查；但 **ckpt 与 config 不匹配**
   （如 b+ 的 config 配 small 的权重）只会在 `load_state_dict` 时报一堆 missing keys，
   容易误判成「权重下载不完整」。

3. **`multimask_output=True` 是刻意的**。SAM2 对单点会返回 3 个候选（整体/局部/子部件，
   如「整个人」「上半身」「头」）。默认取 score 最高的那个，但把候选也传给前端让用户
   改选，比让用户重新点一遍更省事 —— 这正是「用户调整锚点」的另一半价值。
"""
import numpy as np
import logging


log = logging.getLogger(__name__)

# 模块级缓存：模型 + predictor（按 (config, ckpt) 失效）。首次加载数秒，必须复用。
_sam2_cache = {'model': None, 'predictor': None, 'model_key': None}

# image embedding 缓存：{cache_key: (embedding_npz_bytes, feat_shape)}。
# 只保留**最近一张**（单用户本地工具，同时只会描一张图）。
# 存 npz 字节而不是 tensor 对象：predictor 内部状态是私有的，
# 用 `predictor._features` 之类会随 sam2 版本变化；官方提供
# `set_image` / 无公开的 set_features 序列化接口，故这里只缓存
# 「已加载过哪张图」的判定依据，避免重复 set_image。
_image_key = None


def load_sam2_model(config_name, ckpt_path, device=None):
    """加载 SAM2 模型 + predictor，结果缓存到模块级全局变量。

    Args:
        config_name: sam2 内置 config 名，如 'configs/sam2.1/sam2.1_hiera_b+.yaml'
                     （相对 sam2 包目录，build_sam2 自己解析）。
        ckpt_path: 权重 .pt 路径（本地）。
        device: 'cuda' / 'cpu'；None 时自动选。

    Returns:
        SAM2ImagePredictor（已 eval）
    """
    global _sam2_cache
    model_key = (config_name, ckpt_path)
    if _sam2_cache['predictor'] is not None and _sam2_cache['model_key'] == model_key:
        return _sam2_cache['predictor']

    import os
    import torch
    from sam2.build_sam import build_sam2
    from sam2.sam2_image_predictor import SAM2ImagePredictor

    if not os.path.isfile(ckpt_path):
        raise FileNotFoundError(
            f"SAM2 权重不存在: {ckpt_path}\n"
            f"下载：https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_base_plus.pt")

    if device is None:
        device = 'cuda' if torch.cuda.is_available() else 'cpu'

    model = build_sam2(config_name, ckpt_path, device=device)
    predictor = SAM2ImagePredictor(model)

    _sam2_cache = {'model': model, 'predictor': predictor, 'model_key': model_key}
    log.info("[SAM2] 模型加载完成: config=%s, ckpt=%s, device=%s",
             config_name, ckpt_path, device)
    return predictor


def set_image(predictor, img_bgr, cache_key=None):
    """把图片送入 predictor（算 image embedding）。同一张图重复调用会被跳过。

    `cache_key` 传 `(路径, mtime, 大小)` 之类的三元组；传 None 表示每次都重算。
    跳过判定的意义：用户连点锚点时每次都重算编码（~0.3~1s）会让交互不可用。

    Args:
        img_bgr: cv2 BGR numpy 数组（uint8）。内部转 RGB —— SAM2 是 RGB 模型，
                 直接喂 BGR 会让 mask 明显偏移（红蓝通道互换后的语义不同）。
    """
    global _image_key
    if cache_key is not None and _image_key == cache_key:
        return
    import cv2
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    predictor.set_image(img_rgb)
    _image_key = cache_key


def predict_mask(predictor, points, labels):
    """按锚点预测 mask。

    Args:
        points: [[x, y], ...] **原图像素坐标**（不是显示坐标；调用方负责换算）。
        labels: [1, 0, ...]，1 = 正点（要保留），0 = 负点（要排除）。

    Returns:
        [(mask_hw_bool, score_float), ...]，按 score 降序；失败返回 []。
        mask 是原图分辨率的 bool 数组。
    """
    if not points:
        return []
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    lbl = np.asarray(labels, dtype=np.int32).reshape(-1)
    if pts.shape[0] != lbl.shape[0]:
        raise ValueError(f'锚点数({pts.shape[0]})与标签数({lbl.shape[0]})不一致')

    masks, scores, _ = predictor.predict(
        point_coords=pts,
        point_labels=lbl,
        # 单点给 3 个候选：整体 / 局部 / 子部件。默认取最高的，
        # 前端也让用户改选（比重新点一遍省事）
        multimask_output=True,
    )
    out = []
    for m, s in zip(masks, scores):
        out.append((np.asarray(m, dtype=bool), float(s)))
    out.sort(key=lambda t: -t[1])
    return out


def mask_to_png_bytes(mask):
    """bool mask → 8-bit 灰度 PNG（255 = 前景）。前端叠加预览用。

    走 `imencode` 而不是 `cv2.imwrite`：后者按扩展名推断编码器且对非 ASCII
    路径会静默失败（见 core/image_io.py 的说明）。这里本来就只要字节。
    """
    import cv2
    arr = (np.asarray(mask).astype(np.uint8)) * 255
    ok, buf = cv2.imencode('.png', arr, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    if not ok:
        raise RuntimeError('mask PNG 编码失败')
    return buf.tobytes()


def apply_gate(alpha, gate_mask, mode='include', dilate_px=20, feather_px=10):
    """把 SAM2 mask 门控到 ToonOut 的 alpha 上（前景保留 / 背景移除的裁剪）。

    **`dilate_px` 不是可选的美化项，删掉会让结果比不门控更差**：
    SAM2 的边缘比 ToonOut 保守（它给的是「物体大致范围」），直接相乘会把
    SAM2 边界之外那圈发丝/衣角**整圈削掉** —— 而不门控时那些是保留下来的。
    外扩 + 羽化让交界平滑过渡，ToonOut 的软边缘在过渡带内仍完全生效。

    Args:
        alpha: (H, W) float32/uint8，ToonOut 输出的 0~1 软 alpha。
        gate_mask: (H, W) bool，SAM2 输出的目标 mask（原图分辨率）。
        mode: 'include' → 保留 mask 内（alpha * gate）；
              'exclude' → 排除 mask 内（alpha * (1 - gate)）。
        dilate_px: 外扩半径（原图像素）。
        feather_px: 羽化半径（高斯 σ ≈ feather_px / 2）。

    Returns:
        与输入同尺寸的 float32 alpha（0~1）。
    """
    import cv2

    a = np.asarray(alpha)
    if a.dtype == np.uint8:
        a = a.astype(np.float32) / 255.0
    else:
        a = a.astype(np.float32)
    if a.max() > 1.0:
        # 误传 0~255 的 float：按 255 归一（比静默截断成 1.0 安全）
        a = a / 255.0

    # **bool → uint8 必须先乘 255**：`np.bool_.astype(np.uint8)` 给的是 0/1，
    # 后面除以 255 会把整个 gate 压成约 0.004 —— 门控后 alpha 全灭，看起来像
    # 「模型坏了」，而日志里什么都没有（实测踩到过）。
    g = np.asarray(gate_mask)
    g = (g.astype(np.uint8) * 255) if g.dtype == bool else g.astype(np.uint8)
    if g.shape[:2] != a.shape[:2]:
        g = cv2.resize(g, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_NEAREST)

    if dilate_px > 0:
        k = int(dilate_px) * 2 + 1
        g = cv2.dilate(g, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    if feather_px > 0:
        # 高斯核必须是奇数；σ 取半径的一半是常见的羽化口径
        k = int(feather_px) * 2 + 1
        g = cv2.GaussianBlur(g, (k, k), feather_px / 2.0)

    gate = g.astype(np.float32) / 255.0
    if mode == 'exclude':
        gate = 1.0 - gate
    out = a * gate
    return np.clip(out, 0.0, 1.0)
