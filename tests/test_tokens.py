import pytest

from zerostore.access.tokens import AccessLevel, TokenError, TokenStore, hash_token


def test_create_and_verify(zs):
    raw, info = zs.tokens.create("cursor-laptop", "default")
    assert raw.startswith("zs_") and len(raw) > 40
    assert zs.tokens.verify(raw).id == info.id
    assert zs.tokens.verify(f"  {raw}\n").level is AccessLevel.DEFAULT


def test_only_the_hash_is_stored(zs):
    raw, _ = zs.tokens.create("a", "owner")
    dump = "\n".join(str(tuple(r)) for r in zs.state.query("SELECT * FROM tokens"))
    assert raw not in dump
    assert hash_token(raw) in dump


@pytest.mark.parametrize("raw", [None, "", "zs_wrong", "Bearer zs_x"])
def test_unknown_tokens_are_rejected(zs, raw):
    with pytest.raises(TokenError):
        zs.tokens.verify(raw)


def test_revoked_token_stops_working(zs):
    raw, info = zs.tokens.create("a", "confidential")
    assert zs.tokens.revoke(info.id)
    with pytest.raises(TokenError, match="revoked"):
        zs.tokens.verify(raw)
    assert not zs.tokens.revoke(info.id)
    assert zs.tokens.list() == []
    assert len(zs.tokens.list(include_revoked=True)) == 1


def test_levels():
    assert not AccessLevel.DEFAULT.sees_confidential
    assert AccessLevel.CONFIDENTIAL.sees_confidential and AccessLevel.OWNER.sees_confidential
    assert AccessLevel.OWNER.is_owner and not AccessLevel.CONFIDENTIAL.is_owner
    assert AccessLevel.DEFAULT.can_propose and not AccessLevel.OWNER.can_propose


def test_bad_level_and_name(zs):
    with pytest.raises(ValueError):
        zs.tokens.create("a", "admin")
    with pytest.raises(ValueError):
        zs.tokens.create("  ", "default")


def test_auto_approve_is_off_by_default(zs):
    _, info = zs.tokens.create("a", "default")
    assert info.auto_approve is False
    assert zs.tokens.set_auto_approve(info.id, True)
    assert zs.tokens.get(info.id).auto_approve is True


def test_token_changes_are_audited(zs):
    _, info = zs.tokens.create("a", "default")
    zs.tokens.revoke(info.id)
    actions = [r["action"] for r in zs.state.audit_since("2000-01-01")]
    assert actions == ["token_revoked", "token_created"]


def test_state_file_is_private(config, zs):
    mode = config.servers.statedb_path().stat().st_mode & 0o777
    assert mode == 0o600


def test_separate_store_instance_sees_tokens(config, zs):
    raw, _ = zs.tokens.create("a", "default")
    from zerostore.store.state import StateDB

    other = TokenStore(StateDB(config.servers.statedb_path()))
    assert other.verify(raw).name == "a"
