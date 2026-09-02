"""创建Grafana预置看板"""
import httpx, asyncio, json

GRAFANA_URL = 'http://localhost:3001'
AUTH = ('admin', 'admit123')

DASHBOARD = {
    "dashboard": {
        "title": "Gateway 监控看板",
        "tags": ["gateway"],
        "timezone": "browser",
        "panels": [
            {
                "title": "请求总数",
                "type": "stat",
                "gridPos": {"h": 6, "w": 6, "x": 0, "y": 0},
                "targets": [{"expr": "sum(gateway_requests_total{job='gateway'})", "datasource": "prometheus"}]
            },
            {
                "title": "请求速率 (1m)",
                "type": "timeseries",
                "gridPos": {"h": 6, "w": 12, "x": 6, "y": 0},
                "targets": [{"expr": "sum(rate(gateway_requests_total{job='gateway'}[1m]))", "datasource": "prometheus"}]
            },
            {
                "title": "请求延迟 (P50/P95)",
                "type": "timeseries",
                "gridPos": {"h": 8, "w": 12, "x": 0, "y": 6},
                "targets": [
                    {"expr": "histogram_quantile(0.5, sum(rate(gateway_request_duration_seconds_bucket{job='gateway'}[5m])) by (le))", "datasource": "prometheus"},
                    {"expr": "histogram_quantile(0.95, sum(rate(gateway_request_duration_seconds_bucket{job='gateway'}[5m])) by (le))", "datasource": "prometheus"}
                ]
            },
            {
                "title": "状态码分布",
                "type": "piechart",
                "gridPos": {"h": 8, "w": 6, "x": 12, "y": 6},
                "targets": [{"expr": "sum by (status) (gateway_requests_total{job='gateway'})", "datasource": "prometheus"}]
            },
            {
                "title": "各端点请求数",
                "type": "barchart",
                "gridPos": {"h": 8, "w": 12, "x": 0, "y": 14},
                "targets": [{"expr": "sum by (endpoint) (gateway_requests_total{job='gateway'})", "datasource": "prometheus"}]
            },
            {
                "title": "内存使用",
                "type": "gauge",
                "gridPos": {"h": 6, "w": 6, "x": 12, "y": 14},
                "targets": [{"expr": "process_resident_memory_bytes{job='gateway'}", "datasource": "prometheus"}]
            }
        ]
    },
    "overwrite": True
}

async def main():
    # Test Prometheus datasource
    r = await httpx.AsyncClient().get(f'{GRAFANA_URL}/api/datasources/name/prometheus',
        auth=AUTH)
    print(f'Prometheus datasource: {r.status_code}')
    
    # Test query to verify data
    r2 = await httpx.AsyncClient().post(f'{GRAFANA_URL}/api/ds/query',
        auth=AUTH,
        json={
            "queries": [{
                "datasource": {"type": "prometheus", "uid": "prometheus"},
                "expr": "gateway_requests_total{job='gateway'}",
                "refId": "A"
            }]
        })
    if r2.status_code == 200:
        print('✅ Prometheus query successful - data exists!')
    else:
        print(f'Prometheus query failed: {r2.status_code} {r2.text[:200]}')
    
    # Create dashboard
    r3 = await httpx.AsyncClient().post(f'{GRAFANA_URL}/api/dashboards/db',
        auth=AUTH, json=DASHBOARD)
    if r3.status_code == 200:
        url = r3.json().get('url', '')
        print(f'✅ Dashboard created: {GRAFANA_URL}{url}')
    else:
        print(f'Dashboard creation: {r3.status_code} {r3.text[:200]}')

asyncio.run(main())
