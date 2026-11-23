# 云服务器部署与中间件方案

本页描述本地 Caddy 方案，以及单机 Nginx、持久化和备份方案。模板中的域名、端口与路径是示例，部署前需按目标环境调整；不包含生产配置。

## 会话索引升级与回退（SQLite / PostgreSQL）

首次打开知识库会将 `schema_metadata.version` 从 1 升到 2。Trace 正文列从 `traces.value_json` 改名为 `traces.envelope_json`，索引回填与版本更新在同一事务提交。基线 `80cfc7c` 的两个适配器启动时均拒绝版本 2；升级前已打开的旧 Trace 适配器也无法再用原列名读写正文。这不是对其他数据表或已开始事务的隔离，**不支持新旧进程混跑或只换旧镜像回退**。

1. 升级前持维护锁，摘流并停止所有 API、CLI 写入、后台任务和定期清理。禁用自动镜像回退。保留旧镜像和配置；取得并验证同一停写点的完整备份。SQLite 包括已正确 checkpoint 并关闭的全套数据目录；PostgreSQL 包括知识库、账号、图像等权威数据和相应对象存储。不要手动丢弃 WAL 文件。
2. 用新版本打开并检查 `SELECT version FROM schema_metadata WHERE id=1` 为 2（PostgreSQL 使用配置的 schema）。在恢复流量前以测试账号验证旧记录、主动保存的对话和不保存的查询：后者仍有诊断 Trace，但不出现在列表。失败时保持服务离线。
3. 需要旧版本时再次停写，先保留升级后数据用于恢复或审计，再将**已验证的升级前备份恢复到隔离的数据目录或数据库与对象存储**。旧配置只指向该恢复副本；检查版本为 1、登录、隔离读写和 Trace 后再恢复流量与定时任务。恢复点后的写入不会出现在旧环境，撤权或删除也可能被撤销，须先核对并重新执行。切勿改版本号、改回列名、删除历史索引或使用 `deploy/downgrade_schema.py` 绕过保护；没有可用备份时保持停服并向前修复。

## 1. 现在需要什么

**当前只需要一个应用容器、一个反向代理，以及持久化磁盘；不需要先搭建 MySQL、Milvus、Redis 或本地模型服务器。** SQLite、文件归档和派生向量缓存都在应用进程内工作，不是额外中间件服务。这个建议基于当前已有适配器和小规模目标，不是对生产规模的性能承诺。[R1][R2][R3]

| 项目 | 本轮选择 | 是否独立容器 | 作用与边界 |
| --- | --- | --- | --- |
| 应用运行环境 | 项目 Python 镜像，CLI/HTTP 共享实现 | 是，`api` | 提供鉴权接口、知识生命周期、检索编排与云端调用 |
| HTTP 网关 | 本地 Caddy；指定服务器使用宿主现有 Nginx | 本地为 `caddy`；目标服务器无需新增网关容器 | 反向代理与 TLS；服务器使用本项目独立证书、vhost 和端口 [S4][S5] |
| 元数据、权限与 trace | SQLite | 否 | 使用本机持久卷内数据库；当前限制为单实例部署 [R2][S6] |
| 原始文件与解析产物 | 本地文件对象存储 | 否 | 与数据库一起放到 `/data`，避免容器重建丢失 [R2][S1] |
| 向量缓存 | SQLite 派生缓存 | 否 | 当前云 Embedding 适配器使用本地缓存和精确余弦检索，适合小规模；它不是生产向量数据库 [R3] |
| LLM、Embedding、Reranker | 云平台 HTTPS 接口 | 否 | 根据已确认的平台地址与模型 ID 调用；密钥只在服务端注入 [R3] |
| 网页 PDF / Office 解析 | MinerU 云接口 | 否 | 仅在用户同意外发原件后调用；旧 PDF 本地接口保留，无需部署 MinerU GPU 服务 |
| 意图判断 | 默认本地规则，可选 DeepSeek 官方 API | 否 | 单独配置服务端 Key；不需部署 Laya。见[使用流程](deepseek-rag.md)，本条不是生产启用记录 |

Docker Compose 官方文档提供单服务器生产部署方式；当前推荐先采用这一层级，之后以真实文档数、吞吐和耗时决定是否扩容。[S2]

## 2. 部署拓扑与仓库入口

