"""智能体统一入口：info 一次实时查询名称及专属目录，并更新目录缓存。"""

import sys

from api_common import run

VERBS = {"info": None}

if __name__ == "__main__":
    sys.exit(run("agent", VERBS))
