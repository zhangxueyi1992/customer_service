# 智能人工客服系统

一个面向 Web 的智能客服系统，核心体验对标微信 / WhatsApp：用户在类微信的对话界面提问，系统基于知识库（RAG 检索增强生成）实时给出**总结性回答（不带来源引用）**；机器人无法解决时可一键**转人工客服**，由坐席在工作台接管；管理员可上传管理知识库、查看统计数据与账号权限。

- 设计文档：[`Project.md`](Project.md)
- 测试说明：[`Testing.md`](Testing.md)

---

## 一、功能特性

### 1. 对话界面（仿微信 / WhatsApp）

- 左侧会话列表 + 右侧聊天窗口，支持会话搜索、置顶、单个会话删除（悬停删除 + 二次确认）；
- 消息气泡左右区分（用户右 / 客服·机器人左），带头像、昵称、时间；
- 消息状态流转：发送中 → 已发送（单勾）→ 已读（双勾）；
- "对方正在输入…" 实时提示；
- 支持文本、图片、链接卡片等富文本消息；
- 回答直接展示总结文字，**不标注知识库来源**；
- 会话历史持久化到 SQLite，刷新页面不丢失；第一条用户消息自动作为会话标题；
- 底部「转人工客服」按钮与满意度评价（1–5 星 + 评价内容，非法评分拦截）；
- 转人工后可一键「转回智能客服」（AI / 人工双向切换）。

### 2. 知识库管理

- 批量上传文档：PDF、Word(.docx)、TXT、Markdown、HTML（单文件 ≤ 20MB）；
- 文档解析与清洗：自动去除页眉页脚、独立页码、乱码与多余格式；
- 文本切片：按标题结构分节、按段落聚合，固定长度带重叠切块，保留来源文档 / 页码 / 标题元数据；
- 向量化入库：切片经 `BAAI/bge-small-zh-v1.5` 中文向量模型生成 embedding，写入 Chroma（余弦相似度）；
- 知识库管理：文档列表、删除、停用 / 启用、更新（重新解析入库）、上传进度与解析状态；
- 效果测试：输入测试问题，预览命中的知识切片、来源文档、页码与相似度，便于调优。

### 3. 智能问答（RAG）

- **问题预处理**：去除"请问 / 我想问一下"等废话与口语尾词，统一全半角，纠正常见错别字（如"退宽 → 退款"）；
- **意图识别**：知识库问答（kb）/ 转人工（transfer）/ 闲聊（chitchat）三分类；
- **混合检索**：Chroma 向量相似度检索为主，叠加本地 BM25 关键词检索（中文按单字 + 二元组切分），召回 Top-20；
- **重排**：按「向量相似度 + BM25 + 关键词重合 + 标题命中」融合打分，保留 Top-5；
- **生成**：召回片段与最近对话上下文组装进 Prompt，调用大模型（DeepSeek，OpenAI 兼容）连贯总结回答，不标注来源；接口异常时回退本地摘录生成，服务不中断；
- **兜底**：检索不到相关内容时明确告知"知识库暂无此内容"并引导转人工，避免模型胡编。

### 4. 人工客服工作台

- 接管机器人会话，实时收发消息（WebSocket 广播）；
- 转接流程：用户请求转人工 → 进入排队 → 客服点击「接入」分配 → 可一键放回排队；
- 快捷回复：常用话术一键发送，支持新增 / 编辑 / 删除；
- 会话标签与备注，便于统计与复盘；支持会话置顶；
- 同一客服可同时处理多个会话（类似微信多聊天窗口）；
- 转人工期间机器人自动停止应答，避免抢答。

### 5. 管理与统计

- **指标总览**：会话量、转人工率、知识库命中率、平均响应时间、满意度评分与分布，支持日期范围过滤；
- **对话记录**：按关键词 / 状态 / 客服 / 日期查询，查看完整消息详情，导出 CSV（含 BOM，Excel 可直接打开）与 JSON；
- **账号管理**：新增 / 编辑 / 删除账号，角色分管理员（admin）、客服（agent）、普通用户（user），可改密码、停用账号。

---

## 二、技术栈

| 层次 | 技术 |
|---|---|
| 后端 | Python 3.14 + FastAPI + Uvicorn |
| 实时通信 | WebSocket（`/ws/chat`，按会话房间一对多广播） |
| 关系存储 | SQLite（`data/customer_service.db`，WAL 模式） |
| 向量库 | ChromaDB（持久化到 `chroma_data/`，余弦距离） |
| 向量模型 | fastembed + `BAAI/bge-small-zh-v1.5`（本地推理，首次自动下载） |
| 文档解析 | pypdf（PDF）、python-docx（Word）、内置解析器（TXT / Markdown / HTML） |
| 大模型 | DeepSeek / 任意 OpenAI 兼容接口（可选，异常自动回退本地生成） |
| 前端 | 原生 HTML + CSS + JavaScript（无构建步骤，静态文件由 FastAPI 托管） |

