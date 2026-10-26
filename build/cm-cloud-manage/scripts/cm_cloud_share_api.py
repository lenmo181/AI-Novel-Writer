"""分享与外链域入口（share）。

用法：`python3 cm_cloud_share_api.py <动词> [参数 ...]`，如
`python3 cm_cloud_share_api.py browse "https://…" --format json`
"""

import sys

from api_common import run

VERBS = {
    "browse": "linkUrl",
    "list": None,
    "updates": None,
    "create": None,
    "subscribe": None,
    "save": None,
}

if __name__ == "__main__":
    sys.exit(run("share", VERBS))
