# HR Agent

HR Agent 是运行在 Windows PC 上的招聘流程辅助程序。它通过 ADB 连接一台固定准入的 Android 设备，在 BOSS 直聘中执行受约束的页面读取、候选人信息采集和人工授权后的业务动作。

当前实现包含两条流程：

- **1号状态机**：浏览候选人、采集简历、记录 Agent 判断，并仅对明确批准的候选人发起沟通。
- **2号状态机**：处理“新招呼”，根据已采集的会话语境，从核验过的允许话术清单中选择回复；没有安全匹配时挂起人工。

3号状态机不在当前版本范围内。2026-10-27 单账号长时间连续运行后被人工确认封号，真机自动化测试现已暂停。离线测试通过不代表真实页面、发送链路或平台风控已经验证。

## 当前复现边界

本项目有两个不同层次的“可复现”：

1. **离线开发与测试可复现**：Python 代码、SQLite 建表 SQL、测试和文档可以在不连接手机的情况下安装、检查和运行。
2. **真机流程不是通用设备复现**：设备层通过本机 `.env` 绑定固定设备身份。其他开发者可以通过环境变量配置自己的获授权设备，但不能在未经授权的任意设备上运行完整流程。

运行时配置是复现所必需的：`config/job_profiles.yaml` 提供岗位画像，`config/reply_phrases.yaml` 提供允许话术清单。真实配置不进入仓库；仓库只保存脱敏的 `*.example.yaml`，正式配置通过受控渠道提供。

## 目录结构

```text
HR_Agent/
├─ app/                         Python 应用包与两个状态机
├─ config/                      本地真实配置与可入仓的脱敏示例
├─ data/
│  ├─ schema.sql                1号及公共候选人表的可执行 DDL
│  ├─ reply_schema.sql          2号回复状态机的可执行 DDL
│  └─ *.sqlite3                 本地运行库，不进入仓库
├─ docs/                        需求、开发、数据结构、环境、ADR 和实测记录
│  └─ assets/                   DOM 架构图和流程图使用的静态资源
├─ tests/                       基于 unittest 的离线测试
├─ log/                         运行日志、事件、XML 和截图证据，不进入仓库
├─ recon/                       历史实机侦察工具与采集产物，不属于主程序
├─ 素材区_请勿读取/             本地受保护素材区，不读取、不进入仓库
├─ .env.example                 机器与准入设备环境变量模板
├─ .gitignore                   本地数据、真实配置和生成物的屏蔽规则
├─ environment.yml              Conda 环境定义
├─ pyproject.toml               Python 项目与可编辑安装元数据
└─ README.md                    项目入口、复现步骤与仓库边界
```

### `app/` 模块职责

| 模块 | 作用 |
|---|---|
| `cli.py`、`__main__.py` | 提供稳定入口 `python -m app`，路由诊断、1号、2号和调试命令。 |
| `settings.py` | 从系统环境或根目录 `.env` 读取本机 ADB 路径及准入设备身份。 |
| `device.py` | 封装 ADB、设备身份校验、读屏、点击、滑动和目标应用检查。 |
| `logging.py` | 按状态机和运行批次生成事件日志、文本日志及证据目录。 |
| `screen_ops.py` | 页面分类、风控熔断、页面等待、返回、滚动和通用证据工具。 |
| `resume.py`、`resume_collector.py` | 识别简历页面、抽取字段、展开折叠区块并跨屏合并。 |
| `walker.py` | 封装候选人列表遍历、去重、进入详情和采集骨架。 |
| `store.py`、`main.py` | 1号状态机的数据访问、候选人采集、判断和首次沟通编排。 |
| `reply_ui.py` | 识别新招呼列表、会话卡片、聊天方向和发送控件。 |
| `reply_plan.py` | 加载允许话术并校验 `send/hold` 回复计划。 |
| `reply_store.py` | 2号状态机的会话、消息、动作、发送尝试和幂等存储。 |
| `reply_flow.py`、`reply_main.py` | 2号状态机的单候选人及分页处理编排。 |
| `review.py` | Agent 评语的非空校验和建议长度提示。 |

