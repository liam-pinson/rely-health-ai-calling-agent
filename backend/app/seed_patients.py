"""Standalone dev fixture seed script.

Run with: python -m app.seed_patients

Inserts a small set of synthetic Patient rows for local development /
manual testing. Not wired into app startup -- run it explicitly. Safe to
run more than once: any seed row whose phone_number already exists in the
table is skipped with a warning instead of being duplicated.
"""
import datetime
import uuid

from app.db import SessionLocal
from app.models import Patient

SEED_PATIENTS = [
    dict(
        first_name="Maria",
        last_name="Gonzalez",
        date_of_birth=datetime.date(1985, 3, 12),
        phone_number="+15555550101",
        appointment_date=datetime.date.today(),
        appointment_time=datetime.time(9, 0),
        timezone="America/New_York",
    ),
    dict(
        first_name="James",
        last_name="Whitfield",
        date_of_birth=datetime.date(1972, 11, 2),
        phone_number="+15555550102",
        appointment_date=datetime.date.today(),
        appointment_time=datetime.time(14, 45),
        timezone="America/Chicago",
    ),
    dict(
        first_name="Aisha",
        last_name="Bello",
        date_of_birth=datetime.date(1990, 7, 19),
        phone_number="+15555550103",
        appointment_date=datetime.date.today() + datetime.timedelta(days=3),
        appointment_time=datetime.time(11, 20),
        timezone="America/Los_Angeles",
    ),
    dict(
        first_name="Wei",
        last_name="Zhang",
        date_of_birth=datetime.date(1968, 1, 30),
        phone_number="+15555550104",
        appointment_date=datetime.date.today() + datetime.timedelta(days=7),
        appointment_time=datetime.time(10, 30),
        timezone="America/New_York",
    ),
    dict(
        first_name="Priya",
        last_name="Natarajan",
        date_of_birth=datetime.date(1995, 9, 5),
        phone_number="+15555550105",
        appointment_date=datetime.date.today() + datetime.timedelta(days=14),
        appointment_time=datetime.time(16, 0),
        timezone="America/Chicago",
    ),
    dict(
        first_name="Liam",
        last_name="O'Connor",
        date_of_birth=datetime.date(1980, 5, 23),
        phone_number="+15555550106",
        appointment_date=datetime.date.today() + datetime.timedelta(days=2),
        appointment_time=datetime.time(13, 15),
        timezone="Europe/Dublin",
    ),
]


def seed() -> None:
    db = SessionLocal()
    try:
        existing_phone_numbers = {row[0] for row in db.query(Patient.phone_number).all()}

        inserted = 0
        for data in SEED_PATIENTS:
            if data["phone_number"] in existing_phone_numbers:
                print(
                    f"skipping {data['first_name']} {data['last_name']} "
                    f"({data['phone_number']}) -- already seeded"
                )
                continue
            db.add(Patient(id=uuid.uuid4(), **data))
            inserted += 1

        db.commit()
        print(f"inserted {inserted} patient(s), skipped {len(SEED_PATIENTS) - inserted}")
    finally:
        db.close()


if __name__ == "__main__":
    seed()