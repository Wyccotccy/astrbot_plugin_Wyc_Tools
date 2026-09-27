## [5.6.3] - 2026-09-27

### 🐛 已知 Bug 修复 + 接管页监控增强

#### 1. 修复僵尸进程泄漏（严重）

**现象**：容器内累积大量 `chrome-headless` 僵尸进程（defunct），
父进程为 AstrBot 自身，不占内存但持续吞噬 PID，长时间运行会耗尽进程表。

**根因**：`BrowserDownloader.verify_browser()` 是 staticmethod，
启动探测时**没有设置 `PLAYWRIGHT_BROWSERS_PATH`**，
于是走 playwright 默认缓存路径 `/root/.cache/ms-playwright`，
而插件内核实际装在 `data/plugin_data/.../browsers`。后果有两个：

1. 默认路径下找不到内核 → 又下载了一整份（**白占 646 MB 磁盘**）；
2. 探测用的浏览器进程与插件目录脱钩，`close()` 后子进程无人回收，
   累积成僵尸。

**修复**：
- `verify_browser()` 新增 `browsers_dir` 参数，所有调用点统一传插件的内核目录，
  从源头保证「探测用的浏览器 == 插件用的浏览器」
- 新增 `reap_zombies()` / `count_zombies()`，监控循环每轮（默认 10s）尽力回收僵尸；
  注意：僵尸的父进程是 AstrBot，asyncio 已接管 SIGCHLD，`waitpid` 往往抢不到，
  因此**根本解法是路径对齐、不产生僵尸**，回收仅作兜底
- 清理了默认缓存路径那份多余内核（646 MB）

**验证**：修复前每次探测 +4 僵尸；修复后探测 **僵尸增量 0，默认路径残留增量 0**，
重启后僵尸总数 **13 → 0**、默认路径残留 **13 → 0**。

#### 2. 修复 webkit / firefox 装上却启动不了

**现象**：`Host system is missing dependencies to run browsers`，
列出 `libgstreamer-1.0.so.0` / `libgtk-4.so.1` / `libicudata.so.76` 等 28 个缺失库。

**根因**：Playwright 的 webkit / firefox 依赖大量系统共享库，
这些库不在浏览器包内，必须由系统包管理器提供。

**修复**：
- 新增 `BrowserDownloader.install_system_deps(browser)`：
  调用 `playwright install-deps`，用容器内的 apt/dnf/apk 装官方认定的完整依赖集
  （需要 root 权限，非 root 时返回明确原因）
- `verify_browser()` 检测到「缺系统库」类报错时**自动补装一次并重试**，
  用户无需手动进容器折腾
- WebUI 接管页新增「**安装系统依赖**」按钮，一键安装并自动复验

**验证**：本机容器（Debian 13 / root / apt）执行后 28 个库全部补齐，
webkit 启动成功；依赖检测函数 5 组用例全通过。

#### 3. 接管页新增监控与管理（承接 v5.6.2 需求）

- **服务器内存进度条**：分段显示「其他占用 / 浏览器占用」，
  并标注**关闭阈值**竖线；下方列出总内存、已用占比、阈值、浏览器状态
- **实时指标**（全部在用户端计算，服务端卡住一眼可见）：
  画面延迟 / 服务器刷新率 / 下行带宽 / 累计帧数
- **标签页管理**：查看标签页列表（标题 + URL + 当前标记）、
  切换 / 关闭 / 新建 / **一键关闭所有标签页**
- **浏览器自动关闭提示**：因内存或空闲被自动关闭时，
  在接管页顶部显示原因卡片（含具体数值与解决办法），支持「不再提示」

#### 4. 关键行为修正

- **接管页标签页管理方向修正**：此前误把「接管中」当成禁止操作，
  实际 WebUI 的标签页管理**任何时候都可用**（接管中正是给用户用的）；
  AI 侧的工具调用才该被接管拦下
- **「卡住」判据修正**：画面是**变化驱动**的，静止页面本来就不推帧。
  原实现拿「无新帧」当卡住，导致一打开接管页就误报「画面停滞」。
  现改用**状态心跳**（服务端每 0.5s 推一次带时间戳的 state）判活，
  画面静止时显示「画面无变化（正常）」而非误报
- **内存阈值热更新**：`supervisor` 的阈值是构造时读一次就固定的，
  此前 WebUI 改了配置不生效（表现为「阈值一直显示旧值」）。
  新增 `_sync_supervisor_config()`，保存配置时回写
- **内存统计口径修正**：只统计浏览器进程组（含 playwright 驱动），
  刻意不含 AstrBot 自身的 python 进程；新增 `browser_procs` / `browser_zombies` 字段
- **「浏览器已就绪」误报修正**：改为句柄 + 进程双重校验，
  未运行时显示「💤 浏览器未运行（不占用内存）」
- **新增配置** `browser_memory_limit_mb`：按**浏览器自身占用**设上限，
  比百分比阈值更精准，不受服务器上其他服务影响

**验证**：后端语法/配置全通过；僵尸与 webkit 两项修复均经服务器实测确认。

---

## [5.6.2] - 2026-09-27

### ✨ 提示词注入去重融合 + 操作图标换新 + 仓库文件补齐

#### 1. 提示词注入重构（去重融合、零冲突）

原来提示词分散在多处且互相打架，现整合为三段**职责单一**的注入块：

| 注入块 | 内容 | 触发条件 |
|--------|------|----------|
| `_TOOL_RULES` | 工具调用铁律（想说不之前先搜）+ 回复发送规则（防重复回复）+ 说话风格 | 无条件注入 |
| `_MEMORY_DISCIPLINE` | 记忆强化指令 + 记忆频次硬性规定（每 8 句强制记忆一次） | 仅记忆工具可用时 |
| `_BROWSER_GUIDE` | 浏览器自动化工具使用指引（含搜索引擎支持说明） | 仅浏览器已启用时 |

- **消除冲突 ①**：浏览器工具一般需先 `search_wyc_tools` 搜索，但 `request_browser_takeover` 是免搜索直连白名单（应急用途）——在 `_BROWSER_GUIDE` 中显式写明该例外，两边不再矛盾
- **消除冲突 ②**：原「浏览器指引」称支持百度/必应/谷歌搜索，经核查 `browser_search` 工具三个引擎均已实现，说明准确，予以保留
- **条件注入省 token**：记忆强化与浏览器指引分别只在对应功能可用时注入，不用这些功能则零成本
- 全部走官方推荐的 `extra_user_content_parts` + `mark_as_temp()`，不破坏模型前缀缓存
- 新增两个 WebUI 开关：`inject_memory_discipline_enabled`、`inject_browser_guide_enabled`（默认开启）

#### 2. 操作图标换新（AI 操作位置指示）

4 个操作图标升级为**统一设计语言**：鼠标指针 + 动作徽章（原为 4 种互不相干的图形）。

| 图标 | 视觉 | 动作映射 |
|------|------|----------|
| `01-点击-click.png` | 蓝色指针 + 靶心 | click / double_click / right_click / hover |
| `02-长按-long-press.png` | 蓝色指针 + 橙色按下 | long_press |
| `03-输入-input.png` | 蓝色指针 + 绿色 I 光标 | input / input_at |
| `04-拖动-drag.png` | 蓝色指针 + 紫色四向箭头 | drag / scroll |

- 渲染尺寸 256×256 → **400×400**（更清晰）
- 四个图标共用同一条指针路径，**锚点统一为指针尖端** `(52, 30)/128` —— 即「指针指到的位置」，语义与实际操作点完全一致
- 已生成锚点可视化验证图逐一确认落位准确

#### 3. 仓库文件补齐（市场安装可用性修复）

- **修复 `.gitignore` 误屏蔽 `resource/`**：4 个操作图标此前**未随仓库分发**，用户从插件市场安装后图标功能会直接失效。移除该规则并纳入版本控制
- 补 `LICENSE`（MIT）
- 补 `requirements.txt`（声明 aiohttp / psutil / Pillow，playwright 为可选）

**验证**
- 提示词注入专项 44+ 项断言全通过（常量存在性 / 各段内容 / 实际注入 / 开关关闭 / 记忆工具禁用 / 浏览器未启用）
- 图标端到端：9/9 加载成功，`click` 与 `drag` 合成输出 1280×720 正常
- 新图标锚点核对：4/4 均为 `(0.4062, 0.2344)`，指针尖端落位正确

---

## [5.6.1] - 2026-09-26

### 🔧 合规修复：移除内置 logging 兜底

市场安全审查指出 `core/action_overlay.py` 与 `core/takeover.py` 的 `try/except` 兜底分支
使用了 Python 内置 `logging` 模块。按日志规范，logger 必须且只能从 `astrbot.api` 导入。

```python
# 修改前
try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 极端环境兜底
    import logging
    logger = logging.getLogger(__name__)

# 修改后
from astrbot.api import logger
```

全插件复查：7 个模块（main / browser / supervisor / favorite / downloader /
action_overlay / takeover）的 logger 现均来自 `astrbot.api`，无内置 `logging` 残留。

**验证**
- `py_compile` 语法通过（action_overlay / takeover / main）
- 全插件 `grep` 扫描：`import logging`、`from logging`、`logging.getLogger` 均为零命中
- 真实导入自检：两个模块的 logger 类型均为 `_PluginContextLogger`（来自 `astrbot.api`）
- 功能回归：前端 12 项、键盘 29 项、后端键盘注入 17 项、唤醒链路 14 项 —— **全部通过**

---

## [5.6.0] - 2026-09-26

### ⌨️ 接管页键盘重构：电脑端物理键直通 + 手机端虚拟键盘

原来两端共用一排简陋按钮（Esc/Enter/Tab），手机上输入极不方便，电脑上还得点按钮。

#### 电脑端：物理键盘直通
- 接管进行中且停留在接管页时，**直接敲键盘就实时输入到远端浏览器**，体验接近原生
- 单字符键走文本输入（兼容中文与输入法），功能键走按键
- **组合键（Ctrl/Cmd/Alt）与 F5/F11/F12 保留给本地**，不抢占刷新、开发者工具
- **焦点在本地输入框时不劫持**，正常打字不受影响
- 页面下方保留紧凑的桌面特殊键条：Esc / Enter / Tab / 退格 / Del / ↑↓←→ / Home / End / PgUp / PgDn

#### 手机端：完整虚拟键盘
- 补齐 **方向键（↑↓←→）、退格、删除、Home / End / PageUp / PageDown、空格**
- 按钮加大加高（`min-height: 44px`），适配触屏点按
- 输入框旁新增「清空」按钮
- 两套布局用 CSS 媒体查询自动切换，无需手动选择

#### 顺带修复：快速打字丢字

`sendAction` 原来在忙时直接丢弃**所有**非 mouse 操作，连续输入会丢字符。
现在改为：仅「鼠标移动」允许丢帧（位置类事件最新值即最优），
按键 / 点击 / 滚轮等进入队列串行发送，队列上限 60 防无限堆积。

#### 验证

