# \astrbot\core\browser.py

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import uuid
from collections.abc import Coroutine, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from .image_utils import get_format_from_config, get_output_ext, _FORMAT_MAP as IMG_FORMAT_MAP
from astrbot.api import logger

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Cookie, Page, Playwright

T = TypeVar("T")

# ======================================================
# 反风控伪装脚本（stealth）
# 在每个页面所有 JS 执行前注入，抹平 headless 自动化指纹。
# 配合 channel="chromium"（完整版新无头内核）+ 正常 UA 使用。
# ======================================================
STEALTH_JS = """
(() => {
  try {
    // --- navigator.webdriver（配合 --disable-blink-features=AutomationControlled 双保险）---
    Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });

    // --- window.chrome（Chromium 开源内核不带此对象，但 UA 标 Chrome，必须补上）---
    if (!window.chrome) { window.chrome = {}; }
    window.chrome.runtime = window.chrome.runtime || {
      PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
      PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64', MIPS: 'mips', MIPS64: 'mips64' },
      PlatformNaclArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64', MIPS: 'mips', MIPS64: 'mips64' },
      RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available', THROTTLED: 'throttled' },
      OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
      OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
      connect: function () { return { onDisconnect: { addListener: function () {} }, onMessage: { addListener: function () {} }, postMessage: function () {} }; },
      sendMessage: function () {},
    };
    window.chrome.app = window.chrome.app || {
      isInstalled: false,
      InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
      RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
      getDetails: function () {},
      getIsInstalled: function () { return false; },
      runningState: function () { return 'cannot_run'; },
    };
    window.chrome.csi = window.chrome.csi || function () {
      return { startE: Date.now(), onloadT: Date.now(), pageT: Date.now() - 100, tran: 15 };
    };
    window.chrome.loadTimes = window.chrome.loadTimes || function () {
      return {
        commitLoadTime: Date.now() / 1000, connectionInfo: 'h2',
        finishDocumentLoadTime: Date.now() / 1000, finishLoadTime: Date.now() / 1000,
        firstPaintAfterLoadTime: 0, firstPaintTime: Date.now() / 1000,
        navigationType: 'Other', npnNegotiatedProtocol: 'h2',
        requestTime: Date.now() / 1000 - 0.2, startLoadTime: Date.now() / 1000 - 0.2,
        wasAlternateProtocolAvailable: false, wasFetchedViaSpdy: true, wasNpnNegotiated: true,
      };
    };

    // --- plugins/mimeTypes：完整版新无头内核原生带 PDF 插件，不伪造 ---

    // --- 语言 / 硬件 ---
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'], configurable: true });
    try {
      Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
      Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
      Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0, configurable: true });
    } catch (e) {}

    // --- 权限 API（未授权应返回 prompt，headless 默认 denied 是异常特征）---
    const origQuery = navigator.permissions && navigator.permissions.query
      ? navigator.permissions.query.bind(navigator.permissions) : null;
    if (origQuery) {
      navigator.permissions.query = (p) => {
        if (p && p.name === 'notifications') {
          return Promise.resolve({ state: 'prompt', onchange: null, name: 'notifications' });
        }
        return origQuery(p);
      };
    }

    // --- WebGL 厂商/渲染器（软件渲染 SwiftShader 是典型 headless 特征）---
    const GL_VENDOR = 'Intel Inc.';
    const GL_RENDERER = 'Intel(R) UHD Graphics 630';
    const patchGL = (proto) => {
      if (!proto) { return; }
      const orig = proto.getParameter;
      proto.getParameter = function (param) {
        if (param === 37445) { return GL_VENDOR; }
        if (param === 37446) { return GL_RENDERER; }
        return orig.call(this, param);
      };
      const origExt = proto.getExtension;
      proto.getExtension = function (name) {
        const ext = origExt.call(this, name);
        if (ext && name === 'WEBGL_debug_renderer_info') {
          const oGet = ext.getParameter ? ext.getParameter.bind(ext) : null;
          if (oGet) {
            ext.getParameter = (p) => (p === 37445 ? GL_VENDOR : p === 37446 ? GL_RENDERER : oGet(p));
          }
        }
        return ext;
      };
    };
    patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  } catch (e) { /* 伪装失败不影响正常功能 */ }
})();
"""




class CookieManager:
    def __init__(self, data_dir: Path):
        self.cookies_file = data_dir / "browser_cookies.json"

    def load_cookies(self) -> list[dict]:
        """从 json 文件加载 cookies，返回 List[Cookie]（TypedDict）"""
        try:
            with open(self.cookies_file, encoding="utf-8") as f:
                raw_cookies: list[dict] = json.load(f)
                return raw_cookies
        except FileNotFoundError:
            print("Cookies 文件未找到或格式错误，返回空列表")
            self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cookies_file, "w", encoding="utf-8") as f:
                json.dump([], f)
            return []
        except json.JSONDecodeError:
            print("Cookies 文件格式错误，无法解析，返回空列表且保留原文件")
            return []

    def save_cookies(self, cookies: list[dict]):
        """保存cookies到json文件中"""
        self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cookies_file, "w") as f:
            json.dump(cookies, f, indent=4, ensure_ascii=False)


