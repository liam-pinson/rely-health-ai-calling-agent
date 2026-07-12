def test_get_patients_returns_seeded_rows_with_correct_shape(client, patient):
    resp = client.get("/patients")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1

    row = body[0]
    assert row["id"] == str(patient.id)
    assert row["first_name"] == "Test"
    assert row["last_name"] == "Patient"
    assert row["date_of_birth"] == "1990-01-01"
    assert row["phone_number"] == "+15555550100"
    assert row["timezone"] == "America/New_York"
    assert "appointment_date" in row
    assert "appointment_time" in row


def test_get_patients_empty_list_when_none_seeded(client):
    resp = client.get("/patients")

    assert resp.status_code == 200
    assert resp.json() == []