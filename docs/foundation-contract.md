# 可插拔基座实施契约

默认配置保持 `model_free`：不发起模型请求，输出 `evidence_preview`、`answer=null`、`model_calls=0`。云 Embedding、Reranker、Generator，查询预处理、语义质量检查及 PDF 本地/MinerU 解析均通过显式入口选择。`DecisionPolicy` 默认使用本地规则，可显式选用 DeepSeek 官方 API 作意图判断；不下载或部署 Laya。

核心使用Python 3.11+，本地SQLite与内容寻址文件对象存储。HTTP使用FastAPI，CLI和HTTP调用同一应用接口。三个云模型已通过角色适配器接入；MySQL/Milvus/远程对象存储仍需后续实现相应适配器。当前 dense 为授权候选的精确余弦检索与 SQLite 派生向量缓存，不是生产 ANN 索引。

## 当前共享接口

数据以adaptive_rag/domain.py为准，协议以ports.py为准。Snapshot含tenant、generation和document→revision不可变清单；发布使用expected_generation进行CAS，首次发布预期None。stage只暂存；publish事务合并最新清单中的单个文档版本，旧快照保留。删除产生新代并实时撤销历史文档访问；rollback创建新代但不能恢复撤销权限。ACL空集合表示租户内公开；主体或groups命中可读取。存储接口必须按实时ACL及tenant复核，调用者伪造Snapshot也不得扩大权限。

同一原文+元数据+处理配置得到相同revision/job。相同source version改变处理配置可以重新构建。失败不能推进当前发布指针；所有SQLite操作使用独立连接和事务，不跨线程共享连接。

文件回收站是实时访问约束，不等同于撤权或整库回滚。软删除期间，历史快照、候选查询及证据回源都不能显示该资料；单项恢复仅把删除前已发布的版本合入当前代，仍使用当前权限。保留期后的物理清理是不可变历史的明确例外：删除到期资料的历史映射与关联内容，不保证历史快照永久可回放。详见[文件中心](file-center.md)。

## Application公共接口（API/CLI调用）

热更新增加仅供可信操作员使用的存储操作：`create_candidate_snapshot(tenant_id, job_ids, expected_generation)` 固定候选清单；`activate_candidate_snapshot(tenant_id, generation, expected_generation)` 在同一事务内校验基代并切换指针。未激活代不是历史发布代，普通 `open_snapshot/list_chunks/get_parents`、回滚目标及证据读取均不可使用。已激活代和先前发布代仍受实时 ACL 约束。

仅本地操作员可显式预览候选；候选查询产生的 trace 从创建起不向 HTTP 暴露（包括失败、提前结束及激活后）。普通 HTTP 管理员也不能通过指定 generation 或请求预览标志绕开隔离。显式预热只填充文档派生向量缓存，不调用查询规划、查询向量、重排或生成。默认检查主体可见范围；可信 CLI 的 `--all-scopes` 才检查指定代所有未撤销文档（包括受限 ACL、数据域及 SKU）。缓存命中不授予任何读取权限。详见[热更新操作和限制](hot-updates.md)。

- RagApplication(settings: Settings, registry: PluginRegistry | None = None)
- ingest(request: IngestRequest) -> IngestResult
- ingest_pdf(path, *, backend="local", pdf_settings=None, pdf_checkpoint=None, **metadata) -> IngestResult
- publish(tenant_id: str, job_id: str, expected_generation: str | None) -> Snapshot
- delete_document(tenant_id: str, document_id: str, expected_generation: str | None) -> Snapshot
- rollback(tenant_id: str, generation: str, expected_generation: str | None) -> Snapshot
- set_access(tenant_id: str, document_id: str, allowed_subjects: tuple[str,...], revoked: bool=False) -> None
- async query(request: QueryRequest) -> QueryResult
- get_evidence(access: AccessContext, generation: str, evidence_id: str) -> Evidence
- get_job(tenant_id: str, job_id: str) -> dict
- get_trace(tenant_id: str, trace_id: str) -> dict
- capabilities() -> dict

Settings位于adaptive_rag/config.py，可Settings(data_dir=Path(...))，load_settings(path: str|Path|None=None)；没有配置使用安全本地默认。配置未知键必须报错。app对象持有store、objects、trace_sink三个可调用的存储实现。注册插件按角色明确选择，不从用户数据动态import。

## 云能力与凭证注入

