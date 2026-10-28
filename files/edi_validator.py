"""
EDI Validation Assistant
------------------------
A simple base project that parses an X12 837 (Professional) healthcare claim
file, validates it against structural and business rules, and explains each
problem with a suggested fix.

Usage:
    python edi_validator.py sample_data/valid_837.edi
    python edi_validator.py sample_data/invalid_837.edi --json
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime


# --------------------------------------------------------------------------
# Data models
# --------------------------------------------------------------------------
@dataclass
class Segment:
    pos: int            # 1-based position in the file
    id: str             # e.g. "CLM"
    elements: list      # elements after the segment ID
    raw: str

    def el(self, n: int) -> str:
        """Return element n (1-based, as in X12 docs: CLM02 -> el(2))."""
        return self.elements[n - 1].strip() if n - 1 < len(self.elements) else ""


@dataclass
class Issue:
    severity: str       # ERROR or WARNING
    code: str           # short rule code
    segment_pos: int
    segment_id: str
    message: str
    suggestion: str


@dataclass
class Report:
    file: str
    segments_read: int
    issues: list = field(default_factory=list)

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "WARNING"]

    @property
    def is_valid(self):
        return not self.errors


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------
def parse_edi(text: str):
    """Split raw X12 text into Segment objects using the delimiters in ISA."""
    text = text.lstrip("\ufeff").strip()
    if not text.startswith("ISA") or len(text) < 106:
        raise ValueError("File does not start with a valid 106-character ISA segment.")

    element_sep = text[3]
    segment_term = text[105]

    segments = []
    chunks = [c.strip() for c in text.split(segment_term) if c.strip()]
    for i, raw in enumerate(chunks, start=1):
        parts = raw.split(element_sep)
        segments.append(Segment(pos=i, id=parts[0], elements=parts[1:], raw=raw))
    return segments


# --------------------------------------------------------------------------
# Helper checks
# --------------------------------------------------------------------------
def valid_npi(npi: str) -> bool:
    """NPI = 10 digits, Luhn check with the 80840 prefix."""
    if not re.fullmatch(r"\d{10}", npi):
        return False
    digits = [int(d) for d in "80840" + npi]
    total = 0
    for idx, d in enumerate(reversed(digits)):
        if idx % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def valid_date_d8(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y%m%d")
        return True
    except ValueError:
        return False


ICD10_PATTERN = re.compile(r"^[A-TV-Z][0-9][0-9A-Z]{1,5}$")  # dot is omitted in 837


def to_float(value: str):
    try:
        return float(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------
class EDIValidator:
    def __init__(self, segments):
        self.segments = segments
        self.issues = []

    # -- utility ----------------------------------------------------------
    def add(self, severity, code, seg, message, suggestion):
        self.issues.append(
            Issue(severity, code, seg.pos if seg else 0, seg.id if seg else "-", message, suggestion)
        )

    def find(self, seg_id):
        return [s for s in self.segments if s.id == seg_id]

    # -- rule groups ------------------------------------------------------
    def check_envelope(self):
        isa, iea = self.find("ISA"), self.find("IEA")
        gs, ge = self.find("GS"), self.find("GE")
        st, se = self.find("ST"), self.find("SE")

        for name, lst in [("ISA", isa), ("IEA", iea), ("GS", gs), ("GE", ge), ("ST", st), ("SE", se)]:
            if not lst:
                self.add("ERROR", "ENV001", None, f"Missing required {name} segment.",
                         f"Add the {name} segment to complete the X12 envelope.")
        if not (isa and iea and gs and ge and st and se):
            return

        if isa[0].el(13) != iea[0].el(2):
            self.add("ERROR", "ENV002", iea[0],
                     f"ISA13 ({isa[0].el(13)}) does not match IEA02 ({iea[0].el(2)}).",
                     "Make the interchange control numbers identical in ISA13 and IEA02.")
        if iea[0].el(1) != str(len(gs)):
            self.add("ERROR", "ENV003", iea[0],
                     f"IEA01 says {iea[0].el(1)} functional group(s) but {len(gs)} found.",
                     "Set IEA01 to the actual number of GS/GE groups.")
        if gs[0].el(6) != ge[0].el(2):
            self.add("ERROR", "ENV004", ge[0],
                     f"GS06 ({gs[0].el(6)}) does not match GE02 ({ge[0].el(2)}).",
                     "Make group control numbers identical in GS06 and GE02.")
        if ge[0].el(1) != str(len(st)):
            self.add("ERROR", "ENV005", ge[0],
                     f"GE01 says {ge[0].el(1)} transaction set(s) but {len(st)} found.",
                     "Set GE01 to the actual number of ST/SE sets.")
        if st[0].el(2) != se[0].el(2):
            self.add("ERROR", "ENV006", se[0],
                     f"ST02 ({st[0].el(2)}) does not match SE02 ({se[0].el(2)}).",
                     "Make transaction control numbers identical in ST02 and SE02.")

        # SE01 = number of segments from ST to SE inclusive
        actual = se[0].pos - st[0].pos + 1
        if se[0].el(1) != str(actual):
            self.add("ERROR", "ENV007", se[0],
                     f"SE01 says {se[0].el(1)} segments but {actual} counted (ST..SE).",
                     f"Change SE01 to {actual}.")

        if st[0].el(1) != "837":
            self.add("ERROR", "ENV008", st[0],
                     f"Transaction set is '{st[0].el(1)}', this tool validates 837 claims.",
                     "Submit an 837 file or extend the validator for this transaction type.")

    def check_required_segments(self):
        required = ["BHT", "CLM", "HI", "SV1"]
        for seg_id in required:
            if not self.find(seg_id):
                self.add("ERROR", "REQ001", None, f"Missing required segment {seg_id}.",
                         f"Add the {seg_id} segment per the 837P implementation guide.")

        loops = {"85": "Billing Provider", "IL": "Subscriber", "PR": "Payer"}
        found = {s.el(1) for s in self.find("NM1")}
        for code, name in loops.items():
            if code not in found:
                self.add("ERROR", "REQ002", None, f"Missing NM1*{code} ({name}) segment.",
                         f"Add an NM1 segment with qualifier {code} for the {name}.")

    def check_ids_and_npi(self):
        for nm1 in self.find("NM1"):
            qualifier, id_type, id_value = nm1.el(1), nm1.el(8), nm1.el(9)
            if not id_value:
                self.add("ERROR", "NM101", nm1, f"NM1*{qualifier} has no identifier (NM109).",
                         "Provide the ID code in NM109.")
            if id_type == "XX" and not valid_npi(id_value):
                self.add("ERROR", "NPI01", nm1, f"NPI '{id_value}' fails the NPI check-digit test.",
                         "Verify the 10-digit NPI against the NPPES registry.")
            if qualifier == "IL" and id_type == "MI" and len(id_value) < 5:
                self.add("WARNING", "SUB01", nm1, "Subscriber member ID looks unusually short.",
                         "Confirm the member ID on the patient's insurance card.")

    def check_claim(self):
        claims = self.find("CLM")
        lines = self.find("SV1")
        if not claims:
            return
        clm = claims[0]

        if not clm.el(1):
            self.add("ERROR", "CLM01", clm, "Claim submitter ID (CLM01) is empty.",
                     "Provide a unique patient control number in CLM01.")
        charge = to_float(clm.el(2))
        if charge is None or charge <= 0:
            self.add("ERROR", "CLM02", clm, f"Total charge '{clm.el(2)}' is not a positive number.",
                     "Set CLM02 to the total claim charge (e.g. 150.00).")
        elif lines:
            line_total = 0.0
            for sv1 in lines:
                amt = to_float(sv1.el(2))
                if amt is None:
                    self.add("ERROR", "SV102", sv1, f"Line charge '{sv1.el(2)}' is not numeric.",
                             "Provide a numeric line charge in SV102.")
                else:
                    line_total += amt
            if abs(line_total - (charge or 0)) > 0.005:
                self.add("ERROR", "CLM03", clm,
                         f"CLM02 total ({charge:.2f}) differs from sum of service lines ({line_total:.2f}).",
                         f"Set CLM02 to {line_total:.2f} or correct the SV1 line charges.")

        place = clm.el(5).split(":")[0] if clm.el(5) else ""
        if not place:
            self.add("ERROR", "CLM05", clm, "Place of service code is missing in CLM05.",
                     "Add a place-of-service code such as 11 (office).")

    def check_diagnosis(self):
        for hi in self.find("HI"):
            for i, comp in enumerate(hi.elements, start=1):
                qualifier, _, code = comp.partition(":")
                code = code.split(":")[0]
                if not code:
                    self.add("ERROR", "HI001", hi, f"HI{i:02d} has no diagnosis code.",
                             "Provide an ICD-10-CM code after the qualifier (e.g. ABK:J45909).")
                elif "." in code:
                    self.add("ERROR", "HI002", hi, f"Diagnosis '{code}' contains a decimal point.",
                             f"Remove the period: {code.replace('.', '')}.")
                elif not ICD10_PATTERN.match(code):
                    self.add("ERROR", "HI003", hi, f"Diagnosis '{code}' is not a valid ICD-10 format.",
                             "Check the code against the ICD-10-CM code set.")
                if i == 1 and qualifier not in ("ABK", "BK"):
                    self.add("WARNING", "HI004", hi,
                             f"Principal diagnosis qualifier is '{qualifier}', expected ABK (ICD-10) or BK.",
                             "Use qualifier ABK for ICD-10-CM principal diagnosis.")

    def check_dates(self):
        for dtp in self.find("DTP"):
            fmt, value = dtp.el(2), dtp.el(3)
            if fmt == "D8" and not valid_date_d8(value):
                self.add("ERROR", "DTP01", dtp, f"Date '{value}' is not a valid CCYYMMDD date.",
                         "Use the format CCYYMMDD, e.g. 20260131.")
            elif fmt == "D8" and valid_date_d8(value) and datetime.strptime(value, "%Y%m%d") > datetime.now():
                self.add("WARNING", "DTP02", dtp, f"Service date {value} is in the future.",
                         "Confirm the date of service; payers reject future-dated claims.")

        for sv1 in self.find("SV1"):
            proc = sv1.el(1).split(":")
            if proc[0] != "HC" or len(proc) < 2 or not re.fullmatch(r"[A-Z0-9]{5}", proc[1]):
                self.add("ERROR", "SV101", sv1, f"Procedure code '{sv1.el(1)}' is malformed.",
                         "Use HC:<5-character CPT/HCPCS code>, e.g. HC:99213.")

    # -- orchestration ----------------------------------------------------
    def run(self):
        self.check_envelope()
        self.check_required_segments()
        self.check_ids_and_npi()
        self.check_claim()
        self.check_diagnosis()
        self.check_dates()
        return self.issues


# --------------------------------------------------------------------------
# Reporting / CLI
# --------------------------------------------------------------------------
def validate_file(path: str) -> Report:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    segments = parse_edi(text)
    issues = EDIValidator(segments).run()
    return Report(file=path, segments_read=len(segments), issues=issues)


def print_report(report: Report):
    print("=" * 66)
    print(f" EDI Validation Report: {report.file}")
    print("=" * 66)
    print(f" Segments read : {report.segments_read}")
    print(f" Errors        : {len(report.errors)}")
    print(f" Warnings      : {len(report.warnings)}")
    print(f" Result        : {'PASS' if report.is_valid else 'FAIL'}")
    print("-" * 66)
    for i in sorted(report.issues, key=lambda x: (x.severity != "ERROR", x.segment_pos)):
        where = f"segment #{i.segment_pos} ({i.segment_id})" if i.segment_pos else "file level"
        print(f"[{i.severity}] {i.code} at {where}")
        print(f"   Problem : {i.message}")
        print(f"   Fix     : {i.suggestion}")
    if not report.issues:
        print("No issues found. File is ready for submission.")


def main():
    parser = argparse.ArgumentParser(description="EDI 837 Validation Assistant")
    parser.add_argument("file", help="Path to the 837 EDI file")
    parser.add_argument("--json", action="store_true", help="Output the report as JSON")
    args = parser.parse_args()

    try:
        report = validate_file(args.file)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        payload = {
            "file": report.file,
            "segments_read": report.segments_read,
            "valid": report.is_valid,
            "issues": [asdict(i) for i in report.issues],
        }
        print(json.dumps(payload, indent=2))
    else:
        print_report(report)

    sys.exit(0 if report.is_valid else 1)


if __name__ == "__main__":
    main()
