# 百炼云模型接口核对

核对日期：2026-11-15。本文记录官方文档中的协议与适配决策；实际密钥权限、地域、模型可用性和结果质量以项目 smoke 记录为准。本文不包含凭证。

## 已配置的三类模型

| 作用 | 模型 ID | 官方文档核对结果 |
| --- | --- | --- |
| 带证据的答案生成 | `deepseek-v4.1-flash-1017` | [DeepSeek API 文档][deepseek]列出该模型及 Chat Completions、结构化输出能力 |
| 文本向量化 | `qwen3.7-text-embedding-flash` | [同步向量接口文档][embedding]列出该模型，默认 1024 维，每批最多 20 条文本 |
| 候选文档重排 | `qwen3.7-text-rerank` | [重排 API 文档][rerank]列出该模型及原生 input/parameters 协议 |

当前不接入 Laya；规划与路由继续保留现有可替换接口。

## 北京地域请求地址

[官方 Base URL][base-url]说明保留原有 DashScope 共享域名的兼容性，并推荐生产环境使用工作空间独立域名。共享域名可用于当前接入验证；[API Key][get-key]、模型服务地域及计费方案必须匹配，不能假定跨地域通用。

| 调用 | 地址 |
| --- | --- |
| Chat Completions | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` |
| Embeddings | `https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings` |
| 原生重排 | `https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank` |

[重排兼容接口文档][rerank]另列 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks`。其前缀是 `compatible-api`，末尾是复数 `reranks`，不能把它拼成 Chat 使用的 `/compatible-mode/v1/rerank`。本项目百炼默认选择官方原生重排接口。

## 配置约定

调用方显式向 `load_cloud_settings(mapping)` 提供以下变量；适配模块不会读取文件或自动发现凭证：

```dotenv
RAG_CLOUD_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
RAG_CLOUD_API_KEY=<local-secret>
RAG_LLM_MODEL=deepseek-v4.1-flash-1017
RAG_EMBEDDING_MODEL=qwen3.7-text-embedding-flash
RAG_RERANK_MODEL=qwen3.7-text-rerank
```

当显式 Base URL 属于已识别的 DashScope 共享域或百炼 `.maas.aliyuncs.com` 工作空间域时，loader 选择同一主机 `/api/v1` 的原生重排协议，并为 Chat 请求设置 `enable_thinking: false`。该选择依据配置中的主机名，不依据 API Key 或模型名猜测服务商。

需要对接其他兼容服务时，可显式覆盖 `RAG_RERANK_BASE_URL`、`RAG_RERANK_PATH`、`RAG_RERANK_FORMAT`（`flat` 或 `dashscope`），以及 `RAG_CHAT_PATH`、`RAG_EMBEDDINGS_PATH`。服务地址要求 HTTPS；仅本机开发地址允许 HTTP。不会跟随重定向。

## 线协议细节

向量请求使用 `{model, input, encoding_format: "float"}`，按返回的 `index` 恢复输入顺序。项目默认每批 20 条，遵守[当前 flash 模型的限制][embedding]。默认不传 `dimensions`；官方表格列出 1024/768/512/256 维，但页面部分参数说明与表格存在范围表述差异，因此本阶段先验证默认维度。模型或维度改变时，派生向量缓存分命名空间，避免混用。

原生重排请求采用以下结构：

```json
{
  "model": "qwen3.7-text-rerank",
  "input": {"query": "问题", "documents": ["候选文本"]},
  "parameters": {
    "top_n": 1,
    "instruct": "Given a web search query, retrieve relevant passages that answer the query."
  }
}
```

根据[重排 API][rerank]，响应从 `output.results` 提取 `index` 和 `relevance_score`。不向当前模型发送未明确支持的 `return_documents`。重排分数只表达同次查询下的相关性，不能视为事实正确率，也不能直接用于跨请求概率比较。官方原生错误的 `code/message` 不写入用户可见异常。

生成请求要求 JSON 对象，仅允许包含有已知证据引用的 `claims`。答案由经过结构检查的 claims 渲染；无证据或空 claims 时不生成答案。引用存在性检查不等于语义蕴含证明，后续仍需要带人工标注的答案质量评测。

## 验证范围与部署边界

离线协议测试使用 `httpx.MockTransport`，不需要凭证，不访问网络。覆盖北京与工作空间根地址、原生重排请求/响应、20 条分批、独立请求计量、鉴权错误及重定向不重试、429/5xx 最多重试一次、超时不重试、畸形向量及未知引用拒绝。2026-11-15 本地运行 `python -m unittest discover -s tests -p test_cloud.py -v`：21 项通过；`compileall` 通过。此结果仅验证程序协议，不证明账号权限或在线质量。

当前 dense 插件是对调用方已授权 chunks 的精确余弦检索，SQLite 只保存派生向量缓存；它不是生产 ANN 向量库。单次冷检索默认最多补建 2048 个 chunk 向量。生产索引可通过相同检索接口替换，不能把当前缓存等同于已经接入独立向量中间件。

## 官方来源

- [Base URL](https://help.aliyun.com/zh/model-studio/base-url)：共享域名兼容性、工作空间与地域根地址。
- [获取 API Key](https://help.aliyun.com/zh/model-studio/get-api-key)：地域及服务权限。
- [DeepSeek API](https://help.aliyun.com/zh/model-studio/deepseek-api)：模型 ID、Chat、结构化输出、思考参数。
- [文本向量同步 API](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)：向量批量、维度及响应格式。
- [文本重排 API](https://help.aliyun.com/zh/model-studio/text-rerank-api)：原生与兼容接口、参数和 `output.results`。
- [模型列表](https://help.aliyun.com/zh/model-studio/models)：模型服务能力列表。

[base-url]: https://help.aliyun.com/zh/model-studio/base-url
[get-key]: https://help.aliyun.com/zh/model-studio/get-api-key
[deepseek]: https://help.aliyun.com/zh/model-studio/deepseek-api
[embedding]: https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api
[rerank]: https://help.aliyun.com/zh/model-studio/text-rerank-api
