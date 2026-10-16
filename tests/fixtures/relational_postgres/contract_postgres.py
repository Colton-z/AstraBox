"""Real PostgreSQL through an external distribution, without alternate DB claims."""

from astrabox.providers.relational_postgres import PostgresPersistenceProvider


class ContractPostgresPersistenceProvider(PostgresPersistenceProvider):
    pass
