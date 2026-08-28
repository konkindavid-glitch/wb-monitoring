#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Consistency checker for the monitoring-map module.

Verifies that docs, YAML configs, JSON schemas and the SQL migration agree
with each other and with the source requirements.

Run:  PYTHONUTF8=1 python tools/validate.py

Console output is ASCII-only on purpose: the system code page on this machine
is cp1251 and Cyrillic in the console arrives as garbage. The full report,
including Russian text, is written to tools/validation-report.txt in UTF-8 --
read that file rather than trusting the console.
"""

import io
import json
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.stderr.write("FATAL: pyyaml is not installed. Run: pip install pyyaml\n")
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

failures = []
warnings = []
report_lines = []


def read_text(*parts):
    path = os.path.join(ROOT, *parts)
    with io.open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def load_yaml(*parts):
    return yaml.safe_load(read_text(*parts))


def load_json(*parts):
    return json.loads(read_text(*parts))


def check(name, condition, detail=""):
    line = "%-6s %s" % ("PASS" if condition else "FAIL", name)
    if detail:
        line += "  -- " + detail
    report_lines.append(line)
    if not condition:
        failures.append(name + (("  -- " + detail) if detail else ""))
    return condition


def warn(name, detail=""):
    line = "%-6s %s" % ("WARN", name)
    if detail:
        line += "  -- " + detail
    report_lines.append(line)
    warnings.append(name)


def sql_enum(sql_text, type_name):
    """Extract the value list of a CREATE TYPE ... AS ENUM (...) declaration."""
    m = re.search(
        r"CREATE\s+TYPE\s+%s\s+AS\s+ENUM\s*\((.*?)\)\s*;" % re.escape(type_name),
        sql_text,
        re.S | re.I,
    )
    if not m:
        return None
    return set(re.findall(r"'([^']+)'", m.group(1)))


# ---------------------------------------------------------------------------
#  1. Load everything
# ---------------------------------------------------------------------------
report_lines.append("=" * 78)
report_lines.append("  1. LOADING")
report_lines.append("=" * 78)

try:
    mmap = load_yaml("config", "monitoring-map.yaml")
    check("config/monitoring-map.yaml parses", True)
except Exception as exc:
    check("config/monitoring-map.yaml parses", False, str(exc))
    mmap = None

try:
    queries = load_yaml("config", "queries.yaml")
    check("config/queries.yaml parses", True)
except Exception as exc:
    check("config/queries.yaml parses", False, str(exc))
    queries = None

try:
    triage = load_yaml("config", "triage.yaml")
    check("config/triage.yaml parses", True)
except Exception as exc:
    check("config/triage.yaml parses", False, str(exc))
    triage = None

try:
    hit_schema = load_json("schemas", "hit.schema.json")
    check("schemas/hit.schema.json parses", True)
except Exception as exc:
    check("schemas/hit.schema.json parses", False, str(exc))
    hit_schema = None

try:
    hb_schema = load_json("schemas", "heartbeat.schema.json")
    check("schemas/heartbeat.schema.json parses", True)
except Exception as exc:
    check("schemas/heartbeat.schema.json parses", False, str(exc))
    hb_schema = None

sql_text = read_text("sql", "001_monitoring_map.sql")
doc00 = read_text("docs", "00-monitoring-map.md")
doc01 = read_text("docs", "01-triage-scoring.md")
doc02 = read_text("docs", "02-cadence.md")
doc03 = read_text("docs", "03-handoff.md")

if not all([mmap, queries, triage, hit_schema, hb_schema]):
    sys.stderr.write("FATAL: could not load all files; aborting.\n")
    sys.exit(2)

# ---------------------------------------------------------------------------
#  2. Query completeness against the source requirements
# ---------------------------------------------------------------------------
report_lines.append("")
report_lines.append("=" * 78)
report_lines.append("  2. QUERY COMPLETENESS (source requirements, section 6)")
report_lines.append("=" * 78)

groups = {g["key"]: g for g in queries["query_groups"]}
expected = queries["expected_counts"]

actual_total = 0
for key, group in groups.items():
    n = len(group["queries"])
    actual_total += n
    check("group %s has %d queries" % (key, expected.get(key, -1)),
          n == expected.get(key), "actual %d" % n)

check("total queries == 89", actual_total == expected["total"],
      "actual %d, expected %d" % (actual_total, expected["total"]))

wb_actual = len(groups["wb_core"]["queries"]) + len(groups["wb_extended"]["queries"])
check("wildberries total == 28 (wb_core + wb_extended)",
      wb_actual == expected["wildberries_total"], "actual %d" % wb_actual)

all_query_texts = [q["text"] for g in groups.values() for q in g["queries"]]
check("no duplicate query strings",
      len(all_query_texts) == len(set(all_query_texts)),
      "%d unique of %d" % (len(set(all_query_texts)), len(all_query_texts)))

# ---------------------------------------------------------------------------
#  3. Taxonomy: YAML <-> SQL <-> schema <-> docs
# ---------------------------------------------------------------------------
report_lines.append("")
report_lines.append("=" * 78)
report_lines.append("  3. TAXONOMY CONSISTENCY")
report_lines.append("=" * 78)

topic_keys = [t["key"] for t in mmap["topics"]]
subtopics = [(t["key"], s) for t in mmap["topics"] for s in t["subtopics"]]
sub_keys = [s["key"] for _, s in subtopics]
yaml_categories = set(s["category"] for _, s in subtopics)
yaml_platforms = set(p["key"] for p in mmap["platforms"])
signal_keys = [s["key"] for s in mmap["signals"]]
signal_methods_used = set(s["method"] for s in mmap["signals"])
signal_methods_declared = set(m["key"] for m in mmap["signal_methods"])

check("11 top-level topics", len(topic_keys) == 11, "actual %d" % len(topic_keys))
check("topic keys unique", len(topic_keys) == len(set(topic_keys)))
check("subtopic keys unique", len(sub_keys) == len(set(sub_keys)),
      "%d unique of %d" % (len(set(sub_keys)), len(sub_keys)))
report_lines.append("INFO   subtopics total: %d" % len(sub_keys))

sql_categories = sql_enum(sql_text, "monitoring_category")
sql_platforms = sql_enum(sql_text, "monitoring_platform")
sql_cadence = sql_enum(sql_text, "cadence_class")
sql_decisions = sql_enum(sql_text, "triage_decision")
sql_states = sql_enum(sql_text, "hit_state")
sql_methods = sql_enum(sql_text, "signal_method")

check("SQL declares monitoring_category", sql_categories is not None)
check("SQL declares monitoring_platform", sql_platforms is not None)

if sql_categories:
    check("18 categories in SQL enum", len(sql_categories) == 18,
          "actual %d" % len(sql_categories))
    unknown = yaml_categories - sql_categories
    check("every YAML category exists in SQL enum", not unknown,
          "unknown: %s" % sorted(unknown))
    unused = sql_categories - yaml_categories
    if unused:
        warn("categories declared but unused by any subtopic",
             ", ".join(sorted(unused)))

if sql_platforms:
    missing = yaml_platforms - sql_platforms
    check("every YAML platform exists in SQL enum", not missing,
          "missing: %s" % sorted(missing))
    check("platform sets identical", yaml_platforms == sql_platforms,
          "only in SQL: %s" % sorted(sql_platforms - yaml_platforms))

if sql_methods:
    check("every signal method exists in SQL enum",
          signal_methods_used <= sql_methods,
          "unknown: %s" % sorted(signal_methods_used - sql_methods))

check("every signal method is declared in signal_methods",
      signal_methods_used <= signal_methods_declared,
      "undeclared: %s" % sorted(signal_methods_used - signal_methods_declared))
check("18 signals", len(signal_keys) == 18, "actual %d" % len(signal_keys))

# schema enums
schema_topics = set(hit_schema["properties"]["topics"]["items"]["enum"])
schema_cats = set(hit_schema["properties"]["categories"]["items"]["enum"])
schema_plats = set(hit_schema["properties"]["platforms"]["items"]["enum"])
schema_signals = set(hit_schema["properties"]["signal"]["enum"])

check("hit.schema topics == YAML topics", schema_topics == set(topic_keys),
      "diff: %s" % sorted(schema_topics ^ set(topic_keys)))
check("hit.schema platforms == YAML platforms", schema_plats == yaml_platforms,
      "diff: %s" % sorted(schema_plats ^ yaml_platforms))
check("hit.schema signals == YAML signals", schema_signals == set(signal_keys),
      "diff: %s" % sorted(schema_signals ^ set(signal_keys)))
if sql_categories:
    check("hit.schema categories == SQL enum", schema_cats == sql_categories,
          "diff: %s" % sorted(schema_cats ^ sql_categories))

# queries reference real topics and platforms
bad_topic = sorted(set(q["topic"] for g in groups.values() for q in g["queries"])
                   - set(topic_keys))
check("every query references an existing topic", not bad_topic,
      "unknown: %s" % bad_topic)
bad_plat = sorted(set(q["platform"] for g in groups.values() for q in g["queries"])
                  - yaml_platforms)
check("every query references an existing platform", not bad_plat,
      "unknown: %s" % bad_plat)

# docs mention every top-level topic key
missing_in_doc = [k for k in topic_keys if ("`%s`" % k) not in doc00]
check("every topic key documented in docs/00", not missing_in_doc,
      "missing: %s" % missing_in_doc)

# --- source registry -------------------------------------------------------
try:
    sources_cfg = load_yaml("config", "sources.yaml")
    srcs = sources_cfg["sources"]
    check("config/sources.yaml parses", True, "%d sources" % len(srcs))

    source_keys = [s["key"] for s in srcs]
    check("source keys unique", len(source_keys) == len(set(source_keys)))

    bad_plat = sorted(set(s["platform"] for s in srcs) - yaml_platforms)
    check("every source references a declared platform", not bad_plat,
          "unknown: %s" % bad_plat)

    bad_method = sorted(set(s["method"] for s in srcs) - {"rss", "doc_diff"})
    check("every source uses a supported method", not bad_method,
          "unknown: %s" % bad_method)

    bad_tier = sorted(set(s["tier"] for s in srcs)
                      - {"T1", "T2", "T3", "T4", "T5", "T6"})
    check("every source has a valid tier", not bad_tier, "unknown: %s" % bad_tier)

    doc_diff_without_signal = [s["key"] for s in srcs
                               if s["method"] == "doc_diff" and not s.get("signal")]
    check("every doc_diff source declares its signal", not doc_diff_without_signal,
          "missing: %s" % doc_diff_without_signal)

    bad_signal = sorted(set(s.get("signal") for s in srcs if s.get("signal"))
                        - set(signal_keys))
    check("every source signal is declared in the map", not bad_signal,
          "unknown: %s" % bad_signal)
except Exception as exc:
    check("config/sources.yaml parses", False, str(exc))
    srcs = []

# ---------------------------------------------------------------------------
#  4. Scoring matrix
# ---------------------------------------------------------------------------
report_lines.append("")
report_lines.append("=" * 78)
report_lines.append("  4. SCORING MATRIX")
report_lines.append("=" * 78)

factors = {f["key"]: f["weight"] for f in triage["factors"]}
check("14 factors", len(factors) == 14, "actual %d" % len(factors))

max_pos = sum(w for w in factors.values() if w > 0)
min_neg = sum(w for w in factors.values() if w < 0)
bounds = triage["score_bounds"]
check("max_positive matches declared bound", max_pos == bounds["max_positive"],
      "computed %d, declared %d" % (max_pos, bounds["max_positive"]))
check("min_negative matches declared bound", min_neg == bounds["min_negative"],
      "computed %d, declared %d" % (min_neg, bounds["min_negative"]))

schema_factor_props = set(hit_schema["properties"]["factors"]["properties"].keys())
schema_factor_req = set(hit_schema["properties"]["factors"]["required"])
check("hit.schema factor keys == triage.yaml factor keys",
      schema_factor_props == set(factors),
      "diff: %s" % sorted(schema_factor_props ^ set(factors)))
check("all 14 factors required by hit.schema",
      schema_factor_req == set(factors),
      "diff: %s" % sorted(schema_factor_req ^ set(factors)))

check("hit.schema score bounds match triage.yaml",
      hit_schema["properties"]["score"]["minimum"] == bounds["min_negative"]
      and hit_schema["properties"]["score"]["maximum"] == bounds["max_positive"])

sql_bounds = re.search(r"score\s+BETWEEN\s+(-?\d+)\s+AND\s+(\d+)", sql_text)
check("SQL score bounds match triage.yaml",
      bool(sql_bounds)
      and int(sql_bounds.group(1)) == bounds["min_negative"]
      and int(sql_bounds.group(2)) == bounds["max_positive"],
      "SQL says %s" % (str(sql_bounds.groups()) if sql_bounds else "not found",))

# thresholds
thresholds = triage["thresholds"]
check("4 threshold bands", len(thresholds) == 4, "actual %d" % len(thresholds))
band_names = [t["decision"] for t in thresholds]
check("threshold bands match source requirements",
      band_names == ["URGENT", "QUEUE", "BACKLOG", "DROP"], str(band_names))
check("threshold values are 80 / 60 / 40",
      [t["min_score"] for t in thresholds[:3]] == [80, 60, 40],
      str([t["min_score"] for t in thresholds[:3]]))

if sql_decisions:
    check("SQL triage_decision matches threshold bands",
          sql_decisions == set(band_names), "SQL: %s" % sorted(sql_decisions))


def decide(score):
    for band in thresholds:
        if band["min_score"] is not None and score >= band["min_score"]:
            return band["decision"]
    return "DROP"


# Reference cases from docs/01 section 2.3
report_lines.append("")
report_lines.append("  Reference cases (docs/01 section 2.3):")

cases = [
    ("WB storage tariff change, official, effective in a week",
     ["platform_wb", "seller_money_impact", "rules_change",
      "authoritative_source", "is_fresh", "has_practical_takeaway",
      "mass_effect"],
     130, "URGENT"),
    ("fresh confirmed AI news, no platform",
     ["ai_link", "authoritative_source", "is_fresh", "has_practical_takeaway"],
     55, "BACKLOG"),
    ("chat rumour about a new WB fee, no source confirmation",
     ["platform_wb", "seller_money_impact", "rules_change", "is_fresh",
      "has_practical_takeaway", "mass_effect", "no_confirmation"],
     65, "QUEUE"),
]

for title, keys, want_score, want_band in cases:
    got = sum(factors[k] for k in keys)
    band = decide(got)
    check("case: %s -> %d / %s" % (title, want_score, want_band),
          got == want_score and band == want_band,
          "got %d / %s" % (got, band))

# The rumour case is the whole reason the matrix is not a publication gate:
# it must land at or above the QUEUE threshold, not below it.
rumour = sum(factors[k] for k in cases[2][1])
check("rumour case sits in a working band, proving compensation is real",
      rumour >= 60,
      "rumour scores %d; if this drops below 60 the docs/01 section 3 argument "
      "needs rewriting" % rumour)

# backlog promotion arithmetic from docs/01 section 5.2
promotion = -factors["no_confirmation"] + factors["authoritative_source"]
check("backlog promotion adds 65 points", promotion == 65,
      "computed %d" % promotion)
check("promoted rumour reaches the WB tariff score",
      rumour + promotion == 130, "computed %d" % (rumour + promotion))

# ---------------------------------------------------------------------------
#  5. Stop rules
# ---------------------------------------------------------------------------
report_lines.append("")
report_lines.append("=" * 78)
report_lines.append("  5. STOP RULES")
report_lines.append("=" * 78)

stop_codes = [r["code"] for r in triage["stop_rules"]]
check("14 stop rules", len(stop_codes) == 14, "actual %d" % len(stop_codes))
check("stop rule codes unique", len(stop_codes) == len(set(stop_codes)))

missing_doc = [c for c in stop_codes if c not in doc00]
check("every stop rule documented in docs/00", not missing_doc,
      "missing: %s" % missing_doc)

check("rumour rule vs no_confirmation factor is disambiguated in docs/00",
      "5.1" in doc00 and "STOP_UNCONFIRMED_RUMOR" in doc00
      and "no_confirmation" in doc00)

# ---------------------------------------------------------------------------
#  6. Cadence
# ---------------------------------------------------------------------------
report_lines.append("")
report_lines.append("=" * 78)
report_lines.append("  6. CADENCE")
report_lines.append("=" * 78)

cadence = {c["key"]: c for c in queries["cadence_classes"]}
check("6 cadence classes", len(cadence) == 6, "actual %d" % len(cadence))
if sql_cadence:
    check("SQL cadence_class matches YAML", set(cadence) == sql_cadence,
          "diff: %s" % sorted(set(cadence) ^ sql_cadence))

used_classes = set(g["cadence_class"] for g in groups.values())
check("every group uses a declared cadence class", used_classes <= set(cadence),
      "unknown: %s" % sorted(used_classes - set(cadence)))

unused_classes = set(cadence) - used_classes
if unused_classes:
    report_lines.append(
        "INFO   cadence classes not used by any query group (sources only): %s"
        % ", ".join(sorted(unused_classes)))

source_classes = set(s["cadence"] for s in srcs)
check("every source uses a declared cadence class", source_classes <= set(cadence),
      "unknown: %s" % sorted(source_classes - set(cadence)))
check("class A has at least one Wildberries source",
      any(s["cadence"] == "A" and s["platform"] == "WILDBERRIES" for s in srcs),
      "class A exists for WB speed; an empty one makes the 5-minute tick pointless")

check("class A is 300 seconds", cadence["A"]["period_seconds"] == 300,
      "actual %s" % cadence["A"]["period_seconds"])
check("WB core group runs on class A",
      groups["wb_core"]["cadence_class"] == "A",
      "actual %s" % groups["wb_core"]["cadence_class"])

periods = [cadence[k]["period_seconds"] for k in sorted(cadence)]
check("cadence periods increase monotonically A..F",
      periods == sorted(periods), str(periods))

missing_cad_doc = [k for k in cadence if ("**%s**" % k) not in doc02
                   and ("| **%s**" % k) not in doc02]
if missing_cad_doc:
    warn("cadence classes not obviously documented in docs/02",
         ", ".join(sorted(missing_cad_doc)))

# ---------------------------------------------------------------------------
#  7. States and handoff contract
# ---------------------------------------------------------------------------
report_lines.append("")
report_lines.append("=" * 78)
report_lines.append("  7. STATES AND HANDOFF")
report_lines.append("=" * 78)

schema_states = set(hit_schema["properties"]["state"]["enum"])
if sql_states:
    check("hit.schema state enum == SQL hit_state", schema_states == sql_states,
          "diff: %s" % sorted(schema_states ^ sql_states))

schema_decisions = set(hit_schema["properties"]["triage_decision"]["enum"])
check("hit.schema decisions == threshold bands",
      schema_decisions == set(band_names),
      "diff: %s" % sorted(schema_decisions ^ set(band_names)))

check("heartbeat schema pins exactly 10 answers",
      hb_schema["properties"]["answers"]["minItems"] == 10
      and hb_schema["properties"]["answers"]["maxItems"] == 10)

hb_answer_req = set(hb_schema["$defs"]["answer"]["required"])
check("heartbeat requires data_age_seconds on every answer",
      "data_age_seconds" in hb_answer_req)

check("docs/03 states the four publication gates",
      "importance" in doc03 and "confidence" in doc03
      and "novelty" in doc03 and "actionability" in doc03)
check("docs/03 states that URGENT is not permission to publish",
      "URGENT" in doc03 and "3.1" in doc03)

sql_ten = "jsonb_array_length(answers) = 10" in sql_text
check("SQL enforces exactly 10 heartbeat answers", sql_ten)

# ---------------------------------------------------------------------------
#  8. Examples in docs validate against the schemas
# ---------------------------------------------------------------------------
report_lines.append("")
report_lines.append("=" * 78)
report_lines.append("  8. DOC EXAMPLES vs SCHEMAS")
report_lines.append("=" * 78)

json_blocks = re.findall(r"```json\n(.*?)\n```", doc03, re.S)
check("docs/03 contains json examples", len(json_blocks) >= 2,
      "found %d" % len(json_blocks))

hit_example = None
for block in json_blocks:
    try:
        obj = json.loads(block)
    except ValueError as exc:
        check("json example parses", False, str(exc))
        continue
    if "hit_id" in obj:
        hit_example = obj

check("hit example parses and is present", hit_example is not None)

if hit_example:
    req = set(hit_schema["required"])
    missing = req - set(hit_example)
    check("hit example has all required fields", not missing,
          "missing: %s" % sorted(missing))
    extra = set(hit_example) - set(hit_schema["properties"])
    check("hit example has no unknown fields", not extra,
          "unknown: %s" % sorted(extra))
    check("hit example carries all 14 factors",
          set(hit_example["factors"]) == set(factors),
          "diff: %s" % sorted(set(hit_example["factors"]) ^ set(factors)))
    recomputed = sum(f["weight"] for f in hit_example["factors"].values())
    check("hit example score equals sum of its factor weights",
          recomputed == hit_example["score"],
          "sum %d, stated %d" % (recomputed, hit_example["score"]))
    check("hit example decision matches its score",
          decide(hit_example["score"]) == hit_example["triage_decision"],
          "score %d implies %s, stated %s"
          % (hit_example["score"], decide(hit_example["score"]),
             hit_example["triage_decision"]))
    no_why = [k for k, v in hit_example["factors"].items()
              if v.get("hit") and not v.get("why")]
    check("every fired factor in the example has a rationale", not no_why,
          "missing why: %s" % no_why)
    bad_zero = [k for k, v in hit_example["factors"].items()
                if not v.get("hit") and v.get("weight") != 0]
    check("unfired factors in the example weigh zero", not bad_zero,
          "nonzero: %s" % bad_zero)
    check("hit example signal is a declared signal",
          hit_example.get("signal") in signal_keys
          or "signal" not in hit_example,
          "signal: %s" % hit_example.get("signal"))
    check("hit example platforms are declared",
          set(hit_example["platforms"]) <= yaml_platforms)
    check("hit example topics are declared",
          set(hit_example["topics"]) <= set(topic_keys))

# Optional: real JSON Schema validation when the library is available
try:
    import jsonschema  # noqa: F401
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(hit_schema)
    Draft202012Validator.check_schema(hb_schema)
    check("both JSON schemas are valid Draft 2020-12", True)
    if hit_example:
        errors = sorted(Draft202012Validator(hit_schema).iter_errors(hit_example),
                        key=lambda e: e.path)
        check("hit example validates against hit.schema.json", not errors,
              "; ".join(e.message for e in errors[:3]))
except ImportError:
    warn("jsonschema not installed",
         "structural checks ran instead of full JSON Schema validation; "
         "install with: pip install jsonschema")

# ---------------------------------------------------------------------------
#  Report
# ---------------------------------------------------------------------------
report_lines.append("")
report_lines.append("=" * 78)
report_lines.append("  SUMMARY")
report_lines.append("=" * 78)
total = sum(1 for line in report_lines if line.startswith(("PASS", "FAIL")))
report_lines.append("checks run : %d" % total)
report_lines.append("failures   : %d" % len(failures))
report_lines.append("warnings   : %d" % len(warnings))
for f in failures:
    report_lines.append("  FAIL " + f)

out = "\n".join(report_lines) + "\n"
report_path = os.path.join(ROOT, "tools", "validation-report.txt")
with io.open(report_path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(out)

sys.stdout.write(out)
sys.stdout.write("\nreport written to tools/validation-report.txt\n")
sys.exit(1 if failures else 0)
