"""测试路由裁决表"""
from app.agents.routing_table import route_query

tests = [
    ("网购商品七天无理由退货的法律依据", "消费"),
    ("被公司辞退了有经济补偿金吗", "劳动"),
    ("民法典关于离婚冷静期的规定", "民法典"),
    ("被人打了怎么赔偿", "侵权"),
    ("小区电梯广告费归谁", "物业"),
    ("交通事故责任认定", "交通"),
    ("我想注册商标", "商标"),
    ("被狗咬伤了找谁赔", "侵权民事"),
    ("我想离婚孩子归谁", "婚姻"),
    ("借钱不还怎么办", "合同"),
]

for q, label in tests:
    r = route_query(q)
    status = "✅" if r.action == "pass" else "🚫"
    print(f'{status} [{r.action}] {label}: {r.match_type} → {r.domain}')
    if r.action == "reject":
        print(f'   msg: {r.message[:60]}')
