# -*- coding: utf-8 -*-
"""自定义工具子进程执行器（v5.9.0）。

本文件是**插件包内的只读资源**，由 main.py 以子进程方式调用：

    sys.executable -I -B <plugin>/core/custom_runner.py

协议（单行 JSON，stdin 进 / stdout 出）：

    stdin  ← {"protocol":1, "code":..., "entry":"main", "args":{...},
              "introspect_only":false, "workspace":"...", "max_output_chars":8000}
    stdout → {"protocol":1, "status":"success"|"error", "message":..., ...}
    stderr → 日志与 traceback（宿主收集后附加到 message）

设计约束：
- **只依赖标准库**。`-I` 隔离模式下 sys.path 不含脚本目录，无法 import 插件内其他模块。
- **stdout 专用于协议**，用户代码的 print 被重定向到 stderr，否则会污染协议。
- 所有异常在内部捕获并转成协议错误，绝不向外抛。
- 安全分两层：宿主侧 AST/正则静态检测（第一层）+ 本文件的受限 builtins（第二层）。

⚠️ 安全边界（如实声明）：这是**进程隔离 + 静态检测**，不等同于内核级沙箱
（无 seccomp / namespace）。无法完全阻止有意的逃逸尝试。风险等级与既有的
`run_python_code` 工具相同，因此宿主侧默认仅「超管」可用。
"""

import ast as _ast
import asyncio as _asyncio
import inspect as _inspect
import json as _json
import os as _os
import re as _re
import sys as _sys
import traceback as _traceback

PROTOCOL_VERSION = 1

# 协议通道强制 UTF-8。
#
# ⚠️ 必须显式 reconfigure，不能靠 PYTHONIOENCODING：
# 宿主用 `-I` 启动本文件，而 `-I` 隐含 `-E`（忽略所有 PYTHON* 环境变量），
# 于是 PYTHONIOENCODING 完全无效，stdout 会退回系统 locale 编码。
# 在非 UTF-8 locale（如 Windows 中文环境的 cp936、或 LANG=C 的容器）下，
# 含中文的 JSON 会被写成乱码，宿主按 UTF-8 解码直接失败。
for _name in ("stdin", "stdout", "stderr"):
    _stream = getattr(_sys, _name, None)
    try:
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Args: 段里的一行：`    参数名(类型): 描述`
_PARAM_LINE_RE = _re.compile(r"^\s+(\w+)\s*\(([^)]*)\)\s*:\s*(.*)$")

# 与宿主侧 `_check_banned_ast` 的 banned_modules 保持一致（黑名单同步维护）
BANNED_MODULES = {
    "subprocess", "os", "sys", "shutil", "ctypes", "multiprocessing",
    "socket", "http", "ftplib", "smtplib", "pty", "signal", "resource",
    "importlib", "pickle", "marshal", "builtins", "gc",
}


# ==================== 输出（协议） ====================

def emit(obj):
    """把结果写成 stdout 的**单行** JSON（协议要求）。"""
    try:
        line = _json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        line = _json.dumps({"protocol": PROTOCOL_VERSION, "status": "error",
                            "message": "结果无法序列化为 JSON"}, ensure_ascii=False)
    _sys.stdout.write(line + "\n")
    _sys.stdout.flush()


def fail(msg, **extra):
    out = {"protocol": PROTOCOL_VERSION, "status": "error", "message": str(msg)}
    out.update(extra)
    emit(out)
    _sys.exit(0)   # 用 0 退出：错误已通过协议传达，非零会让宿主误判为崩溃


# ==================== 受限 builtins ====================

_real_print = print
_real_import = __import__


def _safe_print(*args, **kwargs):
    """用户代码的 print 必须走 stderr —— stdout 是协议通道。"""
    kwargs.setdefault("file", _sys.stderr)
    _real_print(*args, **kwargs)


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """限制 import：根模块命中黑名单即拒绝（与宿主 AST 检测同一份名单）。"""
    root = str(name).split(".")[0]
    if root in BANNED_MODULES:
        raise ImportError(f"禁止导入模块: {root}")
    return _real_import(name, globals, locals, fromlist, level)