### `docs/` 的事实归属

根 README 只负责项目入口和复现路径。详细事实由以下文档维护：

- [文档索引与维护纪律](docs/README.md)
- [运行环境](docs/项目-运行环境-v0.1.md)
- [项目需求](docs/项目-需求文档-v0.6.md)
- [项目开发文档](docs/项目-开发文档-v0.6.md)
- [1号数据库结构](docs/1号状态机-数据库建表文档-v0.4.md)
- [2号数据库结构](docs/2号状态机-数据库建表文档-v0.5.md)
- [实测记录](docs/项目-实测记录-v0.1.md)

数据库逻辑契约以数据库文档为准，可执行建表行为以 `data/*.sql` 为准，现有 `.sqlite3` 文件只代表本机运行时物理状态。三者不一致时不得静默覆盖。

## 仓库与本地数据边界

建仓时应按“源码与复现契约进入仓库，运行数据与个人信息留在本地”的原则处理：

| 路径或文件 | 建议 | 原因 |
|---|---|---|
| `app/`、`tests/`、`docs/` | 进入仓库 | 当前源码、离线验证和长期文档。 |
| `pyproject.toml`、`environment.yml`、`README.md` | 进入仓库 | 安装、环境和项目入口。 |
| `data/*.sql` | 进入仓库 | 新建数据库和运行测试所需的可执行 DDL。 |
| `data/*.sqlite3`、`data/*.sqlite3-wal`、`data/*.sqlite3-shm` | 屏蔽 | 包含候选人和业务运行事实，且可能处于活动事务状态。 |
| `config/*.example.yaml` | 进入仓库 | 脱敏结构示例和离线测试夹具。 |
| `config/job_profiles.yaml`、`config/reply_phrases.yaml` | 屏蔽 | 真实业务策略和允许话术，只通过受控渠道分发。 |
| `log/` | 整体屏蔽 | 包含事件、页面文本、XML、截图和候选人个人信息。 |
| `recon/` | 整体屏蔽或移出主仓库 | 历史探针、旧结构副本和采集产物不属于当前主程序复现链。 |
| `tmp/` | 整体屏蔽 | 当前已删除；如工具再次生成，只用于临时快照。 |
| `素材区_请勿读取/` | 整体屏蔽 | 受保护的本地素材，不属于项目源码。 |
| `.env`、`.env.local` | 屏蔽 | 保存本机路径和准入设备标识；只提交 `.env.example`。 |
| `.venv/`、`__pycache__/`、`.pytest_cache/`、构建产物 | 屏蔽 | 可由环境或工具重新生成。 |

不要为了提交“完整现场”而复制数据库、日志、截图或真实配置。需要长期保留的实测结论应先脱敏并压缩为文档，原始证据留在受控存储中。

## 环境准备

### 1. 系统前提

- Windows；当前项目只在 Windows 环境验证。
- Python 3.12 或更高版本。
- Android SDK Platform Tools（ADB）。
- 仅在恢复真机测试后：已开启 USB 调试、完成授权并符合设备身份校验的准入设备。
- 仅在恢复真机测试后：设备已安装并登录 BOSS 直聘。

Python 运行时代码只使用标准库，项目没有声明第三方运行依赖。完整环境边界见 [项目运行环境](docs/项目-运行环境-v0.1.md)。

### 2. 创建 Python 环境

推荐使用仓库中的 Conda 定义：

```powershell
conda env create -f environment.yml
conda activate hr-agent
python --version
```

也可以使用 Python 自带的虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

首次执行可编辑安装时，构建工具可能访问 Python 包索引；日常直接在项目根目录运行 `python -m app` 不要求安装项目。

### 3. 准备本机与设备环境变量

复制模板并填写当前开发机及明确获授权设备的值：

```powershell
Copy-Item .env.example .env
```

