# EDI Validation Assistant

A simple base project that validates X12 837 (Professional) healthcare claim files and explains every problem with a suggested fix.

## Run

```bash
python edi_validator.py sample_data/valid_837.edi
python edi_validator.py sample_data/invalid_837.edi
python edi_validator.py sample_data/invalid_837.edi --json
```

Requires Python 3.8+ and no external packages. Exit code is 0 for PASS, 1 for FAIL.

## What it checks

| Group | Rules |
|---|---|
| Envelope | ISA/IEA, GS/GE, ST/SE control numbers, group and segment counts |
| Required segments | BHT, CLM, HI, SV1, NM1 for billing provider, subscriber, payer |
| Identifiers | NPI Luhn check digit, empty IDs, short member IDs |
| Claim | Claim ID, charge amount, CLM total vs. sum of SV1 lines, place of service |
| Diagnosis | ICD-10 format, decimal point not allowed, principal qualifier |
| Dates and codes | CCYYMMDD validity, future dates, procedure code format |

## Project structure

```
edi_validation_assistant/
├── edi_validator.py      # parser + validator + CLI
├── sample_data/
│   ├── valid_837.edi
│   └── invalid_837.edi   # contains 6 deliberate errors
└── README.md
```

## Next steps (the "AI" layer)

1. Train a denial-risk model on historical 835 remittance data and add its score to the report.
2. Use an LLM to turn each issue into a plain-English explanation for billing staff.
3. Add a Streamlit or Flask upload page.
4. Support 835, 270/271 and 837I/837D transaction types.
5. Add unit tests with pytest for each rule code.