_SAFE_BUILTIN_NAMES = (
    # 基础
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
    "callable", "chr", "classmethod", "complex", "dict", "divmod", "enumerate",
    "filter", "float", "format", "frozenset", "getattr", "hasattr", "hash",
    "hex", "id", "int", "isinstance", "issubclass", "iter", "len", "list",
    "map", "max", "min", "next", "object", "oct", "ord", "pow", "property",
    "range", "repr", "reversed", "round", "set", "setattr", "slice", "sorted",
    "staticmethod", "str", "sum", "super", "tuple", "type", "zip",
    "aiter", "anext",
    # 文件（与既有 run_python_code 的风险等级一致；绝对路径仍受宿主正则限制）
    "open",
    # 异常类型（用户代码 try/except 需要）
    "ArithmeticError", "AssertionError", "AttributeError", "BaseException",
    "BufferError", "EOFError", "Exception", "FloatingPointError",
    "GeneratorExit", "ImportError", "IndentationError", "IndexError",
    "KeyError", "KeyboardInterrupt", "LookupError", "MemoryError", "NameError",
    "NotImplementedError", "OSError", "OverflowError", "ReferenceError",
    "RuntimeError", "StopIteration", "SyntaxError", "SystemError",
    "SystemExit", "TabError", "TypeError", "UnboundLocalError",
    "UnicodeDecodeError", "UnicodeEncodeError", "UnicodeError",
    "UnicodeTranslateError", "ValueError", "Warning", "ZeroDivisionError",
    # 类定义 / import 语句所需
    "__build_class__", "__name__",
)

# 明确**不提供**：eval / exec / compile / globals / locals / vars / dir /
# input / breakpoint / memoryview / delattr / __loader__ / __spec__


def build_safe_builtins():
    """构造受限 builtins（显式白名单，不提供 eval/exec/compile/globals 等）。"""
    import builtins as _b
    out = {}
    for n in _SAFE_BUILTIN_NAMES:
        if hasattr(_b, n):
            out[n] = getattr(_b, n)
    out["print"] = _safe_print
    out["__import__"] = _safe_import
    return out


# ==================== docstring / schema 推导 ====================

_TYPE_MAP = {
    "string": "string", "str": "string",
    "integer": "integer", "int": "integer",
    "number": "number", "float": "number",
    "boolean": "boolean", "bool": "boolean",
    "array": "array", "list": "array",
    "object": "object", "dict": "object",
}


def parse_docstring(doc):
    """解析 docstring：返回 (首段描述, {参数名: {type, desc}})。

    只认官方 `@filter.llm_tool` 同款的 Args: 段格式：`    参数名(类型): 描述`
    """
    doc = doc or ""
    lines = doc.split("\n")

    # 首段描述：Args: 之前的非空行
    desc_lines = []
    for ln in lines:
        if ln.strip().lower().startswith(("args:", "arguments:", "参数:")):
            break
        desc_lines.append(ln)
    main_desc = "\n".join(desc_lines).strip()

    # Args: 段
    desc_map = {}
    in_args = False
    for ln in lines:
        s = ln.strip()
        if s.lower().startswith(("args:", "arguments:", "参数:")):
            in_args = True
            continue
        if in_args:
            # 遇到 Returns:/Raises:/Examples: 等段落标题则结束
            if s and not ln.startswith((" ", "\t")) and s.endswith(":"):
                break
            if not s:
                continue
            m = _PARAM_LINE_RE.match(ln)
            if m:
                desc_map[m.group(1)] = {
                    "type": (m.group(2) or "").strip(),
                    "desc": (m.group(3) or "").strip(),
                }
    return main_desc, desc_map


def map_annotation(ann):
    """把类型注解映射成 JSON Schema 类型。"""
    if ann is _inspect.Parameter.empty or ann is None:
        return ""
    if isinstance(ann, str):
        return _TYPE_MAP.get(ann.strip().lower().replace("typing.", ""), "")
    name = getattr(ann, "__name__", None)
    if name:
        return _TYPE_MAP.get(name.lower(), "")
    origin = getattr(ann, "__origin__", None)
    if origin is not None:
        oname = getattr(origin, "__name__", str(origin))
        return _TYPE_MAP.get(str(oname).lower(), "")
    return ""


def derive_schema(fn):
    """从函数签名 + docstring 推导 JSON Schema。

    返回 (schema, params_list, main_desc)
    - docstring 的 `参数名(类型)` **优先于**类型注解（与官方 @filter.llm_tool 一致）
    - 无默认值的参数视为必填
    """
    try:
        sig = _inspect.signature(fn)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}, "required": []}, [], ""

    doc = _inspect.getdoc(fn) or ""
    main_desc, desc_map = parse_docstring(doc)

    props = {}
    required = []
    params = []
    for name, p in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue   # *args / **kwargs 不进 schema

        jtype = ""
        d = desc_map.get(name) or {}
        if d.get("type"):
            jtype = _TYPE_MAP.get(d["type"].strip().lower(), "")
        if not jtype:
            jtype = map_annotation(p.annotation)
        if not jtype:
            jtype = "string"

        pdesc = d.get("desc") or f"{name} 参数"
        props[name] = {"type": jtype, "description": pdesc}
        is_req = p.default is p.empty
        if is_req:
            required.append(name)
        params.append({
            "name": name, "type": jtype, "required": is_req,
            "desc": pdesc,
            "has_default": not is_req,
        })

    schema = {"type": "object", "properties": props, "required": required}
    return schema, params, main_desc


