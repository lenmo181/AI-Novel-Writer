"""动态域入口（activity）。

用法：`python3 cm_cloud_activity_api.py <动词> [参数 ...]`，如
`python3 cm_cloud_activity_api.py list --eventKinds browse --format json`
"""

import sys

from api_common import run

VERBS = {"list": None, "reading": None, "stats": None}

if __name__ == "__main__":
    sys.exit(run("activity", VERBS))