`Evidence.heading_path` 为可选的源标题路径，默认空元组；父块回填及 `get_evidence` 从同一受权父块获取。该元数据与正文、正文哈希和证据 ID 分开，旧数据无需重建或数据库迁移。生成、充分性检查和答案核验共用云证据序列化，标题路径在一次请求内均分 8,000 字符预算，裁剪时带 `heading_path_truncated`。标题仅解释来源范围，不能代替缺失的事实、授予访问权限或作为指令执行。

普通 CLI 不会自动加载 `.env`。本地通过百炼启用云能力需显式传 `--env-file .env`，并在所选文件中设置 `RAG_ENABLE_CLOUD=true`；部署系统也可显式注入进程环境，进程环境优先于文件。该开关选择 `dense/cloud`、`reranker/cloud`、`generator/cloud`，并允许对应模型与网络工厂。以下开关默认 `false`，只接受 `true` / `false`，可与百炼或 `RAG_ENABLE_DEEPSEEK_ANSWERS=true` 搭配。下表为百炼组合：

| 开关 | 插件选择 |
| --- | --- |
| `RAG_ENABLE_QUERY_PREPARATION` | `planner/cloud`、`rewrite/cloud` |
| `RAG_ENABLE_QUALITY_CHECKS` | `judge/cloud`、`verifier/cloud`、`gap/cloud` |
| `RAG_ENABLE_MULTI_QUERY` | `multi_query/cloud` |
| `RAG_ENABLE_HYDE` | `hyde/cloud` |

DeepSeek 问答组合将上表中的聊天插件选择为 `deepseek`，并始终启用 `judge`、`verifier`、`gap`；它不改变向量和重排插件。意图判断由独立的 `RAG_ENABLE_DEEPSEEK_INTENT` 启用，输入输出及故障行为见 [DeepSeek 问答流程](deepseek-rag.md)。多查询和 HyDE 可分别开启，不要求查询预处理或质量检查同时开启。权限过滤、快照和预算不由模型改变。

