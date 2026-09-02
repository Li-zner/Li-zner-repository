import sys
import fitz  # PyMuPDF

path = r"d:\桌面\agent_gateway\黎忠南简历.pdf"
doc = fitz.open(path)
for i, page in enumerate(doc):
    print(f"===== 第 {i+1} 页 =====")
    print(page.get_text())
doc.close()
