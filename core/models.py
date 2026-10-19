"""统一结果数据模型。

本地文件（Everything）与 ima 知识库文档（OpenAPI）经各自检索后，
统一转为 SearchResult，便于 UI 合并展示并打上来源标签。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class SearchResult:
    # 来源标识："local" 本地文件 / "ima" ima 知识库
    source: str
    # 标题 / 文件名
    title: str
    # 本地为绝对路径；ima 为知识库内路径或空
    path: str = ""
    # 条目类型：本地为 'file' / 'folder'，ima 为空（用于显示系统图标）
    kind: str = ""
    # 知识库 id / media_id 等（ima 用）
    ref_id: str = ""
    # 大小（字节），未知为 None
    size: Optional[int] = None
    # 修改时间（本地）或更新时间（ima），可空
    modified: Optional[datetime] = None
    # 高亮摘要（ima 返回 highlight_content）
    snippet: str = ""
    # 知识库名称（ima 用）
    kb_name: str = ""
    # 知识库 id（ima 用；用于打开知识库地址 https://ima.qq.com/wikis?knowledgeBaseId=<kb_id>）
    kb_id: str = ""

    def source_label(self) -> str:
        return "本地" if self.source == "local" else "ima"
