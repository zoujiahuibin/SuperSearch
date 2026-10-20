"""冒烟测试：覆盖配置、模型、本地检索与 ima 检索的纯逻辑（无需 GUI）。

运行：
  venv/Scripts/python.exe -m pytest tests/smoke_test.py -q
  或
  venv/Scripts/python.exe tests/smoke_test.py
"""
import os
import sys
import unittest
import subprocess
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfg_mod
from core import ev_bundler
from core.config import Settings, load_settings, save_settings, EverythingConfig
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

    def test_build_drive_filter(self):
        # 空 -> 不限定（搜全部盘符）
        self.assertEqual(local_search._build_drive_filter([]), "")
        self.assertEqual(local_search._build_drive_filter(None), "")
        # 单盘：必须是 path:C:（带冒号、无结尾反斜杠）；
        # 旧写法 path:C:\ 会被 Everything 匹配成 0 条，必须避免。
        self.assertEqual(local_search._build_drive_filter(["C"]), "path:C:")
        self.assertEqual(local_search._build_drive_filter(["C:"]), "path:C:")
        self.assertEqual(local_search._build_drive_filter(["c:"]), "path:C:")
        # 多盘：空格 + | 连接、不套括号（(path:C: | path:F:) 会被 Everything 整体忽略）
        self.assertEqual(
            local_search._build_drive_filter(["C", "F"]),
            "path:C: | path:F:",
        )
        self.assertEqual(
            local_search._build_drive_filter(["C:", "E:", "F:"]),
            "path:C: | path:E: | path:F:",
        )
        # 含空/非法项应被跳过
        self.assertEqual(
            local_search._build_drive_filter(["C", "", "  "]),
            "path:C:",
        )

    def test_search_local_applies_drive_filter(self):
        # 勾选 C/F 时，search 参数必须含 path:C: | path:F:，且不应有多余反斜杠/括号
        captured = {}

        def fake_get(url, params=None, timeout=None, proxies=None):
            captured["params"] = params
            fr = mock.Mock()
            fr.json.return_value = {"totalResults": 0, "results": []}
            fr.raise_for_status.return_value = None
            return fr

        with mock.patch.object(local_search.requests, "get", side_effect=fake_get):
            local_search.search_local("apple", cfg_mod.load_everything_config(),
                                     drives=["C", "F"])
        self.assertEqual(captured["params"]["search"], "apple path:C: | path:F:")


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
                                   "data": {"info_list": [{"id": "kb1", "name": "KB1",
                                                          "base_type": "个人知识库"}],
                                            "is_end": True, "next_cursor": ""}}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post):
            kbs = ima_search.list_knowledge_bases(self._cfg(), "q")
        self.assertEqual(kbs, [{"id": "kb1", "name": "KB1", "base_type": "个人知识库"}])

    def test_kb_matches_types(self):
        # 子串匹配：base_type 含对应关键字即命中
        self.assertTrue(ima_search._kb_matches_types(
            {"base_type": "个人知识库"}, ["个人知识库"]))
        self.assertTrue(ima_search._kb_matches_types(
            {"base_type": "我加入的订阅知识库"}, ["订阅知识库"]))
        self.assertFalse(ima_search._kb_matches_types(
            {"base_type": "个人知识库"}, ["订阅知识库"]))
        # None -> 命中全部（不限类型）
        self.assertTrue(ima_search._kb_matches_types({"base_type": "x"}, None))
        # [] -> 全不选，命中 none（不检索任何类型）
        self.assertFalse(ima_search._kb_matches_types({"base_type": "x"}, []))
        # 未知 base_type + 全选标签 -> 命中 none（保持"全选=不限"语义，不误排除）
        self.assertFalse(ima_search._kb_matches_types(
            {"base_type": "团队知识库"}, ["个人知识库", "共享知识库", "订阅知识库"]))

    def test_search_ima_type_filter(self):
        # 只选「个人知识库」时，应只检索个人库，跳过订阅库
        searched = []

        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            if url.endswith("search_knowledge_base"):
                data = {"info_list": [
                    {"id": "kb1", "name": "Personal", "base_type": "个人知识库"},
                    {"id": "kb2", "name": "Sub", "base_type": "我加入的订阅知识库"},
                ], "is_end": True, "next_cursor": ""}
            elif url.endswith("search_knowledge"):
                searched.append(json.get("knowledge_base_id"))
                data = {"info_list": [{"media_id": "m1", "title": "Doc"}],
                        "is_end": True, "next_cursor": ""}
            f.json.return_value = {"code": 0, "msg": "ok", "data": data}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post):
            res, errs = ima_search.search_ima("q", self._cfg(), kb_types=["个人知识库"])
        self.assertEqual(searched, ["kb1"])
        self.assertEqual(errs, [])

    def test_search_ima_type_filter_all_none(self):
        # kb_types=None（全选）时不限类型，两类都检索
        searched = []

        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            if url.endswith("search_knowledge_base"):
                data = {"info_list": [
                    {"id": "kb1", "name": "Personal", "base_type": "个人知识库"},
                    {"id": "kb2", "name": "Sub", "base_type": "我加入的订阅知识库"},
                ], "is_end": True, "next_cursor": ""}
            elif url.endswith("search_knowledge"):
                searched.append(json.get("knowledge_base_id"))
                data = {"info_list": [], "is_end": True, "next_cursor": ""}
            f.json.return_value = {"code": 0, "msg": "ok", "data": data}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post):
            res, errs = ima_search.search_ima("q", self._cfg(), kb_types=None)
        self.assertEqual(set(searched), {"kb1", "kb2"})
        self.assertEqual(errs, [])

    def test_search_ima_type_filter_empty_list_searches_nothing(self):
        # kb_types=[]（全不选）时不应检索任何库
        searched = []

        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            if url.endswith("search_knowledge_base"):
                data = {"info_list": [
                    {"id": "kb1", "name": "Personal", "base_type": "个人知识库"},
                ], "is_end": True, "next_cursor": ""}
            else:
                searched.append(json.get("knowledge_base_id"))
                data = {"info_list": [], "is_end": True, "next_cursor": ""}
            f.json.return_value = {"code": 0, "msg": "ok", "data": data}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post):
            res, errs = ima_search.search_ima("q", self._cfg(), kb_types=[])
        self.assertEqual(searched, [])          # 没有任何库被检索
        self.assertEqual(res, [])

    def test_search_ima_subscription_quota_consolidated(self):
        # 订阅库全部命中"资料获取额度"(220021)时，errors 应合并为一条清晰提示，
        # 而非每个库刷一条；个人库真实错误仍单独显示。
        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            if url.endswith("search_knowledge_base"):
                data = {"info_list": [
                    {"id": "kb1", "name": "Personal", "base_type": "个人知识库"},
                    {"id": "kb2", "name": "SubA", "base_type": "我加入的订阅知识库"},
                    {"id": "kb3", "name": "SubB", "base_type": "我加入的订阅知识库"},
                ], "is_end": True, "next_cursor": ""}
            elif url.endswith("search_knowledge"):
                kid = json.get("knowledge_base_id")
                if kid == "kb1":
                    data = {"info_list": [{"media_id": "m1", "title": "Doc"}],
                            "is_end": True, "next_cursor": ""}
                else:
                    raise ima_search.ImaApiError("资料获取次数已达上限，请明天再尝试",
                                                ima_search.KB_QUOTA_EXCEEDED_CODE)
            f.json.return_value = {"code": 0, "msg": "ok", "data": data}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post):
            res, errs = ima_search.search_ima("q", self._cfg())
        self.assertEqual(len(res), 1)  # 个人库仍出结果
        self.assertEqual(len(errs), 1)  # 两条订阅配额错误合并为一条
        self.assertIn("订阅知识库", errs[0])
        self.assertIn("资料获取", errs[0])

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
        # progress_callback 应在每个知识库检索完成后回调 (done, total, result_count)
        seen = []

        def cb(done, total, result_count):
            seen.append((done, total, result_count))

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
        # 两个库均无结果 -> result_count 均为 0
        self.assertEqual(seen, [(1, 2, 0), (2, 2, 0)])
        self.assertEqual(errs, [])

    def test_search_ima_progress_callback_counts_results(self):
        # result_count 应随结果累计增长，最终等于总结果数
        seen = []

        def cb(done, total, result_count):
            seen.append(result_count)

        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            if url.endswith("search_knowledge_base"):
                data = {"info_list": [{"id": "kb1", "name": "KB1"}, {"id": "kb2", "name": "KB2"}],
                        "is_end": True, "next_cursor": ""}
            else:
                data = {"info_list": [{"media_id": "m1", "title": "Doc", "highlight_content": "x"}],
                        "is_end": True, "next_cursor": ""}
            f.json.return_value = {"code": 0, "msg": "ok", "data": data}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post), \
                mock.patch.object(ima_search.time, "sleep", return_value=None):
            res, errs = ima_search.search_ima("q", self._cfg(), progress_callback=cb)
        self.assertEqual(len(res), 2)  # 两个库各 1 条
        self.assertEqual(max(seen), 2)  # 最终 result_count 累计到总结果数
        self.assertEqual(errs, [])

    def test_ima_item_is_folder(self):
        # 真实接口：文件夹 media_type==99 且 media_id 以 "folder_" 开头
        self.assertTrue(ima_search._ima_item_is_folder(
            {"media_id": "folder_7451978045092421", "media_type": 99, "title": "model averaging"}, "folder_7451978045092421"))
        self.assertTrue(ima_search._ima_item_is_folder({"media_type": 99, "media_id": "m"}, "m"))
        self.assertTrue(ima_search._ima_item_is_folder({"media_id": "folder_xxx"}, "folder_xxx"))
        # 空 media_id（兜底）
        self.assertTrue(ima_search._ima_item_is_folder({"title": "sub"}, ""))
        # 文档：PDF media_type==1 / 笔记 media_type==11 -> 非文件夹
        self.assertFalse(ima_search._ima_item_is_folder(
            {"media_id": "pdf_abc", "media_type": 1, "title": "a.pdf"}, "pdf_abc"))
        self.assertFalse(ima_search._ima_item_is_folder(
            {"media_id": "note_abc", "media_type": 11, "title": "note"}, "note_abc"))

    def test_search_ima_folder_detected(self):
        def fake_post(url, json=None, headers=None, timeout=None):
            f = mock.Mock()
            f.status_code = 200
            data = {}
            if url.endswith("search_knowledge_base"):
                data = {"info_list": [{"id": "kb1", "name": "KB1"}], "is_end": True, "next_cursor": ""}
            elif url.endswith("search_knowledge"):
                # 一条文档(m1) + 一条文件夹(无 media_id)
                data = {"info_list": [
                    {"media_id": "m1", "title": "Doc.pdf", "highlight_content": "<em>q</em>"},
                    {"title": "子文件夹", "parent_folder_id": "f1"},
                ], "is_end": True, "next_cursor": ""}
            f.json.return_value = {"code": 0, "msg": "ok", "data": data}
            f.raise_for_status.return_value = None
            return f

        with mock.patch.object(ima_search.requests, "post", side_effect=fake_post):
            res, errs = ima_search.search_ima("q", self._cfg())
        self.assertEqual(len(res), 2)
        docs = [r for r in res if r.kind != "folder"]
        folders = [r for r in res if r.kind == "folder"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].title, "Doc.pdf")
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0].title, "子文件夹")
        self.assertEqual(folders[0].ref_id, "")
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
        # 文档深链必须复用 open?url= 桥并显式带 action=autoOpenMedia，
        # 否则客户端"知识库页"只会定位到库而不会打开该篇文档。
        d = ima_search.ima_client_doc_url("7451978007345957", "pdf_abc_123")
        self.assertEqual(
            d,
            "imacopilot://open?url=chrome-extension://"
            "nkohmbngmopdajidckglcoehlaeepeoi/index.html"
            "?action=autoOpenMedia&knowledgeBaseId=7451978007345957"
            "&mediaId=pdf_abc_123")


