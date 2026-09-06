"""users.github_id 身份键（GitHub 登录在用户名改绑手机号后仍可找回账号）

Revision ID: d9e0f1a2b3c4
Revises: c2d3e4f5a6b7

背景（P1 账号接管修复）：GitHub OAuth 原实现按 username 查到人即登录——任何人注册
与已有用户同名的 GitHub 账号即可接管该账号（admin 种子有守卫，普通用户无防护）。
新设计（手机号为主用户名，用户定稿）：
- GitHub 登录名与本地账号同名 -> 拒绝登录，要求绑定手机号；
- 不冲突 -> 直接创建/进入；绑定手机号后 username 改为 phone_{手机号}；
- username 改名后，GitHub 回访靠 github_id 找回账号（username 已不是原登录名）。
"""
from alembic import op

revision: str = 'd9e0f1a2b3c4'
down_revision = 'c2d3e4f5a6b7'


def upgrade() -> None:
    # P0：迁移链此前从未创建 users.email/avatar_url/display_name/extra，
    # 而 oauth/phone 注册与 admin 种子 INSERT 均引用这些列——全新环境启动即崩
    # （云端老库因历史手工 ALTER 而侥幸正常）。幂等补齐。
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT DEFAULT ''")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS extra JSONB DEFAULT '{}'::jsonb")
    # quota 自增 / 手机绑定等路径写入 updated_at（naive TIMESTAMP，与表内 created_at 一致）
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_id TEXT")
    # 部分唯一索引：允许多个 NULL（手机注册用户无 github_id），GitHub 身份全局唯一
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS users_github_id_key "
        "ON users (github_id) WHERE github_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS users_github_id_key")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS github_id")
