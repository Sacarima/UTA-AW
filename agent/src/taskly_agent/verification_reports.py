"""Bounded, fail-closed parsing. Report contents are data, NOT authenticated evidence.

Formats inspected locally: pytest 9.1.1 _pytest/junitxml.py and Vitest 4.1.11
dist/chunks/index.UpGiHP7g.js JsonReporter. Coverage uses coverage.py JSON and
Istanbul json-summary. Unknown/inconsistent formats cannot produce a pass.
Raw names, errors, console text and report paths never enter summaries.
"""

import json
import xml.etree.ElementTree as ET

from .contracts import Contract, CoverageEvidence, CoverageMetric, CoverageMetrics, RunOutcome, TestCounts
from .execution import ExecutionCapture, ExecutionLimits, Suite


class ReportError(ValueError):
    def __init__(self):
        super().__init__("invalid structured report")


class VerificationSummary(Contract):
    outcome: RunOutcome
    tests: TestCounts | None
    coverage: CoverageEvidence
    reason: str


class SuiteReport(VerificationSummary):
    suite: Suite


def _integer(value) -> int:
    if type(value) is not int or value < 0:
        raise ReportError()
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReportError()
        result[key] = value
    return result


def _json(raw: bytes):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ReportError()))


def _pytest(raw: bytes) -> TestCounts:
    # Reject declarations/entities, including UTF-16 input, before stdlib parsing.
    text = raw.decode("utf-8", errors="strict")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper() or "\0" in text:
        raise ReportError()
    root = ET.fromstring(text)
    suites = [root] if root.tag == "testsuite" else list(root) if root.tag == "testsuites" else []
    if not suites or any(suite.tag != "testsuite" for suite in suites):
        raise ReportError()
    counts = dict(passed=0, failed=0, skipped=0, errors=0)
    for suite in suites:
        current = dict.fromkeys(counts, 0)
        cases = suite.findall("testcase")
        for case in cases:
            statuses = [key for tag, key in (("failure", "failed"), ("error", "errors"), ("skipped", "skipped"))
                        if case.find(tag) is not None]
            if len(statuses) > 1:
                raise ReportError()
            current[statuses[0] if statuses else "passed"] += 1
        for attribute, expected in (("tests", sum(current.values())), ("failures", current["failed"]),
                                    ("errors", current["errors"]), ("skipped", current["skipped"])):
            value = suite.attrib.get(attribute, "")
            if not value.isascii() or not value.isdecimal() or int(value) != expected:
                raise ReportError()
        for key in counts:
            counts[key] += current[key]
    return TestCounts(**counts)


def _vitest(raw: bytes) -> TestCounts:
    report = _json(raw)
    counts = dict(passed=0, failed=0, skipped=0, errors=0)
    for file in report["testResults"]:
        if file["status"] not in {"passed", "failed"}:
            raise ReportError()
        failures = 0
        for assertion in file["assertionResults"]:
            status = assertion["status"]
            # Vitest maps still running/queued assertions to pending, while
            # intentional skip/todo have distinct statuses. Never count an
            # unfinished assertion as an intentionally skipped test.
            if status not in {"passed", "failed", "skipped", "todo"}:
                raise ReportError()
            counts[status if status in {"passed", "failed"} else "skipped"] += 1
            failures += int(status == "failed")
        if failures and file["status"] != "failed":
            raise ReportError()
        if file["status"] == "failed" and not failures:
            counts["errors"] += 1  # module/suite setup failure, not a failed assertion
    passed, failed = _integer(report["numPassedTests"]), _integer(report["numFailedTests"])
    skipped = _integer(report["numPendingTests"]) + _integer(report["numTodoTests"])
    if (passed, failed, skipped) != (counts["passed"], counts["failed"], counts["skipped"]) or (
        _integer(report["numTotalTests"]) != passed + failed + skipped
    ):
        raise ReportError()
    failed_suites = _integer(report["numFailedTestSuites"])
    pending_suites = _integer(report["numPendingTestSuites"])
    if pending_suites or _integer(report["numTotalTestSuites"]) != (
        _integer(report["numPassedTestSuites"]) + failed_suites + pending_suites
    ):
        raise ReportError()
    expected_success = not (failed or counts["errors"] or failed_suites)
    if type(report["success"]) is not bool or report["success"] != expected_success:
        raise ReportError()
    if failed_suites and not (failed or counts["errors"]):
        raise ReportError()
    return TestCounts(**counts)


