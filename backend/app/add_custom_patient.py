"""Standalone dev CLI to add one custom Patient row with a real, callable
phone number.

Run with:
    python -m app.add_custom_patient --name "Full Name" --phone "+1..." \
        --dob YYYY-MM-DD --appointment "YYYY-MM-DD HH:MM:SS" \
        --timezone "America/Los_Angeles"

seed_patients.py only inserts synthetic +1555-prefixed fictional numbers,
which can't receive an actual call -- this is how you add one patient row
with a real number to place a live test call against. All fields come from
CLI args (no interactive prompts, no frontend UI). Not wired into app
startup, not production tooling -- same framing as seed_patients.py.
Idempotent-safe: skips (with a warning) instead of duplicating if the phone
number already exists.
"""
import argparse
import datetime
import uuid

from app.db import SessionLocal
from app.models import Patient


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add one custom Patient row with a real, callable phone number "
        "(dev/testing convenience, not production tooling)."
    )
    parser.add_argument(
        "--name", required=True, help='Full name, e.g. "Jane Doe" (split on the last space into first/last name)'
    )
    parser.add_argument("--phone", required=True, help="E.164 phone number, e.g. +14155551234")
    parser.add_argument("--dob", required=True, help="Date of birth, YYYY-MM-DD")
    parser.add_argument(
        "--appointment", required=True, help='Appointment date+time, "YYYY-MM-DD HH:MM:SS"'
    )
    parser.add_argument("--timezone", required=True, help="IANA timezone, e.g. America/Los_Angeles")
    return parser.parse_args(argv)


def add_custom_patient(name: str, phone: str, dob: str, appointment: str, timezone: str) -> None:
    first_name, _, last_name = name.strip().rpartition(" ")
    if not first_name:
        first_name, last_name = last_name, ""

    date_of_birth = datetime.date.fromisoformat(dob)
    appointment_dt = datetime.datetime.strptime(appointment, "%Y-%m-%d %H:%M:%S")

    db = SessionLocal()
    try:
        existing = db.query(Patient).filter(Patient.phone_number == phone).one_or_none()
        if existing is not None:
            print(f"skipping {name} ({phone}) -- already exists")
            return

        db.add(
            Patient(
                id=uuid.uuid4(),
                first_name=first_name,
                last_name=last_name,
                date_of_birth=date_of_birth,
                phone_number=phone,
                appointment_date=appointment_dt.date(),
                appointment_time=appointment_dt.time(),
                timezone=timezone,
            )
        )
        db.commit()
        print(f"inserted {name} ({phone})")
    finally:
        db.close()


def main(argv=None) -> None:
    args = _parse_args(argv)
    add_custom_patient(
        name=args.name,
        phone=args.phone,
        dob=args.dob,
        appointment=args.appointment,
        timezone=args.timezone,
    )


if __name__ == "__main__":
    main()