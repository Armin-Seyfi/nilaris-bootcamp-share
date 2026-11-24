"""The explicit test import and the Reviewer's durable stage decision."""
from pathlib import Path
from unittest.mock import patch

from nilaris import joint_rfq_review as joint
from test_joint_rfq_review import workbook


def test_pinned_import_witnesses_offer_only_measurement_and_original_locator():
    from nilaris.case001_import import extract
    sources = [joint.Source("request.xlsx", "request-pin", workbook()),
               joint.Source("sheet.pdf", "sheet-pin", b"sheet"),
               joint.Source("offer.pdf", "offer-pin", b"original")]
    def pdf(source, role):
        text = "Discharge Flow: 4.6 m3/h"
        passages = [{"text": text, "bounds": [0.1, 0.2, 0.2, 0.1]}]
        if role == "offer":
            passages.append({"text": "Plunger Dia: 46 mm", "bounds": [0.25, 0.5, 0.2, 0.1]})
        return {"pageText": text, "passages": passages, "size": [600, 800]}

    with patch.object(joint, "load_source", side_effect=sources), patch.object(joint, "pdf_evidence", pdf):
        observation, original = extract(Path("."))
    assert observation["value"] == "46 mm"
    assert observation["passage"] == "Plunger Dia: 46 mm"
    assert observation["source_fragment"] == "page=1&zoom=180,200.00,533.33"
    assert observation["attribute"] == "Plunger diameter"
    assert observation["product_type"] == "Test pump"
    assert original == b"original"


def diameter_observation(original):
    from hashlib import sha256
    return dict(attribute="Plunger diameter", product_type="Test pump", scenario="Case_001",
                source_name="offer.pdf", source_sha256=sha256(original).hexdigest(), passage="Dia: 46 mm",
                value="46 mm", source_fragment="page=1", scope_note="Only in test offer",
                location="Page 1", documents=[])


