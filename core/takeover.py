# takeover.py
"""
浏览器接管 —— 把操作权从 AI 临时移交给真人用户

背景
----
部分网页存在 AI 难以自动完成的环节（验证码、滑块、短信验证等）。
此时由 AI 主动调用工具请求接管，用户在 AstrBot WebUI 上实时看到浏览器画面
并直接操作，完成后把操作权交还 AI。

设计要点
--------
- **单向授权**：用户无法主动夺取权限，必须由 AI 调用工具发起
- **双计时器**：空闲超时（用户多久没动作）+ 硬上限（接管总时长），任一触发即结束
- **仅冻结浏览器**：接管期间 AI 的其他工具（发消息、记忆等）不受影响
- **画面广播**：CDP Page.startScreencast 变化驱动推帧，广播给所有 SSE 订阅者
- **丢帧不丢延迟**：每个订阅者只保留最新一帧，避免慢客户端拖垮整体
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger


# ==================== 结束原因 ====================

REASON_USER_END = "user_end"        # 用户主动点「结束操作」
REASON_IDLE_TIMEOUT = "idle"        # 用户长时间无操作
REASON_MAX_TIMEOUT = "max"          # 达到接管总时长上限
REASON_BROWSER_GONE = "gone"        # 浏览器崩溃 / 关闭
REASON_ABORTED = "aborted"          # 异常中断

# 结束原因 → 给 AI 的说明文案
REASON_TEXT = {
    REASON_USER_END: "用户已完成操作。",
    REASON_IDLE_TIMEOUT: "用户长时间未进行任何操作，系统已自动结束接管。",
    REASON_MAX_TIMEOUT: "已达到接管时长上限，系统自动结束接管（非用户手动结束）。",
    REASON_BROWSER_GONE: "浏览器已关闭或异常，接管被迫结束。",
    REASON_ABORTED: "接管被异常中断。",
}


# ==================== 会话状态 ====================

@dataclass
class Viewer:
    """一个 SSE 画面订阅者。只保留最新一帧，慢客户端自动丢旧帧。"""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    closed: bool = False

    def offer(self, frame: bytes) -> None:
        """投递一帧；队列满则丢弃旧帧保留新帧（丢帧不丢延迟）。"""
        if self.closed:
            return
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass


@dataclass
class TakeoverSession:
    """一次接管会话的全部状态。"""

    session_id: str
    umo: str                          # 会话来源，用于校验操作者身份
    reason: str                       # AI 给出的接管原因
    max_seconds: int                  # 硬上限
    idle_seconds: int                 # 空闲超时
    started_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    # 来源信息：接管结束后要靠这些字段把 AI 唤醒回同一个会话
    sender_id: str = ""               # 发起接管的用户（操作者）
    sender_name: str = ""
    group_id: str = ""                # 群号（私聊为空）
    self_id: str = ""                 # 机器人自身 id
    
    # 结束状态
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    end_reason: str = ""
    action_count: int = 0

    # 画面订阅者
    viewers: dict[str, Viewer] = field(default_factory=dict)
    latest_frame: bytes | None = None
    frame_size: tuple[int, int] = (0, 0)

    # 状态变更回调（用于给前端推状态）
    state_listeners: list[Callable[[dict], Awaitable[None]]] = field(default_factory=list)

    # 内存快照提供者：由外部注入（supervisor.memory_snapshot），
    # 让 SSE 每秒推送的状态里带上内存数据，WebUI 进度条即可实时刷新。
    memory_provider: Callable[[], dict] | None = None

    # 结束回调：非阻塞模式下，由它负责把 AI 唤醒并交回截图
    on_finished: Callable[["TakeoverSession"], Awaitable[None]] | None = None
    # 结束回调是否已执行（防止重复唤醒）
    notified: bool = False

    # 由外部注入的浏览器操作句柄
    browser_call: Callable[..., Awaitable[Any]] | None = None
    viewport: tuple[int, int] = (1280, 720)

    # ---------- 计时 ----------

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def remaining_max(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed)

    @property
    def remaining_idle(self) -> float:
        return max(0.0, self.idle_seconds - (time.time() - self.last_active))

    def touch(self) -> None:
        """用户有动作时调用，重置空闲计时。"""
        self.last_active = time.time()

    def status_payload(self) -> dict:
        """给前端/AI 的状态快照。"""
        payload = {
            "active": not self.finished.is_set(),
            "session_id": self.session_id,
            "reason": self.reason,
            "elapsed": round(self.elapsed, 1),
            "remaining_max": round(self.remaining_max, 1),
            "remaining_idle": round(self.remaining_idle, 1),
            "max_seconds": self.max_seconds,
            "idle_seconds": self.idle_seconds,
            "action_count": self.action_count,
            "ended": self.finished.is_set(),
            "end_reason": self.end_reason,
            # 服务端心跳时间戳：前端据此判断「服务器是否卡住」
            # （画面是变化驱动的，静止页面本来就不推帧，不能拿帧当心跳）
            "ts": time.time(),
        }
        if self.memory_provider is not None:
            try:
                payload["memory"] = self.memory_provider()
            except Exception as e:
                logger.debug(f"[Takeover] 内存快照注入失败: {e}")
        return payload

    async def notify_state(self) -> None:
        """向所有状态监听者广播一次状态。"""
        payload = self.status_payload()
        for cb in list(self.state_listeners):
            try:
                await cb(payload)
            except Exception as e:
                logger.debug(f"[Takeover] 状态通知失败: {e}")

    def offer_frame(self, frame: bytes, size: tuple[int, int] | None = None) -> None:
        """把新帧广播给所有订阅者。"""
        self.latest_frame = frame
        if size:
            self.frame_size = size
        for v in list(self.viewers.values()):
            v.offer(frame)

    def add_viewer(self, viewer_id: str) -> Viewer:
        v = Viewer()
        self.viewers[viewer_id] = v
        return v

    def remove_viewer(self, viewer_id: str) -> None:
        v = self.viewers.pop(viewer_id, None)
        if v:
            v.closed = True

    def finish(self, reason: str) -> None:
        """结束接管（幂等）。"""
        if self.finished.is_set():
            return
        self.end_reason = reason
        self.finished.set()
        for v in list(self.viewers.values()):
            v.closed = True

    # ---------- 操作注入 ----------

    async def apply_action(self, action: str, data: dict) -> dict:
        """
        把前端传来的操作注入浏览器。

        坐标一律用归一化比例（0~1）传入，这里再乘视口尺寸，
        因此前端无论缩放成多大都不会偏。

        :param action: move / down / up / click / wheel / key / touch
        :param data:   操作参数
        :return:       {"ok": bool, "error": str}
        """
        if self.finished.is_set():
            return {"ok": False, "error": "接管已结束"}
        if not self.browser_call:
            return {"ok": False, "error": "浏览器句柄不可用"}

        self.touch()

        try:
            if action == "mouse":
                await self._apply_mouse(data)
            elif action == "wheel":
                await self._apply_wheel(data)
            elif action == "key":
                await self._apply_key(data)
            elif action == "touch":
                await self._apply_touch(data)
            else:
                return {"ok": False, "error": f"未知操作: {action}"}
        except Exception as e:
            logger.warning(f"[Takeover] 操作注入失败 {action}: {e}")
            return {"ok": False, "error": str(e)[:200]}

        self.action_count += 1
        return {"ok": True}

    def _denorm(self, x: float, y: float) -> tuple[int, int]:
        """归一化比例 → 视口像素坐标。"""
        vw, vh = self.viewport
        return int(round(x * vw)), int(round(y * vh))

    async def _apply_mouse(self, data: dict) -> None:
        """鼠标事件：move / down / up / click。"""
        kind = data.get("kind", "")
        x, y = self._denorm(float(data.get("x", 0)), float(data.get("y", 0)))
        button = data.get("button", "left")
        if kind == "move":
            await self.browser_call("mouse_move_raw", x=x, y=y)
        elif kind == "down":
            await self.browser_call("mouse_down_raw", x=x, y=y, button=button)
        elif kind == "up":
            await self.browser_call("mouse_up_raw", x=x, y=y, button=button)
        elif kind == "click":
            # 用 click_raw 而非 click_coord：前者不持有 _op_lock、点击后不 sleep 2 秒。
            # 真人接管场景下，click_coord 的 2 秒等待会让连点/拖动严重卡顿，
            # 表现为「点了没反应」。
            await self.browser_call("click_raw", x=x, y=y, button=button)

    async def _apply_wheel(self, data: dict) -> None:
        """滚轮。"""
        dx = float(data.get("dx", 0))
        dy = float(data.get("dy", 0))
        x, y = self._denorm(float(data.get("x", 0.5)), float(data.get("y", 0.5)))
        await self.browser_call("wheel_raw", x=x, y=y, dx=dx, dy=dy)

    async def _apply_key(self, data: dict) -> None:
        """键盘输入或按键。"""
        text = data.get("text")
        key = data.get("key")
        if text:
            await self.browser_call("type_text_raw", text=str(text))
        elif key:
            await self.browser_call("press_key_raw", key=str(key))

    async def _apply_touch(self, data: dict) -> None:
        """移动端真触摸事件。"""
        kind = data.get("kind", "")
        x, y = self._denorm(float(data.get("x", 0)), float(data.get("y", 0)))
        await self.browser_call("touch_raw", kind=kind, x=x, y=y)


# ==================== 管理器 ====================

class TakeoverManager:
    """
    接管会话管理器。

    同一时刻只允许一个活跃会话（浏览器本身是单实例资源）。
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.session: TakeoverSession | None = None
        self._monitor_task: asyncio.Task | None = None
        self._screencast_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---------- 配置读取 ----------

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            v = int(self.config.get(key, default))
            return v if v > 0 else default
        except (TypeError, ValueError):
            return default

    @property
    def max_seconds(self) -> int:
        """接管总时长上限（默认 120 秒，WebUI 可改）。"""
        return self._cfg_int("takeover_max_seconds", 120)

    @property
    def idle_seconds(self) -> int:
        """用户空闲多久后自动交还（默认 60 秒，WebUI 可改）。"""
        return self._cfg_int("takeover_idle_seconds", 60)

    # ---------- 生命周期 ----------

    async def open(
        self,
        umo: str,
        reason: str,
        browser_call: Callable[..., Awaitable[Any]],
        viewport: tuple[int, int] = (1280, 720),
        max_seconds: int | None = None,
        *,
        sender_id: str = "",
        sender_name: str = "",
        group_id: str = "",
        self_id: str = "",
        on_finished: Callable[["TakeoverSession"], Awaitable[None]] | None = None,
    ) -> TakeoverSession:
        """
        开启一次接管会话（若已有活跃会话则先结束它）。

        :param max_seconds: 覆盖配置里的总时长上限。
        :param sender_id:   发起接管的用户 id（唤醒时要还原成同一个说话人）。
        :param group_id:    群号（私聊传空）。
        :param self_id:     机器人自身 id。
        :param on_finished: 会话结束时的回调，用于把 AI 唤醒并交回截图。
        """
        async with self._lock:
            if self.session and not self.session.finished.is_set():
                self.session.finish(REASON_ABORTED)
                await self._stop_screencast()

            try:
                hard_max = int(max_seconds) if max_seconds else self.max_seconds
            except (TypeError, ValueError):
                hard_max = self.max_seconds
            if hard_max <= 0:
                hard_max = self.max_seconds

            sess = TakeoverSession(
                session_id=uuid.uuid4().hex[:12],
                umo=umo,
                reason=reason,
                max_seconds=hard_max,
                idle_seconds=self.idle_seconds,
                browser_call=browser_call,
                viewport=viewport,
                sender_id=str(sender_id or ""),
                sender_name=str(sender_name or ""),
                group_id=str(group_id or ""),
                self_id=str(self_id or ""),
                on_finished=on_finished,
            )
            self.session = sess
            logger.info(
                f"[Takeover] 会话已开启 {sess.session_id} "
                f"上限={sess.max_seconds}s 空闲={sess.idle_seconds}s 原因={reason}"
            )
            self._monitor_task = asyncio.create_task(self._monitor(sess))
            self._screencast_task = asyncio.create_task(self._screencast(sess))
            return sess

    async def end(self, reason: str, session: TakeoverSession | None = None) -> None:
        """
        结束当前会话。

        :param session: 期望结束的会话。传入时若当前活跃会话不是它，则忽略本次请求
                        （避免上一轮遗留的监控协程误伤新会话）。
        """
        async with self._lock:
            sess = self.session
            if not sess or sess.finished.is_set():
                return
            if session is not None and sess is not session:
                return
            sess.finish(reason)
            logger.info(f"[Takeover] 会话结束 {sess.session_id} 原因={reason}")
            await self._stop_screencast()
            await sess.notify_state()

        # 在锁外触发「结束回调」：它要做截图 + 唤醒 AI，耗时不可预测，
        # 持锁会把前端的状态查询/操作注入全部堵住。
        await self._fire_finished(sess)

    async def _fire_finished(self, sess: TakeoverSession) -> None:
        """
        触发会话的结束回调（幂等）。

        非阻塞模式下，AI 早已返回、不再等待；必须靠这个回调把
        「用户操作完了 + 最新截图」重新送进同一个会话，AI 才能接着干活。
        """
        if sess.notified:
            return
        sess.notified = True
        cb = sess.on_finished
        if cb is None:
            return
        try:
            await cb(sess)
        except Exception as e:
            logger.error(f"[Takeover] 结束回调执行失败: {e}", exc_info=True)

    async def _stop_screencast(self) -> None:
        task = self._screencast_task
        self._screencast_task = None
        if task and not task.done():
            task.cancel()
            # 等它真正退出，避免它的收尾动作（stop_screencast）在
            # 新会话已经开始投屏之后才执行，把新会话的投屏掐断。
            try:
                await task
            except asyncio.CancelledError:
                # 任务自身被取消属正常收尾；调用方自己被取消才需要向上传播
                if not task.cancelled():
                    raise
            except Exception:
                pass

    def is_active(self) -> bool:
        """是否有活跃接管（供浏览器工具判断是否冻结）。"""
        return bool(self.session and not self.session.finished.is_set())

    def wait_event(self) -> asyncio.Event | None:
        """返回当前会话的结束事件，供工具侧等待。"""
        return self.session.finished if self.session else None

    # ---------- 后台任务 ----------

    async def _monitor(self, sess: TakeoverSession) -> None:
        """双计时器监控：空闲超时 + 硬上限。"""
        try:
            while not sess.finished.is_set():
                await asyncio.sleep(1.0)
                if sess.finished.is_set():
                    break
                # 硬上限优先
                if sess.remaining_max <= 0:
                    await self.end(REASON_MAX_TIMEOUT, session=sess)
                    break
                # 空闲超时
                if sess.remaining_idle <= 0:
                    await self.end(REASON_IDLE_TIMEOUT, session=sess)
                    break
                # 心跳式状态同步（让前端倒计时是服务端权威值）
                await sess.notify_state()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Takeover] 监控协程异常: {e}", exc_info=True)

    async def _screencast(self, sess: TakeoverSession) -> None:
        """
        持续从浏览器取画面帧并广播。

        优先走 CDP Page.startScreencast（变化驱动，省资源）；
        不可用时回退到定时截图轮询。
        """
        try:
            await self._screencast_cdp(sess)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[Takeover] CDP 投屏不可用，回退轮询截图: {str(e)[:160]}")
            try:
                await self._screencast_poll(sess)
            except asyncio.CancelledError:
                pass
            except Exception as e2:
                logger.error(f"[Takeover] 轮询截图也失败: {e2}", exc_info=True)

    async def _screencast_cdp(self, sess: TakeoverSession) -> None:
        """CDP 变化驱动投屏。"""
        if not sess.browser_call:
            return
        session_id = await sess.browser_call("start_screencast", **{
            "on_frame": lambda data, size: sess.offer_frame(data, size),
        })
        try:
            while not sess.finished.is_set():
                await asyncio.sleep(0.5)
        finally:
            try:
                await sess.browser_call("stop_screencast")
            except Exception:
                pass
            _ = session_id

    async def _screencast_poll(self, sess: TakeoverSession) -> None:
        """回退方案：定时截图。"""
        if not sess.browser_call:
            return
        interval = max(0.3, float(self.config.get("takeover_poll_interval", 1.0)))
        while not sess.finished.is_set():
            try:
                path = await sess.browser_call("screenshot")
                if path:
                    with open(path, "rb") as f:
                        sess.offer_frame(f.read())
            except Exception as e:
                logger.debug(f"[Takeover] 轮询截图失败: {e}")
            await asyncio.sleep(interval)

    # ---------- 画面转 SSE ----------

    @staticmethod
    def frame_to_sse(frame: bytes) -> bytes:
        """把一帧编码成 SSE 消息（base64）。"""
        b64 = base64.b64encode(frame).decode("ascii")
        return f"event: frame\ndata: {b64}\n\n".encode("utf-8")

    @staticmethod
    def state_to_sse(payload: dict) -> bytes:
        """把状态编码成 SSE 消息。"""
        import json
        return f"event: state\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    async def stream_for(self, viewer_id: str):
        """
        生成给某个订阅者的 SSE 事件流。

        产出顺序：先一帧当前画面，再持续跟随新帧；同时每 1 秒补一次状态。
        """
        sess = self.session
        if not sess:
            yield self.state_to_sse({"active": False, "error": "no_session"})
            return

        viewer = sess.add_viewer(viewer_id)
        try:
            # 首帧：让用户立刻看到画面，不用等下一次变化
            if sess.latest_frame:
                yield self.frame_to_sse(sess.latest_frame)
            yield self.state_to_sse(sess.status_payload())

            last_state = time.time()
            while not sess.finished.is_set() and not viewer.closed:
                try:
                    frame = await asyncio.wait_for(viewer.queue.get(), timeout=0.5)
                    yield self.frame_to_sse(frame)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    break
                # 每 0.5 秒补一次状态：既当心跳（前端据此判断服务器是否卡住），
                # 也让内存进度条能实时刷新
                if time.time() - last_state >= 0.5:
                    yield self.state_to_sse(sess.status_payload())
                    last_state = time.time()

            # 收尾：告知前端已结束
            yield self.state_to_sse(sess.status_payload())
        finally:
            sess.remove_viewer(viewer_id)

    async def shutdown(self) -> None:
        """插件卸载时清理。"""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        await self._stop_screencast()
        if self.session and not self.session.finished.is_set():
            self.session.finish(REASON_ABORTED)