| 层面 | 内容 | 结果 |
|------|------|------|
| 前端 · 电脑端 | 物理键 a/b/Enter/Backspace/ArrowUp 全部送达；连打 `hello` 不丢字；焦点在输入框时不劫持 | 通过 |
| 前端 · 手机端 | 桌面键盘隐藏、虚拟键盘显示；14 个按键全部可用；文字发送后自动清空 | 通过 |
| 后端注入 | 文本/中文/符号输入正确落到输入框；Enter/Esc/Tab/退格/删除/四方向/Home/End/PgUp/PgDn/空格 逐个命中；连发 11 字符无丢失 | 通过 |

两项合计 **48 项断言全部通过**（前端 29 + 后端 19）。

---

## [5.5.0] - 2026-09-26

### ✨ 浏览器接管改为**非阻塞模式**，彻底解除时长限制 + 修复触摸点击失效与画面闪烁

#### 1. 🔓 接管时长不再受框架超时限制（架构改造）

**问题**：v5.4.1 里工具要一直等待用户操作完才返回，而框架对每次工具调用施加
`asyncio.wait_for(tool_call_timeout)` 硬超时（本服务器被设为 **60 秒**），
导致实际只能接管 48 秒就被系统掐断。

**新架构**：工具**立即返回**，接管结束由事件回调把 AI 唤醒。

```
AI 调用 request_browser_takeover
   ↓
工具立即返回"已交给用户，你先待命"     ← 不再阻塞，不受 tool_call_timeout 约束
   ↓
用户在 WebUI 操作（时长只受 WebUI 配置约束，可设到 3600 秒）
   ↓
双计时器触发结束（用户点结束 / 空闲超时 / 到达上限）
   ↓
_wake_ai_after_takeover 合成事件投回会话 → 唤醒 AI + 附最新截图
```

**唤醒机制**（基于 AstrBot 官方能力，非 hack）：
- `CronMessageEvent` 是框架自带的合成事件，`is_wake = True` 可直接通过唤醒检查
- 事件投进 `context.get_event_queue()`，由 EventBus → Pipeline 跑完整流水线，
  等于"用户又发了条消息"，AI 带着完整上下文继续
- 截图以 `Image` 组件挂在事件上，框架的 `collect_initial_request` 自动转成
  `image_urls` 送给多模态模型，AI 能直接"看到"
- **群聊自动补 `@机器人`**：唤醒检查对群消息只认「@机器人/@全体/引用机器人」，
  否则直接丢弃 —— 这是实现时用真实组件复刻判定逻辑后发现的坑
- 唤醒失败时兜底直发消息（含截图），保证信息不丢失

#### 2. 🐛 修复：手机端触摸能看到光标，但点击不生效

**根因**：前端 `moveCursor()` 里残留一行占位语句 `_ = rect;`，抛
`ReferenceError: _ is not defined`。它在 `touchstart` 处理器内，**抛异常后
同一处理器里的 `gestureStart` 永远不会执行** → 手势状态没建立 →
`touchend` 检查 `gesture === null` 直接返回 → 点击请求根本没发出去。
光标能显示，是因为改样式那几行在抛异常之前。

**定位方式**：用 Playwright 加载真实 `index.html` + 注入 mock bridge +
`has_touch=True` 发真实触摸事件，捕获到确切堆栈。

**顺带修**：
- `moveCursor` 支持 `changedTouches`（`touchend` 时 `touches` 为空）
- 补 `touchcancel` 处理（来电/通知栏下拉等场景），只清理手势不补发点击
- `touchend` 补 `{ passive: false }`

#### 3. 🐛 修复：画面一闪一闪

**根因**：接管页每 2 秒轮询状态，每次都无条件重弹深色模糊遮罩；
页面静止时没有新帧来隐藏它，于是每 2 秒闪一次。

**修复**：遮罩只在**首次进入**时显示；同时给遮罩加 `pointer-events: none`，
防止它吞掉触摸事件。

#### 4. 🐛 修复：点击/拖动延迟严重

接管点击原来走 `click_coord`（AI 工具用的路径），它会**持有 `_op_lock`
并 `sleep 2 秒`**，真人连点/拖动会被严重卡顿。

新增 `click_raw`：纯鼠标点击，不持锁、不等待，接管专用。

#### 5. 其它修复

- `_unfreeze_page` 现在真正还原 `setInterval` / `requestAnimationFrame`
  （此前只把 `window._freeze` 置 false，被替换成空函数的定时器永久失效，
  会让页面 JS 逻辑假死）
- 接管页「总时长上限」输入范围放宽到 3600 秒

#### 验证

| 项目 | 结果 |
|------|------|
| 前端真实触摸（Playwright + has_touch） | 全部通过，正确发出 `move` + `click`，坐标为归一化 0~1 |
| 遮罩显示期间点击 | 通过（不再被吞） |
| 后端注入（单击/长按/拖动/触摸/投屏中点击） | 全部命中页面 |
| 非阻塞工具立即返回 | 通过（< 1s） |
| 唤醒事件投递（私聊/群聊/三条结束原因/失败兜底） | 20 项全部通过 |
| 群聊自动补 `@` | 通过（未补则会被唤醒检查丢弃） |

---

## [5.4.1] - 2026-09-26

### 🐛 修复：`request_browser_takeover` 调用即崩（`async_generator can't be used in 'await' expression`）

**报错现场**
```
[ERRO] [run_wyc_tool] 执行工具 request_browser_takeover 失败:
       object async_generator can't be used in 'await' expression
  File "main.py", line 3671, in run_wyc_tool
    result = await handler(event, **args_dict)
TypeError: object async_generator can't be used in 'await' expression
```

**根因**：该工具 5.4.0 时被写成 **async generator**（用 `yield` 做心跳保活），
而插件的 `run_wyc_tool` 包装层用 `await handler(...)` 调用它 → Python 直接抛 TypeError。

**更深一层：原「心跳保活」方案本身是错的**（查阅 AstrBot 4.28 框架源码确认）：

| 框架事实 | 出处 | 后果 |
|---------|------|------|
| `yield None`（裸 yield）语义 = "工具已直接把消息发给用户" | `astr_agent_tool_exec.py:701-715` | 触发 `AgentState.DONE`，**整个 Agent 回合当场结束** |
| 每 yield 一个非空值都会用**同一个 tool_call_id** 追加一条工具结果 | `tool_loop_agent_runner.py:1129-1137` | 多轮 yield 产生重复 tool_call_id，部分 provider 直接报错 |
| `asyncio.wait_for` 施加在**每次** `anext` 上，`tool_call_timeout` 默认 120s | `astr_agent_tool_exec.py:689-693`、`run_context.py:19` | 计时器可被 yield 重置，但上面的两个语义问题无法绕过 |
| `FunctionTool.is_background_task` 是官方长任务机制，但**无插件侧 API** | `agent/tool.py:61` | 无法用"后台任务 + 唤醒"实现长时间等待 |

**修复方案**（改造为普通协程 + 窗口钳制）
1. `request_browser_takeover_tool` 改回**普通协程**，单次 `return`
2. 新增 `_framework_tool_timeout()`：读取 AstrBot 全局 `agent_runner.config.misc.tool_call_timeout`（默认 120s）
3. 有效等待窗口 `= min(WebUI 上限, 框架超时 - 12s)`，保留收尾余量；到点**主动结束会话**并按「系统超时」上报
4. `TakeoverManager.open()` 新增可选 `max_seconds` 参数接收钳制后的值
5. `run_wyc_tool` 增加 `inspect.isasyncgenfunction` 防御分支：再遇到 generator 型 handler 会给出可诊断的错误而非崩溃
6. WebUI 接管设置页明确提示「实际生效值不会超过 AstrBot 全局工具调用超时时间」

**顺带修复的两处并发隐患**（接管会话切换时）
- `TakeoverManager.end()` 新增可选 `session` 参数，避免上一轮遗留的监控协程误结束新会话
- `_stop_screencast()` 现在会等待旧投屏任务真正退出，避免其收尾的 `stopScreencast` 掐断新会话的投屏

> ⚠️ **已知限制**：接管等待时长受 AstrBot 全局「工具调用超时时间」约束（默认 120 秒，实际可用约 108 秒）。
> 若需更长接管时间，请在 AstrBot 设置 → 智能体中调高该项。

---

## [5.4.0] - 2026-09-26

### ✨ 新增功能：浏览器接管

AI 遇到验证码、滑块、人机校验等无法自动完成的环节时，可把浏览器操作权**临时交给真人用户**，
用户在 WebUI 上实时看到画面并直接操作，完成后把权限交还 AI。

#### 1. AI 侧：`request_browser_takeover` 工具
- **免搜索直连**：本工具在 `DIRECT_CALL_TOOLS` 白名单内，AI 可直接用 `run_wyc_tool` 执行，无需先搜索（应急场景来不及搜索）
- **等待期间保持在线**：实现为普通协程，等待时长按框架 `tool_call_timeout` 钳制
  （详见 [5.4.1] 的修复说明——初版的 async generator 心跳方案已被证伪并移除）
- **发起时通知用户**：立即发送系统消息
  > AI已将操作权限转移至WebUI，请前往Astrbot控制台打开"更多LLM工具"插件WEB UI进行接管操作，本次操作将在{max}秒后超时
- **结束时按原因区分文案**（回给 AI）：
  | 结束原因 | 给 AI 的说明 |
  |---------|-------------|
  | 用户手动结束 | 用户已完成操作，请根据截图内容继续操作。 |
  | 达到时长上限 | 因达到时长上限（N秒）由**系统自动结束**，**并非用户手动结束**。请检查用户是否已完成操作，若未完成可再次请求接管。 |
  | 用户空闲超时 | 用户在 N 秒内没有任何操作，系统已自动结束接管。请检查用户是否已完成操作。 |
- 结束时自动附上**最新截图**

#### 2. 权限模型（单向授权）
- **用户无法主动夺取权限**，必须由 AI 调用工具发起
- 接管期间**仅冻结浏览器类工具**（27 个），AI 的其他工具（发消息、记忆、定时等）不受影响
- 双计时器：
  | 计时器 | 起点 | 默认 | 触发说明 |
  |--------|------|------|---------|
  | 空闲超时 | 用户最后一次操作 | 60s | 用户未操作 |
  | 总时长上限 | 接管开始（绝对） | 120s | 系统超时 |
- 两项均可在 WebUI 配置

#### 3. WebUI 新增「浏览器接管」选项卡
- 进入即检查浏览器状态：**未启用时提示「浏览器尚未启用」**
- 接管中自动出现实时画面（SSE 推流），**鼠标与手机触屏都可直接操作**
- 操作手势自动识别并注入浏览器：
  | 用户操作 | 判定 | 注入方式 |
  |---------|------|---------|
  | 点击 | 按下→抬起 < 400ms 且位移 < 8px | CDP 点击 |
  | 长按 | 按住 ≥ 400ms 未移动 | down + 延时 + up |
  | 拖动 | 按住移动 ≥ 8px | down + move 序列 + up |
  | 滚轮 | wheel 事件 | mouse.wheel |
  | 键盘 | 输入框 / Esc / Enter / Tab | keyboard |
  | 手机触摸 | touchstart/move/end | CDP `Input.dispatchTouchEvent`（真触摸） |
