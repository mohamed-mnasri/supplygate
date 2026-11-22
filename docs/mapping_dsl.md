# Mapping spec reference

A mapping spec says how one supplier feed becomes rows of `canonical_offers`. It is YAML,
validated by Pydantic (`supplygate.mapping.spec.MappingSpec`) and compiled to one DuckDB
`SELECT` (`supplygate.mapping.compile_spec`). A spec contains no SQL and no code: every
operation comes from the closed list below, every argument is a literal, and every
identifier and literal is quoted by the compiler. An LLM proposing a spec can only pick
operations and arguments.

## Shape

```yaml
version: 1
supplier: de_csv              # lowercase id, [a-z0-9_]
category: null                # optional, informational
source:
  format: csv                 # csv | json
  encoding: utf-8             # utf-8 | utf-16 | latin-1
  delimiter: ";"              # csv only
  quote: '"'
  header: true
  skip_rows: 0
  record_path: data.items     # json only: dotted path to the record array
filter_rows:                  # RISKY, optional
  keep_if:
    - {column: Status, op: ne, value: EOL}
columns:
  - target: price_eur         # canonical column
    source: Preis             # or  sources: [A, B] + sep: " "   or  const: de_csv
    when:                     # optional: value only where all conditions hold, else NULL
      - {column: Warengruppe, op: eq, value: Prozessoren}
    transforms:
      - parse_number: {decimal: ",", thousands: "."}
```

For JSON feeds, `source` names are dotted paths inside one record (`pricing.amount_cents`).
`FeedProfile.source_options()` from the profiler produces a `source:` block.

## Transforms

Applied in order. Short forms: `- trim`, `- scale: 0.01`, `- cast: INTEGER`,
`- null_if: "N/A"`. Long form: `- {op: scale, factor: 0.01}`.

| op | arguments | result | strict |
|---|---|---|---|
| `trim` | | text, strips whitespace incl. NBSP | |
| `lower`, `upper` | | text | |
| `replace` | `old`, `new` (literal, not regex) | text | |
| `regex_extract` | `pattern`, `group` (default 1) | text | yes |
| `parse_number` | `decimal` (`.`/`,`), `thousands` | DECIMAL(38,10) | yes |
| `scale` | `factor` (non-zero) | number | yes |
| `convert_unit` | `from`, `to` (Hz…GHz, B…TB binary, W, kW, mm, cm, in, g, kg, lb) | number | yes |
| `map_values` | `mapping`, `default` (`error`/`keep`/`null`), `case_sensitive` | mapped value | if `default: error` |
| `parse_date` | `format` (strptime, e.g. `%d.%m.%Y`) | DATE | yes |
| `cast` | `type` | type | yes |
| `cast_loose` | `type` | type | **RISKY**, never reports |
| `null_if` | `values` (compared trimmed) | same | **RISKY** |
| `default_value` | `value` | same | **RISKY** |

Allowed types: VARCHAR, INTEGER, BIGINT, DOUBLE, BOOLEAN, DATE, TIMESTAMP, DECIMAL(p,s).
Regexes are checked with Python `re` and must also be valid RE2 (no lookaround, no
backreferences) and must not match the empty string.

Specs are read with YAML 1.2 booleans: only `true`/`false` are booleans, so code-table
values like `yes`, `no`, `NO` or `on` stay strings.

## Strict steps and `_sg_errors`

A strict step never fails the query. If a non-blank input turns into NULL, the row gets
`"<target>:<op>"` in `_sg_errors` (the first failing step per column). After the last
transform, every column is cast to its canonical type with the same rule
(`<target>:cast_target`), so a forgotten `parse_number` on `1.299,50` is reported, not
silently loaded as NULL. Blank inputs are missing values, not errors.

The compiler output has `_sg_row_id` (1-based record number, for lineage and the
conservation check), the canonical columns in schema order, and `_sg_errors`. The loader
quarantines rows with errors; see [gates.md](gates.md).

## Risky operations

`filter_rows`, `default_value`, `null_if` and `cast_loose` can make bad data look clean:
dropping rows, inventing values, hiding values, swallowing failed casts. They are legal
because real feeds need them (e.g. `null_if: "N/A"`), but `MappingSpec.risky_ops()` lists
every use with its location, so the router can send such specs to review and the
reward-hacking experiment can measure whether the gates catch them. The compiler applies
them exactly as written; it does not try to detect abuse.

`filter_rows` keeps rows where all conditions hold. Conditions: `eq`, `ne`, `lt`, `le`,
`gt`, `ge` (numeric when `value` is a number), `in`, `not_in`, `contains`, `is_null`,
`not_null`. A condition on a NULL value is unknown and drops the row.

`when` on a column uses the same conditions. It is meant for feeds whose generic columns
change meaning per category (the legacy ERP's `A1`..`A7`). It is not listed as risky: it
only produces NULLs, and a strict step never sees the rows it excludes.

## Reference specs

`specs/dev/{de_csv,fr_csv,uk_csv}.yaml` are hand-written mappings for the dev suppliers.
`tests/test_reference_specs.py` runs them on freshly generated feeds and checks every cell
against `ground_truth.csv` (non-benign corruptions must come through as written).

Known gaps, for the held-out suppliers: the JSON API keeps specs as a list of
`{name, value, unit}` objects, which a dotted path cannot select by name; and free-text
`SPEC_TXT` needs one `regex_extract` per attribute, which works only while the text keeps
a fixed layout.

## Other helpers

- `spec.to_yaml()`: stable long-form YAML for diffs and the mapping memory.
- `spec.lineage()`: canonical column to the source columns it reads.
- `run_mapping(con, spec, path, schema)`: compiles, checks CSV headers
  (`MissingSourceColumns`), and returns a DuckDB relation.
