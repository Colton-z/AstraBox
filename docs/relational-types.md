# Relational schema representations

The shared SQLAlchemy models retain PostgreSQL's text keys, timezone-aware
timestamps, JSONB, text arrays and BYTEA. The type declarations in
`astrabox/persistence/sql/types.py` also describe MySQL 8.4 representations.
These declarations are schema groundwork, not complete MySQL backend support.
Repository expressions, schema initialization and physical coordination still
require a compatible implementation and live acceptance. In particular, the
MySQL Agent/Assistant Environment FKs require shared transactional tombstone
cleanup in the Environment deletion operation, described below.
No OceanBase or other MySQL-compatible service support follows from MySQL DDL
compilation.

## Values

`UTCDateTime` uses PostgreSQL TIMESTAMP WITH TIME ZONE and MySQL DATETIME(6).
For MySQL, aware values normalize to UTC before binding and returned values
carry UTC. Naive values represent UTC. The provider must establish UTC database
sessions; database clock expressions use the shared `func.now()` compiler.
There is no application clock substitution or automatic ON UPDATE timestamp.

`payload_json()` keeps native JSON, with Python `None` stored as SQL NULL.
`JSON.NULL` remains an explicit JSON null. Empty arrays and objects remain
distinct from absence. `StringList` uses PostgreSQL text arrays and MySQL JSON
arrays, preserving order, duplicates and null elements. Its MySQL bind check
and JSON_SCHEMA_VALID constraint reject other element types. Service input
validation can require strings without null elements. The MySQL empty-list
default uses a native JSON_ARRAY() expression; empty objects use JSON_OBJECT().
Large-text constant defaults are parenthesized expressions, as MySQL requires.
Shared Python float annotations use MySQL DOUBLE rather than its single-width
FLOAT, retaining PostgreSQL FLOAT compilation.

Unindexed text mapped through `payload_text()` uses MySQL LONGTEXT; native
archive bytes mapped through `binary_payload()` use LONGBLOB. The snapshot
repository's 256 KiB chunks exceed MySQL BLOB's capacity. These physical types
do not override driver packet or server resource limits.

## MySQL key limits

`KeyText` retains PostgreSQL Text without a new length restriction. On MySQL it
uses VARBINARY containing the complete UTF-8 value: case, trailing spaces and
all bytes remain significant. It does not truncate, normalize or replace keys
with hashes. The following are backend physical allocations, not product-wide
identity limits. Refusing an otherwise valid identity still restricts the
reference backend's capabilities; these allocations do not establish that
every developer's external or native identities fit.

| Fields declared in this slice | Maximum UTF-8 bytes | Current producers and callers |
| --- | ---: | --- |
| Session, input and terminal command IDs; reader owner | 128 | Session/input IDs are UUID strings; terminal IDs are `tcmd_` plus 32 hex characters. Reader owners are hostname:PID:8hex. Explicit/imported identities can be longer. |
| Environment name and references to Secret keys | 512 | Environment names have no global maximum. Generated credential IDs are `vcr_` plus 32 hex characters; Environment secret keys contain the field name and a UUID suffix. |
| Secret scope | 768 | Includes `environment/` plus the Environment name, or a vault/deployment identity. |
| Secret key | 512 | Credential/field identities, including generated secret keys. |
| Session input client message ID | 1024 | The API accepts caller-supplied strings without a global maximum; generated input UUIDs are a common default. |
| Input state and terminal status | 32 | Current lifecycle codes, including `delivered` and `interrupted`, fit this allocation. |
| Other generated entity IDs and matching FK fields | 128 | Agent, Assistant, Deployment, Artifact, Vault, sandbox, slot, control, card, credential, reservation and token identifiers. Current UUID/prefixed producers fit; arbitrary imported or provider identities may exceed this size. |
| User, organization, owner and execution identities | 512 | External identity providers supply strings without a universal byte bound. |
| Catalog provider / catalog item / connector ID / folded name | 128 / 1024 / 512 / 512 | External providers can have longer IDs. Unicode casefold can expand an Agent name. |
| Channel conversation, deduplication, alias, participant, reference and outbox response keys | 1024 | Native provider identities and derived routing/response keys have no universal maximum. |
| Channel message timestamp / binding revision | 128 / 64 | Provider timestamp text / platform ISO timestamp. |
| Artifact document path and collection | 1024 each | Slash-path syntax has no global byte limit; deep or multibyte paths may exceed this allocation. PG retains its explicit C collation. |
| Transcript project / native Session / subpath / append identity | 1024 / 512 / 1024 / 128 | Native suppliers do not universally promise these bounds. Oversized values must refuse explicitly, never silently omit native history. |
| Credential match URL / secret name | 2048 / 512 | Valid URLs and names can exceed the reference allocation. |
| Snapshot owner and snapshot digests / archive kind | 64 / 32 | The snapshot repository uses SHA256 hex identities and a platform archive enum. |
| Artifact budget / viewer / task reference | 1024 / 512 / 1024 | Current budgets/viewers use existing bounded HMAC identities; imported or caller-derived values may be longer. |
| Backend / pool / profile / job / profile marker / reservation key | 128 / 512 / 1024 / 512 / 512 / 512 | Provider/runtime strings without universal supplier length guarantees. |