配置名称为 `RAG_CLOUD_BASE_URL`、`RAG_CLOUD_API_KEY`、`RAG_LLM_MODEL`、`RAG_EMBEDDING_MODEL`、`RAG_RERANK_MODEL`；无凭证示例与命令见 [README](../README.md#显式启用云模型)。云适配器自身不发现或读取密钥文件。平台地址显式配置，禁止凭密钥或模型名猜测目的地；仅允许 HTTPS（本机测试可 HTTP），不跟随重定向。百炼生成与向量走兼容地址，重排走独立原生地址，详见 [接口核对](research/bailian-api-notes.md)。

DeepSeek 调用不自动重试。百炼模型请求仅对 429/5xx 最多重试一次；鉴权失败、重定向、网络错误和超时不自动重试，避免不明计费结果下重复调用。查询 usage/trace 记录实际请求尝试（含重试）、模型/端点、状态、耗时和平台返回的 token；字符预算仍单独存在，不能换称 token。默认模式计量为零，云模式的全局 capabilities 不假报固定次数。

`.env`、受控 TOML 与凭证只在本机或服务器保管，不进 Git、镜像、公开日志或 trace。HTTP 的 `RAG_HTTP_API_TOKEN` 与云模型/MinerU Key 相互独立；该环境变量会按 `RAG_HTTP_TENANT`、`RAG_HTTP_SUBJECT` 创建一个管理员身份，多身份仍由可信配置映射。

## PDF 解析入口

`parse-pdf --file <PDF> --backend local|mineru --output <Markdown>` 仅生成解析产物；`ingest --file <PDF> --pdf-backend local|mineru` 暂存，发布仍需显式 CAS。默认 `local`，也可通过 `RAG_PDF_BACKEND` 选择。MinerU 需 `MINERU_API_KEY`，会上传文档；模型开关与 PDF 后端选择分别控制各自能力。

MinerU 的 `parse-pdf` 可带 `--checkpoint <本地JSON路径>`，PDF `ingest` 可带 `--pdf-checkpoint`，在同一 PDF 和解析配置下恢复已确认的批次。提交结果不明时保留检查点并报错，避免自动重复提交；本地后端拒绝检查点参数。源 PDF 按内容哈希独立归档，归档前复核解析时源哈希；MinerU 的插图随文档入库、按证据权限提供（见[文档里的插图](figures.md)），原 PDF 页内坐标回源尚未实现。解析质量按同一文件的完整性、顺序、表格和公式对比，不以字符数或无报错代替质量验收。命令见 [README](../README.md#pdf-解析与对比)。

## 运行与验收边界

真实实现文本/Markdown/HTML安全解析、PDF 本地/MinerU 转 Markdown、清洗父子块、持久化与快照、BM25、RRF、规则计划、父块证据、出处核验、可持久化trace、离线eval。dense/model rerank/generation 以及云查询、质量插件默认关闭，可按上述开关启用。禁用≠空结果≠错误，trace可区分。

`CloudPlanner` 结合当前问题和最多六轮传入 history 做有界语义分解；rewrite 可添加独立检索表述，多查询和 HyDE 各自增加变体。保留原问和子问题 ID，HyDE 草稿仅用于检索，不进入可引用正文。规则守卫仍拦截未接入的实时工具路由与未明确商品身份；模型不能从 history 推断可信 SKU。默认规则模式不消解多轮指代。trace 的 `history_used` 按带历史且确实产生模型请求的相关组件计量；守卫或预算跳过不算已使用。

结构保护识别解析后 Markdown 中的管道表格、HTML table、围栏代码、`$$...$$`、`\[...\]` 和常见闭合块公式环境，以及文档插图行（`![题注](ragfig:…)`）。在对应父块/子块字符上限内保留完整元素，并避免重叠窗口切入元素中间。已识别且超过上限的元素仍切片，其 locator 带 `structure_fragment`；未闭合代码和 HTML table 也可标记，未闭合公式尚未专门识别。不增加重复表头、闭合定界符等非原文文本。处理指纹已变更，旧文档须重新导入/发布才能得到新切块；图片语义、PDF 页坐标及通用版面结构树仍未实现。

云生成只接受已知证据 ID 的 claims，无证据不调用生成。默认 `CitationVerifier` 只核验出处，`support_checked=false`。开启质量检查后，`CloudEvidenceJudge` 判断每个子问题的全部要点是否有证据，返回充分/缺失分区及定向缺口查询；相关词重叠不视为充分。`CloudVerifier.evaluate(plan,draft,evidence)` 先经本地引用完整性检查，再请求云模型判断每条 claim 的支持/矛盾/无依据，以及答案是否漏答；保留原 `verify(draft,evidence)` 接口供只检查事实支持性的适配器使用。`Coverage.missing_queries` 与 `Verification.supported_claim_indexes` / `missing_queries` / `completeness_checked` 明确传递结果。

生成前与生成后共用最多一次补救预算，初检原问优先、扩展按子问题轮转裁剪，并预留可用的补救通道；全流程最多 12 个检索通道。缺口只沿当前授权候选与固定快照检索，合并候选后重新排序、回填、评估。生成后须有新证据才再次生成和核验，重复证据不触发循环。足够证据为零时返回 `insufficient`，部分可答或仍有矛盾/遗漏时只返回已支持 claims 并标记 `partial`；无可支持 claim 时不返回答案。所有暴露证据的新增终态复核实时权限。非法审查输出、未知引用、超时或接口失败不降级为通过，也不释放未核验草稿。

`support_checked=true` 和 `completeness_checked=true` 表示执行过云模型审查，不代表数学证明、独立 NLI 或域内校准；审查与生成共用模型，可能共同出错。检索 eval 仅计算 Hit/Recall/MRR/nDCG，不能替代答案质量、拒答正确性、增强消融和业务 gold 验收。

API从Bearer token的可信映射获得tenant/subject/groups，禁止请求正文自报身份；写操作要求管理员权限。默认只允许loopback监听并需显式配置token。CLI是可信本地管理入口，tenant参数不等于远程认证。

示例、测试替身不得伪装为真实模型调用。保留默认模式的重启、重复导入、并发CAS、旧快照、撤权、跨租户、空文/空检索/错误插件/超时、引用回源、所有插槽替换、零网络/零模型调用、eval无gold分母、CLI/HTTP同逻辑验收。云模式另核验协议、凭证脱敏、实际调用计量、缓存版本和失败处理，真实 smoke 与质量评测分别记录。

部署示例采用单个应用容器、反向代理和持久化数据。根目录 Compose 配置用于本地体验，服务器模板使用独立 Nginx vhost。详见 [部署与中间件方案](deployment.md) 和[服务器模板说明](../deploy/README.md)。仓库配置本身不代表生产环境通过验证。

已有手动冷备和隔离恢复校验脚本：项目维护锁覆盖发布、CLI 写入及备份；冷备只停本项目写进程并恢复其原运行状态，备份含数据、配置和私密材料。恢复检查只写新的隔离目录，验证归档、SQLite 完整性/外键和内容寻址对象，不覆盖生产，不启动恢复应用。锁为协作式约束，不能阻止忽略锁的额外写进程；主机崩溃或 SIGKILL 无法运行 shell 恢复 trap。自动调度、异机副本、原位自动恢复、生产 ANN、离线双索引就绪发布、分布式任务、高可用和 Laya 均未完成。
