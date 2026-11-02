# bambu-ams-check

切片完成后自动解析 plate `.gcode.3mf` 耗材用量，从拓竹云拉取 AMS 余量，弹出桌面对比（或 `--no-ui` 文本报告）。

> English: Windows-ready Python sidecar that compares sliced filament usage against AMS remaining trays via cloud MQTT.

## 功能

- 解析 `.gcode.3mf`（ZIP）中 `Metadata/slice_info.config` 等字段
- 通过云 MQTT `remain`（0–100 / -1）估算剩余克数
- 按 `ams_mapping` / 颜色类型匹配托盘，输出 **够 / 不够 / 未知**
- **首次运行** `watch` / `check` / `devices` / `login` 时自动对本机探测 Studio 路径并生成 `config.local.yaml`（仓库不提供带本机路径的静态配置）
- `reset-config` / `setup --reset` 可清空缓存后重新自动探测
- `login --browser` / `login --gui` 自动获取并保存 token；token 缺失时可弹出「登录拓竹云」

## Windows 推荐流程

```bat
cd /d E:\bambu-ams-check
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

rem 直接 login/watch 即可：若尚无可用 config.local.yaml，会先对本机自动 setup
.venv\Scripts\python -m bambu_ams_check login --browser
.venv\Scripts\python -m bambu_ams_check devices
.venv\Scripts\python -m bambu_ams_check watch
```

换电脑、重装 Studio、或路径变了时：

```bat
rem 清空本机自动配置并重新探测
.venv\Scripts\python -m bambu_ams_check reset-config

rem 重新探测但保留已登录的 access_token
.venv\Scripts\python -m bambu_ams_check reset-config --keep-token

rem 等价写法
.venv\Scripts\python -m bambu_ams_check setup --reset --keep-token
```

## 安装说明

1. 将本目录复制到任意路径（例如 `E:\bambu-ams-check`）
2. 安装 Python 3.12+，创建 venv 并 `pip install -r requirements.txt`
3. **不要**把带本机 Downloads/用户路径的 `config.local.yaml` 提交进 git；`config.example.yaml` 仅含空占位
4. 首次命令会自动生成 `config.local.yaml`（已在 `.gitignore`）

Linux / 本仓库开发机：

```bash
cd /workspace/bambu-ams-check
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m bambu_ams_check check tests/fixtures/sample_plate.gcode.3mf \
  --ams-json tests/fixtures/ams_mock.json --no-ui
```

## 常用一行命令

```bat
.venv\Scripts\python -m bambu_ams_check login --browser
.venv\Scripts\python -m bambu_ams_check devices
.venv\Scripts\python -m bambu_ams_check check path\to\plate.gcode.3mf
.venv\Scripts\python -m bambu_ams_check watch
```

```bash
.venv/bin/python -m bambu_ams_check login --browser
.venv/bin/python -m bambu_ams_check devices
.venv/bin/python -m bambu_ams_check check path/to/plate.gcode.3mf
.venv/bin/python -m bambu_ams_check watch
```

## 登录（推荐浏览器 SSO）

Bambu **没有**面向第三方的 OAuth 回调（Home Assistant / OpenBambuAPI 等同理）。
推荐：`login --browser` 拉起本机 Chrome/Edge（独立 `.browser-profile`，不碰你的日常配置），
你在窗口内完成 MakerWorld 登录后，工具经 CDP **自动**读取 `token` cookie 并写入配置。

```bat
rem 推荐：CDP 自动捕获（打开浏览器 → 登录 → token 自动保存）
.venv\Scripts\python -m bambu_ams_check login --browser

rem 桌面对话框（大按钮「浏览器登录」，密码为备用）
.venv\Scripts\python -m bambu_ams_check login --gui

rem 无参数时默认走浏览器流程
.venv\Scripts\python -m bambu_ams_check login --region cn

rem 备用：账号密码（可能触发邮箱验证码 / TFA；不支持纯社交账号除非已设密码）
.venv\Scripts\python -m bambu_ams_check login --password-login
.venv\Scripts\python -m bambu_ams_check login --account you@example.com --code 123456
.venv\Scripts\python -m bambu_ams_check login --refresh
```

### 浏览器登录说明与限制

1. 若检测到 Chrome/Edge：以 `--remote-debugging-port` + 项目内 `.browser-profile`（已 gitignore）启动，
  打开区域登录页，并轮询 CDP（`/json` + `Network.getCookies` / `document.cookie`）直到出现 `token`。
  - `cn` → `CN_BROWSER_LOGIN_URL`（`bambulab.cn` → MakerWorld CN）
  - `us` → `https://makerworld.com/login`
2. 本地仍起 `127.0.0.1` 辅助页：自动回传失败时可用**书签 / 粘贴 token**（备用）。
3. 未找到 Chrome/Edge 或 CDP 启动失败时，回退到系统 `webbrowser` + 辅助页（需书签/粘贴）。
4. 工具用 preference API 校验 token、解析 `user_id`，写入 `config.local.yaml`（掩码显示）。
5. **不会**读取你日常 Chrome/Edge 用户目录里的 Cookie；也不会绕过 Cloudflare——请在拉起的窗口内完成验证。
6. Token 通常约 3 个月有效，过期后重新 `login --browser`。

