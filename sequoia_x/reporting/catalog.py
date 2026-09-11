"""报告中采用与实际筛选逻辑一致的名称和规则说明。"""

STRATEGIES = {
    "MaVolumeStrategy": {
        "name": "均线放量", "key": "ma_volume", "category": "趋势突破",
        "description": "昨日 MA5 < MA20，今日 MA5 > MA20；成交量 > 含今日的20日均量 × 1.5。",
    },
    "TurtleTradeStrategy": {
        "name": "海龟突破", "key": "turtle", "category": "趋势突破",
        "description": "收盘突破此前20日最高价，成交额 > 1亿元，收阳且较前一根日线上涨。",
    },
    "HighTightFlagStrategy": {
        "name": "高位整理", "key": "flag", "category": "整理观察",
        "description": "40日高低价比 > 1.6；10日高低价比 < 1.15；10日低点 ≥ 40日高点的80%；量 < 前20日均量的60%。尚未验证突破。",
    },
    "LimitUpShakeoutStrategy": {
        "name": "大涨后放量收阴", "key": "shakeout", "category": "整理观察",
        "description": "昨日涨幅达到所属板块涨停幅度减0.5个百分点；今日收阴、量 > 昨日2倍，最低价 ≥ 昨收。形态不能证明洗盘。",
    },
    "UptrendLimitDownStrategy": {
        "name": "趋势股异常大跌", "key": "limit_down", "category": "异常大跌",
        "description": "昨日 MA20 > MA60；今日跌幅达到所属板块跌停幅度减0.5个百分点，量 > 含今日20日均量的2倍。未验证反包。",
    },
    "RpsBreakoutStrategy": {
        "name": "RPS 强势", "key": "rps", "category": "趋势突破",
        "description": "本地股票池中120根日线收益排名 RPS ≥ 90；收盘 ≥ 120日最高价的90%。接近高位不等于突破。",
    },
    "MaVolumeV2Strategy": {
        "name": "均线放量 V2", "key": "ma_volume_v2", "category": "研究候选",
        "description": "均线放量基础条件，加市场宽度、20日趋势、MA60、RPS、流动性和追高距离过滤。仅参与历史验证。",
    },
    "HighTightFlagBreakoutV2Strategy": {
        "name": "高位整理突破 V2", "key": "flag_breakout_v2", "category": "研究候选",
        "description": "先确认高位收敛缩量，再要求当日放量突破10日平台，并通过市场环境、RPS和流动性过滤。仅参与历史验证。",
    },
    "PrivatePlacementStrategy": {
        "name": "定增事件", "key": "private_placement", "category": "事件提醒",
        "description": "增发数据中发行方式为定向增发，发行日期不早于当前日期减7个自然日。按发行日期筛选，非公告日。",
    },
}

CATEGORIES = ["趋势突破", "整理观察", "异常大跌", "研究候选", "事件提醒"]
