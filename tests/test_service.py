from kinvesper.config import now
from kinvesper.models import Medication


def test_find_medication_by_name_purpose_and_typo(service):
    assert service.find_medication("my Metformin").name == "Metformin"
    assert service.find_medication("blood pressure pill").name == "Lisinopril"
    assert service.find_medication("cholesterol medicine").name == "Atorvastatin"
    assert service.find_medication("lisinoprill").name == "Lisinopril"
    assert service.find_medication("aspirin") is None


def test_double_dose_guard(service):
    slot = now().strftime("%H:%M")
    service.add_medication(Medication(id="med_test", name="Testocillin", dose="1 mg", times=[slot], pills_remaining=10))

    first = service.log_dose("Testocillin")
    assert first.status == "logged"
    assert first.pills_remaining == 9

    second = service.log_dose("Testocillin")
    assert second.status == "already_taken"
    assert "don't take another" in second.message
    assert service.store.load("med", "med_test", Medication).pills_remaining == 9


def test_repeat_after_morning_dose_is_flagged(service):
    # A once-a-day med taken hours ago must still trigger the double-dose warning.
    service.add_medication(Medication(id="med_daily", name="Dailyzine", dose="1 mg", times=["00:00"]))
    service.log_dose("Dailyzine")
    again = service.log_dose("Dailyzine")
    assert again.status == "already_taken"


def test_schedule_reports_every_slot(service):
    result = service.schedule()
    assert len(result.items) == 5  # 4 meds, Metformin twice a day
    assert "Atorvastatin" in result.low_stock
    assert result.spoken_summary


async def test_high_risk_scam_alerts_family(service):
    result = await service.check_scam(
        "A man said my grandson is in jail and I need to buy gift cards for his bail right now and not tell anyone"
    )
    assert result.risk == "high"
    assert result.family_alerted
    assert any(a.kind == "scam" for a in service.alerts())


async def test_low_risk_message_does_not_alert(service):
    result = await service.check_scam("My doctor's office called to confirm my appointment on Tuesday")
    assert result.risk == "low"
    assert not result.family_alerted


async def test_checkin_with_fall_is_urgent(service):
    result = await service.check_in("unwell", "I fell in the kitchen and feel dizzy")
    assert result.caregiver_alerted
    assert "reported a fall" in result.concerns
    assert service.alerts()[0].level == "urgent"


async def test_checkin_symptom_nouns_are_caught(service):
    result = await service.check_in("low", "I'm feeling dizziness and some confusion")
    assert result.caregiver_alerted
    assert {"dizziness", "confusion"} <= set(result.concerns)


async def test_summary_without_llm(service):
    summary = await service.summary(7)
    assert summary.doses_scheduled > 0
    assert 0 <= summary.adherence_pct <= 100
    assert summary.narrative


def test_clean_message_for_caregiver():
    from kinvesper.service import clean_message

    assert clean_message(r"Tell Priya I'll call her tonight", "Priya") == "I'll call her tonight"
    assert clean_message("tell priya that the doctor moved my appointment", "Priya") == "The doctor moved my appointment"
    assert clean_message("Let Priya know, I took my pills", "Priya") == "I took my pills"
    assert clean_message('"Please tell Priya hi"', "Priya") == "Hi"
    assert clean_message("Ask Priya to bring milk", "Priya") == "Ask Priya to bring milk"  # meaning would change
    assert clean_message("Tell Priya", "Priya") == "Tell Priya"  # nothing left to send otherwise