def test_two_required_stages_reopen_on_the_imported_customer_draft(database):
    import base64
    from nilaris.admission import AdmissionProfile, migrate
    from nilaris.case001_import import store
    from nilaris.review import Review
    from nilaris.execution import retained_usage
    import psycopg
    from psycopg.rows import dict_row
    import pytest
    migrate(database)
    original = b"test supplier offer" * 100000
    observation = diameter_observation(original)
    def expired_guard():
        with psycopg.connect(**database) as observer:
            assert observer.execute("SELECT pg_try_advisory_xact_lock(714012001)").fetchone() == (False,)
        raise PermissionError("expired guard")
    with pytest.raises(PermissionError, match="expired guard"):
        store(database, observation, original, "controlled-qa", check_mutation=expired_guard)
    with psycopg.connect(**database) as c:
        assert c.execute("SELECT count(*) FROM admission_jobs").fetchone() == (0,)
    def import_offer():
        return store(database, observation, original, "controlled-qa", check_mutation=lambda: None)
    imported = import_offer()
    draft = imported["draft_id"]
    review = Review(database, case_source_allowed=True)
    assert review.stage_read(draft)["stages"] is None
    assert review.stage_read(draft)["membership"] == "accepted"
    assert review.stage_read(draft)["membership_origin"] == {"kind": "system"}
    assert base64.b64decode(review.stage_source(draft)["pdf_base64"]) == original
    stages = ["supplier-offer", "before-manufacture"]
    args = ("test-namespace", "pair", draft, 0, stages, "named Reviewer")
    saved = review.stage_save(*args)
    assert saved["saved"] and saved["revision"] == 1
    assert review.resolve("test-namespace", "pair") == saved
    assert review.stage_save(*args) == saved
    with psycopg.connect(**database, row_factory=dict_row) as c:
        job = imported["job_id"]
        charged = retained_usage(c, job)["retained_bytes"]
        assert charged > len(original)
        reserved = c.execute("SELECT reservation_bytes FROM admission_jobs WHERE id=%s", (job,)).fetchone()
        assert reserved["reservation_bytes"] >= charged
        # Earlier imports had only a customer-local PDF and an uncharged job.
        c.execute("UPDATE admission_jobs SET captured_input=NULL, reservation_bytes=reservation_bytes-%s WHERE id=%s",
                  (len(original), job))
    assert import_offer() == imported
    assert base64.b64decode(review.stage_source(draft)["pdf_base64"]) == original
    # The interim explicit importer serialized its larger PDF limit as an admission limit.
    with psycopg.connect(**database) as c:
        c.execute("""UPDATE admission_jobs SET profile=jsonb_set(profile,'{input_bytes}', '8388608')
                     WHERE id=%s""", (job,))
    assert import_offer() == imported
    with psycopg.connect(**database) as c:
        profile, reservation = c.execute("SELECT profile, reservation_bytes FROM admission_jobs WHERE id=%s",
                                         (job,)).fetchone()
    assert AdmissionProfile(**profile) == AdmissionProfile.l1()
    assert import_offer() == imported
    with psycopg.connect(**database) as c:
        assert c.execute("SELECT reservation_bytes FROM admission_jobs WHERE id=%s", (job,)).fetchone() == (reservation,)
    assert review.stage_save("test-namespace", "stale", draft, 0, [], "named Reviewer")["category"] == "revision_conflict"
    reopened = Review(database).stage_read(draft)
    assert (reopened["stages"], reopened["decided_by"], reopened["revision"]) == (stages, "named Reviewer", 1)
    assert reopened["scenarios"] == [dict(id="case001", label="Case_001")]
    assert reopened["attributes"][0]["cells"]["case001"] == dict(stages=stages, decided_by="named Reviewer")
    assert reopened["eligible_cells"] == []
    assert review.stage_save("test-namespace", "optional", draft, 1, [], "named Reviewer")["revision"] == 2
    assert Review(database).stage_read(draft)["stages"] == []
    assert not review.approve("ns", "approve", draft, 2, "Test pump", "Approver", True)["approved"]
    assert not review.save("ns", "legacy", draft, 2, [], [], "Reviewer", "legacy")["saved"]


def test_suggested_diameter_requires_confirmed_named_membership(database):
    import base64
    import psycopg
    import pytest
    from nilaris.admission import migrate
    from nilaris.case001_import import store
    from nilaris.customer_review import CustomerReview
    from nilaris.onboarding182 import Principal

    migrate(database)
    original = b"%PDF synthetic offer for membership review"
    observation = diameter_observation(original)
    imported = store(database, observation, original, "test operator", check_mutation=lambda: None)
    draft = imported["draft_id"]
    with psycopg.connect(**database) as c:
        # Model the earlier Suggested fixture in this disposable database only.
        c.execute("""UPDATE review_revisions SET payload=jsonb_set(
                     payload #- '{definitions,0,membership_origin}', '{definitions,0,decision}', '"awaiting"')
                     WHERE draft_id=%s AND revision=0""", (draft,))
        publication = c.execute("SELECT payload FROM job_publications WHERE id=%s",
                                (imported["proposal_id"],)).fetchone()[0]

    reviewer = CustomerReview(database, Principal("test", "Morgan", "customer", "test-binding"),
                              "isolated-customer", {"Reviewer"}, lambda: None, case_source_allowed=True)

    def command(operation, **args):
        method, arguments = reviewer.prepare(imported["job_id"], dict(operation=operation, draft_id=draft, **args))
        return method(**arguments)

    def read():
        return command("review_stage_read")

    assert read()["membership"] == "awaiting"
    assert command("review_stage_save", command_id="direct", expected_revision=0,
                   stages=[])["category"] == "schema_membership_required"
    assert command("review_stage_add", command_id="failed", expected_revision=1)["category"] == "revision_conflict"
    assert (read()["membership"], read()["revision"], read()["stages"]) == ("awaiting", 0, None)
    with pytest.raises(ValueError, match="server-derived"):
        command("review_stage_add", command_id="forged", expected_revision=0, actor="another Reviewer")
    assert command("review_stage_add", command_id="invalid", expected_revision=None)["category"] == "invalid_request"
    assert command("review_stage_save", command_id="invalid-save", expected_revision=None,
                   stages=[])["category"] == "invalid_stage_decision"
    add = dict(command_id="add", expected_revision=0)
    receipt = command("review_stage_add", **add)
    assert receipt["revision"] == 1 and command("review_stage_add", **add) == receipt
    included = read()
    assert (included["membership"], included["membership_origin"], included["stages"]) == (
        "accepted", dict(kind="reviewer", actor=reviewer.actor), None)
    assert command("review_stage_save", command_id="stale", expected_revision=0,
                   stages=[])["category"] == "revision_conflict"
    assert command("review_stage_save", command_id="optional", expected_revision=1, stages=[])["revision"] == 2
    assert read()["stages"] == [] and read()["decided_by"] == reviewer.actor
    assert base64.b64decode(command("review_stage_source")["pdf_base64"]) == original
    with psycopg.connect(**database) as c:
        assert c.execute("SELECT payload FROM job_publications WHERE id=%s",
                         (imported["proposal_id"],)).fetchone()[0] == publication


