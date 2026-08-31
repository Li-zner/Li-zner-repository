import asyncio
import json
import time
import uuid
import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import Dict
import httpx

from ..models.schemas import ChatRequest
from ..middleware.auth import get_current_user
from ..middleware.rate_limit import check_qps, check_concurrent, release_concurrent, get_daily_usage, update_daily_usage
from ..core.redis import get_redis
from ..core.memory_manager import MemoryManager
from ..core.config import (
    DAILY_REQUEST_LIMIT, DAILY_TOKEN_LIMIT,
    DEEPSEEK_API_BASE, DEEPSEEK_API_TIMEOUT,
    DEEPSEEK_MODEL, DEEPSEEK_FLASH_MODEL,
    DEEPSEEK_FALLBACK_MESSAGE,
    SUMMARY_THRESHOLD, TOOL_TIMEOUT,
    HTTP_TIMEOUT_MEDIUM, HTTP_TIMEOUT_LONG, LLM_CONNECT_TIMEOUT,
)
from ..core.logging import setup_logging
from ..core.concurrency import llm_semaphore
from ..core.jfast import loads as jloads, dumps as jdumps
from ..agents.memory import compress_message_history
from ..agents.orchestrator import build_shared_context
from ..core.metrics import gateway_requests_total
from ..core.semantic_cache import SemanticCache
from ..core.stream_utils import sse, dispatch_tool, build_file_context, stream_llm
from ..core.quota import is_quota_exhausted, inc_used_questions
import re


# ---------- 非流式接口响应模型（P2 #22：接口文档可推导、字段完整）----------
# 必须定义在所有路由装饰器之前（装饰器求值 response_model 参数时需已存在）
class UploadResponse(BaseModel):
    """/upload 响应"""
    file_id: str
    filename: str
    ext: str
    size: int
    uploaded_by: str
    uploaded_at: str
    text_length: int
    parse_note: str
    text_preview: str
    text_content: str


class FileInfoResponse(BaseModel):
    """/files/{file_id} 响应"""
    file_id: str
    filename: str
    ext: str
    size: int
    uploaded_by: str
    uploaded_at: str
    text_length: int
    parse_note: str
    text_content: str


class TaskResultResponse(BaseModel):
    """任务结果/状态响应"""
    status: str
    content: str
    session_id: str


def _lang_instruction(req) -> str:
    """根据界面语言生成 LLM 输出语言指令（追加到 system prompt）"""
    lang = getattr(req, 'lang', 'zh') or 'zh'
    if lang == 'en':
        return (
            "\n\n【Output Language Requirement】\n"
            "1. Always respond in **English**.\n"
            "2. Keep proper nouns (names, cities, brand/tech terms) as-is or use conventional translations.\n"
            "3. Keep numbers, amounts and dates unchanged.\n"
            "4. Do not output Chinese unless the user explicitly asks."
        )
    return (
        "\n\n【输出语言要求】\n"
        "1. 请始终使用**中文（简体）**回答用户的所有问题。\n"
        "2. 除非用户明确要求，否则不要输出英文。"
    )


def _safe_format_prompt(prompt: str, **kwargs) -> str:
    """安全格式化人格提示词：缺 {today}/{name} 等占位符时不抛 KeyError（P0 #3）
    占位符缺失时返回原始提示词，避免单个坏配置打崩整个请求。"""
    try:
        return prompt.format(**kwargs)
    except (KeyError, ValueError):
        logger.warning("人格提示词格式化失败（缺少占位符），使用原始提示词")
        return prompt


def _hide_reasoning(persona_id: str, persona) -> bool:
    """是否隐藏思考过程：人格配置 show_reasoning=False 或 求职助手(me) 不展示
    说明：show_reasoning 应作为人格配置布尔字段（P2 #24 完整迁移需改 core/persona_manager），
    当前用 getattr 兼容旧配置；未配置时默认展示思考。"""
    show = getattr(persona, "show_reasoning", True) if persona else True
    return (not show) or persona_id == "me"


# ============================================================
# 思考过程元数据过滤：防止 AI 把系统提示词/内部设定/负面信号泄露给用户
# ============================================================
# 强指纹：命中任一即判定该思考段泄露 → 整段不展示
# （系统提示词引用、人设设定、系统限制、工具失败等负面信号）
_REASONING_LEAK_PATTERNS = [
    # ---- 系统提示词 / 指令引用 ----
    "system prompt", "system_prompt", "systemPrompt",
    "根据我的系统提示词", "我的系统提示词", "系统提示词要求", "按照系统提示词",
    "my system prompt", "My system prompt", "my system prompt says",
    "according to my system", "According to my system",
    "the instruction says", "instructions say", "per my instructions",
    "the system prompt says", "system says", "as instructed",
    "my instructions say", "my instructions", "per my system",
    "output language requirement", "always respond in",
    "do not output chinese", "unless the user explicitly asks",
    # ---- 中文指令引用 ----
    "输出语言要求", "请始终使用", "不要输出英文", "不要输出中文",
    "按照指令", "根据指令", "指令要求", "系统设定", "系统限制",
    "系统配置", "根据人设", "按人设", "角色设定", "人设要求",
    # ---- 多 Agent 编排指令复述（模型思考时复述注入指令 → 泄露）----
    "不要提", "不要提及", "禁止提及", "不得提及", "别提",
    "多Agent", "多 Agent", "讨论摘要", "自然口吻", "口吻整合",
    "各领域专家", "专家意见", "专家讨论",
    # ---- 工具调用计划复述（模型思考时写出内部函数名/调用签名 → 泄露）----
    "web_search", "search_knowledge", "search_project_knowledge",
    "fetch_weather_async", "call_sub_agent",
    "query_weather", "query_hotel", "query_route", "query_food",
    # ---- 负面信号：工具失败 / 异常 ----
    "工具调用失败", "调用失败", "工具执行失败", "工具超时", "工具报错",
    "tool call failed", "tool failed", "tool execution failed",
    "tool timeout", "tool error", "call failed",
    "重试失败", "重试仍失败", "请求失败", "请求超时",
    "failed to", "error occurred", "an error occurred",
    # ---- 内部实现细节 ----
    "response_format", "json_object", "llm_semaphore",
    "tools_enabled", "knowledge_base", "max_tokens", "temperature",
    "## 核心原则", "## 可用工具", "## 默认值",
    "防幻觉", "最小工具调用", "路由识别",
    "【输出格式】", "只输出JSON",
]

# 整段抑制：即使正常思考也可能提到工具名，但这些是确凿的系统设定引用
_REASONING_SUPPRESS_PATTERNS = [
    "system prompt", "system_prompt", "systemPrompt",
    "根据我的系统提示词", "我的系统提示词", "系统提示词要求", "按照系统提示词",
    "my system prompt", "according to my system",
    "the instruction says", "instructions say", "per my instructions",
    "as instructed", "my instructions say",
    "output language requirement", "always respond in", "do not output chinese",
    "输出语言要求", "请始终使用", "不要输出英文", "不要输出中文",
    "系统设定", "系统限制", "系统配置", "根据人设", "按人设", "角色设定", "人设要求",
    "不要提", "不要提及", "禁止提及", "不得提及", "别提",
    "多Agent", "多 Agent", "讨论摘要", "自然口吻", "口吻整合",
    "工具调用失败", "调用失败", "工具执行失败", "工具超时", "工具报错",
    "tool call failed", "tool failed", "tool execution failed", "tool timeout",
    "重试失败", "重试仍失败",
]

# 预编译正则：流式思考每 Chunk 上百次过滤，避免逐模式 in 扫描（P1 #14）
_REASONING_LEAK_RE = re.compile(
    "|".join(re.escape(p) for p in _REASONING_LEAK_PATTERNS), re.IGNORECASE
)
_REASONING_SUPPRESS_RE = re.compile(
    "|".join(re.escape(p) for p in _REASONING_SUPPRESS_PATTERNS), re.IGNORECASE
)


def _reasoning_leaked(text: str) -> bool:
    """检测思考内容是否泄露了系统设定/负面信号（整段抑制）"""
    if not text:
        return False
    return _REASONING_SUPPRESS_RE.search(text) is not None


def _sanitize_reasoning(text: str, persona_id: str = "") -> str:
    """过滤思考过程中的敏感内容；命中强泄露指纹返回空串（调用方跳过发送）

    仅对 旅游/法律/综合 模式生效；求职助手（me）不过滤（其内容正常，
    不涉及工具失败/系统设定泄露）。
    策略：
    1. 整段检测：命中系统设定/失败等强指纹 → 整段不展示（返回空串）
    2. 否则逐行剔除敏感碎片，保留正常推理（含重复用户输入，可接受）
    """
    if not text:
        return ""
    # 求职助手不过滤（用户明确要求）
    if persona_id == "me":
        return text
    if _reasoning_leaked(text):
        return ""
    lines = text.split("\n")
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if _REASONING_LEAK_RE.search(stripped):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


# ---------- DeepSeek API Key 轮询（Round Robin + 连续失败剔除）----------
# 模块加载时缓存 Key 列表，避免每次请求重复 os.getenv（P1 #12 消除 3 次系统调用）
_DEEPSEEK_KEYS = [
    k for k in (
        os.getenv("DEEPSEEK_API_KEY", ""),
        os.getenv("DEEPSEEK_API_KEY_2", ""),
        os.getenv("DEEPSEEK_API_KEY_3", ""),
    ) if k
]
_key_fail_count: Dict[str, int] = {}   # key -> 连续失败次数
_key_round_index = 0                   # 轮询游标
_key_lock = asyncio.Lock()             # 保护上面两个共享状态


async def _get_deepseek_key() -> str:
    """带权重的 Round Robin 选择 Key，连续失败 >=3 的 Key 暂被剔除（P1 #13）"""
    global _key_round_index   # 函数内 rebind，需声明 global，否则被当作局部变量（UnboundLocalError）
    async with _key_lock:
        if not _DEEPSEEK_KEYS:
            return ""
        healthy = [k for k in _DEEPSEEK_KEYS if _key_fail_count.get(k, 0) < 3]
        if not healthy:
            healthy = _DEEPSEEK_KEYS   # 全部被剔除则重置，避免永久降级
        key = healthy[_key_round_index % len(healthy)]
        _key_round_index = (_key_round_index + 1) % len(healthy)
    tag = f"...{key[-4:]}" if key else "NONE"
    logger.info(f"🔑 Key 轮询: {tag}")
    return key


