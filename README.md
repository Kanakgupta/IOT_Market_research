# Wireless Intelligence: Wi-Fi & Bluetooth Market Research Agent

An agent that researches the Wi-Fi, Bluetooth and short-range wireless industry on demand locally or on a cloud schedule. It publishes a static website with a daily report, company pages, a News tab and an Applications tab. Every item links to its public source.

## What you get

The website uses a light, blue/violet intelligence-hub design with an interactive wireless ecosystem illustration, source-linked story spotlights, and application discovery cards. Shared navigation and responsive layouts work across the report, company, news, and application pages. Decorative entrance, signal, and chart animations finish within four seconds and are disabled when the browser requests reduced motion. No external UI libraries, fonts, or image services are required.

| Page | What it shows |
|---|---|
| `site/index.html`: **Daily Report** (landing page) | KPIs, executive summary, themes (Wi-Fi 7/8, LE Audio, M&A…), region and segment mix, top stories, deals, launches, partnerships, regulation, earnings, a sortable/filterable **company report** and a **per-company brief** ("what each company is up to") |
| `site/company.html?id=…` | Wireless business model, product families, agent brief, theme mix, recent announcements, all news from the last 30 days, the applications the company plays in, peers, and the exact public queries used |
| `site/news.html`: **News** | About 900 de-duplicated stories with filters: technology, region (Americas / Europe / China / APAC), theme, topic feed, time window, press releases only, and search |
| `site/applications.html`: **Applications** | 40 applications in the left panel. For each: major players, chip suppliers, a **10-year market trend** built from public forecasts (evidence table with links; outliers flagged), analyst signals, and latest news |
| `site/sources.html` | Methodology, every query from the last run as a clickable link, run statistics and the agent log |

Coverage is defined in `agent/catalog_companies.py` and `agent/catalog_applications.py`:
- 124 companies: chip vendors (well-known and niche, including China), software/IP, network equipment, modules/RF, test, standards bodies, 30+ OEMs and AI-device makers
- 40 applications
- 23 industry topics and 7 regional news editions

Edit these files to add or remove coverage. No other code changes are needed.

## Run it (Windows scripts in `Script\Windows`)

| Script | What it does |
|---|---|
| `Run_Agent.bat` | Double-click to run the research agent (full run, ~4 minutes). It refreshes `site\data\*` and writes a log to `agent\logs\`. Options: `-Quick` (30-second smoke test), `-Open` (opens the report when done). |
| `Start_Server.bat` | Double-click to start the local web server and open http://localhost:8080 in your browser. If 8080 is busy, it uses the next free port. Options: `-Port 9000`, `-NoBrowser`. Stop with Ctrl+C or by closing the window. |
| `Loop.bat` | Run research immediately, validate generated site data, commit all non-ignored project changes, rebase onto the current branch's upstream, and push. Repeats every 24 hours; stop with Ctrl+C. Requires Git for Windows with GitHub authentication configured. |

Each `.bat` file calls the `.ps1` file of the same name, so you can also run them from PowerShell:

```powershell
C:\guptakanak\AI_Agents\Market_Research\Script\Windows\Run_Agent.ps1 -Open
C:\guptakanak\AI_Agents\Market_Research\Script\Windows\Start_Server.ps1
```

To change the update interval, run `Script\Windows\Loop.ps1 -IntervalMinutes 60`. Add `-Once` to run a single update cycle. The website is static and needs no separate build step; the loop validates every generated JavaScript data file before publishing. Each cycle stages all non-ignored changes, including source, design, and documentation files. It fetches and rebases before pushing, retries once if the remote advances during a push, and confirms the worktree is clean afterward. `.gitignore` excludes logs, virtual environments, environment files, private keys, and certificates.

Requirements: Python 3.10+ on PATH. `Run_Agent` installs `requests` if it is missing. You can also open `site\index.html` directly, without the server.

For a fresh report before 6 AM Pacific, run `Run_Agent.bat` before you leave the PC (on and awake). Each run builds on the stored 30-day history, so a run late in the evening still gives a complete report.

The cloud workflow `.github/workflows/nightly.yml` requests a daily run at 09:17 UTC (1:17 AM PST / 2:17 AM PDT), leaving a buffer before 6 AM Pacific. GitHub Actions schedules are best-effort and can be delayed; this is not a guaranteed delivery deadline. Cloud runs research and publish `site/` without your PC. No Windows scheduled task or wake timer is installed.

### Local versus online reports

- **Local:** http://localhost:8080/index.html#exec serves this checkout's `site/` folder. Starting the server does not run research or download cloud updates. Stop the server before closing its window; if it chooses a different port, use the URL it prints.
- **Online:** https://kanakgupta.github.io/IOT_Market_research/ serves the last successful GitHub Pages deployment. A cloud research run commits refreshed data to GitHub, but does not change files on your PC.
- **Refresh local data from cloud:** run `git pull --ff-only` from the repository root, then start `Script\Windows\Start_Server.bat` and reload the page. If Git reports conflicting local edits or divergent history, resolve them before retrying; do not discard local work.
- **Generate locally instead:** run `Script\Windows\Run_Agent.bat`, then reload the local page. This updates local data only unless a publishing command is configured.
- The displayed update time comes from the research snapshot, not the webpage build time. The `#exec` fragment only scrolls to the briefing; it does not trigger a refresh.

