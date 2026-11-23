# PostgreSQL、Milvus 与图片对象存储的可选部署

这一套是独立的候选拓扑，尚未切换现有服务器数据。原来的
`compose.server.yaml` 保持 SQLite 与文件存储方案；不要将新配置直接覆盖在
运行中的生产 Compose 上。新方案使用独立的 `an-rag-storage` 项目名、数据
目录、数据库卷和网络，仅 API 在回环地址暴露端口。

## 组成与资源

| 组件 | 部署方式 | 保存的数据 |
| --- | --- | --- |
| API | 本项目应用镜像 | HTTP、账号与检索入口 |
| asset-worker | 同一个应用镜像的独立进程 | 执行持久化图片索引任务 |
| PostgreSQL | 官方 `postgres:17.12-alpine` | 知识库、trace、图片元数据与任务 |
| Milvus | 官方 `milvusdb/milvus:v2.6.26` standalone | 图片文字的派生向量索引 |
| S3 兼容对象服务 | 独立配置的服务地址与专用 bucket | 原文、原图与缩略图 |
| 本地数据目录 | API 与 worker 共享的专用 bind | 账号 SQLite、会话及既有本地缓存 |

Milvus 按官方 embedded-etcd/local-storage standalone 方式运行，etcd、WAL
和索引文件均在 `milvus_data` 卷中。它的内部存储与应用图片所用的 S3 bucket
分开。Milvus、etcd、PostgreSQL 及管理端口均不发布到宿主机；应用通过内部
服务名 `postgres`、`milvus` 连接。Milvus 的 `seccomp:unconfined` 来自官方
standalone 启动配置，只作用于该容器。

Compose 中 Milvus 上限为 8GiB，PG 为 1GiB，API 为 768MiB，worker 为
512MiB，另有宿主系统与其他项目开销。建议为新拓扑预留至少 16GiB 主机
内存及相应 CPU、磁盘空间；不要在当前共享的 8GiB Docker 环境中直接启动
全栈。官方 Milvus 对小规模 standalone 的最低内存要求不代表本项目与同机
其他应用合计所需内存。正式容量仍需按数据量与并发实测。

应用可接 MinIO 的 S3 接口，但本配置不自动部署已归档的 MinIO Community
旧二进制，也不借用同机其他项目的对象存储或凭证。若选择 MinIO，需独立
确定可维护发行、授权、专用服务与备份方案；或提供其他受支持的 S3 兼容
服务。bucket 需要先创建，本项目不会在运行时自动创建它。

## 文件与启动

1. 构建带 `remote` 依赖的应用镜像，固定为源码提交对应的发布标签或摘要。
   `Dockerfile` 默认安装 PostgreSQL 与 S3 客户端依赖，普通本地 `uv sync`
   仍可只安装基础依赖。
2. 在新的私密目录准备 `config/app.env`、`secrets/postgres-password` 和
   `data`。数据目录必须允许应用 UID/GID `10001:10001` 写入；不要软链接到
   现有生产目录。密码文件只含 PostgreSQL 密码并限制宿主访问权限。
3. 将 `storage.env.template` 复制为私密 `app.env`，填写 PG DSN、专用 S3
   endpoint、bucket、凭证与 Milvus 配置。DSN 内密码须与密码文件一致；密码
   中的 URI 保留字符必须做百分号编码。DSN 的内部主机为 `postgres`，Milvus
   地址为 `http://milvus:19530`。
4. 复制 `storage.deployment.env.template` 为私密部署参数，填入上述绝对路径、
   固定镜像、**与现有账号数据库一致的租户**和未占用的回环 API 端口。
5. 复制 `compose.storage.yaml` 和相对目录 `milvus/`，保持相对结构。Compose
   要求 2.30+，因为运行时环境文件采用 `format: raw`；不要用 shell `source`
   执行环境文件。私密配置不提交 Git。

候选主机上的明确命令示例：

```bash
docker build --tag an-rag/app:release-<source-sha> .
docker compose --project-name an-rag-storage --env-file /opt/adaptive-rag-storage/deployment.env -f /opt/adaptive-rag-storage/compose.storage.yaml config --quiet
docker compose --project-name an-rag-storage --env-file /opt/adaptive-rag-storage/deployment.env -f /opt/adaptive-rag-storage/compose.storage.yaml up -d --wait --wait-timeout 300
curl --fail http://127.0.0.1:18010/health
```

API 的 `/health` 只证明进程可响应。上线前还要通过登录、图片上传、对象读回、
索引、检索、trace 回放和服务重启验证各后端；关闭模型时使用文字检索并确认
模型调用为零。需要语义索引时，再显式配置并启用原有云模型开关；远程存储
开关本身不会启用或调用模型。更换 Embedding 配置或维度时使用新的 Milvus
collection 并重新处理索引，不能把旧配置索引视为当前配置已经就绪。

这份 Compose 的 worker 只服务一个明确租户；多租户应为每个租户配置受控
worker 实例或另行实现调度，不能依赖默认 `demo` 猜测。启动 worker 会处理
当前尚未执行的任务；若开启云模型，待建任务会发起实际文本 Embedding 请求。

公网网关仍复用既有宿主 Nginx。只有候选实例验证完毕后，才按单独的切换方案
更新本项目 upstream；不要同时给两个实例写同一套账号文件或生产数据。

## 显式迁移知识库到空 PostgreSQL

