#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Static parse check for sql/001_monitoring_map.sql.

This is NOT a substitute for applying the migration to a real Postgres --
there is no psql or Docker on this machine, so the migration has not been
executed anywhere. What this does verify:

  * every statement parses as PostgreSQL
  * declared tables, types and constraints are the ones the docs promise
  * foreign keys point at tables that exist in this file

Run:  PYTHONUTF8=1 python tools/check_sql.py
"""

import io
import os
import re
import sys

try:
    import sqlglot
    from sqlglot import exp
except ImportError:
    sys.stderr.write("FATAL: sqlglot is not installed. Run: pip install sqlglot\n")
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_PATH = os.path.join(ROOT, "sql", "001_monitoring_map.sql")

with io.open(SQL_PATH, encoding="utf-8", newline="") as fh:
    sql_text = fh.read()

failures = []
lines = []


def check(name, condition, detail=""):
    line = "%-6s %s" % ("PASS" if condition else "FAIL", name)
    if detail:
        line += "  -- " + detail
    lines.append(line)
    if not condition:
        failures.append(name)
    return condition


# --- parse -----------------------------------------------------------------
try:
    statements = sqlglot.parse(sql_text, dialect="postgres")
    check("file parses as PostgreSQL", True,
          "%d statements" % len([s for s in statements if s is not None]))
except Exception as exc:
    check("file parses as PostgreSQL", False, str(exc))
    statements = []

# --- inventory -------------------------------------------------------------
tables = set()
for stmt in statements:
    if isinstance(stmt, exp.Create) and (stmt.kind or "").upper() == "TABLE":
        tables.add(stmt.this.this.name if stmt.this.this else "")

expected_tables = {
    "monitoring_topics", "monitoring_platforms", "monitoring_signals",
    "monitoring_queries", "monitoring_runs", "monitoring_hits",
    "stop_rule_drops", "heartbeat_reports", "triage_transitions",
}
check("all 9 tables declared", tables == expected_tables,
      "missing: %s / unexpected: %s"
      % (sorted(expected_tables - tables), sorted(tables - expected_tables)))

types = set(re.findall(r"CREATE\s+TYPE\s+(\w+)\s+AS\s+ENUM", sql_text, re.I))
expected_types = {
    "monitoring_platform", "monitoring_category", "cadence_class",
    "triage_decision", "hit_state", "signal_method",
}
check("all 6 enum types declared", types == expected_types,
      "missing: %s" % sorted(expected_types - types))

# --- foreign keys resolve --------------------------------------------------
refs = set(re.findall(r"REFERENCES\s+(\w+)", sql_text, re.I))
unresolved = refs - tables
check("every REFERENCES target exists in this file", not unresolved,
      "unresolved: %s" % sorted(unresolved))

# --- constraints the docs promise -----------------------------------------
promised_constraints = [
    ("topic_category_by_level", "subtopic must carry a category, top level must not"),
    ("score_within_bounds", "score stays inside -180..160"),
    # origin_present intentionally removed: it demanded query_id or signal_key,
    # but an RSS item arrives straight from a feed with neither, and the very
    # first insert failed on it. A hit always has source_key, declared NOT NULL.
    ("scored_has_factors", "a scored hit must carry its factor breakdown"),
    ("dropped_has_reason", "a dropped hit must carry a reason"),
    ("answers_is_array", "heartbeat answers is a json array"),
    ("answers_count_ten", "heartbeat carries exactly ten answers"),
]
for name, meaning in promised_constraints:
    check("constraint %s present (%s)" % (name, meaning), name in sql_text)

# --- transaction safety ----------------------------------------------------
# Leading comments are stripped before looking for the transaction markers.
code_only = re.sub(r"--[^\n]*\n", "\n", sql_text).strip()
check("migration is wrapped in a transaction",
      code_only.startswith("BEGIN;") and code_only.endswith("COMMIT;"),
      "starts %r, ends %r" % (code_only[:8], code_only[-8:]))

check("origin_present is dropped, not re-created",
      "DROP CONSTRAINT IF EXISTS origin_present" in sql_text
      and "CONSTRAINT origin_present CHECK" not in sql_text,
      "an RSS hit has neither query_id nor signal_key; source_key is its origin")

# --- idempotency -----------------------------------------------------------
# The service restarts and re-applies the migration. Bare CREATE TYPE / CREATE
# TABLE would abort the second start, so every one of them must be guarded.
guarded_types = sql_text.count("EXCEPTION WHEN duplicate_object")
guarded_tables = sql_text.count("CREATE TABLE IF NOT EXISTS")
bare_tables = len(re.findall(r"CREATE\s+TABLE\s+(?!IF NOT EXISTS)", sql_text, re.I))
bare_indexes = len(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+ON\s", sql_text, re.I))

check("all 6 enum types are guarded against re-creation", guarded_types == 6,
      "%d guarded" % guarded_types)
check("all 9 tables use IF NOT EXISTS", guarded_tables == 9,
      "%d guarded, %d bare" % (guarded_tables, bare_tables))
check("no unnamed indexes (IF NOT EXISTS needs explicit names)",
      bare_indexes == 0, "%d bare" % bare_indexes)
check("migration is safe to apply twice",
      guarded_types == 6 and bare_tables == 0 and bare_indexes == 0)

# --- indexes ---------------------------------------------------------------
index_count = len(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX", sql_text, re.I))
check("indexes declared on the hot query paths", index_count >= 15,
      "%d indexes" % index_count)
check("url_hash is unique on monitoring_hits",
      re.search(r"CREATE\s+UNIQUE\s+INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+\w*\s*"
                r"ON\s+monitoring_hits\s*\(url_hash\)",
                sql_text, re.I | re.S) is not None)

lines.append("")
lines.append("NOTE   Postgres is not installed on this machine and Docker is not")
lines.append("       available, so this migration has NOT been applied anywhere.")
lines.append("       Static parse only. Apply on deployment with:")
lines.append("         psql -f sql/001_monitoring_map.sql")

lines.append("")
lines.append("checks run : %d" % sum(1 for l in lines if l.startswith(("PASS", "FAIL"))))
lines.append("failures   : %d" % len(failures))

out = "\n".join(lines) + "\n"
with io.open(os.path.join(ROOT, "tools", "sql-check-report.txt"),
             "w", encoding="utf-8", newline="\n") as fh:
    fh.write(out)

sys.stdout.write(out)
sys.exit(1 if failures else 0)
