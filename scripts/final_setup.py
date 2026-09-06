"""Clean up duplicate datasources and create proper dashboard"""
import httpx, asyncio

GURL = 'http://localhost:3001'
AUTH = ('admin', 'admit123')

async def main():
    # 1. List all datasources
    r = await httpx.AsyncClient().get(f'{GURL}/api/datasources', auth=AUTH)
    dss = r.json()
    
    # 2. Delete duplicates (keep the first one of each type)
    seen_types = {}
    for ds in list(dss):  # copy list
        t = ds['type']
        if t in seen_types:
            print(f'Deleting duplicate {ds["name"]} (uid={ds["uid"]})...')
            await httpx.AsyncClient().delete(f'{GURL}/api/datasources/uid/{ds["uid"]}', auth=AUTH)
        else:
            seen_types[t] = ds
    
    # 3. Verify remaining
    r2 = await httpx.AsyncClient().get(f'{GURL}/api/datasources', auth=AUTH)
    print('\nRemaining datasources:')
    for ds in r2.json():
        isdef = ' (default)' if ds.get('isDefault') else ''
        print(f'  {ds["name"]}: type={ds["type"]} url={ds.get("url")}{isdef}')
    
    # 4. Create dashboard
    dashboard = {
        "dashboard": {
            "title": "Gateway 监控",
            "tags": ["gateway"],
            "timezone": "browser",
            "schemaVersion": 36,
            "panels": [
                # ── 第一行：概览 ──
                {
                    "title": "请求总数",
                    "type": "stat",
                    "gridPos": {"h": 5, "w": 4, "x": 0, "y": 0},
                    "targets": [{"expr": "sum(gateway_requests_total{job='gateway'})", "datasource": {"type": "prometheus"}}],
                    "fieldConfig": {"defaults": {"unit": "short"}}
                },
                {
                    "title": "错误率",
                    "type": "stat",
                    "gridPos": {"h": 5, "w": 4, "x": 4, "y": 0},
                    "targets": [{
                        "expr": "sum(rate(gateway_requests_total{job='gateway',status=~'5..'}[5m])) / sum(rate(gateway_requests_total{job='gateway'}[5m])) * 100",
                        "datasource": {"type": "prometheus"}
                    }],
                    "fieldConfig": {
                        "defaults": {
                            "unit": "percent",
                            "color": {"mode": "thresholds"},
                            "thresholds": {"mode": "absolute", "steps": [
                                {"color": "green", "value": 0},
                                {"color": "orange", "value": 1},
                                {"color": "red", "value": 5}
                            ]}
                        }
                    }
                },
                {
                    "title": "请求速率",
                    "type": "timeseries",
                    "gridPos": {"h": 5, "w": 8, "x": 8, "y": 0},
                    "targets": [{"expr": "sum(rate(gateway_requests_total{job='gateway'}[1m]))", "datasource": {"type": "prometheus"}}]
                },
                {
                    "title": "内存",
                    "type": "gauge",
                    "gridPos": {"h": 5, "w": 4, "x": 16, "y": 0},
                    "targets": [{"expr": "process_resident_memory_bytes{job='gateway'}", "datasource": {"type": "prometheus"}}]
                },
                # ── 第二行：延迟 + 状态码 ──
                {
                    "title": "延迟 P50 / P95 / P99",
                    "type": "timeseries",
                    "gridPos": {"h": 9, "w": 12, "x": 0, "y": 5},
                    "targets": [
                        {"expr": "histogram_quantile(0.5, sum(rate(gateway_request_duration_seconds_bucket{job='gateway'}[5m])) by (le))", "datasource": {"type": "prometheus"}, "legendFormat": "P50"},
                        {"expr": "histogram_quantile(0.95, sum(rate(gateway_request_duration_seconds_bucket{job='gateway'}[5m])) by (le))", "datasource": {"type": "prometheus"}, "legendFormat": "P95"},
                        {"expr": "histogram_quantile(0.99, sum(rate(gateway_request_duration_seconds_bucket{job='gateway'}[5m])) by (le))", "datasource": {"type": "prometheus"}, "legendFormat": "P99"}
                    ]
                },
                {
                    "title": "状态码分布",
                    "type": "piechart",
                    "gridPos": {"h": 9, "w": 6, "x": 12, "y": 5},
                    "targets": [{"expr": "sum by (status) (gateway_requests_total{job='gateway'})", "datasource": {"type": "prometheus"}}]
                },
                {
                    "title": "错误率趋势",
                    "type": "timeseries",
                    "gridPos": {"h": 9, "w": 6, "x": 18, "y": 5},
                    "targets": [{"expr": "sum(rate(gateway_requests_total{job='gateway',status=~'5..'}[1m])) / sum(rate(gateway_requests_total{job='gateway'}[1m])) * 100", "datasource": {"type": "prometheus"}, "legendFormat": "错误率%"}],
                    "fieldConfig": {"defaults": {"unit": "percent"}}
                },
                # ── 第三行：各端点请求 ──
                {
                    "title": "各端点请求",
                    "type": "barchart",
                    "gridPos": {"h": 8, "w": 12, "x": 0, "y": 14},
                    "targets": [{"expr": "sum by (endpoint) (gateway_requests_total{job='gateway'})", "datasource": {"type": "prometheus"}}]
                },
                # ── 第四行：Tempo 链路追踪瀑布图 ──
                {
                    "title": "Tempo 链路追踪（最近 50 条）",
                    "type": "traces",
                    "gridPos": {"h": 12, "w": 24, "x": 0, "y": 22},
                    "datasource": {"type": "tempo"},
                    "targets": [{"query": "{job=\"gateway\"}", "datasource": {"type": "tempo"}, "queryType": "serviceMap"}],
                    "fieldConfig": {"defaults": {}},
                    "options": {
                        "serviceName": "agent-gateway",
                        "spanType": "waterfall",
                        "showRoot": True
                    }
                }
            ]
        },
        "overwrite": True
    }
    
    r3 = await httpx.AsyncClient().post(f'{GURL}/api/dashboards/db', auth=AUTH, json=dashboard)
    if r3.status_code == 200:
        url = r3.json().get('url', '')
        print(f'\n✅ Dashboard: {GURL}{url}')
    else:
        print(f'\n❌ Dashboard error: {r3.status_code} {r3.text[:200]}')

asyncio.run(main())
