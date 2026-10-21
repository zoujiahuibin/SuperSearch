"""冒烟测试：覆盖配置、模型、本地检索与 ima 检索的纯逻辑（无需 GUI）。

运行：
  venv/Scripts/python.exe -m pytest tests/smoke_test.py -q
  或
  venv/Scripts/python.exe tests/smoke_test.py
"""
import os
import sys
import time
import shutil
import tempfile
import unittest
import subprocess
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfg_mod
from core import ev_bundler
from core import baidu_pan
from core.config import Settings, load_settings, save_settings, EverythingConfig
from core import format_sort
from core.models import SearchResult
from search import local_search, ima_search, baidu_search


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
        # ini 必须有 [Everything] 节头：节外键不生效
        self.assertIn("[Everything]", content)
        # 启用 HTTP 服务器的合法键名是 http_server_enabled；http_server=1 是
        # 非法键，会被 Everything 静默忽略（v1.2.1 本地检索不可用的根因）
        self.assertIn("http_server_enabled=1", content)
        self.assertNotIn("http_server=1", content)
        self.assertIn("http_server_port=8180", content)
        # ini 内实例名键是 instance_name（instance= 为非法键）
        self.assertIn("instance_name=SuperSearch", content)
        self.assertNotIn("instance=SuperSearch", content)
        # 后台/标准用户运行：不弹管理员对话框、不触发 UAC、无托盘图标、无窗口
        self.assertIn("run_as_admin=0", content)
        self.assertIn("run_in_background=1", content)
        self.assertIn("show_tray_icon=0", content)
        self.assertIn("auto_include_fixed_volumes=1", content)
        self.assertIn("check_for_updates_on_startup=0", content)

    def test_ensure_everything_running_quits_stale_instance_first(self):
        # 升级场景：旧版本（错误 ini 键）启动的实例可能仍在运行，其内存配置中
        # HTTP 服务器是关闭的。必须先退出旧实例再启动——否则新进程只会激活旧
        # 实例而不会重载配置，HTTP 永远无法就绪。
        cfg = cfg_mod.EverythingConfig(port=80)
        calls = {"quit": 0, "launch": 0}

        class FakeProc:
            def __init__(self, alive):
                self._alive = alive
                self.returncode = None

            def poll(self):
                if self._alive:
                    return None
                self.returncode = 0
                return 0

        def fake_quit(exe_path=None):
            calls["quit"] += 1

        def fake_launch(port, exe_path=None, base_dir=None):
            calls["launch"] += 1
            return FakeProc(alive=True), None

        with mock.patch.object(ev_bundler, "_reachable", return_value=False), \
                mock.patch.object(ev_bundler, "bundled_everything_exe",
                                  return_value="X:/everything.exe"), \
                mock.patch.object(ev_bundler, "service_installed", return_value=True), \
                mock.patch.object(ev_bundler, "_ensure_stable_exe",
                                  return_value="X:/everything.exe"), \
                mock.patch.object(ev_bundler, "quit_our_instance",
                                  side_effect=fake_quit), \
                mock.patch.object(ev_bundler, "launch_bundled",
                                  side_effect=fake_launch), \
                mock.patch.object(ev_bundler, "wait_reachable", return_value=True):
            new_cfg, note, ok = ev_bundler.ensure_everything_running(
                cfg, wait_seconds=1)
        self.assertTrue(ok)
        self.assertEqual(calls["quit"], 1, "启动前必须先退出可能残留的旧实例")
        self.assertEqual(calls["launch"], 1)

    def test_ensure_everything_running_absorbed_launch_retries(self):
        # “吸收”竞态：旧实例尚未退完（大数据库落盘需要数秒），首次启动的进程
        # 被旧实例吸收而立即退出；必须再次 quit + 重启直至新进程稳定存活。
        cfg = cfg_mod.EverythingConfig(port=80)
        calls = {"quit": 0, "launch": 0}

        class FakeProc:
            def __init__(self, alive):
                self._alive = alive
                self.returncode = None

            def poll(self):
                if self._alive:
                    return None
                self.returncode = 0
                return 0

        procs = [FakeProc(alive=False), FakeProc(alive=True)]

        def fake_quit(exe_path=None):
            calls["quit"] += 1

        def fake_launch(port, exe_path=None, base_dir=None):
            calls["launch"] += 1
            return procs[min(calls["launch"] - 1, len(procs) - 1)], None

        with mock.patch.object(ev_bundler, "_reachable", return_value=False), \
                mock.patch.object(ev_bundler, "bundled_everything_exe",
                                  return_value="X:/everything.exe"), \
                mock.patch.object(ev_bundler, "service_installed", return_value=True), \
                mock.patch.object(ev_bundler, "_ensure_stable_exe",
                                  return_value="X:/everything.exe"), \
                mock.patch.object(ev_bundler, "quit_our_instance",
                                  side_effect=fake_quit), \
                mock.patch.object(ev_bundler, "launch_bundled",
                                  side_effect=fake_launch), \
                mock.patch.object(ev_bundler, "wait_reachable", return_value=True):
            new_cfg, note, ok = ev_bundler.ensure_everything_running(
                cfg, wait_seconds=1)
        self.assertTrue(ok)
        self.assertEqual(calls["launch"], 2, "被吸收的启动必须重试")
        self.assertEqual(calls["quit"], 2, "每次启动前都应先退出旧实例")

    def test_ensure_everything_running_relaunches_when_last_proc_dead(self):
        # 上次拉起的进程已死亡且 HTTP 不可达（吸收竞态残留的无实例状态）：
        # 不能按"已启动"空等 wait_seconds，应直接走重新拉起路径。
        cfg = cfg_mod.EverythingConfig(port=80)
        launches = []

        class FakeProc:
            def __init__(self, alive):
                self._alive = alive

            def poll(self):
                return None if self._alive else 0

        def fake_launch(port, exe_path=None, base_dir=None):
            launches.append(port)
            return FakeProc(alive=True), None

        with mock.patch.object(ev_bundler, "_reachable", return_value=False), \
                mock.patch.object(ev_bundler, "bundled_everything_exe",
                                  return_value="X:/everything.exe"), \
                mock.patch.object(ev_bundler, "service_installed", return_value=True), \
                mock.patch.object(ev_bundler, "_ensure_stable_exe",
                                  return_value="X:/everything.exe"), \
                mock.patch.object(ev_bundler, "quit_our_instance"), \
                mock.patch.object(ev_bundler, "launch_bundled",
                                  side_effect=fake_launch), \
                mock.patch.object(ev_bundler, "wait_reachable", return_value=True):
            ev_bundler._launched = True
            ev_bundler._last_proc = FakeProc(alive=False)
            try:
                new_cfg, note, ok = ev_bundler.ensure_everything_running(
                    cfg, wait_seconds=1)
            finally:
                ev_bundler._launched = False
                ev_bundler._last_proc = None
        self.assertTrue(ok)
        self.assertEqual(len(launches), 1,
                         "进程已死且 HTTP 不可达应触发重新拉起")

    def test_ensure_everything_running_waits_when_last_proc_alive(self):
        # 进程仍活着（HTTP 尚未就绪的正常启动中）-> 按"已启动"等待，不重启
        cfg = cfg_mod.EverythingConfig(port=80)
        launches = []

        class FakeProc:
            def poll(self):
                return None

        def fake_launch(port, exe_path=None, base_dir=None):
            launches.append(port)
            return FakeProc(), None

        with mock.patch.object(ev_bundler, "_reachable", return_value=False), \
                mock.patch.object(ev_bundler, "bundled_everything_exe",
                                  return_value="X:/everything.exe"), \
                mock.patch.object(ev_bundler, "service_installed", return_value=True), \
                mock.patch.object(ev_bundler, "quit_our_instance"), \
                mock.patch.object(ev_bundler, "launch_bundled",
                                  side_effect=fake_launch), \
                mock.patch.object(ev_bundler, "wait_reachable", return_value=True):
            ev_bundler._launched = True
            ev_bundler._last_proc = FakeProc()
            try:
                new_cfg, note, ok = ev_bundler.ensure_everything_running(
                    cfg, wait_seconds=1)
            finally:
                ev_bundler._launched = False
                ev_bundler._last_proc = None
        self.assertTrue(ok)
        self.assertEqual(launches, [], "进程仍活着时不应重复拉起")

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

    def test_index_probe_total_parsing(self):
        # totalResults 为整数 -> 原样返回；0 -> 0；缺失/非法/异常 -> None
        cfg = cfg_mod.EverythingConfig(port=80)

        class FakeResp:
            def __init__(self, data):
                self._data = data

            def json(self):
                return self._data

        with mock.patch.object(ev_bundler.requests, "get",
                               return_value=FakeResp({"totalResults": 123})):
            self.assertEqual(ev_bundler.index_probe_total(cfg), 123)
        with mock.patch.object(ev_bundler.requests, "get",
                               return_value=FakeResp({"totalResults": 0})):
            self.assertEqual(ev_bundler.index_probe_total(cfg), 0)
            self.assertFalse(ev_bundler.is_index_ready(cfg))
        with mock.patch.object(ev_bundler.requests, "get",
                               return_value=FakeResp({})):
            self.assertIsNone(ev_bundler.index_probe_total(cfg))
        with mock.patch.object(ev_bundler.requests, "get",
                               return_value=FakeResp({"totalResults": "abc"})):
            self.assertIsNone(ev_bundler.index_probe_total(cfg))
        with mock.patch.object(ev_bundler.requests, "get",
                               side_effect=ev_bundler.requests.RequestException):
            self.assertIsNone(ev_bundler.index_probe_total(cfg))
            self.assertFalse(ev_bundler.is_index_ready(cfg))
        # totalResults>0 -> 索引可查询
        with mock.patch.object(ev_bundler.requests, "get",
                               return_value=FakeResp({"totalResults": 3475448})):
            self.assertTrue(ev_bundler.is_index_ready(cfg))

    def test_wait_index_ready_polls_and_reports_progress(self):
        # 索引未就绪 -> 轮询至超时返回 False；progress_cb 收到进度消息
        cfg = cfg_mod.EverythingConfig(port=80)
        msgs = []
        clock = [1000.0]

        def fake_monotonic():
            clock[0] += 1.0
            return clock[0]

        with mock.patch.object(ev_bundler, "is_index_ready",
                               side_effect=[False] * 20), \
                mock.patch.object(ev_bundler.time, "sleep", lambda s: None), \
                mock.patch.object(ev_bundler.time, "monotonic", fake_monotonic):
            ok = ev_bundler.wait_index_ready(cfg, 3.0, progress_cb=msgs.append)
        self.assertFalse(ok)
        self.assertTrue(msgs, "应通过 progress_cb 汇报索引建立进度")
        self.assertIn("索引", msgs[0])

    def test_wait_index_ready_returns_true_immediately_when_ready(self):
        cfg = cfg_mod.EverythingConfig(port=80)
        with mock.patch.object(ev_bundler, "is_index_ready", return_value=True):
            self.assertTrue(ev_bundler.wait_index_ready(cfg, 5.0))

    def test_ensure_everything_running_waits_index_when_requested(self):
        # HTTP 已就绪但索引未就绪 -> 返回提示与 ok=False（不能放行静默空检索）
        cfg = cfg_mod.EverythingConfig(port=80)
        with mock.patch.object(ev_bundler, "_reachable", return_value=True), \
                mock.patch.object(ev_bundler, "wait_index_ready",
                                  return_value=False) as m_wait:
            new_cfg, note, ok = ev_bundler.ensure_everything_running(
                cfg, wait_seconds=1, index_wait_seconds=120)
        self.assertFalse(ok)
        self.assertEqual(note, ev_bundler.INDEX_BUILDING_NOTE)
        self.assertEqual(m_wait.call_args[0][1], 120)
        # 索引就绪 -> 放行
        with mock.patch.object(ev_bundler, "_reachable", return_value=True), \
                mock.patch.object(ev_bundler, "wait_index_ready",
                                  return_value=True):
            new_cfg, note, ok = ev_bundler.ensure_everything_running(
                cfg, wait_seconds=1, index_wait_seconds=120)
        self.assertTrue(ok)
        self.assertIsNone(note)

    def test_ensure_everything_running_skips_index_wait_by_default(self):
        # 不传 index_wait_seconds（如启动预热带）-> 不等待索引，保持启动不阻塞
        cfg = cfg_mod.EverythingConfig(port=80)
        with mock.patch.object(ev_bundler, "_reachable", return_value=True), \
                mock.patch.object(ev_bundler, "wait_index_ready") as m_wait:
            new_cfg, note, ok = ev_bundler.ensure_everything_running(
                cfg, wait_seconds=1)
        self.assertTrue(ok)
        m_wait.assert_not_called()

    def test_ensure_everything_running_index_wait_after_fresh_launch(self):
        # 全新启动路径：HTTP 就绪后同样要等索引就绪，未就绪则给出建立中提示
        cfg = cfg_mod.EverythingConfig(port=80)

        class FakeProc:
            returncode = None

            def poll(self):
                return None

        with mock.patch.object(ev_bundler, "_reachable", return_value=False), \
                mock.patch.object(ev_bundler, "bundled_everything_exe",
                                  return_value="X:/everything.exe"), \
                mock.patch.object(ev_bundler, "service_installed", return_value=True), \
                mock.patch.object(ev_bundler, "_ensure_stable_exe",
                                  return_value="X:/everything.exe"), \
                mock.patch.object(ev_bundler, "quit_our_instance"), \
                mock.patch.object(ev_bundler, "launch_bundled",
                                  return_value=(FakeProc(), None)), \
                mock.patch.object(ev_bundler, "wait_reachable", return_value=True), \
                mock.patch.object(ev_bundler, "wait_index_ready",
                                  return_value=False):
            new_cfg, note, ok = ev_bundler.ensure_everything_running(
                cfg, wait_seconds=1, index_wait_seconds=120)
        self.assertFalse(ok)
        self.assertEqual(note, ev_bundler.INDEX_BUILDING_NOTE)
        self.assertEqual(new_cfg.port, 80)

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

    def test_quit_and_wait_save_waits_for_tmp_lifecycle(self):
        # .tmp 先出现、后消失 -> 观察到一次完整保存，返回 True 且 quit 被调用一次
        calls = {"quit": 0, "exists": 0}

        def fake_exists(p):
            calls["exists"] += 1
            if p.endswith(".db.tmp"):
                # 前 3 次探测模拟 -quit 到开始写盘的延迟；第 4-5 次写盘中；
                # 第 6 次起已保存完成
                return 4 <= calls["exists"] <= 5
            return False

        def fake_quit(exe_path=None):
            calls["quit"] += 1

        with mock.patch.object(ev_bundler, "quit_our_instance", side_effect=fake_quit), \
                mock.patch.object(ev_bundler.os.path, "exists", side_effect=fake_exists), \
                mock.patch.object(ev_bundler.os.path, "getmtime",
                                  return_value=time.time()), \
                mock.patch.object(ev_bundler.time, "sleep", lambda s: None):
            ok = ev_bundler.quit_our_instance_and_wait_save(timeout=5.0)
        self.assertTrue(ok)
        self.assertEqual(calls["quit"], 1)

    def test_quit_and_wait_save_returns_fast_when_no_instance(self):
        # 没有实例在运行：.tmp 始终不出现 -> 约 1s 内返回 False，不卡退出
        seen_exe = []

        def fake_quit(exe_path=None):
            seen_exe.append(exe_path)

        clock = [1000.0]

        def fake_monotonic():
            clock[0] += 0.4
            return clock[0]

        with mock.patch.object(ev_bundler, "quit_our_instance", side_effect=fake_quit), \
                mock.patch.object(ev_bundler.os.path, "exists", return_value=False), \
                mock.patch.object(ev_bundler.time, "sleep", lambda s: None), \
                mock.patch.object(ev_bundler.time, "monotonic", fake_monotonic):
            ok = ev_bundler.quit_our_instance_and_wait_save("X:/everything.exe", timeout=1.0)
        self.assertFalse(ok)
        self.assertEqual(seen_exe, ["X:/everything.exe"])

    def test_quit_and_wait_save_ignores_orphan_tmp(self):
        # 异常残留的孤儿 .tmp（mtime 超过 20s）-> 立即放弃等待返回 False，
        # 不会把退出流程卡满 timeout
        with mock.patch.object(ev_bundler, "quit_our_instance"), \
                mock.patch.object(ev_bundler.os.path, "exists", return_value=True), \
                mock.patch.object(ev_bundler.os.path, "getmtime", return_value=0.0), \
                mock.patch.object(ev_bundler.time, "sleep", lambda s: None):
            ok = ev_bundler.quit_our_instance_and_wait_save(timeout=10.0)
        self.assertFalse(ok)

    def test_wait_db_save_complete_shared_helper(self):
        # _wait_db_save_complete 与 quit_our_instance_and_wait_save 行为一致：
        # 卸载钩子复用同一等待逻辑（.tmp 出现后消失 -> True）
        state = {"n": 0}

        def fake_exists(p):
            state["n"] += 1
            # 第 2 次起 .tmp 出现（保存开始），第 4 次起消失（保存完成）
            return 2 <= state["n"] <= 3

        with mock.patch.object(ev_bundler.os.path, "exists", side_effect=fake_exists), \
                mock.patch.object(ev_bundler.os.path, "getmtime",
                                  return_value=time.time()), \
                mock.patch.object(ev_bundler.time, "sleep", lambda s: None):
            ok = ev_bundler._wait_db_save_complete(timeout=5.0)
        self.assertTrue(ok)


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