> 说明：`Project.md` 中的 React/Vue 为选型建议，当前实现采用零构建的原生前端，开箱即用。

---

## 三、系统架构

```
用户端（Web 仿微信界面 /agent 工作台 /admin 后台）
        │  WebSocket / HTTP
        ▼
FastAPI 应用（app/main.py）
        ├── 会话管理 / 消息路由 / 转人工 / 鉴权
        ├── 机器人应答（app/bot.py → app/rag.py）
        │      └── 预处理 → 意图识别 → 混合检索 → 重排 → LLM 生成 / 本地摘录
        ├── 知识库服务（app/knowledge.py）：解析 → 清洗 → 切片 → 向量化
        └── 数据访问（app/db.py）
                ├── SQLite：会话 / 消息 / 用户 / 反馈 / 快捷回复 / 知识库元数据
                └── Chroma：知识切片向量
```

**核心链路**：用户提问 → 预处理与意图识别 → 混合检索 Top-20 → 融合重排 Top-5 → 组装上下文 → LLM 生成总结回答 → WebSocket 实时推送（不带来源引用）。

---

## 四、目录结构

```
customer_service/
├── app/
│   ├── main.py          # FastAPI 入口：REST 接口 + WebSocket + 静态页面挂载
│   ├── db.py            # SQLite 数据访问层（会话/消息/用户/统计/知识库元数据）
│   ├── knowledge.py     # 知识库：文档解析、清洗、切片、向量化入库、检索
│   ├── rag.py           # RAG 管线：预处理、意图识别、BM25、混合检索、重排、生成
│   ├── bot.py           # 机器人回答入口（委托给 rag）
│   └── static/          # 前端静态资源
│       ├── index.html / app.js / style.css     # 用户对话界面（含知识库管理弹窗）
│       ├── agent.html / agent.js / agent.css   # 人工客服工作台
│       └── admin.html / admin.js / admin.css   # 管理后台
├── tests/               # 冒烟测试脚本（直接运行，无需 pytest）
│   ├── smoke_test.py           # 对话界面：REST + WebSocket 全链路
│   ├── knowledge_test.py       # 知识库：五种格式上传、解析、检索、停用、更新、删除
│   ├── rag_test.py             # RAG：预处理、意图、BM25、检索重排、兜底
│   ├── agent_console_test.py   # 客服工作台：接管、快捷回复、实时收发
│   ├── admin_test.py           # 管理后台：登录、统计、查询、导出、账号管理
│   └── samples/                # 测试样例文档（txt / md / html）
├── resources/           # 示例知识库素材（话术、尺码表、产品信息）
├── data/                # 运行期数据：SQLite 库 + uploads（已 gitignore）
├── chroma_data/         # Chroma 向量库持久化目录（已 gitignore）
├── demo_chroma.py       # Chroma 最小验证：文本 → 向量化 → 入库 → 检索
├── run_server.bat       # Windows 一键启动脚本
├── requirements.txt
├── Project.md           # 建设方案 / 需求清单
└── Testing.md           # 测试命令与预期结果
```

---

## 五、快速开始

### 1. 环境要求

- Python 3.11+（开发环境为 3.14）
- Windows / macOS / Linux（`run_server.bat` 为 Windows 脚本）
- 首次运行需联网下载向量模型（约 95MB），国内建议走镜像（见下文）

### 2. 安装依赖

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> 依赖：`fastapi`、`uvicorn`、`chromadb`、`fastembed`、`pypdf`、`python-docx`、`python-multipart`、`httpx`、`websockets`。

### 3. 首次下载模型（国内镜像，仅首次需要）

```bat
set HF_ENDPOINT=https://hf-mirror.com
set HF_HUB_DISABLE_XET=1
.venv\Scripts\python.exe demo_chroma.py
```

模型下载后会缓存到本机，之后无需再设置这两个环境变量。

### 4. 启动服务

```bat
run_server.bat
```

等价命令：

