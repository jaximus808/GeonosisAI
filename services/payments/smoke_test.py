"""Kept so the documented command still works: services/payments/.venv/bin/python services/payments/smoke_test.py
The smoke test lives in rdf_payments.smoke_test (run `cd services/payments && uv run rdf-payments-smoke`)."""
from rdf_payments.smoke_test import cli

if __name__ == "__main__":
    cli()
