# -*- coding: utf-8 -*-
"""环境自检 —— 装完依赖后跑一次，确认哪些功能可用、哪些会降级。

设计原则：**只报告，不改动环境**。安装脚本不该偷偷替用户做决定（比如为了对齐
版本而降级一个已经能用的包），所以这里把「能跑 / 会降级 / 缺什么」如实说出来，
由用户决定要不要处理。

用法：
    python setup_check.py

退出码（调用方约定，别退回「恒 0」）：
    0 = 没有发现问题；1 = 至少记录了一条 issue（环境有缺件或降级）。
    setup.bat 的第 6 步靠这个区分「装完了，没毛病」和「装完了，但有降级」——
    恒返回 0 时那个兜底分支永远不会命中，自检发现的问题也传不出去。
"""
import importlib.metadata as md
import os
import sys

OK, WARN, BAD = '  [OK]', '  [注意]', '  [缺失]'
issues = []


def ver(pkg):
    try:
        return md.version(pkg)
    except Exception:
        return None


def has(mod):
    """能否导入该模块。

    basicsr 特殊处理：它 import 时会扫描 basicsr.data.*，其中 degradations.py 用了
    torchvision 0.20+ 已移除的 functional_tensor 子模块。应用侧靠
    realesrgan_utils 顶部的兼容垫片解决（先 import 它再 import basicsr）。
    这里必须复现同样的顺序，否则会误报「basicsr 缺失」——而它其实是好的。
    """
    try:
        if mod == 'basicsr':
            import tageditor.image.realesrgan_utils
        __import__(mod)
        return True
    except Exception:
        return False


# onnxruntime 加载失败时会往 stderr 直接打一大段英文日志（不是 Python 异常，
# 抓不到也屏不掉，只有 SessionOptions.log_severity_level 能降级）。这里提前设好，
# 免得它把自检输出冲得看不见。
os.environ.setdefault('ORT_LOG_SEVERITY_LEVEL', '4')

print('=' * 64)
print(' TagEditor_Web 环境自检')
print('=' * 64)
print('  Python: %s' % sys.version.split()[0])
print('  环境:   %s' % sys.prefix)
print()

# ── 核心依赖（缺了应用起不来）─────────────────────────────────────────
print('核心依赖')
CORE = [('flask', 'flask'), ('requests', 'requests'), ('numpy', 'numpy'),
        ('pandas', 'pandas'), ('cv2', 'opencv-python'),
        ('openai', 'openai'), ('PIL', 'Pillow'), ('json_repair', 'json-repair')]
for mod, pkg in CORE:
    if has(mod):
        print('%s %-16s %s' % (OK, pkg, ver(pkg) or ''))
    else:
        print('%s %-16s 应用无法启动' % (BAD, pkg))
        issues.append('缺少核心依赖 %s' % pkg)

# ── 可选功能：缺了只影响对应功能 ─────────────────────────────────────
print()
print('可选功能（缺哪个就少哪个功能，其余照常）')

# WD14 打标
# **检查顺序刻意放在 torch 之后**：onnxruntime 1.26 加载 CUDA provider 时会先把它
# 自带的 cuDNN 9 DLL 灌进进程，之后再 import torch 就撞 WinError 127（cudnn_cnn64_9.dll
# 加载失败）——那是**检查顺序造成的假缺失**，不是 torch 坏了。应用本体（tagger.py 等）
# 的懒加载顺序是 torch 在前、onnxruntime 在后，两者共存正常（实测复现过两种顺序）。
# 误报会让用户以为超清放大/背景移除坏了，而实际上它们都能用。
ort_v = ver('onnxruntime-gpu') or ver('onnxruntime')

# torch 先检查（顺序原因见上）
if has('torch'):
    import torch
    if torch.cuda.is_available():
        print('%s %-16s %s | CUDA %s | %s'
              % (OK, 'torch', torch.__version__, torch.version.cuda,
                 torch.cuda.get_device_name(0)))
    else:
        print('%s %-16s %s（无 CUDA，超分/抠图会很慢）'
              % (WARN, 'torch', torch.__version__))
        issues.append('torch 无 CUDA')
else:
    print('%s %-16s 装它才有超清放大 / 背景移除' % (BAD, 'torch'))

