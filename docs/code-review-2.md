# Code review 2 (Codex, read-only, code only)

Reviewed commit: 5b2cb44 (before the fixes below). Line numbers refer to that commit.

## Status of the findings

| # | Status |
|---|---|
| 1, 2, 4, 11, 12 (knowledge) | fixed in bccfb5b (12: corrected project; folder grouping of documents still open) |
| 3, 9, 10 (release evidence, severities, sheets) | fixed in 5e016e0, 1244812 |
| 5 (sheet merges) | partly fixed in 1244812: clip/duplicate rules; 'continued' merge still keeps only the first layer's properties |
| 6, 7 (review binding, concurrency) | fixed in 84304cb |
| 8, 13, 14, 15, 16, 17 | fixed in 1244812 |
| 18 (classified vs assembled hole inventory) | open |
| 19 (dedupe before knowledge/corrections) | open |

## Report

Ranked by expected impact on stored records. **E2E** means verified by reading the path through application, scoring and storage; no pipeline or tests were run. Auto-release examples assume an otherwise clean new record and the default 0.90 threshold.

1. **Learned CRS confirmations can manufacture certainty — E2E.** [knowledge.py:164](pipeline/knowledge.py:164), [store.py:239](pipeline/store.py:239). Three identical correction submissions for one hole count as three confirmations. Knowledge then clears coordinate ambiguities and assigns 0.90—even without a reference position. For example, an AZ log with `E 500000 N 4500000`, no CRS/place, can receive learned UTM 39 although it uses UTM 38. **Fix:** count independent correction evidence; require a geographic cross-check; preserve axis/notation ambiguity; prioritize specific scopes over confirmation totals.

2. **Term rules overwrite correctly identified main constituents — E2E.** [knowledge.py:193](pipeline/knowledge.py:193). A single rule `caliche → limestone` changes `primary=sand` for `"SAND, trace of caliche"`. Neither confirmation count nor source limits this, and the resulting note does not lower the score. Later global rules can also override document rules by insertion order. **Fix:** use rules as suggestions for unknown lithology; require primary-material evidence before replacing an existing class, with explicit scope precedence and conflict blocking.

3. **The depth-evidence gate can discard contradictions or ignore most of a record — E2E.** [scoring.py:44](pipeline/scoring.py:44), [scoring.py:110](pipeline/scoring.py:110). Printed boundaries `1, 2, 3` versus extracted `1.4, 2.4, 3` produce agreement `1/5`; `prints_depths()` ignores integers, so disagreement becomes “not printed.” Coverage from 0 to stated 3 then permits release. Separately, three confirmed boundaries on one PDF page can approve additional unconfirmed scan pages because skipped layers never enter the denominator. **Fix:** track evidence and coverage per boundary/page; recognize integer depth labels; never turn detected contradictions into absent evidence.

4. **Grid RMS does not establish predictive accuracy — E2E.** [knowledge.py:105](pipeline/knowledge.py:105), [knowledge.py:182](pipeline/knowledge.py:182). Three distinct printed points all manually placed at `(48, 8)` yield a zero-scale Helmert fit with RMS zero. Other holes collapse to that point at confidence 0.90. Extrapolation is also unrestricted, and reviewer position accuracy is ignored. **Fix:** reject collapsed/ill-conditioned fits, incorporate control-point uncertainty, validate predictions independently, and limit extrapolation. Training RMS alone must not authorize release.

5. **Sheet cleanup removes conflicting observations before validation — E2E.** [assemble.py:250](pipeline/assemble.py:250), [assemble.py:279](pipeline/assemble.py:279), [assemble.py:285](pipeline/assemble.py:285). Examples:
   - Page 1: sand `0–5`, clay `5–10`; page 2: limestone `0–15`. Matching *any* earlier top clips limestone to `10–15`, hiding the contradiction.
   - Equal intervals with different lithologies silently discard the later observation.
   - `"SAND (continued), loose"` extends an earlier dense-sand layer while discarding the new description/properties.
   - Any adjacent unknown layer across a page break inherits lithology without continuation evidence.

   **Fix:** require verified continuation and compatible properties; retain conflicting observations and all source references for review.

