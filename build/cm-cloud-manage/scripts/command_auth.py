"""auth 领域命令登记（IDaaS AUT KMS v3.1.1 集成，CLI 表面 V1.8）。

`cmcloud auth <action>` 由 CLI 入口的 auth 独立分支分派，不经过 S01~S35 业务注册表路由，
因此本模块不向 `registry` 登记任何场景（`SCENARIO_COUNT` 与场景完整性校验保持不变）。

- `login` ：KMS-01 登录取票入口，只允许 `--profile` / `--format`；
- `revoke`：KMS-02 远端解绑，两阶段可信确认，未取得与 planHash/profile 绑定的确认不得调用 SDK；
- `logout`：KMS-04 清理本机凭据，两阶段可信确认，不触碰远端授权关系；
- `status`：KMS-05 Keychain 槽位状态查询，键存在不等同于凭据当前可用。

授权、取票与解绑的 SDK 调用边界都在 `scripts/auth_*.py` 包装文件中；本模块只登记命令元数据
（用于命令可发现性），不承载任何执行逻辑。
"""

DOMAIN = "auth"

ACTIONS = {
    "login": {
        "scenarios": (),
        "summary": "登录取票：完成授权并获取业务票据；只允许 --profile / --format json",
    },
    "revoke": {
        "scenarios": (),
        "summary": "远端解绑：先冻结影响计划，取得与 planHash 绑定的可信确认后才调用 SDK",
    },
    "logout": {
        "scenarios": (),
        "summary": "本地退出：可信确认后清理本机凭据，不调用服务端解绑",
    },
    "status": {
        "scenarios": (),
        "summary": "Keychain 凭据槽位状态查询；键存在不等同于凭据可用",
    },
}
