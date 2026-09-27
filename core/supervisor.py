# astrbot/core/supervisor.py

import asyncio
import os
import time
import traceback
from pathlib import Path
from typing import Any, Callable

import psutil

from astrbot.api import logger

# 浏览器进程名关键字（用于统计浏览器自身的真实内存占用）
_BROWSER_PROC_HINTS = ("chrome", "headless", "firefox", "webkit")
# Playwright 驱动进程标识（node 跑 cli.js run-driver，属于浏览器底座）
_DRIVER_HINT = "run-driver"


def _browser_mem_detail() -> dict:
    """统计浏览器进程组的真实内存占用。

    注意：容器内 psutil.virtual_memory() 读到的是**宿主机**内存，
    而浏览器只占其中一部分。判断「浏览器是否吃太多」必须看它自己的用量，
    否则宿主机上别的服务一涨就会误杀浏览器。

    统计范围（都是「浏览器开关一关就消失」的进程）：
      - chrome / chromium / headless_shell / firefox / webkit 主进程与子进程
      - crashpad 崩溃处理器
      - Playwright 的 node 驱动进程（cli.js run-driver）
    刻意**不含** AstrBot 自身的 python 进程 —— 那是宿主程序，不是浏览器开销。

    返回 {"rss_mb": 浏览器占用, "procs": 进程数, "zombies": 僵尸数}
    """
    total = 0
    procs = 0
    zombies = 0
    try:
        for proc in psutil.process_iter(["name", "memory_info", "cmdline", "status"]):
            try:
                info = proc.info
                if info.get("status") == psutil.STATUS_ZOMBIE:
                    name_low = (info.get("name") or "").lower()
                    if any(h in name_low for h in _BROWSER_PROC_HINTS):
                        zombies += 1
                    continue

                name_low = (info.get("name") or "").lower()
                cmdline = " ".join(info.get("cmdline") or [])
                is_browser = any(h in name_low for h in _BROWSER_PROC_HINTS)
                is_driver = _DRIVER_HINT in cmdline
                if not (is_browser or is_driver):
                    continue

                mi = info.get("memory_info")
                if mi is None:
                    continue
                total += mi.rss
                procs += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return {
        "rss_mb": total / 1024 / 1024,
        "procs": procs,
        "zombies": zombies,
    }


def _browser_rss_mb() -> float:
    """兼容旧调用：只取浏览器占用数值（MB）。"""
    return _browser_mem_detail()["rss_mb"]


