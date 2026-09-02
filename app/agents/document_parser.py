"""
文档解析器 — 提取 PDF / 图片 / Word / TXT 中的文本内容
供 Agent 进行语义理解和推荐/规划
"""
import os
import io
import json
import zipfile
import asyncio
import threading
from pathlib import Path
from typing import Optional
from ..core.logging import setup_logging

logger = setup_logging()

# OCR 引擎串行化锁：RapidOCR 内部 ONNX Runtime 不支持并发调用（P1 #43）
_OCR_LOCK = threading.Lock()

# ============================================================
# 图片 OCR 引擎（RapidOCR 优先，Tesseract 降级）
# ============================================================
# 图片 OCR 引擎
# ============================================================
# PIL 必须优先导入（被 RapidOCR / Tesseract / _ocr_image_bytes 共用）
try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

# RapidOCR（深度学习，中文优秀，基于 ONNX Runtime）
_HAS_RAPID = False
if _HAS_PIL:
    try:
        from rapidocr_onnxruntime import RapidOCR
        _RAPID_ENGINE = RapidOCR()
        _HAS_RAPID = True
        logger.info("RapidOCR 引擎已就绪")
    except Exception as e:
        logger.warning(f"RapidOCR 加载失败（将使用 Tesseract 降级）: {e}")

# Tesseract（传统 OCR，作为降级）
_HAS_TESSERACT = False
if _HAS_PIL:
    try:
        import pytesseract
        _HAS_TESSERACT = True
    except ImportError:
        pass

def _ocr_image(img: Image.Image, label: str = "") -> str:
    """对 PIL Image 执行 OCR，RapidOCR 优先"""
    if not _HAS_RAPID and not _HAS_TESSERACT:
        return ""
    try:
        with _OCR_LOCK:  # RapidOCR/ONNX 不支持并发，串行化（P1 #43）
            if _HAS_RAPID:
                # RapidOCR：直接处理 PIL Image
                result, elapse = _RAPID_ENGINE(img)
                if result:
                    texts = []
                    for box, text, conf in result:
                        t = text.strip()
                        if t:
                            texts.append(t)
                    if texts:
                        combined = " ".join(texts)
                        return f"\n[图片 OCR{ ' ('+label+')' if label else '' }]: {combined}"
                return ""
            else:
                # Tesseract 降级
                if img.mode != 'L':
                    img = img.convert('L')
                from PIL import ImageEnhance, ImageFilter
                enhancer = ImageEnhance.Contrast(img)
                img = enhancer.enhance(1.5)
                text = pytesseract.image_to_string(img, lang='chi_sim+eng', config='--psm 6')
                result = text.strip()
                if result:
                    return f"\n[图片 OCR{ ' ('+label+')' if label else '' }]: {result}"
                return ""
    except Exception as e:
        logger.warning(f"OCR 失败 {label}: {e}")
        return ""

def _ocr_image_bytes(img_bytes: bytes, ext: str = "png", label: str = "") -> str:
    """对图片字节执行 OCR"""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        return _ocr_image(img, label)
    except Exception as e:
        logger.warning(f"图片字节解析失败 {label}: {e}")
        return ""


# ============================================================
# PDF 解析（PyMuPDF）— 文本 + 内嵌图片 OCR
# ============================================================
try:
    import fitz  # PyMuPDF
    _HAS_PDF = True
except ImportError:
    _HAS_PDF = False
    logger.warning("PyMuPDF 未安装，PDF 解析不可用")

async def parse_pdf(filepath: str) -> str:
    """解析 PDF：先提取文本，再提取内嵌图片进行 OCR，合并返回"""
    if not _HAS_PDF:
        return "[PDF 解析引擎未安装]"
    loop = asyncio.get_running_loop()

    def _extract_all():
        text_parts = []
        with fitz.open(filepath) as doc:
            for page_num, page in enumerate(doc):
                # 1. 提取文本
                text = page.get_text()
                if text.strip():
                    text_parts.append(f"--- 第 {page_num+1} 页 ---\n{text}")

                # 2. 提取本页内嵌图片做 OCR
                images_on_page = page.get_images(full=True)
                for img_idx, img_ref in enumerate(images_on_page):
                    xref = img_ref[0]  # 图片引用编号
                    try:
                        img_dict = doc.extract_image(xref)
                        img_bytes = img_dict["image"]
                        pil_img = Image.open(io.BytesIO(img_bytes))
                        ocr_text = _ocr_image(pil_img, f"第{page_num+1}页-图{img_idx+1}")
                        if ocr_text and ocr_text.strip():
                            text_parts.append(ocr_text)
                    except Exception:
                        pass

        return "\n\n".join(text_parts) if text_parts else ""

    try:
        text = await loop.run_in_executor(None, _extract_all)
        if not text.strip():
            # 纯扫描件 → 全页图片 OCR
            logger.info("PDF 无直接文本且无内嵌图片，尝试全页 OCR...")
            text = await parse_image(filepath, dpi=300)
        return text[:150000]  # 放宽到 15 万字
    except Exception as e:
        logger.warning(f"PDF 解析失败: {e}")
        return f"[PDF 解析错误: {e}]"


