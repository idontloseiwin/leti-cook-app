"""Selected source columns shared by SQL projection and response shaping."""
from . import growth
from .operations import FIELD_OPERATIONS
from .registry import FIELDS

RAW, TEXT, INSTANT = "raw", "text", "instant"


def field_specs(request):
    """Facts of each selected field, computed once per call."""
    specs = []
    growth_carrier = growth.evidence_carrier(request.select)
    for field_id in request.select:
        field = FIELDS[field_id]
        column = ({"video.id": "video_id", "channel.id":"channel_id", "video.channel_id": "channel_id", "video.type": "video_type"}.get(field_id)
                  or (field_id.replace('.', '_') if field_id.startswith(('channel.', 'observation.')) else field_id.removeprefix('video.')))
        core = field.clock_family in {"membership","scope"}
        observation = field_id in FIELD_OPERATIONS
        # The five growth cells of a row share one clock pair and evidence object.
        growth_cell = field_id in growth.FIELDS
        evidence_column = growth.EVIDENCE_PREFIX if growth_cell else column
        clock_key = (evidence_column + "_observed_at") if observation else {"scope":"identity_clock", "membership":"identity_clock", "counters":"checked_at", "metadata":"metadata_checked_at",
                     "channel_profile":"channel_metadata_checked_at", "channel_country":"country_collected_at",
                     "description":"description_checked_at", "paid_promotion":"paid_promotion_checked_at"}[field.clock_family]
        # Without its own expiry a clock is current for HOT_DATA_WINDOW (None).
        expiry_key = evidence_column + "_expires_at" if observation else "identity_expiry" if core else None
        base = {"origin": field.origin, "source_paths": list(field.api_paths),
                "method": "tracked_channel_reference_v1" if field_id=="channel.id" else "canonical_membership_v1" if core else "reported_current_v1",
                "definition_epoch": None}
        if field_id == 'video.thumbnail_url':
            base['method'] = 'youtube_hqdefault_reference_v1'
        writes = (RAW if field.value_type in {"boolean", "text_array"} else
                  INSTANT if field.value_type == "utc_instant" else TEXT)
        specs.append((field_id, column, column + "_state" if field.nullable else None, writes, clock_key, expiry_key,
                      evidence_column + "_evidence" if observation else None, observation, growth_cell,
                      field_id == growth_carrier, FIELD_OPERATIONS.get(field_id), base))
    return specs


def frozen_video_columns(request):
    """Trusted registry column names, in a single order for SQL and its decoder."""
    names = dict.fromkeys(("video_id", "type_expiry"))
    for (field_id, value, state, _, clock, expiry, evidence, *_) in field_specs(request):
        names.update(dict.fromkeys(key for key in (value, state, clock, expiry, evidence) if key))
        if field_id == "video.type":
            names.update(dict.fromkeys(("type_method", "type_rule_version", "type_threshold")))
    return tuple(names)
