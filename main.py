# -*- coding: utf-8 -*-
import asyncio
import hashlib
import inspect
import json
import os
import re
import socket
import time
import uuid
import smtplib
import random
import sys
import textwrap
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from urllib.parse import urlencode, quote as url_quote, urljoin

import aiohttp
import ipaddress
from pathlib import Path
from quart import jsonify, request
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain, Reply, File, Image
try:
    from astrbot.core.agent.message import TextPart
except ImportError:  # 极旧版本框架无此类，退化为 system_prompt 注入
    TextPart = None
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from .core.supervisor import BrowserSupervisor
from .core.favorite import FavoriteManager
from .core.ticks_overlay import TickOverlay
from .core.action_overlay import ActionOverlay
from .core.takeover import (
    TakeoverManager,
    REASON_USER_END, REASON_IDLE_TIMEOUT, REASON_MAX_TIMEOUT,
    REASON_BROWSER_GONE, REASON_ABORTED, REASON_TEXT,
)
from .core.image_utils import convert_image_format, get_format_from_config, get_output_ext

import mcp

PLUGIN_NAME = "astrbot_plugin_qzone_tools"


# ==================== 安全辅助 ====================

# SSRF 黑名单默认值
DEFAULT_SSRF_BLACKLIST = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "0.0.0.0/8",
    "metadata.google.internal",
    "169.254.169.254",
]

# 配置白名单 — 只允许这些字段通过 WebUI 保存
CONFIG_SAVE_WHITELIST = {
    "email_sender", "email_authorization_code", "email_smtp_server", "email_smtp_port",
    "max_memories_per_user", "max_inject_memories", "memory_inject_enabled",
    "max_output_chars", "search_max_chars",
    "ai_voice_default_character", "ai_voice_max_text_length",
    "auto_input_status_enabled", "auto_input_status_timeout",
    "enable_human_typing", "typing_idle_threshold",
    "typing_initial_delay_min", "typing_initial_delay_max",
    "enabled", "group_manage_enabled", "kick_enabled", "search_enabled",
    "inject_tool_prompt_enabled", "inject_group_role_enabled",
    "inject_memory_discipline_enabled", "inject_browser_guide_enabled",
    "image_output_format", "browser_render_mode", "llm_screenshot_text_only",
    "screenshot_quality",
    "privacy_mode",
    # 工作区
    "workspace_enabled", "workspace_banned_patterns", "flash_transfer_dir",
    # 安全
    "ssrf_blocked_urls", "ssrf_custom_blocked_ranges",
    "python_run_auto_image_enabled",
    "resolve_image_restricted", "run_python_sandbox_enabled",
    "docker_container_name", "napcat_container_name",
    "tool_permissions",
    # 浏览器
    "browser_type", "browser_mode", "cdp_url", "verify_browser",
    "default_url", "proxy", "viewport_size", "max_pages",
    "timeout", "zoom_factor", "max_memory_percent",
    "idle_timeout", "monitor_interval", "browser_memory_limit_mb",
    "browser_vision_gate_enabled", "browser_stealth_enabled",
    # 浏览器操作展示（v5.3.0）
    "browser_always_show_action", "browser_action_overlay_enabled",
    "browser_action_push_text",
    # 浏览器接管（v5.4.0）
    "takeover_max_seconds", "takeover_idle_seconds",
    "takeover_jpeg_quality", "takeover_max_width", "takeover_auto_detected",
}


# WebUI 返回配置时需要脱敏的字段（保存时若回传掩码则保留原值）
SENSITIVE_FIELDS = {"email_authorization_code"}

# 掩码占位符 — 前端回传此值时视为"未修改"
MASK_PLACEHOLDER = "***"

# 敏感工具 — 未在 tool_permissions 中显式配置时，默认仅管理员可用
# （全局管理员 admins_id / 群主 / 群管理）
SENSITIVE_TOOLS = {
    # 群管理
    "set_group_ban", "set_group_kick", "set_group_whole_ban", "set_group_card",
    "set_group_admin", "set_group_name", "set_group_special_title",
    "set_group_add_option", "set_group_portrait", "send_group_sign",
    "set_essence_msg", "delete_essence_msg",
    "send_group_notice", "delete_group_notice",
    "delete_group_file", "delete_group_folder", "upload_group_file",
    "create_group_file_folder", "move_group_file", "rename_group_file",
    "trans_group_file",
    # 身份与资料
    "set_qq_avatar", "set_qq_profile", "update_qq_status",
    # 高危：代码执行 / 文件外发 / 删除
    "delete_friend", "run_python_code", "delete_workspace_file", "send_file",
    # 对外发消息与定时
    "send_message", "schedule_message", "publish_qzone", "recall_by_reply",
    "send_qq_email", "create_scheduled_command",
    "cancel_scheduled_command", "delete_scheduled_command",
    # 文件传输
    "create_flash_task", "send_flash_msg", "send_online_file",
    "send_online_folder", "receive_online_file", "refuse_online_file",
    "cancel_online_file", "download_fileset",
    # 浏览器（资源占用高，且以机器人身份操作）
    "fetch_url", "open_page",
    "screenshot_page", "close_page", "browser_install",
    "browser_close", "browser_close_tab", "browser_visit",
    "browser_click", "browser_double_click", "browser_right_click",
    "browser_long_press", "browser_drag", "browser_input_at",
    "browser_input", "browser_hover", "browser_favorite_add",
    "browser_favorite_delete", "browser_chat",
}

# ======================================================
# 工具权限档位（4 档）
#   global     —— 全局：所有人都可用
#   groupadmin —— 群主/管理员（仅群聊）：群聊中群主/群管理员可用；
#                 私聊中该档位不生效，自动回退为 admin（超管）
#   admin      —— 超管：仅 AstrBot 全局管理员（admins_id 配置）可用
#   disabled   —— 禁用：任何人都不可用
# ======================================================
PERMISSION_LEVELS = ("global", "groupadmin", "admin", "disabled")

# 档位显示名（用于提示文案）
PERMISSION_LABELS = {
    "global": "全局",
    "groupadmin": "群主/管理员（仅群聊）",
    "admin": "超管",
    "disabled": "禁用",
}


def _normalize_ip_literal(hostname: str) -> Optional[str]:
    """把十进制/八进制/十六进制等非标准 IP 写法归一化成标准点分十进制。

    返回归一化后的 IP 字符串；无法识别为 IP 时返回 None。
    """
    host = hostname.strip().strip("[]")
    if not host:
        return None

    # 纯数字形式：http://2130706433/ == 127.0.0.1
    if host.isdigit():
        try:
            value = int(host, 10)
            if 0 <= value <= 0xFFFFFFFF:
                return str(ipaddress.IPv4Address(value))
        except (ValueError, ipaddress.AddressValueError):
            return None
        return None

    # 十六进制形式：http://0x7f000001/
    if host.lower().startswith("0x"):
        try:
            value = int(host, 16)
            if 0 <= value <= 0xFFFFFFFF:
                return str(ipaddress.IPv4Address(value))
        except (ValueError, ipaddress.AddressValueError):
            return None
        return None

    # 含点的混合进制形式：0177.0.0.1 / 0x7f.1.1.1 / 2130706433
    parts = host.split(".")
    if len(parts) == 4:
        octets = []
        for part in parts:
            try:
                if part.lower().startswith("0x"):
                    octets.append(int(part, 16))
                elif len(part) > 1 and part.startswith("0"):
                    octets.append(int(part, 8))
                else:
                    octets.append(int(part, 10))
            except ValueError:
                return None
        if all(0 <= o <= 255 for o in octets):
            return ".".join(str(o) for o in octets)

    return None


# 需要视觉能力的工具 —— 仅多模态（支持图像输入）的模型可用
# 这些工具的输出依赖截图，纯文本模型无法使用
VISION_REQUIRED_TOOLS = {
    "open_page", "screenshot_page", "close_page",
    "browser_search", "browser_visit", "browser_click",
    "browser_double_click", "browser_right_click",
    "browser_long_press", "browser_drag", "browser_input_at",
    "browser_input", "browser_hover", "browser_scroll", "browser_wait",
    "browser_zoom", "browser_screenshot", "browser_back",
    "browser_forward", "browser_tabs", "browser_close_tab",
    "browser_close", "browser_chat", "browser_favorite_list",
    "browser_favorite_add", "browser_favorite_delete",
}

# 视觉模型名启发式（provider 未声明 modalities 时的兜底判定）
_VISION_MODEL_PATTERNS = (
    "gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-4-vision", "gpt-5", "o3", "o4",
    "chatgpt-4o", "vision", "-vl", "vl-", "qwen-vl", "qvq", "gemini",
    "claude-3", "claude-4", "claude-opus", "claude-sonnet", "claude-haiku",
    "minimax-v", "abab", "doubao-vision", "step-1v", "step-3", "yi-vision",
    "internvl", "minicpm-v", "llama-3.2-vision", "pixtral", "molmo",
    "grok-4", "grok-vision", "mimo-vision", "4v",
)

# GLM 系列视觉版本（glm-4v / glm4v / glm-4.5v / glm-4.6v / glm-5v ...）
_VISION_GLM_RE = re.compile(r"glm[-.]?\d+(?:\.\d+)?v", re.IGNORECASE)


def _model_looks_visual(model_name: str) -> bool:
    """按模型名启发式判断是否为视觉模型。"""
    name = (model_name or "").lower()
    if any(p in name for p in _VISION_MODEL_PATTERNS):
        return True
    return bool(_VISION_GLM_RE.search(name))


def _is_ip_blocked(hostname: str, blocked_ranges: list) -> bool:
    """检查 hostname 是否命中 SSRF 黑名单。支持 CIDR、精确域名、非标准 IP 写法。"""
    # 先尝试归一化非标准写法（十进制/八进制/十六进制/IPv6 映射）
    normalized = _normalize_ip_literal(hostname)
    candidates = [hostname]
    if normalized and normalized != hostname:
        candidates.insert(0, normalized)

    for cand in candidates:
        try:
            ip = ipaddress.ip_address(cand)
        except ValueError:
            continue

        # IPv4-mapped IPv6（::ffff:127.0.0.1）解包成 IPv4 再判
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped

        for cidr in blocked_ranges:
            try:
                if ip in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:
                pass
        # 未配 CIDR 时，也拦私有/回环/链路本地/保留地址
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
        return False

    # 非 IP 字面量：按域名精确/后缀匹配
    hn = hostname.lower().strip(".")
    for entry in blocked_ranges:
        if "/" in entry:
            continue
        entry_l = entry.lower()
        if hn == entry_l or hn.endswith("." + entry_l):
            return True
    return False


async def _resolve_and_check_host(hostname: str, blocked_ranges: list) -> Optional[str]:
    """解析域名的所有 A/AAAA 记录，任一命中黑名单即阻断（防 DNS rebinding）。"""
    if _is_ip_blocked(hostname, blocked_ranges):
        return f"URL 被安全策略阻断: {hostname} 命中 SSRF 黑名单"

    # 非 IP 字面量才需要 DNS 解析
    try:
        ipaddress.ip_address(hostname.strip("[]"))
        return None
    except ValueError:
        pass

    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM),
            timeout=5,
        )
    except Exception:
        return None

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return f"URL 被安全策略阻断: {hostname} 解析到内网地址 {addr}"
        for cidr in blocked_ranges:
            try:
                if ip in ipaddress.ip_network(cidr, strict=False):
                    return f"URL 被安全策略阻断: {hostname} 解析到 {addr} 命中黑名单"
            except ValueError:
                pass
    return None


def _check_ssrf(url: str, blocked_ranges: list) -> Optional[str]:
    """同步版 SSRF 检查（仅做字面量判断，用于快速拦截）。"""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    hostname = parsed.hostname
    if not hostname:
        return None
    if _is_ip_blocked(hostname, blocked_ranges):
        return f"URL 被安全策略阻断: {hostname} 命中 SSRF 黑名单"
    return None


def _safe_error_msg(e: Exception) -> str:
    """返回脱敏的错误信息。"""
    msg = str(e)
    msg = re.sub(r"/[/\\w.-]+\.py", "[internal]", msg)
    msg = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", "[redacted]", msg)
    if len(msg) > 200:
        msg = msg[:200] + "..."
    return msg or "操作失败"


async def _safe_call_action(client, action: str, timeout: float = 30, **params):
    """模块级 NapCat API 调用封装（带超时）。

    供没有 config 访问能力的类（QzoneSession / QQStatusManager /
    ScheduledCommandExecutor）使用，避免 NapCat 无响应时永久挂起。
    """
    return await asyncio.wait_for(
        client.call_action(action, **params), timeout=max(float(timeout), 1.0)
    )


class MemoryManager:
    def __init__(self, data_dir: str, max_memories_per_user: int = 100):
        self.data_dir = data_dir
        self.max_memories_per_user = max_memories_per_user
        self._lock = asyncio.Lock()
        self._file_path = os.path.join(data_dir, "memories.json")
        self._ensure_file()

    def _ensure_file(self):
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self._file_path):
            self._save_data({"memories": []})

    def _load_data(self) -> dict:
        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {"memories": []}

    def _save_data(self, data: dict):
        with open(self._file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _get_timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def _cleanup_if_needed(self, user_id: str):
        if self.max_memories_per_user <= 0:
            return
        data = self._load_data()
        memories = data.get("memories", [])
        user_memories = [m for m in memories if m.get("user_id") == str(user_id)]
        if len(user_memories) > self.max_memories_per_user:
            user_memories.sort(key=lambda x: x.get("updated_at", ""))
            to_delete_ids = [m["id"] for m in user_memories[:len(user_memories) - self.max_memories_per_user]]
            memories = [m for m in memories if m.get("id") not in to_delete_ids]
            self._save_data({"memories": memories})
            logger.info(f"[MemoryManager] 已清理用户 {user_id} 的 {len(to_delete_ids)} 条旧记忆")

    async def add_memory(self, user_id: str, content: str, tags: list = None, importance: int = 5) -> str:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            memory_id = str(uuid.uuid4())[:8]
            new_memory = {
                "id": memory_id,
                "user_id": str(user_id),
                "content": content,
                "tags": tags or [],
                "importance": max(1, min(10, importance)),
                "created_at": self._get_timestamp(),
                "updated_at": self._get_timestamp()
            }
            memories.append(new_memory)
            self._save_data({"memories": memories})
            await self._cleanup_if_needed(user_id)
            logger.info(f"[MemoryManager] 添加记忆成功: {memory_id} 用户: {user_id}")
            return memory_id

    async def update_memory(self, memory_id: str, content: str = None, tags: list = None, importance: int = None) -> bool:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            for memory in memories:
                if memory.get("id") == memory_id:
                    if content is not None:
                        memory["content"] = content
                    if tags is not None:
                        memory["tags"] = tags
                    if importance is not None:
                        memory["importance"] = max(1, min(10, importance))
                    memory["updated_at"] = self._get_timestamp()
                    self._save_data({"memories": memories})
                    return True
            return False

    async def delete_memory(self, memory_id: str) -> bool:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            original_len = len(memories)
            memories = [m for m in memories if m.get("id") != memory_id]
            if len(memories) < original_len:
                self._save_data({"memories": memories})
                return True
            return False

    async def get_memories(self, user_id: str = None, keyword: str = None,
                          limit: int = 10, sort_by: str = "updated_at") -> List[dict]:
        data = self._load_data()
        memories = data.get("memories", [])
        if user_id:
            memories = [m for m in memories if m.get("user_id") == str(user_id)]
        if keyword:
            keyword_lower = keyword.lower()
            filtered = []
            for m in memories:
                if keyword_lower in m.get("content", "").lower() or any(keyword_lower in tag.lower() for tag in m.get("tags", [])):
                    filtered.append(m)
            memories = filtered
        if sort_by == "importance":
            memories.sort(key=lambda x: x.get("importance", 0), reverse=True)
        elif sort_by in ["updated_at", "created_at"]:
            memories.sort(key=lambda x: x.get(sort_by, ""), reverse=True)
        return memories[:limit]

    async def get_memory_by_id(self, memory_id: str) -> Optional[dict]:
        memories = await self.get_memories(limit=10000)
        for m in memories:
            if m.get("id") == memory_id:
                return m
        return None

    async def get_latest_memories_for_inject(self, user_id: str, count: int = 5) -> List[dict]:
        return await self.get_memories(user_id=user_id, limit=count, sort_by="updated_at")

    # ==================== 增强管理能力（v5.3.0） ====================

    async def get_stats(self) -> dict:
        """统计概览：总数、用户数、标签分布、重要度分布。"""
        data = self._load_data()
        memories = data.get("memories", [])
        by_user: Dict[str, int] = {}
        by_tag: Dict[str, int] = {}
        by_importance: Dict[str, int] = {}
        for m in memories:
            uid = str(m.get("user_id", ""))
            by_user[uid] = by_user.get(uid, 0) + 1
            for tag in (m.get("tags") or []):
                by_tag[tag] = by_tag.get(tag, 0) + 1
            imp = int(m.get("importance", 5) or 5)
            key = "高(8-10)" if imp >= 8 else ("中(4-7)" if imp >= 4 else "低(1-3)")
            by_importance[key] = by_importance.get(key, 0) + 1

        top_users = sorted(by_user.items(), key=lambda kv: kv[1], reverse=True)[:20]
        top_tags = sorted(by_tag.items(), key=lambda kv: kv[1], reverse=True)[:20]
        return {
            "total": len(memories),
            "user_count": len(by_user),
            "tag_count": len(by_tag),
            "top_users": [{"user_id": u, "count": c} for u, c in top_users],
            "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
            "by_importance": by_importance,
        }

    async def list_users(self) -> List[dict]:
        """列出所有有记忆的用户及其条数。"""
        data = self._load_data()
        counters: Dict[str, int] = {}
        for m in data.get("memories", []):
            uid = str(m.get("user_id", ""))
            counters[uid] = counters.get(uid, 0) + 1
        return [
            {"user_id": u, "count": c}
            for u, c in sorted(counters.items(), key=lambda kv: kv[1], reverse=True)
        ]

    async def list_tags(self) -> List[dict]:
        """列出所有标签及其出现次数。"""
        data = self._load_data()
        counters: Dict[str, int] = {}
        for m in data.get("memories", []):
            for tag in (m.get("tags") or []):
                counters[tag] = counters.get(tag, 0) + 1
        return [
            {"tag": t, "count": c}
            for t, c in sorted(counters.items(), key=lambda kv: kv[1], reverse=True)
        ]

    async def batch_delete(self, memory_ids: List[str]) -> int:
        """批量删除，返回实际删除条数。"""
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            ids = {str(i) for i in memory_ids}
            remain = [m for m in memories if str(m.get("id")) not in ids]
            deleted = len(memories) - len(remain)
            if deleted:
                self._save_data({"memories": remain})
            return deleted

    async def batch_update_importance(self, memory_ids: List[str], importance: int) -> int:
        """批量调整重要度，返回实际改动条数。"""
        importance = max(1, min(10, int(importance)))
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            ids = {str(i) for i in memory_ids}
            changed = 0
            for m in memories:
                if str(m.get("id")) in ids:
                    m["importance"] = importance
                    m["updated_at"] = self._get_timestamp()
                    changed += 1
            if changed:
                self._save_data({"memories": memories})
            return changed

    async def batch_add_tag(self, memory_ids: List[str], tag: str) -> int:
        """批量追加标签，返回实际改动条数。"""
        tag = (tag or "").strip()
        if not tag:
            return 0
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            ids = {str(i) for i in memory_ids}
            changed = 0
            for m in memories:
                if str(m.get("id")) in ids:
                    tags = list(m.get("tags") or [])
                    if tag not in tags:
                        tags.append(tag)
                        m["tags"] = tags
                        m["updated_at"] = self._get_timestamp()
                        changed += 1
            if changed:
                self._save_data({"memories": memories})
            return changed

    async def find_duplicates(self) -> List[dict]:
        """找出内容完全重复的记忆分组（按 user_id + 内容归一化）。"""
        data = self._load_data()
        groups: Dict[str, List[dict]] = {}
        for m in data.get("memories", []):
            key = f"{m.get('user_id')}::{(m.get('content') or '').strip().lower()}"
            groups.setdefault(key, []).append(m)
        result = []
        for key, items in groups.items():
            if len(items) > 1:
                items.sort(key=lambda x: x.get("created_at", ""))
                result.append({
                    "user_id": items[0].get("user_id"),
                    "content": items[0].get("content"),
                    "count": len(items),
                    "keep_id": items[0].get("id"),
                    "remove_ids": [i.get("id") for i in items[1:]],
                })
        return result

    async def deduplicate(self) -> int:
        """一键去重：同用户下内容重复的记忆只保留最早一条。返回删除条数。"""
        dup_groups = await self.find_duplicates()
        remove_ids = [rid for g in dup_groups for rid in g["remove_ids"]]
        if not remove_ids:
            return 0
        return await self.batch_delete(remove_ids)

    async def export_all(self) -> dict:
        """导出全部记忆（用于备份）。"""
        return self._load_data()

    async def import_merge(self, payload: Any) -> tuple[int, int]:
        """
        导入记忆并合并（按 id 去重）。

        :return: (新增条数, 跳过条数)
        """
        if isinstance(payload, dict):
            incoming = payload.get("memories", [])
        elif isinstance(payload, list):
            incoming = payload
        else:
            incoming = []
        if not isinstance(incoming, list):
            return 0, 0

        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            existing_ids = {str(m.get("id")) for m in memories}
            added = skipped = 0
            for m in incoming:
                if not isinstance(m, dict):
                    skipped += 1
                    continue
                mid = str(m.get("id") or "")
                content_val = str(m.get("content") or "").strip()
                if not mid or not content_val:
                    skipped += 1
                    continue
                if mid in existing_ids:
                    skipped += 1
                    continue
                memories.append({
                    "id": mid,
                    "user_id": str(m.get("user_id", "")),
                    "content": content_val,
                    "tags": list(m.get("tags") or []),
                    "importance": max(1, min(10, int(m.get("importance", 5) or 5))),
                    "created_at": m.get("created_at") or self._get_timestamp(),
                    "updated_at": m.get("updated_at") or self._get_timestamp(),
                })
                existing_ids.add(mid)
                added += 1
            if added:
                self._save_data({"memories": memories})
            return added, skipped


class DatabaseManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.db_path = os.path.join(data_dir, "commands_db.json")
        self.status_path = os.path.join(data_dir, "status.json")
        self.scheduled_path = os.path.join(data_dir, "scheduled_messages.json")
        self._lock = asyncio.Lock()
        self._init_storage()

    def _init_storage(self):
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.db_path):
            self._save_json(self.db_path, {"scheduled_commands": [], "version": "1.0"})
        if not os.path.exists(self.status_path):
            self._save_json(self.status_path, {"current_status": "online", "status_name": "在线"})
        if not os.path.exists(self.scheduled_path):
            self._save_json(self.scheduled_path, {"tasks": []})

    def _load_json(self, filepath: str, default: Any = None) -> Any:
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"[DatabaseManager] 读取文件失败: {e}")
        return default

    def _save_json(self, filepath: str, data: Any) -> bool:
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath + ".tmp", 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(filepath + ".tmp", filepath)
            return True
        except Exception as e:
            logger.error(f"[DatabaseManager] 保存文件失败: {e}")
            return False

    async def save_scheduled_command(self, task_id: str, command_type: str,
                                     params: dict, execute_time: datetime,
                                     recurrence: str = "once",
                                     session_info: dict = None) -> bool:
        async with self._lock:
            db_data = self._load_json(self.db_path, {"scheduled_commands": []})
            commands = db_data.get("scheduled_commands", [])
            record = {
                "id": task_id,
                "command_type": command_type,
                "params": json.dumps(params, ensure_ascii=False),
                "execute_time": execute_time.isoformat(),
                "created_at": datetime.now().isoformat(),
                "executed": 0,
                "recurrence": recurrence,
                "session_info": session_info
            }
            for i, cmd in enumerate(commands):
                if isinstance(cmd, dict) and cmd.get('id') == task_id:
                    commands[i] = record
                    break
            else:
                commands.append(record)
            db_data["scheduled_commands"] = commands
            return self._save_json(self.db_path, db_data)

    async def get_pending_commands(self) -> List[dict]:
        db_data = self._load_json(self.db_path, {"scheduled_commands": []})
        commands = db_data.get("scheduled_commands", [])
        return [cmd for cmd in commands if isinstance(cmd, dict) and cmd.get("executed") == 0]

    async def get_all_commands(self, include_executed: bool = False) -> List[dict]:
        db_data = self._load_json(self.db_path, {"scheduled_commands": []})
        commands = db_data.get("scheduled_commands", [])
        if include_executed:
            return commands
        return await self.get_pending_commands()

    async def mark_command_executed(self, task_id: str, executed: int = 1):
        async with self._lock:
            db_data = self._load_json(self.db_path, {"scheduled_commands": []})
            commands = db_data.get("scheduled_commands", [])
            for cmd in commands:
                if isinstance(cmd, dict) and cmd.get('id') == task_id:
                    cmd['executed'] = executed
                    cmd['completed_at'] = datetime.now().isoformat()
                    break
            db_data["scheduled_commands"] = commands
            self._save_json(self.db_path, db_data)

    async def delete_command(self, task_id: str):
        async with self._lock:
            db_data = self._load_json(self.db_path, {"scheduled_commands": []})
            commands = db_data.get("scheduled_commands", [])
            commands = [cmd for cmd in commands if isinstance(cmd, dict) and cmd.get('id') != task_id]
            db_data["scheduled_commands"] = commands
            self._save_json(self.db_path, db_data)

    async def cancel_command(self, task_id: str):
        await self.mark_command_executed(task_id, 2)

    async def save_status(self, status_key: str, status_name: str, end_time: Optional[datetime]):
        async with self._lock:
            record = {
                "current_status": status_key,
                "status_name": status_name,
                "end_time": end_time.isoformat() if end_time else "",
                "updated_at": datetime.now().isoformat()
            }
            self._save_json(self.status_path, record)

    # ---------- 定时消息持久化 ----------

    async def save_scheduled_task(self, task: dict):
        """保存/更新一条定时消息（按 task_id 去重）。"""
        async with self._lock:
            db_data = self._load_json(self.scheduled_path, {"tasks": []})
            tasks = db_data.get("tasks", [])
            for i, t in enumerate(tasks):
                if isinstance(t, dict) and t.get("task_id") == task.get("task_id"):
                    tasks[i] = task
                    break
            else:
                tasks.append(task)
            db_data["tasks"] = tasks
            self._save_json(self.scheduled_path, db_data)

    async def delete_scheduled_task(self, task_id: str):
        async with self._lock:
            db_data = self._load_json(self.scheduled_path, {"tasks": []})
            tasks = [t for t in db_data.get("tasks", [])
                     if isinstance(t, dict) and t.get("task_id") != task_id]
            db_data["tasks"] = tasks
            self._save_json(self.scheduled_path, db_data)

    async def load_scheduled_tasks(self) -> List[dict]:
        """读取所有未完成、未取消的定时消息。"""
        db_data = self._load_json(self.scheduled_path, {"tasks": []})
        tasks = db_data.get("tasks", [])
        return [t for t in tasks if isinstance(t, dict)
                and not t.get("completed") and not t.get("cancelled")]

    async def load_status(self) -> Optional[dict]:
        return self._load_json(self.status_path)

    async def clear_status(self):
        await self.save_status("online", "在线", None)


class QzoneSession:
    def __init__(self):
        self.uin: str = ""
        self.cookie: str = ""
        self.gtk: str = ""
        self.client = None
        self.initialized = False

    def _cookie_to_dict(self, cookie_str: str) -> dict:
        cookie_dict = {}
        for item in cookie_str.split(';'):
            item = item.strip()
            if '=' in item:
                key, value = item.split('=', 1)
                cookie_dict[key] = value
        return cookie_dict

    def _calc_gtk(self, skey: str) -> str:
        hash_val = 5381
        for char in skey:
            hash_val += (hash_val << 5) + ord(char)
        return str(hash_val & 0x7fffffff)

    async def initialize(self, client) -> bool:
        try:
            self.client = client
            login_info = await _safe_call_action(client, 'get_login_info')
            self.uin = str(login_info.get('user_id', ''))
            if not self.uin:
                return False
            try:
                creds = await _safe_call_action(client, 'get_credentials', domain='qzone.qq.com')
                self.cookie = creds.get('cookies', '')
            except Exception:
                try:
                    cookies = await _safe_call_action(client, 'get_cookies', domain='qzone.qq.com')
                    self.cookie = cookies.get('cookies', '')
                except:
                    return False
            if not self.cookie:
                return False
            cookie_dict = self._cookie_to_dict(self.cookie)
            key = cookie_dict.get('p_skey') or cookie_dict.get('skey')
            if not key:
                return False
            self.gtk = self._calc_gtk(key)
            self.initialized = True
            logger.info(f"[QzoneSession] 初始化成功")
            return True
        except Exception as e:
            logger.error(f"[QzoneSession] 初始化失败: {e}")
            return False

    async def ensure_initialized(self, client) -> bool:
        if self.initialized:
            return True
        return await self.initialize(client)


class QzoneAPI:
    def __init__(self, session: QzoneSession):
        self.session = session

    async def publish_post(self, text: str, images: list = None) -> dict:
        images = images or []
        if not self.session.initialized:
            return {"success": False, "msg": "会话未初始化"}
        try:
            url = f"https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6?g_tk={self.session.gtk}"
            payload = {
                'syn_tweet_verson': '1',
                'con': text,
                'feedversion': '1',
                'ver': '1',
                'ugc_right': '1',
                'to_sign': '0',
                'hostuin': self.session.uin,
                'code_version': '1',
                'format': 'fs',
                'qzreferrer': f'https://user.qzone.qq.com/{self.session.uin}/infocenter',
            }
            encoded_data = urlencode(payload)
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Cookie': self.session.cookie,
                'Origin': 'https://user.qzone.qq.com',
                'Referer': f'https://user.qzone.qq.com/{self.session.uin}/infocenter',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, data=encoded_data, headers=headers, timeout=30) as resp:
                    response_text = await resp.text()
                    if '"code":0' in response_text or '"code": 0' in response_text:
                        return {"success": True, "msg": "发表成功"}
                    return {"success": False, "msg": f"响应: {response_text[:200]}"}
        except Exception as e:
            return {"success": False, "msg": _safe_error_msg(e)}


class ScheduledTask:
    def __init__(self, task_id: str, target_id: str, message: str, send_time: datetime,
                 chat_type: str, target_name: str = ""):
        self.task_id = task_id
        self.target_id = target_id
        self.message = message
        self.send_time = send_time
        self.chat_type = chat_type
        self.target_name = target_name
        self.cancelled = False
        self.completed = False

    def to_dict(self) -> dict:
        """序列化为可持久化的字典。"""
        return {
            "task_id": self.task_id,
            "target_id": self.target_id,
            "message": self.message,
            "send_time": self.send_time.isoformat(),
            "chat_type": self.chat_type,
            "target_name": self.target_name,
            "cancelled": self.cancelled,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduledTask":
        """从持久化字典恢复。send_time 解析失败时抛 ValueError。"""
        send_time = datetime.fromisoformat(str(d.get("send_time", "")))
        task = cls(
            task_id=str(d.get("task_id", "")),
            target_id=str(d.get("target_id", "")),
            message=str(d.get("message", "")),
            send_time=send_time,
            chat_type=str(d.get("chat_type", "group")),
            target_name=str(d.get("target_name", "")),
        )
        task.cancelled = bool(d.get("cancelled", False))
        task.completed = bool(d.get("completed", False))
        return task


class QQStatusManager:
    def __init__(self):
        self.current_status: Optional[str] = "online"
        self.current_status_name: str = "在线"
        self.status_end_time: Optional[datetime] = None
        self.restore_task: Optional[asyncio.Task] = None
        self.pending_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self.db_manager: Optional[DatabaseManager] = None

    def set_db_manager(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    async def restore_from_db(self, client):
        if not self.db_manager:
            return
        try:
            record = await self.db_manager.load_status()
            if not record:
                return
            status_key = record.get('current_status', 'online')
            end_time_str = record.get('end_time', '')
            if status_key == 'online' or not end_time_str:
                return
            end_time = datetime.fromisoformat(end_time_str)
            now = datetime.now()
            if end_time <= now:
                await self._force_set_online(client)
            else:
                status_info = self.get_status_info(status_key)
                if status_info:
                    self.current_status = status_key
                    self.current_status_name = status_info['name']
                    self.status_end_time = end_time
                    remain_minutes = (end_time - now).total_seconds() / 60
                    self.restore_task = asyncio.create_task(
                        self._auto_restore_online(client, int(remain_minutes))
                    )
        except Exception as e:
            logger.error(f"[QQStatusManager] 恢复状态失败: {e}")

    async def _force_set_online(self, client):
        try:
            params = {"status": 10, "ext_status": 0, "battery_status": 0}
            await _safe_call_action(client, 'set_online_status', **params)
            self.current_status = "online"
            self.current_status_name = "在线"
            self.status_end_time = None
            if self.db_manager:
                await self.db_manager.clear_status()
        except Exception as e:
            logger.error(f"[QQStatusManager] 强制恢复在线失败: {e}")

    def get_status_info(self, status_key: str) -> Optional[dict]:
        BASIC_STATUS = {
            "online": {"name": "在线", "status": 10, "ext": 0},
            "qme": {"name": "Q我吧", "status": 60, "ext": 0},
            "away": {"name": "离开", "status": 30, "ext": 0},
            "busy": {"name": "忙碌", "status": 50, "ext": 0},
            "dnd": {"name": "请勿打扰", "status": 70, "ext": 0},
            "invisible": {"name": "隐身", "status": 40, "ext": 0},
        }
        FUN_STATUS = {
            "listening": {"name": "听歌中", "status": 10, "ext": 1028},
            "sleeping": {"name": "睡觉中", "status": 10, "ext": 1016},
            "studying": {"name": "学习中", "status": 10, "ext": 1018},
        }
        if status_key in BASIC_STATUS:
            return BASIC_STATUS[status_key]
        return FUN_STATUS.get(status_key)

    def get_current_status_desc(self) -> str:
        if self.current_status == "online":
            return "当前状态：在线"
        now = datetime.now()
        if self.status_end_time and self.status_end_time > now:
            remain = self.status_end_time - now
            remain_min = remain.seconds // 60 + remain.days * 1440
            return f"当前状态：{self.current_status_name}（还剩约{remain_min}分钟）"
        return f"当前状态：{self.current_status_name}"

    def is_status_active(self) -> bool:
        if self.current_status == "online":
            return False
        if self.status_end_time and datetime.now() < self.status_end_time:
            return True
        return False

    async def set_status(self, client, status_key: str, duration_minutes: int, delay_minutes: int = 0) -> dict:
        async with self._lock:
            status_info = self.get_status_info(status_key)
            if not status_info:
                return {"success": False, "msg": f"无效的状态码: {status_key}"}
            if delay_minutes <= 0:
                return await self._execute_set_status(client, status_key, duration_minutes)
            else:
                async def delayed_task():
                    await asyncio.sleep(delay_minutes * 60)
                    await self._execute_set_status(client, status_key, duration_minutes)
                self.pending_task = asyncio.create_task(delayed_task())
                return {"success": True, "msg": f"已设置定时状态：{delay_minutes}分钟后切换", "is_pending": True}

    async def _execute_set_status(self, client, status_key: str, duration_minutes: int) -> dict:
        status_info = self.get_status_info(status_key)
        try:
            params = {"status": status_info["status"], "ext_status": status_info["ext"], "battery_status": 0}
            await _safe_call_action(client, 'set_online_status', **params)
            if status_key == "online":
                if self.restore_task and not self.restore_task.done():
                    self.restore_task.cancel()
                self.current_status = "online"
                self.current_status_name = "在线"
                self.status_end_time = None
                if self.db_manager:
                    await self.db_manager.clear_status()
                return {"success": True, "msg": "状态已恢复为「在线」", "is_online": True}
            self.current_status = status_key
            self.current_status_name = status_info['name']
            self.status_end_time = datetime.now() + timedelta(minutes=duration_minutes)
            if self.db_manager:
                await self.db_manager.save_status(status_key, status_info['name'], self.status_end_time)
            self.restore_task = asyncio.create_task(self._auto_restore_online(client, duration_minutes))
            return {
                "success": True,
                "msg": f"状态已设置为「{status_info['name']}」，持续{duration_minutes}分钟",
                "end_time": self.status_end_time.strftime("%H:%M:%S")
            }
        except Exception as e:
            return {"success": False, "msg": f"设置失败: {_safe_error_msg(e)}"}

    async def _auto_restore_online(self, client, delay_minutes: int):
        try:
            await asyncio.sleep(delay_minutes * 60)
            if not client:
                return
            params = {"status": 10, "ext_status": 0, "battery_status": 0}
            await _safe_call_action(client, 'set_online_status', **params)
            async with self._lock:
                self.current_status = "online"
                self.current_status_name = "在线"
                self.status_end_time = None
                self.restore_task = None
                if self.db_manager:
                    await self.db_manager.clear_status()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[QQStatusManager] 自动恢复在线失败: {e}")


class ScheduledCommandExecutor:
    def __init__(self, plugin: 'Main'):
        self.plugin = plugin
        self.db_manager = plugin.db_manager
        self.running_tasks: Dict[str, asyncio.Task] = {}
        self._stop_check = False

    async def start_periodic_check(self):
        self._stop_check = False
        while not self._stop_check:
            await asyncio.sleep(60)
            if not self._stop_check:
                try:
                    await self._check_and_execute_pending()
                except Exception as e:
                    logger.error(f"[ScheduledCommandExecutor] 定时检查失败: {e}")

    def stop_periodic_check(self):
        self._stop_check = True

    def cancel_task(self, task_id: str):
        if task_id in self.running_tasks:
            task = self.running_tasks[task_id]
            if not task.done():
                task.cancel()
            del self.running_tasks[task_id]

    async def _check_and_execute_pending(self):
        pending = await self.db_manager.get_pending_commands()
        now = datetime.now()
        for cmd in pending:
            task_id = cmd.get('id')
            if task_id in self.running_tasks:
                continue
            try:
                execute_time_str = cmd.get('execute_time', '')
                if not execute_time_str:
                    continue
                execute_time = datetime.fromisoformat(execute_time_str)
                if (now - execute_time).total_seconds() >= 0:
                    await self.db_manager.mark_command_executed(task_id, 2)
                    task = asyncio.create_task(
                        self._execute_command(cmd['id'], cmd['command_type'], json.loads(cmd['params']), cmd.get('session_info'))
                    )
                    self.running_tasks[task_id] = task
            except Exception as e:
                logger.error(f"[ScheduledCommandExecutor] 处理指令失败: {e}")

    async def _execute_command(self, task_id: str, command_type: str, params: dict, session_info: dict = None):
        try:
            client = self.plugin._client
            if not client:
                return
            if command_type == "qzone_post":
                content = params.get("content", "")
                if content:
                    success = await self.plugin.session.initialize(client)
                    if success:
                        await self.plugin.qzone.publish_post(content)
            elif command_type == "status_change":
                status = params.get("status", "online")
                duration = params.get("duration_minutes", 30)
                await self.plugin.status_manager.set_status(client, status, duration, 0)
            elif command_type == "send_message":
                target_id = params.get("target_id", "")
                message = params.get("message", "")
                chat_type = params.get("chat_type", "group")
                if target_id and message:
                    if chat_type == "group":
                        await _safe_call_action(client, 'send_group_msg', group_id=int(target_id), message=message)
                    else:
                        await _safe_call_action(client, 'send_private_msg', user_id=int(target_id), message=message)
            elif command_type == "llm_remind":
                prompt = params.get("prompt", "")
                if prompt and session_info:
                    unified_msg_origin = session_info.get('unified_msg_origin')
                    if unified_msg_origin:
                        remind_message = f"[定时提醒 #{task_id}]\n{prompt}"
                        await self.plugin.context.send_message(unified_msg_origin, MessageChain().message(remind_message))
            await self.db_manager.mark_command_executed(task_id, 1)
        except Exception as e:
            logger.error(f"[ScheduledCommandExecutor] 执行任务失败: {e}")
            await self.db_manager.mark_command_executed(task_id, -1)
        finally:
            if task_id in self.running_tasks:
                del self.running_tasks[task_id]


class EmailSender:
    def __init__(self, config: AstrBotConfig):
        self.config = config

    def _get_smtp_settings(self) -> Tuple[str, int, str, str]:
        sender = self.config.get("email_sender", "").strip()
        auth_code = self.config.get("email_authorization_code", "").strip()
        server = self.config.get("email_smtp_server", "smtp.qq.com").strip()
        port = self.config.get("email_smtp_port", 465)
        return server, port, sender, auth_code

    async def send_email(self, to_email: str, subject: str, content: str, sender_nickname: str = "") -> dict:
        server, port, sender, auth_code = self._get_smtp_settings()
        if not sender or not auth_code:
            return {"success": False, "msg": "发件人邮箱或授权码未配置"}
        to_email = to_email.strip()
        if not to_email:
            return {"success": False, "msg": "收件人邮箱不能为空"}
        try:
            msg = MIMEText(content, "plain", "utf-8")
            from_addr = formataddr((sender_nickname, sender), charset="utf-8")
            msg["From"] = from_addr
            msg["To"] = formataddr(("", to_email), charset="utf-8")
            msg["Subject"] = Header(subject or "来自AstrBot的邮件", "utf-8")
            loop = asyncio.get_running_loop()
            def send_sync():
                with smtplib.SMTP_SSL(server, port, timeout=15) as smtp:
                    smtp.login(sender, auth_code)
                    smtp.sendmail(sender, [to_email], msg.as_string())
            await loop.run_in_executor(None, send_sync)
            return {"success": True, "msg": f"邮件已发送至 {to_email}"}
        except smtplib.SMTPAuthenticationError:
            return {"success": False, "msg": "登录失败：请检查邮箱地址和授权码"}
        except Exception as e:
            return {"success": False, "msg": f"发送失败: {_safe_error_msg(e)}"}


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.context = context
        try:
            self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except RuntimeError:
            self.data_dir = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
        os.makedirs(self.data_dir, exist_ok=True)

        self.memory_manager = MemoryManager(self.data_dir, self.config.get("max_memories_per_user", 100))
        self.email_sender = EmailSender(self.config)
        self.db_manager = DatabaseManager(self.data_dir)
        self.session = QzoneSession()
        self.qzone = QzoneAPI(self.session)
        self._client = None
        self.scheduled_tasks: Dict[str, ScheduledTask] = {}
        self.running_tasks: Dict[str, asyncio.Task] = {}
        self._groups_cache: List[dict] = []
        self._friends_cache: List[dict] = []
        self._cache_time = 0
        self._cache_expire = 300
        self._cache_lock = asyncio.Lock()
        self.status_manager = QQStatusManager()
        self.command_executor: Optional[ScheduledCommandExecutor] = None
        self._restored = False
        self._bg_tasks: Dict[str, asyncio.Task] = {}
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_lock = asyncio.Lock()
        self.ai_default_character = self.config.get("ai_voice_default_character", "")
        self.ai_voice_max_length = self.config.get("ai_voice_max_text_length", 500)
        self._ai_characters_cache: Dict[str, Tuple[float, list]] = {}
        # 群成员角色缓存: "group:user" -> (timestamp, role)
        self._role_cache: Dict[str, Tuple[float, str]] = {}
        self.auto_input_status_enabled = self.config.get("auto_input_status_enabled", False)
        self.auto_input_status_timeout = self.config.get("auto_input_status_timeout", 10)
        self.tool_enabled = self._load_tool_enabled_flags()
        self._tool_registry = self._build_tool_registry()
        # 工具权限表（独立存储，避免被 AstrBot 配置完整性检查清空）
        self._tool_permissions = self._load_tool_permissions()
        # 所有工具的 enable_* 开关集合（用于 WebUI 保存白名单，避免开关被丢弃）
        self._tool_enable_keys = {f"enable_{name}" for name in self._tool_registry}
        self.enable_human_typing = self.config.get("enable_human_typing", False)
        self.typing_idle_threshold = self.config.get("typing_idle_threshold", 900)
        self.typing_initial_delay_min = self.config.get("typing_initial_delay_min", 5)
        self.typing_initial_delay_max = self.config.get("typing_initial_delay_max", 15)
        self._user_last_active: Dict[str, float] = {}
        self._typing_lock = asyncio.Lock()
        # 工作区配置
        # 注意：工作区必须位于 data_dir（持久化数据目录），不能写在插件安装目录内，
        # 否则用户文件会污染包体、影响插件升级与审计（AstrBot 插件市场上架要求）
        self.workspace_enabled = self.config.get("workspace_enabled", True)
        self.workspace_dir = os.path.join(self.data_dir, "workspace")
        self.workspace_banned_patterns = self.config.get("workspace_banned_patterns", [])
        # 先迁移旧数据（此时目标目录尚未创建，迁移判断才生效），再确保目录存在
        self._migrate_legacy_dirs()
        os.makedirs(self.workspace_dir, exist_ok=True)
        
        # 闪传中转目录配置
        # 用于在 AstrBot 和 NapCat 容器之间共享文件
        self.flash_transfer_dir = self.config.get("flash_transfer_dir", "/tmp/astrbot_flash")
        
        # 安全配置
        self.ssrf_blocked_urls = self.config.get("ssrf_blocked_urls", [])
        self.ssrf_custom_blocked_ranges = self.config.get("ssrf_custom_blocked_ranges", [])
        self.resolve_image_restricted = self.config.get("resolve_image_restricted", True)
        self.run_python_sandbox_enabled = self.config.get("run_python_sandbox_enabled", False)
        self.docker_container_name = self.config.get("docker_container_name", "napcat")
        
        # 浏览器管理（统一使用 browser_supervisor）
        # 高级浏览器管理（来自 astrbot_plugin_browser）
        self.browser_supervisor = None
        self.fav_mgr = None
        self.overlay = None
        self.action_overlay = None
        # 浏览器操作展示（v5.3.0）
        #  - browser_always_show_action：每次浏览器操作后强制把截图发给用户（纯代码，不依赖提示词）
        #  - browser_action_overlay_enabled：在截图上叠加操作图标，标出 AI 点了哪里
        self.browser_always_show_action = self.config.get("browser_always_show_action", False)
        self.browser_action_overlay_enabled = self.config.get("browser_action_overlay_enabled", False)
        self.browser_action_push_text = self.config.get("browser_action_push_text", True)
        # 浏览器接管（v5.4.0）：AI 可把操作权临时交给 WebUI 上的真人用户
        self.takeover_manager = TakeoverManager(self.config)

    def _load_tool_enabled_flags(self) -> Dict[str, bool]:
        default_enabled = {
            "add_memory": True, "search_memories": True, "update_memory": True, "delete_memory": True,
            "get_memory_detail": True, "send_message": True, "schedule_message": True,
            "cancel_scheduled_message": True, "list_scheduled_messages": True, "publish_qzone": True,
            "send_poke": True, "update_qq_status": True, "get_qq_status": True, "get_fun_status_list": True,
            "create_scheduled_command": True, "list_scheduled_commands": True, "cancel_scheduled_command": True,
            "delete_scheduled_command": True, "recall_by_reply": True, "send_qq_email": True,
            "get_user_group_role": True, "set_essence_msg": True, "delete_essence_msg": True,
            "set_group_ban": True, "set_group_kick": True, "set_group_whole_ban": True, "set_group_card": True,
            "send_group_notice": True, "delete_group_notice": True, "list_group_files": True,
            "delete_group_file": True, "get_group_members_info": True, "set_group_admin": True,
            "set_group_name": True, "get_group_notice_list": True, "upload_group_file": True,
            "create_group_file_folder": True, "delete_group_folder": True, "get_group_honor_info": True,
            "get_group_at_all_remain": True, "set_group_special_title": True, "get_group_shut_list": True,
            "get_group_ignore_add_request": True, "set_group_add_option": True, "send_group_sign": True,
            "set_qq_avatar": True, "move_group_file": True, "rename_group_file": True, "trans_group_file": True,
            "send_like": True, "get_group_msg_history": True, "get_friend_msg_history": True,
            "set_group_portrait": True, "fetch_custom_face": True, "set_input_status": True,
            "get_ai_characters": True, "send_ai_voice": True, "search_contacts": True, "list_contacts": True,
            "set_qq_profile": True,
            "create_flash_task": True, "get_flash_file_list": True,
            "get_flash_file_url": True, "send_flash_msg": True, "get_share_link": True,
            "get_fileset_info": True, "get_fileset_id": True, "download_fileset": True,
            "get_online_file_msg": True, "send_online_file": True, "send_online_folder": True,
            "receive_online_file": True, "refuse_online_file": True, "cancel_online_file": True,
            "delete_friend": True,
            "run_python_code": True, "list_workspace_files": True,
            "read_workspace_file": True, "delete_workspace_file": True,
            "read_image": True, "send_file": True,
            "fetch_url": True,
            "open_page": True,
            "screenshot_page": True, "close_page": True,
            # 高级浏览器工具（坐标交互体系）
            "browser_search": True, "browser_visit": True, "browser_click": True,
            "browser_double_click": True, "browser_right_click": True,
            "browser_long_press": True, "browser_drag": True,
            "browser_input_at": True, "browser_input": True,
            "browser_scroll": True, "browser_hover": True, "browser_wait": True,
            "browser_zoom": True, "browser_screenshot": True, "browser_back": True,
            "browser_forward": True, "browser_tabs": True, "browser_close_tab": True,
            "browser_close": True, "browser_chat": True,
            "browser_favorite_list": True, "browser_favorite_add": True, "browser_favorite_delete": True,
            "browser_install": True,
        }
        for tool_name in default_enabled.keys():
            config_key = f"enable_{tool_name}"
            if config_key in self.config:
                default_enabled[tool_name] = self.config.get(config_key)
        return default_enabled

    def _get_tool_permission(self, name: str) -> str:
        """取得工具的有效权限档位：global / groupadmin / admin / disabled。

        优先级：tool_permissions 显式配置 > SENSITIVE_TOOLS 默认 admin > global

        注意：权限表独立存储于 tool_permissions.json（见 _load_tool_permissions），
        不放在插件主配置里 —— AstrBot 的 check_config_integrity 会把 schema 中
        `items` 为空的 object 类型的子键全部当作"未知配置"删除，导致保存在主配置
        里的 tool_permissions 在重载/重启后被清空（issue #13）。
        """
        tool_perms = self._tool_permissions if isinstance(self._tool_permissions, dict) else {}
        if name in tool_perms:
            perm = tool_perms.get(name)
            if perm in PERMISSION_LEVELS:
                return perm
        if name in SENSITIVE_TOOLS:
            return "admin"
        return "global"

    def _permissions_file(self) -> str:
        return os.path.join(self.data_dir, "tool_permissions.json")

    def _migrate_legacy_dirs(self) -> None:
        """把旧版本误写在插件安装目录内的持久化数据迁移到 data_dir。

        历史版本曾把 workspace/ fonts/ favorite.json 写在插件包内，
        升级到新版本后这些数据需要搬走，否则用户会"丢失"文件
        （AstrBot 插件市场要求持久化数据必须位于 data_dir）。
        """
        package_dir = os.path.dirname(os.path.abspath(__file__))
        if os.path.realpath(package_dir) == os.path.realpath(self.data_dir):
            return  # 极端情况下两者相同，无需迁移

        # (旧路径, 新路径) —— 仅迁移"文件/目录存在 且 目标不存在"的项
        pairs = [
            (os.path.join(package_dir, "workspace"), self.workspace_dir),
            (os.path.join(package_dir, "fonts"), os.path.join(self.data_dir, "fonts")),
        ]
        for old, new in pairs:
            try:
                if not os.path.exists(old) or os.path.exists(new):
                    continue
                import shutil

                shutil.move(old, new)
                logger.info(f"[QZoneTools] 已迁移旧数据目录: {old} -> {new}")
            except Exception as e:
                logger.warning(f"[QZoneTools] 迁移 {old} 失败（可手动处理）: {_safe_error_msg(e)}")

        # favorite.json → data_dir/favorites.json
        old_fav = os.path.join(package_dir, "favorite.json")
        new_fav = os.path.join(self.data_dir, "favorites.json")
        try:
            if os.path.exists(old_fav) and not os.path.exists(new_fav):
                import shutil

                shutil.move(old_fav, new_fav)
                logger.info(f"[QZoneTools] 已迁移收藏夹: {old_fav} -> {new_fav}")
        except Exception as e:
            logger.warning(f"[QZoneTools] 迁移收藏夹失败（可手动处理）: {_safe_error_msg(e)}")

    def _load_tool_permissions(self) -> Dict[str, str]:
        """从独立文件载入权限表；兼容读取旧版主配置里的 tool_permissions。"""
        # 1) 独立文件（权威）
        path = self._permissions_file()
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {k: v for k, v in data.items() if v in PERMISSION_LEVELS}
                logger.warning("[QZoneTools] tool_permissions.json 格式异常，忽略")
            except Exception as e:
                logger.error(f"[QZoneTools] 读取 tool_permissions.json 失败: {_safe_error_msg(e)}")
        # 2) 旧版主配置迁移（保存一次后即迁移完成）
        legacy = self.config.get("tool_permissions", {})
        if isinstance(legacy, dict) and legacy:
            cleaned = {k: v for k, v in legacy.items() if v in PERMISSION_LEVELS}
            if cleaned:
                logger.info(f"[QZoneTools] 从主配置迁移 {len(cleaned)} 条权限设置到 tool_permissions.json")
                self._save_tool_permissions(cleaned)
                return cleaned
        return {}

    def _save_tool_permissions(self, perms: Dict[str, str]) -> bool:
        """把权限表原子写入独立文件。"""
        path = self._permissions_file()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(perms, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        except Exception as e:
            logger.error(f"[QZoneTools] 写入 tool_permissions.json 失败: {_safe_error_msg(e)}")
            return False

    def _get_available_tools(self, event: AstrMessageEvent = None) -> Dict[str, dict]:
        if not self.config.get("enabled", True):
            return {}
        # 同步判定：超管看 admins_id，群主/群管理看入站消息自带群信息
        is_admin = bool(event is not None and self._event_is_admin(event))
        is_group_admin = bool(event is not None and self._event_is_group_admin(event))
        available = {}
        for name, meta in self._tool_registry.items():
            if not self.tool_enabled.get(name, True):
                continue
            perm = self._get_tool_permission(name)
            if perm == "disabled":
                continue
            # admin 档：仅超管可见（避免 LLM 看到后用不了，浪费上下文）
            if perm == "admin" and not is_admin:
                continue
            # groupadmin 档：群聊中群主/群管理可见；私聊回退为 admin（仅超管）
            if perm == "groupadmin" and not (is_group_admin or is_admin):
                continue
            available[name] = meta
        return available

    def _event_is_admin(self, event: AstrMessageEvent) -> bool:
        """判断事件发送者是否为「超管」—— 仅看 AstrBot 全局管理员（admins_id）。

        群主/群管理不再算超管，而是独立的 groupadmin 档（见 _event_is_group_admin）。
        """
        try:
            if event.is_admin():
                return True
        except Exception:
            pass

        sender_id = ""
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            pass
        if not sender_id:
            return False

        # 兜底：直接比对 AstrBot 配置中的 admins_id
        try:
            admins = self.context.get_config().get("admins_id", []) or []
            if sender_id in {str(a) for a in admins}:
                return True
        except Exception:
            pass
        return False

    def _event_is_group_admin(self, event: AstrMessageEvent) -> bool:
        """判断事件发送者是否为本群群主/群管理（仅群聊有意义）。

        群主/群管理员判定完全依赖群信息：
        - 优先复用入站消息自带的 message_obj.group
        - 缓存不可用时由 _event_is_group_admin_async 实时查询
        """
        group_id = ""
        try:
            group_id = event.get_group_id() or ""
        except Exception:
            pass
        if not group_id:
            return False

        sender_id = ""
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            pass
        if not sender_id:
            return False

        try:
            group = getattr(event.message_obj, "group", None)
            if group is not None:
                owner = getattr(group, "group_owner", None)
                admins = getattr(group, "group_admins", None) or []
                if owner and str(owner) == sender_id:
                    return True
                if sender_id in {str(a) for a in admins}:
                    return True
        except Exception:
            pass
        return False

    async def _event_is_group_admin_async(self, event: AstrMessageEvent) -> bool:
        """异步版群主/群管理判定（必要时实时查询群成员角色）。"""
        if self._event_is_group_admin(event):
            return True
        try:
            group_id = event.get_group_id() or ""
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            return False
        if not group_id or not sender_id:
            return False
        role = await self._get_group_member_role(group_id, sender_id)
        return role in ("群主", "管理员")

    async def _check_tool_permission_async(self, event: AstrMessageEvent, perm: str) -> bool:
        """按档位校验调用者是否有权使用该工具（权限判定的唯一入口）。

        - global     → 放行
        - groupadmin → 群聊中群主/群管理放行；**私聊自动回退为 admin**（仅超管）
        - admin      → 仅超管放行
        - disabled   → 拒绝
        """
        if perm == "global":
            return True
        if perm == "disabled":
            return False
        # groupadmin：私聊不生效，回退为 admin
        if perm == "groupadmin" and await self._event_is_group_admin_async(event):
            return True
        return await self._event_is_admin_async(event)

    async def _event_is_admin_async(self, event: AstrMessageEvent) -> bool:
        """异步版超管判定。"""
        return self._event_is_admin(event)

    # ==================== 视觉模型门禁 ====================

    async def _require_vision_model(self, event: AstrMessageEvent) -> Optional[str]:
        """检查当前会话模型是否支持视觉（图像输入）。

        返回 None = 允许；返回字符串 = 拒绝原因。
        判定顺序：
        1. provider 配置的 modalities 含 "image" → 允许
        2. modalities 已配置但不含 "image" → 拒绝
        3. modalities 未配置 → 模型名启发式判定
        """
        if not self.config.get("browser_vision_gate_enabled", True):
            return None
        try:
            umo = getattr(event, "unified_msg_origin", None)
            prov = None
            if hasattr(self.context, "get_using_provider_async"):
                prov = await self.context.get_using_provider_async(umo=umo)
            elif hasattr(self.context, "get_using_provider"):
                prov = self.context.get_using_provider(umo=umo)
            if prov is None:
                # 拿不到 provider 时保守放行（避免误伤）
                return None

            pcfg = getattr(prov, "provider_config", None)
            if not isinstance(pcfg, dict):
                pcfg = {}
            model_name = str(pcfg.get("model") or "")
            if not model_name:
                meta = getattr(prov, "meta", None)
                model_name = str(getattr(meta, "model", "") or "")
            modalities = pcfg.get("modalities")

            if isinstance(modalities, list) and modalities:
                if "image" in modalities:
                    return None
                return (f"⛔ 当前模型「{model_name or '未知'}」不支持图像输入（视觉），"
                        f"无法使用浏览器功能。浏览器操作依赖截图识别，请切换到视觉模型，"
                        f"或在服务商配置中为该模型勾选「图像」能力。")
            # modalities 未配置 → 启发式兜底
            if _model_looks_visual(model_name):
                return None
            return (f"⛔ 当前模型「{model_name or '未知'}」未声明视觉能力，"
                    f"无法使用浏览器功能。浏览器操作依赖截图识别，请切换到视觉模型，"
                    f"或在服务商配置中为该模型勾选「图像」能力。")
        except Exception as e:
            logger.debug(f"[VisionGate] 检测异常（放行）: {e}")
            return None

    # ==================== 隐私模式 ====================

    def _privacy_on(self) -> bool:
        return str(self.config.get("privacy_mode", "normal")).lower() == "privacy"

    @staticmethod
    def _mask_id(value) -> str:
        """把群号/QQ号打码成不可逆的短标识，保留可区分性。"""
        s = str(value or "")
        if not s:
            return s
        if len(s) <= 2:
            return "*" * len(s)
        return f"ID-{hashlib.sha1(s.encode()).hexdigest()[:6]}"

    def _apply_privacy(self, text: str) -> str:
        """隐私模式下把文本中的长数字串（群号/QQ号）替换为脱敏标识。

        同时移除工具返回的 ID 字段名中的真实号码。
        """
        if not self._privacy_on() or not text:
            return text

        # 5 位以上的连续数字视为 QQ 号/群号
        def _repl(m):
            return self._mask_id(m.group(0))

        return re.sub(r"\b\d{5,12}\b", _repl, text)

    def _privacy_filter_result(self, result):
        """对工具返回值做隐私脱敏（仅处理 message 文本字段）。"""
        if not self._privacy_on():
            return result
        if isinstance(result, dict):
            msg = result.get("message")
            if isinstance(msg, str):
                result = dict(result)
                result["message"] = self._apply_privacy(msg)
        return result

    def _build_tool_registry(self) -> Dict[str, dict]:
        registry = {}

        # ---------- 记忆管理 ----------
        registry["add_memory"] = {
            "name": "add_memory",
            "description": "添加重要记忆到存储中。AI 可以根据对话内容自动提取关键信息并保存，以便后续对话中回忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆内容，必填，例如用户喜欢喝咖啡"},
                    "tags": {"type": "string", "description": "标签，多个标签用英文逗号分隔，例如偏好,饮食"},
                    "importance": {"type": "integer", "description": "重要程度，1-10，数字越大越重要，默认5"}
                },
                "required": ["content"]
            },
            "keywords": [
                "记忆", "保存记忆", "记住", "添加记忆", "存储记忆", "备忘录", "笔记", "记录", "备忘", "提醒内容",
                "个人信息", "偏好", "喜好", "习惯", "重要信息", "用户资料", "存档", "留存", "write memory", "save note",
                "记一下", "记下来", "帮我记", "给我记", "备注一下", "存一下", "写下来", "留个底", "别忘了", "提醒我",
                "记个事", "记事本", "记忆本", "记忆库", "add note", "remember this", "memorize", "store info",
                "keep in mind", "note down", "jot down", "record info", "存档信息", "保存资料", "记住这个", "别忘掉",
                "脑子记一下", "记一笔", "留个记录", "俺记一下", "侬记牢", "记到", "记住哈", "记好", "mark memory", "set reminder"
            ],
            "handler": self.add_memory
        }

        registry["search_memories"] = {
            "name": "search_memories",
            "description": "搜索已保存的记忆。支持按关键词、用户范围筛选。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词，可选，不提供则返回最新记忆"},
                    "user_specific": {"type": "boolean", "description": "是否只搜索当前用户的记忆，默认为True"},
                    "limit": {"type": "integer", "description": "返回结果数量限制，默认10，最大20"}
                },
                "required": []
            },
            "keywords": [
                "搜索记忆", "查找记忆", "回忆", "查询记忆", "记忆搜索", "检索笔记", "找一下", "记忆列表", "列出记忆",
                "search memory", "find note", "recall", "查看记忆", "我的记忆", "用户记忆", "搜寻记忆", "翻记忆",
                "找记忆", "搜一下记忆", "记忆查询", "查询笔记", "查记录", "有啥记忆", "记得什么", "回忆一下", "回想",
                "想想以前", "以前记的", "历史记录", "过往记录", "搜索笔记", "查找笔记", "找找看", "检索记忆", "找回忆",
                "回忆录", "个人档案", "资料查询", "信息检索", "查存档", "看存档", "读记忆", "读取记忆", "翻阅记忆",
                "查看记录", "看记录", "搜记录", "检索记录", "查备忘", "看备忘", "搜索备忘", "search note", "query memory",
                "lookup memory", "find memory", "recall memory"
            ],
            "handler": self.search_memories
        }

        registry["update_memory"] = {
            "name": "update_memory",
            "description": "更新已有的记忆内容、标签或重要度。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆ID，必填（可从 search_memories 获取）"},
                    "content": {"type": "string", "description": "新的记忆内容，可选"},
                    "tags": {"type": "string", "description": "新的标签，多个用逗号分隔，可选"},
                    "importance": {"type": "integer", "description": "新的重要程度1-10，可选"}
                },
                "required": ["memory_id"]
            },
            "keywords": [
                "修改记忆", "更新记忆", "编辑记忆", "更改笔记", "修改备忘", "update memory", "edit note", "改记忆",
                "修正记忆", "调整记忆", "刷新记忆", "更新笔记", "修改记录", "编辑记录", "改备注", "修正备注",
                "改一下记忆", "更新一下", "编辑一下", "修改存档", "更新存档", "刷新存档", "修正信息", "更正信息",
                "改内容", "更新内容", "修改内容", "编辑内容", "调整内容", "变更记忆", "改变记忆", "变动记忆",
                "修改个人信息", "更新资料", "编辑资料", "change memory", "modify note", "revise memory",
                "alter memory", "update note", "refresh memory", "correct memory", "amend memory"
            ],
            "handler": self.update_memory
        }

        registry["delete_memory"] = {
            "name": "delete_memory",
            "description": "删除指定的记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "要删除的记忆ID，必填"}
                },
                "required": ["memory_id"]
            },
            "keywords": [
                "删除记忆", "移除记忆", "忘记", "清除记忆", "删掉笔记", "delete memory", "remove note", "删记忆",
                "消除记忆", "抹除记忆", "擦除记忆", "去掉记忆", "清理记忆", "丢弃记忆", "舍弃记忆", "扔掉记忆",
                "删了它", "不要了", "作废", "删除记录", "移除记录", "清除记录", "删档", "删掉存档", "删除备忘",
                "移除备忘", "清除备忘", "删除存档", "移除存档", "清除存档", "forget", "erase memory", "wipe memory",
                "discard memory", "drop memory", "purge memory", "clear memory", "delete note", "remove memory",
                "clean memory", "删掉记忆", "抹掉记忆", "取消记忆", "撤销记忆"
            ],
            "handler": self.delete_memory
        }

        registry["get_memory_detail"] = {
            "name": "get_memory_detail",
            "description": "获取单条记忆的完整详情。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆ID，必填"}
                },
                "required": ["memory_id"]
            },
            "keywords": [
                "记忆详情", "查看记忆", "记忆内容", "具体记忆", "detail memory", "show note", "查看详细", "详细记忆",
                "记忆明细", "记忆信息", "显示记忆", "展示记忆", "查看某条记忆", "看那条记忆", "那条记忆是什么",
                "记忆详情页", "记忆详细内容", "具体内容", "查看具体", "详细内容", "完整记忆", "记忆全文",
                "查看完整记录", "显示完整信息", "get memory detail", "view memory", "show memory", "inspect memory",
                "memory info", "memory details", "look at memory", "examine memory", "check memory"
            ],
            "handler": self.get_memory_detail
        }

        # ---------- 消息与定时 ----------
        registry["send_message"] = {
            "name": "send_message",
            "description": "立即向指定的QQ好友或群聊发送文本消息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "目标QQ号或群号，必填"},
                    "message": {"type": "string", "description": "要发送的消息内容，必填"},
                    "chat_type": {"type": "string", "description": "聊天类型，可选值：group(群聊)/private(私聊)/auto(自动识别)，默认auto"}
                },
                "required": ["target_id", "message"]
            },
            "keywords": [
                "发消息", "发送消息", "发信息", "私聊", "群发", "告诉", "通知", "send msg", "message", "chat",
                "发一条", "发个消息", "发信息给", "给某人发消息", "传话", "转告", "告知", "通报", "传达", "发文字",
                "发短信", "发送文本", "群内发", "群里说", "在群里发", "私信", "小窗", "私聊一下", "单独发", "send text",
                "text someone", "message someone", "notify", "alert", "ping", "发个信息", "捎个话", "带个话",
                "讲一声", "说一声", "通知一下", "告诉一下", "告知一下", "转达一下", "传达一下", "发过去"
            ],
            "handler": self.send_message_tool
        }

        registry["schedule_message"] = {
            "name": "schedule_message",
            "description": "创建简单的定时消息任务（仅发送文本消息，重启后丢失）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "目标QQ号或群号，必填"},
                    "message": {"type": "string", "description": "要发送的消息内容，必填"},
                    "send_time": {"type": "string", "description": "发送时间，支持格式：YYYY-MM-DD HH:MM、HH:MM、每天的HH:MM，必填"},
                    "chat_type": {"type": "string", "description": "聊天类型，group(群聊)或private(私聊)，默认group"}
                },
                "required": ["target_id", "message", "send_time"]
            },
            "keywords": [
                "定时消息", "定时发送", "延迟消息", "计划发送", "schedule", "reminder", "稍后发送", "预约消息",
                "定时发", "定时提醒", "到时发", "预约发送", "延迟发送", "预定消息", "设置定时", "定时任务", "闹钟消息",
                "定时传话", "定时通知", "定时告知", "定时转告", "定时提醒我", "定时告诉他", "定时告诉她", "定时广播",
                "定时群发", "定时私聊", "delay message", "send later", "schedule msg", "scheduled text",
                "future message", "plan message", "time message", "set reminder", "alarm message"
            ],
            "handler": self.schedule_message
        }

        registry["cancel_scheduled_message"] = {
            "name": "cancel_scheduled_message",
            "description": "取消由 schedule_message 创建的定时消息任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "要取消的任务ID，必填"}
                },
                "required": ["task_id"]
            },
            "keywords": [
                "取消定时", "撤销定时", "取消任务", "cancel schedule", "删除定时", "取消预约", "取消预定", "取消闹钟",
                "停止定时", "终止定时", "取消定时消息", "撤销定时消息", "取消延迟", "撤销延迟", "取消计划", "取消安排",
                "cancel reminder", "stop schedule", "remove schedule", "unschedule", "drop task", "abort task",
                "取消那个任务", "不要定时了", "算了别发了", "取消发送", "撤销发送"
            ],
            "handler": self.cancel_scheduled_message
        }

        registry["list_scheduled_messages"] = {
            "name": "list_scheduled_messages",
            "description": "列出由 schedule_message 创建的定时消息任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "show_all": {"type": "boolean", "description": "是否显示所有任务（包括已完成和已取消的），默认False"}
                },
                "required": []
            },
            "keywords": [
                "定时列表", "查看定时", "任务列表", "pending tasks", "scheduled list", "有哪些定时", "定时任务列表",
                "查看预约", "查看计划", "定时清单", "待发送列表", "排队消息", "列出定时", "显示定时", "展示定时",
                "list schedules", "show scheduled", "view schedules", "get tasks", "check tasks", "all timers",
                "当前定时", "进行中的定时"
            ],
            "handler": self.list_scheduled_messages
        }

        # ---------- QQ空间 ----------
        registry["publish_qzone"] = {
            "name": "publish_qzone",
            "description": "发布QQ空间说说（需要机器人已登录且支持QQ空间操作）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "说说内容，必填"}
                },
                "required": ["content"]
            },
            "keywords": [
                "发说说", "空间动态", "QQ空间", "发空间", "publish post", "qzone", "说说", "发一条说说", "写说说",
                "分享到空间", "空间发文", "空间说说", "发表动态", "空间状态", "空间日志", "写日志", "post to qzone",
                "update qzone", "share to qzone", "qzone feed", "说说内容", "发个说说", "更新空间", "发动态"
            ],
            "handler": self.publish_qzone
        }

        # ---------- 戳一戳 ----------
        registry["send_poke"] = {
            "name": "send_poke",
            "description": "发送戳一戳（窗口抖动）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_qq": {"type": "string", "description": "目标QQ号，必填"},
                    "chat_type": {"type": "string", "description": "聊天类型，可选值：group(群聊)/private(私聊)/auto(自动识别)，默认auto"}
                },
                "required": ["target_qq"]
            },
            "keywords": [
                "戳一戳", "窗口抖动", "poke", "戳", "抖动", "提醒", "戳一下", "戳他", "戳她", "戳戳", "戳人", "抖窗",
                "振屏", "拍一拍", "碰一碰", "轻触", "点一下", "戳一下用户", "抖动窗口", "send poke", "nudge", "tap",
                "shake", "buzz", "ping someone"
            ],
            "handler": self.send_poke
        }

        # ---------- QQ状态 ----------
        registry["update_qq_status"] = {
            "name": "update_qq_status",
            "description": "设置QQ在线状态（支持基础状态和娱乐状态）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "状态码，必填。可选值：online(在线), qme(Q我吧), away(离开), busy(忙碌), dnd(请勿打扰), invisible(隐身), listening(听歌中), sleeping(睡觉中), studying(学习中)"},
                    "duration_minutes": {"type": "integer", "description": "状态持续时间（分钟），必填，到期后自动恢复为在线"},
                    "delay_minutes": {"type": "integer", "description": "延迟执行时间（分钟），默认0"}
                },
                "required": ["status", "duration_minutes"]
            },
            "keywords": [
                "在线状态", "QQ状态", "设置状态", "隐身", "忙碌", "离开", "Q我吧", "请勿打扰", "听歌中", "睡觉中", "学习中",
                "status", "online", "away", "busy", "invisible", "状态切换", "改状态", "换状态", "设置在线", "设为隐身",
                "设为忙碌", "设为离开", "设为Q我吧", "设为请勿打扰", "设为听歌中", "设为睡觉中", "设为学习中", "调整状态",
                "变更状态", "修改状态", "更新状态", "change status", "set status", "update status", "status update",
                "appear offline", "go invisible", "go busy", "go away", "set online", "状态设置"
            ],
            "handler": self.update_qq_status
        }

        registry["get_qq_status"] = {
            "name": "get_qq_status",
            "description": "获取当前QQ在线状态描述（包含剩余时间）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "当前状态", "状态查询", "在线状态查询", "get status", "查看状态", "我现在什么状态", "什么状态",
                "查询状态", "看看状态", "我的状态", "机器人状态", "bot状态", "check status", "show status",
                "status info", "current status", "what is my status"
            ],
            "handler": self.get_qq_status
        }

        registry["get_fun_status_list"] = {
            "name": "get_fun_status_list",
            "description": "获取可用的娱乐状态列表。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "娱乐状态", "fun status", "状态列表", "可用状态", "有哪些娱乐状态", "娱乐状态列表", "趣味状态",
                "特殊状态", "所有状态", "状态选项", "可选状态", "list status", "get status list", "show statuses"
            ],
            "handler": self.get_fun_status_list
        }

        # ---------- 高级定时指令 ----------
        registry["create_scheduled_command"] = {
            "name": "create_scheduled_command",
            "description": "【高级定时指令】持久化存储，支持重启恢复，可执行多种操作：qzone_post（发空间）、status_change（改状态）、llm_remind（LLM提醒）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command_type": {"type": "string", "description": "指令类型，必填。可选值：qzone_post(发说说), status_change(改状态), llm_remind(LLM提醒)"},
                    "execute_time": {"type": "string", "description": "执行时间，必填。支持格式：YYYY-MM-DD HH:MM、HH:MM、每天的HH:MM"},
                    "params": {"type": "string", "description": "指令参数，JSON格式字符串，必填。例如：{\"content\": \"晚安\"}"},
                    "recurrence": {"type": "string", "description": "重复类型，可选值：once(单次)/daily(每天)，默认once"}
                },
                "required": ["command_type", "execute_time", "params"]
            },
            "keywords": [
                "高级定时", "持久定时", "定时指令", "计划任务", "cron", "scheduled command", "自动化", "定时发空间",
                "定时改状态", "定时提醒", "每天定时", "重复任务", "周期任务", "例行任务", "预约指令", "延迟执行",
                "计划指令", "预置任务", "设置自动化", "自动化任务", "定时执行", "计划执行", "create cron", "schedule job",
                "set cron", "add cron", "new schedule", "定时任务持久化", "重启后保留", "永久定时"
            ],
            "handler": self.create_scheduled_command
        }

        registry["list_scheduled_commands"] = {
            "name": "list_scheduled_commands",
            "description": "列出由 create_scheduled_command 创建的定时指令任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_executed": {"type": "boolean", "description": "是否包含已执行的指令，默认False"}
                },
                "required": []
            },
            "keywords": [
                "定时指令列表", "查看计划", "cron list", "scheduled commands list", "列出高级定时", "高级定时列表",
                "查看自动化任务", "自动化列表", "计划列表", "查看 cron", "show crons", "list jobs", "get schedules",
                "有哪些定时任务"
            ],
            "handler": self.list_scheduled_commands
        }

        registry["cancel_scheduled_command"] = {
            "name": "cancel_scheduled_command",
            "description": "取消由 create_scheduled_command 创建的定时指令任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "要取消的任务ID，必填"}
                },
                "required": ["task_id"]
            },
            "keywords": [
                "取消计划", "取消定时指令", "cancel cron", "delete scheduled command", "停止高级定时",
                "终止定时任务", "取消自动化", "取消预约", "取消周期", "取消重复", "取消计划任务", "abort cron",
                "stop schedule", "remove cron"
            ],
            "handler": self.cancel_scheduled_command
        }

        registry["delete_scheduled_command"] = {
            "name": "delete_scheduled_command",
            "description": "彻底删除由 create_scheduled_command 创建的定时指令任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "要删除的任务ID，必填"}
                },
                "required": ["task_id"]
            },
            "keywords": [
                "删除计划", "移除定时指令", "永久删除", "delete cron", "清除任务", "删除高级定时", "删除自动化",
                "抹掉任务", "清除 cron", "移除 cron", "remove job", "delete job"
            ],
            "handler": self.delete_scheduled_command
        }

        # ---------- 撤回消息 ----------
        registry["recall_by_reply"] = {
            "name": "recall_by_reply",
            "description": "撤回消息。支持两种方式：1) 直接传入 message_id 撤回任意消息；2) 引用消息撤回（回复时勾选引用）。支持群聊和私聊。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "要撤回的消息ID，可选。如果不提供则需要引用消息"}
                },
                "required": []
            },
            "keywords": [
                "撤回", "撤回消息", "recall", "delete message", "撤销", "删消息", "撤回那条", "撤销消息", "收回",
                "取消发送", "撤回我的", "撤了", "撤掉", "撤除", "撤回刚发的", "撤回上一句", "撤回群消息", "recall msg",
                "unsend", "revoke", "retract", "withdraw", "remove message"
            ],
            "handler": self.recall_by_reply
        }

        # ---------- 邮件 ----------
        registry["send_qq_email"] = {
            "name": "send_qq_email",
            "description": "通过QQ邮箱SMTP服务发送电子邮件。需要先在插件配置中设置发件人邮箱和授权码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "收件人邮箱地址，必填"},
                    "subject": {"type": "string", "description": "邮件主题，必填"},
                    "content": {"type": "string", "description": "邮件正文内容，必填"},
                    "nickname": {"type": "string", "description": "发件人昵称，可选"}
                },
                "required": ["to", "subject", "content"]
            },
            "keywords": [
                "邮件", "发送邮件", "email", "QQ邮箱", "邮箱", "mail", "发信", "发邮件", "寄信", "写信", "send email",
                "e-mail", "电子邮件", "电邮", "发伊妹儿", "邮寄", "发封信", "发个邮件", "发一封邮件", "send mail",
                "mail to", "email to", "compose email", "send an email"
            ],
            "handler": self.send_qq_email_tool
        }

        # ---------- 群成员身份 ----------
        registry["get_user_group_role"] = {
            "name": "get_user_group_role",
            "description": "查询指定用户在指定QQ群中的身份（群主/管理员/成员）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "群号，必填"},
                    "user_id": {"type": "string", "description": "用户QQ号，必填"}
                },
                "required": ["group_id", "user_id"]
            },
            "keywords": [
                "群身份", "管理员", "群主", "成员", "权限", "role", "group role", "查身份", "是什么身份", "什么角色",
                "群内身份", "群内职位", "查看权限", "检查身份", "查询角色", "用户角色", "get role", "check role",
                "user role", "member role", "群地位"
            ],
            "handler": self.get_user_group_role
        }

        # ---------- 群管理基础 ----------
        registry["set_essence_msg"] = {
            "name": "set_essence_msg",
            "description": "将引用消息添加到群精华。使用时需要引用要设置精华的消息。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "精华消息", "加精", "设为精华", "essence", "pin message", "设置精华", "标记精华", "精华", "加精华",
                "设为群精华", "添加精华", "推为精华", "加精消息", "pin msg", "set essence", "make essence"
            ],
            "handler": self.set_essence_msg
        }

        registry["delete_essence_msg"] = {
            "name": "delete_essence_msg",
            "description": "将引用消息从群精华中移除。使用时需要引用要取消精华的消息。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "取消精华", "移除精华", "unpin", "delete essence", "取消加精", "去掉精华", "删除精华", "撤销精华",
                "移出精华", "unset essence", "remove essence", "unpin message", "取消精华消息"
            ],
            "handler": self.delete_essence_msg
        }

        registry["set_group_ban"] = {
            "name": "set_group_ban",
            "description": "禁言或解禁指定用户。duration为禁言秒数（必须是60的倍数），设置为0即解除禁言。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "要禁言/解禁的用户QQ号"},
                    "duration": {"type": "integer", "description": "禁言持续时间（秒），0表示解禁"}
                },
                "required": ["user_id", "duration"]
            },
            "keywords": [
                "禁言", "解禁", "ban", "mute", "禁言用户", "解除禁言", "unmute", "禁止发言", "不许说话", "闭嘴",
                "关小黑屋", "封口", "禁言某人", "让他闭嘴", "把她禁言", "禁言多少分钟", "解封", "恢复发言",
                "允许说话", "取消禁言", "ban user", "mute member", "unmute user", "shut up", "silence"
            ],
            "handler": self.set_group_ban
        }

        registry["set_group_kick"] = {
            "name": "set_group_kick",
            "description": "将用户从群聊中移除。需要开启踢人功能（kick_enabled=true）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "要踢出的用户QQ号"}
                },
                "required": ["user_id"]
            },
            "keywords": [
                "踢人", "移除成员", "kick", "踢出群", "请出群", "踢掉", "移除", "逐出", "赶出去", "踢了",
                "把他踢了", "把她踢了", "移除群聊", "移出群", "删除成员", "kick user", "remove member", "boot",
                "expel", "banish"
            ],
            "handler": self.set_group_kick
        }

        registry["set_group_whole_ban"] = {
            "name": "set_group_whole_ban",
            "description": "开启或关闭全体禁言。",
            "parameters": {
                "type": "object",
                "properties": {
                    "enable": {"type": "boolean", "description": "True开启全体禁言，False关闭"}
                },
                "required": ["enable"]
            },
            "keywords": [
                "全体禁言", "全员禁言", "全群禁言", "mute all", "whole ban", "全部禁言", "所有人禁言", "开启全员禁言",
                "关闭全员禁言", "全群静音", "全体静音", "mute everyone", "ban all", "全群闭嘴"
            ],
            "handler": self.set_group_whole_ban
        }

        registry["set_group_card"] = {
            "name": "set_group_card",
            "description": "修改群成员的群昵称（群名片）。card为空字符串时取消群昵称。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "要修改群昵称的用户QQ号"},
                    "card": {"type": "string", "description": "新的群昵称，为空则取消"}
                },
                "required": ["user_id", "card"]
            },
            "keywords": [
                "群昵称", "群名片", "改名片", "set card", "改名", "nickname", "修改群昵称", "改群名", "设置群名片",
                "修改名片", "改他的名片", "改她的名片", "群内昵称", "群别名", "group nickname", "set nickname",
                "change card", "update card", "rename member"
            ],
            "handler": self.set_group_card
        }

        registry["send_group_notice"] = {
            "name": "send_group_notice",
            "description": "发布群公告。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "公告内容"}
                },
                "required": ["content"]
            },
            "keywords": [
                "群公告", "发布公告", "notice", "announcement", "通知", "发公告", "群通知", "发群公告", "写公告",
                "贴公告", "公布", "广而告之", "群内通知", "发布群通知", "send notice", "post announcement",
                "group notice", "make announcement"
            ],
            "handler": self.send_group_notice
        }

        registry["delete_group_notice"] = {
            "name": "delete_group_notice",
            "description": "撤回群公告。",
            "parameters": {
                "type": "object",
                "properties": {
                    "notice_id": {"type": "string", "description": "公告ID"}
                },
                "required": ["notice_id"]
            },
            "keywords": [
                "删除公告", "撤回公告", "delete notice", "取消公告", "移除公告", "撤公告", "删公告", "删除群公告",
                "撤销群公告", "remove notice", "recall notice"
            ],
            "handler": self.delete_group_notice
        }

        registry["list_group_files"] = {
            "name": "list_group_files",
            "description": "查询群文件列表（根目录）。返回文件名和大小。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "群文件", "文件列表", "list files", "查看文件", "群共享文件", "群资料", "群文档", "有哪些文件",
                "看看群文件", "列出群文件", "group files", "list group files", "show files", "dir"
            ],
            "handler": self.list_group_files
        }

        registry["delete_group_file"] = {
            "name": "delete_group_file",
            "description": "删除群文件。可通过 file_id 指定要删除的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "文件ID，必填（可通过 list_group_files 获取）"}
                },
                "required": ["file_id"]
            },
            "keywords": [
                "删除群文件", "删文件", "delete file", "移除文件", "删除群共享", "删掉群文件", "清除群文件",
                "remove file", "delete group file", "erase file"
            ],
            "handler": self.delete_group_file
        }

        registry["get_group_members_info"] = {
            "name": "get_group_members_info",
            "description": "获取当前群聊的成员信息列表（包含user_id、display_name、username、role）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "群成员", "成员列表", "member list", "群友", "查看成员", "群成员信息", "群内成员", "群成员名单",
                "列出成员", "list members", "group members", "show members", "who is in group"
            ],
            "handler": self.get_group_members_info
        }

        # ---------- 群管理增强 ----------
        registry["set_group_admin"] = {
            "name": "set_group_admin",
            "description": "设置或取消群管理员。需要机器人是群主。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "目标用户QQ号，必填"},
                    "enable": {"type": "boolean", "description": "True设置为管理员，False取消管理员"}
                },
                "required": ["user_id", "enable"]
            },
            "keywords": [
                "设置管理员", "取消管理员", "admin", "群管", "任命", "给管理", "提升为管理", "设为管理", "撤管理",
                "下管理", "取消管理", "set admin", "promote to admin", "demote from admin", "make admin",
                "remove admin", "群管理员"
            ],
            "handler": self.set_group_admin
        }

        registry["set_group_name"] = {
            "name": "set_group_name",
            "description": "修改群名称。需要机器人有相应的管理权限。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_name": {"type": "string", "description": "新的群名称，必填"}
                },
                "required": ["group_name"]
            },
            "keywords": [
                "群名称", "改名", "修改群名", "rename group", "群名", "改群名", "重命名群", "变更群名称",
                "set group name", "change group name", "update group name"
            ],
            "handler": self.set_group_name
        }

        registry["get_group_notice_list"] = {
            "name": "get_group_notice_list",
            "description": "获取群公告列表。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "公告列表", "查看公告", "list notices", "群公告列表", "所有公告", "列出公告", "show notices",
                "get notices", "list group notices"
            ],
            "handler": self.get_group_notice_list
        }

        registry["upload_group_file"] = {
            "name": "upload_group_file",
            "description": "上传本地文件到群文件。需要机器人有上传权限。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "本地文件的绝对路径，必填"},
                    "file_name": {"type": "string", "description": "上传后显示的文件名，可选"}
                },
                "required": ["file_path"]
            },
            "keywords": [
                "上传文件", "群文件上传", "upload file", "传文件", "上传到群", "群共享上传", "发送文件到群",
                "upload to group", "share file"
            ],
            "handler": self.upload_group_file
        }

        registry["create_group_file_folder"] = {
            "name": "create_group_file_folder",
            "description": "在群文件根目录创建文件夹。",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_name": {"type": "string", "description": "文件夹名称，必填"}
                },
                "required": ["folder_name"]
            },
            "keywords": [
                "新建文件夹", "创建目录", "create folder", "新建目录", "创建群文件夹", "群文件新建文件夹",
                "make directory", "new folder"
            ],
            "handler": self.create_group_file_folder
        }

        registry["delete_group_folder"] = {
            "name": "delete_group_folder",
            "description": "删除群文件夹（注意：会连带删除文件夹内所有文件）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_id": {"type": "string", "description": "文件夹ID，必填（可通过 list_group_files 获取）"}
                },
                "required": ["folder_id"]
            },
            "keywords": [
                "删除文件夹", "删目录", "delete folder", "移除文件夹", "删除群文件夹", "remove folder",
                "delete directory"
            ],
            "handler": self.delete_group_folder
        }

        registry["get_group_honor_info"] = {
            "name": "get_group_honor_info",
            "description": "获取群荣誉信息（龙王、群聊之火、快乐源泉等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "honor_type": {"type": "string", "description": "荣誉类型，可选：talkative(龙王)/performer(群聊之火)/legend(传说)/strong_newbie(新人王)/emotion(快乐源泉)/all(全部)，默认all"}
                },
                "required": []
            },
            "keywords": [
                "龙王", "荣誉", "群聊之火", "快乐源泉", "honor", "群荣誉", "查看荣誉", "群内荣誉", "群称号",
                "群活跃", "group honor", "查看龙王", "谁最活跃", "群称号列表"
            ],
            "handler": self.get_group_honor_info
        }

        registry["get_group_at_all_remain"] = {
            "name": "get_group_at_all_remain",
            "description": "查询群聊中 @全体成员 的剩余次数。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "@全体成员", "at全体", "剩余次数", "at all remain", "还能@几次", "@所有人剩余次数",
                "at全体剩余", "查看@全体次数", "check at all", "at all quota"
            ],
            "handler": self.get_group_at_all_remain
        }

        registry["set_group_special_title"] = {
            "name": "set_group_special_title",
            "description": "设置群成员专属头衔（需要群主权限）。头衔长度不超过6个字符。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "目标用户QQ号，必填"},
                    "special_title": {"type": "string", "description": "专属头衔，空字符串表示取消头衔"}
                },
                "required": ["user_id", "special_title"]
            },
            "keywords": [
                "专属头衔", "头衔", "特殊头衔", "title", "special title", "给头衔", "设置头衔", "授予头衔",
                "取消头衔", "群头衔", "成员头衔", "set title", "custom title"
            ],
            "handler": self.set_group_special_title
        }

        registry["get_group_shut_list"] = {
            "name": "get_group_shut_list",
            "description": "获取当前群聊中被禁言的成员列表。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "禁言列表", "被禁言", "shut list", "muted members", "谁被禁言了", "禁言人员", "查看禁言",
                "list muted", "show muted", "禁言中的人"
            ],
            "handler": self.get_group_shut_list
        }

        registry["get_group_ignore_add_request"] = {
            "name": "get_group_ignore_add_request",
            "description": "获取群聊中被忽略的加群请求列表。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "加群请求", "忽略请求", "入群申请", "join request", "被忽略的加群", "忽略的申请", "查看忽略请求",
                "pending requests", "ignored join requests"
            ],
            "handler": self.get_group_ignore_add_request
        }

        registry["set_group_add_option"] = {
            "name": "set_group_add_option",
            "description": "设置群聊的加群方式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "option": {"type": "string", "description": "加群选项，必填。可选值：allow(允许任何人)/need_verify(需要验证)/not_allow(不允许加群)"}
                },
                "required": ["option"]
            },
            "keywords": [
                "加群方式", "入群设置", "join option", "验证", "允许加群", "禁止加群", "加群验证", "加群权限",
                "设置加群", "群加入方式", "set join option", "group join setting"
            ],
            "handler": self.set_group_add_option
        }

        registry["send_group_sign"] = {
            "name": "send_group_sign",
            "description": "群打卡（需要机器人是群成员）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "打卡", "群打卡", "签到", "sign", "群签到", "每日打卡", "群内打卡", "check in", "group sign",
                "daily sign"
            ],
            "handler": self.send_group_sign
        }

        # ---------- 设置QQ头像 ----------
        registry["set_qq_avatar"] = {
            "name": "set_qq_avatar",
            "description": "设置机器人的QQ头像，需要提供图片文件路径/URL/Base64。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "图片路径、URL或Base64，必填。"}
                },
                "required": ["file"]
            },
            "keywords": [
                "QQ头像", "设置头像", "更换头像", "avatar", "profile picture", "改头像", "换头像", "头像设置",
                "上传头像", "更新头像", "set avatar", "change avatar", "update avatar", "换一个头像"
            ],
            "handler": self.set_qq_avatar
        }

        # ---------- 群文件移动/重命名/传输 ----------
        registry["move_group_file"] = {
            "name": "move_group_file",
            "description": "移动群文件到指定目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "文件ID，必填"},
                    "current_parent_directory": {"type": "string", "description": "当前父目录ID，根目录为/"},
                    "target_parent_directory": {"type": "string", "description": "目标父目录ID，根目录为/"}
                },
                "required": ["file_id", "current_parent_directory", "target_parent_directory"]
            },
            "keywords": [
                "移动文件", "移动群文件", "move file", "剪切", "文件移动", "移动位置", "挪文件", "转移文件",
                "移动群共享", "move group file", "relocate file"
            ],
            "handler": self.move_group_file
        }

        registry["rename_group_file"] = {
            "name": "rename_group_file",
            "description": "重命名群文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "文件ID，必填"},
                    "current_parent_directory": {"type": "string", "description": "当前父目录ID，根目录为/"},
                    "new_name": {"type": "string", "description": "新文件名，必填"}
                },
                "required": ["file_id", "current_parent_directory", "new_name"]
            },
            "keywords": [
                "重命名文件", "改名", "rename file", "文件重命名", "修改文件名", "群文件改名", "更改文件名",
                "rename group file"
            ],
            "handler": self.rename_group_file
        }

        registry["trans_group_file"] = {
            "name": "trans_group_file",
            "description": "传输群文件（获取下载链接等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "文件ID，必填"}
                },
                "required": ["file_id"]
            },
            "keywords": [
                "传输文件", "获取文件链接", "trans file", "下载链接", "文件直链", "群文件下载", "获取下载地址",
                "get file url", "transfer file"
            ],
            "handler": self.trans_group_file
        }

        # ---------- 点赞 ----------
        registry["send_like"] = {
            "name": "send_like",
            "description": "给指定用户点赞（名片赞）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "对方QQ号，必填"},
                    "times": {"type": "integer", "description": "点赞次数，默认1，建议不超过10"}
                },
                "required": ["user_id"]
            },
            "keywords": [
                "点赞", "名片赞", "like", "送赞", "点个赞", "给个赞", "赞一个", "点赞一下", "刷赞", "送爱心",
                "send like", "give like", "thumb up", "praise"
            ],
            "handler": self.send_like_tool
        }

        # ---------- 获取历史消息 ----------
        registry["get_group_msg_history"] = {
            "name": "get_group_msg_history",
            "description": "获取群里最新的一批消息记录，可用于查看最近群内在聊什么、回顾群聊内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "群号，可选，默认为当前群聊"},
                    "count": {"type": "integer", "description": "获取最近多少条消息，默认20，最大100。用户说'获取10条'时传10"}
                },
                "required": []
            },
            "keywords": [
                "历史消息", "聊天记录", "history", "message log", "群记录", "群聊天记录", "过往消息", "旧消息",
                "查看历史", "群历史", "group history", "get history", "past messages",
                "群聊记录", "群聊历史", "群消息记录", "群内聊天", "群里说了什么", "群聊天",
                "看看群消息", "查群记录", "群对话", "群消息历史", "群聊内容", "群消息回溯",
                "群内记录", "群里消息", "群最近消息", "群最新消息", "lookup group messages",
                "group chat log", "group conversation", "群聊历史记录"
            ],
            "handler": self.get_group_msg_history
        }

        registry["get_friend_msg_history"] = {
            "name": "get_friend_msg_history",
            "description": "获取与好友的最新一批私聊消息记录，可用于查看与好友最近的聊天内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "好友QQ号，必填"},
                    "count": {"type": "integer", "description": "获取最近多少条消息，默认20，最大100。用户说'获取10条'时传10"}
                },
                "required": ["user_id"]
            },
            "keywords": [
                "好友历史", "私聊记录", "friend history", "聊天记录", "私聊历史", "与某人的记录",
                "查看私聊记录", "friend chat history", "private history",
                "私聊聊天", "私聊消息", "好友聊天", "看看和某人的聊天", "查私聊记录",
                "好友对话", "私聊对话", "私人消息记录", "和XXX的聊天", "私聊历史消息",
                "和好友的聊天", "私人聊天", "私信记录", "私信历史", "private message log",
                "direct message history"
            ],
            "handler": self.get_friend_msg_history
        }

        # ---------- 设置群头像 ----------
        registry["set_group_portrait"] = {
            "name": "set_group_portrait",
            "description": "设置群头像，需要提供群号和图片文件路径/URL/Base64。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "群号"},
                    "file": {"type": "string", "description": "图片路径、URL或Base64，必填"}
                },
                "required": ["group_id", "file"]
            },
            "keywords": [
                "群头像", "设置群头像", "group portrait", "group avatar", "改群头像", "换群头像", "群图标",
                "set group avatar", "change group icon"
            ],
            "handler": self.set_group_portrait
        }

        # ---------- 获取自定义表情 ----------
        registry["fetch_custom_face"] = {
            "name": "fetch_custom_face",
            "description": "获取机器人的自定义表情列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "获取数量，默认48"}
                },
                "required": []
            },
            "keywords": [
                "自定义表情", "表情列表", "custom face", "emoji", "表情包", "我的表情", "机器人表情", "收藏表情",
                "get custom faces", "list faces", "fetch faces"
            ],
            "handler": self.fetch_custom_face
        }

        # ---------- 设置输入状态 ----------
        registry["set_input_status"] = {
            "name": "set_input_status",
            "description": "设置输入状态（显示正在输入...）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "目标用户QQ号，私聊时必填，群聊时可省略"},
                    "event_type": {"type": "integer", "description": "事件类型，1表示正在输入，2表示取消，默认1"}
                },
                "required": []
            },
            "keywords": [
                "输入状态", "正在输入", "typing", "输入中", "显示输入", "设置输入中", "让对方看到正在输入",
                "show typing", "typing indicator"
            ],
            "handler": self.set_input_status_tool
        }

        # ---------- AI 声聊 ----------
        registry["get_ai_characters"] = {
            "name": "get_ai_characters",
            "description": "获取当前可用的 AI 语音角色列表。在发送 AI 语音前可调用此工具了解可选角色。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "AI语音", "语音角色", "AI角色", "characters", "声聊", "音色", "有哪些角色", "AI音色列表",
                "语音角色列表", "list characters", "get voices", "voice characters", "AI voice"
            ],
            "handler": self.get_ai_characters_tool
        }

        registry["send_ai_voice"] = {
            "name": "send_ai_voice",
            "description": "在群聊中发送 AI 语音消息（使用指定角色的音色朗读文本）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要转换为语音的文本内容，必填"},
                    "character": {"type": "string", "description": "AI 角色ID或名称，可选。若不填则使用配置文件中的默认角色或自动选择第一个可用角色"}
                },
                "required": ["text"]
            },
            "keywords": [
                "AI语音", "发送语音", "语音消息", "朗读", "voice", "speak", "tts", "语音合成", "文字转语音",
                "让机器人说话", "机器人语音", "AI朗读", "ai voice", "text to speech", "say"
            ],
            "handler": self.send_ai_voice_tool
        }

        # ---------- 联系人 ----------
        registry["search_contacts"] = {
            "name": "search_contacts",
            "description": "搜索QQ好友或群聊，支持按QQ号、昵称、群名模糊匹配。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词，必填（可以是QQ号、昵称或群名的一部分）"},
                    "search_type": {"type": "string", "description": "搜索范围，可选值：all/friend/group，默认all"}
                },
                "required": ["keyword"]
            },
            "keywords": [
                "搜索好友", "搜索群", "查找联系人", "search contact", "找人", "找群", "搜索用户", "查找好友",
                "查找群", "搜一下", "找一下", "搜索QQ", "search friend", "search group", "lookup"
            ],
            "handler": self.search_contacts
        }

        registry["list_contacts"] = {
            "name": "list_contacts",
            "description": "获取好友或群聊列表（不进行模糊搜索，直接列出）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_type": {"type": "string", "description": "类型，可选值：all/friend/group，默认all"},
                    "limit": {"type": "integer", "description": "返回的最大数量，默认20，最大100"}
                },
                "required": []
            },
            "keywords": [
                "好友列表", "群列表", "联系人", "contact list", "friends", "groups", "列出好友", "列出群聊",
                "所有好友", "所有群", "list friends", "list groups"
            ],
            "handler": self.list_contacts
        }

        # ---------- 设置个人资料 ----------
        registry["set_qq_profile"] = {
            "name": "set_qq_profile",
            "description": "修改机器人自己的QQ个人资料，包括昵称和个人说明。",
            "parameters": {
                "type": "object",
                "properties": {
                    "nickname": {"type": "string", "description": "新的昵称，可选"},
                    "personal_note": {"type": "string", "description": "新的个性签名/个人说明，可选"}
                },
                "required": []
            },
            "keywords": [
                "个人资料", "修改资料", "改昵称", "改签名", "个性签名", "QQ资料", "profile", "set profile",
                "更新资料", "编辑资料", "修改个人信息", "change nickname", "update signature"
            ],
            "handler": self.set_qq_profile_tool
        }

        # ---------- 闪传功能 ----------
        registry["create_flash_task"] = {
            "name": "create_flash_task",
            "description": "创建闪传任务，用于快速传输文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {"type": "string", "description": "文件路径（多个文件用逗号分隔），必填"},
                    "name": {"type": "string", "description": "任务名称，可选"},
                    "thumb_path": {"type": "string", "description": "缩略图路径，可选"}
                },
                "required": ["files"]
            },
            "keywords": [
                "闪传", "创建闪传", "flash task", "快传", "闪电传输", "create flash", "new flash task",
                "闪传任务", "发起闪传", "创建快传"
            ],
            "handler": self.create_flash_task_tool
        }

        registry["get_flash_file_list"] = {
            "name": "get_flash_file_list",
            "description": "获取闪传任务中的文件列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fileset_id": {"type": "string", "description": "文件集ID，必填"}
                },
                "required": ["fileset_id"]
            },
            "keywords": [
                "闪传文件列表", "flash files", "查看闪传", "闪传里有什么", "get flash files",
                "闪传文件", "快传文件", "查看快传文件"
            ],
            "handler": self.get_flash_file_list_tool
        }

        registry["get_flash_file_url"] = {
            "name": "get_flash_file_url",
            "description": "获取闪传文件的下载链接。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fileset_id": {"type": "string", "description": "文件集ID，必填"},
                    "file_name": {"type": "string", "description": "文件名，可选"},
                    "file_index": {"type": "integer", "description": "文件索引，可选"}
                },
                "required": ["fileset_id"]
            },
            "keywords": [
                "闪传下载链接", "flash url", "获取闪传链接", "get flash url", "闪传链接",
                "下载闪传", "快传链接", "快传下载"
            ],
            "handler": self.get_flash_file_url_tool
        }

        registry["send_flash_msg"] = {
            "name": "send_flash_msg",
            "description": "发送闪传消息给好友或群。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fileset_id": {"type": "string", "description": "文件集ID，必填"},
                    "user_id": {"type": "string", "description": "用户QQ号，可选"},
                    "group_id": {"type": "string", "description": "群号，可选"}
                },
                "required": ["fileset_id"]
            },
            "keywords": [
                "发送闪传", "send flash", "闪传发送", "发闪传", "send flash msg",
                "快传发送", "发快传", "闪传给别人"
            ],
            "handler": self.send_flash_msg_tool
        }

        registry["get_share_link"] = {
            "name": "get_share_link",
            "description": "获取文件分享链接。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fileset_id": {"type": "string", "description": "文件集ID，必填"}
                },
                "required": ["fileset_id"]
            },
            "keywords": [
                "分享链接", "share link", "获取分享链接", "get share link", "文件分享",
                "生成分享链接", "分享文件", "创建分享"
            ],
            "handler": self.get_share_link_tool
        }

        registry["get_fileset_info"] = {
            "name": "get_fileset_info",
            "description": "获取文件集详细信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fileset_id": {"type": "string", "description": "文件集ID，必填"}
                },
                "required": ["fileset_id"]
            },
            "keywords": [
                "文件集信息", "fileset info", "查看文件集", "get fileset", "文件集详情",
                "快传信息", "闪传信息"
            ],
            "handler": self.get_fileset_info_tool
        }

        registry["get_fileset_id"] = {
            "name": "get_fileset_id",
            "description": "通过分享码或链接获取文件集ID。",
            "parameters": {
                "type": "object",
                "properties": {
                    "share_code": {"type": "string", "description": "分享码或分享链接，必填"}
                },
                "required": ["share_code"]
            },
            "keywords": [
                "分享码", "share code", "获取文件集ID", "get fileset id",
                "通过分享码", "解析分享链接", "提取文件集"
            ],
            "handler": self.get_fileset_id_tool
        }

        registry["download_fileset"] = {
            "name": "download_fileset",
            "description": "下载文件集到本地。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fileset_id": {"type": "string", "description": "文件集ID，必填"}
                },
                "required": ["fileset_id"]
            },
            "keywords": [
                "下载文件集", "download fileset", "下载快传", "下载闪传",
                "获取文件", "拉取文件"
            ],
            "handler": self.download_fileset_tool
        }

        # ---------- 在线文件 ----------
        registry["get_online_file_msg"] = {
            "name": "get_online_file_msg",
            "description": "获取在线文件消息列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户QQ号，必填"}
                },
                "required": ["user_id"]
            },
            "keywords": [
                "在线文件", "online file", "查看在线文件", "get online files",
                "文件消息", "文件传输", "在线传输"
            ],
            "handler": self.get_online_file_msg_tool
        }

        registry["send_online_file"] = {
            "name": "send_online_file",
            "description": "发送在线文件给好友。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户QQ号，必填"},
                    "file_path": {"type": "string", "description": "本地文件路径，必填"},
                    "file_name": {"type": "string", "description": "文件名，可选"}
                },
                "required": ["user_id", "file_path"]
            },
            "keywords": [
                "发送文件", "send file", "在线发送文件", "send online file",
                "传文件", "文件传输", "发个文件"
            ],
            "handler": self.send_online_file_tool
        }

        registry["send_online_folder"] = {
            "name": "send_online_folder",
            "description": "发送在线文件夹给好友。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户QQ号，必填"},
                    "folder_path": {"type": "string", "description": "本地文件夹路径，必填"},
                    "folder_name": {"type": "string", "description": "文件夹名称，可选"}
                },
                "required": ["user_id", "folder_path"]
            },
            "keywords": [
                "发送文件夹", "send folder", "在线发送文件夹", "send online folder",
                "传文件夹", "文件夹传输"
            ],
            "handler": self.send_online_folder_tool
        }

        registry["receive_online_file"] = {
            "name": "receive_online_file",
            "description": "接收在线文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户QQ号，必填"},
                    "msg_id": {"type": "string", "description": "消息ID，必填"},
                    "element_id": {"type": "string", "description": "元素ID，必填"}
                },
                "required": ["user_id", "msg_id", "element_id"]
            },
            "keywords": [
                "接收文件", "receive file", "接收在线文件", "receive online file",
                "下载文件", "保存文件"
            ],
            "handler": self.receive_online_file_tool
        }

        registry["refuse_online_file"] = {
            "name": "refuse_online_file",
            "description": "拒绝接收在线文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户QQ号，必填"},
                    "msg_id": {"type": "string", "description": "消息ID，必填"},
                    "element_id": {"type": "string", "description": "元素ID，必填"}
                },
                "required": ["user_id", "msg_id", "element_id"]
            },
            "keywords": [
                "拒绝文件", "refuse file", "拒绝接收", "refuse online file",
                "不接收", "取消接收"
            ],
            "handler": self.refuse_online_file_tool
        }

        registry["cancel_online_file"] = {
            "name": "cancel_online_file",
            "description": "取消在线文件传输。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户QQ号，必填"},
                    "msg_id": {"type": "string", "description": "消息ID，必填"}
                },
                "required": ["user_id", "msg_id"]
            },
            "keywords": [
                "取消文件传输", "cancel file", "取消传输", "cancel online file",
                "停止传输", "中断传输"
            ],
            "handler": self.cancel_online_file_tool
        }

        # ---------- 工作区 ----------
        registry["run_python_code"] = {
            "name": "run_python_code",
            "description": "在工作区执行Python代码。代码中可以使用workspace_path变量访问工作区目录。重要：文件会自动保存到工作区，不要硬编码路径，使用workspace_path变量。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的Python代码，必填"}
                },
                "required": ["code"]
            },
            "keywords": [
                "执行代码", "运行代码", "run code", "执行python", "运行python",
                "工作区", "workspace", "生成文件", "执行脚本", "python代码"
            ],
            "handler": self.run_python_code_tool
        }

        registry["list_workspace_files"] = {
            "name": "list_workspace_files",
            "description": "列出工作区中的所有文件。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": [
                "工作区文件", "workspace files", "列出文件", "查看文件",
                "文件列表", "有哪些文件", "list files"
            ],
            "handler": self.list_workspace_files_tool
        }

        registry["read_workspace_file"] = {
            "name": "read_workspace_file",
            "description": "读取工作区中的文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名，必填"}
                },
                "required": ["filename"]
            },
            "keywords": [
                "读取文件", "read file", "查看文件内容", "打开文件",
                "读文件", "看文件"
            ],
            "handler": self.read_workspace_file_tool
        }

        registry["read_image"] = {
            "name": "read_image",
            "description": "读取图片文件并返回base64编码内容。支持工作区文件名、绝对路径、或截图路径（screenshot_cache中的文件）。用于查看Python代码生成的图表、截图等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "图片文件名、绝对路径或截图路径，必填"}
                },
                "required": ["filename"]
            },
            "keywords": [
                "查看图片", "读取图片", "看图片", "read image", "view image", "图片内容",
                "看看图", "打开图片", "查看图表", "看图表", "看看生成的图", "看截图",
                "view chart", "show image", "open image", "check image", "inspect image"
            ],
            "handler": self.read_image_tool
        }

        registry["send_file"] = {
            "name": "send_file",
            "description": "发送文件到QQ群或私聊。支持工作区文件名、绝对路径、或截图路径（screenshot_cache中的文件）。图片以图片消息形式发送，其他以文件形式发送。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名、绝对路径或截图路径（screenshot_cache中的文件），必填"},
                    "target_id": {"type": "string", "description": "目标群号或QQ号，必填"},
                    "chat_type": {"type": "string", "description": "聊天类型：group(群聊)/private(私聊)/auto(自动识别)，默认auto"},
                    "as_image": {"type": "boolean", "description": "是否以图片形式发送（仅支持图片文件），默认false"}
                },
                "required": ["filename", "target_id"]
            },
            "keywords": [
                "发送文件", "send file", "发文件", "传文件", "发送图片", "send image",
                "把文件发", "把图发", "文件发给", "图片发给", "发送到群", "发送到私聊",
                "deliver file", "share file", "transfer file", "推送文件", "投递文件",
                "发个工作区文件", "把生成的图发出去", "发过去", "file delivery"
            ],
            "handler": self.send_file_tool
        }

        registry["delete_workspace_file"] = {
            "name": "delete_workspace_file",
            "description": "删除工作区中的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名，必填"}
                },
                "required": ["filename"]
            },
            "keywords": [
                "删除文件", "delete file", "移除文件", "删文件",
                "清除文件", "remove file"
            ],
            "handler": self.delete_workspace_file_tool
        }

        registry["fetch_url"] = {
            "name": "fetch_url",
            "description": "获取网页内容。可以打开链接并返回网页文本内容，用于阅读文章、获取信息等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "网页URL，必填"},
                    "max_chars": {"type": "integer", "description": "最大返回字数，默认500"}
                },
                "required": ["url"]
            },
            "keywords": [
                "获取网页", "打开链接", "读取网页", "抓取网页",
                "fetch url", "read webpage", "get content"
            ],
            "handler": self.fetch_url_tool
        }

        registry["browser_search"] = {
            "name": "browser_search",
            "description": "搜索关键词。使用浏览器打开搜索引擎搜索指定关键词。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词，必填"},
                    "engine": {"type": "string", "description": "搜索引擎，可选值：百度、必应、谷歌，默认百度"}
                },
                "required": ["keyword"]
            },
            "keywords": ["搜索", "百度搜索", "必应搜索", "谷歌搜索", "search"],
            "handler": self.browser_search_tool
        }

        registry["browser_visit"] = {
            "name": "browser_visit",
            "description": "访问指定链接。打开浏览器访问URL。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要访问的URL，必填"}
                },
                "required": ["url"]
            },
            "keywords": ["访问链接", "打开网址", "visit url", "open url"],
            "handler": self.browser_visit_tool
        }

        registry["browser_click"] = {
            "name": "browser_click",
            "description": "点击页面坐标。坐标与你最近看到的截图像素一一对应（左上角为原点）。请先看截图确定目标位置，再输出 x,y 坐标。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X坐标（截图像素），必填"},
                    "y": {"type": "integer", "description": "Y坐标（截图像素），必填"}
                },
                "required": ["x", "y"]
            },
            "keywords": ["点击坐标", "click coord", "点击位置", "点击", "click", "点击按钮", "点击元素", "点一下"],
            "handler": self.browser_click_tool
        }

        registry["browser_double_click"] = {
            "name": "browser_double_click",
            "description": "在页面坐标处双击（用于选中文本、打开文件夹等场景）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X坐标（截图像素），必填"},
                    "y": {"type": "integer", "description": "Y坐标（截图像素），必填"}
                },
                "required": ["x", "y"]
            },
            "keywords": ["双击", "double click", "连点两下"],
            "handler": self.browser_double_click_tool
        }

        registry["browser_right_click"] = {
            "name": "browser_right_click",
            "description": "在页面坐标处右键单击（打开浏览器上下文菜单）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X坐标（截图像素），必填"},
                    "y": {"type": "integer", "description": "Y坐标（截图像素），必填"}
                },
                "required": ["x", "y"]
            },
            "keywords": ["右键", "right click", "右击", "上下文菜单"],
            "handler": self.browser_right_click_tool
        }

        registry["browser_long_press"] = {
            "name": "browser_long_press",
            "description": "在页面坐标处长按（按住不放一段时间后松开，用于唤起悬浮菜单/拖动条等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X坐标（截图像素），必填"},
                    "y": {"type": "integer", "description": "Y坐标（截图像素），必填"},
                    "duration_ms": {"type": "integer", "description": "按住时长（毫秒），默认1000，范围100-10000"}
                },
                "required": ["x", "y"]
            },
            "keywords": ["长按", "long press", "按住不放", "长击"],
            "handler": self.browser_long_press_tool
        }

        registry["browser_drag"] = {
            "name": "browser_drag",
            "description": "从起点坐标拖拽到终点坐标（按住不放移动，用于拖动滑块、移动元素、拖拽排序等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer", "description": "起点X坐标（截图像素），必填"},
                    "start_y": {"type": "integer", "description": "起点Y坐标（截图像素），必填"},
                    "end_x": {"type": "integer", "description": "终点X坐标（截图像素），必填"},
                    "end_y": {"type": "integer", "description": "终点Y坐标（截图像素），必填"},
                    "duration_ms": {"type": "integer", "description": "拖动持续时间（毫秒），默认600"}
                },
                "required": ["start_x", "start_y", "end_x", "end_y"]
            },
            "keywords": ["拖拽", "拖动", "drag", "滑动", "swipe", "拉动", "拖滑块"],
            "handler": self.browser_drag_tool
        }

        registry["browser_input_at"] = {
            "name": "browser_input_at",
            "description": "点击指定坐标处的输入框并输入文字。请先看截图确定输入框位置，再输出坐标和文字。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "输入框X坐标（截图像素），必填"},
                    "y": {"type": "integer", "description": "输入框Y坐标（截图像素），必填"},
                    "text": {"type": "string", "description": "要输入的文字，必填"},
                    "press_enter": {"type": "boolean", "description": "输入后是否按回车提交，默认false"}
                },
                "required": ["x", "y", "text"]
            },
            "keywords": ["输入文字", "input text", "填写输入框", "填写表单", "输入内容", "打字", "type text"],
            "handler": self.browser_input_at_tool
        }

        registry["browser_hover"] = {
            "name": "browser_hover",
            "description": "把鼠标悬停在指定坐标（触发下拉菜单、悬浮提示等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X坐标（截图像素），必填"},
                    "y": {"type": "integer", "description": "Y坐标（截图像素），必填"}
                },
                "required": ["x", "y"]
            },
            "keywords": ["悬停", "hover", "悬浮", "鼠标移上去"],
            "handler": self.browser_hover_tool
        }

        registry["browser_input"] = {
            "name": "browser_input",
            "description": "在当前页面的输入框中输入文字。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要输入的文字，必填"},
                    "enter": {"type": "boolean", "description": "输入后是否按回车，默认true"}
                },
                "required": ["text"]
            },
            "keywords": ["输入文字", "input text", "填写输入框"],
            "handler": self.browser_input_tool
        }

        registry["browser_scroll"] = {
            "name": "browser_scroll",
            "description": "滚动页面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "description": "方向：上/下/左/右，默认下"},
                    "distance": {"type": "integer", "description": "滚动距离，默认1300"}
                }
            },
            "keywords": ["滚动页面", "scroll", "上下滚动"],
            "handler": self.browser_scroll_tool
        }

        registry["browser_wait"] = {
            "name": "browser_wait",
            "description": ("等待页面加载/跳转/倒计时。调用后会立即通知用户「AI正在等待网页(Ns)」，"
                            "等待结束后自动截取最新页面截图给你。"
                            "适用于：页面正在跳转、验证码倒计时、异步加载、点击后结果未出现等场景。"
                            "一次调用完成「通知用户+等待+重看页面」，无需反复截图轮询浪费次数。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "integer", "description": "等待秒数（5-45），默认10"}
                }
            },
            "keywords": ["等待", "等待页面", "wait", "等待加载", "等一下", "倒计时", "页面跳转"],
            "handler": self.browser_wait_tool
        }

        registry["browser_zoom"] = {
            "name": "browser_zoom",
            "description": "缩放页面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scale": {"type": "number", "description": "缩放比例，默认1.5"}
                }
            },
            "keywords": ["缩放", "zoom", "放大缩小"],
            "handler": self.browser_zoom_tool
        }

        registry["browser_screenshot"] = {
            "name": "browser_screenshot",
            "description": "对当前页面截图。",
            "parameters": {
                "type": "object",
                "properties": {
                    "full_page": {"type": "boolean", "description": "是否截取整个页面，默认false"},
                    "zoom_factor": {"type": "number", "description": "截图缩放比例，可选"}
                }
            },
            "keywords": ["截图", "screenshot", "截取页面", "获取验证码"],
            "handler": self.browser_screenshot_tool
        }

        registry["browser_back"] = {
            "name": "browser_back",
            "description": "返回上一页。",
            "parameters": {
                "type": "object",
                "properties": {}
            },
            "keywords": ["返回", "go back", "上一页"],
            "handler": self.browser_back_tool
        }

        registry["browser_forward"] = {
            "name": "browser_forward",
            "description": "前进到下一页。",
            "parameters": {
                "type": "object",
                "properties": {}
            },
            "keywords": ["前进", "go forward", "下一页"],
            "handler": self.browser_forward_tool
        }

        registry["browser_tabs"] = {
            "name": "browser_tabs",
            "description": "查看所有标签页或切换标签页。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "标签页序号（从1开始），不填则查看所有标签页"}
                }
            },
            "keywords": ["标签页", "tabs", "切换标签", "查看标签"],
            "handler": self.browser_tabs_tool
        }

        registry["browser_close_tab"] = {
            "name": "browser_close_tab",
            "description": "关闭指定标签页。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "标签页序号（从1开始），必填"}
                },
                "required": ["index"]
            },
            "keywords": ["关闭标签", "close tab"],
            "handler": self.browser_close_tab_tool
        }

        registry["browser_close"] = {
            "name": "browser_close",
            "description": "关闭浏览器。",
            "parameters": {
                "type": "object",
                "properties": {}
            },
            "keywords": ["关闭浏览器", "close browser"],
            "handler": self.browser_close_tool
        }

        registry["browser_chat"] = {
            "name": "browser_chat",
            "description": "向当前页面的输入框发送对话内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要发送的内容，必填"}
                },
                "required": ["text"]
            },
            "keywords": ["对话", "发送消息", "chat"],
            "handler": self.browser_chat_tool
        }

        registry["browser_favorite_list"] = {
            "name": "browser_favorite_list",
            "description": "查看收藏夹列表。",
            "parameters": {
                "type": "object",
                "properties": {}
            },
            "keywords": ["收藏夹", "查看收藏", "favorites"],
            "handler": self.browser_favorite_list_tool
        }

        registry["browser_favorite_add"] = {
            "name": "browser_favorite_add",
            "description": "添加收藏。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "收藏名称，必填"},
                    "url": {"type": "string", "description": "收藏URL，必填"}
                },
                "required": ["name", "url"]
            },
            "keywords": ["添加收藏", "收藏链接", "add favorite"],
            "handler": self.browser_favorite_add_tool
        }

        registry["browser_favorite_delete"] = {
            "name": "browser_favorite_delete",
            "description": "删除收藏。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "收藏名称，必填"}
                },
                "required": ["name"]
            },
            "keywords": ["删除收藏", "取消收藏", "delete favorite"],
            "handler": self.browser_favorite_delete_tool
        }

        registry["browser_install"] = {
            "name": "browser_install",
            "description": "安装浏览器依赖。",
            "parameters": {
                "type": "object",
                "properties": {
                    "browser_type": {"type": "string", "description": "浏览器类型：chromium/firefox/webkit，默认chromium"}
                }
            },
            "keywords": ["安装浏览器", "install browser"],
            "handler": self.browser_install_tool
        }

        registry["open_page"] = {
            "name": "open_page",
            "description": "打开网页（简化版，使用内置浏览器）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要打开的网页URL，必填"}
                },
                "required": ["url"]
            },
            "keywords": ["打开网页", "open page", "浏览器打开"],
            "handler": self.open_page_tool
        }

        registry["screenshot_page"] = {
            "name": "screenshot_page",
            "description": "对当前网页截图（简化版）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "save_path": {"type": "string", "description": "保存路径（可选，默认保存到工作区）"}
                }
            },
            "keywords": ["截图", "screenshot", "截取网页", "获取验证码"],
            "handler": self.screenshot_page_tool
        }

        registry["close_page"] = {
            "name": "close_page",
            "description": "关闭浏览器（简化版）。",
            "parameters": {
                "type": "object",
                "properties": {}
            },
            "keywords": ["关闭浏览器", "close page", "关闭网页"],
            "handler": self.close_page_tool
        }

        # ---------- 好友管理 ----------
        registry["delete_friend"] = {
            "name": "delete_friend",
            "description": "删除好友。支持加入黑名单和双向删除。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "要删除的好友QQ号，必填"},
                    "temp_block": {"type": "boolean", "description": "是否加入黑名单，默认False"},
                    "temp_both_del": {"type": "boolean", "description": "是否双向删除，默认False"}
                },
                "required": ["user_id"]
            },
            "keywords": [
                "删除好友", "删好友", "delete friend", "移除好友", "拉黑",
                "解除好友关系", "断交", "删除联系人", "remove friend"
            ],
            "handler": self.delete_friend_tool
        }

        # ---------- 浏览器接管（v5.4.0）----------
        # 注意：本工具在 run_wyc_tool 的「免搜索白名单」内，
        # AI 可直接执行，无需先搜索（应急场景，来不及搜索）。
        registry["request_browser_takeover"] = {
            "name": "request_browser_takeover",
            "description": (
                "【应急】请求真人用户接管浏览器操作。"
                "仅在确认无法自动完成时使用，例如遇到验证码、滑块、拼图、"
                "短信验证码、人脸核身等需要真人的环节；"
                "普通操作失败（元素没点到、页面没加载完）请先重试或换思路，"
                "不要动不动就交给用户。"
                "调用后 AI 会暂停并等待，用户在 AstrBot 控制台的插件 WebUI 上操作，"
                "结束后会把最新截图返回给你继续处理。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "请求接管的原因，会展示给用户，例如：遇到滑块验证码，需要真人手动通过"
                    }
                },
                "required": ["reason"]
            },
            "keywords": [
                "接管", "用户接管", "人工接管", "请求接管", "转交用户", "交给用户",
                "验证码", "人机验证", "人机校验", "滑块", "拼图", "点选验证", "图形验证",
                "短信验证", "手机验证", "人脸识别", "实名认证", "无法自动完成", "自动不了",
                "卡住了", "卡住", "搞不定", "需要真人", "真人操作", "手动操作", "帮我操作",
                "takeover", "captcha", "human verification", "manual operation"
            ],
            "handler": self.request_browser_takeover_tool
        }

        return registry

    # 免搜索白名单 —— 这些工具绕开三步协议，AI 可直接用 run_wyc_tool 执行
    # 仅限应急场景（来不及搜索），务必保持极短
    DIRECT_CALL_TOOLS = {"request_browser_takeover"}

    # ==================== LLM 工具 ====================
    @filter.llm_tool(name="search_wyc_tools")
    async def search_wyc_tools(self, event: AstrMessageEvent, query: str) -> dict:
        """【必须优先使用】根据简短关键词搜索匹配的工具。请使用单个词语或短语（如"邮箱"、"禁言"、"发说说"），不要使用完整问句。
        
        Args:
            query(string): 搜索关键词（如"记忆"、"邮件"、"禁言"），必填
        """
        if not query or not query.strip():
            return {"status": "error", "message": "请提供搜索关键词（简短词语，如邮箱、禁言）"}
        available_tools = self._get_available_tools(event)
        query_lower = query.strip().lower()
        matched = []
        for name, meta in available_tools.items():
            keywords = meta.get("keywords", [])
            if (query_lower in name.lower() or
                query_lower in meta["description"].lower() or
                any(query_lower in kw.lower() for kw in keywords)):
                matched.append({
                    "name": name,
                    "description": meta["description"],
                    "parameters": meta["parameters"]
                })
        if not matched:
            return {"status": "success", "message": f"未找到与「{query}」相关的工具，可尝试其他关键词或使用 call_wyc_tools 查看全部可用工具。"}
        result_lines = [f"🔍 找到 {len(matched)} 个相关工具："]
        for tool in matched[:10]:
            result_lines.append(f"- {tool['name']}: {tool['description'][:60]}...")
        return {"status": "success", "message": "\n".join(result_lines), "tools": matched}

    @filter.llm_tool(name="call_wyc_tools")
    async def call_wyc_tools(self, event: AstrMessageEvent, **kwargs) -> dict:
        """返回当前可用的所有工具的简要列表（名称 + 描述）。此工具无需参数，仅当 search_wyc_tools 找不到合适工具时使用。"""
        available_tools = self._get_available_tools(event)
        tools_list = []
        for name, meta in available_tools.items():
            tools_list.append(f"- {name}: {meta['description']}")
        msg = "📦 可用工具列表：\n" + "\n".join(tools_list)
        return {"status": "success", "message": msg, "tool_names": list(available_tools.keys())}

    @filter.llm_tool(name="run_wyc_tool")
    async def run_wyc_tool(self, event: AstrMessageEvent, tool_name: str, tool_args: str, **kwargs) -> dict:
        """执行指定的工具。需要先通过 search_wyc_tools 或 call_wyc_tools 获取工具名称和参数格式。
        
        Args:
            tool_name(string): 要执行的工具名称，必填
            tool_args(string): 工具参数的 JSON 字符串，必填。例如：'{"content": "你好"}'
        """
        available_tools = self._get_available_tools(event)
        if not tool_name or tool_name not in available_tools:
            # 免搜索白名单：应急工具允许直接执行，不必先搜索（节省一轮往返）
            if tool_name in self.DIRECT_CALL_TOOLS and tool_name in self._tool_registry:
                available_tools = dict(available_tools)
                available_tools[tool_name] = self._tool_registry[tool_name]
            else:
                # 区分"不存在/已禁用"与"权限不足"，给出正确提示
                if tool_name and tool_name in self._tool_registry:
                    perm = self._get_tool_permission(tool_name)
                    if perm == "disabled":
                        return {"status": "error", "message": f"工具 {tool_name} 已被管理员禁用。"}
                    if perm in ("admin", "groupadmin") and not await self._check_tool_permission_async(event, perm):
                        return {"status": "error", "message": f"⛔ 工具 {tool_name} 需要「{PERMISSION_LABELS.get(perm, perm)}」权限，当前用户无权限。"}
                return {"status": "error", "message": f"无效的工具名称或工具未启用: {tool_name}。请先使用 search_wyc_tools 或 call_wyc_tools 获取可用工具。"}
        # 二次权限校验（防御 _get_available_tools 的判定偏差）
        perm = self._get_tool_permission(tool_name)
        if perm == "disabled":
            return {"status": "error", "message": f"工具 {tool_name} 已被管理员禁用。"}
        if perm in ("admin", "groupadmin") and not await self._check_tool_permission_async(event, perm):
            logger.warning(
                f"[QZoneTools] 拒绝越权调用: tool={tool_name} perm={perm} "
                f"sender={event.get_sender_id()} group={event.get_group_id()}"
            )
            return {"status": "error", "message": f"⛔ 工具 {tool_name} 需要「{PERMISSION_LABELS.get(perm, perm)}」权限，当前用户无权限。"}
        # 视觉模型门禁：浏览器类工具仅多模态模型可用
        if tool_name in VISION_REQUIRED_TOOLS:
            vision_err = await self._require_vision_model(event)
            if vision_err:
                return {"status": "error", "message": vision_err}
        # 接管冻结：用户操作浏览器期间，AI 的浏览器工具一律拒绝
        # （仅冻结浏览器类工具，发消息/记忆等其他工具不受影响）
        if tool_name in self._BROWSER_TOOL_NAMES and self.takeover_manager.is_active():
            if tool_name != "request_browser_takeover":
                return {
                    "status": "error",
                    "message": "⏸️ 浏览器当前正由用户接管操作中，AI 暂时无法操作浏览器。"
                              "请等待用户完成操作（工具会自动把最新截图返回给你）。",
                }
        try:
            if isinstance(tool_args, dict):
                args_dict = tool_args
            elif isinstance(tool_args, str):
                args_dict = json.loads(tool_args) if tool_args else {}
            else:
                return {"status": "error", "message": "参数格式错误，必须是 JSON 字符串或字典。"}
        except json.JSONDecodeError:
            return {"status": "error", "message": "参数格式错误，必须是有效的 JSON 字符串。"}
        handler = available_tools[tool_name]["handler"]
        # 校验必填参数
        tool_params = available_tools[tool_name].get("parameters", {})
        required = tool_params.get("required", [])
        missing = [p for p in required if p not in args_dict]
        if missing:
            param_desc = []
            props = tool_params.get("properties", {})
            for p in missing:
                desc = props.get(p, {}).get("description", p)
                param_desc.append(f"{p}({desc})")
            return {"status": "error", "message": f"缺少必填参数: {', '.join(param_desc)}。请参考工具定义传入正确参数。"}
        try:
            # 防御：若某个 handler 误写成 async generator，直接 await 会抛
            # TypeError: object async_generator can't be used in 'await' expression。
            # 这里主动探测并给出可诊断的错误，而不是抛到底层。
            if inspect.isasyncgenfunction(handler):
                logger.error(
                    f"[run_wyc_tool] 工具 {tool_name} 的 handler 是 async generator，"
                    "本插件只支持普通协程；请改为普通 async def + return。"
                )
                return {
                    "status": "error",
                    "message": f"工具 {tool_name} 实现方式不受支持（async generator），请联系插件作者。",
                }
            result = await handler(event, **args_dict)
            # 隐私模式下对返回文本脱敏（隐藏群号/QQ号）
            result = self._privacy_filter_result(result)
            # 浏览器操作增强显示：叠加操作图标 + 按需把截图推送给用户（纯代码实现）
            if isinstance(result, dict) and result.get("screenshot"):
                result = await self._apply_action_overlay(event, tool_name, args_dict, result)
            # 浏览器工具：无条件追加「接管提示」（不依赖失败检测，避免漏判）
            if isinstance(result, dict) and tool_name in self._BROWSER_TOOL_NAMES:
                if isinstance(result.get("message"), str):
                    result["message"] = self._with_takeover_hint(result["message"])
            # 如果结果包含截图路径，自动读取图片并返回 ImageContent
            # 这样 LLM 可以直接看到截图，无需再调 read_image
            if isinstance(result, dict) and "screenshot" in result:
                screenshot_path = result["screenshot"]
                if screenshot_path and os.path.isfile(screenshot_path):
                    try:
                        import base64 as _b64
                        with open(screenshot_path, "rb") as f:
                            img_data = f.read()
                        ext = os.path.splitext(screenshot_path)[1].lower()
                        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                                    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
                        mime_type = mime_map.get(ext, "image/png")
                        img_b64 = _b64.b64encode(img_data).decode("utf-8")
                        msg_text = result.get("message", "截图完成")
                        return mcp.types.CallToolResult(
                            content=[
                                mcp.types.TextContent(type="text", text=msg_text),
                                mcp.types.ImageContent(type="image", data=img_b64, mimeType=mime_type),
                            ]
                        )
                    except Exception as e:
                        logger.warning(f"[run_wyc_tool] 读取截图失败，回退到文本: {e}")
            return result
        except Exception as e:
            logger.error(f"[run_wyc_tool] 执行工具 {tool_name} 失败: {e}", exc_info=True)
            return {"status": "error", "message": f"工具执行出错: {_safe_error_msg(e)}"}

    # ==================== 浏览器接管（v5.5.0 非阻塞改造） ====================

    async def request_browser_takeover_tool(self, event: AstrMessageEvent, reason: str) -> Any:
        """
        请求真人用户接管浏览器（非阻塞）。

        为什么不再阻塞等待：
        - 框架对每次工具调用施加 asyncio.wait_for(tool_call_timeout) 硬超时
          （默认 120 秒，可通过全局设置调整），超时即强杀工具，接管时长被迫受限；
        - 即便不超时，长时间挂起也会占住这一轮 Agent，用户干等没有反馈。

        现在的做法：
        1. 工具**立即返回**，告诉 AI「已交给用户，你先待命，别再操作浏览器」；
        2. 用户在 WebUI 上操作，`TakeoverManager` 的双计时器负责收尾；
        3. 会话结束时由 `_wake_ai_after_takeover` 合成一个事件投回会话，
           把「用户已完成操作 + 最新截图」重新送给 AI，AI 自然接着干活。

        这样接管时长只受 WebUI 的上限配置约束，与框架工具超时彻底解耦。
        """
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器尚未启用，无法请求接管。"}

        # 已有活跃接管时直接拒绝，避免重复发起
        if self.takeover_manager.is_active():
            return {"status": "error", "message": "已有一个接管会话正在进行中。"}

        reason = (reason or "").strip() or "需要真人协助完成操作"
        cfg_max = self.takeover_manager.max_seconds

        # ---------- 1. 发系统消息通知用户 ----------
        notice = (
            "AI已将操作权限转移至WebUI，请前往Astrbot控制台打开"
            "\u201c更多LLM工具\u201d插件WEB UI进行接管操作，"
            f"本次操作将在{cfg_max}秒后超时"
        )
        try:
            await event.send(MessageChain().message(notice))
        except Exception as e:
            logger.warning(f"[Takeover] 通知用户失败: {e}")

        # ---------- 2. 建立接管会话 ----------
        try:
            try:
                viewport = await self.browser_supervisor.call("viewport_size")
            except Exception:
                viewport = (1280, 720)

            umo = event.unified_msg_origin

            async def _browser_call(method: str, **kwargs):
                return await self.browser_supervisor.call(method, **kwargs)

            def _resolve_group_id() -> str:
                try:
                    return str(event.get_group_id() or "")
                except Exception:
                    return ""

            sess = await self.takeover_manager.open(
                umo=umo,
                reason=reason,
                browser_call=_browser_call,
                viewport=viewport,
                sender_id=event.get_sender_id(),
                sender_name=event.get_sender_name(),
                group_id=_resolve_group_id(),
                self_id=event.get_self_id(),
                on_finished=self._wake_ai_after_takeover,
            )
        except Exception as e:
            logger.error(f"[Takeover] 开启会话失败: {e}", exc_info=True)
            return {"status": "error", "message": f"开启接管失败: {_safe_error_msg(e)}"}

        logger.info(
            f"[Takeover] AI 请求接管 {sess.session_id}，原因: {reason}，"
            f"上限 {cfg_max}s（非阻塞模式，工具立即返回）"
        )

        return {
            "status": "success",
            "message": (
                f"👤 已把浏览器操作权交给用户（原因：{reason}）。\n\n"
                f"用户正在 AstrBot 控制台的插件 WebUI 上操作，最长 {cfg_max} 秒。\n"
                "**请你现在就结束本轮回复，不要再调用任何浏览器工具**——"
                "此刻浏览器由用户独占，你的浏览器操作会被拒绝。\n"
                "用户操作完成后，系统会自动把最新截图和结束原因推送给你，"
                "届时再根据截图继续操作即可。"
            ),
        }

    # ==================== 接管结束后的 AI 唤醒 ====================

    _TAKEOVER_WAKE_NOTE = (
        "[浏览器接管结束] 用户在 WebUI 上完成了操作，操作权已交还给你。"
        "下方是结束时的浏览器截图，请根据截图判断用户的完成情况并继续操作。"
    )

    async def _wake_ai_after_takeover(self, sess) -> None:
        """
        接管结束回调：合成一个事件投回会话，把 AI 唤醒并交回截图。

        实现依据（AstrBot 4.28 官方机制）：
        - `CronMessageEvent` 是框架自带的合成事件，其 `is_wake = True`
          可直接通过唤醒检查阶段，无需 @ 或唤醒前缀；
        - 事件投进 `context.get_event_queue()` 后，由 EventBus → Pipeline
          正常跑完整流水线，等于"用户又发了条消息"，AI 会带着完整上下文继续；
        - 截图以 `Image` 组件挂在事件上，框架的 collect_initial_request
          会自动把它转成 image_urls 送给多模态模型，AI 能直接"看到"。
        """
        ended_reason = sess.end_reason or REASON_ABORTED
        note = REASON_TEXT.get(ended_reason, "接管已结束。")

        # ---------- 1. 收尾截图（必须在停止投屏之后取，保证是最终画面）----------
        shot = None
        try:
            if self.browser_supervisor:
                shot = await self.browser_supervisor.call("screenshot")
                if shot:
                    shot = self._convert_image(shot)
        except Exception as e:
            logger.warning(f"[Takeover] 结束后截图失败: {e}")

        # ---------- 2. 按结束原因生成给 AI 的文案 ----------
        if ended_reason == REASON_USER_END:
            detail = "用户已手动点「结束操作」，本次操作是用户主动完成的。"
        elif ended_reason == REASON_MAX_TIMEOUT:
            detail = (
                f"本次接管因达到时长上限（{sess.max_seconds}秒）由系统自动结束，"
                "**并非用户手动结束**。请检查用户是否已完成操作，若未完成可再次请求接管。"
            )
        elif ended_reason == REASON_IDLE_TIMEOUT:
            detail = (
                f"用户在 {sess.idle_seconds} 秒内没有任何操作，系统已自动结束接管并交还权限。"
                "请检查用户是否已完成操作。"
            )
        else:
            detail = note

        body = (
            f"{self._TAKEOVER_WAKE_NOTE}\n\n"
            f"结束原因：{note}\n"
            f"用户共执行 {sess.action_count} 次操作，持续 {sess.elapsed:.0f} 秒。\n"
            f"{detail}"
        )
        if not shot:
            body += "\n\n（截图获取失败，可用 browser_screenshot 重新取图）"

        logger.info(
            f"[Takeover] 唤醒 AI：会话={sess.session_id} 原因={ended_reason} "
            f"操作数={sess.action_count} 截图={'有' if shot else '无'}"
        )

        # ---------- 3. 合成事件并投回会话 ----------
        await self._dispatch_wake_event(sess, body, shot)

    async def _dispatch_wake_event(self, sess, body: str, shot: str | None) -> None:
        """合成一个"用户发来的消息"事件，投进事件队列唤醒 AI。"""
        try:
            from astrbot.core.cron.events import CronMessageEvent
            from astrbot.core.platform.message_session import MessageSession
            from astrbot.core.message.components import Image as _Image
            from astrbot.core.message.components import At as _At
            from astrbot.core.platform.message_type import MessageType as _MT
        except Exception as e:
            logger.error(f"[Takeover] 唤醒所需模块不可用，回退为直发消息: {e}")
            await self._fallback_notify(sess, body, shot)
            return

        try:
            session = MessageSession.from_str(sess.umo)
        except Exception as e:
            logger.error(f"[Takeover] 无法解析会话 {sess.umo}: {e}")
            await self._fallback_notify(sess, body, shot)
            return

        try:
            ev = CronMessageEvent(
                context=self.context,
                session=session,
                message=body,
                sender_id=sess.self_id or "astrbot",
                sender_name="接管完成",
                message_type=session.message_type,
            )

            # 还原真实说话人：唤醒检查会用 sender_id 判权限，AI 也靠它认人
            if sess.sender_id:
                ev.message_obj.sender.user_id = sess.sender_id
            ev.message_obj.group_id = sess.group_id or ""
            if sess.sender_name:
                ev.message_obj.sender.nickname = sess.sender_name

            # 群聊必须补一个 @机器人：WakingCheckStage 对群消息只在
            # 「@了机器人 / @全体 / 引用机器人」三种情况下判定为唤醒，
            # 否则直接 stop_event 丢掉。私聊不需要。
            if session.message_type == _MT.GROUP_MESSAGE and sess.self_id:
                try:
                    ev.message_obj.message.insert(0, _At(qq=sess.self_id))
                except Exception as e:
                    logger.warning(f"[Takeover] 补 @ 失败（群聊可能无法唤醒）: {e}")

            # 截图挂到事件上：框架会自动转成 image_urls，AI 能直接看图
            if shot and os.path.isfile(shot):
                try:
                    ev.message_obj.message.append(_Image.fromFileSystem(shot))
                except Exception as e:
                    logger.warning(f"[Takeover] 截图附加失败，仅回文案: {e}")

            self.context.get_event_queue().put_nowait(ev)
            logger.info(f"[Takeover] 唤醒事件已投递到 {sess.umo}")
        except Exception as e:
            logger.error(f"[Takeover] 投递唤醒事件失败: {e}", exc_info=True)
            await self._fallback_notify(sess, body, shot)

    async def _fallback_notify(self, sess, body: str, shot: str | None) -> None:
        """唤醒失败时的兜底：至少把结果和截图直接发给用户，不让信息丢失。"""
        try:
            chain = MessageChain()
            if shot and os.path.isfile(shot):
                try:
                    import base64 as _b64
                    with open(shot, "rb") as f:
                        chain = chain.image(_b64.b64encode(f.read()).decode())
                except Exception:
                    chain = MessageChain()
            await self.context.send_message(sess.umo, chain.message(body))
            logger.info("[Takeover] 已用兜底方式把接管结果发给用户")
        except Exception as e:
            logger.error(f"[Takeover] 兜底通知也失败: {e}", exc_info=True)

    def _init_action_overlay(self):
        """初始化操作图标叠加组件（图标随包分发，只读；产物写 data_dir）。"""
        try:
            # 图标位于插件安装目录（随包分发，只读），data_dir 仅存运行时产物
            pkg_resource_dir = Path(__file__).parent / "resource"
            if not pkg_resource_dir.exists():
                pkg_resource_dir = Path(self.data_dir) / "resource"
            self.action_overlay = ActionOverlay(self.data_dir, pkg_resource_dir, self.config)
            logger.info("[Browser] 操作图标叠加已初始化")
        except Exception as e:
            logger.warning(f"[Browser] 操作图标叠加初始化失败（功能不可用）: {e}")

    # ==================== 浏览器操作增强显示（v5.3.0） ====================

    # 每次浏览器操作后固定携带的提示（无条件，不依赖失败检测）
    # 目的：让 AI 在任何时刻都知道「接管」这根救命绳存在
    _TAKEOVER_HINT = (
        "\n\n💡 若遇到验证码 / 滑块 / 人机校验等无法自动完成的步骤，"
        "可用 request_browser_takeover 请求用户接管"
        "（工具名可直接用于 run_wyc_tool，无需搜索）"
    )

    # 动作速查（补全 browser_wait / browser_scroll —— 这两个最容易漏）
    _BROWSER_ACTION_HINT = (
        "📌 动作速查：点击 browser_click(x,y) · 输入 browser_input_at(x,y,text) · "
        "静候加载 browser_wait(seconds) · 翻页 browser_scroll(方向) · "
        "拖拽 browser_drag(...) · 长按 browser_long_press(x,y)"
    )

    def _with_takeover_hint(self, message: str) -> str:
        """给浏览器工具的返回消息追加接管提示与动作速查。"""
        try:
            if not isinstance(message, str) or not message:
                return message
            if "request_browser_takeover" in message:
                return message
            return message + self._TAKEOVER_HINT
        except Exception:
            return message

    # 会触发「操作展示」的浏览器工具（截图上叠加图标 + 强制推送）
    _BROWSER_TOOL_NAMES = {
        "open_page", "screenshot_page", "browser_search", "browser_visit",
        "browser_click", "browser_double_click", "browser_right_click",
        "browser_long_press", "browser_drag", "browser_input_at",
        "browser_input", "browser_hover", "browser_scroll", "browser_wait",
        "browser_zoom", "browser_screenshot", "browser_back", "browser_forward",
        "browser_tabs", "browser_chat",
    }

    # 工具名 → (动作名, 取坐标的方式)
    #   mode：xy=单点 / xyxy=起止两点 / None=无坐标（仅推送不叠加）
    _ACTION_TOOL_MAP = {
        "browser_click": ("click", "xy"),
        "browser_double_click": ("double_click", "xy"),
        "browser_right_click": ("right_click", "xy"),
        "browser_hover": ("hover", "xy"),
        "browser_long_press": ("long_press", "xy"),
        "browser_input_at": ("input_at", "xy"),
        "browser_drag": ("drag", "xyxy"),
        "browser_scroll": ("scroll", None),
        "browser_input": ("input", None),
        "browser_search": ("click", None),
        "browser_visit": ("click", None),
        "open_page": ("click", None),
        "browser_back": ("click", None),
        "browser_forward": ("click", None),
    }

    def _extract_action_points(self, tool_name: str, args: dict) -> list:
        """从工具参数中提取操作点坐标（像素，左上原点）。"""
        spec = self._ACTION_TOOL_MAP.get(tool_name)
        if not spec:
            return []
        mode = spec[1]
        try:
            if mode == "xy":
                return [(int(args.get("x")), int(args.get("y")))]
            if mode == "xyxy":
                return [
                    (int(args.get("start_x")), int(args.get("start_y"))),
                    (int(args.get("end_x")), int(args.get("end_y"))),
                ]
        except (TypeError, ValueError):
            return []
        return []

    def _action_label(self, tool_name: str, args: dict) -> str:
        """生成给用户看的一句话操作说明。"""
        action = self._ACTION_TOOL_MAP.get(tool_name, ("", None))[0]
        base = {
            "click": "点击", "double_click": "双击", "right_click": "右键点击",
            "hover": "悬停", "long_press": "长按", "input_at": "输入",
            "input": "输入", "drag": "拖动", "scroll": "滚动",
        }.get(action, "操作")
        try:
            if action in ("input", "input_at"):
                txt = str(args.get("text", ""))[:20]
                return f"{base}「{txt}」" if txt else base
            if tool_name == "browser_scroll":
                d = args.get("direction", "下")
                return f"向{d}滚动"
            if tool_name == "browser_search":
                return f"搜索「{str(args.get('keyword', ''))[:20]}」"
            if tool_name in ("browser_visit", "open_page"):
                return f"打开 {str(args.get('url', ''))[:60]}"
        except Exception:
            pass
        return base

    async def _apply_action_overlay(self, event: AstrMessageEvent, tool_name: str,
                                    args: dict, result: dict) -> dict:
        """
        浏览器操作增强显示的统一处理（在 run_wyc_tool 处收口，覆盖所有工具）：

        1. 若开启「操作增强显示」→ 在截图上叠加操作图标，标出 AI 操作的位置；
        2. 若开启「始终显示AI操作」→ 不管 AI 是否愿意，强制把截图推送给当前会话。

        全程不依赖提示词，纯代码实现；任何异常都不影响工具正常返回。
        """
        shot = result.get("screenshot")
        if not shot or not os.path.isfile(shot):
            return result

        # 仅对浏览器类工具生效（其他工具如代码生成图片不应被强行推送）
        if tool_name not in self._BROWSER_TOOL_NAMES:
            return result

        action_tool = tool_name in self._ACTION_TOOL_MAP

        # ---- 1. 叠加操作图标 ----
        if self.browser_action_overlay_enabled and action_tool and self.action_overlay:
            try:
                points = self._extract_action_points(tool_name, args or {})
                if points:
                    viewport = None
                    try:
                        vs = self.config.get("viewport_size", {}) or {}
                        vw = int(vs.get("width", 0) or 0)
                        vh = int(vs.get("height", 0) or 0)
                        if vw > 0 and vh > 0:
                            viewport = (vw, vh)
                    except Exception:
                        viewport = None
                    annotated = self.action_overlay.annotate(
                        shot, self._ACTION_TOOL_MAP[tool_name][0], points,
                        viewport=viewport,
                    )
                    if annotated and annotated != shot and os.path.isfile(annotated):
                        result["screenshot"] = annotated
                        shot = annotated
            except Exception as e:
                logger.warning(f"[ActionOverlay] 标注截图失败（不影响主流程）: {e}")

        # ---- 2. 强制把截图推送给用户 ----
        if self.browser_always_show_action:
            try:
                label = self._action_label(tool_name, args or {})
                await self._push_browser_action(event, shot, label)
            except Exception as e:
                logger.warning(f"[ActionOverlay] 推送截图失败（不影响主流程）: {e}")

        return result

    async def _push_browser_action(self, event: AstrMessageEvent, image_path: str,
                                   label: str = "") -> bool:
        """把浏览器操作截图直接发送到当前会话（代码强制，不经由 LLM）。"""
        if not image_path or not os.path.isfile(image_path):
            return False
        client = await self._get_client(event)
        if not client:
            logger.warning("[ActionOverlay] 无法获取 client，跳过推送")
            return False

        try:
            import base64 as _b64
            with open(image_path, "rb") as f:
                img_b64 = _b64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.warning(f"[ActionOverlay] 截图读取失败: {e}")
            return False

        # 组织消息：可选的说明文字 + 图片
        text = f"🖥️ AI 操作：{label}" if (self.browser_action_push_text and label) else ""
        message = ""
        if text:
            message += text + "\n"
        message += f"[CQ:image,file=base64://{img_b64}]"

        try:
            group_id = event.get_group_id()
            if group_id:
                await self._call(client, 'send_group_msg', group_id=int(group_id), message=message)
            else:
                await self._call(client, 'send_private_msg',
                                 user_id=int(event.get_sender_id()), message=message)
            logger.info(f"[ActionOverlay] 已推送操作截图: {label or image_path}")
            return True
        except Exception as e:
            logger.warning(f"[ActionOverlay] 发送失败: {e}")
            return False

    # ==================== WebUI API ====================
    def _register_page_routes(self):
        try:
            self.context.register_web_api(f"/{PLUGIN_NAME}/get_config", self.handle_get_config, ["GET"], "获取配置")
            self.context.register_web_api(f"/{PLUGIN_NAME}/save_config", self.handle_save_config, ["POST"], "保存配置")
            self.context.register_web_api(f"/{PLUGIN_NAME}/memories", self.handle_get_memories, ["GET"], "获取记忆")
            self.context.register_web_api(f"/{PLUGIN_NAME}/delete_memory", self.handle_delete_memory, ["POST"], "删除记忆")
            self.context.register_web_api(f"/{PLUGIN_NAME}/add_memory", self.handle_add_memory, ["POST"], "添加记忆")
            self.context.register_web_api(f"/{PLUGIN_NAME}/update_memory", self.handle_update_memory, ["POST"], "更新记忆")
            # 记忆增强管理（v5.3.0）
            self.context.register_web_api(f"/{PLUGIN_NAME}/memory_stats", self.handle_memory_stats, ["GET"], "记忆统计")
            self.context.register_web_api(f"/{PLUGIN_NAME}/memory_users", self.handle_memory_users, ["GET"], "记忆用户列表")
            self.context.register_web_api(f"/{PLUGIN_NAME}/memory_tags", self.handle_memory_tags, ["GET"], "记忆标签列表")
            self.context.register_web_api(f"/{PLUGIN_NAME}/memory_batch", self.handle_memory_batch, ["POST"], "记忆批量操作")
            self.context.register_web_api(f"/{PLUGIN_NAME}/memory_duplicates", self.handle_memory_duplicates, ["GET"], "记忆重复检测")
            self.context.register_web_api(f"/{PLUGIN_NAME}/memory_dedupe", self.handle_memory_dedupe, ["POST"], "记忆一键去重")
            self.context.register_web_api(f"/{PLUGIN_NAME}/memory_export", self.handle_memory_export, ["GET"], "导出记忆")
            self.context.register_web_api(f"/{PLUGIN_NAME}/memory_import", self.handle_memory_import, ["POST"], "导入记忆")
            # 浏览器接管（v5.4.0）
            self.context.register_web_api(f"/{PLUGIN_NAME}/takeover_status", self.handle_takeover_status, ["GET"], "接管状态")
            self.context.register_web_api(f"/{PLUGIN_NAME}/takeover_stream", self.handle_takeover_stream, ["GET"], "接管画面流(SSE)")
            self.context.register_web_api(f"/{PLUGIN_NAME}/takeover_action", self.handle_takeover_action, ["POST"], "接管操作注入")
            self.context.register_web_api(f"/{PLUGIN_NAME}/takeover_end", self.handle_takeover_end, ["POST"], "结束接管")
            self.context.register_web_api(f"/{PLUGIN_NAME}/takeover_probe", self.handle_takeover_probe, ["POST"], "接管画面带宽探测")
            self.context.register_web_api(f"/{PLUGIN_NAME}/tabs", self.handle_tabs, ["GET"], "标签页列表")
            self.context.register_web_api(f"/{PLUGIN_NAME}/tab_action", self.handle_tab_action, ["POST"], "标签页操作")
            self.context.register_web_api(f"/{PLUGIN_NAME}/browser_memory", self.handle_browser_memory, ["GET"], "浏览器与服务器内存状态")
            self.context.register_web_api(f"/{PLUGIN_NAME}/install_deps", self.handle_install_deps, ["POST"], "安装浏览器系统依赖")
            self.context.register_web_api(f"/{PLUGIN_NAME}/scheduled_messages", self.handle_get_scheduled_messages, ["GET"], "获取定时消息")
            self.context.register_web_api(f"/{PLUGIN_NAME}/add_scheduled_message", self.handle_add_scheduled_message, ["POST"], "添加定时消息")
            self.context.register_web_api(f"/{PLUGIN_NAME}/cancel_scheduled_message", self.handle_cancel_scheduled_message, ["POST"], "取消定时消息")
            self.context.register_web_api(f"/{PLUGIN_NAME}/workspace_files", self.handle_workspace_files, ["GET"], "获取工作区文件列表")
            self.context.register_web_api(f"/{PLUGIN_NAME}/workspace_file_content", self.handle_workspace_file_content, ["GET"], "获取工作区文件内容")
            self.context.register_web_api(f"/{PLUGIN_NAME}/workspace_delete_file", self.handle_workspace_delete_file, ["POST"], "删除工作区文件")
            self.context.register_web_api(f"/{PLUGIN_NAME}/workspace_upload_file", self.handle_workspace_upload_file, ["POST"], "上传文件到工作区")
            logger.info("[QZoneTools] WebUI API 已注册")
        except Exception as e:
            logger.error(f"[QZoneTools] 注册失败: {e}")

    async def handle_get_config(self):
        cfg = dict(self.config)
        safe = {}
        for k, v in cfg.items():
            if k in SENSITIVE_FIELDS:
                # 脱敏：仅回传掩码，不回传真实密钥
                safe[k] = MASK_PLACEHOLDER if v else ""
            else:
                safe[k] = v
        # tool_permissions 已迁移到独立文件存储，需单独回传（见 _load_tool_permissions）
        # 回传"生效档位"而非原始表：敏感工具未显式配置时实际生效为 admin，
        # 若回传空表前端会全部显示为「全局」，造成"设置没生效"的误判（issue #10）。
        effective_perms = {}
        for name in self._tool_registry:
            effective_perms[name] = self._get_tool_permission(name)
        effective_perms.update(self._tool_permissions)  # 显式配置优先展示
        safe["tool_permissions"] = effective_perms
        return jsonify({"success": True, "config": safe})

    async def handle_save_config(self):
        try:
            data = await request.get_json()
            new_config = data.get("config", {})
            if not isinstance(new_config, dict):
                return jsonify({"success": False, "error": "格式错误"})

            # tool_permissions 单独落盘（不写入主配置，避免被 AstrBot 完整性检查清空）
            if "tool_permissions" in new_config:
                raw_perms = new_config.pop("tool_permissions")
                if isinstance(raw_perms, dict):
                    cleaned = {
                        k: v for k, v in raw_perms.items()
                        if isinstance(k, str) and v in PERMISSION_LEVELS
                    }
                    if self._save_tool_permissions(cleaned):
                        self._tool_permissions = cleaned
                        logger.info(f"[QZoneTools] 权限设置已保存（{len(cleaned)} 条）")
                    else:
                        return jsonify({"success": False, "error": "权限设置写入失败，请查看日志"})
                else:
                    logger.warning("[WebUI] tool_permissions 格式异常，已忽略")

            # 白名单过滤：允许已声明配置项 + 所有 enable_* 工具开关
            safe_config = {}
            for k, v in new_config.items():
                if k in CONFIG_SAVE_WHITELIST or k in self._tool_enable_keys:
                    # 敏感字段：回传掩码时表示"未修改"，保留原值
                    if k in SENSITIVE_FIELDS and v == MASK_PLACEHOLDER:
                        continue
                    safe_config[k] = v
                else:
                    logger.warning(f"[WebUI] 忽略未知配置项: {k}")
            # 合并配置（保留未传入的字段）
            self.config.update(safe_config)
            if hasattr(self.config, 'save_config') and callable(self.config.save_config):
                self.config.save_config()
            # 重新加载运行时状态
            self.tool_enabled = self._load_tool_enabled_flags()
            self.workspace_banned_patterns = self.config.get("workspace_banned_patterns", [])
            self.ssrf_blocked_urls = self.config.get("ssrf_blocked_urls", [])
            self.ssrf_custom_blocked_ranges = self.config.get("ssrf_custom_blocked_ranges", [])
            self.resolve_image_restricted = self.config.get("resolve_image_restricted", True)
            self.run_python_sandbox_enabled = self.config.get("run_python_sandbox_enabled", False)
            self.docker_container_name = self.config.get("docker_container_name", "napcat")
            # 隐私模式 / 工作区开关可能变化
            self.workspace_enabled = self.config.get("workspace_enabled", True)
            # 浏览器操作展示开关（热更新，无需重启）
            self.browser_always_show_action = self.config.get("browser_always_show_action", False)
            self.browser_action_overlay_enabled = self.config.get("browser_action_overlay_enabled", False)
            self.browser_action_push_text = self.config.get("browser_action_push_text", True)
            # 操作图标叠加组件懒初始化（插件加载时可能尚未就绪）
            if self.browser_action_overlay_enabled and not self.action_overlay:
                self._init_action_overlay()
            # ★ 浏览器性能阈值热更新（关键修复）：
            #   supervisor 是在插件初始化时构造的，其内存阈值/空闲超时/监控间隔
            #   都是构造时读一次就固定。若这里不重新赋值，WebUI 改了配置也不生效，
            #   表现为「进度条上的阈值一直显示旧值」。
            self._sync_supervisor_config()
            return jsonify({"success": True, "message": "配置已保存"})
        except Exception as e:
            logger.error(f"[WebUI] 保存配置失败: {_safe_error_msg(e)}", exc_info=True)
            return jsonify({"success": False, "error": "保存失败，请查看日志"})

    def _sync_supervisor_config(self) -> None:
        """把最新的浏览器性能配置同步给已存在的 supervisor（配置热更新）。

        supervisor 的阈值都在构造时读取，插件运行中改配置必须显式回写，
        否则 WebUI 改了「服务器内存占用上限 / 浏览器内存上限 / 空闲超时 /
        监控间隔」都不会生效。
        """
        if not self.browser_supervisor:
            return
        try:
            sup = self.browser_supervisor
            sup.max_memory_percent = int(self.config.get("max_memory_percent", 90) or 90)
            sup.idle_timeout = int(self.config.get("idle_timeout", 300) or 300)
            sup.monitor_interval = float(self.config.get("monitor_interval", 10) or 10)
            sup.browser_memory_limit_mb = int(
                self.config.get("browser_memory_limit_mb", 0) or 0
            )
            logger.info(
                f"[Browser] 性能阈值已热更新: 服务器上限={sup.max_memory_percent}% "
                f"浏览器上限={sup.browser_memory_limit_mb}MB "
                f"空闲超时={sup.idle_timeout}s 监控间隔={sup.monitor_interval}s"
            )
        except Exception as e:
            logger.warning(f"[Browser] 性能阈值热更新失败: {_safe_error_msg(e)}")

    async def handle_get_memories(self):
        try:
            keyword = request.args.get("keyword", "")
            limit = int(request.args.get("limit", 99999))
            memories = await self.memory_manager.get_memories(keyword=keyword if keyword else None, limit=limit)
            return jsonify({"success": True, "memories": memories})
        except Exception as e:
            logger.error(f"[WebUI] 获取记忆失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_delete_memory(self):
        try:
            data = await request.get_json()
            if not data or "memory_id" not in data:
                return jsonify({"success": False, "error": "缺少 memory_id"})
            ok = await self.memory_manager.delete_memory(data["memory_id"])
            return jsonify({"success": ok, "message": "已删除" if ok else "未找到记忆"})
        except Exception as e:
            logger.error(f"[WebUI] 删除记忆失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_add_memory(self):
        try:
            data = await request.get_json()
            if not data:
                return jsonify({"success": False, "error": "请求数据为空"})
            user_id = str(data.get("user_id", "")).strip()
            content_val = data.get("content", "").strip()
            tags = data.get("tags", [])
            importance = int(data.get("importance", 5))
            if not user_id or not content_val:
                return jsonify({"success": False, "error": "缺少 user_id 或 content"})
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
            memory_id = await self.memory_manager.add_memory(user_id, content_val, tags, importance)
            return jsonify({"success": True, "memory_id": memory_id, "message": "记忆已添加"})
        except Exception as e:
            logger.error(f"[WebUI] 添加记忆失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_update_memory(self):
        try:
            data = await request.get_json()
            if not data or "memory_id" not in data:
                return jsonify({"success": False, "error": "缺少 memory_id"})
            memory_id = data["memory_id"]
            content_val = data.get("content")
            tags = data.get("tags")
            importance = data.get("importance")
            if content_val is not None:
                content_val = content_val.strip()
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
            ok = await self.memory_manager.update_memory(memory_id, content=content_val, tags=tags, importance=importance)
            return jsonify({"success": ok, "message": "已更新" if ok else "未找到记忆"})
        except Exception as e:
            logger.error(f"[WebUI] 更新记忆失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    # ---------- 记忆增强管理（v5.3.0） ----------

    @staticmethod
    def _parse_id_list(raw) -> List[str]:
        """把前端传来的 id 列表规整成字符串列表。"""
        if isinstance(raw, str):
            return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return []

    async def handle_memory_stats(self):
        try:
            stats = await self.memory_manager.get_stats()
            return jsonify({"success": True, "stats": stats})
        except Exception as e:
            logger.error(f"[WebUI] 记忆统计失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_memory_users(self):
        try:
            users = await self.memory_manager.list_users()
            return jsonify({"success": True, "users": users})
        except Exception as e:
            logger.error(f"[WebUI] 获取记忆用户失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_memory_tags(self):
        try:
            tags = await self.memory_manager.list_tags()
            return jsonify({"success": True, "tags": tags})
        except Exception as e:
            logger.error(f"[WebUI] 获取记忆标签失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_memory_batch(self):
        """批量操作：delete=删除 / importance=改重要度 / tag=追加标签。"""
        try:
            data = await request.get_json() or {}
            action = data.get("action", "")
            ids = self._parse_id_list(data.get("memory_ids"))
            if not ids:
                return jsonify({"success": False, "error": "请先选择要操作的记忆"})

            if action == "delete":
                n = await self.memory_manager.batch_delete(ids)
                return jsonify({"success": True, "affected": n, "message": f"已删除 {n} 条记忆"})
            if action == "importance":
                imp = int(data.get("importance", 5))
                n = await self.memory_manager.batch_update_importance(ids, imp)
                return jsonify({"success": True, "affected": n, "message": f"已调整 {n} 条记忆的重要度为 {imp}"})
            if action == "tag":
                tag = data.get("tag", "")
                n = await self.memory_manager.batch_add_tag(ids, tag)
                return jsonify({"success": True, "affected": n, "message": f"已为 {n} 条记忆添加标签「{tag}」"})
            return jsonify({"success": False, "error": f"未知操作: {action}"})
        except Exception as e:
            logger.error(f"[WebUI] 记忆批量操作失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_memory_duplicates(self):
        try:
            dups = await self.memory_manager.find_duplicates()
            total = sum(d["count"] - 1 for d in dups)
            return jsonify({"success": True, "groups": dups, "removable": total})
        except Exception as e:
            logger.error(f"[WebUI] 重复检测失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_memory_dedupe(self):
        try:
            n = await self.memory_manager.deduplicate()
            return jsonify({
                "success": True, "removed": n,
                "message": f"已清理 {n} 条重复记忆" if n else "没有发现重复记忆",
            })
        except Exception as e:
            logger.error(f"[WebUI] 一键去重失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_memory_export(self):
        try:
            payload = await self.memory_manager.export_all()
            return jsonify({"success": True, "data": payload})
        except Exception as e:
            logger.error(f"[WebUI] 导出记忆失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_memory_import(self):
        try:
            data = await request.get_json() or {}
            payload = data.get("data")
            if payload is None:
                return jsonify({"success": False, "error": "缺少导入数据"})
            added, skipped = await self.memory_manager.import_merge(payload)
            return jsonify({
                "success": True, "added": added, "skipped": skipped,
                "message": f"导入完成：新增 {added} 条，跳过 {skipped} 条",
            })
        except Exception as e:
            logger.error(f"[WebUI] 导入记忆失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    # ---------- 浏览器接管（v5.4.0） ----------

    async def handle_takeover_status(self):
        """查询接管状态与浏览器可用性。"""
        try:
            # 浏览器是否真的在跑：以 supervisor 内部句柄为准（那是权威状态）。
            # 注意不要用 takeover 会话是否存在来判断，两者独立。
            browser_running = False
            memory = None
            if self.browser_supervisor:
                try:
                    browser_running = bool(getattr(self.browser_supervisor, "browser", None))
                except Exception:
                    browser_running = False
                try:
                    memory = self.browser_supervisor.memory_snapshot()
                except Exception as e:
                    logger.debug(f"[Takeover] 内存快照失败: {e}")
                # 交叉校验：句柄在但进程没了（外部被杀），同样算「未运行」
                if browser_running and memory is not None:
                    if not memory.get("browser_procs"):
                        browser_running = False

            mgr = self.takeover_manager
            sess = mgr.session
            payload = {
                "success": True,
                "browser_enabled": bool(self.browser_supervisor) and self.config.get("enabled", True),
                "browser_running": browser_running,
                "active": mgr.is_active(),
                "session": sess.status_payload() if sess else None,
                "max_seconds": mgr.max_seconds,
                "idle_seconds": mgr.idle_seconds,
            }
            if sess and not sess.finished.is_set():
                payload["reason"] = sess.reason
                payload["viewport"] = list(sess.viewport)
                payload["frame_size"] = list(sess.frame_size)

            # 内存快照（供 WebUI 进度条）+ 最近一次自动关闭记录（供提示用户）
            if memory is not None:
                memory["browser_running"] = browser_running
                payload["memory"] = memory
            return jsonify(payload)
        except Exception as e:
            logger.error(f"[Takeover] 查询状态失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    # ==================== 标签页管理（v5.6.3） ====================

    async def handle_tabs(self):
        """标签页列表。"""
        if not self.browser_supervisor:
            return jsonify({"success": False, "error": "浏览器未启用"})
        try:
            tabs = await self.browser_supervisor.call("tabs_detail")
            mem = None
            try:
                mem = self.browser_supervisor.memory_snapshot()
            except Exception:
                pass
            return jsonify({
                "success": True,
                "tabs": tabs or [],
                "memory": mem,
                "running": bool(getattr(self.browser_supervisor, "browser", None)),
            })
        except Exception as e:
            logger.error(f"[Tabs] 获取标签页失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_tab_action(self):
        """标签页操作：switch / close / new / close_all。

        权限方向（v5.6.3 修正）：
          - **接管进行中** → 是「真人用户在操作」，此时 WebUI 的标签页管理
            正是给用户用的，必须放行。
          - **未接管** → 由 AI 独占控制，用户在 WebUI 上改标签页会打断 AI，
            这里也放行但仅作提示（用户对浏览器有最终处置权）。
        换句话说：AI 侧的工具调用会被接管拦下（那条在工具入口判断），
        WebUI 侧的标签页管理**任何时候都可用**。
        """
        if not self.browser_supervisor:
            return jsonify({"success": False, "error": "浏览器未启用"})

        try:
            data = await request.get_json() or {}
            action = str(data.get("action", "")).strip()
            if action == "switch":
                idx = int(data.get("index", -1))
                msg = await self.browser_supervisor.call("switch_tab", index=idx)
                ok = not msg or "无效" not in str(msg)
                return jsonify({"success": ok, "message": msg or f"已切换到标签页 {idx + 1}",
                                "error": None if ok else str(msg)})
            if action == "close":
                idx = int(data.get("index", -1))
                msg = await self.browser_supervisor.call("close_tab", index=idx)
                ok = "无效" not in str(msg)
                return jsonify({"success": ok, "message": msg,
                                "error": None if ok else str(msg)})
            if action == "new":
                url = str(data.get("url", "")).strip()
                msg = await self.browser_supervisor.call("new_tab", url=url)
                ok = "失败" not in str(msg)
                return jsonify({"success": ok, "message": msg,
                                "error": None if ok else str(msg)})
            if action == "close_all":
                msg = await self.browser_supervisor.call("close_all_tabs", keep_one=True)
                return jsonify({"success": True, "message": msg})
            return jsonify({"success": False, "error": f"未知操作: {action}"})
        except Exception as e:
            logger.error(f"[Tabs] 标签页操作失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_browser_memory(self):
        """浏览器与服务器内存状态（供进度条）。"""
        if not self.browser_supervisor:
            return jsonify({"success": False, "error": "浏览器未启用"})
        try:
            snap = self.browser_supervisor.memory_snapshot()
            snap["success"] = True
            return jsonify(snap)
        except Exception as e:
            logger.error(f"[Memory] 内存快照失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_install_deps(self):
        """安装浏览器所需的系统依赖库。

        webkit / firefox 依赖 gtk / gstreamer 等系统库，缺失时报
        「Host system is missing dependencies to run browsers」。
        这里提供 WebUI 一键安装入口，省去用户进容器敲命令。
        """
        try:
            data = await request.get_json() or {}
            browser = str(data.get("browser", "") or
                          self.config.get("browser_type", "chromium"))
            if browser not in ("chromium", "firefox", "webkit"):
                return jsonify({"success": False, "error": f"不支持的浏览器: {browser}"})

            from .core.downloader import BrowserDownloader

            ok, msg = await BrowserDownloader.install_system_deps(browser)
            if not ok:
                return jsonify({"success": False, "error": msg})

            # 装完立刻验证一次，把结果一并返回
            verified = await BrowserDownloader.verify_browser(
                browser, retries=1, auto_install_deps=False,
                browsers_dir=Path(self.data_dir) / "browsers",
            )
            return jsonify({
                "success": True,
                "message": msg + ("，浏览器已可用 ✓" if verified else "，但启动验证仍失败"),
                "verified": verified,
            })
        except Exception as e:
            logger.error(f"[Browser] 安装系统依赖失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_takeover_stream(self):
        """SSE 画面流：持续推送帧 + 状态。"""
        from starlette.responses import StreamingResponse

        mgr = self.takeover_manager
        if not mgr.session:
            return jsonify({"success": False, "error": "当前没有进行中的接管会话"})

        viewer_id = uuid.uuid4().hex[:10]
        return StreamingResponse(
            mgr.stream_for(viewer_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    async def handle_takeover_action(self):
        """接收前端操作并注入浏览器。"""
        try:
            data = await request.get_json() or {}
            sess = self.takeover_manager.session
            if not sess or sess.finished.is_set():
                return jsonify({"success": False, "error": "接管已结束"})

            action = data.get("action", "")
            payload = data.get("data") or {}
            result = await sess.apply_action(action, payload)
            return jsonify({
                "success": result.get("ok", False),
                "error": result.get("error"),
                "action_count": sess.action_count,
                "remaining_idle": round(sess.remaining_idle, 1),
            })
        except Exception as e:
            logger.error(f"[Takeover] 操作注入失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_takeover_end(self):
        """用户点击「结束操作」。"""
        try:
            mgr = self.takeover_manager
            if not mgr.session or mgr.session.finished.is_set():
                return jsonify({"success": False, "error": "当前没有进行中的接管会话"})
            count = mgr.session.action_count
            await mgr.end(REASON_USER_END)
            return jsonify({
                "success": True,
                "message": "已结束接管，操作权交还 AI",
                "action_count": count,
            })
        except Exception as e:
            logger.error(f"[Takeover] 结束接管失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_takeover_probe(self):
        """
        带宽探测：返回指定大小的随机数据，供前端测实际吞吐。

        前端据此推算可持续的单帧大小，再取 80% 作为实际预算，
        映射为 JPEG quality + 最大宽度，实现「自动检测最佳画质」。
        """
        try:
            data = await request.get_json() or {}
            try:
                size_kb = int(data.get("size_kb", 100))
            except (TypeError, ValueError):
                size_kb = 100
            size_kb = max(1, min(size_kb, 2048))
            blob = os.urandom(size_kb * 1024)
            import base64 as _b64
            return jsonify({
                "success": True,
                "size_kb": size_kb,
                "payload": _b64.b64encode(blob).decode("ascii"),
            })
        except Exception as e:
            logger.error(f"[Takeover] 带宽探测失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_get_scheduled_messages(self):
        try:
            tasks = [t for t in self.scheduled_tasks.values() if not t.cancelled and not t.completed]
            result = []
            for t in tasks:
                result.append({
                    "task_id": t.task_id,
                    "target_id": t.target_id,
                    "message": t.message,
                    "send_time": t.send_time.isoformat(),
                    "chat_type": t.chat_type
                })
            return jsonify({"success": True, "tasks": result})
        except Exception as e:
            logger.error(f"[WebUI] 获取定时消息失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_add_scheduled_message(self):
        try:
            data = await request.get_json()
            if not data:
                return jsonify({"success": False, "error": "请求数据为空"})
            target_id = str(data.get("target_id", "")).strip()
            message = data.get("message", "").strip()
            send_time_str = data.get("send_time", "").strip()
            chat_type = data.get("chat_type", "group").strip()
            if not target_id or not message or not send_time_str:
                return jsonify({"success": False, "error": "参数缺失"})
            if not target_id.isdigit():
                return jsonify({"success": False, "error": "目标ID必须是纯数字"})
            parsed_time = self._parse_time(send_time_str)
            if not parsed_time:
                return jsonify({"success": False, "error": "无法解析时间格式"})
            if parsed_time <= datetime.now():
                return jsonify({"success": False, "error": "时间已过"})
            client = await self._get_client()
            if not client:
                return jsonify({"success": False, "error": "无法获取QQ客户端"})
            task_id = str(uuid.uuid4())[:8]
            task = ScheduledTask(
                task_id=task_id, target_id=target_id, message=message,
                send_time=parsed_time, chat_type=chat_type
            )
            self.scheduled_tasks[task_id] = task
            try:
                await self.db_manager.save_scheduled_task(task.to_dict())
            except Exception as e:
                logger.error(f"[WebUI] 定时消息持久化失败: {e}")
            delay_seconds = (parsed_time - datetime.now()).total_seconds()
            async_task = asyncio.create_task(self._execute_scheduled_task(task_id, delay_seconds))
            self.running_tasks[task_id] = async_task
            return jsonify({"success": True, "task_id": task_id, "message": "定时消息已创建"})
        except Exception as e:
            logger.error(f"[WebUI] 添加定时消息失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_cancel_scheduled_message(self):
        try:
            data = await request.get_json()
            if not data or "task_id" not in data:
                return jsonify({"success": False, "error": "缺少 task_id"})
            task_id = data["task_id"]
            if task_id not in self.scheduled_tasks:
                return jsonify({"success": False, "error": "任务不存在"})
            task = self.scheduled_tasks[task_id]
            task.cancelled = True
            if task_id in self.running_tasks:
                self.running_tasks[task_id].cancel()
                del self.running_tasks[task_id]
            del self.scheduled_tasks[task_id]
            try:
                await self.db_manager.delete_scheduled_task(task_id)
            except Exception as e:
                logger.debug(f"[WebUI] 清理定时消息持久化记录失败: {e}")
            return jsonify({"success": True, "message": "定时消息已取消"})
        except Exception as e:
            logger.error(f"[WebUI] 取消定时消息失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    # ==================== 工作区文件管理 API ====================

    async def handle_workspace_files(self):
        try:
            files = []
            for f in os.listdir(self.workspace_dir):
                if f.startswith('.'):
                    continue
                path = os.path.join(self.workspace_dir, f)
                if os.path.isfile(path):
                    files.append({"name": f, "size": os.path.getsize(path)})
            files.sort(key=lambda x: x['name'])
            return jsonify({"success": True, "files": files})
        except Exception as e:
            logger.error(f"[WebUI] 获取文件列表失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_workspace_file_content(self):
        try:
            filename = request.args.get("filename", "")
            if not filename:
                return jsonify({"success": False, "error": "缺少 filename"})
            filename = os.path.basename(filename)
            filepath = os.path.join(self.workspace_dir, filename)
            if not os.path.exists(filepath):
                return jsonify({"success": False, "error": "文件不存在"})
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            return jsonify({"success": True, "content": content})
        except Exception as e:
            logger.error(f"[WebUI] 读取文件失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_workspace_delete_file(self):
        try:
            data = await request.get_json()
            if not data or "filename" not in data:
                return jsonify({"success": False, "error": "缺少 filename"})
            filename = os.path.basename(data["filename"])
            filepath = os.path.join(self.workspace_dir, filename)
            if not os.path.exists(filepath):
                return jsonify({"success": False, "error": "文件不存在"})
            os.remove(filepath)
            return jsonify({"success": True, "message": "已删除"})
        except Exception as e:
            logger.error(f"[WebUI] 删除文件失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    async def handle_workspace_upload_file(self):
        try:
            data = await request.get_json()
            if not data or "filename" not in data or "content" not in data:
                return jsonify({"success": False, "error": "缺少 filename 或 content"})
            filename = os.path.basename(str(data["filename"]))
            if not filename:
                return jsonify({"success": False, "error": "文件名无效"})
            import base64 as _b64
            raw = data["content"]
            if not isinstance(raw, str):
                return jsonify({"success": False, "error": "content 必须是 base64 字符串"})
            # 限制大小：base64 膨胀约 4/3，这里限制原始大小 32MB
            max_b64_len = 32 * 1024 * 1024 * 4 // 3
            if len(raw) > max_b64_len:
                return jsonify({"success": False, "error": "文件过大（上限 32MB）"})
            try:
                content = _b64.b64decode(raw, validate=True)
            except Exception:
                return jsonify({"success": False, "error": "base64 内容无效"})
            filepath = os.path.join(self.workspace_dir, filename)
            # 原子写：避免写一半被读取
            tmp_path = filepath + ".part"
            with open(tmp_path, 'wb') as f:
                f.write(content)
            os.replace(tmp_path, filepath)
            return jsonify({"success": True, "message": "已上传"})
        except Exception as e:
            logger.error(f"[WebUI] 上传文件失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": _safe_error_msg(e)})

    # ==================== 初始化与生命周期 ====================


    async def initialize(self):
        self._register_page_routes()
        self.status_manager.set_db_manager(self.db_manager)
        self.command_executor = ScheduledCommandExecutor(self)
        # 所有后台任务均保存引用，避免被 GC 回收 + 便于 terminate 时统一取消
        self._create_bg_task(self.command_executor.start_periodic_check(), "command_executor")
        self._create_bg_task(self._delayed_restore(), "delayed_restore")
        self._refresh_task = self._create_bg_task(self._periodic_refresh(), "periodic_refresh")

        # 检查并安装 Playwright
        self._create_bg_task(self._check_and_install_playwright(), "playwright_setup")

        # 初始化高级浏览器管理
        self._init_browser_advanced()

        logger.info(f"[Main] 插件已加载")

    def _create_bg_task(self, coro, name: str) -> asyncio.Task:
        """创建并登记后台任务：保存引用 + 统一异常日志，防止静默失败与 GC 回收。"""
        task = asyncio.create_task(coro)
        self._bg_tasks[name] = task

        def _on_done(t: asyncio.Task, _name: str = name):
            self._bg_tasks.pop(_name, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error(f"[QZoneTools] 后台任务 {_name} 异常退出: {exc}", exc_info=exc)

        task.add_done_callback(_on_done)
        return task
    
    def _init_browser_advanced(self):
        """初始化高级浏览器管理组件。

        各组件独立 try：任一失败不影响其他组件（尤其是 browser_supervisor）。
        """
        # 收藏夹
        try:
            # 持久化数据必须写 data_dir，不能写插件安装目录（市场上架要求）
            favorite_file = Path(self.data_dir) / "favorites.json"
            self.fav_mgr = FavoriteManager(favorite_file)
        except Exception as e:
            logger.error(f"[Browser] 收藏夹初始化失败: {e}")

        # 刻度叠加（resource 目录位于包内，只读资源；运行时产物写入 data_dir）
        try:
            resource_dir = Path(self.data_dir) / "resource"
            try:
                resource_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            self.overlay = TickOverlay(self.data_dir, resource_dir, self.config)
        except Exception as e:
            logger.warning(f"[Browser] 刻度叠加初始化失败（功能不可用）: {e}")

        # 操作图标叠加（包内 resource/action_icons 为只读图标，产物写入 data_dir/overlay_cache）
        self._init_action_overlay()

        # 浏览器监控器（独立于上面，必须初始化成功）
        try:
            browser_config = {
                "browser_type": self.config.get("browser_type", "chromium"),
                "browser_mode": self.config.get("browser_mode", "embedded"),
                "cdp_url": self.config.get("cdp_url", "http://127.0.0.1:9222"),
                "verify_browser": self.config.get("verify_browser", True),
                "default_url": self.config.get("default_url", "https://www.baidu.com"),
                "proxy": self.config.get("proxy", ""),
                "viewport_size": self.config.get("viewport_size", {"width": 1280, "height": 720}),
                "max_pages": self.config.get("max_pages", 10),
                "timeout": self.config.get("timeout", 30),
                "screenshot_quality": self.config.get("screenshot_quality", 80),
                "zoom_factor": self.config.get("zoom_factor", 1.0),
                "enable_overlay": self.config.get("enable_overlay", False),
                "browser_render_mode": self.config.get("browser_render_mode", "full"),
                "supervisor": {
                    "max_memory_percent": self.config.get("max_memory_percent", 90),
                    "idle_timeout": self.config.get("idle_timeout", 300),
                    "monitor_interval": self.config.get("monitor_interval", 10),
                    "browser_memory_limit_mb": self.config.get("browser_memory_limit_mb", 0),
                }
            }
            self.browser_supervisor = BrowserSupervisor(browser_config, str(self.data_dir))
            # 接管中禁止自动关闭浏览器（用户正在操作，杀掉等于毁掉整场操作）
            try:
                self.browser_supervisor._in_takeover = self.takeover_manager.is_active
            except Exception:
                pass
            # 让接管会话的状态推送带上内存快照（WebUI 进度条实时刷新）
            try:
                self.takeover_manager.memory_provider = (
                    self.browser_supervisor.memory_snapshot
                )
            except Exception:
                pass
            self._create_bg_task(self.browser_supervisor.start(), "browser_supervisor")
            logger.info("[Browser] 高级浏览器管理已初始化")
        except Exception as e:
            logger.error(f"[Browser] 浏览器监控器初始化失败: {e}")
    
    async def _check_and_install_playwright(self):
        """检查 Playwright 是否安装，未安装则自动安装"""
        try:
            # 检查 playwright 模块
            try:
                import playwright  # noqa: F401
                logger.info("[Playwright] 模块已安装")
            except ImportError:
                logger.info("[Playwright] 未安装，开始安装...")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, '-m', 'pip', 'install', 'playwright',
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
                except asyncio.TimeoutError:
                    proc.kill()
                    logger.error("[Playwright] 模块安装超时")
                    return
                if proc.returncode == 0:
                    logger.info("[Playwright] 模块安装成功")
                else:
                    logger.error(
                        f"[Playwright] 模块安装失败: "
                        f"{stderr.decode(errors='ignore')[:200]}"
                    )
                    return

            # 检查浏览器是否可用（统一走 BrowserDownloader，避免多处并发 launch）
            from .core.downloader import BrowserDownloader
            browser_type = self.config.get("browser_type", "chromium")
            # 传对浏览器目录：插件把内核装在 data_dir/browsers，
            # 不传会走 playwright 默认缓存路径，导致重复下载 / 起无关进程
            if await BrowserDownloader.verify_browser(
                    browser_type, browsers_dir=Path(self.data_dir) / "browsers"):
                logger.info(f"[Playwright] {browser_type} 浏览器已安装且可用")
                return

            logger.info(f"[Playwright] {browser_type} 浏览器不可用，开始安装...")
            downloader = BrowserDownloader(Path(self.data_dir))
            ok, msg = await downloader.download(browser_type)
            if ok:
                logger.info(f"[Playwright] {msg}")
            else:
                logger.error(f"[Playwright] 浏览器安装失败: {msg}")
        except Exception as e:
            logger.error(f"[Playwright] 检查/安装过程异常: {_safe_error_msg(e)}")

    async def _periodic_refresh(self):
        while True:
            try:
                await asyncio.sleep(2 * 3600)
                await self._refresh_session()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # 单次刷新失败不应终止整个循环
                logger.warning(f"[Session] 定期刷新失败: {_safe_error_msg(e)}")

    async def _refresh_session(self):
        async with self._refresh_lock:
            client = await self._get_client()
            if client:
                await self.session.initialize(client)

    async def _delayed_restore(self):
        await asyncio.sleep(10)
        try:
            if hasattr(self.context, 'platform_manager'):
                pm = self.context.platform_manager
                if hasattr(pm, 'get_insts'):
                    platforms = pm.get_insts()
                    for platform in platforms:
                        if hasattr(platform, 'get_client'):
                            self._client = platform.get_client()
                            break
                        elif hasattr(platform, 'client'):
                            self._client = platform.client
                            break
        except Exception as e:
            logger.warning(f"获取客户端失败: {e}")
        if self._client:
            await self._do_restore()

    async def _do_restore(self):
        if self._restored:
            return
        try:
            if self._client:
                await self.status_manager.restore_from_db(self._client)
                await self.command_executor._check_and_execute_pending()
                await self._restore_scheduled_tasks()
                self._restored = True
        except Exception as e:
            logger.error(f"恢复失败: {e}")

    async def terminate(self):
        # 1) 取消所有登记在册的后台任务
        bg_snapshot = list(self._bg_tasks.values())
        for task in bg_snapshot:
            if not task.done():
                task.cancel()
        self._bg_tasks.clear()

        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()

        # 2) 停止定时命令执行器
        if self.command_executor:
            self.command_executor.stop_periodic_check()
            for task_id, task in list(self.command_executor.running_tasks.items()):
                if not task.done():
                    task.cancel()

        # 3) 取消定时消息任务
        for task_id, task in list(self.running_tasks.items()):
            if not task.done():
                task.cancel()

        # 4) 取消状态管理任务
        if self.status_manager.restore_task and not self.status_manager.restore_task.done():
            self.status_manager.restore_task.cancel()
        if self.status_manager.pending_task and not self.status_manager.pending_task.done():
            self.status_manager.pending_task.cancel()

        # 5) 持久化当前 QQ 状态
        if self.status_manager.is_status_active() and self.db_manager:
            await self.db_manager.save_status(
                self.status_manager.current_status,
                self.status_manager.current_status_name,
                self.status_manager.status_end_time
            )

        # 6) 关闭浏览器，释放 Playwright 进程
        if self.browser_supervisor:
            try:
                await self.browser_supervisor.stop()
            except Exception as e:
                logger.warning(f"[Browser] 关闭浏览器失败: {e}")

        # 7) 等待被取消的任务真正结束，避免 "Task was destroyed" 警告
        pending = [t for t in bg_snapshot if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        logger.info("[Main] 插件已卸载")

    # ==================== 图片格式转换 & 文本提取 ====================

    def _convert_image(self, image_path: str) -> str:
        """将图片转换为配置的输出格式，返回转换后的路径。"""
        if not image_path or not os.path.exists(image_path):
            return image_path
        try:
            fmt = get_format_from_config(self.config)
            return convert_image_format(image_path, fmt, quality=80)
        except Exception as e:
            logger.warning(f"[ImageConvert] 转换失败，保留原文件: {e}")
            return image_path

    async def _extract_page_text(self, page) -> str:
        """从 Playwright Page 中提取纯文本内容。"""
        try:
            text = await page.evaluate("document.body.innerText")
            return text or "(页面无文本内容)"
        except Exception as e:
            return f"(提取文本失败: {e})"

    # ==================== 核心辅助方法 ====================
    async def _get_client(self, event: AstrMessageEvent = None):
        if event:
            client = getattr(event, 'bot', None)
            if client and hasattr(client, 'call_action'):
                self._client = client
                return client
        if self._client and hasattr(self._client, 'call_action'):
            return self._client
        try:
            pm = self.context.platform_manager
            if hasattr(pm, 'get_insts'):
                platforms = pm.get_insts()
            else:
                platforms = pm._platforms.values() if hasattr(pm, '_platforms') else []
            for platform in platforms:
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client and hasattr(client, 'call_action'):
                        self._client = client
                        return client
                elif hasattr(platform, 'client') and hasattr(platform.client, 'call_action'):
                    self._client = platform.client
                    return platform.client
        except Exception as e:
            logger.debug(f"从 platform_manager 获取 client 失败: {e}")
        return None

    async def _call(self, client, action: str, timeout: float = None, **params):
        """统一的 NapCat API 调用封装：强制超时，避免 NapCat 无响应导致永久挂起。

        - 若调用方已显式传 timeout，则沿用该值
        - 未传时使用 client_timeout（默认 30 秒）
        """
        if timeout is None:
            timeout = params.pop("timeout", None)
        if timeout is None:
            try:
                timeout = float(self.config.get("client_timeout", 30))
            except (TypeError, ValueError):
                timeout = 30.0
        timeout = max(float(timeout), 1.0)
        return await asyncio.wait_for(
            client.call_action(action, **params), timeout=timeout
        )

    async def _update_contacts_cache(self, client):
        # 先判断是否需要刷新，避免在持锁期间做网络 IO
        now = time.time()
        if now - self._cache_time < self._cache_expire and (self._groups_cache or self._friends_cache):
            return
        # 网络请求放在锁外，避免 NapCat 卡顿导致锁长期占用
        groups, friends = [], []
        try:
            groups_result = await self._call(client, 'get_group_list')
            groups = groups_result if isinstance(groups_result, list) else groups_result.get('data', [])
        except Exception as e:
            logger.debug(f"[ContactsCache] get_group_list 失败: {e}")
        try:
            friends_result = await self._call(client, 'get_friend_list')
            friends = friends_result if isinstance(friends_result, list) else friends_result.get('data', [])
        except Exception as e:
            logger.debug(f"[ContactsCache] get_friend_list 失败: {e}")
        async with self._cache_lock:
            self._groups_cache = groups
            self._friends_cache = friends
            self._cache_time = time.time()

    def _validate_target_id(self, target_id: str) -> Tuple[bool, str]:
        target_id = str(target_id).strip()
        if not target_id:
            return False, "目标ID不能为空"
        if not target_id.isdigit():
            return False, "目标ID必须是纯数字"
        return True, target_id

    def _parse_time(self, time_str: str) -> Optional[datetime]:
        time_str = time_str.strip()
        now = datetime.now()
        daily_match = re.match(r'每天的(\d{1,2}):(\d{2})', time_str)
        if daily_match:
            hour, minute = int(daily_match.group(1)), int(daily_match.group(2))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target
        formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M", "%H:%M"]
        for fmt in formats:
            try:
                parsed = datetime.strptime(time_str, fmt)
                if fmt == "%H:%M":
                    parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
                    if parsed <= now:
                        parsed += timedelta(days=1)
                elif fmt == "%m-%d %H:%M":
                    parsed = parsed.replace(year=now.year)
                    if parsed <= now:
                        parsed = parsed.replace(year=now.year + 1)
                return parsed
            except:
                continue
        return None

    async def _execute_scheduled_task(self, task_id: str, delay_seconds: float):
        task = self.scheduled_tasks.get(task_id)
        try:
            await asyncio.sleep(delay_seconds)
            task = self.scheduled_tasks.get(task_id)
            if not task or task.cancelled:
                return
            client = self._client or await self._get_client()
            if not client:
                logger.warning(f"[定时消息] 任务 {task_id} 执行时无法获取 client，跳过")
                return
            if task.chat_type == "group":
                await self._call(client, 'send_group_msg', group_id=int(task.target_id), message=task.message)
            else:
                await self._call(client, 'send_private_msg', user_id=int(task.target_id), message=task.message)
            task.completed = True
        except asyncio.CancelledError:
            # 被取消：不标记完成，交由 cancel 流程清理
            raise
        except Exception as e:
            logger.error(f"定时任务执行失败: {e}")
        finally:
            if task_id in self.scheduled_tasks:
                del self.scheduled_tasks[task_id]
            if task_id in self.running_tasks:
                del self.running_tasks[task_id]
            # 完成后从持久化存储中移除（一次性任务）
            try:
                await self.db_manager.delete_scheduled_task(task_id)
            except Exception as e:
                logger.debug(f"[定时消息] 清理持久化记录失败: {e}")

    async def _restore_scheduled_tasks(self):
        """插件启动时恢复未执行的定时消息。"""
        try:
            records = await self.db_manager.load_scheduled_tasks()
        except Exception as e:
            logger.error(f"[定时消息] 读取持久化任务失败: {e}")
            return
        if not records:
            return
        now = datetime.now()
        restored = 0
        for rec in records:
            try:
                task = ScheduledTask.from_dict(rec)
            except (ValueError, TypeError) as e:
                logger.warning(f"[定时消息] 记录格式错误已跳过: {e}")
                try:
                    await self.db_manager.delete_scheduled_task(rec.get("task_id", ""))
                except Exception:
                    pass
                continue
            if task.completed or task.cancelled:
                continue
            # 已过期较久的任务不再补发（超过 5 分钟），直接清理
            delay = (task.send_time - now).total_seconds()
            if delay < -300:
                logger.warning(f"[定时消息] 任务 {task.task_id} 已过期，跳过")
                await self.db_manager.delete_scheduled_task(task.task_id)
                continue
            self.scheduled_tasks[task.task_id] = task
            coro = self._execute_scheduled_task(task.task_id, max(delay, 0))
            self.running_tasks[task.task_id] = self._create_bg_task(
                coro, f"scheduled_{task.task_id}"
            )
            restored += 1
        if restored:
            logger.info(f"[定时消息] 已恢复 {restored} 个未执行的定时消息任务")

    async def _get_group_member_role(self, group_id: str, user_id: str) -> str:
        """查询群成员角色。带 60 秒缓存，避免权限校验反复打 NapCat API。"""
        cache_key = f"{group_id}:{user_id}"
        cached = self._role_cache.get(cache_key)
        if cached is not None and time.time() - cached[0] < 60:
            return cached[1]

        client = await self._get_client()
        if not client:
            return "unknown"
        try:
            info = await self._call(client, 'get_group_member_info',
                                    group_id=int(group_id), user_id=int(user_id),
                                    no_cache=False)
            role = info.get('role', 'member')
            if role == 'owner':
                result = '群主'
            elif role == 'admin':
                result = '管理员'
            else:
                result = '成员'
            self._role_cache[cache_key] = (time.time(), result)
            # 简单的缓存清理，避免无限增长
            if len(self._role_cache) > 500:
                cutoff = time.time() - 60
                self._role_cache = {k: v for k, v in self._role_cache.items()
                                    if v[0] > cutoff}
            return result
        except Exception as e:
            logger.debug(f"获取群成员角色失败: {e}")
            return "unknown"

    async def _get_ai_characters_raw(self, event: AstrMessageEvent, group_id: str) -> list:
        cache_key = f"ai_characters_{group_id}"
        if cache_key in self._ai_characters_cache:
            cached_time, cached_data = self._ai_characters_cache[cache_key]
            if time.time() - cached_time < 600:
                return cached_data
        client = await self._get_client(event)
        if not client:
            return []
        try:
            response = await self._call(client, 'get_ai_characters', group_id=group_id, chat_type=1, timeout=8)
            if isinstance(response, dict) and response.get("status") == "ok":
                data = response.get("data", [])
            elif isinstance(response, list):
                data = response
            else:
                data = []
            self._ai_characters_cache[cache_key] = (time.time(), data)
            return data
        except Exception as e:
            logger.error(f"[AI声聊] 获取角色列表失败: {e}")
            return []

    async def _get_character_id_by_name_or_id(self, event: AstrMessageEvent, group_id: str, identifier: str) -> Optional[str]:
        if not identifier:
            return None
        data = await self._get_ai_characters_raw(event, group_id)
        for cat in data:
            if not isinstance(cat, dict):
                continue
            for char in cat.get("characters", []):
                if str(char.get("character_id")) == identifier or char.get("character_name") == identifier:
                    return str(char.get("character_id"))
        return None

    async def _get_image_file_from_event(self, event: AiocqhttpMessageEvent) -> Optional[str]:
        chain = event.get_messages()
        if not chain:
            return None
        if isinstance(chain[0], Reply):
            reply_chain = chain[0].chain
            if reply_chain:
                for seg in reply_chain:
                    if isinstance(seg, Image):
                        return seg.file or seg.url or seg.path
        for seg in chain:
            if isinstance(seg, Image):
                return seg.file or seg.url or seg.path
        raw = event.message_obj.raw_message
        if hasattr(raw, 'image') and raw.image:
            return raw.image
        return None

    def _allowed_file_roots(self) -> tuple:
        """返回允许访问的文件根目录（realpath 形式）。"""
        roots = [os.path.realpath(self.workspace_dir)]
        try:
            roots.append(os.path.realpath(os.path.join(self.data_dir, "screenshot_cache")))
        except Exception:
            pass
        if self.flash_transfer_dir:
            try:
                roots.append(os.path.realpath(self.flash_transfer_dir))
            except Exception:
                pass
        return tuple(roots)

    def _safe_resolve_path(self, filename: str, must_exist: bool = True) -> Optional[str]:
        """把用户传入的文件名解析为受限路径。

        规则：
        - 相对路径：仅允许在工作区内（含 screenshot_cache / 闪传目录）
        - 绝对路径：仅允许落在允许的根目录内（用 realpath 防软链接穿透）
        - 拒绝 `..`、软链接穿透、Windows 反斜杠等穿越手法

        返回绝对路径；不合规或不存在时返回 None。
        """
        if not filename:
            return None
        raw = str(filename).strip().replace("\\", "/")
        if not raw:
            return None

        roots = self._allowed_file_roots()

        if os.path.isabs(raw):
            # Windows 盘符形式在 Linux 下不视为绝对路径，这里额外兜底
            candidate = raw
        else:
            base = os.path.basename(raw)
            candidate = os.path.join(self.workspace_dir, base)
            if not os.path.exists(candidate):
                alt = os.path.join(self.data_dir, "screenshot_cache", base)
                if os.path.isfile(alt):
                    candidate = alt

        try:
            real = os.path.realpath(candidate)
        except Exception:
            return None

        # 必须以允许的根目录开头（加分隔符避免 /workspace2 命中 /workspace）
        if not any(real == r or real.startswith(r + os.sep) for r in roots):
            logger.warning(f"[QZoneTools] 路径越界已拒绝: {real}")
            return None

        if must_exist and not os.path.exists(real):
            return None
        return real

    async def _resolve_image_file(self, file: str) -> Optional[str]:
        """将图片文件转为 base64:// 格式，用于跨容器传递。安全限制: 仅允许工作区目录和 /tmp。"""
        if not file:
            return None
        if file.startswith("base64://") or file.startswith("http://") or file.startswith("https://"):
            return file
        if os.path.isfile(file):
            # 路径安全检查
            if self.resolve_image_restricted:
                real_path = os.path.realpath(file)
                allowed_prefixes = (
                    os.path.realpath(self.workspace_dir),
                    os.path.realpath(self.flash_transfer_dir),
                    "/tmp",
                )
                if not any(real_path.startswith(p) for p in allowed_prefixes):
                    logger.warning(f"[QZoneTools] 路径拒绝 (安全限制): {real_path}")
                    return None
            try:
                with open(file, 'rb') as f:
                    data = f.read()
                import base64
                encoded = base64.b64encode(data).decode('ascii')
                logger.info(f"[QZoneTools] 已转为base64 (源: {os.path.basename(file)})")
                return "base64://" + encoded
            except Exception as e:
                logger.error(f"[QZoneTools] 读取图片文件失败: {_safe_error_msg(e)}")
                return None
        return file

    # ==================== 具体工具实现函数 ====================
    async def add_memory(self, event: AstrMessageEvent, content: str, tags: str = "", importance: int = 5) -> dict:
        if not content or content.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供记忆内容。"}
        user_id = event.get_sender_id()
        tags_list = [t.strip() for t in tags.split(",")] if tags else []
        importance = max(1, min(10, importance))
        memory_id = await self.memory_manager.add_memory(user_id, content, tags_list, importance)
        msg = f"✅ 记忆已保存\nID: {memory_id}\n内容: {content[:50]}{'...' if len(content)>50 else ''}"
        return {"status": "success", "message": msg}

    async def search_memories(self, event: AstrMessageEvent, keyword: str = "", user_specific: bool = True, limit: int = 10) -> dict:
        user_id = event.get_sender_id() if user_specific else None
        limit = min(limit, 20)
        memories = await self.memory_manager.get_memories(user_id=user_id, keyword=keyword if keyword else None, limit=limit)
        if not memories:
            if keyword:
                return {"status": "success", "message": f"📭 未找到包含「{keyword}」的记忆"}
            return {"status": "success", "message": "📭 暂无记忆"}
        lines = [f"📚 找到 {len(memories)} 条记忆："]
        for i, m in enumerate(memories, 1):
            tags_str = f"[{', '.join(m.get('tags', []))}]" if m.get('tags') else ""
            content = m.get('content', '')[:40] + ('...' if len(m.get('content',''))>40 else '')
            lines.append(f"{i}. [{m['id']}] {content} (重要度:{m.get('importance',5)}) {tags_str} - {m.get('updated_at','')[:10]}")
        msg = "\n".join(lines)
        return {"status": "success", "message": msg}

    async def update_memory(self, event: AstrMessageEvent, memory_id: str, content: str = None, tags: str = None, importance: int = None) -> dict:
        if not memory_id or memory_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供要更新的记忆ID。"}
        existing = await self.memory_manager.get_memory_by_id(memory_id)
        if not existing:
            return {"status": "error", "message": f"❌ 未找到记忆ID: {memory_id}"}
        tags_list = [t.strip() for t in tags.split(",")] if tags is not None else None
        success = await self.memory_manager.update_memory(memory_id, content, tags_list, importance)
        if success:
            return {"status": "success", "message": f"✅ 记忆已更新\nID: {memory_id}"}
        else:
            return {"status": "error", "message": "❌ 更新失败"}

    async def delete_memory(self, event: AstrMessageEvent, memory_id: str) -> dict:
        if not memory_id or memory_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供要删除的记忆ID。"}
        existing = await self.memory_manager.get_memory_by_id(memory_id)
        if not existing:
            return {"status": "error", "message": f"❌ 未找到记忆ID: {memory_id}"}
        success = await self.memory_manager.delete_memory(memory_id)
        if success:
            return {"status": "success", "message": f"🗑️ 记忆已删除\nID: {memory_id}"}
        else:
            return {"status": "error", "message": "❌ 删除失败"}

    async def get_memory_detail(self, event: AstrMessageEvent, memory_id: str) -> dict:
        if not memory_id or memory_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供记忆ID。"}
        m = await self.memory_manager.get_memory_by_id(memory_id)
        if not m:
            return {"status": "error", "message": f"❌ 未找到记忆ID: {memory_id}"}
        lines = [
            f"📋 记忆详情",
            f"ID: {m['id']}",
            f"用户: {m['user_id']}",
            f"内容: {m['content']}",
            f"标签: {', '.join(m.get('tags', [])) or '无'}",
            f"重要度: {m.get('importance',5)}/10",
            f"创建: {m.get('created_at')}",
            f"更新: {m.get('updated_at')}"
        ]
        msg = "\n".join(lines)
        return {"status": "success", "message": msg}

    async def send_message_tool(self, event: AstrMessageEvent, target_id: str, message: str, chat_type: str = "auto") -> dict:
        if not target_id or target_id.strip() == "" or not message or message.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供目标ID和消息内容。"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        is_valid, result = self._validate_target_id(target_id)
        if not is_valid:
            return {"status": "error", "message": f"参数错误: {result}"}
        if chat_type == "auto":
            await self._update_contacts_cache(client)
            is_group = any(str(g.get('group_id')) == target_id for g in self._groups_cache)
            chat_type = "group" if is_group else "private"
        try:
            if chat_type == "group":
                await self._call(client, 'send_group_msg', group_id=int(target_id), message=message)
            else:
                await self._call(client, 'send_private_msg', user_id=int(target_id), message=message)
            return {"status": "success", "message": f"✅ 已发送消息到 {target_id}"}
        except Exception as e:
            return {"status": "error", "message": f"发送失败: {_safe_error_msg(e)}"}

    async def schedule_message(self, event: AstrMessageEvent, target_id: str, message: str, send_time: str, chat_type: str = "group") -> dict:
        if not target_id or target_id.strip() == "" or not message or message.strip() == "" or not send_time or send_time.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供目标ID、消息内容和发送时间。"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        is_valid, result = self._validate_target_id(target_id)
        if not is_valid:
            return {"status": "error", "message": f"参数错误: {result}"}
        parsed_time = self._parse_time(send_time)
        if not parsed_time:
            return {"status": "error", "message": "错误：无法理解时间格式，请使用如 明天08:00、2026-01-01 12:00、每天的08:00"}
        if parsed_time <= datetime.now():
            return {"status": "error", "message": "错误：指定的时间已经过去"}
        task_id = str(uuid.uuid4())[:8]
        task = ScheduledTask(task_id=task_id, target_id=target_id, message=message, send_time=parsed_time, chat_type=chat_type)
        self.scheduled_tasks[task_id] = task
        try:
            await self.db_manager.save_scheduled_task(task.to_dict())
        except Exception as e:
            logger.error(f"[定时消息] 持久化失败（任务仅存于内存）: {e}")
        delay_seconds = (parsed_time - datetime.now()).total_seconds()
        asyncio_task = asyncio.create_task(self._execute_scheduled_task(task_id, delay_seconds))
        self.running_tasks[task_id] = asyncio_task
        msg = f"✅ 定时任务已创建\n任务ID: {task_id}\n时间: {parsed_time.strftime('%Y-%m-%d %H:%M:%S')}\n💾 已持久化，重启后自动恢复"
        return {"status": "success", "message": msg}

    async def cancel_scheduled_message(self, event: AstrMessageEvent, task_id: str) -> dict:
        if not task_id or task_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供任务ID。"}
        if task_id not in self.scheduled_tasks:
            return {"status": "error", "message": f"错误：未找到任务 {task_id}"}
        task = self.scheduled_tasks[task_id]
        task.cancelled = True
        if task_id in self.running_tasks:
            self.running_tasks[task_id].cancel()
            del self.running_tasks[task_id]
        if task_id in self.scheduled_tasks:
            del self.scheduled_tasks[task_id]
        try:
            await self.db_manager.delete_scheduled_task(task_id)
        except Exception as e:
            logger.debug(f"[定时消息] 清理持久化记录失败: {e}")
        return {"status": "success", "message": f"✅ 已取消任务 {task_id}"}

    async def list_scheduled_messages(self, event: AstrMessageEvent, show_all: bool = False) -> dict:
        tasks = list(self.scheduled_tasks.values()) if show_all else [t for t in self.scheduled_tasks.values() if not t.cancelled and not t.completed]
        if not tasks:
            return {"status": "success", "message": "当前没有定时消息任务"}
        lines = [f"📋 定时消息任务列表（{len(tasks)}个）"]
        for t in sorted(tasks, key=lambda x: x.send_time):
            status = "✅" if t.completed else "❌" if t.cancelled else "⏳"
            lines.append(f"{status} [{t.task_id}] {t.send_time.strftime('%m-%d %H:%M')} -> {t.target_id}")
        msg = "\n".join(lines)
        return {"status": "success", "message": msg}

    async def publish_qzone(self, event: AstrMessageEvent, content: str) -> dict:
        if not content or content.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供说说内容。"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        success = await self.session.initialize(client)
        if not success:
            return {"status": "error", "message": "错误：无法初始化QQ空间，请检查网络或重新登录"}
        result = await self.qzone.publish_post(content)
        if result.get('success'):
            return {"status": "success", "message": result['msg']}
        else:
            return {"status": "error", "message": result['msg']}

    async def send_poke(self, event: AstrMessageEvent, target_qq: str, chat_type: str = "auto") -> dict:
        if not target_qq or target_qq.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供目标QQ号。"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        is_valid, result = self._validate_target_id(target_qq)
        if not is_valid:
            return {"status": "error", "message": f"参数错误: {result}"}
        if chat_type == "auto":
            chat_type = "private" if event.is_private_chat() else "group"
        try:
            if chat_type == "private":
                await self._call(client, 'friend_poke', user_id=int(target_qq))
            else:
                group_id = event.get_group_id()
                if not group_id:
                    return {"status": "error", "message": "错误：无法获取群号"}
                await self._call(client, 'group_poke', group_id=int(group_id), user_id=int(target_qq))
            return {"status": "success", "message": f"✅ 已戳一戳 {target_qq}"}
        except Exception as e:
            return {"status": "error", "message": f"发送失败: {_safe_error_msg(e)}"}

    async def update_qq_status(self, event: AstrMessageEvent, status: str, duration_minutes: int, delay_minutes: int = 0) -> dict:
        if not status or status.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供状态码。"}
        if duration_minutes is None:
            return {"status": "error", "message": "❌ 参数缺失：请提供持续时间（分钟）。"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        if duration_minutes < 1:
            duration_minutes = 1
        result = await self.status_manager.set_status(client, status, duration_minutes, delay_minutes)
        if result.get('success'):
            return {"status": "success", "message": result['msg']}
        else:
            return {"status": "error", "message": result['msg']}

    async def get_qq_status(self, event: AstrMessageEvent) -> dict:
        return {"status": "success", "message": self.status_manager.get_current_status_desc()}

    async def get_fun_status_list(self, event: AstrMessageEvent) -> dict:
        return {"status": "success", "message": "娱乐状态：listening(听歌中), sleeping(睡觉中), studying(学习中)"}

    async def create_scheduled_command(self, event: AstrMessageEvent, command_type: str, execute_time: str, params: str, recurrence: str = "once") -> dict:
        if not command_type or command_type.strip() == "" or not execute_time or execute_time.strip() == "" or not params or params.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供指令类型、执行时间和参数。"}
        parsed_time = self._parse_time(execute_time)
        if not parsed_time:
            return {"status": "error", "message": "错误：无法理解时间格式"}
        if parsed_time <= datetime.now():
            return {"status": "error", "message": "❌ 执行时间不能早于当前时间"}
        try:
            params_dict = json.loads(params)
        except json.JSONDecodeError:
            return {"status": "error", "message": "错误：params必须是有效JSON字符串"}
        valid_types = ["qzone_post", "status_change", "llm_remind"]
        if command_type not in valid_types:
            return {"status": "error", "message": f"错误：无效类型，可选: {', '.join(valid_types)}"}
        session_info = None
        if command_type == "llm_remind":
            session_info = {
                'unified_msg_origin': event.unified_msg_origin,
                'platform_name': event.get_platform_name(),
                'sender_id': event.get_sender_id(),
                'sender_name': event.get_sender_name()
            }
        task_id = str(uuid.uuid4())[:8]
        success = await self.db_manager.save_scheduled_command(task_id, command_type, params_dict, parsed_time, recurrence, session_info)
        if success:
            return {"status": "success", "message": f"✅ 定时指令已创建\n任务ID: {task_id}\n此指令持久化存储，重启后仍会执行。"}
        else:
            return {"status": "error", "message": "❌ 保存失败"}

    async def list_scheduled_commands(self, event: AstrMessageEvent, include_executed: bool = False) -> dict:
        commands = await self.db_manager.get_all_commands(include_executed)
        if not commands:
            return {"status": "success", "message": "当前没有定时指令任务"}
        lines = [f"📋 定时指令列表（{len(commands)}条）"]
        for cmd in commands[:15]:
            status_map = {0: "⏳", 1: "✅", 2: "❌", -1: "⚠️"}
            status = status_map.get(cmd.get('executed'), "❓")
            time_str = datetime.fromisoformat(cmd['execute_time']).strftime("%m-%d %H:%M")
            lines.append(f"{status} [{cmd['id']}] {cmd['command_type']} {time_str}")
        msg = "\n".join(lines)
        return {"status": "success", "message": msg}

    async def cancel_scheduled_command(self, event: AstrMessageEvent, task_id: str) -> dict:
        if not task_id or task_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供要取消的任务ID。"}
        await self.db_manager.cancel_command(task_id)
        self.command_executor.cancel_task(task_id)
        return {"status": "success", "message": f"✅ 已取消指令 {task_id}"}

    async def delete_scheduled_command(self, event: AstrMessageEvent, task_id: str) -> dict:
        if not task_id or task_id.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供要删除的任务ID。"}
        await self.db_manager.delete_command(task_id)
        self.command_executor.cancel_task(task_id)
        return {"status": "success", "message": f"✅ 已删除指令 {task_id}"}

    async def recall_by_reply(self, event: AiocqhttpMessageEvent, message_id: str = None) -> dict:
        # 优先使用传入的 message_id
        if message_id and message_id.strip():
            msg_id = message_id.strip()
            if not msg_id.isdigit():
                return {"status": "error", "message": "❌ 消息ID无效，必须是数字"}
            # 尝试获取 group_id（群聊场景）
            group_id = event.get_group_id()
            try:
                if group_id:
                    await event.bot.delete_msg(message_id=int(msg_id), group_id=int(group_id))
                else:
                    # 私聊场景，不传 group_id
                    await event.bot.delete_msg(message_id=int(msg_id))
                return {"status": "success", "message": f"✅ 撤回成功\n• 消息ID: {msg_id}"}
            except Exception as e:
                return {"status": "error", "message": f"❌ 撤回失败: {_safe_error_msg(e)}"}
        # 回退到引用消息模式
        chain = event.get_messages()
        if not chain or len(chain)==0 or not isinstance(chain[0], Reply):
            return {"status": "error", "message": "❌ 请提供要撤回的消息ID，或引用要撤回的消息"}
        msg_id = str(chain[0].id)
        if not msg_id.isdigit():
            return {"status": "error", "message": "❌ 引用的消息ID无效"}
        group_id = event.get_group_id()
        try:
            if group_id:
                await event.bot.delete_msg(message_id=int(msg_id), group_id=int(group_id))
            else:
                await event.bot.delete_msg(message_id=int(msg_id))
            return {"status": "success", "message": f"✅ 撤回成功\n• 消息ID: {msg_id}"}
        except Exception as e:
            return {"status": "error", "message": f"❌ 撤回失败: {_safe_error_msg(e)}"}

    async def send_qq_email_tool(self, event: AstrMessageEvent, to: str, subject: str, content: str, nickname: str = "") -> dict:
        if not to or to.strip() == "" or not subject or subject.strip() == "" or not content or content.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供收件人、主题和内容。"}
        result = await self.email_sender.send_email(to, subject, content, nickname)
        if result.get('success'):
            return {"status": "success", "message": result['msg']}
        else:
            return {"status": "error", "message": result['msg']}

    async def get_user_group_role(self, event: AiocqhttpMessageEvent, group_id: str, user_id: str) -> dict:
        if not group_id or not group_id.strip() or not user_id or not user_id.strip():
            return {"status": "error", "message": "❌ 参数缺失：请提供群号和用户QQ号。"}
        if not group_id.isdigit() or not user_id.isdigit():
            return {"status": "error", "message": "❌ 群号和用户QQ号必须为纯数字"}
        role = await self._get_group_member_role(group_id, user_id)
        if role == "unknown":
            return {"status": "error", "message": f"无法查询用户 {user_id} 在群 {group_id} 的身份，请确认机器人是否在群内且有权限。"}
        return {"status": "success", "message": f"用户 {user_id} 在群 {group_id} 中的身份是：{role}"}

    async def set_essence_msg(self, event: AiocqhttpMessageEvent) -> dict:
        first_seg = event.get_messages()[0]
        if isinstance(first_seg, Reply):
            try:
                await event.bot.set_essence_msg(message_id=int(first_seg.id))
                msg = f"已将消息 {first_seg.id} 添加到群精华"
                return {"status": "success", "message": msg}
            except Exception as e:
                return {"status": "error", "message": f"设置精华失败: {_safe_error_msg(e)}"}
        else:
            return {"status": "error", "message": "请引用要设置为精华的消息"}

    async def delete_essence_msg(self, event: AiocqhttpMessageEvent) -> dict:
        first_seg = event.get_messages()[0]
        if isinstance(first_seg, Reply):
            try:
                await event.bot.delete_essence_msg(message_id=int(first_seg.id))
                msg = f"已将消息 {first_seg.id} 移出群精华"
                return {"status": "success", "message": msg}
            except Exception as e:
                return {"status": "error", "message": f"取消精华失败: {_safe_error_msg(e)}"}
        else:
            return {"status": "error", "message": "请引用要取消精华的消息"}

    async def set_group_ban(self, event: AiocqhttpMessageEvent, user_id: str, duration: int) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot.set_group_ban(group_id=int(group_id), user_id=int(user_id), duration=duration)
            if duration == 0:
                msg = f"已解禁用户 {user_id}"
            else:
                minutes = duration // 60
                msg = f"已禁言用户 {user_id}，时长 {minutes} 分钟"
            return {"status": "success", "message": msg}
        except Exception as e:
            return {"status": "error", "message": f"禁言操作失败: {_safe_error_msg(e)}"}

    async def set_group_kick(self, event: AiocqhttpMessageEvent, user_id: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        if not self.config.get("kick_enabled", True):
            return {"status": "error", "message": "❌ 踢人功能已被管理员禁用（kick_enabled=false）"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot.set_group_kick(group_id=int(group_id), user_id=int(user_id), reject_add_request=False)
            return {"status": "success", "message": f"已踢出用户 {user_id}"}
        except Exception as e:
            return {"status": "error", "message": f"踢人失败: {_safe_error_msg(e)}"}

    async def set_group_whole_ban(self, event: AiocqhttpMessageEvent, enable: bool) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot.set_group_whole_ban(group_id=int(group_id), enable=enable)
            action = "开启" if enable else "关闭"
            return {"status": "success", "message": f"已{action}全体禁言"}
        except Exception as e:
            return {"status": "error", "message": f"全体禁言操作失败: {_safe_error_msg(e)}"}

    async def set_group_card(self, event: AiocqhttpMessageEvent, user_id: str, card: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot.set_group_card(group_id=int(group_id), user_id=int(user_id), card=card)
            if card:
                msg = f"已将用户 {user_id} 的群昵称修改为：{card}"
            else:
                msg = f"已取消用户 {user_id} 的群昵称"
            return {"status": "success", "message": msg}
        except Exception as e:
            return {"status": "error", "message": f"修改群昵称失败: {_safe_error_msg(e)}"}

    async def send_group_notice(self, event: AiocqhttpMessageEvent, content: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot._send_group_notice(group_id=int(group_id), content=content)
            return {"status": "success", "message": f"群公告已发布：{content}"}
        except Exception as e:
            return {"status": "error", "message": f"发布公告失败: {_safe_error_msg(e)}"}

    async def delete_group_notice(self, event: AiocqhttpMessageEvent, notice_id: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            await event.bot._del_group_notice(group_id=int(group_id), notice_id=notice_id)
            return {"status": "success", "message": f"已撤回公告 {notice_id}"}
        except Exception as e:
            return {"status": "error", "message": f"撤回公告失败: {_safe_error_msg(e)}"}

    async def list_group_files(self, event: AiocqhttpMessageEvent) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            result = await event.bot.get_group_root_files(group_id=int(group_id))
            files = result.get('files', [])
            if not files:
                return {"status": "success", "message": f"群 {group_id} 根目录下没有文件"}
            lines = [f"群 {group_id} 根目录文件列表："]
            for f in files[:20]:
                name = f.get('file_name', '未知')
                size = f.get('file_size', 0)
                size_mb = size / (1024 * 1024)
                lines.append(f"  • {name} ({size_mb:.2f} MB) [file_id: {f.get('file_id', 'N/A')}]")
            if len(files) > 20:
                lines.append(f"  ... 共 {len(files)} 个文件，仅显示前20个")
            msg = "\n".join(lines)
            return {"status": "success", "message": msg}
        except Exception as e:
            return {"status": "error", "message": f"查询文件失败: {_safe_error_msg(e)}"}

    async def delete_group_file(self, event: AiocqhttpMessageEvent, file_id: str = None) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
            if not file_id:
                return {"status": "error", "message": "❌ 参数缺失：请提供 file_id"}
            await event.bot.delete_group_file(group_id=int(group_id), file_id=file_id)
            return {"status": "success", "message": f"✅ 已删除群文件 {file_id}"}
        except Exception as e:
            return {"status": "error", "message": f"删除文件失败: {_safe_error_msg(e)}"}

    async def get_group_members_info(self, event: AiocqhttpMessageEvent) -> dict:
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "这不是群聊"}
            members = await event.bot.get_group_member_list(group_id=int(group_id))
            if not members:
                return {"status": "error", "message": "获取群成员信息失败"}
            processed = []
            for m in members:
                processed.append({
                    "user_id": str(m.get("user_id", "")),
                    "display_name": m.get("card") or m.get("nickname") or f"用户{m.get('user_id')}",
                    "username": m.get("nickname") or f"用户{m.get('user_id')}",
                    "role": m.get("role", "member")
                })
            result_json = json.dumps({
                "group_id": group_id,
                "member_count": len(processed),
                "members": processed
            }, ensure_ascii=False, indent=2)
            return {"status": "success", "message": result_json}
        except Exception as e:
            return {"status": "error", "message": f"获取成员信息失败: {_safe_error_msg(e)}"}

    async def set_group_admin(self, event: AiocqhttpMessageEvent, user_id: str, enable: bool) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            await self._call(client, 'set_group_admin', group_id=int(group_id), user_id=int(user_id), enable=enable)
            action = "设置为管理员" if enable else "取消管理员"
            return {"status": "success", "message": f"✅ 已{action}用户 {user_id}"}
        except Exception as e:
            return {"status": "error", "message": f"操作失败: {_safe_error_msg(e)}"}

    async def set_group_name(self, event: AiocqhttpMessageEvent, group_name: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            await self._call(client, 'set_group_name', group_id=int(group_id), group_name=group_name)
            return {"status": "success", "message": f"✅ 群名称已修改为：{group_name}"}
        except Exception as e:
            return {"status": "error", "message": f"操作失败: {_safe_error_msg(e)}"}

    async def get_group_notice_list(self, event: AiocqhttpMessageEvent) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            result = await self._call(client, '_get_group_notice', group_id=int(group_id))
            notices = result.get('data', []) if isinstance(result, dict) else result
            if not notices:
                return {"status": "success", "message": "该群暂无公告"}
            lines = [f"📢 群公告列表（共{len(notices)}条）"]
            for n in notices[:10]:
                notice_id = n.get('notice_id', '')
                sender_id = n.get('sender_id', '')
                content = n.get('content', '')[:50]
                publish_time = n.get('publish_time', 0)
                time_str = datetime.fromtimestamp(publish_time).strftime('%Y-%m-%d %H:%M:%S') if publish_time else '未知'
                lines.append(f"• [{notice_id}] {content}... (发布者:{sender_id}, 时间:{time_str})")
            return {"status": "success", "message": "\n".join(lines)}
        except Exception as e:
            return {"status": "error", "message": f"获取公告失败: {_safe_error_msg(e)}"}

    async def upload_group_file(self, event: AiocqhttpMessageEvent, file_path: str, file_name: str = "") -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        if not file_path or not os.path.exists(file_path):
            return {"status": "error", "message": f"文件不存在: {file_path}"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            name = file_name if file_name else os.path.basename(file_path)
            result = await self._call(client, 'upload_group_file', group_id=int(group_id), file=file_path, name=name)
            return {"status": "success", "message": f"✅ 文件上传成功，file_id: {result.get('file_id', '未知')}"}
        except Exception as e:
            return {"status": "error", "message": f"上传失败: {_safe_error_msg(e)}"}

    async def create_group_file_folder(self, event: AiocqhttpMessageEvent, folder_name: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            result = await self._call(client, 'create_group_file_folder', group_id=int(group_id), folder_name=folder_name)
            return {"status": "success", "message": f"✅ 文件夹创建成功，ID: {result.get('folder_id', '未知')}"}
        except Exception as e:
            return {"status": "error", "message": f"创建失败: {_safe_error_msg(e)}"}

    async def delete_group_folder(self, event: AiocqhttpMessageEvent, folder_id: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            await self._call(client, 'delete_group_folder', group_id=int(group_id), folder_id=folder_id)
            return {"status": "success", "message": f"✅ 文件夹 {folder_id} 已删除"}
        except Exception as e:
            return {"status": "error", "message": f"删除失败: {_safe_error_msg(e)}"}

    async def get_group_honor_info(self, event: AiocqhttpMessageEvent, honor_type: str = "all") -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            result = await self._call(client, 'get_group_honor_info', group_id=int(group_id), type=honor_type)
            if honor_type == "talkative" or honor_type == "all":
                current = result.get('current_talkative', {})
                if current:
                    return {"status": "success", "message": f"当前龙王: {current.get('nickname', '')}({current.get('user_id', '')})"}
            lines = ["🏆 群荣誉信息"]
            for key, name in [('talkative_list', '历史龙王'), ('performer_list', '群聊之火'), ('legend_list', '传说'), ('strong_newbie_list', '新人王'), ('emotion_list', '快乐源泉')]:
                items = result.get(key, [])
                if items:
                    item_strs = []
                    for i in items[:5]:
                        nickname = i.get('nickname', '')
                        user_id = i.get('user_id', '')
                        item_strs.append(f"{nickname}({user_id})")
                    lines.append(f"{name}: {', '.join(item_strs)}")
            return {"status": "success", "message": "\n".join(lines)}
        except Exception as e:
            return {"status": "error", "message": f"获取失败: {_safe_error_msg(e)}"}

    async def get_group_at_all_remain(self, event: AiocqhttpMessageEvent) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            result = await self._call(client, 'get_group_at_all_remain', group_id=int(group_id))
            can = result.get('can_at_all', False)
            remain = result.get('remain_at_all_count', 0)
            return {"status": "success", "message": f"@全体成员: {'可用' if can else '不可用'}，剩余次数: {remain}"}
        except Exception as e:
            return {"status": "error", "message": f"查询失败: {_safe_error_msg(e)}"}

    async def set_group_special_title(self, event: AiocqhttpMessageEvent, user_id: str, special_title: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            await self._call(client, 'set_group_special_title', group_id=int(group_id), user_id=int(user_id), special_title=special_title)
            if special_title:
                return {"status": "success", "message": f"✅ 已将用户 {user_id} 的头衔设置为：{special_title}"}
            else:
                return {"status": "success", "message": f"✅ 已取消用户 {user_id} 的专属头衔"}
        except Exception as e:
            return {"status": "error", "message": f"操作失败: {_safe_error_msg(e)}"}

    async def get_group_shut_list(self, event: AiocqhttpMessageEvent) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            result = await self._call(client, 'get_group_shut_list', group_id=int(group_id))
            if not result:
                return {"status": "success", "message": "当前没有被禁言的成员"}
            lines = [f"🔇 被禁言成员列表（共{len(result)}人）"]
            for m in result[:15]:
                user_id = m.get('user_id', '')
                shut_time = m.get('shut_up_timestamp', 0)
                if shut_time:
                    remain = max(0, shut_time - int(time.time()))
                    remain_str = f"{remain//60}分{remain%60}秒"
                else:
                    remain_str = "未知"
                lines.append(f"• {user_id} (剩余: {remain_str})")
            return {"status": "success", "message": "\n".join(lines)}
        except Exception as e:
            return {"status": "error", "message": f"获取失败: {_safe_error_msg(e)}"}

    async def get_group_ignore_add_request(self, event: AiocqhttpMessageEvent) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            result = await self._call(client, 'get_group_ignore_add_request', group_id=int(group_id))
            requests = result.get('data', []) if isinstance(result, dict) else result
            if not requests:
                return {"status": "success", "message": "没有被忽略的加群请求"}
            lines = [f"📋 被忽略的加群请求（{len(requests)}条）"]
            for r in requests[:10]:
                user_id = r.get('user_id', '')
                nickname = r.get('nickname', '')
                comment = r.get('comment', '')
                lines.append(f"• {user_id}({nickname}): {comment[:30]}")
            return {"status": "success", "message": "\n".join(lines)}
        except Exception as e:
            return {"status": "error", "message": f"获取失败: {_safe_error_msg(e)}"}

    async def set_group_add_option(self, event: AiocqhttpMessageEvent, option: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        option_map = {"allow": 1, "need_verify": 2, "not_allow": 3}
        add_type = option_map.get(option)
        if add_type is None:
            return {"status": "error", "message": "无效选项，请使用 allow/need_verify/not_allow"}
        try:
            await self._call(client, 'set_group_add_option', group_id=group_id, add_type=add_type)
            return {"status": "success", "message": f"✅ 加群方式已设置为: {option}"}
        except Exception as e:
            return {"status": "error", "message": f"设置失败: {_safe_error_msg(e)}"}

    async def send_group_sign(self, event: AiocqhttpMessageEvent) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            await self._call(client, 'send_group_sign', group_id=int(group_id))
            return {"status": "success", "message": "✅ 群打卡成功"}
        except Exception as e:
            return {"status": "error", "message": f"打卡失败: {_safe_error_msg(e)}"}

    async def set_qq_avatar(self, event: AiocqhttpMessageEvent, file: str = "") -> dict:
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        if not file:
            file = await self._get_image_file_from_event(event)
            if not file:
                return {"status": "error", "message": "❌ 请引用一张图片或提供图片路径/URL"}
        if not isinstance(file, str):
            return {"status": "error", "message": "❌ 图片数据无效"}
        file = await self._resolve_image_file(file)
        if not file:
            return {"status": "error", "message": "❌ 无法读取图片文件"}
        try:
            await self._call(client, 'set_qq_avatar', file=file)
            return {"status": "success", "message": "✅ QQ头像设置成功"}
        except Exception as e:
            logger.error(f"[QZoneTools] 设置QQ头像失败: {type(e).__name__}", exc_info=False)
            return {"status": "error", "message": "❌ 设置头像失败，请检查图片是否有效"}

    async def move_group_file(self, event: AiocqhttpMessageEvent, file_id: str, current_parent_directory: str, target_parent_directory: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            result = await self._call(client, 'move_group_file', group_id=group_id, file_id=file_id,
                                             current_parent_directory=current_parent_directory,
                                             target_parent_directory=target_parent_directory)
            if result.get('data', {}).get('ok'):
                return {"status": "success", "message": "✅ 文件移动成功"}
            return {"status": "error", "message": "移动失败"}
        except Exception as e:
            return {"status": "error", "message": f"移动失败: {_safe_error_msg(e)}"}

    async def rename_group_file(self, event: AiocqhttpMessageEvent, file_id: str, current_parent_directory: str, new_name: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            result = await self._call(client, 'rename_group_file', group_id=group_id, file_id=file_id,
                                             current_parent_directory=current_parent_directory, new_name=new_name)
            if result.get('data', {}).get('ok'):
                return {"status": "success", "message": f"✅ 文件已重命名为：{new_name}"}
            return {"status": "error", "message": "重命名失败"}
        except Exception as e:
            return {"status": "error", "message": f"重命名失败: {_safe_error_msg(e)}"}

    async def trans_group_file(self, event: AiocqhttpMessageEvent, file_id: str) -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            result = await self._call(client, 'trans_group_file', group_id=group_id, file_id=file_id)
            if result.get('data', {}).get('ok'):
                return {"status": "success", "message": "✅ 文件传输请求成功"}
            return {"status": "error", "message": "传输失败"}
        except Exception as e:
            return {"status": "error", "message": f"传输失败: {_safe_error_msg(e)}"}

    async def send_like_tool(self, event: AstrMessageEvent, user_id: str, times: int = 1) -> dict:
        if not user_id or not user_id.strip():
            return {"status": "error", "message": "❌ 参数缺失：请提供对方QQ号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            await self._call(client, 'send_like', user_id=user_id, times=min(times, 20))
            return {"status": "success", "message": f"✅ 已给 {user_id} 点赞 {times} 次"}
        except Exception as e:
            return {"status": "error", "message": f"点赞失败: {_safe_error_msg(e)}"}

    @staticmethod
    def _format_message_content(message_data) -> str:
        """将 OneBot v11 消息段数组或字符串转为可读文本。"""
        if isinstance(message_data, str):
            return message_data[:150]
        if not isinstance(message_data, list):
            return str(message_data)[:150]
        parts = []
        for seg in message_data:
            if not isinstance(seg, dict):
                parts.append(str(seg))
                continue
            seg_type = seg.get("type", "")
            seg_data = seg.get("data", {}) or {}
            if seg_type == "text":
                parts.append(seg_data.get("text", ""))
            elif seg_type == "image":
                parts.append("[图片]")
            elif seg_type == "face":
                parts.append("[表情]")
            elif seg_type == "at":
                parts.append(f"@{seg_data.get('qq', seg_data.get('name', '?') )}")
            elif seg_type == "reply":
                parts.append(f"[回复:{seg_data.get('id','?')}]")
            elif seg_type == "forward":
                parts.append("[合并转发]")
            elif seg_type == "video":
                parts.append("[视频]")
            elif seg_type == "voice":
                parts.append("[语音]")
            elif seg_type == "file":
                parts.append(f"[文件:{seg_data.get('name','?')}]")
            elif seg_type == "share":
                parts.append(f"[分享:{seg_data.get('title','?')}]")
            elif seg_type == "music":
                parts.append("[音乐]")
            elif seg_type == "location":
                parts.append("[位置]")
            elif seg_type == "contact":
                parts.append("[推荐]")
            elif seg_type == "json":
                parts.append("[卡片消息]")
            elif seg_type == "xml":
                parts.append("[XML消息]")
            elif seg_type == "poke":
                parts.append("[戳一戳]")
            elif seg_type == "node":
                parts.append("[合并转发节点]")
            else:
                text = seg_data.get("text", "")
                parts.append(text if text else f"[{seg_type}]")
        result = "".join(parts).strip()
        return result[:150] if result else "[空消息]"

    async def _call_history_api_with_seq(self, client, action: str, base_params: dict, count: int) -> list:
        """调用历史消息API，获取最新的 N 条消息。"""
        PAGE_SIZE = 200
        MAX_TOTAL = 999
        total_needed = min(count, MAX_TOTAL)

        def _build_params(seq=None):
            p = dict(base_params)
            if seq is not None:
                p["message_seq"] = seq
            p["count"] = PAGE_SIZE
            p["reverse_order"] = False
            p["disable_get_url"] = False
            p["parse_mult_msg"] = True
            p["quick_reply"] = False
            p["reverseOrder"] = False
            return p

        async def _fetch_page(seq=None) -> list:
            try:
                result = await self._call(client, action, **_build_params(seq))
                return result.get('messages', [])
            except Exception:
                return []

        all_messages: list = []

        # 1) 不传 message_seq 获取首批消息
        batch = await _fetch_page(seq=None)
        if not batch:
            return []
        all_messages = list(batch)
        max_id = max(m.get("message_id", 0) for m in batch)

        # 2) forward 补全：用最新 message_id 获取更新消息
        while True:
            newer_batch = await _fetch_page(seq=max_id)
            if not newer_batch:
                break
            new_stuff = newer_batch[1:] if newer_batch[0].get("message_id") == max_id else newer_batch
            if not new_stuff:
                break
            all_messages.extend(new_stuff)
            max_id = max(m.get("message_id", 0) for m in new_stuff)

        # 3) 反向翻页获取更早消息
        while len(all_messages) < total_needed:
            oldest_id = all_messages[0].get("message_id", 0)
            if not oldest_id:
                break
            p = _build_params(oldest_id)
            p["reverse_order"] = True
            p["reverseOrder"] = True
            try:
                result = await self._call(client, action, **p)
                older_batch = result.get('messages', [])
            except Exception:
                break
            if not older_batch:
                break
            if older_batch[-1].get("message_id") == oldest_id:
                older_batch = older_batch[:-1]
            if not older_batch:
                break
            all_messages = list(reversed(older_batch)) + all_messages
            if len(older_batch) < PAGE_SIZE:
                break

        return all_messages[-total_needed:]

    async def get_group_msg_history(self, event: AstrMessageEvent, group_id: str = "", count: int = 20) -> dict:
        if not group_id:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "请提供群号或在群聊中使用"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            base_params = {"group_id": str(group_id)}
            messages = await self._call_history_api_with_seq(client, "get_group_msg_history", base_params, count)
            if not messages:
                return {"status": "success", "message": f"群 {group_id} 暂无历史消息"}
            msg_count = len(messages)
            lines = [f"📜 群 {group_id} 最近{msg_count}条消息："]
            for msg in messages:
                sender = msg.get("sender", {}).get("nickname", msg.get("sender", {}).get("user_id", "未知"))
                content = self._format_message_content(msg.get("message", ""))
                lines.append(f"• {sender}: {content}")
            return {"status": "success", "message": "\n".join(lines)}
        except Exception as e:
            return {"status": "error", "message": f"获取失败: {type(e).__name__}: {_safe_error_msg(e)}"}

    async def get_friend_msg_history(self, event: AstrMessageEvent, user_id: str, count: int = 20) -> dict:
        if not user_id:
            return {"status": "error", "message": "请提供好友QQ号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            base_params = {"user_id": str(user_id)}
            messages = await self._call_history_api_with_seq(client, "get_friend_msg_history", base_params, count)
            if not messages:
                return {"status": "success", "message": f"好友 {user_id} 暂无历史消息"}
            msg_count = len(messages)
            lines = [f"📜 好友 {user_id} 最近{msg_count}条消息："]
            for msg in messages:
                sender = msg.get("sender", {}).get("nickname", msg.get("sender", {}).get("user_id", "未知"))
                content = self._format_message_content(msg.get("message", ""))
                lines.append(f"• {sender}: {content}")
            return {"status": "success", "message": "\n".join(lines)}
        except Exception as e:
            return {"status": "error", "message": f"获取失败: {type(e).__name__}: {_safe_error_msg(e)}"}

    async def set_group_portrait(self, event: AiocqhttpMessageEvent, group_id: str = "", file: str = "") -> dict:
        if not self.config.get("group_manage_enabled", True):
            return {"status": "error", "message": "❌ 群管理功能已禁用"}
        if not group_id:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "无法获取群号"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        if not file:
            file = await self._get_image_file_from_event(event)
            if not file:
                return {"status": "error", "message": "❌ 请引用一张图片或提供图片路径/URL"}
        file = await self._resolve_image_file(file)
        if not file:
            return {"status": "error", "message": "❌ 无法读取图片文件"}
        try:
            await self._call(client, 'set_group_portrait', group_id=group_id, file=file)
            return {"status": "success", "message": "✅ 群头像设置成功"}
        except Exception as e:
            logger.error(f"[QZoneTools] 设置群头像失败: {type(e).__name__}", exc_info=False)
            return {"status": "error", "message": "❌ 设置群头像失败，请检查图片是否有效"}

    async def fetch_custom_face(self, event: AstrMessageEvent, count: int = 48) -> dict:
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            result = await self._call(client, 'fetch_custom_face', count=count)
            faces = result.get('data', [])
            if not faces:
                return {"status": "success", "message": "暂无自定义表情"}
            lines = [f"🎭 自定义表情列表（共{len(faces)}个）："]
            for i, url in enumerate(faces[:10], 1):
                lines.append(f"{i}. {url}")
            return {"status": "success", "message": "\n".join(lines)}
        except Exception as e:
            return {"status": "error", "message": f"获取失败: {_safe_error_msg(e)}"}

    async def set_input_status_tool(self, event: AstrMessageEvent, user_id: str = "", event_type: int = 1) -> dict:
        if not user_id:
            if event.is_private_chat():
                user_id = event.get_sender_id()
            else:
                user_id = event.get_sender_id()
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        try:
            await self._call(client, 'set_input_status', user_id=user_id, event_type=event_type)
            status = "输入中" if event_type == 1 else "取消输入"
            return {"status": "success", "message": f"✅ 已设置状态：{status}"}
        except Exception as e:
            return {"status": "error", "message": f"设置失败: {_safe_error_msg(e)}"}

    async def get_ai_characters_tool(self, event: AstrMessageEvent) -> dict:
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "此功能仅支持群聊"}
        data = await self._get_ai_characters_raw(event, group_id)
        if not data:
            return {"status": "success", "message": "当前无可用的 AI 语音角色"}
        lines = ["🎤 可用的 AI 语音角色："]
        for cat in data:
            if not isinstance(cat, dict):
                continue
            cat_type = cat.get('type', '未分类')
            lines.append(f"\n▍{cat_type}：")
            for char in cat.get("characters", []):
                lines.append(f"  • {char.get('character_id', '')} - {char.get('character_name', '')}")
        return {"status": "success", "message": "\n".join(lines)}

    async def send_ai_voice_tool(self, event: AiocqhttpMessageEvent, text: str, character: str = "") -> dict:
        if not text or text.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供要朗读的文本内容。"}
        group_id = event.get_group_id()
        if not group_id:
            return {"status": "error", "message": "❌ 此功能仅支持群聊中使用"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        character_id = None
        if character:
            character_id = await self._get_character_id_by_name_or_id(event, group_id, character)
            if not character_id:
                return {"status": "error", "message": f"未找到角色: {character}"}
        else:
            if self.ai_default_character:
                character_id = await self._get_character_id_by_name_or_id(event, group_id, self.ai_default_character)
            if not character_id:
                data = await self._get_ai_characters_raw(event, group_id)
                for cat in data:
                    if isinstance(cat, dict) and cat.get("characters"):
                        first_char = cat["characters"][0]
                        character_id = str(first_char.get("character_id"))
                        break
                if not character_id:
                    return {"status": "error", "message": "没有可用的语音角色"}
        max_len = self.ai_voice_max_length
        actual_text = text
        if len(actual_text) > max_len:
            actual_text = actual_text[:max_len]
        try:
            await self._call(client, 'send_group_ai_record', group_id=group_id, character=character_id, text=actual_text, timeout=10)
            return {"status": "success", "message": f"✅ AI 语音已发送（角色ID: {character_id}），内容：{actual_text}"}
        except Exception as e:
            logger.error(f"[AI声聊] 发送失败: {e}")
            return {"status": "error", "message": f"发送 AI 语音失败: {_safe_error_msg(e)}"}

    async def search_contacts(self, event: AstrMessageEvent, keyword: str = "", search_type: str = "all") -> dict:
        if not keyword or keyword.strip() == "":
            return {"status": "error", "message": "❌ 参数缺失：请提供搜索关键词。"}
        if not self.config.get("search_enabled", True):
            return {"status": "error", "message": "联系人搜索功能已禁用"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        await self._update_contacts_cache(client)
        results = []
        keyword_lower = keyword.lower().strip()
        if search_type in ("all", "friend"):
            for friend in self._friends_cache:
                user_id = str(friend.get('user_id', ''))
                nickname = friend.get('nickname', '')
                remark = friend.get('remark', '')
                match = False
                if keyword_lower:
                    if keyword_lower in user_id or keyword_lower in nickname.lower() or keyword_lower in remark.lower():
                        match = True
                else:
                    match = True
                if match:
                    display_name = remark if remark else nickname
                    results.append(f"👤 好友 | {user_id} | {display_name}")
        if search_type in ("all", "group"):
            for group in self._groups_cache:
                group_id = str(group.get('group_id', ''))
                group_name = group.get('group_name', '')
                match = False
                if keyword_lower:
                    if keyword_lower in group_id or keyword_lower in group_name.lower():
                        match = True
                else:
                    match = True
                if match:
                    results.append(f"👥 群聊 | {group_id} | {group_name}")
        if not results:
            return {"status": "success", "message": f"未找到与「{keyword}」相关的联系人"}
        output = f"📇 搜索结果（共{len(results)}项）：\n" + "\n".join(results)
        return {"status": "success", "message": output}

    async def list_contacts(self, event: AstrMessageEvent, contact_type: str = "all", limit: int = 20) -> dict:
        if not self.config.get("search_enabled", True):
            return {"status": "error", "message": "联系人列表功能已禁用"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "错误：无法获取客户端"}
        await self._update_contacts_cache(client)
        limit = min(limit, 100)
        lines = []
        if contact_type in ("all", "friend"):
            friends = self._friends_cache[:limit]
            for f in friends:
                user_id = f.get('user_id', '')
                name = f.get('remark') or f.get('nickname', '')
                lines.append(f"👤 {user_id} | {name}")
            if contact_type == "friend":
                lines.insert(0, f"📋 好友列表（共{len(self._friends_cache)}，显示{len(friends)}）：")
        if contact_type in ("all", "group"):
            groups = self._groups_cache[:limit]
            for g in groups:
                group_id = g.get('group_id', '')
                name = g.get('group_name', '')
                lines.append(f"👥 {group_id} | {name}")
            if contact_type == "group":
                lines.insert(0, f"📋 群聊列表（共{len(self._groups_cache)}，显示{len(groups)}）：")
        if not lines:
            return {"status": "success", "message": "暂无联系人数据"}
        output = "\n".join(lines)
        return {"status": "success", "message": output}

    async def set_qq_profile_tool(self, event: AstrMessageEvent, nickname: str = None, personal_note: str = None) -> dict:
        if not nickname and not personal_note:
            return {"status": "error", "message": "❌ 至少需要提供 nickname 或 personal_note 之一"}
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}

        # NapCat 的 set_qq_profile 要求 nickname / personal_note 均为必填项，
        # 只传其中一个会报 "Schema compilation error: Expected required property"。
        # 因此未指定的字段先用当前资料回填（get_login_info 取昵称，本身即当前签名兜底为空串）。
        params = {"nickname": nickname or "", "personal_note": personal_note or ""}
        if not nickname or not personal_note:
            try:
                info = await self._call(client, 'get_login_info')
                data = info.get("data", info) if isinstance(info, dict) else {}
                if not nickname:
                    params["nickname"] = str(data.get("nickname") or "")
                if not personal_note:
                    params["personal_note"] = str(data.get("personal_note") or "")
            except Exception as e:
                logger.debug(f"获取当前资料失败，将使用空值回填: {e}")
            # 昵称仍然为空时无法提交（NapCat 不允许清空昵称）
            if not params["nickname"]:
                return {
                    "status": "error",
                    "message": "❌ 无法获取当前昵称。修改签名时必须同时提交昵称，请显式指定 nickname。",
                }

        try:
            await self._call(client, 'set_qq_profile', **params)
            changes = []
            if nickname:
                changes.append(f"昵称改为「{nickname}」")
            if personal_note:
                changes.append(f"签名改为「{personal_note}」")
            return {"status": "success", "message": f"✅ 已修改个人资料：{', '.join(changes)}"}
        except Exception as e:
            return {"status": "error", "message": f"设置失败: {_safe_error_msg(e)}"}

    # ==================== 工作区工具处理函数 ====================

    def _check_banned_patterns(self, code: str) -> Optional[str]:
        """检查代码是否包含禁止的模式。

        先做 AST 静态分析（识别 import 语句、动态导入调用），
        再用正则做补充匹配，最后追加用户自定义规则。
        """
        # ---------- 1) AST 静态分析 ----------
        ast_reason = self._check_banned_ast(code)
        if ast_reason:
            return ast_reason

        # ---------- 2) 正则补充 ----------
        default_banned = [
            r"\b__import__\s*\(",
            r"\bimportlib\s*\.\s*import_module\s*\(",
            r"\bgetattr\s*\(\s*__builtins__",
            r"\bglobals\s*\(\s*\)\s*\[",
            r"\bbuiltins\s*\.\s*__import__",
            r"\bos\s*\.\s*(system|popen|exec[lv]\w*|spawn\w*|fork)\s*\(",
            r"\bsubprocess\s*\.",
            r"\bctypes\s*\.",
            r"\bsocket\s*\.\s*socket\s*\(",
            r"\bcompile\s*\(",
            r"\beval\s*\(",
            r"\bexec\s*\(",
            r"\bpty\s*\.\s*spawn",
            r"\bopen\s*\(\s*['\"]/(etc|proc|root|www|AstrBot)",
        ]
        patterns = default_banned + (self.workspace_banned_patterns or [])
        import re
        for pattern in patterns:
            try:
                if re.search(pattern, code):
                    return pattern
            except re.error:
                if pattern in code:
                    return pattern
        return None

    def _check_banned_ast(self, code: str) -> Optional[str]:
        """基于 AST 的禁止模式检测，覆盖正则难以处理的动态导入。"""
        import ast as _ast

        banned_modules = {
            "subprocess", "os", "sys", "shutil", "ctypes", "multiprocessing",
            "socket", "http", "ftplib", "smtplib", "pty", "signal", "resource",
            "importlib", "pickle", "marshal", "builtins", "gc",
        }
        # 危险属性名
        banned_attrs = {"system", "popen", "__import__", "import_module"}
        # 动态取值入口
        dynamic_entry = {"getattr", "setattr", "globals", "locals", "vars",
                         "eval", "exec", "compile"}

        try:
            tree = _ast.parse(code)
        except SyntaxError:
            # 语法错误交给执行阶段报错，这里不拦
            return None

        for node in _ast.walk(tree):
            # import X / import X.Y
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in banned_modules:
                        return f"禁止导入模块: {root}"
            # from X import Y
            elif isinstance(node, _ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in banned_modules:
                    return f"禁止导入模块: {root}"
            # 属性访问：x.system / x.__import__
            elif isinstance(node, _ast.Attribute):
                if node.attr in banned_attrs:
                    return f"禁止调用: .{node.attr}"
            # 调用：getattr(x, "system") / getattr(__builtins__, ...)
            elif isinstance(node, _ast.Call):
                func = node.func
                fname = None
                if isinstance(func, _ast.Name):
                    fname = func.id
                elif isinstance(func, _ast.Attribute):
                    fname = func.attr

                if fname in dynamic_entry:
                    # 参数中若出现字符串形式的危险名，判定为绕过尝试
                    for arg in list(node.args) + [kw.value for kw in node.keywords]:
                        if isinstance(arg, _ast.Constant) and isinstance(arg.value, str):
                            val = arg.value.strip()
                            if (val in banned_attrs or val in banned_modules
                                    or val == "__builtins__"):
                                return f"禁止的动态调用: {fname}({val!r})"
                    # eval/exec/compile 本身就是危险入口
                    if fname in ("eval", "exec", "compile"):
                        return f"禁止调用: {fname}"
        return None

    async def run_python_code_tool(self, event: AstrMessageEvent, code: str) -> dict:
        """在工作区执行Python代码（异步 + 资源限制）"""
        if not self.workspace_enabled:
            return {"status": "error", "message": "工作区功能未启用，请在配置中开启"}
        if not code or not code.strip():
            return {"status": "error", "message": "请提供要执行的代码"}
        # 检查禁止模式（AST + 正则）
        banned = self._check_banned_patterns(code)
        if banned:
            return {"status": "error", "message": f"代码包含禁止的内容: {banned}"}

        import tempfile

        # 获取字体路径（不存在则自动下载，使用异步 HTTP）
        # 字体属于运行时下载产物，写入 data_dir 而非插件安装目录（市场上架要求）
        font_dir = os.path.join(self.data_dir, 'fonts')
        font_path = os.path.join(font_dir, 'NotoSansCJK-Regular.ttc')
        if not os.path.exists(font_path):
            await self._ensure_font(font_dir, font_path)

        # 注意：不再向用户脚本注入 `import os as _os`（曾导致沙箱可被绕过）
        font_config_code = ''
        if os.path.exists(font_path):
            font_config_code = (
                "\n# ===== 中文字体自动配置 =====\n"
                "_FONT_PATH = r'" + font_path + "'\n"
                "try:\n"
                "    import matplotlib\n"
                "    matplotlib.use('Agg')\n"
                "    import matplotlib.pyplot as _plt\n"
                "    from matplotlib import font_manager as _fm\n"
                "    _fm.fontManager.addfont(_FONT_PATH)\n"
                "    _plt.rcParams['font.sans-serif'] = ['Noto Sans CJK SC'] + _plt.rcParams.get('font.sans-serif', [])\n"
                "    _plt.rcParams['axes.unicode_minus'] = False\n"
                "except Exception:\n"
                "    pass\n"
                "# ===== 字体配置结束 =====\n"
            )

        # 用户代码包进函数体，避免污染全局命名空间
        script_content = (
            "import sys\n"
            "\n"
            "# 设置工作区路径\n"
            "def __setup__():\n"
            "    import os as __os__\n"
            "    __os__.chdir(r'" + self.workspace_dir + "')\n"
            "__setup__()\n"
            "del __setup__\n"
            + font_config_code +
            "\n# ===== 用户代码 =====\n"
            "def __user_code__():\n"
            + textwrap.indent(code, '    ') + "\n"
            "\ntry:\n"
            "    __user_code__()\n"
            "except Exception as _e:\n"
            "    import traceback\n"
            "    traceback.print_exc()\n"
            "    sys.exit(1)\n"
        )

        script_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False,
                                            encoding='utf-8', dir=self.workspace_dir) as f:
                f.write(script_content)
                script_path = f.name

            rc, stdout, stderr = await self._run_python_async(script_path, timeout=30)

            output = stdout
            if stderr:
                output += f"\n[stderr]\n{stderr}"
            if not output.strip():
                output = "代码执行完成（无输出）" if rc == 0 else f"执行失败（退出码 {rc}）"

            # 自动检测生成的图片文件
            if self.config.get("python_run_auto_image_enabled", True):
                image_exts = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
                new_images = []
                try:
                    for fname in os.listdir(self.workspace_dir):
                        fpath = os.path.join(self.workspace_dir, fname)
                        if os.path.isfile(fpath) and os.path.splitext(fname)[1].lower() in image_exts:
                            if os.path.getmtime(fpath) > (time.time() - 35):
                                new_images.append(fname)
                except Exception:
                    pass
                if new_images:
                    output += f"\n\n🖼️ 检测到生成的图片文件: {', '.join(new_images)}\n💡 使用 read_image 工具查看图片内容。"

            max_out = self.config.get("max_output_chars", 2000)
            try:
                max_out = max(200, int(max_out))
            except (TypeError, ValueError):
                max_out = 2000
            if len(output) > max_out:
                output = output[:max_out] + "\n... (输出过长已截断)"
            return {"status": "success", "message": output}
        except asyncio.TimeoutError:
            return {"status": "error", "message": "代码执行超时（超过30秒）"}
        except Exception as e:
            return {"status": "error", "message": f"执行失败: {_safe_error_msg(e)}"}
        finally:
            if script_path and os.path.exists(script_path):
                try:
                    os.unlink(script_path)
                except OSError:
                    pass

    async def _download_to_file(self, url: str, dest_path: str,
                                max_bytes: int = 100 * 1024 * 1024) -> bool:
        """异步下载 URL 到指定路径（带 SSRF 校验、大小限制、超时、原子写）。"""
        try:
            if not url.startswith(('http://', 'https://')):
                return False
            all_blocked = (list(DEFAULT_SSRF_BLACKLIST) + list(self.ssrf_blocked_urls)
                           + list(self.ssrf_custom_blocked_ranges))
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ""
            if host:
                reason = await _resolve_and_check_host(host, all_blocked)
                if reason:
                    logger.warning(f"[download] 拒绝下载: {reason}")
                    return False

            timeout = aiohttp.ClientTimeout(total=300, sock_read=60)
            tmp_path = dest_path + ".part"
            written = 0
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, allow_redirects=False) as resp:
                    if resp.status != 200:
                        logger.warning(f"[download] HTTP {resp.status}: {url[:80]}")
                        return False
                    with open(tmp_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            written += len(chunk)
                            if written > max_bytes:
                                logger.warning("[download] 文件超过大小上限，已中断")
                                f.close()
                                try:
                                    os.unlink(tmp_path)
                                except OSError:
                                    pass
                                return False
                            f.write(chunk)
            os.replace(tmp_path, dest_path)
            return True
        except Exception as e:
            logger.warning(f"[download] 下载失败: {_safe_error_msg(e)}")
            return False

    async def _ensure_font(self, font_dir: str, font_path: str) -> None:
        """异步下载中文字体（不阻塞事件循环）"""
        try:
            os.makedirs(font_dir, exist_ok=True)
            font_url = ('https://github.com/notofonts/noto-cjk/raw/main/Sans/'
                        'OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf')
            logger.info("[qzone_tools] 字体不存在，正在下载...")
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(font_url) as resp:
                    if resp.status != 200:
                        logger.warning(f"[qzone_tools] 字体下载失败: HTTP {resp.status}")
                        return
                    data = await resp.read()
            with open(font_path, 'wb') as f:
                f.write(data)
            logger.info(f"[qzone_tools] 字体下载完成: {font_path}")
        except Exception as e:
            logger.warning(f"[qzone_tools] 字体下载失败: {_safe_error_msg(e)}")

    async def _run_python_async(self, script_path: str, timeout: int = 30):
        """异步执行 Python 脚本，带资源限制。返回 (returncode, stdout, stderr)。"""
        def _limits():
            """子进程资源限制（仅 Linux 生效）"""
            try:
                import resource
                resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 5))
                resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 * 1024, 100 * 1024 * 1024))
                resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
                try:
                    mem = 2 * 1024 * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
                except (ValueError, OSError):
                    pass
            except Exception:
                pass

        # 受限环境变量：不继承敏感配置
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": self.workspace_dir,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "MPLBACKEND": "Agg",
            "TMPDIR": self.workspace_dir,
        }

        proc_kwargs = dict(
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace_dir,
            env=env,
        )
        # preexec_fn 仅 Unix 支持；不支持时降级（仍受 timeout 保护）
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-B", script_path,
                preexec_fn=_limits, **proc_kwargs,
            )
        except (NotImplementedError, ValueError, OSError):
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-B", script_path, **proc_kwargs,
            )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            raise
        return (
            proc.returncode,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )

    async def list_workspace_files_tool(self, event: AstrMessageEvent) -> dict:
        """列出工作区文件"""
        try:
            files = []
            for f in os.listdir(self.workspace_dir):
                if f.startswith('.'):
                    continue
                path = os.path.join(self.workspace_dir, f)
                size = os.path.getsize(path)
                if os.path.isfile(path):
                    files.append({"name": f, "size": size, "type": "file"})
            if not files:
                return {"status": "success", "message": "工作区为空"}
            lines = ["工作区文件列表:\n"]
            for f in sorted(files, key=lambda x: x['name']):
                size_str = f"{f['size']/1024:.1f}KB" if f['size'] < 1048576 else f"{f['size']/1048576:.1f}MB"
                lines.append(f"📄 {f['name']} ({size_str})")
            return {"status": "success", "message": "\n".join(lines)}
        except Exception as e:
            return {"status": "error", "message": f"获取文件列表失败: {_safe_error_msg(e)}"}

    async def read_workspace_file_tool(self, event: AstrMessageEvent, filename: str) -> dict:
        """读取工作区文件内容"""
        if not filename:
            return {"status": "error", "message": f"请提供文件名。文件需放在工作区: {self.workspace_dir}"}
        filepath = self._safe_resolve_path(filename)
        if not filepath:
            return {"status": "error", "message": f"文件不存在或路径不允许访问: {filename}（工作区: {self.workspace_dir}）"}
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            if len(content) > 3000:
                content = content[:3000] + "\n... (内容过长已截断)"
            return {"status": "success", "message": f"文件 {os.path.basename(filepath)} 内容:\n\n{content}"}
        except Exception as e:
            return {"status": "error", "message": f"读取文件失败: {_safe_error_msg(e)}"}

    async def read_image_tool(self, event: AstrMessageEvent, filename: str = None, **kwargs) -> dict:
        """读取工作区图片，返回base64"""
        if not filename:
            filename = kwargs.get('filename') or kwargs.get('file') or kwargs.get('name')
        if not filename:
            return {"status": "error", "message": f"请提供文件名参数。图片需放在工作区: {self.workspace_dir}"}
        # 路径安全解析（拒绝越界/软链接穿透）
        filepath = self._safe_resolve_path(filename)
        if not filepath:
            return {"status": "error", "message": f"文件不存在或路径不允许访问: {filename}（工作区: {self.workspace_dir}）"}
        filename = os.path.basename(filepath)
        # 检查是否是图片文件
        ext = os.path.splitext(filename)[1].lower()
        image_exts = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'}
        if ext not in image_exts:
            return {"status": "error", "message": f"不支持的图片格式: {ext}，支持: {', '.join(sorted(image_exts))}"}
        try:
            with open(filepath, 'rb') as f:
                img_data = f.read()
            b64 = base64.b64encode(img_data).decode('utf-8')
            mime = {
                '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
                '.svg': 'image/svg+xml'
            }.get(ext, 'image/png')
            # Truncate if too large (over 1MB)
            if len(b64) > 1_400_000:
                return {"status": "error", "message": f"图片过大（{len(img_data)/1024:.0f}KB），无法返回base64"}
            return {
                "status": "success",
                "message": f"图片 {filename} ({len(img_data)/1024:.1f}KB)",
                "image": f"data:{mime};base64,{b64}"
            }
        except Exception as e:
            return {"status": "error", "message": f"读取图片失败: {_safe_error_msg(e)}"}

    async def send_file_tool(self, event: AstrMessageEvent, filename: str = None, target_id: str = None,
                         chat_type: str = "auto", as_image: bool = False, **kwargs) -> dict:
        """发送工作区文件到QQ"""
        if not filename:
            filename = kwargs.get('filename') or kwargs.get('file') or kwargs.get('name')
        if not target_id:
            target_id = kwargs.get('target_id') or kwargs.get('target') or kwargs.get('chat_id')
        if not filename:
            return {"status": "error", "message": f"请提供文件名。文件需放在工作区: {self.workspace_dir}"}
        if not target_id:
            return {"status": "error", "message": "请提供目标群号或QQ号"}
        # 路径安全解析：只允许工作区 / 截图缓存 / 闪传目录内的文件
        filepath = self._safe_resolve_path(filename)
        if not filepath:
            return {"status": "error", "message": f"文件不存在或路径不允许访问: {filename}（工作区: {self.workspace_dir}）"}
        filename = os.path.basename(filepath)
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取客户端"}
        # 自动判断聊天类型
        if chat_type == "auto":
            await self._update_contacts_cache(client)
            is_group = any(str(g.get('group_id')) == target_id for g in self._groups_cache)
            chat_type = "group" if is_group else "private"
        try:
            import base64 as _b64
            ext = os.path.splitext(filename)[1].lower()
            image_exts = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
            if as_image and ext in image_exts:
                # 图片转 base64 发送（NapCat CQ 码不支持本地绝对路径）
                with open(filepath, 'rb') as f:
                    img_b64 = _b64.b64encode(f.read()).decode('utf-8')
                msg = f'[CQ:image,file=base64://{img_b64}]'
                if chat_type == "group":
                    await self._call(client, 'send_group_msg', group_id=int(target_id), message=msg)
                else:
                    await self._call(client, 'send_private_msg', user_id=int(target_id), message=msg)
                return {"status": "success", "message": f"✅ 已发送图片 {filename} 到 {target_id}"}
            else:
                # 非图片或非 as_image：以文件形式发送
                # 先尝试 upload_group_file/send_online_file（NapCat 原生文件发送）
                try:
                    if chat_type == "group":
                        await self._call(client, 'upload_group_file', group_id=int(target_id), file=filepath, name=filename)
                    else:
                        await self._call(client, 'send_online_file', user_id=int(target_id), file_path=filepath, file_name=filename)
                    return {"status": "success", "message": f"✅ 已发送文件 {filename} 到 {target_id}"}
                except Exception:
                    # 如果原生文件发送失败（如路径不可达），尝试 base64 方式
                    with open(filepath, 'rb') as f:
                        file_b64 = _b64.b64encode(f.read()).decode('utf-8')
                    file_ext = ext.lstrip('.')
                    mime_map = {'pdf': 'application/pdf', 'zip': 'application/zip',
                                'txt': 'text/plain', 'json': 'application/json',
                                'csv': 'text/csv', 'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
                    mime = mime_map.get(file_ext, 'application/octet-stream')
                    msg = f'[CQ:file,file=base64://{file_b64},file_name={filename}]'
                    if chat_type == "group":
                        await self._call(client, 'send_group_msg', group_id=int(target_id), message=msg)
                    else:
                        await self._call(client, 'send_private_msg', user_id=int(target_id), message=msg)
                    return {"status": "success", "message": f"✅ 已发送文件 {filename} 到 {target_id}"}
        except Exception as e:
            return {"status": "error", "message": f"发送失败: {_safe_error_msg(e)}"}

    async def delete_workspace_file_tool(self, event: AstrMessageEvent, filename: str) -> dict:
        """删除工作区文件"""
        if not filename:
            return {"status": "error", "message": f"请提供文件名。文件需放在工作区: {self.workspace_dir}"}
        # 防止路径穿越
        filename = os.path.basename(filename)
        filepath = os.path.join(self.workspace_dir, filename)
        if not os.path.exists(filepath):
            return {"status": "error", "message": f"文件不存在: {filename}（工作区: {self.workspace_dir}）"}
        try:
            os.remove(filepath)
            return {"status": "success", "message": f"已删除文件: {filename}"}
        except Exception as e:
            return {"status": "error", "message": f"删除文件失败: {_safe_error_msg(e)}"}

    async def fetch_url_tool(self, event: AstrMessageEvent, url: str, max_chars: int = 500) -> dict:
        """获取网页内容"""
        import html
        try:
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
            # SSRF 检查：字面量 + DNS 解析双重校验
            all_blocked = list(DEFAULT_SSRF_BLACKLIST) + list(self.ssrf_blocked_urls) + list(self.ssrf_custom_blocked_ranges)
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ""
            ssrf_reason = await _resolve_and_check_host(host, all_blocked) if host else None
            if ssrf_reason:
                return {"status": "error", "message": f"🚫 {ssrf_reason}"}

            async with aiohttp.ClientSession() as session:
                # 禁止自动跟随重定向，避免 302 跳到内网绕过检查
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30),
                                       allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        # 对重定向目标做同样的 SSRF 校验
                        redirect_url = urljoin(url, location)
                        r_host = urlparse(redirect_url).hostname or ""
                        r_reason = (await _resolve_and_check_host(r_host, all_blocked)
                                    if r_host else None)
                        if r_reason:
                            return {"status": "error", "message": f"🚫 重定向被安全策略阻断: {r_reason}"}
                        return {"status": "error",
                                "message": f"页面发生重定向（HTTP {resp.status}）到 {redirect_url}，请直接使用该地址。"}
                    if resp.status != 200:
                        return {"status": "error", "message": f"HTTP {resp.status}: {resp.reason}"}
                    text = await resp.text()
            
            # 移除 script 和 style 标签
            text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
            # 移除 HTML 标签
            text = re.sub(r'<[^>]+>', ' ', text)
            # 解码 HTML 实体
            text = html.unescape(text)
            # 压缩空白
            text = re.sub(r'\s+', ' ', text).strip()
            
            # 截断
            if max_chars and len(text) > max_chars:
                text = text[:max_chars] + '...'
            
            return {"status": "success", "message": f"网页内容 (\n{len(text)} 字):\n{text}"}
        except asyncio.TimeoutError:
            return {"status": "error", "message": "请求超时"}
        except Exception as e:
            return {"status": "error", "message": f"获取网页失败: {_safe_error_msg(e)}"}


    
    # ==================== 渲染模式路由（简单浏览器路径） ====================

    async def open_page_tool(self, event: AstrMessageEvent, url: str) -> dict:
        """打开网页"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        try:
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
            # SSRF 黑名单检查（字面量 + DNS 解析）
            all_blocked = list(DEFAULT_SSRF_BLACKLIST) + list(self.ssrf_blocked_urls) + list(self.ssrf_custom_blocked_ranges)
            from urllib.parse import urlparse as _urlparse
            _host = _urlparse(url).hostname or ""
            ssrf_reason = (await _resolve_and_check_host(_host, all_blocked)) if _host else None
            if ssrf_reason:
                return {"status": "error", "message": f"🚫 {ssrf_reason}"}
            result = await self.browser_supervisor.call("search", url=url)
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                return {"status": "success", "message": f"已打开: {url}\n\n💡 截图已自动展示给你。后续操作请基于截图输出坐标：点击用 browser_click(x,y)，长按用 browser_long_press，拖拽用 browser_drag，在输入框打字用 browser_input_at。坐标以截图左上角为原点。\n如需发送给用户，请用 send_file：filename 填 screenshot 字段的路径，target_id 填目标QQ号或群号，as_image=true。", "screenshot": screenshot}
            return {"status": "success", "message": f"已打开: {url}"}
        except Exception as e:
            return {"status": "error", "message": f"打开网页失败: {_safe_error_msg(e)}"}

    async def screenshot_page_tool(self, event: AstrMessageEvent, save_path: str = None) -> dict:
        """对当前网页截图"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}

        # 纯文本模式
        if self.config.get("llm_screenshot_text_only", False):
            try:
                page = self.browser_supervisor.browser.page
                if page:
                    text = await self._extract_page_text(page)
                    return {"status": "success", "message": "页面文本内容（纯文本模式）", "text": text}
            except Exception as e:
                return {"status": "error", "message": f"提取文本失败: {_safe_error_msg(e)}"}

        try:
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                # 如果指定了保存路径，复制过去
                if save_path:
                    import shutil
                    import os.path as _osp
                    real_save = _osp.realpath(save_path)
                    real_ws = _osp.realpath(self.workspace_dir)
                    if not real_save.startswith(real_ws):
                        return {"status": "error", "message": "保存路径必须在工作区目录内"}
                    shutil.copy2(screenshot, save_path)
                    return {"status": "success", "message": f"截图已保存: {save_path}"}
                return {"status": "success", "message": "截图成功\n\n💡 截图已自动展示给你。如需发送给用户，请用 send_file：filename 填 screenshot 字段的路径，target_id 填目标QQ号或群号，as_image=true。", "screenshot": screenshot}
            return {"status": "error", "message": "截图失败"}
        except Exception as e:
            return {"status": "error", "message": f"截图失败: {_safe_error_msg(e)}"}
    




    # ==================== 高级浏览器工具 ====================

    async def browser_search_tool(self, event: AstrMessageEvent, keyword: str, engine: str = "百度") -> dict:
        """搜索关键词"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        
        # 构建搜索URL
        engine_urls = {
            "百度": "https://www.baidu.com/s?wd={keyword}",
            "必应": "https://www.bing.com/search?q={keyword}",
            "谷歌": "https://www.google.com/search?q={keyword}",
        }
        url_template = engine_urls.get(engine, engine_urls["百度"])
        url = url_template.format(keyword=url_quote(keyword))
        
        try:
            await self.browser_supervisor.call("search", url=url)
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {"status": "success", "message": f"已搜索: {keyword}\n\n💡 截图已自动展示给你。如需发送给用户，请用 send_file：filename 填 screenshot 字段的路径，target_id 填目标QQ号或群号，as_image=true。", "screenshot": screenshot}
            return {"status": "success", "message": f"已搜索: {keyword}"}
        except Exception as e:
            return {"status": "error", "message": f"搜索失败: {_safe_error_msg(e)}"}

    async def browser_visit_tool(self, event: AstrMessageEvent, url: str) -> dict:
        """访问指定链接"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        # SSRF 黑名单检查（字面量 + DNS 解析）
        all_blocked = list(DEFAULT_SSRF_BLACKLIST) + list(self.ssrf_blocked_urls) + list(self.ssrf_custom_blocked_ranges)
        from urllib.parse import urlparse as _urlparse
        _host = _urlparse(url).hostname or ""
        ssrf_reason = (await _resolve_and_check_host(_host, all_blocked)) if _host else None
        if ssrf_reason:
            return {"status": "error", "message": f"🚫 {ssrf_reason}"}
        try:
            await self.browser_supervisor.call("search", url=url)
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {"status": "success", "message": f"已访问: {url}\n\n💡 截图已自动展示给你。如需发送给用户，请用 send_file：filename 填 screenshot 字段的路径，target_id 填目标QQ号或群号，as_image=true。", "screenshot": screenshot}
            return {"status": "success", "message": f"已访问: {url}"}
        except Exception as e:
            return {"status": "error", "message": f"访问失败: {_safe_error_msg(e)}"}

    async def browser_click_tool(self, event: AstrMessageEvent, x: int, y: int) -> dict:
        """点击页面坐标"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        
        try:
            await self.browser_supervisor.call("click_coord", coords=[x, y])
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {"status": "success", "message": f"已点击: ({x}, {y})\n\n💡 截图已自动展示给你。如需发送给用户，请用 send_file：filename 填 screenshot 字段的路径，target_id 填目标QQ号或群号，as_image=true。", "screenshot": screenshot}
            return {"status": "success", "message": f"已点击: ({x}, {y})"}
        except Exception as e:
            return {"status": "error", "message": f"点击失败: {_safe_error_msg(e)}"}

    async def browser_input_tool(self, event: AstrMessageEvent, text: str, enter: bool = True) -> dict:
        """输入文字"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        
        try:
            await self.browser_supervisor.call("text_input", text=text, enter=enter)
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {"status": "success", "message": f"已输入: {text[:50]}\n\n💡 截图已自动展示给你。如需发送给用户，请用 send_file：filename 填 screenshot 字段的路径，target_id 填目标QQ号或群号，as_image=true。", "screenshot": screenshot}
            return {"status": "success", "message": f"已输入: {text[:50]}"}
        except Exception as e:
            return {"status": "error", "message": f"输入失败: {_safe_error_msg(e)}"}

    async def browser_scroll_tool(self, event: AstrMessageEvent, direction: str = "下", distance: int = 1300) -> dict:
        """滚动页面"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}

        try:
            await self.browser_supervisor.call("scroll_by", distance=distance, direction=direction)
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {"status": "success", "message": f"已向{direction}滚动{distance}px", "screenshot": screenshot}
            return {"status": "success", "message": f"已向{direction}滚动{distance}px"}
        except Exception as e:
            return {"status": "error", "message": f"滚动失败: {_safe_error_msg(e)}"}

    async def browser_wait_tool(self, event: AstrMessageEvent, seconds: int = 10) -> dict:
        """等待页面加载/跳转/倒计时，期间先通知用户，结束后自动截图回传。

        一次调用完成「通知用户 + 等待 + 重看页面」，
        避免模型反复调用截图工具轮询浪费上下文。
        """
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}

        # 等待时长限制 5-45 秒
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            seconds = 10
        seconds = max(5, min(seconds, 45))

        # 1) 立即通知用户（不等模型回复，直接发送到当前会话）
        try:
            await event.send(MessageChain().message(f"⏳ AI正在等待网页→{seconds}s"))
        except Exception as e:
            logger.debug(f"[browser_wait] 通知用户失败（不影响等待）: {e}")

        # 2) 等待页面（可被取消）
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            raise

        # 3) 等待结束后截图回传给 LLM
        try:
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {
                    "status": "success",
                    "message": (f"已等待 {seconds} 秒。💡 等待后的页面截图已自动展示给你，"
                                f"请观察页面当前状态决定下一步。"),
                    "screenshot": screenshot,
                }
            return {"status": "success", "message": f"已等待 {seconds} 秒（截图失败，可用 browser_screenshot 重试）"}
        except Exception as e:
            return {"status": "error", "message": f"等待完成但截图失败: {_safe_error_msg(e)}"}

    async def _coord_action(self, action: str, action_desc: str, **kwargs) -> dict:
        """坐标交互通用流程：执行动作 → 截图回传给 LLM 看结果。"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        try:
            err = await self.browser_supervisor.call(action, **kwargs)
            if err:
                return {"status": "error", "message": err}
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {
                    "status": "success",
                    "message": (f"{action_desc}\n\n💡 操作后的截图已自动展示给你。请观察结果决定下一步。"
                                f"如需发送给用户，请用 send_file：filename 填 screenshot 字段的路径，"
                                f"target_id 填目标QQ号或群号，as_image=true。"),
                    "screenshot": screenshot,
                }
            return {"status": "success", "message": action_desc}
        except Exception as e:
            return {"status": "error", "message": f"操作失败: {_safe_error_msg(e)}"}

    async def browser_double_click_tool(self, event: AstrMessageEvent, x: int, y: int) -> dict:
        """双击坐标"""
        return await self._coord_action("double_click", f"已双击坐标 ({x}, {y})", x=x, y=y)

    async def browser_right_click_tool(self, event: AstrMessageEvent, x: int, y: int) -> dict:
        """右键坐标"""
        return await self._coord_action("right_click", f"已右键坐标 ({x}, {y})", x=x, y=y)

    async def browser_long_press_tool(self, event: AstrMessageEvent, x: int, y: int, duration_ms: int = 1000) -> dict:
        """长按坐标"""
        try:
            duration_ms = int(duration_ms)
        except (TypeError, ValueError):
            duration_ms = 1000
        return await self._coord_action(
            "long_press", f"已在坐标 ({x}, {y}) 长按 {duration_ms}ms",
            x=x, y=y, duration_ms=duration_ms,
        )

    async def browser_drag_tool(self, event: AstrMessageEvent, start_x: int, start_y: int,
                                end_x: int, end_y: int, duration_ms: int = 600) -> dict:
        """拖拽"""
        try:
            duration_ms = int(duration_ms)
        except (TypeError, ValueError):
            duration_ms = 600
        return await self._coord_action(
            "drag", f"已从 ({start_x}, {start_y}) 拖拽到 ({end_x}, {end_y})",
            start_x=start_x, start_y=start_y, end_x=end_x, end_y=end_y,
            duration_ms=duration_ms,
        )

    async def browser_input_at_tool(self, event: AstrMessageEvent, x: int, y: int,
                                    text: str, press_enter: bool = False) -> dict:
        """点击坐标输入文字"""
        return await self._coord_action(
            "input_at", f"已在 ({x}, {y}) 输入: {str(text)[:50]}",
            x=x, y=y, text=text, press_enter=press_enter,
        )

    async def browser_hover_tool(self, event: AstrMessageEvent, x: int, y: int) -> dict:
        """悬停坐标"""
        return await self._coord_action("hover", f"已悬停在坐标 ({x}, {y})", x=x, y=y)

    async def browser_zoom_tool(self, event: AstrMessageEvent, scale: float = 1.5) -> dict:
        """缩放页面"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        
        try:
            await self.browser_supervisor.call("zoom_to_scale", scale=scale)
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {"status": "success", "message": f"已缩放到 {scale}x", "screenshot": screenshot}
            return {"status": "success", "message": f"已缩放到 {scale}x"}
        except Exception as e:
            return {"status": "error", "message": f"缩放失败: {_safe_error_msg(e)}"}

    async def browser_screenshot_tool(self, event: AstrMessageEvent, full_page: bool = False, zoom_factor: float = None) -> dict:
        """截图"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        
        # 纯文本模式：返回页面文本而非截图
        if self.config.get("llm_screenshot_text_only", False):
            try:
                browser = self.browser_supervisor.browser
                if browser and browser.page:
                    text = await self._extract_page_text(browser.page)
                    return {"status": "success", "message": "页面文本内容（纯文本模式）", "text": text}
                return {"status": "error", "message": "无可用页面"}
            except Exception as e:
                return {"status": "error", "message": f"提取文本失败: {_safe_error_msg(e)}"}
        
        try:
            screenshot = await self.browser_supervisor.call("screenshot", full_page=full_page, zoom_factor=zoom_factor)
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {"status": "success", "message": "截图成功\n\n💡 截图已自动展示给你。如需发送给用户，请用 send_file：filename 填 screenshot 字段的路径，target_id 填目标QQ号或群号，as_image=true。", "screenshot": screenshot}
            return {"status": "error", "message": "截图失败"}
        except Exception as e:
            return {"status": "error", "message": f"截图失败: {_safe_error_msg(e)}"}

    async def browser_back_tool(self, event: AstrMessageEvent) -> dict:
        """返回上一页"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        
        try:
            await self.browser_supervisor.call("go_back")
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {"status": "success", "message": "已返回上一页\n\n💡 截图已自动展示给你。如需发送给用户，请用 send_file：filename 填 screenshot 字段的路径，target_id 填目标QQ号或群号，as_image=true。", "screenshot": screenshot}
            return {"status": "success", "message": "已返回上一页"}
        except Exception as e:
            return {"status": "error", "message": f"返回失败: {_safe_error_msg(e)}"}

    async def browser_forward_tool(self, event: AstrMessageEvent) -> dict:
        """前进到下一页"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        
        try:
            await self.browser_supervisor.call("go_forward")
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {"status": "success", "message": "已前进到下一页", "screenshot": screenshot}
            return {"status": "success", "message": "已前进到下一页"}
        except Exception as e:
            return {"status": "error", "message": f"前进失败: {_safe_error_msg(e)}"}

    async def browser_tabs_tool(self, event: AstrMessageEvent, index: int = None) -> dict:
        """查看标签页或切换标签页"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        
        try:
            if index is not None:
                await self.browser_supervisor.call("switch_tab", index=index - 1)
                screenshot = await self.browser_supervisor.call("screenshot")
                if screenshot:
                    screenshot = self._convert_image(screenshot)
                    return {"status": "success", "message": f"已切换到标签页 {index}", "screenshot": screenshot}
                return {"status": "success", "message": f"已切换到标签页 {index}"}
            else:
                titles = await self.browser_supervisor.call("get_all_tabs_titles")
                if not titles:
                    return {"status": "success", "message": "暂无打开的标签页"}
                text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
                return {"status": "success", "message": f"标签页列表:\n{text}"}
        except Exception as e:
            return {"status": "error", "message": f"标签页操作失败: {_safe_error_msg(e)}"}

    async def browser_close_tab_tool(self, event: AstrMessageEvent, index: int) -> dict:
        """关闭标签页"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        
        try:
            result = await self.browser_supervisor.call("close_tab", index=index - 1)
            return {"status": "success", "message": result or f"已关闭标签页 {index}"}
        except Exception as e:
            return {"status": "error", "message": f"关闭标签页失败: {_safe_error_msg(e)}"}

    async def browser_close_tool(self, event: AstrMessageEvent) -> dict:
        """关闭浏览器"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        
        try:
            await self.browser_supervisor._stop_browser()
            return {"status": "success", "message": "浏览器已关闭"}
        except Exception as e:
            return {"status": "error", "message": f"关闭浏览器失败: {_safe_error_msg(e)}"}

    async def browser_chat_tool(self, event: AstrMessageEvent, text: str) -> dict:
        """向当前页面发送对话"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        
        try:
            await self.browser_supervisor.call("chat_send", text=text)
            screenshot = await self.browser_supervisor.call("screenshot")
            if screenshot:
                screenshot = self._convert_image(screenshot)
                return {"status": "success", "message": f"已发送: {text[:50]}", "screenshot": screenshot}
            return {"status": "success", "message": f"已发送: {text[:50]}"}
        except Exception as e:
            return {"status": "error", "message": f"发送失败: {_safe_error_msg(e)}"}

    async def browser_favorite_list_tool(self, event: AstrMessageEvent) -> dict:
        """查看收藏夹"""
        if not self.fav_mgr:
            return {"status": "error", "message": "收藏夹管理器未初始化"}
        
        favorites = self.fav_mgr.dump()
        if not favorites:
            return {"status": "success", "message": "收藏夹为空"}
        
        lines = [f"{idx}. {name}: {url}" for idx, (name, url) in enumerate(favorites.items(), 1)]
        return {"status": "success", "message": "收藏夹列表:\n" + "\n".join(lines)}

    async def browser_favorite_add_tool(self, event: AstrMessageEvent, name: str, url: str) -> dict:
        """添加收藏"""
        if not self.fav_mgr:
            return {"status": "error", "message": "收藏夹管理器未初始化"}
        
        if self.fav_mgr.add(name, url):
            return {"status": "success", "message": f"已收藏: {name}"}
        return {"status": "success", "message": f"{name} 已存在于收藏夹中"}

    async def browser_favorite_delete_tool(self, event: AstrMessageEvent, name: str) -> dict:
        """删除收藏"""
        if not self.fav_mgr:
            return {"status": "error", "message": "收藏夹管理器未初始化"}
        
        if self.fav_mgr.remove(name):
            return {"status": "success", "message": f"已取消收藏: {name}"}
        return {"status": "success", "message": f"{name} 不在收藏夹中"}

    async def browser_install_tool(self, event: AstrMessageEvent, browser_type: str = "chromium") -> dict:
        """安装浏览器（统一走 BrowserDownloader：并发安全 + 安装后验证）"""
        try:
            from .core.downloader import BrowserDownloader
            downloader = BrowserDownloader(Path(self.data_dir))
            ok, msg = await downloader.download(browser_type)
            if ok:
                return {"status": "success", "message": f"✅ {msg}"}
            return {"status": "error", "message": f"安装失败: {msg}"}
        except Exception as e:
            return {"status": "error", "message": f"安装失败: {_safe_error_msg(e)}"}

    # ==================== 简化版浏览器工具（统一走 browser_supervisor） ====================

    async def close_page_tool(self, event: AstrMessageEvent) -> dict:
        """关闭浏览器"""
        if not self.browser_supervisor:
            return {"status": "error", "message": "浏览器管理器未初始化"}
        try:
            await self.browser_supervisor._stop_browser()
            return {"status": "success", "message": "浏览器已关闭"}
        except Exception as e:
            return {"status": "error", "message": f"关闭浏览器失败: {_safe_error_msg(e)}"}

    # ==================== 闪传工具 ====================

    async def _copy_file_to_napcat(self, file_path: str) -> str:
        filename = os.path.basename(file_path)
        # 清理文件名，防止路径注入
        filename = re.sub(r'[^a-zA-Z0-9._\-]', '_', filename)
        # NapCat 容器内的临时目录
        napcat_dest = f"/tmp/{filename}"
        container_name = self.docker_container_name

        try:
            # 使用 docker cp 将文件复制到 napcat 容器（异步，避免阻塞事件循环）
            cmd = ["docker", "cp", file_path, f"{container_name}:{napcat_dest}"]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                logger.error("[create_flash_task] docker cp 超时")
                return file_path
            if proc.returncode != 0:
                logger.error(
                    f"[create_flash_task] docker cp 失败: "
                    f"{stderr_b.decode('utf-8', errors='replace')[:200]}"
                )
                return file_path  # 失败时返回原路径
            logger.info(f"[create_flash_task] 文件已复制到 {container_name} 容器: {napcat_dest}")
            return napcat_dest
        except Exception as e:
            logger.error(f"[create_flash_task] docker cp 异常: {_safe_error_msg(e)}")
            return file_path
    
    async def create_flash_task_tool(self, event: AstrMessageEvent, files: str, name: str = None, thumb_path: str = None) -> dict:
        """创建闪传任务并发送到当前会话"""
        import shutil
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            file_list = [f.strip() for f in files.split(',') if f.strip()]
            
            # 检查文件是否存在
            missing_files = [f for f in file_list if not os.path.exists(f)]
            if missing_files:
                return {
                    "status": "error",
                    "message": f"❌ 文件不存在: {', '.join(missing_files)}"
                }
            
            # 检查是否在 Docker 环境中
            in_docker = os.path.exists('/.dockerenv')
            
            # 检查是否需要复制文件到 NapCat 容器
            napcat_container = self.config.get("napcat_container_name", "napcat")
            need_copy = False
            
            # 如果在 Docker 中，检查文件是否在 NapCat 可访问的路径
            if in_docker:
                # 检查共享目录是否可用
                flash_dir = self.config.get("flash_transfer_dir", "")
                if flash_dir:
                    # 尝试复制到共享目录
                    transfer_files = []
                    for f in file_list:
                        try:
                            filename = os.path.basename(f)
                            dest = os.path.join(flash_dir, filename)
                            if f != dest:
                                shutil.copy2(f, dest)
                            transfer_files.append(dest)
                        except Exception:
                            transfer_files.append(f)
                else:
                    # 没有共享目录，使用 docker cp
                    need_copy = True
                    transfer_files = []
                    for f in file_list:
                        napcat_path = await self._copy_file_to_napcat(f)
                        transfer_files.append(napcat_path)
            else:
                # 非 Docker 环境，直接使用文件路径
                transfer_files = file_list
            
            params = {"files": transfer_files if len(transfer_files) > 1 else transfer_files[0]}
            if name:
                params["name"] = name
            if thumb_path:
                params["thumb_path"] = thumb_path
            
            logger.info(f"[create_flash_task] 调用参数: {params}")
            result = await self._call(client, 'create_flash_task', **params)
            logger.info(f"[create_flash_task] 返回结果: {result}")
            
            if not result:
                return {"status": "error", "message": "创建闪传任务失败：无返回结果"}
            
            # 检查API返回的错误
            api_result = result.get('result', 0)
            err_msg = result.get('errMsg', '')
            if api_result != 0 and err_msg:
                error_msg = f"❌ NapCat API 错误: {err_msg}\n"
                error_msg += f"\n文件路径: {transfer_files}\n"
                
                if "文件不存在" in err_msg:
                    if in_docker:
                        error_msg += "\n⚠️ Docker 环境提示：\n"
                        error_msg += "闪传功能需要 AstrBot 和 NapCat 共享文件目录。\n"
                        error_msg += "请配置 flash_transfer_dir 并在两个容器中挂载同一目录。\n"
                        error_msg += f"当前中转目录: {self.flash_transfer_dir}\n\n"
                        error_msg += "Docker 启动示例：\n"
                        error_msg += f"docker run -v /你的共享目录:{self.flash_transfer_dir}:rw ..."
                    else:
                        error_msg += "\n⚠️ 请确保文件路径正确且 NapCat 可以访问该文件。"
                
                return {"status": "error", "message": error_msg}
            
            # NapCat返回: {createFlashTransferResult: {fileSetId, shareLink, ...}}
            flash_result = result.get('createFlashTransferResult', {})
            fileset_id = flash_result.get('fileSetId', '') or result.get('fileset_id', '') or result.get('task_id', '')
            share_link = flash_result.get('shareLink', '') or result.get('share_link', '')
            
            if not fileset_id:
                return {"status": "error", "message": f"创建闪传任务失败：未获取到fileSetId\n返回: {str(result)[:500]}"}
            
            # 发送闪传消息到当前会话
            msg_sent = False
            msg_error = ''
            try:
                group_id = event.get_group_id() if hasattr(event, 'get_group_id') else None
                user_id = event.get_sender_id() if hasattr(event, 'get_sender_id') else None
                
                flash_params = {"fileset_id": fileset_id}
                if group_id:
                    flash_params["group_id"] = str(group_id)
                    logger.info(f"[create_flash_task] 发送到群: {group_id}")
                elif user_id:
                    flash_params["user_id"] = str(user_id)
                    logger.info(f"[create_flash_task] 发送到用户: {user_id}")
                else:
                    logger.warning(f"[create_flash_task] 无法获取群ID或用户ID")
                    msg_error = "无法获取当前会话的群ID或用户ID"
                
                if flash_params.get('group_id') or flash_params.get('user_id'):
                    send_result = await self._call(client, 'send_flash_msg', **flash_params)
                    logger.info(f"[create_flash_task] 发送闪传消息结果: {send_result}")
                    msg_sent = True
            except Exception as msg_err:
                logger.error(f"[create_flash_task] 发送闪传消息失败: {msg_err}", exc_info=True)
                msg_error = str(msg_err)[:200]
            
            # 构建响应
            lines = ["✅ 闪传任务已创建"]
            if fileset_id:
                lines.append(f"文件集ID: {fileset_id}")
            if share_link:
                lines.append(f"分享链接: {share_link}")
            if msg_sent:
                lines.append("✅ 闪传消息已发送到当前会话")
            elif msg_error:
                lines.append(f"❌ 发送失败: {msg_error}")
            
            return {"status": "success", "message": "\n".join(lines)}
        except Exception as e:
            logger.error(f"[create_flash_task] 失败: {e}", exc_info=True)
            return {"status": "error", "message": f"创建闪传任务失败: {_safe_error_msg(e)}"}

    async def get_flash_file_list_tool(self, event: AstrMessageEvent, fileset_id: str) -> dict:
        """获取闪传文件列表"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            result = await self._call(client, 'get_flash_file_list', fileset_id=fileset_id)
            if result and isinstance(result, list):
                if not result:
                    return {"status": "success", "message": "该闪传任务没有文件"}
                lines = [f"共 {len(result)} 个文件："]
                for i, f in enumerate(result[:20], 1):
                    name = f.get('file_name', '未知')
                    size = f.get('size', 0)
                    size_str = f"{size/1024:.1f}KB" if size < 1048576 else f"{size/1048576:.1f}MB"
                    lines.append(f"{i}. {name} ({size_str})")
                return {"status": "success", "message": "\n".join(lines)}
            return {"status": "success", "message": "该闪传任务没有文件"}
        except Exception as e:
            return {"status": "error", "message": f"获取闪传文件列表失败: {_safe_error_msg(e)}"}

    async def get_flash_file_url_tool(self, event: AstrMessageEvent, fileset_id: str, file_name: str = None, file_index: int = None) -> dict:
        """获取闪传文件下载链接"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            params = {"fileset_id": fileset_id}
            if file_name:
                params["file_name"] = file_name
            if file_index is not None:
                params["file_index"] = file_index
            result = await self._call(client, 'get_flash_file_url', **params)
            url = result.get('url', '') if result else ''
            if url:
                return {"status": "success", "message": f"下载链接: {url}"}
            return {"status": "error", "message": "未获取到下载链接"}
        except Exception as e:
            return {"status": "error", "message": f"获取闪传链接失败: {_safe_error_msg(e)}"}

    async def send_flash_msg_tool(self, event: AstrMessageEvent, fileset_id: str, user_id: str = None, group_id: str = None) -> dict:
        """发送闪传消息"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            params = {"fileset_id": fileset_id}
            if user_id:
                params["user_id"] = user_id
            if group_id:
                params["group_id"] = group_id
            result = await self._call(client, 'send_flash_msg', **params)
            msg_id = result.get('message_id', '') if result else ''
            return {"status": "success", "message": f"✅ 闪传消息已发送\n消息ID: {msg_id}"}
        except Exception as e:
            return {"status": "error", "message": f"发送闪传消息失败: {_safe_error_msg(e)}"}

    async def get_share_link_tool(self, event: AstrMessageEvent, fileset_id: str) -> dict:
        """获取文件分享链接"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            result = await self._call(client, 'get_share_link', fileset_id=fileset_id)
            link = result if isinstance(result, str) else (result.get('data', '') if result else '')
            if link:
                return {"status": "success", "message": f"分享链接: {link}"}
            return {"status": "error", "message": "未获取到分享链接"}
        except Exception as e:
            return {"status": "error", "message": f"获取分享链接失败: {_safe_error_msg(e)}"}

    async def get_fileset_info_tool(self, event: AstrMessageEvent, fileset_id: str) -> dict:
        """获取文件集信息"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            result = await self._call(client, 'get_fileset_info', fileset_id=fileset_id)
            if result:
                files = result.get('file_list', [])
                lines = [f"文件集ID: {result.get('fileset_id', fileset_id)}"]
                if files:
                    lines.append(f"包含 {len(files)} 个文件")
                return {"status": "success", "message": "\n".join(lines)}
            return {"status": "success", "message": f"文件集ID: {fileset_id}"}
        except Exception as e:
            return {"status": "error", "message": f"获取文件集信息失败: {_safe_error_msg(e)}"}

    async def get_fileset_id_tool(self, event: AstrMessageEvent, share_code: str) -> dict:
        """通过分享码获取文件集ID"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            result = await self._call(client, 'get_fileset_id', share_code=share_code)
            fileset_id = result.get('fileset_id', '') if result else ''
            if fileset_id:
                return {"status": "success", "message": f"文件集ID: {fileset_id}"}
            return {"status": "error", "message": "未找到对应的文件集"}
        except Exception as e:
            return {"status": "error", "message": f"获取文件集ID失败: {_safe_error_msg(e)}"}

    async def download_fileset_tool(self, event: AstrMessageEvent, fileset_id: str) -> dict:
        """下载文件集并保存到工作区"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            # 先获取文件列表
            try:
                files_result = await self._call(client, 'get_flash_file_list', fileset_id=fileset_id)
                if files_result and isinstance(files_result, list):
                    downloaded = []
                    for file_item in files_result:
                        # 文件名净化，防止路径穿越写入工作区外
                        raw_name = str(file_item.get('file_name', '') or '')
                        file_name = os.path.basename(raw_name.replace("\\", "/"))
                        if not file_name or file_name in (".", ".."):
                            continue
                        # 尝试获取下载链接
                        try:
                            url_result = await self._call(client, 'get_flash_file_url', fileset_id=fileset_id, file_name=raw_name)
                            url = (url_result or {}).get('url')
                            if url:
                                dest_path = os.path.join(self.workspace_dir, file_name)
                                if await self._download_to_file(url, dest_path):
                                    downloaded.append(file_name)
                        except Exception as dl_err:
                            logger.warning(f"[download_fileset] 下载文件 {file_name} 失败: {dl_err}")
                    if downloaded:
                        return {"status": "success", "message": f"✅ 已下载 {len(downloaded)} 个文件到工作区:\n" + "\n".join(downloaded)}
            except Exception as list_err:
                logger.warning(f"[download_fileset] 获取文件列表失败: {list_err}")
            # 如果上面失败，发送下载请求
            await self._call(client, 'download_fileset', fileset_id=fileset_id)
            return {"status": "success", "message": f"✅ 文件集下载请求已发送"}
        except Exception as e:
            return {"status": "error", "message": f"下载文件集失败: {_safe_error_msg(e)}"}

    async def get_online_file_msg_tool(self, event: AstrMessageEvent, user_id: str) -> dict:
        """获取在线文件消息"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            result = await self._call(client, 'get_online_file_msg', user_id=user_id)
            return {"status": "success", "message": f"已获取在线文件消息"}
        except Exception as e:
            return {"status": "error", "message": f"获取在线文件消息失败: {_safe_error_msg(e)}"}

    async def send_online_file_tool(self, event: AstrMessageEvent, user_id: str, file_path: str, file_name: str = None) -> dict:
        """发送在线文件"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            params = {"user_id": user_id, "file_path": file_path}
            if file_name:
                params["file_name"] = file_name
            result = await self._call(client, 'send_online_file', **params)
            return {"status": "success", "message": f"✅ 文件发送请求已发送"}
        except Exception as e:
            return {"status": "error", "message": f"发送文件失败: {_safe_error_msg(e)}"}

    async def send_online_folder_tool(self, event: AstrMessageEvent, user_id: str, folder_path: str, folder_name: str = None) -> dict:
        """发送在线文件夹"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            params = {"user_id": user_id, "folder_path": folder_path}
            if folder_name:
                params["folder_name"] = folder_name
            result = await self._call(client, 'send_online_folder', **params)
            return {"status": "success", "message": f"✅ 文件夹发送请求已发送"}
        except Exception as e:
            return {"status": "error", "message": f"发送文件夹失败: {_safe_error_msg(e)}"}

    async def receive_online_file_tool(self, event: AstrMessageEvent, user_id: str, msg_id: str, element_id: str) -> dict:
        """接收在线文件并保存到工作区"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            # 发送接收请求
            await self._call(client, 'receive_online_file', user_id=user_id, msg_id=msg_id, element_id=element_id)
            # 尝试获取文件信息并下载到工作区
            try:
                file_info = await self._call(client, 'get_file', file_id=element_id)
                if file_info and file_info.get('file'):
                    file_path = file_info['file']
                    file_name = file_info.get('file_name', os.path.basename(file_path))
                    # 复制文件到工作区
                    import shutil
                    dest_path = os.path.join(self.workspace_dir, file_name)
                    shutil.copy2(file_path, dest_path)
                    return {"status": "success", "message": f"✅ 文件已接收并保存到工作区: {file_name}"}
            except Exception as inner_e:
                logger.warning(f"[receive_online_file] 下载文件到工作区失败: {inner_e}")
            return {"status": "success", "message": f"✅ 文件接收请求已发送"}
        except Exception as e:
            return {"status": "error", "message": f"接收文件失败: {_safe_error_msg(e)}"}

    async def refuse_online_file_tool(self, event: AstrMessageEvent, user_id: str, msg_id: str, element_id: str) -> dict:
        """拒绝在线文件"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            result = await self._call(client, 'refuse_online_file', user_id=user_id, msg_id=msg_id, element_id=element_id)
            return {"status": "success", "message": f"✅ 已拒绝接收文件"}
        except Exception as e:
            return {"status": "error", "message": f"拒绝文件失败: {_safe_error_msg(e)}"}

    async def cancel_online_file_tool(self, event: AstrMessageEvent, user_id: str, msg_id: str) -> dict:
        """取消在线文件传输"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            result = await self._call(client, 'cancel_online_file', user_id=user_id, msg_id=msg_id)
            return {"status": "success", "message": f"✅ 已取消文件传输"}
        except Exception as e:
            return {"status": "error", "message": f"取消传输失败: {_safe_error_msg(e)}"}

    async def delete_friend_tool(self, event: AstrMessageEvent, user_id: str, temp_block: bool = False, temp_both_del: bool = False) -> dict:
        """删除好友"""
        client = await self._get_client(event)
        if not client:
            return {"status": "error", "message": "无法获取QQ客户端"}
        try:
            params = {"user_id": user_id, "temp_block": temp_block, "temp_both_del": temp_both_del}
            await self._call(client, 'delete_friend', **params)
            msg = f"✅ 已删除好友 {user_id}"
            if temp_block:
                msg += "（已加入黑名单）"
            if temp_both_del:
                msg += "（双向删除）"
            return {"status": "success", "message": msg}
        except Exception as e:
            return {"status": "error", "message": f"删除好友失败: {_safe_error_msg(e)}"}

    # ==================== 管理员指令 ====================
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_all_help")
    async def admin_all_help(self, event: AstrMessageEvent):
        help_text = """【AstrBot 插件管理命令总览】

═══ 基础命令 ═══

/tool_memory - 记忆管理
  子命令: list [user_id], add <内容> [标签] [重要度], delete <ID>, update <ID> [新内容] [新标签] [重要度], get <ID>

/tool_send_message <目标ID> <消息内容> [chat_type]
  立即发送消息（chat_type: group/private/auto）

/tool_schedule <目标ID> <消息内容> <时间> [chat_type]
  简单定时消息（重启后丢失）

/tool_publish_qzone <说说内容>
  发布QQ空间说说

/tool_status <状态> <持续分钟> [延迟分钟]
  设置QQ在线状态（状态: online/qme/away/busy/dnd/invisible/listening/sleeping/studying）

/tool_status_get
  获取当前QQ在线状态

/tool_poke <目标QQ> [chat_type]
  发送戳一戳

/tool_recall
  引用消息撤回（仅QQ群聊）

/tool_email <收件人> <主题> <内容> [昵称]
  发送QQ邮箱邮件

/tool_scheduled_list [include_executed]
  列出定时指令（持久化）

/tool_scheduled_cancel <任务ID>
  取消定时指令

/tool_scheduled_delete <任务ID>
  彻底删除定时指令

/tool_search <关键词> [类型]  类型: all/friend/group
  搜索联系人

/tool_list [类型] [limit]  类型: all/friend/group
  列出联系人

═══ AI 声聊 ═══

/ai_characters  查看可用 AI 语音角色列表
/ai_voice <角色ID/名称> <文本>  发送 AI 语音消息（仅群聊，角色可选）

═══ 群管理（需 group_manage_enabled=true）═══

/ban_user <QQ号> <禁言分钟>
/unban_user <QQ号>
/kick <QQ号>   (需 kick_enabled=true)
/whole_ban <on/off>
/set_card <QQ号> <新群昵称>
/send_notice <公告内容>
/del_notice <公告ID>
/list_files
/group_members
/delete_group_file <file_id>
/set_admin <QQ号> <on/off>
/set_group_name <新群名>
/list_notices
/upload_file <文件路径> [文件名]
/create_folder <文件夹名>
/del_folder <文件夹ID>
/group_honor [类型]
/at_all_remain
/set_title <QQ号> <头衔>
/shut_list
/ignore_requests
/set_add_option <allow/need_verify/not_allow>
/group_sign

═══ 资料与互动 ═══

/set_qq_avatar [图片]  设置QQ头像（引用图片）
/set_profile <nickname=新昵称> [personal_note=新签名]  修改机器人个人资料
/send_like <QQ号> [次数]  给用户点赞
/get_group_msg_history [群号] [起始序号] [数量]  获取群历史消息
/get_friend_msg_history <QQ号> [起始序号] [数量]  获取好友历史消息
/set_group_portrait [群号] [图片]  设置群头像（引用图片）
/fetch_custom_face [数量]  获取自定义表情列表
/set_input_status <QQ号> <类型>  设置输入状态（1=正在输入，2=取消）

═══ 群文件 ═══

/move_group_file <file_id> <当前目录> <目标目录>  移动群文件
/rename_group_file <file_id> <当前目录> <新名称>  重命名群文件
/trans_group_file <file_id>  传输群文件（获取链接）

═══ 以下功能仅限 LLM 工具调用（通过 run_wyc_tool）═══

记忆: add_memory, search_memories, update_memory, delete_memory, get_memory_detail
消息: send_message, schedule_message, cancel_scheduled_message, list_scheduled_messages
定时指令: create_scheduled_command, list_scheduled_commands, cancel_scheduled_command, delete_scheduled_command
QQ空间: publish_qzone
戳一戳/状态: send_poke, update_qq_status, get_qq_status, get_fun_status_list
消息操作: recall_by_reply
邮件: send_qq_email
联系人: search_contacts, list_contacts
群管理: get_user_group_role, set_essence_msg, delete_essence_msg, set_group_ban,
       set_group_kick, set_group_whole_ban, set_group_card, send_group_notice,
       delete_group_notice, get_group_notice_list, list_group_files, delete_group_file,
       upload_group_file, create_group_file_folder, delete_group_folder, get_group_members_info,
       set_group_admin, set_group_name, get_group_honor_info, get_group_at_all_remain,
       set_group_special_title, get_group_shut_list, get_group_ignore_add_request,
       set_group_add_option, send_group_sign
资料: set_qq_avatar, set_qq_profile, send_like, get_group_msg_history, get_friend_msg_history,
      fetch_custom_face, set_input_status, set_group_portrait
AI语音: get_ai_characters, send_ai_voice
闪传: create_flash_task, get_flash_file_list, get_flash_file_url, send_flash_msg,
      get_share_link, get_fileset_info, get_fileset_id, download_fileset
在线文件: get_online_file_msg, send_online_file, send_online_folder,
         receive_online_file, refuse_online_file, cancel_online_file
好友: delete_friend
工作区: run_python_code, list_workspace_files, read_workspace_file, delete_workspace_file, read_image, send_file
浏览器基础: fetch_url, open_page, screenshot_page, close_page
浏览器坐标交互: browser_click, browser_double_click, browser_right_click,
               browser_long_press, browser_drag, browser_input_at, browser_hover,
               browser_input, browser_scroll, browser_wait
浏览器页面: browser_search, browser_visit, browser_zoom, browser_screenshot,
           browser_back, browser_forward, browser_tabs, browser_close_tab,
           browser_close, browser_chat, browser_install
收藏夹: browser_favorite_list, browser_favorite_add, browser_favorite_delete

═══════════════════════════════════════════
/tool_all_help  显示本帮助
"""
        await event.send(MessageChain().message(help_text))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_memory")
    async def admin_memory(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_memory list/add/delete/update/get [参数]"))
            return
        sub = args[1].lower()
        if sub == "list":
            user_id = args[2] if len(args) > 2 else None
            memories = await self.memory_manager.get_memories(user_id=user_id, limit=50)
            if not memories:
                await event.send(MessageChain().message("暂无记忆"))
                return
            lines = [f"📚 记忆列表（共{len(memories)}条）"]
            for m in memories:
                lines.append(f"{m['id']} | {m['user_id']} | {m['content'][:30]} | 重要:{m.get('importance',5)}")
            await event.send(MessageChain().message("\n".join(lines)))
        elif sub == "add":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory add <内容> [标签] [重要度]"))
                return
            content = args[2]
            tags = args[3] if len(args) > 3 else ""
            importance = int(args[4]) if len(args) > 4 and args[4].isdigit() else 5
            tags_list = [t.strip() for t in tags.split(",")] if tags else []
            memory_id = await self.memory_manager.add_memory("admin", content, tags_list, importance)
            await event.send(MessageChain().message(f"✅ 记忆已添加，ID: {memory_id}"))
        elif sub == "delete":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory delete <记忆ID>"))
                return
            memory_id = args[2]
            success = await self.memory_manager.delete_memory(memory_id)
            await event.send(MessageChain().message("✅ 记忆已删除" if success else "❌ 删除失败"))
        elif sub == "update":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory update <记忆ID> [新内容] [新标签] [新重要度]"))
                return
            memory_id = args[2]
            content = args[3] if len(args) > 3 else None
            tags = args[4] if len(args) > 4 else None
            importance = int(args[5]) if len(args) > 5 and args[5].isdigit() else None
            tags_list = [t.strip() for t in tags.split(",")] if tags is not None else None
            success = await self.memory_manager.update_memory(memory_id, content, tags_list, importance)
            await event.send(MessageChain().message("✅ 记忆已更新" if success else "❌ 更新失败"))
        elif sub == "get":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory get <记忆ID>"))
                return
            m = await self.memory_manager.get_memory_by_id(args[2])
            if not m:
                await event.send(MessageChain().message("未找到记忆"))
                return
            lines = [f"ID: {m['id']}", f"用户: {m['user_id']}", f"内容: {m['content']}",
                     f"标签: {', '.join(m.get('tags',[]))}", f"重要度: {m.get('importance',5)}",
                     f"创建: {m.get('created_at')}", f"更新: {m.get('updated_at')}"]
            await event.send(MessageChain().message("\n".join(lines)))
        else:
            await event.send(MessageChain().message("未知子命令，可用: list, add, delete, update, get"))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_send_message")
    async def admin_send_message(self, event: AstrMessageEvent):
        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 3:
            await event.send(MessageChain().message("用法：/tool_send_message <目标ID> <消息内容> [chat_type]"))
            return
        target_id, message = args[1], args[2]
        result = await self.send_message_tool(event, target_id, message)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_schedule")
    async def admin_schedule(self, event: AstrMessageEvent):
        args = event.message_str.strip().split(maxsplit=3)
        if len(args) < 4:
            await event.send(MessageChain().message("用法：/tool_schedule <目标ID> <消息内容> <时间> [chat_type]"))
            return
        target_id, message, time_str = args[1], args[2], args[3]
        result = await self.schedule_message(event, target_id, message, time_str)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_publish_qzone")
    async def admin_publish_qzone(self, event: AstrMessageEvent):
        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_publish_qzone <说说内容>"))
            return
        result = await self.publish_qzone_tool(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_status")
    async def admin_status(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 3:
            await event.send(MessageChain().message("用法：/tool_status <状态> <持续分钟> [延迟分钟]"))
            return
        status = args[1]
        duration = int(args[2]) if args[2].isdigit() else 30
        delay = int(args[3]) if len(args) > 3 and args[3].isdigit() else 0
        result = await self.update_qq_status(event, status, duration, delay)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_status_get")
    async def admin_status_get(self, event: AstrMessageEvent):
        result = self.status_manager.get_current_status_desc()
        await event.send(MessageChain().message(result))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_poke")
    async def admin_poke(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_poke <目标QQ> [chat_type]"))
            return
        target = args[1]
        chat_type = args[2] if len(args) > 2 else "auto"
        result = await self.send_poke(event, target, chat_type)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_recall")
    async def admin_recall(self, event: AiocqhttpMessageEvent):
        result = await self.recall_by_reply(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_email")
    async def admin_email(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await event.send(MessageChain().message("用法：/tool_email <收件人> <主题> <内容> [昵称]"))
            return
        import shlex
        try:
            argv = shlex.split(parts[1])
        except:
            argv = parts[1].split()
        if len(argv) < 3:
            await event.send(MessageChain().message("用法：/tool_email <收件人> <主题> <内容> [昵称]"))
            return
        to, subject, content = argv[0], argv[1], argv[2]
        nickname = argv[3] if len(argv) > 3 else ""
        result = await self.send_qq_email_tool(event, to, subject, content, nickname)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_scheduled_list")
    async def admin_scheduled_list(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        include = len(args) > 1 and args[1].lower() == "true"
        result = await self.list_scheduled_commands(event, include)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_scheduled_cancel")
    async def admin_scheduled_cancel(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_scheduled_cancel <任务ID>"))
            return
        result = await self.cancel_scheduled_command(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_scheduled_delete")
    async def admin_scheduled_delete(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_scheduled_delete <任务ID>"))
            return
        result = await self.delete_scheduled_command(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_search")
    async def admin_search(self, event: AstrMessageEvent):
        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_search <关键词> [类型]"))
            return
        keyword = args[1]
        search_type = args[2] if len(args) > 2 else "all"
        result = await self.search_contacts(event, keyword, search_type)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_list")
    async def admin_list(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        contact_type = args[1] if len(args) > 1 else "all"
        limit = int(args[2]) if len(args) > 2 and args[2].isdigit() else 20
        result = await self.list_contacts(event, contact_type, limit)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ai_characters")
    async def cmd_ai_characters(self, event: AstrMessageEvent):
        result = await self.get_ai_characters_tool(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ai_voice")
    async def cmd_ai_voice(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/ai_voice [角色ID/名称] <文本>"))
            return
        text = args[1]
        character = ""
        if len(args) == 3:
            character, text = args[1], args[2]
        else:
            parts = args[1].split(maxsplit=1)
            if len(parts) == 2:
                character, text = parts[0], parts[1]
        result = await self.send_ai_voice_tool(event, text, character)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban_user")
    async def cmd_ban_user(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 3:
            await event.send(MessageChain().message("用法：/ban_user <QQ号> <禁言分钟>"))
            return
        try:
            minutes = int(args[2])
        except:
            await event.send(MessageChain().message("禁言时长必须是数字（分钟）"))
            return
        result = await self.set_group_ban(event, args[1], minutes * 60)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("unban_user")
    async def cmd_unban_user(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/unban_user <QQ号>"))
            return
        result = await self.set_group_ban(event, args[1], 0)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("kick")
    async def cmd_kick(self, event: AiocqhttpMessageEvent):
        if not self.config.get("kick_enabled", True):
            await event.send(MessageChain().message("❌ 踢人功能已被禁用"))
            return
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/kick <QQ号>"))
            return
        result = await self.set_group_kick(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("whole_ban")
    async def cmd_whole_ban(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/whole_ban <on/off>"))
            return
        enable = args[1].lower() == "on"
        result = await self.set_group_whole_ban(event, enable)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("set_card")
    async def cmd_set_card(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 3:
            await event.send(MessageChain().message("用法：/set_card <QQ号> <新群昵称>"))
            return
        result = await self.set_group_card(event, args[1], args[2])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("send_notice")
    async def cmd_send_notice(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/send_notice <公告内容>"))
            return
        result = await self.send_group_notice(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("del_notice")
    async def cmd_del_notice(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/del_notice <公告ID>"))
            return
        result = await self.delete_group_notice(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("list_files")
    async def cmd_list_files(self, event: AiocqhttpMessageEvent):
        result = await self.list_group_files(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("delete_group_file")
    async def cmd_delete_group_file(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/delete_group_file <file_id>"))
            return
        result = await self.delete_group_file(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("group_members")
    async def cmd_group_members(self, event: AiocqhttpMessageEvent):
        result = await self.get_group_members_info(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("set_admin")
    async def cmd_set_admin(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 3:
            await event.send(MessageChain().message("用法：/set_admin <QQ号> <on/off>"))
            return
        enable = args[2].lower() == "on"
        result = await self.set_group_admin(event, args[1], enable)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("set_group_name")
    async def cmd_set_group_name(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/set_group_name <新群名>"))
            return
        result = await self.set_group_name(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("list_notices")
    async def cmd_list_notices(self, event: AiocqhttpMessageEvent):
        result = await self.get_group_notice_list(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("upload_file")
    async def cmd_upload_file(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/upload_file <文件路径> [文件名]"))
            return
        file_path = args[1]
        file_name = args[2] if len(args) > 2 else ""
        result = await self.upload_group_file(event, file_path, file_name)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("create_folder")
    async def cmd_create_folder(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/create_folder <文件夹名>"))
            return
        result = await self.create_group_file_folder(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("del_folder")
    async def cmd_del_folder(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/del_folder <文件夹ID>"))
            return
        result = await self.delete_group_folder(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("group_honor")
    async def cmd_group_honor(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        honor_type = args[1] if len(args) > 1 else "all"
        result = await self.get_group_honor_info(event, honor_type)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("at_all_remain")
    async def cmd_at_all_remain(self, event: AiocqhttpMessageEvent):
        result = await self.get_group_at_all_remain(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("set_title")
    async def cmd_set_title(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 3:
            await event.send(MessageChain().message("用法：/set_title <QQ号> <头衔>"))
            return
        result = await self.set_group_special_title(event, args[1], args[2])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("shut_list")
    async def cmd_shut_list(self, event: AiocqhttpMessageEvent):
        result = await self.get_group_shut_list(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ignore_requests")
    async def cmd_ignore_requests(self, event: AiocqhttpMessageEvent):
        result = await self.get_group_ignore_add_request(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("set_add_option")
    async def cmd_set_add_option(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/set_add_option <allow/need_verify/not_allow>"))
            return
        result = await self.set_group_add_option(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("group_sign")
    async def cmd_group_sign(self, event: AiocqhttpMessageEvent):
        result = await self.send_group_sign(event)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("set_qq_avatar")
    async def cmd_set_qq_avatar(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=1)
        file = args[1] if len(args) > 1 else ""
        result = await self.set_qq_avatar(event, file)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("move_group_file")
    async def cmd_move_group_file(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 4:
            await event.send(MessageChain().message("用法：/move_group_file <file_id> <当前目录> <目标目录>"))
            return
        file_id, current_dir, target_dir = args[1], args[2], args[3]
        result = await self.move_group_file(event, file_id, current_dir, target_dir)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("rename_group_file")
    async def cmd_rename_group_file(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=3)
        if len(args) < 4:
            await event.send(MessageChain().message("用法：/rename_group_file <file_id> <当前目录> <新名称>"))
            return
        file_id, current_dir, new_name = args[1], args[2], args[3]
        result = await self.rename_group_file(event, file_id, current_dir, new_name)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("trans_group_file")
    async def cmd_trans_group_file(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/trans_group_file <file_id>"))
            return
        result = await self.trans_group_file(event, args[1])
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("send_like")
    async def cmd_send_like(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/send_like <QQ号> [次数]"))
            return
        user_id = args[1]
        times = int(args[2]) if len(args) > 2 and args[2].isdigit() else 1
        result = await self.send_like_tool(event, user_id, times)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("get_group_msg_history")
    async def cmd_get_group_msg_history(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        group_id = args[1] if len(args) > 1 else ""
        count = int(args[2]) if len(args) > 2 and args[2].isdigit() else 20
        result = await self.get_group_msg_history(event, group_id, count)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("get_friend_msg_history")
    async def cmd_get_friend_msg_history(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/get_friend_msg_history <QQ号> [数量]"))
            return
        user_id = args[1]
        count = int(args[2]) if len(args) > 2 and args[2].isdigit() else 20
        result = await self.get_friend_msg_history(event, user_id, count)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("set_group_portrait")
    async def cmd_set_group_portrait(self, event: AiocqhttpMessageEvent):
        args = event.message_str.strip().split(maxsplit=2)
        group_id = args[1] if len(args) > 1 else ""
        file = args[2] if len(args) > 2 else ""
        result = await self.set_group_portrait(event, group_id, file)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("fetch_custom_face")
    async def cmd_fetch_custom_face(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        count = int(args[1]) if len(args) > 1 and args[1].isdigit() else 48
        result = await self.fetch_custom_face(event, count)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("set_input_status")
    async def cmd_set_input_status(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 3:
            await event.send(MessageChain().message("用法：/set_input_status <QQ号> <类型>"))
            return
        user_id = args[1]
        event_type = int(args[2]) if args[2].isdigit() else 1
        result = await self.set_input_status_tool(event, user_id, event_type)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("set_profile")
    async def cmd_set_profile(self, event: AstrMessageEvent):
        args = event.message_str.strip()
        params = {}
        for part in args.split():
            if '=' in part:
                k, v = part.split('=', 1)
                if k in ("nickname", "personal_note"):
                    params[k] = v
        if not params:
            await event.send(MessageChain().message("用法：/set_profile nickname=新昵称 personal_note=新签名"))
            return
        result = await self.set_qq_profile_tool(event, **params)
        await event.send(MessageChain().message(result.get("message", "操作失败")))

    # ==================== 提示词注入 ====================
    _TOOL_PROMPT = """【QZoneTools 插件工具使用说明】

本插件为机器人提供了大量扩展能力，涵盖QQ空间操作、群聊管理、消息发送、记忆存储、AI语音、历史消息查询等功能。你需要通过一套标准的三步调用流程来使用这些工具，不能直接猜测或编造工具名称。

工具调用流程：
第一步 — 搜索工具。调用 search_wyc_tools 并传入简短关键词，系统会根据关键词模糊匹配并返回相关的工具列表。注意关键词必须是简短词语，不要使用完整的问句。
第二步 — 查看全部工具。如果第一步搜索后没有找到你需要的工具，调用 call_wyc_tools 获取当前已启用的全部工具名称和简要说明列表。这个工具不需要任何参数。
第三步 — 执行工具。确定工具名称后，调用 run_wyc_tool 并传入两个参数：tool_name 是你要执行的具体工具名称，args 是该工具所需的参数（以 JSON 对象格式传入）。

工具完整清单（每个工具对应一个二字简称，可直接作为关键词搜索）：

记忆管理类：加忆（添加用户记忆）、搜忆（搜索记忆）、改忆（更新记忆内容）、删忆（删除指定记忆）、详忆（获取单条记忆详情）
消息发送类：发信（主动发送消息到群聊或私聊）、定信（创建一次性定时消息）、列信（查看待执行的定时消息）、取消（取消已创建的定时消息）
高级定时类：创建定（创建持久化高级定时指令，支持发空间、改状态、LLM提醒）、列定（查看所有高级定时指令）、取消定（取消未执行的高级定时指令）、删定（删除指定的高级定时指令）
QQ空间与互动：发说（发表QQ空间说说）、戳戳（发送戳一戳/窗口抖动）
状态管理：改态（修改QQ在线状态）、查态（查看当前QQ状态）、乐态（获取可用的娱乐状态列表）
其他操作：撤回（引用消息撤回，仅限群聊2分钟内）、邮件（通过QQ邮箱发送邮件）
联系人：搜人（模糊搜索好友或群聊）、列人（列出全部联系人或群聊列表）
群成员：身份（查询指定用户在群内的身份是群主、管理员还是成员）
群精华：设精（将某条消息设为精华消息）、删精（取消精华消息）
群处罚与名片：禁言（对指定成员禁言或解禁）、踢人（将成员移出群聊）、全禁（开启或关闭全体禁言）、名片（修改群成员在群内的昵称）
群公告：公告（发布群公告）、删告（删除指定公告）、列告（查看群公告列表）
群文件：列件（查看群文件列表）、删件（删除指定文件）、上传（上传文件到群文件）、建夹（在群文件创建文件夹）、删夹（删除群文件夹及其内容）、移件（移动群文件到其他目录）、改名（重命名群文件）、传链（获取文件传输链接）
群配置：成员（获取群成员列表及详细信息）、管管（设置或取消群管理员）、群名（修改群名称）、荣誉（查询群聊荣誉信息）、全体（查看@全体成员剩余次数）、头衔（设置群成员专属头衔）、禁列（获取当前被禁言的成员列表）、加审（查看被忽略的加群请求）、设审（设置加群方式为允许/需验证/禁止）、打卡（执行群打卡签到）
AI语音：角色（获取可用的AI语音角色列表）、语音（发送指定文本的AI语音消息到群聊）
个人资料：资料（修改机器人昵称和个性签名）、头像（设置机器人QQ头像）、群头（设置群聊头像）
历史消息：群史（获取群聊最近的消息记录，可指定条数）、友史（获取与好友的私聊消息记录）
其他：表情（获取机器人账号下的自定义表情列表）、输入（手动设置或取消输入状态/正在输入中）、点赞（给指定QQ用户发送名片赞）

每次需要使用以上任一工具时，务必从第一步开始，通过 search_wyc_tools 搜索关键词找到对应的工具，然后通过 run_wyc_tool 执行，切勿直接跳过搜索步骤。如果某个关键词搜索不到想要的结果，换一个更精确的关键词重试，或者直接调用 call_wyc_tools 查看完整列表。"""

    # 工具铁律 + 回复规则 + 说话风格（无条件注入）
    # 与旧版「重要工具使用规范」合并：旧版三步流程措辞与用户新给的「工具调用铁律」重复，此处统一
    _TOOL_RULES = """[工具调用铁律——想说不之前先搜一下]
用户让你做事（发空间、禁言、踢人、发邮件、改名片、设定时、查记忆等），
你觉得好像做不到、或不知道有没有这个功能时，绝对不准直接说"我不行""我做不到""我没有这个功能"。
必须先搜一下，搜完确认真的没有，才能拒绝。别瞎猜自己有没有能力，搜了再说。
1. search_wyc_tools —— 传简短关键词（如"邮箱""禁言""发说说""记忆""状态"），不要用完整问句
2. call_wyc_tools —— 第 1 步没找到时，查看全部可用工具列表
3. run_wyc_tool —— 传 tool_name 与 JSON 格式的 args 执行
已经知道工具名时可直接 run_wyc_tool，不必再搜；但禁止凭空猜测工具名。
纯查询类需求（"今天天气""XX 是什么"）直接联网搜索，不必动用本插件工具。
你还拥有「查询聊天记录」的能力：用户说"刚刚在群里…""刚刚在私信…"时，
可以用 wyc_tools 里的聊天记录工具（搜"聊天记录""群史""友史"）查用户口中的上下文。

[回复与发送规则（防止重复回复，最高优先级）]
1. 调用 send_message_to_user 时，session 参数必须省略（留空即发送到当前会话）。任何自己拼写的 session 值都会被系统拒绝并导致重发。
2. 完整回复只输出一次：要么直接输出文字作为回复，要么调用 send_message_to_user 发送（需要附图片/文件时用它）。绝不允许先输出一段完整文字、再用 send_message_to_user 把同样内容再发一遍。
3. 使用浏览器等工具期间，如需说明进度只写一句简短提示（如"正在打开网页…"），完整结果和页面描述放在最后一次性输出，不要在中间就写出最终结论。

[说话风格]
用工具的时候别跟机器人似的，可以自然地说"马上""搞完了""行吧"；
人设带点脾气也没问题。一件事没办完之前可以先不说话，
不用输出什么怪格式的东西，直接说人话就行。
多使用函数工具，拿不准就用 search_wyc_tools 查查有没有相关的，尽量成为用户真正的好朋友。"""

    # 记忆强化（仅当记忆工具可用时注入）
    _MEMORY_DISCIPLINE = """[记忆强化指令——强制执行]
记忆是你的核心任务之一。你的脑子记性很差，不记下来转眼就忘，全清空之后你就什么都不记得了。
所以别偷懒，多记、勤记、反复确认。聊天时自然用上记住的内容，让对方觉得你真的很懂他。

每次聊天中，只要出现以下任意内容，立刻用 wyc_tools 里的记忆工具动手记录，绝不允许嘴上说"记住了"：
· 作息时间（几点睡几点起）
· 饮食口味（爱吃什么、讨厌什么）
· 兴趣爱好（打什么游戏、看什么番、听什么歌）
· 口头禅和说话习惯
· 重要日期（生日、纪念日、考试日）
· 人际关系（提到谁对他好、谁跟他吵架了）
· 心情状态（开心、焦虑、难过、生气）
· 生活琐事（养了什么宠物、最近忙什么、有什么计划）
· 任何用户主动说"你记住"的内容
· 任何你下意识觉得"以后可能会用到"的信息（小习惯、小性格也都算）

[记忆频次硬性规定]
在当前对话中悄悄计数。从任意一次调用记忆工具之后开始算，每累积到第 8 句对话
（对方说的话和你自己的回复都算一句），必须强制调用一次 wyc_tools 里的记忆工具。
调用时优先检查之前记住的内容有没有需要更新的，有就修改，没有就新增一条。
8 句是硬指标，不是建议，到了就必须记——哪怕你觉得没什么特别的内容，
也要把这段时间里最可能有用的一两条信息记进去。
连续长聊时每 8 句记一次，循环往复，不许中断。

用户明确说不许记某件事，就立刻删掉。除此以外，使劲记，多多益善。"""

    # 浏览器自动化指引（仅当配置启用了浏览器时注入，不用浏览器则零 token 成本）
    _BROWSER_GUIDE = """[浏览器自动化工具使用指引]
你现在能直接控制浏览器上网冲浪了。打开网页、点击按钮、填写文字、截图看页面、
切换标签、管理收藏夹，这些都能做。搜索支持百度、必应、谷歌。
所有浏览器工具都在 wyc_tools 里面，用法跟其他工具一样：先 search_wyc_tools 搜工具名
（关键词如"浏览器""点击""截图""标签页""收藏夹""搜索"），确认了就用 run_wyc_tool 调起来。
操作默默在后台做，聊天框里照常跟用户说话就行。
（唯一例外：request_browser_takeover 是应急工具，可直接调用、无需搜索，
  仅在遇到验证码/滑块/人机校验等无法自动完成的环节时使用，别有事没事就转交给真人。）"""

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request: Any, *args, **kwargs) -> None:
        try:
            inject_parts = []
            
            if self.config.get("inject_tool_prompt_enabled", False):
                inject_parts.append(self._TOOL_PROMPT)
            
            inject_parts.append(self._TOOL_RULES)

            # 记忆强化：仅在记忆工具实际可用时注入（避免教唆 AI 调用被禁用的工具）
            if self.config.get("inject_memory_discipline_enabled", True):
                try:
                    _avail = self._get_available_tools(event)
                    if "add_memory" in _avail or "update_memory" in _avail:
                        inject_parts.append(self._MEMORY_DISCIPLINE)
                except Exception:
                    pass

            # 浏览器指引：仅在启用了浏览器时注入
            if self.browser_supervisor and self.config.get("inject_browser_guide_enabled", True):
                inject_parts.append(self._BROWSER_GUIDE)
            
            status_desc = self.status_manager.get_current_status_desc()
            inject_parts.append(f"[系统状态] {status_desc}")

            if self.config.get("enabled", True) and event.get_platform_name() in ["aiocqhttp", "qq"] and self.config.get("memory_inject_enabled", True):
                user_id = event.get_sender_id()
                max_memories = self.config.get("max_inject_memories", 5)
                memories = await self.memory_manager.get_latest_memories_for_inject(user_id, max_memories)
                if memories:
                    memory_lines = [f"[用户历史记忆] 该用户({user_id})的重要信息："]
                    for i, m in enumerate(memories, 1):
                        tags = f"[{', '.join(m.get('tags', []))}]" if m.get('tags') else ""
                        content = m.get('content', '').replace('\n', ' ').replace('\r', '')
                        memory_lines.append(f"{i}. {content} {tags}")
                    inject_parts.append("\n".join(memory_lines))

            if self.config.get("enabled", True) and event.get_platform_name() in ["aiocqhttp", "qq"] and self.config.get("inject_group_role_enabled", True):
                if not event.is_private_chat():
                    group_id = event.get_group_id()
                    if group_id:
                        user_id = event.get_sender_id()
                        role = await self._get_group_member_role(group_id, user_id)
                        if role != "unknown":
                            inject_parts.append(f"[当前群身份] 用户 {user_id} 在本群({group_id})的身份是：{role}")

            if self.ai_default_character:
                inject_parts.append(f"[AI语音配置] 默认角色ID为 '{self.ai_default_character}'。调用 send_ai_voice 时若未指定角色，将自动使用此默认值。")
            else:
                inject_parts.append("[AI语音配置] 未设置默认角色。调用 send_ai_voice 时若未指定角色，将自动选择第一个可用角色。")

            # [浏览器能力] 仅在浏览器会话活跃时注入（不用浏览器则零 token 成本）
            # 目的：让 AI 在做浏览器任务期间持续知道「接管」这根救命绳存在，
            #       覆盖"AI 静默放弃、一个工具都不调"这类无法被检测的场景。
            try:
                if self.browser_supervisor and getattr(self.browser_supervisor, "browser", None):
                    if self.takeover_manager.is_active():
                        sess = self.takeover_manager.session
                        inject_parts.append(
                            f"[浏览器能力] 浏览器当前正由真人用户接管操作中"
                            f"（剩余 {sess.remaining_max:.0f} 秒）。"
                            f"请等待用户完成，结束后系统会自动把最新截图返回给你。"
                        )
                    else:
                        inject_parts.append(
                            "[浏览器能力] 当前浏览器会话活跃。可用 request_browser_takeover "
                            "把操作权临时交给真人用户（适用于验证码/滑块/人机校验等"
                            "无法自动完成的环节）；普通操作失败请先自行重试，不要随意转交。"
                        )
            except Exception:
                pass

            if self.enable_human_typing and event.is_private_chat():
                user_key = event.unified_msg_origin
                now = time.time()
                last = self._user_last_active.get(user_key, 0)
                if now - last > self.typing_idle_threshold:
                    delay = random.uniform(self.typing_initial_delay_min, self.typing_initial_delay_max)
                    logger.info(f"[拟人化] 用户 {user_key} 进入延迟 {delay:.2f} 秒")
                    await asyncio.sleep(delay)
                self._user_last_active[user_key] = now
                try:
                    client = await self._get_client(event)
                    if client:
                        await self._call(client, 'set_input_status', user_id=event.get_sender_id(), event_type=1)
                except Exception as e:
                    logger.debug(f"设置输入状态失败: {e}")

            if inject_parts:
                inject_text = "\n".join(inject_parts)
                # 官方推荐做法：追加到用户消息末尾（extra_user_content_parts），
                # 并用 mark_as_temp() 标记为临时内容——仅本次请求发给模型，
                # 不写入会话历史，从而不破坏模型上下文的前缀缓存命中率。
                parts = getattr(request, "extra_user_content_parts", None)
                if parts is not None and TextPart is not None:
                    parts.append(TextPart(text=inject_text).mark_as_temp())
                elif hasattr(request, "system_prompt"):
                    # 兼容极旧版本框架：退化为原有行为
                    request.system_prompt = (
                        f"{request.system_prompt}\n{inject_text}\n"
                        if request.system_prompt
                        else inject_text + "\n"
                    )
        except Exception as e:
            logger.error(f"[注入] 失败: {e}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: Any) -> None:
        if self.enable_human_typing and event.is_private_chat():
            try:
                client = await self._get_client(event)
                if client:
                    user_id = event.get_sender_id()
                    await self._call(client, 'set_input_status', user_id=user_id, event_type=2)
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    await self._call(client, 'set_input_status', user_id=user_id, event_type=1)
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    await self._call(client, 'set_input_status', user_id=user_id, event_type=2)
                    await asyncio.sleep(random.uniform(0.1, 0.7))
                    await self._call(client, 'set_input_status', user_id=user_id, event_type=1)
                    await asyncio.sleep(0.2)
                    await self._call(client, 'set_input_status', user_id=user_id, event_type=2)
            except Exception as e:
                logger.debug(f"拟人化输入状态切换失败: {e}")
