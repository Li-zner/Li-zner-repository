"""
文档解析器 — 提取 PDF / 图片 / Word / TXT 中的文本内容
供 Agent 进行语义理解和推荐/规划
"""
import os
import io
import time
import zipfile
import asyncio
import threading
from ..core.logging import setup_logging

logger = setup_logging()

# OCR 引擎串行化锁：RapidOCR 内部 ONNX Runtime 不支持并发调用（P1 #43）
_OCR_LOCK = threading.Lock()

# ---------- 解析资源上限（2026-09-07 审查 P1）----------
# 20MB 大小限制约束不了页数/内嵌图数：1MB PDF 可含上万页/图，每页渲染 150dpi+
# OCR（全局锁串行）→ 单个恶意上传可占住 OCR 通道小时级并占满执行线程池。
# 超限截断并注明，不整体失败——正常文档远低于此阈值。
_MAX_PDF_PAGES = 200        # PDF 最大处理页数
_MAX_OCR_IMAGES = 50        # 单文档最大 OCR 图片数（内嵌图/整页渲染共用）
_PARSE_DEADLINE_S = 60.0    # 单文档解析 wall-clock 预算
# ponytail: deadline 是协作式检查（页/图粒度），单次 OCR 无法抢占，
# 实际耗时最多超出预算一次单页 OCR；如需硬中断改子进程隔离。

# ============================================================
# 图片 OCR 引擎（RapidOCR 优先，Tesseract 降级）
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
        # 可选依赖：未安装则保持 False，OCR 走 RapidOCR 或返回引擎未安装提示
        _HAS_TESSERACT = False

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

def _truncation_note(pages: int, images: int, seconds: float, reason: str) -> str:
    """截断标注（拼在文本尾部，让上层与用户知道内容不完整）"""
    return (f"\n\n[文档过大已截断：处理了 {pages} 页 / {images} 张图 / {seconds:.0f} 秒，"
            f"原因：{reason}；其余内容未解析]")


def _render_pages_ocr(filepath: str) -> str:
    """整页渲染 + 逐页 OCR（纯扫描件兜底）。

    旧实现把 PDF 路径直接丢给图片 OCR 引擎（RapidOCR/Tesseract 都打不开 PDF），必然失败；
    这里用 fitz 把每页渲染成位图再走统一的 OCR 通道。
    受页数上限与 deadline 约束（2026-09-07 审查 P1）。
    """
    parts = []
    started = time.monotonic()
    with fitz.open(filepath) as doc:
        for page_num, page in enumerate(doc):
            if page_num + 1 > _MAX_PDF_PAGES:
                parts.append(_truncation_note(_MAX_PDF_PAGES, 0, time.monotonic() - started, "页数超限"))
                break
            if time.monotonic() - started > _PARSE_DEADLINE_S:
                parts.append(_truncation_note(page_num, 0, time.monotonic() - started, "解析超时"))
                break
            pix = page.get_pixmap(dpi=150)
            ocr_text = _ocr_image_bytes(pix.tobytes("png"), "png", f"第{page_num+1}页")
            if ocr_text and ocr_text.strip():
                parts.append(ocr_text)
    return "\n".join(parts)