The model declarations give the exact allocation for each column and matching
foreign key. They do not define a universal provider identity format. A
different schema representation needs independent review and acceptance.

Matching foreign keys use the same binary representation and length. The
Secret composite key uses at most 1280 bytes; the input deduplication key uses
1152 bytes. The selected tables specify InnoDB DYNAMIC row format; MySQL must
provide its 3072-byte index budget (16 KiB pages). Strict SQL mode is required
for database writes; it does not replace application-side validation.

Typed SQLAlchemy values call `KeyText` validation before sending their
statement. Importers must call `validate_backend_keys(table, rows, dialect)`
over the complete concrete batch before its first write. INSERT FROM SELECT
must type each key literal with the destination column's type, or validate its
concrete values before writing. Raw SQL and untyped expressions bypass these
bind checks and cannot claim the same refusal guarantee.

The validator uses the selected dialect's adapted bind processor. Independent
providers can supply schema and type/compiler adaptations through SQLAlchemy's
extension points; they must preserve complete key equality, constraints and
binding behavior together. The reference widths are not a new global provider
API or a requirement to use a particular driver.

Source-valid PostgreSQL values can exceed these allocations. For example,
`"研发" * 86` is a 516-byte nonempty Environment name; an external client message
ID may exceed 1024 bytes; an explicit Session ID may exceed 128 bytes. Reject
such values for MySQL before writing or importing them. Do not rename or
truncate them, and do not impose the MySQL limits on PostgreSQL.

The actual producers are in `session_lifecycle/service.py`,
`session_runtime/service.py` and `session_runtime/host.py` under
`astrabox/core/service/orchestrator/`; credential IDs are generated in
`astrabox/persistence/repository/vault_repository.py`. External request types
are in `astrabox/api/routes/sessions.py`.

## Ordering and conditional uniqueness

Session input order remains database-generated. PostgreSQL keeps its non-PK
Identity(always=True). MySQL uses non-PK AUTO_INCREMENT with its required
inline, nonunique sequence index. Explicit sequence assignments are a shared
repository/import responsibility: MySQL AUTO_INCREMENT does not enforce
PostgreSQL's GENERATED ALWAYS restriction. Read the generated row through its
business key on the same transaction; do not use unsupported MySQL RETURNING.

The terminal's one-running-command constraint uses the same status predicate
on both dialects. PostgreSQL retains its partial unique index. MySQL creates
a STORED generated marker that is 1 for a running command and NULL otherwise,
plus UNIQUE(session_id, marker). The generated column and constraint are in
the same CREATE TABLE statement. The marker is not a shared projected field.
Inactive rows remain unrestricted; active duplicates fail atomically.
The MySQL input-state index includes closed rows rather than relying on an
unsupported partial index. It is a performance index, not a uniqueness rule.

## Environment references

The Agent and Assistant live-Environment CHECKs remain enforced on both
dialects. PostgreSQL retains ON DELETE SET NULL. Exactly those two FKs compile
as ON DELETE RESTRICT on MySQL because its CHECK evaluator cannot validate
storage-engine SET NULL actions. Other FK actions and logical names stay
unchanged. See the [exact foreign-key validation](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_table.cc#L6325-L6342).

The shared Environment deletion operation must hold the Environment lock,
refuse live or compute-bearing references, clear only eligible deleted
Agent/Assistant references, delete the Environment and purge its secrets on
the same transaction. The MySQL schema requires that cleanup recipe; schema
compilation alone does not provide it. RESTRICT refuses surviving references,
and CHECK prevents a live definition from having no Environment. A raw SQL
DELETE with remaining tombstone references therefore differs from PostgreSQL;
it must not be presented as equivalent to the shared deletion operation.

These choices use SQLAlchemy 2.0.52's native types and compiler hooks. Reference
contracts: [SQLAlchemy UTC types](https://docs.sqlalchemy.org/en/20/core/custom_types.html#store-timezone-aware-timestamps-as-timezone-naive-utc),
[MySQL 8.4 index limits](https://dev.mysql.com/doc/refman/8.4/en/innodb-limits.html),
[generated columns](https://dev.mysql.com/doc/refman/8.4/en/create-table-generated-columns.html)
[JSON schema validation](https://dev.mysql.com/doc/refman/8.4/en/json-validation-functions.html)
and [expression defaults](https://dev.mysql.com/doc/refman/8.4/en/data-type-defaults.html).