class BrowserSupervisor:
    """
    纯 asyncio 版本浏览器管理器
    支持：
    - 检查浏览器安装
    - 启动/停止浏览器
    - 内存监控自动重启
    - 闲置自动关闭
    - 异步调用 BrowserCore 方法
    """

    def __init__(self, config: dict, data_dir: str):
        self.config = config
        sup_cfg: dict[str, Any] = config.get("supervisor", {})
        self.max_memory_percent: int = sup_cfg.get("max_memory_percent", 90)
        self.idle_timeout: int = sup_cfg.get("idle_timeout", 300)
        self.monitor_interval: float = sup_cfg.get("monitor_interval", 10.0)
        self.browser_type = config.get("browser_type", "firefox")
        self.browser_mode = config.get("browser_mode", "embedded")
        self.verify_browser = config.get("verify_browser", True)

        self.data_dir = data_dir

        self.browser = None

        self._call_lock = asyncio.Lock()
        self._browser_lock = asyncio.Lock()

        self._last_active: float = time.time()
        self._monitor_task: asyncio.Task | None = None

        # 最近一次「自动关闭浏览器」的记录，供 WebUI 提示用户
        self._last_close: dict[str, Any] | None = None
        # 浏览器自身的关闭阈值（MB），0 表示不限制
        self.browser_memory_limit_mb: int = int(
            sup_cfg.get("browser_memory_limit_mb", 0) or 0
        )
        # 接管状态探测钩子：返回 True 时监控循环跳过一切自动关闭
        self._in_takeover: Callable[[], bool] | None = None

    # ---------------- 生命周期 ----------------
    async def start(self):
        """启动监控协程，浏览器暂时不启动"""
        async with self._call_lock:
            if self._monitor_task is None or self._monitor_task.done():
                self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """停止浏览器和监控"""
        async with self._call_lock:
            if self._monitor_task and not self._monitor_task.done():
                self._monitor_task.cancel()
                try:
                    await self._monitor_task
                except asyncio.CancelledError:
                    pass
                self._monitor_task = None

            if self.browser:
                try:
                    await self.browser.terminate()
                except Exception:
                    pass
                self.browser = None

    # ---------------- 对外调用 ----------------
    async def call(self, method: str, **kwargs):
        # 1) 确保浏览器已启动（_start_browser 内部自带 _browser_lock）
        if not self.browser:
            await self._start_browser()

        browser = self.browser
        if not browser:
            return None
        func = getattr(browser, method, None)
        if func is None:
            raise AttributeError(f"BrowserCore 没有方法 {method}")

        # 2) 不在锁内执行真正的浏览器操作：
        #    浏览器操作动辄数十秒，持锁会串行化所有并发调用
        self._last_active = time.time()
        return await func(**kwargs)

    # ---------------- 内部浏览器启动/重启 ----------------
    async def _start_browser(self):
        """启动浏览器"""
        async with self._browser_lock:
            # 检测浏览器安装
            if not self.browser:
                if self.browser_mode != "local_cdp" and self.verify_browser:
                    from .downloader import BrowserDownloader

                    # 传对浏览器目录：否则会去 playwright 默认缓存路径找，
                    # 找不到就白下载一份，找到了也会起一套无关进程
                    browsers_dir = Path(self.data_dir) / "browsers"
                    if not await BrowserDownloader.verify_browser(
                            self.browser_type, browsers_dir=browsers_dir):
                        logger.error(
                            "浏览器未安装或不可用，请先在聊天窗口发送命令：安装浏览器"
                        )
                        raise RuntimeError("浏览器未安装或不可用")

                from .browser import BrowserCore

                core = BrowserCore(self.config, Path(self.data_dir))
                try:
                    await core.initialize()
                except Exception:
                    logger.error("[Supervisor] BrowserCore.initialize 失败")
                    raise
                self.browser = core
                self._last_active = time.time()

    async def _stop_browser(self, reason: str = "", detail: str = "") -> None:
        """停止浏览器。

        :param reason: 关闭原因标识（idle / memory / manual ...），
                       自动关闭时会记录到 _last_close，供 WebUI 提示用户。
        """
        async with self._browser_lock:
            if self.browser:
                try:
                    await self.browser.terminate()
                except Exception as e:
                    # 记录但不抛出：否则 self.browser 无法置空，会造成状态不一致
                    logger.error(f"[Supervisor] BrowserCore.terminate 失败: {e}")
                finally:
                    self.browser = None
                    self._last_active = time.time()
                    if reason:
                        self._last_close = {
                            "reason": reason,
                            "detail": detail,
                            "at": time.time(),
                        }

    # ---------------- 内存快照 ----------------

    def memory_snapshot(self) -> dict:
        """给 WebUI 的内存占用快照。

        字段说明（单位 MB）：
          total     —— 宿主机总内存
          used      —— 宿主机已用内存（含其他所有服务）
          other     —— 除浏览器外的占用
          browser   —— 浏览器进程组占用（含 playwright 驱动）
          threshold —— 触发自动关闭的阈值（按已用百分比换算成 MB）
          percent   —— 当前已用百分比
          limit_percent —— 配置的百分比阈值
          browser_running —— 浏览器当前是否真的在跑（以进程为准，不看句柄）
        """
        try:
            vm = psutil.virtual_memory()
            total = vm.total / 1024 / 1024
            used = vm.used / 1024 / 1024
            percent = float(vm.percent)
        except Exception:
            total = used = 0.0
            percent = 0.0

        # 以进程为准判断浏览器是否活着：句柄可能因为外部 close 而失效，
        # 但进程还在（或反过来）。两边都看，避免 UI 显示「就绪」其实没进程。
        detail = _browser_mem_detail()
        browser = detail["rss_mb"]
        has_proc = detail["procs"] > 0
        # 进程占位也可能只是残留的僵尸/驱动，故综合句柄与进程判断
        running = bool(self.browser) and has_proc
        if not has_proc:
            running = False

        other = max(0.0, used - browser)
        threshold = total * self.max_memory_percent / 100.0 if total else 0.0

        return {
            "total": round(total, 1),
            "used": round(used, 1),
            "other": round(other, 1),
            "browser": round(browser, 1),
            "threshold": round(threshold, 1),
            "percent": round(percent, 1),
            "limit_percent": self.max_memory_percent,
            "browser_running": running,
            "browser_handle": bool(self.browser),
            "browser_procs": detail["procs"],
            "browser_zombies": detail["zombies"],
            "browser_limit_mb": self.browser_memory_limit_mb,
            "last_close": self._last_close,
        }

    # ---------------- 监控 ----------------

    async def _monitor_loop(self):
        while True:
            try:
                await asyncio.sleep(self.monitor_interval)

                # ★ 僵尸进程回收：必须在「浏览器是否在跑」判断**之前**执行。
                # 僵尸主要来自 verify_browser 的一过性探测——探测完浏览器就退了，
                # 此时 self.browser 为 None，若放在后面会被 continue 跳过，
                # 僵尸永远清不掉。
                try:
                    from .downloader import BrowserDownloader

                    reaped = await BrowserDownloader.reap_zombies()
                    if reaped:
                        logger.info(f"[Supervisor] 回收了 {reaped} 个僵尸子进程")
                except Exception:
                    pass

                if not self.browser:
                    continue

                # ★ 接管中绝不自动关闭：用户正在操作，杀掉浏览器等于毁掉整场操作
                if self._in_takeover and self._in_takeover():
                    continue

                # 空闲检测
                if time.time() - self._last_active > self.idle_timeout:
                    detail = f"浏览器闲置超过 {self.idle_timeout} 秒"
                    await self._stop_browser("idle", detail)
                    logger.warning(f"[Supervisor] {detail}，自动关闭浏览器")
                    continue

                # ★ 浏览器自身内存监控（不看整机百分比，避免被别的服务牵连）
                if self.browser_memory_limit_mb > 0:
                    used_mb = _browser_rss_mb()
                    if used_mb > self.browser_memory_limit_mb:
                        detail = (
                            f"浏览器内存占用 {used_mb:.0f}MB，"
                            f"超过上限 {self.browser_memory_limit_mb}MB"
                        )
                        await self._stop_browser("memory", detail)
                        logger.warning(f"[Supervisor] {detail}，自动关闭浏览器")
                        continue

                # 整机内存兜底：仅在极端情况下（默认阈值很高）才触发
                mem = psutil.virtual_memory()
                if mem.percent > self.max_memory_percent:
                    detail = (
                        f"服务器内存占用 {mem.percent:.1f}%，"
                        f"超过阈值 {self.max_memory_percent}%"
                    )
                    await self._stop_browser("memory", detail)
                    logger.warning(f"[Supervisor] {detail}，自动关闭浏览器")

            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Supervisor 监控循环异常:\n" + traceback.format_exc())
