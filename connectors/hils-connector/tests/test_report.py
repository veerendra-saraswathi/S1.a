"""The contract is only worth having if off-contract reports are refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from s2_hils.report import ReportError, load, parse

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "TR-HILS-0058.json"


def test_the_worked_example_is_on_contract():
    report = load(EXAMPLE)
    assert report.run.testRunId == "TR-HILS-0058"
    assert report.run.verdict == "FAIL"
    assert report.run.verifies == ("SYS-NAV-012",)
    assert len(report.anomalies) == 2


def test_the_window_keeps_its_type():
    """durationS must stay a number: S2 holds it as a double, not a string."""
    report = load(EXAMPLE)
    assert report.anomalies[0].durationS == pytest.approx(6.6)
    assert isinstance(report.anomalies[0].durationS, float)


def test_an_anomaly_is_attributed_only_when_both_halves_are_present():
    report = load(EXAMPLE)
    assert report.anomalies[0].attributed is True
    assert report.anomalies[1].attributed is False
    assert report.anomalies[1].subsystemId is None


def test_another_contract_version_is_refused(document):
    document["contract"] = "s2.bench-report/v2"
    with pytest.raises(ReportError):
        parse(document)


def test_an_unknown_fault_class_is_refused(document):
    document["anomalies"][0]["faultClass"] = "GREMLINS"
    with pytest.raises(ReportError, match="faultClass"):
        parse(document)


def test_an_unknown_verdict_is_refused(document):
    document["run"]["verdict"] = "PROBABLY_FINE"
    with pytest.raises(ReportError):
        parse(document)


def test_a_subsystem_without_a_cause_is_refused(document):
    """Attributing a fault to a subsystem without saying why is not an attribution."""
    del document["anomalies"][0]["rootCause"]
    with pytest.raises(ReportError):
        parse(document)


def test_a_cause_without_a_subsystem_is_refused(document):
    del document["anomalies"][0]["subsystemId"]
    with pytest.raises(ReportError):
        parse(document)


def test_a_negative_window_is_refused(document):
    document["anomalies"][0]["durationS"] = -1
    with pytest.raises(ReportError):
        parse(document)


def test_an_unknown_field_is_refused(document):
    """A bench that invents a field has diverged from the contract and should be told."""
    document["run"]["temperatureC"] = 41
    with pytest.raises(ReportError):
        parse(document)


def test_a_missing_run_is_refused(document):
    del document["run"]
    with pytest.raises(ReportError):
        parse(document)


def test_a_report_that_is_not_json_says_so(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ not json")
    with pytest.raises(ReportError, match="not valid JSON"):
        load(path)


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(ReportError, match="cannot read"):
        load(tmp_path / "absent.json")
