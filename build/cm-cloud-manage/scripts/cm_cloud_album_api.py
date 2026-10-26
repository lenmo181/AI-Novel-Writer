"""相簿与故事域入口（album）。

用法：`python3 cm_cloud_album_api.py <动词> [参数 ...]`，如
`python3 cm_cloud_album_api.py search_album "旅行" --kind album --format json`
"""

import sys

from api_common import run

VERBS = {
    "search": "keyword",
    "list": None,
    "files": None,
    "create": None,
    "add": None,
    "update": None,
    "share": None,
}

if __name__ == "__main__":
    sys.exit(run("album", VERBS))
