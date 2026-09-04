"""串行 baostock 会话：检查错误，失败即停止，并关闭底层连接。"""

from contextlib import contextmanager
import time

import baostock as bs
from baostock.common import context


class BaostockError(RuntimeError):
    """数据源拒绝请求或发生网络错误。"""


def check_result(result, operation: str) -> None:
    if result is None or result.error_code != "0":
        code = getattr(result, "error_code", "unknown")
        message = getattr(result, "error_msg", "未收到响应")
        raise BaostockError(
            f"baostock {operation}失败 [{code}]：{message}。已停止请求，"
            "请检查网络或官网限制状态，不要连续重跑；已有行情可用 --local-only 分析。"
        )


def read_rows(result, operation: str) -> list:
    check_result(result, operation)
    rows = []
    while result.next():
        check_result(result, operation)
        rows.append(result.get_row_data())
    check_result(result, operation)
    return rows


@contextmanager
def baostock_session():
    """仅正常完成时发送 logout；失败时直接关闭 socket，不再请求服务器。"""
    try:
        check_result(bs.login(), "登录")
        yield bs
        check_result(bs.logout(), "退出登录")
    finally:
        # baostock 登录失败或 logout 超时时也可能留下 socket。
        sock = getattr(context, "default_socket", None)
        if sock is not None:
            sock.close()
            context.default_socket = None


def pace_request() -> None:
    """请求之间稍作间隔；并非服务端配额或解封保证。"""
    time.sleep(0.2)
