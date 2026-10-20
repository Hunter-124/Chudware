#!/usr/bin/env python3
"""Read-only, offline assessment of sanitized GitHub CI configuration exports.

No network calls, credential discovery, workflow execution, or signing occurs.
An export bundle is JSON with schema_version=1, collected_at (UTC RFC3339),
scope={organization, enterprise (null when absent)}, and exports=[{id, kind,
file, sha256, collected_at, complete, origin}]. Files are relative to the bundle.
API origins are {host: "api.github.com", method: "GET", endpoint: "..."}; operator
origins are {kind: "operator"}. API payloads use GitHub REST response shapes,
with paginated arrays flattened and total_count retained. See the behavioral
fixture generator in test_collect_ci_release_evidence.py for the complete input
contract, including controller/image/migration claims. Only secret NAMES and
scope inventories belong in these inputs, never credentials or secret values.

All successful checks mean ONLY that the supplied bytes describe the requested
policy. Neither a nonempty export nor an operator's authentication/signature
claim authenticates it. Live API/export authenticity, external machine authority,
and future independently verified signed approval always remain unavailable.
Exit 1 means open gates (including those authority gates); exit 2 means invalid
input. This tool deliberately cannot approve or enable a release.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_BYTES = 24 * 1024 * 1024
MAX_EXPORTS = 256
MAX_ITEMS = 4096
MAX_OUTPUT_BYTES = 512 * 1024
ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "chudware_ci_evidence_workflow_policy", ROOT / "scripts/verify-ci-workflows.py"
)
policy = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = policy
sys.dont_write_bytecode = True
_SPEC.loader.exec_module(policy)
NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SECRET = re.compile(r"[A-Z_][A-Z0-9_]{0,99}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?\Z")
SENSITIVE_KEYS = frozenset({
    "value", "secret_value", "encrypted_value", "private_key", "password",
    "access_token", "refresh_token", "authorization", "credential_value",
})


class InputError(ValueError):
    """An input failure whose text is safe to emit."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise InputError(message)


def timestamp(value: Any) -> datetime:
    require(isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value),
        "UTC collection/qualification timestamp unavailable")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise InputError("UTC timestamp malformed") from None
    require(parsed <= datetime.now(timezone.utc), "future timestamp is not evidence")
    return parsed


def integer(value: Any) -> bool:
    return type(value) is int and value > 0


def pairs(items: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in items:
        require(key not in result, "duplicate JSON key refused")
        result[key] = value
    return result


def validate_tree(value: Any, depth: int = 0) -> None:
    require(depth <= 32, "JSON nesting limit exceeded")
    if isinstance(value, dict):
        require(len(value) <= MAX_ITEMS, "JSON object limit exceeded")
        for key, item in value.items():
            require(len(key) <= 200 and key.lower() not in SENSITIVE_KEYS,
                    "secret-bearing or oversized field refused; export names/scopes only")
            validate_tree(item, depth + 1)
    elif isinstance(value, list):
        require(len(value) <= MAX_ITEMS, "JSON array limit exceeded")
        for item in value:
            validate_tree(item, depth + 1)
    elif isinstance(value, str):
        require(len(value) <= 16384, "JSON string limit exceeded")


def read_bound(root: Path, name: str, *, json_document: bool = True) -> tuple[Any, dict]:
    """Open every component without following symlinks; bound bytes and identity."""
    require(isinstance(name, str) and len(name) <= 240, "export filename invalid")
    parts = PurePosixPath(name).parts
    require(parts and not name.startswith("/") and "\\" not in name
            and all(part not in (".", "..") for part in parts), "export path escapes bundle")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    directories = []
    fd = None
    try:
        directory = os.open(root, flags | os.O_DIRECTORY)
        directories.append(directory)
        for part in parts[:-1]:
            directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            directories.append(directory)
        fd = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_FILE_BYTES,
                "export must be a bounded regular file")
        chunks = []
        size = 0
        while True:
            chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            require(size <= MAX_FILE_BYTES, "export size limit exceeded")
        after = os.fstat(fd)
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                 before.st_ctime_ns) == (after.st_dev, after.st_ino, after.st_size,
                                        after.st_mtime_ns, after.st_ctime_ns),
                "export changed during collection")
        raw = b"".join(chunks)
        try:
            text = raw.decode("utf-8")
            document = (json.loads(text, object_pairs_hook=pairs,
                                   parse_constant=lambda _: require(False, "nonfinite JSON refused"))
                        if json_document else text)
        except InputError:
            raise
        except (UnicodeError, ValueError, RecursionError):
            raise InputError("export JSON malformed") from None
        if json_document:
            validate_tree(document)
        return document, {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": size,
                          "device": after.st_dev, "inode": after.st_ino,
                          "mtime_ns": after.st_mtime_ns}
    except OSError:
        raise InputError("export unavailable or unsafe filesystem object") from None
    finally:
        if fd is not None:
            os.close(fd)
        for directory in reversed(directories):
            os.close(directory)


