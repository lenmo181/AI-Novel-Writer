"""文件域入口（files）。

用法：`python3 cm_cloud_files_api.py <动词> [参数 ...]`，如
`python3 cm_cloud_files_api.py search "项目报告" --format json`
"""

import sys

from api_common import run

VERBS = {
    "search": "keyword",
    "list": "folderRef",
    "info": None,
    "upload": None,
    "download": None,
    "mkdir": None,
    "copy": None,
}

if __name__ == "__main__":
    sys.exit(run("files", VERBS))