def filter_args(fn, args):
    """只保留函数签名接受的参数（多余键丢弃，缺失必填报错）。"""
    try:
        sig = _inspect.signature(fn)
    except (TypeError, ValueError):
        return {}, None
    accepted = {n for n, p in sig.parameters.items()
                if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
                and n not in ("self", "cls")}
    has_var_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())

    clean = {}
    for k, v in (args or {}).items():
        if k in accepted or has_var_kw:
            clean[k] = v
    missing = []
    for n, p in sig.parameters.items():
        if n in ("self", "cls"):
            continue
        if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
            continue
        if p.default is p.empty and n not in clean:
            missing.append(n)
    return clean, (missing or None)


# ==================== 结果归一化 ====================

def normalize_result(res, max_chars):
    """把用户函数返回值统一成协议结果。

    约定（与工具描述一致）：
      dict → 直接用（status/message/screenshot 等）
      str  → 包装成 {"status":"success","message": <str>}
    """
    if isinstance(res, dict):
        out = dict(res)
        out.setdefault("status", "success")
        if "message" not in out:
            out["message"] = ""
    elif res is None:
        out = {"status": "success", "message": "（函数无返回值）"}
    elif isinstance(res, str):
        out = {"status": "success", "message": res}
    else:
        out = {"status": "success", "message": str(res)}

    # 规范化类型：message / status 必须是字符串。
    # 否则宿主侧下游（隐私脱敏、自定义返回文案、base64 护栏）都要求
    # `isinstance(x, str)`，类型不对会被**静默跳过** —— 脱敏失效且无任何报错。
    # 用户写 `{"message": 42}`（忘了转 str）是很常见的。
    if not isinstance(out.get("status"), str):
        out["status"] = str(out.get("status"))
    if not isinstance(out.get("message"), str):
        out["message"] = str(out.get("message"))

    # 文本截断
    msg = out.get("message")
    if isinstance(msg, str) and max_chars and len(msg) > max_chars:
        out["message"] = msg[:max_chars] + f"\n…（已截断，共 {len(msg)} 字符）"

    # screenshot 只接受**文件路径**，拒绝 data: / base64（§27.2 的硬教训）
    shot = out.get("screenshot")
    if shot is not None:
        if not isinstance(shot, str) or shot.startswith("data:") or len(shot) > 4096:
            out.pop("screenshot", None)
            out["status"] = out.get("status") or "success"
            note = "\n（screenshot 字段被忽略：只接受工作区内的图片文件路径）"
            if isinstance(out.get("message"), str):
                out["message"] += note
        elif not _os.path.isfile(shot):
            out.pop("screenshot", None)
            if isinstance(out.get("message"), str):
                out["message"] += f"\n（screenshot 路径不存在，已忽略：{shot}）"

    out["protocol"] = PROTOCOL_VERSION
    return out


# ==================== 主流程 ====================

class WycHelper:
    """提供给用户代码的辅助对象 `wyc`。

    只做纯内存辅助，不直接触碰敏感资源；图片路径由宿主在返回时再校验。
    """

    def __init__(self, workspace):
        self.workspace = workspace or ""
        self._images = []

    def log(self, *a):
        _real_print(*a, file=_sys.stderr)

    def image(self, path):
        """声明「这是要发给 AI 的图片」，只记路径，不读内容。"""
        try:
            p = str(path)
            if _os.path.isfile(p):
                self._images.append(p)
                return True
            _real_print(f"[wyc.image] 文件不存在: {p}", file=_sys.stderr)
        except Exception as e:
            _real_print(f"[wyc.image] 失败: {e}", file=_sys.stderr)
        return False

    def images(self):
        return list(self._images)


def read_request():
    """从 stdin 读入请求（JSON）。stdin 为空或非法时返回 None。"""
    try:
        raw = _sys.stdin.read()
    except Exception:
        return None
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        req = _json.loads(raw)
    except Exception:
        return None
    return req if isinstance(req, dict) else None


