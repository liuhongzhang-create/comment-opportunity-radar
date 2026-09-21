# 评论商机雷达

一个本地运行的开源工具：把 CSV 中的评论批量交给 TypeSafe Jev 分析，输出评论意图、购买意向、回复紧迫度和综合优先级，**并把每条评论的原始身份信息（昵称、ID、主页链接）一起导出**，让"筛出意向人群"这件事能真正闭环到私信。

![MIT License](https://img.shields.io/badge/license-MIT-d7ff35)
![Python](https://img.shields.io/badge/python-3.10%2B-62a8ff)

## 它能做什么

- 识别购买咨询、成交顾虑、内容需求、投诉和普通互动
- 给出购买意向分（0–4）
- 判断是否值得尽快人工回复
- 按高、中、低和人工复核排序
- **导出时带出原始 CSV 的全部列**，包括用户昵称 / 用户 ID / 主页链接，并附上原 CSV 行号
- **阈值可在界面上调整**，不必改代码
- 边跑边出结果（流式进度），失败的行可以单独重试
- 单次处理最多 500 条评论，API Key 不写入磁盘

> 这是独立的社区项目，并非 TypeSafe AI 官方产品。你需要使用自己的 TypeSafe API Key，调用会消耗你的 TypeSafe 额度。

---

## ⚠️ 先用 100–200 条真实评论验证中文准确率，再决定是否依赖它

这不是免责声明，是使用前提。TypeSafe 官方文档（<https://docs.typesafe.ai/models#language-support>）原话是：

> English is the primary training language and where accuracy is currently best. Other languages, including CJK scripts, are handled but not equally well; **test on your own content before relying on Jev for a non-English workload**.

同一份文档还说明 Jev 会**按字面理解**，这让中文评论区最常见的反话和阴阳怪气成为高危区。例如：

| 评论 | 真实意图 | 只按字面理解的后果 |
| --- | --- | --- |
| 呵呵，售后真棒，等了半个月才回我一句 | 投诉 | 可能被判成"普通互动"或"内容需求" |
| 好的呢，说好的七十二小时发货，现在第七十二天了好开心 | 投诉 | 同上 |

所以流程应该是：**先采样标注 → 跑校准脚本 → 按结果调阈值或补充 `instructions`**，而不是导入 500 条就直接照着排序去私信。

### 校准你的数据

```bash
export TYPESAFE_API_KEY=sk-...
python3 tools/calibrate.py --csv tools/sample_comments.csv
```

脚本会给出：意图准确率、混淆矩阵、每类的精确率/召回率、**置信度阈值扫描**（告诉你在多少覆盖率下能保住多少准确率，并直接推荐一个 `review_confidence`）、优先级准确率，以及判错样本清单——判错清单是用来改 `app.py` 里 `QUESTIONS` 的 `instructions` 的。

`tools/sample_comments.csv` 是 24 条带标注的种子样本，只用于确认流程能跑通。**它不能替代你自己账号下的真实评论。**

### 别拿它当自动决策工具

Jev 的输出是概率，不是结论。招聘录用、借贷、医疗、法律这类高风险判断不要交给它，本项目也不为这些用途设计。

---

## 快速开始

要求：Python 3.10 或更高版本，无需安装任何第三方包。

```bash
git clone https://github.com/liuhongzhang-create/comment-opportunity-radar.git
cd comment-opportunity-radar
python3 app.py
```

浏览器打开 <http://127.0.0.1:8765>，然后：

1. 在 [TypeSafe 控制台](https://console.typesafe.ai/keys) 创建 API Key（早期访问阶段可能需要先加入候补名单）。
2. 把 Key 填进页面，点 **检查 Key**——它会调用免费的 `GET /v1/models` 确认 Key 有效，不消耗分析额度。
3. 拖入 CSV，选择评论所在列。
4. 点 **开始分析**。结果边跑边出。
5. 点 **导出 CSV**，导出文件里每一行都带回了原始的身份列。

## CSV 格式

第一行必须是列名，评论列可以自动识别，其余列会被完整保留到导出文件里：

```csv
用户ID,昵称,评论
10001,小林,怎么买？可以发一下链接吗
10002,阿杰,看着不错，就是有点担心售后
```

支持标准 CSV（带引号、逗号、字段内换行）。**编码支持 UTF-8、带 BOM 的 UTF-8、UTF-16（Excel 另存为）以及 GBK（中文版 Excel 默认导出）**——GBK 文件不会再出现乱码。

`samples/comments.csv` 是一个可以直接拖进页面的示例。

---

## 高级设置（模型与阈值）

界面上的「高级设置」面板对应下面这些值，保存在本机浏览器里（API Key 不保存）。

### 阈值含义

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `review_confidence` | 0.45 | 意图分类置信度低于此值 → 标记为**人工复核**，不参与自动排序 |
| `high_purchase_score` | 2.5 | `购买咨询` / `投诉` 类，购买意向分达到此值 → **高** |
| `high_urgent` | 0.65 | `购买咨询` / `投诉` 类，尽快回复概率达到此值 → **高** |
| `medium_purchase_score` | 1.5 | 购买意向分达到此值 → **中** |
| `medium_urgent` | 0.45 | 尽快回复概率达到此值 → **中** |

判定顺序（见 `app.py` 的 `priority_for()`）：

1. 置信度低于 `review_confidence` → `人工复核`
2. 意图是 `purchase` 或 `complaint`，且（购买分 ≥ `high_purchase_score` 或 紧急度 ≥ `high_urgent`）→ `高`
3. 意图是 `objection` 或 `content_request`，或 购买分 ≥ `medium_purchase_score`，或 紧急度 ≥ `medium_urgent` → `中`
4. 其余 → `低`

### 置信度是怎么算的：选项越多，同一个数字含义越不同

官方文档（<https://docs.typesafe.ai/confidence>）说明 `confidence` 由概率分布的形状折算而来——全部概率集中在一个选项是 1.0，越平均越低。文档给出的三选项近似公式是 `(3 × 最大概率 − 1) / 2`，一般化成 N 个选项：

```
confidence = (N × p_max − 1) / (N − 1)
```

本项目的意图题有 **5 个选项**，所以：

```
0.45 ≈ 要求首选选项的概率至少 56%
```

也就是说 0.45 并不算宽松。官方建议的"模型真的不确定"下限是 **0.5**，本项目保留 0.45 是为了不改变原有行为，你可以按校准结果调整。**如果你改了题目选项数量，这个阈值的含义会跟着变，必须重新校准。**

### 模型：为什么钉在版本号上

官方明确说明：别名会在新版本发布时自动前移，**如果你针对某个版本调过阈值，就应该钉版本号 ID，而不是用别名**，否则模型升级后阈值会静默失效，而你的结果会和之前不可比。

所以默认值是 `jev-1.13.0`（`jev-latest` 当前指向的版本），不是 `jev-latest`。想跟随最新版就把模型 ID 改成 `jev-latest`，但请重新校准。

每次请求的响应里都会带回实际应答的模型 ID，界面会显示 `实际使用 jev-1.13.0`，便于事后核对。

---

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RADAR_HOST` | `127.0.0.1` | 监听地址，不建议改成公网地址 |
| `RADAR_PORT` | `8765` | 监听端口 |
| `RADAR_MODEL` | `jev-1.13.0` | 默认模型 ID |
| `RADAR_API_BASE` | `https://api.typesafe.ai` | 上游地址，测试用 |
| `RADAR_PROXY` | 未设置 | 出站代理，如 `http://127.0.0.1:7897`；填 `direct` 强制不走代理 |

不设置 `RADAR_PROXY` 时，走 Python 标准的代理环境变量和 macOS 系统代理设置。**如果你所在网络需要代理才能访问 TypeSafe，就设置 `RADAR_PROXY`。**

---

## 分析逻辑

每条评论会同时执行三个 Jev 判断（一次请求，三个问题并行评估）：

1. `Choice` —— 主要商业意图（5 选 1，返回 `choice` / `probabilities` / `confidence`）
2. `Score` —— 购买意向等级（5 档，返回概率加权后的 `score`，可以落在两档之间）
3. `Noul` —— 是否需要尽快回复（返回 0–1 的概率；官方规定 Noul 不带 `confidence`）

请求体和响应解析与官方 API 参考逐字段核对过（含 `noul` 的 `criteria` 字段，这是合法可选字段），并且有契约测试锁住，见 `tests/test_core.py` 的 `RequestContractTests`。

成本参考：输入 $0.042 / 百万 token，输出免费，每个评论约占 300 token。**500 条约 $0.0063。**

---

## 测试

```bash
bash tools/run-tests.sh
```

或者分开跑：

```bash
python3 -m unittest discover -s tests -v   # 后端：单元 + 契约 + 端到端
node --test tests/web_core.test.cjs        # 前端纯函数
```

**全部测试不需要 API Key，也不产生任何费用。** 端到端测试会启动一个严格遵循官方响应契约的假上游（`tests/fake_typesafe.py`），通过 `RADAR_API_BASE` 指过去，覆盖了流式进度、失败行隔离、529 退避重试、路径穿越防护和密钥不泄露等场景。

---

## 项目结构

```text
.
├── app.py                       # 本地服务器、Jev 调用、优先级判定
├── web/
│   ├── index.html               # 页面结构
│   ├── styles.css               # 界面样式
│   ├── core.js                  # 纯函数：CSV 解析/编码、导出、排序（可被 Node 测试）
│   └── app.js                   # 交互、流式进度、设置面板
├── tools/
│   ├── calibrate.py             # 用你自己的标注数据校准阈值
│   ├── sample_comments.csv      # 24 条带标注的种子样本
│   └── run-tests.sh             # 一键跑全部测试
├── samples/
│   └── comments.csv             # 可直接拖进页面的示例数据
├── tests/
│   ├── test_core.py             # 单元 + API 契约测试
│   ├── test_end_to_end.py       # 起真服务 + 假上游的端到端测试
│   ├── test_frontend_wiring.py  # 前后端接线与跨语言常量一致性
│   ├── web_core.test.cjs        # 前端纯函数测试
│   └── fake_typesafe.py         # 契约级假上游
├── CONTRIBUTING.md
├── SECURITY.md
├── CHANGELOG.md
└── LICENSE
```

## 隐私与安全

- API Key 只存在于当前浏览器页面内存中，刷新即清除；不写入磁盘，也不出现在服务端日志里。
- Key 会从浏览器发送到本机 `127.0.0.1` 服务，再用于请求 TypeSafe 官方 API。
- 服务默认只监听 `127.0.0.1`，并带路径穿越防护；静态文件响应带 `nosniff`。
- 评论内容会发送给 TypeSafe 进行分析；请确保你有权处理这些数据。官方说明不会用客户请求训练模型。
- 导出的 CSV 会对以 `=`、`+`、`-`、`@`、制表符开头的单元格加前导单引号，防止在 Excel 里被当作公式执行。
- 不要上传包含不必要个人信息、密码、验证码或支付信息的数据。
- 不要把自己的 API Key 写进代码、截图或提交到 GitHub。

## 局限

- **中文准确率低于英文**，反话和阴阳怪气尤其容易判错——必须先用自己的数据校准。
- 工具会告诉你"这条评论可能想买"，但**不会替你判断这个人值不值得投入**。
- 只支持 CSV，不支持直接读取抖音/小红书等平台的账号数据。
- 是单机工具，不含账号系统、云端存储或团队协作。
- 阈值是经验值，校准脚本给的是在你样本上的最优解，样本分布变了要重新校准。

## 许可证

[MIT](LICENSE)
