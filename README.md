# Wyc Tools · 更多LLM工具

> **关于仓库更名**
>
> 本仓库原名 **QzoneTools**（`astrbot_plugin_qzone_tools`），随着功能从 QQ 空间
> 扩展到群管理、消息收发、记忆管理、浏览器自动化等全域工具集，现已更名为
> **Wyc Tools**（`astrbot_plugin_Wyc_Tools`）。
>
> 为兼容老用户，插件内部标识（数据目录 `plugin_data/astrbot_plugin_qzone_tools/`、
> WebUI API 路径等）仍沿用原插件名 `astrbot_plugin_qzone_tools` ——
> **升级 / 重装不丢数据，无需手动迁移**。本文档历史更新日志中出现的
> `qzone_tools` 均指本插件旧名。

为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供 **109 个 LLM 可调用工具**：QQ空间、群管理、消息收发、记忆管理，以及一套完整的**视觉浏览器自动化**。

<p>
  <img src="https://img.shields.io/badge/version-5.7.5-blue" alt="version">
  <img src="https://img.shields.io/badge/AstrBot-%3E%3D4.24.2-green" alt="astrbot">
  <img src="https://img.shields.io/badge/NapCat-%3E4.17.55-orange" alt="napcat">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="license">
</p>

---

## 目录

