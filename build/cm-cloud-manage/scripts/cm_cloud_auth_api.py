"""授权域入口（auth）。

用法：`python3 cm_cloud_auth_api.py <login|reauthorize|revoke|logout|status> [参数 ...]`
"""

import sys

from api_common import run

VERBS = {"login": None, "reauthorize": None, "revoke": None, "logout": None, "status": None}

if __name__ == "__main__":
    # auth 未登记 help 领域，`-h` 取全局 help
    sys.exit(run("auth", VERBS, help_topic=False))
