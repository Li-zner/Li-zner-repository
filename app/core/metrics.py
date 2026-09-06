from prometheus_client import Counter, Histogram, Gauge

gateway_requests_total = Counter(
    'gateway_requests_total',
    'Total requests to gateway',
    ['method', 'endpoint', 'status']
)

gateway_request_duration_seconds = Histogram(
    'gateway_request_duration_seconds',
    'Request duration in seconds',
    ['method', 'endpoint']
)

gateway_errors_total = Counter(
    'gateway_errors_total',
    'Total errors by status code',
    ['status']
)

app_exceptions_total = Counter(
    'app_exceptions_total',
    'Unhandled exceptions aggregated by type',
    ['type']
)

# 慢查询观测（后台任务查 pg_stat_statements，见 core/slow_query_watch.py）
app_slow_queries_total = Gauge(
    'app_slow_queries_total',
    'Slow queries count from pg_stat_statements (mean_exec_time > threshold) in last check',
    ['threshold_ms']
)
app_slow_query_max_ms = Gauge(
    'app_slow_query_max_ms',
    'Max mean exec time (ms) of slow queries in last check'
)

llm_tokens_total = Counter(
    'llm_tokens_total',
    'LLM tokens consumed (total)',
    ['type']  # input / output
)

llm_tokens_detail = Counter(
    'llm_tokens_detail',
    'LLM tokens by model and endpoint',
    ['model', 'endpoint', 'type']  # model=DEEPSEEK_MODEL, endpoint=v2_chat/sub_agent, type=input/output
)

llm_requests_total = Counter(
    'llm_requests_total',
    'LLM API request count',
    ['model', 'endpoint', 'status']  # status=success/error
)

kb_search_total = Counter(
    'kb_search_total',
    'Knowledge base searches by source',
    ['source', 'result']  # source=civil_code/travel, result=hit/miss/decomposed
)

semantic_cache_hits_total = Counter(
    'semantic_cache_hits_total',
    'Semantic cache hits'
)

semantic_cache_misses_total = Counter(
    'semantic_cache_misses_total',
    'Semantic cache misses'
)

rate_limit_rejects_total = Counter(
    'rate_limit_rejects_total',
    'Rate limit rejects'
)

# ============================================================
# 支付系统指标
# ============================================================
payment_orders_total = Counter(
    'payment_orders_total',
    '支付订单总数',
    ['order_type', 'status', 'payment_method']
)

payment_amount_total = Counter(
    'payment_amount_total',
    '支付金额总额（元）',
    ['order_type', 'payment_method']
)

payment_channel_duration_seconds = Histogram(
    'payment_channel_duration_seconds',
    '支付渠道处理耗时（秒）',
    ['channel_code']
)

payment_token_cost_total = Counter(
    'payment_token_cost_total',
    'Token 扣费统计',
    ['currency']
)

# ============================================================
# CDC（变更数据捕获）指标
# ============================================================
cdc_events_processed_total = Counter(
    'cdc_events_processed_total',
    'CDC 事件已落盘总数'
)

cdc_errors_total = Counter(
    'cdc_errors_total',
    'CDC worker 错误总数'
)

cdc_lag_seconds = Gauge(
    'cdc_lag_seconds',
    'CDC 处理延迟（当前时间 - 最新事件时间，秒）'
)
# DFA 过滤器超时放行次数（P2 修复：超时 fail-open 原先不可观测）
safety_filter_timeout_total = Counter(
    'safety_filter_timeout_total',
    'DFA safety filter scans aborted by timeout (content passed unfiltered)'
)
