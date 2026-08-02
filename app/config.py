"""轻量 .env 加载（仅标准库）。

导入本模块即读取项目根目录的 .env；已存在的系统环境变量优先，不被覆盖。
"""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path: str = None) -> None:
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


# 导入即加载，任何入口（server / cli / 直接调用）都能拿到 .env 中的变量。
load_env()