async def _mark_key_result(key: str, ok: bool):
    """记录 Key 调用结果：成功清零失败计数，失败累计（供健康剔除使用）"""
    async with _key_lock:
        if ok:
            _key_fail_count.pop(key, None)
        else:
            _key_fail_count[key] = _key_fail_count.get(key, 0) + 1


# ---------- 推理内容过滤器：屏蔽内部提示词泄漏 ----------
_INTERNAL_PATTERNS = [
    r'工具定义.*?(?:query_weather|query_hotel|query_route|query_food)',
    r'(?:query_weather|query_hotel|query_route|query_food).*?(?:工具|函数|tool)',
    r'(?:parameters|type|description|required).*?(?:object|string)',
    r'你是旅行规划总控助手',
    r'判断用户意图.*?天气.*?酒店.*?路线.*?美食',
    r'根据意图调用对应的工具',
    r'如果用户问题涉及多个方面',
    r'收到工具返回的JSON数据后',
    r'你必须记住用户之前提到的',
    r'如果工具返回的JSON中包含',
    r'语气稍微热情即可',
    r'思考.*?过程请使用中文进行推理',
    r'保留专业术语的英文原名',
    r'当用户询问出行路线时',
    r'除非用户明确说.*?驾车.*?开车.*?自驾',
    r'根据两地距离智能选择',
    r'mode 字段.*?智能选择',
    r'工具 query_route 返回的 mode',
    r'当用户说.*?我在XX.*?或告知位置',
    r'这仅用于确定出发地和天气查询',
    r'不要擅自将此位置用于酒店推荐',
    r'如果用户问天气但没有说城市',
    r'用户告知位置后，不要反问用户位置',
    r'role.*?tool.*?tool_call_id',
    r'## 基本信息',
    r'## 角色定位',
    r'## 交通出行规则',
    r'## 定位与位置处理规则',
    r'【用户当前位置】',
    r'【用户画像】',
    r'用户上传了以下文件',
    r'【用户上传文件:',
    r'【文件结束】',
    # ── 人格系统元数据 ──
    r'你是{name}',
    r'你的专长是',
    r'## 你的角色',
    r'## 核心规则',
    r'### 规则A',
    r'### 规则B',
    r'### 规则C',
    r'禁止反问',
    r'禁止编造',
    r'有工具必须用工具',
    r'## 综合回答规则',
    r'## 情绪感知.*?旅游推荐映射',
    r'### 情绪.*?旅游风格映射',
    r'### 情绪推荐规则',
    r'## 禁止拒绝规则',
    r'## 多 Agent 协作',
    r'## 输出格式',
    r'## 🚨 核心规则',
    r'请参考这些专业意见来回答',
    r'禁止在回答中提及',
    r'### 你的任务',
    r'## 用户问题',
    r'各位专家的初步分析意见',
    r'## 各位专家的初步分析意见',
    r'## 各位专家的分析',
    r'只输出JSON',
    r'【约束】',
    r'## 定位与位置处理',
    r'## 多领域支持规则',
]

# 合并为单一预编译正则，逐行一次匹配（P1 #14 消除逐模式 for 循环）
_INTERNAL_PATTERNS_RE = re.compile("|".join(_INTERNAL_PATTERNS), re.IGNORECASE)

def _clean_reasoning(text: str) -> str:
    if not text:
        return text
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _INTERNAL_PATTERNS_RE.search(stripped):
            continue
        if re.match(r'^\s*[{}\[\],]\s*$', stripped):
            continue
        if re.match(r'^\s*"[^"]*"\s*:\s*\{', stripped):
            continue
        if re.match(r'^\s*"[^"]*"\s*:\s*\[', stripped):
            continue
        cleaned.append(line)
    return '\n'.join(cleaned)


logger = setup_logging()
router = APIRouter(prefix="/v2", tags=["v2"])

# ---------- 文件上传 ----------
from pathlib import Path
# 基于 __file__ 的绝对路径（不依赖进程 CWD），统一落在项目根 uploads/（已 gitignore）
UPLOAD_DIR = str(Path(__file__).resolve().parents[2] / "uploads")
Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

# 允许的文件类型（文本类 + 文档类 + 图片/PDF/Word）
# 安全边界：生产环境禁止源码/密钥/脚本类扩展名（.py/.js/.env/.sql 等），
# 防止用户上传 main.py / .env 把业务源码或密钥送入 LLM（P0 #8）
ALLOWED_EXTENSIONS = {
    '.txt', '.md', '.csv', '.json', '.xml', '.yaml', '.yml',
    '.rst', '.rtf',
    '.pdf',                # PDF 文档
    '.jpg', '.jpeg',       # JPEG 图片
    '.png',                # PNG 图片
    '.bmp', '.webp',       # 其他图片格式
    '.docx',               # Word 文档
}

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB (PDF/图片可能较大)
# 存入 Redis 的提取文本上限（200KB 足够 LLM 理解，防恶意上传撑爆 Redis 内存，P0 #4）
MAX_TEXT_CONTENT = 200 * 1024
# 历史压缩时保留的最近消息条数（与 SUMMARY_THRESHOLD 的 token 阈值构成双保险，P2 #23）
_HISTORY_COMPACT_MSGS = 6

# 文本文件魔数 / 内容特征检测
TEXT_MAGIC_PATTERNS = {
    b'{\n', b'{\r',        # JSON 起始
    b'<',                   # HTML/XML
    b'---',                 # YAML front matter
    b'#',                   # 很多配置文件/脚本
    b'\xef\xbb\xbf',        # UTF-8 BOM
    b'\xff\xfe',            # UTF-16 LE BOM
    b'\xfe\xff',            # UTF-16 BE BOM
}

def _is_likely_text_file(raw_data: bytes) -> bool:
    """通过检测空字节和可打印字符比例，判断是否为真正的文本文件"""
    # 检查空字节 —— 文本文件不应包含空字节
    if b'\x00' in raw_data:
        return False
    # 检查前 512 字节是否含已知文本特征
    head = raw_data[:512]
    for pattern in TEXT_MAGIC_PATTERNS:
        if head.startswith(pattern):
            return True
    # 统计可打印字符比例
    if len(head) == 0:
        return False
    printable = sum(1 for b in head if 9 <= b <= 10 or 13 == b or 32 <= b <= 126 or b >= 128)
    return (printable / len(head)) >= 0.8


def _stream_upload_to_disk(src, path: str, max_bytes: int) -> int:
    """分块把上传流写入磁盘，返回实际字节数；超过 max_bytes 抛 ValueError（P1 #20）"""
    total = 0
    with open(path, "wb") as out:
        while True:
            chunk = src.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("file too large")
            out.write(chunk)
    return total


async def extract_text_content_async(filepath: str, filename: str) -> str:
    """异步提取文本文件内容"""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"不支持的文件类型: {ext}，仅支持文本类文件")
    loop = asyncio.get_running_loop()

    def _read_file():
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            with open(filepath, 'r', encoding='gbk') as f:
                return f.read()

    try:
        content = await loop.run_in_executor(None, _read_file)
    except Exception:
        raise ValueError("无法解码文件内容，请确保文件为 UTF-8 或 GBK 编码的文本文件")

    # 限制内容长度，避免超出 token 限制
    max_chars = 50000
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n\n...（文件过长，仅截取前 {max_chars} 字符）"
    return content


async def _extract_upload_text(save_path: str, safe_filename: str, ext: str, loop) -> tuple:
    """按扩展名解析上传文件为文本；返回 (text_content, parse_note)"""
    BINARY_EXTS = {'.pdf', '.jpg', '.jpeg', '.png', '.bmp', '.webp', '.docx'}
    if ext in BINARY_EXTS:
        # 使用文档解析器
        from ..agents.document_parser import parse_document
        result = await parse_document(save_path, safe_filename)
        if result["success"]:
            return result["text"], f"（{result['format']}，已自动提取文本）"
        return result.get("error", "解析失败"), f"（{result['format']}，提取失败）"
    # 文本类文件直接读取
    try:
        text_content = await extract_text_content_async(save_path, safe_filename)
        return text_content, "（文本文件，已直接读取）"
    except ValueError as e:
        # 解析失败：删除落盘文件；可能已被并发清理，忽略 FileNotFoundError（P0 #2）
        try:
            await loop.run_in_executor(None, lambda: os.remove(save_path))
        except FileNotFoundError:
            pass
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    """上传文件（支持 TXT/PDF/图片/Word 等），返回文件ID与提取的文本"""
    safe_filename = os.path.basename(file.filename or "")
    if not safe_filename:
        raise HTTPException(status_code=400, detail="文件名无效")

    ext = os.path.splitext(safe_filename)[1].lower()
    if not ext:
        raise HTTPException(status_code=400, detail="文件缺少扩展名")
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 '{ext}'"
        )

    file_id = str(uuid.uuid4())
    save_path = os.path.join(UPLOAD_DIR, f"{file_id}{ext}")
    loop = asyncio.get_running_loop()
    # 分块流式写入磁盘（避免整文件读入内存，100 并发上传不再占用 2GB 内存，P1 #20）
    try:
        size = await loop.run_in_executor(None, _stream_upload_to_disk, file.file, save_path, MAX_FILE_SIZE)
    except ValueError:
        await loop.run_in_executor(None, lambda: os.remove(save_path))
        raise HTTPException(status_code=400, detail=f"文件过大，最大支持 {MAX_FILE_SIZE // 1024 // 1024}MB")

    # ---- 按扩展名解析为文本（含失败时清理落盘文件）----
    text_content, parse_note = await _extract_upload_text(save_path, safe_filename, ext, loop)

    # Bug #7 修复：解析成功后删除落盘文件，防止 uploads/ 目录无限累积。
    # 文件内容已存入 Redis（1h 过期），落盘文件不再被任何路径读取；
    # 解析失败路径已在 _extract_upload_text 内自行清理。残留场景仅剩
    # 「写入磁盘后、解析前进程崩溃」这一窄窗口，由目录清理任务兜底（低风险）。
    try:
        await loop.run_in_executor(None, lambda: os.remove(save_path))
    except FileNotFoundError:
        pass

    # 限制存入 Redis 的文本大小（防恶意上传撑爆 Redis 内存，P0 #4）
    if len(text_content) > MAX_TEXT_CONTENT:
        logger.warning(f"文件文本超长，截断至 {MAX_TEXT_CONTENT} 字节: {safe_filename}")
        text_content = text_content[:MAX_TEXT_CONTENT]

    # 存入 Redis（1小时过期）
    r = await get_redis()
    file_meta = {
        "file_id": file_id,
        "filename": safe_filename,
        "ext": ext,
        "size": size,
        "uploaded_by": current_user["username"],
        "uploaded_at": datetime.now().isoformat(),
        "text_length": len(text_content),
        "parse_note": parse_note,
    }
    await r.setex(f"file:{file_id}:content", 3600, text_content)
    await r.setex(f"file:{file_id}:meta", 3600, json.dumps(file_meta))

    logger.info(f"📎 文件上传成功: user={current_user['username']}, file={safe_filename}, "
                f"type={ext}, size={size}, text_len={len(text_content)}, note={parse_note}")

    return {
        **file_meta,
        "text_preview": text_content[:2000] + ("..." if len(text_content) > 2000 else ""),
        "text_content": text_content[:50000]
    }


