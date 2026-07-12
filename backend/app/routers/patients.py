import uuid
from datetime import date, time
from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Patient

router = APIRouter()


class PatientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str
    last_name: str
    date_of_birth: date
    phone_number: str
    appointment_date: date
    appointment_time: time
    timezone: str


@router.get("/patients", response_model=List[PatientResponse])
async def list_patients(db: Session = Depends(get_db)):
    return db.query(Patient).all()