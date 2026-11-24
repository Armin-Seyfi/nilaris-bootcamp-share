"""Explicitly import the three pinned Case_001 sources into a controlled test customer."""
from dataclasses import asdict
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nilaris.admission import AdmissionProfile, _admission_usage
from nilaris.execution import retained_usage
from nilaris.results import canonical
from nilaris.rfq_stage_decision import DIAMETER_ID, IMPORT_CONTRACT
from nilaris import joint_rfq_review as joint


def extract(directory):
    case = joint.inspect_case(directory)
    diameter, = [row for row in case.proposals if row.name == "Plunger diameter"]
    offer = case.offer
    return dict(product_type=case.product, scenario=" · ".join(joint.SCENARIO),
                attribute=diameter.name, value=diameter.value, passage=diameter.passage["text"],
                location="Page 1 · Scope of Supply · Item 1",
                source_fragment=joint.pdf_fragment(case.bid, diameter.passage),
                source_name=offer.name, source_sha256=offer.digest,
                scope_note="Observed only in the supplier offer within these three documents.",
                documents=[dict(name=s.name, sha256=s.digest) for s in (case.book, case.sheet, offer)]), offer.data


def reserve_import(c, job, profile):
    size = retained_usage(c, job)["retained_bytes"]
    usage = _admission_usage(c)
    prior = c.execute("SELECT reservation_bytes FROM admission_jobs WHERE id=%s", (job,)).fetchone()
    if (size > profile["job_bytes"] or usage["job_reservation_bytes"] - prior["reservation_bytes"]
            + usage["receipt_reservation_bytes"] + size > profile["logical_bytes"] - profile["terminal_headroom_bytes"]):
        raise ValueError("Import exceeds available capacity")
    c.execute("UPDATE admission_jobs SET reservation_bytes=%s WHERE id=%s", (size, job))


def store(database, observation, original, operator, *, check_mutation):
    """Commit the finite import, its pinned bytes and undetermined draft together."""
    profile = asdict(AdmissionProfile.l1())
    # Explicit test offers share the completed job retention cap with its other records.
    pin = observation["source_sha256"]
    if len(original) > profile["job_bytes"] or sha256(original).hexdigest() != pin:
        raise ValueError("Pinned bounded offer required")
    job = str(uuid5(NAMESPACE_URL, IMPORT_CONTRACT))
    with psycopg.connect(**database, row_factory=dict_row) as c:
        c.execute("SELECT pg_advisory_xact_lock(714012001)")
        check_mutation()
        existing = c.execute("""
            SELECT d.id AS draft_id, p.id AS proposal_id, d.job_id, p.payload->'stage_observation' AS observation,
                   j.captured_input, j.profile
            FROM review_drafts d JOIN job_publications p ON p.id=d.proposal_id
            JOIN admission_jobs j ON j.id=d.job_id WHERE d.job_id=%s""", (job,)).fetchone()
        if existing:
            captured = existing.pop("captured_input")
            prior_profile = existing.pop("profile")
            if existing.pop("observation") != observation or (captured is not None and captured != original):
                raise ValueError("Existing import differs from these source records")
            if captured is None or prior_profile != profile:
                c.execute("UPDATE admission_jobs SET captured_input=coalesce(captured_input,%s), profile=%s WHERE id=%s",
                          (original, Jsonb(profile), job))
                reserve_import(c, job, profile)
                check_mutation()
            return existing
        product = observation["product_type"]
        context = dict(case_id="Case_001", product_type=product, document_id=observation["source_name"],
                       source_revision=pin, membership_revision=IMPORT_CONTRACT, processing_contract=IMPORT_CONTRACT,
                       reference_set="Case_001 three-source set", selection_policy="offer-only diameter", intent_id="test import")
        input_record = dict(context, source_id=observation["source_name"], connector=IMPORT_CONTRACT,
                            input_bytes=len(original), media_type="application/pdf", identity="resolved")
        keys = ["document_id", "source_revision", "case_id", "membership_revision", "product_type",
                "processing_contract", "reference_set", "selection_policy", "intent_id"]
        identity = Jsonb([context[k] for k in keys])
        c.execute("""INSERT INTO admission_jobs(id,input,state,reason,generation,profile,
                     captured_input,content_pin,canonical_job_id,semantic_key,processing_key,reservation_bytes)
                     VALUES (%s,%s,'completed','explicit_test_import',1,%s,%s,%s,%s,%s,%s,0)""",
                  (job, Jsonb(input_record), Jsonb(profile), original, pin, job, identity, identity))
        attempt, proposal, draft = [str(uuid4()) for _ in range(3)]
        c.execute("""INSERT INTO job_attempts(id,job_id,stage,generation,token,stopped,reason,completion_evidence)
                     VALUES (%s,%s,'processing',1,%s,true,'explicit_test_import','finite_controlled_call')""",
                  (attempt, job, str(uuid4())))
        candidate = dict(id=DIAMETER_ID, label=observation["attribute"], value_type="decimal",
                         domain="technical", unit="mm", observation_ids=["offer-diameter"], requirements="unspecified")
        source = dict(id="offer-diameter", kind="property", presence="observed", source_type="text",
                      original_content=observation["passage"], locator=observation["location"])
        publication = dict(id=proposal, job_id=job, initial_publication_id=proposal, sequence=0,
                           kind="initial", adapter=IMPORT_CONTRACT, stage_contract=IMPORT_CONTRACT,
                           context=context, content_pin=pin, coverage={"inspected": observation["documents"]},
                           outcome="completed", stage_observation=observation,
                           candidates=[candidate], observations=[source], associations=[])
        c.execute("""INSERT INTO job_publications(id,job_id,attempt_id,sequence,request,payload)
                     VALUES (%s,%s,%s,0,%s,%s)""", (proposal, job, attempt, Jsonb(dict(operator=operator)), Jsonb(publication)))
        c.execute("""INSERT INTO review_publication_bases(proposal_id,job_id,reservation_bytes)
                     VALUES (%s,%s,8192)""", (proposal, job))
        c.execute("""INSERT INTO review_drafts(id,job_id,proposal_id,product_type,actor,reservation_bytes)
                     VALUES (%s,%s,%s,%s,%s,8192)""", (draft, job, proposal, product, operator))
        definition = dict(id=candidate["id"], label=candidate["label"], value_type="decimal", unit="mm",
                          requirements="unspecified", conditions="unspecified", observation_ids=candidate["observation_ids"],
                          decision="accepted", membership_origin=dict(kind="system"),
                          origin=dict(kind="initial_proposal", proposal_id=proposal,
                                                           candidate_id=candidate["id"]))
        snapshot = dict(revision=0, definitions=[definition], associations=[], stage_decision=None,
                        actor=operator, reason="Explicit Case_001 test import")
        c.execute("""INSERT INTO review_revisions(draft_id,revision,payload,reservation_bytes)
                     VALUES (%s,0,%s,%s)""", (draft, Jsonb(snapshot), 8192 + len(canonical(snapshot))))
        reserve_import(c, job, profile)
        check_mutation()
        return dict(job_id=job, proposal_id=proposal, draft_id=draft)


def main():
    raise SystemExit(joint.HISTORICAL_SOURCES_UNAVAILABLE)


if __name__ == "__main__":
    main()
