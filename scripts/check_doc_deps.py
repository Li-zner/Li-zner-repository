"""验证 Docker 镜像中的文档解析依赖"""
import subprocess, sys

tests = [
    ("PyMuPDF (PDF)", "import fitz; print('PyMuPDF OK:', fitz.__version__)"),
    ("pytesseract (OCR)", "import pytesseract; print('Tesseract:', pytesseract.__version__)"),
    ("PIL (Images)", "from PIL import Image; print('Pillow OK')"),
    ("python-docx (Word)", "from docx import Document; print('python-docx OK')"),
]

for name, code in tests:
    cmd = f'docker run --rm agent_gateway-gateway:latest python -c "{code}"'
    r = subprocess.run(cmd, capture_output=True, text=True, shell=True, timeout=30)
    if r.returncode == 0:
        print(f'✅ {name}: {r.stdout.strip()}')
    else:
        print(f'❌ {name}: {r.stderr.strip()[:100]}')