**配置切换不会迁移数据。** `migrate_knowledge.py` 仅复制离线
`knowledge.sqlite3` 中的知识、快照、权限、作业和文档 trace；不迁移账号、
图片资产数据库、向量缓存或对象字节。

1. 持有项目维护锁并停止该项目所有写入者，包含 API、asset-worker、CLI 和
   其他维护脚本。保存旧镜像、旧配置与完整备份，确认源 SQLite 完整性。
2. 在隔离副本上预演，目标 PostgreSQL schema 必须为空。迁移拒绝合并或覆盖
   非空目标；一次事务写入全部表，失败回滚。源连接为只读事务。
3. 在安装了项目及 `remote` 依赖的受控环境执行；私密迁移环境文件中放目标
   DSN 和 `RAG_ENABLE_REMOTE_STORAGE=true`。目标地址应从执行位置可达；
   容器内可用 `postgres`，宿主不能直接把该内部名称当公网地址。

```bash
uv sync --extra remote
uv run --extra remote python deploy/migrate_knowledge.py --source /private/offline-copy/knowledge.sqlite3 --env-file /private/migration.env
```

在候选容器网络内执行时，可将脚本与离线副本以只读 bind 挂载到一次性的应用
容器，再执行相同命令的 Python 部分；此迁移脚本不包含在运行镜像中，不应在
API 容器里临时安装依赖或向其复制凭证。成功输出仅包含各表行数，不输出 DSN。
`read_environment` 的进程注入值优先于文件，使用独立干净的迁移进程以避免
继承错误目标配置。脚本不提供停止生产写入者的自动保证。

4. 将原 `accounts.sqlite3` 保留到新数据目录并保持原租户，账号不会因为知识
   库迁入 PG 而自动迁移。图片 SQLite 数据迁往 PG 尚无通用迁移工具；已有
   图片需要另行实现受审查的资产迁移或在候选环境重新导入，不能直接丢弃原库。
5. 对象从文件切到 S3 时，需保留原内容哈希 key，在专用 bucket 与 prefix 下
   同步字节并逐对象校验。当前发布物没有自动同步工具；未完成同步不能切换
   `RAG_OBJECT_BACKEND`，否则旧证据和原图会找不到。
6. 用迁移后的候选实例核验数量、版本、ACL、历史 trace、引用与图片，再设计
   正式切换。将空 PG 或空 S3 指向现有流量不能算迁移完成。

## 备份、恢复与回退范围

现有 `backup.sh` / `snapshot.py` 只服务旧的 `an-rag` 本地存储拓扑。它只停止
该拓扑的 API，**不会停止新 worker，也不会备份 PG、远程 S3 或 Milvus 卷**。
不要将旧脚本的成功结果作为新拓扑完整备份成功。

新拓扑冷备时应持有专用维护锁，并停止所有本项目 API、worker 和 CLI 写入：

- PostgreSQL：用匹配主版本工具产生可恢复的自定义格式逻辑备份，保存角色与
  扩展配置需求；在独立 PG 实例实际恢复并核对知识、权限、图片、任务及 trace。
- S3：保存专用 bucket 的对象与版本清单、hash、prefix 配置，使用存储方支持
  的版本化、快照或复制策略。恢复到独立 bucket 并按 PG 中对象 key 完整读回；
  一份文件列表不等于已经备份对象内容。
- 账号与本地缓存：在写入者停止期间备份整个专用 data bind，包括
  `accounts.sqlite3`；确认恢复后账号租户与 PG 配置一致。
- Milvus：可选择受支持的备份工具或在容器完全停止后保存完整专用卷（含 etcd
  与 WAL）；不能在线只复制部分文件。索引是派生数据，丢失后应创建新空
  collection，再对所有有效资产显式登记和执行重建任务。当前仅有逐资产
  reindex 入口，尚无批量重建 CLI；不能只恢复元数据里的 `ready` 状态就假定
  Milvus 数据仍在。重建可能再次产生云 Embedding 费用。
- 配置与秘密：分别备份发布镜像标识、私密环境与密码文件，限制访问，并保存
  备份时各组件版本以及同一静止点的清单。

恢复验收必须在独立目录、数据库、bucket 与 collection 中启动应用，验证
登录、资料读取、撤权、索引任务和检索。正式回退前确认旧版本兼容新 schema；
配置回退不代表数据回退，不能用旧配置去覆盖新产生的知识或对象。不要运行
全局 Docker prune，也不要使用带卷删除的命令处理共享服务器。

## 上游核验依据

2026-11-16 核对了以下官方来源。部署清单固定版本，后续升级应重新走兼容性与
实际运行验收；这里的语法检查不等于已启动新版本服务。

- Milvus 官方 v2.6.26 release 的 standalone Compose 使用
  `milvusdb/milvus:v2.6.26`；embedded 脚本提供 embedded etcd 与 local storage
  配置。该 tag 下脚本内镜像版本存在滞后，因此本配置采用 release 对应的
  v2.6.26 镜像，不直接执行上游脚本。
  `https://github.com/milvus-io/milvus/releases/tag/v2.6.26`
  `https://raw.githubusercontent.com/milvus-io/milvus/v2.6.26/scripts/standalone_embed.sh`
- PostgreSQL 官方镜像与版本清单：
  `https://hub.docker.com/_/postgres`
  `https://github.com/docker-library/official-images/blob/master/library/postgres`
- MinIO Community 仓库与当前维护状态：
  `https://github.com/minio/minio`
