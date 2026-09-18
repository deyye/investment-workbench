# 投资项目智能工作台

将审批文件结构化与政策文件归集合并为一个 Python 项目：**一次启动、一个访问地址、一处模型配置、两个业务入口**。

## 启动

需要 Python 3.10+，建议 3.12。首次安装：

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

之后每次只运行（默认使用 Waitress 生产级 WSGI 服务）：

```bash
python start.py
```

打开 **http://127.0.0.1:8765/**。无需启动第二个服务，不需要 Node.js。

仅在本地开发调试时可显式使用 Werkzeug：`python start.py --dev-server`。

也可 `python -m workbench`；修改端口或数据目录：

```bash
python start.py --port 9000 --data-dir /path/to/workbench-data
```

| 地址 | 功能 |
| --- | --- |
| `/` | 两个业务入口与模型状态 |
| `/approval/` | 上传批复、提取字段、原文核对、阶段比对、Excel 导出 |
| `/policy/` | 来源采集、范围判断、分类、复核、政策资料库 |
| `/settings/model` | 唯一模型设置页，文字与视觉模型、连接检查、启停 |
| `/tasks` | 两个业务的任务记录，5 秒刷新 |
| `/api/health` | 健康检查 |

## 两个业务怎么用

### 审批文件结构化

1. 进入“审批文件结构化”，上传 PDF。每批最多 10 份，单份不超过 20MB，每批合计不超过 23MB。
2. 查看逐步处理状态，核对 14 项基本字段与动态建设指标。
3. 点击字段查看原文页面与高亮证据；确认或修改结果。
4. 查看同一项目不同审批阶段的变化，导出 Excel / JSON。
5. 可勾选“大模型辅助抽取”。没有配置模型时，仍可用本地解析。

### 政策文件归集

1. 进入“政策文件归集”→“采集来源”，选择来源或一键采集。
2. 到“处理进度”查看采集、附件处理和研判过程。
3. 在“待办清单”核对材料不全、证据不足或范围不确定的文件。
4. 在“政策资料库”按地区、类别、关键词查询，保留来源和复核记录。

原有来源及分类规则保留在 `config/`。系统不会在启动时自动执行公网采集；由用户在页面启动。原站点需要动态浏览器握手的可选能力依赖 Node.js 和 Playwright，基础启动不需要这两项；未安装时相关来源可能无法采集，会保留失败信息。

## 模型只配置一次

打开“模型设置”，填写兼容 OpenAI Chat Completions 的服务地址、密钥与文字模型名称。两个业务立即共用；视觉模型可选，需要填写支持图像输入的模型。

- 可选择百炼、DeepSeek、本地 Ollama / vLLM 等预设；模型名称以账户或本地实际可用模型为准。
- 配置保存后立即生效，无须重启；旧政策配置页自动跳转到统一页面。
- 配置优先级：**界面保存值 > 进程环境变量 / `.env` > 默认值**。`.env` 不覆盖已设置的进程环境变量。
- 共用配置仅保存为 `data/model_config.json`；密钥不下发浏览器，文件权限设为 0600。
- 更换服务地址需重新填写密钥，或勾选“清除已保存的密钥”，防止误用旧服务凭证。
- 停用后两个业务均不再发起新的模型请求；已发出的请求可能完成。审批仍保留本地抽取，政策回退本地规则。
- 每个模型请求有超时、有限重试、输出大小限制、JSON 检查；模型故障保留可读原因。
- 本地无密钥服务需勾选“服务无需密钥”。内网 HTTP 服务需在高级参数中明确填写允许的主机名。
- Docker 内的 `127.0.0.1` 指容器自身。访问宿主机模型需配置可达地址，并将该 HTTP 主机加入允许列表。

合并后的 Web 工作流使用 `core/llm.py`。`app/model_client.py` 为兼容导入；`core/policy_model.py` 将政策分类器适配到共用客户端。政策原有 CLI 及其配置代码保留供回归和维护，**不作为统一工作台的启动或模型配置入口**。

## Docker 一次启动

```bash
docker compose up --build -d
```

同样访问 http://127.0.0.1:8765/ 。数据保存在 `workbench-data` 命名卷，重建容器不丢失。镜像包含中文 OCR、Poppler 和 LibreOffice，可处理扫描页及旧版办公附件。不要执行 `docker compose down -v`，除非确实要删除全部数据。

本次环境没有 Docker，镜像构建尚未实测；本地 Python 启动和 API 流程已实测。

## 访问与部署安全

