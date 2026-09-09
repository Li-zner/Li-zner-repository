# 自产代码（前端可复用原语）

> **用途**：从 agent_gateway 前端 v2（生产运行中）萃取的、与具体 UI 无关的可复用原语。
> 每件都是"一行 import 即用"形态；UI 组件一律不收录（业务页面不复用，见上级 README 结论）。
> **提取日期**：2026-09-06。**来源项目**：agent_gateway/frontend（the-world-agent.cloud 在线运行）。

## 成熟度标记约定

每件资产头部注释三行：`来源` / `实战验证`（生产运行中 / 单测覆盖 / 未验证）/ `依赖`。
下次复用后请在来源行补注"二次复用于：<项目>"。

## 资产索引（按复用价值排序）

| 资产 | 文件 | 解决什么 | 依赖 | 实战验证 |
|---|---|---|---|---|
| SSE 跨 chunk 分帧器 | `sse-framer.ts` | 流式接口的半行粘包难题：feed 任意切分的文本块，内部攒帧回调完整行 | 零依赖 | 生产 + 6 条单测 |
| JWT HTTP 客户端（401 单飞刷新） | `http-jwt-client.ts` | token 注入、401 自动刷新重放、**单飞防轮换互踩**、统一 ApiError | 零依赖（fetch） | 生产 |
| Markdown 消毒链 | `markdown-sanitizer.ts` | LLM 输出渲染防 XSS：marked 解析 + DOMPurify 消毒，两步缺一不可 | marked + dompurify | 生产 + 单测 |
| 本地会话存储 | `local-session-store.ts` | localStorage 多会话持久化：损坏数据形状防御、按维度隔离键、至少保留一个 | 零依赖 | 生产 |
| 用户定位解析器 | `user-location-resolver.ts` | 三级兜底（本地缓存清理 → 浏览器定位+逆地编 → IP 定位），定位源可注入 | 注入（默认接高德） | 生产 |
| PWA 离线壳 | `pwa-sw.js` | Service Worker：哈希资产缓存优先 / 导航网络优先 / API 永不拦截 | 部署于站点根 | 生产 |
| 中国地图 GeoJSON 工具 | `geo-china-utils.ts` | DataV adcode → GeoJSON、质心锚点、下钻初始缩放、天气文案 | 数据源 geo.datav.aliyun.com | 生产 |

## 使用纪律

1. 取用时连同头部资产注释一起拷贝（来源可溯源）；
2. TS 文件假定 ES2020+ 模块环境；`markdown-sanitizer` 需 `npm i marked dompurify`；
3. `user-location-resolver` 的 IP 定位/逆地编默认留空——接入高德 Web 服务 API 时自行注入，密钥走 .env；
4. 上级 README 维护约定继续生效：CRLF 转 LF、剥离 emoji。