class BrowserCore:
    """
    浏览器核心
    """

    _BROWSER_ENGINES = {"firefox", "chromium", "webkit"}

    def __init__(self, config: dict, data_dir: Path):
        self.config = config
        self.cookie = CookieManager(data_dir)

        self.cache_dir = data_dir / "screenshot_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.browser_mode: str = self.config.get("browser_mode", "embedded")
        if self.browser_mode not in {"embedded", "local_cdp"}:
            raise ValueError(f"不支持的浏览器接入模式: {self.browser_mode}")

        self.cdp_url: str = self.config.get("cdp_url", "http://127.0.0.1:9222")
        if self.browser_mode == "local_cdp" and not self.cdp_url:
            raise ValueError("local_cdp 模式下 cdp_url 不能为空")

        self.browser_type: str = self.config.get("browser_type", "firefox")
        if self.browser_mode == "local_cdp":
            self.browser_type = "chromium"
        if self.browser_type not in self._BROWSER_ENGINES:
            raise ValueError(f"不支持的浏览器类型: {self.browser_type}")

        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

        self.all_pages: list[Page] = []
        self.current_index: int | None = None
        self.page: Page | None = None

        self._terminated = False
        self._is_cdp_mode = self.browser_mode == "local_cdp"

        # 图片输出格式 & 渲染模式（每次截图时从 config 实时读取，避免重载后失效）

        # ===== 核心防护 =====
        self._op_lock = asyncio.Lock()

        # ===== 接管投屏 =====
        self._screencast_client = None
        self._screencast_frames = 0
        self._touch_client = None

    # ======================================================
    # 通用兜底工具
    # ======================================================

    async def _safe_await(self, coro_factory, retries: int = 2) -> T:  # type: ignore
        """
        Playwright 操作超时/状态异常重试机制
        :param coro_factory: 无参 callable，每次调用返回一个新的协程（因为协程对象是一次性的）
        :param retries: 重试次数
        """
        timeout_raw = self.config.get("timeout", 30)
        try:
            timeout = max(float(timeout_raw), 1.0)
        except (TypeError, ValueError):
            timeout = 30.0

        # 可重试的异常特征：超时 / 浏览器或页面已关闭 / 连接断开
        retryable_markers = (
            "has been closed",
            "Target closed",
            "Browser has been closed",
            "Connection closed",
            "Target page, context or browser has been closed",
        )

        last_exc = None
        for attempt in range(retries + 1):
            try:
                return await asyncio.wait_for(coro_factory(), timeout)
            except asyncio.TimeoutError as e:
                last_exc = e
                if attempt < retries:
                    await asyncio.sleep(0.5)
            except Exception as e:
                # 浏览器/页面被关闭类错误同样可重试
                msg = str(e)
                if any(marker in msg for marker in retryable_markers):
                    last_exc = e
                    if attempt < retries:
                        await asyncio.sleep(0.5)
                        continue
                raise
        raise RuntimeError("Playwright 操作超时") from last_exc

    async def _safe_page_op(self, page: Page, coro: Coroutine[Any, Any, T]) -> T:
        """
        Page 级操作兜底：
        - 任意异常 -> 关闭 Page -> 移除 -> 重建
        """
        try:
            return await coro
        except Exception:
            await self._discard_page(page)
            raise

    async def _discard_page(self, page: Page):
        try:
            await page.close()
        except Exception:
            pass

        if page in self.all_pages:
            self.all_pages.remove(page)

        if not self.all_pages:
            await self._ensure_page()
        else:
            await self._ensure_page(
                min(self.current_index or 0, len(self.all_pages) - 1)
            )

    # ======================================================
    # 生命周期
    # ======================================================

    async def _restore_cookies(self):
        """恢复本地持久化 cookies，异常 cookie 自动跳过"""
        if not self.context:
            return

        raw_cookies = self.cookie.load_cookies()
        cookies = [{k: v for k, v in c.items() if v is not None} for c in raw_cookies]
        if not cookies:
            return

        try:
            await self.context.add_cookies(cookies)  # type: ignore[arg-type]
            return
        except Exception:
            pass

        for ck in cookies:
            try:
                await self.context.add_cookies([ck])  # type: ignore[arg-type]
            except Exception:
                continue

    async def initialize(self):
        async with self._op_lock:
            try:
                from playwright.async_api import async_playwright
            except ModuleNotFoundError as e:
                raise RuntimeError(
                    "缺少 playwright 运行时，请先执行命令：安装浏览器"
                ) from e

            self.playwright = await async_playwright().start()

            if self._is_cdp_mode:
                self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
                if not self.browser.contexts:
                    raise RuntimeError(
                        "CDP 连接成功但未获取到浏览器上下文，请检查本地 Chromium 启动参数（建议带 --user-data-dir）"
                    )

                self.context = self.browser.contexts[0]
                if bool(self.config.get("browser_stealth_enabled", True)) and self.browser_type == "chromium":
                    try:
                        await self.context.add_init_script(STEALTH_JS)
                    except Exception:
                        pass
                await self._restore_cookies()

                self.all_pages = list(self.context.pages)
                if self.all_pages:
                    self.current_index = 0
                    self.page = self.all_pages[0]
                else:
                    await self._ensure_page()
                return

            engine = getattr(self.playwright, self.browser_type)
            launch_opts = self._get_launch_options(self.browser_type)
            stealth_on = bool(self.config.get("browser_stealth_enabled", True))

            self.browser = None
            if self.browser_type == "chromium":
                try:
                    # channel="chromium" → Playwright ≥1.49 走完整版 Chromium 新无头内核，
                    # 指纹远比默认 headless_shell 接近真人浏览器；缺失时回退默认内核
                    self.browser = await engine.launch(
                        channel="chromium", headless=True, **launch_opts
                    )
                except Exception:
                    self.browser = None
            if self.browser is None:
                self.browser = await engine.launch(headless=True, **launch_opts)

            ctx_opts: dict[str, Any] = {}
            init_scripts: list[str] = []
            if stealth_on:
                # UA：读取内核真实 UA 并抹去 Headless 字样，保证版本号与内核精确匹配
                stealth_ua = ""
                try:
                    tmp_ctx = await self.browser.new_context()
                    tmp_page = await tmp_ctx.new_page()
                    stealth_ua = await tmp_page.evaluate("navigator.userAgent")
                    await tmp_ctx.close()
                except Exception:
                    stealth_ua = ""
                if not stealth_ua:
                    stealth_ua = (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    )
                stealth_ua = stealth_ua.replace("HeadlessChrome", "Chrome").replace(
                    "HeadlessFirefox", "Firefox"
                )
                ctx_opts = {
                    "user_agent": stealth_ua,
                    "locale": "zh-CN",
                    "timezone_id": "Asia/Shanghai",
                    "color_scheme": "light",
                    "device_scale_factor": 1,
                }
                if self.browser_type == "chromium":
                    init_scripts.append(STEALTH_JS)

            self.context = await self.browser.new_context(
                viewport=self.config["viewport_size"] or None,
                proxy=self._normalize_proxy(self.config.get("proxy")),
                **ctx_opts,
            )
            for script in init_scripts:
                try:
                    await self.context.add_init_script(script)
                except Exception:
                    pass
            await self._restore_cookies()

            await self._ensure_page()

    async def terminate(self):
        """优雅关闭浏览器及相关资源，幂等执行"""
        async with self._op_lock:
            if self._terminated:
                return
            self._terminated = True

            async def safe_close(obj, close_method="close"):
                if obj is None:
                    return
                try:
                    coro = getattr(obj, close_method)
                    if asyncio.iscoroutinefunction(coro):
                        await coro()
                    else:
                        coro()
                except Exception:
                    pass

            # 保存 cookies（local_cdp 下仅尽力保存，不影响断连）
            try:
                await self.save_cookies()
            except Exception:
                pass

            if not self._is_cdp_mode:
                # embedded 模式下关闭所有 Page/context/browser
                for page in self.all_pages:
                    await safe_close(page)

                await safe_close(self.context)
                await safe_close(self.browser)

            self.all_pages.clear()
            self.current_index = None
            self.page = None
            self.context = None
            self.browser = None

            # local_cdp 下 stop playwright 仅断开连接，不会强杀本地浏览器进程
            await safe_close(self.playwright, "stop")
            self.playwright = None

            # 清空并重建缓存目录
            if self.cache_dir.exists():
                try:
                    shutil.rmtree(self.cache_dir, ignore_errors=True)
                except Exception:
                    pass
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ======================================================
    # 参数
    # ======================================================

    @staticmethod
    def _normalize_proxy(raw: Any) -> dict[str, str] | None:
        """把配置里的代理字符串规范成 Playwright 要求的 dict 形式。

        Playwright 的 new_context(proxy=...) 只接受 dict（{"server": ..., "username": ..., "password": ...}），
        传字符串会直接抛错导致浏览器启动失败（issue #12）。此处兼容多种写法：

        - "http://127.0.0.1:7890"                → {"server": "http://127.0.0.1:7890"}
        - "socks5://user:pass@127.0.0.1:1080"    → 拆出 username / password
        - "127.0.0.1:7890"                       → 自动补 http:// 前缀
        - {"server": "...", ...}                 → 原样使用
        """
        if not raw:
            return None
        if isinstance(raw, dict):
            server = str(raw.get("server") or "").strip()
            if not server:
                return None
            result = {"server": server}
            if raw.get("username"):
                result["username"] = str(raw["username"])
            if raw.get("password"):
                result["password"] = str(raw["password"])
            return result

        text = str(raw).strip()
        if not text:
            return None
        if "://" not in text:
            text = f"http://{text}"

        result: dict[str, str] = {}
        try:
            from urllib.parse import unquote, urlsplit

            parts = urlsplit(text)
            if not parts.hostname:
                return {"server": text}
            port = f":{parts.port}" if parts.port else ""
            result["server"] = f"{parts.scheme}://{parts.hostname}{port}"
            if parts.username:
                result["username"] = unquote(parts.username)
            if parts.password:
                result["password"] = unquote(parts.password)
        except Exception:
            # 解析失败时退回原串，交给 Playwright 判断
            return {"server": text}
        return result or None

    def _get_launch_options(self, engine: str) -> dict[str, Any]:
        args = [
            "--mute-audio",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-extensions",
            # 隐藏自动化标志（navigator.webdriver）+ 正常语言
            "--disable-blink-features=AutomationControlled",
            "--lang=zh-CN",
        ]

        # 容器内以 root 运行必须关闭 Chromium 沙箱，否则可能启动失败
        if engine == "chromium" and hasattr(os, "geteuid") and os.geteuid() == 0:
            args += ["--no-sandbox", "--disable-setuid-sandbox"]

        opts: dict[str, Any] = {"args": args}

        if engine == "firefox":
            opts["firefox_user_prefs"] = {
                "intl.accept_languages": "zh-CN,zh",
                "intl.locale.requested": "zh-CN",
                "general.useragent.locale": "zh-CN",
                "media.autoplay.default": 5,
                "media.autoplay.blocking_policy": 2,
                "dom.ipc.processCount": 1,
                "browser.tabs.remote.autostart": False,
            }

        return opts

    async def _freeze_page(self, page: Page):
        """
        冻结指定 Page，使其不活动（暂停视频、动画、定时器等）。
        """
        try:
            await page.evaluate("""
                (() => {
                    document.querySelectorAll('video,audio').forEach(v => v.pause());
                    if (!window._freeze) {
                        window._oldSetInterval = window.setInterval;
                        window._oldRequestAnimationFrame = window.requestAnimationFrame;
                        window.setInterval = () => 0;
                        window.requestAnimationFrame = () => {};
                        window._freeze = true;
                    }
                })()
            """)
        except Exception:
            pass

    async def _unfreeze_page(self, page: Page):
        """
        解冻指定 Page，恢复被 _freeze_page 接管的定时器与动画帧回调。

        注意：必须把 setInterval / requestAnimationFrame **真正还原**。
        早期版本只把 window._freeze 置 false，却让被替换成空函数的
        setInterval/rAF 一直留着，导致页面定时器永久失效——
        表现就是「点击了但页面毫无反应」（依赖定时器/动画帧的 JS 逻辑全死了）。
        """
        try:
            await page.evaluate("""
                (() => {
                    if (window._oldSetInterval) {
                        window.setInterval = window._oldSetInterval;
                        window._oldSetInterval = null;
                    }
                    if (window._oldRequestAnimationFrame) {
                        window.requestAnimationFrame = window._oldRequestAnimationFrame;
                        window._oldRequestAnimationFrame = null;
                    }
                    window._freeze = false;
                })()
            """)
        except Exception:
            pass

    # ======================================================
    # 渲染模式注入
    # ======================================================

    _RENDER_MODE_CSS = {
        "simplified": """
            *, *::before, *::after {
                backdrop-filter: none !important;
                -webkit-backdrop-filter: none !important;
                filter: none !important;
            }
        """,
        "minimal": """
            *, *::before, *::after {
                backdrop-filter: none !important;
                -webkit-backdrop-filter: none !important;
                filter: none !important;
                animation: none !important;
                transition: none !important;
            }
            video, audio { display: none !important; }
            @font-face { font-family: initial !important; }
        """,
        "text_only": """
            /* 1. 杀掉所有布局，回归 inline 流 */
            * {
                display: block !important;
                float: none !important;
                position: static !important;
                flex: none !important;
                grid: none !important;
                flex-direction: column !important;
                order: 0 !important;
            }
            /* 2. 清除所有视觉装饰 */
            *, *::before, *::after {
                backdrop-filter: none !important;
                -webkit-backdrop-filter: none !important;
                filter: none !important;
                animation: none !important;
                transition: none !important;
                text-shadow: none !important;
                box-shadow: none !important;
                border: none !important;
                outline: none !important;
                background: transparent !important;
                background-image: none !important;
                background-color: transparent !important;
                border-radius: 0 !important;
                color: #000 !important;
            }
            /* 3. 清除间距 */
            * {
                margin: 0 !important;
                padding: 0 !important;
                gap: 0 !important;
                min-width: 0 !important;
                min-height: 0 !important;
                max-width: none !important;
                max-height: none !important;
                width: auto !important;
                height: auto !important;
            }
            /* 4. 隐藏所有非文本、非交互元素 */
            img, video, audio, canvas, svg, iframe,
            object, embed, picture, source, figure,
            nav, header, footer, aside, section, article,
            .icon, .emoji, [role="img"],
            [style*="background-image"],
            [style*="background: url"] {
                display: none !important;
            }
            /* 5. 保留输入框和按钮的基本可用性 */
            input, textarea, button, select, a {
                display: inline !important;
                padding: 2px 4px !important;
                border: 1px solid #999 !important;
                background: #fff !important;
                width: auto !important;
                height: auto !important;
            }
        """,
    }

    # 各模式拦截的资源类型
    _BLOCKED_RESOURCE_TYPES: dict[str, set[str]] = {
        "simplified": {"font", "media"},
        "minimal": {"font", "media", "stylesheet", "image"},
        "text_only": {"font", "media", "stylesheet", "image", "script"},
    }

    async def _setup_render_mode_route(self, page: Page):
        """在页面创建时绑定渲染模式路由，之后所有导航自动拦截。"""
        mode = self.config.get("browser_render_mode", "full")
        blocked = self._BLOCKED_RESOURCE_TYPES.get(mode, set())
        if not blocked:
            return
        try:
            # 清除该页面上已有的 **/* 路由
            await page.unroute("**/*")
        except Exception:
            pass
        try:
            # 绑定新路由
            async def _intercept(route):
                if route.request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()
            await page.route("**/*", _intercept)
        except Exception:
            pass

    async def _apply_render_mode(self, page: Page):
        """注入渲染模式 CSS（路由已在页面创建时绑定）。"""
        mode = self.config.get("browser_render_mode", "full")
        if mode == "full":
            return

        css = self._RENDER_MODE_CSS.get(mode)
        if css:
            try:
                await page.evaluate("""
                    (cssText) => {
                        if (!window._renderModeStyle) {
                            const s = document.createElement('style');
                            s.id = '__render_mode__';
                            document.head.appendChild(s);
                            window._renderModeStyle = s;
                        }
                        window._renderModeStyle.textContent = cssText;
                    }
                """, css)
            except Exception:
                pass

    # ======================================================
    # 内部保障
    # ======================================================

    def _require_context(self) -> BrowserContext:
        if self._terminated:
            raise RuntimeError("BrowserManager 已终止")
        if self.context is None:
            raise RuntimeError("BrowserContext 未初始化")
        return self.context

    async def _ensure_page(self, index: int | None = None) -> Page:
        context = self._require_context()

        if not self.all_pages:
            # 初始化第一页
            page = await context.new_page()
            await self._setup_render_mode_route(page)
            await self._safe_await(lambda: page.goto(self.config["default_url"]))
            self.all_pages.append(page)
            self.current_index = 0
            self.page = page
            return page

        if index is None:
            index = self.current_index or 0

        # 保证索引合法
        index = max(0, min(index, len(self.all_pages) - 1))

        # 切换前冻结旧 Page
        if self.page is not None and index != self.current_index:
            await self._freeze_page(self.page)

        # 更新索引和当前页
        self.current_index = index
        self.page = self.all_pages[index]

        # 激活新 Page（解除冻结）
        await self._unfreeze_page(self.page)

        return self.page

    async def save_cookies(self):
        if not self.context:
            return
        cookies: list[Cookie] = await self.context.cookies()
        self.cookie.save_cookies(cookies)  # type: ignore

    # ======================================================
    # 标签页管理
    # ======================================================

    async def get_all_tabs_titles(self) -> list[str]:
        async with self._op_lock:
            return await asyncio.gather(*(p.title() for p in self.all_pages))

    async def tabs_detail(self) -> list[dict]:
        """标签页清单（供 WebUI 展示与管理）。

        返回每项的 index / title / url / active，标题取不到时回退到 URL。
        """
        async with self._op_lock:
            cur = self.current_index if self.current_index is not None else 0
            items: list[dict] = []
            for i, p in enumerate(list(self.all_pages)):
                try:
                    title = await p.title()
                except Exception:
                    title = ""
                try:
                    url = p.url
                except Exception:
                    url = ""
                items.append({
                    "index": i,
                    "title": title or url or "(无标题)",
                    "url": url,
                    "active": i == cur,
                })
            return items

    async def new_tab(self, url: str = "") -> str:
        """新建标签页（不传 url 则打开默认页）。"""
        target = (url or "").strip() or self.config.get("default_url") or "about:blank"
        async with self._op_lock:
            context = self._require_context()
            while len(self.all_pages) >= self.config.get("max_pages", 10):
                old = self.all_pages.pop(0)
                await self._discard_page(old)
            page = await context.new_page()
            await self._setup_render_mode_route(page)
            try:
                await self._safe_await(
                    lambda: page.goto(target, wait_until="domcontentloaded"),
                )
            except Exception:
                await self._discard_page(page)
                return "新标签页打开失败"
            self.all_pages.append(page)
            self.current_index = len(self.all_pages) - 1
            self.page = page
            await self.save_cookies()
            return f"已新建标签页：{target}"

    async def close_all_tabs(self, keep_one: bool = True) -> str:
        """一键关闭所有标签页。

        :param keep_one: 是否保留（并重建）一个空白标签页。浏览器上下文
                         一个页面都不剩时很多操作会异常，故默认保留一个。
        """
        async with self._op_lock:
            closed = len(self.all_pages)
            for page in list(self.all_pages):
                try:
                    await page.close()
                except Exception:
                    pass
            self.all_pages.clear()
            self.current_index = None
            self.page = None

            if keep_one:
                try:
                    await self._ensure_page()
                except Exception as e:
                    logger.warning(f"[Browser] 关闭全部标签页后重建失败: {e}")
            return f"已关闭 {closed} 个标签页"

    async def switch_tab(self, index: int) -> str | None:
        async with self._op_lock:
            if not (0 <= index < len(self.all_pages)):
                return f"无效的标签页序号 {index}"
            await self._ensure_page(index)

    async def close_tab(self, index: int) -> str:
        async with self._op_lock:
            if not (0 <= index < len(self.all_pages)):
                return f"无效的标签页序号 {index}"

            page = self.all_pages[index]
            title = await page.title()
            await self._discard_page(page)
            return f"已关闭标签页【{title}】"

    # ======================================================
    # 页面展示
    # ======================================================

    async def zoom_to_scale(self, scale: float) -> str | None:
        async with self._op_lock:
            page = await self._ensure_page()
            await page.evaluate(f"document.body.style.zoom = {scale};")
            return None

    async def screenshot(
        self,
        zoom_factor: float | None = None,
        full_page: bool = False,
    ) -> str | None:
        async with self._op_lock:
            page = await self._ensure_page()

            # 注入渲染模式 CSS
            await self._apply_render_mode(page)

            # 确定截图格式
            fmt = get_format_from_config(self.config)  # webp / png / jpg (实时读取)
            # Playwright 只支持 png / jpeg，webp 必须先存 PNG 再转换
            pw_format = "png" if fmt == "webp" else IMG_FORMAT_MAP.get(fmt, "jpeg").lower()
            quality = min(self.config.get("screenshot_quality", 80), 100)

            async def _shot() -> bytes | None:
                if zoom_factor:
                    await page.evaluate(f"document.body.style.zoom = {zoom_factor};")
                    await page.evaluate("window.scrollTo(0, 0);")

                # 优先用 CDP 直接截图：Playwright 的 page.screenshot() 在截图前会等待
                # document.fonts.ready（所有字体加载完成），遇到字体请求卡住不返回的
                # 站点（如百度首页）会一直等到超时，导致「点击成功但截图失败」。
                # CDP 的 Page.captureScreenshot 不做这一等待，实测 0.1 秒返回。
                raw = await self._cdp_screenshot(page, pw_format, quality, full_page)
                if raw is not None:
                    return raw

                # 回退：CDP 不可用时用原生方式（并加超时保护，避免拖死整个流程）
                shot_kwargs: dict[str, Any] = {"full_page": full_page}
                if pw_format != "png":
                    shot_kwargs["quality"] = quality
                try:
                    return await page.screenshot(type=pw_format, timeout=15000, **shot_kwargs)
                except Exception as e:
                    logger.warning(f"[Browser] 原生截图也失败: {str(e)[:160]}")
                    return None

            raw: bytes = await _shot()

            if raw is None:  # 截图失败
                return None

            # ========== 落地到缓存文件 ==========
            # 先存为 PNG 临时文件，再用 Pillow 转换为目标格式
            tmp_name = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.png"
            tmp_path = self.cache_dir / tmp_name
            tmp_path.write_bytes(raw)

            if fmt != "png":
                try:
                    from .image_utils import convert_image_format
                    final_path = convert_image_format(
                        str(tmp_path), output_format=fmt,
                        quality=self.config.get("screenshot_quality", 80),
                        overwrite=True,
                    )
                    return final_path
                except Exception as e:
                    logger.warning(f"截图格式转换失败，使用 PNG: {e}")
                    return str(tmp_path)
            else:
                return str(tmp_path)

    async def _cdp_screenshot(self, page, pw_format: str, quality: int,
                              full_page: bool) -> bytes | None:
        """
        用 CDP 直接截图，绕过 Playwright 的「等待字体加载完成」环节。

        某些站点存在长期挂起的字体请求，Playwright 原生截图会一直等到超时；
        CDP 的 Page.captureScreenshot 不做该等待，可稳定秒级返回。
        失败时返回 None，由调用方回退到原生截图。
        """
        client = None
        try:
            client = await page.context.new_cdp_session(page)
            params: dict[str, Any] = {
                "format": pw_format,
                "fromSurface": True,
                "captureBeyondViewport": bool(full_page),
            }
            if pw_format != "png":
                params["quality"] = quality
            if full_page:
                metrics = await client.send("Page.getLayoutMetrics")
                css = metrics.get("cssContentSize") or metrics.get("contentSize")
                if css:
                    params["clip"] = {
                        "x": 0,
                        "y": 0,
                        "width": css.get("width", 0),
                        "height": css.get("height", 0),
                        "scale": 1,
                    }
            resp = await asyncio.wait_for(
                client.send("Page.captureScreenshot", params), timeout=20
            )
            data = resp.get("data") if isinstance(resp, dict) else None
            if not data:
                return None
            import base64 as _b64
            return _b64.b64decode(data)
        except Exception as e:
            logger.debug(f"[Browser] CDP 截图不可用，将回退原生方式: {str(e)[:160]}")
            return None
        finally:
            if client is not None:
                try:
                    await client.detach()
                except Exception:
                    pass

    # ======================================================
    # 浏览器接管：原始输入事件 + 实时投屏
    # ======================================================
    # 这些方法不做坐标钳制、不做等待，尽量贴近"真人操作"，
    # 供 WebUI 接管模式把用户操作原样转发进浏览器。

    async def mouse_move_raw(self, x: int, y: int) -> None:
        page = await self._ensure_page()
        await page.mouse.move(x, y)

    async def mouse_down_raw(self, x: int, y: int, button: str = "left") -> None:
        page = await self._ensure_page()
        await page.mouse.move(x, y)
        await page.mouse.down(button=button)

    async def mouse_up_raw(self, x: int, y: int, button: str = "left") -> None:
        page = await self._ensure_page()
        await page.mouse.move(x, y)
        await page.mouse.up(button=button)

    async def click_raw(self, x: int, y: int, button: str = "left") -> None:
        """
        接管专用：纯鼠标点击。

        与 click_coord 的区别：
        - 不做弹窗监听（那是给 AI 工具用的，需要感知新标签页）
        - 点击后**不 sleep 2 秒、不持有 _op_lock**
        这两个特性对真人操作是致命的：一次点击会把后续所有输入卡住 2 秒以上，
        连点、拖动都会严重延迟，表现为「点了没反应」。
        """
        page = await self._ensure_page()
        await page.mouse.click(x, y, button=button)

    async def wheel_raw(self, x: int, y: int, dx: float = 0, dy: float = 0) -> None:
        page = await self._ensure_page()
        await page.mouse.move(x, y)
        await page.mouse.wheel(dx, dy)

    async def type_text_raw(self, text: str) -> None:
        """逐字输入，模拟真人打字节奏。"""
        page = await self._ensure_page()
        await page.keyboard.type(str(text), delay=30)

    async def press_key_raw(self, key: str) -> None:
        page = await self._ensure_page()
        await page.keyboard.press(str(key))

    async def touch_raw(self, kind: str, x: int, y: int) -> None:
        """
        移动端真触摸事件（CDP Input.dispatchTouchEvent）。

        kind: start / move / end

        注意：CDP 要求同一触摸序列（start→move→end）必须复用**同一个 CDP session**，
        否则 touchEnd 会报 "Must send a TouchStart first to start a new touch"。
        因此这里为触摸序列维持一个持久 session，序列结束后才释放。
        """
        page = await self._ensure_page()
        type_map = {
            "start": "touchStart",
            "move": "touchMove",
            "end": "touchEnd",
        }
        cdp_type = type_map.get(kind)
        if not cdp_type:
            return

        # 复用或新建触摸 session
        client = getattr(self, "_touch_client", None)
        if client is None or kind == "start":
            if client is not None:
                try:
                    await client.detach()
                except Exception:
                    pass
            client = await page.context.new_cdp_session(page)
            self._touch_client = client

        try:
            if kind == "end":
                await client.send("Input.dispatchTouchEvent",
                                  {"type": cdp_type, "touchPoints": []})
            else:
                await client.send("Input.dispatchTouchEvent", {
                    "type": cdp_type,
                    "touchPoints": [{"x": x, "y": y}],
                })
        finally:
            # 序列结束后释放 session
            if kind == "end":
                try:
                    await client.detach()
                except Exception:
                    pass
                self._touch_client = None

    async def release_touch(self) -> None:
        """兜底释放可能残留的触摸 session（例如用户中途松手回到前台）。"""
        client = getattr(self, "_touch_client", None)
        if client is None:
            return
        try:
            await client.send("Input.dispatchTouchEvent",
                              {"type": "touchEnd", "touchPoints": []})
        except Exception:
            pass
        try:
            await client.detach()
        except Exception:
            pass
        self._touch_client = None

    async def start_screencast(self, on_frame=None, quality: int = 70,
                               max_width: int = 1280) -> str | None:
        """
        启动 CDP 实时投屏（变化驱动推帧）。

        :param on_frame: 收到帧时的回调，签名 (jpeg_bytes, (w, h)) -> None
        :return: 投屏会话标识；失败返回 None
        """
        page = await self._ensure_page()
        client = await page.context.new_cdp_session(page)
        self._screencast_client = client
        self._screencast_frames = 0

        def _on_frame(params):
            try:
                data = params.get("data", "")
                if not data or on_frame is None:
                    return
                import base64 as _b64
                raw = _b64.b64decode(data)
                meta = params.get("metadata") or {}
                size = (int(meta.get("deviceWidth") or 0),
                        int(meta.get("deviceHeight") or 0))
                on_frame(raw, size if size[0] else None)
                self._screencast_frames += 1
                # 必须回 ACK，否则浏览器不再推新帧
                asyncio.ensure_future(
                    client.send("Page.screencastFrameAck",
                                {"sessionId": params.get("sessionId")})
                )
            except Exception as e:
                logger.debug(f"[Browser] 投屏帧处理失败: {e}")

        client.on("Page.screencastFrame", _on_frame)

        # 画质/尺寸按配置（自动检测的结果会写回配置）
        q = min(int(self.config.get("takeover_jpeg_quality", quality) or quality), 100)
        mw = int(self.config.get("takeover_max_width", max_width) or max_width)
        await client.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": max(10, q),
            "maxWidth": max(320, mw),
            "maxHeight": max(240, int(mw * 0.75)),
            "everyNthFrame": 1,
        })
        logger.info(f"[Browser] 实时投屏已启动 quality={q} maxWidth={mw}")
        return f"screencast-{id(client)}"

    async def stop_screencast(self) -> None:
        client = getattr(self, "_screencast_client", None)
        if client is None:
            return
        try:
            await client.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            await client.detach()
        except Exception:
            pass
        self._screencast_client = None
        logger.info("[Browser] 实时投屏已停止")

    async def viewport_size(self) -> tuple[int, int]:
        """当前视口尺寸（供接管坐标换算）。"""
        try:
            page = await self._ensure_page()
            vs = page.viewport_size
            if vs:
                return int(vs["width"]), int(vs["height"])
        except Exception:
            pass
        cfg = self.config.get("viewport_size") or {}
        return int(cfg.get("width", 1280)), int(cfg.get("height", 720))

    # ======================================================
    # 页面访问
    # ======================================================

    async def search(self, url: str) -> str | None:
        async with self._op_lock:
            for i, p in enumerate(self.all_pages):
                if p.url == url:
                    await self._ensure_page(i)
                    return None

            while len(self.all_pages) > self.config["max_pages"]:
                old_page = self.all_pages.pop(0)
                await self._discard_page(old_page)

            page = await self._require_context().new_page()
            await self._setup_render_mode_route(page)

            try:
                await self._safe_await(
                    lambda: page.goto(url, wait_until="domcontentloaded"),
                )
                await page.evaluate(
                    f"document.body.style.zoom = {self.config['zoom_factor']};"
                )
            except Exception:
                await self._discard_page(page)
                return "URL 访问失败"

            self.all_pages.append(page)
            self.current_index = len(self.all_pages) - 1
            self.page = page

            await self.save_cookies()
            return None

    # ======================================================
    # 页面交互
    # ======================================================
    async def click_coord(self, coords: Sequence[int]) -> str | None:
        if len(coords) != 2:
            return "坐标参数格式错误"

        x, y = map(int, coords)

        async with self._op_lock:
            page = await self._ensure_page()
            new_page: Page | None = None

            # 弹窗回调：新页面产生后立即替换当前页
            def on_popup(popup: Page):
                nonlocal new_page, page
                new_page = popup
                # 切换当前页到 popup
                self.all_pages.append(popup)
                self.current_index = len(self.all_pages) - 1
                self.page = popup

            page.on("popup", on_popup)

            try:
                await self._safe_page_op(
                    page,
                    self._safe_await(lambda: page.mouse.click(x, y, delay=100)),
                )
                await asyncio.sleep(2)

            finally:
                page.remove_listener("popup", on_popup)

        return None

    async def scroll_by(self, distance: int, direction: str) -> str | None:
        async with self._op_lock:
            page = await self._ensure_page()

            dx = dy = 0
            if direction == "上":
                dy = -distance
            elif direction == "下":
                dy = distance
            elif direction == "左":
                dx = -distance
            elif direction == "右":
                dx = distance
            else:
                return "无效的滚动方向"

            await self._safe_page_op(
                page,
                page.evaluate(f"window.scrollBy({dx}, {dy});"),
            )
            return None

    async def swipe(self, coords: Sequence[int]) -> str | None:
        """兼容保留：等价于 drag。"""
        if len(coords) != 4:
            return "滑动参数格式错误"
        sx, sy, ex, ey = coords
        return await self.drag(sx, sy, ex, ey)

    async def text_input(self, text: str, enter: bool = True) -> str | None:
        async with self._op_lock:
            page = await self._ensure_page()
            await page.wait_for_load_state("load")

            inputs = await page.query_selector_all(
                "input:not([disabled]):not([readonly])"
            )

            for el in inputs:
                if await el.is_visible():
                    await el.fill(text)
                    if enter:
                        await page.keyboard.press("Enter")
                    return None

            return "未找到可用的输入框"

    # ======================================================
    # 坐标交互原语（AI 视觉定位 → 坐标操作）
    # 坐标系：与 screenshot() 返回的截图像素一一对应（当前视口）
    # ======================================================

    def _viewport(self) -> tuple[int, int]:
        """返回当前视口尺寸 (w, h)。"""
        try:
            vs = self.config.get("viewport_size") or {}
            return int(vs.get("width", 1280)), int(vs.get("height", 720))
        except Exception:
            return 1280, 720

    def _clamp_coord(self, x: int, y: int) -> tuple[int, int]:
        """把坐标限制在视口范围内，越界时贴边并记录。"""
        w, h = self._viewport()
        cx, cy = max(0, min(int(x), w - 1)), max(0, min(int(y), h - 1))
        if (cx, cy) != (int(x), int(y)):
            logger.debug(f"[Browser] 坐标越界已贴边: ({x},{y}) -> ({cx},{cy})")
        return cx, cy

    async def long_press(self, x: int, y: int, duration_ms: int = 1000) -> str | None:
        """在坐标处长按（按下并保持一段时间后松开）。"""
        async with self._op_lock:
            page = await self._ensure_page()
            x, y = self._clamp_coord(x, y)
            duration_ms = max(100, min(int(duration_ms), 10000))
            try:
                await self._safe_page_op(
                    page,
                    self._safe_await(
                        lambda: page.mouse.move(x, y)
                    ),
                )
                await self._safe_page_op(
                    page,
                    self._safe_await(
                        lambda: page.mouse.down()
                    ),
                )
                await asyncio.sleep(duration_ms / 1000)
                await self._safe_page_op(
                    page,
                    self._safe_await(
                        lambda: page.mouse.up()
                    ),
                )
                return None
            except Exception as e:
                return f"长按失败: {str(e)[:200]}"

    async def drag(self, start_x: int, start_y: int,
                   end_x: int, end_y: int,
                   duration_ms: int = 600) -> str | None:
        """从起点坐标拖拽到终点坐标（按住不放移动，模拟真实拖动）。"""
        async with self._op_lock:
            page = await self._ensure_page()
            sx, sy = self._clamp_coord(start_x, start_y)
            ex, ey = self._clamp_coord(end_x, end_y)
            duration_ms = max(100, min(int(duration_ms), 10000))
            # 按拖动距离估算插值步数（每步约 8px）
            dist = max(abs(ex - sx), abs(ey - sy))
            steps = max(5, min(dist // 8 if dist > 0 else 5, 60))
            try:
                await self._safe_page_op(
                    page,
                    self._safe_await(lambda: page.mouse.move(sx, sy)),
                )
                await self._safe_page_op(
                    page,
                    self._safe_await(lambda: page.mouse.down()),
                )
                await self._safe_page_op(
                    page,
                    self._safe_await(
                        lambda: page.mouse.move(ex, ey, steps=steps)
                    ),
                )
                await asyncio.sleep(duration_ms / 1000)
                await self._safe_page_op(
                    page,
                    self._safe_await(lambda: page.mouse.up()),
                )
                return None
            except Exception as e:
                return f"拖拽失败: {str(e)[:200]}"

    async def input_at(self, x: int, y: int, text: str,
                       press_enter: bool = False) -> str | None:
        """点击坐标处（聚焦输入框），然后键入文字。"""
        async with self._op_lock:
            page = await self._ensure_page()
            x, y = self._clamp_coord(x, y)
            try:
                await self._safe_page_op(
                    page,
                    self._safe_await(
                        lambda: page.mouse.click(x, y, delay=60)
                    ),
                )
                await asyncio.sleep(0.2)
                await self._safe_page_op(
                    page,
                    self._safe_await(lambda: page.keyboard.type(text, delay=30)),
                )
                if press_enter:
                    await self._safe_page_op(
                        page,
                        self._safe_await(lambda: page.keyboard.press("Enter")),
                    )
                return None
            except Exception as e:
                return f"输入失败: {str(e)[:200]}"

    async def double_click(self, x: int, y: int) -> str | None:
        """在坐标处双击。"""
        async with self._op_lock:
            page = await self._ensure_page()
            x, y = self._clamp_coord(x, y)
            try:
                await self._safe_page_op(
                    page,
                    self._safe_await(lambda: page.mouse.dblclick(x, y)),
                )
                return None
            except Exception as e:
                return f"双击失败: {str(e)[:200]}"

    async def right_click(self, x: int, y: int) -> str | None:
        """在坐标处右键单击（打开上下文菜单）。"""
        async with self._op_lock:
            page = await self._ensure_page()
            x, y = self._clamp_coord(x, y)
            try:
                await self._safe_page_op(
                    page,
                    self._safe_await(lambda: page.mouse.click(x, y, button="right")),
                )
                return None
            except Exception as e:
                return f"右键失败: {str(e)[:200]}"

    async def hover(self, x: int, y: int) -> str | None:
        """悬停在坐标处（触发悬浮菜单/提示）。"""
        async with self._op_lock:
            page = await self._ensure_page()
            x, y = self._clamp_coord(x, y)
            try:
                await self._safe_page_op(
                    page,
                    self._safe_await(lambda: page.mouse.move(x, y)),
                )
                await asyncio.sleep(0.3)
                return None
            except Exception as e:
                return f"悬停失败: {str(e)[:200]}"

    async def go_back(self) -> str | None:
        async with self._op_lock:
            page = await self._ensure_page()
            await page.go_back()
            await page.wait_for_load_state("load")
            return None

    async def go_forward(self) -> str | None:
        async with self._op_lock:
            page = await self._ensure_page()
            await page.go_forward()
            await page.wait_for_load_state("load")
            return None

    async def chat_send(
        self,
        text: str,
        input_selector: str,
        send_selector: str = "",
        wait_ms: int = 1500,
    ) -> str | None:
        async with self._op_lock:
            page = await self._ensure_page()
            await page.wait_for_load_state("domcontentloaded")

            selector = input_selector.strip() or "textarea, div[contenteditable='true'], input[type='text']"
            el = await page.query_selector(selector)
            if el is None:
                return f"未找到输入框，请检查 chat_input_selector：{selector}"

            await el.click()
            try:
                select_all_hotkey = (
                    "Meta+A" if platform.system().lower() == "darwin" else "Control+A"
                )
                await page.keyboard.press(select_all_hotkey)
                await page.keyboard.press("Backspace")
            except Exception:
                pass

            await page.keyboard.type(text, delay=20)

            if send_selector.strip():
                btn = await page.query_selector(send_selector)
                if btn is None:
                    return f"未找到发送按钮，请检查 chat_send_selector：{send_selector}"
                await btn.click()
            else:
                await page.keyboard.press("Enter")

            await asyncio.sleep(max(wait_ms, 0) / 1000)
            return None