class _FakeResp:
    """最小 HTTP 响应假件：仅需 .json()。"""

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class TestBaiduPan(unittest.TestCase):
    """百度网盘客户端：令牌存取、设备码授权、轮询、刷新、检索。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.token_file = os.path.join(self.tmpdir, "baidu_netdisk.json")
        os.environ["BAIDU_TOKEN_FILE"] = self.token_file

    def tearDown(self):
        os.environ.pop("BAIDU_TOKEN_FILE", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---------- 令牌存取 ----------

    def test_token_path_env_override_and_dev_default(self):
        # 环境变量优先（测试隔离用）；开发环境（未 frozen）落仓库根
        self.assertEqual(baidu_pan.token_path(),
                         __import__("pathlib").Path(self.token_file))
        os.environ.pop("BAIDU_TOKEN_FILE", None)
        with mock.patch.object(sys, "frozen", False, create=True):
            p = baidu_pan.token_path()
        self.assertTrue(str(p).replace("\\", "/").endswith(
            "ima-everything-search/baidu_netdisk.json"))

    def test_token_roundtrip_and_states(self):
        # 空 -> 存应用凭证(configured) -> 存令牌(authorized) -> 清除令牌
        self.assertEqual(baidu_pan.authorization_state(), "none")
        self.assertFalse(baidu_pan.has_authorization())
        baidu_pan.save_app_credentials("cid", "secret")
        self.assertEqual(baidu_pan.authorization_state(), "configured")
        self.assertTrue(baidu_pan.has_app_credentials())
        baidu_pan.save_token({
            "access_token": "a1", "refresh_token": "r1",
            "expires_at": time.time() + 30 * 86400, "scope": "basic,netdisk",
        })
        self.assertEqual(baidu_pan.authorization_state(), "authorized")
        t = baidu_pan.load_token()
        self.assertEqual(t["client_id"], "cid")  # 合并写不丢应用凭证
        self.assertEqual(t["access_token"], "a1")
        baidu_pan.clear_token()
        t = baidu_pan.load_token()
        self.assertIsNone(t.get("access_token"))
        self.assertIsNone(t.get("refresh_token"))
        self.assertEqual(t.get("client_id"), "cid")  # 保留应用凭证
        self.assertEqual(baidu_pan.authorization_state(), "configured")

    def test_load_token_corrupt_file_returns_none(self):
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(baidu_pan.load_token())

    # ---------- 设备码请求 ----------

    def test_request_device_code_params(self):
        seen = {}

        def fake_post(url, data=None, timeout=None):
            seen["url"] = url
            seen["data"] = data
            return _FakeResp({
                "device_code": "dc1", "user_code": "uc1",
                "verification_url": "https://openapi.baidu.com/device",
                "qrcode_url": "", "expires_in": 300, "interval": 5,
            })

        with mock.patch.object(baidu_pan.requests, "post", side_effect=fake_post):
            info = baidu_pan.request_device_code("cid")
        self.assertEqual(seen["url"], baidu_pan.OAUTH_DEVICE_CODE_URL)
        self.assertEqual(seen["data"]["client_id"], "cid")
        self.assertEqual(seen["data"]["response_type"], "device_code")
        self.assertEqual(seen["data"]["scope"], "basic,netdisk")
        self.assertEqual(info["user_code"], "uc1")

    def test_request_device_code_error(self):
        with mock.patch.object(baidu_pan.requests, "post",
                               return_value=_FakeResp({"error": "invalid_client"})):
            with self.assertRaises(baidu_pan.BaiduAuthError):
                baidu_pan.request_device_code("cid")

    # ---------- 轮询授权 ----------

    def test_poll_pending_then_success(self):
        responses = [
            _FakeResp({"error": "authorization_pending"}),
            _FakeResp({"access_token": "a1", "expires_in": 2592000,
                       "refresh_token": "r1", "scope": "basic,netdisk"}),
        ]
        sleeps = []

        def fake_sleep(s):
            sleeps.append(s)

        with mock.patch.object(baidu_pan.requests, "post",
                               side_effect=responses), \
                mock.patch.object(baidu_pan.time, "sleep", side_effect=fake_sleep):
            token = baidu_pan.poll_device_token(
                "dc1", "cid", "secret", interval=5, timeout=300)
        self.assertEqual(token["access_token"], "a1")
        # 轮询间隔必须 >= 5s（百度要求）
        self.assertTrue(sleeps and all(s >= 5.0 for s in sleeps), sleeps)
        # 成功后令牌已落盘
        self.assertEqual(baidu_pan.load_token()["access_token"], "a1")
        self.assertGreater(baidu_pan.load_token()["expires_at"], time.time())

    def test_poll_slow_down_increases_interval(self):
        responses = [
            _FakeResp({"error": "slow_down"}),
            _FakeResp({"access_token": "a1", "expires_in": 2592000,
                       "refresh_token": "r1"}),
        ]
        sleeps = []

        with mock.patch.object(baidu_pan.requests, "post",
                               side_effect=responses), \
                mock.patch.object(baidu_pan.time, "sleep",
                                  side_effect=lambda s: sleeps.append(s)):
            baidu_pan.poll_device_token("dc1", "cid", "secret", interval=5)
        # slow_down 后间隔应 +5s（>=10）
        self.assertTrue(any(s >= 10.0 for s in sleeps), sleeps)

    def test_poll_expired_and_denied_raise(self):
        with mock.patch.object(baidu_pan.requests, "post",
                               return_value=_FakeResp({"error": "expired_token"})):
            with self.assertRaises(baidu_pan.BaiduAuthError):
                baidu_pan.poll_device_token("dc1", "cid", "secret")
        with mock.patch.object(baidu_pan.requests, "post",
                               return_value=_FakeResp({"error": "access_denied"})):
            with self.assertRaises(baidu_pan.BaiduAuthError):
                baidu_pan.poll_device_token("dc1", "cid", "secret")

    def test_poll_should_stop_returns_none_without_request(self):
        calls = []

        def fake_post(*a, **k):
            calls.append(1)
            return _FakeResp({"error": "authorization_pending"})

        with mock.patch.object(baidu_pan.requests, "post", side_effect=fake_post), \
                mock.patch.object(baidu_pan.time, "sleep"):
            token = baidu_pan.poll_device_token(
                "dc1", "cid", "secret", should_stop=lambda: True)
        self.assertIsNone(token)
        self.assertEqual(calls, [])

    def test_poll_network_error_keeps_waiting_until_deadline(self):
        def fake_post(*a, **k):
            raise baidu_pan.requests.RequestException("boom")

        clock = [1000.0]

        def fake_monotonic():
            clock[0] += 60.0  # 每次检查都推进 60s，越过 300s 截止
            return clock[0]

        with mock.patch.object(baidu_pan.requests, "post", side_effect=fake_post), \
                mock.patch.object(baidu_pan.time, "sleep"), \
                mock.patch.object(baidu_pan.time, "monotonic", fake_monotonic):
            with self.assertRaises(baidu_pan.BaiduAuthError):
                baidu_pan.poll_device_token("dc1", "cid", "secret", timeout=300)

    # ---------- 续期 ----------

    def test_refresh_when_near_expiry(self):
        baidu_pan.save_token({
            "client_id": "cid", "client_secret": "secret",
            "access_token": "old", "refresh_token": "r1",
            "expires_at": time.time() + 2 * 86400,  # 剩 2 天 < 5 天边际
        })
        with mock.patch.object(baidu_pan.requests, "post",
                               return_value=_FakeResp({
                                   "access_token": "new",
                                   "expires_in": 2592000,
                                   "refresh_token": "r2",
                                   "scope": "basic,netdisk"})):
            tok = baidu_pan.refresh_token()
        self.assertEqual(tok["access_token"], "new")
        self.assertEqual(tok["refresh_token"], "r2")  # 新 refresh_token 生效
        self.assertGreater(tok["expires_at"], time.time() + 20 * 86400)

    def test_no_refresh_when_fresh(self):
        baidu_pan.save_token({
            "client_id": "cid", "client_secret": "secret",
            "access_token": "a1", "refresh_token": "r1",
            "expires_at": time.time() + 20 * 86400,
        })
        with mock.patch.object(baidu_pan.requests, "post") as m_post:
            tok = baidu_pan.ensure_token()
        m_post.assert_not_called()
        self.assertEqual(tok["access_token"], "a1")

    def test_ensure_token_without_authorization_raises(self):
        with self.assertRaises(baidu_pan.BaiduAuthError):
            baidu_pan.ensure_token()

    # ---------- 关键词检索 ----------

    def _fresh_token(self):
        baidu_pan.save_token({
            "client_id": "cid", "client_secret": "secret",
            "access_token": "a1", "refresh_token": "r1",
            "expires_at": time.time() + 20 * 86400,
        })

    def test_search_files_params_and_mapping(self):
        self._fresh_token()
        seen = {}

        def fake_get(url, params=None, timeout=None):
            seen["url"] = url
            seen["params"] = params
            return _FakeResp({"errno": 0, "has_more": 0, "list": [
                {"fsid": 1, "path": "/a/b.txt", "server_filename": "b.txt",
                 "isdir": 0, "size": 10, "server_mtime": 1700000000},
                {"fsid": 2, "path": "/a", "server_filename": "a",
                 "isdir": 1, "size": 0, "server_mtime": 0},
            ]})

        with mock.patch.object(baidu_pan.requests, "get", side_effect=fake_get):
            items, err = baidu_pan.search_files("b.txt")
        self.assertIsNone(err)
        self.assertEqual(len(items), 2)
        self.assertEqual(seen["url"], baidu_pan.FILE_SEARCH_URL)
        p = seen["params"]
        self.assertEqual(p["method"], "search")
        self.assertEqual(p["access_token"], "a1")
        self.assertEqual(p["key"], "b.txt")
        self.assertEqual(p["dir"], "/")
        self.assertEqual(p["recursion"], 1)

    def test_search_key_truncated_to_30_chars(self):
        self._fresh_token()
        seen = {}

        def fake_get(url, params=None, timeout=None):
            seen["params"] = params
            return _FakeResp({"errno": 0, "list": []})

        with mock.patch.object(baidu_pan.requests, "get", side_effect=fake_get):
            baidu_pan.search_files("x" * 40)
        self.assertEqual(len(seen["params"]["key"]), 30)

    def test_search_category_param_sent_when_given(self):
        self._fresh_token()
        seen = {}

        def fake_get(url, params=None, timeout=None):
            seen["params"] = params
            return _FakeResp({"errno": 0, "list": []})

        with mock.patch.object(baidu_pan.requests, "get", side_effect=fake_get):
            baidu_pan.search_files("合同", dir="/资料", category=4, num=100)
        self.assertEqual(seen["params"]["category"], 4)
        self.assertEqual(seen["params"]["dir"], "/资料")
        self.assertEqual(seen["params"]["num"], 100)

    def test_search_token_invalid_refreshes_and_retries(self):
        self._fresh_token()
        get_calls = []

        def fake_get(url, params=None, timeout=None):
            get_calls.append(params)
            if len(get_calls) == 1:
                return _FakeResp({"errno": -6, "errmsg": "no permission"})
            return _FakeResp({"errno": 0, "list": [
                {"fsid": 9, "path": "/x.txt", "server_filename": "x.txt",
                 "isdir": 0, "size": 1, "server_mtime": 1700000000}]})

        # errno -6 时先刷新（令牌"新鲜"不会真正发刷新请求，直接重试）
        with mock.patch.object(baidu_pan.requests, "get", side_effect=fake_get):
            items, err = baidu_pan.search_files("x")
        self.assertIsNone(err)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(get_calls), 2)  # 确实重试了一次

    def test_search_token_invalid_persistent_returns_reauth_msg(self):
        self._fresh_token()
        with mock.patch.object(baidu_pan.requests, "get",
                               return_value=_FakeResp({"errno": -6, "errmsg": "no permission"})):
            items, err = baidu_pan.search_files("x")
        self.assertEqual(items, [])
        self.assertIn("重新授权", err)

    def test_search_other_errno_passthrough(self):
        self._fresh_token()
        with mock.patch.object(baidu_pan.requests, "get",
                               return_value=_FakeResp({"errno": 2, "errmsg": "param error"})):
            items, err = baidu_pan.search_files("x")
        self.assertEqual(items, [])
        self.assertIn("2", err)
        self.assertIn("param error", err)

    def test_search_network_error(self):
        self._fresh_token()

        def fake_get(*a, **k):
            raise baidu_pan.requests.RequestException("timeout")

        with mock.patch.object(baidu_pan.requests, "get", side_effect=fake_get):
            items, err = baidu_pan.search_files("x")
        self.assertEqual(items, [])
        self.assertIn("连接百度网盘失败", err)

    def test_disk_home_url_encoding(self):
        url = baidu_pan.disk_home_url("/雅思/听力")
        self.assertTrue(url.startswith("https://pan.baidu.com/disk/home#/all?path="))
        # 路径须整体 URL 编码（含斜杠），中文与 / 都不裸奔
        self.assertIn("%2F", url)
        self.assertNotIn("雅思", url)


class TestBaiduSearch(unittest.TestCase):
    """百度网盘结果映射层。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["BAIDU_TOKEN_FILE"] = os.path.join(
            self.tmpdir, "baidu_netdisk.json")

    def tearDown(self):
        os.environ.pop("BAIDU_TOKEN_FILE", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _save_token(self):
        baidu_pan.save_token({
            "client_id": "cid", "client_secret": "secret",
            "access_token": "a1", "refresh_token": "r1",
            "expires_at": time.time() + 20 * 86400,
        })

    def test_search_baidu_unauthenticated(self):
        results, errors = baidu_search.search_baidu("合同")
        self.assertEqual(results, [])
        self.assertTrue(errors and "未授权" in errors[0])

    def test_search_baidu_empty_query(self):
        results, errors = baidu_search.search_baidu("   ")
        self.assertEqual((results, errors), ([], []))

    def test_search_baidu_mapping_and_limit(self):
        self._save_token()
        raw = [
            {"fsid": 1, "path": "/a/b.txt", "server_filename": "b.txt",
             "isdir": 0, "size": 2048, "server_mtime": 1700000000},
            {"fsid": 2, "path": "/a/文件夹", "server_filename": "文件夹",
             "isdir": 1, "size": 0, "server_mtime": 0},
            {"fsid": 3, "path": "/c.pdf", "server_filename": "c.pdf",
             "isdir": 0, "size": 10, "server_mtime": 1690000000},
        ]
        with mock.patch.object(baidu_pan, "search_files",
                               return_value=(raw, None)):
            results, errors = baidu_search.search_baidu("a", limit=2)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)  # 展示上限截断
        r0, r1 = results
        self.assertEqual(r0.source, "baidu")
        self.assertEqual(r0.title, "b.txt")
        self.assertEqual(r0.path, "/a/b.txt")
        self.assertEqual(r0.kind, "")
        self.assertEqual(r0.size, 2048)
        self.assertIsInstance(r0.modified, datetime)
        self.assertEqual(r0.ref_id, "1")
        # 文件夹：kind=folder、size 0 按未知(None)、mtime 0 -> None
        self.assertEqual(r1.kind, "folder")
        self.assertIsNone(r1.size)
        self.assertIsNone(r1.modified)

    def test_search_baidu_error_passthrough(self):
        self._save_token()
        with mock.patch.object(baidu_pan, "search_files",
                               return_value=([], "百度网盘检索失败（错误码 2：x）")):
            results, errors = baidu_search.search_baidu("a")
        self.assertEqual(results, [])
        self.assertEqual(errors, ["百度网盘检索失败（错误码 2：x）"])

    def test_search_baidu_skips_invalid_items(self):
        self._save_token()
        raw = [
            {"fsid": 1, "path": "/a.txt", "server_filename": "a.txt",
             "isdir": 0, "size": 1, "server_mtime": 1700000000},
            {"fsid": 2, "path": "", "server_filename": "缺路径"},  # 无路径 -> 跳过
            {"path": "/无fsid.txt", "server_filename": "x"},          # 无 fsid -> 跳过
        ]
        with mock.patch.object(baidu_pan, "search_files",
                               return_value=(raw, None)):
            results, _ = baidu_search.search_baidu("a")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].path, "/a.txt")

    def test_category_name(self):
        self.assertEqual(baidu_search.category_name(4), "文档")
        self.assertEqual(baidu_search.category_name(1), "视频")
        self.assertEqual(baidu_search.category_name(99), "")
        self.assertEqual(baidu_search.category_name(None), "")


