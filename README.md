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

`search.provider` lists how candidate sites are found, tried in order:

- `brave`: the Brave Search API (`BRAVE_SEARCH_API_KEY`). It searches the SIREN first, with the big directories
  excluded, and then the name and city. Results whose snippet already shows the SIREN are tried first.
- `guess`: tries the trade name, or the legal name, as a `.fr` or `.com` domain. It's free and catches sites
  the search misses.
- `none`: finds nothing.

- `agentreach`: Agent-Reach's web search (Exa semantic search, no key). See below.

The default is `brave+agentreach+guess`. Whatever finds a candidate, it only counts as the company's site when one
of its pages shows the SIREN.

## Agent-Reach

[Agent-Reach](https://github.com/Panniantong/Agent-Reach) is a separate command-line tool. Install it from its
GitHub release (not the PyPI package of the same name, which is someone else's):

```
uv tool install https://github.com/Panniantong/Agent-Reach/releases/latest/download/agent-reach.zip
agent-reach channels        # check it runs
```

LeadH uses three of its features, each switched in the `reach` section of `config/leadh.yaml`:

- **Search** (`search.provider` entry `agentreach`): more candidate sites for the Site matcher.
- **Read fallback** (`reach.read_fallback`): a page that refuses LeadH's crawler (HTTP 401, 403, 406, 429 or 503) is
  read through Agent-Reach instead, and stored with `via: reach`. A page robots.txt disallows is never read this way.
- **LinkedIn job ads** (`reach.linkedin_jobs`): public job search, with no login, for admin, accounting or invoicing
  ads posted by the lead's company. Up to two ads per lead are stored as pages with `via: job_ad`; the Signal reader
  can quote them for `admin_job_ad`, and the Evidence check holds the quote to the ad text. Contacts, tools, language
  and outreach slots never come from an ad.

LeadH never runs Agent-Reach's login channels. If `agent-reach` is not installed, that helper is skipped. When a
channel is rate-limited or busy it rests for the time Agent-Reach gives (`reach.pause_s` otherwise) and is then
tried again; a lead whose site search fell in that rest stays `found` for the next run instead of becoming
`no_site`. Three such answers in a row, or a captcha or login request, turn the channel off for the run.

Public LinkedIn results have no company field, so the posting company is read from the job URL
(`…-at-<company>-<id>`), then the title. It must equal the lead's name (another division does not count), and
ads shown outside France are dropped.

## Campaigns, templates and A/B tests

A campaign file in `campaigns/` bundles one customer profile (a config file: NAF families, scoring, rules), one
lead source (overrides of its `registry` section), one or two templates, and a lead count. Each campaign keeps its
own data under `data/campaigns/<name>/`, so campaigns can run side by side and be compared.

```
uv run leadh --campaign campaigns/einvoicing-it-idf.yaml run --limit 50
uv run leadh --campaign campaigns/einvoicing-it-idf.yaml draft     # fill the templates for APPROVED leads
uv run leadh --campaign campaigns/einvoicing-it-idf.yaml export    # leads.csv with variant, subject, body
uv run leadh --campaign campaigns/einvoicing-it-idf.yaml outcome L-000014 sent    # then replied, meeting, bounced
uv run leadh compare campaigns/*.yaml                              # drafts, sends, replies per variant
```

A template (`templates/*.txt`) is a `subject:` line in lowercase, a `---` line, then the body. You write the
words; the model only fills these slots, from the lead's own pages:

- `{second_decision_maker}`: someone in management named on the site, as "Name, role".
- `{peer_company}`: a customer the site names.
- `{specific_fact}`: one concrete fact the site states about the company.

Each fill must come with a sentence quoted from a page the lead's site served, checked like a signal; a name must
appear in its own quote. Outreach slots never come from LinkedIn or other networks. `{company}`, `{city}`, `{site}` and
`{email_source}` come from the lead record. `{slot|fallback}` gives the words to use when nothing on the site
supports the slot; without a fallback the draft is marked `needs_edit` and shows `[[slot]]` for you to finish.

With two templates, each lead gets variant A or B from a hash of the campaign name and its SIREN: the same lead
always gets the same variant, in any run order, and roughly half go each way. `lead_count` caps the drafts,
best-ranked leads first. Recording `sent` also adds the SIREN to the contacted ledger (`data/ledger.db`, shared by every
campaign along with the opt-out list), so no campaign drafts that company again.

## Rules the code enforces

- Companies with Sirene diffusion status P, or flagged as opposed to reuse of their data for prospecting
  (`opposition_diffusion_commerciale`, GDPR art. 21), are dropped before they are stored.
- Only addresses a company publishes on its own site are used; none are guessed. A site with only a contact form
  is rejected as `no_published_contact`. Every lead keeps `contact.source_url` so the email can say where the
  address came from.
- Crawling stays on the company's own domain, respects robots.txt, waits a second between requests and names
  itself in the user agent. Agent-Reach reads a page only when the site refused LeadH, never when robots.txt does.
- The e-invoicing directory (annuaire) is checked by hand only.
- The report carries lead IDs and reason codes, never names or addresses.

## Develop

```
uv run ruff check . && uv run mypy && uv run pytest
```
