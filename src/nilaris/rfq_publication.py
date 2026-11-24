"""Publish the two local, pinned PDFs through the bounded Ingestion contract."""
from hashlib import sha256

from nilaris.admission import AdmissionProfile, Ingestion
from nilaris.execution import ControlledSource
from nilaris.results import Effect, StageOutput, canonical


ALIAS_ASSOCIATION = 'pump-row-5-note-7'
ADAPTER = 'pump-rfq-pdfkit-v1'


def _observation(location, *, row=None, note_publication=None):
    locator = dict(location)
    if row is not None:
        locator['rfq_row'] = row
    if note_publication is not None:
        locator['source_job_id'], locator['source_proposal_id'] = note_publication
    presence = 'observed'
    if row and row['unresolved_reason']:
        presence = 'ambiguous'
    elif row and row['value'] == '':
        presence = 'blank'
    return dict(id=location['source_url'], locator=locator, kind='property' if row else 'note',
                original_content=location['passage'], value=row['value'] if row else location['passage'],
                unit=row['unit'] if row else None, source_type='pdf-text',
                presence=presence)


def _output(effects, inspected):
    return StageOutput('partial', dict(inspected=inspected, unread=['Other PDF sections'],
                                      limits=['Page 1 pump table and referenced note only']),
                       tuple(effects), adapter=ADAPTER)


def _publish_pdf(ingestion, source, scope, output):
    revision = source.manifest['sha256']
    filename = source.manifest['filename']
    member = dict(member_key=filename, connector='synthetic', source_id=filename,
                  source_revision=revision, document_id='pump-rfq/' + filename,
                  case_id='pump-rfq/' + scope, membership_revision=scope, product_type='Pump',
                  processing_contract='fixture-v1', reference_set='none', selection_policy='none',
                  intent_id='ordinary', input_bytes=len(source.pdf), media_type='application/pdf',
                  identity='resolved')
    # The legacy finite-source contract uses the synthetic connector. Capture the
    # pinned original bytes and identify the actual PDFKit adapter on publication.
    receipt = ingestion.offer('pump-rfq/' + scope, filename, [member])
    members = receipt.get('members') or []
    if not members or not members[0].get('job_id'):
        raise OSError('Pinned PDF admission unavailable.')
    job_id = members[0]['job_id']
    state = ingestion.status(job_id)
    if state.get('state') == 'acquiring':
        claim = ingestion.claim(job_id, state['revision'])
        if claim.get('claimed'):
            source_reader = ControlledSource({filename: (revision, source.pdf)})
            ingestion.capture(job_id, claim, source_reader)
        state = ingestion.status(job_id)
    if state.get('content_pin') != revision:
        raise OSError('Pinned PDF capture unavailable.')
    if state.get('state') == 'queued':
        claim = ingestion.claim(job_id, state['revision'])
        if claim.get('claimed'):
            ingestion.publish(job_id, claim, output)
        state = ingestion.status(job_id)
    initial = next((p for p in state.get('results', []) if p['kind'] == 'initial'), None)
    if initial is None:
        raise OSError('Pinned PDF publication unavailable.')
    return job_id, initial['id']


def seed_pump_review(config, bundle, schema, *, scope):
    """Return the immutable table proposal, reusing admitted jobs and captures."""
    rows, locations = schema
    if bundle.note is None:
        raise ValueError('Both pinned PDFs are required for whole-Schema review.')
    for source in (bundle, bundle.note):
        if sha256(source.pdf).hexdigest() != source.manifest['sha256']:
            raise ValueError('Pump PDF bytes differ from their source revision.')
    scope_pin = sha256(canonical([ADAPTER, scope, bundle.manifest['sha256'],
                                  bundle.note.manifest['sha256']]).encode()).hexdigest()
    alias_row, = (row for row in rows if 'alias' in row)
    evidence = alias_row['alias']['initial']['evidence']
    note = evidence['note']
    if (note['sha256'] != bundle.note.manifest['sha256'] or
            evidence['table']['source_url'] != alias_row['source_url'] or
            evidence['table']['sha256'] != bundle.manifest['sha256']):
        raise ValueError('Alias observations differ from the pinned PDFs.')
    if locations[note['prefix']].location != note:
        raise ValueError('The note observation no longer matches its source location.')
    effects = []
    for row in rows:
        number = row['row']
        location = locations[row['source_url'].split('/#')[0]].location
        if (location['sha256'] != bundle.manifest['sha256'] or
                location['source_url'] != row['source_url'] or location['row'] != number):
            raise ValueError('Pump row source revision or location changed.')
        effects.append(Effect('observation', number, _observation(location, row=row)))
        if not row['unresolved_reason']:
            candidate = dict(id='row-' + str(number), label=row['attribute'],
                             value_type='decimal' if row['unit'] else 'text',
                             unit=row['unit'], domain='technical', observation_ids=[row['source_url']],
                             requirements='unspecified')
            effects.append(Effect('candidate', number, candidate))
    ingestion = Ingestion(config.database, AdmissionProfile.l1(), monitor_path=config.monitor_path)
    note_output = _output([Effect('observation', 0, _observation(note))], [note['source_url']])
    note_publication = _publish_pdf(ingestion, bundle.note, scope_pin, note_output)
    effects.append(Effect('observation', 0, _observation(note, note_publication=note_publication)))
    association = dict(id=ALIAS_ASSOCIATION, observation_ids=[alias_row['source_url'], note['source_url']],
                       candidate_ids=['row-' + str(alias_row['row'])], method=ADAPTER,
                       rationale='Proposed Product Type Alias across the pinned row and note.',
                       disposition='unresolved')
    effects.append(Effect('association', 0, association))
    output = _output(effects, [row['source_url'] for row in rows] + [note['source_url']])
    return _publish_pdf(ingestion, bundle, scope_pin, output)
