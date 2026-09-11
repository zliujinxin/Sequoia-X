"""只监听本机的 Sequoia-X 交互页面与缠论 API。"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
import webbrowser

from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.providers.base import ProviderError
from sequoia_x.web.chanlun import ChanlunAnalysisService

logger = get_logger(__name__)


def _handler_factory(settings: Any, analysis: ChanlunAnalysisService):
    page = Path(__file__).with_name("chanlun.html").read_bytes()
    report_path = Path(settings.report_dir) / "latest.html"
    validation_path = Path(settings.report_dir) / "strategy-validation-latest.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "SequoiaX/2"

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.info("页面请求 " + fmt % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: dict[str, Any]) -> None:
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path in {"/", "/report"}:
                if report_path.exists():
                    self._send(HTTPStatus.OK, report_path.read_bytes(), "text/html; charset=utf-8")
                else:
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", "/chanlun")
                    self.end_headers()
                return
            if parsed.path == "/chanlun":
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
                return
            if parsed.path == "/validation":
                if validation_path.exists():
                    self._send(
                        HTTPStatus.OK,
                        validation_path.read_bytes(),
                        "text/html; charset=utf-8",
                    )
                else:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "尚未生成策略验证报告，请先运行 main.py --validate-strategies"},
                    )
                return
            if parsed.path == "/api/health":
                self._json(HTTPStatus.OK, {"status": "ok"})
                return
            if parsed.path != "/api/chanlun":
                self._json(HTTPStatus.NOT_FOUND, {"error": "页面不存在"})
                return

            query = parse_qs(parsed.query)
            try:
                value = analysis.analyse(
                    symbol=query.get("symbol", [""])[0],
                    market=query.get("market", ["AUTO"])[0],
                    period=query.get("period", ["DAILY"])[0],
                    adjustment=query.get("adjust", ["QFQ"])[0],
                    count=int(query.get("count", ["800"])[0]),
                )
            except (ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except ProviderError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            except Exception:
                logger.exception("缠论页面分析失败")
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "分析失败，请查看终端日志"})
            else:
                self._json(HTTPStatus.OK, value)

    return Handler


def create_server(settings: Any, host: str, port: int, analysis: Any | None = None):
    if analysis is None:
        names = DataEngine(settings).get_stock_names()
        analysis = ChanlunAnalysisService(settings, names)
    return ThreadingHTTPServer((host, port), _handler_factory(settings, analysis))


def run_server(
    settings: Any, host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = True
) -> None:
    server = create_server(settings, host, port)
    url = f"http://{host}:{server.server_port}/chanlun"
    logger.info(f"本地观察台已启动：{url}")
    logger.info("按 Ctrl+C 停止服务")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("本地观察台已停止")
    finally:
        server.server_close()
