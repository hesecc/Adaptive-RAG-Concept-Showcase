# Adaptive RAG · 可追溯的知识库问答

面向产品手册、内部政策与电商资料的知识库问答系统。它把**文件上传、版本发布、权限检索、带出处的回答和检索诊断**串成同一条流程。默认只用本地检索，不需要模型密钥即可体验入库、问答证据和回源。

## 能解决什么问题

- **资料多、版本杂：** 上传与发布分离，查询固定一个知识版本。批量更新可先预览候选版本，再用 CAS 一次切换。文件中心支持回收站和恢复，软删除后不再参与检索。
- **不同账号能看不同资料：** 资料分类、租户和访问名单参与检索过滤；历史对话与原文回源重新核验当前权限。用户可调整自己的检索选项，不能改写他人的配置或绕过必需步骤。
- **回答需要可核对：** 逐条引用能打开证据原文。证据不足时明确不生成答案，并标注可展示的参考片段。结构足够明确的证据可展示趋势图、柱状图或流程图，同时保留原始数据。
- **检索效果需要能解释：** 工作台可查看各阶段的输入摘要、耗时、命中与模型用量；支持合成权限矩阵和带标注的检索评测。

## 架构

```text
上传 → 解析 / 清洗 / 父子切块 → 暂存 → 显式发布知识快照
                                        ↓
提问 → 固定版本 / 权限过滤 → BM25（可选向量）→ 融合与重排 → 授权证据
                                                               ↓
                         原文引用 / 历史会话 / Trace ← 生成与核验（可选）
```

**技术栈：** Python、FastAPI、原生 HTML/CSS/JavaScript、uv、SQLite、Docker Compose。可选集成 MinerU、百炼和 DeepSeek；存储扩展支持 PostgreSQL、S3 兼容对象存储和用于图片文本表示的 Milvus。默认体验不依赖这些外部服务。

## 本地工作台

需要 Python 3.11+ 与 uv。在仓库根目录运行（PowerShell）：

```powershell
uv sync --locked
Copy-Item .env.example .env  # 仅首次创建；已有配置时不要覆盖
# 在本机 .env 中填写 RAG_ADMIN_PASSWORD，其余模型开关保持默认 false
uv run adaptive-rag --env-file .env serve
```

打开 `http://127.0.0.1:8000/`，以 `ragadmin` 和自己设置的口令登录。进入「知识文件管理」，上传 `examples/sku-warranty.md`，分类选「公共」，暂存后发布。返回检索工作台提问：**“A100 保修规则是什么？”**

默认模式返回证据预览，不会假装调用模型生成答案。点击「证据」和「查看原文」核对出处；左侧历史对话可以回看该轮执行。同样的虚构资料也可通过 [CLI 示例](examples/README.md) 演示。

## 验证与评测

```powershell
uv run python -m unittest discover -s tests -v
node --test tests/conversation_ui.test.cjs tests/page_owner.test.cjs
uv run adaptive-rag eval-permissions --tenant demo
```

Python 测试覆盖发布冲突、分类隔离、历史回放权限、回收站、引用与模型接入边界；依赖外部服务的场景按运行条件跳过。前端测试需要 Node.js，应用本身不需要 Node.js 构建。检索指标（Hit@K、Recall@K、MRR、NDCG）与权限矩阵见 [评测说明](docs/evaluation.md)，不把检索命中当成回答准确率。

## 显式启用云模型

默认不开启模型调用。[配置模板](.env.example) 中的凭据均为空。按需启用百炼检索与生成、DeepSeek 意图识别或 DeepSeek 问答，并使用自己的密钥。不要提交 `.env`。配置见 [DeepSeek 使用说明](docs/deepseek-rag.md) 和[百炼接口说明](docs/research/bailian-api-notes.md)。文件中心的 PDF / Office 云解析需要另行配置 MinerU，且每次外发均需上传者确认。

## 深入阅读

| 想看什么 | 入口 |
| --- | --- |
| 知识入库、发布、回收站 | [文件中心](docs/file-center.md) · [知识热更新](docs/hot-updates.md) |
| 权限、证据、界面与历史对话 | [分类鉴权](docs/data-permissions.md) · [检索工作台](docs/workbench.md) · [文档插图](docs/figures.md) |
| 评估、角色接口与常见边界 | [评测](docs/evaluation.md) · [基座契约](docs/foundation-contract.md) · [相似度判断](docs/relevance.md) |
| 容器、远端存储与部署模板 | [部署结构](docs/deployment.md) · [存储模板](deploy/STORAGE.md) · [服务器模板](deploy/README.md) |

展示仓库包含应用源码、测试、部署模板和虚构样例，不包含生产配置或业务数据。外部服务、协议与依赖以各自的官方文档及许可证为准。
