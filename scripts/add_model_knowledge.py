# -*- coding: utf-8 -*-
"""在 黎忠南简历-最终版.docx 中写入大模型经验与模型认知技术栈。
- 个人简介末尾追加一句（保持 9.5pt 正文）
- 核心优势末尾新增「模型认知」一条（9.5pt，•前缀 + 加粗标签）
- 技术栈末尾新增「模型认知」一行（9.5pt）
- 技术栈之后新增「大模型认知与实操」章节（标题 12.5pt bold + 4 条 9.5pt）
保留与现有条目一致的格式：字号、字体、• 前缀。
"""
from docx import Document
from docx.shared import Pt
from docx.oxml.ns import qn

SRC = r"d:\桌面\agent_gateway\黎忠南简历-最终版.docx"
doc = Document(SRC)


def set_cn(run, name="微软雅黑"):
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)


def make_bullet(text, label=None, size=9.5):
    """创建一个条目段落：label 加粗 + 正文（不加粗），与现有条目格式一致。"""
    p = doc.add_paragraph()
    try:
        p.style = doc.styles["Body Text"]
    except Exception:
        pass
    if label:
        r0 = p.add_run("•" + label)
        r0.bold = True
        r0.font.size = Pt(size)
        set_cn(r0)
    r = p.add_run(text)
    r.bold = False  # 正文不加粗，与现有条目一致
    r.font.size = Pt(size)
    set_cn(r)
    p.paragraph_format.space_after = Pt(2)
    return p  # 返回段落对象


def make_heading(text):
    """章节标题：与现有 12.5pt bold 标题一致。"""
    p = doc.add_paragraph()
    try:
        p.style = doc.styles["Body Text"]
    except Exception:
        pass
    r = p.add_run(text)
    r.bold = True
    r.font.size = Pt(12.5)
    set_cn(r)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(3)
    return p


def find_para(frag):
    for p in doc.paragraphs:
        if frag in p.text:
            return p
    return None


# ============ 1. 个人简介末尾追加大模型认知 ============
intro = find_para("独立设计并交付一套 Multi-Agent 网关")
if intro:
    add = (
        "在大模型侧具备从量化部署到微调训练的实操认知：本地完成过量化模型部署"
        "（Ollama/GGUF），在阿里云百炼平台用自有数据集完成过微调训练，理解微调与 RAG 的适用边界。"
    )
    if "大模型侧" not in intro.text:
        # 追加到现有段落末尾（利用最后一个 run）
        if intro.runs:
            intro.runs[-1].text += add
        else:
            r = intro.add_run(add)
            r.font.size = Pt(9.5)
            set_cn(r)
        print("✓ 个人简介已追加大模型认知")
    else:
        print("= 个人简介已包含大模型内容，跳过")

# ============ 2. 核心优势末尾新增「模型认知」 ============
adv = find_para("可观测与排障")
if adv:
    # 在「可观测与排障」条目之后插入「模型认知」条目
    newp = make_bullet(
        "系统学习大模型微调、量化、部署：本地实操量化模型部署（Ollama/GGUF），"
        "在阿里云百炼平台用自有数据集完成微调训练，能清晰阐述微调与 RAG 的适用边界与组合策略。",
        label="模型认知：",
    )
    adv._p.addnext(newp._p)
    print("✓ 核心优势新增「模型认知」")

# ============ 3. 技术栈末尾新增「模型认知」行 ============
stack = find_para("Kubernetes 基础概念")
if stack:
    newp = make_bullet(
        "大模型微调与部署：阿里云百炼/PAI 微调流程（数据集准备→训练→评估）、量化部署"
        "（Ollama/GGUF，实测 Qwen2.5B/3.5B、DeepSeek-7B）、RAG 与微调适用边界。",
        label="模型认知：",
    )
    stack._p.addnext(newp._p)
    print("✓ 技术栈新增「模型认知」行")

# ============ 4. 技术栈之后新增「大模型认知与实操」章节 ============
stack_head = find_para("技术栈")
if stack_head:
    # 找到技术栈最后一条（模型认知行）作为锚点
    anchor = find_para("RAG 与微调适用边界")
    if anchor is None:
        anchor = stack_head
    # 章节标题
    h = make_heading("大模型认知与实操")
    anchor._p.addnext(h._p)
    prev = h
    items = [
        ("微调实操：",
         "在阿里云百炼平台使用自有数据集完成过模型微调训练（数据集构建→训练任务→效果评估全流程，"
         "部署环节未实操），理解 LoRA 等参数高效微调思路，掌握过拟合、学习率、loss 收敛等训练指标的基础概念。"),
        ("量化部署：",
         "本地部署量化模型：Ollama + GGUF 量化格式，实测运行 Qwen 2.5B / 3.5B 与 DeepSeek 7B，"
         "理解量化精度（如 Q4_K_M）对显存占用、推理速度与生成质量的取舍。"),
        ("技术边界：",
         "能结合业务场景决策：知识时效性与私有数据优先 RAG，领域风格与特定能力优先微调，"
         "复杂场景两者组合（如知识库检索 + 领域术语风格输出）。"),
        ("持续学习：",
         "备考阿里云大模型 ACP 认证，系统学习了大模型原理、提示工程、微调、RAG、Agent 等核心知识体系。"),
    ]
    for label, text in items:
        b = make_bullet(text, label=label)
        prev._p.addnext(b._p)
        prev = b
    print("✓ 新增「大模型认知与实操」章节（4 条）")

doc.save(SRC)
print("saved:", SRC)
