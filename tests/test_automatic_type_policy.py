"""Offline guards for the automatic type proof and its read contracts.

These guard the embedded policy, accepted duration grammar and unchanged view
boundaries. Execution, permissions and timestamp edges belong to the scoped
PostgreSQL check; string assertions are not a substitute for that check.
"""

from pathlib import Path
import re
import runpy
import unittest


ROOT = Path(__file__).resolve().parents[1]
SQL = runpy.run_path(str(ROOT / "application/db/inventory/automatic_type_policy.py"))[
    "AUTOMATIC_TYPE_CANDIDATES_SQL"
]
SCHEMA = (ROOT / "schema/schema.sql").read_text(encoding="utf-8")


def views(name):
    return re.findall(
        rf"CREATE (?:OR REPLACE )?VIEW {re.escape(name)}\b.*?;", SCHEMA, re.S
    )


class AutomaticTypePolicyTests(unittest.TestCase):
    def test_schema_embeds_the_single_canonical_query(self):
        candidate = views("public.video_type_automatic_candidates")
        self.assertEqual(len(candidate), 1)
        self.assertEqual(candidate[0].split(" AS\n", 1)[1].removesuffix(";"), SQL)
        self.assertIn("security_invoker=true, security_barrier=true", candidate[0])
        self.assertIn(
            "GRANT SELECT ON public.video_type_automatic_candidates TO service_role;",
            SCHEMA,
        )

    def test_reader_permission_step_preserves_policy_barrier_and_acl(self):
        import hashlib
        step = (ROOT / "schema/steps/0030-automatic-type-reader-permissions.sql").read_text()
        statements = "\n".join(line for line in step.splitlines() if not line.startswith("--")).strip()
        expected = "ALTER VIEW public.video_type_automatic_candidates SET (security_invoker=false);"
        self.assertEqual(statements, expected)
        self.assertIn(expected, SCHEMA)
        self.assertGreater(SCHEMA.index(expected), SCHEMA.index("-- END automatic Long/Short policy."))
        self.assertIn("ALTER VIEW public.video_type_automatic_candidates OWNER TO postgres;", SCHEMA)
        self.assertNotRegex(SCHEMA, r"GRANT[^;]*video_type_automatic_candidates[^;]*lic_reader")
        self.assertEqual(hashlib.sha256((ROOT / "schema/steps/0029-automatic-long-short-policy.sql").read_bytes()).hexdigest(),
                         "86c9d4aef1012db34216b498215f28d864114a35419f69f7699b2a131f29eb8d")

    def test_exact_raw_evidence_is_checked_before_freshness(self):
        evidence = SQL.split("FROM (\n", 1)[1].split(") evidence", 1)[0]
        before_having = evidence.split("HAVING", 1)[0]
        self.assertIn("FROM public.video_inventory i", before_having)
        self.assertIn("JOIN public.video_inventory_memberships m ON m.video_id=i.video_id AND m.membership_status='present'", before_having)
        self.assertIn("GROUP BY i.video_id,i.channel_id", before_having)
        self.assertNotIn("isfinite(", before_having)
        self.assertNotIn("BETWEEN", before_having)
        self.assertIn(
            "array_agg(DISTINCT m.video_type ORDER BY m.video_type)=ARRAY['long','short']::text[]",
            evidence,
        )
        self.assertIn("bool_and((m.channel_id=i.channel_id", evidence)
        for clock in ("last_seen_at", "last_present_at"):
            self.assertIn(f"isfinite(m.{clock})", evidence)
            self.assertIn(
                f"m.{clock} BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()",
                evidence,
            )
        # NULL clocks must fail the proof, not disappear inside bool_and.
        self.assertIn("statement_timestamp()) IS TRUE)", evidence)

    def test_fresh_metadata_and_all_explicit_decisions_are_respected(self):
        self.assertIn("metadata.video_id=evidence.video_id AND metadata.channel_id=evidence.channel_id", SQL)
        self.assertIn("isfinite(v.metadata_checked_at)", SQL)
        self.assertIn(
            "v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()",
            SQL,
        )
        self.assertIn("(i.video_type IS NULL OR i.video_type IN ('long','short'))", SQL)
        decision = SQL.split("AND NOT EXISTS (", 1)[1].split("\n      )", 1)[0]
        self.assertIn("decision.video_id=i.video_id AND decision.decision_type IS NOT NULL", decision)
        self.assertNotIn("state", decision)
        self.assertNotIn("decision_at", decision)
        self.assertNotIn("blocked_reason", decision)

    def test_metadata_parse_follows_overlap_without_materializing_all_point_reads(self):
        # OFFSET 0 keeps the metadata read correlated with the admitted group.
        # The group is inline so an exact video predicate can reach its input.
        self.assertNotIn("MATERIALIZED", SQL)
        self.assertLess(SQL.index(") evidence"), SQL.index("FROM public.videos metadata"))
        self.assertLess(SQL.index("OFFSET 0"), SQL.index("regexp_match(v.duration"))
        step = (ROOT / "schema/steps/0032-automatic-type-read-plan.sql").read_text()
        statement = "\n".join(line for line in step.splitlines() if not line.startswith("--")).strip()
        self.assertEqual(statement,
            "CREATE OR REPLACE VIEW public.video_type_automatic_candidates\n"
            "WITH (security_barrier=true, security_invoker=false) AS\n" + SQL + ";")
        self.assertNotRegex(statement, r"(?i)\b(?:GRANT|REVOKE|INSERT|UPDATE|DELETE|DROP)\b")

    def test_duration_grammar_consumes_the_entire_supported_value(self):
        iso = re.search(r"regexp_match\(v.duration, '([^']+)'\)", SQL).group(1)
        numeric = re.search(r"WHEN v.duration ~ '([^']+)'", SQL).group(1)
        for value, expected in (
            ("PT1S", (None, None, None, "1")),
            ("PT3M", (None, None, "3", None)),
            ("PT180S", (None, None, None, "180")),
            ("PT181S", (None, None, None, "181")),
            ("P1DT2H3M4S", ("1", "2", "3", "4")),
            ("P1D", ("1", None, None, None)),
        ):
            with self.subTest(value=value):
                self.assertEqual(re.fullmatch(iso, value).groups(), expected)
        for value in ("PT3Mgarbage", "P1Y", "P1W", "PT1.5S", "-1", "NaN", "Infinity", " 180", "180x"):
            with self.subTest(value=value):
                self.assertIsNone(re.fullmatch(iso, value))
                self.assertIsNone(re.fullmatch(numeric, value))
        for value in ("1", "180", "180.1", "181"):
            with self.subTest(value=value):
                self.assertIsNotNone(re.fullmatch(numeric, value))
        # P/PT and zero durations may match the grammar but never prove a type.
        self.assertIn("AND duration_value.seconds > 0", SQL)
        self.assertIn("duration_iso.parts IS NOT NULL AND v.duration !~ 'T$'", SQL)
        self.assertIn("duration_value.seconds <= 180 THEN 'short'::text ELSE 'long'::text", SQL)

    def test_automatic_proof_only_resolves_the_matching_canonical_type(self):
        for namespace in ("public", "readable"):
            with self.subTest(namespace=namespace):
                resolution = views(f"{namespace}.video_inventory_type_resolution")[-1]
                self.assertIn("coalesce(automatic.automatic_type=e.canonical_video_type,false) AS automatic_valid", resolution)
                self.assertIn("automatic.video_id=e.video_id", resolution)
                self.assertIn("automatic.channel_id=e.canonical_channel_id", resolution)
                self.assertIn("WHEN manual_valid OR automatic_valid THEN 'resolved'::text", resolution)
                # Existing output columns stay in place; the new flag is last.
                output = resolution.split("\n SELECT video_id,", 1)[1].split("\n   FROM resolved", 1)[0]
                self.assertTrue(output.rstrip().endswith("automatic_valid"))
                self.assertIn("manual_valid", output)

    def test_hot_proof_only_relaxes_exact_singleton_admission(self):
        for namespace in ("public", "readable"):
            with self.subTest(namespace=namespace):
                original, automatic, current = views(f"{namespace}.hot_video_inventory")
                waiting = ("\n AND NOT EXISTS(SELECT 1 FROM public.videos publication WHERE publication.video_id=inv.video_id"
                    "\n AND publication.channel_id=inv.channel_id AND publication.publication_status IS NOT NULL)")
                self.assertEqual(current.count(waiting), 1)
                self.assertEqual(current.replace(waiting, "", 1), automatic)
                original = original.replace("CREATE VIEW", "CREATE OR REPLACE VIEW", 1)
                self.assertEqual(
                    automatic.replace(" OR (resolution.automatic_valid IS TRUE)", "", 1),
                    original,
                )
                self.assertIn("(resolution.manual_valid IS TRUE) OR (membership.video_type = inv.video_type)", current)
                self.assertIn("inv.skeleton_since IS NULL", current)

    def test_read_deadline_contains_automatic_proof_expiry(self):
        self.assertIn("min(least(m.last_seen_at, m.last_present_at))", SQL)
        self.assertIn("least(v.metadata_checked_at, evidence.oldest_clock)+interval '696 hours'", SQL)
        resolution = views("readable.video_inventory_type_resolution")[-1]
        self.assertIn("automatic.source_expires_at AS automatic_source_expires_at", resolution)
        self.assertIn("CASE WHEN automatic_valid THEN automatic_source_expires_at END) AS source_expires_at", resolution)
        self.assertIn("least(resolved.source_expires_at,CASE WHEN manual_valid", resolution)
        self.assertIn("least(resolution.source_expires_at,", views("readable.hot_video_inventory")[-1])

    def test_review_suppression_does_not_rewrite_or_hide_manual_decisions(self):
        review = views("readable.video_type_review_cases")[-1]
        self.assertIn("WHERE c.state <> 'open' OR c.decision_type IS NOT NULL OR NOT EXISTS (", review)
        self.assertIn("i.video_type=automatic.automatic_type", review)
        self.assertIn("automatic.video_id=c.video_id AND automatic.channel_id=c.channel_id", review)
        self.assertIn("c.state AS state", review)
        self.assertIn("c.decision_type AS decision_type", review)
        block = SCHEMA.split("-- BEGIN automatic Long/Short policy", 1)[1].split("-- END automatic Long/Short policy", 1)[0]
        self.assertNotRegex(block, r"(?i)\b(?:UPDATE|DELETE|INSERT|CREATE\s+FUNCTION|ALTER\s+TABLE)\b")


if __name__ == "__main__":
    unittest.main()
