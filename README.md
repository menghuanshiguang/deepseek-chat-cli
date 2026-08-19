# dsc — DeepSeek 网页版对话 CLI（纯文本对话版）

参考 [menghuanshiguang/deepseek-vision-cli](https://github.com/menghuanshiguang/deepseek-vision-cli) 的登录与会话管理设计，把「识图 CLI」改成「对话 CLI」：

- 你输入一句提示词，`dsc` 驱动浏览器打开 DeepSeek 网页版
- 新建对话 → 发送提示词 → 等待网页端流式回答完成 → 把回答纯文本输出到 stdout
- 默认**用后即删**：对话结束立即删除本次会话，不污染对话列表
- 登录采用与参考项目相同的异步设计：`--login` 发短信后立即退出，`--verify` 完成登录并存 token

---

## 安装

```bash
# 克隆本项目
git clone <your-repo-url>
cd deepseek_webapi

# 安装依赖
pip install -r requirements.txt

# Linux / GitHub Actions 需要安装 Chromium 浏览器
python -m playwright install --with-deps chromium

# Windows 不需要下载浏览器：驱动系统 Edge/Chrome
```

> 在 iSH / OpenMinis 等环境会自动使用系统自带的 `minis-browser-use`，无需额外安装。

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

## GitHub Actions 直接运行

仓库已包含 `.github/workflows/dsc.yml`，可以在 GitHub Actions 上直接运行浏览器自动化。

### 1. 发短信登录

在 Actions 页面选择 **Run workflow**，填写：

- `phone`：`15183076179`
- 其它留空

运行后会给该手机号发送 DeepSeek 登录短信。

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
| 1 | 用法错误（没有提示词） |
| 2 | 参数缺失（`--login` / `--verify` 未给值） |
| 3 | 短信发送失败（验证未通过/被风控，等 30s 重试） |
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

## 文件

```
deepseek_webapi/
├── dsc.py                  # 单文件对话 CLI（Windows Edge / Linux Chromium / iSH minis）
├── requirements.txt        # 依赖
├── README.md               # 本文件
└── .github/workflows/dsc.yml  # GitHub Actions 直接运行方案
```

## 免责声明

- 仅供个人学习与研究使用，通过浏览器自动化操作您自己的 DeepSeek 账号
- 依赖 DeepSeek 网页结构，官网改版可能失效，需自行更新选择器
- 请遵守 DeepSeek 服务条款，使用后果自负
