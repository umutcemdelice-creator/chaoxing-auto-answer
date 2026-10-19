# 学习通作业自动作答（Selenium + DeepSeek）

用 Selenium 驱动 Edge 打开学习通，自动找到未完成的作业，把每道题丢给 DeepSeek 拿答案，填好并提交。

- 支持 **单选 / 多选 / 判断 / 填空** 四种题型
- 有题没答好（比如简答、论述）**默认不提交**，避免交白卷
- `--dry-run` 只填不交，先看效果
- 答案带缓存，同一道题不重复请求；失败自动重试；答不上的题写进 `failed_questions.txt`

## 环境要求

- Python 3.10+
- [Edge 浏览器](https://www.microsoft.com/edge)（Selenium 4.6+ 会自动匹配驱动；想用 Chrome 就改 `build_driver()`）
- 一个 [DeepSeek](https://platform.deepseek.com) API Key

## 快速开始

```bash
git clone <你的仓库地址>
cd chaoxing-auto-answer

python -m venv .venv
.\.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

copy .env.example .env            # macOS/Linux: cp .env.example .env
# 打开 .env，填账号、Key、三个元素 id

python main.py --check-config     # 检查配置，不启动浏览器
python main.py --dry-run          # 只填不交，确认页面选择器还有效
python main.py                    # 正式跑
```

## 配置项

全部写在 `.env` 里（或设成同名环境变量），`.env` 已被 `.gitignore` 忽略。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `CHAOXING_USER` | ✅ | 学习通账号（手机号/超星号） |
| `CHAOXING_PASSWORD` | ✅ | 学习通密码 |
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek API Key，形如 `sk-...` |
| `CHAOXING_COURSE_ENTRY` | ✅ | 首页课程入口元素，形如 `#first123456` |
| `CHAOXING_COURSE_ID` | ✅ | 课程块 id，形如 `c_123456789` |
| `CHAOXING_COURSE_NAV` | ✅ | 课程页「作业」导航，形如 `#nav_123456 > a > i` |
| `CHAOXING_TIMEOUT` | | 显式等待秒数，默认 `20`，网慢就调大 |
| `CHAOXING_MAX` | | 单次最多处理几份作业，默认 `20` |
| `CHAOXING_HEADLESS` | | `1` = 无头模式，默认 `0` |
| `DEEPSEEK_MODEL` | | 默认 `deepseek-chat` |
| `DEEPSEEK_BASE_URL` | | 默认 `https://api.deepseek.com` |

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--check-config` | 只检查配置是否齐全，不启动浏览器 |
| `--dry-run` / `--no-submit` | 只填不提交，回车后关闭浏览器 |
| `--force-submit` | 有跳过的题（简答等）也照样提交 |
| `--max N` | 本次最多处理 N 份作业 |
| `--timeout N` | 覆盖显式等待秒数 |
| `--headless` | 无头模式 |
| `-v` / `--verbose` | 调试日志 |

## 怎么找那三个元素

只有第一次需要，之后写进 `.env` 就不用管了。

1. 浏览器登录 `i.chaoxing.com`，进入「课程」页
2. 按 `F12` 打开开发者工具，点左上角的 **选择元素** 箭头（或 `Ctrl+Shift+C`）
3. 鼠标点你想自动化的那门课，在 Elements 面板里往上找那个外层 `<div id="c_xxxxxxx">`——`c_xxxxxxx` 就是 `CHAOXING_COURSE_ID`
4. 回首页，用同样方式点第一门课的卡片，记下它的 `id`（形如 `first123456`），填进 `CHAOXING_COURSE_ENTRY`
5. 进课程页点「作业」，在作业列表页选中左侧「作业」图标所在元素，把它的选择器（形如 `#nav_123456 > a > i`）填进 `CHAOXING_COURSE_NAV`

> 上面出现的 id 全是**假的示例值**，每门课、每个账号都不一样；这三个都属于你自己的页面信息，不要写进代码、更不要提交到仓库。

## 目录结构

```
chaoxing-auto-answer/
├─ main.py                  # 主流程：登录 → 进课程 → 找作业 → 逐题作答 → 提交
├─ answer_engine.py         # 答案模块：DeepSeek 调用、答案归一化、缓存、重试
├─ tests/
│  └─ test_answer_engine.py # 纯函数测试（不需要网络和浏览器）
├─ .env.example             # 配置模板
├─ .gitignore
├─ requirements.txt
└─ README.md
```

## 工作原理

1. `login()` 填账号密码登录，等课程页 iframe 出现
2. `open_course()` 点课程入口 → 切 iframe → 点课程 → 切新窗口 → 点「作业」
3. `list_unfinished()` 每轮重新拉一次「未完成」列表（不缓存元素，避免过期）
4. 逐题作答：
   - `detect_type()` 按题干关键字判断题型
   - 选择/判断：**先在题目内部**找 `span[data="A"]` / `div[aria-label="A对选择"]`，找不到才退到全局下标兜底
   - 填空：找题目内部的 `iframe[id^=ueditor_]`，多空自动拆分答案分别填
5. 全部答好才提交；`_submit()` 点提交 → 点确认弹窗
6. 交完再查一次「未完成」列表，确认这份真的消失了，否则停下，**不会重复提交**

## 选择器失效怎么办

学习通改版、或你的课程页面结构不同，只需要改 `main.py` 顶部「一、页面选择器」那一段（`SEL_*` 常量），改完用 `--dry-run` 验证。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 一直停在登录页 | 账号密码错、或弹了验证码——手动登录一次再跑 |
| `没找到课程 c_xxx` | `CHAOXING_COURSE_ID` 写错，或该课程不在首页列表里 |
| 所有题都被跳过 | 题目容器选择器失效，多是 `SEL_QUESTION` 变了 |
| 答案填到了别的题上 | 先用 `--dry-run` 看；通常是选项定位退化到了全局兜底 |
| 提交按钮点不到 | `SEL_SUBMIT_BTN` / `SEL_SUBMIT_CONFIRM` 变了 |
| 想重新问一遍答案 | 删掉 `answer_cache.json` |
| 看不到报错细节 | 加 `-v`，日志同时写进 `run.log` |

## 隐私与安全

- 所有凭据只存在于 `.env`（已 gitignore），代码里没有任何硬编码账号、密码、Key
- 提交前建议 `git status` 看一眼，确认 `.env`、`run.log`、`answer_cache.json`、`failed_questions.txt` 都不在暂存区
- **Key 一旦泄露，立刻去平台重置**——它可以直接花你的额度

## 免责声明

仅供个人学习与自动化技术交流。使用前请确认符合你所在学校/机构的规定以及平台服务条款；因使用本工具造成的任何后果由使用者自行承担。