- 状态条显示：AI 请求原因、**倒计时**（取空闲/总时长中更紧迫者）、已操作次数、已持续时间
- 「结束操作」按钮（带二次确认）、全屏按钮

#### 4. 画面参数与自动检测
- 新增配置：`takeover_jpeg_quality`（默认 70）、`takeover_max_width`（默认 1280）
- **自动检测最佳画质**：向服务端连续请求已知大小的数据块，测出**端到端实际吞吐**（含 SSE 通道与用户本地网络），按目标 10fps 反推可持续单帧大小，取 **80%** 作为预算，映射为画质与宽度自动填入

#### 5. 三重提示保障 AI 知道「接管」存在
考虑到「失败检测」不可靠（点击可能"成功"却点错、AI 可能陷入死循环、甚至静默放弃不调工具），采用**无条件提示**而非失败时提示：
1. **工具描述**：`request_browser_takeover` 的描述与 keywords 覆盖验证码/滑块/人机校验等词
2. **返回消息**：所有浏览器工具的返回值固定追加提示（不依赖失败检测）
   > 💡 若遇到验证码 / 滑块 / 人机校验等无法自动完成的步骤，可用 request_browser_takeover 请求用户接管（工具名可直接用于 run_wyc_tool，无需搜索）
3. **上下文注入**：浏览器会话活跃期间，`on_llm_request` 持续注入
   > [浏览器能力] 当前浏览器会话活跃。可用 request_browser_takeover 把操作权临时交给真人用户…

同时**约束 AI 不得滥用**：工具描述明确"仅在确认无法自动完成时使用；普通操作失败请先自行重试，不要随意转交"。

#### 6. 技术实现
- 新增 `core/takeover.py`：会话状态机、双计时器、帧广播（每个订阅者只保留最新帧，**丢帧不丢延迟**）、操作注入、坐标归一化换算
- `core/browser.py` 新增接管原语：`mouse_*_raw` / `wheel_raw` / `type_text_raw` / `press_key_raw` / `touch_raw` / `start_screencast` / `stop_screencast` / `viewport_size`
- 画面走 **CDP `Page.startScreencast`**（变化驱动推帧，首帧约 0.04s），不可用时自动回退定时截图
- 传输走 **SSE**（框架 bridge SDK 原生支持 `subscribeSSE`），断线自动重连

### ✅ 验证
- Python 语法、JSON schema、HTML 标签配对 / id 引用 全部校验通过
- 心跳保活机制经对照实验验证：阻塞型工具 5s 被掐断；心跳型（每 2s yield）**活满 20s 不被掐断**；心跳过疏（6s > 5s 阈值）仍被掐断
- CDP 投屏与事件注入能力经实测验证（点击 / 拖动 / 长按 / 输入 / 滚轮 / 真触摸）
- 框架流式透传验证：`StreamingResponse` 经 `_coerce_view_result` 原样透传（`is` 相同）
- **端到端实测 65/66 通过**：
  - 状态机 51 项：开启/结束/幂等/双计时器（空闲 3s、上限 4s 均按预期触发）/坐标归一化换算（0.5→640, 360 精确命中）/帧广播（多订阅者、丢帧保最新、移除后停发）/SSE 编码（event+base64+JSON 可解析）/结束文案
  - 真实浏览器 14 项：点击/拖动/长按/键盘/功能键/滚轮/触摸全序列/二次触摸序列/触摸 tap 触发页面点击/CDP 投屏收帧/SSE 流出帧与状态
  - 唯一未过项为测试桩自身问题（Python `__` 名字修饰导致桩方法不可见），非插件缺陷

### 🐛 开发中修正的真实缺陷
- **async generator 内 `return 值` 语法错误**：Python 不允许在 async generator 中带值 return，接管工具所有返回值改为 `yield`（此错误被容器内 `py_compile` 捕获并修复）
- **触摸序列 CDP session 复用**：CDP 要求 `touchStart→move→end` 必须复用同一 session，否则报 `Must send a TouchStart first to start a new touch`。改为序列内维持持久 session、结束后释放，并新增 `release_touch()` 兜底

---

## [5.3.1] - 2026-09-26

### 🐛 修复

#### 1. 点击成功但报「点击失败」（截图超时）— 严重
- **现象**：日志出现 `点击失败: Page.screenshot: Timeout 30000ms exceeded. Call log: - taking page screenshot - waiting for fonts to load...`，点击动作实际已执行，但工具返回错误；重试那次耗时 29 秒
- **根因**：Playwright 的 `page.screenshot()` 在截图前会等待 `document.fonts.ready`（页面所有字体加载完成）。百度首页等站点存在长期挂起、永不返回的字体请求，截图便一路等到 30 秒超时
- **修复**：新增 `BrowserCore._cdp_screenshot()`，改用 CDP 原生接口 `Page.captureScreenshot` 直接取图，**不做字体等待**
  - 实测对比（构造"字体永久挂起"页面复现）：原生方式**超时失败**；CDP 视口截图 **0.1s**、全页截图 **0.2s**、JPEG **0.0s**
  - 全页模式通过 `Page.getLayoutMetrics` 取 `cssContentSize` 后以 `clip` 参数截取，长页面（1857px）尺寸正确
  - CDP 不可用时自动回退原生方式，并把原生超时从 30s 收紧到 15s，避免长时间拖死流程

#### 2. WebUI 无法删除记忆
- **根因**：插件页面运行在 iframe 中，浏览器原生 `confirm()` 被沙箱拦截（静默返回 false，且不弹窗），删除逻辑在入口处就被 `return` 掉
- **修复**：新增自带确认弹窗 `askConfirm()`（Promise 化，支持 ESC / 点遮罩关闭、危险操作红色按钮），**替换全部 5 处 `confirm()`**：删除记忆、批量删除、清理重复、删除文件、取消定时消息

#### 3. 部分输入框显示为原生黑框
- **根因**：样式选择器只写了 `input[type="text"]` 等，但页面上有 9 个 `<input>` 未写 `type` 属性——它们实际是 text 类型，却不匹配 `input[type="text"]`，于是完全没吃到样式
- **修复**：选择器补充 `input:not([type])`，并加 `-webkit-appearance: none` 抹平各平台原生外观；同步补齐 `::placeholder` 与 `checkbox` 的 `accent-color` 统一

#### 4. 手机端记忆管理布局混乱、文字超长
- **修复**：
  - 记忆卡片加 `min-width: 0` + `overflow-wrap: anywhere`，长 URL / 无空格长串强制断行，不再撑破布局
  - 超长记忆（>160 字）默认折叠 6 行，提供「展开全文 / 收起」按钮，避免卡片高矮悬殊
  - 标签 chip 限宽省略号，防止超长标签撑开
  - 手机端筛选栏 / 批量操作栏改为纵向自适应布局（输入框整行、下拉与按钮两列），不再被压扁成一团

### ✨ 优化

#### 操作图标全面升级
- **改用 SVG 源渲染的 PNG**：用 Playwright 将官方 SVG 渲染为 256×256 带透明通道的 PNG，完整保留 SVG 里设定的 **50% 半透明**效果（原 PNG 为不透明实心）
- **每个图标独立锚点**（关键修复）：此前用 alpha 包围盒中心对齐，导致图标主体与实际操作点偏移。现按图标语义逐个指定锚点，贴图时令锚点对准操作坐标：
  | 图标 | 锚点位置 |
  |------|----------|
  | 点击 | **鼠标指针箭头尖端**（指针指到的位置才是真正点击点） |
  | 长按 | 手指底部中央（按压屏幕的接触点） |
  | 输入 | I 形光标正中心 |
  | 拖动 | 四向箭头正中心 |
- **拖动同时标注起点与终点**：起点蓝点 + 紫色主线 + 方向箭头 + 两端各一颗图标，并分别标注「起点」「终点」（起点标签画在图标上方，避免遮挡）

### ✅ 验证
- 截图修复：构造字体挂起页面复现线上故障，确认原生超时、CDP 秒级返回
- 图标锚点：逐个生成锚点可视化验证图，确认四个锚点均落在语义正确位置
- 图标透明度：渲染后校验不透明像素占比 0.0%、半透明 11.0% / 12.3% / 6.6% / 7.6%
- WebUI：HTML 标签配对、id 引用差集、页面容器、表单字段全部校验通过

---

## [5.3.0] - 2026-09-26

### ✨ 新增功能

#### 1. 浏览器操作展示（纯代码实现，不依赖提示词）

##### 1.1 始终显示 AI 操作（`browser_always_show_action`）
- AI 每操作一次浏览器（点击 / 双击 / 右键 / 长按 / 输入 / 悬停 / 拖动 / 滚动 / 搜索 / 跳转 / 后退 / 前进），**强制把当前截图发送到当前会话**
- **纯代码实现**：在 `run_wyc_tool` 单一出口收口，不经过 LLM、不依赖提示词，AI 无法"忘记"发图
- 自动判定会话类型：群聊走 `send_group_msg`、私聊走 `send_private_msg`
- 截图以 base64 CQ 码发送，无需额外文件挂载

##### 1.2 浏览器操作增强显示（`browser_action_overlay_enabled`）
- 在截图上用图标标出 AI 操作的**精确位置**，用户一眼看出「AI 点的是哪里」
- 新增 `core/action_overlay.py` 模块 + 4 个内置图标（`resource/action_icons/`）：

  | 图标 | 动作 | 视觉 |
  |------|------|------|
  | `01-点击-click.png` | 点击 / 双击 / 右键 / 悬停 | 蓝色指针 + 靶心 |
  | `02-长按-long-press.png` | 长按 | 橙色手指 + 波纹 |
  | `03-输入-input.png` | 点击输入 / 输入文字 | 绿色 I 形光标 |
  | `04-拖动-drag.png` | 拖动 / 滚动 | 紫色四向箭头 |

- 每个标注下方带半透明圆角标签（点击 / 双击 / 输入 / 拖动…），并自动避让贴边
- 拖动动作额外绘制**起点圆点 → 紫色连线 → 终点图标**的完整轨迹
- **精确定位**：用图标 alpha 包围盒算出图形真实视觉中心来锚定操作点（图标画布 400×400 但图形不居中，若按方框左上角对齐会偏移）
- **坐标自适应**：截图尺寸与视口不一致时（缩放截图）自动按比例换算
- **边界安全**：坐标越界自动贴边；图标缺失时退化为绘制十字准星
- **缓存管理**：产物写入 `data_dir/overlay_cache`，文件名带动作 + 坐标指纹（同一张截图多次标注互不覆盖），内置 `clear_cache()` 保留策略

##### 1.3 推送时附带说明文字（`browser_action_push_text`）
- 推送截图时附加一行 `🖥️ AI 操作：<动作描述>`，如「点击」「输入「hello」」「向 下 滚动」

