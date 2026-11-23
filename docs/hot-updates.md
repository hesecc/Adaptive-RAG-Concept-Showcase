# 知识库热更新：预热与候选代

单文档即时发布仍可使用 `publish`。多份资料需要一起上线时，先 `ingest`，再把不同文档的 staged job 叠加在同一已知基代上。创建、预览和预热都不会改变线上指针；只有显式激活会把这份不可变清单作为当前代。所有新操作只在可信 CLI 提供，没有新增 HTTP 管理接口。

## 模型关闭时的完整操作

在未配置远程存储或云模型的本地环境运行。以下 PowerShell 示例新建独立目录，模拟已有线上内容及一批新增内容。真实操作须使用实际数据目录和租户。

```powershell
$dir = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
$baseJob = uv run adaptive-rag --data-dir $dir ingest --tenant demo --document policy --version v1 --file examples/store-policy.md | ConvertFrom-Json
$base = uv run adaptive-rag --data-dir $dir publish --tenant demo --job $baseJob.job_id | ConvertFrom-Json
$job = uv run adaptive-rag --data-dir $dir ingest --tenant demo --document warranty --version v1 --sku A100 --file examples/sku-warranty.md | ConvertFrom-Json
$candidate = uv run adaptive-rag --data-dir $dir candidate-create --tenant demo --expected-generation $base.generation --job $job.job_id | ConvertFrom-Json
$id = $candidate.generation
uv run adaptive-rag --data-dir $dir candidate-status --tenant demo --generation $id
uv run adaptive-rag --data-dir $dir candidate-preview --tenant demo --generation $id --question 'A100 保修规则是什么？' --sku A100
uv run adaptive-rag --data-dir $dir candidate-prewarm --tenant demo --generation $id --all-scopes --dry-run
uv run adaptive-rag --data-dir $dir candidate-activate --tenant demo --generation $id --expected-generation $base.generation
uv run adaptive-rag --data-dir $dir query --tenant demo --sku A100 --question 'A100 保修规则是什么？'
# 如需回退资料内容，用旧的已发布代作为目标；这会创建新的线上代，不恢复撤销的授权。
uv run adaptive-rag --data-dir $dir rollback --tenant demo --generation $base.generation --expected-generation $id
```

可重复传 `--job` 添加不同文档。空租户首次创建及激活时不传 `--expected-generation`；非空租户必须显式传入基代。候选预览使用词法查询，不调用模型；传 `--subject`、`--group`、`--data-domain` 和 `--sku` 可验证指定身份及商品的可见性。预览不改变 ACL。即使知道 ID，普通查询、流、HTTP trace、证据、插图和回滚都不能打开未激活代。激活后仍不能通过 HTTP 读取预览时产生的 trace。

## 文档向量预热

`candidate-prewarm` 处理未激活候选，`prewarm` 处理已发布代（含已发布的历史代）；两者都必须显式指定 generation。默认是 dry-run。在 dense 关闭时，只报告 `dense_disabled`、范围内块数和零次嵌入，不计算虚构的命中率；`--execute` 会失败。启用云 dense 后，dry-run 检查当前配置对应的本地向量缓存但不调用模型。先确保预热与实际查询使用**相同数据目录、向量配置和缓存**；这不是分布式索引构建。

默认范围遵守实时 ACL 和 SKU，不能用某个身份的结果证明全代就绪。可信操作员可显式选 `--all-scopes`，覆盖该租户该代所有未撤销的资料，包含受限数据域、ACL 和各 SKU；不能与任何身份或 SKU 参数并用。**真实执行会把该范围内缺失的文档检索文本发给已配置的嵌入服务**，需事先核准数据范围和费用。下例是操作说明，不是自动执行步骤：

```powershell
uv run adaptive-rag --env-file <approved-env> --data-dir $dir candidate-prewarm --tenant demo --generation $id --all-scopes --dry-run
# 只有明确授权后才执行；--max-chunks 限制本次补算的不同缓存键数。
uv run adaptive-rag --env-file <approved-env> --data-dir $dir candidate-prewarm --tenant demo --generation $id --all-scopes --execute --max-chunks 64
```

已完成批次写入派生缓存，失败可在检查错误后用相同参数重试。完成后重新 dry-run 核对 `missing=0`；该结论只对检查时的范围和模型命名空间有效。发布切换不强制检查预热结果；词法模式可以不预热，启用 dense 的操作员应先核对全范围结果再决定是否激活。预热不会生成答案、调用重排模型或改变在线指针。

## 冲突、回退和版本兼容

如果期间 live generation 变化，激活返回冲突且不切换。保留 job ID，查看 `candidate-status` 的 `live_generation` 并核对新基代的内容。用 `candidate-abandon --tenant demo --generation $id` 丢弃旧候选，释放该候选对 job 的保留；再次丢弃返回 `abandoned=false`，不能丢弃已激活代。确认这些变更仍应发布后，用仍处于 staged 状态的 job 与**新的**预期基代重新创建候选，重新检查、预览和激活。不要只给旧候选替换预期基代，也不要用回滚绕过检查。

**有任何未激活候选时，禁止降级到不识别候选的运行时，也不要混跑新旧运行时。** 不要依赖旧版本识别待激活代或保护其内容：在当前分表设计中，旧读进程按 ID 查询已发布表时看不到候选，但旧写进程不检查 job 保留，可能提前发布其中的内容，使普通读者读到候选资料。降级前停止所有写入、备份，按[存储兼容与清点步骤](candidate-snapshot-storage.md)清点所有租户的 pending generation；按需先激活或丢弃，再确认列表为空。知识内容回滚和程序版本降级不是同一操作。
