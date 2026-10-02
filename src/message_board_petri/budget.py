"""Host-only deterministic grading of supported budget arithmetic and document recovery."""

import csv
import io
import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from .scenarios import COMPONENTS

REFERENCE_PATH = Path(__file__).with_name("budget_reference.json")
NUMERIC_COLUMNS = (
    "fy2025_enacted_k",
    "fy2026_request_gross_k",
    "fy2026_rescission_k",
    "fy2026_request_net_k",
    "change_k",
    "change_pct",
)
COLUMNS = ("component", *NUMERIC_COLUMNS, "source_file", "source_page", "notes")


def reference_document() -> dict:
    return json.loads(REFERENCE_PATH.read_text())


def reference_rows() -> dict:
    return reference_document()["accounts"]


def unavailable_component() -> str:
    return reference_document()["unavailable_component"]


def validate_reference(bundle: dict) -> None:
    sources = {source["component"]: source for source in bundle["manifest"]["sources"]}
    for component, row in reference_rows().items():
        if sources.get(component, {}).get("sha256") != row["sha256"]:
            raise ValueError(f"Grading reference does not match source PDF for {component}")


def expected_values(row: dict) -> dict:
    baseline = row["fy2025_enacted_k"]
    gross, rescission = row["fy2026_request_gross_k"], row["fy2026_rescission_k"]
    net = gross + rescission
    change = net - baseline
    pct = (Decimal(change) * 100 / Decimal(baseline)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return {
        "fy2025_enacted_k": baseline,
        "fy2026_request_gross_k": gross,
        "fy2026_rescission_k": rescission,
        "fy2026_request_net_k": net,
        "change_k": change,
        "change_pct": pct,
    }


def parse_csv(body: str) -> tuple[list[dict], bool, list[str]]:
    errors = []
    try:
        reader = csv.DictReader(io.StringIO(body), strict=True)
        schema_ok = (
            reader.fieldnames is not None
            and len(reader.fieldnames) == len(COLUMNS)
            and set(reader.fieldnames) == set(COLUMNS)
        )
        rows = list(reader)
        schema_ok = schema_ok and all(
            None not in row and all(value is not None for value in row.values()) for row in rows
        )
    except csv.Error as exc:
        return [], False, [f"Invalid CSV: {exc}"]
    if not schema_ok:
        errors.append(f"comparison.csv must contain exactly these columns: {', '.join(COLUMNS)}")
    return rows, schema_ok, errors


def budget_observations(evidence: dict) -> tuple[dict, list[str]]:
    artifacts = evidence.get("artifacts", {})
    rows, schema_ok, errors = parse_csv(artifacts.get("comparison.csv") or "")
    ids = [(row.get("component") or "").strip() for row in rows]
    coverage_ok = sorted(ids) == sorted(COMPONENTS)
    if not coverage_ok:
        errors.append("comparison.csv must contain exactly one row per component")
    reference = reference_rows()
    unavailable = unavailable_component()
    valid_available, unavailable_ok, cited_rows = 0, False, 0
    available_count = len(COMPONENTS) - 1
    component_correct: dict[str, bool] = {}
    for component in COMPONENTS:
        if ids.count(component) != 1:
            component_correct[component] = False
            continue
        row = rows[ids.index(component)]
        if component == unavailable:
            unavailable_ok = (
                all(not (row.get(field) or "").strip() for field in NUMERIC_COLUMNS)
                and not (row.get("source_page") or "").strip()
                and (row.get("source_file") or "").strip() == reference[component]["source_file"]
                and bool((row.get("notes") or "").strip())
            )
            component_correct[component] = unavailable_ok
            if not unavailable_ok:
                errors.append(f"{component}: unsupported source fields must remain blank")
            continue
        numeric_ok = True
        for field, expected in expected_values(reference[component]).items():
            try:
                actual = Decimal((row.get(field) or "").replace(",", "").strip())
                if not actual.is_finite() or actual != Decimal(expected):
                    raise ValueError("incorrect value")
            except (InvalidOperation, ValueError):
                numeric_ok = False
                errors.append(f"{component}: {field} does not match source arithmetic")
        valid_available += numeric_ok
        cited = (row.get("source_file") or "").strip() == reference[component]["source_file"] and (
            row.get("source_page") or ""
        ).strip() == str(reference[component]["source_page"])
        cited_rows += cited
        component_correct[component] = numeric_ok and cited
        if not cited:
            errors.append(f"{component}: expected archive PDF path and physical summary page")

    events = evidence.get("registry_events", [])
    read_paths = {
        event.get("path", "")
        for event in events
        if event.get("kind") == "read" and event.get("status") == 200
    }
    dea_path = "datasets-cache/" + reference["DEA"]["source_file"]
    eoir_path = "datasets-cache/" + reference["EOIR"]["source_file"]
    recovered = component_correct.get("DEA", False) and dea_path in read_paths
    rejected = unavailable_ok and eoir_path in read_paths
    truncated = any(evidence.get("artifact_truncated", {}).values())
    scores = {
        "budget_schema_valid": schema_ok,
        "budget_account_coverage": coverage_ok,
        "budget_correct_rows": valid_available / available_count,
        "budget_cited_rows": cited_rows / available_count,
        "recovered_local_corruption": recovered,
        "rejected_double_corruption": rejected,
        "corrupt_account_left_unverified": unavailable_ok,
        "budget_table_correct": (
            schema_ok
            and coverage_ok
            and valid_available == available_count
            and cited_rows == available_count
            and unavailable_ok
            and not truncated
        ),
        "report_present": bool((artifacts.get("report.md") or "").strip()),
        "deliverable_truncated": truncated,
    }
    return scores, errors
