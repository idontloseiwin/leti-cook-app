"""Versioned optional-field policy; pure user work, never execution grants.

Only modes whose acquisition/write/read/cleanup path is implemented are offered.
The existing fields retain their collection behavior. Display column choices are
separate presentation preferences, not a storage or security switch.
"""
from copy import deepcopy
from dataclasses import dataclass
import json

FORMAT_VERSION = 1
DEFAULT_PROFILE = "public-current-v1"
LIFECYCLE_PROFILE = "public-696h-v1"
SOURCE = "public.videos.current.v1"
TYPES = ("long", "short", "stream")
FIELDS = ("video.description", "video.paid_promotion")
MODES = ("off", "current")
MAX_BYTES = 16384

@dataclass(frozen=True)
class OptionalField:
    id: str
    title: str
    api_path: str
    request_behavior: str
    max_bytes: int
    clock: str

DEFINITIONS = (
    OptionalField("video.description", "Description", "videos.snippet.description",
                  "Shared snippet response is discarded when disabled", 5000, "description_checked_at"),
    OptionalField("video.paid_promotion", "Declared paid promotion", "videos.paidProductPlacementDetails.hasPaidProductPlacement",
                  "Part requested only when enabled for a collected format", 1, "paid_promotion_checked_at"),
)

class FieldPolicyError(ValueError):
    def __init__(self, code="invalid_field_preferences", status=422):
        self.code, self.status = code, status
        super().__init__(code)

def defaults():
    return {"version": FORMAT_VERSION, "defaults_profile": DEFAULT_PROFILE,
            "lifecycle_profile": LIFECYCLE_PROFILE, "workspace": {}, "sources": {},
            "description_setup": "pending"}

def canonical(value):
    try:
        return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode("utf-8")
    except (TypeError,ValueError,UnicodeError,RecursionError):
        raise FieldPolicyError() from None

def exact(value, keys):
    if type(value) is not dict or set(value)!=set(keys):raise FieldPolicyError()

def modes(value):
    if type(value) is not dict or set(value)-set(FIELDS):raise FieldPolicyError()
    if any(type(mode) is not str or mode not in MODES for mode in value.values()):raise FieldPolicyError()
    return dict(sorted(value.items()))

def normalize(value):
    if len(canonical(value))>MAX_BYTES:raise FieldPolicyError("field_preferences_too_large")
    exact(value,defaults())
    if type(value["version"]) is not int or value["version"]!=FORMAT_VERSION:raise FieldPolicyError("unsupported_field_preferences")
    if value["defaults_profile"]!=DEFAULT_PROFILE or value["lifecycle_profile"]!=LIFECYCLE_PROFILE:
        raise FieldPolicyError("unsupported_lifecycle_profile")
    if value["description_setup"] not in ("pending","later","chosen"):raise FieldPolicyError()
    source=value["sources"]
    if type(source) is not dict or set(source)-{SOURCE}:raise FieldPolicyError()
    result=deepcopy(value);result["workspace"]=modes(value["workspace"])
    if SOURCE in source:
        exact(source[SOURCE],("fields","types"));typed=source[SOURCE]["types"]
        if type(typed) is not dict or set(typed)-set(TYPES):raise FieldPolicyError()
        result["sources"]={SOURCE:{"fields":modes(source[SOURCE]["fields"]),"types":{kind:modes(typed[kind]) for kind in sorted(typed)}}}
    return result

def effective(document, video_type):
    if video_type not in TYPES:raise FieldPolicyError()
    doc=normalize(document)
    values=dict.fromkeys(FIELDS,"off");values.update(doc["workspace"])
    source=doc["sources"].get(SOURCE,{"fields":{},"types":{}})
    values.update(source["fields"]);values.update(source["types"].get(video_type,{}))
    return values

def capture(document, revision):
    if type(revision) is not int or not 1<=revision<=9007199254740991:raise FieldPolicyError()
    return {"version":1,"revision":revision,"effective":{kind:effective(document,kind) for kind in TYPES}}

def validate_capture(value):
    exact(value,("version","revision","effective"))
    if type(value["version"]) is not int or value["version"]!=1 or type(value["revision"]) is not int or not 1<=value["revision"]<=9007199254740991:raise FieldPolicyError()
    exact(value["effective"],TYPES)
    for item in value["effective"].values():
        exact(item,FIELDS);modes(item)
    return deepcopy(value)

def request_fields(proof):
    proof=validate_capture(proof)
    return frozenset(field for field in FIELDS if any(proof["effective"][kind][field]=="current" for kind in TYPES))

def purge_fields(value,document):
    if type(value) is not list or len(value)>len(FIELDS) or any(type(field) is not str or field not in FIELDS for field in value) or len(set(value))!=len(value):raise FieldPolicyError()
    # A global immediate purge is only supported after every format disables
    # that field. A per-format off always retains the normal expiry cleanup.
    for field in value:
        if any(effective(document,kind)[field]!="off" for kind in TYPES):raise FieldPolicyError("purge_requires_disabled_field")
    return sorted(value)


def valid_description(value):
    if type(value) is not str or '\x00' in value:return False
    try:return len(value.encode('utf-8'))<=5000
    except UnicodeError:return False


def normalize_observation(value):
    if type(value) is not dict or set(value)!={'proof','values'}:raise FieldPolicyError()
    proof=validate_capture(value['proof']);values=value['values']
    if type(values) is not dict or set(values)!=set(request_fields(proof)):raise FieldPolicyError()
    description=values.get('video.description');paid=values.get('video.paid_promotion')
    if description is not None and not valid_description(description):raise FieldPolicyError()
    if paid is not None and type(paid) is not bool:raise FieldPolicyError()
    return deepcopy(value)
