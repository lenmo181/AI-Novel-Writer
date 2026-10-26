"""操作台账域入口（operation）。

用法：`python3 cm_cloud_operation_api.py <status|confirm|execute> <operationRef> [参数 ...]`
"""

import sys

from api_common import run

VERBS = {"status": "operation-ref", "confirm": "operation-ref", "execute": "operation-ref"}

if __name__ == "__main__":
    sys.exit(run("operation", VERBS))
