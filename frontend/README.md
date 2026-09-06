# agent_gateway 前端（frontend/）

> 技术栈：Vue 3 + TypeScript + Vite + Pinia + Vue Router + vue-i18n + echarts
> 架构惯例对照：[vue3-element-admin](https://github.com/youlaitech/vue3-element-admin)（api 按模块拆分、utils/request 统一封装、enums 枚举、composables、类型就近）——2026-09-05 二次遍历对齐落地
> 分支：`feat/frontend-v2`（全量迁移验收后合回 main 并归档旧 `static/`）

## 常用命令

```bash
npm install          # 安装依赖
npm run dev          # 开发服务器 :5173（/api /v2 /auth 代理到后端 :10086）
npm run build        # vue-tsc 类型检查 + 产物构建 → dist/
npm run test         # Vitest 单测
npm run lint         # ESLint（flat config，0 error 基线）
npm run e2e          # Playwright 主链路 E2E（首次需 npx playwright install chromium；
                     # 需后端 :10086 在跑，并注入 E2E_USER/E2E_PASS 环境变量）
```

## 目录约定（对照 vue3-element-admin 惯例）

```
src/
├── api/        传输层：http（JWT 注入/401 刷新重试）、sse（跨 chunk 分帧器）、
│               chat/auth/personas/files/map（按模块拆分，类型就近定义）
├── stores/     Pinia：auth（登录态）、chat（会话/消息流/人格/autoPrompt）
├── views/      页面：LoginView / ChatView / MapView（echarts 中国地图，省市下钻）
├── components/ MessageBubble（markdown+DOMPurify）/ ChatComposer / PersonaPicker
├── layouts/    BasicLayout（顶部导航：页面切换/语言/用户区，页面共用）
├── composables/useUserLocation（缓存→geolocation→regeo→IP 三级兜底定位）
├── enums/      SseEventType / StorageKey 等枚举常量
├── types/      全局领域类型（UserLocation / GeoJson 等）
├── locales/    vue-i18n：zh-CN / en-US（词典平移自旧 i18n.js，键名不变）
├── utils/      markdown（marked+DOMPurify）、geo（GeoJSON 纯函数：质心/下钻缩放/adcode）
└── styles/     全局样式
tests/          Vitest 单测（SSE 分帧器 / markdown 消毒 / geo 工具）
e2e/            Playwright 主链路（登录 → 对话 → 流式渲染）
```

## 关键设计

- **SSE 协议**：后端 `POST /v2/chat/stream` 事件为 `data: {"type": ...}`，终止 `data: [DONE]`；
  `api/sse.ts` 负责跨 chunk 攒帧（单测覆盖半行/坏 JSON/[DONE]），`api/chat.ts` 消费事件流
- **跨页协作**：地图「去这里」→ Pinia `autoPrompt` → ChatView 挂载时自动发送（替代旧 localStorage 手递）
- **i18n**：vue-i18n 组合式 API，语言偏好持久化 `travel_lang`（与旧版键兼容），聊天请求 lang 随语言切换
- **XSS 防线**：LLM 输出经 `marked.parse` + `DOMPurify.sanitize` 双重处理（有注入消毒单测）

## 迁移清单（旧 static/ → 本工程）

- [x] 聊天主链路：流式渲染、人格选择、文件上传、会话管理、剩余配额展示
- [x] 登录态：账密/手机号登录、GitHub 回调令牌落地、401 刷新重试
- [x] 地图页：省级地图/省份下钻/城市气泡（天气+推荐）/历史收藏/随机城市/用户定位标记/「去这里」
- [x] i18n：词典全量平移（zh 224 键 / en 224 键）+ 语言切换
- [ ] 切换期：`app/main.py` 静态挂载改指 `frontend/dist`，端到端验收后归档旧 `static/`

## 已知边界

- E2E 需要真实后端与测试账号，CI 中通过 E2E_PASS 注入，未配置时自动跳过
- token 存 localStorage（演示部署形态）；生产建议 httpOnly Cookie，见 src/api/http.ts 头注
- 地图 GeoJSON 来自阿里云 DataV 公网地址（后端 /api/map/geojson 代理为可选方案）