@router.get("/files/{file_id}", response_model=FileInfoResponse)
async def get_file_info(
    file_id: str,
    current_user: dict = Depends(get_current_user)
):
    """获取已上传文件的信息和内容"""
    r = await get_redis()
    meta_raw = await r.get(f"file:{file_id}:meta")
    if not meta_raw:
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    meta = json.loads(meta_raw)
    # Bug #6 修复（IDOR 越权）：文件只能由上传者本人读取，admin 豁免可代查。
    # 之前任何人凭 file_id 都能读任意文件内容。
    if meta.get("uploaded_by") != current_user["username"] and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="无权访问该文件")
    content = await r.get(f"file:{file_id}:content")
    return {
        **meta,
        "text_content": content or ""
    }



# ---------- 降级备胎（单级降级：DeepSeek Flash；Dify 已彻底摒弃）----------
async def fallback_flash(req: ChatRequest, username: str):
    """第一级降级：使用 DeepSeek Flash（无工具调用，纯聊天）"""
    from ..core.metrics import gateway_errors_total
    gateway_errors_total.labels(status='fallback_flash').inc()
    logger.info(f"⚡ 触发 Flash 降级: username={username}")
    deepseek_api_key = await _get_deepseek_key()
    if not deepseek_api_key:
        logger.warning("Flash 降级无 API Key，跳过")
        return
    try:
        async with llm_semaphore:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
                async with client.stream(
                    "POST",
                    f"{DEEPSEEK_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {deepseek_api_key}", "Content-Type": "application/json"},
                    json={
                        "model": DEEPSEEK_FLASH_MODEL,
                        "messages": [
                            {"role": "system", "content": "你是AI助手，请直接回答用户的问题。如果涉及法律问题，回答相关法律规定。如果无法查询知识库，请用已有知识回答。" + _lang_instruction(req)},
                            {"role": "user", "content": req.query}
                        ],
                        "user": username,
                        "stream": True,
                        "temperature": 0.3,
                        "max_tokens": 2048
                    }
                ) as resp:
                    resp.raise_for_status()
                    await _mark_key_result(deepseek_api_key, True)
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                yield "data: [DONE]\n\n"
                                return
                            try:
                                data = jloads(data_str)
                                chunk = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                if chunk:
                                    # ===== DFA 敏感词过滤 =====
                                    from ..core.safety_filter import get_filter
                                    sf = get_filter()
                                    result = sf.check_stream(chunk)
                                    if not result['safe']:
                                        logger.warning(f"🛡️ DFA 拦截Flash降级: 敏感词='{result['triggered_word']}'")
                                        yield sse("answer_chunk", sf.safe_message)
                                        yield sse("answer_complete", sf.safe_message) + "data: [DONE]\n\n"
                                        return
                                    # ============================
                                    yield sse("answer_chunk", chunk)
                            except (json.JSONDecodeError, ValueError):
                                pass
                    # 流正常结束但未收到 [DONE]（兜底）
                    yield "data: [DONE]\n\n"
    except Exception as e:
        await _mark_key_result(deepseek_api_key, False)
        logger.warning(f"Flash 降级失败: {e}")

async def fallback_chain(req, username, cache_ctx: str = ""):
    """降级链：仅 Flash（Dify 已移除）"""
    try:
        async for chunk in fallback_flash(req, username):
            yield chunk
    except Exception:
        # 穿透防护：兜底失败 → 写入短 TTL 空值占位，避免后续请求持续打 LLM
        # 用 await 而非 create_task，确保写入完成（P0 #11 防击穿占位不悬空）
        try:
            await SemanticCache.set_empty(req.query, cache_ctx=cache_ctx, ttl=30)
        except Exception:
            pass
        yield sse("answer_complete", DEEPSEEK_FALLBACK_MESSAGE) + "data: [DONE]\n\n"

# ---------- V2 主力路由 ----------
async def _finalize_answer(mm, query: str, answer: str, cache_ctx: str,
                           username: str, today: str, *, write_cache: bool = True) -> None:
    """保存对话并累计用量（generate 内 6 处重复 finalize 尾的统一封装）

    行为等价：save_messages + 可选写语义缓存 + update_daily_usage + inc_used_questions。
    saved_normally 标志由调用方在 return 前置 True（闭包状态，helper 无法改）。
    write_cache=False 用于 max_steps 截断的不完整回答（Bug #2：残缺答案禁止入缓存）。
    """
    await mm.save_messages(
        {"role": "user", "content": query},
        {"role": "assistant", "content": answer},
    )
    if write_cache:
        asyncio.create_task(SemanticCache.set(query, answer, cache_ctx=cache_ctx))
    await update_daily_usage(username, today, inc_request=1, inc_token=0)
    asyncio.create_task(inc_used_questions(username))


async def _route_intent(user_query: str, persona_id: str) -> tuple:
    """意图路由：先分类用户意图，只加载匹配工具；返回 (matched_agents, is_simple, is_recommend, tools)

    民法典人格强制 search_knowledge（路由裁决表已处理领域分类）；
    分类异常降级为全部工具（纯 LLM 兜底）。
    """
    try:
        from ..agents.router import classify_intent, get_tools_for_intent
        intent_result = await classify_intent(user_query, use_llm=False)
        matched_agents = intent_result.get("agents", [])
        is_simple = intent_result.get("is_simple", True)
        is_recommend = intent_result.get("is_recommend", False)
        logger.info(f"🔀 意图路由: agents={matched_agents}, simple={is_simple}, recommend={is_recommend}")

        # 民法典人格强制使用 search_knowledge（路由裁决表已处理领域分类）
        if persona_id == "civil_code":
            matched_agents = ["search_knowledge"]
            is_simple = True
            is_recommend = False
            logger.info("⚖️ 民法典人格，强制意图=search_knowledge")

        if matched_agents:
            # 只加载匹配到的 Agent 的工具 → prompt 更小，LLM 响应更快
            tools = get_tools_for_intent(matched_agents)
        else:
            # 未匹配到任何工具 → 纯 LLM 回答，不给工具定义
            tools = []
    except Exception as route_err:
        logger.warning(f"意图路由异常（降级为全部工具）: {route_err}")
        from ..agents.tool_definitions import ALL_TOOLS
        return [], True, False, ALL_TOOLS
    return matched_agents, is_simple, is_recommend, tools


def _record_token_usage(_stream_usage: dict, username: str, conv_id: str):
    """Token 精细计量 + 扣费（10元/万token，模拟模式）

    Prometheus 指标 + 异步扣费；扣费失败仅记 warning，不影响对话主流程。
    """
    from ..core.metrics import llm_tokens_total, llm_tokens_detail, llm_requests_total
    prompt_tk = _stream_usage.get("prompt_tokens", 0)
    completion_tk = _stream_usage.get("completion_tokens", 0)
    if prompt_tk or completion_tk:
        llm_tokens_total.labels(type='input').inc(prompt_tk)
        llm_tokens_total.labels(type='output').inc(completion_tk)
        llm_tokens_detail.labels(model=DEEPSEEK_MODEL, endpoint='v2_chat', type='input').inc(prompt_tk)
        llm_tokens_detail.labels(model=DEEPSEEK_MODEL, endpoint='v2_chat', type='output').inc(completion_tk)
    llm_requests_total.labels(model=DEEPSEEK_MODEL, endpoint='v2_chat', status='success').inc()

    if prompt_tk or completion_tk:
        total_tk = prompt_tk + completion_tk
        try:
            from ..payment.service import deduct_token_cost
            asyncio.create_task(deduct_token_cost(
                user_id=username,
                token_count=total_tk,
                session_id=conv_id or "",
                remark=f"AI对话消耗 {total_tk} tokens（输入 {prompt_tk} + 输出 {completion_tk}）",
            ))
        except Exception as deduct_err:
            logger.warning(f"Token扣费失败（不影响对话）: {deduct_err}")


