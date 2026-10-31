"""pypi-mirror-check 的单元测试（纯标准库，本地假站点）。"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pypi_mirror_check as pmc  # noqa: E402

SHA_TAR = "a" * 64
SHA_WHL = "b" * 64
FILES = {"foo-1.0.tar.gz": SHA_TAR, "foo-1.0-py3-none-any.whl": SHA_WHL}


def simple_html(files: dict[str, str | None]) -> str:
    parts = []
    for name, sha in files.items():
        href = f"/files/{name}" + (f"#sha256={sha}" if sha else "")
        parts.append(f'<a href="{href}">{name}</a>')
    return "<html><body>" + "".join(parts) + "</body></html>"


class Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, object, str]] = {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        item = self.routes.get(path)
        if item is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        status, body, ctype = item
        raw = body.encode("utf-8") if isinstance(body, str) else body  # type: ignore[arg-type]
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        pass


class MirrorCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        release = {
            "info": {"version": "1.0"},
            "urls": [
                {"filename": name, "digests": {"sha256": sha}}
                for name, sha in FILES.items()
            ],
        }
        Handler.routes = {
            "/pypi/foo/1.0/json": (200, json.dumps(release), "application/json"),
            "/pypi/foo/json": (200, json.dumps(release), "application/json"),
            "/simple/ok/foo/": (200, simple_html(FILES), "text/html"),
            "/simple/missing/foo/": (200, "<html></html>", "text/html"),
            "/simple/partial/foo/": (
                200,
                simple_html({"foo-1.0.tar.gz": SHA_TAR}),
                "text/html",
            ),
            "/simple/badhash/foo/": (
                200,
                simple_html({"foo-1.0.tar.gz": "c" * 64}),
                "text/html",
            ),
            "/simple/nohash/foo/": (
                200,
                simple_html({name: None for name in FILES}),
                "text/html",
            ),
            "/simple/boom/foo/": (500, "boom", "text/plain"),
            # 根索引可达、但没有这个包 → 应是 missing 而不是 error
            "/simple/pkg404/": (200, "<html></html>", "text/html"),
            "/simple/cdnlag/": (200, "<html></html>", "text/html"),
            "/simple/behind/foo/": (200, simple_html({"foo-0.9.tar.gz": "d" * 64}), "text/html"),
            "/simple/multi/foo/": (
                200,
                simple_html({**FILES, "foo-0.9.tar.gz": "d" * 64}),
                "text/html",
            ),
        }
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def mirror(self, key: str, path: str) -> pmc.Mirror:
        return pmc.Mirror(key, key, f"{self.base}/simple/{path}/{{name}}/")

    def run_check(self, mirrors, **kwargs):
        return pmc.check(
            "foo",
            "1.0",
            official_json=self.base + "/pypi",
            mirrors=mirrors,
            timeout=5,
            **kwargs,
        )

    # ------------------------------------------------------------ 配置

    def test_builtin_config_loads(self) -> None:
        cfg = pmc.load_config()
        self.assertTrue(cfg.official_json.startswith("http"))
        self.assertGreaterEqual(len(cfg.mirrors), 3)
        self.assertTrue(all("{name}" in m.simple for m in cfg.mirrors))

    def test_config_from_file_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "m.json"
            good.write_text(
                json.dumps(
                    {
                        "official_json": "http://x/pypi",
                        "mirrors": [{"key": "a", "label": "A", "simple": "http://m/{name}/"}],
                    }
                ),
                encoding="utf-8",
            )
            cfg = pmc.load_config(good)
            self.assertEqual(cfg.official_json, "http://x/pypi")
            self.assertEqual([m.key for m in cfg.mirrors], ["a"])

            bad = Path(tmp) / "bad.json"
            bad.write_text(
                json.dumps({"official_json": "http://x", "mirrors": [{"key": "b", "simple": "http://m/"}]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                pmc.load_config(bad)

    def test_config_rejects_bad_official_and_name_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad_official = Path(tmp) / "a.json"
            bad_official.write_text(
                json.dumps({"official_json": "ftp://x", "mirrors": [{"key": "a", "simple": "http://m/{name}/"}]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                pmc.load_config(bad_official)

            bad_pos = Path(tmp) / "b.json"
            bad_pos.write_text(
                json.dumps(
                    {
                        "official_json": "http://x",
                        "mirrors": [{"key": "a", "simple": "http://m/{name}/sub/"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                pmc.load_config(bad_pos)

    def test_add_mirror_override(self) -> None:
        cfg = pmc.load_config()
        merged = pmc._add_mirror(cfg, "tuna=TUNA=http://m/{name}/")
        tuna = [m for m in merged.mirrors if m.key == "tuna"]
        self.assertEqual(len(tuna), 1)
        self.assertEqual(tuna[0].simple, "http://m/{name}/")

    # ------------------------------------------------------------ 解析 / 判定

    def test_parse_simple_extracts_names_and_hashes(self) -> None:
        self.assertEqual(pmc.parse_simple(simple_html(FILES)), FILES)
        self.assertEqual(pmc.parse_simple(simple_html({"x.tar.gz": None})), {"x.tar.gz": None})

    def test_version_from_filename(self) -> None:
        self.assertEqual(pmc.version_from_filename("foo-1.0.tar.gz", "foo"), "1.0")
        self.assertEqual(
            pmc.version_from_filename("foo-1.0-py3-none-any.whl", "foo"), "1.0"
        )
        self.assertEqual(
            pmc.version_from_filename("python-dateutil-2.8.2.tar.gz", "python-dateutil"),
            "2.8.2",
        )
        self.assertEqual(
            pmc.version_from_filename("typing_extensions-4.9.0-py3-none-any.whl", "typing-extensions"),
            "4.9.0",
        )
        self.assertIsNone(pmc.version_from_filename("other-1.0.tar.gz", "foo"))

    def test_version_key_ordering(self) -> None:
        versions = ["1.0", "0.9.10", "1.0rc1", "0.9.9"]
        self.assertEqual(
            sorted(versions, key=pmc.version_key), ["0.9.9", "0.9.10", "1.0rc1", "1.0"]
        )

    def test_local_version(self) -> None:
        self.assertEqual(
            pmc.version_from_filename("torch-2.1.0+cu118-cp311-cp311-win_amd64.whl", "torch"),
            "2.1.0+cu118",
        )
        self.assertEqual(
            sorted(["1.0", "1.0+cu118"], key=pmc.version_key), ["1.0", "1.0+cu118"]
        )

    def test_wait_json_prints_single_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "c.json"
            cfg.write_text(
                json.dumps(
                    {
                        "official_json": self.base + "/pypi",
                        "mirrors": [
                            {"key": "m", "label": "M", "simple": self.base + "/simple/missing/{name}/"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                code = pmc.main(
                    ["--config", str(cfg), "foo", "1.0", "--wait", "--timeout", "0", "--json"]
                )
            finally:
                sys.stdout = old
            self.assertEqual(code, 3)
            payload = json.loads(buf.getvalue())  # 必须是单个合法 JSON，不是多段拼接
            self.assertEqual(payload["version"], "1.0")
            self.assertFalse(payload["ok"])

    def test_version_key_orders_dev_before_pre(self) -> None:
        versions = ["1.0.post1", "1.0", "1.0a1", "1.0a1.dev1", "1.0.dev1"]
        self.assertEqual(
            sorted(versions, key=pmc.version_key),
            ["1.0.dev1", "1.0a1.dev1", "1.0a1", "1.0", "1.0.post1"],
        )

    def test_parse_simple_handles_nested_tags(self) -> None:
        html = '<a href="/f/foo-1.0.tar.gz#sha256=' + SHA_TAR + '"><span>foo-1.0.tar.gz</span></a>'
        self.assertEqual(pmc.parse_simple(html), {"foo-1.0.tar.gz": SHA_TAR})

    def test_cli_only_unknown_returns_2(self) -> None:
        self.assertEqual(pmc.main(["--only", "nope", "foo"]), 2)

    def test_cli_version_flag(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            pmc.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_latest_and_all_versions(self) -> None:
        mirror = self.mirror("multi", "multi")
        result = self.run_check([mirror]).results[0]
        self.assertEqual(result.status, "ok")  # 1.0 在
        self.assertEqual(result.latest, "1.0")
        self.assertEqual(result.version_count, 2)
        self.assertEqual(result.versions, [])  # 未要求就不回全量
        full = self.run_check([mirror], collect_versions=True).results[0]
        self.assertEqual(full.versions, ["0.9", "1.0"])

    def test_statuses(self) -> None:
        report = self.run_check(
            [
                self.mirror("ok", "ok"),
                self.mirror("missing", "missing"),
                self.mirror("partial", "partial"),
                self.mirror("badhash", "badhash"),
                self.mirror("boom", "boom"),
            ]
        )
        by = {r.key: r for r in report.results}
        self.assertEqual(by["ok"].status, "ok")
        self.assertEqual(by["missing"].status, "missing")
        self.assertEqual(by["partial"].status, "partial")
        self.assertEqual(by["badhash"].status, "integrity")
        self.assertEqual(by["badhash"].mismatched, ["foo-1.0.tar.gz"])
        self.assertIn(by["boom"].status, ("error", "missing"))
        self.assertFalse(report.ok)
        self.assertEqual(report.version, "1.0")

    def test_404_distinguishes_missing_package_from_dead_index(self) -> None:
        # 索引可达、没有包 → missing
        missing = self.run_check([self.mirror("pkg404", "pkg404")]).results[0]
        self.assertEqual(missing.status, "missing")
        self.assertTrue(missing.index_ok)
        self.assertIn("无此包", missing.error)
        # 索引本身 404 → error（路径不对 / 站点不可用）
        dead = self.run_check([self.mirror("dead", "dead")]).results[0]
        self.assertEqual(dead.status, "error")
        self.assertFalse(dead.index_ok)
        self.assertIn("索引页", dead.error)

    def test_caching_when_json_has_but_simple_lags(self) -> None:
        mirror = pmc.Mirror(
            "cdn",
            "CDN",
            f"{self.base}/simple/cdnlag/{{name}}/",
            json=f"{self.base}/pypi/{{name}}/{{version}}/json",
        )
        result = self.run_check([mirror]).results[0]
        self.assertEqual(result.status, "caching")
        self.assertTrue(result.index_ok)
        self.assertIn("CDN 缓存", result.error)

    def test_caching_note_mentions_stale_index_version(self) -> None:
        mirror = pmc.Mirror(
            "behind",
            "Behind",
            f"{self.base}/simple/behind/{{name}}/",
            json=f"{self.base}/pypi/{{name}}/{{version}}/json",
        )
        result = self.run_check([mirror]).results[0]
        self.assertEqual(result.status, "caching")
        self.assertEqual(result.latest, "0.9")
        self.assertIn("索引仍为 0.9", result.error)
        table = pmc.render_table(self.run_check([mirror]))
        self.assertIn("不是目标版本", table)

    def test_ok_report(self) -> None:
        report = self.run_check([self.mirror("ok", "ok")])
        self.assertTrue(report.ok)
        self.assertEqual(report.expected, FILES)

    def test_require_hash_turns_nohash_into_partial(self) -> None:
        m = self.mirror("nohash", "nohash")
        self.assertEqual(self.run_check([m]).results[0].status, "ok")
        self.assertEqual(
            self.run_check([m], require_hash=True).results[0].status, "partial"
        )

    def test_latest_version_when_omitted(self) -> None:
        report = pmc.check(
            "foo", None, official_json=self.base + "/pypi",
            mirrors=[self.mirror("ok", "ok")], timeout=5,
        )
        self.assertEqual(report.version, "1.0")

    def test_unknown_version_raises(self) -> None:
        with self.assertRaises(Exception):
            pmc.check("foo", "9.9", official_json=self.base + "/pypi", mirrors=[], timeout=5)

    # ------------------------------------------------------------ 输出 / CLI

    def test_json_and_table(self) -> None:
        report = self.run_check([self.mirror("ok", "ok")])
        payload = json.loads(json.dumps(report.to_json()))
        self.assertEqual(payload["version"], "1.0")
        self.assertTrue(payload["ok"])
        table = pmc.render_table(report)
        self.assertIn("包 foo", table)
        self.assertIn("目标 1.0", table)
        self.assertIn("索引最新", table)

    def test_cli_list_mirrors_and_usage(self) -> None:
        self.assertEqual(pmc.main(["--list-mirrors"]), 0)
        self.assertEqual(pmc.main([]), 2)

    def test_cli_bad_config_returns_2(self) -> None:
        self.assertEqual(pmc.main(["--config", "nope.json", "foo"]), 2)


if __name__ == "__main__":
    unittest.main()