```bat
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 5. 页面入口

| 页面 | 地址 | 说明 |
|---|---|---|
| 用户对话界面 | http://127.0.0.1:8000/ | 仿微信聊天，左下角可进入知识库管理与客服工作台 |
| 客服工作台 | http://127.0.0.1:8000/agent | 顶部可设置客服姓名，排队会话点「接入」 |
| 管理后台 | http://127.0.0.1:8000/admin | 默认管理员 `管理员` / `admin123`（首次初始化自动创建） |

API 文档（FastAPI 自动生成）：http://127.0.0.1:8000/docs

---

## 六、配置项（环境变量）

服务启动前按需设置，未设置时使用默认值。

### 大模型（可选，不配置则使用本地摘录生成）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RAG_USE_LLM` | `1` | 是否启用大模型生成；测试环境建议设为 `0` |
| `OPENAI_API_KEY` | 空 | 未设置时自动读取 `~/.codex/auth.json` 中的 Codex Key（密钥不写入项目） |
| `OPENAI_BASE_URL` | 空 | 未设置时读取 `~/.codex/config.toml`，否则 `https://api.deepseek.com` |
| `OPENAI_MODEL` | 空 | 未设置时读取 Codex 配置，否则 `deepseek-chat` |
| `RAG_LLM_WIRE_API` | `auto` | 接口形态：`auto` / `chat` / `responses` |
| `RAG_LLM_TIMEOUT` | `30` | 大模型请求超时（秒） |
| `RAG_LLM_MAX_TOKENS` | `800` | 回答最大输出 token 数 |

关闭 / 指定其他 OpenAI 兼容服务示例：

```bat
set RAG_USE_LLM=0
:: 或
set OPENAI_API_KEY=sk-xxx
set OPENAI_BASE_URL=https://api.deepseek.com
set OPENAI_MODEL=deepseek-chat
```

### 检索与重排阈值

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RAG_MIN_VECTOR_SIM` | `0.45` | 向量相似度下限，低于则走兜底 |
| `RAG_MIN_RERANK_SCORE` | `0.38` | 融合重排得分下限，低于则走兜底 |
| `RAG_BM25_REF` | `2.0` | BM25 归一化参考分 |

### 其他

| 变量 | 说明 |
|---|---|
| `HF_ENDPOINT` | 向量模型下载源（如 `https://hf-mirror.com`） |
| `HF_HUB_DISABLE_XET` | 设为 `1` 关闭新下载协议，兼容镜像 |
| `CS_PORT` | 测试脚本连接的服务端口（默认 `8000`） |

---

## 七、API 一览

### 会话与消息（用户端）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/conversations` | 会话列表 |
| `POST` | `/api/conversations` | 创建会话 |
| `GET` | `/api/conversations/{id}` | 会话详情 |
| `GET` | `/api/conversations/{id}/messages` | 拉取历史消息 |
| `DELETE` | `/api/conversations/{id}` | 删除会话 |
| `POST` | `/api/conversations/{id}/transfer` | 请求转人工（进入排队） |
| `POST` | `/api/conversations/{id}/switch-ai` | 切回智能客服 |
| `POST` | `/api/conversations/{id}/feedback` | 提交满意度评价 |
| `WS` | `/ws/chat?conversation_id=&role=&agent_name=` | 实时收发消息 |

### 客服工作台

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/agent/conversations?agent_name=` | 排队中 + 当前客服接管的会话 |
| `POST` | `/api/agent/conversations/{id}/assign` | 接管排队会话（重复接管返回 409） |
| `POST` | `/api/agent/conversations/{id}/return` | 放回排队（仅限接管客服） |
| `PATCH` | `/api/agent/conversations/{id}/meta` | 设置标签与备注 |
| `PATCH` | `/api/agent/conversations/{id}/pinned` | 置顶 / 取消置顶 |
| `GET`/`POST` | `/api/agent/quick-replies` | 快捷回复列表 / 新增 |
| `PUT`/`DELETE` | `/api/agent/quick-replies/{id}` | 编辑 / 删除快捷回复 |

### 知识库

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/knowledge/documents` | 批量上传文档（multipart，字段名 `files`） |
| `GET` | `/api/knowledge/documents` | 文档列表 + 知识库统计 |
| `GET` | `/api/knowledge/documents/{id}` | 文档详情 |
| `GET` | `/api/knowledge/documents/{id}/chunks` | 文档切片列表 |
| `PATCH` | `/api/knowledge/documents/{id}/enabled` | 停用 / 启用 |
| `POST` | `/api/knowledge/documents/{id}/reindex` | 更新（重新解析入库） |
| `DELETE` | `/api/knowledge/documents/{id}` | 删除文档 |
| `POST` | `/api/knowledge/test` | 效果测试：预览命中切片与相似度 |

