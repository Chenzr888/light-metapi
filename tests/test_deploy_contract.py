import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
import json
from pathlib import Path

import app


ROOT = Path(__file__).resolve().parents[1]


class DeployContractTest(unittest.TestCase):
    def test_release_scripts_are_valid_shell(self):
        scripts = sorted((ROOT / "scripts").glob("*.sh"))
        self.assertGreaterEqual(len(scripts), 4)
        subprocess.run(["bash", "-n", *map(str, scripts)], check=True)

    def test_remote_deploy_has_required_safety_gates(self):
        script = (ROOT / "scripts" / "remote-deploy-cy16.sh").read_text()
        for required in (
            "flock -n",
            "create_backup",
            "pragma integrity_check",
            "run_canary",
            "127.0.0.1:18756",
            "--user 1000:1000",
            "restore_backup",
            "image revision label mismatch",
            "exact image artifact tested by GitHub CI",
            "protected endpoint returned",
            "APP_ROOT=/home/ubuntu/upstream-balance",
            "pragma journal_mode=delete",
            'for suffix in ("-wal", "-shm")',
            '--no-deps upstream-balance',
            'releases/$RELEASE_SHA/$ATTEMPT_ID',
        ):
            self.assertIn(required, script)
        for forbidden in ("docker compose down", "rsync --delete", ":latest"):
            self.assertNotIn(forbidden, script)

        rollback = (ROOT / "scripts" / "remote-rollback-cy16.sh").read_text()
        self.assertIn('--no-deps upstream-balance', rollback)
        launcher = (ROOT / "scripts" / "rollback-cy16.sh").read_text()
        self.assertIn('backups/$BACKUP_ID', launcher)
        deploy_launcher = (ROOT / "scripts" / "deploy-cy16.sh").read_text()
        self.assertIn("secrets.token_hex(16)", deploy_launcher)
        self.assertIn("mkdir '$RELEASE_DIR'", deploy_launcher)
        self.assertIn("download_ci_artifact", deploy_launcher)
        self.assertIn("--max-connection-per-server=16", deploy_launcher)
        self.assertNotIn("gh run download", deploy_launcher)

    def test_production_compose_is_bounded_and_local_only(self):
        for compose_file in ("docker-compose.cy16.yml", "docker-compose.rollback.yml"):
            compose = (ROOT / "deploy" / compose_file).read_text()
            for required in (
                "127.0.0.1:8756",
                "read_only: true",
                "cap_drop:",
                "no-new-privileges:true",
                "mem_limit: 512m",
                "pids_limit: 128",
                "restart: unless-stopped",
                'user: "1000:1000"',
                'SESSION_COOKIE_SECURE: "1"',
            ):
                self.assertIn(required, compose)
            self.assertNotIn("build:", compose)
            self.assertNotIn("latest", compose)

    def test_sensitive_import_files_are_excluded_from_git_and_build_context(self):
        for ignored_file in (ROOT / ".gitignore", ROOT / ".dockerignore"):
            content = ignored_file.read_text()
            self.assertIn("/config.json", content)

    def test_compose_policy_rejects_an_accidental_second_service(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            resolved = Path(temp_dir) / "resolved.json"
            resolved.write_text(json.dumps({
                "services": {"upstream-balance": {}, "accidental-worker": {}},
            }))
            result = subprocess.run(
                [
                    "python3",
                    str(ROOT / "scripts" / "validate-compose-policy.py"),
                    str(resolved),
                    "example.invalid/image:sha",
                    str(Path(temp_dir) / "data"),
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("service set must be exactly upstream-balance", result.stderr)

    def test_wal_snapshot_is_normalized_to_one_standalone_database_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.sqlite3"
            snapshot_path = root / "snapshot.sqlite3"
            source = sqlite3.connect(source_path)
            source.execute("pragma journal_mode=wal")
            source.execute("create table evidence(value text not null)")
            source.execute("insert into evidence values ('ready')")
            source.commit()
            target = sqlite3.connect(snapshot_path)
            source.backup(target)
            self.assertEqual(target.execute("pragma integrity_check").fetchone()[0], "ok")
            target.execute("pragma wal_checkpoint(truncate)")
            self.assertEqual(target.execute("pragma journal_mode=delete").fetchone()[0], "delete")
            target.close()
            source.close()
            for suffix in ("-wal", "-shm"):
                candidate = Path(str(snapshot_path) + suffix)
                if candidate.exists():
                    candidate.unlink()
            self.assertFalse(Path(str(snapshot_path) + "-wal").exists())
            self.assertFalse(Path(str(snapshot_path) + "-shm").exists())
            verify = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
            self.assertEqual(verify.execute("select value from evidence").fetchone()[0], "ready")
            verify.close()

    def test_image_inputs_are_immutable(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("FROM python:3.11-slim@sha256:", dockerfile)
        self.assertIn("pip install --no-cache-dir --require-hashes", dockerfile)
        self.assertIn("USER 1000:1000", dockerfile)

    def test_health_checks_database_and_permissions_are_private(self):
        response = app.app.test_client().get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["database"], "ready")
        self.assertEqual(stat.S_IMODE(os.stat(app.DATA_DIR).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(app.DB_PATH).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
