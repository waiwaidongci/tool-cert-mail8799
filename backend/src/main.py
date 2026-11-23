"""应用入口：初始化数据库、账号与 HTTP 服务。

环境变量（均有演示默认值）：
- REJUDGE_DB       SQLite 文件路径，默认 backend/data/rejudgment.db
- REJUDGE_HOST     监听地址，默认 127.0.0.1
- REJUDGE_PORT     监听端口，默认 19283
- REJUDGE_SECRET   令牌签名密钥，未设置则随机生成（重启后旧令牌失效）
- REJUDGE_SEED     true 时启动自动播种演示数据，默认 true
"""

import json
import os
from pathlib import Path

from .domain.database import Database
from .domain.service import RejudgmentService
from .web.auth import Authenticator
from .web.server import AppState, build_server

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS = {
    'admin': {'password': 'admin-demo-2611', 'role': 'admin', 'name': '复核主管'},
    'analyst': {'password': 'analyst-demo-2611', 'role': 'analyst', 'name': '计量分析员'},
    'viewer': {'password': 'viewer-demo-2611', 'role': 'viewer', 'name': '只读查看'},
}


def build_state(db_path: Path, accounts=None, secret=None) -> AppState:
    db = Database(db_path)
    service = RejudgmentService(db)
    auth = Authenticator(accounts or DEFAULT_ACCOUNTS,
                         secret or os.environ.get('REJUDGE_SECRET') or Authenticator.random_secret())
    return AppState(service, auth)


def main():
    db_path = Path(os.environ.get('REJUDGE_DB', BASE_DIR / 'data' / 'rejudgment.db'))
    host = os.environ.get('REJUDGE_HOST', '127.0.0.1')
    port = int(os.environ.get('REJUDGE_PORT', '19283'))
    state = build_state(db_path)

    if os.environ.get('REJUDGE_SEED', 'true').lower() == 'true':
        from scripts.seed_demo import seed
        seed(state.service, db_path)

    server = build_server(host, port, state)
    print(f'计量复判班报已启动：http://{host}:{port}  （数据 {db_path}）')
    print('演示账号 admin / admin-demo-2611、analyst / analyst-demo-2611、viewer / viewer-demo-2611')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == '__main__':
    main()