class TestEvBundler(unittest.TestCase):
    def test_pick_bundled_port_order_and_fallback(self):
        # 全部空闲 -> 优先 preferred
        with mock.patch.object(ev_bundler, "_port_free", return_value=True):
            self.assertEqual(ev_bundler.pick_bundled_port(80), 80)
        # preferred 被占，回退到 BUNDLED_PORTS[0]=8180
        def free_only_8180(p, host="127.0.0.1"):
            return p == 8180
        with mock.patch.object(ev_bundler, "_port_free", side_effect=free_only_8180):
            self.assertEqual(ev_bundler.pick_bundled_port(80), 8180)
        # 全部被占 -> None
        with mock.patch.object(ev_bundler, "_port_free", return_value=False):
            self.assertIsNone(ev_bundler.pick_bundled_port(80))

    def test_write_bundled_ini(self):
        import tempfile
        d = tempfile.mkdtemp()
        ini = ev_bundler.write_bundled_ini(8180, base_dir=d)
        self.assertTrue(os.path.isfile(ini))
        content = open(ini, encoding="utf-8").read()
        self.assertIn("instance=SuperSearch", content)
        self.assertIn("http_server=1", content)
        self.assertIn("http_server_port=8180", content)
        self.assertIn("db_path=", content)
        # 后台/标准用户运行：不弹管理员对话框、不触发 UAC、无托盘图标、无窗口
        self.assertIn("run_as_admin=0", content)
        self.assertIn("run_in_background=1", content)
        self.assertIn("show_tray_icon=0", content)
        self.assertIn("auto_include_fixed_volumes=1", content)
        self.assertIn("check_for_updates_on_startup=0", content)

    def test_service_installed_uses_sc_query(self):
        # returncode==0 -> 已安装；非 0 -> 未安装；异常 -> False
        with mock.patch.object(ev_bundler.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(ev_bundler.service_installed())
        with mock.patch.object(ev_bundler.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1060)):
            self.assertFalse(ev_bundler.service_installed())
        with mock.patch.object(ev_bundler.subprocess, "run", side_effect=OSError("no sc")):
            self.assertFalse(ev_bundler.service_installed())

    def test_install_service_uses_stable_exe(self):
        # 安装服务必须从持久化稳定目录执行（_MEIPASS 会被清理），并调用 -install-service
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(ev_bundler, "bundled_everything_exe",
                               return_value="X:/meipass/Everything/everything.exe"), \
                mock.patch.object(ev_bundler, "_ensure_stable_exe",
                                  return_value="X:/localappdata/everything.exe") as m_stable, \
                mock.patch.object(ev_bundler.subprocess, "run", side_effect=fake_run):
            ok, err = ev_bundler.install_service()
        self.assertTrue(ok)
        self.assertIsNone(err)
        m_stable.assert_called_once()
        self.assertEqual(seen["cmd"][-1], "-install-service")
        self.assertEqual(seen["cmd"][0], "X:/localappdata/everything.exe")

    def test_uninstall_service_best_effort(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(ev_bundler, "bundled_everything_exe",
                               return_value="X:/meipass/Everything/everything.exe"), \
                mock.patch.object(ev_bundler.subprocess, "run", side_effect=fake_run):
            ev_bundler.uninstall_service()  # 不抛异常即通过
        self.assertEqual(seen["cmd"][-1], "-uninstall-service")

    def test_bundled_everything_exe_dev_path(self):
        # 开发环境：回退到仓库 vendor/Everything/everything.exe
        orig_isfile = os.path.isfile

        def fake_isfile(p):
            if p.replace("\\", "/").endswith("vendor/Everything/everything.exe"):
                return True
            return orig_isfile(p)

        with mock.patch.object(sys, "frozen", False, create=True), \
                mock.patch.object(os.path, "isfile", side_effect=fake_isfile):
            found = ev_bundler.bundled_everything_exe()
        self.assertIsNotNone(found)
        self.assertTrue(found.replace("\\", "/").endswith("vendor/Everything/everything.exe"))

    def test_bundled_everything_exe_frozen_meipass(self):
        # 打包形态：优先用 sys._MEIPASS/Everything/everything.exe
        mp = "C:\\_MEI12345"
        expected = os.path.join(mp, "Everything", "everything.exe")

        def fake_isfile(p):
            return p == expected

        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", mp, create=True), \
                mock.patch.object(os.path, "isfile", side_effect=fake_isfile):
            found = ev_bundler.bundled_everything_exe()
        self.assertEqual(found, expected)

    def test_ensure_everything_running_reachable(self):
        # 已可达 -> 不改端口、可直接本地检索
        cfg = cfg_mod.EverythingConfig(port=80)
        with mock.patch.object(ev_bundler, "_reachable", return_value=True):
            new_cfg, note, ok = ev_bundler.ensure_everything_running(cfg, wait_seconds=0)
        self.assertIsNone(note)
        self.assertTrue(ok)
        self.assertEqual(new_cfg.port, 80)

    def test_ensure_everything_running_no_bundled(self):
        # 不可达且找不到内置 exe -> 返回错误、本地检索不可用
        cfg = cfg_mod.EverythingConfig(port=80)
        with mock.patch.object(ev_bundler, "_reachable", return_value=False), \
                mock.patch.object(ev_bundler, "bundled_everything_exe", return_value=None):
            new_cfg, note, ok = ev_bundler.ensure_everything_running(cfg, wait_seconds=0)
        self.assertFalse(ok)
        self.assertIsNotNone(note)
        self.assertIn("本地检索不可用", note)


class TestTray(unittest.TestCase):
    """系统托盘相关逻辑（不依赖显示器，纯导入/路径/绑定校验）。"""

    def test_app_icon_path_dev(self):
        # 开发环境下应解析到仓库 assets/SuperSearch.ico，且该文件存在
        from ui import main_window
        p = main_window._app_icon_path()
        self.assertTrue(os.path.isfile(p), f"托盘/窗口图标不存在: {p}")

    def test_tray_methods_bound(self):
        # 托盘相关方法应通过 setattr 绑定到 MainWindow
        from ui import main_window
        for m in ("_setup_tray", "on_tray_activated", "show_normal",
                  "quit_app", "closeEvent"):
            self.assertTrue(
                hasattr(main_window.MainWindow, m),
                f"MainWindow 缺少托盘方法: {m}",
            )

    def test_close_event_minimizes_to_tray_when_available(self):
        # 仅在具备真实托盘环境时执行（offscreen 下跳过）
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QCloseEvent
        from ui import main_window

        app = QApplication.instance() or QApplication([])
        if not main_window.QSystemTrayIcon.isSystemTrayAvailable():
            self.skipTest("no system tray in this environment")
        win = main_window.MainWindow()
        win._tray_available = True
        win._force_quit = False
        evt = QCloseEvent()
        win.closeEvent(evt)
        self.assertTrue(win.isHidden(), "有关闭事件时应最小化到托盘而非退出")
        self.assertFalse(evt.isAccepted(), "最小化到托盘时 closeEvent 应 ignore")


if __name__ == "__main__":
    unittest.main(verbosity=2)
