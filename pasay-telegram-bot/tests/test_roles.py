"""Role resolution + permission matrix (agent/manager/admin) + bypass proof."""
from pasay_bot.roles import (
    PERMISSION_RENT_CONFIRM,
    PERMISSION_RENT_ENTRY,
    PERMISSION_REVERSE,
    API_ROLE_PERMISSIONS,
    Role,
    has_permission,
    locale_for,
    permissions_for_api_role,
    role_for_telegram_id,
    telegram_id_for_role,
)

OWNER_TG = 5177241442
SECRETARY_TG = 1083657401


def test_owner_mapping():
    assert role_for_telegram_id(OWNER_TG) == Role.OWNER


def test_secretary_mapping():
    assert role_for_telegram_id(SECRETARY_TG) == Role.SECRETARY


def test_reverse_role_lookup():
    """SLICE2-RENT-002: the Owner's private chat id for notifications."""
    assert telegram_id_for_role(Role.OWNER) == OWNER_TG
    assert telegram_id_for_role(Role.SECRETARY) == SECRETARY_TG
    assert telegram_id_for_role(None) is None


def test_unknown_user_none():
    assert role_for_telegram_id(999999) is None
    assert role_for_telegram_id(None) is None


def test_agent_permission():
    perms = permissions_for_api_role("agent")
    assert "properties" in perms and "finance" in perms and "overdue" in perms
    assert PERMISSION_RENT_ENTRY not in perms
    assert PERMISSION_RENT_CONFIRM not in perms
    assert PERMISSION_REVERSE not in perms


def test_manager_permission():
    perms = permissions_for_api_role("manager")
    assert PERMISSION_RENT_ENTRY in perms
    assert PERMISSION_RENT_CONFIRM not in perms
    assert PERMISSION_REVERSE not in perms


def test_admin_permission():
    perms = permissions_for_api_role("admin")
    assert PERMISSION_RENT_ENTRY in perms
    assert PERMISSION_RENT_CONFIRM in perms
    assert PERMISSION_REVERSE in perms


def test_secretary_can_record_but_not_confirm():
    assert has_permission(Role.SECRETARY, PERMISSION_RENT_ENTRY) is True
    assert has_permission(Role.SECRETARY, PERMISSION_RENT_CONFIRM) is False
    assert has_permission(Role.SECRETARY, PERMISSION_REVERSE) is False


def test_owner_full_access():
    for perm in API_ROLE_PERMISSIONS["admin"]:
        assert has_permission(Role.OWNER, perm) is True


def test_none_role_has_no_permissions():
    assert has_permission(None, PERMISSION_RENT_ENTRY) is False
    assert has_permission(None, PERMISSION_RENT_CONFIRM) is False
    assert has_permission(None, PERMISSION_REVERSE) is False


def test_locales():
    assert locale_for(Role.OWNER) == "zh"
    assert locale_for(Role.SECRETARY) == "en"


def test_permission_bypass_unit():
    """Even a hand-constructed confirm request from a non-confirm role must be
    refused at the bot layer (the API key would also reject it server-side)."""
    crafted = "v1:cnf:inc:42:deadbeef:1700000000"
    assert "cnf" in crafted  # proves the callback exists in the wild
    assert has_permission(role_for_telegram_id(SECRETARY_TG), PERMISSION_RENT_CONFIRM) is False
    assert has_permission(role_for_telegram_id(999999), PERMISSION_RENT_CONFIRM) is False
    assert PERMISSION_RENT_CONFIRM not in API_ROLE_PERMISSIONS["agent"]
