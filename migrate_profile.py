"""Copy an existing ARGUS profile into a freshly extracted sibling release."""
from __future__ import annotations

import argparse
from contextlib import closing, ExitStack
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from dotenv import dotenv_values, set_key


def _new_file(path: Path):
    fd = os.open(path, os.O_EXCL | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    return os.fdopen(fd, "wb")


def migrate(source: Path, destination: Path) -> Path:
    source = source.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=True)
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("The old and new releases need separate, non-nested directories.")
    old_env = source / ".env"
    if not destination.is_dir() or not old_env.is_file():
        raise ValueError("Expected an existing release directory and an old installation containing .env.")
    settings = dotenv_values(old_env, interpolate=False)

    def stored_path(key, fallback):
        value = os.environ.get(key) or settings.get(key) or fallback
        return (source / Path(value).expanduser()).resolve()

    profile = stored_path("ARGUS_DB_PATH", "argus_user_profile.db")
    material = stored_path("ARGUS_DATA_DIR", "data")
    if not profile.is_file():
        raise FileNotFoundError(f"No profile database at {profile}. Check the old ARGUS_DB_PATH.")
    if material.exists() and not material.is_dir():
        raise ValueError(f"The configured material library is not a directory: {material}")
    # Keep customized paths even when an older installation hasn't created its library yet.
    if not material.exists() and not (settings.get("ARGUS_DATA_DIR") or os.environ.get("ARGUS_DATA_DIR")):
        material = destination / "data"
    result_db = destination / "argus_user_profile.db"
    result_env = destination / ".env"
    result_skills = destination / "custom_skills"
    old_skills = source / "custom_skills"
    reserved = (result_db, result_env, result_skills, destination / "data",
                *(result_db.with_name(result_db.name + suffix) for suffix in ("-wal", "-shm", "-journal")))
    lock = destination / ".argus-profile-migration.lock"
    with ExitStack() as lifetime:
        lock.mkdir(mode=0o700)
        lifetime.callback(lock.rmdir)
        collision = next((p for p in reserved if os.path.lexists(p)), None)
        if collision is not None:
            raise FileExistsError(f"{collision.name} already exists. Extract the release into a fresh directory.")
        scratch = Path(lifetime.enter_context(tempfile.TemporaryDirectory(dir=destination, prefix=".profile-")))
        staged_db = scratch / "profile.db"
        with closing(sqlite3.connect(profile.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(staged_db)) as snapshot:
                src.backup(snapshot)
                if snapshot.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("SQLite integrity check failed.")
        staged_env = scratch / ".env"
        shutil.copyfile(old_env, staged_env)
        staged_env.chmod(0o600)
        set_key(str(staged_env), "ARGUS_DB_PATH", str(result_db))
        set_key(str(staged_env), "ARGUS_DATA_DIR", str(material))

        # Commit the configuration last. An error unwinds only the files and
        # directory exclusively claimed by this run.
        with ExitStack() as rollback:
            if old_skills.is_dir():
                result_skills.mkdir(mode=0o700)
                rollback.callback(shutil.rmtree, result_skills)
                shutil.copytree(old_skills, result_skills, dirs_exist_ok=True, symlinks=True)
            for original, target in ((staged_db, result_db), (staged_env, result_env)):
                with _new_file(target) as output:
                    rollback.callback(target.unlink, missing_ok=True)
                    with original.open("rb") as stream:
                        shutil.copyfileobj(stream, output)
                    output.flush()
                    os.fsync(output.fileno())
            rollback.pop_all()
    return result_db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="?", default=Path.home() / "argus")
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parent,
                        help="Fresh release directory (default: the directory containing this script)")
    args = parser.parse_args()
    try:
        result = migrate(args.source, args.destination)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"Migration failed: {exc}\n")
    print(f"Copied and verified the profile at {result}. Previous configuration and database retained.")


if __name__ == "__main__":
    main()
