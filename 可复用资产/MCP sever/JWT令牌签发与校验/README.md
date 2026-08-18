# JWT 令牌签发与校验（MCP Server 004）

## 作用

把 agent_gateway 认证体系（JWT + 刷新轮换 + 吊销）封装为通用 MCP 工具，任何需要
「无状态认证」的 Host 直接调用。**纯标准库实现 HS256**（base64url + hmac + 常量时间
比较），零第三方依赖，可移植到任何 Python 3.8+ 环境。

## 能力

| 工具 | 作用 | 设计要点 |
|------|------|---------|
| `create_access_token` | 签发 access（短时效，含 exp/iat/jti） | 生产建议 15min~2h，泄露面最小 |
| `verify_token` | 验签 + 过期 + 吊销黑名单校验 | 非法一律返回 `{"error": ...}`，绝不抛异常 |
| `create_refresh_token` | 签发 refresh（长时效 7 天） | 与 access 分型（`type` 字段） |
| `refresh_access_token` | 刷新轮换：旧 refresh 立即作废 | 防重放：泄露的 refresh 只能用一次 |
| `revoke_token` | 按 jti 吊销（踢人/泄露处置） | Redis 有则跨实例，无则进程内降级 |

## 为什么这样设计（面试必答）

- **无状态 = 多实例天然适配**：验签即验真，不需要共享会话存储——这是 JWT 在网关
  多实例架构里能用的根本原因。
- **短 access + refresh 轮换**：access 泄露攻击者只能用几分钟；refresh 用一次就作废
  （jti 进黑名单），即使泄露也只能用一次。
- **常量时间比较**（`hmac.compare_digest`）：防时序侧信道攻击——不能直接 `==` 比较签名。
- **JWT_SECRET 无默认值（fail loudly）**：密钥缺失拒绝签发，防默认密钥上线（安全审计 24 项之一）。
- **`ponytail:` 简化**：黑名单在无 Redis 时退化为进程内 dict（单实例够用）；多实例部署
  配 `REDIS_URL` 即跨实例共享。

## 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `JWT_SECRET` | **必填** | 签名密钥，缺失时工具返回错误 |
| `ACCESS_TTL_MINUTES` | `120` | access 有效期（分钟） |
| `REFRESH_TTL_DAYS` | `7` | refresh 有效期（天） |
| `REDIS_URL` | 空 | 配置后黑名单跨实例共享 |

## 运行与验证

```bash
pip install mcp

python jwt_token_service_server.py            # 拉起 Server（stdio）
python jwt_token_service_server.py --self-check   # 纯逻辑自检（无需任何服务）
python test_client.py                         # 完整验证（Redis 可选）
```

## 密钥轮换（生产必做）

双 Key 并行期：新 JWT_SECRET + 旧值保留为 JWT_SECRET_OLD，解码时两个密钥都试
（旧 token 仍可验）→ 旧 token 全过期后再移除旧 Key。轮换脚本模式见
agent_gateway `scripts/rotate_jwt_secret.sh`。
