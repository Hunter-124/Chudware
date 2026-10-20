#!/usr/bin/env python3
"""Behavioral offline CI export tests; --write-fixture DIR creates synthetic inputs.

Fixture IDs, digests, authority declarations and artifacts are deliberately
synthetic, NOT authenticated CI/image/destruction/approval evidence. The real
collector CLI must leave the three authority-only gates open for these inputs.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
_SPEC = importlib.util.spec_from_file_location(
    "chudware_ci_release_evidence", Path(__file__).with_name("collect_ci_release_evidence.py")
)
collector = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = collector
_SPEC.loader.exec_module(collector)

REPOSITORY = "FixtureOrg/CAD"
REPO_ID = 31415
TAG = "v1.2.3"
COLLECTED = "2026-10-12T00:00:00Z"
COMMIT = hashlib.sha1(b"synthetic fixture commit, not a real source revision").hexdigest()


def inventory(key, rows):
    return {"total_count": len(rows), key: rows}


def authority(kind):
    return {"authority": {"kind": kind, "identity": "synthetic-fixture-not-real-authority"},
            "collected_at": COLLECTED, "artifacts": ["fixture-artifact"]}


class Fixture:
    def __init__(self):
        self.documents = {}
        self.entries = {}
        self.scope = {"organization": "FixtureOrg", "enterprise": None}
        contract = collector.source_contract(REPOSITORY, [TAG], [])
        repo_base = "repos/" + REPOSITORY
        org_base = "orgs/FixtureOrg"
        self.add("repository", repo_base,
                 {"id": REPO_ID, "full_name": REPOSITORY, "private": True,
                  "owner": {"id": 7, "type": "Organization", "login": "FixtureOrg"}})
        self.add("organization", org_base, {"id": 7, "login": "FixtureOrg", "plan": {"name": "enterprise"}})
        groups = []
        runners = []
        for identifier, name in ((1, collector.policy.UNTRUSTED_RUNNER_GROUP),
                                 (2, collector.policy.TRUSTED_RUNNER_GROUP)):
            group = {"id": identifier, "name": name, "inherited": False, "default": False,
                     "visibility": "selected", "allows_public_repositories": False,
                     "restricted_to_workflows": identifier == 2,
                     "selected_workflows": contract["selected_workflows"] if identifier == 2 else []}
            groups.append(copy.deepcopy(group))
            prefix = "org.group." + str(identifier)
            path = org_base + "/actions/runner-groups/" + str(identifier)
            members = [{"id": identifier * 10 + i, "os": os_name}
                       for i, os_name in enumerate(("Linux", "Windows", "macOS"))]
            runners.extend(members)
            self.add(prefix, path, group)
            self.add(prefix + ".runners", path + "/runners", inventory("runners", members))
            self.add(prefix + ".repositories", path + "/repositories", inventory("repositories", [{"id": REPO_ID}]))
        self.add("org.groups", org_base + "/actions/runner-groups", inventory("runner_groups", groups))
        self.add("org.runners", org_base + "/actions/runners", inventory("runners", runners))
        self.add("repository.runners", repo_base + "/actions/runners", inventory("runners", []))
        self.add("repository.secrets", repo_base + "/actions/secrets", inventory("secrets", []))
        self.add("organization.secrets", org_base + "/actions/secrets", inventory("secrets", []))
        self.add("rulesets", repo_base + "/rulesets", inventory("rulesets", [{"id": 100}, {"id": 101}]))
        self.entries["rulesets"]["origin"]["includes_parents"] = True
        self.add("ruleset.100", repo_base + "/rulesets/100", {
            "id": 100, "target": "branch", "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["refs/heads/master"], "exclude": []}},
            "rules": [{"type": "deletion"}, {"type": "non_fast_forward"},
                      {"type": "pull_request", "parameters": {"required_approving_review_count": 2,
                       "require_code_owner_review": True, "dismiss_stale_reviews_on_push": True,
                       "require_last_push_approval": True}},
                      {"type": "required_status_checks", "parameters": {
                       "required_status_checks": [{"context": "reviewed-policy-gate"}]}}]})
        self.add("ruleset.101", repo_base + "/rulesets/101", {
            "id": 101, "target": "tag", "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
            "rules": [{"type": kind} for kind in ("creation", "update", "deletion", "non_fast_forward")]})
        for identifier, ref in (("master", "heads/master"), ("tag." + TAG, "tags/" + TAG)):
            self.add("ref." + identifier, repo_base + "/git/ref/" + ref,
                     {"ref": "refs/" + ref, "object": {"type": "commit", "sha": COMMIT}})
        environments = []
        for identifier, name in enumerate(sorted(collector.policy.PROTECTED_ENVIRONMENTS), 200):
            path = repo_base + "/environments/" + name
            environments.append({"id": identifier, "name": name})
            self.add("environment." + name, path, {
                "id": identifier, "name": name, "can_admins_bypass": False,
                "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
                "protection_rules": [{"type": "required_reviewers", "prevent_self_review": True,
                                      "reviewers": [{"type": "Team", "reviewer": {"id": 88}}]}]})
            branches = [{"type": "branch", "name": "master"}]
            if name != "chudware-maintenance-protected":
                branches.append({"type": "tag", "name": TAG})
            self.add("environment." + name + ".branches", path + "/deployment-branch-policies",
                     inventory("branch_policies", branches))
            self.add("environment." + name + ".secrets", path + "/secrets",
                     inventory("secrets", [{"name": item} for item in contract["required_secrets"][name]]))
        self.add("environments", repo_base + "/environments", inventory("environments", environments))
        controller = {**authority("external-controller"), "group_id": 1,
                      "facts": {"registration_ephemeral": True, "max_jobs": 1,
                                **{name: True for name in (
                                    "complete_vm_os_disk_deleted", "runner_deregistered", "quarantine_failed_destruction",
                                    "success", "failure", "cancellation", "abnormal_shutdown")}}}
        images = []
        for identifier, group in ((1, collector.policy.UNTRUSTED_RUNNER_GROUP),
                                  (2, collector.policy.TRUSTED_RUNNER_GROUP)):
            for os_name in ("Linux", "Windows", "macOS"):
                seed = (group + os_name).encode("ascii")
                images.append({**authority("external-image-qualifier"), "group": group, "group_id": identifier,
                               "os": os_name, "architecture": "X64", "image_id": "fixture-" + str(identifier) + "-" + os_name,
                               "digest": "sha256:" + hashlib.sha256(seed).hexdigest(),
                               "facts": {name: True for name in (
                                   "qualified_before_dispatch_and_secret_release", "outside_writable_checkout",
                                   "os_interpreter_startup_native_tools_sdks_qualified", "reviewed_runtime_locks_and_wheel_closure",
                                   "read_only_digest_qualified_seed", "not_pr_built", "native_architecture")}})
        separation = {**authority("external-controller"), "facts": {name: True for name in (
            "separate_os_vm_boundary", "no_shared_writable_home_userprofile", "no_shared_interpreter_venv_site_packages_pth",
            "no_shared_path_homebrew_cargo_rustup", "no_shared_npm_pip_sdk_tool_caches", "no_shared_workspace_temp_mounts",
            "no_shared_keychains_certificate_stores_signing_sockets", "no_host_admin_sockets_credentials_or_production_network",
            "no_pr_artifact_or_mutable_cache_consumption", "sentinel_success_failure_cancellation_isolation")}}
        migration = {**authority("organization-administrator"), "repository_id": REPO_ID,
                     "organization": "FixtureOrg", "approved_tags": [TAG], "approved_shas": [],
                     "previously_shared_hosts": [{"id": "fixture-old-signer", "disposition": "quarantined"}],
                     "facts": {name: True for name in (
                         "organization_enterprise_restricted_workflows_capability_verified", "repository_ownership_and_registration_migrated",
                         "legacy_repository_default_protected_capacity_removed", "protected_secrets_removed_from_branch_visible_scopes",
                         "offline_owner_key_absent_from_actions", "previously_shared_signers_rebuilt_or_quarantined",
                         "credentials_reenabled_only_after_external_qualification", "workflow_script_lock_image_changes_codeowner_reviewed",
                         "modified_branch_cannot_schedule_protected_member", "dummy_environment_secret_denied_to_branch",
                         "environment_reviewers_authorized", "approved_exact_refs_reviewed")}}
        self.add("claims", None, {"controller": controller, "images": images,
                                 "state_separation": separation, "migration": migration}, "operator-claim")
        self.add("fixture-artifact", None, {"fixture": True, "description": "Synthetic fixture; not real authority or evidence."}, "operator-artifact")

    def add(self, identifier, endpoint, document, kind="api"):
        self.documents[identifier] = document
        self.entries[identifier] = {"id": identifier, "kind": kind, "file": identifier + ".json",
                                    "collected_at": COLLECTED, "complete": True,
                                    "origin": {"host": "api.github.com", "method": "GET", "endpoint": endpoint}
                                    if kind == "api" else {"kind": "operator"}}

    def write(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        entries = []
        for identifier, document in self.documents.items():
            entry = copy.deepcopy(self.entries[identifier])
            raw = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            with (root / entry["file"]).open("xb") as stream:
                stream.write(raw)
            entries.append(entry)
        path = root / "bundle.json"
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"schema_version": 1, "collected_at": COLLECTED,
                       "scope": self.scope, "exports": entries}, stream, sort_keys=True, indent=2)
        return path

    def enterprise_protected(self):
        self.scope["enterprise"] = "FixtureEnterprise"
        group = self.documents["org.groups"]["runner_groups"][1]
        group["inherited"] = True
        for suffix in ("", ".runners", ".repositories"):
            old = "org.group.2" + suffix
            entry = self.entries.pop(old)
            document = self.documents.pop(old)
            self.add("enterprise.group.2" + suffix, entry["origin"]["endpoint"].replace(
                "orgs/FixtureOrg", "enterprises/FixtureEnterprise"), document)
        enterprise_group = copy.deepcopy(self.documents["enterprise.group.2"])
        self.add("enterprise.groups", "enterprises/FixtureEnterprise/actions/runner-groups",
                 inventory("runner_groups", [enterprise_group]))
        self.add("enterprise.runners", "enterprises/FixtureEnterprise/actions/runners",
                 inventory("runners", copy.deepcopy(self.documents["enterprise.group.2.runners"]["runners"])))


class EvidenceBehaviorTests(unittest.TestCase):
    def invoke(self, fixture, raw_mutation=None, extra_args=()):
        with tempfile.TemporaryDirectory(prefix="chudware-ci-evidence-") as directory:
            bundle = fixture.write(directory)
            if raw_mutation:
                raw_mutation(Path(directory), bundle)
            before = {path.name: path.read_bytes() for path in Path(directory).iterdir() if path.is_file()}
            environment = dict(os.environ)
            environment["GH_TOKEN"] = "fixture-ambient-credential-must-not-be-used-or-printed"
            environment["GITHUB_TOKEN"] = environment["GH_TOKEN"]
            result = subprocess.run([sys.executable, "-B", str(Path(collector.__file__)),
                                     "--bundle", str(bundle), "--repo", REPOSITORY, "--repo-id", str(REPO_ID),
                                     "--approved-tag", TAG, *extra_args], capture_output=True, text=True,
                                    timeout=30, env=environment)
            after = {path.name: path.read_bytes() for path in Path(directory).iterdir() if path.is_file()}
            self.assertEqual(before, after, "collector must never alter export fixture bytes")
            self.assertNotIn(environment["GH_TOKEN"], result.stdout + result.stderr)
            self.assertLess(len(result.stdout.encode()), collector.MAX_OUTPUT_BYTES + 1)
            self.assertEqual(result.stderr, "")
            return result.returncode, json.loads(result.stdout)

    def gate(self, fixture, identifier):
        code, result = self.invoke(fixture)
        self.assertEqual(code, 1)
        self.assertFalse(result["release_approved"])
        return next(gate for gate in result["gates"] if gate["id"] == identifier)

    def test_correct_exports_accept_policy_but_never_authenticate_or_approve(self):
        code, result = self.invoke(Fixture())
        self.assertEqual(code, 1)
        self.assertTrue(result["api_export_policy_matches"])
        self.assertTrue(result["operator_claims_complete"])
        self.assertEqual(set(result["open_gates"]), {"authenticated-api-export", "external-controller-image-authority", "signed-release-approval"})
        groups = result["observed_export_facts_not_live_attestation"]["runner_groups"]
        self.assertEqual([row["selected_repository_ids"] for row in groups], [[REPO_ID], [REPO_ID]])
        refs = groups[1]["selected_workflows"]
        self.assertIn(REPOSITORY + "/.github/workflows/release-prepare.yml@refs/tags/" + TAG, refs)
        self.assertIn(REPOSITORY + "/.github/workflows/stripe-sandbox.yml@refs/tags/" + TAG, refs)
        self.assertNotIn(REPOSITORY + "/.github/workflows/release.yml@refs/tags/" + TAG, refs)
        self.assertTrue(all("sha256" in item and "inode" in item for item in result["file_identities"]))

    def test_enterprise_inherited_group_is_assessed_at_its_authority(self):
        fixture = Fixture()
        fixture.enterprise_protected()
        code, result = self.invoke(fixture)
        self.assertEqual(code, 1)
        self.assertTrue(result["api_export_policy_matches"])
        self.assertTrue(result["operator_claims_complete"])

    def test_wildcard_wrong_caller_branch_and_missing_nested_refs_are_refused(self):
        for replacement in ("refs/tags/v*", "refs/heads/feature", "refs/tags/v9.9.9"):
            with self.subTest(replacement=replacement):
                fixture = Fixture()
                refs = fixture.documents["org.group.2"]["selected_workflows"]
                refs[0] = refs[0].split("@")[0] + "@" + replacement
                self.assertEqual(self.gate(fixture, "runner-groups-and-defining-refs")["status"], "blocked")
        fixture = Fixture()
        refs = fixture.documents["org.group.2"]["selected_workflows"]
        refs[:] = [ref.replace("release-prepare.yml", "release.yml") for ref in refs]
        self.assertEqual(self.gate(fixture, "runner-groups-and-defining-refs")["status"], "blocked")
        fixture = Fixture()
        refs = fixture.documents["org.group.2"]["selected_workflows"]
        refs[:] = [ref for ref in refs if not ref.endswith("stripe-sandbox.yml@refs/tags/" + TAG)]
        self.assertEqual(self.gate(fixture, "runner-groups-and-defining-refs")["status"], "blocked")

    def test_overlap_selected_repo_and_unrestricted_groups_are_refused(self):
        for mutate in (
            lambda f: f.documents["org.group.2.runners"]["runners"][0].update(id=10),
            lambda f: f.documents["org.group.2.repositories"]["repositories"][0].update(id=999),
            lambda f: f.documents["org.group.2"].update(restricted_to_workflows=False),
            lambda f: f.documents["org.group.2"].update(default=True),
        ):
            fixture = Fixture()
            mutate(fixture)
            self.assertEqual(self.gate(fixture, "runner-groups-and-defining-refs")["status"], "blocked")

    def test_repository_and_organization_branch_visible_secrets_are_refused(self):
        fixture = Fixture()
        fixture.documents["repository.secrets"] = inventory("secrets", [{"name": "CHUDWARE_RELEASE_CI_KEY"}])
        self.assertEqual(self.gate(fixture, "environments-and-secret-scopes")["status"], "blocked")
        for visibility in ("all", "private", "selected"):
            fixture = Fixture()
            fixture.documents["organization.secrets"] = inventory("secrets", [{"name": "R2_SECRET_ACCESS_KEY", "visibility": visibility}])
            if visibility == "selected":
                fixture.add("organization.secret.R2_SECRET_ACCESS_KEY.repositories",
                            "orgs/FixtureOrg/actions/secrets/R2_SECRET_ACCESS_KEY/repositories",
                            inventory("repositories", [{"id": REPO_ID}]))
            self.assertEqual(self.gate(fixture, "environments-and-secret-scopes")["status"], "blocked")

    def test_protected_secret_in_wrong_environment_is_refused(self):
        fixture = Fixture()
        document = fixture.documents["environment.chudware-control-protected.secrets"]
        document["secrets"].append({"name": "R2_SECRET_ACCESS_KEY"})
        document["total_count"] += 1
        self.assertEqual(self.gate(fixture, "environments-and-secret-scopes")["status"], "blocked")

    def test_legacy_repository_and_default_group_capacity_are_refused(self):
        fixture = Fixture()
        fixture.documents["repository.runners"] = inventory("runners", [{"id": 999}])
        self.assertEqual(self.gate(fixture, "runner-groups-and-defining-refs")["status"], "blocked")
        fixture = Fixture()
        group = {"id": 3, "name": "Default", "default": True, "inherited": False, "visibility": "all"}
        fixture.documents["org.groups"]["runner_groups"].append(group)
        fixture.documents["org.groups"]["total_count"] += 1
        fixture.documents["org.runners"]["runners"].append({"id": 999})
        fixture.documents["org.runners"]["total_count"] += 1
        fixture.add("org.group.3", "orgs/FixtureOrg/actions/runner-groups/3", group)
        fixture.add("org.group.3.runners", "orgs/FixtureOrg/actions/runner-groups/3/runners",
                    inventory("runners", [{"id": 999}]))
        fixture.add("org.group.3.repositories", "orgs/FixtureOrg/actions/runner-groups/3/repositories",
                    inventory("repositories", []))
        self.assertEqual(self.gate(fixture, "runner-groups-and-defining-refs")["status"], "blocked")

    def test_missing_external_destruction_and_image_authorities_are_refused(self):
        for record, gate in (("controller", "ephemeral-destruction-evidence"),
                             ("images", "image-and-state-separation-evidence"),
                             ("state_separation", "image-and-state-separation-evidence"),
                             ("migration", "migration-and-negative-routing-evidence")):
            fixture = Fixture()
            item = fixture.documents["claims"][record]
            if isinstance(item, list):
                item = item[0]
            item.pop("authority")
            self.assertEqual(self.gate(fixture, gate)["status"], "blocked")
        fixture = Fixture()
        fixture.documents["claims"]["controller"]["facts"]["complete_vm_os_disk_deleted"] = False
        self.assertEqual(self.gate(fixture, "ephemeral-destruction-evidence")["status"], "blocked")

    def test_missing_images_controller_artifacts_and_mutable_state_are_refused(self):
        fixture = Fixture()
        fixture.documents["claims"]["images"] = []
        self.assertEqual(self.gate(fixture, "image-and-state-separation-evidence")["status"], "blocked")
        fixture = Fixture()
        fixture.documents["claims"]["controller"]["artifacts"] = []
        self.assertEqual(self.gate(fixture, "ephemeral-destruction-evidence")["status"], "blocked")
        fixture = Fixture()
        fixture.documents["claims"]["state_separation"]["facts"]["no_shared_interpreter_venv_site_packages_pth"] = False
        self.assertEqual(self.gate(fixture, "image-and-state-separation-evidence")["status"], "blocked")
        fixture = Fixture()
        fixture.documents["claims"]["migration"]["facts"].pop("protected_secrets_removed_from_branch_visible_scopes")
        self.assertEqual(self.gate(fixture, "migration-and-negative-routing-evidence")["status"], "blocked")

    def test_ref_rules_and_environment_bypasses_fail_closed(self):
        fixture = Fixture()
        fixture.documents["ruleset.101"]["rules"] = [{"type": "deletion"}]
        self.assertEqual(self.gate(fixture, "protected-branch-tag-rules")["status"], "blocked")
        fixture = Fixture()
        fixture.documents["ruleset.100"]["bypass_actors"] = [{"actor_id": 1, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
        self.assertEqual(self.gate(fixture, "protected-branch-tag-rules")["status"], "blocked")
        fixture = Fixture()
        fixture.documents["environment.chudware-release-protected"]["can_admins_bypass"] = True
        self.assertEqual(self.gate(fixture, "environments-and-secret-scopes")["status"], "blocked")
        fixture = Fixture()
        fixture.documents["environment.chudware-release-protected.branches"]["branch_policies"][1]["name"] = "v*"
        self.assertEqual(self.gate(fixture, "environments-and-secret-scopes")["status"], "blocked")

    def test_annotated_tag_resolution_binds_actual_commit(self):
        fixture = Fixture()
        sha = hashlib.sha1(b"synthetic annotated tag").hexdigest()
        fixture.documents["ref.tag." + TAG]["object"] = {"type": "tag", "sha": sha}
        fixture.add("tag-object." + sha, "repos/" + REPOSITORY + "/git/tags/" + sha,
                    {"sha": sha, "object": {"type": "commit", "sha": COMMIT}})
        code, result = self.invoke(fixture)
        self.assertEqual(code, 1)
        self.assertTrue(result["api_export_policy_matches"])
        tag = next(row for row in result["observed_export_facts_not_live_attestation"]["protected_refs"] if row["ref"] == "refs/tags/" + TAG)
        self.assertEqual(tag["tag_objects"], [sha])
        self.assertEqual(tag["commit"], COMMIT)

    def test_missing_types_origins_digests_and_secret_values_are_refused(self):
        fixture = Fixture()
        fixture.documents["org.group.2"]["selected_workflows"] = None
        self.assertEqual(self.gate(fixture, "runner-groups-and-defining-refs")["status"], "blocked")
        fixture = Fixture()
        fixture.documents["environment.chudware-release-protected"]["deployment_branch_policy"] = None
        self.assertEqual(self.gate(fixture, "environments-and-secret-scopes")["status"], "blocked")
        fixture = Fixture()
        fixture.entries["repository"]["origin"]["endpoint"] = "repos/Other/Repository"
        self.assertEqual(self.gate(fixture, "repository-organization-plan")["status"], "blocked")
        fixture = Fixture()
        fixture.documents["repository"]["secret_value"] = "fixture-secret-never-emit"
        code, result = self.invoke(fixture)
        self.assertEqual(code, 2)
        self.assertNotIn("fixture-secret-never-emit", json.dumps(result))
        fixture = Fixture()
        code, result = self.invoke(fixture, lambda root, _: (root / "repository.json").write_text("{}", encoding="utf-8"))
        self.assertEqual(code, 2)
        self.assertIn("digest mismatch", result["reason"])

    def test_claimed_authentication_never_closes_authority_gates(self):
        fixture = Fixture()
        fixture.documents["claims"]["authenticated"] = True
        fixture.documents["claims"]["signed_approval"] = {"verified": True, "signature": "fixture-not-a-signature"}
        code, result = self.invoke(fixture)
        self.assertEqual(code, 1)
        self.assertFalse(result["release_approved"])
        self.assertEqual(set(result["open_gates"]), {"authenticated-api-export", "external-controller-image-authority", "signed-release-approval"})
        self.assertNotIn("fixture-not-a-signature", json.dumps(result))

    def test_traversal_symlinks_oversized_and_incomplete_exports_are_refused(self):
        fixture = Fixture()
        fixture.entries["repository"]["complete"] = False
        self.assertEqual(self.invoke(fixture)[0], 2)
        fixture = Fixture()
        def traversal(root, path):
            manifest = json.loads(path.read_text())
            manifest["exports"][0]["file"] = "../repository.json"
            path.write_text(json.dumps(manifest))
        self.assertEqual(self.invoke(fixture, traversal)[0], 2)
        fixture = Fixture()
        def symlink(root, path):
            target = root / "repository.json"
            target.unlink()
            target.symlink_to("organization.json")
        self.assertEqual(self.invoke(fixture, symlink)[0], 2)
        fixture = Fixture()
        self.assertEqual(self.invoke(fixture, lambda root, _: (root / "repository.json").write_bytes(
            b" " * (collector.MAX_FILE_BYTES + 1)))[0], 2)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--write-fixture":
        parser = argparse.ArgumentParser(description="Write synthetic offline inputs, not authenticated evidence")
        parser.add_argument("--write-fixture", type=Path, required=True)
        arguments = parser.parse_args()
        print(Fixture().write(arguments.write_fixture))
    else:
        unittest.main()
