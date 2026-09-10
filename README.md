# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每日收盘后自动选股并推送至飞书群。

数据层通过统一 Provider 接口拉取历史及增量日 K，存储于本地 SQLite。默认数据源仍是
[baostock](http://baostock.com)（后复权），访问必须遵守其每日请求限额与禁止并发连接规则。
已加入固定审计版本的 easy-tdx 候选源和只读影子校验；定增监控另使用 AKShare 的东方财富接口。

---

## 运行模式

```bash
python main.py               # 单连接串行增量更新 + 策略 + 飞书推送
python main.py --backfill     # 单连接串行回填历史K线，可续传
python main.py --local-only   # 使用本地行情生成报告并推送变化摘要
python main.py --local-only --no-notify  # 完全离线预览，不发送消息
python main.py --local-only --force-notify # 无变化时也发送当前摘要
python main.py --refresh-names # 只补股票名称并更新现有报告，不拉行情、不推送
python main.py --check-provider easy_tdx # 抽样比较 easy-tdx，不改正式行情
```

本地模式跳过在线定增策略、海龟候选股的市值查询与股票名称更新；名称使用已有缓存，
没有缓存时显示代码。该模式仍会向配置的飞书机器人发送变化摘要。
`--no-notify` 只关闭推送，需同时加上 `--local-only` 才不会请求数据源。

### easy-tdx 影子校验

easy-tdx 不属于默认依赖。只从已审计提交安装，不使用浮动 `main` 或同名 PyPI 包：

```powershell
.\.venv\Scripts\python.exe scripts\install_easy_tdx.py
.\.venv\Scripts\python.exe main.py --check-provider easy_tdx
```

安装脚本会核对提交归档 SHA-256、版本和 MIT 许可证，并处理上游缺失 `web-ui/dist` 导致的
Git 安装失败，同时写入供运行时复核的来源标记；它不构建或启动 Web UI。每次执行
`uv sync` 后若要继续使用候选源，请再运行一次安装脚本。校验默认从本地 SQLite 按板块和 ST 状态抽取 30 只
股票，各比较最近 250 根日线，使用与正式库相同的 `hfq` 后复权口径。
它只读取 `stock_daily`，结果写到 `data/reports/latest-provider-check.html` 和对应 JSON，
不会更新行情、运行策略或发送飞书。可用 `--check-sample-size 50 --check-days 500` 调整范围。
当前代码会拒绝用 `DATA_PROVIDER=easy_tdx` 写正式行情表，防止两种 HFQ 路径混在同一数据库；
等后续加入来源、复权和抓取时间元数据后再开放生产切换。

报告中的 PASS 表示日期覆盖、收盘价、成交量和成交额均在严格容差内；WARN 表示存在可解释
差异或疑似单位问题；FAIL 表示数据为空、重合不足、价格差异超过 2% 或连接失败。通过一次
抽样校验只说明该样本和时点可用，正式切换仍需连续观察。

### 按板块与 ST 过滤

观察台提供独立的“交易所”“板块”“ST状态”筛选，可组合使用：上交所包含沪市主板与科创板，深交所包含深市主板与创业板。
点击 **沪深主板 · 排除ST**，即可只看两市主板中缓存简称未标记 ST 的候选；支持只看 ST、只看 *ST、名称缺失待核对等选项。每行与详情显示分类标签，CSV和飞书也携带分类。
页面筛选只影响当前查看和导出文件，不修改已经发送的消息。

希望生成报告及发送飞书时就过滤，可运行：

```powershell
# 仅沪深主板，排除 ST、*ST 和名称缺失者；使用本地行情，不推送
.\.venv\Scripts\python.exe main.py --local-only --no-notify --boards sh_main sz_main --exclude-st
# 仅科创板；保留 ST，覆盖 .env 中的排除设置
.\.venv\Scripts\python.exe main.py --local-only --no-notify --boards star --no-exclude-st
```

去掉 `--no-notify` 会发送相同过滤范围的飞书摘要。板块参数：`sh_main` 沪市主板、`sz_main` 深市主板、`star` 科创板、`chinext` 创业板、`bse` 北交所、`sh_b` 沪市B股、`sz_b` 深市B股、`unknown` 其他/待核对。
也可在 `.env` 长期设置 `INCLUDE_BOARDS=["sh_main","sz_main"]` 和 `EXCLUDE_ST=true`；默认空列表、false，保留全部结果。

过滤发生在策略计算后，RPS计算池不变。报告显示过滤前后数量；不同输出过滤条件的报告与通知基准隔离。
ST和*ST根据最近缓存的简称前缀识别，兼容全角星号和空格；详情显示名称采集日期。它不是历史交易日的ST状态，也不能替代实时风险警示名单。名称缺失者标记待核对，在“排除ST”时一并排除；执行 `--refresh-names` 可刷新名称。
板块按股票代码号段区分。北交所使用920号段；4/8开头历史代码在缺少上市市场元数据时标记“其他/待核对”，避免把新三板直接当作北交所。
分类参考：[上交所股票分类](https://www.sse.com.cn/assortment/stock/list/share/)、[深交所代码区间表](https://www.szse.cn/marketServices/technicalservice/index.html)、[北交所代码切换说明](https://www.bse.cn/important_news/200024115.html)、[上交所风险警示规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/bond/convertible/listing/c/c_20260424_10817746.shtml)。

### 导入通达信自选股

在观察台设置筛选条件和排序后，点击 **导出通达信自选股**，下载 `.EBK` 文件。
文件覆盖当前筛选后的全部候选，包括列表滚动区域内的股票；保留查看顺序、自动去重。
在通达信电脑版按 `Ctrl+D`，进入“板块”，选中“自选股”或自行建立的板块，点击“导入”，选择下载的文件。若客户端询问追加或覆盖，追加可保留已有股票。菜单位置参考[通达信官方说明](https://zxfile.tdx.com.cn/zx/201612/3185123/3185123_fj.pdf)。

格式为每行“市场标识＋六位股票代码”：上海 `1`、深圳 `0`，例如 `1600000`、`0000001`。
使用 CRLF 换行，纯 ASCII（与 GBK/ANSI 的数字字节一致）、无 BOM、无表头和名称，避免代码前导零丢失。
当前支持沪深主板、科创板、创业板及沪深B股；北交所和无法确认市场的记录会提示不支持并停止整批导出，不会静默漏掉。空筛选禁用导出。
下载功能不修改通达信安装目录或已有自选股。文件格式已与本机通达信自选股样本核对，客户端实际导入结果需在目标通达信版本确认。

导出格式测试：`node --test tests/test_tdx_export.cjs`。

### 报告内容

每次选股完成后，双击 `data/reports/latest.html` 即可在浏览器打开观察台，无需启动服务器。
每次也保存一份带时间戳的独立 HTML，便于留存；所有图表、样式和数据内嵌，断网可用。

- **一股一行**：合并各策略结果，保留所有命中策略、具体入选理由与指标。
- **直接看图**：30 / 60 / 120 根日线切换，K线、成交量、MA5 / MA20 / MA60 和此前20日高点。
- **快速筛选**：按代码或名称搜索，按类型、名单变化过滤，按涨跌幅、成交额、RPS等排序；支持导出当前筛选 CSV。
- **变化跟踪**：首次、新增、持续、明显变化、移出分别显示。明显变化包括策略、新鲜度、风险提示变化，或涨跌幅较基准变化至少2个百分点、相对成交量变化至少0.5倍。仅日期前进不算明显变化。
- **飞书合并推送**：使用同一机器人地址的策略合并去重，不同机器人仍按原配置路由。首次发送当前候选摘要，之后只发送新增、明显变化和移出；每类默认最多展示5只，完整名单在报告内。消息过长自动拆卡片。

报告与上次生成的报告比较；飞书与该机器人上次成功收到的摘要比较。预览报告不会消耗待发送变化，发送失败不会推进通知基准，下次可重试。多张卡片中途失败时，重试可能重复前面已成功的部分。
本地模式与联网模式、不同数据库或不同策略组合使用独立比较基准。
状态文件在 `data/reports/state/`，删除后将重新视为首次；报表与通知仅比较历史快照，不是收益回测。

可在 `.env` 设置：

```dotenv
REPORT_DIR=data/reports
REPORT_TOP_N=5
```

首次回填获取股票列表时同时把简称保存到 SQLite 的 `stock_basic` 表。
联网报告每日最多批量更新一次股票名称，同时保存在数据库和报告目录的 `stock_names.json`；本地模式直接读取已有名称。
旧数据库缺少名称时，执行 `python main.py --refresh-names` 即可单独补齐并更新 `latest.html`，不重拉日线、不执行策略、不推送，也不改变名单比较基准。浏览器刷新即可看到名称。
飞书中的股票链接可打开外部行情页。完整报告保存在运行电脑，手机访问需要另外部署网页，本项目不会自动上传。

### 个股缠论页面

安装固定版本的 easy-tdx 后，可启动只监听本机的交互页面：

```powershell
.\.venv\Scripts\python.exe main.py --serve
```

浏览器会打开 `http://127.0.0.1:8765/chanlun`。输入六位股票代码即可选择市场、日线或分钟周期、复权方式，并自定义 100–2000 根K线数据量。页面展示原始K线最新日期、最后确认结构日期、K线与笔的叠加图、中枢、买卖点、背驰及最近行情。观察台中的股票详情也可直接跳转并带入代码。

“模拟收益”页签按买卖点做探索性历史回放：初始资金 10 万元，只做多，空仓遇买点全仓买入、持仓遇卖点全部卖出。页面同时展示原始信号和严格逐日回放两种结果。逐日回放会把历史K线一根一根加入算法，记录图形锚点、程序首次可见日期和后来是否消失，只在首次可见后的下一根K线开盘成交。结果包括期末资产、累计收益、同期持有收益、最大回撤、胜率、权益曲线和逐笔交易；费用按佣金万分之三（最低 5 元）及卖出印花税万分之五估算。

页面请求由本地 Python 服务串行执行，按 `Ctrl+C` 停止。若不希望自动打开浏览器，可增加 `--no-browser`；端口被占用时可用 `--port 8766`。直接双击 `latest.html` 仍可离线查看选股报告，但动态缠论查询必须先启动本地服务。

买卖点与背驰是 easy-tdx 的简化算法。原始信号仍会标记历史笔与未来中枢的时间错配；“逐日信号”页签展示严格回放得到的首次可见时间、确认延迟和后来发生的重画。模拟尚未处理滑点和涨跌停无法成交，因此当前用于研究，不进入正式选股或推送。

**指标口径**：所有价格及图表均为本地后复权日线，不能直接作为下单报价。
相对成交量是当日成交量/此前20根日线均量，不是盘中量比；策略采用含今日均量的地方会在理由中单独标明。
系统默认选择最近一个覆盖率达到95%的交易日作为统一分析基准，忽略同步过程中覆盖不足的较新日期；可通过 `ANALYSIS_MIN_COVERAGE` 调整。RPS使用该基准日且有至少121根日线的本地股票池计算。
排序用于安排查看，不代表收益预测，多策略命中也不等于独立证据。
近120根日线出现相邻收盘价格跳变至少35%时，提示核对复权口径或特殊交易情形，并在默认查看顺序中后移；不自动改写原始行情或改变策略的入选规则。

同步登录或查询失败时立即停止后续请求，已完成的股票数据保留，下次从断点继续。
请只运行一个联网实例，同一网络出口也不要同时运行其他 baostock 客户端。
遇到超时或黑名单提示时，先停止程序并查看官网限制状态，等待解除后再联网运行。
单连接与请求间隔不能保证不会触及每日配额；不要通过反复重跑增加请求量。

---

## 内置策略 | Strategies

| 策略 | 说明 |
|---|---|
| **TurtleTrade** | 收盘突破此前20日高点、成交额过亿，且高于开盘与前收盘 |
| **MaVolume** | 均线+放量突破 |
| **HighTightFlag** | 前期大幅波动后高位缩量整理，尚不要求突破 |
| **LimitUpShakeout** | 昨日接近所属板块涨停，今日放量收阴但低点守住昨收；不能证明洗盘 |
| **UptrendLimitDown** | 昨日MA20高于MA60，今日接近所属板块跌停且放量；尚未验证反包 |
| **RpsBreakout** | 欧奈尔 RPS 相对强度突破 |
| **PrivatePlacement** | 联网模式查询近7日发行的定向增发记录 |

---

## 快速开始 | Quick Start

### 环境要求

- Python >= 3.10

### 1. 安装依赖

```bash
# 推荐使用 uv（快速包管理器）
uv sync

# 或者 pip
pip install .
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写飞书 Webhook URL
```

### 3. 首次回填历史数据

```bash
python main.py --backfill
```

执行时长取决于股票数量、历史范围和数据源响应速度。

### 4. 日常运行

```bash
python main.py
```

建议配合 crontab 每个交易日收盘后自动执行：

```cron
15 19 * * 1-5 cd /root/Sequoia-X && .venv/bin/python main.py >> log.txt 2>&1
```

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── main.py                      # 入口：日常、回填、本地和影子校验模式
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── .env.example                 # 环境变量模板
├── data/                        # SQLite 数据库（运行时生成，不入 git）
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理
│   │   └── logger.py            # rich 结构化日志
│   ├── data/
│   │   ├── providers/           # 数据源接口、Baostock 与 easy-tdx 适配器
│   │   ├── quality.py           # 候选源 HTML/JSON 影子校验
│   │   └── engine.py            # 数据引擎（baostock 回填 + 增量同步 + SQLite）
│   ├── strategy/
│   │   ├── base.py              # 策略抽象基类
│   │   ├── turtle_trade.py      # 海龟交易策略
│   │   ├── ma_volume.py         # 均线放量策略
│   │   ├── high_tight_flag.py   # 高窄旗形策略
│   │   ├── limit_up_shakeout.py # 涨停洗盘策略
│   │   ├── uptrend_limit_down.py # 上升跌停策略
│   │   └── rps_breakout.py      # RPS 突破策略
│   └── notify/
│       └── feishu.py            # 飞书 Webhook 推送
└── tests/                       # 属性测试（hypothesis）
```

`sequoia_x/reporting/` 包含策略展示口径、报告计算及内嵌网页模板。
同步安全、报告计算和通知状态回归测试无需联网，可直接运行：

```bash
python -m unittest tests.test_market_data_provider tests.test_baostock_safety tests.test_reporting tests.test_stock_profile
```

---

## 数据说明

- **正式数据源**：[baostock](http://baostock.com)（默认；有请求限额，禁止并发连接）
- **候选数据源**：[zliujinxin/easy-tdx](https://github.com/zliujinxin/easy-tdx) 固定提交 `7c9e19de...`，当前只建议先做影子校验
- **复权方式**：后复权（hfq）— 历史价格不变，适合增量存储，避免除权导致数据错乱
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
- **日常增量**：单连接串行拉取，逐只股票提交，按股票代码与日期更新，保留同日其他股票数据

---

## 许可证 | License

MIT