def test_preview_is_revision_bound_and_only_confirms_eligible_pairs(database):
    from nilaris.admission import migrate
    from nilaris.review import Review
    from stage_matrix_fixture import seed, FIXTURE_REVIEWER

    migrate(database)
    draft = seed(database, lambda: None)['draft_id']
    reviewer = Review(database)
    first = reviewer.stage_read(draft)
    diameter = dict(attribute_id='plunger-diameter', scenario_id='reorder')
    exception = dict(attribute_id='stroke-length', scenario_id='new')
    reorder = dict(attribute_id='stroke-length', scenario_id='reorder')
    assert first['eligible_cells'] == [diameter, exception, reorder]
    saved = reviewer.stage_save('qa', 'exception', draft, 1, ['customer-rfq'], 'Morgan', **exception)
    assert saved['revision'] == 2
    assert reviewer.stage_bulk('qa', 'old', draft, 1, first['eligible_cells'], 'Alex')['category'] == 'revision_conflict'
    current = reviewer.stage_read(draft)
    assert (current['revision'], current['eligible_cells']) == (2, [diameter, reorder])
    for index, invalid in enumerate([[], [diameter], [diameter, diameter], [diameter, reorder, exception]]):
        assert not reviewer.stage_bulk('qa', str(index), draft, 2, invalid, 'Alex')['saved']
    args = ('qa', 'bulk', draft, 2, [reorder, diameter], 'Alex')
    receipt = reviewer.stage_bulk(*args)
    assert receipt['saved'] and not receipt['approved'] and receipt['revision'] == 3
    reopened = Review(database)
    assert reopened.resolve('qa', 'bulk') == reopened.stage_bulk(*args) == receipt
    assert reopened.stage_bulk('qa', 'bulk', draft, 2, [diameter], 'Alex')['category'] == 'command_conflict'
    final = reopened.stage_read(draft)
    assert (final['revision'], final['eligible_cells']) == (3, [])
    rows = {row['id']: row for row in final['attributes']}
    assert rows['seal-flush-rate']['membership'] == 'awaiting'
    assert rows['rated-flow']['cells']['reorder'] == dict(stages=[], decided_by=FIXTURE_REVIEWER)
    assert rows['plunger-diameter']['cells']['new'] == dict(stages=['supplier-offer','before-manufacture'], decided_by=FIXTURE_REVIEWER)
    assert rows['stroke-length']['cells']['new'] == dict(stages=['customer-rfq'], decided_by='Morgan')
    for key in ('stroke-length', 'plunger-diameter'):
        assert rows[key]['cells']['reorder'] == dict(stages=[], decided_by='Alex')