- 默认只监听 `127.0.0.1`。绑定 `0.0.0.0` 或局域网地址时默认拒绝启动，避免意外把材料、模型配置和维护入口暴露出去。
- 推荐由带 TLS 与认证的反向代理对外提供服务，应用本身仍绑定回环地址。
- 如确需让应用直接监听非本机地址，必须同时设置 `WORKBENCH_ALLOW_REMOTE=true`、`WORKBENCH_AUTH_USER`、`WORKBENCH_AUTH_PASSWORD` 与 `WORKBENCH_COOKIE_SECURE=true`；最后一项要求外层已经提供 HTTPS。
- 官方 `compose.yaml` 是例外：容器内监听 `0.0.0.0`，但宿主机只发布到 `127.0.0.1`，因此通过 `WORKBENCH_CONTAINER_LOOPBACK_ONLY=true` 明确声明仍是本机访问。不要在对外发布容器端口时沿用这个值。
- 也可在本机监听时设置用户名和密码，为全部三个模块启用统一 HTTP Basic 认证。不要把密码写进仓库；使用进程环境变量或系统密钥管理工具注入。
- 页面统一返回 CSP、防嵌入、来源策略、权限策略等安全响应头；政策表单保留 CSRF 校验，JSON 写操作保留同源请求校验。

## OCR 与文件格式

文本 PDF 可直接解析。扫描 PDF 推荐安装 Tesseract 及 `chi_sim` 中文语言包；政策扫描附件还需 Poppler。旧 `.doc/.xls` 转换需要 LibreOffice。macOS 审批侧保留原有 Vision OCR 备用能力。缺少依赖时处理过程会给出失败或待复核提示，不能把“程序启动成功”等同于所有格式均可识别。

## 数据与旧项目

```text
data/
  model_config.json     # 共用模型配置
  approval/             # PDF、结构化 JSON、复核历史、任务与回收站
  policy/               # SQLite、原件与附件、采集及复核记录
```

首次启动建立空业务库，不自动搬动旧项目数据。两个原仓库和原数据不受影响。迁移前停掉原服务并备份；审批侧文档数据可复制到 `data/approval/`（不要复制旧模型设置）。政策库可能包含原件的绝对路径，仅复制 `policy.db` 不足以迁移附件，需连同原件目录迁移并核对路径。模型配置通过新页面重新填写，不自动导入旧密钥。

## 代码结构

| 目录 | 职责 |
| --- | --- |
| `workbench/` | 主入口、Flask 应用装配、审批适配器、工作台及设置页 |
| `core/` | 共用模型配置、请求、结果校验和政策模型适配 |
| `app/` | 审批抽取、OCR、印章、证据、阶段比对、人工复核与导出 |
| `policy_collector/` | 来源适配、采集、附件解析、分类、入库、版本和复核 |
| `config/` | 政策来源与业务分类规则 |
| `samples/`、`gold/`、`evaluation/` | 原项目样例及评测基准 |
| `tests/approval/`、`tests/policy/` | 两套原业务回归测试 |
| `tests/integration/` | 单地址、共用配置、真实 HTTP 模型模拟、业务闭环测试 |
| `scripts/` | 原项目评测、来源检查和维护脚本；具体启动命令以本 README 为准 |
| `docs/upstream/` | 导入版本的原说明与历史验证记录，不代表本次测试结论 |

底层保留两个业务的数据模型与处理流程。审批通过 Flask 适配现有 `Handler.get/post`；政策通过 WSGI 挂载到 `/policy`，自动保留表单和静态资源前缀。**只有一个 HTTP 监听端口，没有反向代理到第二个进程，也没有 iframe 套两个网站。**

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

最新验证结果见 [验证记录](docs/VALIDATION.md)。不需要真实 API 密钥；模型调用测试通过本地 HTTP 模拟服务完成。外部付费模型、公网全量采集和 Docker 仍需在对应环境单独验收。

## 使用边界

默认绑定本机，适合个人研究与内网演示。已提供统一的单账户访问保护，但仍没有多用户角色、项目级权限隔离和分布式任务队列；多人生产使用还需要外置身份系统和细粒度授权。当前不要启用多进程 worker，否则审批的内存任务状态会分散。

## 来源

- `deyye/approval-structuring-agent` / `main`：`89700dac7a2859bad8f532ab4e8fc797a386e999`
- `deyye/policy-collector` / `release/integrated`：`69d7887b476329891aabab1fd49cea2f7469ab99`

原始业务代码作为快照导入；本仓库不包含两个源仓库的完整 Git 历史。来源和版本记录在此，便于后续对照。