English: `login --browser` opens Edge/Chrome with an isolated profile; you sign in normally; the
sidecar auto-captures the MakerWorld `token` cookie over CDP and saves it. Bookmarklet/paste are fallbacks.

成功后写入 `cloud.access_token` / `cloud.user_id`。**完整 token 不会打印**。密码登录不会把密码写入配置文件。

## 多设备与 AMS 余量

同一账号可绑定多台打印机。用 `devices` 列出云端设备，再在 `config.local.yaml` 里用 `device.device_id` 指定要读哪台机器的 AMS（勿把本机配置提交进 git）。

```bat
.venv\Scripts\python -m bambu_ams_check devices
```

运维注意（实测）：


| 机型（示例）          | 示例 `device_id`    | AMS `remain` 表现               |
| --------------- | ----------------- | ----------------------------- |
| A1 mini         | `0309AA520601008` | 常报 `remain=-1` → UI 显示 **未知** |
| X2D / AMS 2 Pro | `20P5BJ681001479` | 返回真实剩余百分比                     |


若关心的是带 AMS 的那台（例如 X2D），把 `device.device_id` 设成对应 `dev_id`，否则可能一直读到 A1 mini 的「未知」。

English: one account can have multiple printers; `devices` lists them; set `device.device_id` to the printer whose AMS you care about. A1 mini often reports `remain=-1` (未知); X2D / AMS 2 Pro usually returns real %.

## 监视模式（watch）

`watch` 轮询 `watch.directories`，按 `watch.patterns` 匹配新切片文件后自动 `check`。

**推荐只监视 `*.gcode.3mf`**（Studio 导出的 plate 切片）。裸 `*.3mf` 容易误触发：Studio 缓存/临时目录里也可能出现奇怪的 `.3mf`（例如 `bamboo_model` 下），导致无意义的弹窗或误报。

`config.example.yaml` 默认同时列了两种 pattern；本机 `config.local.yaml` 建议改成：

```yaml
watch:
  patterns:
    - "*.gcode.3mf"
  # 不建议: - "*.3mf"   # 易匹配 Studio 缓存/临时文件
```

English: prefer `*.gcode.3mf` only; bare `*.3mf` can false-trigger on Studio cache/temp files (e.g. odd `.3mf` under `bamboo_model`).

## CLI


| 命令                                               | 说明                                         |
| ------------------------------------------------ | ------------------------------------------ |
| `watch` / `check` / `devices` / `login`          | 若缺少可用本地配置，**先自动 autoconfig 本机**再继续         |
| `setup` / `autoconfig`                           | 手动探测；`--reset` 清空后重探；`--keep-token` 保留登录令牌 |
| `reset-config [--keep-token] [--in-place]`       | 清空自动配置缓存并立即重新探测本机                          |
| `login --browser` / `--gui` / `--password-login` | 浏览器 SSO（推荐）或密码备用                           |
| `devices`                                        | 列出账号下打印机；配合 `device.device_id` 选择 AMS 来源   |
| `check <file>`                                   | 对单个 `.gcode.3mf` 做用量 vs AMS 对比             |


查看帮助：`login --help`、`setup --help`、`reset-config --help`、`devices --help`。

### reset-config 行为

1. 默认**删除** `config.local.yaml`（`--in-place` 则只清空派生字段）
2. `--keep-token` 时保留 `access_token` / `refresh_token` / `account`；`user_id` 优先用新探测结果，探测不到才回退旧值
3. 立即按本机 `%APPDATA%` / Studio conf 等重新写入监视目录、`device_id` 等
4. 不会发明 token；若仍缺 token 可提示登录

退出码：`0` 无明确不足；`1` 存在 SHORT；`2` 配置/网络错误。

## 测试

```bash
cd /workspace/bambu-ams-check
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

## 已知限制

- AMS `remain` 多为 **估算百分比**；第三方线材 / 关闭 RFID 时常为 `-1`（未知）
- **机型差异**：部分机型（如 A1 mini）即使有耗材也可能长期 `remain=-1`；带 AMS 2 Pro 的机型（如 X2D）更常返回真实 %
- 克数 = `remain/100 * spool_weight_g`，默认整卷 1000g，与真实剩余有偏差
- 云登录、MQTT 协议非官方公开 API，账号风控、Cloudflare、固件变更可能导致失败
- `POST .../user/refreshtoken` 社区反馈常返回 401，请以重新 `login` 为准
- 不修改、不挂钩 Bambu Studio 进程；自动配置仅读取本机 Studio 目录
- 多 AMS / AMS HT 槽位编码以 Studio `ams_mapping` 为准，启发式匹配可能误配
- `watch` 使用裸 `*.3mf` 时可能匹配 Studio 缓存/临时文件，建议仅 `*.gcode.3mf`

## 目录结构

见仓库根目录；设计说明与所用端点见 [docs/DESIGN.md](docs/DESIGN.md)。