#### 2. WebUI 全量重绘（PC / 移动端分别兼容）

- **PC 端**：左侧固定侧边栏导航（图标 + 文字），主内容区 1080px 居中，卡片式分区
- **移动端（≤900px）**：侧边栏隐藏，改为**顶部栏 + 底部固定标签栏**（图标 + 文字，可横向滑动），适配 iOS 安全区（`env(safe-area-inset-*)`）
- 视觉全面升级：全新配色变量体系、iOS 风格滑动开关（`.switch-row`）、圆角标签、悬停反馈、页面切换淡入动画、统一状态提示
- 保留并增强原有全部功能：危险项（工具提示词注入）用橙底警示样式单独标出

#### 3. 记忆管理大幅增强

##### 3.1 可视化统计看板
- 顶部 4 张统计卡：记忆总数 / 涉及用户 / 标签种类 / 高重要度条数
- 后端新增 `MemoryManager.get_stats()`，含重要度分布（高 8-10 / 中 4-7 / 低 1-3）

##### 3.2 筛选与排序
- 按**用户**筛选（下拉显示每个用户的记忆条数）
- 按**标签**筛选（下拉显示每个标签的出现次数）
- 按**最近更新 / 创建时间 / 重要度**排序
- 搜索框支持回车直接搜索

##### 3.3 批量维护
- 记忆卡片改为**网格布局**，每张带勾选框，支持**全选**
- 批量操作三件套：
  - **批量删除**（带二次确认，显示将删除的条数）
  - **批量改重要度**（1-10）
  - **批量加标签**（自动去重，已存在的标签不重复添加）
- 选中状态在筛选变化时自动清理无效项

##### 3.4 数据管理
- **导出备份**：一键导出 JSON（文件名带日期），用于迁移或备份
- **导入合并**：上传 JSON 自动按 id 去重合并，返回「新增 N 条 / 跳过 M 条」
- **一键去重**：自动检测「同用户 + 内容完全相同」的重复记忆（每组保留最早一条），先预览可清理数量再确认执行

##### 3.5 重要度可视化
- 记忆卡片上以彩色 chip 显示重要度：≥8 红色、4-7 橙色、≤3 灰色
- 标签以蓝色 chip 展示

##### 3.6 新增 8 个 WebUI API
`memory_stats` / `memory_users` / `memory_tags` / `memory_batch` / `memory_duplicates` / `memory_dedupe` / `memory_export` / `memory_import`

### 🔧 说明
- **图标为包内只读资源**（`resource/action_icons/`，随插件分发），运行时产物写入 `data_dir/overlay_cache`，符合插件市场上架规范
- 新增配置项已同步 `_conf_schema.json` 与 `CONFIG_SAVE_WHITELIST`，WebUI 保存后**热更新生效**（无需重启），叠加组件支持懒初始化

### ✅ 验证
- 端到端实测 **23 项中 22 项通过**（唯一未过项为测试脚本自身断言问题：被测数据已被前序去重步骤清理，非代码缺陷）
  - 图标资源完整性 4/4
  - 四种动作叠加（click / long_press / input_at / drag）全部成功
  - 边界与异常：越界坐标贴边、缩放换算、空坐标返回原图、坏路径返回原值 —— 全部安全
  - 真实 Playwright 截图 + 叠加成功
  - 记忆增强：统计 / 用户列表 / 标签列表 / 重复检测 / 一键去重 / 批量改重要度 / 批量加标签 / 导出 / 导入去重 / 批量删除 —— 全部通过
- 视觉效果人工确认：输入图标精准落在密码框、点击图标落在目标坐标、拖拽轨迹（起点蓝点 → 连线 → 终点图标）绘制正确
- 插件重启加载正常，无新增异常日志

---

## [5.2.5] - 2026-09-26

### 🐛 修复（AstrBot 插件市场上架合规 · 上下文前缀缓存）

#### 不再改动 system_prompt，注入内容改走用户消息临时块
- **背景**：插件市场审核指出，`on_llm_request` 直接向 `request.system_prompt` 追加内容，其中「系统状态、用户记忆、群成员身份、AI语音配置」等片段随每次请求动态变化，会破坏模型上下文的前缀缓存命中率，导致推理成本上升、响应变慢
- **修复**：改用官方推荐方式 —— 通过 `req.extra_user_content_parts.append(TextPart(text=...).mark_as_temp())` 把注入内容附加到**当前用户消息末尾**
  - `mark_as_temp()` 等价于 `_no_save=True`：内容只发给模型，**不写入会话历史**，历史消息前缀保持稳定
  - 用户原始发言始终排在内容块第一位，注入内容排在其后，语义位置合理
- **兼容性处理**：
  - `TextPart` 采用防御式导入（`try/except ImportError`），极旧版本框架下自动退化为原有 `system_prompt` 行为，不会导致插件加载失败
  - 注入前用 `getattr(request, "extra_user_content_parts", None)` 探测，兼容第三方 Agent（Dify/Coze/DashScope 等）使用的 `ProviderRequest`
- **配置说明同步**：`_conf_schema.json` 中 `memory_inject_enabled`、`inject_group_role_enabled`、`inject_tool_prompt_enabled` 三项的 description / hint 文案由「注入到系统提示词」更正为「作为临时内容附加在用户消息后，不写入会话历史」

### ✅ 验证
- 框架链路实测（容器内 Python）：注入块正确附加于用户消息之后，`_no_save` 标记在落库时被剥离，历史仅保留用户原始发言 —— 全项通过
- 插件加载/重启实测：版本 5.2.5 正常加载，浏览器与 WebUI API 均就绪，无新增报错

---

## [5.2.4] - 2026-09-26

### 🐛 修复（AstrBot 插件市场上架合规）

#### 持久化数据不再写入插件安装目录
- **背景**：插件市场审核指出，插件把持久化数据写到了自身安装目录而非 `data/plugin_data/astrbot_plugin_qzone_tools`，会导致插件更新/重装时用户数据被覆盖
- **修复三处路径**（全部迁移到 `data_dir`）：
  | 项目 | 原路径（包内） | 新路径 |
  |------|---------------|--------|
  | 工作区 | `<插件目录>/workspace` | `data_dir/workspace` |
  | 中文字体 | `<插件目录>/fonts` | `data_dir/fonts` |
  | 收藏夹 | `<插件目录>/favorite.json` | `data_dir/favorites.json` |
  | 刻度叠加资源 | `<插件目录>/resource` | `data_dir/resource` |
- **自动迁移**：新增 `_migrate_legacy_dirs()`，首次启动时自动把旧位置的 `workspace/`、`fonts/`、`favorite.json` 搬到 `data_dir`，老用户升级不会丢文件
- **仓库清理**：`.gitignore` 补充运行时数据目录；`favorite.json` 从版本控制移除（属于用户数据，不应入库）
- 效果：插件安装目录体积由 17.6MB 降至 2.7MB（仅剩代码与只读资源）

### ✅ 验证
- 数据目录检查 11/11 通过（路径指向 / 收藏夹增删 / 刻度图输出 / 插件目录只读性）
- 迁移实测：3 项数据全部搬迁成功，插件正常加载

---

## [5.2.3] - 2026-09-25

### ✨ 新增功能

#### 工具权限档位扩展为 4 档（原 3 档）
- **新增 `groupadmin` 档「群主/管理员（仅群聊）」**：群聊中群主与群管理员可用；**私聊中该档位不生效，自动回退为 `admin`（超管）**
- 档位语义梳理（此前「超管」实际混入了群主/群管理判定，语义不清）：
  | 档位 | 含义 |
  |------|------|
  | `global` | 全局 —— 所有人都可用 |
  | `groupadmin` | 群主/管理员（仅群聊）—— 群聊中群主/群管理可用；私聊回退超管 |
  | `admin` | 超管 —— 仅 AstrBot 全局管理员（`admins_id` 配置）可用 |
  | `disabled` | 禁用 —— 任何人都不可用 |
- **判定逻辑拆分**：`_event_is_admin`（仅 `admins_id`）与 `_event_is_group_admin`（群主/群管理，仅群聊有意义）分离；新增统一入口 `_check_tool_permission_async(event, perm)`
- **WebUI 权限页**：批量设置与单项下拉均新增第 4 档；说明文案明确「私聊不生效，自动按超管处理」；批量设置提示同步显示档位名
- **工具可见性**：`groupadmin` 档在群聊中对群主/群管理可见，私聊中仅超管可见（避免 LLM 看到后用不了）
- **兼容性**：权限表读取白名单同步扩展，旧 3 档配置无需改动即可继续生效

### ✅ 验证
- 4 档矩阵实测 24/24 通过（身份判定 / 群聊档位授权 / 私聊回退 / 工具可见性）
- 浏览器坐标交互回归 10/10 通过，改动无副作用

---

## [5.2.2] - 2026-09-25

### 🐛 问题修复（Issues #9 / #12 / #13）

#### #13 权限修改无效，重载/重启后全部变回默认
- **根因**：`tool_permissions` 在 `_conf_schema.json` 中声明为 `type: "object"` + `items: {}`（空）。AstrBot 的 `AstrConfig.check_config_integrity()` 递归比对时会把这些动态子键全部判定为「参考配置中没有的配置项」并删除 —— 日志实锤 `Config key removed: tool_permissions.set_group_ban`，因此保存后重载/重启即被清空
- **修复**：权限表改存**独立文件** `plugin_data/astrbot_plugin_qzone_tools/tool_permissions.json`（原子写入，不进主配置，从根上避开完整性检查）；schema 中该键降级为 string 占位；WebUI 读写走独立通道；自动迁移旧版主配置中的已有权限

#### #12 浏览器设置代理后无法使用
- **根因**：`new_context(proxy=...)` 传的是配置里的**字符串**（如 `http://127.0.0.1:7890`），而 Playwright 只接受 **dict**（`{"server": ..., "username": ..., "password": ...}`），字符串会直接导致上下文创建失败
- **修复**：新增 `_normalize_proxy()` 统一规范化 —— 支持 `http://host:port`、`host:port`（自动补 scheme）、`socks5://user:pass@host:port`（自动拆出账密并 URL 解码）、dict 原样透传、空值返回 None

#### #9 无法设置 personal_note
- **根因**：NapCat 的 `set_qq_profile` 要求 `nickname` 与 `personal_note` **均为必填**。只传 `personal_note` 时报 `Schema compilation error: Expected required property`
- **修复**：未指定的字段先用 `get_login_info` 取回当前资料回填后再提交；确实取不到昵称时给出明确错误提示而非报 schema 错

### 🔧 改进
- WebUI「权限控制」页改为回传**实际生效档位**：此前敏感工具未显式配置时实际按 admin 生效，但页面因空表全部显示为「全局」，造成"设置没生效"的误判（issue #10 的观感来源）
- 版本号 5.2.1 → 5.2.2