class Bundle:
    def __init__(self, path: Path, repository: str):
        self.root = path.absolute().parent
        self.manifest, identity = read_bound(self.root, path.name)
        require(isinstance(self.manifest, dict) and type(self.manifest.get("schema_version")) is int
                and self.manifest["schema_version"] == 1, "bundle schema_version must be 1")
        timestamp(self.manifest.get("collected_at"))
        scope = self.manifest.get("scope")
        require(isinstance(scope, dict) and scope.get("organization") == repository.split("/")[0]
                and NAME.fullmatch(scope["organization"]), "organization scope mismatch")
        enterprise = scope.get("enterprise", "missing")
        require(enterprise is None or isinstance(enterprise, str) and NAME.fullmatch(enterprise),
                "enterprise scope must be explicit (name or null)")
        self.scope = scope
        exports = self.manifest.get("exports")
        require(isinstance(exports, list) and 0 < len(exports) <= MAX_EXPORTS,
                "bounded export manifest unavailable")
        self.entries = {}
        self.documents = {}
        self.identities = [{"id": "bundle", **identity,
                            "collected_at": self.manifest["collected_at"],
                            "origin": {"kind": "operator-manifest"}}]
        total = identity["bytes"]
        files = {path.name}
        for entry in exports:
            require(isinstance(entry, dict), "export entry malformed")
            identifier = entry.get("id")
            require(isinstance(identifier, str) and NAME.fullmatch(identifier)
                    and identifier not in self.entries, "export id invalid or duplicate")
            require(entry.get("kind") in ("api", "operator-claim", "operator-artifact"),
                    "export kind unavailable")
            require(isinstance(entry.get("sha256"), str) and DIGEST.fullmatch(entry["sha256"]),
                    "export digest unavailable")
            collected = timestamp(entry.get("collected_at"))
            require(collected <= timestamp(self.manifest["collected_at"]),
                    "export timestamp exceeds bundle timestamp")
            require(entry.get("complete") is True, "incomplete/paginated export refused")
            origin = entry.get("origin")
            if entry["kind"] == "api":
                require(isinstance(origin, dict) and origin.get("host") == "api.github.com"
                        and origin.get("method") == "GET"
                        and isinstance(origin.get("endpoint"), str)
                        and re.fullmatch(r"[A-Za-z0-9_./-]{1,400}", origin["endpoint"]),
                        "read-only GitHub API origin unavailable")
                safe_origin = {key: origin[key] for key in ("host", "method", "endpoint")}
                if "includes_parents" in origin:
                    require(type(origin["includes_parents"]) is bool, "ruleset parent origin marker malformed")
                    safe_origin["includes_parents"] = origin["includes_parents"]
            else:
                require(isinstance(origin, dict) and origin.get("kind") == "operator",
                        "operator claim/artifact origin unavailable")
                safe_origin = {"kind": "operator"}
            require(entry.get("file") not in files, "export file reused under multiple identities")
            document, bound = read_bound(self.root, entry.get("file"))
            require(bound["sha256"] == entry["sha256"], "export digest mismatch")
            total += bound["bytes"]
            require(total <= MAX_BUNDLE_BYTES, "bundle aggregate byte limit exceeded")
            files.add(entry["file"])
            self.entries[identifier] = entry
            self.documents[identifier] = document
            self.identities.append({"id": identifier, "kind": entry["kind"], **bound,
                                    "collected_at": entry["collected_at"], "origin": safe_origin})

    def api(self, identifier: str, endpoint: str) -> dict:
        entry = self.entries.get(identifier, {})
        require(entry.get("kind") == "api" and entry.get("origin", {}).get("endpoint") == endpoint,
                "required API export/origin unavailable: " + identifier)
        document = self.documents[identifier]
        require(isinstance(document, dict), "API object unavailable: " + identifier)
        return document

    def items(self, identifier: str, endpoint: str, key: str) -> list[dict]:
        document = self.api(identifier, endpoint)
        rows = document.get(key)
        require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
                and type(document.get("total_count")) is int
                and document["total_count"] == len(rows), "complete API inventory unavailable: " + identifier)
        return rows

    def claims(self) -> dict:
        require(self.entries.get("claims", {}).get("kind") == "operator-claim"
                and isinstance(self.documents.get("claims"), dict), "operator claims unavailable")
        return self.documents["claims"]

    def authority(self, record: Any, kind: str) -> None:
        require(isinstance(record, dict), "operator evidence record unavailable")
        authority = record.get("authority")
        require(isinstance(authority, dict) and authority.get("kind") == kind
                and isinstance(authority.get("identity"), str)
                and 0 < len(authority["identity"]) <= 200,
                "external " + kind + " authority declaration unavailable")
        require(timestamp(record.get("collected_at")) <= timestamp(self.manifest["collected_at"]),
                "authority declaration timestamp exceeds collection")
        artifacts = record.get("artifacts")
        require(isinstance(artifacts, list) and 0 < len(artifacts) <= 32
                and all(isinstance(item, str) and self.entries.get(item, {}).get("kind") == "operator-artifact"
                        and bool(self.documents.get(item)) for item in artifacts),
                "hash-bound external authority artifacts unavailable")


def walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)


def source_contract(repository: str, tags: list[str], shas: list[str]) -> dict:
    """Derive definitions and secret/environment routing from the SAME policy/parser."""
    defining = set()
    reusable = set()
    required_secrets = {name: set() for name in policy.PROTECTED_ENVIRONMENTS}
    inputs = []
    for filename in policy.POLICY_WORKFLOWS:
        text, identity = read_bound(ROOT, str(policy.WORKFLOW_DIR / filename), json_document=False)
        try:
            document = policy.parse_workflow(text)
        except policy.WorkflowParseError:
            raise InputError("source workflow policy cannot be resolved") from None
        inputs.append({"id": "source." + filename, **identity})
        for job in document.get("jobs", {}).values():
            selector = job.get("runs-on")
            if not isinstance(selector, dict) or selector.get("group") != policy.TRUSTED_RUNNER_GROUP:
                continue
            defining.add(filename)
            if isinstance(document.get("on"), dict) and "workflow_call" in document["on"]:
                reusable.add(filename)
            environment = job.get("environment")
            if isinstance(environment, dict):
                environment = environment.get("name")
            require(environment in required_secrets, "protected source environment unresolved")
            for value in walk_strings([document.get("env", {}), job]):
                required_secrets[environment].update(re.findall(r"\bsecrets\.([A-Z_][A-Z0-9_]*)", value))
    require(defining and reusable, "protected defining workflows unavailable")
    refs = {repository + "/.github/workflows/" + filename + "@refs/heads/master"
            for filename in defining}
    refs.update(repository + "/.github/workflows/" + filename + "@" + ref
                for filename in reusable for ref in
                ["refs/tags/" + tag for tag in tags] + shas)
    _, identity = read_bound(ROOT, "scripts/verify-ci-workflows.py", json_document=False)
    inputs.append({"id": "source.workflow-policy", **identity})
    return {"defining_workflows": sorted(defining), "reusable_workflows": sorted(reusable),
            "selected_workflows": sorted(refs),
            "required_secrets": {name: sorted(names) for name, names in required_secrets.items()},
            "source_identities": inputs}


