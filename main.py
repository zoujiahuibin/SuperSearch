"""统一检索工具入口：本地文件（Everything）+ ima 知识库。

用法：
  venv/Scripts/python.exe main.py

前置：
  - Everything 已启用 HTTP Server（默认 127.0.0.1:80）
  - ima 凭证位于 ~/.config/ima/client_id 与 api_key（或环境变量）
"""
from ui.main_window import main

if __name__ == "__main__":
    main()
