"""安全安装审计过的 easy-tdx 固定提交。

上游 1.20.8 的 Hatch 配置强制包含 web-ui/dist，但该目录没有提交到 Git。
本脚本核对源码归档后只补空占位文件，使核心 Python wheel 可以正常构建。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

COMMIT = "7c9e19de937946d231735dabb4a275440578b753"
ARCHIVE_URL = f"https://api.github.com/repos/zliujinxin/easy-tdx/zipball/{COMMIT}"
ARCHIVE_SHA256 = "945a6eefbd2880fed0cdb5b0124e32c85f5f18670416bb2a84b9e4e6890e402b"


def _safe_extract(archive: Path, destination: Path) -> Path:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"归档包含越界路径：{member.filename}")
        zipped.extractall(destination)
    directories = [path for path in destination.iterdir() if path.is_dir()]
    if len(directories) != 1:
        raise RuntimeError("easy-tdx 归档目录结构异常")
    return directories[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="安装审计过的 easy-tdx 固定提交")
    parser.add_argument("--archive", type=Path, help="使用已下载的源码 zip，仍会核对 SHA-256")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sequoia-easy-tdx-") as tmp:
        temp = Path(tmp)
        archive = temp / "easy-tdx.zip"
        if args.archive:
            shutil.copyfile(args.archive.resolve(), archive)
            print(f"使用本地归档：{args.archive.resolve()}", flush=True)
        else:
            print(f"下载 easy-tdx 固定提交 {COMMIT} ...", flush=True)
            request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "Sequoia-X"})
            with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != ARCHIVE_SHA256:
            raise RuntimeError(f"源码归档 SHA-256 不匹配：{actual}")

        source = _safe_extract(archive, temp / "source")
        pyproject = (source / "pyproject.toml").read_text(encoding="utf-8")
        if 'version = "1.20.8"' not in pyproject:
            raise RuntimeError("easy-tdx 版本标记不符合已审计版本 1.20.8")
        license_text = (source / "LICENSE").read_text(encoding="utf-8")
        if not license_text.startswith("MIT License"):
            raise RuntimeError("easy-tdx 许可证与审计记录不一致")

        placeholder = source / "web-ui" / "dist" / ".keep"
        placeholder.parent.mkdir(parents=True, exist_ok=True)
        placeholder.write_text("core-only install\n", encoding="ascii")

        uv = shutil.which("uv")
        if uv:
            command = [uv, "pip", "install", "--python", sys.executable, str(source)]
        else:
            command = [sys.executable, "-m", "pip", "install", str(source)]
        subprocess.run(command, check=True)
        from importlib.metadata import distribution

        package_dir = Path(distribution("easy-tdx").locate_file("easy_tdx"))
        marker = {
            "repository": "https://github.com/zliujinxin/easy-tdx",
            "commit": COMMIT,
            "archive_sha256": ARCHIVE_SHA256,
            "install_mode": "core-only-placeholder",
        }
        (package_dir / "_sequoia_provenance.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"easy-tdx 1.20.8 安装完成，来源提交：{COMMIT}", flush=True)


if __name__ == "__main__":
    main()
