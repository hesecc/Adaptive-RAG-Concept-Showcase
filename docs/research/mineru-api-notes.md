# PDF 解析适配器协议核验

核验日期：2026-11-15。此文记录官方协议与本项目实现选择，不是解析质量实测报告。

## 已核验的官方事实

- pypdf 官方仓库明确发布 `pip install pypdf`、`from pypdf import PdfReader` 以及 `page.extract_text()`。它提取 PDF 内嵌文本；不应把此能力当作 OCR 或公式、表格语义恢复。[pypdf README](https://github.com/py-pdf/pypdf/blob/main/README.md)；[官方文本提取说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。
- MinerU 官方开源仓库指向 `mineru.net` 在线服务；开源自托管 4.x `/v1/tasks` 与在线精准解析 `/api/v4` 是不同协议，不能混用。[MinerU README](https://github.com/opendatalab/MinerU/blob/master/README.md)。
- 在线精准解析的本地文件流程：以 Bearer Token 调用 `POST https://mineru.net/api/v4/file-urls/batch`，取得 `data.batch_id` 与 `data.file_urls`；对签名 URL `PUT` 文件，上传完成自动提交，无须再次发起解析任务。官方说明上传不需要 `Content-Type`。[在线接口文档](https://mineru.net/apiManage/docs)。
- 申请体支持 `files: [{name, data_id}]`、`model_version`、`enable_formula`、`enable_table` 与 `language`。PDF 的模型选项为 `pipeline` 和 `vlm`，文档推荐 `vlm`；`enable_formula` 对 VLM 主要影响行内公式。请求开启某能力不证明任意文档上的识别正确率。[在线接口文档](https://mineru.net/apiManage/docs)。
- 查询 `GET /api/v4/extract-results/batch/{batch_id}` 得到 `data.extract_result[]`。状态含 `waiting-file`、`pending`、`running`、`converting`、`done`、`failed`；成功项的 `full_zip_url` 指向含 `full.md` 的结果压缩包。`extract_progress.total_pages` 只保证运行状态下可用。[在线接口文档](https://mineru.net/apiManage/docs)。

## 本项目实现与约束

`adaptive_rag/pdf.py` 提供相同返回契约的 `LocalPdfParser` 与 `MinerUPdfParser`，以及 `parse_pdf(path, backend, settings)` 入口。`ParsedPdf` 含 Markdown、源 PDF 页数、后端、耗时和去密元数据；模块不读取 `.env`，调用者显式传入 `PdfSettings`。

- 本地适配器用 pypdf layout 文本提取，并添加逐页标题，保留阅读顺序的能力应以原页面抽检判断；不声称 OCR、公式或表格结构恢复。全空文字层会返回明确的 OCR 能力错误。
- MinerU 上传是显式选择。只有申请与轮询带 API Bearer；签名上传和结果下载不带 API Bearer。请求不跟随重定向，不记录密钥、提供方错误正文或签名 URL。
- 远端 URL 必须为 HTTPS，拒绝用户信息、非 443 端口、直接的内网 IP、本地主机；签名 URL 还必须位于允许的存储域。当前默认域为 `aliyuncs.com`、`openxlab.org.cn`、`mineru.net`、`shlab.tech`，新域必须先核验来源后配置，不能关闭校验。该白名单不是网络级 DNS rebinding 防护，生产环境仍应限制出站流量。
- 默认最大 200 页、50 MB 输入、80 MB 结果下载、16 MB Markdown，实际可调。云端轮询共享总时间预算；HTTP 单次超时也受剩余预算约束。同步 PDF 解码和底层请求不提供可强杀的硬墙钟保证。
- ZIP 只在内存读取唯一 `full.md`，不解压文件到磁盘、不下载图片、不执行 Markdown 内 HTML。拒绝路径穿越、绝对路径、符号链接、重复条目、加密条目，限制条目数量与文字大小。
- 不自动重试任务提交。超时或网络失败发生在上传后时，远端任务可能仍在运行；后续重试属于新的提交，不承诺远端 exactly-once，也不把内容哈希当作平台已保证的幂等键。
- 数据 ID 使用源文件 SHA256，返回页数从本地源 PDF 结构取得。开启公式与表格是解析请求参数，不是质量评分。真实效果必须用同一源文件与人工核对样本比较。

## 可验证范围

单元测试通过 HTTPX MockTransport 验证请求格式、凭据隔离、轮询、错误去密、超时、归档安全及本地 PDF 读取。它们证明协议实现的行为，不证明云端可用性、账户配额或真实 PDF 质量；这些需另行实测。