```mermaid
flowchart LR
    U[客户端] -->|HTTPS| G[Caddy / 云网关]
    G -->|私有容器网络| A[单个 api 容器]
    A --> D[/data 持久卷]
    A -->|HTTPS| M[LLM / Embedding / Reranker 云平台]
    A -->|显式选用并上传| P[MinerU 云解析]
    D --> DB[知识 SQLite / trace]
    D --> O[文件对象归档]
    D --> V[派生向量缓存]
```

本地通用部署入口为根目录 `Dockerfile`、`compose.yaml`、`.dockerignore`，以及 `deploy/Caddyfile`；应用容器工厂位于 `adaptive_rag/server.py`。指定服务器使用 `deploy/compose.server.yaml`、`deploy/nginx.server.conf`，API 仅映射 `127.0.0.1:18000:8000`，公网使用独立 `8443`，不运行本地 Caddy 配置。使用前以实际文件内容和验收结果为准。

- 应用镜像从官方 Python slim 镜像构建。官方镜像清单核验到 `3.13-slim` 标签；正式发布应记录实际镜像 digest，不能仅凭可变标签宣称构建完全可复现。[S8]
- 若使用 uv 构建，遵循 Astral 官方容器方案，使用锁文件安装运行依赖，不把宿主的 Windows `.venv` 复制到 Linux 镜像。官方方案支持从 `ghcr.io/astral-sh/uv` 复制 uv 可执行文件。[S7]
- API 容器由显式服务器入口监听 `0.0.0.0:8000`，供同一私有容器网络中的 Caddy 访问。保留普通本地 CLI 的 loopback 默认；容器监听不是关闭鉴权。
- Compose 中 API 仅 `expose: 8000`，不要将 8000 映射到宿主公网。宿主只向外开放网关所需端口，数据库和缓存没有对外监听端口。
- 数据目录明确配置为 `/data`，挂载命名卷或云盘目录；配置与密钥只读挂载或通过运行环境注入。[S1][S3]

## 3. 启动与公网入口

**网页解析的部署核对项。** 在服务端配置 `MINERU_API_KEY`，不把密钥交给浏览器。单个原件最多 10,000,000 字节；代理请求体还需容纳 Base64 和 JSON。网页解析限制 MinerU 结果包为 32 MB、提取的 Markdown 为 2 MB。上游网络请求的超时配置为 20 秒，解析等待预算为 100 秒，不是整个上传、解析和暂存的完成时限。

仓库中的 `deploy/nginx.server.conf` 模板设置了 `client_max_body_size 16m` 和 `proxy_read_timeout 150s`。部署前核对实际代理配置，并用允许外发的样本验证；模板参数不代表线上配置或解析耗时。超时后远端任务可能仍在运行，不要自动重发原件。

首次启动先验证配置：

```bash
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

`docker compose config` 不带 `--quiet` 可能展开并打印环境变量；不要把其完整输出当作可公开的诊断日志。构建上下文必须用 `.dockerignore` 排除 `.env`、`.venv`、运行数据库、缓存、PDF 测试夹具和无关归档，Dockerfile 不得 `COPY` 密钥文件。

默认 `:80` 网关只用于本机或可信私网验收。**向公网提供鉴权接口前，必须启用 HTTPS。** 将 Caddy 站点地址改成实际域名，让 DNS 指向服务器，并按实际证书挑战方式开放入口；Caddy 的普通自动 HTTPS 配置涉及 80/443。官方文档说明它会自动申请和续期合格站点的证书，并将 HTTP 重定向至 HTTPS。[S5]

当前 Compose 将网关绑定到宿主 `127.0.0.1:8080`，本地验收地址为 `http://127.0.0.1:8080/health`。公网使用 Caddy 时，除修改站点域名，还需将 `ports` 改为 `80:80`、`443:443`（按需增加 `443:443/udp`），并核对云安全组；若前置云 HTTPS 网关，则保持回源只对可信网络开放。

示意 Caddyfile（域名需替换，不能照抄为已上线地址）：

```caddyfile
rag.example.com {
    reverse_proxy api:8000
}
```

公网部署还需核验：健康检查、无凭证访问被拒绝、管理员与普通身份分离、模型调用失败可观测、TLS 有效，以及日志中没有 Authorization、密钥和未经允许的原文。域名、证书、云安全组和数据出域政策不是 Docker Compose 自动完成的事项。

