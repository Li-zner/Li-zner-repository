"""文件上传与读取：分块落盘 / 按扩展名解析文本 / Redis 存储 / IDOR 越权防护

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
"""
import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from ..core.logging import setup_logging
from ..core.redis import get_redis

logger = setup_logging()

# ---------- 文件上传 ----------
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
    """异步提取文本文件内容（UTF-8 优先，GBK 兜底；超长截断防撑爆 token）"""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"不支持的文件类型: {ext}，仅支持文本类文件")
    loop = asyncio.get_running_loop()

    def _read_file() -> str:
        """同步读文件（UTF-8 优先，GBK 兜底），放到线程池避免阻塞事件循环"""
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
        except FileNotFoundError:  # noqa: silent-except 豁免：并发清理竞态为预期路径
            pass
        raise HTTPException(status_code=400, detail=str(e))


async def handle_upload(file, username: str) -> dict:
    """上传文件（支持 TXT/PDF/图片/Word 等），返回文件ID与提取的文本

    路由层仅参数解析后调用本函数；业务错误抛 HTTPException（由路由层转响应）。
    """
    safe_filename = os.path.basename(file.filename or "")
    if not safe_filename:
        raise HTTPException(status_code=400, detail="文件名无效")

    ext = os.path.splitext(safe_filename)[1].lower()
    if not ext:
        raise HTTPException(status_code=400, detail="文件缺少扩展名")
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型 '{ext}'")

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
    except FileNotFoundError:  # noqa: silent-except 豁免：并发清理竞态为预期路径
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
        "uploaded_by": username,
        "uploaded_at": datetime.now().isoformat(),
        "text_length": len(text_content),
        "parse_note": parse_note,
    }
    await r.setex(f"file:{file_id}:content", 3600, text_content)
    await r.setex(f"file:{file_id}:meta", 3600, json.dumps(file_meta))

    logger.info(f"文件上传成功: user={username}, file={safe_filename}, "
                f"type={ext}, size={size}, text_len={len(text_content)}, note={parse_note}")

    return {
        **file_meta,
        "text_preview": text_content[:2000] + ("..." if len(text_content) > 2000 else ""),
        "text_content": text_content[:50000],
    }


async def get_user_file(file_id: str, current_user: dict) -> dict:
    """获取已上传文件的信息和内容（仅上传者/admin 可读，IDOR 越权防护）"""
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
    return {**meta, "text_content": content or ""}
