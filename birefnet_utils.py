# -*- coding: utf-8 -*-
"""BiRefNet / ToonOut 背景移除推理工具。

来源：https://github.com/ZhengPeng7/BiRefNet + https://github.com/MatteoKartoon/BiRefNet
（ToonOut = BiRefNet 的动漫背景移除微调）。

依赖：torch、torchvision、transformers（trust_remote_code 加载模型类）、
kornia/einops/timm（模型类 birefnet.py 的 import 依赖，推理路径不全用到，但必须能 import）。

推理流程（数值与官方逐字一致）：
1. 预处理：PIL convert('RGB') → Resize((1024,1024)) 强制拉伸 → ToTensor → Normalize(ImageNet)
2. 推理：model(input)[-1].sigmoid() 取最后一层 + sigmoid 得到 0~1 mask
3. 后处理：mask resize 回原图尺寸 → putalpha 合成透明 PNG；或与底色 alpha 混合
"""
import numpy as np


# 模块级缓存：首次加载后常驻内存（模型加载耗时数秒，需复用）。按 base_dir+weights 失效。
_birefnet_cache = {'model': None, 'model_key': None}

# 预处理常量（官方数值，不能改）
_IMAGE_SIZE = (1024, 1024)
_IMAGENET_MEAN = [0.485, 0.456, 0.406]
_IMAGENET_STD = [0.229, 0.224, 0.225]


def _apply_transformers_compat_patch():
    """ToonOut notebook 的兼容性补丁：强制 PretrainedConfig.is_encoder_decoder 返回 False。

    新版 transformers 会把 BiRefNet 误判为 encoder-decoder 导致加载报错。
    幂等——重复调用无副作用（属性已被覆盖时直接返回）。
    """
    import transformers.configuration_utils as cu

    if getattr(cu.PretrainedConfig, '_toonout_patched', False):
        return
    original_getattribute = cu.PretrainedConfig.__getattribute__

    def patched_getattribute(self, key):
        if key == 'is_encoder_decoder':
            return False
        return original_getattribute(self, key)

    cu.PretrainedConfig.__getattribute__ = patched_getattribute
    cu.PretrainedConfig._toonout_patched = True


def load_birefnet_model(base_model_dir, toonout_weights_path):
    """加载 BiRefNet（ToonOut 权重），结果缓存到模块级全局变量。

    Args:
        base_model_dir: 本地 base 模型目录（含 birefnet.py + config.json + 权重），
                        提供 trust_remote_code 所需的模型类代码和结构。
        toonout_weights_path: ToonOut 微调权重 .pth 文件路径。

    Returns:
        torch.nn.Module（已 eval、已 to(device)、FP32）
    """
    global _birefnet_cache
    model_key = (base_model_dir, toonout_weights_path)
    if _birefnet_cache['model'] is not None and _birefnet_cache['model_key'] == model_key:
        return _birefnet_cache['model']

    import torch
    from transformers import AutoModelForImageSegmentation

    _apply_transformers_compat_patch()

    # 1. 加载 base 模型（获取结构 + config + 模型类代码）
    model = AutoModelForImageSegmentation.from_pretrained(
        base_model_dir, trust_remote_code=True
    )

    # 2. 覆盖为 ToonOut 微调权重（清洗 torch.compile/DataParallel 前缀）
    raw = torch.load(toonout_weights_path, map_location='cpu', weights_only=True)
    clean = {}
    for k, v in raw.items():
        if k.startswith('module._orig_mod.'):
            clean[k[len('module._orig_mod.'):]] = v
        elif k.startswith('module.'):
            clean[k[len('module.'):]] = v
        else:
            clean[k] = v
    model.load_state_dict(clean)
    model.float()  # ToonOut 权重可能存为 FP16，强制转 FP32 与输入类型一致（避免 conv2d 类型不匹配）

    # 3. 模式设置（FP32，与 ToonOut notebook 一致；CPU/GPU 自适应）
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model.to(device).eval()

    _birefnet_cache = {'model': model, 'model_key': model_key}
    print(f"[BiRefNet] 模型加载完成: base={base_model_dir}, weights={toonout_weights_path}, "
          f"device={device}, dtype=float32")
    return model


def remove_background(model, img_bgr, bg_color=None):
    """对单张图片执行背景移除。

    Args:
        model: load_birefnet_model 返回的模型（已 eval）。
        img_bgr: cv2 BGR numpy 数组（uint8），来自 cv2.imread(IMREAD_UNCHANGED)。
        bg_color: None 表示输出透明背景（RGBA）；否则为 (R,G,B) float32 数组，
                  将前景与该底色按 alpha 混合，输出 RGB。

    Returns:
        bg_color=None: 4 通道 BGRA numpy（uint8）
        bg_color 给定: 3 通道 BGR numpy（uint8）
    """
    import cv2
    import torch
    from PIL import Image
    from torchvision import transforms

    device = next(model.parameters()).device

    # cv2 BGR → PIL RGB
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(img_rgb).convert('RGB')

    # 预处理（官方数值：强制拉伸到 1024×1024，ImageNet 归一化）
    transform = transforms.Compose([
        transforms.Resize(_IMAGE_SIZE),
        transforms.ToTensor(),
        transforms.Normalize(_IMAGENET_MEAN, _IMAGENET_STD),
    ])
    input_tensor = transform(image).unsqueeze(0).to(device)

    # 推理：取多尺度输出的最后一层 + sigmoid
    with torch.no_grad():
        preds = model(input_tensor)[-1].sigmoid().cpu()
    pred = preds[0].squeeze()  # (H, W)，值域 0~1

    # mask 转回原图尺寸（默认双线性）
    mask_pil = transforms.ToPILImage()(pred).resize(image.size)
    mask = np.array(mask_pil, dtype=np.float32) / 255.0  # (H, W) 0~1

    h, w = mask.shape
    if bg_color is None:
        # 透明背景：BGRA，alpha = mask
        # bg_color 是 RGB 顺序，img_bgr 是 BGR；这里直接用 BGR 做前景通道
        alpha = (mask * 255.0).round().astype(np.uint8)
        result = np.dstack([img_bgr[:, :, :3], alpha])  # BGRA
        return result
    else:
        # 纯色底：前景与底色按 alpha 混合。bg_color=(R,G,B)，转 BGR
        bg_bgr = np.array([bg_color[2], bg_color[1], bg_color[0]], dtype=np.float32)
        alpha3 = mask[:, :, None]  # (H, W, 1)
        result = img_bgr[:, :, :3].astype(np.float32) * alpha3 + bg_bgr * (1.0 - alpha3)
        return result.round().astype(np.uint8)  # BGR
