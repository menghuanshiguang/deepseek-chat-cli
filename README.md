# dsc — DeepSeek 网页版对话 CLI（纯文本对话版）

参考 [menghuanshiguang/deepseek-vision-cli](https://github.com/menghuanshiguang/deepseek-vision-cli) 的登录与会话管理设计，把「识图 CLI」改成「对话 CLI」：

- 你输入一句提示词，`dsc` 驱动浏览器打开 DeepSeek 网页版
- 新建对话 → 发送提示词 → 等待网页端流式回答完成 → 把回答纯文本输出到 stdout
- 默认**用后即删**：对话结束立即删除本次会话，不污染对话列表
- 登录采用与参考项目相同的异步设计：`--login` 发短信后立即退出，`--verify` 完成登录并存 token

> **已适配最新官网前端**（基于 `chat.deepseek.com` 的 `commit-id dda740b5` 逆向 `main.js` / `main.css` 校验；
> **2026-09-10 对最新 `main.5748b4eb39` 真机端到端复测，修复 4 处回归**，详见文末）。
> 本次修复的关键回归与现代站点契约见文末「[适配说明](#适配说明)」与「[选择器契约](#选择器契约)」。

---

## 安装

```bash
# 克隆本项目
git clone <your-repo-url>
cd deepseek-chat-cli

# 安装依赖
pip install -r requirements.txt

# Linux / GitHub Actions 需要安装 Chromium 浏览器
python -m playwright install --with-deps chromium

# Windows 不需要下载浏览器：驱动系统 Edge/Chrome
```

> - `dsc.py` 自身**只用 Python 标准库**；`playwright` 仅作为连接浏览器的 CDP 客户端。
> - 在 iSH / OpenMinis 等环境会自动使用系统自带的 `minis-browser-use`，无需额外安装。
> - 旧版为破解数美图片点选题型依赖的 `opencv-python-headless / numpy / pillow / scipy` **已移除**（该题型已下线）。

## 使用

```bash
# 1. 登录（发短信后立即退出，不阻塞）
python dsc.py --login 13800138000

# 2. 收到短信后，用验证码完成登录
python dsc.py --verify 123456

# 3. 对话：输入提示词，网页返回结果后结束
python dsc.py "用一句话介绍你自己"

# 也支持管道输入
echo "你好" | python dsc.py

# 保留本次会话（默认会删除）
python dsc.py --keep "帮我写一首诗"

# 清除本地登录 token
python dsc.py --logout

# 帮助
python dsc.py --help
```

### 风控验证（重要）

最新登录页在「发送验证码」时会触发风控：

| 地区 | 验证方式 |
|---|---|
| 中国大陆 | 数美 v1.0.4 滑块（`smcp.min.js`） |
| 海外 | hCaptcha |

这类验证**无法可靠地用脚本自动化**（旧版的「点击图中XX」图片点选题型已彻底下线，原 OpenCV 解题逻辑作废）。
`dsc` 现在会：

1. 自动尝试交互一次；
2. 失败时给出明确提示，而不是静默卡住；
3. 支持人工介入 —— 用有头浏览器手动完成验证：

```bash
# Windows
set DSC_HEADFUL=1 && python dsc.py --login 13800138000
# 或
python dsc.py --headful --login 13800138000

# Linux / macOS
DSC_HEADFUL=1 python dsc.py --login 13800138000
```

有头窗口里手动拖动滑块/完成验证后，短信即会发出；后续 `--verify` 可照常无头运行。

### 环境变量

| 变量 | 作用 |
|---|---|
| `DSC_TIMEOUT` | 回答等待上限秒数（默认 300） |
| `DSC_HEADFUL=1` | 用有头浏览器（人工过风控 / 观察过程） |
| `DSV_DEBUG=1` | 打印等待轮询细节，便于排查选择器 |
| `DSV_EDGE` | 指定 Edge/Chrome 可执行文件路径 |
| `DSV_EDGE_PROFILE` | Windows 浏览器 profile 目录（默认 `~/.dsv_edge_profile`） |
| `DSV_CHROME_PROFILE` | Linux 浏览器 profile 目录（默认 `~/.dsc_chrome_profile`） |

## GitHub Actions 直接运行

仓库已包含 `.github/workflows/dsc.yml`，可以在 GitHub Actions 上直接运行浏览器自动化。

### 1. 发短信登录

在 Actions 页面选择 **Run workflow**，填写：

- `phone`：`15183076179`
- 其它留空

运行后会给该手机号发送 DeepSeek 登录短信。

> Actions 里是无头 Chromium，若触发滑块风控会失败（见上文「风控验证」）。
> 国内手机号建议在本地用 `DSC_HEADFUL=1` 完成一次登录，把 token 存为 secret 后只跑对话。

### 2. 用验证码完成登录

再次 **Run workflow**，填写：

- `phone`：同一个手机号（用于恢复上一次的浏览器登录态）
- `code`：收到的短信验证码

运行成功后会保存登录 token 到 runner 缓存，并上传一个名为 `dsv-token` 的 artifact，里面就是 `~/.dsv_token` 文件内容。下载后把它填到仓库 secret `DSV_TOKEN` 里，之后就可以直接对话。

### 3. 直接对话

在仓库 Settings → Secrets and variables → Actions 里添加 secret：

- `DSV_TOKEN`：DeepSeek 网页版登录后的 token（可以用本地 `dsc --verify` 成功后从 `~/.dsv_token` 获取）

然后 **Run workflow**，填写：

- `prompt`：你想问的内容，例如 `用一句话介绍你自己`
- 可选 `keep`：保留本次会话

> 登录和对话可以分开跑：登录时用手机号+验证码，对话时用 `DSV_TOKEN` secret，不需要每次都在 Actions 里登录。

## 退出码契约

| 退出码 | 含义 |
|---|---|
| 0 | 成功，stdout 为回答文本 |
| 1 | 用法错误（没有提示词）/ `--verify` 失败 |
| 2 | 参数缺失（`--login` / `--verify` 未给值） |
| 3 | 短信发送失败（风控未通过 / 未找到输入框，等 30s 重试） |
| 4 | token 无效/缺失 → 快速失败，提示先登录 |
| 5 | 并发冲突（另一个 dsc/dsv 进程在跑） |

## 工作原理

```
dsc "<prompt>"
  │
  ├─ 1. 登录态注入（Authorization: Bearer <token>，持久化于 ~/.dsv_token）
  ├─ 2. 新建对话
  ├─ 3. 输入提示词并发送
  ├─ 4. 流式读取回答（轮询网页端回复完成标志）
  └─ 5. 用后即删（自动删除本次会话，对话列表零污染）
```

## 适配说明

针对最新官网前端（`commit-id dda740b5`）修复了以下**会导致完全不可用**的回归：

| # | 位置 | 旧实现 | 最新官网现状 | 修复 |
|---|---|---|---|---|
| 1 | `get_token()` | 校验 `data.biz_code == 0` | 统一外壳改为 `{"code":…,"msg":…,"data":…}`，无 token 返回 `40002 Missing Token` / `40003 Authorization Failed` | 改判顶层 `code == 0`（**此前所有 token 一律判无效，`dsc` 永远 exit 4，从不发消息**） |
| 2 | `do_login()` | `placeholder === '请输入手机号'` | 账号输入框改为 `请输入手机号/邮箱地址`（英文 `Phone number / email address`） | 改为「候选文案列表 + 精确/包含/aria-label/可见性」多级回退查找 |
| 3 | `do_login()` | 登录页单表单 | 改为「验证码登录 / 密码登录」双 Tab | 发码前先切到验证码 Tab |
| 4 | `do_login()` | OpenCV 破「点击图中XX」 | 该题型已下线；改为数美 v1.0.4 滑块 / hCaptcha | 删除 OpenCV 逻辑，改为探测 + 尝试交互 + 明确提示 + `--headful` 人工过验证 |
| 5 | `delete_session()` | `{"chat_session_id": id}`（单数） | 接口收 `{"chat_session_ids": [...]}`（数组） | 改传数组（**用后即删此前静默失败**） |
| 6 | `send_and_wait()` | 在 `closest('div[class*=input]')` 内按 `ds-button--primary` 找发送键 | `ds-button--primary` 只存在于 CSS，JS 里 0 命中（类名运行时组合） | 改为**结构性定位**：在输入框同一行区域内、类名含 `button`、未被禁用的候选中就近取最小者 |
| 7 | `get_current_session_id()` | 硬编码 `len(sid) == 36` | 路由 `/a/:agentId/s/:sessionId` | 正则 `/a/<agent>/s/<id>`，不写死长度 |
| 8 | 锁文件 | 用 `/proc/<pid>` 判存活 | Windows 无 `/proc` | Windows 走 `tasklist`，避免锁被误判为残留 |
| 9 | 文案 | 仅中文 | 站点按地区渲染 zh_CN / en_US | 关键文案全部中英双语匹配 |
| 10 | 依赖 | `opencv-python-headless` 等 4 个重依赖 | 解题逻辑已废 | 移除，`dsc.py` 仅用标准库 |

**未变（无需改）**：所有 API 路径、`Authorization: Bearer` 认证、`localStorage['userToken']` 为 `{"value":…,"__version":"0"}` JSON 包装（注：写入必须带 `__version` 包装，裸字符串会被站点 `JSON.parse` 回退成 `null`，永远卡 `/sign_in`）、
回答容器 `.ds-markdown`、输入框仍是 `<textarea>`、`Enter` 发送 / `Shift+Enter` 换行、
停止按钮 tooltip `停止生成`、`biz_code` 仍用于 `create_pow_challenge` 等业务接口。

### 2026-09-10 真机复测修复（iSH + minis-browser-use）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| 1 | `ensure_login()` | token 按**裸字符串**写入 localStorage；站点 storage 层读取时 `JSON.parse(...).value`，解析失败回退 `null` → 永远卡 `/sign_in`，**完全无法对话** | 改回 `{"value": token, "__version": "0"}` JSON 包装 |
| 2 | `send_and_wait()` 发送键 | 「最小面积」结构法被按钮**内部图标层**（`ds-button__icon`，面积更小）截胡 → 点到隔壁胶囊按钮，消息根本发不出去、等回答超时 | ① 排除按钮内部嵌套层（有 `ds-button` 祖先）② 优先精确 class token `ds-button--primary`（实测即发送键） |
| 3 | `js()` 的 `clean()` | 空返回值实际尾巴为 `"  tab_id: 0"`（**无换行前缀**），旧正则漏匹配 → 假错误串 `tab_id: 0` 被当作「拒绝处理」误报 | 正则改为 `\s*tab_id:\s*\d+\s*$` |
| 4 | `main()` 管道输入 | `echo xx \| dsc` 永远走不到 stdin 分支（无参数时先被当成「显示帮助」return）→ 管道模式死代码 | 无参数时优先读 stdin；子命令分发改 `sub = args[0] if args else None` 防越界 |

验证方式：`dsc "<提示词>"` / `echo … | dsc` / `--keep` 三种入口端到端通过（登录注入 → 发消息 → 流式回答 → 用后即删）。

## 选择器契约

站点改版时**优先只改 `dsc.py` 顶部这一段**：

```python
SEL_MARKDOWN = ".ds-markdown"      # 消息正文容器
SEL_TEXTAREA = "textarea"          # 输入框(实为 <textarea class="ds-textarea__textarea">)
PH_CHAT      = ["给 DeepSeek 发送消息", "Message DeepSeek"]
PH_ACCOUNT   = ["请输入手机号/邮箱地址", "Phone number / email address"]
PH_CODE      = ["请输入验证码", "Code"]
BTN_SEND_CODE= ["发送验证码", "Send code"]
BTN_LOGIN    = ["登录", "Log in"]
TAB_SMS      = ["验证码登录", "Code login"]
BTN_NEW_CHAT = ["开启新对话", "新对话", "New chat"]
TXT_STOP     = ["停止生成", "Stop"]
CODE_SENT    = ["秒后可再次获取", "Resend after"]
```

排查时用 `DSV_DEBUG=1` 打印轮询细节；用 `DSC_HEADFUL=1` 直接看着浏览器跑，最快定位是哪一步没匹配上。

服务端路由与接口（最新快照）：

```
路由: /  /a/:agentId  /a/:agentId/s/:sessionId  /sign_in  /sign_up  /forgot_password
接口: /api/v0/users/current
      /api/v0/users/create_sms_verification_code
      /api/v0/chat_session/create | delete | fetch_page(GET)
      /api/v0/chat/create_pow_challenge | completion
```

## 文件

```
deepseek-chat-cli/
├── dsc.py                  # 单文件对话 CLI（Windows Edge / Linux Chromium / iSH minis）
├── requirements.txt        # 依赖
├── README.md               # 本文件
└── .github/workflows/dsc.yml  # GitHub Actions 直接运行方案
```

## 免责声明

- 仅供个人学习与研究使用，通过浏览器自动化操作您自己的 DeepSeek 账号
- 依赖 DeepSeek 网页结构，官网改版可能失效，需自行更新选择器（见「选择器契约」）
- 请遵守 DeepSeek 服务条款，使用后果自负