if has('onnxruntime'):
    try:
        import onnxruntime as ort
        provs = ort.get_available_providers()
        cuda_ok = 'CUDAExecutionProvider' in provs
        # 光「可用」不够：真正加载时可能因缺 cuDNN 而回落到 CPU
        settled = None
        mp = os.path.join('models', 'wd-eva02-large-tagger-v3', 'model.onnx')
        probed = cuda_ok and os.path.isfile(mp)
        if probed:
            try:
                so = ort.SessionOptions()
                so.log_severity_level = 4          # 静音 onnxruntime 的 stderr 噪音
                s = ort.InferenceSession(mp, so, providers=['CUDAExecutionProvider',
                                                            'CPUExecutionProvider'])
                settled = s.get_providers()[0]
            except Exception:
                settled = None
        if settled and settled == 'CUDAExecutionProvider':
            print('%s %-16s %s（WD14 走 GPU）' % (OK, 'onnxruntime', ort_v))
        elif not cuda_ok:
            print('%s %-16s %s（仅 CPU 版，WD14 可用但慢）' % (WARN, 'onnxruntime', ort_v))
            print('         这是 CPU 版 onnxruntime；requirements.txt 钉的是 GPU 版：')
            print('         onnxruntime-gpu==1.26.0 —— 装 GPU 版才会走显卡。')
            issues.append('onnxruntime 是 CPU 版（WD14 较慢）')
        elif not probed:
            print('%s %-16s %s（模型不在，CUDA 未经验证）' % (WARN, 'onnxruntime', ort_v))
            print('         缺 %s，没法真去建一个 CUDA session 验证。' % mp)
            print('         「支持 CUDA」此时只是 onnxruntime 的自述，装好模型后重跑本脚本。')
            issues.append('缺少 WD14 模型文件（CUDA provider 未经验证）')
        else:
            print('%s %-16s %s' % (WARN, 'onnxruntime', ort_v))
            print('         WD14 会回落到 CPU（慢约 10 倍）。onnxruntime 声明支持 CUDA，')
            print('         但实际加载 GPU provider 时失败（依赖的 cuDNN 库没就位）。')
            print('         不影响正确性，只是慢。requirements.txt 钉的是')
            print('         onnxruntime-gpu==1.26.0，它需要 **cuDNN 9.x**（不是 8.x）；')
            print('         让 onnxruntime 找到匹配的 cuDNN 9.x 的常见做法是 pip install')
            print('         nvidia-cudnn-cu12 nvidia-cublas-cu12（这两个包会把 dll 放到')
            print('         onnxruntime 会去查找的位置）。本脚本不自动装，先确认再动。')
            issues.append('onnxruntime 实际走 CPU（WD14 较慢）')
    except Exception as e:
        print('%s %-16s 已装但导入失败: %s' % (WARN, 'onnxruntime', e))
        issues.append('onnxruntime 已装但导入失败（WD14 不可用）')
else:
    print('%s %-16s 装它才有 WD14 自动打标' % (BAD, 'onnxruntime-gpu'))

for mod, pkg, why in [('basicsr', 'basicsr', '超清放大'),
                      ('transformers', 'transformers', '背景移除'),
                      ('kornia', 'kornia', '背景移除'),
                      ('einops', 'einops', '背景移除'),
                      ('timm', 'timm', '背景移除'),
                      ('sam2', 'sam2', '描点抠图'),
                      ('pyarrow', 'pyarrow', '构建标签库'),
                      ('dateutil', 'python-dateutil', '日期解析')]:
    if has(mod):
        print('%s %-16s %s' % (OK, pkg, ver(pkg) or ''))
    else:
        print('%s %-16s 缺它 %s 用不了' % (BAD, pkg, why))
        issues.append('缺少 %s（%s）' % (pkg, why))

# ── 文件与配置 ────────────────────────────────────────────────────────
print()
print('文件与配置')
for path, desc, need in [('.env', '配置文件', True),
                         ('prompts/vlm_caption.txt', 'VLM 描述提示词', True),
                         ('prompts/llm_general.txt', '翻译提示词', False),
                         ('prompts/prompt_planner.txt', '提示词优化器', False),
                         ('data/danbooru_tags.db', '标签数据库', False)]:
    if os.path.exists(path):
        print('%s %-28s %s' % (OK, path, desc))
    else:
        print('%s %-28s %s' % (BAD if need else WARN, path, desc))
        if need:
            issues.append('缺少 %s' % path)

if not os.path.exists('.env'):
    print()
    print('  提示：复制 .env.example 为 .env 并填写模型地址与密钥')

# ── 模型文件 ──────────────────────────────────────────────────────────
print()
print('模型文件（不用的功能可以不管）')
MODELS = [('models/RealESRGAN_x4plus_anime_6B.pth', '超清放大'),
          ('models/birefnet-base', '背景移除 base'),
          ('models/toonout.pth', '背景移除权重'),
          ('models/sam2.1_hiera_base_plus.pt', 'SAM2 描点抠图'),
          ('models/wd-eva02-large-tagger-v3/model.onnx', 'WD14 打标')]
for path, desc in MODELS:
    if os.path.exists(path):
        sz = (os.path.getsize(path) / 1024 / 1024) if os.path.isfile(path) else 0
        extra = ' (%.0f MB)' % sz if sz else ''
        print('%s %-42s %s%s' % (OK, path, desc, extra))
    else:
        print('%s %-42s %s 不可用' % (WARN, path, desc))

# ── 总结 ──────────────────────────────────────────────────────────────
print()
print('=' * 64)
if issues:
    print(' 有 %d 处需要注意：' % len(issues))
    for i in issues:
        print('   - %s' % i)
    print()
    print(' 只影响列出的功能，其余可以正常用。')
else:
    print(' 环境完整，所有功能可用。')
print('=' * 64)

# 退出码是给调用方（setup.bat 第 6 步）看的：1 = 有降级，0 = 干净。
# 自检只报告不改环境，所以「有 issues」绝不能静默变成成功。
sys.exit(1 if issues else 0)
