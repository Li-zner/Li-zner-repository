# 笔记解剖与 Obsidian 语法规范

## 1. YAML Frontmatter (必须包含)
每个新建的笔记顶部必须包含以下 YAML 属性：
---
title: "{{笔记标题}}"
created: {{YYYY-MM-DD}}
tags:
  - {{领域标签}}
  - {{状态标签, 如 seedling/sapling/evergreen}}
aliases: []
---

## 2. 链接与标签规范
- **双链 `[[ ]]`**：用于连接知识库内的核心概念、项目或人物。尽量使用别名（如 `[[Obsidian|黑曜石]]`）。
- **标签 `#`**：用于分类和状态标记（如 `#todo`, `#review`, `#tech/python`）。支持嵌套标签。
- **高亮 `== ==`**：用于标记正文中的核心结论或金句。
- **Callouts (admonitions)**：使用 `> [!info]` 或 `> [!warning]` 来突出重要提示，而不是普通的引用块。

## 3. MOC (Map of Content) 维护
- 当在某个领域（如 `30-Areas/Backend/`）下新增超过 3 篇笔记时，提醒我更新该目录下的 `MOC.md` 索引文件。