@router.post("/chat/stream")
async def chat_stream_v2(
    req: ChatRequest,
    current_user: dict = Depends(get_current_user)
):
    username = current_user["username"]
    user_role = current_user.get("role", "user")
    # 知识库权限：admin 不过滤；普通用户按其权限组过滤（空=仅公开）
    user_perms = None if user_role == "admin" else (current_user.get("permissions") or [])
    request_id = str(uuid.uuid4())
    start_time = time.time()
    
    today = datetime.now().strftime("%Y-%m-%d")
    # ===== 用户隔离：角色分级限流 + 并发控制 =====
    if not await check_qps(username, user_role):
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    if not await check_concurrent(username, user_role):
        # 拒绝时不占用并发槽位，禁止再 release（P0 #27：避免双重释放把计数减为负数）
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="并发请求过多，请稍后再试")
    usage = await get_daily_usage(username, today)
    limits = {"daily_req": DAILY_REQUEST_LIMIT, "daily_token": DAILY_TOKEN_LIMIT}
    if user_role == "admin":
        from ..middleware.rate_limit import _ROLE_LIMITS
        limits = _ROLE_LIMITS["admin"]
    if usage["request_count"] >= limits["daily_req"]:
        await release_concurrent(username)
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="今日请求次数已达上限")
    if usage["token_sum"] >= limits["daily_token"]:
        await release_concurrent(username)
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="今日 Token 消耗已达上限")
    # GitHub 试用额度：受限用户累计 1 万 token 后需绑定手机号解锁（admin 豁免）
    if user_role != "admin" and is_quota_exhausted(current_user):
        await release_concurrent(username)
        raise HTTPException(status_code=402, detail="免费额度已用完，请绑定手机号后继续使用")

    async def generate():
        # 语言指令在入口计算一次，后续三处复用（P1 #15 减少重复字符串拼接）
        lang_instr = _lang_instruction(req)
        partial_answer = ""
        conv_id = None
        saved_normally = False  # 标记正常路径是否已完成保存，防止 finally 重复保存

        # ===== 缓存上下文 + 会话/画像提前加载（Bug #1：语义缓存按上下文隔离）=====
        # 画像指纹参与缓存键：A 用户的个性化回答不会被缓存命中给 B 用户。
        # 画像加载失败按无画像处理，不阻塞主流程。persona 取请求显式声明值
        # （默认人格共用 "" 键，确定性好，不受全局人格切换竞态影响）。
        conv_id = req.conversation_id or f"conv_{username}_{int(time.time())}"
        mm = MemoryManager(username, conv_id)
        try:
            user_profile = await mm.get_profile()
        except Exception as _pf_err:
            logger.warning(f"画像加载失败（按无画像处理）: {_pf_err}")
            user_profile = ""
        _cache_ctx = SemanticCache.build_cache_ctx(
            getattr(req, "persona_id", "") or "", req.user_location or "", user_profile
        )

        # ===== 前置路由裁决表（硬规则引擎）- 在一切逻辑之前 =====
        _civil_mapped_query = None  # 保存口语映射后的查询，供 search_knowledge 使用
        _civil_route_match = None   # 保存路由匹配类型
        if getattr(req, 'persona_id', None) == "civil_code":
            try:
                from ..agents.routing_table import route_query
                _route = route_query(req.query)
                _civil_route_match = _route.match_type  # 保存供后续使用
                if _route.action == "reject":
                    logger.info(f"🚫 路由裁决: reject → {_route.domain} ({_route.match_type})")
                    yield sse("answer_complete", _route.message) + "data: [DONE]\n\n"
                    return
                elif _route.action == "pass":
                    logger.info(f"✅ 路由裁决: pass → {_route.match_type}")
                    if _route.mapped_query:
                        _civil_mapped_query = _route.mapped_query
                        logger.info(f"📝 口语映射待用: {req.query[:30]}... → {_route.mapped_query[:60]}...")
                # retry 等情况后续处理
            except Exception as _re:
                logger.warning(f"路由裁决异常（不影响正常流程）: {_re}")

        # ===== 语义缓存拦截（有文件上传时跳过缓存，必须重新处理）=====
        if not req.file_ids:
            try:
                cached_response = await SemanticCache.get(req.query, cache_ctx=_cache_ctx)
                # 穿透占位命中（__EMPTY__）→ 说明底层最近失败过，直接返回提示，不再打 LLM（防穿透风暴）
                if cached_response and await SemanticCache.is_empty(cached_response):
                    logger.info(f"⚡ 穿透占位命中，直接返回提示（不再打 LLM）")
                    _busy_msg = "服务暂时繁忙，请稍后重试。" if getattr(req, 'lang', 'zh') != 'en' else "Service is busy, please try again later."
                    yield sse("answer_chunk", _busy_msg)
                    yield sse("answer_complete", _busy_msg) + "data: [DONE]\n\n"
                    return
                if cached_response and not await SemanticCache.is_empty(cached_response):
                    # ===== DFA 敏感词过滤 =====
                    from ..core.safety_filter import get_filter
                    sf = get_filter()
                    if sf.contains_sensitive(cached_response):
                        logger.warning(f"🛡️ DFA 拦截语义缓存")
                        cached_response = sf.safe_message
                    # ============================
                    logger.info(f"✅ 语义缓存命中")
                    # 缓存内容直接整段返回（前端会流式渲染）
                    yield sse("answer_chunk", cached_response)
                    yield sse("answer_complete", cached_response) + "data: [DONE]\n\n"
                    return
            except Exception as cache_err:
                logger.warning(f"缓存查询失败，继续正常推理: {cache_err}")

        # ===== 防击穿（互斥重建）：同一 query 并发时只允许一个调用 LLM =====
        _rebuild_lock = None
        if not req.file_ids:
            try:
                _rebuild_lock = await SemanticCache.acquire_rebuild_lock(req.query, cache_ctx=_cache_ctx)
                if _rebuild_lock is None:
                    # 已有请求在重建：等待后重读缓存（可能命中刚写入的结果）
                    await asyncio.sleep(0.3)
                    _cached_retry = await SemanticCache.get(req.query, cache_ctx=_cache_ctx)
                    if _cached_retry and await SemanticCache.is_empty(_cached_retry):
                        # 重试也命中穿透占位：直接返回提示，不再等待重建
                        _busy_msg = "服务暂时繁忙，请稍后重试。" if getattr(req, 'lang', 'zh') != 'en' else "Service is busy, please try again later."
                        yield sse("answer_chunk", _busy_msg)
                        yield sse("answer_complete", _busy_msg) + "data: [DONE]\n\n"
                        return
                    if _cached_retry and not await SemanticCache.is_empty(_cached_retry):
                        yield sse("answer_chunk", _cached_retry)
                        yield sse("answer_complete", _cached_retry) + "data: [DONE]\n\n"
                        return
            except Exception as _rebuild_err:
                # 锁获取失败不阻塞业务：降级为直接重建
                logger.warning(f"防击穿锁获取失败（继续重建）: {_rebuild_err}")
                _rebuild_lock = None

        # 防击穿锁续期任务（P0 #50：长重建中周期续期，防锁超时被并发方抢走导致击穿）
        _rebuild_renew_task = None
        if _rebuild_lock:
            async def _renew_rebuild_loop():
                while True:
                    await asyncio.sleep(15)
                    try:
                        if not await SemanticCache.renew_rebuild_lock(req.query, _rebuild_lock, cache_ctx=_cache_ctx, ttl=45):
                            break  # 锁已失效/被他人持有，停止续期
                    except Exception:
                        break
            _rebuild_renew_task = asyncio.create_task(_renew_rebuild_loop())

        try:
            deepseek_api_key = await _get_deepseek_key()
            if not deepseek_api_key:
                logger.error("DeepSeek API Key 未设置，无法调用")
                yield sse("answer_complete", '服务配置不完整（API Key 缺失），请联系管理员。') + "data: [DONE]\n\n"
                return

            # 使用 MemoryManager 管理历史（conv_id/mm/画像已在入口提前创建）
            history_dicts = await mm.get_context(limit=SUMMARY_THRESHOLD)
            user_query = req.query

            # ===== 人格系统：动态加载 System Prompt =====
            from ..core.persona_manager import get_persona_manager
            pm = get_persona_manager()
            persona = pm.current
            persona_id = req.persona_id or pm.current_id

            # 允许请求中指定人格
            if req.persona_id and req.persona_id != pm.current_id:
                pm.switch(req.persona_id)
                persona = pm.current
                persona_id = req.persona_id

            today_str = datetime.now().strftime("%Y年%m月%d日 %A")
            if persona:
                system_content = _safe_format_prompt(
                    persona.system_prompt, today=today_str, name=persona.name
                )
                selected_model = persona.model or DEEPSEEK_MODEL
            else:
                system_content = f"你是AI助手。今天是{today_str}。"
                selected_model = DEEPSEEK_MODEL
            model_try_list = [selected_model]
            if DEEPSEEK_FLASH_MODEL != selected_model:
                model_try_list.append(DEEPSEEK_FLASH_MODEL)

            if req.user_location:
                asyncio.create_task(mm.save_user_location(req.user_location))

            shared_context = build_shared_context(user_query, req.user_location or "", user_profile or "")
            system_content += f"\n{shared_context}"

            # ===== 语言指令：控制 LLM 输出语言（英文界面时强制英文输出）=====
            system_content += lang_instr

            messages = [{"role": "system", "content": system_content}]

            # 注入上传文件的内容 — 带智能引用指令（统一走 build_file_context）
            file_context_str = await build_file_context(req)
            if file_context_str:
                messages.append({"role": "system", "content": file_context_str})

            history_dicts = compress_message_history(history_dicts, max_messages=_HISTORY_COMPACT_MSGS)
            for msg in history_dicts:
                messages.append(msg)

            # 【热缓存指令】告诉 AI 优先关注用户最新的消息，以前的消息仅供参考
            messages.append({
                "role": "system",
                "content": "注意：请以用户最新的消息为准。如果用户改变了目的地、预算、人数等计划，立即按新信息回答，不要沿用旧信息。"
            })

            if req.user_location:
                loc_name = req.user_location.replace("市", "")
                if loc_name not in user_query:
                    intent_keywords = ["天气", "酒店", "路线", "美食", "餐厅", "怎么去", "旅游"]
                    if any(kw in user_query for kw in intent_keywords):
                        user_query = f"{user_query}（我在{req.user_location}）"

            # 民法典人格：如果问题属于其他法律领域，给LLM一个提示
            if persona_id == "civil_code":
                _non_civil_keywords = {
                    "七天无理由退货": "【提示：此问题受《消费者权益保护法》调整，不属于民法典。请引用《消费者权益保护法》回答，不要引用民法典。】",
                    "退货": "【提示：退货问题受《消费者权益保护法》调整，请考虑适用该法。】",
                    "假货": "【提示：假货问题受《消费者权益保护法》调整（假一赔三），请考虑适用该法。】",
                    "被公司辞退": "【提示：劳动纠纷受《劳动合同法》调整，不属于民法典。】",
                    "工伤": "【提示：工伤问题受《工伤保险条例》调整，不属于民法典。】",
                }
                for kw, hint in _non_civil_keywords.items():
                    if kw in user_query:
                        user_query = hint + "\n" + user_query
                        break

            messages.append({"role": "user", "content": user_query})

            # ===== 意图路由：先分类用户意图，只加载需要的工具 → 更快更准 =====
            matched_agents, is_simple, is_recommend, tools = await _route_intent(user_query, persona_id)

            # ===== 法律依据纠正映射表检查（优先于LLM调用）=====
            # ⚠️ 仅对非强制白名单的查询检查映射表。强制白名单意味着路由裁决已确认为民法典问题，
            #    即使涉及交叉领域（如偷拍同时涉及民法典+治安处罚），也应回答民法典部分。
            _skip_law_mapping = False
            if persona_id == "civil_code":
                try:
                    from ..agents.routing_table import route_query as _rt_check2
                    _rt_result = _rt_check2(user_query)
                    # 仅明确判定为 forced_whitelist 才跳过映射表；其它/异常 → 保守走映射表检查（P1 #19）
                    _skip_law_mapping = bool(getattr(_rt_result, "match_type", "") == "forced_whitelist")
                    logger.info(f"⚖️ 法律映射表前置检查: match={getattr(_rt_result, 'match_type', 'N/A')}, skip={_skip_law_mapping}")
                except Exception as _rt_err:
                    logger.warning(f"法律映射表前置检查异常: {_rt_err}")
            
            if persona_id == "civil_code" and not _skip_law_mapping:
                try:
                    from ..agents.law_mapping import check_query as _check_law
                    _law_match = _check_law(user_query)
                    if _law_match:
                        logger.info(f"⚖️ 法律映射表命中 #{_law_match['id']}: {_law_match['scenario']} → {_law_match['law']}")
                        yield sse("answer_complete", _law_match['message']) + "data: [DONE]\n\n"
                        await _finalize_answer(mm, req.query, _law_match['message'], _cache_ctx, username, today)
                        saved_normally = True
                        logger.info(f"⚖️ 法律映射表已拦截，返回引导信息")
                        return
                except Exception as _le:
                    logger.warning(f"法律映射表检查失败（不影响正常流程）: {_le}")

            # =====================================================================
            # 简单任务快速通道：单Agent 或 纯LLM → 直接执行工具(如有) + 单次LLM格式化
            # 包括 search_knowledge（知识库查询也直接执行，不依赖LLM自行调用工具）
            # =====================================================================
            if (is_simple and len(matched_agents) == 1) or (len(matched_agents) == 0):
                is_pure_chat = len(matched_agents) == 0
                agent_name = matched_agents[0] if not is_pure_chat else "chat"
                logger.info(f"⚡ 简单任务快速通道: agent={agent_name}")

                yield sse("thought", '正在查询相关信息...' if not is_pure_chat else '正在思考...')

                try:
                    args = {}
                    tool_result = None
                    if not is_pure_chat:
                        # ---- 1. 直接执行工具（执行统一走 dispatch_tool）----
                        if agent_name == "query_weather":
                            # 优先从用户问题中提取城市名
                            city = ""
                            if not city:
                                from ..core.constants import CITIES
                                for c in CITIES:
                                    if c in user_query:
                                        city = c
                                        break
                            if not city:
                                city = req.user_location.replace("市", "") if req.user_location else ""
                            if not city:
                                city = user_query
                            args["city"] = city
                            yield sse("reasoning_chunk", f'正在查询 {city} 天气...')
                        elif agent_name == "query_hotel":
                            args["destination"] = req.user_location.replace("市", "") if req.user_location else user_query
                            nfo = f"正在搜索 {args['destination']} 酒店..."
                            yield sse("reasoning_chunk", nfo)
                        elif agent_name == "query_route":
                            args["departure"] = req.user_location or "当前位置"
                            args["destination"] = user_query
                            nfo = f"正在规划从 {args['departure']} 到 {args['destination']} 的路线..."
                            yield sse("reasoning_chunk", nfo)
                        elif agent_name == "query_food":
                            args["destination"] = req.user_location.replace("市", "") if req.user_location else user_query
                            nfo = f"正在搜索 {args['destination']} 美食..."
                            yield sse("reasoning_chunk", nfo)
                        elif agent_name == "search_knowledge":
                            args["query"] = _civil_mapped_query or user_query
                            logger.info(f"📤 search_knowledge 查询: {args['query'][:80]}... (原: {user_query[:30]}...)")
                            yield sse("reasoning_chunk", '正在查询知识库...')
                        elif agent_name == "search_project_knowledge":
                            args["query"] = user_query
                            logger.info(f"📤 search_project_knowledge 查询: {user_query[:80]}...")
                            yield sse("reasoning_chunk", '正在查询项目知识库...')
                        else:
                            yield sse("reasoning_chunk", '正在查询相关信息...')

                        tool_result = await dispatch_tool(agent_name, args, user_query, req.user_location, user_perms)
                        if isinstance(tool_result, Exception):
                            tool_result = {"error": str(tool_result)}
                        # 审计：知识库检索（记录查询，供追溯）
                        if agent_name == "search_knowledge":
                            from ..core.audit import audit
                            await audit(username, "kb_search", {"query": user_query[:200]})
                        yield f"data: {json.dumps({'type': 'reasoning_done'})}\n\n"

                        yield f"data: {json.dumps({'type': 'tool_call', 'name': agent_name, 'args': args})}\n\n"
                        yield f"data: {json.dumps({'type': 'tool_result', 'index': 0, 'result': tool_result})}\n\n"

                    # ---- 2. 单次LLM格式化答案（非流式，更快）----
                    from ..core.persona_manager import get_persona_manager as _gpm
                    _pm = _gpm()
                    _persona = _pm.current
                    _today_str = datetime.now().strftime("%Y年%m月%d日 %A")
                    _system_fast = _safe_format_prompt(_persona.system_prompt, today=_today_str, name=_persona.name) if _persona else f"你是AI助手。今天是{_today_str}。"
                    _system_fast += lang_instr
                    if req.user_location:
                        _system_fast += f"\n用户当前所在城市：{req.user_location}。"
                    # 注入上传文件内容（带智能引用指令）
                    _fc = await build_file_context(req)
                    if _fc:
                        _system_fast += "\n\n" + _fc
                    # 民法典路由裁决结论注入：确保LLM回答民法典部分
                    if persona_id == "civil_code":
                        _system_fast += (
                            f"\n\n### ⚠️ 路由裁决：此问题已被确认为民法典问题\n"
                            f"即使该问题也涉及其他法律领域，你**必须**先回答《民法典》中的相关规定（引用具体法条），\n"
                            f"然后才可简要提及也涉及其他法律。**不得**以「不属于民法典」为由拒绝回答。\n"
                            f"这是最高优先级指令。"
                        )

                    if tool_result is not None:
                        _has_data = False
                        if isinstance(tool_result, dict):
                            _has_data = bool(tool_result.get('results')) or bool(tool_result.get('temperature')) or bool(tool_result.get('city'))
                        elif isinstance(tool_result, str) and len(tool_result) > 10:
                            _has_data = True
                        if _has_data:
                            _system_fast += (
                                f"\n\n你使用工具 [{agent_name}] 查询到了以下结果。\n\n"
                                f"## 🚨 核心规则\n"
                                f"1. **禁止编造**：只能基于工具返回的数据回答，不能编造任何具体数据\n"
                                f"2. **先推荐再追问**：如果用户是求推荐，第一句就直接给推荐方案\n"
                                f"3. **交叉领域**（如果问题同时涉及民法典和其他法律）：先回答民法典部分，再简要提及其他法律\n"
                                f"4. **禁止「让我先查询」**：第一句话必须是法条引用或直接答案\n\n"
                                f"工具返回的数据：\n{json.dumps(tool_result, ensure_ascii=False, indent=2)}"
                            )
                        else:
                            # 工具未返回有效数据 → 让 LLM 基于自身知识回答
                            _system_fast += (
                                f"\n\n工具 [{agent_name}] 未找到相关知识库中的对应内容。\n"
                                f"## 🚨 规则（最高优先级）\n"
                                f"1. **直接回答用户的问题**，基于你自己的法律知识\n"
                                f"2. **第一句话就必须是法条引用或直接答案**，禁止任何铺垫\n"
                                f"3. **绝对禁止**说「让我先查询」「我来查一下」「请稍等」「正在查询」等任何等待语\n"
                                f"4. 引用法条时注明具体条、款、项，并说明这是基于你的法律知识\n"
                                f"5. 如果不确定某一具体法条编号，可以说明「根据民法典相关规定」\n"
                                f"6. 提示用户：回答仅供参考，不构成正式法律意见\n"
                                f"7. **如果问题涉及多个法律领域**（如同时涉及民法典和治安管理处罚法），先回答民法典部分，再简要提及其他法律"
                            )

                    _result_text = ""
                    # 尝试 persona 指定模型，超时则降级到 Flash（统一走 stream_llm）
                    _flash_used = False
                    for _model_try in model_try_list:
                        try:
                            _result_text = ""
                            _first_chunk_time = None
                            _last_chunk_time = None
                            _stream_buffer = ""
                            async for _ev in stream_llm(
                                deepseek_api_key, _model_try,
                                [{"role": "system", "content": _system_fast},
                                 {"role": "user", "content": user_query}],
                            ):
                                if _ev["type"] == "reasoning":
                                    # 元数据过滤：剥离泄露的系统提示词；按人格配置隐藏思考（P2 #24）
                                    if not _hide_reasoning(persona_id, persona):
                                        _rc = _sanitize_reasoning(_ev["text"])
                                        if _rc:
                                            yield sse("reasoning_chunk", _rc)
                                elif _ev["type"] == "content":
                                    _chunk = _ev["text"]
                                    _result_text += _chunk
                                    _stream_buffer += _chunk
                                    _now = __import__('time').time()
                                    # 首字到达立即输出
                                    if _first_chunk_time is None:
                                        _first_chunk_time = _now
                                        _last_chunk_time = _now
                                        yield sse("answer_chunk", _chunk)
                                        _stream_buffer = ""
                                    # 动态节流：40字符上限 或 每100ms推送（生成快时更快）
                                    elif len(_stream_buffer) >= 40 or (_now - _last_chunk_time >= 0.1 and _stream_buffer):
                                        yield sse("answer_chunk", _stream_buffer)
                                        _stream_buffer = ""
                                        _last_chunk_time = _now
                            # 刷出残余buffer
                            if _stream_buffer:
                                yield sse("answer_chunk", _stream_buffer)
                            if _result_text:
                                break  # 成功获取回复
                        except Exception as _fast_err:
                            await _mark_key_result(deepseek_api_key, False)
                            if _model_try == selected_model:
                                logger.warning(f"简单任务 {selected_model} 调用失败，降级到 {DEEPSEEK_FLASH_MODEL}: {_fast_err}")
                                _flash_used = True
                            else:
                                logger.warning(f"简单任务 {DEEPSEEK_FLASH_MODEL} 也失败: {_fast_err}")
                    
                    if not _result_text:
                        _result_text = DEEPSEEK_FALLBACK_MESSAGE
                        logger.warning(f"简单任务快速通道未返回有效回复: agent={agent_name}, models={model_try_list}")
                        await _mark_key_result(deepseek_api_key, False)
                        # 穿透防护：底层失败 → 写入短 TTL 空值占位（await 确保写入，P0 #11）
                        await SemanticCache.set_empty(req.query, cache_ctx=_cache_ctx, ttl=30)

                    # DFA 安全过滤
                    from ..core.safety_filter import get_filter as _get_sf
                    _sf = _get_sf()
                    if _sf.contains_sensitive(_result_text):
                        _result_text = _sf.safe_message

                    # ---- 3. 一次性输出完整答案（不流式分块，更快）----
                    yield sse("answer_complete", _result_text) + "data: [DONE]\n\n"

                    # ---- 4. 保存 ----
                    await _finalize_answer(mm, req.query, _result_text, _cache_ctx, username, today)
                    saved_normally = True
                    logger.info(f"✅ 简单任务快速通道完成: agent={agent_name}")
                    return

                except Exception as _fast_err:
                    logger.warning(f"简单任务快速通道失败，降级到流式模式: {_fast_err}")
                    # 降级：继续走下面的流式循环

            # =====================================================================
            # 推荐/多Agent快速通道：已知意图 → 直接并行执行所有工具 → 单次LLM格式化
            # 跳过"LLM决策调用哪些工具"步骤，省掉一轮流式调用
            # =====================================================================
            if is_recommend and len(matched_agents) >= 2:
                logger.info(f"推荐/多Agent快速通道: agents={matched_agents}")
                yield sse("thought", '正在查询多个信息，请稍候...')

                try:
                    # 1. 并行执行所有匹配的工具（create_task 真正并行；as_completed 逐个完成立即输出）
                    _tool_tasks = []
                    _tool_names = []
                    for _agent in matched_agents:
                        # 仅旅游四工具参与推荐通道（与原行为一致）；参数由 dispatch_tool 按上下文自动补全
                        if _agent in ("query_weather", "query_hotel", "query_route", "query_food"):
                            _tool_tasks.append(dispatch_tool(_agent, {}, user_query, req.user_location))
                            _tool_names.append(_agent)

                    # 关键：把协程包装成 Task 真正并行调度（否则 for-await 会串行执行 → 122s 灾难）
                    _tool_task_map = {}
                    for _name, _coro in zip(_tool_names, _tool_tasks):
                        _task = asyncio.create_task(_coro)
                        _tool_task_map[_task] = _name

                    # 逐个完成立即输出 tool_result（前端可实时看到进度；总耗时 = 最慢工具而非总和）
                    _tool_results = []
                    try:
                        for _done in asyncio.as_completed(_tool_task_map.keys(), timeout=TOOL_TIMEOUT):
                            _name = _tool_task_map[_done]
                            try:
                                _res = await _done
                            except Exception as _te:
                                _res = {"error": str(_te)}
                            if isinstance(_res, Exception):
                                _res = {"error": str(_res)}
                            _tool_results.append((_name, _res))
                            yield f"data: {json.dumps({'type': 'tool_call', 'name': _name, 'args': {}})}\n\n"
                            yield f"data: {json.dumps({'type': 'tool_result', 'index': 0, 'result': _res})}\n\n"
                    except TimeoutError:
                        # 超时后取消仍在运行的子任务（防僵尸协程占用 Redis/HTTP 连接池，P0 #9）
                        for _t in list(_tool_task_map):
                            if not _t.done():
                                _t.cancel()
                        # 个别工具超时：补 yield 超时结果，不阻塞整体
                        for _name in _tool_names:
                            if not any(n == _name for n, _ in _tool_results):
                                _tool_results.append((_name, {"error": "tool timeout"}))
                                yield f"data: {json.dumps({'type': 'tool_call', 'name': _name, 'args': {}})}\n\n"
                                yield f"data: {json.dumps({'type': 'tool_result', 'index': 0, 'result': {'error': 'tool timeout'}})}\n\n"

                    # 2. 单次LLM调用生成答案（非流式）
                    from ..core.persona_manager import get_persona_manager as _gpm
                    _pm = _gpm()
                    _persona = _pm.current
                    _today_str = datetime.now().strftime("%Y年%m月%d日 %A")
                    _sys = _safe_format_prompt(_persona.system_prompt, today=_today_str, name=_persona.name) if _persona else f"你是AI助手。今天是{_today_str}。"
                    _sys += lang_instr
                    if req.user_location:
                        _sys += f"\n用户当前所在城市：{req.user_location}。"
                    # 注入上传文件内容（带智能引用指令，统一走 build_file_context）
                    _fc = await build_file_context(req)
                    if _fc:
                        _sys += "\n\n" + _fc

                    _sys += "\n\n你查询到了以下信息，请整合成一份完整、自然、连贯的回答给用户。不要提及「专家」「查询工具」等词。\n\n"
                    for _name, _res in _tool_results:
                        if isinstance(_res, Exception):
                            _res = {"error": str(_res)}
                        _sys += f"--- {_name} ---\n{json.dumps(_res, ensure_ascii=False, indent=2)}\n\n"

                    _result_text = ""
                    for _model_try in model_try_list:
                        try:
                            _result_text = ""
                            _first_chunk_time = None
                            _last_chunk_time = None
                            _stream_buffer = ""
                            async for _ev in stream_llm(
                                deepseek_api_key, _model_try,
                                [{"role": "system", "content": _sys},
                                 {"role": "user", "content": user_query}],
                            ):
                                if _ev["type"] == "reasoning":
                                    # 元数据过滤：剥离泄露的系统提示词；按人格配置隐藏思考（P2 #24）
                                    if not _hide_reasoning(persona_id, persona):
                                        _rc = _sanitize_reasoning(_ev["text"])
                                        if _rc:
                                            yield sse("reasoning_chunk", _rc)
                                elif _ev["type"] == "content":
                                    _chunk = _ev["text"]
                                    _result_text += _chunk
                                    _stream_buffer += _chunk
                                    _now = __import__('time').time()
                                    if _first_chunk_time is None:
                                        _first_chunk_time = _now
                                        _last_chunk_time = _now
                                        yield sse("answer_chunk", _chunk)
                                        _stream_buffer = ""
                                    elif len(_stream_buffer) >= 40 or (_now - _last_chunk_time >= 0.1 and _stream_buffer):
                                        yield sse("answer_chunk", _stream_buffer)
                                        _stream_buffer = ""
                                        _last_chunk_time = _now
                            if _stream_buffer:
                                yield sse("answer_chunk", _stream_buffer)
                            if _result_text:
                                break
                        except Exception as _fast_err:
                            await _mark_key_result(deepseek_api_key, False)
                            if _model_try == selected_model:
                                logger.warning(f"多Agent {selected_model} 调用失败，降级到 {DEEPSEEK_FLASH_MODEL}: {_fast_err}")
                            else:
                                logger.warning(f"多Agent {DEEPSEEK_FLASH_MODEL} 也失败: {_fast_err}")

                    if not _result_text:
                        _result_text = DEEPSEEK_FALLBACK_MESSAGE
                        logger.warning(f"多Agent快速通道未返回有效回复: agents={matched_agents}, models={model_try_list}")
                        await _mark_key_result(deepseek_api_key, False)
                        # 穿透防护：底层失败 → 写入短 TTL 空值占位（await 确保写入，P0 #11）
                        await SemanticCache.set_empty(req.query, cache_ctx=_cache_ctx, ttl=30)

                    from ..core.safety_filter import get_filter as _get_sf
                    if _get_sf().contains_sensitive(_result_text):
                        _result_text = _get_sf().safe_message

                    # 3. 标记流式完成（内容已逐块通过 answer_chunk 发出）
                    yield sse("answer_complete", _result_text) + "data: [DONE]\n\n"

                    await _finalize_answer(mm, req.query, _result_text, _cache_ctx, username, today)
                    saved_normally = True
                    logger.info(f"✅ 多Agent快速通道完成: {len(_tool_names)}个工具")
                    return

                except Exception as _fast_err:
                    logger.warning(f"多Agent快速通道失败，降级到流式模式: {_fast_err}")
                    # 降级：继续走下面的流式循环

            max_steps = 3
            final_answer = ""

            yield sse("thought", '正在分析你的问题...')

            # 立即保存用户消息（防止刷新丢失）
            await mm.save_user_message(
                {"role": "user", "content": req.query}
            )
            _partial_saved = False  # 标记是否已保存部分助手回复

            for step in range(max_steps):
                # ----- 流式调用 DeepSeek API（真正流式）-----
                full_reasoning = ""
                full_content = ""
                has_tool_calls = False
                tool_calls_index = {}  # 用于合并同一 tool_call 的多个片段

                try:
                    # 统一走 stream_llm（分级超时在内部：连接 LLM_CONNECT_TIMEOUT + 读取 DEEPSEEK_API_TIMEOUT）
                    _stream_usage = {"prompt_tokens": 0, "completion_tokens": 0}
                    _ans_buf = ""
                    _ans_first_time = None
                    _ans_last_time = None
                    async for _ev in stream_llm(
                        deepseek_api_key, DEEPSEEK_MODEL, messages,
                        tools=tools, tool_choice="auto", username=username,
                        temperature=None, max_tokens=None,  # ReAct 路径保持模型默认
                    ):
                        if _ev["type"] == "usage":
                            _stream_usage["prompt_tokens"] = _ev["prompt_tokens"]
                            _stream_usage["completion_tokens"] = _ev["completion_tokens"]
                        elif _ev["type"] == "reasoning":
                            # 思考内容：实时流式发送（元数据过滤防泄露；按人格配置隐藏思考 P2 #24）
                            if not _hide_reasoning(persona_id, persona):
                                chunk = _sanitize_reasoning(_ev["text"])
                                if chunk:
                                    full_reasoning += chunk
                                    yield sse("reasoning_chunk", chunk)
                        elif _ev["type"] == "content":
                            # 回答内容：动态节流推送（首字立即，后续 100ms/40字符上限）
                            chunk = _ev["text"]
                            full_content += chunk
                            _ans_buf += chunk
                            _now = __import__('time').time()
                            if _ans_first_time is None:
                                _ans_first_time = _now
                                _ans_last_time = _now
                                yield sse("answer_chunk", chunk)
                                _ans_buf = ""
                            elif len(_ans_buf) >= 40 or (_now - _ans_last_time >= 0.1 and _ans_buf):
                                yield sse("answer_chunk", _ans_buf)
                                _ans_buf = ""
                                _ans_last_time = _now
                        elif _ev["type"] == "tool_calls":
                            # 工具调用：累积但不流式（需等待完整参数）
                            has_tool_calls = True
                            for tc in _ev["delta"]:
                                idx = tc.get("index")
                                if idx is not None:
                                    if idx not in tool_calls_index:
                                        tool_calls_index[idx] = {
                                            "id": tc.get("id", ""),
                                            "type": tc.get("type", "function"),
                                            "function": {"name": "", "arguments": ""}
                                        }
                                    if tc.get("id"):
                                        tool_calls_index[idx]["id"] = tc["id"]
                                    if tc.get("function"):
                                        if tc["function"].get("name"):
                                            tool_calls_index[idx]["function"]["name"] = tc["function"]["name"]
                                        if tc["function"].get("arguments"):
                                            tool_calls_index[idx]["function"]["arguments"] += tc["function"]["arguments"]

                    # ---- Token 精细计量 + 扣费（10元/万token，模拟模式）----
                    _record_token_usage(_stream_usage, username, conv_id)

                    # 思考结束信号（思考内容已实时发送）
                    if full_reasoning:
                        yield f"data: {json.dumps({'type': 'reasoning_done'})}\n\n"

                    # 工具调用处理
                    if has_tool_calls and tool_calls_index:
                        tool_calls = [
                            {
                                "id": v["id"],
                                "type": v["type"],
                                "function": {
                                    "name": v["function"]["name"],
                                    "arguments": v["function"]["arguments"]
                                }
                            }
                            for v in tool_calls_index.values()
                        ]
                        message = {
                            "role": "assistant",
                            "content": full_content,
                            "tool_calls": tool_calls
                        }
                        # 工具调用事件将在执行阶段统一发送（避免重复）
                    else:
                        # 纯文本回答：内容已逐块流式发送，此处仅做 DFA 检查 + 完成标记
                        final_safe = full_content or "抱歉，我暂时无法回答。"
                        # 刷出残余buffer
                        if _ans_buf and _ans_buf.strip():
                            yield sse("answer_chunk", _ans_buf)
                            _ans_buf = ""

                        # ===== DFA 敏感词过滤（最终检查，如有问题则替换已显示内容）=====
                        from ..core.safety_filter import get_filter
                        sf = get_filter()
                        if sf.contains_sensitive(final_safe):
                            logger.warning(f"DFA 拦截响应")
                            final_safe = sf.safe_message
                            # DFA 触发，覆盖已发送的块内容
                            yield sse("answer_chunk", final_safe)
                        # ============================
                        yield sse("answer_complete", final_safe) + "data: [DONE]\n\n"
                        partial_answer = final_safe
                        # 保存记忆（MemoryManager 双写 Redis + PG）
                        await _finalize_answer(mm, req.query, final_safe, _cache_ctx, username, today)
                        saved_normally = True
                        return

                except Exception as e:
                    logger.warning(f"DeepSeek API 调用失败 (流式): {e}")
                    await _mark_key_result(deepseek_api_key, False)
                    # 降级前先保存已生成的部分内容
                    if full_content:
                        partial_answer = full_content
                    async for chunk in fallback_chain(req, username, cache_ctx=_cache_ctx):
                        yield chunk
                    saved_normally = True
                    return

                tool_calls = message["tool_calls"]
                # 发送工具调用通知
                for tc in tool_calls:
                    yield f"data: {json.dumps({'type': 'tool_call', 'name': tc['function']['name'], 'args': json.loads(tc['function']['arguments'])})}\n\n"
                tasks = []
                for tc in tool_calls:
                    func_name = tc["function"]["name"]
                    args = json.loads(tc["function"]["arguments"])
                    tasks.append(dispatch_tool(func_name, args, req.query, req.user_location, user_perms))

                # 显式包装为 Task：wait_for 超时不会取消 gather 的子任务，需手动逐个 cancel（P0 #10）
                _tool_task_list = [asyncio.create_task(coro) for coro in tasks]
                try:
                    tool_results = await asyncio.wait_for(
                        asyncio.gather(*_tool_task_list, return_exceptions=True),
                        timeout=TOOL_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    # 超时后取消仍在运行的工具任务，避免僵尸协程占用连接池
                    for _t in _tool_task_list:
                        if not _t.done():
                            _t.cancel()
                    logger.warning("工具并行调用超时，触发降级")
                    # 不直接返回，而是给工具结果赋空值让循环继续，
                    # 让 LLM 基于已有知识回答（不依赖工具结果）
                    yield sse("reasoning_chunk", '知识库查询超时，正在基于已有知识继续回答...')
                    # 用空结果填充，让后续流程能继续
                    tool_results = []
                    for tc in tool_calls:
                        tool_results.append({"error": "工具查询超时", "fallback": True})

                for idx, result in enumerate(tool_results):
                    if isinstance(result, Exception):
                        result = {"error": str(result)}
                    yield f"data: {json.dumps({'type': 'tool_result', 'index': idx, 'result': result})}\n\n"

                # ===== 法律映射表命中 → 直接返回引导信息，不再调用LLM =====
                _law_mapping_hit = None
                for _res in tool_results:
                    if isinstance(_res, dict) and _res.get("mapping_hit"):
                        _law_mapping_hit = _res.get("message", "")
                        break
                if _law_mapping_hit:
                    logger.info(f"⚖️ 法律映射表命中，跳过LLM生成，直接返回引导信息")
                    yield sse("answer_complete", _law_mapping_hit) + "data: [DONE]\n\n"
                    await _finalize_answer(mm, req.query, _law_mapping_hit, _cache_ctx, username, today)
                    saved_normally = True
                    return

                # ----- 多 Agent 圆桌讨论（只用匹配到的 Agent）-----
                orchestrator = None
                try:
                    from ..agents.orchestrator import AgentOrchestrator
                    from ..agents.router import get_agent_names_for_orchestrator
                    orch = AgentOrchestrator(req.query, req.user_location or "")
                    # 只让匹配到的 Agent 参与讨论，不浪费未涉及的 Agent
                    agent_names = get_agent_names_for_orchestrator(matched_agents)
                    if not agent_names:
                        agent_names = ["query_weather", "query_hotel", "query_route", "query_food"]
                    for idx, tc in enumerate(tool_calls):
                        func_name = tc["function"]["name"]
                        result = tool_results[idx] if idx < len(tool_results) else {"error": "missing result"}
                        if isinstance(result, Exception):
                            result = {"error": str(result)}
                        if func_name in agent_names:
                            if isinstance(result, dict) and "error" in result:
                                orch.add_tool_result(func_name, {}, error=str(result["error"]))
                            else:
                                orch.add_tool_result(func_name, result)
                    if orch.get_involved_agents() and len(agent_names) > 1:
                        yield sse("thought", '专家们正在讨论分析...')
                        # Phase 1: 所有匹配到的 Agent 并行分析
                        discussion_summary = await orch.run(enable_phase2=False)
                        orchestrator = orch
                        logger.info(f"🧠 多Agent讨论完成（{len(agent_names)}个Agent参与）")
                    else:
                        # 单个 Agent 或无匹配 → 跳过讨论，直接进入下一步
                        discussion_summary = ""
                except Exception as orch_err:
                    logger.warning(f"多Agent讨论异常（降级为常规模式）: {orch_err}")
                    discussion_summary = ""

                # 将工具结果拼回消息
                tool_messages = []
                for idx, result in enumerate(tool_results):
                    if isinstance(result, Exception):
                        result = {"error": str(result)}
                    if isinstance(result, dict) and "error" in result:
                        error_msg = result["error"]
                        fallback_text = "获取数据失败了，可能服务暂时不可用。"
                        if "未找到" in error_msg:
                            fallback_text = "暂时没找到这个城市的数据，建议换个关键词试试～"
                        elif "超时" in error_msg:
                            fallback_text = "查询有点慢，可能网络问题，请稍后再试～"
                        elif "Key" in error_msg or "授权" in error_msg:
                            fallback_text = "服务配置正在更新，暂时无法使用，我试试其他方式帮你。"
                        result = {
                            "error": error_msg,
                            "fallback_message": fallback_text,
                            "success": False
                        }
                    tool_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_calls[idx]["id"],
                        "content": json.dumps(result, ensure_ascii=False)
                    })
                
                messages.append(message)
                messages.extend(tool_messages)

                # 注入多 Agent 讨论摘要（仅作为内部参考，禁止提及"专家""讨论"等词）
                if discussion_summary:
                    # 先发到思考区，让用户看到专家的分析过程
                    for line in discussion_summary.split('\n'):
                        if line.strip():
                            txt = line + "\n"
                            yield sse("reasoning_chunk", txt)
                    yield f"data: {json.dumps({'type': 'reasoning_done'})}\n\n"

                    messages.append({
                        "role": "system",
                        "content": (
                            "以下是基于工具查询结果的专业分析，已作为内部参考提供给你。\n"
                            "请**直接**使用这些信息来回答用户，形成一份完整、连贯、自然的回答。\n"
                            "🚨 **禁止在回答中提及**「专家」「天气专家说」「据讨论」「根据分析」等词汇。\n"
                            "不要引用任何专家意见，不要用「某某专家认为」的句式。\n"
                            "用你自己的口吻，把这些信息整合成一段流畅的建议。\n\n"
                            f"{discussion_summary}"
                        )
                    })

            # 循环结束未返回（max_steps 耗尽）
            if not saved_normally:
                # 尝试从最后一条消息获取已有内容
                last_content = ""
                for m in reversed(messages):
                    if m.get("role") == "assistant" and m.get("content"):
                        last_content = m["content"]
                        break
                if not last_content:
                    last_content = full_content or "抱歉，我暂时无法完成完整的回答。"
                logger.warning(f"V2 循环达到最大步数，返回已有内容: {len(last_content)} chars")
                yield sse("answer_complete", last_content) + "data: [DONE]\n\n"
                partial_answer = last_content
                # Bug #2：max_steps 截断是不完整回答，禁止写入语义缓存（write_cache=False），
                # 否则后续用户会命中残缺答案。
                await _finalize_answer(mm, req.query, last_content, _cache_ctx, username, today, write_cache=False)
                saved_normally = True

        except Exception as e:
            logger.error(f"V2 未知异常，降级备胎: {e}", exc_info=True)
            try:
                async for chunk in fallback_chain(req, username, cache_ctx=_cache_ctx):
                    yield chunk
            except Exception as fb_err:
                logger.error(f"降级也失败: {fb_err}")
                yield sse("answer_complete", '服务暂时不可用，请稍后再试。') + "data: [DONE]\n\n"
        finally:
            # 停止锁续期任务（防后台任务悬空）
            if _rebuild_renew_task:
                _rebuild_renew_task.cancel()
            # 防击穿：释放重建锁（仅持有者能释放；失败不影响主流程）
            if _rebuild_lock:
                try:
                    await SemanticCache.release_rebuild_lock(req.query, _rebuild_lock, cache_ctx=_cache_ctx)
                except Exception as _rel_err:
                    logger.warning(f"防击穿锁释放失败（TTL 自愈兜底）: {_rel_err}")
            # 用户隔离：释放并发槽位
            await release_concurrent(username)
            # 用户断开连接或异常时，保存已生成的部分内容（仅当正常路径未保存时）
            if not saved_normally and mm and partial_answer and partial_answer != "抱歉，我暂时无法回答。":
                try:
                    await mm.save_messages(
                        {"role": "user", "content": req.query},
                        {"role": "assistant", "content": partial_answer + "\n\n（内容不完整，连接已断开）"}
                    )
                    logger.info(f"💾 断线保存: conv_id={conv_id}, 内容长度={len(partial_answer)}")
                    # Bug #2：断线内容不完整，禁止写入语义缓存，否则会污染共享缓存。
                except Exception as save_err:
                    logger.warning(f"断线保存失败: {save_err}")

    return StreamingResponse(generate(), media_type="text/event-stream")


