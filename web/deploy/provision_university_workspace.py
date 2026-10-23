#!/usr/bin/env python3
"""Provision the UA collaboration boundary; never mint credentials or paid grants.

Run with the production gateway Python and its existing environment loaded.
The mount must be installed separately with an independently authorized Box token.
This command updates only organization/team membership and three gateway settings.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))

from db import tx


def provision(owner: str, steward: str, root: Path, env_file: Path) -> dict[str, str]:
    if not root.is_absolute():
        raise ValueError("mount root must be absolute")
    with tx() as cur:
        cur.execute("SELECT email FROM accounts WHERE email = %s AND status = 'active' AND role = 'admin' AND plan IN ('studio','enterprise')", (owner,))
        if cur.fetchone() is None:
            raise ValueError("owner must already be an active Studio/Enterprise operator")
        cur.execute("""SELECT a.email FROM accounts a WHERE a.email = %s AND a.status = 'active'
                       AND (EXISTS (SELECT 1 FROM credentials c WHERE c.email=a.email AND c.email_verified)
                            OR EXISTS (SELECT 1 FROM oauth_identities i WHERE i.email=a.email))""", (steward,))
        if cur.fetchone() is None or steward.split("@")[-1] not in {"ua.edu", "crimson.ua.edu"}:
            raise ValueError("steward must already have a verified active UA account")
        cur.execute("SELECT id FROM organizations WHERE name = 'University of Alabama' AND created_by = %s", (owner,))
        rows = cur.fetchall()
        if len(rows) > 1:
            raise ValueError("ambiguous university organizations; resolve explicitly")
        org_id = str(rows[0]["id"]) if rows else str(uuid.uuid4())
        cur.execute("INSERT INTO organizations(id,name,created_by) VALUES (%s,'University of Alabama',%s) ON CONFLICT DO NOTHING", (org_id, owner))
        cur.execute("INSERT INTO org_members(org_id,email,role) VALUES (%s,%s,'owner') ON CONFLICT DO NOTHING", (org_id, owner))
        cur.execute("SELECT id FROM teams WHERE org_id = %s AND is_general", (org_id,))
        row = cur.fetchone()
        team_id = str(row["id"]) if row else str(uuid.uuid4())
        cur.execute("INSERT INTO teams(id,org_id,name,is_general,created_by) VALUES (%s,%s,'ME-490',TRUE,%s) ON CONFLICT DO NOTHING", (team_id, org_id, owner))
        cur.execute("INSERT INTO org_members(org_id,email,role) VALUES (%s,%s,'member') ON CONFLICT DO NOTHING", (org_id, steward))
        cur.execute("INSERT INTO team_members(team_id,email,role) VALUES (%s,%s,'manager') ON CONFLICT (team_id,email) DO UPDATE SET role='manager'", (team_id, steward))
    values = {
        "CHUDWARE_WEB_UNIVERSITY_ORG_ID": org_id,
        "CHUDWARE_WEB_UNIVERSITY_TEAM_ID": team_id,
        "CHUDWARE_WEB_UNIVERSITY_WORKSPACE_ROOT": str(root),
    }
    previous = env_file.read_text()
    lines = [line for line in previous.splitlines() if line.split("=", 1)[0] not in values]
    lines.extend(f'{key}="{value}"' for key, value in values.items())
    temporary = env_file.with_name(env_file.name + ".university-new")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write("\n".join(lines) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, env_file)
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--steward", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    for key, value in provision(args.owner, args.steward, args.root, args.env_file).items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
