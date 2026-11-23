"""Atomic user field policy and bounded command replay; no provider I/O."""
from hashlib import sha256
from uuid import UUID
from psycopg.types.json import Jsonb
from research_field_policy import (FieldPolicyError, canonical, normalize, effective,
                                   purge_fields, FIELDS, TYPES)
from ..base.postgres import Operation

_READ = "SELECT revision,document,pending_import,purge_fields,updated_at FROM readable.research_preferences WHERE workspace_id=%s"
_LOCK = "SELECT revision,document,pending_import,purge_fields,updated_at FROM public.research_preferences WHERE workspace_id=%s FOR UPDATE"
_LOOKUP = "SELECT fingerprint,applied_revision FROM operator_private.research_preference_commands WHERE workspace_id=%s AND request_id=%s::uuid"
_UPDATE = "UPDATE public.research_preferences SET document=%s::jsonb,pending_import=NULL,revision=revision+1,purge_fields=%s::text[],updated_at=clock_timestamp() WHERE workspace_id=%s RETURNING revision,document,pending_import,purge_fields,updated_at"
_RECEIPT = "INSERT INTO operator_private.research_preference_commands(workspace_id,request_id,fingerprint,applied_revision) VALUES(%s,%s::uuid,%s,%s)"
_PRUNE = "DELETE FROM operator_private.research_preference_commands WHERE workspace_id=%s AND request_id IN (SELECT request_id FROM operator_private.research_preference_commands WHERE workspace_id=%s ORDER BY created_at DESC,request_id DESC OFFSET 1000)"


def workspace(value):
    if value!='installation':raise FieldPolicyError('workspace_unavailable',404)
    return value


def state(row):
    if not isinstance(row,dict):raise FieldPolicyError('field_preferences_unavailable',503)
    document=normalize(row['document'])
    revision=row['revision']
    if type(revision) is not int or not 1<=revision<=9007199254740991:raise FieldPolicyError('field_preferences_unavailable',503)
    return dict(revision=revision,document=document,pending_import=normalize(row['pending_import']) if row.get('pending_import') is not None else None,purge_fields=purge_fields(row['purge_fields'],document),
                effective={kind:effective(document,kind) for kind in TYPES})


class ResearchPreferencesRepository:
    def __init__(self,database,workspace_id='installation'):
        self.database=database;self.workspace_id=workspace(workspace_id)

    def read(self):
        return state(self.database.read_row('research_preferences_read',_READ,(self.workspace_id,)))

    def apply(self,document,expected_revision,request_id,purge=()):
        document=normalize(document);purge=purge_fields(list(purge),document)
        if type(expected_revision) is not int or not 1<=expected_revision<9007199254740991:raise FieldPolicyError()
        try:
            identity=UUID(request_id)
            if identity.version!=4 or str(identity)!=request_id:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise FieldPolicyError() from None
        fingerprint=sha256(canonical(dict(document=document,expected_revision=expected_revision,purge=purge))).hexdigest()
        with self.database.transaction(Operation('research_preferences_apply',statement_timeout_ms=5000,total_timeout_ms=6000,lock_timeout_ms=500)) as tx:
            before=state(tx.fetch_one(_LOCK,(self.workspace_id,)))
            old=tx.fetch_one(_LOOKUP,(self.workspace_id,request_id))
            if old is not None:
                if old['fingerprint']!=fingerprint:raise FieldPolicyError('request_conflict',409)
                return dict(status='replayed',applied_revision=old['applied_revision'],preferences=before)
            if before['revision']!=expected_revision:raise FieldPolicyError('revision_conflict',409)
            pending=sorted(set(purge)|{field for field in before['purge_fields'] if all(effective(document,kind)[field]=='off' for kind in TYPES)})
            after=state(tx.returning_one(_UPDATE,(Jsonb(document),pending,self.workspace_id)))
            tx.execute(_RECEIPT,(self.workspace_id,request_id,fingerprint,after['revision']))
            tx.execute(_PRUNE,(self.workspace_id,self.workspace_id))
            return dict(status='applied',applied_revision=after['revision'],preferences=after)

    def preview_inputs(self):
        # Count is actual catalogue storage, not a promise of API coverage or a
        # newly triggered discovery. No description data leaves this query.
        data=self.database.read_row('research_preferences_estimate','SELECT * FROM readable.research_field_storage')
        # Definitions are user work, not YouTube data; fixed limit prevents an
        # unbounded "which saved investigations depend on this" traversal.
        definitions=self.database.read_rows('research_preferences_saved',"SELECT id,name,conditions FROM public.saved_objects WHERE kind='view' AND deleted_at IS NULL ORDER BY id LIMIT 1001",max_rows=1001)
        return data,definitions
