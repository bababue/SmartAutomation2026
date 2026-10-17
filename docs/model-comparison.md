# Model comparison: extraction with Opus 5.5 and Haiku 5.5

Which model reads the logs, at what price, and how well. Built from existing runs only; the numbers are
from 17 Oct 2026. How the runs are made: README → Prompt experiments.

## Against human-checked records

Reference: 36 records approved by reviewers (`data/eval/reference.json`, local only). Each arm
re-extracts the pages these records come from with the v3 prompt; classification and lab sheets come from the
same cache for all arms, so only log extraction differs.

The statistics page (Quality & models) recomputes this comparison from the current reference, which has grown
since: it scores both models on the reference records both extracted themselves, so its figures differ from
the table below.

| | Opus 5.5 (Claude subscription) | Haiku 5.5 (OpenRouter) |
|---|---|---|
| Records missing | 0 | 2 |
| Layer boundaries found (within 0.1 m) | 95.2 % | 89.8 % |
| Boundaries that are in the reference (precision) | 92.3 % | 83.5 % |
| Same lithology at the same depth (log length) | 94.4 % | 87.6 % |
| Final depth | 97.2 % | 86.1 % |
| Groundwater levels | 93.3 % | 73.3 % |
| Lab values on the log | 95.4 % | 81.5 % |
| Largest position error (median 0 m for both) | 7.0 km | 6.5 km |
| Seconds per page (median) | ≈ 30 | 29 |
| Tokens per page in / out (median) | not logged by the CLI | 11.9 k / 7.1 k |
| Cost per page | ≈ $0.14 at API prices (estimate), on the subscription quota otherwise | $0.005 (billed) |
| Cost for all ≈ 700 log pages | ≈ $95 at API prices | ≈ $3.50 |

Two plain Opus runs agree with each other on 98 % of the boundaries and 97 % of the
lithology metres: differences of a few points between arms are within that spread.

The reference was approved from earlier Opus output, which favours Opus somewhat. The Opus column is a fresh
re-extraction, not that earlier output.

## What the numbers do not show

- Haiku copies the printed field label into raw values ("Location: …", "Ground level: …"), also into the
  place name; Opus rarely does. The v4 prompt asks for the value without its label.
- On a log without coordinates Haiku took a same-named town in another country as the site, which put the
  position thousands of kilometres off; Opus kept the right country. The v4 prompt takes the country only from evidence on the sheet. The scoring caught it:
  a position from a place name alone has confidence 0.3 and always goes to review.
- Asking Haiku to leave out empty fields shortens its output by 3 % only, so it does not get faster or cheaper
  that way (backlog item 11).

## Wider benchmark (agreement only)

Before there were reviewed records, nine configurations read the same 6 log pages from 4 documents with the
same prompt and were compared with Opus. That measures agreement, not accuracy, and 6 pages are enough to see
coarse differences only.

| Model | Route | s / page | $ / page | Boundaries as Opus | Lithology by depth as Opus |
|---|---|---|---|---|---|
| Sonnet 5.5 | OpenRouter | 28 | 0.069 | 100 % | 84 % |
| Gemini 4 Argon | OpenRouter | 30 | 0.035 | 86 % | 95 % |
| GPT-6.1 Sol | OpenRouter | 44 | 0.039 | 97 % | 89 % |
| Haiku 5.5 | OpenRouter | 27 | 0.0046 | 100 % | 85 % |
| Gemini 3.8 Flash | OpenRouter | 32 | 0.026 | 86 % | 88 % |
| GPT-6 Luna | OpenRouter | 29 | 0.0025 | 81 % | 77 % |
| Qwen3-VL 8B, 4 bit | local (Apple M4, 24 GB) | 140–245 | 0 | 30–54 % | 9–52 % |

The local model mismatched holes and put positions thousands of kilometres off; the review rules held all of
its records back.

## Conclusion

Opus stays the extraction model for the corpus. Haiku finds about as many layer boundaries but invents more
(precision 83.5 % against 92.3 %), and it is clearly weaker on groundwater (73 % against 93 %) and lab values
(82 % against 95 %); its place errors need the review rules to catch them. At about 1/30 of the price it is
good for quick uploads and a first look, with every record going through the usual release rules. The Haiku
column ran on 44 of the current 45 documents.
