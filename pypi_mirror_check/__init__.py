#!/usr/bin/env python3
"""pypi-mirror-check —— 检查指定 PyPI 包/版本是否已同步到全球镜像。

只依赖标准库。给「刚发完包，等镜像同步」这件事一个可执行的答案：

    pypi-mirror-check hubrelay 0.9.5
    pypi-mirror-check hubrelay                # 不写版本 = 查官方最新版
    pypi-mirror-check hubrelay 0.9.5 --json
    pypi-mirror-check hubrelay 0.9.5 --wait --interval 30 --timeout 900
    pypi-mirror-check hubrelay 0.9.5 --only tuna,aliyun
    pypi-mirror-check --list-mirrors
    pypi-mirror-check hubrelay 0.9.5 --config ./my-mirrors.json

**镜像清单不写死在代码里**：默认读同包的 `mirrors.json`（数据文件，改它不用改代码），
也可用 `--config <文件>` 或环境变量 `PYPI_MIRROR_CHECK_CONFIG` 指定，再用 `--mirror`
与 `--official` 做临时覆盖。官方 JSON 基址同样来自配置（默认值在 mirrors.json 里）。

判定不看「有没有这个版本号」，而是比对**文件名 + sha256**：镜像把包同步过去但
内容不对（截断/换源）也算没同步。镜像 simple 页不带 hash 时退化为「文件名在不在」，
除非加 --require-hash。

退出码：0 全部同步；1 有镜像缺失/不一致；2 参数或官方源不可用；3 --wait 超时。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "__version__",
    "Config",
    "Mirror",
    "MirrorResult",
    "Report",
    "check",
    "load_config",
    "main",
    "parse_simple",
    "render_table",
    "resolve",
]


def _detect_version() -> str:
    """版本单一来源：仓库内读 pyproject.toml，装好的包读分发元数据。"""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.is_file():
        match = re.search(
            r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M
        )
        if match:
            return match.group(1)
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("pypi-mirror-check")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover
        pass
    return "0+unknown"


__version__ = _detect_version()
USER_AGENT = f"pypi-mirror-check/{__version__}"

CONFIG_ENV = "PYPI_MIRROR_CHECK_CONFIG"


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True)
class Mirror:
    key: str
    label: str
    simple: str  # 含 {name}
    official: bool = False


@dataclass(frozen=True)
class Config:
    official_json: str
    mirrors: tuple[Mirror, ...]
    source: str = "(builtin)"


def default_config_path() -> Path:
    return Path(__file__).with_name("mirrors.json")


def load_config(path: str | Path | None = None) -> Config:
    """从 JSON 读镜像清单。顺序：显式 path → 环境变量 → 同包 mirrors.json。"""
    chosen = path or os.environ.get(CONFIG_ENV) or default_config_path()
    source = str(chosen)
    raw = json.loads(Path(chosen).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: 顶层必须是对象")
    official = raw.get("official_json")
    if not isinstance(official, str) or not official:
        raise ValueError(f"{source}: 缺少 official_json")
    items = raw.get("mirrors")
    if not isinstance(items, list) or not items:
        raise ValueError(f"{source}: mirrors 必须是非空数组")
    mirrors: list[Mirror] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"{source}: mirrors 项必须是对象")
        key = str(item.get("key") or "").strip()
        label = str(item.get("label") or key).strip()
        simple = str(item.get("simple") or "").strip()
        if not key or key in seen:
            raise ValueError(f"{source}: key 缺失或重复：{key!r}")
        if "{name}" not in simple:
            raise ValueError(f"{source}: {key} 的 simple 必须含 {{name}}")
        seen.add(key)
        mirrors.append(
            Mirror(key=key, label=label, simple=simple, official=bool(item.get("official")))
        )
    return Config(official_json=official, mirrors=tuple(mirrors), source=source)


def _add_mirror(config: Config, spec: str) -> Config:
    """--mirror key=label=url 追加/覆盖一条。"""
    parts = spec.split("=", 2)
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise SystemExit("--mirror 需要 key=label=url 三段")
    key, label, simple = (p.strip() for p in parts)
    if "{name}" not in simple:
        raise SystemExit("--mirror 的 url 必须含 {name}")
    rest = [m for m in config.mirrors if m.key != key]
    rest.append(Mirror(key=key, label=label, simple=simple))
    return Config(config.official_json, tuple(rest), config.source)


# ---------------------------------------------------------------- HTTP


def _fetch(url: str, timeout: float, retries: int = 2) -> tuple[bytes, float]:
    """GET，返回 (body, 秒)。4xx 不重试，5xx/网络错误重试。"""
    last: Exception | None = None
    for attempt in range(retries + 1):
        started = time.perf_counter()
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
            return body, time.perf_counter() - started
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < retries:
            time.sleep(0.4 * (attempt + 1))
    raise last or RuntimeError("请求失败")


# ---------------------------------------------------------------- 官方源


def resolve(
    package: str, version: str | None, timeout: float, official_json: str
) -> tuple[str, dict[str, str | None]]:
    """返回 (版本, {文件名: sha256})。版本为空则取官方最新。"""
    base = official_json.rstrip("/")
    url = f"{base}/{package}/{version}/json" if version else f"{base}/{package}/json"
    body, _ = _fetch(url, timeout)
    data = json.loads(body.decode("utf-8"))
    resolved = str(data["info"]["version"]) if version is None else version
    files: dict[str, str | None] = {}
    for item in data.get("urls") or []:
        name = item.get("filename")
        if not name:
            continue
        files[name] = (item.get("digests") or {}).get("sha256")
    if not files:
        raise RuntimeError(f"官方源没有 {package} {resolved} 的发布文件")
    return resolved, files


# ---------------------------------------------------------------- simple 页解析


class _SimpleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")

    def handle_data(self, data: str) -> None:
        if self._href is not None and data.strip():
            self.links.append((self._href, data.strip()))
            self._href = None

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a":
            self._href = None


_HASH_RE = re.compile(r"[#&]sha256=([0-9a-fA-F]{64})")


def parse_simple(html: str) -> dict[str, str | None]:
    """把 simple 页解析成 {文件名: sha256|None}。"""
    parser = _SimpleParser()
    parser.feed(html)
    out: dict[str, str | None] = {}
    for href, text in parser.links:
        match = _HASH_RE.search(href)
        out[text] = match.group(1).lower() if match else None
    return out


# ---------------------------------------------------------------- 版本号


_EXTENSIONS = (".tar.gz", ".tar.bz2", ".zip", ".whl")
_NAME_SEP_RE = re.compile(r"[._-]+")
_VERSION_RE = re.compile(
    r"^(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev(\d+))?$", re.I
)


def normalize_name(name: str) -> str:
    """PEP 503 归一化：小写、连续 [-_.] 折叠成 '-'。"""
    return _NAME_SEP_RE.sub("-", str(name or "")).lower()


def version_from_filename(filename: str, package: str) -> str | None:
    """从发布文件名里取出属于 package 的版本号（`foo-1.2.3-py3...whl` → `1.2.3`）。"""
    base = filename
    lower = base.lower()
    for ext in _EXTENSIONS:
        if lower.endswith(ext):
            base = base[: -len(ext)]
            break
    parts = base.split("-")
    target = normalize_name(package)
    for i in range(1, len(parts)):
        if _VERSION_RE.match(parts[i]) and normalize_name("-".join(parts[:i])) == target:
            return parts[i]
    return None


def version_key(version: str) -> tuple:
    """够用的 PEP 440 排序键（release / a|b|rc / post / dev）。"""
    match = _VERSION_RE.match(str(version or ""))
    if not match:
        return ((0,), 0, 0, 0, -1)
    release = tuple(int(x) for x in match.group(1).split("."))
    pre = (match.group(2) or "").lower()
    kind = {"a": 0, "b": 1, "rc": 2, "": 3}[pre]
    num = int(match.group(3) or 0)
    post = int(match.group(4) or 0)
    dev = int(match.group(5) or 0)
    return (release, kind, num, post, -dev if dev else 0)


def extract_versions(filenames: Iterable[str], package: str) -> list[str]:
    """列出这些文件覆盖的版本（升序）。"""
    found = {v for v in (version_from_filename(f, package) for f in filenames) if v}
    return sorted(found, key=version_key)


# ---------------------------------------------------------------- 检查


@dataclass
class MirrorResult:
    key: str
    label: str
    status: str  # ok | partial | missing | error
    latency_ms: int | None = None
    found: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    hash_checked: int = 0
    error: str = ""
    # 该镜像的索引页本身是否可达（None=未探测）。用来区分「包缺失」与「镜像不可用」。
    index_ok: bool | None = None
    # 该镜像上这个包的最新版本 / 版本总数 /（按需）全部版本
    latest: str = ""
    version_count: int = 0
    versions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class Report:
    package: str
    version: str
    expected: dict[str, str | None]
    results: list[MirrorResult]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def to_json(self) -> dict[str, Any]:
        rows = []
        for r in self.results:
            row: dict[str, Any] = {
                "mirror": r.key,
                "label": r.label,
                "status": r.status,
                "latest": r.latest,
                "version_count": r.version_count,
                "latency_ms": r.latency_ms,
                "found": r.found,
                "missing": r.missing,
                "mismatched": r.mismatched,
                "hash_checked": r.hash_checked,
                "index_ok": r.index_ok,
                "error": r.error,
            }
            if r.versions:
                row["versions"] = r.versions
            rows.append(row)
        return {
            "package": self.package,
            "version": self.version,
            "ok": self.ok,
            "expected_files": self.expected,
            "results": rows,
        }


def _probe_index(mirror: Mirror, timeout: float) -> bool:
    """只探镜像根索引是否可达（读几百字节就断开，不拉整页）。

    404 有两种完全不同的含义：镜像说「我没有这个包」，或这个 simple 路径压根不对 /
    站点下线。前者根索引是 200，后者不是——这一步就是把两者分开。
    """
    root = mirror.simple.split("{name}")[0]
    if not root:
        return False
    request = urllib.request.Request(
        root, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(512)
        return True
    except urllib.error.HTTPError:
        return False
    except Exception:  # noqa: BLE001
        return False


def _check_one(
    mirror: Mirror,
    package: str,
    expected: dict[str, str | None],
    timeout: float,
    require_hash: bool,
    collect_versions: bool = False,
) -> MirrorResult:
    url = mirror.simple.format(name=package)
    try:
        body, elapsed = _fetch(url, timeout)
    except urllib.error.HTTPError as exc:
        # 404/403：先看索引页在不在，能分清「无此包」与「路径不对/站点不可用」
        index_ok = _probe_index(mirror, timeout) if exc.code in (403, 404) else None
        status = "missing" if index_ok else "error"
        reason = "索引可达，但无此包" if index_ok else "索引页也不可达（路径或站点问题）"
        return MirrorResult(
            mirror.key, mirror.label, status, error=f"HTTP {exc.code}：{reason}", index_ok=index_ok
        )
    except Exception as exc:  # noqa: BLE001 - 单个镜像失败不该终止整轮
        return MirrorResult(
            mirror.key,
            mirror.label,
            "error",
            error=f"{type(exc).__name__}: {exc}",
            index_ok=_probe_index(mirror, timeout),
        )

    available = parse_simple(body.decode("utf-8", "replace"))
    versions = extract_versions(available.keys(), package)
    latest = versions[-1] if versions else ""
    found = [name for name in expected if name in available]
    missing = [name for name in expected if name not in available]
    mismatched = [
        name
        for name in found
        if expected[name] and available.get(name) and expected[name] != available.get(name)
    ]
    hash_checked = sum(1 for name in found if available.get(name))

    if not found:
        status = "missing"
    elif missing or mismatched:
        status = "partial"
    elif require_hash and hash_checked < len(found):
        status = "partial"
    else:
        status = "ok"

    return MirrorResult(
        mirror.key,
        mirror.label,
        status,
        latency_ms=int(elapsed * 1000),
        found=found,
        missing=missing,
        mismatched=mismatched,
        hash_checked=hash_checked,
        latest=latest,
        version_count=len(versions),
        versions=versions if collect_versions else [],
        index_ok=True,
    )


def check(
    package: str,
    version: str | None = None,
    *,
    config: Config | None = None,
    official_json: str | None = None,
    mirrors: Sequence[Mirror] | None = None,
    timeout: float = 10.0,
    workers: int = 8,
    require_hash: bool = False,
    collect_versions: bool = False,
) -> Report:
    cfg = config or load_config()
    base = official_json or cfg.official_json
    targets = list(cfg.mirrors if mirrors is None else mirrors)
    resolved, expected = resolve(package, version, timeout, base)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(
                _check_one, mirror, package, expected, timeout, require_hash, collect_versions
            )
            for mirror in targets
        ]
        results = [f.result() for f in futures]
    return Report(package=package, version=resolved, expected=expected, results=results)


# ---------------------------------------------------------------- 输出


STATUS_LABEL = {"ok": "已同步", "partial": "不完整", "missing": "缺失", "error": "错误"}


def render_table(report: Report) -> str:
    lines = [
        f"包 {report.package} · 目标 {report.version} · 期望 {len(report.expected)} 个文件"
    ]
    width = max([len(r.label) for r in report.results] + [4])
    lw = max([len(r.latest or "-") for r in report.results] + [8])
    lines.append(f"{'镜像'.ljust(width)}  {'站点最新'.ljust(lw)}  状态    延迟     备注")
    for r in report.results:
        note = ""
        if r.error:
            note = r.error
        elif r.missing or r.mismatched:
            bits = []
            if r.missing:
                bits.append("缺 " + ", ".join(r.missing))
            if r.mismatched:
                bits.append("sha256 不一致 " + ", ".join(r.mismatched))
            note = "；".join(bits)
        latency = f"{r.latency_ms}ms" if r.latency_ms is not None else "-"
        latest = (r.latest or "-").ljust(lw)
        lines.append(
            f"{r.label.ljust(width)}  {latest}  "
            f"{STATUS_LABEL.get(r.status, r.status):<6}  {latency:<8}  {note}"
        )
    return "\n".join(lines)


def _summary(report: Report) -> str:
    bad = [r.label for r in report.results if not r.ok]
    if not bad:
        return f"全部 {len(report.results)} 个镜像已同步 {report.package} {report.version}"
    return f"{len(bad)}/{len(report.results)} 个镜像未同步：{', '.join(bad)}"


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pypi-mirror-check",
        description="检查指定 PyPI 包/版本是否已同步到全球镜像（文件名 + sha256 比对）。",
    )
    parser.add_argument("package", nargs="?", help="包名，如 hubrelay")
    parser.add_argument("version", nargs="?", help="版本；省略则查官方最新版")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--wait", action="store_true", help="轮询直到全部同步或超时")
    parser.add_argument("--interval", type=float, default=30.0, help="轮询间隔秒（默认 30）")
    parser.add_argument("--timeout", type=float, default=900.0, help="--wait 总超时秒（默认 900）")
    parser.add_argument("--http-timeout", type=float, default=10.0, help="单请求超时秒（默认 10）")
    parser.add_argument("--workers", type=int, default=8, help="并发数（默认 8）")
    parser.add_argument("--only", default="", help="只查这些镜像，逗号分隔（如 tuna,aliyun）")
    parser.add_argument("--require-hash", action="store_true", help="镜像页没给 sha256 也算未同步")
    parser.add_argument(
        "--all-versions", action="store_true", help="同时列出各镜像上的全部版本"
    )
    parser.add_argument("--config", default="", help="镜像清单 JSON（默认同包 mirrors.json）")
    parser.add_argument("--official", default="", help="覆盖官方 JSON 基址")
    parser.add_argument(
        "--mirror",
        action="append",
        default=[],
        metavar="KEY=LABEL=URL",
        help="临时追加/覆盖一个镜像（可重复；URL 需含 {name}）",
    )
    parser.add_argument("--list-mirrors", action="store_true", help="列出镜像后退出")
    return parser


def _select_mirrors(config: Config, only: str) -> list[Mirror]:
    if not only.strip():
        return list(config.mirrors)
    keys = [k.strip() for k in only.split(",") if k.strip()]
    by_key = {m.key: m for m in config.mirrors}
    unknown = [k for k in keys if k not in by_key]
    if unknown:
        raise SystemExit(f"未知镜像：{', '.join(unknown)}（--list-mirrors 查看）")
    return [by_key[k] for k in keys]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

    try:
        config = load_config(args.config or None)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"读取镜像配置失败：{exc}", file=sys.stderr)
        return 2
    for spec in args.mirror:
        config = _add_mirror(config, spec)
    official = args.official or config.official_json

    if args.list_mirrors:
        print(f"配置来源：{config.source}")
        print(f"官方 JSON：{official}")
        for m in config.mirrors:
            print(f"{m.key:<9} {m.label:<12} {m.simple.format(name='<pkg>')}")
        return 0
    if not args.package:
        build_parser().print_help()
        return 2

    mirrors = _select_mirrors(config, args.only)
    deadline = time.monotonic() + max(0.0, args.timeout)

    while True:
        try:
            report = check(
                args.package,
                args.version,
                config=config,
                official_json=official,
                mirrors=mirrors,
                timeout=args.http_timeout,
                workers=args.workers,
                require_hash=args.require_hash,
                collect_versions=args.all_versions,
            )
        except Exception as exc:  # noqa: BLE001 - 官方源失败要给清晰退出码
            print(f"无法解析官方发布信息：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

        if args.json:
            print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
        else:
            print(render_table(report))
            print(_summary(report))
            if args.all_versions:
                print()
                for r in report.results:
                    if r.versions:
                        print(f"  {r.label}（{r.version_count}）: {', '.join(r.versions)}")

        if report.ok or not args.wait:
            return 0 if report.ok else 1
        if time.monotonic() + args.interval > deadline:
            print(f"等待超时（{int(args.timeout)}s），仍有镜像未同步", file=sys.stderr)
            return 3
        time.sleep(args.interval)