6. **Approval is not bound to the complete content reviewed — E2E.** [store.py:89](pipeline/store.py:89), [api/main.py:183](api/main.py:183). Changing approved UCS from `10` to `100 MPa`, core recovery, samples, length or elevation datum leaves the review hash unchanged. Approval survives. Also, a reviewer can approve an old browser view after a rebuild changes the record: the request carries no expected version. **Fix:** hash all substantive reviewed/exported fields; require that hash on review submissions and reject stale approvals.

7. **Rebuilds and concurrent corrections can lose saved fixes — E2E.** [__main__.py:219](pipeline/__main__.py:219), [api/main.py:307](api/main.py:307). A rebuild reads corrections; a reviewer then corrects sand to clay; the rebuild subsequently replaces the record with its earlier sand snapshot. The correction remains in its table but disappears from current data until another rebuild. Concurrent correction requests can similarly overwrite each other. Separate connections and WAL do not prevent this. **Fix:** re-read/apply corrections within the replacement transaction, serialize read-modify-write operations, and check revisions before saving.

8. **Core-loss inference spends the same recovery budget repeatedly — E2E.** [assemble.py:308](pipeline/assemble.py:308). In a `0–10 m` run with TCR 90%, two gaps of `0.8 m` each both pass the individual `≤1 m` check. Assembly invents `1.6 m` of loss, removes both blocking gaps, and can release the record. Even one permitted gap is not proof that recovery loss occurred there. **Fix:** reconcile total loss per run and keep inferred placement subject to review unless supported by printed evidence.

9. **Several added validation checks do not affect release — E2E.** [schema/borehole.py:449](schema/borehole.py:449), [scoring.py:21](pipeline/scoring.py:21). `TCR=60, RQD=80` creates an issue but incurs neither a penalty nor a blocker. Thus the RQD fix still permits release. A single inferred boundary deducts exactly 0.10, leaving confidence at the approval threshold. **Fix:** assign typed severities to findings; contradictory recovery values and uncertain boundaries must block or impose a cap strictly below release.

10. **Sheet completeness deliberately ignores severe incompleteness — E2E.** [assemble.py:212](pipeline/assemble.py:212). Only `"1 of 3"` is extracted, with layers `0–8` and reported final depth `8`. Since two missing sheets exceed one observed sheet, the function returns no finding; the truncated log can auto-release. **Fix:** distinguish report pagination explicitly; never suppress a known per-hole sheet total because many sheets are absent.

11. **Removing knowledge cannot undo its influence on cached extraction — E2E.** [llm.py:358](pipeline/llm.py:358), [knowledge.py:235](pipeline/knowledge.py:235). Knowledge enters the prompt without its confirmation count but is excluded from the cache identity. If extraction incorporates it into `crs_raw`, lithology or descriptions, deleting the item merely rebuilds that conditioned output. A copied CRS may already score 0.95 and bypass the guarded knowledge application entirely. **Fix:** retain knowledge dependencies/provenance in cached outputs; invalidate affected extractions on withdrawal, or keep learned assertions outside observational extraction entirely.

12. **Knowledge uses the wrong effective scope — E2E.** [knowledge.py:44](pipeline/knowledge.py:44), [__main__.py:219](pipeline/__main__.py:219). Extraction says project `Old Road`; a persistent reviewer correction says `New Road`. Old Road’s term rules run first, then the header correction changes the project. New Road receives geology learned elsewhere. Separately, `reports/A.pdf` and `reports/B.pdf` both have document scope `reports`, sharing document corrections, knowledge and grid controls. **Fix:** establish corrected scope metadata before applying knowledge, and represent document groups explicitly rather than truncating every path at its first separator.

