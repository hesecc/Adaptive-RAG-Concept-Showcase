# 无模型演示

以下示例只运行本地解析、检索和证据回溯，不调用模型。文件中的商店与产品条款全部为虚构测试数据。

在项目根目录运行，复制每条命令输出的真实 ID 替换后续命令中的占位符：

```powershell
python -m adaptive_rag --data-dir .demo-data ingest --tenant demo --document store-policy --version v1 --file examples/store-policy.md
python -m adaptive_rag --data-dir .demo-data publish --tenant demo --job <首个导入结果的job_id>
python -m adaptive_rag --data-dir .demo-data ingest --tenant demo --document a100-warranty --version v1 --file examples/sku-warranty.md --sku A100
python -m adaptive_rag --data-dir .demo-data publish --tenant demo --job <第二个导入结果的job_id> --expected-generation <首次发布的generation>
python -m adaptive_rag --data-dir .demo-data query --tenant demo --question "A100 保修规则是什么？" --sku A100
python -m adaptive_rag --data-dir .demo-data eval --tenant demo --fixture examples/eval.jsonl --output .demo-data/eval-result.json
python -m adaptive_rag --data-dir .demo-data capabilities
```

导入只暂存；发布后才能查询。第二次发布必须提供当前 generation，冲突时应读取状态后重新决策，不能盲目覆盖。正常命中返回 `evidence_preview`、`answer=null`，并可通过 `trace` 与 `evidence` 命令回溯证据。

评测只计算检索指标。带非空 gold 的题目纳入 Hit@K、Recall@K、MRR@K、NDCG@K；没有 gold 的题目单独统计 retrieval_empty_rate，这不是拒答质量评测。可以使用 `gold_document_ids` 或 `gold_chunk_ids`，同一道题不能混用。

CLI 是可信本地管理入口。浏览器登录使用本机设置的管理员或普通账号；Bearer Token 仅用于可选的 API 客户端。参见[本地工作台](../README.md#本地工作台)。不要提交真实口令或令牌。