```dotenv
HR_AGENT_ADB_PATH=C:\Android\platform-tools\adb.exe
HR_AGENT_DEVICE_SERIAL=replace-with-authorized-adb-serial
HR_AGENT_DEVICE_MANUFACTURER=replace-with-expected-manufacturer
HR_AGENT_DEVICE_MODEL=replace-with-expected-model
HR_AGENT_DEVICE_CODENAME=replace-with-expected-device-codename
```

系统环境变量优先于 `.env`，适合 CI 或临时覆盖；`.env` 用于开发机持久化，并已加入 `.gitignore`。缺少任一设备变量时，程序会在接触 ADB 前拒绝继续。

### 4. 准备业务配置

项目根目录必须存在：

- `config/job_profiles.yaml`：由 `config/job_profiles.example.yaml` 复制，填写岗位画像和候选人判断依据。
- `config/reply_phrases.yaml`：由 `config/reply_phrases.example.yaml` 复制，填写经审批的允许话术；其内容是 JSON 兼容格式，`phrases` 中每项包含 `key`、`intent`、`text`，且 `observed_count` 与列表长度一致。

两个真实文件均被 `.gitignore` 屏蔽。示例话术只能用于理解结构和离线测试，禁止用于真机发送。

### 5. 初始化本地数据库

不应从其他开发者处复制正式 `.sqlite3` 文件。1号和2号数据访问层会在本地数据库打开时执行 `data/schema.sql`；2号随后还会执行 `data/reply_schema.sql`。因此仓库必须保留这两份 SQL，数据库文件本身则留在本地。

备份或迁移数据库前必须停止程序。若数据库旁存在同名 `-wal`、`-shm` 文件，应和主数据库作为同一组处理。

### 6. 运行离线测试

```powershell
python -m unittest discover -s tests -v
```

测试会使用临时 SQLite 数据库和脱敏示例话术，不读取真实业务配置，也不需要连接手机。测试通过只证明离线契约，没有验证真机页面或发送链路。

## ADB 与诊断

先确认 ADB 可见：

```powershell
adb devices
python -m app --help
```

`python -m app doctor` 使用 `.env` 或系统环境变量中的 ADB 路径，并会写入一次本地运行日志。1号状态机也允许只覆盖 ADB 路径：

```powershell
python -m app state1 --adb "C:\Android\platform-tools\adb.exe" selfcheck
```

`--adb` 不会覆盖准入设备身份。设备身份校验是安全边界，不应为了让任意设备通过而直接删除。若未来需要支持多台准入设备，应设计明确的准入清单并补充测试。

## 常用命令

以下命令仅展示入口；当前阶段不得执行真机自动化。恢复必须由需求方明确授权，并设置单次运行预算和人工停止条件。

```powershell
# 1号：查看当前列表，不发送
python -m app state1 scan-list

# 1号：采集一位候选人，不发送
python -m app state1 collect --max-candidates 1

# 2号：扫描当前“新招呼”列表，不发送
python -m app state2 scan

# 调试时保存完整 XML/PNG 证据
python -m app --evidence debug state2 scan
```

标准证据等级为 `minimal`；也可使用 `off` 或 `debug`。事件日志始终写入 `log/`，XML/PNG 是否保存由证据等级决定。

## 安全边界

- 默认优先使用只读命令或 dry-run。带 `--execute` 的流程会触发真实发送。
- 当前阶段不得执行真机自动化命令。
- 1号 `greet` 和2号 `send-planned` 属于显式发送命令，不以 `--execute` 作为二次开关；运行前必须确认目标和数据库状态。
- 不要在程序运行时替换数据库，也不要把生产数据库作为测试夹具。
- `config/`、`data/`、`log/`、截图和控件树可能包含业务敏感信息或候选人个人信息，不应提交、外传或作为未脱敏示例长期保存。
- 任何恢复真机验证的决定都必须同时考虑账号风控、候选人隐私、不可逆发送边界和人工停止机制。