13. **Near-rival detection ignores the uncertainty of the place estimate — E2E.** [georef.py:448](pipeline/georef.py:448). For `E 500000 N 530000`, EPSG:32632, the two axis readings are roughly 42 km apart. A place estimate 1 km from the labelled reading with precision 50 km cannot distinguish them. Nevertheless, the rival exceeds `max(25, best_distance*3)`, so the labelled reading can receive 0.95. Positive/negative westing alternatives have the same weakness. **Fix:** evaluate rivals against the place uncertainty and ranking margin, retaining sign/axis ambiguity when both readings are plausible.

14. **Document CRS voting counts duplicated mentions as independent evidence — E2E.** [assemble.py:340](pipeline/assemble.py:340), [assemble.py:429](pipeline/assemble.py:429). Twenty old MGA94 coordinate rows outweigh one explicit MGA2020 project statement. Logs without their own CRS inherit MGA94 at confidence up to 0.90; the small datum displacement need not trigger rivalry. **Fix:** deduplicate statements by source/applicability, detect conflicting systems, and require resolution of that conflict instead of choosing a frequency winner.

15. **Lab normalization publishes unsupported point values and units — E2E.** [assemble.py:121](pipeline/assemble.py:121), [assemble.py:156](pipeline/assemble.py:156). UCS `"1500"` with unit `"psi"` becomes `1500 MPa`; unknown units fall through unchanged. `"<5"` and `"5–10"` become exact value `5`. These values do not reduce release confidence. **Fix:** whitelist supported units, preserve comparators/ranges structurally, and flag unsupported normalization instead of emitting a canonical number.

16. **An added-layer correction can silently stop correcting lithology — E2E.** [corrections.py:107](pipeline/corrections.py:107). A reviewer adds missing `2–4 m CLAY`. A later extraction supplies `2–4 m SAND`; matching depths make `_layer_add()` return success without restoring clay or reporting conflict. **Fix:** compare substantive content, apply the reviewer’s asserted fields, or mark the correction stale.

17. **Replacing a source in place leaves released records stale — E2E.** [ingest.py:107](pipeline/ingest.py:107). Saving a corrected PDF or DOCX PDF export at the same path leaves `same_source=True`. Rendering returns the old manifest/images, so even a normal rerun uses old extraction inputs. **Fix:** fingerprint source content and rendering settings; regenerate when either changes.

18. **“Complete document” does not mean successfully assembled records — E2E.** [__main__.py:194](pipeline/__main__.py:194), [assemble.py:555](pipeline/assemble.py:555). Classification lists BH1, but a valid extraction returns `logs=[]`; alternatively, BH1 assembly raises and is caught. The document still counts as complete, so replacement deletes its previous BH1 without a review record. **Fix:** reconcile classified versus extracted/assembled hole inventories and propagate assembly failures into document replacement eligibility.

19. **Dedupe still chooses before the important checks and corrections — E2E.** [assemble.py:585](pipeline/assemble.py:585), [__main__.py:217](pipeline/__main__.py:217). Duplicate A with extraction confidence `1.0` and position confidence `0.5` beats B at `0.95/0.95`, moving usable data into review. Worse, corrections attached to discarded B never run, and its human decision can disappear during document replacement. **Fix:** deduplicate after knowledge/corrections, consider full release eligibility, and preserve stable aliases linking corrections and decisions.

The five changes I would make first:

1. Replace aggregate depth agreement with boundary/page evidence coverage and enforce blocking validation severities.
2. Constrain knowledge application: independent confirmations, effective scopes, safe term matching, and validated grid predictions.
3. Make sheet merging preserve conflicting observations and continuation properties.
4. Add complete content versions, stale-review rejection, and transactional correction replay.
5. Track knowledge dependencies in extraction caches so withdrawing a rule actually removes its effects.