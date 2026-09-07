# AI 问数系统（AI SQL Bot）

自然语言问数平台：用自然语言提问，系统自动完成 NL2SQL、知识检索、权限控制、图表生成与定时任务，让业务人员直接「问」出数据。

- 后端：Python + FastAPI（/api/v1）
- 前端：React 19 + TypeScript + Vite + Ant Design v6
- 依据文档：《AI 问数系统需求说明书 V1.3》《AI 问数系统概要设计说明书 HLD V1.0》及后续补充设计（见 `docs/`）

## 功能特性

### 数据管理
- **数据源管理**：数据源 CRUD、连接测试、Schema 采集（表名与字段 comment、主键、外键关系图信息）
- **表关联**：外键自动生成 + 手动添加/删除；ER 关系图可视化（Schema 详情页）
- **知识库（RAG）**：FAQ（问题/答案/SQL 示例）+ 文档（txt/md/pdf/docx）上传，智能切片、向量化、混合检索、few-shot 注入，问数时「越问越准」

### 智能问数
- **AI 问数（NL2SQL）**：Schema 注释提示词 + RAG 检索 + SQL 自纠正；SSE 流式下发；**文字说明 + 图表优先，SQL 默认收起（可展开/复制/下载）**
- **文件问答**：对话中上传文档作为附件，基于文档内容问答，返回引用来源；支持 txt/md/pdf/docx
- **模型配置**：OpenAI 兼容协议（DeepSeek / 通义 / Kimi / 本地 vLLM / Ollama 等），未配置 Key 时自动进入 mock 演示模式
- **历史会话**：落库保存，从历史对话进入继续追问

### 权限与审计
- **字段级权限**：数据库表 `permission_rule` 驱动；角色全局 + 用户级，**可查并集 / 不可查并集 / 黑名单优先**；命中受限字段注入 `AND 1=2`，全可查注入 `AND 1=1`
- **组织管理**：角色、用户组、用户三级管理，菜单权限随角色下发
- **审计日志**：问数全链路留痕（含权限注入类型、SQL、行数、耗时）

### 结果复用与调度
- **结果复用**：问数结果保存为查询，支持 `{param}` 参数化
- **定时任务**：cron 表达式，按参数化内容定时生成数据与图表快照，失败重试并记录运行历史

## 技术栈

| 端 | 技术 |
|---|---|
| 后端 | FastAPI、SQLAlchemy 2、PyMySQL、PyJWT、sqlglot（SQL 改写/校验）、httpx、pydantic-settings |
| 前端 | React 19、TypeScript、Vite 8、Ant Design v6、Zustand、ECharts、react-markdown |
| 元数据库 | MySQL（默认 `AI_Infra`）；本机无 MySQL 时降级 SQLite（`DATABASE_URL=sqlite:///./ai_infra.db`） |
| 向量库 | 内置演示实现（builtin），预留 Milvus / Chroma 适配器（`VECTOR_BACKEND`） |

## 目录结构

```
.
├── backend/                 FastAPI 后端
│   ├── app/
│   │   ├── main.py         应用入口：路由注册、CORS、启动初始化（建表+默认数据+调度器）
│   │   ├── config.py       pydantic-settings 配置（.env）
│   │   ├── database.py     数据库连接与会话
│   │   ├── models.py       ORM 模型
│   │   ├── security.py     密码哈希 / JWT / AES 密钥加解密
│   │   ├── llm.py          OpenAI 兼容 chat/embedding 客户端（未配置 Key 时 mock）
│   │   ├── vector_store.py 向量存储抽象（builtin/milvus/chroma）
│   │   ├── executor.py     SQL 执行器（超时、行数限制）
│   │   ├── api/            路由层：auth、datasources、knowledge、models_config、chat、
│   │   │                   doc_chat_api、scheduled_tasks、permissions、dicts、audit、org
│   │   ├── engine/         核心引擎：nl2sql、rag、intent、chart、permission、scheduler、
│   │   │                   llm_provider、doc_chat、doc_parser、doc_store、doc_retriever、
│   │   │                   prompt_kit、analyzer、preprocess、sql_extractor、mapping 等
│   │   └── services/       服务层（datasource_service）
│   ├── uploads/            文档上传目录（git 忽略）
│   ├── requirements.txt
│   └── .env.example        环境变量模板
├── frontend/                React + TS + AntD v6 前端
│   └── src/
│       ├── pages/          页面：Login、Chat、Datasource、Schema、Knowledge、Model、
│       │                   Permission、SavedQuery、Audit、Role、Group、User
│       ├── components/     通用组件（SqlBlock、ChartCard、MessageCard、PageHeader）
│       ├── api/            axios 客户端与 SSE 流式封装
│       ├── stores/         Zustand 状态（chat）
│       └── hooks/          通用 hooks（CRUD 列表、消息提示、下拉选项）
├── docs/                    设计文档（需求说明书、概要设计、查询链路重设计、文件问答需求等）
├── logs/                    运行日志（backend.log / frontend.log，git 忽略）
└── start.sh                 一键启动脚本（见下）
```

## 快速开始

### 方式一：一键启动（推荐）

```bash
./start.sh
```

脚本会同时启动后端（uvicorn，端口 **8001**）与前端（Vite，端口 **3001**），日志实时滚动到 `logs/backend.log`、`logs/frontend.log`；启动前自动释放被占用的端口，Ctrl+C 优雅停止（先停后端调度线程，再停前端）。

- 自定义端口：`BACKEND_PORT=9001 FRONTEND_PORT=5173 ./start.sh`
- 前置要求：`backend/.venv`（依赖已装）、`frontend/node_modules`（依赖已装），否则脚本会提示安装命令

### 方式二：手动启动

```bash
# 后端（端口 8001）
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # 按需修改 LLM_API_KEY 等
.venv/bin/uvicorn app.main:app --reload --port 8001

# 前端（端口 3001，已代理 /api → :8001）
cd frontend
npm install
npm run dev                 # http://localhost:3001
```

生产构建：`cd frontend && npm run build && npm run preview`

### 访问入口

- 前端：http://localhost:3001 ，默认账号 `admin / admin123`
- API 文档（Swagger）：http://localhost:8001/docs （全部接口挂载于 `/api/v1` 前缀）


## 使用流程

1. **数据源管理** → 新建数据源 → 测试连接 → 采集 Schema（自动获取注释与外键关系）
2. **Schema 详情** → 查看 ER 关系图 / 表与字段（可补录注释）/ 手动添加表关联
3. **模型配置** → 配置 LLM（可选；不配置则 mock 演示）
4. **知识库** → 新建 FAQ / 上传文档，检索测试台验证命中
5. **权限控制** → 配置角色/用户的可查/不可查规则，查看生效预览
6. **AI 问数** → 输入业务问题，结果含文字说明 + 图表，SQL 可展开；可上传文档进行文件问答；可保存为查询
7. **结果复用/定时任务** → 参数化保存查询，创建 cron 任务，查看运行历史与图表快照

## 权限口径（与需求一致）

- 可查权限 = 角色可查 ∪ 用户可查；不可查权限 = 角色不可查 ∪ 用户不可查
- 不可查优先：不可查包含可查时以不可查为准
- SQL 涉及受限字段 → 注入 `AND 1=2`；全部可查 → 注入 `AND 1=1`（保证改写可叠加）

