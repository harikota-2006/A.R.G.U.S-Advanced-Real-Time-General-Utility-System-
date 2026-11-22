"""WAL-safe deployment migration, storage paths and failure rollback."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from dotenv import dotenv_values
import migrate_profile


class ProfileMigrationTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="argus-profile-test-")
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.old = root / "previous"
        self.new = root / "release"
        self.old.mkdir()
        self.new.mkdir()
        self.original = self.old / "profile.db"
        self.library = self.old / "library"
        self.library.mkdir()
        self.settings = self.old / ".env"
        self.settings.write_text("ARGUS_PIN_HASH='test$fixture$hash'\nARGUS_DB_PATH='profile.db'\n"
                                 "ARGUS_DATA_DIR='library'\n", encoding="utf8")
        self.src = sqlite3.connect(self.original)
        self.addCleanup(self.src.close)
        self.src.execute("PRAGMA journal_mode=WAL")
        self.src.execute("CREATE TABLE prefs (item TEXT)")
        self.src.execute("INSERT INTO prefs VALUES ('prefer SI units')")
        self.src.commit()
        skills = self.old / "custom_skills"
        skills.mkdir()
        (skills / "formula.py").write_text("def run(params):\n    return params['value']\n", encoding="utf8")
        self.env = patch.dict(os.environ, {"ARGUS_DB_PATH": "", "ARGUS_DATA_DIR": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_wal_snapshot_pin_skills_and_absolute_material_path(self):
        self.assertTrue(self.original.with_name("profile.db-wal").is_file())
        result = migrate_profile.migrate(self.old, self.new)
        with closing(sqlite3.connect(result)) as db:
            self.assertEqual(db.execute("SELECT item FROM prefs").fetchall(), [("prefer SI units",)])
        settings = dotenv_values(self.new / ".env", interpolate=False)
        self.assertEqual(settings["ARGUS_PIN_HASH"], "test$fixture$hash")
        self.assertEqual(Path(settings["ARGUS_DATA_DIR"]), self.library)
        self.assertEqual(Path(settings["ARGUS_DB_PATH"]), self.new / "argus_user_profile.db")
        self.assertEqual((self.old / "custom_skills/formula.py").read_bytes(),
                         (self.new / "custom_skills/formula.py").read_bytes())
        self.assertFalse((self.new / "data").exists())
        self.assertEqual(dotenv_values(self.settings)["ARGUS_DB_PATH"], "profile.db")
        with self.assertRaises(FileExistsError):
            migrate_profile.migrate(self.old, self.new)

    def test_exclusive_publication_failure_rolls_back_only_new_outputs(self):
        original_open = migrate_profile._new_file
        def external_claim(path):
            if path.name == ".env":
                path.write_text("concurrent edit", encoding="utf8")
            return original_open(path)
        with patch.object(migrate_profile, "_new_file", side_effect=external_claim):
            with self.assertRaises(FileExistsError):
                migrate_profile.migrate(self.old, self.new)
        self.assertEqual(list(self.new.iterdir()), [self.new / ".env"])
        self.assertEqual((self.new / ".env").read_text(), "concurrent edit")
        self.assertEqual(self.src.execute("SELECT item FROM prefs").fetchone(), ("prefer SI units",))

    def test_stale_sidecar_and_nested_destination_are_rejected(self):
        sidecar = self.new / "argus_user_profile.db-wal"
        sidecar.write_bytes(b"old WAL state")
        with self.assertRaisesRegex(FileExistsError, "argus_user_profile.db-wal"):
            migrate_profile.migrate(self.old, self.new)
        self.assertEqual(sidecar.read_bytes(), b"old WAL state")
        nested = self.old / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "non-nested"):
            migrate_profile.migrate(self.old, nested)
        self.assertFalse(list(nested.iterdir()))

    def test_missing_database_does_not_create_empty_profile(self):
        with patch.dict(os.environ, {"ARGUS_DB_PATH": str(self.old / "missing.db")}):
            with self.assertRaises(FileNotFoundError):
                migrate_profile.migrate(self.old, self.new)
        self.assertEqual(list(self.new.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
