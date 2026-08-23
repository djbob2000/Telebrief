"""Direct-SQL tests for domain composite invariants and cross-edition integrity (Milestone E Task 10)."""

import psycopg
import pytest


@pytest.mark.postgres
class TestDomainCompositeInvariants:
    async def test_cross_edition_claim_relation_rejected(self, conn: psycopg.AsyncConnection):
        # 1. Create two distinct editions
        cur = await conn.execute(
            "INSERT INTO editions (slug, name) VALUES ('ed_a', 'Edition A'), ('ed_b', 'Edition B') RETURNING id"
        )
        rows = await cur.fetchall()
        ed_a, ed_b = rows[0][0], rows[1][0]

        # 2. Ingestion sources & revisions for each edition
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, name, external_id)
            VALUES ('telegram', 'channel', 'Test A', 'src_a'), ('telegram', 'channel', 'Test B', 'src_b')
            RETURNING id
            """
        )
        src_rows = await cur.fetchall()
        src_a, src_b = src_rows[0][0], src_rows[1][0]

        await conn.execute(
            "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s), (%s, %s)",
            (src_a, ed_a, src_b, ed_b),
        )

        cur = await conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
            VALUES (%s, 'message', '1', now()), (%s, 'message', '2', now())
            RETURNING id
            """,
            (src_a, src_b),
        )
        item_rows = await cur.fetchall()
        item_a, item_b = item_rows[0][0], item_rows[1][0]

        cur = await conn.execute(
            """
            INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
            VALUES (%s, 1, 'hash_a', 'A'), (%s, 1, 'hash_b', 'B')
            RETURNING id
            """,
            (item_a, item_b),
        )
        rev_rows = await cur.fetchall()
        rev_a, rev_b = rev_rows[0][0], rev_rows[1][0]

        # 3. Relevance policies & decisions
        cur = await conn.execute(
            """
            INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version)
            VALUES (%s, 1, 'rcfg_a', 'rp_1'), (%s, 1, 'rcfg_b', 'rp_1') RETURNING id
            """,
            (ed_a, ed_b),
        )
        rpol_rows = await cur.fetchall()
        rpol_a, rpol_b = rpol_rows[0][0], rpol_rows[1][0]

        cur = await conn.execute(
            """
            INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason)
            VALUES (%s, %s, %s, 'relevant', 'test'), (%s, %s, %s, 'relevant', 'test') RETURNING id
            """,
            (rev_a, ed_a, rpol_a, rev_b, ed_b, rpol_b),
        )
        dec_rows = await cur.fetchall()
        dec_a, dec_b = dec_rows[0][0], dec_rows[1][0]

        # 4. Extraction policies & runs
        cur = await conn.execute(
            """
            INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version)
            VALUES (%s, 1, 'ecfg_a', 'ep_1'), (%s, 1, 'ecfg_b', 'ep_1') RETURNING id
            """,
            (ed_a, ed_b),
        )
        epol_rows = await cur.fetchall()
        epol_a, epol_b = epol_rows[0][0], epol_rows[1][0]

        cur = await conn.execute(
            """
            INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status)
            VALUES (%s, %s, %s, %s, 'succeeded'), (%s, %s, %s, %s, 'succeeded') RETURNING id
            """,
            (rev_a, ed_a, epol_a, dec_a, rev_b, ed_b, epol_b, dec_b),
        )
        run_rows = await cur.fetchall()
        run_a, run_b = run_rows[0][0], run_rows[1][0]

        # 5. Insert claims for Edition A and Edition B
        cur = await conn.execute(
            """
            INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion)
            VALUES (%s, %s, %s, 'Claim A', 'claim a'), (%s, %s, %s, 'Claim B', 'claim b') RETURNING id
            """,
            (run_a, rev_a, ed_a, run_b, rev_b, ed_b),
        )
        claim_rows = await cur.fetchall()
        claim_a, claim_b = claim_rows[0][0], claim_rows[1][0]

        # 6. Attempt to insert cross-edition claim_relation (from Edition A claim to Edition B claim)
        # MUST BE REJECTED by Postgres FK constraint
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(
                """
                INSERT INTO claim_relations (from_claim_id, to_claim_id, edition_id, relation_type)
                VALUES (%s, %s, %s, 'CORRECTS')
                """,
                (claim_a, claim_b, ed_a),
            )

    async def test_cross_edition_story_claim_rejected(self, conn: psycopg.AsyncConnection):
        # 1. Create two editions
        cur = await conn.execute(
            "INSERT INTO editions (slug, name) VALUES ('ed_x', 'Edition X'), ('ed_y', 'Edition Y') RETURNING id"
        )
        rows = await cur.fetchall()
        ed_x, ed_y = rows[0][0], rows[1][0]

        # 2. Source, item, revision, claim in Edition X
        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, name, external_id) VALUES ('telegram', 'channel', 'Src X', 'x') RETURNING id"
        )
        src_x = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'message', '10', now()) RETURNING id",
            (src_x,),
        )
        item_x = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'hx', 'X') RETURNING id",
            (item_x,),
        )
        rev_x = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'r_x', 'p_1') RETURNING id",
            (ed_x,),
        )
        rpol_x = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 't') RETURNING id",
            (rev_x, ed_x, rpol_x),
        )
        dec_x = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'e_x', 'p_1') RETURNING id",
            (ed_x,),
        )
        epol_x = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status) VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id",
            (rev_x, ed_x, epol_x, dec_x),
        )
        run_x = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion) VALUES (%s, %s, %s, 'Claim X', 'claim x') RETURNING id",
            (run_x, rev_x, ed_x),
        )
        claim_x = (await cur.fetchone())[0]

        # 3. Create Story in Edition Y
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state) VALUES (%s, 'active') RETURNING id",
            (ed_y,),
        )
        story_y = (await cur.fetchone())[0]

        # 4. Attempt to attach Edition X claim to Edition Y story
        # MUST BE REJECTED by Postgres FK constraint
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(
                "INSERT INTO story_claims (story_id, claim_id, edition_id) VALUES (%s, %s, %s)",
                (story_y, claim_x, ed_y),
            )

    async def test_mismatched_story_revision_in_candidate_rejected(
        self, conn: psycopg.AsyncConnection
    ):
        # 1. Create Edition and two stories
        cur = await conn.execute(
            "INSERT INTO editions (slug, name) VALUES ('ed_z', 'Edition Z') RETURNING id"
        )
        ed_z = (await cur.fetchone())[0]

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state) VALUES (%s, 'active'), (%s, 'active') RETURNING id",
            (ed_z, ed_z),
        )
        rows = await cur.fetchall()
        story_1, story_2 = rows[0][0], rows[1][0]

        # 2. Insert revision for story 1 and revision for story 2
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash)
            VALUES (%s, 1, 'open', 'text 1', 'h1'), (%s, 1, 'open', 'text 2', 'h2') RETURNING id
            """,
            (story_1, story_2),
        )
        rev_rows = await cur.fetchall()
        _, rev_2 = rev_rows[0][0], rev_rows[1][0]

        # 3. Story matching policy and run
        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, name, external_id) VALUES ('telegram', 'channel', 'Src Z', 'z') RETURNING id"
        )
        src_z = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'message', '100', now()) RETURNING id",
            (src_z,),
        )
        item_z = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'hz', 'Z') RETURNING id",
            (item_z,),
        )
        rev_z = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'r_z', 'p_1') RETURNING id",
            (ed_z,),
        )
        rpol_z = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 't') RETURNING id",
            (rev_z, ed_z, rpol_z),
        )
        dec_z = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'e_z', 'p_1') RETURNING id",
            (ed_z,),
        )
        epol_z = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status) VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id",
            (rev_z, ed_z, epol_z, dec_z),
        )
        run_z = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion) VALUES (%s, %s, %s, 'Claim Z', 'claim z') RETURNING id",
            (run_z, rev_z, ed_z),
        )
        claim_z = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO story_matching_policy_versions (edition_id, version, config_hash, prompt_version, embedding_model, embedding_dimensions)
            VALUES (%s, 1, 'sm_z', 'p_1', 'gemini', 1536) RETURNING id
            """,
            (ed_z,),
        )
        sm_pol_z = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO story_matching_runs (claim_id, edition_id, policy_id, status)
            VALUES (%s, %s, %s, 'running') RETURNING id
            """,
            (claim_z, ed_z, sm_pol_z),
        )
        match_run_z = (await cur.fetchone())[0]

        # 4. Attempt to insert candidate pairing story_1 with rev_2 (which belongs to story_2!)
        # MUST BE REJECTED by Postgres FK constraint
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(
                """
                INSERT INTO story_matching_candidates (run_id, story_id, story_revision_id, rank)
                VALUES (%s, %s, %s, 1)
                """,
                (match_run_z, story_1, rev_2),
            )