def setup_font(font_path):
    """给 matplotlib 注册中文字体（对齐 run_python_code 的既有行为）。

    不做这件事的话，用户用 matplotlib 画图时中文全是方块 ——
    run_python_code 会自动注入这段配置，自定义工具走子进程，
    所以由宿主把字体路径传进来、在这里完成注册。

    字体缺失 / matplotlib 未安装时静默跳过（不影响用户代码运行）。
    """
    if not font_path or not _os.path.isfile(font_path):
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        font_manager.fontManager.addfont(font_path)
        name = font_manager.FontProperties(fname=font_path).get_name()
        plt.rcParams["font.sans-serif"] = [name] + list(
            plt.rcParams.get("font.sans-serif", []))
        plt.rcParams["axes.unicode_minus"] = False
    except Exception as e:
        _real_print(f"[字体配置] 跳过: {e}", file=_sys.stderr)


def load_entry(code, entry, workspace):
    """exec 用户代码，返回 (入口函数, 模块 globals)。失败时抛异常。"""
    g = {
        "__builtins__": build_safe_builtins(),
        "__name__": "__wyc_custom__",
        "__doc__": None,
        "wyc": WycHelper(workspace),
    }
    compiled = compile(code, "<custom_tool>", "exec")
    exec(compiled, g)   # noqa: S102 —— 本文件的存在意义就是执行用户代码

    fn = g.get(entry)
    if fn is None:
        names = [k for k, v in g.items()
                 if callable(v) and not k.startswith("_")]
        hint = ("找到这些函数：" + ", ".join(names)) if names else "文件里没有任何顶层函数"
        raise NameError(f"找不到入口函数 {entry}()。{hint}")
    if not callable(fn):
        raise TypeError(f"{entry} 不是函数（实际是 {type(fn).__name__}）")
    return fn, g


def do_introspect(req):
    """只推导 schema，不调用函数。"""
    code = req.get("code") or ""
    entry = req.get("entry") or "main"
    try:
        fn, _ = load_entry(code, entry, req.get("workspace"))
    except SyntaxError as e:
        fail(f"语法错误（第 {e.lineno} 行）：{e.msg}", error_line=e.lineno)
    except Exception as e:
        fail(f"代码加载失败：{e.__class__.__name__}: {e}")

    schema, params, main_desc = derive_schema(fn)
    emit({
        "protocol": PROTOCOL_VERSION,
        "status": "success",
        "entry": entry,
        "schema": schema,
        "params": params,
        "description": main_desc,
    })


def do_run(req):
    """调用入口函数并返回结果。"""
    code = req.get("code") or ""
    entry = req.get("entry") or "main"
    args = req.get("args") or {}
    max_chars = int(req.get("max_output_chars") or 8000)

    try:
        fn, g = load_entry(code, entry, req.get("workspace"))
    except SyntaxError as e:
        fail(f"语法错误（第 {e.lineno} 行）：{e.msg}", error_line=e.lineno)
    except Exception as e:
        fail(f"代码加载失败：{e.__class__.__name__}: {e}")

    clean, missing = filter_args(fn, args)
    if missing:
        fail(f"缺少必填参数：{', '.join(missing)}")

    # 调用（支持 async def main）
    try:
        res = fn(**clean)
        if _inspect.isawaitable(res):
            res = _asyncio.run(res)
    except Exception as e:
        tb = _traceback.format_exc()
        _real_print(tb, file=_sys.stderr)
        fail(f"执行失败：{e.__class__.__name__}: {e}")

    out = normalize_result(res, max_chars)

    # 用户通过 wyc.image() 声明的图片
    try:
        helper = g.get("wyc")
        imgs = helper.images() if helper is not None else []
        if imgs and not out.get("screenshot"):
            out["screenshot"] = imgs[0]
        if len(imgs) > 1:
            out["images"] = imgs
    except Exception:
        pass

    emit(out)


def main():
    req = read_request()
    if req is None:
        fail("未收到合法的请求（stdin 为空或不是 JSON）")

    ver = req.get("protocol")
    if ver != PROTOCOL_VERSION:
        fail(f"协议版本不匹配：期望 {PROTOCOL_VERSION}，收到 {ver}")

    code = req.get("code")
    if not isinstance(code, str) or not code.strip():
        fail("缺少 code 参数")

    # 切到工作区（宿主已设 cwd，这里再保一道）
    ws = req.get("workspace")
    if isinstance(ws, str) and ws and _os.path.isdir(ws):
        try:
            _os.chdir(ws)
        except Exception:
            pass

    # 注册中文字体（画图时中文不变方块）
    setup_font(req.get("font_path"))

    if req.get("introspect_only"):
        do_introspect(req)
    else:
        do_run(req)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:   # 最后一道兜底：绝不把裸 traceback 丢给宿主
        try:
            _real_print(_traceback.format_exc(), file=_sys.stderr)
        except Exception:
            pass
        fail(f"执行器内部错误：{e.__class__.__name__}: {e}")

