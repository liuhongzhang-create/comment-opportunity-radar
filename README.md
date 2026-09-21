# 评论商机雷达

一个本地运行的开源工具：把 CSV 中的评论批量交给 TypeSafe Jev 分析，输出评论意图、购买意向、回复紧迫度和综合优先级。

![MIT License](https://img.shields.io/badge/license-MIT-d7ff35)
![Python](https://img.shields.io/badge/python-3.10%2B-62a8ff)

## 它能做什么

- 识别购买咨询、成交顾虑、内容需求、投诉和普通互动
- 给出购买意向分（0–4）
- 判断是否值得尽快人工回复
- 按高、中、低和人工复核排序
- 导出带分析结果的 CSV
- 单次处理最多 500 条评论
- API Key 不写入磁盘，不经过第三方服务器

> 这是独立的社区项目，并非 TypeSafe AI 官方产品。你需要使用自己的 TypeSafe API Key，调用会消耗你的 TypeSafe 额度。

## 快速开始

要求：Python 3.10 或更高版本。

```bash
git clone https://github.com/liuhongzhang-create/comment-opportunity-radar.git
cd comment-opportunity-radar
python3 app.py
```

浏览器打开：<http://127.0.0.1:8765>

然后：

1. 在 TypeSafe 控制台创建 API Key。
2. 在页面中填写 Key。
3. 上传 CSV，选择评论所在列。
4. 点击“开始分析”。
5. 检查结果并导出 CSV。

不需要安装任何第三方 Python 包。

## CSV 示例

```csv
用户,评论
小林,怎么买？可以发一下链接吗
阿杰,看着不错，就是有点担心售后
琪琪,能不能出一期新手完整教程
```

支持带引号、逗号和换行的标准 CSV。第一行必须是列名。

## 隐私与安全

- API Key 只存在于当前浏览器页面内存中，刷新后清除。
- Key 会从浏览器发送到本机 `127.0.0.1` 服务，再用于请求 TypeSafe 官方 API。
- 评论内容会发送给 TypeSafe 进行分析；请确保你有权处理这些数据。
- 不要上传包含不必要个人信息、密码、验证码或支付信息的数据。
- 不要把自己的 API Key 写进代码、截图或提交到 GitHub。

## 分析逻辑

每条评论会同时执行三个 Jev 判断：

1. `Choice`：主要商业意图
2. `Score`：购买意向等级
3. `Noul`：是否需要尽快回复

分类信心低于 45% 时会标记为“人工复核”。优先级规则位于 `app.py` 的 `priority_for()`，可以按自己的业务修改。

## 项目结构

```text
.
├── app.py           # 本地服务器与 TypeSafe API 调用
├── web/
│   ├── index.html   # 页面结构
│   ├── styles.css   # 界面样式
│   └── app.js       # CSV 解析、交互和导出
├── tests/
│   └── test_core.py
├── CONTRIBUTING.md
├── SECURITY.md
└── LICENSE
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试不会调用 TypeSafe API，也不会产生费用。

## 局限

- Jev 的结果可能不准确，高风险决策必须人工复核。
- 当前只支持 CSV，不支持直接读取抖音或其他平台账号。
- 当前是单机工具，不包含账号系统、云端存储或团队协作。
- 不建议用于招聘录用、借贷、医疗、法律等高风险自动决策。

## 许可证

[MIT](LICENSE)