## 4. 密钥与配置

- 本地开发使用 Git 忽略的 `.env`；仓库只保存无密钥的 `.env.example`。云服务器由部署系统注入环境变量或读取受控文件，不把开发机 `.env` 打进镜像。
- `env_file` 是便利的环境变量注入，不等同于加密的密钥管理；拥有容器/主机管理权限的人可能读取运行环境。生产可使用云密钥管理系统或 Docker Compose secrets。[S3]
- Compose secrets 将授权给服务的内容作为 `/run/secrets/<name>` 文件提供；应用必须显式支持读取该路径。定义一个 secret 并不会自动把文件转换为 `RAG_CLOUD_API_KEY`，也不能据此声称当前代码支持 `*_FILE` 变量。[S3]
- 普通 Compose 文件型 secrets 的机制是文件挂载；不要把它误称为云 KMS 或自动静态加密方案。[S3]
- 云平台 Key 与访问本项目 HTTP 接口的 Bearer token 是不同凭据。前者用于出站模型调用；后者用于鉴别项目客户端身份，不应互相复用。[R1][R3]
- 显式指定平台 Base URL，不根据密钥字符串或模型名称猜地址。只把密钥发送到确认的平台地址。模型及平台变更需重新检查协议、配额、模型版本和 embedding 维度。[R3]
- 文件和 trace 可能含业务文档、问题和证据；持久卷访问权限、备份加密与保留周期应与业务数据政策一致。

## 5. 持久化、备份与恢复

应用所有需要保留的数据统一放在 `/data`：知识库数据库、归档对象、解析产物，以及云 Embedding 的派生缓存。网关证书状态也需要独立持久卷；Caddy 官方 Compose 示例为它的 `/data` 和 `/config` 配置卷。[R2][R3][S4]

**当前采用单个 API 实例，不配置多副本。** SQLite WAL 要求所有访问同一数据库的进程位于同一主机，不适用于网络文件系统；不能把一个 SQLite 文件放到 NFS/SMB 后交给多台服务器并发使用。[S6] SQLite 本身支持同主机多进程，但本项目的全链路吞吐、任务恢复和多副本行为尚未经过生产验收，单实例是本轮部署约束，并非“SQLite 完全不支持并发”。

最简单可靠的初期备份流程：

1. 暂停入口写操作并等待在途请求完成，停止应用容器，使知识发布、对象写入、trace 和缓存写入静止。
2. 备份整个 `/data` 与部署配置，另行保管密钥；记录应用提交、镜像 digest、模型 ID/版本和备份时间。不只拷贝单个 `knowledge.sqlite3`。
3. 启动应用并检查健康与读写。定期在隔离目录或测试服务器执行恢复演练，检查快照、ACL、证据回源和文件 hash。

指定服务器已有 `deploy/backup.sh` 和 `deploy/snapshot.py` 实现上述手动冷备、失败后恢复原运行状态，以及隔离恢复的归档、SQLite 和对象引用校验；发布、额外 CLI 写入与备份须持有同一项目维护锁。它们不覆盖生产数据，也未实现自动调度、异机副本或自动原位恢复。如需要不停机备份，应使用 SQLite 官方 Online Backup API 等受支持手段，并额外设计数据库与文件对象的一致性策略；仅在线复制主数据库文件可能遗漏仍在 WAL 中的数据。[S6][S9]

向量缓存属于派生数据，损坏后原则上可按相同模型配置重建；删除缓存会产生额外调用、费用和冷启动耗时，不能在生产中无计划清空。替换 Embedding 模型或维度必须隔离旧缓存，避免混用向量空间。[R3]

## 6. 什么时候再增加中间件

以下是扩容方向，不是本轮已接入或必须全部安装的清单：