### ✅ 验证
- 配置持久化：模拟 AstrBot 完整性检查 + 插件重载，权限表完整恢复（主配置仍为空 `{}`，独立文件生效）
- 代理规范化：7 种输入形态全部正确（含 URL 编码账密解码）
- 浏览器回归：坐标交互 10/10 通过，改动无副作用

---

## [5.2.1] - 2026-09-24

### 🐛 问题修复

#### 修复浏览器场景重复回复（同一内容发两次）
- **根因**（日志实锤的三步链）：
  1. AstrBot 截图审查机制下，LLM 在工具循环中先输出完整文本 → `buffer_intermediate_messages=false` 使其**立即发出**（消息①）
  2. LLM 又调 `send_message_to_user`（文本+截图），但**幻觉编造 session 参数**（`platform_id:QQ:group:979465319`）→ `invalid session` 失败
  3. 工具失败后 Agent 结束，最终文本回复（重复同样内容）再次发出（消息②）
- **修复**：`on_llm_request` 注入新增 [回复与发送规则]：① `send_message_to_user` 的 session 参数必须省略 ② 完整回复只输出一次，禁止先输出文字再重复发送 ③ 工具期间进度只用一句简短提示，完整结果最后一次性输出
- 备注：`buffer_intermediate_messages=true` 只能把两条合并为一条（重复内容仍在），治标不治本，未启用

---

## [5.2.0] - 2026-09-24

### ✨ 新增功能

#### 浏览器反风控伪装（browser_stealth）
- **背景**：此前浏览器以 `headless=True` 裸指纹运行，UA 自带 `HeadlessChrome`、`navigator.webdriver=true`、WebGL 渲染器暴露 SwiftShader，带风控的网站（知乎/淘宝/B站/云服务登录页等）几乎必然识别为机器人并拦截
- **完整版新无头内核**：chromium 引擎优先以 `channel="chromium"` 启动（Playwright ≥1.49 完整版 Chromium 新无头模式，插件/`window.chrome` 等与真人浏览器一致），不可用时自动回退默认内核
- **真实 UA 抹平**：启动时读取内核真实 User-Agent 并抹去 `Headless` 字样（版本号与内核精确匹配），外发请求 UA 为正常 `Chrome/149.x`
- **stealth 注入**（`add_init_script`，页面 JS 执行前生效）：
  - `navigator.webdriver → false`（配合启动参数 `--disable-blink-features=AutomationControlled` 双保险）
  - 补齐 Chromium 内核缺失的 `window.chrome`（runtime/app/csi/loadTimes）
  - `navigator.languages → ["zh-CN","zh"]`、hardwareConcurrency/deviceMemory=8
  - WebGL UNMASKED vendor/renderer 伪装为 Intel 显卡（隐藏软件渲染特征）
  - `permissions.query('notifications') → prompt`（修复 headless 默认 denied 的异常特征）
- **context 拟真**：`locale=zh-CN`、`timezone=Asia/Shanghai`、浅色配色、`device_scale_factor=1`
- **启动参数调整**：移除 `--disable-gpu`（避免 WebGL 软件渲染指纹），新增 `--lang=zh-CN`
- **新增配置 `browser_stealth_enabled`**（默认开启，WebUI 可关）；CDP 模式同样注入 stealth 脚本
- **实测**：指纹自检 13/13 通过、坐标交互回归 10/10 通过、外发 UA 无 Headless 字样

---

## [5.1.0] - 2026-09-19

### ✨ 新增功能

#### 浏览器坐标交互体系（视觉模型专属）
- **完全移除选择器操作**：删除 `click_element`（CSS 选择器点击）、`type_text`（选择器输入）、`browser_swipe`，浏览器操作全面转向 **AI 看截图 → 输出坐标** 的视觉交互模式
- **新增 6 个坐标交互工具**（坐标与截图像素一一对应，左上角为原点）：
  - `browser_double_click(x, y)` - 双击坐标（选中文本/打开文件夹）
  - `browser_right_click(x, y)` - 右键坐标（上下文菜单）
  - `browser_long_press(x, y, duration_ms)` - 长按坐标（唤起悬浮菜单，100-10000ms）
  - `browser_drag(x1, y1, x2, y2, duration_ms)` - 坐标拖拽（拖滑块/移动元素，按距离自动插值模拟真实拖动）
  - `browser_input_at(x, y, text, press_enter)` - 点击坐标处输入框并逐字键入（带 30ms 打字延迟，真实触发 input 事件）
  - `browser_hover(x, y)` - 悬停坐标（触发下拉菜单/悬浮提示）
- **坐标越界自动贴边**：超出视口的坐标自动限制到边界内，不报错
- **每次操作自动回传新截图**：AI 点击/拖拽后立即看到页面最新状态，形成「看截图→出坐标→看结果」视觉闭环

#### browser_wait 等待工具
- **新增 `browser_wait(seconds)`**：一次调用完成「通知用户 + 等待 + 重看页面」
  - 调用后立即向当前会话发送 `⏳ AI正在等待网页→Ns`
  - 等待 5-45 秒（自动夹紧）后自动截取最新页面截图回传给 LLM
  - 适用于页面跳转、验证码倒计时、异步加载等场景，避免 LLM 反复截图轮询浪费 token
  - 通知用户失败不影响等待流程

#### 视觉模型门禁
- **新增 `browser_vision_gate_enabled` 配置**（默认开启）：浏览器工具仅对支持图像输入的多模态模型开放
- 三层判定：provider `modalities` 含 `image` → 放行；配置了但无 `image` → 拒绝；未配置 → 模型名启发式兜底（GPT-4o/GLM-4v/VL/Gemini/Claude 等 + GLM 版本号正则）
- 非视觉模型调用浏览器工具会收到明确提示，引导切换模型或补配能力

### 🔒 安全修复

#### 工具权限控制（重大）
- **修复 LLM 工具路径完全绕过权限校验的严重漏洞**：此前 `run_wyc_tool` 内的 105 个工具无任何角色校验，群内任意用户可通过提示词注入让 LLM 执行踢人/禁言/删文件
- **新增 `SENSITIVE_TOOLS` 敏感工具集**（59 个高危工具默认仅管理员可用）：群管理、身份资料修改、代码执行、文件外发、定时任务、浏览器等
- **`tool_permissions` 的 admin 档真正生效**：此前仅 disabled 档有效，admin 档等同无限制（安全误导）
- 权限判定三级：AstrBot 全局管理员（admins_id）→ 群主/群管理员（含 60 秒角色缓存）→ 拒绝
- `run_wyc_tool` 双重校验 + 越权调用告警日志；非管理员在工具搜索/列表阶段即看不到敏感工具

#### Python 沙箱（重大）
- **修复沙箱完全失效**：`run_python_sandbox_enabled` 配置此前从未被读取
- **移除自动注入的 `import os as _os`**（此前用户代码可直接 `_os.system()` 拿 shell）
- **检测改为 AST + 正则双层**：拦截 `getattr(__builtins__, '__import__')` 等动态导入绕过，覆盖 importlib/builtins/pickle/pty 等危险模块
- **子进程资源限制**：RLIMIT_CPU/AS/FSIZE/NPROC + `-I` 隔离模式 + 白名单环境变量 + 用户代码封装进函数作用域

#### SSRF 防护加固
- **非标准 IP 写法归一化**：拦截十进制（`2130706433`）/十六进制（`0x7f000001`）/八进制（`0177.0.0.1`）/IPv4 映射 IPv6（`::ffff:127.0.0.1`）等绕过手法
- **DNS 解析校验**：域名解析到内网地址即阻断（防 DNS rebinding）
- **重定向拦截**：`fetch_url` 禁止自动跟随重定向，302 目标同样做 SSRF 校验
- 默认拦截全部私有/回环/链路本地/保留网段

#### 路径穿越修复
- **统一 `_safe_resolve_path`**：`send_file`/`read_image`/`read_workspace_file` 此前支持任意绝对路径（可读容器内任意文件发到 QQ），现仅允许工作区/截图缓存/闪传目录（realpath 防软链接穿透）
- `download_fileset` 下载文件名净化，防路径穿越写入

#### 其他安全
- 邮箱授权码 WebUI 脱敏（回传 `***` 表示不修改），不再明文下发前端
- WebUI 文件上传增加 32MB 大小限制 + base64 校验 + 原子写

### 🐛 修复

#### 事件循环阻塞（重大）
- **修复 `run_python_code` 同步 `subprocess.run` 阻塞**：此前最长冻结整个 AstrBot 事件循环 30 秒（所有群消息/插件/WebUI 全部停摆），改用 `asyncio.create_subprocess_exec`
- 同步阻塞的 `docker cp`（闪传）、`urllib.request.urlretrieve`（字体/文件下载）、`apt-get install` 全部异步化或移除

#### 卡死/资源泄漏
- **72 处 NapCat API 调用统一接入超时封装**（`_call`/`_safe_call_action`，默认 30s）：此前 NapCat 无响应时 await 永久挂起
- **`_update_contacts_cache` 网络 IO 移出锁外**：消除"持锁 + 无超时 IO"死锁配方
- **7 处 `asyncio.create_task` 全部登记引用**（`_create_bg_task`）：修复后台任务被 GC 静默回收（定时指令轮询/浏览器监控可能无声停止），带异常回调
- **`terminate()` 完整清理**：取消全部登记任务 + 关闭浏览器释放 Playwright 进程 + 等待任务真正结束（消除 "Task was destroyed" 警告），修复 `command_executor` 为 None 时的潜在崩溃
- `_periodic_refresh` 单次刷新失败不再终止整个刷新循环
- `BrowserDownloader.verify_browser` 加验证锁 + 2 次重试 + `--no-sandbox`（容器 root 运行），修复并发启动导致的 `Target page, context or browser has been closed`
- `_safe_await` 异常覆盖面扩展：浏览器/页面被关闭类错误同样触发重试
- `BrowserSupervisor` 锁范围缩小：浏览器操作不再串行化所有并发调用；`_stop_browser` 失败时仍正确置空状态
- `FavoriteManager` 原子写（临时文件 + fsync + replace），防并发写损坏

#### 配置系统
- **补齐 45 个工具开关**：`_conf_schema.json` 此前只有 61 个 `enable_*`，45 个工具（含 `run_python_code`/`send_file`/全部浏览器工具）无法通过 WebUI 关闭；现 109 个工具全量覆盖
- WebUI 保存白名单放行全部 `enable_*` 开关（此前保存被静默丢弃）

### 🔧 变更

- **定时消息持久化**：`schedule_message`/WebUI 定时消息接入 `scheduled_messages.json`，重启自动恢复（过期 5 分钟以上不补发），提示文案同步更新
- **隐私模式真正实现**：`privacy_mode=privacy` 时工具返回文本中的群号/QQ号替换为不可逆短标识（SHA1 前 6 位，可区分不可还原）
- 隐私脱敏挂在 `run_wyc_tool` 统一出口，全部工具生效
- 浏览器启动检查统一走 `BrowserDownloader`（并发安全），移除每次启动都执行的 `apt-get install`
- `metadata.yaml` 版本 5.0.6 → 5.1.0

