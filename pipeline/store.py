"""Writes for one pipeline run. Search for the reject log lives here too."""

from shared.pipeline_store import lodge_lead, record_criterion_result
from shared.store import allocate_run_id, utc_now

from pipeline.load import extract_accession, extract_cik, filing_date
from pipeline.names import normalise_name

_IN_FLIGHT = ("fresh", "assigned", "researched", "presented", "acted", "position")


def open_position_id(conn, ticker):
    row = conn.execute(
        """
        SELECT lead_id FROM leads
        WHERE ticker = ? AND status IN ('acted', 'position')
        ORDER BY lead_id
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if not row:
        return None
    return row["lead_id"]


def criteria_for(conn, lead_types):
    rows = []
    for lead_type in lead_types:
        found = conn.execute(
            """
            SELECT c.criterion_id, c.name, c.description, r.lead_type
            FROM rubric_criteria AS c
            JOIN rubrics AS r ON r.rubric_id = c.rubric_id
            WHERE r.lead_type = ? AND r.version = 1
            ORDER BY c.sort_order
            """,
            (lead_type,),
        ).fetchall()
        rows.extend(dict(row) for row in found)
    return rows


def start_run(conn, started_at):
    run_id = allocate_run_id(conn, started_at, "pipeline")
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, scout, started_at, notes) VALUES (?, ?, ?, ?)",
            (run_id, "pipeline", started_at, "lead pipeline"),
        )
    return run_id


def finish_run(conn, run_id, token_budget, tokens_used, provider, started_at, finished_at):
    with conn:
        conn.execute(
            """
            INSERT INTO pipeline_runs (
                run_id, token_budget, tokens_used, provider, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, int(token_budget), int(tokens_used), provider, started_at, finished_at),
        )


def insert_reject(conn, run_id, planned, created_at):
    figure = planned.figure
    rating = planned.rating
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO lead_rejects (
                scout, ticker, cik, filing_date, source_url, accession,
                rejection_stage, reason,
                significance_magnitude, significance_magnitude_rationale,
                significance_novelty, significance_novelty_rationale,
                significance_attention, significance_attention_rationale,
                significance_timing, significance_timing_rationale,
                run_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                planned.lead_type,
                figure.ticker,
                extract_cik(figure.source_url),
                filing_date(figure),
                figure.source_url or None,
                extract_accession(figure),
                planned.stage,
                planned.reason,
                _rating_attr(rating, "magnitude"),
                _rating_attr(rating, "magnitude_rationale"),
                _rating_attr(rating, "novelty"),
                _rating_attr(rating, "novelty_rationale"),
                _rating_attr(rating, "attention"),
                _rating_attr(rating, "attention_rationale"),
                _rating_attr(rating, "timing"),
                _rating_attr(rating, "timing_rationale"),
                run_id,
                created_at,
            ),
        )
        reject_id = cursor.lastrowid
        for party in planned.parties:
            conn.execute(
                """
                INSERT OR IGNORE INTO lead_reject_counterparties (
                    reject_id, role, name, name_normalised, ticker, cik
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    reject_id,
                    party["role"],
                    party["name"],
                    party["name_normalised"],
                    party.get("ticker"),
                    party.get("cik"),
                ),
            )
    return reject_id


def insert_lead(conn, run_id, planned, created_at):
    lead_id = lodge_lead(
        conn,
        planned.lead_type,
        planned.ticker,
        planned.exchange,
        planned.source_scout,
        run_id,
        figure_ids=planned.figure_ids,
        company=planned.company,
        source_url=planned.source_url,
        summary=planned.summary,
        parent_position_id=planned.parent_position_id,
        created_at=created_at,
    )
    rating = planned.rating
    with conn:
        conn.execute(
            """
            UPDATE leads SET
                significance_magnitude = ?,
                significance_magnitude_rationale = ?,
                significance_novelty = ?,
                significance_novelty_rationale = ?,
                significance_attention = ?,
                significance_attention_rationale = ?,
                significance_timing = ?,
                significance_timing_rationale = ?,
                updated_at = ?
            WHERE lead_id = ?
            """,
            (
                rating.magnitude,
                rating.magnitude_rationale,
                rating.novelty,
                rating.novelty_rationale,
                rating.attention,
                rating.attention_rationale,
                rating.timing,
                rating.timing_rationale,
                utc_now(),
                lead_id,
            ),
        )
    return lead_id


def set_status(conn, lead_id, status, priority=None):
    with conn:
        if priority is None:
            conn.execute(
                "UPDATE leads SET status = ?, updated_at = ? WHERE lead_id = ?",
                (status, utc_now(), lead_id),
            )
        else:
            conn.execute(
                "UPDATE leads SET status = ?, priority = ?, updated_at = ? WHERE lead_id = ?",
                (status, priority, utc_now(), lead_id),
            )


def insert_thesis(conn, run_id, lead_id, thesis, written_at):
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO theses (
                lead_id, run_id, thesis_text, risks, quality_score, written_at,
                recommendation, assumptions, roi, timeframe_days, price_run,
                macro_context, pass_number
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_id,
                run_id,
                thesis.markdown,
                thesis.risks_text,
                written_at,
                thesis.recommendation,
                thesis.assumptions_text,
                thesis.roi,
                thesis.timeframe_days,
                thesis.price_run,
                thesis.macro_context,
                thesis.pass_number,
            ),
        )
        thesis_id = cursor.lastrowid
    for criterion in thesis.criteria:
        grade = thesis.grades_by_name.get(criterion["name"])
        if grade is None:
            passed = 0
            evidence = "not graded"
        else:
            passed = 1 if grade.get("passed") else 0
            evidence = grade.get("evidence")
        record_criterion_result(
            conn,
            lead_id,
            criterion["criterion_id"],
            passed,
            run_id,
            evidence=evidence,
            source_url=thesis.source_url,
            assessed_at=written_at,
        )
    return thesis_id


def insert_merge(conn, run_id, survivor_id, constituent_ids, rationale, evidence_refs, created_at):
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO legate_merges (
                created_at, survivor_lead_id, rationale, evidence_refs, run_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (created_at, survivor_id, rationale, evidence_refs, run_id),
        )
        merge_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO legate_merge_leads (merge_id, lead_id) VALUES (?, ?)",
            [(merge_id, lead_id) for lead_id in constituent_ids],
        )
    return merge_id


def set_merge_rank(conn, merge_id, rank):
    with conn:
        conn.execute(
            "UPDATE legate_merges SET priority_rank = ? WHERE merge_id = ?",
            (rank, merge_id),
        )


def insert_review(conn, run_id, thesis_id, review, critique_body, written_at):
    with conn:
        conn.execute(
            """
            INSERT INTO praetorian_reviews (
                thesis_id, run_id, assumptions_that_fail, bad_sources,
                contradicting_research, other_side, surviving_issues, written_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thesis_id,
                run_id,
                review["assumptions_that_fail"],
                review["bad_sources"],
                review["contradicting_research"],
                review["other_side"],
                int(review["surviving_issues"]),
                written_at,
            ),
        )
        verdict = "accept" if int(review["surviving_issues"]) == 0 else "revise"
        conn.execute(
            """
            INSERT INTO critiques (
                thesis_id, run_id, critique, verdict, round, written_at
            ) VALUES (?, ?, ?, ?, 1, ?)
            """,
            (thesis_id, run_id, critique_body, verdict, written_at),
        )


