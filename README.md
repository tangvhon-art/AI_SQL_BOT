# AI 问数系统（AI SQL Bot）

自然语言问数平台：Python + FastAPI 后端，React + Ant Design v6 前端。
基于《AI 问数系统需求说明书 V1.3》与《概要设计 HLD V1.0》实现。

## 功能

- **数据源管理**：CRUD / 连接测试 / Schema 采集（表名与字段 comment、主键、外键关系图信息）
- **表关联**：外键自动生成 + 手动添加 / 删除；ER 关系图可视化
- **知识库（RAG）**：FAQ（问题/答案/SQL 示例）+ 文档（txt/md/pdf/docx）上传、智能切片、向量化、混合检索、few-shot 注入
- **模型配置**：OpenAI 兼容（DeepSeek / 通义 / Kimi / 本地 vLLM / Ollama），未配置 Key 时进入 mock 演示模式
- **AI 问数**：NL2SQL + Schema 注释提示词 + RAG 检索 + 自纠正；SSE 流式下发；**文字说明 + 图表优先，SQL 默认收起可展开/复制/下载**
- **历史会话**：落库保存，从历史对话进入继续追问
- **结果复用**：问数结果保存为查询，支持 `{param}` 参数化
- **定时任务**：cron 表达式，按参数化内容定时生成数据与图表快照，失败重试记录
- **字段级权限**：数据库表 `permission_rule` 驱动；角色全局 + 用户级，**可查并集 / 不可查并集 / 黑名单优先**；命中受限字段注入 `AND 1=2`，全可查注入 `AND 1=1`
- **审计日志**：问数全链路留痕（含权限注入类型、SQL、行数、耗时）

## 目录

```
backend/   FastAPI 后端（app/：api 路由 / engine 引擎 / services 服务）
frontend/  React + TS + AntD v6 前端（src/pages 页面 / components 组件）
docs/      需求说明书 V1.3 与概要设计 HLD V1.0（HTML）
```

## 快速开始

### 1. 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # 按需修改 LLM_API_KEY 等
.venv/bin/uvicorn app.main:app --reload --port 8000
```

- 元数据库默认 MySQL（AI_Infra，见 `.env` 的 DB_* 配置）；本机无 MySQL 时自动降级 SQLite 演示库
- 默认账号：`admin / admin123`
- API 文档：http://localhost:8000/docs

### 2. 前端

```bash
cd frontend
npm install
npm run dev                 # http://localhost:5173 （已代理 /api → :8000）
```

生产构建：`npm run build && npm run preview`

## 使用流程

1. **数据源管理** → 新建数据源 → 测试连接 → 采集 Schema（自动获取注释与外键关系）
2. **Schema 详情** → 查看 ER 关系图 / 表与字段（可补录注释）/ 手动添加表关联
3. **模型配置** → 配置 LLM（可选；不配置则 mock 演示）
4. **知识库** → 新建 FAQ / 上传文档，检索测试台验证命中
5. **权限控制** → 配置角色/用户的可查/不可查规则，查看生效预览
6. **AI 问数** → 输入业务问题，结果含文字说明 + 图表，SQL 可展开；可保存为查询
7. **结果复用/定时任务** → 参数化保存查询，创建 cron 任务，查看运行历史与图表快照

## 权限口径（与需求一致）

- 可查权限 = 角色可查 ∪ 用户可查；不可查权限 = 角色不可查 ∪ 用户不可查
- 不可查优先：不可查包含可查时以不可查为准
- SQL 涉及受限字段 → 注入 `AND 1=2`；全部可查 → 注入 `AND 1=1`（保证改写可叠加）

## 待确认项（见 HLD 13.1）

向量库最终选型（当前内置演示实现，预留 Milvus/Chroma 适配器）、调度器（当前单机线程调度）、通知渠道、SSO、K8s 部署等。