### Publishing online

GitHub Pages must be enabled in repository **Settings > Pages > Source: GitHub Actions**. The workflow checks this configuration explicitly. Pushes to `main` that change `site/`, `agent/`, or the workflow publish the committed website immediately using the existing report. Scheduled runs and **Actions > nightly-wireless-research > Run workflow** perform full research, persist the data, and deploy. Check both the research and deployment jobs: a successful research commit alone does not mean the public site was deployed.

`site/` is the complete build/deploy directory; no npm build or bundler is required. All asset and data links are relative so the repository subpath works. If publishing fails, inspect the failed workflow job rather than relying on the Git commit date.

For a different web host, set `WR_PUBLISH_CMD` to a copy or upload command; `Run_Agent` runs it after a successful local run. Examples: `robocopy C:\...\site \\server\wwwroot /MIR`, `az storage blob upload-batch ...`, `aws s3 sync ...`.

### Optional AI narrative
Set `LLM_API_KEY` (plus optional `LLM_BASE_URL` and `LLM_MODEL`, for any OpenAI-compatible endpoint). The executive summary then gets an analyst narrative. It is written **only** from the numbered public headlines and cites them as clickable `[n]` links. Without a key, the summary is fully deterministic.

## How accuracy is protected
- **Public sources only.** Google News RSS (US/UK/DE/IN/CN/TW/JP editions) and Bing News RSS, plus official company websites. Every headline, forecast and query is clickable.
- **Relevance guards.**
  - The company must be named in the headline.
  - Ambiguous brand words (Ring, Echo, Honor, Nothing…) are matched case-sensitively.
  - For diversified companies, the headline must be about wireless, devices or that company's relevant segment.
  - Shopping-deal and fund-holding/analyst-chatter items are dropped.
- **De-duplication and clustering.** Syndicated copies collapse into one story with "+N more outlets".
- **Forecasts.**
  - A forecast only counts if the headline names that application's market.
  - Figures are read from the headline, the snippet, or the public press-release page next to the market phrase.
  - The trend uses the median of the evidence. Outliers are excluded and flagged.
  - If no public evidence exists, the chart says so and shows an indicative growth index.
- **Profiles.** Business-model text covers stable, publicly documented business lines. Time-sensitive facts always come from dated, linked news.
- **State.** `agent/state/` keeps a rolling 40-day article store and a 400-day forecast store, so coverage deepens every night.

> Feed terms: the Google and Bing RSS feeds are licensed for personal, non-commercial use. For a commercial or public site, swap `Fetcher` in `research_agent.py` for a licensed news API (e.g. Bing News Search API, NewsAPI, GDELT).
