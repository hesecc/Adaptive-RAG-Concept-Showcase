"""Saved evaluation reports: one JSON file per run in <data_dir>/evaluations."""

from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile
from typing import Any
from uuid import uuid4

from .domain import ValidationError

REPORT_KINDS = ("retrieval", "permissions")
# The report ID doubles as the file name, so only this exact shape is ever opened.
_REPORT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-(?:retrieval|permissions)-[0-9a-f]{8}")
# A listing reads at most this many of the newest files, so a long history cannot slow the page.
_SCAN_LIMIT = 500


def _tenant(report: dict[str, Any]) -> str | None:
    parameters = report.get("parameters")
    return parameters.get("tenant_id") if isinstance(parameters, dict) else None


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """The few fields a report list needs, without the per-case or per-cell detail."""
    parameters = report.get("parameters") if isinstance(report.get("parameters"), dict) else {}
    summary: dict[str, Any] = {
        "report_id": report.get("report_id"), "kind": report.get("kind"),
        "created_at": report.get("created_at"), "tenant_id": parameters.get("tenant_id"),
        "model_calls": report.get("model_calls", 0),
    }
    if report.get("kind") == "retrieval":
        fixture = parameters.get("fixture_path")
        summary.update({
            "case_count": report.get("case_count"), "positive_case_count": report.get("positive_case_count"),
            "top_k": parameters.get("top_k"), "data_domains": parameters.get("data_domains"),
            "fixture": PureWindowsPath(fixture).name if isinstance(fixture, str) and fixture else None,
            "metrics": report.get("metrics"), "latency_ms": report.get("latency_ms"),
        })
    elif report.get("kind") == "permissions":
        counts = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        summary.update({name: counts.get(name) for name in
                        ("cells", "leaks", "missing", "broad_checks", "broad_failures", "passed")})
    return summary


class ReportStore:
    """Plain files, so reports survive restarts and can be copied or archived like any other data."""

    def __init__(self, data_dir: str | Path):
        self.directory = Path(data_dir) / "evaluations"

    def save(self, report: dict[str, Any], kind: str) -> dict[str, Any]:
        """Assign an ID, write atomically, and return the report as stored."""
        if kind not in REPORT_KINDS:
            raise ValidationError("Unknown evaluation report kind")
        if not isinstance(_tenant(report), str) or not _tenant(report):
            raise ValidationError("An evaluation report needs parameters.tenant_id")
        report_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{kind}-{uuid4().hex[:8]}"
        saved = {**report, "report_id": report_id, "kind": kind}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=self.directory, prefix=".saving-", suffix=".tmp")
        except OSError as exc:
            raise ValidationError("Cannot write evaluation report") from exc
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
            os.replace(temporary, self.directory / f"{report_id}.json")
        except BaseException as exc:
            with suppress(OSError):
                os.unlink(temporary)
            if isinstance(exc, OSError):
                raise ValidationError("Cannot write evaluation report") from exc
            raise
        return saved

    def scratch(self) -> tempfile.TemporaryDirectory:
        """A throwaway directory beside the reports, so a read-only system temp directory cannot block a run."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            return tempfile.TemporaryDirectory(prefix=".scratch-", dir=self.directory, ignore_cleanup_errors=True)
        except OSError as exc:
            raise ValidationError("Cannot prepare the evaluation work directory") from exc

    def _names(self) -> list[str]:
        try:
            names = [path.stem for path in self.directory.glob("*.json") if _REPORT_ID.fullmatch(path.stem)]
        except OSError:
            return []
        return sorted(names, reverse=True)

    def _read(self, report_id: str) -> dict[str, Any] | None:
        try:
            value = json.loads((self.directory / f"{report_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        # The file name is the identity: a copied or renamed file must still open under the ID the list shows.
        return {**value, "report_id": report_id} if isinstance(value, dict) else None

    def list(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Summaries of this tenant's reports, newest first. Unreadable files are skipped."""
        found: list[dict[str, Any]] = []
        if limit <= 0:
            return found
        for report_id in self._names()[:_SCAN_LIMIT]:
            report = self._read(report_id)
            if report is not None and _tenant(report) == tenant_id:
                found.append(summarize(report))
                if len(found) >= limit:
                    break
        return found

    def get(self, tenant_id: str, report_id: str) -> dict[str, Any] | None:
        """The full report, or None when it is missing, malformed, or belongs to another tenant."""
        if not isinstance(report_id, str) or not _REPORT_ID.fullmatch(report_id):
            return None
        report = self._read(report_id)
        return report if report is not None and _tenant(report) == tenant_id else None