# ============================================================
# 图片 OCR（pytesseract）— 独立图片文件
# （pytesseract/PIL 已在顶部导入，_HAS_TESSERACT 可用）
async def parse_image(filepath: str, dpi: int = 200) -> str:
    """对独立图片文件进行 OCR（RapidOCR 优先）"""
    if not _HAS_RAPID and not _HAS_TESSERACT:
        return "[OCR 引擎未安装]"
    loop = asyncio.get_running_loop()
    def _ocr():
        try:
            if _HAS_RAPID:
                result, elapse = _RAPID_ENGINE(filepath)
                if result:
                    texts = [f"[{conf:.2f}] {text}" for box, text, conf in result if text.strip()]
                    return "\n".join(texts) if texts else "[图片中未识别到文字]"
                return "[图片中未识别到文字]"
            else:
                from PIL import Image, ImageEnhance, ImageFilter
                with Image.open(filepath) as raw_img:  # P1 #6：显式关闭文件句柄
                    if raw_img.mode != 'L':
                        img = raw_img.convert('L')
                    else:
                        img = raw_img
                    enhancer = ImageEnhance.Contrast(img)
                    img = enhancer.enhance(1.5)
                    text = pytesseract.image_to_string(img, lang='chi_sim+eng', config='--psm 6')
                return text.strip() or "[图片中未识别到文字]"
        except Exception as e:
            return f"[OCR 识别失败: {e}]"
    try:
        text = await loop.run_in_executor(None, _ocr)
        return text[:50000]
    except Exception as e:
        return f"[图片解析错误: {e}]"


# ============================================================
# Word 解析（python-docx）— 文本 + 内嵌图片 OCR
# ============================================================
try:
    from docx import Document as DocxDocument
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    _HAS_DOCX = True
except ImportError:
    _HAS_DOCX = False
    logger.warning("python-docx 未安装，Word 解析不可用")

async def parse_docx(filepath: str) -> str:
    """解析 .docx：先提取文本/表格，再提取内嵌图片进行 OCR，合并返回"""
    if not _HAS_DOCX:
        return "[Word 解析引擎未安装]"
    loop = asyncio.get_running_loop()

    def _extract_all():
        doc = DocxDocument(filepath)
        parts = []

        # 1. 提取段落文本
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        if paragraphs:
            parts.append("\n".join(paragraphs))

        # 2. 提取表格文本
        tables_text = []
        for table in doc.tables:
            for row in table.rows:
                row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if row_text:
                    tables_text.append(" | ".join(row_text))
        if tables_text:
            parts.append("【表格内容】\n" + "\n".join(tables_text))

        # 3. 提取内嵌图片做 OCR
        # .docx 本质是 ZIP，图片在 word/media/ 下
        ocr_results = []
        try:
            with zipfile.ZipFile(filepath, 'r') as z:
                # Zip Slip 防护（P0 #4）：仅读取 word/media/ 下无路径穿越的成员
                media_files = [
                    f for f in z.namelist()
                    if f.startswith('word/media/')
                    and not f.startswith('/')
                    and '..' not in os.path.normpath(f).split(os.sep)
                ]
                for idx, media_path in enumerate(sorted(media_files)):
                    img_bytes = z.read(media_path)
                    ext = os.path.splitext(media_path)[1].lstrip('.')
                    label = f"文档插图{idx+1}"
                    ocr_text = _ocr_image_bytes(img_bytes, ext, label)
                    if ocr_text and ocr_text.strip():
                        ocr_results.append(ocr_text)
        except Exception as e:
            logger.warning(f"Word 图片提取失败: {e}")

        if ocr_results:
            parts.append("【文档内嵌图片文字】\n" + "\n".join(ocr_results))

        return "\n\n".join(parts) if parts else ""

    try:
        text = await loop.run_in_executor(None, _extract_all)
        return text[:150000]
    except Exception as e:
        logger.warning(f"Word 解析失败: {e}")
        return f"[Word 解析错误: {e}]"


# ============================================================
# TXT 解析（直接读取）
# ============================================================
async def parse_txt(filepath: str, encoding: str = None) -> str:
    """解析纯文本文件"""
    loop = asyncio.get_running_loop()
    def _read():
        encodings = [encoding, 'utf-8', 'gbk', 'gb2312', 'utf-16'] if not encoding else [encoding]
        for enc in encodings:
            if not enc:
                continue
            try:
                with open(filepath, 'r', encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        return "[无法解码文件内容]"
    try:
        text = await loop.run_in_executor(None, _read)
        return text[:100000]
    except Exception as e:
        return f"[文本解析错误: {e}]"


# ============================================================
# 统一入口
# ============================================================
SUPPORTED_EXTENSIONS = {
    '.pdf':  'PDF 文档',
    '.jpg':  'JPEG 图片',
    '.jpeg': 'JPEG 图片',
    '.png':  'PNG 图片',
    '.bmp':  'BMP 图片',
    '.webp': 'WebP 图片',
    '.docx': 'Word 文档',
    '.doc':  'Word 文档（旧格式，需先转 .docx）',
    '.txt':  '纯文本',
    '.md':   'Markdown',
    '.csv':  'CSV 表格',
    '.json': 'JSON 数据',
    '.xml':  'XML 文档',
}

async def parse_document(filepath: str, filename: str) -> dict:
    """
    统一文档解析入口
    返回: {"text": str, "format": str, "pages": int, "success": bool}
    """
    ext = os.path.splitext(filename)[1].lower()
    logger.info(f"解析文档: {filename} (ext={ext})")

    if ext == '.pdf':
        text = await parse_pdf(filepath)
    elif ext in ('.jpg', '.jpeg', '.png', '.bmp', '.webp'):
        text = await parse_image(filepath)
    elif ext == '.docx':
        text = await parse_docx(filepath)
    elif ext in ('.txt', '.md', '.csv', '.json', '.xml'):
        text = await parse_txt(filepath)
    else:
        return {"text": "", "format": ext, "success": False, "error": f"不支持的文件格式: {ext}"}

    success = bool(text.strip()) and not text.startswith("[")
    return {
        "text": text,
        "format": SUPPORTED_EXTENSIONS.get(ext, ext),
        "success": success,
        "error": None if success else text
    }