class Assessment:
    def __init__(self, bundle: Bundle, repository: str, repo_id: int,
                 tags: list[str], shas: list[str], contract: dict):
        self.bundle = bundle
        self.repository = repository
        self.repo_id = repo_id
        self.tags = tags
        self.shas = shas
        self.contract = contract
        self.gates = []
        self.observed = {}
        self.supplied = {}
        self.group_ids = {}

    def check(self, name: str, classification: str, function) -> None:
        try:
            function()
        except InputError as error:
            self.gates.append({"id": name, "status": "blocked", "classification": classification,
                               "reason": str(error)})
        except (TypeError, AttributeError, KeyError):
            self.gates.append({"id": name, "status": "blocked", "classification": classification,
                               "reason": "Required typed field unavailable or malformed."})
        else:
            self.gates.append({"id": name, "status": "pass", "classification": classification,
                               "reason": "Supplied bytes meet policy; authenticity is not established."})

    def repository_policy(self) -> None:
        repo = self.bundle.api("repository", "repos/" + self.repository)
        owner = repo.get("owner", {})
        require(integer(repo.get("id")) and repo["id"] == self.repo_id and repo.get("full_name") == self.repository
                and repo.get("private") is True and owner.get("type") == "Organization"
                and integer(owner.get("id")), "private organization repository identity unavailable/mismatched")
        account = self.bundle.api("organization", "orgs/" + self.bundle.scope["organization"])
        require(integer(account.get("id")) and account["id"] == owner["id"]
                and account.get("login") == owner.get("login")
                == self.bundle.scope["organization"], "organization identity mismatch")
        plan = account.get("plan", {})
        require(isinstance(plan, dict) and isinstance(plan.get("name"), str)
                and NAME.fullmatch(plan["name"]), "account plan export unavailable")
        self.observed["repository"] = {"id": self.repo_id, "full_name": self.repository,
                                       "owner_id": owner["id"], "plan_name": plan["name"]}

    def groups_policy(self) -> None:
        scopes = [("org", "orgs/" + self.bundle.scope["organization"])]
        if self.bundle.scope["enterprise"] is not None:
            scopes.append(("enterprise", "enterprises/" + self.bundle.scope["enterprise"]))
        seen_runners = {}
        group_runners = {}
        required_groups = {policy.UNTRUSTED_RUNNER_GROUP, policy.TRUSTED_RUNNER_GROUP}
        summaries = []
        inherited = []
        memberships = {}
        scoped_members = {}
        for scope, base in scopes:
            inventory = self.bundle.items(scope + ".runners", base + "/actions/runners", "runners")
            ids = [runner.get("id") for runner in inventory]
            require(all(integer(item) for item in ids) and len(set(ids)) == len(ids),
                    "runner inventory identities invalid")
            seen_runners[scope] = set(ids)
            groups = self.bundle.items(scope + ".groups", base + "/actions/runner-groups", "runner_groups")
            local_members = set()
            local_group_ids = set()
            for group in groups:
                group_id = group.get("id")
                require(integer(group_id) and group_id not in local_group_ids,
                        "group id unavailable/duplicate")
                local_group_ids.add(group_id)
                # An inherited group is assessed at its enterprise authority, not twice.
                if group.get("inherited") is True:
                    require(scope == "org" and self.bundle.scope["enterprise"] is not None,
                            "inherited group lacks enterprise inventory authority")
                    inherited.append((group_id, group.get("name")))
                    continue
                require(group.get("inherited") is False, "group inheritance unknown")
                prefix = scope + ".group." + str(group_id)
                detail = self.bundle.api(prefix, base + "/actions/runner-groups/" + str(group_id))
                require(integer(detail.get("id")) and detail["id"] == group_id
                        and detail.get("name") == group.get("name"), "group detail/list identity mismatch")
                members = self.bundle.items(prefix + ".runners", base + "/actions/runner-groups/"
                                            + str(group_id) + "/runners", "runners")
                member_ids = [runner.get("id") for runner in members]
                require(all(integer(item) for item in member_ids) and len(set(member_ids)) == len(member_ids),
                        "runner membership identities invalid")
                require(not local_members.intersection(member_ids), "overlapping runner memberships refused")
                local_members.update(member_ids)
                memberships[(scope, group_id)] = (detail.get("name"), set(member_ids))
                name = detail.get("name")
                require(isinstance(name, str) and NAME.fullmatch(name), "group name invalid")
                repositories = self.bundle.items(prefix + ".repositories", base + "/actions/runner-groups/"
                                                 + str(group_id) + "/repositories", "repositories")
                selected_ids = [repo.get("id") for repo in repositories]
                require(all(integer(item) for item in selected_ids) and len(set(selected_ids)) == len(selected_ids),
                        "selected repository inventory invalid")
                if name in required_groups:
                    require(name not in self.group_ids, "required group has multiple authorities")
                    require(detail.get("visibility") == "selected" and selected_ids == [self.repo_id]
                            and detail.get("allows_public_repositories") is False
                            and detail.get("default") is False, "required group selected/private repository scope invalid")
                    require(member_ids, "required runner capacity unavailable")
                    if name == policy.TRUSTED_RUNNER_GROUP:
                        workflows = detail.get("selected_workflows")
                        require(detail.get("restricted_to_workflows") is True
                                and isinstance(workflows, list) and all(isinstance(item, str) for item in workflows)
                                and len(set(workflows)) == len(workflows)
                                and set(workflows) == set(self.contract["selected_workflows"]),
                                "protected defining workflow refs are missing, wildcard, obsolete, or incorrect")
                    self.group_ids[name] = {"scope": scope, "id": group_id, "runner_ids": sorted(member_ids)}
                    group_runners[name] = set(member_ids)
                else:
                    require(not member_ids or detail.get("visibility") == "selected"
                            and self.repo_id not in selected_ids,
                            "default/legacy/alternate group exposes repository capacity")
                summaries.append({"scope": scope, "id": group_id, "name": name,
                                  "runner_ids": sorted(member_ids), "selected_repository_ids": sorted(selected_ids),
                                  "visibility": detail.get("visibility"),
                                  "restricted_to_workflows": detail.get("restricted_to_workflows"),
                                  "selected_workflows": self.contract["selected_workflows"]
                                  if name == policy.TRUSTED_RUNNER_GROUP else []})
            scoped_members[scope] = local_members
        parent_members = set()
        for group_id, name in inherited:
            parent = memberships.get(("enterprise", group_id))
            require(parent is not None and parent[0] == name, "inherited group identity lacks matching enterprise authority")
            parent_members.update(parent[1])
        for scope, members in scoped_members.items():
            expected = members | parent_members if scope == "org" else members
            require(seen_runners[scope] == expected, "group membership inventory does not cover runner inventory")
        require(set(self.group_ids) == required_groups, "required disjoint groups unavailable")
        require(not group_runners[policy.TRUSTED_RUNNER_GROUP].intersection(group_runners[policy.UNTRUSTED_RUNNER_GROUP]),
                "overlapping trusted/untrusted runner identities refused")
        if len(seen_runners) == 2:
            require(not scoped_members["org"].intersection(scoped_members["enterprise"]),
                    "independently registered organization/enterprise runner identities overlap")
        legacy = self.bundle.items("repository.runners", "repos/" + self.repository + "/actions/runners", "runners")
        require(not legacy, "legacy repository-scoped runner registrations remain")
        self.observed["runner_groups"] = summaries

    def ref_policy(self) -> None:
        base = "repos/" + self.repository
        listing = self.bundle.items("rulesets", base + "/rulesets", "rulesets")
        require(self.bundle.entries["rulesets"]["origin"].get("includes_parents") is True,
                "inherited organization/enterprise ruleset collection unavailable")
        branch_ok = False
        tag_ok = False
        ruleset_ids = set()
        for listed in listing:
            identifier = listed.get("id")
            require(integer(identifier) and identifier not in ruleset_ids, "ruleset identity invalid")
            ruleset_ids.add(identifier)
            detail = self.bundle.api("ruleset." + str(identifier), base + "/rulesets/" + str(identifier))
            require(integer(detail.get("id")) and detail["id"] == identifier, "ruleset identity mismatch")
            require(detail.get("enforcement") in ("active", "disabled", "evaluate"),
                    "ruleset enforcement unavailable")
            if detail["enforcement"] != "active":
                continue
            refs = detail.get("conditions", {}).get("ref_name", {})
            require(isinstance(refs, dict) and isinstance(refs.get("include"), list)
                    and refs.get("exclude") == [], "active ruleset ref coverage unknown")
            require(detail.get("bypass_actors") == [], "ruleset bypass authority remains open")
            rules = detail.get("rules")
            require(isinstance(rules, list) and all(isinstance(rule, dict) for rule in rules),
                    "ruleset rules unavailable")
            by_type = {rule.get("type"): rule for rule in rules}
            if detail.get("target") == "branch" and ("refs/heads/master" in refs["include"]
                                                         or "~ALL" in refs["include"]):
                review = by_type.get("pull_request", {}).get("parameters", {})
                checks = by_type.get("required_status_checks", {}).get("parameters", {})
                count = review.get("required_approving_review_count")
                branch_ok |= (type(count) is int and count >= 1
                              and review.get("require_code_owner_review") is True
                              and review.get("dismiss_stale_reviews_on_push") is True
                              and review.get("require_last_push_approval") is True
                              and bool(checks.get("required_status_checks"))
                              and {"deletion", "non_fast_forward"}.issubset(by_type))
            if detail.get("target") == "tag" and ("refs/tags/v*" in refs["include"]
                                                      or "~ALL" in refs["include"]):
                tag_ok |= {"creation", "update", "deletion", "non_fast_forward"}.issubset(by_type)
        require(branch_ok and tag_ok, "protected master review/status and version-tag creation/update rules unavailable")
        bindings = []
        for identifier, ref in [("master", "heads/master")] + [("tag." + tag, "tags/" + tag) for tag in self.tags]:
            exported = self.bundle.api("ref." + identifier, base + "/git/ref/" + ref)
            obj = exported.get("object", {})
            require(exported.get("ref") == "refs/" + ref, "protected ref identity mismatch")
            tag_objects = []
            while isinstance(obj, dict) and obj.get("type") == "tag":
                sha = obj.get("sha")
                require(isinstance(sha, str) and policy.SHA_RE.fullmatch(sha)
                        and sha not in tag_objects and len(tag_objects) < 8,
                        "annotated tag resolution is cyclic, malformed, or too deep")
                tag_objects.append(sha)
                annotated = self.bundle.api("tag-object." + sha, base + "/git/tags/" + sha)
                require(annotated.get("sha") == sha, "annotated tag object identity mismatch")
                obj = annotated.get("object")
            require(isinstance(obj, dict) and obj.get("type") == "commit"
                    and isinstance(obj.get("sha"), str) and policy.SHA_RE.fullmatch(obj["sha"]),
                    "protected exact commit unavailable")
            bindings.append({"ref": exported["ref"], "commit": obj["sha"], "tag_objects": tag_objects})
        for sha in self.shas:
            commit = self.bundle.api("commit." + sha, base + "/git/commits/" + sha)
            require(commit.get("sha") == sha, "approved immutable commit identity mismatch")
            bindings.append({"ref": sha, "commit": sha})
        self.observed["protected_refs"] = bindings
        self.observed["ruleset_ids"] = sorted(ruleset_ids)

    def environment_and_secret_policy(self) -> None:
        base = "repos/" + self.repository
        environments = self.bundle.items("environments", base + "/environments", "environments")
        require(all(isinstance(row.get("name"), str) and NAME.fullmatch(row["name"])
                    for row in environments), "environment identity unavailable")
        names = [row["name"] for row in environments]
        require(len(names) == len(set(names)) and policy.PROTECTED_ENVIRONMENTS.issubset(names),
                "protected environment inventory unavailable/duplicate")
        secret_summary = []
        protections = []
        for listed in environments:
            name = listed["name"]
            detail = self.bundle.api("environment." + name, base + "/environments/" + name)
            require(detail.get("name") == name and integer(detail.get("id"))
                    and detail["id"] == listed.get("id"), "environment detail identity mismatch")
            if name in policy.PROTECTED_ENVIRONMENTS:
                branch_policy = detail.get("deployment_branch_policy", {})
                require(branch_policy.get("protected_branches") is False
                        and branch_policy.get("custom_branch_policies") is True
                        and detail.get("can_admins_bypass") is False, "environment ref/admin bypass protection unavailable")
                reviewer_rules = [rule for rule in detail.get("protection_rules", [])
                                  if isinstance(rule, dict) and rule.get("type") == "required_reviewers"]
                require(len(reviewer_rules) == 1 and reviewer_rules[0].get("prevent_self_review") is True
                        and isinstance(reviewer_rules[0].get("reviewers"), list)
                        and reviewer_rules[0]["reviewers"]
                        and all(row.get("type") in ("User", "Team")
                                and integer(row.get("reviewer", {}).get("id"))
                                for row in reviewer_rules[0]["reviewers"]),
                        "authorized environment reviewers/self-review prevention unavailable")
                refs = self.bundle.items("environment." + name + ".branches",
                                         base + "/environments/" + name + "/deployment-branch-policies", "branch_policies")
                allowed = {("branch", "master")}
                if name != "chudware-maintenance-protected":
                    allowed.update(("tag", tag) for tag in self.tags)
                actual = [(row.get("type"), row.get("name")) for row in refs]
                require(all(isinstance(kind, str) and isinstance(ref, str) for kind, ref in actual)
                        and len(set(actual)) == len(actual) and set(actual) == allowed,
                        "environment refs are wildcard, incorrect, missing, or obsolete")
                protections.append({"name": name, "id": detail["id"],
                                    "reviewer_ids": sorted(row["reviewer"]["id"] for row in reviewer_rules[0]["reviewers"]),
                                    "allowed_refs": [{"type": kind, "name": ref} for kind, ref in sorted(allowed)]})
            secrets = self.bundle.items("environment." + name + ".secrets",
                                       base + "/environments/" + name + "/secrets", "secrets")
            secret_names = self.secret_names(secrets)
            require(name in policy.PROTECTED_ENVIRONMENTS or not secret_names,
                    "unprotected environment exposes secret names")
            require(set(self.contract["required_secrets"].get(name, [])) == secret_names,
                    "protected environment secret names/scopes differ from reviewed workflow requirements")
            secret_summary.extend({"name": item, "scope": "environment", "environment": name}
                                  for item in sorted(secret_names))
        repository_secrets = self.bundle.items("repository.secrets", base + "/actions/secrets", "secrets")
        self.secret_names(repository_secrets)
        require(not repository_secrets, "branch-visible repository secrets remain; migrate protected credentials")
        org_base = "orgs/" + self.bundle.scope["organization"]
        org_secrets = self.bundle.items("organization.secrets", org_base + "/actions/secrets", "secrets")
        self.secret_names(org_secrets)
        for secret in org_secrets:
            name = secret["name"]
            require(secret.get("visibility") == "selected", "branch-visible organization secret scope remains")
            repositories = self.bundle.items("organization.secret." + name + ".repositories",
                                            org_base + "/actions/secrets/" + name + "/repositories", "repositories")
            require(all(integer(repo.get("id")) for repo in repositories), "organization secret repository scope unknown")
            selected_ids = sorted(repo["id"] for repo in repositories)
            require(self.repo_id not in selected_ids, "branch-visible organization secret remains for repository")
            secret_summary.append({"name": name, "scope": "organization", "selected_repository_ids": selected_ids})
        self.observed["environment_protections"] = protections
        self.observed["secret_names_and_scopes"] = secret_summary

    @staticmethod
    def secret_names(rows: list[dict]) -> set[str]:
        names = [row.get("name") for row in rows]
        require(all(isinstance(name, str) and SECRET.fullmatch(name) for name in names)
                and len(set(names)) == len(names), "secret NAME inventory malformed")
        require(not any("OWNER" in name and "KEY" in name or "OFFLINE" in name and "KEY" in name
                        for name in names), "offline owner key must be absent from Actions")
        return set(names)

    def controller_claims(self) -> None:
        record = self.bundle.claims().get("controller")
        self.bundle.authority(record, "external-controller")
        require(integer(record.get("group_id"))
                and record["group_id"] == self.group_ids.get(policy.UNTRUSTED_RUNNER_GROUP, {}).get("id"),
                "destruction controller group identity unavailable/mismatched")
        facts = record.get("facts", {})
        require(facts.get("registration_ephemeral") is True and type(facts.get("max_jobs")) is int
                and facts["max_jobs"] == 1 and facts.get("complete_vm_os_disk_deleted") is True
                and facts.get("runner_deregistered") is True and facts.get("quarantine_failed_destruction") is True
                and facts.get("success") is True and facts.get("failure") is True
                and facts.get("cancellation") is True and facts.get("abnormal_shutdown") is True,
                "complete ephemeral destruction/deregistration/quarantine evidence unavailable")
        self.supplied["controller"] = {"status": "supplied-unverified", "artifacts": record["artifacts"]}

    def image_and_state_claims(self) -> None:
        claims = self.bundle.claims()
        images = claims.get("images")
        require(isinstance(images, list) and images, "qualified image evidence unavailable")
        coverage = {name: set() for name in (policy.UNTRUSTED_RUNNER_GROUP, policy.TRUSTED_RUNNER_GROUP)}
        image_summary = []
        for image in images:
            self.bundle.authority(image, "external-image-qualifier")
            group = image.get("group")
            require(group in coverage and integer(image.get("group_id"))
                    and image["group_id"] == self.group_ids.get(group, {}).get("id"),
                    "image group identity unavailable/mismatched")
            require(image.get("os") in ("Linux", "Windows", "macOS") and image.get("architecture") in ("X64", "ARM64")
                    and isinstance(image.get("image_id"), str) and NAME.fullmatch(image["image_id"])
                    and isinstance(image.get("digest"), str) and policy.DOCKER_DIGEST_RE.fullmatch(image["digest"]),
                    "qualified image ID/digest/native platform unavailable")
            facts = image.get("facts", {})
            require(all(facts.get(name) is True for name in (
                "qualified_before_dispatch_and_secret_release", "outside_writable_checkout",
                "os_interpreter_startup_native_tools_sdks_qualified", "reviewed_runtime_locks_and_wheel_closure",
                "read_only_digest_qualified_seed", "not_pr_built", "native_architecture")),
                "external image/runtime/tool qualification claims incomplete")
            coverage[group].add(image["os"])
            image_summary.append({key: image[key] for key in ("group", "group_id", "os", "architecture", "image_id", "digest")})
        require(all(oses == {"Linux", "Windows", "macOS"} for oses in coverage.values()),
                "complete separate native platform image evidence unavailable")
        separation = claims.get("state_separation")
        self.bundle.authority(separation, "external-controller")
        facts = separation.get("facts", {})
        require(all(facts.get(name) is True for name in (
            "separate_os_vm_boundary", "no_shared_writable_home_userprofile", "no_shared_interpreter_venv_site_packages_pth",
            "no_shared_path_homebrew_cargo_rustup", "no_shared_npm_pip_sdk_tool_caches", "no_shared_workspace_temp_mounts",
            "no_shared_keychains_certificate_stores_signing_sockets", "no_host_admin_sockets_credentials_or_production_network",
            "no_pr_artifact_or_mutable_cache_consumption", "sentinel_success_failure_cancellation_isolation")),
            "writable state/network/OS separation claims incomplete")
        self.supplied["images"] = {"status": "supplied-unverified", "images": image_summary}
        self.supplied["state_separation"] = {"status": "supplied-unverified", "artifacts": separation["artifacts"]}

    def migration_claims(self) -> None:
        record = self.bundle.claims().get("migration")
        self.bundle.authority(record, "organization-administrator")
        require(record.get("repository_id") == self.repo_id and record.get("organization") == self.bundle.scope["organization"],
                "migration authority repository/organization mismatch")
        facts = record.get("facts", {})
        require(all(facts.get(name) is True for name in (
            "organization_enterprise_restricted_workflows_capability_verified", "repository_ownership_and_registration_migrated",
            "legacy_repository_default_protected_capacity_removed", "protected_secrets_removed_from_branch_visible_scopes",
            "offline_owner_key_absent_from_actions", "previously_shared_signers_rebuilt_or_quarantined",
            "credentials_reenabled_only_after_external_qualification", "workflow_script_lock_image_changes_codeowner_reviewed",
            "modified_branch_cannot_schedule_protected_member", "dummy_environment_secret_denied_to_branch",
            "environment_reviewers_authorized", "approved_exact_refs_reviewed")),
            "required migration/rebuild/credential/routing/review claims incomplete")
        previous = record.get("previously_shared_hosts")
        require(isinstance(previous, list) and previous and all(
            isinstance(host, dict) and isinstance(host.get("id"), str) and NAME.fullmatch(host["id"])
            and host.get("disposition") in ("rebuilt", "quarantined") for host in previous),
            "previously shared signing-host disposition inventory unavailable")
        require(record.get("approved_tags") == self.tags and record.get("approved_shas") == self.shas,
                "approved reusable exact-ref declarations mismatch")
        self.supplied["migration"] = {"status": "supplied-unverified", "artifacts": record["artifacts"],
                                       "previously_shared_hosts": previous}

    def run(self) -> dict:
        for name, function in (("repository-organization-plan", self.repository_policy),
                               ("runner-groups-and-defining-refs", self.groups_policy),
                               ("protected-branch-tag-rules", self.ref_policy),
                               ("environments-and-secret-scopes", self.environment_and_secret_policy)):
            self.check(name, "api-export-policy", function)
        for name, function in (("ephemeral-destruction-evidence", self.controller_claims),
                               ("image-and-state-separation-evidence", self.image_and_state_claims),
                               ("migration-and-negative-routing-evidence", self.migration_claims)):
            self.check(name, "operator-claim-completeness", function)
        for name, reason in (
            ("authenticated-api-export", "Offline files do not prove authenticated collection, current server state, or account capability."),
            ("external-controller-image-authority", "Operator artifacts/claims need independently authenticated controller, image, state-separation and signer-rebuild authority."),
            ("signed-release-approval", "No future signed approval is fabricated or accepted here; an authorized independent verifier must bind reviewed evidence and exact protected build inputs."),
        ):
            self.gates.append({"id": name, "status": "unavailable", "classification": "authority-only", "reason": reason})
        policy_ok = all(gate["status"] == "pass" for gate in self.gates if gate["classification"] == "api-export-policy")
        claims_ok = all(gate["status"] == "pass" for gate in self.gates if gate["classification"] == "operator-claim-completeness")
        return {"schema_version": 1, "mode": "offline-read-only", "release_approved": False,
                "api_export_policy_matches": policy_ok, "operator_claims_complete": claims_ok,
                "repository": {"name": self.repository, "id": self.repo_id},
                "scope": self.bundle.scope, "collection_timestamp": self.bundle.manifest["collected_at"],
                "assessment_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "requested_refs_not_approvals": {"tags": self.tags, "immutable_shas": self.shas},
                "source_policy": self.contract, "file_identities": self.bundle.identities,
                "observed_export_facts_not_live_attestation": self.observed,
                "operator_supplied_claims_not_verified_authority": self.supplied,
                "gates": self.gates, "open_gates": [gate["id"] for gate in self.gates if gate["status"] != "pass"]}