# ============================================================
# 任务制接口（替代流式，支持刷新恢复）
# ============================================================

from ..models.schemas import CreateTaskRequest
from ..core.task_manager import (
    create_task, get_task, update_status, is_cancelled,
    set_cancelled, cleanup_event, try_idempotent, save_idempotent,
    read_accumulated_result
)
from ..agents.runner import run_agent_task


# ---------- 后台任务并发上限（P1 #18：防恶意用户无限创建任务拖垮 LLM/DB）----------
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "50"))
_task_active_count = 0
_task_count_lock = asyncio.Lock()


async def _try_reserve_task_slot() -> bool:
    """尝试预留一个后台任务并发槽位；已满返回 False（调用方返回 429）"""
    global _task_active_count
    async with _task_count_lock:
        if _task_active_count >= MAX_CONCURRENT_TASKS:
            return False
        _task_active_count += 1
        return True


async def _release_task_slot():
    """回收后台任务并发槽位（任务结束或启动失败时调用）"""
    global _task_active_count
    async with _task_count_lock:
        _task_active_count = max(0, _task_active_count - 1)


async def _launch_agent_task(**kwargs):
    """启动后台 Agent 任务，任务结束后自动释放并发槽位（防止计数泄漏）"""
    async def _wrapped():
        try:
            await run_agent_task(**kwargs)
        finally:
            await _release_task_slot()
    try:
        asyncio.create_task(_wrapped())
    except Exception:
        # 调度失败（事件循环关闭等极少数场景）：回收槽位避免泄漏
        await _release_task_slot()
        raise