def insert_ranking(conn, run_id, lead_id, rank, roi, timeframe_days, issues, risk_count, rationale, presented, created_at):
    with conn:
        conn.execute(
            """
            INSERT INTO rankings (
                run_id, lead_id, rank, roi, timeframe_days, surviving_issues,
                risk_count, rationale, presented, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                lead_id,
                rank,
                roi,
                timeframe_days,
                issues,
                risk_count,
                rationale,
                1 if presented else 0,
                created_at,
            ),
        )


def insert_decision(conn, run_id, lead_id, created_at):
    with conn:
        conn.execute(
            """
            INSERT INTO decisions (
                lead_id, run_id, status, created_at, updated_at
            ) VALUES (?, ?, 'pending', ?, ?)
            """,
            (lead_id, run_id, created_at, created_at),
        )


def search_rejects(conn, ticker=None, counterparty=None):
    clauses = []
    params = []
    if ticker:
        clauses.append("upper(r.ticker) = upper(?)")
        params.append(ticker.strip())
    if counterparty:
        clauses.append(
            """
            EXISTS (
                SELECT 1 FROM lead_reject_counterparties AS c
                WHERE c.reject_id = r.reject_id
                  AND (
                    c.name_normalised = ?
                    OR upper(COALESCE(c.ticker, '')) = upper(?)
                  )
            )
            """
        )
        params.extend([normalise_name(counterparty), counterparty.strip()])
    if not clauses:
        raise ValueError("ticker or counterparty is required")
    rows = conn.execute(
        f"""
        SELECT r.*
        FROM lead_rejects AS r
        WHERE {" AND ".join(clauses)}
        ORDER BY r.filing_date, r.ticker, r.reject_id
        """,
        params,
    ).fetchall()
    results = []
    for row in rows:
        parties = conn.execute(
            """
            SELECT role, name, name_normalised, ticker, cik
            FROM lead_reject_counterparties
            WHERE reject_id = ?
            ORDER BY role, name_normalised
            """,
            (row["reject_id"],),
        ).fetchall()
        results.append((row, parties))
    return results


def _rating_attr(rating, name):
    if rating is None:
        return None
    return getattr(rating, name)
