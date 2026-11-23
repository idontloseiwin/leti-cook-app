"""Bounded user-facing consequences of a field policy change."""
from research_field_policy import normalize, effective, FIELDS, TYPES

def dependencies(definition):
    found=set();stack=[definition]
    while stack:
        item=stack.pop()
        if isinstance(item,dict):stack.extend(item.values())
        elif isinstance(item,list):stack.extend(item)
        elif isinstance(item,str) and item in FIELDS:found.add(item)
    return found

def preview(repository, document):
    document=normalize(document);current=repository.read();storage,definitions=repository.preview_inputs()
    disabled={field for field in FIELDS if any(current['effective'][kind][field]=='current' and effective(document,kind)[field]=='off' for kind in TYPES)}
    affected=[{'id':str(row['id']),'name':row['name'],'fields':sorted(dependencies(row['conditions'])&disabled)}
              for row in definitions if dependencies(row['conditions'])&disabled]
    count=int(storage['stored_videos']);sample=storage['sampled_description_bytes']
    storage=dict(storage,stored_videos=count,
        estimated_description_payload_bytes=None if sample is None else int(count*sample),
        maximum_description_and_folded_bytes=count*20000,
        estimate_basis='retained_sample_up_to_1000' if sample is not None else 'maximum_field_size_only')
    return dict(expected_revision=current['revision'],document=document,storage=storage,affected_saved=affected,
        current_collection='Existing schedules collect current values; no backfill is started.',
        gaps='Disabled periods cannot be reconstructed. Re-enabling waits for the next metadata refresh.',
        expiry='Hidden immediately when disabled; retained values still expire after 29 days.',
        backup='Choices are portable; imported choices require review before activation.')
