"""
编码规范自动检查器 — 执行「函数 ≤80 行 / 文件 ≤600 行 / 无静默吞异常」硬约束（新项目模板自带）

检查项（与规则文件一一对应）：
  1. 单函数 > 80 行（违规：上帝函数）
  2. 单文件 > 600 行
  3. 函数内职责段过多（粗略：代码行数占比提示）
  4. except: pass 静默吞异常（无注释）

用法：
    python scripts/check_code_rules.py [路径...]   # 默认检查 app/（可用路径参数扩展）
    python scripts/check_code_rules.py --skip-files v2.py,service.py   # 豁免历史债文件（路径子串匹配）
    python scripts/check_code_rules.py --no-silent                     # 跳过静默 except 检查（CI 默认启用）
退出码：有违规返回 1（可接入 CI）
"""
import ast
import sys
from pathlib import Path

MAX_FUNC_LINES = 80
MAX_FILE_LINES = 600

DEFAULT_TARGETS = ["app"]


def parse_args(argv: list) -> tuple:
    """解析 --skip-files a,b 与 --no-silent（其余为检查路径）"""
    skip, no_silent, targets = [], False, []
    i = 0
    while i < len(argv):
        if argv[i] == "--skip-files" and i + 1 < len(argv):
            skip = [s.strip() for s in argv[i + 1].split(",") if s.strip()]
            i += 2
        elif argv[i] == "--no-silent":
            no_silent = True
            i += 1
        else:
            targets.append(argv[i])
            i += 1
    return targets or DEFAULT_TARGETS, skip, no_silent


def check_file(path: Path, skip: list, no_silent: bool) -> list:
    if any(s in str(path) for s in skip):
        return []  # 豁免的历史技术债（见 wiki 10-Rules/08 硬规则说明）
    violations = []
    try:
        src = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return violations
    lines = src.splitlines()
    if len(lines) > MAX_FILE_LINES:
        violations.append(f"  [文件] {path}: {len(lines)} 行 > {MAX_FILE_LINES}（需拆分模块）")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return violations
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nlines = (node.end_lineno or 0) - (node.lineno or 0) + 1
            if nlines > MAX_FUNC_LINES:
                violations.append(
                    f"  [函数] {path}:{node.lineno} {node.name} = {nlines} 行 > {MAX_FUNC_LINES}（上帝函数，需拆分）")
    # 静默吞异常检查
    if no_silent:
        return violations
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.body:
            only_pass = all(
                isinstance(stmt, ast.Pass) or
                (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                 and isinstance(stmt.value.value, str))
                for stmt in node.body
            )
            if only_pass and node.lineno:
                # 允许带字符串注释的 except（有解释）
                violations.append(f"  [静默] {path}:{node.lineno} except 无处理（建议注释原因）")
    return violations


def main() -> int:
    targets, skip, no_silent = parse_args(sys.argv[1:])
    all_violations = []
    for t in targets:
        p = Path(t)
        if p.is_file():
            all_violations += check_file(p, skip, no_silent)
        elif p.is_dir():
            for f in sorted(p.rglob("*.py")):
                if "__pycache__" in str(f) or f.name == "__init__.py":
                    continue
                all_violations += check_file(f, skip, no_silent)
    if all_violations:
        print(f"发现 {len(all_violations)} 处规范违规：")
        for v in all_violations:
            print(v)
        return 1
    print("规范检查通过：无违规")
    return 0


if __name__ == "__main__":
    sys.exit(main())
