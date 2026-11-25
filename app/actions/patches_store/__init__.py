"""The patch store: PostgreSQL persistence for patches and their change feed.

- `sql/` holds the schema.
- `db` opens connections from the runner's settings.
- `store` holds the queries. Every function takes a connection and leaves the
  transaction to its caller.
- `models` holds the rows those queries take and return.
"""