---

## [5.0.6] - 2026-06-16

### ✨ 改进
- **截图自动展示给 LLM**：`run_wyc_tool` 检测到截图时自动返回 `ImageContent`，LLM 无需再调用 `read_image` 即可直接看到截图内容
- **`send_file` / `read_image` 支持绝对路径**：传入绝对路径时直接使用，不再截断为文件名；工作区找不到时自动回退到 `screenshot_cache` 目录查找
- **工具描述更新**：`send_file` 和 `read_image` 的参数描述明确支持绝对路径和截图路径

### 🐛 修复
- **图片发送路径错位修复**：截图保存在 `screenshot_cache/` 但 `send_file` 在 `workspace/` 查找，现已支持跨目录查找
- **`_safe_await` 协程重试 bug**：重试机制复用已耗尽的协程对象导致 `cannot reuse already awaited coroutine`，改为协程工厂（callable）模式，每次重试创建新协程

### 🔧 变更
- **图片默认输出格式 webp → png**：`image_output_format` 默认值从 `webp` 改为 `png`，解决部分 LLM（如 mimo-v2.5）不支持 webp 格式的问题
- **截图工具提示词更新**：提示 LLM 截图已自动展示，如需发送给用户请用 `send_file`

---

## [5.0.4] - 2026-06-10

### 🐛 修复
- **`send_file` 图片发送修复**：NapCat CQ 码不支持本地绝对路径，改为 base64 编码发送（`CQ:image,file=base64://...`）。文件发送优先使用 NapCat 原生接口，失败时 fallback 到 base64 CQ 码。发送过程中 base64 数据不会返回给 LLM，避免上下文膨胀。


## [5.0.3] - 2026-06-10

### ✨ 新增功能

#### 图片读取与发送
- **新增 `read_image` 工具**：读取工作区图片文件，返回 base64 编码内容，LLM（多模态模型）可直接查看 Python 代码生成的图表、截图等
- **新增 `send_file` 工具**：将工作区文件发送到 QQ 群或私聊。支持两种模式：
  - 默认以 QQ 文件形式发送（群聊用 `upload_group_file`，私聊用 `send_online_file`）
  - `as_image=true` 时以图片消息形式发送（CQ:image）
- **`run_python_code` 图片自动检测**：执行完毕后自动扫描工作区新增的图片文件，提示 LLM 使用 `read_image` 查看
  - 通过 `python_run_auto_image_enabled` 配置项控制开关（默认开启）
  - 检测最近 35 秒内修改的图片文件，避免误报旧文件

---

## [5.0.2] - 2026-06-10

### 🐛 修复
- **Python 代码执行字体注入修复**：`shlex.quote()` 在仅包含安全字符的路径（如 `/AstrBot/...`）上不会添加引号，导致生成的 `_FONT_PATH = /path/to/font` 成为语法错误。改用 raw string 直接引用路径。

---

## [5.0.1] - 2026-06-08

### 📦 仓库瘦身
- **移除内置字体文件** `fonts/NotoSansCJK-Regular.ttc`（19.5MB），改为运行时按需从 GitHub 下载
- 修正字体路径（之前错误指向 `plugins/fonts/`，实际应为插件内 `fonts/`）
- 仓库体积从 16.39MB 降至 1.62MB

## [5.0.0] - 2026-06-02

### 🔒 安全修复（重大更新）

#### SSRF 防护
- **新增 `_check_ssrf()`**：所有 HTTP 请求（`fetch_url`、`browser_visit`、`open_page`）自动检查目标地址
- **内置黑名单**：默认阻止 127.0.0.0/8、10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、169.254.0.0/16、metadata.google.internal 等内网/元数据地址
- **WebUI 可配置**：支持自定义阻止 IP/URL 和额外 CIDR 范围

#### 图片路径安全
- **新增 `_resolve_image_file()` 路径限制**：仅允许工作区目录、/tmp、闪传中转目录
- **WebUI 开关**：`resolve_image_restricted` 可关闭限制

#### Python 沙箱
- **新增 `run_python_sandbox_enabled`**：可一键禁用 Python 代码执行工具
- **默认危险模式拦截**：内置 8 个危险代码模式（subprocess、os、sys、shutil、ctypes、__import__、eval、exec），即使用户清空配置也生效
- **字体路径注入修复**：`shlex.quote()` 防止命令注入

#### 异常信息泄露修复
- **全局 `_safe_error_msg()`**：替换全部 85+ 处用户可见的 `str(e)`，自动剥离文件路径、IP 地址，截断到 200 字符

#### 截图路径校验
- `screenshot_page_tool` 新增 `save_path` 校验，仅允许保存到工作区目录

#### Docker 容器名可配置
- 新增 `docker_container_name` 配置项（默认 `napcat`），不再硬编码

#### 浏览器搜索安全
- `browser_search_tool` 使用 `url_quote()` 编码关键词，防止 URL 注入

#### 配置端点安全
- `handle_get_config` 脱敏处理 `email_authorization_code` 字段
- `handle_save_config` 白名单校验，拒绝未知配置键，防止覆盖敏感数据

#### 工具权限硬拦截
- `_get_available_tools()` 在 Python 代码层面强制执行权限检查，LLM 无法绕过

### 📝 文档修复

#### tool_all_help 命令帮助重构
- **移除 26 个幻影斜杠命令**：闪传、在线文件、工作区、浏览器工具是 LLM 工具而非斜杠命令，已改为明确标注
- **补全全部 103 个 LLM 工具列表**：按功能分类列出所有可用工具

#### README 更新
- **新增 52 个工具文档**：闪传（8 个）、在线文件（6 个）、好友管理（1 个）、工作区（4 个）、浏览器基础（6 个）、浏览器高级（14 个）、收藏夹（3 个）、浏览器安装（1 个）
- **管理员命令表**：从 23 行扩展到 51 行，与代码完全对齐
- **修复幻影引用**：`download_flash_file` → `download_fileset`，`send_message_to_user` → `send_message`

#### WebUI 修复
- 移除不存在的 `send_file_to_user` 权限配置项

### 🐛 修复
- **插件载入崩溃修复**：`_conf_schema.json` 中 `viewport_size` 和 `tool_permissions` 的 type 从 `dict` 改为 `object`（AstrBotConfig 不支持 dict 类型）
- **授权码脱敏修复**：移除 `email_authorization_code` 的 `***` 脱敏，本地 WebUI 用户需要看到真实值
- **配置持久化修复**：`_conf_schema.json` 缺少所有 v5.0.0 新增的配置 key，导致工作区设置、安全设置、浏览器设置等无法持久化
  - 根因：AstrBotConfig 通过 schema 决定哪些 key 需要持久化，没有 schema 定义的 key 在 `save_config()` 时被忽略
  - 修复：`_conf_schema.json` 新增 23 个 key，`CONFIG_SAVE_WHITELIST` 新增 17 个浏览器相关 key
- **CONFIG_SAVE_WHITELIST 补全**：新增 `browser_type`, `browser_mode`, `cdp_url`, `viewport_size`, `max_pages`, `timeout`, `zoom_factor`, `max_memory_percent`, `idle_timeout`, `monitor_interval` 等浏览器配置项

### 🔧 改进
- **WebUI 浏览器引擎设置面板**：新增可视化配置浏览器类型、模式、CDP 地址、代理、视口大小、缩放、标签页数、超时等
- **WebUI 浏览器性能设置面板**：内存使用上限、空闲超时、监控间隔，带滑块和直观说明
- WebUI 安全设置面板：可视化配置 SSRF 黑名单、图片路径限制、Python 沙箱、Docker 容器名
- 所有配置变更通过 `self.config.update()` + `self.config.save_config()` 持久化到插件配置文件

---

## [4.6.0] - 2026-06-01

### ✨ 新增功能

#### 图片输出格式转换
- **新增 `image_output_format` 配置项**：支持 `webp`（默认）、`png`、`jpg` 三种格式
- **统一转换**：所有插件生成的图片（浏览器截图、刻度叠加图、下载图片等）发送给 AI 前自动转换为目标格式
- **兼容性优化**：webp 格式体积最小，适合大多数场景；jpg 通用性最好；png 无损保真

#### 浏览器渲染模式
- **新增 `browser_render_mode` 配置项**：四种渲染模式可选
  - `完全`（默认）：完整渲染网页，适合性能中上的设备
  - `简略`：禁用毛玻璃效果和滤镜，适合普通设备
  - `极简`：不渲染动画/毛玻璃/自定义字体，仅保留布局和文字，适合 2C2G 服务器
  - `纯文本`：仅渲染文字，不加载图片/视频/Canvas 等，适合超低性能主机
- **CSS 注入机制**：截图前自动注入对应模式的 CSS 样式，不影响页面实际渲染

#### LLM 截图纯文本模式
- **新增 `llm_screenshot_text_only` 配置项**：开启后截图工具只返回页面文本，不返回图片
- **适合非多模态模型**：当 LLM 不支持图片输入时，自动提取页面 `innerText` 返回
- **支持所有截图工具**：`browser_screenshot`、`screenshot_page` 均支持

### 🔧 改进
- **WebUI 新增「浏览器 & 图片设置」面板**：可视化配置图片格式、渲染模式、纯文本模式
- **`TickOverlay` 支持配置**：刻度叠加图输出格式跟随 `image_output_format` 配置

---

## [4.2.0] - 2026-05-31

### ✨ 新增功能

#### 工具权限控制
- **新增 WebUI 权限控制页面**：可视化管理每个工具的访问权限
- **三级权限**：全局（所有人可用）、超管（仅 AstrBot 管理员可用）、禁用（任何人都不能使用）
- **按分类批量设置**：一键将整个分类的工具设为同一权限级别
- **代码层面硬拦截**：权限检查在 Python 代码中强制执行，LLM 无法绕过

#### 隐私控制
- **新增隐私模式**：开启后 LLM 只能看到群名字，无法看到群号和 QQ 号
- **聊天记录过滤**：隐私模式下自动隐藏消息中的 QQ 号（替换为 [用户]）
- **联系人搜索过滤**：搜索结果中隐藏群号和 QQ 号，只显示名字
- **群成员列表过滤**：隐私模式下隐藏成员的 QQ 号
- **群名自动解析**：隐私模式下传入群名字可自动解析为群 ID

### 🔧 改进
- **`_get_available_tools` 支持事件过滤**：根据用户身份动态过滤可用工具
- **`search_wyc_tools` / `call_wyc_tools` 权限感知**：非管理员搜不到超管工具

## [4.1.0] - 2026-05-27

### ✨ 新增功能

#### 高级浏览器自动化
- **搜索引擎支持**：百度、必应、谷歌搜索，自动打开搜索结果
- **页面交互**：点击坐标、输入文字、滚动、滑动、缩放
- **标签页管理**：查看所有标签、切换标签、关闭标签
- **收藏夹功能**：添加、查看、删除收藏，支持 URL 模板
- **对话功能**：向当前页面输入框发送对话内容
- **浏览器监控**：内存监控自动重启、闲置自动关闭
- **截图刻度**：可选在截图上叠加坐标刻度
- **多引擎支持**：chromium、firefox、webkit 可选