### 管理后台

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/admin/login` | 管理员登录（非 admin 角色拒绝） |
| `GET` | `/api/admin/stats` | 关键指标（支持 `date_from` / `date_to`） |
| `GET` | `/api/admin/conversations` | 对话记录查询（关键词 / 状态 / 客服 / 日期） |
| `GET` | `/api/admin/conversations/{id}` | 对话详情（含完整消息） |
| `GET` | `/api/admin/export?fmt=csv\|json` | 导出对话记录 |
| `GET`/`POST` | `/api/admin/users` | 账号列表 / 新增 |
| `PUT`/`DELETE` | `/api/admin/users/{id}` | 编辑（角色、密码、状态）/ 删除 |

### WebSocket 消息协议

客户端 → 服务端：

```json
{ "type": "send", "content": { "text": "怎么退货？" }, "msg_type": "text", "client_id": "c-1" }
{ "type": "typing" }
{ "type": "read" }
```

服务端 → 客户端：

| `type` | 载荷 | 说明 |
|---|---|---|
| `hello` | `conversation`、`role`、`agent_name` | 连接建立 |
| `message_ack` | `client_id`、`message_id`、`status` | 消息已入库（发送中 → 已发送） |
| `message` | `message`、`sender`、`client_id` | 新消息广播 |
| `typing` | `sender`、`is_typing` | 正在输入提示 |
| `status` | `message_ids`、`status` | 已读回执 |
| `conversation_updated` | `conversation` | 会话状态变化（接管 / 放回排队等） |

---

## 八、数据模型

SQLite 表结构（`app/db.py` 初始化，含旧库自动补列兼容）：

| 表 | 主要字段 |
|---|---|
| `conversations` | id、title、status（bot / queued / human / closed）、agent_name、tag、note、pinned、created_at |
| `messages` | id、conversation_id、sender（user / bot / human / system）、msg_type（text / image / card / answer）、content(JSON)、status（sent / read）、created_at |
| `feedback` | id、conversation_id、rating、comment、created_at |
| `users` | id、name(unique)、role（admin / agent / user）、password、status、created_at |
| `quick_replies` | id、title、content、created_at、updated_at |
| `knowledge_docs` | id、filename、doc_type、size、uploader、status（processing / completed / failed）、enabled、error、created_at、updated_at |
| `knowledge_chunks` | id、doc_id、chunk_index、content、page、title、metadata、vector、enabled、created_at |

Chroma 集合：`knowledge_chunks`（余弦距离，元数据含 `doc_id` / `doc_name` / `title` / `page` / `chunk_id` / `enabled`）。

初始化时会自动创建两个账号：`管理员` / `admin123`（admin）、`客服1`（agent，无密码）。

---

## 九、测试

所有命令均在项目根目录执行，需先启动服务（`rag_test.py` 除外）。

```bat
:: 0. 启动服务
run_server.bat

:: 1. 对话界面冒烟测试（REST + WebSocket 全链路）
.venv\Scripts\python.exe tests\smoke_test.py

:: 2. 知识库管理测试（五种格式上传、解析清洗、检索、停用、更新、删除）
.venv\Scripts\python.exe tests\knowledge_test.py

:: 3. 智能问答 RAG 测试（无需启动服务）
set RAG_USE_LLM=0
.venv\Scripts\python.exe tests\rag_test.py

:: 4. 人工客服工作台测试
.venv\Scripts\python.exe tests\agent_console_test.py

:: 5. 管理与统计测试
.venv\Scripts\python.exe tests\admin_test.py
```

指定端口：`set CS_PORT=8000`。全部通过时脚本输出「全部通过 ✔ / ✓」。

向量链路最小验证：

```bat
.venv\Scripts\python.exe -c "import chromadb; print(chromadb.__version__)"
.venv\Scripts\python.exe demo_chroma.py
```

各测试的详细覆盖范围与预期结果见 [`Testing.md`](Testing.md)。

---

## 十、常见问题

**Q：首次运行卡在下载模型 / 下载失败？**
设置 `HF_ENDPOINT=https://hf-mirror.com` 与 `HF_HUB_DISABLE_XET=1` 后重试，模型缓存后不再需要联网。

**Q：回答没有走大模型，只是摘录知识片段？**
检查是否设置了 `RAG_USE_LLM=0`，或 `OPENAI_API_KEY` 与 `~/.codex/auth.json` 是否可用。大模型调用失败会自动回退本地生成（日志中会有 warning），服务不受影响。

**Q：某个问题总是回复"知识库暂无此内容"？**
说明检索未命中阈值。可在「知识库管理 → 效果测试」中查看召回切片与相似度，或调整 `RAG_MIN_VECTOR_SIM` / `RAG_MIN_RERANK_SCORE` / `RAG_BM25_REF`。

**Q：转人工后机器人还在回答？**
机器人仅在会话状态为 `bot` 时应答；转人工（`queued`）或客服接管（`human`）后自动静默。

**Q：数据存放在哪里？如何清空？**
业务数据在 `data/customer_service.db`，向量数据在 `chroma_data/`，上传文档在 `data/uploads/`。三者均在 `.gitignore` 中，删除目录并重启服务即可重建（管理员账号会重新初始化）。

**Q：能换其他向量模型吗？**
可以。修改 `app/knowledge.py` 中的 `EMBED_MODEL`，并重建知识库索引（删除 `chroma_data/` 后对文档逐个「更新」或重新上传）。