| 触发条件 | 候选中间件类别 | 实施要求 |
| --- | --- | --- |
| 多实例应用、较多并发写、迁移/运维需求明确 | PostgreSQL 或 MySQL 等服务型关系库 | 实现 `KnowledgeStore`/`TraceSink` 适配器、迁移与事务语义，重验 CAS、快照、ACL 和回滚；启动一个数据库容器不会自动替换 SQLite |
| 本地精确向量扫描或 BM25 的耗时/内存超过实测预算 | 专用向量/检索引擎，如 Milvus、Qdrant | 实现检索适配器；保持模型/索引版本绑定，权限过滤和证据回源不能弱化。按业务实测再选，不在本轮先承诺具体产品 [S10] |
| 多台应用共享文件、归档容量或可用性要求提升 | S3 兼容对象存储或云对象存储 | 实现 `ObjectStore` 适配器及访问控制、版本/校验、重试与备份；当前本地文件实现不会直接读取 bucket |
| PDF/批量入库成为长任务，需要跨重启恢复和多 worker | 任务队列/消息代理；Redis 只是候选之一 | 先定义任务幂等、可见性超时、重试、取消和死信策略；不要将队列确认当成“已成功发布” [S11] |
| 多实例配额、缓存或速率控制确实需要共享状态 | Redis 等外部缓存 | 引入共享限流/缓存适配器并明确失效策略，Redis 不替代知识真源或模型调用审计 [S11] |
| 达到真实运维需要 | 指标、日志与告警平台 | 先输出受控结构化日志和调用计量，再按规模引入平台，避免记录密钥和全量敏感正文 |

目前没有 PostgreSQL/MySQL、Milvus/Qdrant、S3、Redis 或分布式任务的生产适配器。未来迁移时须实现并验收相应接口，不能通过新增 Compose 服务宣称完成集成。[R1][R2][R3]

## 7. 本次核验与未覆盖范围

已核验官方文档支持 Docker Compose 单服务器部署、持久卷、secrets 文件注入、Caddy 官方容器/TLS，以及 uv 官方 Docker 构建方式。[S1]–[S9] Docker volumes 页面直连曾失败，最终读取 Docker 官方 GitHub 仓库的同一文档；Python 镜像标签也来自 Docker Official Images 的维护清单。

部署前应分别验收镜像构建、Compose 启动、TLS、持久化、备份与恢复。静态校验不能替代回源测试、负载测试或灾难恢复演练。

后续开放事项：正式域名与公开信任证书、真实文档规模与并发、可接受延迟/成本、业务文档出域政策、备份恢复目标与异机副本。它们可能改变中间件选择。

## 来源

本地实现依据：

- [R1] [当前应用与鉴权入口](../adaptive_rag/api.py)、[配置](../adaptive_rag/config.py)、[角色接口](../adaptive_rag/ports.py)。
- [R2] [SQLite 与本地对象存储实现](../adaptive_rag/storage.py)。
- [R3] [本轮云模型与派生向量缓存实现](../adaptive_rag/cloud.py)。

官方来源，访问日期均为 2026-11-15；链接内容可能在此后更新，不将其示例基准视为本项目压测结果：

- [S1] Docker，Volumes：<https://docs.docker.com/engine/storage/volumes/>；本次实际读取官方源文件：<https://github.com/docker/docs/blob/main/content/manuals/engine/storage/volumes.md>。
- [S2] Docker，Use Compose in production：<https://docs.docker.com/compose/how-tos/production/>。
- [S3] Docker，Use secrets in Docker Compose：<https://docs.docker.com/compose/how-tos/use-secrets/>。
- [S4] Caddy，Keep Caddy Running / Docker Compose：<https://caddyserver.com/docs/running#docker-compose>。官方示例使用 `caddy:<version>` 及证书状态卷。
- [S5] Caddy，Automatic HTTPS：<https://caddyserver.com/docs/automatic-https>。
- [S6] SQLite，Write-Ahead Logging：<https://sqlite.org/wal.html>，特别是同主机与网络文件系统限制，以及 WAL/检查点行为。
- [S7] Astral，Using uv in Docker：<https://docs.astral.sh/uv/guides/integration/docker/>。
- [S8] Docker Official Images，Python 维护清单：<https://github.com/docker-library/official-images/blob/master/library/python>；官方镜像说明：<https://github.com/docker-library/docs/blob/master/python/README.md>。核验了 `3.13-slim` 标签，不将可变标签当作固定 digest。
- [S9] SQLite，Online Backup API：<https://sqlite.org/backup.html>。
- [S10] Qdrant 官方文档：<https://qdrant.tech/documentation/>。本次仅核验向量/索引/过滤类能力，未选择或部署该产品；Milvus 同样保留为候选，未作本轮选型结论。
- [S11] Redis 官方 Get started：<https://redis.io/docs/latest/develop/get-started/>。本次仅核验其数据结构存储/缓存类能力；可靠任务编排仍需项目自行定义与实现。