def _coverage(suite: Suite, raw: bytes) -> CoverageMetrics:
    report = _json(raw)
    if suite == "backend":
        branch = report["meta"]["branch_coverage"]
        if type(branch) is not bool or not report["files"]:
            raise ReportError()
        summaries = [file["summary"] for file in report["files"].values()]
        totals = report["totals"]
        pairs = (("covered_lines", "num_statements"),) + (("covered_branches", "num_branches"),) * branch
        for covered, total in pairs:
            for summary in summaries:
                CoverageMetric(covered=_integer(summary[covered]), total=_integer(summary[total]))
            for key in (covered, total):
                if _integer(totals[key]) != sum(_integer(summary[key]) for summary in summaries):
                    raise ReportError()
        lines = CoverageMetric(covered=_integer(totals["covered_lines"]), total=_integer(totals["num_statements"]))
        branches = CoverageMetric(covered=_integer(totals["covered_branches"]), total=_integer(totals["num_branches"])) if branch else None
        return CoverageMetrics(statements=lines, lines=lines, branches=branches)
    totals = report["total"]
    files = [value for key, value in report.items() if key != "total"]
    if not files:
        raise ReportError()
    metrics = {}
    for name in ("statements", "branches", "lines"):
        for file in files:
            CoverageMetric(covered=_integer(file[name]["covered"]), total=_integer(file[name]["total"]))
        for key in ("covered", "total"):
            if _integer(totals[name][key]) != sum(_integer(file[name][key]) for file in files):
                raise ReportError()
        metrics[name] = CoverageMetric(covered=_integer(totals[name]["covered"]), total=_integer(totals[name]["total"]))
    return CoverageMetrics(**metrics)


def parse_capture(capture: ExecutionCapture, limits: ExecutionLimits) -> SuiteReport:
    capture = ExecutionCapture.model_validate(capture)
    limits = ExecutionLimits.model_validate(limits)

    def unavailable(reason):
        return SuiteReport(suite=capture.suite, outcome="incomplete", tests=None,
            coverage=CoverageEvidence(state="unavailable", reason=reason), reason=reason)

    if len(capture.stdout) + len(capture.stderr) > limits.max_output_bytes or any(
        raw is not None and len(raw) > limits.max_report_bytes for raw in (capture.tests_report, capture.coverage_report)
    ):
        return unavailable("output_limit")
    if capture.state != "completed":
        return unavailable("runner_" + capture.state)
    if capture.exit_code not in {0, 1}:
        # Pytest 2 may carry authentic collection errors; other exits are infrastructure failures.
        if not (capture.suite == "backend" and capture.exit_code == 2):
            return unavailable("runner_exit")
    if capture.tests_report is None:
        return unavailable("test_report_missing")
    try:
        tests = (_pytest if capture.suite == "backend" else _vitest)(capture.tests_report)
        failures = tests.failed + tests.errors
        if (capture.exit_code == 0 and failures) or (capture.exit_code != 0 and not failures):
            raise ReportError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, ET.ParseError):
        return unavailable("test_report_invalid")
    if capture.coverage_report is None:
        coverage = CoverageEvidence(state="missing", reason="coverage_missing")
    else:
        try:
            coverage = CoverageEvidence(state="available", metrics=_coverage(capture.suite, capture.coverage_report))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            coverage = CoverageEvidence(state="unavailable", reason="coverage_invalid")
    outcome = "failed" if failures else "passed" if tests.passed > 0 and coverage.state == "available" else "incomplete"
    return SuiteReport(suite=capture.suite, outcome=outcome, tests=tests, coverage=coverage,
        reason="tests_failed" if failures else "complete" if outcome == "passed" else "evidence_incomplete")


def aggregate_reports(reports: tuple[SuiteReport, ...], required: tuple[Suite, ...]) -> VerificationSummary:
    """Pure hypothetical aggregation, never attestation of execution/provenance.

    Missing counts/metrics are never summed as zero, and partial mixed coverage
    is never presented as complete coverage.
    """
    if not required or len(set(required)) != len(required):
        raise ReportError()
    if len(reports) != len(required) or {report.suite for report in reports} != set(required) or any(report.tests is None for report in reports):
        return VerificationSummary(outcome="incomplete", tests=None,
            coverage=CoverageEvidence(state="unavailable", reason="suite_missing"), reason="suite_missing")
    tests = TestCounts(**{key: sum(getattr(report.tests, key) for report in reports) for key in TestCounts.model_fields})
    if all(report.coverage.state == "available" for report in reports):
        metrics = {}
        for key in CoverageMetrics.model_fields:
            values = [getattr(report.coverage.metrics, key) for report in reports]
            metrics[key] = CoverageMetric(covered=sum(value.covered for value in values), total=sum(value.total for value in values)) if all(value is not None for value in values) else None
        coverage = CoverageEvidence(state="available", metrics=CoverageMetrics(**metrics))
    else:
        coverage = CoverageEvidence(state="unavailable", reason="coverage_incomplete")
    outcome = "failed" if tests.failed + tests.errors else "passed" if all(report.outcome == "passed" for report in reports) and coverage.state == "available" else "incomplete"
    return VerificationSummary(outcome=outcome, tests=tests, coverage=coverage, reason="aggregate_" + outcome)
