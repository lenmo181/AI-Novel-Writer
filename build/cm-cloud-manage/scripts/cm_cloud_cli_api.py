"""控制面入口：能力清单、命令 schema、领域帮助、写动作编排。

用法：`python3 cm_cloud_cli_api.py <capabilities|schema|help|orchestrate> [参数 ...]`
"""

import sys

from api_common import run_control

COMMANDS = {"capabilities": None, "schema": "command-id", "help": "topic", "orchestrate": None}

if __name__ == "__main__":
    sys.exit(run_control(COMMANDS))