def assess(path: Path, repository: str, repo_id: int, tags: list[str], shas: list[str]) -> dict:
    require(isinstance(repository, str) and REPO.fullmatch(repository), "repository must be exact ORG/REPO")
    require(integer(repo_id), "exact positive repository ID required")
    require(len(tags) <= 32 and len(shas) <= 32
            and all(isinstance(tag, str) and TAG.fullmatch(tag) for tag in tags)
            and all(isinstance(sha, str) and policy.SHA_RE.fullmatch(sha) for sha in shas)
            and len(set(tags)) == len(tags) and len(set(shas)) == len(shas), "approved refs must be bounded, unique and exact")
    tags, shas = sorted(tags), sorted(shas)
    contract = source_contract(repository, tags, shas)
    return Assessment(Bundle(path, repository), repository, repo_id, tags, shas, contract).run()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, required=True, help="Explicit sanitized export manifest; no ambient credential discovery")
    parser.add_argument("--repo", required=True, help="Exact expected organization/repository")
    parser.add_argument("--repo-id", type=int, required=True, help="Exact independently expected repository ID")
    parser.add_argument("--approved-tag", action="append", default=[], help="Requested exact approved vX.Y.Z tag, never a wildcard or evidence of approval")
    parser.add_argument("--approved-sha", action="append", default=[], help="Requested immutable reusable-workflow SHA, never evidence of approval")
    args = parser.parse_args(argv)
    try:
        result = assess(args.bundle, args.repo, args.repo_id, args.approved_tag, args.approved_sha)
        text = json.dumps(result, sort_keys=True, indent=2, ensure_ascii=True)
        require(len(text.encode("utf-8")) <= MAX_OUTPUT_BYTES, "sanitized evidence output limit exceeded")
    except InputError as error:
        print(json.dumps({"schema_version": 1, "release_approved": False,
                          "status": "blocked", "reason": str(error)}, ensure_ascii=True))
        return 2
    except (TypeError, AttributeError, KeyError, OverflowError, OSError):
        print(json.dumps({"schema_version": 1, "release_approved": False, "status": "blocked",
                          "reason": "Required typed input or bounded file unavailable."}))
        return 2
    print(text)
    return 1 if result["open_gates"] else 0


if __name__ == "__main__":
    sys.exit(main())
