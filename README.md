# LeadH

Finds French SMEs that must start issuing e-invoices on 1 September 2027 and show no sign of being ready, checks
that each one is a lawful, reachable B2B lead, and produces a daily scored batch for the Write stage.

It is the Find stage of the outreach pipeline: eight steps, of which only the Signal reader uses a model.

| Step | Kind | Output |
|---|---|---|
| Registry scout | code | SME candidates from the public Recherche d'entreprises API |
| Site matcher | code | the company's own site, proven by its SIREN on a page |
| Tech detector | code | invoicing tools visible in the site's code |
| Signal reader | small LLM, no tools | signals, each with a quote and its URL |
| Evidence check | code | only signals whose quote is on the page |
| Scorer | code | need and fit out of 100, every point traced to a signal |
| Validator | code | APPROVED / CONDITIONAL / REJECTED with reason codes |
| You | human | decide CONDITIONAL leads |

Each lead's status is its checkpoint (`found → matched → read → checked → scored → validated → handed_off`, with
side exits `no_site`, `parked` and `rejected`), so a run that stops halfway resumes where it left off.

## Run

```
uv sync
cp .env.example .env        # add DEEPSEEK_API_KEY, or LLM_BASE_URL=mock to run offline
uv run leadh run            # one morning run; prints the daily report
uv run leadh review         # CONDITIONAL leads with quotes and links
uv run leadh decide L-000412 approve plateforme_fictive
uv run leadh optout domain example.fr
uv run leadh purge          # retention: pages after 30 days, never-contacted leads after 6 months
```

Settings, weights and limits are in `config/leadh.yaml`. `shadow_mode: true` (the default) writes verdicts but
hands nothing off; switch it off once you agree with 80% of APPROVED leads for five days in a row. Hand-offs are
appended to `data/handoff/<day>.jsonl`.

## Site matching

`search.provider` picks how candidate sites are found:

- `guess` (default) tries the company name as a `.fr` or `.com` domain. Free, and only a page showing the SIREN
  counts as a match, but it misses sites whose domain differs from the legal name.
- `brave` uses the Brave Search API (`BRAVE_API_KEY`). Check its terms allow this use first.
- `none` matches nothing.

## Rules the code enforces

- Companies with Sirene diffusion status P, or flagged as opposed to reuse of their data for prospecting
  (`opposition_diffusion_commerciale`, GDPR art. 21), are dropped before they are stored.
- Only addresses a company publishes on its own site are used; none are guessed. A site with only a contact form
  is rejected as `no_published_contact`. Every lead keeps `contact.source_url` so the email can say where the
  address came from.
- Crawling stays on the company's own domain, respects robots.txt, waits a second between requests and names
  itself in the user agent.
- The e-invoicing directory (annuaire) is checked by hand only.
- The report carries lead IDs and reason codes, never names or addresses.

## Develop

```
uv run ruff check . && uv run mypy && uv run pytest
```