@router.post("/chat/tasks", status_code=201)
async def create_chat_task(
    req: CreateTaskRequest,
    current_user: dict = Depends(get_current_user)
):
    """创建 Agent 生成任务，立即返回 task_id"""
    username = current_user["username"]

    # GitHub 试用额度防御：任务端点也走 LLM，受限用户同样拦截（admin 豁免）
    if current_user.get("role") != "admin" and is_quota_exhausted(current_user):
        raise HTTPException(status_code=402, detail="免费额度已用完，请绑定手机号后继续使用")

    # 幂等：同一 username+session + 相同消息 10秒内复用（P0 #5：键含 username 防跨用户串号）
    existing = await try_idempotent(f"{username}:{req.session_id}", req.message)
    if existing:
        return {"task_id": existing, "idempotent": True}

    # 后台任务并发上限（P1 #18：防恶意用户无限创建任务）
    if not await _try_reserve_task_slot():
        raise HTTPException(status_code=429, detail="系统任务已满，请稍后再试")

    try:
        task_id = await create_task(req.session_id, req.message, req.user_location)
        # 在 task 存储中保留恢复所需字段（persona_id / file_ids），供 /resume 透传（P0 #6）
        _r = await get_redis()
        await _r.hset(f"task:{task_id}", mapping={
            "persona_id": req.persona_id or "",
            "file_ids": json.dumps(req.file_ids or []),
        })
        save_idempotent(f"{username}:{req.session_id}", req.message, task_id)

        # 启动后台任务（任务结束自动释放并发槽位）
        await _launch_agent_task(
            task_id=task_id,
            username=username,
            session_id=req.session_id,
            user_query=req.message,
            user_location=req.user_location,
            persona_id=req.persona_id,
            file_ids=req.file_ids,
        )
    except Exception:
        await _release_task_slot()
        raise

    return {"task_id": task_id, "idempotent": False}


