"""LLM transport 薄客户端：OpenAI-compatible Chat Completions。

刻意不锁定厂商：任何兼容该协议的服务（DeepSeek、Kimi、本地 vLLM 等）都可通过
base_url 接入。本模块只负责网络、瞬时故障重试与响应解析，并把所有最终失败统一包装成
:class:`~repo2gal.errors.GenerationError`（CLI 退出码 4），错误正文一律脱敏。
叙事 prompt 的组装在 ``generator.py``，导演 JSON 的校验重试在 ``pipeline.py``。

重试只覆盖可以重发的瞬时故障：限流（429）、网关与上游过载（5xx）、连接层异常。
其余失败（401、404、响应结构异常）立即抛出，不做无谓等待。
"""

from __future__ import annotations

import time
from typing import Callable

import requests

from .config import DEFAULT_LLM_TIMEOUT, resolve_api_key
from .errors import GenerationError, redact_error

MISSING_KEY_MESSAGE = "缺少 API Key，请设置环境变量 REPO2GAL_API_KEY"

# 免费与共享端点最常见的瞬时状态码，重发同一请求有机会成功。
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_ATTEMPTS = 4
# 免费端点的 high demand 过载可持续数十秒，退避要够长才能穿越。
DEFAULT_RETRY_BACKOFF = (10.0, 30.0, 60.0)


class LLMClient:
    """一次运行复用一个客户端；complete() 是唯一入口。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: int = DEFAULT_LLM_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff: tuple[float, ...] = DEFAULT_RETRY_BACKOFF,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.retry_backoff = tuple(retry_backoff)
        self.notify = notify

    def complete(self, prompt: str, *, temperature: float = 0.8) -> str:
        """调用一次 Chat Completions 并返回 content；瞬时故障按退避序列重试。"""
        api_key = self.api_key or resolve_api_key()
        if not api_key:
            raise GenerationError(MISSING_KEY_MESSAGE)
        for attempt in range(1, self.max_attempts + 1):
            reason = ""
            retryable = True
            error: GenerationError
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": temperature,
                    },
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                reason = "连接异常"
                error = GenerationError(
                    f"LLM 请求失败：{redact_error(str(exc), secret=api_key)}"
                )
            else:
                if resp.ok:
                    return _extract_content(resp)
                reason = f"HTTP {resp.status_code}"
                error = GenerationError(
                    f"LLM 返回 {resp.status_code}：{redact_error(resp.text, secret=api_key)}"
                )
                retryable = resp.status_code in RETRYABLE_STATUS_CODES
            if not retryable or attempt == self.max_attempts:
                raise error
            delay = self._backoff_for(attempt)
            if self.notify is not None:
                self.notify(
                    f"LLM 第 {attempt}/{self.max_attempts} 次尝试失败（{reason}），"
                    f"{delay:g} 秒后重试"
                )
            if delay:
                time.sleep(delay)
        raise GenerationError("LLM 调用未产生结果")

    def _backoff_for(self, attempt: int) -> float:
        """第 attempt 次失败后的等待秒数；退避序列用尽后沿用最后一个值。"""
        if not self.retry_backoff:
            return 0.0
        return self.retry_backoff[min(attempt - 1, len(self.retry_backoff) - 1)]


def _extract_content(resp: requests.Response) -> str:
    """从 2xx 响应里取出 content；结构异常一律抛 GenerationError。"""
    try:
        data = resp.json()
    except ValueError as exc:
        raise GenerationError("LLM 响应不是合法 JSON") from exc
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GenerationError("LLM 响应结构异常：缺少 choices[0].message.content") from exc
    if not isinstance(content, str):
        raise GenerationError("LLM 响应 content 不是字符串")
    return content
