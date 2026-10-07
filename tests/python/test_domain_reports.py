import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from clipen.domain import (
    ValidationIssue,
    ValidationReport,
    ValidationSeverity,
    ValidationStatus,
)


class ValidationReportTests(unittest.TestCase):
    def issue(self, severity: ValidationSeverity) -> ValidationIssue:
        return ValidationIssue(
            "timeline.delivery.unsupported",
            severity,
            "/tracks/0/items/0",
            "這個元素尚未證實可用兩種方式交付。",
            "請移除元素，或選擇已有測試證據的實作。",
            item_id="clip-1",
            evidence_refs=("test-resolve-1",),
        )

    def report(self, *issues: ValidationIssue) -> ValidationReport:
        return ValidationReport(
            "report-1",
            "timeline-1",
            "desktop-macos",
            "a" * 64,
            issues,
        )

    def test_status_and_execution_gate_follow_the_highest_severity(self) -> None:
        passed = self.report()
        self.assertEqual(passed.status, ValidationStatus.PASS)
        self.assertTrue(passed.can_execute)

        warning = self.report(self.issue(ValidationSeverity.WARNING))
        self.assertEqual(warning.status, ValidationStatus.WARNING)
        self.assertFalse(warning.can_execute)

        blocking = self.report(self.issue(ValidationSeverity.BLOCKING))
        self.assertEqual(blocking.status, ValidationStatus.BLOCKING)
        self.assertFalse(blocking.can_execute)

    def test_report_snapshots_mutable_issue_inputs(self) -> None:
        issues = [self.issue(ValidationSeverity.WARNING)]
        report = self.report(*issues)
        issues.clear()
        self.assertEqual(len(report.issues), 1)

    def test_rejects_duplicate_findings_and_unsafe_locations(self) -> None:
        issue = self.issue(ValidationSeverity.BLOCKING)
        with self.assertRaisesRegex(ValueError, "unique locations"):
            self.report(issue, issue)
        with self.assertRaisesRegex(ValueError, "location"):
            ValidationIssue(
                "timeline.invalid",
                ValidationSeverity.BLOCKING,
                "tracks/0",
                "錯誤。",
                "請修正。",
            )

    def test_payload_matches_public_schema(self) -> None:
        report = self.report(self.issue(ValidationSeverity.WARNING))
        schema_path = Path(__file__).parents[2] / "schemas/v1/validation-report.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(report.to_payload())


if __name__ == "__main__":
    unittest.main()