@router.get("/chat/tasks/{task_id}/result", response_model=TaskResultResponse)
async def get_task_result(
    task_id: str,
    wait: int = 0,
    current_user: dict = Depends(get_current_user)
):
    """
    查询/等待任务结果。
    wait=0: 立即返回当前状态
    wait=1: 长轮询，最多等 60 秒
    """
    # 立即查一次
    task = await get_task(task_id)
    if not task:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "task not found"}, status_code=404)

    status = task["status"]

    # 已终结的状态直接返回（含 timeout，P2 C1）
    if status in ("completed", "cancelled", "error", "timeout"):
        result = await read_accumulated_result(task_id) or task.get("result", "")
        return {
            "status": status,
            "content": result,
            "session_id": task.get("session_id", ""),
        }

    # 长轮询（wait 参数生效，上限 60 秒，P0 #7）
    max_wait = min(wait, 60) if wait and wait > 0 else 0
    if max_wait and status in ("pending", "generating"):
        deadline = time.time() + max_wait
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            task = await get_task(task_id)
            if not task:
                return {"status": "error", "content": "task not found"}
            st = task["status"]
            if st in ("completed", "cancelled", "error", "timeout"):  # P2 C1：timeout 视为终态
                result = await read_accumulated_result(task_id) or task.get("result", "")
                return {"status": st, "content": result, "session_id": task.get("session_id", "")}
            # 还处于 generating，返回当前进度
            partial = await read_accumulated_result(task_id)
            if partial:
                return {"status": "generating", "content": partial, "session_id": task.get("session_id", "")}

    # 超时或仍在生成中，返回进度
    partial = await read_accumulated_result(task_id) or ""
    return {"status": status, "content": partial, "session_id": task.get("session_id", "")}


