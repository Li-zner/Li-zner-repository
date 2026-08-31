"""CDC（Change Data Capture）模块

基于 PostgreSQL 触发器 + 事件表 + 后台 Worker 的变更数据捕获实现：
- 触发器把 payment_orders / transaction_logs / user_wallets 的增删改写入 cdc_events 表
- Worker 单写者（Redis 锁）轮询事件表，追加写入 JSONL 日志（SSD 落盘）
- 支持断点续传（checkpoint）与消费 API
"""
