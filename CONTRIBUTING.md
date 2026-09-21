# 参与贡献

感谢你愿意改进评论商机雷达。

1. Fork 本仓库并创建功能分支。
2. 保持项目零第三方依赖，除非新能力确实需要。
3. 不要在代码、测试、Issue 或截图中提交真实 API Key 和私人数据。
4. 改完先跑一遍全部测试：`bash tools/run-tests.sh`。全部测试都不需要 API Key，也不产生费用。
5. 修改核心优先级逻辑时，请补充对应测试。
6. 提交 Pull Request，说明问题、改动与验证方式。

功能建议请优先描述真实工作流，而不只是新增一个按钮。

## 改动时注意这些约定

- **阈值逻辑只在 `app.py` 的 `priority_for()` 里**，前端只负责传值。阈值键名前后端共用，`tests/test_frontend_wiring.py` 会拦住两边不一致的情况。
- **前端的纯函数放 `web/core.js`**，不要塞进 `app.js`——`core.js` 会在 Node 里被测到，`app.js` 依赖 DOM 测不了。
- **新增元素 id 后**，`tests/test_frontend_wiring.py` 会检查脚本引用的 id 在页面上是否存在，反向的遗漏它拦不住，写的时候留意。
- **Jev 的请求/响应字段以官方文档为准**（<https://docs.typesafe.ai/api>）。要改 `QUESTIONS`，请同时更新 `tests/test_core.py` 的契约测试。
- **默认模型保持钉版本号**。官方说明别名会随新版本前移，用别名会让已校准的阈值静默失效。