@router.post("/chat/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    current_user: dict = Depends(get_current_user)
):
    """取消生成任务"""
    task = await get_task(task_id)
    if not task:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "task not found"}, status_code=404)

    if task["status"] in ("completed", "cancelled", "error"):
        return {"status": task["status"], "message": "任务已终结，无需取消"}

    # 设置取消信号
    set_cancelled(task_id)
    partial = await read_accumulated_result(task_id)
    await update_status(task_id, "cancelled", partial)

    return {"status": "cancelled", "message": "已取消"}


@router.post("/chat/tasks/{task_id}/resume")
async def resume_task(
    task_id: str,
    current_user: dict = Depends(get_current_user)
):
    """重新生成（创建新任务，丢弃旧草稿）"""
    task = await get_task(task_id)
    if not task:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "task not found"}, status_code=404)

    if task["status"] != "cancelled":
        return {"error": "只有已取消的任务才能恢复", "status": task["status"]}

    # 创建新任务，复用原参数（含恢复 persona_id / file_ids，P0 #6）
    username = current_user["username"]
    # 后台任务并发上限（P1 #18）
    if not await _try_reserve_task_slot():
        raise HTTPException(status_code=429, detail="系统任务已满，请稍后再试")

    try:
        new_task_id = await create_task(
            task["session_id"],
            task["user_message"],
            task.get("user_location", "")
        )
        # 从原任务透传恢复字段（兼容旧任务未存字段的情况）
        persona_id = task.get("persona_id") or ""
        file_ids_raw = task.get("file_ids") or ""
        file_ids = json.loads(file_ids_raw) if file_ids_raw else []

        await _launch_agent_task(
            task_id=new_task_id,
            username=username,
            session_id=task["session_id"],
            user_query=task["user_message"],
            user_location=task.get("user_location", ""),
            persona_id=persona_id,
            file_ids=file_ids,
        )
    except Exception:
        await _release_task_slot()
        raise

    return {"task_id": new_task_id, "previous_task_id": task_id}


