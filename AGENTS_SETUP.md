# cmvijay-agents · Setup Guide

**Path B — autonomous agents with public transparency.**

This is a real agentic system for cmvijay.ai. Three agents (Watcher, Verifier, Drafter) run once daily, verify claims using tool-use, and either auto-commit routine entries or escalate uncertain ones to you via GitHub Issues. Every decision is publicly auditable at `cmvijay.ai/agents`.

**Build time: 3-4 focused weekends. This document is the setup for the first weekend.**

---

## What you're deploying

```
cmvijay-agents/
├── agents/
│   ├── shared/           # Claude client (tool-use), whitelist, logger, feeds
│   ├── watcher/          # RSS polling (5 whitelisted outlets)
│   ├── verifier/         # Agentic core — Claude + tools verify each candidate
│   ├── drafter/          # Writes timeline entries in site voice
│   ├── tools/            # web_search, web_fetch, read_site_state, read_manifesto_promise
│   ├── prompts/          # verifier_v1.md, drafter_v1.md
│   ├── state/            # known_claims.json, orchestrator, committer
│   └── log/              # Daily agent decision logs (JSONL)
├── .github/workflows/
│   ├── cmvijay-agents.yml               # Daily 19:30 IST run
│   └── cmvijay-agents-approval-bot.yml  # /approve /skip /edit /undo handler
└── public/agents/
    └── index.html        # The /agents transparency page
```

---

## Step 1 — Extract into your repo

Assuming you're on Mac (`~/Documents/source/cmvijay/`):

```bash
cd ~/Documents/source/cmvijay
tar -xzf ~/Downloads/cmvijay-agents.tar.gz --strip-components=1
git status  # should show new files: agents/, .github/workflows/, public/agents/
```

On Windows (Git Bash, `~/source/repos/cmvijay/`):

```bash
cd ~/source/repos/cmvijay
tar -xzf ~/Downloads/cmvijay-agents.tar.gz --strip-components=1
git status
```

## Step 2 — Set up GitHub secrets

Go to `https://github.com/ssriramhere/cmvijay/settings/secrets/actions` and add:

**Required:**
- `ANTHROPIC_API_KEY` — your Anthropic API key (from console.anthropic.com)

**Optional (recommended):**
- `SERPER_API_KEY` — for web_search tool. Free tier: 2,500 queries/month. Get it at serper.dev. If not set, Verifier falls back to DuckDuckGo scraping (less reliable but works).

**Already available (no action needed):**
- `GITHUB_TOKEN` — automatically provided by GitHub Actions.

## Step 3 — Configure repo permissions

Go to `https://github.com/ssriramhere/cmvijay/settings/actions` and ensure:
- **Workflow permissions**: "Read and write permissions" (required for autonomous commits)
- **Allow GitHub Actions to create and approve pull requests**: enabled

## Step 4 — Verify local install (optional dry-run)

If you want to test locally before pushing:

```bash
cd ~/Documents/source/cmvijay
export ANTHROPIC_API_KEY="sk-ant-..."
export SERPER_API_KEY="..."  # optional
pip install anthropic requests feedparser
python agents/watcher/watcher.py    # Should fetch RSS, write candidates.json
python agents/verifier/verifier.py  # Should verify each — this uses API calls (~$0.20-$1)
python agents/drafter/drafter.py    # Should draft surfacing candidates
```

Inspect `agents/state/candidates.json`, `agents/state/verified_candidates.json`, and `agents/state/drafts.json` after each step.

**Note**: local dry-run won't auto-commit or create GitHub Issues (those require GitHub Actions context). It's purely for validating the agent chain works.

## Step 5 — Commit and push

```bash
git add agents/ .github/workflows/ public/agents/
git commit -m "Deploy cmvijay-agents (Path B): 3-agent autonomous pipeline with public transparency page"
git push
```

