"""LLM transport 薄客户端：OpenAI-compatible Chat Completions。

刻意不锁定厂商：任何兼容该协议的服务（DeepSeek、Kimi、本地 vLLM 等）都可通过
base_url 接入。本模块负责网络、模型降级与响应解析，并把所有最终失败统一包装成
:class:`~repo2gal.errors.GenerationError`（CLI 退出码 4），错误正文一律脱敏。
叙事 prompt 的组装在 ``generator.py``，导演 JSON 的校验重试在 ``pipeline.py``。

模型链按顺序尝试，像 PATH 一样从左到右找第一个能用的。免费与共享端点最常见的
故障是单模型过载（5xx）与限流（429），而配额和过载都按模型独立，所以恢复策略
是换模型而不是干等：同一模型内短暂重试，用尽后立刻切到链上的下一个；模型不
存在（404）不重试直接切；认证失败等其余错误立即抛出。最近成功过的模型会提到
链首，避免在已知过载的模型上反复试错。
"""

from __future__ import annotations

import time
from typing import Callable

import requests

from .config import DEFAULT_LLM_TIMEOUT, DEFAULT_MODEL, resolve_api_key
from .errors import GenerationError, redact_error

MISSING_KEY_MESSAGE = "缺少 API Key，请设置环境变量 REPO2GAL_API_KEY"

# 单模型内可重发的瞬时状态码。
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# 模型不存在或当前凭据无权访问：重试同一个模型没有意义，直接换下一个。
MODEL_UNAVAILABLE_STATUS_CODES = frozenset({404})
# 降级链承担大部分恢复工作，单模型内的重试保持轻量。
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RETRY_BACKOFF = (5.0,)


class LLMClient:
    """一次运行复用一个客户端；complete() 是唯一入口。"""

    def __init__(
        self,
        *,
        base_url: str,
        models: tuple[str, ...] | list[str],
        api_key: str | None = None,
        timeout: int = DEFAULT_LLM_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff: tuple[float, ...] = DEFAULT_RETRY_BACKOFF,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.models = tuple(models) or (DEFAULT_MODEL,)
        self.api_key = api_key
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.retry_backoff = tuple(retry_backoff)
        self.notify = notify
        self._preferred_model: str | None = None

    def complete(self, prompt: str, *, temperature: float = 0.8) -> str:
        """按模型链调用一次 Chat Completions 并返回 content 字符串。"""
        api_key = self.api_key or resolve_api_key()
        if not api_key:
            raise GenerationError(MISSING_KEY_MESSAGE)
        order = self._model_order()
        last_error: GenerationError | None = None
        for index, model in enumerate(order):
            for attempt in range(1, self.max_attempts + 1):
                reason = ""
                error: GenerationError
                try:
                    resp = requests.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
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
                        self._preferred_model = model
                        return _extract_content(resp)
                    reason = f"HTTP {resp.status_code}"
                    error = GenerationError(
                        f"LLM 返回 {resp.status_code}：{redact_error(resp.text, secret=api_key)}"
                    )
                    if resp.status_code in MODEL_UNAVAILABLE_STATUS_CODES:
                        last_error = error
                        if index + 1 >= len(order):
                            raise error
                        break
                    if resp.status_code not in RETRYABLE_STATUS_CODES:
                        raise error
                last_error = error
                if attempt < self.max_attempts:
                    delay = self._backoff_for(attempt)
                    self._notify(
                        f"模型 {model} 第 {attempt}/{self.max_attempts} 次尝试失败"
                        f"（{reason}），{delay:g} 秒后重试"
                    )
                    if delay:
                        time.sleep(delay)
            if index + 1 < len(order):
                self._notify(f"模型 {model} 未通过，切换下一个模型 {order[index + 1]}")
        raise last_error or GenerationError("LLM 调用未产生结果")

    def _model_order(self) -> list[str]:
        """最近一次成功过的模型排到链首，减少在已知过载的模型上反复试错。"""
        chain = list(self.models)
        if self._preferred_model in chain:
            chain.remove(self._preferred_model)
            chain.insert(0, self._preferred_model)
        return chain

    def _backoff_for(self, attempt: int) -> float:
        """第 attempt 次失败后的等待秒数；退避序列用尽后沿用最后一个值。"""
        if not self.retry_backoff:
            return 0.0
        return self.retry_backoff[min(attempt - 1, len(self.retry_backoff) - 1)]

    def _notify(self, message: str) -> None:
        if self.notify is not None:
            self.notify(message)


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
