"""冒烟测试：覆盖配置、模型、本地检索与 ima 检索的纯逻辑（无需 GUI）。

运行：
  venv/Scripts/python.exe -m pytest tests/smoke_test.py -q
  或
  venv/Scripts/python.exe tests/smoke_test.py
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfg_mod
from core.config import Settings, load_settings, save_settings
from core import format_sort
from core.models import SearchResult
from search import local_search, ima_search


class TestConfig(unittest.TestCase):
    def test_load_everything_default(self):
        c = cfg_mod.load_everything_config()
        self.assertEqual(c.host, "127.0.0.1")
        self.assertEqual(c.port, 80)

    def test_load_everything_from_env(self):
        with mock.patch.dict(os.environ, {"EVERYTHING_HTTP_PORT": "8920"}):
            c = cfg_mod.load_everything_config()
            self.assertEqual(c.port, 8920)
            self.assertEqual(c.base_url, "http://127.0.0.1:8920")

    def test_load_ima_from_env(self):
        with mock.patch.dict(os.environ, {"IMA_OPENAPI_CLIENTID": "cid", "IMA_OPENAPI_APIKEY": "key"}):
            c = cfg_mod.load_ima_config()
            self.assertTrue(c.has_credentials)
            self.assertEqual(c.client_id, "cid")

    def test_save_and_load_ima_credentials(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        env = {
            "IMA_CONFIG_DIR": tmp,
            "IMA_OPENAPI_CLIENTID": "",
            "IMA_OPENAPI_APIKEY": "",
            "IMA_CLIENT_ID": "",
            "IMA_API_KEY": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg_mod.save_ima_credentials("my-cid", "my-key")
            c = cfg_mod.load_ima_config()
        self.assertEqual(c.client_id, "my-cid")
        self.assertEqual(c.api_key, "my-key")


class TestModels(unittest.TestCase):
    def test_source_label(self):
        self.assertEqual(SearchResult(source="local", title="a").source_label(), "本地")
        self.assertEqual(SearchResult(source="ima", title="b").source_label(), "ima")


class TestLocalSearch(unittest.TestCase):
    def test_filetime_to_datetime(self):
        # 2021-01-01 00:00:00 UTC 的近似 FILETIME
        ft = int((datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() + 11644473600) * 10_000_000)
        dt = local_search._filetime_to_datetime(ft)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2021)

    def test_parse_json(self):
        fake = mock.Mock()
        fake.json.return_value = {
            "totalResults": 2,
            "results": [
                {"path": "C:\\Users\\x\\a.pdf", "size": 1024, "date_modified": 0},
                {"path": "C:\\Users\\x\\b.docx", "size": 2048},
            ],
        }
        fake.raise_for_status.return_value = None
        with mock.patch.object(local_search.requests, "get", return_value=fake):
            res = local_search.search_local("report", cfg_mod.load_everything_config())
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0].title, "a.pdf")
        self.assertEqual(res[0].size, 1024)
        self.assertEqual(res[1].source, "local")

    def test_sort_params_sent(self):
        captured = {}

        def fake_get(url, params=None, timeout=None, proxies=None):
            captured["params"] = params
            f = mock.Mock()
            f.json.return_value = {"totalResults": 0, "results": []}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(local_search.requests, "get", side_effect=fake_get):
            local_search.search_local("x", cfg_mod.load_everything_config())
        p = captured["params"]
        self.assertEqual(p["sort"], "name")
        self.assertEqual(p["ascending"], 1)

        # 自定义排序（大小降序）应透传
        c = cfg_mod.EverythingConfig(sort="size", ascending=False)
        with mock.patch.object(local_search.requests, "get", side_effect=fake_get):
            local_search.search_local("x", c)
        self.assertEqual(captured["params"]["sort"], "size")
        self.assertEqual(captured["params"]["ascending"], 0)

    def test_size_string_parsed(self):
        # Everything 以字符串返回 size（如 '15722453382'），文件夹为 '' 空串
        fake = mock.Mock()
        fake.json.return_value = {
            "totalResults": 2,
            "results": [
                {"name": "a.zip", "path": "C:\\x", "size": "15722453382", "date_modified": 0},
                {"name": "folder", "path": "C:\\x", "size": "", "date_modified": 0},
            ],
        }
        fake.raise_for_status.return_value = None
        with mock.patch.object(local_search.requests, "get", return_value=fake):
            res = local_search.search_local("x", cfg_mod.load_everything_config())
        self.assertEqual(res[0].size, 15722453382)
        self.assertIsNone(res[1].size)

    def test_kind_parsed(self):
        # Everything 返回 type 字段（file/folder），用于显示系统图标
        fake = mock.Mock()
        fake.json.return_value = {
            "totalResults": 2,
            "results": [
                {"name": "a.zip", "path": "C:\\x", "type": "file", "size": "1", "date_modified": 0},
                {"name": "sub", "path": "C:\\x", "type": "folder", "size": "", "date_modified": 0},
            ],
        }
        fake.raise_for_status.return_value = None
        with mock.patch.object(local_search.requests, "get", return_value=fake):
            res = local_search.search_local("x", cfg_mod.load_everything_config())
        self.assertEqual(res[0].kind, "file")
        self.assertEqual(res[1].kind, "folder")


class TestSettings(unittest.TestCase):
    def test_default(self):
        s = Settings()
        self.assertFalse(s.format_sort_enabled)
        self.assertTrue(s.folders_first)
        self.assertTrue(s.group_by_name)
        self.assertIn("pdf", s.format_order)

    def test_roundtrip(self):
        s = Settings(format_sort_enabled=True, folders_first=False,
                     format_order=["pdf", "docx", "XLSX"])
        save_settings(s)
        loaded = load_settings()
        self.assertTrue(loaded.format_sort_enabled)
        self.assertFalse(loaded.folders_first)
        self.assertEqual(loaded.format_order, ["pdf", "docx", "xlsx"])  # 归一化小写
        # 恢复默认，避免污染项目设置文件
        save_settings(Settings())

    def test_reorder_by_format(self):
        s = Settings(format_sort_enabled=True, folders_first=True,
                     format_order=["pdf", "docx"])
        results = [
            SearchResult(source="local", title="b.xlsx", kind="file"),
            SearchResult(source="local", title="a.pdf", kind="file"),
            SearchResult(source="local", title="c.docx", kind="file"),
            SearchResult(source="ima", title="i2.pdf"),
            SearchResult(source="ima", title="i3.docx"),
            SearchResult(source="ima", title="note"),  # 无扩展名 -> 未列入
            SearchResult(source="local", title="z folder", kind="folder"),
        ]
        out = format_sort.reorder_by_format(results, s)
        titles = [r.title for r in out]
        # 文件夹置顶 -> pdf(本地 a.pdf, ima i2.pdf) -> docx(本地 c.docx, ima i3.docx)
        #   -> 未列入(b.xlsx, note)。ima 不再固定排最后，而是按格式与本地混排。
        self.assertEqual(
            titles,
            ["z folder", "a.pdf", "i2.pdf", "c.docx", "i3.docx", "b.xlsx", "note"],
        )

    def test_folder_as_format_first(self):
        # 'folder' 列入格式列表：文件夹按其所列位置与各格式混排
        s = Settings(format_sort_enabled=True, folders_first=True,
                     format_order=["folder", "pdf", "docx"])
        results = [
            SearchResult(source="local", title="a.pdf", kind="file"),
            SearchResult(source="local", title="z folder", kind="folder"),
            SearchResult(source="local", title="c.docx", kind="file"),
        ]
        out = format_sort.reorder_by_format(results, s)
        self.assertEqual([r.title for r in out], ["z folder", "a.pdf", "c.docx"])

    def test_folder_position_in_list(self):
        # 'folder' 放在中间：覆盖 folders_first，文件夹排在 pdf 与 docx 之间
        s = Settings(format_sort_enabled=True, folders_first=True,
                     format_order=["pdf", "folder", "docx"])
        results = [
            SearchResult(source="local", title="a.pdf", kind="file"),
            SearchResult(source="local", title="c.docx", kind="file"),
            SearchResult(source="local", title="z folder", kind="folder"),
        ]
        out = format_sort.reorder_by_format(results, s)
        self.assertEqual([r.title for r in out], ["a.pdf", "z folder", "c.docx"])

    def test_folder_not_listed_not_first_goes_last(self):
        # 未列入 folder 且不置顶：文件夹按未列入处理，排在已列格式之后
        s = Settings(format_sort_enabled=True, folders_first=False,
                     format_order=["pdf"])
        results = [
            SearchResult(source="local", title="z folder", kind="folder"),
            SearchResult(source="local", title="a.pdf", kind="file"),
        ]
        out = format_sort.reorder_by_format(results, s)
        self.assertEqual([r.title for r in out], ["a.pdf", "z folder"])

    def test_reorder_disabled_keeps_order(self):
        s = Settings(format_sort_enabled=False, group_by_name=False,
                     format_order=["pdf", "docx"])
        results = [
            SearchResult(source="local", title="b.xlsx", kind="file"),
            SearchResult(source="local", title="a.pdf", kind="file"),
        ]
        out = format_sort.reorder_by_format(results, s)
        self.assertEqual([r.title for r in out], ["b.xlsx", "a.pdf"])

    def test_group_by_name_mixes_sources(self):
        # 同名本地+ima 归组到首次出现处，混排（不再分本地/ima 两部分）
        s = Settings(format_sort_enabled=False, group_by_name=True)
        results = [
            SearchResult(source="local", title="a.pdf", kind="file"),
            SearchResult(source="local", title="b.pdf", kind="file"),
            SearchResult(source="ima", title="a.pdf"),
            SearchResult(source="ima", title="b.pdf"),
            SearchResult(source="ima", title="c.pdf"),  # ima 独有，跟在后面
        ]
        out = format_sort.reorder_by_format(results, s)
        self.assertEqual(
            [(r.title, r.source) for r in out],
            [("a.pdf", "local"), ("a.pdf", "ima"),
             ("b.pdf", "local"), ("b.pdf", "ima"),
             ("c.pdf", "ima")],
        )

    def test_group_by_name_disabled_keeps_separated(self):
        # 关闭同名归组：保持“本地一堆 / ima 一堆”的原始顺序
        s = Settings(format_sort_enabled=False, group_by_name=False)
        results = [
            SearchResult(source="local", title="a.pdf", kind="file"),
            SearchResult(source="local", title="b.pdf", kind="file"),
            SearchResult(source="ima", title="a.pdf"),
            SearchResult(source="ima", title="b.pdf"),
        ]
        out = format_sort.reorder_by_format(results, s)
        self.assertEqual(
            [(r.title, r.source) for r in out],
            [("a.pdf", "local"), ("b.pdf", "local"),
             ("a.pdf", "ima"), ("b.pdf", "ima")],
        )

    def test_group_by_name_with_format(self):
        # 格式优先 + 同名归组：同一格式内同名相邻混排
        s = Settings(format_sort_enabled=True, group_by_name=True,
                     format_order=["pdf"])
        results = [
            SearchResult(source="local", title="a.pdf", kind="file"),
            SearchResult(source="local", title="b.pdf", kind="file"),
            SearchResult(source="ima", title="a.pdf"),
            SearchResult(source="ima", title="b.pdf"),
        ]
        out = format_sort.reorder_by_format(results, s)
        self.assertEqual(
            [(r.title, r.source) for r in out],
            [("a.pdf", "local"), ("a.pdf", "ima"),
             ("b.pdf", "local"), ("b.pdf", "ima")],
        )

    def test_group_by_name_case_insensitive(self):
        # 忽略大小写：Report.pdf 与 report.pdf 视为同名归组
        s = Settings(format_sort_enabled=False, group_by_name=True)
        results = [
            SearchResult(source="local", title="Report.pdf", kind="file"),
            SearchResult(source="ima", title="report.pdf"),
        ]
        out = format_sort.reorder_by_format(results, s)
        self.assertEqual(
            [(r.title, r.source) for r in out],
            [("Report.pdf", "local"), ("report.pdf", "ima")],
        )


class TestImaSearch(unittest.TestCase):
    def _cfg(self):
        return cfg_mod.ImaConfig(client_id="cid", api_key="key")

    def test_list_kb(self):
        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            f.json.return_value = {"code": 0, "msg": "ok",
                                   "data": {"info_list": [{"id": "kb1", "name": "KB1"}],
                                            "is_end": True, "next_cursor": ""}}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post):
            kbs = ima_search.list_knowledge_bases(self._cfg(), "q")
        self.assertEqual(kbs, [{"id": "kb1", "name": "KB1"}])

    def test_search_ima(self):
        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            data = {}
            if url.endswith("search_knowledge_base"):
                data = {"info_list": [{"id": "kb1", "name": "KB1"}], "is_end": True, "next_cursor": ""}
            elif url.endswith("search_knowledge"):
                data = {"info_list": [{"media_id": "m1", "title": "Doc", "highlight_content": "<em>q</em>"}],
                        "is_end": True, "next_cursor": ""}
            f.json.return_value = {"code": 0, "msg": "ok", "data": data}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post):
            res, errs = ima_search.search_ima("q", self._cfg())
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].source, "ima")
        self.assertEqual(res[0].snippet, "<em>q</em>")
        self.assertEqual(errs, [])

    def test_search_ima_no_creds(self):
        c = cfg_mod.ImaConfig(client_id="", api_key="")
        res, errs = ima_search.search_ima("q", c)
        self.assertEqual(res, [])
        self.assertTrue(errs)

    def test_search_ima_retries_rate_limit_then_succeeds(self):
        # 前两次返回限流 200001，第三次成功 → 重试逻辑应最终拿到结果
        calls = {"n": 0}

        def fake_post(url, json=None, headers=None, timeout=None):
            calls["n"] += 1
            if url.endswith("search_knowledge_base"):
                f = mock.Mock()
                f.status_code = 200
                f.json.return_value = {"code": 0, "msg": "ok",
                                       "data": {"info_list": [{"id": "kb1", "name": "KB1"}],
                                                "is_end": True, "next_cursor": ""}}
                f.raise_for_status.return_value = None
                return f
            # search_knowledge：前两次限流，第三次成功
            if calls["n"] <= 2:
                raise ima_search.ImaApiError("请求频率超限，请稍后重试", 200001)
            f = mock.Mock()
            f.status_code = 200
            f.json.return_value = {"code": 0, "msg": "ok",
                                   "data": {"info_list": [{"media_id": "m1", "title": "Doc",
                                                           "highlight_content": "<em>q</em>"}],
                                            "is_end": True, "next_cursor": ""}}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post), \
                mock.patch.object(ima_search.time, "sleep", return_value=None):
            res, errs = ima_search.search_ima("q", self._cfg())
        self.assertEqual(len(res), 1)
        self.assertEqual(errs, [])
        self.assertGreaterEqual(calls["n"], 3)

    def test_search_ima_rate_limit_exhausted_reports_error(self):
        # 始终限流 → 重试耗尽后应在 errors 中记录，而不是崩溃或静默丢结果
        def fake_post(url, json=None, headers=None, timeout=None):
            if url.endswith("search_knowledge_base"):
                f = mock.Mock()
                f.status_code = 200
                f.json.return_value = {"code": 0, "msg": "ok",
                                       "data": {"info_list": [{"id": "kb1", "name": "KB1"}],
                                                "is_end": True, "next_cursor": ""}}
                f.raise_for_status.return_value = None
                return f
            raise ima_search.ImaApiError("请求频率超限，请稍后重试", 200001)

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post), \
                mock.patch.object(ima_search.time, "sleep", return_value=None):
            res, errs = ima_search.search_ima("q", self._cfg())
        self.assertEqual(res, [])
        self.assertEqual(len(errs), 1)
        self.assertIn("请求频率超限", errs[0])

    def test_search_ima_progress_callback(self):
        # progress_callback 应在每个知识库检索完成后回调 (done, total)
        seen = []

        def cb(done, total):
            seen.append((done, total))

        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            if url.endswith("search_knowledge_base"):
                data = {"info_list": [{"id": "kb1", "name": "KB1"}, {"id": "kb2", "name": "KB2"}],
                        "is_end": True, "next_cursor": ""}
            else:
                data = {"info_list": [], "is_end": True, "next_cursor": ""}
            f.json.return_value = {"code": 0, "msg": "ok", "data": data}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post), \
                mock.patch.object(ima_search.time, "sleep", return_value=None):
            res, errs = ima_search.search_ima("q", self._cfg(), progress_callback=cb)
        self.assertEqual(seen, [(1, 2), (2, 2)])
        self.assertEqual(errs, [])

    def test_get_kb_numeric_id(self):
        # 根目录 current_path[0].folder_id 即网页数字 knowledgeBaseId
        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            f.json.return_value = {"code": 0, "msg": "ok", "data": {
                "current_path": [{"folder_id": "7451978007345957", "name": "论文集"}],
                "knowledge_list": [], "is_end": True, "next_cursor": ""}}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post):
            nid = ima_search.get_kb_numeric_id(self._cfg(), "b64kb")
        self.assertEqual(nid, "7451978007345957")

    def test_get_kb_numeric_id_fallback_parent_folder(self):
        # 无 current_path 时回退到根条目 parent_folder_id
        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            f.json.return_value = {"code": 0, "msg": "ok", "data": {
                "knowledge_list": [{"media_id": "m", "parent_folder_id": "123"}],
                "is_end": True, "next_cursor": ""}}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post):
            nid = ima_search.get_kb_numeric_id(self._cfg(), "b64kb")
        self.assertEqual(nid, "123")

    def test_get_kb_numeric_id_error_returns_none(self):
        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            f.json.return_value = {"code": 200001, "msg": "limited", "data": {}}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post):
            nid = ima_search.get_kb_numeric_id(self._cfg(), "b64kb")
        self.assertIsNone(nid)

    def test_ima_client_kb_url(self):
        d = ima_search.ima_client_kb_url("7451978007345957")
        self.assertEqual(
            d,
            "imacopilot://open?url=chrome-extension://"
            "nkohmbngmopdajidckglcoehlaeepeoi/index.html?knowledgeBaseId=7451978007345957")

    def test_ima_client_doc_url(self):
        d = ima_search.ima_client_doc_url("7451978007345957", "pdf_abc_123")
        self.assertEqual(
            d,
            "imacopilot://knowledgeDetail?knowledgeBaseId=7451978007345957"
            "&mediaId=pdf_abc_123&launchChannel=10000154")


if __name__ == "__main__":
    unittest.main(verbosity=2)
