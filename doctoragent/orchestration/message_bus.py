"""Agent 间通信消息总线。

支持发布/订阅模式、主题过滤、消息持久化、超时处理。
用于多智能体协作场景中 Agent 间的异步通信。

核心模型::

    publish  ──►  历史 + 订阅者分发 + 回复解析
    subscribe ──► 注册 handler（支持通配 "*" 主题）
    request   ──► 发布消息并等待 reply（asyncio.Future + 超时）
    reply     ──► 针对原始消息发布回复（reply_to 关联）
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from doctoragent.compat import UTC

logger = logging.getLogger(__name__)

# 订阅者类型：``(subscription_id, handler)`` 元组。
_Subscriber = tuple[str, Callable[[Any], None]]


class AgentMessage(BaseModel):
    """Agent 间传递的消息。"""

    id: str
    sender: str
    recipient: str = "*"
    topic: str
    content: Any = None
    timestamp: str
    reply_to: str | None = None


class MessageBus:
    """发布/订阅消息总线。

    - 支持精确主题订阅与 ``"*"`` 通配订阅。
    - ``_history`` 环形缓冲保留最近 ``_max_history`` 条消息。
    - ``request`` / ``reply`` 基于 :class:`asyncio.Future` 实现请求-响应。
    - handler 可同步可异步（返回 awaitable 时自动调度到事件循环）。
    """

    def __init__(self) -> None:
        # 主题 -> 订阅者列表；``"*"`` 为通配主题。
        self._subscribers: dict[str, list[_Subscriber]] = {}
        self._history: list[AgentMessage] = []
        self._max_history: int = 1000
        # request/reply：message_id -> Future。
        self._pending_requests: dict[str, asyncio.Future] = {}
        # subscription_id -> topic，用于 O(1) 取消订阅。
        self._subscription_index: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 发布 & 订阅
    # ------------------------------------------------------------------

    def publish(
        self,
        sender: str,
        topic: str,
        content: Any,
        recipient: str = "*",
    ) -> AgentMessage:
        """发布消息，触发订阅者分发与回复解析，返回该消息。"""
        msg = AgentMessage(
            id=str(uuid4()),
            sender=sender,
            recipient=recipient,
            topic=topic,
            content=content,
            timestamp=datetime.now(UTC).isoformat(),
            reply_to=None,
        )
        self._store_and_dispatch(msg)
        return msg

    def subscribe(self, topic: str, handler: Callable[[AgentMessage], None]) -> str:
        """订阅主题，返回 ``subscription_id``。

        主题为 ``"*"`` 时接收所有消息。
        """
        sub_id = str(uuid4())
        with self._lock:
            self._subscribers.setdefault(topic, []).append((sub_id, handler))
            self._subscription_index[sub_id] = topic
        logger.debug("subscribed", topic=topic, subscription_id=sub_id)
        return sub_id

    def unsubscribe(self, subscription_id: str) -> None:
        """取消订阅。未知 ID 静默忽略。"""
        with self._lock:
            topic = self._subscription_index.pop(subscription_id, None)
            if topic is None:
                return
            subs = self._subscribers.get(topic)
            if subs is None:
                return
            subs[:] = [s for s in subs if s[0] != subscription_id]
            if not subs:
                self._subscribers.pop(topic, None)
        logger.debug("unsubscribed", subscription_id=subscription_id)

    # ------------------------------------------------------------------
    # 请求-响应
    # ------------------------------------------------------------------

    async def request(
        self,
        sender: str,
        recipient: str,
        topic: str,
        content: Any,
        timeout: float = 30.0,
    ) -> AgentMessage | None:
        """请求-响应模式：发布消息并等待回复。

        超时或异常时返回 ``None``。必须在事件循环中 ``await`` 调用。
        """
        msg = AgentMessage(
            id=str(uuid4()),
            sender=sender,
            recipient=recipient,
            topic=topic,
            content=content,
            timestamp=datetime.now(UTC).isoformat(),
            reply_to=None,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_requests[msg.id] = future
        # 发布消息（触发订阅者分发）。
        self._store_and_dispatch(msg)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "request_timeout",
                extra={"message_id": msg.id, "timeout": timeout},
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "request_error",
                extra={"message_id": msg.id, "error": str(exc)},
            )
            return None
        finally:
            self._pending_requests.pop(msg.id, None)

    def reply(
        self,
        original_message: AgentMessage,
        sender: str,
        content: Any,
    ) -> AgentMessage:
        """回复 ``original_message``，返回新建的回复消息。

        回复消息的 ``reply_to`` 设为原始消息 ID，``recipient`` 设为
        原始消息发送者。若原始消息存在未完成的 ``request``，则解析其
        Future。
        """
        msg = AgentMessage(
            id=str(uuid4()),
            sender=sender,
            recipient=original_message.sender,
            topic=original_message.topic,
            content=content,
            timestamp=datetime.now(UTC).isoformat(),
            reply_to=original_message.id,
        )
        self._store_and_dispatch(msg)
        return msg

    # ------------------------------------------------------------------
    # 历史查询
    # ------------------------------------------------------------------

    def get_history(
        self,
        topic: str | None = None,
        sender: str | None = None,
        limit: int = 100,
    ) -> list[AgentMessage]:
        """查询消息历史，支持按主题与发送者过滤，返回最近 ``limit`` 条。"""
        with self._lock:
            snapshot = list(self._history)
        results: list[AgentMessage] = []
        for msg in reversed(snapshot):
            if topic is not None and msg.topic != topic:
                continue
            if sender is not None and msg.sender != sender:
                continue
            results.append(msg)
            if len(results) >= limit:
                break
        # 反转回时间正序。
        results.reverse()
        return results

    def clear_history(self) -> None:
        """清空历史。"""
        with self._lock:
            self._history.clear()
        logger.debug("history_cleared")

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _store_and_dispatch(self, msg: AgentMessage) -> None:
        """存入历史、解析回复 Future、分发订阅者。"""
        # 1) 存入历史（环形缓冲）。
        with self._lock:
            self._history.append(msg)
            if len(self._history) > self._max_history:
                # 保留最后 _max_history 条。
                del self._history[: len(self._history) - self._max_history]

        # 2) 如果是某条 request 的回复，解析对应的 Future。
        if msg.reply_to:
            future = self._pending_requests.get(msg.reply_to)
            if future is not None and not future.done():
                future.set_result(msg)

        # 3) 分发给订阅者（精确主题 + 通配 "*"）。
        self._dispatch(msg)

    def _dispatch(self, msg: AgentMessage) -> None:
        """将消息分发给匹配的订阅者。

        收集精确主题与 ``"*"`` 通配主题下的所有 handler，逐一调用。
        同步 handler 直接调用；返回 awaitable 的 handler 调度到事件循环。
        单个 handler 异常被隔离，不影响其他 handler。
        """
        # 收集匹配的订阅者（拷贝以避免迭代期间修改）。
        handlers: list[_Subscriber] = []
        with self._lock:
            for topic_key in (msg.topic, "*"):
                handlers.extend(self._subscribers.get(topic_key, []))

        for sub_id, handler in handlers:
            try:
                result = handler(msg)
                if inspect.isawaitable(result):
                    self._schedule_coro(result)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "subscriber_handler_error",
                    extra={"subscription_id": sub_id, "topic": msg.topic},
                )

    @staticmethod
    def _schedule_coro(coro: Any) -> None:
        """将协程调度到事件循环执行。

        有运行中的事件循环时用 ``create_task``；否则在守护线程中
        用 ``asyncio.run`` 执行，确保同步上下文下的异步 handler 也能运行。
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            # 没有运行中的事件循环：在守护线程中执行。
            import threading

            def _run() -> None:
                try:
                    asyncio.run(coro)
                except Exception:  # noqa: BLE001
                    logger.exception("async_handler_thread_error")

            threading.Thread(target=_run, daemon=True).start()
