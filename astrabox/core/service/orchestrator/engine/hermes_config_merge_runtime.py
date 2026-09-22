#!/usr/bin/env python3
"""Generic Hermes profile config.yaml merger.

Reads up to two env JSON blobs and deep-merges them into
``$HERMES_HOME/config.yaml``:

- ``ASTRABOX_HERMES_CONFIG_OVERWRITE`` - platform-owned, force-overwrite at
  every nested key. Lists are atomic leaves (replaced wholesale).
- ``ASTRABOX_HERMES_CONFIG_DEFAULTS``  - user-overridable, setdefault at every
  nested key. Already-present keys are not touched.
- ``ASTRABOX_HERMES_SKILL_SOURCE_DIRS`` - platform-owned shared skill source
  directories. Skills are copied into ``$HERMES_HOME/skills`` only when the
  user's copy is missing; existing user copies are never overwritten.
- ``ASTRABOX_HERMES_SOUL_B64`` - platform-owned UTF-8 ``SOUL.md`` content,
  base64-encoded to preserve multiline markdown through shell env plumbing.
- ``ASTRABOX_HERMES_CRON_JOBS_B64`` - platform-owned Hermes Cron jobs,
  base64-encoded JSON array, reconciled into ``$HERMES_HOME/cron/jobs.json``.

All envs are optional. The script is the single contract between platform and
the Hermes runtime image: adding profile config/materialization fields should
only require platform-side payload changes.

Exit codes: 0 OK, 65 EX_DATAERR (PyYAML missing, malformed env JSON, etc).
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

_ENV_SOURCES: tuple[tuple[str, str], ...] = (
    ("ASTRABOX_HERMES_CONFIG_OVERWRITE", "overwrite"),
    ("ASTRABOX_HERMES_CONFIG_DEFAULTS", "setdefault"),
)
_SKILL_SOURCE_DIRS_ENV = "ASTRABOX_HERMES_SKILL_SOURCE_DIRS"
_SOUL_B64_ENV = "ASTRABOX_HERMES_SOUL_B64"
_CRON_JOBS_B64_ENV = "ASTRABOX_HERMES_CRON_JOBS_B64"
_MANAGED_CRON_IDS_FILE = "astrabox-managed-jobs.json"
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_CRON_EXPR_RE = re.compile(r"^[0-9A-Za-z*/,#?\-]+$")


def deep_merge(target: dict[str, Any], source: dict[str, Any], mode: str) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            deep_merge(existing, value, mode)
            continue
        if mode == "overwrite":
            target[key] = value
        elif mode == "setdefault":
            target.setdefault(key, value)
        else:
            raise ValueError(f"unknown merge mode: {mode!r}")


def _coerce_string_list(value: Any, *, source: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{source} must be a JSON array")
    return [str(item).strip() for item in value if str(item).strip()]


def materialize_platform_skills(home: Path) -> bool:
    raw = (os.environ.get(_SKILL_SOURCE_DIRS_ENV) or "").strip()
    if not raw:
        return False
    try:
        source_dirs = _coerce_string_list(json.loads(raw), source=_SKILL_SOURCE_DIRS_ENV)
    except (json.JSONDecodeError, TypeError) as exc:
        sys.stderr.write(f"{_SKILL_SOURCE_DIRS_ENV} invalid: {exc}\n")
        raise TypeError(str(exc)) from exc
    if not source_dirs:
        return False

    dest_root = home / "skills"
    copied = 0
    for source_dir in source_dirs:
        source_root = Path(source_dir).expanduser()
        if not source_root.is_dir():
            continue
        for skill_file in sorted(source_root.rglob("SKILL.md")):
            skill_dir = skill_file.parent
            rel = (
                Path(source_root.name)
                if skill_dir == source_root
                else skill_dir.relative_to(source_root)
            )
            if not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
                continue
            dest = dest_root / rel
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill_dir, dest)
            copied += 1

    if copied:
        print(f"ASTRABOX_HERMES_SKILLS_READY copied={copied}")
    return copied > 0


def materialize_soul(home: Path) -> bool:
    raw = os.environ.get(_SOUL_B64_ENV)
    if raw is None:
        return False
    try:
        content = base64.b64decode(raw.encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"{_SOUL_B64_ENV} invalid: {exc}\n")
        raise TypeError(str(exc)) from exc
    soul_path = home / "SOUL.md"
    current = soul_path.read_text(encoding="utf-8") if soul_path.exists() else None
    if current == content:
        return False
    soul_path.write_text(content, encoding="utf-8")
    soul_path.chmod(0o600)
    print("ASTRABOX_HERMES_SOUL_READY")
    return True


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json_file(path: Path, *, expected: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TypeError(f"{expected} is not valid JSON: {exc}") from exc


def _write_json_file(path: Path, payload: Any, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.chmod(mode)
    os.replace(tmp_path, path)
    path.chmod(mode)


def _duration_to_minutes(raw: str, *, source: str) -> int:
    match = _DURATION_RE.match(raw)
    if not match:
        raise TypeError(f"{source} duration must look like 15m, 2h, 1d, or 1w")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount <= 0:
        raise TypeError(f"{source} duration must be positive")
    factors = {"s": 1 / 60, "m": 1, "h": 60, "d": 1440, "w": 10080}
    minutes = int(amount * factors[unit])
    return max(minutes, 1)


def _normalize_cron_schedule(raw: Any, *, source: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        kind = str(raw.get("kind") or "").strip()
        if kind == "cron":
            expr = str(raw.get("expr") or "").strip()
            if not expr:
                raise TypeError(f"{source}.expr is required for cron schedule")
            display = str(raw.get("display") or expr).strip() or expr
            return {"kind": "cron", "expr": expr, "display": display}
        if kind == "interval":
            minutes_raw = raw.get("minutes")
            try:
                minutes = int(minutes_raw)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"{source}.minutes must be a positive integer") from exc
            if minutes <= 0:
                raise TypeError(f"{source}.minutes must be a positive integer")
            display = str(raw.get("display") or f"every {minutes}m").strip()
            return {"kind": "interval", "minutes": minutes, "display": display}
        if kind == "once":
            run_at = str(raw.get("run_at") or "").strip()
            if not run_at:
                raise TypeError(f"{source}.run_at is required for once schedule")
            display = str(raw.get("display") or run_at).strip()
            return {"kind": "once", "run_at": run_at, "display": display}
        raise TypeError(f"{source}.kind must be cron, interval, or once")

    if not isinstance(raw, str):
        raise TypeError(f"{source} must be a string or object")
    value = raw.strip()
    if not value:
        raise TypeError(f"{source} must not be empty")
    lower = value.lower()
    if lower.startswith("every "):
        minutes = _duration_to_minutes(value[6:].strip(), source=source)
        return {"kind": "interval", "minutes": minutes, "display": value}
    parts = value.split()
    if len(parts) in {5, 6} and all(_CRON_EXPR_RE.fullmatch(part) for part in parts):
        return {"kind": "cron", "expr": value, "display": value}
    try:
        run_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        run_at = None
    if run_at is not None:
        return {"kind": "once", "run_at": run_at.isoformat(), "display": value}
    minutes = _duration_to_minutes(value, source=source)
    run_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return {"kind": "once", "run_at": run_at.isoformat(), "display": value}


def _required_string(job: dict[str, Any], key: str, *, source: str) -> str:
    value = job.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{source}.{key} must be a non-empty string")
    return value.strip()


def _optional_string_list(value: Any, *, source: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        item = value.strip()
        return [item] if item else []
    if isinstance(value, list):
        result: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise TypeError(f"{source}[{index}] must be a string")
            stripped = item.strip()
            if stripped:
                result.append(stripped)
        return result
    raise TypeError(f"{source} must be a string or array of strings")


def _normalize_repeat(raw: Any, *, schedule: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    existing_repeat = existing.get("repeat")
    completed = 0
    if isinstance(existing_repeat, dict):
        try:
            completed = max(int(existing_repeat.get("completed") or 0), 0)
        except (TypeError, ValueError):
            completed = 0
    if raw is None:
        times = 1 if schedule.get("kind") == "once" else None
    elif isinstance(raw, int):
        if raw <= 0:
            raise TypeError("repeat must be positive when provided")
        times = raw
    elif isinstance(raw, dict):
        value = raw.get("times")
        if value is None:
            times = None
        else:
            try:
                times = int(value)
            except (TypeError, ValueError) as exc:
                raise TypeError("repeat.times must be a positive integer") from exc
            if times <= 0:
                raise TypeError("repeat.times must be a positive integer")
    else:
        raise TypeError("repeat must be a positive integer or object")
    return {"times": times, "completed": completed}


def _optional_origin(value: Any, *, source: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f"{source} must be an object")
    return value


def _normalize_template_cron_job(
    raw: Any,
    *,
    source: str,
    existing: dict[str, Any] | None,
    now: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError(f"{source} must be an object")
    job_id = _required_string(raw, "id", source=source)
    if not _JOB_ID_RE.fullmatch(job_id):
        raise TypeError(
            f"{source}.id may only contain letters, numbers, dot, underscore, colon, or dash"
        )
    no_agent = bool(raw.get("no_agent", False))
    if "no_agent" in raw and not isinstance(raw.get("no_agent"), bool):
        raise TypeError(f"{source}.no_agent must be a boolean")
    if no_agent:
        prompt = str(raw.get("prompt") or "").strip()
        script = _required_string(raw, "script", source=source)
    else:
        prompt = _required_string(raw, "prompt", source=source)
        script = str(raw.get("script") or "").strip()
    name = str(raw.get("name") or raw.get("description") or job_id).strip() or job_id
    schedule = _normalize_cron_schedule(raw.get("schedule"), source=f"{source}.schedule")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TypeError(f"{source}.enabled must be a boolean")
    existing = existing or {}
    schedule_changed = existing.get("schedule") != schedule
    repeat = _normalize_repeat(raw.get("repeat"), schedule=schedule, existing=existing)
    job: dict[str, Any] = {
        "id": job_id,
        "name": name,
        "prompt": prompt,
        "skills": _optional_string_list(raw.get("skills"), source=f"{source}.skills"),
        "skill": str(raw.get("skill") or "").strip(),
        "model": str(raw.get("model") or "").strip(),
        "provider": str(raw.get("provider") or "").strip(),
        "base_url": str(raw.get("base_url") or "").strip(),
        "script": script,
        "no_agent": no_agent,
        "context_from": _optional_string_list(
            raw.get("context_from"),
            source=f"{source}.context_from",
        ),
        "schedule": schedule,
        "schedule_display": str(schedule.get("display") or "").strip(),
        "repeat": repeat,
        "enabled": enabled,
        "state": "scheduled" if enabled else "paused",
        "paused_at": None if enabled else existing.get("paused_at"),
        "paused_reason": (
            None
            if enabled
            else str(existing.get("paused_reason") or "template disabled")
        ),
        "created_at": str(existing.get("created_at") or now),
        "next_run_at": None if schedule_changed else existing.get("next_run_at"),
        "last_run_at": existing.get("last_run_at"),
        "last_status": existing.get("last_status"),
        "last_error": existing.get("last_error"),
        "last_delivery_error": existing.get("last_delivery_error"),
        "deliver": str(raw.get("deliver") or "local").strip() or "local",
        "origin": _optional_origin(raw.get("origin"), source=f"{source}.origin"),
        "enabled_toolsets": _optional_string_list(
            raw.get("enabled_toolsets"),
            source=f"{source}.enabled_toolsets",
        ),
        "workdir": str(raw.get("workdir") or "").strip(),
        "profile": str(raw.get("profile") or "").strip(),
    }
    return job


def materialize_cron_jobs(home: Path) -> bool:
    raw = os.environ.get(_CRON_JOBS_B64_ENV)
    if raw is None:
        return False
    try:
        payload = json.loads(
            base64.b64decode(raw.encode("ascii"), validate=True).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"{_CRON_JOBS_B64_ENV} invalid: {exc}\n")
        raise TypeError(str(exc)) from exc
    if not isinstance(payload, list):
        exc = TypeError(f"{_CRON_JOBS_B64_ENV} top-level must be a JSON array")
        sys.stderr.write(f"{_CRON_JOBS_B64_ENV} invalid: {exc}\n")
        raise exc

    cron_dir = home / "cron"
    jobs_path = cron_dir / "jobs.json"
    managed_path = cron_dir / _MANAGED_CRON_IDS_FILE
    if jobs_path.exists():
        existing_data = _read_json_file(jobs_path, expected="Hermes cron/jobs.json")
        if not isinstance(existing_data, dict) or not isinstance(existing_data.get("jobs"), list):
            raise TypeError("Hermes cron/jobs.json must contain a jobs array")
        existing_jobs = existing_data["jobs"]
    else:
        existing_jobs = []
    if managed_path.exists():
        managed_data = _read_json_file(managed_path, expected=_MANAGED_CRON_IDS_FILE)
        if not isinstance(managed_data, dict):
            raise TypeError(f"{_MANAGED_CRON_IDS_FILE} must be a JSON object")
        previous_managed_ids = set(
            _coerce_string_list(
                managed_data.get("ids"),
                source=_MANAGED_CRON_IDS_FILE,
            )
        )
    else:
        previous_managed_ids = set()

    existing_by_id: dict[str, dict[str, Any]] = {}
    user_owned_ids: set[str] = set()
    for job in existing_jobs:
        if not isinstance(job, dict):
            raise TypeError("Hermes cron/jobs.json jobs entries must be objects")
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            continue
        existing_by_id[job_id] = job
        if job_id not in previous_managed_ids:
            user_owned_ids.add(job_id)

    now = _utc_now_iso()
    normalized_jobs: list[dict[str, Any]] = []
    incoming_ids: set[str] = set()
    for index, item in enumerate(payload):
        existing_job = None
        if isinstance(item, dict):
            existing_job = existing_by_id.get(str(item.get("id") or "").strip())
        job = _normalize_template_cron_job(
            item,
            source=f"{_CRON_JOBS_B64_ENV}[{index}]",
            existing=existing_job,
            now=now,
        )
        job_id = job["id"]
        if job_id in incoming_ids:
            raise TypeError(f"{_CRON_JOBS_B64_ENV} duplicate job id {job_id!r}")
        if job_id in user_owned_ids:
            raise TypeError(
                f"{_CRON_JOBS_B64_ENV} job id {job_id!r} already exists outside "
                "platform management"
            )
        incoming_ids.add(job_id)
        normalized_jobs.append(job)

    preserved_jobs = [
        job
        for job in existing_jobs
        if isinstance(job, dict)
        and str(job.get("id") or "").strip() not in previous_managed_ids
        and str(job.get("id") or "").strip() not in incoming_ids
    ]
    next_jobs = preserved_jobs + normalized_jobs
    next_managed_ids = sorted(incoming_ids)
    if existing_jobs == next_jobs and sorted(previous_managed_ids) == next_managed_ids:
        return False

    cron_dir.mkdir(parents=True, exist_ok=True)
    cron_dir.chmod(0o700)
    _write_json_file(jobs_path, {"jobs": next_jobs}, mode=0o600)
    _write_json_file(
        managed_path,
        {"ids": next_managed_ids, "updated_at": now},
        mode=0o600,
    )
    removed = len(previous_managed_ids - incoming_ids)
    print(f"ASTRABOX_HERMES_CRON_JOBS_READY jobs={len(normalized_jobs)} removed={removed}")
    return True


def main() -> int:
    try:
        import yaml
    except ImportError as exc:
        sys.stderr.write(f"PyYAML required to write Hermes config: {exc}\n")
        return 65

    home = Path(os.environ.get("HERMES_HOME") or "/root/.hermes")
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    cfg_path = home / "config.yaml"

    if cfg_path.exists():
        try:
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            sys.stderr.write(f"Hermes config.yaml parse failed: {exc}\n")
            return 65
        cfg: dict[str, Any] = {} if loaded is None else loaded
    else:
        cfg = {}

    if not isinstance(cfg, dict):
        sys.stderr.write("Hermes config.yaml top-level must be a YAML mapping\n")
        return 65

    applied_config = False
    for env_name, mode in _ENV_SOURCES:
        raw = (os.environ.get(env_name) or "").strip()
        if not raw:
            continue
        try:
            patch = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"{env_name} not valid JSON: {exc}\n")
            return 65
        if not isinstance(patch, dict):
            sys.stderr.write(f"{env_name} top-level must be a JSON object\n")
            return 65
        deep_merge(cfg, patch, mode)
        applied_config = True

    try:
        materialized_skills = materialize_platform_skills(home)
    except TypeError:
        return 65
    try:
        materialized_soul = materialize_soul(home)
    except TypeError:
        return 65
    try:
        materialized_cron = materialize_cron_jobs(home)
    except TypeError as exc:
        if _CRON_JOBS_B64_ENV not in str(exc):
            sys.stderr.write(f"{_CRON_JOBS_B64_ENV} invalid: {exc}\n")
        return 65

    if not applied_config and not cfg_path.exists():
        if materialized_skills or materialized_soul or materialized_cron:
            return 0
        print("ASTRABOX_HERMES_CONFIG_NOOP")
        return 0

    cfg_path.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    cfg_path.chmod(0o600)
    print("ASTRABOX_HERMES_CONFIG_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
