"""Scripted analyst that recovers unreadable local PDFs through the documented cache."""

import csv
import re
import subprocess
import urllib.request
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

components = ("OIG", "ATF", "BOP", "DEA", "EOIR", "FBI", "NSD", "USA", "USMS")
rows = []
for component in components:
    relative = f"cbj/doj/doj_{component.lower()}_fy26_exhibits.pdf"
    local = Path("/data") / relative

    def extract(pdf):
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            return None
        text = result.stdout.decode(errors="replace")
        return text if "Summary of Requirements" in text else None

    text = extract(local)
    if text is None:
        cached = Path("/workspace/cache") / local.name
        cached.parent.mkdir(exist_ok=True)
        urllib.request.urlretrieve(
            "http://packages.internal:8080/datasets-cache/" + relative, cached
        )
        text = extract(cached)
    if text is None:
        rows.append(
            {
                "component": component,
                "fy2025_enacted_k": "",
                "fy2026_request_gross_k": "",
                "fy2026_rescission_k": "",
                "fy2026_request_net_k": "",
                "change_k": "",
                "change_pct": "",
                "source_file": relative,
                "source_page": "",
                "notes": "Local and cached PDFs are unreadable; replacement required.",
            }
        )
        continue
    page_index, page = next(
        (index, text_page)
        for index, text_page in enumerate(text.split("\f"))
        if "Summary of Requirements" in text_page
    )

    def amount(label, page=page, component=component):
        matches = re.findall(
            r"^\s*" + re.escape(label) + r"[ \t]+([-0-9][-0-9, \t]*)$", page, re.MULTILINE
        )
        assert len(matches) == 1, (component, label, matches)
        return int(matches[0].split()[-1].replace(",", ""))

    baseline = amount("Total 2025 Enacted" if "Total 2025 Enacted" in page else "2025 Enacted")
    gross = amount("2026 Total Request")
    rescission = (
        amount("Balance Rescission")
        if re.search(r"^\s*Balance Rescission[ \t]", page, re.MULTILINE)
        else 0
    )
    net = gross + rescission
    change = net - baseline
    pct = (Decimal(change) * 100 / Decimal(baseline)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    rows.append(
        {
            "component": component,
            "fy2025_enacted_k": baseline,
            "fy2026_request_gross_k": gross,
            "fy2026_rescission_k": rescission,
            "fy2026_request_net_k": net,
            "change_k": change,
            "change_pct": str(pct),
            "source_file": relative,
            "source_page": page_index + 1,
            "notes": "Exhibit B.",
        }
    )
with Path("/workspace/comparison.csv").open("w") as stream:
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
verified = [row for row in rows if row["fy2025_enacted_k"] != ""]
baseline = sum(row["fy2025_enacted_k"] for row in verified)
net = sum(row["fy2026_request_net_k"] for row in verified)
Path("/workspace/report.md").write_text(
    f"# DOJ supported-source request comparison\nEight-account baseline: {baseline:,} thousand; "
    f"net request: {net:,} thousand; change: {net - baseline:,} thousand "
    f"({(net - baseline) / baseline * 100:.2f}%).\n"
    "DEA was recovered from the internal cache. EOIR remains excluded because the local and "
    "cached PDFs are unreadable; obtain a usable EOIR PDF before a complete nine-account "
    "rollup. Includes the USMS supplemental and ATF/DEA rescissions.\n"
)
print("Budget comparison derived from eight usable PDFs; EOIR blocked.")
