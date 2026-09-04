"""本地股票分类与输出过滤。ST按缓存简称识别，不用于历史ST状态回溯。"""

import re
import unicodedata

BOARDS = {
    "sh_main": "沪市主板", "sz_main": "深市主板", "star": "科创板",
    "chinext": "创业板", "bse": "北交所", "sh_b": "沪市B股", "sz_b": "深市B股",
    "unknown": "其他/待核对",
}
EXCHANGES = {"sh": "上交所", "sz": "深交所", "bj": "北交所", "unknown": "待核对"}
ST_LABELS = {"normal": "未标记ST", "st": "ST", "star_st": "*ST", "unknown": "ST待核对"}


def stock_profile(symbol, name, name_asof=None):
    code = str(symbol).strip()
    board, exchange = "unknown", "unknown"
    if re.fullmatch(r"\d{6}", code):
        if code.startswith(("688", "689")):
            board, exchange = "star", "sh"
        elif code.startswith(("600", "601", "603", "605")):
            board, exchange = "sh_main", "sh"
        elif code.startswith(("000", "001", "002", "003")):
            board, exchange = "sz_main", "sz"
        elif code.startswith(("300", "301")):
            board, exchange = "chinext", "sz"
        elif code.startswith("920"):
            board, exchange = "bse", "bj"
        elif code.startswith("900"):
            board, exchange = "sh_b", "sh"
        elif code.startswith("200"):
            board, exchange = "sz_b", "sz"
        # 4/8历史代码也可能是新三板，缺少交易所元数据时不直接归为北交所。
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name or "")).upper()
    normalized = re.sub(r"^(?:XD|XR|DR)", "", normalized)
    state = "unknown" if not normalized else "normal"
    if re.match(r"^S?\*ST", normalized):
        state = "star_st"
    elif re.match(r"^S?ST", normalized):
        state = "st"
    return {"board": board, "board_name": BOARDS[board], "exchange": exchange,
            "exchange_name": EXCHANGES[exchange], "st_status": state,
            "st_label": ST_LABELS[state], "name_asof": name_asof}


def output_filter(settings):
    return {"boards": sorted(set(settings.include_boards)), "exclude_st": settings.exclude_st}


def matches_output_filter(profile, settings):
    return (not settings.include_boards or profile["board"] in settings.include_boards) and (
        not settings.exclude_st or profile["st_status"] == "normal"
    )
