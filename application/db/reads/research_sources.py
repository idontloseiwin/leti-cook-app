"""Bounded discovery of user-defined source identities, not YouTube metadata."""
from ..base.repository import DatabaseRepository

# Identifiers and SQL fragments are closed definitions, never client values.
SOURCES = {
    'folder': ('public.folders', 'id::text', 'TRUE'),
    'label': ('public.channel_labels', 'label_key', 'TRUE'),
    'collection': ('public.saved_objects', 'id::text', "kind='collection' AND deleted_at IS NULL"),
}


class ResearchSourcesRepository(DatabaseRepository):
    def search(self, kind, text, limit, after=''):
        if kind not in SOURCES or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('invalid_source_search')
        table, identity, predicate = SOURCES[kind]
        rows = self.database.read_rows('research_source_search',
            f'SELECT {identity} AS id,name FROM {table} WHERE {predicate} '
            f'AND {identity}>%s AND strpos(lower(name),lower(%s))>0 ORDER BY {identity} LIMIT %s',
            (after, text, limit + 1), max_rows=limit + 1)
        items = [{'id': str(row['id']), 'name': row['name'], 'scope': {'kind': kind, 'id': str(row['id'])}}
                 for row in rows[:limit]]
        return {'items': items, 'next_cursor': items[-1]['id'] if len(rows) > limit else None,
                'consistency': 'current', 'origin': 'user', 'kind': kind}