**Do NOT commit the following** (should be `.gitignore`d or handled by workflow):
- `agents/state/candidates.json`
- `agents/state/verified_candidates.json`
- `agents/state/drafts.json`
- `agents/state/seen.json`
- `agents/state/daily_digest.md`

The workflow itself will manage state through `actions/cache@v4`.

## Step 6 — First manual run

Once pushed:

1. Go to `https://github.com/ssriramhere/cmvijay/actions`
2. Click **cmvijay-agents** in the sidebar
3. Click **Run workflow** on the right
4. Watch the run complete (~2-5 minutes depending on candidate volume)

What should happen:
- Watcher fetches ~5-30 candidates from RSS
- Verifier calls Claude with tools ~5-30 times (one per candidate, each ~2-6 API calls)
- Drafter drafts entries for `surface_*` decisions
- For `surface_autonomous`: auto-commit to `index.html` (max 5/day)
- For `surface_escalate`: GitHub Issue created with full reasoning
- Daily digest issue created summarizing everything

## Step 7 — Verify /agents page

The `/agents` page at `cmvijay.ai/agents` will initially show placeholder text ("Stats will populate after first agent run"). After the first run completes, you need to generate `stats.json`, `entries.json`, and `corrections.json` in `public/agents/`. This can be done manually or automated as a workflow step in Weekend 2.

For the first weekend, generating these files manually is fine — I'll walk through it after your first run.

## Step 8 — Cleanup old watcher (recommended)

The old `cmvijay-watcher.yml` (parked, disabled) can now be removed:

```bash
rm .github/workflows/cmvijay-watcher.yml
rm .github/scripts/watcher.py  # if it exists
rm .github/state/seen.json     # if it exists
git commit -am "Remove parked Phase 0 watcher (superseded by cmvijay-agents)"
git push
```

---

## Cost expectations

**API costs per day** (5 candidates verified, 3 drafted):
- Verifier: 5 candidates × ~4 API calls × ~$0.05/call = ~$1.00
- Drafter: 3 drafts × ~$0.02/draft = ~$0.06
- **Daily: ~$1.06 · Monthly: ~$32**

**Higher-volume days** (10-15 candidates on major news days): ~$3-5/day.

**Monthly steady state: $50-100.**

## What to watch for on Day 1

- **Zero candidates surfaced** — RSS may be quiet; normal if occurring 1-2 days consecutively. If 3+ days, tune keywords in `agents/shared/feeds.py`.
- **Verifier errors** — check `agents/log/YYYY-MM-DD.jsonl` for `"type": "error"` events. Common causes: SERPER_API_KEY missing (falls back to DDG which sometimes rate-limits), URL fetch timeouts.
- **Drafter parse failures** — Claude occasionally returns malformed JSON. Log shows raw response; iterate on drafter prompt if this recurs.
- **Autonomous commit failures** — safety rails require specific `TIMELINE` and `SOURCES` array markers in `index.html`. If those markers move due to formatting changes, autonomous commits fail gracefully and escalate to issue.

## Rate limits & safety rails

- Max **5 autonomous commits per day** (rate-limited in `orchestrator.py`)
- Excess autonomous candidates escalate to GitHub Issues
- All autonomous commits signed as `cmvijay-agent` — `git log --author=cmvijay-agent` shows agent history
- `/undo <sha>` on the daily digest reverts any autonomous commit
- Autonomous commits **only APPEND** to TIMELINE and SOURCES arrays — never modify existing entries

## Weekend 2 preview

- Generate `stats.json`, `entries.json`, `corrections.json` for the `/agents` page (auto in workflow)
- First round of prompt tuning based on Weekend 1 output
- Ship any Verifier / Drafter tweaks

## Weekend 3-4 preview

- Prompt refinement across live operation
- Any additional watchers (e.g. Assembly proceedings) if needed
- CloudDon writeup drafted

---

## Ping list — what to tell me

After your first manual run, share:
- Screenshot / paste of the run's console output
- Content of the daily digest issue
- Any surfaced issues that felt off (over-caution, wrong framing, missed context)

Then we'll iterate.
