"""Capture field policy before provider I/O; provider text cannot supply proof."""
from copy import deepcopy
from research_field_policy import request_fields, validate_capture, valid_description, normalize_observation

PROOF_KEY = '_lic_optional_observation'

def observe(items, proof):
    proof=validate_capture(proof); requested=request_fields(proof)
    result=[]
    for item in items:
        if not isinstance(item,dict):
            result.append(item);continue
        item=deepcopy(item);item.pop(PROOF_KEY,None)
        snippet=item.get('snippet');snippet=snippet if isinstance(snippet,dict) else {}
        description=snippet.pop('description',None)
        paid=item.pop('paidProductPlacementDetails',None)
        values={}
        if 'video.description' in requested:
            values['video.description']=description if valid_description(description) else None
        if 'video.paid_promotion' in requested:
            value=paid.get('hasPaidProductPlacement') if isinstance(paid,dict) else None
            values['video.paid_promotion']=value if type(value) is bool else None
        item[PROOF_KEY]={'proof':proof,'values':values}
        result.append(item)
    return result

def payload(item):
    if PROOF_KEY not in item:return {}
    return {'optional_observation':normalize_observation(item[PROOF_KEY])}
