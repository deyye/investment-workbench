"""统一外壳（L0–L4）的共用逻辑：判断当前请求属于哪个模块。

**为什么要抽成一个函数**：套件条高亮原本由两份 base 模板各写一套——
工作台用 `request.path.startswith('/approval')` 这一串 if/elif，政策侧则在 for 循环里
内联 `request.path.startswith(href.rstrip('/'))`。两份判据只要有一份漏了坑，
就表现为"点进去高亮不动"，而且只有打开页面才看得出。

**这个坑是什么**：政策子应用被 `DispatcherMiddleware` 挂在 `/policy` 下，
WSGI 把它拆成 `SCRIPT_NAME='/policy'` + `PATH_INFO='/policies'`。
Flask 的 `request.path` **只有 PATH_INFO**，不含挂载前缀，所以：
  - 访问 `/policy/`        → `request.path == '/'` → 政策侧模板里 `href == '/'`
                             那条恰好命中 → **高亮停在"工作台"**；
  - 访问 `/policy/policies` → `request.path == '/policies'` → 四条都不匹配
                              → **一个高亮都没有**。
用户看到的地址是 `SCRIPT_NAME + PATH_INFO`，所以判据必须用这两段相加。

`default` 用来兜住"没匹配上任何前缀"的情况：在工作台应用里未匹配就是工作台
（`/`、`/tasks`），在政策应用里未匹配就是政策（政策单独跑时 `script_root` 为空、
`path` 是 `/policies`，按前缀一个都对不上，但它确实是政策模块）。
"""

# 顺序即优先级：先比更长的/更具体的。`/policy/settings/model` 要判成 policy
# 而不是 settings——它是政策侧自己的路由，只是 303 跳到共用设置页。
PREFIXES = (('approval', '/approval'), ('policy', '/policy'), ('settings', '/settings'))


def current_module(script_root: str, path: str, default: str = 'workbench') -> str:
    """返回 'workbench' / 'approval' / 'policy' / 'settings'。"""
    full = f'{script_root or ""}{path or ""}'
    for name, prefix in PREFIXES:
        if full == prefix or full.startswith(prefix + '/'):
            return name
    return default


def install(app, default: str = 'workbench'):
    """把 current_module 装成 Jinja 全局：模板里写 `current_module(request)`。"""
    app.jinja_env.globals['current_module'] = (
        lambda request: current_module(request.script_root, request.path, default)
    )
    return app