async def parse_pdf(filepath: str) -> str:
    """解析 PDF：先提取文本，再提取内嵌图片进行 OCR，合并返回"""
    if not _HAS_PDF:
        return "[PDF 解析引擎未安装]"
    loop = asyncio.get_running_loop()

    def _extract_all():
        text_parts = []
        started = time.monotonic()
        page_count = 0
        img_count = 0
        stop_reason = ""
        with fitz.open(filepath) as doc:
            for page_num, page in enumerate(doc):
                page_count += 1
                if page_count > _MAX_PDF_PAGES:
                    stop_reason = "页数超限"
                    page_count = _MAX_PDF_PAGES
                    break
                if time.monotonic() - started > _PARSE_DEADLINE_S:
                    stop_reason = "解析超时"
                    break
                # 1. 提取文本
                text = page.get_text()
                if text.strip():
                    text_parts.append(f"--- 第 {page_num+1} 页 ---\n{text}")

                # 2. 提取本页内嵌图片做 OCR（受图片总数上限约束）
                images_on_page = page.get_images(full=True)
                for img_idx, img_ref in enumerate(images_on_page):
                    img_count += 1
                    if img_count > _MAX_OCR_IMAGES:
                        stop_reason = "内嵌图片数超限"
                        break
                    xref = img_ref[0]  # 图片引用编号
                    try:
                        img_dict = doc.extract_image(xref)
                        img_bytes = img_dict["image"]
                        pil_img = Image.open(io.BytesIO(img_bytes))
                        ocr_text = _ocr_image(pil_img, f"第{page_num+1}页-图{img_idx+1}")
                        if ocr_text and ocr_text.strip():
                            text_parts.append(ocr_text)
                    except Exception as img_err:
                        # 单张内嵌图片损坏不该废掉整份文档，跳过并留排查日志（P2 修复：不再裸 pass）
                        logger.debug(f"跳过无法解析的内嵌图片(第{page_num+1}页-图{img_idx+1}): {img_err}")
                if img_count > _MAX_OCR_IMAGES:
                    break

        if stop_reason:
            # 超限截断如实标注（内容不完整必须让调用方知道，2026-09-07 审查 P1）
            text_parts.append(_truncation_note(
                page_count, min(img_count, _MAX_OCR_IMAGES),
                time.monotonic() - started, stop_reason))
        return "\n\n".join(text_parts) if text_parts else ""

    try:
        text = await loop.run_in_executor(None, _extract_all)
        if not text.strip():
            # 纯扫描件（无文本层且无内嵌图片）→ fitz 整页渲染后 OCR
            logger.info("PDF 无直接文本且无内嵌图片，尝试整页渲染 OCR...")
            text = await loop.run_in_executor(None, _render_pages_ocr, filepath)
        return text[:150000]  # 放宽到 15 万字
    except Exception as e:
        logger.warning(f"PDF 解析失败: {e}")
        return f"[PDF 解析错误: {e}]"


# ============================================================
# 图片 OCR（pytesseract）— 独立图片文件
# （pytesseract/PIL 已在顶部导入，_HAS_TESSERACT 可用）
async def parse_image(filepath: str) -> str:
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

        # 3. 提取内嵌图片做 OCR（受图片数上限与 deadline 约束，2026-09-07 审查 P1）
        # .docx 本质是 ZIP，图片在 word/media/ 下
        ocr_results = []
        started = time.monotonic()
        try:
            with zipfile.ZipFile(filepath, 'r') as z:
                # Zip Slip 防护（P0 #4）：仅读取 word/media/ 下无路径穿越的成员。
                # 关键：必须用 RAW 成员名判断 — os.path.normpath 会把 'word/media/../X'
                # 折叠成 'word/X' 从而绕过 '..' 检查。ZIP 成员名恒用 '/' 分隔，按 '/' 切分即可。
                media_files = [
                    f for f in z.namelist()
                    if f.startswith('word/media/')
                    and not f.startswith('/')
                    and '..' not in f.split('/')
                ]
                for idx, media_path in enumerate(sorted(media_files)):
                    if idx + 1 > _MAX_OCR_IMAGES:
                        ocr_results.append(f"[截断：内嵌图片超过 {_MAX_OCR_IMAGES} 张，其余未解析]")
                        break
                    if time.monotonic() - started > _PARSE_DEADLINE_S:
                        ocr_results.append("[截断：图片 OCR 超时，其余未解析]")
                        break
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

    # 错误判定用显式标记而非 startswith("[")：正文以 "[" 开头的正常文档（如
    # JSON 数组导出、引用块）曾被误判为解析失败
    _ERROR_MARKERS = (
        "[PDF 解析错误", "[PDF 解析引擎未安装]",
        "[Word 解析错误", "[Word 解析引擎未安装]",
        "[图片解析错误", "[OCR 引擎未安装]", "[OCR 识别失败",
        "[文本解析错误", "[无法解码文件内容]",
    )
    is_error = any(text.startswith(m) for m in _ERROR_MARKERS)
    success = bool(text.strip()) and not is_error
    return {
        "text": text,
        "format": SUPPORTED_EXTENSIONS.get(ext, ext),
        "success": success,
        "error": text if is_error else None
    }