### 🔧 改进
- **WebUI 能力控制**：新增所有浏览器工具的开关控制
- **工具注册优化**：浏览器工具分为高级版和简化版，LLM 可根据场景选择
- **自动依赖安装**：插件加载时自动检查并安装 Playwright 和系统依赖

## [4.0.0] - 2026-05-27

### ✨ 新增功能

#### 浏览器自动化
- **新增 `open_page` 工具**：打开网页，支持 CSS 选择器和文字点击
- **新增 `click_element` 工具**：点击网页元素（按钮、链接等）
- **新增 `type_text` 工具**：在网页输入框中输入文字，可选按回车
- **新增 `screenshot_page` 工具**：对当前网页截图，用于验证码识别等场景
- **新增 `close_page` 工具**：关闭浏览器，释放资源
- **自动安装依赖**：插件加载时自动检查并安装 Playwright 和 Chromium 浏览器
- **会话复用**：同一会话内浏览器保持打开，无需重复启动

#### 网页内容获取
- **新增 `fetch_url` 工具**：获取网页文本内容，支持自定义最大字数（默认500）

#### 闪传功能优化
- **Docker 环境支持**：自动使用 `docker cp` 将文件复制到 NapCat 容器，无需手动配置共享目录
- **智能检测**：自动判断 Docker/非 Docker 环境，选择最佳文件传输方式

### 🐛 修复
- **配置保存问题**：修复保存配置时会清空其他配置项（如工具开关）的问题
- **工具开关失效**：修复关闭工具开关后 LLM 仍能调用的问题

### 🔧 改进
- **WebUI 能力控制**：新增浏览器自动化工具的开关控制
- **错误提示优化**：闪传功能在 Docker 环境下给出更清晰的配置指引
- **日志增强**：Playwright 安装过程记录详细日志

### 📝 依赖更新
- 新增可选依赖：playwright（浏览器自动化）
- 新增系统依赖：libnss3, libatk1.0-0 等（Playwright 运行所需）

## [3.6.0] - 2026-05-22

### ✨ WebUI 优化
- **WebUI 面板重写**：全新的配置、记忆与定时消息管理面板，支持可视化编辑插件配置、管理用户记忆、查看和取消定时消息。
- **Bridge SDK 内联化**：将 AstrBot Plugin Page Bridge SDK 直接内联到 HTML 中，避免沙箱环境下外部脚本加载失败导致页面卡死。
- **超时容错**：添加 `bridge.ready()` 5 秒超时回落机制，即使父窗口上下文未及时返回也能正常加载配置。
- **错误可见化**：配置加载失败时页面顶部显示红色错误横幅，不再静默失败。
- **WebUI 后端 API 完善**：新增 `add_memory` 和 `update_memory` Web API 端点，WebUI 记忆管理功能完整可用。

### 🐛 修复

#### 获取历史记录工具
- **重写消息获取逻辑**：`get_group_msg_history` 和 `get_friend_msg_history` 移除已废弃的 `message_seq` 参数，改用 `_call_history_api_with_seq` 分页拉取机制，避免 NapCat 接口变更导致无法获取消息。
- **消息内容可读性增强**：新增 `_format_message_content` 方法，将 OneBot v11 消息段（图片、表情、@提及、回复、文件、语音等）转换为可读文本，LLM 能理解消息中的媒体内容。
- **搜索关键词扩充**：历史记录工具的搜索标签增加 30+ 中文口语化关键词（"群里说了什么"、"看看群消息"、"查群记录"等），LLM 更容易找到匹配工具。

#### 设置 QQ 头像 / 群头像
- **跨容器图片传递**：新增 `_resolve_image_file` 方法，自动将本地文件路径转为 `base64://` 格式，解决 Docker 容器内文件路径无法直接传递给 NapCat 的问题。
- **参数合理性调整**：`set_qq_avatar` 和 `set_group_portrait` 的 `file` 参数改为必填，LLM 必须提供图片路径/URL/Base64，不再依赖引用消息（NapCat 下引用消息获取图片不可靠）。
- **错误提示友好化**：设置失败时返回 "❌ 设置头像失败，请检查图片是否有效" 而非原始异常信息，用户端体验更好。
- **文件有效性检查**：添加前置检查，文件不存在时直接返回明确错误提示，避免 NapCat 接口调用失败后吞异常。

#### 其他
- 设置头像/群头像异常日志优化（`exc_info=False`），减少非必要堆栈输出。

### ⚡ 优化
- WebUI 底层的 Bridge 通信容错增强


## [3.5.1] - 2026.5.4
### 同步更新了WebUI，可以更方便直观的查看插件信息（**额现在还不能编辑信息，Astrbou的这玩意还有点没搞明白.....等等我**）

### 部分工具NAPCAT似乎不太兼容......我直接测试API接口是可用的，但是换成LLM调用函数工具就用不了了，我还在研究当中

## [3.0.1] - 2026-04-21

## 没啥更新的，加了个"输入状态拟人"，私信时如果一定时间内没有找bot，bot会进入"休息"状态，在这个状态下给bot发消息，会随机延迟一段时间再回复。回复之后，后续一定时间内的回复都是实时回复。


## [3.0.0] - 2026-04-11

### ⚡ 重大性能优化
- **大幅降低 LLM Token 消耗**：将原有的 40+ 个独立工具整合为三个核心工具（`search_wyc_tools`、`call_wyc_tools`、`run_wyc_tool`），LLM 不再需要加载全部工具描述，仅按需搜索匹配的工具。实测上下文 token 占用减少 **90% 以上**。
- **强制关键词搜索机制**：LLM 必须优先使用 `search_wyc_tools` 并传入简短关键词（如"邮箱"、"禁言"），禁止直接猜测工具名，进一步减少无效调用和 token 浪费。

### 新增
- **工具 `set_qq_profile`**：调用 NapCat `set_qq_profile` 接口，支持机器人修改自身 QQ 个人资料（昵称、个性签名）。
- **管理员指令 `/set_profile`**：用法 `/set_profile nickname=新昵称 personal_note=新签名`，便于管理员快速修改机器人资料。
- **工具独立启用开关**：在配置文件中为每个工具新增 `enable_xxx` 布尔选项（默认全部启用），可在 WebUI 中单独禁用任意工具，禁用后 LLM 将无法搜索到该工具。

### 变更
- **工具调用架构重构**：
  - 新增 `search_wyc_tools`：根据关键词模糊匹配工具（支持中英文口语化描述）。
  - 新增 `call_wyc_tools`：返回当前已启用的全部工具列表（仅名称+简述）。
  - 新增 `run_wyc_tool`：执行指定工具，需传入工具名称和 JSON 参数。
  - 原有所有工具（如 `add_memory`、`send_message` 等）不再直接暴露给 LLM，仅作为内部处理函数。
- **配置系统简化**：移除基于用户角色的权限控制（如"管理员"、"群主"），统一使用工具独立开关控制可见性。
- **优化系统提示词注入**：在 LLM 请求时强制注入工具使用规范，明确要求优先搜索、使用简短关键词。

### 修复
- 修复 `call_wyc_tools` 和 `run_wyc_tool` 因 LLM 传入多余参数（如 `tool_name`、`tool_args` 错误传递）导致的日志警告问题。

### 文档
- 更新 `_conf_schema.json`，补充所有 `enable_xxx` 配置项及默认值说明。


## [v2.1.0] - 2026-04-10  

*** 本次更新包含了一个我自己认为非常好的功能，不过默认是关闭的，需要自己去配置文件打开，打开之后在私聊中发送消息就能看到"对方正在输入中…"的提示啦！***

### ✨ 新增功能

#### 个人资料与互动
- **设置QQ头像**：新增 `set_qq_avatar` 工具及 `/set_qq_avatar` 指令，支持通过引用图片消息、本地路径或URL设置机器人头像
- **用户点赞**：新增 `send_like` 工具及 `/send_like` 指令，可给指定QQ用户发送名片赞
- **获取自定义表情**：新增 `fetch_custom_face` 工具及 `/fetch_custom_face` 指令，获取机器人账号下的自定义表情列表

#### 群文件管理增强
- **移动群文件**：新增 `move_group_file` 工具及 `/move_group_file` 指令，支持将群文件移动到指定目录
- **重命名群文件**：新增 `rename_group_file` 工具及 `/rename_group_file` 指令，支持修改群文件名称
- **传输群文件**：新增 `trans_group_file` 工具及 `/trans_group_file` 指令，用于获取群文件的传输链接

#### 历史消息获取
- **获取群历史消息**：新增 `get_group_msg_history` 工具及 `/get_group_msg_history` 指令，可按序号和数量拉取群聊历史记录
- **获取好友历史消息**：新增 `get_friend_msg_history` 工具及 `/get_friend_msg_history` 指令，可按序号和数量拉取私聊历史记录

#### 群资料设置
- **设置群头像**：新增 `set_group_portrait` 工具及 `/set_group_portrait` 指令，支持通过引用图片或本地路径修改群头像

#### 用户体验优化
- **自动输入状态**：新增配置项 `auto_input_status_enabled` 和 `auto_input_status_timeout`，开启后机器人在私聊中自动显示"正在输入"状态，回复完成后自动取消
- **手动设置输入状态**：新增 `set_input_status` 工具及 `/set_input_status` 指令，可手动控制输入状态的显示与取消

#### 配置项新增
- `auto_input_status_enabled`：是否启用自动输入状态（默认关闭）
- `auto_input_status_timeout`：自动输入状态超时时间（默认10秒）



## [2.0.0] - 2026-04-09 

### ✨ 新增

- **AI 声聊功能**：基于 NapCat AI 扩展接口实现在群内发送 AI 语音消息
  - 新增 `get_ai_characters` LLM 工具，用于获取当前可用的 AI 语音角色列表
  - 新增 `send_ai_voice` LLM 工具，支持在群聊中指定角色朗读文本
  - 新增 `/ai_characters` 管理员指令，查看所有可用角色及 ID
  - 新增 `/ai_voice` 管理员指令，手动发送 AI 语音消息
  - 配置文件中新增 `ai_voice_default_character` 选项，可预设默认音色
  - 配置文件中新增 `ai_voice_max_text_length` 选项，限制单次文本长度（默认500字符）
  - 内置角色列表缓存机制，有效期10分钟，减少重复 API 调用

