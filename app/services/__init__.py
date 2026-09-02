"""app/services：v2 对话流业务服务层

2026-09 重构：原 app/routes/v2.py（1845 行上帝文件）按职责拆分下沉；
routes 只做路由与参数校验，services 依赖 core/agents/models，行为等价。
"""
