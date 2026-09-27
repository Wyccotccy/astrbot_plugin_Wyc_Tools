# browser_downloader.py
"""
Production-ready Playwright browser downloader

特性：
- Virtualenv safe
- Concurrent safe
- 同一浏览器只允许一个下载任务
- 重复触发会复用当前下载任务
- 返回 (bool, message)
- 下载完成后自动验证可启动性
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from astrbot.api import logger


class BrowserDownloader:
    _SUPPORTED = {"firefox", "chromium", "webkit"}

    # 全局锁：防止 playwright install 竞态（跨实例共享）
    _global_lock = asyncio.Lock()

    # ★ 验证锁：避免多处并发启动 chromium 互相踩踏
    _verify_lock = asyncio.Lock()

    # ★ 每个 browser 一个下载任务
    _download_tasks: dict[str, asyncio.Task] = {}

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.browsers_dir = data_dir / "browsers"
        self.browsers_dir.mkdir(parents=True, exist_ok=True)

        self.env = os.environ.copy()
        self.env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.browsers_dir)

        # 同步到当前进程，供 async_playwright 使用
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(self.browsers_dir)

        logger.debug(f"PLAYWRIGHT_BROWSERS_PATH = {self.browsers_dir}")

    # ================== public ==================

    async def ensure_playwright_runtime(self) -> tuple[bool, str]:
        """仅确保 playwright Python 包可用（不下载浏览器内核）"""
        return await self._ensure_playwright()

    async def download(self, browser: str) -> tuple[bool, str]:
        """
        下载指定浏览器（幂等）
        - 返回 (success, message)
        - 若已有下载任务，直接复用
        """
        if browser not in self._SUPPORTED:
            return False, f"不支持的浏览器类型: {browser}"

        # ★ 已有下载任务 → 复用
        task = self._download_tasks.get(browser)
        if task:
            logger.info(f"{browser} 已有下载任务，复用中")
            return await task

        # ★ 创建新任务
        task = asyncio.create_task(self._download_impl(browser))
        self._download_tasks[browser] = task

        try:
            return await task
        finally:
            # 清理 task（无论成功失败）
            self._download_tasks.pop(browser, None)

    # ================== core ==================

    async def _download_impl(self, browser: str) -> tuple[bool, str]:
        async with self._global_lock:
            ok, msg = await self._ensure_playwright()
            if not ok:
                return False, msg

            if await self._browser_installed(browser):
                logger.info(f"{browser} 已存在，进行完整性验证")
                if await self.verify_browser(browser, browsers_dir=self.browsers_dir):
                    return True, f"{browser} 已安装且可用"
                else:
                    logger.warning(f"{browser} 已存在但不可用，重新安装")

            ok, msg = await self._install_browser(browser)
            if not ok:
                return False, msg

            if await self.verify_browser(browser, browsers_dir=self.browsers_dir):
                return True, f"{browser} 下载并验证成功"

            return False, f"{browser} 下载完成，但启动验证失败"

    # ================== playwright ==================

    async def _ensure_playwright(self) -> tuple[bool, str]:
        if await self._run("playwright", "--version"):
            return True, "playwright 已就绪"

        logger.info("playwright 未安装，开始安装（当前虚拟环境）")

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pip",
            "install",
            "-U",
            "playwright",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")
            logger.error(f"pip install playwright 失败:\n{err}")
            return False, "playwright 安装失败"

        if await self._run("playwright", "--version"):
            return True, "playwright 安装成功"

        return False, "playwright 安装完成但无法运行"

    # ================== browser ==================

    async def _browser_installed(self, browser: str) -> bool:
        if not self.browsers_dir.exists():
            return False

        prefix = f"{browser}-"
        try:
            return any(
                p.is_dir() and p.name.startswith(prefix)
                for p in self.browsers_dir.iterdir()
            )
        except Exception:
            return False

    async def _install_browser(self, browser: str) -> tuple[bool, str]:
        logger.info(f"开始下载 {browser}")

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install",
            browser,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            logger.info(f"{browser} 下载完成")
            return True, f"{browser} 下载完成"

        out = stdout.decode(errors="ignore")
        err = stderr.decode(errors="ignore")
        logger.error(f"{browser} 下载失败\nstdout:\n{out}\nstderr:\n{err}")
        return False, f"{browser} 下载失败"

    # ================== 系统依赖（缺失库） ==================

    @staticmethod
    async def install_system_deps(browser: str = "webkit") -> tuple[bool, str]:
        """安装浏览器所需的系统库（仅 Linux 容器内有效）。

        背景：Playwright 的 webkit / firefox 依赖大量系统共享库
        （libgstreamer / libgtk-4 / libicudata / libepoxy ...）。
        这些库不在浏览器包内，必须由系统包管理器提供；
        缺库时的报错是：
            Host system is missing dependencies to run browsers.

        做法：调用 `playwright install-deps <browser>`，
        它会用容器内的 apt/dnf/apk 装官方认定的完整依赖集。
        需要 root 权限，失败时返回原因供上层提示用户。

        :return: (success, message)
        """
        if os.name != "posix":
            return False, "当前系统不支持自动安装系统依赖"

        # 只有 root 才能装包
        try:
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                return False, "需要 root 权限才能安装系统依赖"
        except Exception:
            pass

        logger.info(f"开始安装 {browser} 的系统依赖（可能需要几分钟）")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "playwright",
                "install-deps",
                browser,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return False, "安装系统依赖超时（15 分钟）"

            if proc.returncode == 0:
                logger.info(f"{browser} 系统依赖安装完成")
                return True, f"{browser} 系统依赖安装完成"

            err = stderr.decode(errors="ignore")[-600:]
            logger.error(f"{browser} 系统依赖安装失败:\n{err}")
            return False, f"系统依赖安装失败（返回码 {proc.returncode}）"
        except FileNotFoundError:
            return False, "未找到 apt/dnf/apk，本机可能不是常见 Linux 发行版"
        except Exception as e:
            logger.error(f"{browser} 系统依赖安装异常: {e}")
            return False, f"系统依赖安装异常: {str(e)[:200]}"

    @staticmethod
    def _is_missing_deps_error(err_text: str) -> bool:
        """判断报错是否为「缺系统库」。"""
        t = (err_text or "").lower()
        return ("missing dependencies" in t
                or "host system is missing" in t
                or "error while loading shared libraries" in t)

    @staticmethod
    async def verify_browser(browser: str, retries: int = 2,
                             auto_install_deps: bool = True,
                             browsers_dir: str | Path | None = None) -> bool:
        """真正启动一次浏览器，验证可用性。

        - 用 _verify_lock 串行化：避免与 supervisor / main 的并发 launch 冲突
        - 带重试：浏览器启动是瞬时资源竞争，偶发失败可自愈
        - 显式设置参数：容器内 root 运行需要 --no-sandbox
        - **缺系统库自动补装**：webkit/firefox 依赖 gtk/gstreamer 等系统库，
          缺失时报「Host system is missing dependencies」。首次遇到就自动
          `playwright install-deps` 装上并重试，避免用户手动折腾。
        - **务必回收子进程**：探测用的浏览器若只 close 不 wait，
          会留下 chrome-headless 僵尸（父进程为 AstrBot），
          每次探测 +2 个，长时间运行攒出一堆 defunct 把 PID 吃满。

        :param browsers_dir: 浏览器内核目录（PLAYWRIGHT_BROWSERS_PATH）。
            不传则用进程环境变量里的值。**必须与插件实际使用的目录一致**，
            否则会去默认路径 /root/.cache/ms-playwright 找浏览器：
            找不到就再下载一份（白占几百 MB），找到了也会起一套
            与插件无关的进程。
        """
        logger.debug(f"验证 {browser} 可启动性")
        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError:
            return False

        # 确保子进程用对的浏览器目录（关键：避免走默认缓存路径）
        if browsers_dir:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)

        args = [
            "--mute-audio",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            # 容器内以 root 运行，Chromium 沙箱需要显式关闭，否则可能启动失败
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ]

        deps_tried = False
        async with BrowserDownloader._verify_lock:
            for attempt in range(retries + 1):
                last_err = ""
                try:
                    async with async_playwright() as p:
                        launcher = getattr(p, browser, None)
                        if launcher is None:
                            logger.error(f"不支持的浏览器类型: {browser}")
                            return False
                        b = await asyncio.wait_for(
                            launcher.launch(headless=True, args=args), timeout=60
                        )
                        try:
                            await b.close()
                        finally:
                            # close() 只发关闭请求，不等子进程退出；
                            # 必须回收，否则留下僵尸进程
                            await BrowserDownloader._reap_children()
                    return True
                except asyncio.TimeoutError:
                    logger.warning(f"{browser} 启动验证超时（第 {attempt + 1} 次）")
                except Exception as e:
                    last_err = str(e)
                    logger.warning(
                        f"{browser} 启动验证失败（第 {attempt + 1} 次）: {last_err[:200]}"
                    )

                    # 缺系统库 → 自动补装一次，然后立即重试
                    if (auto_install_deps and not deps_tried
                            and BrowserDownloader._is_missing_deps_error(last_err)):
                        deps_tried = True
                        logger.info(f"{browser} 缺少系统依赖，尝试自动安装...")
                        ok, msg = await BrowserDownloader.install_system_deps(browser)
                        if ok:
                            # 让动态库缓存生效后再验证
                            await asyncio.sleep(1.0)
                            continue

                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))

            logger.error(f"{browser} 启动验证失败: 已重试 {retries} 次仍不可用")
            return False

    # ================== utils ==================

    @staticmethod
    async def reap_zombies(log: bool = False) -> int:
        """回收浏览器留下的僵尸子进程，返回清理数量。

        为什么需要：playwright 的 node 驱动 fork 出 chrome 子进程，
        主进程是 AstrBot。子进程退出后若父进程从不 wait，就会以
        defunct 状态留在进程表里占 PID（不占内存，但一直涨）。

        ⚠️ 为什么不用 os.waitpid：
        在 asyncio 事件循环下，SIGCHLD 由 asyncio 的子进程监视器接管，
        直接调 waitpid 往往抢不到（返回 0 / ChildProcessError），
        实测无法清除 playwright 留下的僵尸。

        改用 psutil 遍历：僵尸（STATUS_ZOMBIE）本身无法被 kill，
        真正的解法是**让父进程 wait**。但这里的僵尸父进程是 AstrBot 自身
        （python main.py），我们无法替它 wait 任意子进程。

        因此实际可行的做法是：
        1. 对这些僵尸调用 os.waitpid(pid, WNOHANG) 逐个尝试收割；
        2. 若仍失败（asyncio 接管了 SIGCHLD），退而求其次记录数量，
           由调用方决定是否告警。

        经验：真正有效的预防是**别让它们产生**——
        verify_browser 每次探测都会起一轮浏览器，所以已把探测频率降到最低，
        并在此处尽力收割。
        """
        reaped = 0
        try:
            import os as _os

            import psutil as _ps

            for proc in _ps.process_iter(["pid", "status"]):
                try:
                    if proc.info.get("status") != _ps.STATUS_ZOMBIE:
                        continue
                    pid = proc.info["pid"]
                    # 优先用 waitpid 精确收割这个 pid
                    try:
                        done, _st = _os.waitpid(pid, _os.WNOHANG)
                        if done:
                            reaped += 1
                    except ChildProcessError:
                        # 不是我们的子进程（或已被回收），跳过
                        continue
                    except OSError:
                        continue
                except (_ps.NoSuchProcess, _ps.AccessDenied):
                    continue
        except Exception:
            pass

        if reaped and log:
            logger.info(f"[Browser] 已回收 {reaped} 个僵尸子进程")
        return reaped

    @staticmethod
    def count_zombies() -> int:
        """统计当前浏览器相关僵尸进程数量（供 WebUI 展示）。"""
        try:
            import psutil as _ps

            n = 0
            for proc in _ps.process_iter(["name", "status"]):
                try:
                    if proc.info.get("status") != _ps.STATUS_ZOMBIE:
                        continue
                    name_low = (proc.info.get("name") or "").lower()
                    if any(h in name_low for h in ("chrome", "headless",
                                                   "firefox", "webkit")):
                        n += 1
                except (_ps.NoSuchProcess, _ps.AccessDenied):
                    continue
            return n
        except Exception:
            return 0

    # 兼容旧名字（内部调用）
    @staticmethod
    async def _reap_children() -> None:
        await BrowserDownloader.reap_zombies()
        await asyncio.sleep(0)

    async def _run(self, *args: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            *args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.env,
        )
        await proc.communicate()
        return proc.returncode == 0
