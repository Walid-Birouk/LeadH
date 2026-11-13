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

The default is `brave+guess`. Whatever finds a candidate, it only counts as the company's site when one of its
pages shows the SIREN.

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
appear in its own quote. LinkedIn and other networks are never read. `{company}`, `{city}`, `{site}` and
`{email_source}` come from the lead record. `{slot|fallback}` gives the words to use when nothing on the site
supports the slot; without a fallback the draft is marked `needs_edit` and shows `[[slot]]` for you to finish.

With two templates, each lead gets variant A or B from a hash of the campaign name and its SIREN: the same lead
always gets the same variant, in any run order, and roughly half go each way. `lead_count` caps the drafts,
best-ranked leads first. Recording `sent` also adds the SIREN to the contacted ledger (`data/ledger.db`, shared by every
campaign along with the opt-out list), so no campaign drafts that company again.

## Drafts in your mailbox

`uv run leadh --campaign <file> to-mailbox` saves every ready draft into your mailbox's Drafts folder over IMAP,
best leads first, each once. LeadH has no sending code: you open each draft in your mail app, read it, edit it and
send it yourself, then record it with `outcome <lead> sent`. Drafts marked `needs_edit`, and companies already
contacted or opted out, are left out. Each draft carries an `X-LeadH-Lead` header with its lead, campaign and
variant.

Set `LEADH_MAIL_USER` and `LEADH_MAIL_IMAP_HOST` in `.env` (`outlook`, `gmail`, `ovh`, `icloud`, `yahoo`, or a
host name). Gmail, OVH, iCloud and Yahoo take an app password in `LEADH_MAIL_APP_PASSWORD`.

Outlook.com and Microsoft 365 no longer accept any password over IMAP (since September 2024); they need an OAuth
sign-in, which needs an app ID of your own, once:

1. Sign in at [portal.azure.com](https://portal.azure.com) (a free Azure account is enough) and open
   Microsoft Entra ID, then App registrations, then New registration.
2. Name it LeadH. Supported account types: "Personal Microsoft accounts only" for an @outlook / @hotmail
   address, or "Accounts in any organizational directory and personal Microsoft accounts" for a work address.
   No redirect URI.
3. In Authentication, set "Allow public client flows" to Yes and save.
4. Copy the Application (client) ID into `LEADH_MAIL_MS_CLIENT_ID`.

The first `to-mailbox` prints a code to enter at microsoft.com/devicelogin and asks you to allow IMAP access.
The sign-in is then cached in `data/ms_token_cache.json` (never committed) and renewed silently.

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
