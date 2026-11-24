"""Descriptive equipment metadata with server-controlled human provenance."""


def validation_errors(equipment):
    """Validate exact descriptive fields without trimming, merging or inferring."""
    errors = []

    def error(field, reason):
        if len(errors) < 64:
            errors.append(dict(field='equipment_review' + field, reason=reason))

    if not isinstance(equipment, dict):
        return [dict(field='equipment_review', reason='Equipment review object required')]
    required = {'version', 'included_equipment', 'exclusions', 'categories'}
    if not required <= set(equipment) or set(equipment) - required - {'origin'}:
        error('', 'Declare exactly version, included_equipment, exclusions and categories; origin is server-controlled')
    if equipment.get('version') != 'equipment-review-v1':
        error('.version', 'Use recognized equipment-review-v1 contract')
    for field in ('included_equipment', 'exclusions'):
        value = equipment.get(field)
        if not isinstance(value, str) or len(value) > 4000:
            error('.' + field, 'Use plain text of at most 4000 characters')
    categories = equipment.get('categories')
    if not isinstance(categories, list):
        error('.categories', 'Categories must be a list')
        return errors
    if len(categories) > 16:
        error('.categories', 'Use at most 16 categories')
    seen = set()
    for index, category in enumerate(categories):
        path = f'.categories[{index}]'
        if not isinstance(category, str) or not category.strip() or len(category) > 200:
            error(path, 'Use nonblank plain text of at most 200 characters')
        elif category in seen:
            error(path, 'Category labels must be distinct')
        else:
            seen.add(category)
    return errors


def with_origin(equipment, previous, actor, reason):
    """Retain provenance for unchanged wording; caller origins never confer proof."""
    content = {key: value for key, value in equipment.items() if key != 'origin'}
    prior = ({key: value for key, value in previous.items() if key != 'origin'}
             if previous is not None else None)
    origin = (previous['origin'] if content == prior else
              dict(kind='human', actor=actor, reason=reason, source_is_proof_of_edit=False))
    return dict(content, origin=origin)
