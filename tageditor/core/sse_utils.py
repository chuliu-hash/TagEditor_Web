# -*- coding: utf-8 -*-
import json


def sse_event(event_type, data):
    """格式化单个 SSE 事件字符串"""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
