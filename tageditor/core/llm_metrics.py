# -*- coding: utf-8 -*-
"""模型调用的**耗时与 token 用量**记录（可观测性的唯一出处）。

## 为什么需要它

改造前整个项目**没有任何运行时模型指标**：全仓 0 处读 `response.usage`、
0 处 `time.perf_counter()` 计时，而 `batch_size=8`、`240 + 120*attempt` 超时、
`max_attempts=5` 这些决策的依据，全是注释里写死的历史数字：

    llm_pipeline.py: 「batch=8 约 1000 token @10.86 tok/s → 每批约 94 秒」
    translation.py: 「135 token @10.86 tok/s —— 剩下约 82 秒是每次请求的固定开销」

这些数字冻结在某一台机器、某一次测量上，而端点与模型随时会换：注释描述的是
「远程隧道 + RTX 4060 上的 Qwen3.5-9B Q8_0」，而 `.env` 现在是
`LLM_TEXT_API_URL=http://127.0.0.1:8050/` + `model=deepseek-flash`。
CLAUDE.md 自己写着「**先查清那 82 秒是什么再考虑调优**」——没有指标就无从查起。

`usage` 是 OpenAI 兼容端点**本来就返回**的字段，读它不额外花钱，所以这里只做
「读出来 + 打成一行」，不做任何聚合统计。

## 日志格式

固定为一行、字段用空格分隔、中文键名，方便直接 `Select-String` / `grep` 聚合：

    [LLM用量] label=批量翻译/general model=deepseek-flash 耗时=12.3s in=1355 out=420 finish=stop

失败路径也会记（没有 response 时 in/out 为 `-`），因为**超时/报错前的耗时同样是
那 82 秒之谜的一部分**。
"""
import logging

log = logging.getLogger(__name__)


def _usage_of(response):
    """从 OpenAI 兼容响应里安全取 (prompt_tokens, completion_tokens)。

    端点在报错、流式、或非标准实现时可能没有 usage，故全部走 getattr 兜底 ——
    指标记录绝不能让一次成功的调用因为读字段而失败。
    """
    usage = getattr(response, 'usage', None)
    if usage is None:
        return None, None
    return (getattr(usage, 'prompt_tokens', None),
            getattr(usage, 'completion_tokens', None))


def _finish_of(response):
    try:
        return response.choices[0].finish_reason or '-'
    except (AttributeError, IndexError, TypeError):
        return '-'


def log_llm_usage(label, model, elapsed_s, response=None, note='', level=logging.INFO):
    """记一行模型调用指标。label 用「阶段/子类」形式（如 `批量翻译/general`）。

    只在 INFO 级别打一行，不抛异常、不返回任何东西 —— 调用方把它当 log 用。
    """
    pin, pout = _usage_of(response)
    fields = [
        f'label={label}',
        f'model={model}',
        f'耗时={elapsed_s:.1f}s',
        f'in={pin if pin is not None else "-"}',
        f'out={pout if pout is not None else "-"}',
        f'finish={_finish_of(response)}',
    ]
    if note:
        fields.append(f'note={note}')
    log.log(level, '[LLM用量] ' + ' '.join(fields))