- [功能特性](#功能特性)
- [安装](#安装)
- [快速开始](#快速开始)
- [浏览器自动化](#浏览器自动化)
- [完整工具列表](#完整工具列表)
- [配置说明](#配置说明)
- [管理员命令](#管理员命令)
- [常见问题](#常见问题)
- [更新日志](#更新日志)

---

## 功能特性

| 模块 | 能力 |
|------|------|
| 📝 **QQ空间** | 发表说说（自动获取最新 Cookie） |
| 💬 **消息** | 主动发消息、引用撤回、戳一戳、定时消息、高级定时指令（持久化） |
| 👥 **群管理** | 禁言、踢人、全体禁言、改名片、群公告、群文件、管理员设置、群荣誉、加群方式、打卡等 25 项 |
| 🎨 **个人资料** | 修改昵称/签名、设置 QQ 头像、设置群头像、点赞、自定义表情 |
| 🧠 **记忆管理** | 自动提取用户重要信息，支持增删改查、标签分类、自动注入上下文 |
| 📧 **邮件** | 通过 QQ 邮箱 SMTP 发送邮件 |
| 🤖 **AI 声聊** | QQ 官方免费 TTS，指定角色发送语音 |
| 🌐 **浏览器自动化** | 坐标交互（点击/双击/右键/长按/拖拽/悬停/输入）、搜索、标签页、收藏夹、反风控伪装 |
| 🧪 **工作区** | Python 代码执行（AST 沙箱）、文件读写、图片生成与发送 |
| 🛡️ **权限控制** | 109 个工具逐一配置 `global/admin/disabled`，63 个敏感工具默认仅管理员可用 |
| 🔒 **隐私模式** | 群号/QQ号 SHA1 不可逆脱敏，LLM 看不到真实 ID |
| ⚡ **稳定性** | 全部 NapCat API 带超时、异步无阻塞、后台任务防回收、浏览器空闲自动回收 |

---

## 安装

### 方式一：插件市场（推荐）

AstrBot WebUI → 插件市场 → 搜索 `Wyc_Tools`（原名 QzoneTools / `qzone_tools`）→ 安装

### 方式二：Git 克隆

```bash
cd /AstrBot/data/plugins
git clone https://github.com/Wyccotccy/astrbot_plugin_Wyc_Tools.git
```

### 方式三：手动上传

下载仓库 ZIP → WebUI → 插件 → 安装插件 → 上传压缩包

安装后重启 AstrBot 或重载插件即可。首次使用浏览器功能时，插件会自动检测并安装 Playwright + Chromium（约 2 分钟）。

---

## 快速开始

### 三步工具调用机制（LLM 必读）

插件共有 109 个工具，**不会一次性全部注入上下文**（那会浪费大量 token）。LLM 必须遵循三步流程：

```
第 1 步：search_wyc_tools("关键词")      ← 用简短关键词搜索，禁止用完整问句
第 2 步：call_wyc_tools()                ← 搜索不到时才查看完整列表
第 3 步：run_wyc_tool("工具名", {...})   ← 确定工具名后执行
```

### 推荐系统提示词

插件会通过 `on_llm_request` 自动注入工具使用规范，通常无需额外配置。如需自行添加：

```
你拥有通过工具调用实现的以下能力，在适当场景下请主动使用：

调用任何功能前，必须先使用 search_wyc_tools 搜索工具名称（使用简短关键词），
再通过 run_wyc_tool 执行。禁止直接猜测或编造工具名称！

可用功能领域：QQ空间、戳一戳、联系人搜索、主动发消息、定时任务、QQ状态、
消息撤回、QQ邮件、记忆管理、群管理、AI语音、个人资料、浏览器自动化
```

### 使用示例

| 用户说 | LLM 调用链 |
|--------|-----------|
| "帮我发条说说，今天天气真好" | `search_wyc_tools("发说说")` → `run_wyc_tool("publish_qzone", {"content": "今天天气真好"})` |
| "找一下通知群" | `search_wyc_tools("搜索")` → `run_wyc_tool("search_contacts", {"keyword": "通知"})` |
| "把捣乱的张三禁言 10 分钟" | `search_wyc_tools("禁言")` → `run_wyc_tool("set_group_ban", {...})` |
| "记住我喜欢喝咖啡" | `search_wyc_tools("记忆")` → `run_wyc_tool("add_memory", {"content": "用户喜欢喝咖啡"})` |
| "打开 B 站看看热搜" | `search_wyc_tools("浏览器")` → `run_wyc_tool("browser_visit", {"url": "..."})` |

---

## 浏览器自动化

### 坐标交互体系（v5.1.0+）

插件**不使用 CSS 选择器**，而是采用「AI 看截图 → 输出坐标 → 看新截图」的视觉闭环，因此能操作任何页面（包括动态渲染、Canvas、验证码）：

```
1. browser_visit("https://example.com")   → 自动回传截图
2. AI 看图，确定按钮在 (253, 158)
3. browser_click(253, 158)                → 又回传新截图
4. AI 看到结果，决定下一步
```

坐标与截图像素一一对应（左上角为原点），每次操作后自动回传最新截图。坐标越界会自动贴边，不会报错。

| 工具 | 用途 |
|------|------|
| `browser_click` | 点击坐标 |
| `browser_double_click` | 双击（选中文本/打开文件夹） |
| `browser_right_click` | 右键（上下文菜单） |
| `browser_long_press` | 长按（100–10000ms，唤起悬浮菜单） |
| `browser_drag` | 拖拽（自动插值，模拟真实拖动） |
| `browser_hover` | 悬停（触发下拉菜单/提示） |
| `browser_input_at` | 点击坐标处输入框并逐字键入 |
| `browser_wait` | 等待 5–45 秒（自动通知用户 + 回传新截图） |

### 反风控伪装（v5.2.0+）

以 `headless=True` 裸指纹运行会被绝大多数带风控的网站识别拦截。插件已内置完整伪装（配置项 `browser_stealth_enabled`，默认开启）：

| 层面 | 措施 |
|------|------|
| 内核 | 优先 `channel="chromium"` 完整版新无头模式，插件/`window.chrome` 与真人浏览器一致 |
| UA | 读取内核真实 UA 并抹除 `Headless` 字样，版本号与内核精确匹配 |
| JS 注入 | 隐藏 `navigator.webdriver`、补齐 `window.chrome`、WebGL 厂商伪装、`permissions` 修正、中文语言 |
| 环境 | `locale=zh-CN`、`timezone=Asia/Shanghai`、浅色配色 |
| 启动参数 | 移除 `--disable-gpu`、新增 `--disable-blink-features=AutomationControlled` |

> ⚠️ 仍可能被拦的残留特征：**海外机房 IP**、首次访问无历史 Cookie。遇到滑块验证需人工过一次，之后 Cookie 会持久化复用。

### 视觉模型门禁

26 个浏览器工具**仅对支持图像输入的模型开放**（配置项 `browser_vision_gate_enabled`）。非多模态模型调用会收到明确提示，引导切换模型。

### 其他浏览器工具

`browser_search`（百度/必应/谷歌）、`browser_visit`、`browser_input`、`browser_scroll`、`browser_zoom`、`browser_screenshot`、`browser_back` / `browser_forward`、`browser_tabs` / `browser_close_tab`、`browser_chat`、`browser_favorite_*`（收藏夹）、`browser_install`（手动装依赖）

### 支持的浏览器引擎

- **chromium**（默认）— 通用性强，推荐
- **firefox** — 兼容性好
- **webkit** — 资源占用低

---

## 完整工具列表

<details>
<summary><b>展开全部 109 个工具</b></summary>

### 记忆管理（5）
`add_memory` · `search_memories` · `update_memory` · `delete_memory` · `get_memory_detail`

### 消息与定时（8）
`send_message` · `schedule_message` · `cancel_scheduled_message` · `list_scheduled_messages`
`create_scheduled_command` · `list_scheduled_commands` · `cancel_scheduled_command` · `delete_scheduled_command`

### QQ空间 / 互动（4）
`publish_qzone` · `send_poke` · `send_like` · `recall_by_reply`

### QQ状态（3）
`update_qq_status` · `get_qq_status` · `get_fun_status_list`

### 邮件（1）
`send_qq_email`

### 联系人（3）
`search_contacts` · `list_contacts` · `get_user_group_role`

### 群管理（25）
`set_group_ban` · `set_group_kick` · `set_group_whole_ban` · `set_group_card` · `set_group_admin`
`set_group_name` · `set_group_special_title` · `set_group_add_option` · `set_group_portrait`
`send_group_notice` · `delete_group_notice` · `get_group_notice_list`
`set_essence_msg` · `delete_essence_msg` · `get_group_members_info` · `get_group_shut_list`
`get_group_honor_info` · `get_group_at_all_remain` · `get_group_ignore_add_request` · `send_group_sign`

### 群文件（9）
`list_group_files` · `upload_group_file` · `delete_group_file` · `create_group_file_folder`
`delete_group_folder` · `move_group_file` · `rename_group_file` · `trans_group_file` · `get_share_link`

### 个人资料（4）
`set_qq_profile` · `set_qq_avatar` · `fetch_custom_face` · `set_input_status`

### AI 声聊（2）
`get_ai_characters` · `send_ai_voice`

### 历史消息（2）
`get_group_msg_history` · `get_friend_msg_history`

### 闪传（8）
`create_flash_task` · `get_flash_file_list` · `get_flash_file_url` · `send_flash_msg`
`get_fileset_info` · `get_fileset_id` · `download_fileset` · `send_flash_msg`

### 在线文件（6）
`get_online_file_msg` · `send_online_file` · `send_online_folder`
`receive_online_file` · `refuse_online_file` · `cancel_online_file`

### 好友管理（1）
`delete_friend`

### 工作区（5）
`run_python_code` · `list_workspace_files` · `read_workspace_file` · `read_image` · `send_file` · `delete_workspace_file`

### 浏览器（26）
**坐标交互**：`browser_click` · `browser_double_click` · `browser_right_click` · `browser_long_press` · `browser_drag` · `browser_input_at` · `browser_hover` · `browser_wait`

**页面操作**：`browser_search` · `browser_visit` · `browser_input` · `browser_scroll` · `browser_zoom` · `browser_screenshot` · `browser_back` · `browser_forward` · `browser_tabs` · `browser_close_tab` · `browser_close` · `browser_chat`

**收藏夹**：`browser_favorite_list` · `browser_favorite_add` · `browser_favorite_delete`

**其他**：`fetch_url` · `browser_install` · `open_page` · `screenshot_page` · `close_page`

</details>

> 工具名可用 `search_wyc_tools("<关键词>")` 检索，或 `call_wyc_tools()` 查看完整列表（含参数说明）。

---

## 配置说明

WebUI → 插件 → 更多LLM工具 → 配置页。共 **164 个配置项**，常用项：

### 基础

| 配置 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 插件总开关 |
| `enable_<工具名>` | `true` | 单独启停任意工具（109 项全量覆盖） |
| `tool_permissions` | `{}` | 权限档位 `{"工具名": "global/admin/disabled"}` |
| `privacy_mode` | `normal` | `privacy` 时群号/QQ号 SHA1 脱敏 |
| `max_output_chars` | — | 工具返回内容字符上限（防上下文溢出） |

### 记忆与人格

| 配置 | 说明 |
|------|------|
| `max_memories_per_user` | 每用户最大记忆数（超出删最旧） |
| `memory_inject_enabled` | 自动注入用户记忆到上下文 |
| `max_inject_memories` | 单次注入的最大记忆条数 |
| `inject_group_role_enabled` | 自动注入发言者在群内的身份 |
| `inject_tool_prompt_enabled` | 注入工具使用说明（约 800–1000 token，默认关） |

### 功能开关

| 配置 | 说明 |
|------|------|
| `group_manage_enabled` | 群管理功能总开关 |
| `kick_enabled` | 踢人开关（需群管理总开关开启） |
| `search_enabled` | 联系人搜索 |
| `auto_input_status_enabled` | 私聊自动显示"正在输入" |
| `enable_human_typing` | 拟人化回复延迟（含 `typing_*` 系列参数） |

### 邮件

| 配置 | 说明 |
|------|------|
| `email_sender` | 发件人 QQ 邮箱 |
| `email_authorization_code` | 授权码（非登录密码，WebUI 脱敏） |
| `email_smtp_server` / `email_smtp_port` | SMTP 服务器与端口 |

### 浏览器

| 配置 | 默认 | 说明 |
|------|------|------|
| `browser_vision_gate_enabled` | `true` | 视觉模型门禁 |
| `browser_stealth_enabled` | `true` | 反风控伪装 |
| `browser_type` | `chromium` | chromium / firefox / webkit |
| `browser_mode` | `embedded` | embedded / external(CDP) |
| `cdp_url` | — | CDP 远程调试地址（external 模式） |
| `browser_render_mode` | `full` | full / simple / minimal / text |
| `image_output_format` | `png` | 截图输出格式（webp 部分模型不支持） |
| `llm_screenshot_text_only` | `false` | 截图仅返回文本（非多模态模型用） |
| `viewport_size` / `max_pages` / `timeout` | — | 视口、标签页上限、超时 |
| `idle_timeout` | `300` | 空闲多久自动关浏览器 |

### 安全

| 配置 | 说明 |
|------|------|
| `ssrf_blocked_urls` | 自定义阻断 URL/IP |
| `ssrf_custom_blocked_ranges` | 自定义阻断网段（CIDR） |
| `run_python_sandbox_enabled` | Python 增强沙箱 |
| `workspace_banned_patterns` | 工作区禁止的代码正则 |
| `resolve_image_restricted` | 限制图片读取路径 |
| `flash_transfer_dir` / `docker_container_name` | 闪传共享目录与 NapCat 容器名 |

---

## 管理员命令

51 个 QQ 命令走独立路径，**仅 AstrBot 管理员**（`admins_id`）可用，不受 LLM 权限体系影响：

<details>
<summary><b>展开命令列表</b></summary>

| 命令 | 说明 |
|------|------|
| `/tool_all_help` | 查看完整帮助 |
| `/tool_memory list/add/delete/update/get` | 记忆管理 |
| `/tool_send_message <目标ID> <消息>` | 发送消息 |
| `/tool_schedule <目标ID> <消息> <时间>` | 定时消息 |
| `/tool_scheduled_list` / `_cancel <ID>` / `_delete <ID>` | 定时指令管理 |
| `/tool_publish_qzone <内容>` | 发说说 |
| `/tool_status <状态> <分钟>` / `/tool_status_get` | QQ 状态 |
| `/tool_poke <QQ号>` | 戳一戳 |
| `/tool_recall` | 引用撤回 |
| `/tool_email <收件人> <主题> <内容>` | 发邮件 |
| `/tool_search <关键词>` / `/tool_list [类型] [limit]` | 联系人 |
| `/ai_characters` / `/ai_voice [角色] <文本>` | AI 语音 |
| `/ban_user <QQ号> <分钟>` / `/unban_user <QQ号>` | 禁言/解禁 |
| `/kick <QQ号>` | 踢出 |
| `/whole_ban <on/off>` | 全体禁言 |
| `/set_card <QQ号> <昵称>` | 改名片 |
| `/send_notice <内容>` / `/del_notice <公告ID>` / `/list_notices` | 群公告 |
| `/list_files` / `/delete_group_file <file_id>` / `/upload_file <路径> [文件名]` | 群文件 |
| `/create_folder <名称>` / `/del_folder <ID>` | 群文件夹 |
| `/move_group_file` / `/rename_group_file` / `/trans_group_file` | 文件操作 |
| `/group_members` | 群成员列表 |
| `/set_admin <QQ号> <on/off>` | 管理员设置 |
| `/set_group_name <名称>` | 群名称 |
| `/group_honor [类型]` | 群荣誉 |
| `/at_all_remain` | @全体剩余次数 |
| `/set_title <QQ号> <头衔>` | 专属头衔 |
| `/shut_list` | 禁言列表 |
| `/ignore_requests` | 忽略的加群请求 |
| `/set_add_option <选项>` | 加群验证方式 |
| `/group_sign` | 群打卡 |
| `/set_qq_avatar [图片]` | 设置QQ头像 |
| `/set_group_portrait [群号] [图片]` | 设置群头像 |
| `/set_profile nickname=xxx personal_note=xxx` | 个人资料 |
| `/send_like <QQ号> [次数]` | 点赞 |
| `/get_group_msg_history [群号] [序号] [数量]` | 群历史消息 |
| `/get_friend_msg_history <QQ号> [序号] [数量]` | 好友历史消息 |
| `/fetch_custom_face [数量]` | 自定义表情 |
| `/set_input_status <QQ号> <类型>` | 输入状态 |

</details>

---

## 常见问题

<details>
<summary><b>发说说失败（Cookie 无效）</b></summary>

1. 检查 NapCat 是否已登录
2. 确认 NapCat 版本 ≥ 4.17.55（需支持 `get_credentials` / `get_cookies`）
3. 重新登录 NapCat 刷新 Cookie（插件每次发说说都会自动取最新 Cookie，无需手动配置）
</details>

<details>
<summary><b>LLM 不调用工具 / 说不知道怎么做</b></summary>

1. 确认插件已启用（`enabled: true`）
2. 日志中确认 `search_wyc_tools` 已加载
3. 该模型需支持 Function Calling
</details>

<details>
<summary><b>浏览器工具被拒绝（视觉门禁）</b></summary>

提示「当前模型不支持图像输入（视觉），无法使用浏览器功能」：

1. 切换到多模态模型（GPT-4o / GLM-4V / Qwen-VL / Gemini / Claude 等）
2. 或到 AstrBot 服务商配置为当前模型勾选「图像」能力
3. 或关闭 `browser_vision_gate_enabled`（不推荐，AI 看不到截图就无法操作）
</details>

<details>
<summary><b>网页被风控拦截 / 一直弹验证码</b></summary>

1. 确认 `browser_stealth_enabled` 为开启状态
2. 海外机房 IP 本身信誉低，这是伪装无法消除的
3. 首次访问无 Cookie，遇到滑块需人工过一次，之后 Cookie 会复用
4. 极严风控（如部分登录页）建议改用 `browser_mode=external` 接真机浏览器 CDP
</details>

<details>
<summary><b>定时任务到点没执行</b></summary>

1. 检查日志确认任务已加载（重启后自动恢复）
2. 超过计划时间 5 分钟以上的任务会被跳过（防过期补发）
3. 确认目标 ID 仍然有效
</details>

<details>
<summary><b>浏览器启动失败 / PTY spawn failed</b></summary>

1. 插件会自动安装 Playwright，首次约需 2 分钟
2. 可手动调用 `browser_install` 工具
3. 容器内以 root 运行需 `--no-sandbox`（插件已自动处理）
</details>

<details>
<summary><b>闪传功能不可用</b></summary>

闪传需要 AstrBot 与 NapCat 共享目录（Docker 环境两容器文件系统隔离）：

```bash
# 宿主机创建共享目录
mkdir -p /opt/astrbot_flash

# AstrBot 容器挂载（读写）
docker run -v /opt/astrbot_flash:/tmp/astrbot_flash:rw ...

# NapCat 容器挂载（只读）
docker run -v /opt/astrbot_flash:/tmp/astrbot_flash:ro ...
```

然后配置 `flash_transfer_dir = /tmp/astrbot_flash`。

替代方案：用 `send_file` 工具直接发送文件。
</details>

---

## 更新日志

完整历史见 [CHANGELOG.md](CHANGELOG.md)。

### v5.7.5 — 仓库更名 + 高危工具权限硬下限修复 + 补充搜索词

**🏷️ 仓库更名**：`astrbot_plugin_qzone_tools`（QzoneTools）→
`astrbot_plugin_Wyc_Tools`（Wyc Tools）。GitHub 旧地址会自动跳转到新地址；
插件内部标识与数据目录仍沿用旧名，升级无需任何迁移操作。

**🔒 安全修复（严重）**：修复适配器高危工具（/ban、/kick、/退群 等源命令
带超管门槛的 50 个）可在 WebUI 被降档为「全局」的漏洞——现 perm_default
为 admin 的适配器工具**永远**按超管档判定，配置无法覆盖；
WebUI 同步锁定该类工具的下拉框（「超管锁定」徽章）。

**修复**：进入「浏览器接管」页不再误自动拉起浏览器（只读查询不再产生副作用）。

**✨ 新增「补充工具搜索词」页**：每个工具（含适配器工具）可在内置搜索词外
追加自定义词（列表式 chips，内置词锁定不可删、补充词可增删），
有改动时右下角浮出保存按钮；显著提升 `search_wyc_tools` 命中率。

**界面**：适配器权限控制页适配手机端；WebUI 头像更换为插件形象图。

### v5.7.0 — 工具适配器（桥接全生态插件命令给 AI）

**新功能**：把 AstrBot 全生态其他插件的 `@filter.command` 命令自动桥接为
AI 可调用的 wyc-tools 工具，WebUI 勾选即生效。

- **工具适配器页**：一键枚举全部可桥接命令（自动取插件名/描述/参数表/权限门槛），
  按来源插件分组展示，勾选保存后注册为
  `Wyc_assistive_tool_<插件名>_<工具名>`；中文命令自动转拼音；
  **默认全部关闭**，未勾选绝不注册
- **适配器权限控制页**：对已注册的适配器工具单独设权限档位（四档），
  与既有 109 个工具的权限表合并保存；源命令带管理员门槛的默认「超管」档
- **AI 调用**：经 search_wyc_tools → run_wyc_tool 三步协议，
  支持框架带参指令（自动类型转换）与自行解析 message_str 两类命令，
  协程 / 异步生成器两种 handler 形态均可执行

设计说明：命令枚举依赖 AstrBot 内部 API（无稳定性承诺），
所有字段访问均为防御式，框架升级后表现为"枚举为空/跳过该条"而非崩溃。

### v5.6.3 — 已知 Bug 修复 + 接管页监控增强

**修复僵尸进程泄漏（严重）**：`verify_browser` 启动探测时未设置
`PLAYWRIGHT_BROWSERS_PATH`，走了 playwright 默认缓存路径，导致重复下载一份内核（白占 646 MB）
且探测进程无人回收、累积成僵尸。现所有调用点统一传插件内核目录，从源头消除。

**修复 webkit / firefox 装上却启动不了**：缺少 `libgstreamer` / `libgtk-4` 等 28 个系统库。
新增 `install_system_deps()`（调 `playwright install-deps`），
并在检测到缺库时自动补装重试；WebUI 新增「安装系统依赖」一键按钮。

**接管页增强**：
- 服务器内存进度条（其他占用 / 浏览器占用 / 关闭阈值线）
- 实时指标：画面延迟 / 服务器刷新率 / 下行带宽 / 累计帧数（全部用户端计算）
- 标签页管理：列表 / 切换 / 关闭 / 新建 / 一键关闭全部
- 浏览器被自动关闭时在页面顶部提示原因

**行为修正**：标签页管理方向（接管中也可用）、
「卡住」判据改用状态心跳（静止画面不再误报）、
内存阈值热更新、「就绪」状态按真实进程判定。

### v5.6.2 — 提示词去重融合 + 操作图标换新 + 仓库文件补齐

**提示词注入重构**：整合为三段职责单一的注入块，消除原有冲突，并支持按条件注入省 token。

- `_TOOL_RULES`（无条件）：工具调用铁律 + 回复发送规则（防重复回复）+ 说话风格
- `_MEMORY_DISCIPLINE`（仅记忆工具可用时）：记忆强化 + 每 8 句强制记忆一次
- `_BROWSER_GUIDE`（仅浏览器启用时）：浏览器工具使用指引，含「接管工具免搜索」例外说明
- 新增两个 WebUI 开关，可分别关闭记忆强化 / 浏览器指引注入

**操作图标换新**：4 个图标升级为统一设计（鼠标指针 + 动作徽章），400×400 更清晰，
锚点统一为指针尖端，落位语义与实际操作点完全一致。

**仓库文件补齐**：修复 `.gitignore` 误屏蔽 `resource/` 导致图标未随仓库分发的问题（市场安装后图标会失效），
补 `LICENSE`（MIT）与 `requirements.txt`。

### v5.6.1 — 合规修复：移除内置 logging
市场安全审查指出两处 `try/except` 兜底用了 Python 内置 `logging`，违反「logger 必须且只能从 `astrbot.api` 导入」。
移除兜底改为直接导入，全插件 7 个模块复查无残留。

### v5.6.0 — 接管页键盘重构
电脑端物理键盘直通（组合键与 F5/F11/F12 保留本地）、手机端完整虚拟键盘（方向键/退格/删除/Home/End/PgUp/PgDn/空格），
顺带修复快速打字丢字（改为队列串行 + 仅鼠标移动允许丢帧）。

### v5.5.0 — 非阻塞接管（时长自由）+ 触摸点击/画面闪烁修复
**架构改造**：接管工具改为**立即返回**，用户操作结束后由事件回调把 AI 唤醒。
接管时长因此**不再受框架「工具调用超时时间」约束**，可在 WebUI 里自由设置（最高 3600 秒）。

- 唤醒走 AstrBot 官方的合成事件机制（`CronMessageEvent` + 事件队列），
  等于"用户又发了条消息"，AI 带完整上下文继续；截图自动转成图片输入，AI 能直接看到
- 群聊自动补 `@机器人`（唤醒检查对群消息有此要求，否则会被丢弃）
- 唤醒失败时兜底直发消息，信息不丢

**修复**
- **手机端触摸点击失效**：`moveCursor()` 里一行残留占位语句抛 `ReferenceError`，
  导致 `gestureStart` 未执行、`touchend` 直接返回。用 Playwright 真实触摸事件定位
- **画面一闪一闪**：状态轮询每 2 秒重弹一次连接遮罩，静止页面无新帧来隐藏它
- **点击延迟**：接管点击改走 `click_raw`（原来走的 `click_coord` 会持锁 + sleep 2 秒）
- `_unfreeze_page` 现在真正还原 `setInterval` / `requestAnimationFrame`

### v5.4.1 — 修复接管工具调用即崩
**核心修复**：`request_browser_takeover` 此前被实现为 async generator（用 `yield` 做"心跳保活"），
但插件的 `run_wyc_tool` 用 `await` 调用它，导致线上报错 `object async_generator can't be used in 'await' expression`。

**更深一层**：原"心跳保活"方案本身违反框架语义——`yield None` 在 AstrBot 中意味着"工具已直接把消息发给用户"，
会触发 `AgentState.DONE` **提前结束整个 Agent 回合**；多次 yield 非空值还会产生重复 tool_call_id。

**修复方案**：改造为**普通协程**，等待时长按框架 `tool_call_timeout` 钳制
（`min(WebUI上限, 框架超时 - 12s)`），到点主动结束并按「系统超时」上报；新增 `inspect.isasyncgenfunction` 防御分支。

> ⚠️ **注意**：接管等待时长受 AstrBot 全局「工具调用超时时间」约束（默认 120 秒）。
> 若该值设为 60 秒，WebUI 里填 120 秒会被自动钳制为 **48 秒**（并在通知中如实告知）。
> 需要更长接管时间，请调高 AstrBot 设置 → 智能体中的「工具调用超时时间」。

同时修复两处会话切换并发隐患（监控协程误伤新会话、旧投屏掐断新投屏）。

### v5.4.0 — 浏览器接管（AI 求助真人过验证码）
**核心能力**：AI 遇到验证码 / 滑块 / 人机校验等无法自动完成的环节时，可把浏览器操作权**临时交给真人用户**，用户在 WebUI 上实时看到画面并直接操作，完成后交还 AI。

**AI 侧**
- 新增 `request_browser_takeover` 工具，**免搜索直连**（应急场景来不及搜索）
- **非阻塞**：工具立即返回，用户操作结束后由事件回调唤醒 AI（见 v5.5.0 架构说明）
- 发起时自动发送系统消息通知用户（含超时秒数）
- 结束时按原因区分文案：用户手动结束 / 系统超时 / 用户久未操作，并附上最新截图

**权限模型**
- **单向授权**：用户无法主动夺取权限，必须由 AI 发起
- 接管期间**仅冻结浏览器工具**，其他工具不受影响
- 双计时器：空闲超时（默认 60s）+ 总时长上限（默认 120s），均可配置

**WebUI 新增「浏览器接管」选项卡**
- 未启用浏览器时提示「浏览器尚未启用」
- 接管中自动出现实时画面，**鼠标与手机触屏都可直接操作**
- 手势自动识别：点击 / 长按 / 拖动 / 滚轮 / 键盘 / 手机真触摸
- 倒计时（取更紧迫者）、已操作次数、全屏、结束操作按钮

**画面与画质**
- 走 CDP `Page.startScreencast`（变化驱动推帧），SSE 传输，断线自动重连
- 提供「自动检测最佳画质」：实测端到端吞吐后取 80% 预算，自动填入画质与宽度

**三重提示保障 AI 知道接管存在**（不依赖不可靠的失败检测）
1. 工具描述与 keywords 覆盖验证码/滑块等词
2. 所有浏览器工具返回值固定追加提示
3. 浏览器会话活跃期间上下文持续注入

同时明确约束"普通操作失败请先自行重试，不要随意转交"，避免 AI 滥用。

### v5.3.1 — 修复截图超时/删除失效/输入框样式 + 图标锚点精准化
**修复**
- **「点击失败」实为截图超时**：Playwright 截图前会等页面所有字体加载完成，百度等站点字体请求永久挂起导致 30 秒超时（点击其实已生效）。改用 CDP `Page.captureScreenshot` 直接取图，实测 **0.1 秒**返回；不可用时自动回退，并把原生超时收紧到 15 秒
- **WebUI 无法删除记忆**：iframe 沙箱拦截了原生 `confirm()`（静默返回 false）。新增自带确认弹窗，替换全部 5 处（删除记忆/批量删除/清理重复/删除文件/取消定时）
- **部分输入框显示为原生黑框**：页面上 9 个 `<input>` 未写 `type`，不匹配 `input[type="text"]` 选择器而漏掉样式。补 `input:not([type])` + `-webkit-appearance: none`
- **手机端记忆页文字溢出**：卡片加 `min-width:0` + `overflow-wrap:anywhere`；超长内容折叠 6 行并提供「展开全文」；筛选/批量栏改纵向自适应

**优化**
- **图标改为 SVG 渲染的半透明 PNG**：完整保留原设计的 50% 半透明效果
- **每个图标独立锚点**：点击=鼠标指针尖端、长按=手指接触点、输入=I 光标中心、拖动=箭头中心，贴图时锚点对准操作坐标（此前用包围盒中心会导致偏移）
- **拖动同时标注起点与终点**：起点蓝点 + 主线 + 方向箭头 + 两端图标，分别标注「起点」「终点」

### v5.3.0 — 浏览器操作展示 + WebUI 重绘 + 记忆管理增强
**浏览器操作展示（纯代码实现，不依赖提示词）**
- **始终显示 AI 操作**：AI 每操作一次浏览器就强制把截图发到当前会话，不经过 LLM，AI 无法"忘记"发图
- **浏览器操作增强显示**：在截图上用图标标出 AI 操作的精确位置 —— 点击=蓝色指针、长按=橙色手指、输入=绿色光标、拖动=紫色箭头连线；拖动还绘制起点→终点完整轨迹
- 图标用 alpha 包围盒定位到图形视觉中心，坐标缩放自适应，越界自动贴边，异常时退化为十字准星

**WebUI 全量重绘（PC / 移动端分别兼容）**
- PC 端：左侧固定侧边栏 + 卡片式主内容区
- 移动端（≤900px）：顶部栏 + 底部固定标签栏，适配 iOS 安全区
- iOS 风格滑动开关、全新配色体系、页面切换动画、工具搜索框

**记忆管理大幅增强**
- 可视化统计看板（总数 / 用户数 / 标签数 / 高重要度）
- 按用户、标签筛选 + 按时间/重要度排序
- 批量维护：全选、批量删除、批量改重要度、批量加标签
- 数据管理：导出 JSON 备份、导入合并（自动去重）、一键检测并清理重复记忆
- 记忆卡片彩色标签与重要度分级展示

### v5.2.5 — 注入上下文不再破坏模型前缀缓存（市场上架合规）
- `on_llm_request` 不再改写 `request.system_prompt`，改用官方推荐方式：`req.extra_user_content_parts` + `TextPart(...).mark_as_temp()`
- 注入内容（系统状态 / 用户记忆 / 群身份 / AI语音配置 / 工具说明）附加到**当前用户消息末尾**，并标记为临时内容——只发给模型、不写入会话历史，历史前缀保持稳定，前缀缓存命中率不再受影响
- 防御式导入 `TextPart` + 探测 `extra_user_content_parts`，极旧框架或第三方 Agent 下自动退化，不影响插件加载
- 配置项文案同步更正为「临时内容，不写入会话历史」

### v5.2.4 — 持久化数据迁出插件目录（市场上架合规）
- 工作区 / 字体 / 收藏夹 / 刻度资源全部迁移到 `data/plugin_data/astrbot_plugin_qzone_tools/`（旧插件名，现继续沿用）
- 新增自动迁移逻辑，老用户升级不丢数据
- `.gitignore` 补充运行时目录，`favorite.json` 移出版本控制

### v5.2.3 — 权限档位扩展为 4 档
- 新增「群主/管理员（仅群聊）」档：群聊中群主/群管理可用，私聊自动回退超管
- 档位语义拆分：`global` / `groupadmin` / `admin`（仅 admins_id）/ `disabled`
- WebUI 权限页新增第 4 档选项与说明

### v5.2.2 — 修复权限/代理/资料三处问题
- **#13 权限设置重载后失效**：`tool_permissions` 改存独立文件 `tool_permissions.json`，避开 AstrBot 配置完整性检查对空 object 子键的清理（此前保存即被删，日志实锤）
- **#12 浏览器设置代理后无法使用**：Playwright 的 `new_context(proxy=)` 只接受 dict，新增 `_normalize_proxy()` 自动转换字符串（支持无 scheme / 账密 / socks5）
- **#9 无法设置 personal_note**：NapCat 要求 `nickname` 与 `personal_note` 同时必填，未指定字段自动用当前资料回填
- WebUI 权限页改为显示**实际生效档位**，不再把敏感工具误显示为「全局」

### v5.2.1 — 修复重复回复
LLM 先输出完整文本（立即发出）→ 再调 `send_message_to_user` 因幻觉 session 失败 → 最终回复重复同样内容。已通过 `on_llm_request` 注入发送规则约束。

### v5.2.0 — 浏览器反风控伪装
修复裸 headless 指纹导致 99% 带风控网站拦截：完整版 Chromium 新无头内核 + UA 抹平 + `navigator.webdriver` 隐藏 + WebGL 厂商伪装 + 中文语言/时区。

### v5.1.0 — 坐标交互体系 + 安全大修
- 移除 CSS 选择器操作，全面转向视觉坐标交互（新增 6 个坐标工具 + `browser_wait`）
- 视觉模型门禁
- 安全：修复工具路径权限绕过（严重）、Python 沙箱失效、SSRF 多种绕过、路径穿越
- 稳定性：消除 30 秒事件循环阻塞、72 处 NapCat API 加超时、后台任务防回收
- 补齐 45 个工具开关（109 个全覆盖）

### 早期版本
v5.0.x（图片发送/安全加固）、v4.x（浏览器自动化首版）、v3.x（记忆管理/群管扩展）、v1.x–v2.x（初版）

---

## 开发者信息

- **作者**：Wyccotccy
- **仓库**：https://github.com/Wyccotccy/astrbot_plugin_Wyc_Tools
- **反馈**：GitHub Issues 或 QQ 1449783068（12:00–03:00）

## 许可证

MIT License
