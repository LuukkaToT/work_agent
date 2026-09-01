"""用户表当前只提供目录存取，不参与鉴权或权限判断。"""

import pytest

from work_agent.core.users import get_user, upsert_user


def test_user_upsert_normalizes_and_roundtrips(pg_pool, pg_env):
    user_id = "Z12345678"
    try:
        record = upsert_user(user_id, "张三", "ZHANG.SAN@EXAMPLE.COM")
        assert record.id == "z12345678"
        assert record.email == "zhang.san@example.com"
        assert get_user("Z12345678") == record

        updated = upsert_user(user_id, "张三丰", "zhang.san@example.com")
        assert get_user(user_id) == updated
        assert updated.cn_name == "张三丰"
    finally:
        with pg_pool.connection() as conn:
            conn.execute("DELETE FROM users WHERE id=%s", ("z12345678",))


@pytest.mark.parametrize("user_id", ["local-dev", "z123", "123456789", "zz1234567"])
def test_user_id_format_is_strict(pg_env, user_id):
    with pytest.raises(ValueError, match="工号"):
        upsert_user(user_id, "测试", "test@example.com")


def test_user_email_must_be_valid(pg_env):
    with pytest.raises(ValueError, match="邮箱"):
        upsert_user("z87654321", "测试", "not-an-email")