class TestBaiduUiBinding(unittest.TestCase):
    """百度网盘 UI 绑定的轻量校验（不构造主窗口，避免触发后端探测）。"""

    def test_source_label_and_color(self):
        self.assertEqual(
            SearchResult(source="baidu", title="x").source_label(), "百度网盘")
        from ui import main_window
        self.assertTrue(main_window.COLOR_BAIDU.isValid())

    def test_worker_signature_has_baidu_params(self):
        import inspect
        from ui import main_window
        sig = inspect.signature(main_window.SearchWorker.__init__)
        self.assertIn("do_baidu", sig.parameters)
        self.assertIn("baidu_limit", sig.parameters)

    def test_dialogs_exist(self):
        from ui import main_window
        self.assertTrue(hasattr(main_window, "BaiduCredsDialog"))
        self.assertTrue(hasattr(main_window, "BaiduDeviceAuthDialog"))
        self.assertTrue(hasattr(main_window, "_DeviceAuthPollThread"))

    def test_creds_dialog_roundtrip(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from ui import main_window
        app = QApplication.instance() or QApplication([])
        dlg = main_window.BaiduCredsDialog()
        dlg.edit_cid.setText("cid")
        dlg.edit_secret.setText("sec")
        self.assertEqual(dlg.get_credentials(), ("cid", "sec"))

    def test_settings_baidu_fields_roundtrip(self):
        # Settings 新字段：默认值 + 存档/读回
        s = Settings()
        self.assertFalse(s.baidu_enabled)
        self.assertEqual(s.baidu_limit, 100)
        d = tempfile.mkdtemp()
        try:
            p = cfg_mod.settings_path()
            with mock.patch.object(cfg_mod, "settings_path",
                                   return_value=__import__("pathlib").Path(d) / "settings.json"):
                s.baidu_enabled = True
                s.baidu_limit = 50
                cfg_mod.save_settings(s)
                back = cfg_mod.load_settings()
            self.assertTrue(back.baidu_enabled)
            self.assertEqual(back.baidu_limit, 50)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        _ = p


if __name__ == "__main__":
    unittest.main(verbosity=2)
