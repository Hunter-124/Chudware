"""Organization / team domain: lifecycle, the role matrix, and its security rules.

Every case drives the real HTTP surface against a real PostgreSQL (the autouse
``pg`` fixture), because the guarantees under test — opaque 404s, single-use
invitations bound to the caller's address, exactly one owner, immediate loss of
access on removal — are enforced by SQL predicates and constraints rather than
by Python bookkeeping.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

# Dev auth BEFORE importing the app, so X-Dev-User is accepted.
os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

from fastapi.testclient import TestClient  # noqa: E402

from gateway import accounts, app as app_mod, db, mailer, orgs  # noqa: E402
from gateway.config import settings  # noqa: E402
from gateway.limits import RateLimiter  # noqa: E402

OWNER = "owner@studio.example"
ADMIN = "admin@studio.example"
MEMBER = "member@studio.example"
SECOND = "second@studio.example"
OUTSIDER = "outsider@studio.example"
SITE_ADMIN = "siteadmin@studio.example"

H_OWNER = {"X-Dev-User": OWNER}
H_ADMIN = {"X-Dev-User": ADMIN}
H_MEMBER = {"X-Dev-User": MEMBER}
H_SECOND = {"X-Dev-User": SECOND}
H_OUTSIDER = {"X-Dev-User": OUTSIDER}
H_SITE_ADMIN = {"X-Dev-User": SITE_ADMIN}

NOT_AN_ORG = "00000000-0000-4000-8000-00000000dead"


def _seed(email: str, *, plan: str = "trial", role: str = "user") -> None:
    accounts.ensure_account(email, display=email.split("@")[0])
    accounts.set_status(email, "active", decided_by="test")
    accounts.set_plan(email, plan)
    if role == "admin":
        with db.tx() as cur:
            cur.execute("UPDATE accounts SET role = 'admin' WHERE email = %s", (email,))


@pytest.fixture(autouse=True)
def _isolate(pg, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(settings, "data_root", tmp_path / "data")
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    monkeypatch.setattr(settings, "bucket_root", bucket)
    monkeypatch.setattr(settings, "public_base_url", "https://chudware.example")
    # Process-global limiters would otherwise carry spent budget in from
    # whichever test file happened to run first.
    monkeypatch.setattr(orgs, "api_writes", RateLimiter(10_000, 60.0))
    monkeypatch.setattr(mailer, "outbound", RateLimiter(10_000, 3600.0))

    _seed(OWNER, plan="studio")
    _seed(ADMIN, plan="trial")
    _seed(MEMBER, plan="trial")
    _seed(SECOND, plan="studio")
    _seed(OUTSIDER, plan="studio")
    _seed(SITE_ADMIN, plan="studio", role="admin")


@pytest.fixture
def client(_isolate):
    with TestClient(app_mod.app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_org(client, headers=None, name="Bridge Works") -> dict:
    r = client.post("/api/orgs", headers=headers or H_OWNER, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _invite(client, org_id: str, email: str, role: str = "member", headers=None):
    return client.post(
        f"/api/orgs/{org_id}/invitations",
        headers=headers or H_OWNER,
        json={"email": email, "role": role},
    )


def _queued_invite_ctx(to: str) -> dict:
    """The context of the newest invitation mail queued for ``to``."""
    with db.tx() as cur:
        cur.execute(
            "SELECT ctx_json FROM mail_outbox"
            " WHERE to_addr = %s AND template = 'org_invite'"
            " ORDER BY id DESC LIMIT 1",
            (to,),
        )
        row = cur.fetchone()
    assert row is not None, f"no invitation mail queued for {to}"
    ctx = row["ctx_json"]
    return json.loads(ctx) if isinstance(ctx, str) else ctx


def _invite_token(to: str) -> str:
    url = _queued_invite_ctx(to)["invite_url"]
    assert url.startswith("https://chudware.example/?org_invite=")
    assert url.endswith("#team")
    return url.split("org_invite=", 1)[1].split("#", 1)[0]


def _accept(client, headers, **payload):
    return client.post("/api/orgs/invitations/accept", headers=headers, json=payload)


def _join(client, org_id: str, email: str, headers, role: str = "member") -> None:
    """Invite ``email`` and redeem the mailed token as that account."""
    assert _invite(client, org_id, email, role=role).status_code == 201
    assert _accept(client, headers, token=_invite_token(email)).status_code == 200


def _team_of(org: dict, name: str) -> dict:
    for team in org["teams"]:
        if team["name"] == name:
            return team
    raise AssertionError(f"no team named {name} in {org}")


def _owner_count(org_id: str) -> int:
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM org_members WHERE org_id = %s AND role = 'owner'",
            (org_id,),
        )
        return int(cur.fetchone()["n"])


# ---------------------------------------------------------------------------
# Creation gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plan", ["trial", "desktop"])
def test_creating_an_org_requires_a_shared_plan(client, plan):
    accounts.set_plan(OWNER, plan)
    r = client.post("/api/orgs", headers=H_OWNER, json={"name": "Nope"})
    assert r.status_code == 402
    assert client.get("/api/orgs", headers=H_OWNER).json()["organizations"] == []


@pytest.mark.parametrize("name", ["", "   ", "x" * 81])
def test_org_name_is_validated(client, name):
    assert client.post("/api/orgs", headers=H_OWNER, json={"name": name}).status_code == 400


def test_create_org_makes_owner_and_general_team(client):
    org = _create_org(client)
    assert org["role"] == "owner"
    assert org["owner_email"] == OWNER
    general = _team_of(org, "General")
    assert general["is_general"] is True
    assert general["role"] == "manager"
    assert general["bucket_url"] == f"/bucket?team={general['id']}"

    listing = client.get("/api/orgs", headers=H_OWNER).json()
    assert [o["id"] for o in listing["organizations"]] == [org["id"]]
    only = listing["organizations"][0]
    assert only["member_count"] == 1
    assert [t["name"] for t in only["teams"]] == ["General"]
    assert listing["invitations"] == []


def test_owned_org_count_is_capped(client, monkeypatch):
    monkeypatch.setattr(orgs, "MAX_ORGS_PER_OWNER", 2)
    _create_org(client, name="One")
    _create_org(client, name="Two")
    assert client.post("/api/orgs", headers=H_OWNER, json={"name": "Three"}).status_code == 409


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


def test_invitation_mail_carries_a_single_use_link_and_accept_joins_general(client):
    org = _create_org(client)
    r = _invite(client, org["id"], MEMBER)
    assert r.status_code == 201, r.text
    assert "token" not in r.text

    ctx = _queued_invite_ctx(MEMBER)
    assert ctx["org_name"] == "Bridge Works"
    assert ctx["inviter"] == OWNER
    assert ctx["role"] == "member"
    token = _invite_token(MEMBER)

    accepted = _accept(client, H_MEMBER, token=token)
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["org"]["id"] == org["id"]
    assert body["org"]["role"] == "member"
    assert body["team"]["name"] == "General"

    listing = client.get("/api/orgs", headers=H_MEMBER).json()
    assert listing["organizations"][0]["role"] == "member"
    assert [t["name"] for t in listing["organizations"][0]["teams"]] == ["General"]
    assert listing["invitations"] == []

    # Single use: the same link cannot be redeemed twice.
    assert _accept(client, H_MEMBER, token=token).status_code == 404


def test_invitation_is_bound_to_the_invited_address_case_insensitively(client):
    org = _create_org(client)
    assert _invite(client, org["id"], "Member@Studio.Example").status_code == 201
    token = _invite_token("member@studio.example")

    # A different signed-in account holding the token gets nothing.
    assert _accept(client, H_OUTSIDER, token=token).status_code == 404
    assert client.get("/api/orgs", headers=H_OUTSIDER).json()["organizations"] == []
    # The invited address redeems it, matched case-insensitively.
    assert _accept(client, H_MEMBER, token=token).status_code == 200


def test_pending_invitation_is_listed_and_acceptable_by_id_only_by_the_invitee(client):
    org = _create_org(client)
    assert _invite(client, org["id"], MEMBER).status_code == 201

    listed = client.get("/api/orgs", headers=H_MEMBER).json()["invitations"]
    assert len(listed) == 1
    invitation = listed[0]
    assert invitation["org_name"] == "Bridge Works"
    assert invitation["invited_by"] == OWNER
    assert "token" not in invitation

    # IDOR: the id is visible to its invitee, and useless to anybody else.
    assert _accept(client, H_OUTSIDER, invitation_id=invitation["id"]).status_code == 404
    assert _accept(client, H_MEMBER, invitation_id=invitation["id"]).status_code == 200


def test_accept_requires_a_token_or_an_id(client):
    assert _accept(client, H_MEMBER).status_code == 400
    assert _accept(client, H_MEMBER, token="nonsense").status_code == 404
    assert _accept(client, H_MEMBER, invitation_id="not-a-uuid").status_code == 404


def test_expired_invitation_cannot_be_redeemed(client):
    org = _create_org(client)
    assert _invite(client, org["id"], MEMBER).status_code == 201
    token = _invite_token(MEMBER)
    with db.tx() as cur:
        cur.execute(
            "UPDATE org_invitations SET expires_at = now() - interval '1 day'"
            " WHERE org_id = %s",
            (org["id"],),
        )
    assert _accept(client, H_MEMBER, token=token).status_code == 404
    assert client.get("/api/orgs", headers=H_MEMBER).json()["invitations"] == []


def test_revoked_invitation_cannot_be_redeemed(client):
    org = _create_org(client)
    assert _invite(client, org["id"], MEMBER).status_code == 201
    token = _invite_token(MEMBER)
    detail = client.get(f"/api/orgs/{org['id']}", headers=H_OWNER).json()
    invitation_id = detail["invitations"][0]["id"]

    assert client.delete(
        f"/api/orgs/{org['id']}/invitations/{invitation_id}", headers=H_OWNER
    ).status_code == 200
    assert _accept(client, H_MEMBER, token=token).status_code == 404
    # Revoking twice is a 404, not a silent success.
    assert client.delete(
        f"/api/orgs/{org['id']}/invitations/{invitation_id}", headers=H_OWNER
    ).status_code == 404


def test_invitation_ids_are_not_cross_org_handles(client):
    victim = _create_org(client, name="Victim")
    attacker = _create_org(client, headers=H_OUTSIDER, name="Attacker")
    assert _invite(client, victim["id"], MEMBER).status_code == 201
    invitation_id = client.get(f"/api/orgs/{victim['id']}", headers=H_OWNER).json()[
        "invitations"
    ][0]["id"]

    # Through the attacker's own org (they are its owner): the id belongs to a
    # different org, so it simply does not exist here.
    assert client.delete(
        f"/api/orgs/{attacker['id']}/invitations/{invitation_id}", headers=H_OUTSIDER
    ).status_code == 404
    # And through the victim's org id, where they are not a member at all.
    assert client.delete(
        f"/api/orgs/{victim['id']}/invitations/{invitation_id}", headers=H_OUTSIDER
    ).status_code == 404
    # Still redeemable by its rightful invitee.
    assert _accept(client, H_MEMBER, token=_invite_token(MEMBER)).status_code == 200


def test_resending_an_invitation_supersedes_the_previous_link(client):
    org = _create_org(client)
    assert _invite(client, org["id"], MEMBER).status_code == 201
    first = _invite_token(MEMBER)
    assert _invite(client, org["id"], MEMBER).status_code == 201
    second = _invite_token(MEMBER)
    assert first != second
    assert _accept(client, H_MEMBER, token=first).status_code == 404
    assert _accept(client, H_MEMBER, token=second).status_code == 200


def test_cannot_invite_an_existing_member_or_an_invalid_role(client):
    org = _create_org(client)
    assert _invite(client, org["id"], OWNER).status_code == 409
    assert _invite(client, org["id"], MEMBER, role="owner").status_code == 400
    assert _invite(client, org["id"], "not-an-address").status_code == 400


# ---------------------------------------------------------------------------
# Opacity: non-members, site admins, malformed ids
# ---------------------------------------------------------------------------


def test_non_members_get_404_for_opaque_org_and_team_ids(client):
    org = _create_org(client)
    team = _team_of(org, "General")
    for headers in (H_OUTSIDER, H_MEMBER):
        assert client.get(f"/api/orgs/{org['id']}", headers=headers).status_code == 404
        assert client.get(
            f"/api/orgs/{org['id']}/teams/{team['id']}", headers=headers
        ).status_code == 404
        assert client.patch(
            f"/api/orgs/{org['id']}", headers=headers, json={"name": "Stolen"}
        ).status_code == 404
        assert client.delete(f"/api/orgs/{org['id']}", headers=headers).status_code == 404
        assert _invite(client, org["id"], "x@example.com", headers=headers).status_code == 404
    assert (
        client.get(f"/api/orgs/{org['id']}", headers=H_OWNER).json()["organization"]["name"]
        == "Bridge Works"
    )


def test_site_admin_role_grants_no_implicit_org_access(client):
    org = _create_org(client)
    team = _team_of(org, "General")
    assert client.get("/api/orgs", headers=H_SITE_ADMIN).json()["organizations"] == []
    assert client.get(f"/api/orgs/{org['id']}", headers=H_SITE_ADMIN).status_code == 404
    assert client.get(
        f"/api/orgs/{org['id']}/teams/{team['id']}", headers=H_SITE_ADMIN
    ).status_code == 404
    assert client.delete(f"/api/orgs/{org['id']}", headers=H_SITE_ADMIN).status_code == 404


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-uuid",
        NOT_AN_ORG.upper(),
        "{" + NOT_AN_ORG + "}",
        f"urn:uuid:{NOT_AN_ORG}",
        NOT_AN_ORG.replace("-", ""),
    ],
)
def test_non_canonical_org_ids_are_rejected_as_not_found(client, bad):
    _create_org(client)
    assert client.get(f"/api/orgs/{bad}", headers=H_OWNER).status_code == 404


def test_unknown_but_well_formed_org_id_is_404(client):
    _create_org(client)
    assert client.get(f"/api/orgs/{NOT_AN_ORG}", headers=H_OWNER).status_code == 404


# ---------------------------------------------------------------------------
# Role matrix
# ---------------------------------------------------------------------------


@pytest.fixture
def staffed(client):
    """An org with an owner, an org admin, and one ordinary member."""
    org = _create_org(client)
    _join(client, org["id"], ADMIN, H_ADMIN, role="admin")
    _join(client, org["id"], MEMBER, H_MEMBER)
    return org


def test_ordinary_member_cannot_administer_the_org(client, staffed):
    org_id = staffed["id"]
    assert client.patch(
        f"/api/orgs/{org_id}", headers=H_MEMBER, json={"name": "Mine"}
    ).status_code == 403
    assert _invite(client, org_id, "x@example.com", headers=H_MEMBER).status_code == 403
    assert client.post(
        f"/api/orgs/{org_id}/teams", headers=H_MEMBER, json={"name": "Skunkworks"}
    ).status_code == 403
    assert client.patch(
        f"/api/orgs/{org_id}/members/{ADMIN}", headers=H_MEMBER, json={"role": "member"}
    ).status_code == 403
    assert client.delete(f"/api/orgs/{org_id}", headers=H_MEMBER).status_code == 403


def test_org_admin_can_run_the_org_but_cannot_escalate(client, staffed):
    org_id = staffed["id"]
    assert client.patch(
        f"/api/orgs/{org_id}", headers=H_ADMIN, json={"name": "Bridge Works II"}
    ).status_code == 200
    assert _invite(client, org_id, "new@example.com", headers=H_ADMIN).status_code == 201
    # Minting another administrator is the owner's call alone.
    assert _invite(
        client, org_id, "peer@example.com", role="admin", headers=H_ADMIN
    ).status_code == 403
    assert client.patch(
        f"/api/orgs/{org_id}/members/{MEMBER}", headers=H_ADMIN, json={"role": "admin"}
    ).status_code == 403
    assert client.post(
        f"/api/orgs/{org_id}/transfer-ownership", headers=H_ADMIN, json={"email": ADMIN}
    ).status_code == 403
    assert client.delete(f"/api/orgs/{org_id}", headers=H_ADMIN).status_code == 403


def test_owner_moves_members_between_admin_and_member(client, staffed):
    org_id = staffed["id"]
    assert client.patch(
        f"/api/orgs/{org_id}/members/{MEMBER}", headers=H_OWNER, json={"role": "admin"}
    ).status_code == 200
    assert client.get("/api/orgs", headers=H_MEMBER).json()["organizations"][0]["role"] == "admin"
    assert client.patch(
        f"/api/orgs/{org_id}/members/{MEMBER}", headers=H_OWNER, json={"role": "member"}
    ).status_code == 200
    assert client.get("/api/orgs", headers=H_MEMBER).json()["organizations"][0]["role"] == "member"


@pytest.mark.parametrize("role", ["owner", "manager", "", "root"])
def test_org_member_role_values_are_restricted(client, staffed, role):
    assert client.patch(
        f"/api/orgs/{staffed['id']}/members/{MEMBER}", headers=H_OWNER, json={"role": role}
    ).status_code == 400


def test_the_owner_role_cannot_be_changed_or_removed(client, staffed):
    org_id = staffed["id"]
    assert client.patch(
        f"/api/orgs/{org_id}/members/{OWNER}", headers=H_OWNER, json={"role": "member"}
    ).status_code == 400
    assert client.delete(f"/api/orgs/{org_id}/members/{OWNER}", headers=H_OWNER).status_code == 400
    assert _owner_count(org_id) == 1


def test_admin_removes_members_but_not_administrators(client, staffed):
    org_id = staffed["id"]
    # Create a second administrator so this is not the self-removal/leave path.
    assert client.patch(
        f"/api/orgs/{org_id}/members/{MEMBER}",
        headers=H_OWNER,
        json={"role": "admin"},
    ).status_code == 200
    assert client.delete(
        f"/api/orgs/{org_id}/members/{MEMBER}", headers=H_ADMIN
    ).status_code == 403
    assert client.patch(
        f"/api/orgs/{org_id}/members/{MEMBER}",
        headers=H_OWNER,
        json={"role": "member"},
    ).status_code == 200
    assert client.delete(
        f"/api/orgs/{org_id}/members/{MEMBER}", headers=H_ADMIN
    ).status_code == 200
    assert client.get("/api/orgs", headers=H_MEMBER).json()["organizations"] == []
    # The owner outranks an admin.
    assert client.delete(
        f"/api/orgs/{org_id}/members/{ADMIN}", headers=H_OWNER
    ).status_code == 200


def test_a_member_can_leave_on_their_own(client, staffed):
    org_id = staffed["id"]
    assert client.delete(
        f"/api/orgs/{org_id}/members/{MEMBER}", headers=H_MEMBER
    ).status_code == 200
    assert client.get("/api/orgs", headers=H_MEMBER).json()["organizations"] == []
    assert client.get(f"/api/orgs/{org_id}", headers=H_MEMBER).status_code == 404


def test_removing_a_member_also_clears_their_team_rows(client, staffed):
    org_id = staffed["id"]
    general = _team_of(staffed, "General")
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM team_members WHERE team_id = %s AND email = %s",
            (general["id"], MEMBER),
        )
        assert int(cur.fetchone()["n"]) == 1
    assert client.delete(f"/api/orgs/{org_id}/members/{MEMBER}", headers=H_OWNER).status_code == 200
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM team_members WHERE email = %s", (MEMBER,))
        assert int(cur.fetchone()["n"]) == 0


def test_invitation_detail_is_redacted_for_ordinary_members(client, staffed):
    org_id = staffed["id"]
    assert _invite(client, org_id, "pending@example.com").status_code == 201

    as_member = client.get(f"/api/orgs/{org_id}", headers=H_MEMBER).json()["invitations"]
    assert len(as_member) == 1
    assert set(as_member[0]) == {"id", "role", "created_at"}

    as_admin = client.get(f"/api/orgs/{org_id}", headers=H_ADMIN).json()["invitations"]
    assert as_admin[0]["email"] == "pending@example.com"
    assert as_admin[0]["invited_by"] == OWNER


def test_org_detail_lists_members_with_roles(client, staffed):
    detail = client.get(f"/api/orgs/{staffed['id']}", headers=H_MEMBER).json()
    assert detail["organization"]["owner_email"] == OWNER
    assert detail["organization"]["member_count"] == 3
    assert [(m["email"], m["role"]) for m in detail["members"]] == [
        (OWNER, "owner"),
        (ADMIN, "admin"),
        (MEMBER, "member"),
    ]


# ---------------------------------------------------------------------------
# Ownership transfer
# ---------------------------------------------------------------------------


def test_transfer_ownership_is_atomic_and_leaves_exactly_one_owner(client):
    org = _create_org(client)
    _join(client, org["id"], SECOND, H_SECOND)  # SECOND is on a studio plan

    r = client.post(
        f"/api/orgs/{org['id']}/transfer-ownership", headers=H_OWNER, json={"email": SECOND}
    )
    assert r.status_code == 200, r.text
    assert _owner_count(org["id"]) == 1
    assert client.get("/api/orgs", headers=H_SECOND).json()["organizations"][0]["role"] == "owner"
    assert client.get("/api/orgs", headers=H_OWNER).json()["organizations"][0]["role"] == "admin"
    # The former owner has lost owner-only powers to the new one.
    assert client.delete(f"/api/orgs/{org['id']}", headers=H_OWNER).status_code == 403
    assert client.delete(f"/api/orgs/{org['id']}", headers=H_SECOND).status_code == 200


def test_transfer_ownership_rejects_non_members_self_and_unfunded_targets(client):
    org = _create_org(client)
    _join(client, org["id"], MEMBER, H_MEMBER)  # MEMBER is on a trial plan

    def transfer(email):
        return client.post(
            f"/api/orgs/{org['id']}/transfer-ownership", headers=H_OWNER, json={"email": email}
        )

    assert transfer(OWNER).status_code == 400
    assert transfer(MEMBER).status_code == 402       # trial cannot fund shared storage
    assert transfer(OUTSIDER).status_code == 404     # studio plan, but not a member
    accounts.set_status(SECOND, "suspended", decided_by="test")
    assert transfer(SECOND).status_code == 402
    assert _owner_count(org["id"]) == 1
    assert client.get("/api/orgs", headers=H_OWNER).json()["organizations"][0]["role"] == "owner"


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


def test_teams_are_created_named_and_scoped_to_their_org(client, staffed):
    org_id = staffed["id"]
    r = client.post(f"/api/orgs/{org_id}/teams", headers=H_ADMIN, json={"name": "Fixtures"})
    assert r.status_code == 201, r.text
    team = r.json()["team"]
    assert team["org_id"] == org_id
    assert team["is_general"] is False
    assert team["bucket_url"] == f"/bucket?team={team['id']}"

    # Case-insensitively unique within the org...
    assert client.post(
        f"/api/orgs/{org_id}/teams", headers=H_ADMIN, json={"name": "fixtures"}
    ).status_code == 409
    # ...but not across orgs.
    other = _create_org(client, headers=H_OUTSIDER, name="Other Co")
    assert client.post(
        f"/api/orgs/{other['id']}/teams", headers=H_OUTSIDER, json={"name": "Fixtures"}
    ).status_code == 201


def test_team_ids_are_not_valid_in_another_orgs_url(client, staffed):
    other = _create_org(client, headers=H_OUTSIDER, name="Other Co")
    victim_team = _team_of(staffed, "General")
    # The outsider owns `other`, so the org half of the path authorizes — the
    # team half must still refuse a team that lives somewhere else.
    assert client.get(
        f"/api/orgs/{other['id']}/teams/{victim_team['id']}", headers=H_OUTSIDER
    ).status_code == 404
    assert client.delete(
        f"/api/orgs/{other['id']}/teams/{victim_team['id']}", headers=H_OUTSIDER
    ).status_code == 404


def test_general_team_cannot_be_deleted_and_other_teams_can(client, staffed):
    org_id = staffed["id"]
    general = _team_of(staffed, "General")
    assert client.delete(
        f"/api/orgs/{org_id}/teams/{general['id']}", headers=H_OWNER
    ).status_code == 400
    made = client.post(
        f"/api/orgs/{org_id}/teams", headers=H_OWNER, json={"name": "Temp"}
    ).json()["team"]
    assert client.delete(
        f"/api/orgs/{org_id}/teams/{made['id']}", headers=H_OWNER
    ).status_code == 200
    assert client.get(
        f"/api/orgs/{org_id}/teams/{made['id']}", headers=H_OWNER
    ).status_code == 404


def test_team_manager_renames_but_ordinary_team_members_cannot(client, staffed):
    org_id = staffed["id"]
    team = client.post(
        f"/api/orgs/{org_id}/teams", headers=H_OWNER, json={"name": "Fixtures"}
    ).json()["team"]
    assert client.post(
        f"/api/orgs/{org_id}/teams/{team['id']}/members",
        headers=H_OWNER,
        json={"email": MEMBER, "role": "member"},
    ).status_code == 201

    assert client.patch(
        f"/api/orgs/{org_id}/teams/{team['id']}", headers=H_MEMBER, json={"name": "Mine"}
    ).status_code == 403
    assert client.patch(
        f"/api/orgs/{org_id}/teams/{team['id']}/members/{ADMIN}",
        headers=H_MEMBER,
        json={"role": "manager"},
    ).status_code == 403

    # Promote them to manager and the same calls land.
    assert client.patch(
        f"/api/orgs/{org_id}/teams/{team['id']}/members/{MEMBER}",
        headers=H_OWNER,
        json={"role": "manager"},
    ).status_code == 200
    assert client.patch(
        f"/api/orgs/{org_id}/teams/{team['id']}", headers=H_MEMBER, json={"name": "Jigs"}
    ).status_code == 200
    detail = client.get(f"/api/orgs/{org_id}/teams/{team['id']}", headers=H_MEMBER).json()
    assert detail["team"]["name"] == "Jigs"
    assert detail["team"]["role"] == "manager"
    # Renaming onto an existing team name is refused.
    assert client.patch(
        f"/api/orgs/{org_id}/teams/{team['id']}", headers=H_OWNER, json={"name": "general"}
    ).status_code == 409


def test_team_membership_is_confined_to_the_organization(client, staffed):
    org_id = staffed["id"]
    team = client.post(
        f"/api/orgs/{org_id}/teams", headers=H_OWNER, json={"name": "Fixtures"}
    ).json()["team"]
    assert client.post(
        f"/api/orgs/{org_id}/teams/{team['id']}/members",
        headers=H_OWNER,
        json={"email": OUTSIDER},
    ).status_code == 400
    assert client.post(
        f"/api/orgs/{org_id}/teams/{team['id']}/members",
        headers=H_OWNER,
        json={"email": MEMBER},
    ).status_code == 201
    assert client.post(
        f"/api/orgs/{org_id}/teams/{team['id']}/members",
        headers=H_OWNER,
        json={"email": MEMBER},
    ).status_code == 409
    assert client.post(
        f"/api/orgs/{org_id}/teams/{team['id']}/members",
        headers=H_OWNER,
        json={"email": MEMBER, "role": "owner"},
    ).status_code == 400


def test_a_team_is_invisible_to_org_members_who_are_not_on_it(client, staffed):
    org_id = staffed["id"]
    team = client.post(
        f"/api/orgs/{org_id}/teams", headers=H_OWNER, json={"name": "Fixtures"}
    ).json()["team"]

    listed = client.get("/api/orgs", headers=H_MEMBER).json()["organizations"][0]["teams"]
    assert [t["name"] for t in listed] == ["General"]
    assert client.get(
        f"/api/orgs/{org_id}/teams/{team['id']}", headers=H_MEMBER
    ).status_code == 404
    assert client.get(f"/api/orgs/{org_id}", headers=H_MEMBER).json()["teams"] == listed

    # The org admin reaches it without any team_members row, as manager.
    as_admin = client.get(f"/api/orgs/{org_id}/teams/{team['id']}", headers=H_ADMIN).json()
    assert as_admin["team"]["role"] == "manager"
    assert [m["email"] for m in as_admin["members"]] == [OWNER]


def test_team_members_can_be_removed_and_can_leave(client, staffed):
    org_id = staffed["id"]
    general = _team_of(staffed, "General")
    # MEMBER joined General automatically when the invitation was accepted.
    detail = client.get(f"/api/orgs/{org_id}/teams/{general['id']}", headers=H_OWNER).json()
    assert MEMBER in [m["email"] for m in detail["members"]]

    assert client.delete(
        f"/api/orgs/{org_id}/teams/{general['id']}/members/{MEMBER}", headers=H_MEMBER
    ).status_code == 200
    assert client.delete(
        f"/api/orgs/{org_id}/teams/{general['id']}/members/{MEMBER}", headers=H_MEMBER
    ).status_code == 404
    # Re-added by a manager, then removed by one.
    assert client.post(
        f"/api/orgs/{org_id}/teams/{general['id']}/members",
        headers=H_OWNER,
        json={"email": MEMBER},
    ).status_code == 201
    assert client.delete(
        f"/api/orgs/{org_id}/teams/{general['id']}/members/{MEMBER}", headers=H_OWNER
    ).status_code == 200


def test_deleting_an_org_removes_it_from_every_members_view(client, staffed):
    org_id = staffed["id"]
    assert client.delete(f"/api/orgs/{org_id}", headers=H_OWNER).status_code == 200
    for headers in (H_OWNER, H_ADMIN, H_MEMBER):
        assert client.get("/api/orgs", headers=headers).json()["organizations"] == []
        assert client.get(f"/api/orgs/{org_id}", headers=headers).status_code == 404
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM teams WHERE org_id = %s", (org_id,))
        assert int(cur.fetchone()["n"]) == 0
        cur.execute("SELECT count(*) AS n FROM org_members WHERE org_id = %s", (org_id,))
        assert int(cur.fetchone()["n"]) == 0


def _university(client, monkeypatch, tmp_path):
    data = _create_org(client, name="University of Alabama")
    org_id = data["id"]
    with db.tx() as cur:
        cur.execute("SELECT id FROM teams WHERE org_id = %s", (org_id,))
        team_id = str(cur.fetchone()["id"])
    root = tmp_path / "box"
    root.mkdir()
    monkeypatch.setattr(settings, "university_org_id", org_id)
    monkeypatch.setattr(settings, "university_team_id", team_id)
    monkeypatch.setattr(settings, "university_workspace_root", str(root))
    original = Path.is_mount
    monkeypatch.setattr(Path, "is_mount", lambda p: p == root or original(p))
    return team_id, root


@pytest.mark.parametrize("email,verified,allowed", [
    ("student@ua.edu", True, True),
    ("student@crimson.ua.edu", True, True),
    ("student@crimson.ua.edu", False, False),
    ("student@notua.edu", True, False),
    ("student@ua.edu.attacker.example", True, False),
])
def test_university_requires_verified_domain_boundary(client, monkeypatch, tmp_path, email, verified, allowed):
    team_id, root = _university(client, monkeypatch, tmp_path)
    _seed(email)
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO credentials(email,password_hash,email_verified) VALUES (%s,'unused',%s)",
            (email, verified),
        )
    (root / "project.txt").write_text("university project")
    headers = {"X-Dev-User": email}
    response = client.get("/workspace/list", headers=headers)
    assert response.status_code == 200
    assert (response.json()["root"] == str(root)) is allowed
    direct = client.get("/api/bucket/download", params={"team": team_id, "path": "project.txt"}, headers=headers)
    assert direct.status_code == (200 if allowed else 404)
    assert accounts.get_account(email).role == "user"
    assert accounts.get_account(email).plan == "trial"
    assert not accounts.get_account(email).ai_grant


def test_university_selection_mount_loss_and_suspension(client, monkeypatch, tmp_path):
    team_id, root = _university(client, monkeypatch, tmp_path)
    email = "verified@crimson.ua.edu"
    _seed(email)
    with db.tx() as cur:
        cur.execute("INSERT INTO oauth_identities(provider,subject,email) VALUES ('google','university-test',%s)", (email,))
    headers = {"X-Dev-User": email}
    assert client.get("/workspace/list", headers=headers).json()["root"] == str(root)
    selected = client.post("/workspace/selection", headers=headers, json={"team": "personal"})
    assert selected.status_code == 200
    assert client.get("/workspace/list", headers=headers).json()["root"] != str(root)
    assert client.get("/workspace/list", params={"team": team_id}, headers=headers).json()["root"] == str(root)
    monkeypatch.setattr(Path, "is_mount", lambda p: False)
    assert client.get("/workspace/list", params={"team": team_id}, headers=headers).status_code == 503
    accounts.set_status(email, "suspended", decided_by="test")
    assert client.get("/workspace/list", params={"team": team_id}, headers=headers).status_code == 403


def test_open_workspace_rebinds_files_and_rejects_another_teams_link(client, monkeypatch, tmp_path):
    """The app-entry link switches subsequent file calls, not merely its URL."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_root", workspace)
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "index.html").write_text("<!doctype html><title>CAD</title>")
    monkeypatch.setattr(settings, "app_dir", app_dir)
    team = _team_of(_create_org(client), "General")
    team_root = settings.team_bucket_root(team["org_id"], team["id"])
    team_root.mkdir(parents=True)
    (team_root / "team-project.txt").write_text("shared design")

    opened = client.get("/app/", params={"team": team["id"]}, headers=H_OWNER)
    assert opened.status_code == 200, opened.text
    listing = client.get("/workspace/list", headers=H_OWNER).json()
    assert listing["root"] == str(team_root)
    assert "team-project.txt" in {entry["name"] for entry in listing["files"]}

    other = _team_of(_create_org(client, H_OUTSIDER, "Other team"), "General")
    denied = client.get("/app/", params={"team": other["id"]}, headers=H_OWNER)
    assert denied.status_code == 404
    assert client.get("/workspace/list", headers=H_OWNER).json()["root"] == str(team_root)

    personal = client.get("/app/", params={"team": "personal"}, headers=H_OWNER)
    assert personal.status_code == 200, personal.text
    listing = client.get("/workspace/list", headers=H_OWNER).json()
    assert listing["root"] != str(team_root)
    assert "team-project.txt" not in {entry["name"] for entry in listing["files"]}


def test_university_expired_trial_does_not_unlock_other_services(client, monkeypatch, tmp_path):
    _, root = _university(client, monkeypatch, tmp_path)
    email = "expired@ua.edu"
    _seed(email)
    with db.tx() as cur:
        cur.execute("INSERT INTO oauth_identities(provider,subject,email) VALUES ('google','expired-university',%s)", (email,))
        cur.execute("UPDATE accounts SET trial_ends_at = 1 WHERE email = %s", (email,))
    headers = {"X-Dev-User": email}
    assert client.get("/workspace/list", headers=headers).json()["root"] == str(root)
    portal = client.get("/api/portal/account", headers=headers)
    assert portal.status_code == 200
    assert portal.json()["workspace"]["storage_provider"] == "box"
    assert client.get("/api/portal/usage", headers=headers).status_code == 403
    assert client.get("/api/provider-keys", headers=headers).status_code == 403
    assert accounts.get_account(email).trial_ends_at == 1
