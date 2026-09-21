# 变更记录

## 0.2.0 — 修复可用性缺陷

### 修复

- **导出丢失用户身份** —— 导出文件现在带出原始 CSV 的全部列（昵称、用户 ID、主页链接等），并附上原 CSV 行号，可以回到原文件核对，也能直接找到人私信。（`web/core.js`、`web/app.js`）
- **行号错位** —— 原实现先按评论列取值再做 `.filter(Boolean)`，一旦有空白评论行，后续结果的序号就会整体前移，补上身份列后会张冠李戴。现在改为传递 `{text, source_index}`，索引全程不丢。（`web/app.js`、`app.py`）
- **阈值硬编码不可调** —— 五个阈值改为按请求传入并在服务端做边界校验，界面上有「高级设置」面板，保存在浏览器本地。（`app.py` 的 `resolve_thresholds` / `resolve_options`）
- **模型别名未固定** —— 默认模型从浮动别名 `jev-latest` 改为钉住版本 `jev-1.13.0`。官方说明别名会随新版本前移，调过阈值后继续用别名会让阈值静默失效。
- **CSV 公式注入** —— 导出时对以 `=`、`+`、`-`、`@`、制表符、回车开头的文本单元格加前导单引号，防止在 Excel 中作为公式执行；纯数字不受影响。（`web/core.js` 的 `guardFormula`）
- **BOM 与中文编码** —— 读入 CSV 时剥离 UTF-8 BOM，并新增 UTF-16 与 GBK 回退，中文版 Excel 导出的文件不再乱码。（`web/core.js` 的 `decodeCsvBuffer`）
- **无代理配置** —— 新增 `RADAR_PROXY`，需要代理才能访问 TypeSafe 的网络环境可以直接配置；`direct` 可强制不走代理。
- **黑盒等待** —— 分析改为流式返回，界面显示进度条与已用时，不必等几分钟看白屏。

### 新增

- `tools/calibrate.py`：用带标注的 CSV 实测意图准确率，输出混淆矩阵、每类精确率/召回率、置信度阈值扫描与判错样本清单，并推荐 `review_confidence`。
- `tools/sample_comments.csv`：24 条带标注的种子样本（含反话与阴阳怪气），用于验证校准流程。
- 界面上「检查 Key」按钮：调用免费的 `GET /v1/models` 验证 Key，不消耗分析额度。
- 界面上「重试失败项」按钮：只重跑失败的行。
- `samples/comments.csv`：可直接拖入页面的示例数据。
- `GET /api/config`：把模型、阈值默认值与边界暴露给前端，避免前后端各写一份常量。
- 路由表新增 `/api/verify`、`/api/config`。

### 测试

- 从 5 个用例扩展到 **106 个**，且全部不依赖真实 API Key、不产生费用：
  - `tests/test_core.py`：44 项。新增 `normalize_result` 契约测试、阈值边界与非法输入、请求体字段级契约、重试策略分类、错误信息提取。
  - `tests/test_end_to_end.py`：21 项。启动真实服务 + 契约级假上游，覆盖参数校验、稀疏 `source_index`、流式事件序列、失败行隔离、529 退避重试、路径穿越防护、密钥不泄露。
  - `tests/test_frontend_wiring.py`：14 项。静态校验脚本引用的元素 id 是否存在于页面、`core.js` 是否先于 `app.js` 加载、前端阈值键与后端 `THRESHOLD_BOUNDS` 是否一致。
  - `tests/web_core.test.cjs`：27 项。CSV 往返、公式注入防护、GBK/UTF-16/BOM 解码、导出矩阵与行号映射、排序稳定性。
- `tests/fake_typesafe.py`：严格按官方响应契约实现的假上游，支持故障注入（`[unsure]` / `[reject]` / `[fail]` / `[retry]` / `[slow]`）。
- `tools/run-tests.sh`：一键跑全部测试。

### 文档

- README 重写：把中文准确率风险、置信度的选项数归一化含义（5 选项下 0.45 ≈ 首选概率 56%）、阈值判定顺序、模型钉版本的原因、代理与编码支持写成独立章节。

## 0.1.0 — 首个开源版本

- 本地服务 + 单页面界面，导入 CSV 后调用 TypeSafe Jev 判定意图、购买意向与回复紧迫度，按优先级排序并导出 CSV。