- **群管理功能大幅扩展**：补全 NapCat 协议中常用群管接口
  - 新增 `set_group_admin` 工具，用于设置或取消群管理员
  - 新增 `set_group_name` 工具，修改群名称（需机器人有对应权限）
  - 新增 `get_group_notice_list` 工具，获取群公告列表
  - 新增 `upload_group_file` 工具，上传本地文件到群文件
  - 新增 `create_group_file_folder` 工具，在群文件根目录创建文件夹
  - 新增 `delete_group_folder` 工具，删除群文件夹（含内部所有文件）
  - 新增 `get_group_honor_info` 工具，查询群荣誉（龙王、群聊之火等）
  - 新增 `get_group_at_all_remain` 工具，查询 @全体成员 剩余次数
  - 新增 `set_group_special_title` 工具，设置群成员专属头衔（群主权限）
  - 新增 `get_group_shut_list` 工具，获取当前被禁言的成员列表
  - 新增 `get_group_ignore_add_request` 工具，查看被忽略的加群请求
  - 新增 `set_group_add_option` 工具，修改加群方式（允许/需验证/禁止）
  - 新增 `send_group_sign` 工具，执行群打卡操作
  - 对应管理员指令同步添加（`/set_admin`、`/set_group_name`、`/list_notices` 等13个新指令）

- **智能交互增强**
  - 新增 `get_user_group_role` 工具，查询指定用户在群内的身份（群主/管理员/成员）
  - 新增 `list_contacts` 工具，直接列出好友或群聊列表，无需关键词搜索
  - 新增 `search_contacts` 工具，支持按 QQ 号、昵称、群名模糊搜索联系人

- **定时任务系统**
  - 新增 `create_scheduled_command` 高级定时指令，持久化存储并支持重启恢复
  - 支持三种操作类型：发空间说说、修改在线状态、LLM 提醒
  - 新增 `list_scheduled_commands`、`cancel_scheduled_command`、`delete_scheduled_command` 配套工具

- **记忆系统**
  - 新增 `add_memory`、`search_memories`、`update_memory`、`delete_memory`、`get_memory_detail` 五个 LLM 工具
  - 支持为记忆添加标签、设置重要度，并自动清理超量记忆

- **邮件发送**
  - 新增 `send_qq_email` 工具，通过 QQ 邮箱 SMTP 发送邮件
  - 配置项支持发件人、授权码、SMTP 服务器和端口

- **QQ 空间功能**
  - 新增 `publish_qzone` 工具，发布 QQ 空间说说
  - 自动获取并维护 cookie 和 g_tk，支持定时发布

### 🔧 优化

- **客户端获取逻辑重构**：优先从事件中获取 `bot` 实例，确保群管操作使用正确的会话权限，解决因缓存客户端导致的 `1010` 权限不足错误
- **输出内容自动截断**：所有可能返回大量数据的工具（如成员列表、文件列表）均已加入 `max_output_chars` 限制，避免超出 LLM 上下文窗口
- **群角色自动注入**：在群聊中自动将用户身份（成员/管理员/群主）注入 LLM 系统提示词，提升 AI 对权限情境的感知
- **配置文件完善**：新增 `_conf_schema.json` 完整配置项，支持在 WebUI 中可视化编辑所有插件设置

### 🐛 修复

- 修复 AI 语音接口调用失败的问题：参照正常工作的插件，增加了 `chat_type=1` 参数、文本长度限制和超时设置
- 修复 `set_group_name` 等群管接口因客户端实例错误导致的权限异常
- 修复联系人缓存过期后无法自动刷新的问题

### ⚠️ 破坏性变更

- 插件数据目录更名为 `astrbot_plugin_qzone_tools`（如需保留旧数据，请手动迁移）
- 部分 LLM 工具的参数名称与旧版本可能不一致，请重新配置 LLM 调用

### 📚 文档

- 新增 `/tool_all_help` 管理员总帮助指令，分类展示所有可用命令
- 各 LLM 工具和指令均添加了详细的参数说明和用法示例

## [1.4.0] - 2026-04-08

### ✨ 新增

- 完整群管理工具集：新增 8 个 LLM 可调用的群管理工具，支持禁言/解禁、踢人、全体禁言、修改群名片、发送/撤回群公告、设置/取消精华消息、查询群文件列表、删除群文件、处理加群申请。所有工具均通过引用消息或参数调用，群号自动从事件获取，使用更便捷。

- 删除群文件：新增 delete_group_file 工具，支持通过 file_id 删除指定群文件（需机器人有管理文件权限）。

- 输出长度限制：新增配置项 max_output_chars（默认 2000），对群成员列表、好友列表、群文件列表等大段返回内容进行截断，避免超出模型上下文限制。

- 群管理功能总开关：新增 group_manage_enabled（默认 true），可一键禁用所有群管理相关工具和指令。
- 踢人功能独立开关：新增 kick_enabled（默认 true），可单独控制踢人操作，防止误操作。
- 管理员指令全面恢复：修复了 1.3.0 版本中管理员指令仅剩 /tool_all_help 的问题，现已恢复全部 10+ 条管理指令（记忆管理、发送消息、定时任务、空间说说、状态管理、戳一戳、撤回、邮件、定时指令、联系人搜索等）。
- 群成员列表查询工具：新增 get_group_members_info 工具，返回群成员完整信息（包含 user_id、display_name、username、role），支持输出长度自动截断。

### 🔧 修复

- 设置精华消息失败：改用引用消息方式获取消息 ID，避免手动输入消息 ID 导致的 msg not found 错误（完全参照示例插件实现）。

- 管理员指令缺失：补全了所有因篇幅被省略的管理员指令，现在 /tool_send_message、/tool_schedule、/tool_publish_qzone 等命令均可正常使用。

- 群管理工具返回格式统一：所有群管理工具现在返回 {"status": "success/error", "message": "..."} 格式，与插件内其他工具保持一致。

- 定时任务检查逻辑优化：修复了周期性扫描可能漏掉刚好到期的任务的问题，现在每分钟扫描一次，精度满足大多数场景。

### ⚡ 优化

- 联系人搜索与列表输出截断：search_contacts、list_contacts、list_group_files、get_group_members_info 等工具的输出内容现在受 max_output_chars 限制，自动截断并提示，避免 LLM 上下文溢出。

- 群文件列表显示文件 ID：list_group_files 现在会同时显示 file_id，方便后续调用 delete_group_file 删除文件。

- 删除群文件支持引用（预留）：工具函数预留了从引用消息中提取 file_id 的能力，但当前要求用户显式提供 file_id（可通过 list_group_files 获取）。

### 📦 配置更新

- 新增 group_manage_enabled：群管理功能总开关，默认 true。

- 新增 kick_enabled：踢人功能独立开关，默认 true。

- 新增 max_output_chars：限制工具返回内容的最大字符数，默认 2000。

- 原有配置项（记忆上限、角色注入、邮箱设置等）保持不变，升级时自动合并。

### 🗑️ 废弃

- 移除 set_essence / del_essence 工具（旧版要求手动输入消息 ID），请使用新版 set_essence_msg / delete_essence_msg（引用消息方式）。

###📝 计划

- 后续版本将增加更多自动化管理能力（如定时清理群文件、自动审批申请等）。

## 欢迎通过 GitHub Issues 或联系作者 QQ: 1449783068（中午 12:00～凌晨 3:00）反馈建议。


## [1.3.0] - 2026-04-07 

### ✨ 新增
- **群成员身份自动注入**：在群聊中，LLM 对话时会自动注入当前用户在群内的身份（群主/管理员/成员），帮助 AI 更好地理解上下文。可通过配置项 `inject_group_role_enabled` 开关。
- **查询群成员身份工具**：新增函数工具 `get_user_group_role`，LLM 可主动查询任意用户在任意群的身份，返回"群主/管理员/成员"。
- **总帮助命令**：新增管理员指令 `/tool_all_help`，一次性展示所有可用命令及用法。

### 🔧 修复
- **定时任务重复执行**：移除独立的延迟任务，统一由周期扫描执行，并添加执行状态锁，彻底解决同一任务被多次触发的问题。
- **`recall_by_reply` 平台兼容性**：移除了对 `PlatformAdapterType` 的依赖（因旧版 AstrBot 无此导出），现在直接使用 `AiocqhttpMessageEvent` 类型，兼容性更强。
- **过去时间创建定时指令**：增加时间校验，若执行时间早于当前时间会直接返回错误，避免任务永久挂起。
- **记忆配置无法持久化**：记忆存储改用独立 JSON 文件（`memories.json`），不再依赖 `AstrBotConfig` 不可靠的保存机制，重启后数据不丢失。
- **群聊戳一戳群号获取失败**：增加群号有效性检查，若无法获取群号则返回明确错误提示。
- **QQ 空间 Cookie 解析**：改用字典解析 Cookie，优先获取 `p_skey` 再获取 `skey`，提升健壮性。
- **联系人缓存并发写入**：添加 `asyncio.Lock` 保护缓存更新，避免多协程同时修改导致数据错乱。
- **周期扫描重复添加任务**：执行前检查任务是否已在 `running_tasks` 中，防止重复创建。

### ⚡ 优化
- **记忆自动清理**：新增配置项 `max_memories_per_user`（默认 100），每个用户的记忆超过上限时自动删除最旧的记忆（按更新时间）。
- **搜索/列表支持单独类型**：`search_contacts` 和 `list_contacts` 的 `search_type` / `contact_type` 参数现支持 `all`（全部）、`friend`（仅好友）、`group`（仅群聊），LLM 和管理员指令均可使用。
- **定时任务调度统一**：所有持久化定时指令（`create_scheduled_command`）统一由后台周期扫描执行，不再混合使用延迟任务，逻辑更清晰。
- **联系人缓存更新策略**：缓存有效期延长至 5 分钟，减少 API 调用频率。

### 📦 配置更新
- 新增 `max_memories_per_user`：控制每个用户最大记忆条数，默认 100。
- 新增 `inject_group_role_enabled`：控制是否自动注入群成员身份，默认 `true`。
- 原有配置项保持不变，现有用户升级后自动合并新配置项。

## 都给我去死吧，啥玩意啊这辈子不碰py了

## [1.2.1] - 2026-04-06

### ✨ 新增
- 群聊列表/好友列表的搜索功能，支持模糊搜索（可通过 `/tool_search` 和 `/tool_list` 指令使用）。
- 完善"定时任务"与"定时消息"的区分，避免 LLM 混用两种工具。

### 🔧 修复
- 修复了插件长时间运行时 QQ 空间 Cookie 过期的问题，现在每次发空间前都会重新获取最新 Cookie。

### 📝 计划
- 下个版本添加更多工具（有建议可联系作者 QQ: 1449783068，中午 12:00～凌晨 3:00 在线）。
- 作者状态：力竭了，爱咋咋吧。

## [1.2.0] - 2026-04-01

### 🔧 修复
- 修复了部分情况下部分工具可能出现"初始化失败: 'BotAPI' object has no attribute 'call_action'"的问题。
- 修复了 `metadata.yaml` 中多余空格导致插件无法加载的问题（向受影响用户致歉，可联系作者领取 0 元补贴）。

### 📝 计划（4.1～4.20）
- 计划 5 天内完成下一个工具的开发。
- 完善"A 让 Bot 对 B 说……，结果 B 在回应时并不知道 A 让 Bot 说的话"的上下文连贯性问题。
