# Post-Merge Autonomous QA Agent

When a pull request is merged, this tool works out **what the change can break**, asks an LLM to write
**QA scenarios**, maps them onto your **existing Playwright suite**, **automates the missing ones**, runs
**only the affected tests**, and reports the result and the **LLM cost** on the PR.

```
PR merged ─► git diff ─► static impact analysis ─► Agent 1 (QA architect)      ─► Agent 2 (automation engineer)
              (files)     (project graph, types,    scenarios + "covered by      writes Playwright specs, runs them,
                           journey map - no AI)      existing test X / missing"   rewrites on failure (self-heal)
                                                                    │
         PR comment + report  ◄─  run affected tests only  ◄────────┘      green → PR with new tests
         (+ cost report)          (existing + new)                          red   → new tests held back for review
```

## What is in this folder

| File | Purpose |
|---|---|
| `../post_merge_qa_agent.py` | The agent (Python standard library only, no `pip install`). |
| `qa-agent.config.json` | **All project-specific settings.** The only file you edit to move to a new project. |
| `prompts/agent1.md`, `prompts/agent2.md` | The LLM prompts. Created from defaults on the first run, then **read from these files** - edit to tune. |
| `pricing.json` | Claude API list prices used by the cost report. |
| `qa_cost.py` | Token and cost calculator. Also runnable on its own (`python qa-agent/qa_cost.py`). |
| `../.github/workflows/post-merge-qa.yml` | Runs the agent after a merge. |

## Quick start (this repository)

1. Merge the files above to `main` (a PR into **your own repo**, not an upstream you forked from).
2. GitHub → **Settings → Actions → General**: tick **Allow GitHub Actions to create and approve pull requests**.
3. Give the agent an LLM (pick one, they are tried in this order):
   - **GitHub Models** - free, nothing to configure (uses `GITHUB_TOKEN`; the input limit is small).
   - **Gemini** - add a repository secret `GEMINI_API_KEY`. The free tier is rate-limited; enabling billing on the key removes that.
   - **Anthropic** - add `ANTHROPIC_API_KEY` (Claude API, billed per token; a Claude.ai subscription does not include API access).
4. Merge any PR that changes application code, or run **Actions → Post-Merge Autonomous QA Pipeline → Run workflow**
   (`base` = the full commit SHA to diff from; leave `dry_run` off).
5. Read the PR comment (report + cost). If everything passed, merge the `qa-agent/tests-…` PR the agent opens.

Try it on your machine first (no LLM, no browser, no Docker): `.\run-qa-local.ps1` (dry run, prints the impact analysis).
Full local run: `.\run-qa-local.ps1 -Mode full -Base <sha> -Head main` (needs Docker, Node, .NET and an LLM key in your shell).

## Using it in another project

You need: an automated test suite in **any framework** (Playwright, Cypress, Jest, Vitest, pytest, or your own command - see *Test frameworks* below), git, GitHub Actions. The agent only reads files and runs your test command.

1. Copy into the target repo: `post_merge_qa_agent.py`, the `qa-agent/` folder (without `prompts/`), `.github/workflows/post-merge-qa.yml`.
2. Edit `qa-agent/qa-agent.config.json` (reference below). Minimum: `project_name`, `project_description`, `src_dir`,
   `e2e_dir`, `suite_description`.
3. Make your framework run the generated tests. For Playwright, in `playwright.config.ts` add the generated folder to a project, e.g.
   `testMatch: ['**/generated/*.auth.spec.ts']` (logged-in) and `'**/generated/*.anon.spec.ts'` (anonymous).
4. Edit the workflow: replace the `.NET` setup step with your runtime, make the app start the way your
   `playwright.config.ts` `webServer` expects, set `QA_GENERATED_DIR` to your `generated_dir`.
5. Run it once with `dry_run` ticked and read the log: *changed projects*, *impacted journeys* and *inventory* must look right.
   If not, fix `spec_map` / `src_dir` in the config.
6. Run for real. The first run creates `qa-agent/prompts/*.md`; the workflow commits them with the first generated-tests PR.

### Config reference (`qa-agent.config.json`)

| Key | Meaning |
|---|---|
| `project_name`, `project_description`, `suite_description` | Plain-language context injected into both prompts. The better this describes your app, the better the scenarios. |
| `src_dir`, `e2e_dir` | Application source and automated tests (default test folder depends on the runner preset). |
| `runner` | **Which test framework** and how to run it - see *Test frameworks* below. |
| `keep_env` | Secret-looking environment variables the tests really need (everything matching TOKEN/PASSWORD/... is hidden from test processes unless listed here). |
| `generated_dir`, `login_setup` | Where new specs go; the Playwright login setup file (`""` if none). |
| `source_extensions` | Source file types scanned for the type graph (`.cs`, `.razor`, `.ts`, `.tsx`, ...). |
| `spec_map` | **Component → journeys.** Which e2e specs exercise each component (folder or .NET project) under `src_dir`. |
| `default_specs` | Journeys for components not in `spec_map`: `"all"` (safe default) or `[]`. |
| `all_specs`, `auth_specs` | Spec names (without `.spec.ts`); the second list runs as the logged-in user. Empty `all_specs` = discovered from `e2e_dir`. |
| `global_files` | Repo-wide inputs (lock files, build props...): a change here considers every journey. |
| `shared_dir`, `excluded_projects`, `generic_types` | Shared code folder; components to ignore; type names too generic to follow. |
| `helper_files` | Existing helper files Agent 2 must reuse (page objects, utilities). |
| `ui_markup` | `dirs` + `extensions` of UI files Agent 2 reads to pick real locators. |
| `test_data` | **Seed data files the agent may use** (name, price...). `line` is the format per item, e.g. `"- {Name} (${Price})"`. Stops the LLM inventing data. |
| `prompt_vars` | Project-specific text filled into the prompts (helper rules, login rules, example values). |
| `cost` | `free_providers`, `reference_models` and `prs_per_month` for the cost report. |

Anything not set falls back to a generic default, so a minimal config works.

## Test frameworks (the `runner` section)

The pipeline never calls a framework directly. Everything framework-specific is the `runner` block of the config:

```json
"runner": { "preset": "pytest" }
```

| Preset | Test files | Run command | Notes |
|---|---|---|---|
| `playwright` (default) | `*.spec.ts` | `npx playwright test` | per-test selection (`file:line`), login-setup project, browser install in CI |
| `cypress` | `*.cy.ts`, `*.cy.js` | `npx cypress run --spec` | file-level selection; add `junit` reporter support (built in to Cypress/mocha) |
| `jest` | `*.test.ts/js` | `npx jest` | needs `jest-junit` (`npm i -D jest-junit`) |
| `vitest` | `*.test.ts/js` | `npx vitest run` | JUnit reporter is built in |
| `pytest` | `test_*.py`, `*_test.py` | `python -m pytest` | per-test selection (`file::test`); any plugin stack (Selenium, Playwright-python, requests) |
| `generic` | you define it | you define it | start point for Robot Framework, Maven/Gradle (Selenium, REST-assured), .NET `dotnet test`, ... |

Override any preset key; only the keys you set change:

| Key | Meaning |
|---|---|
| `spec_globs`, `spec_suffix`, `support_exts` | Which files are tests; the suffix of generated files; extensions of helper files (a change in them re-runs every journey). |
| `generated_name` | File name of generated tests, with `{name}` and `{auth}` (`auth`/`anon`), e.g. `test_{name}_{auth}.py`. |
| `test_regex` | One regular expression per test declaration with a named group `title`. |
| `block_style` | `call` (`test("..", () => {})` brace-matching) or `indent` (python `def test_x():`). Used to keep passing tests and drop failing ones. |
| `title_key_regex` | Extracts the scenario id (e.g. `TC_CART_001`) from a test title. |
| `id_template`, `target_template`, `targets_join` | How a test is identified in the inventory (`{file}:{line}`, `{file}::{title}`) and what is passed to the runner (`{id}` or `{file}` for file-level runners; `","` joins them into one argument). |
| `list_cmd`, `list_ok_regex` | Command that proves a generated file loads (compile / collect). `[]` = skip; the execution step catches errors. |
| `exec_cmd`, `exec_results` | Runs one generated file during self-healing. Results: `junit` (any framework) or `playwright-json`. |
| `run_cmd` | Runs the affected tests; **must write a JUnit XML to `{junit}`**. |
| `env`, `exec_env`, `headed_env` | Extra environment for the runs (tokens `{junit}`, `{json}`); `headed_env` is applied when `QA_HEADED=1`. |
| `install_cmd` | One-off setup on CI (browser download); `[]` = none. |
| `assert_regex`, `negative_markers` | The "test only asserts absence" check; set `[]` to disable. |
| `agent2_rules`, `code_lang`, `language_name`, `label`, `automation_phrase` | Wording of the prompts (language, framework rules). |

Command templates are argument lists. Tokens: `{spec}` (one file), `{targets}` (the selected tests, expands to many arguments),
`{setup}` (login/setup project, dropped when not needed), `{junit}`, `{json}`. `"python"` means the interpreter running the agent.
An item that ends up empty is dropped.

**Custom framework example** (Maven + JUnit, file-level, results from surefire):

```json
"runner": {
  "preset": "generic",
  "label": "JUnit", "code_lang": "java", "language_name": "Java",
  "e2e_dir": "src/test/java", "spec_globs": ["*IT.java"], "spec_suffix": "IT.java",
  "generated_name": "{name}{auth}IT.java",
  "test_regex": "^\\s*public void (?P<title>test\\w+)\\(", "block_style": "indent",
  "id_template": "{file}::{title}", "target_template": "{file}",
  "list_cmd": [], "exec_cmd": ["mvn", "-q", "test", "-Dtest={spec}"], "exec_results": "junit",
  "run_cmd": ["mvn", "-q", "test"], "install_cmd": [], "assert_regex": ""
}
```

Honest limits: JUnit-XML is the contract (nearly every framework can produce it); with file-level runners
(Cypress, Jest, Java) a whole file is re-run, not one test; the "keep passing tests, drop failing" step needs a
`test_regex` that matches one declaration per line.

## Prompts

`qa-agent/prompts/agent1.md` (QA architect) and `agent2.md` (automation engineer) are plain text. On the first run
the agent writes them from its built-in defaults, filled in from your config; **from then on the files are the source of truth.**

- Tune behaviour by editing them (add rules like "always assert the page title", change the strictness of "covered").
  Lines in `<!-- ... -->` are not sent to the model.
- Keep the JSON shape in `agent1.md` intact - the agent parses it.
- `QA_REFRESH_PROMPTS=1` regenerates a file from the defaults (the old one is kept as `.md.bak`).
- Commit the files, so every run (and every teammate) uses the same prompts.

## Cost report

Every LLM response reports its token counts; the agent records them and `qa_cost.py` prices them with `pricing.json`.
Each run writes `qa-results/qa-cost-report.md`, `qa-cost-report.html` and `llm-usage.json` (uploaded with the run's artifacts) and
adds a **LLM cost** section to the PR comment:

- tokens per stage (Agent 1, Agent 2 write, Agent 2 self-heal)
- what the run **actually** cost (free tiers = $0.00; Anthropic is priced from `pricing.json`)
- what the same run would cost on Claude Haiku / Sonnet / Opus, per run and per month at `prs_per_month`

Notes: figures for models other than the one that answered are **estimates** (tokenizers differ). Update `pricing.json`
when prices change (source: the Claude API pricing page). For a quick demo: `python qa-agent/qa_cost.py --prs-per-month 200`.

## Safety guardrails

- LLM keys and `GITHUB_TOKEN` are **never** passed to Playwright or the app (a malicious PR could otherwise read them).
- New tests must **pass against the running app** before they are kept; a test that cannot be made to pass is dropped.
- If the targeted run fails because of an **existing** test, the new tests are held back (`qa-quarantine/`) and nothing is committed.
- New tests arrive as a **pull request for human review**, never straight on `main` (unless `QA_COMMIT_MODE=main`).
- The agent ignores PRs from `qa-agent/*` branches, so it cannot loop on itself.

## Limitations (be upfront about these)

- Test framework: **Playwright with TypeScript** only. The impact analysis understands .NET project references natively;
  for other stacks it treats each folder under `src_dir` as a component, so `spec_map` matters more.
- Only scenarios that can be driven through the UI are automated; others are listed as manual checks.
- LLM output varies run to run. Review generated tests like any other PR.
- The free Gemini tier is rate-limited; the agent retries with backoff, but a paid key is more reliable for a demo.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "No application changes detected" | The diff only touched files outside `src_dir`/`e2e_dir` (e.g. the agent itself). Merge a real code change or run manually with an earlier `base`. |
| "LLM fallback used" | No provider answered. Search the log for `HTTP`: 429 = rate limit, 401/403 = bad key, 400/413 = prompt too large. |
| Generated tests dropped ("failed validation") | Search the log for `Agent 2 error output` - it shows the real Playwright error. |
| `Executable doesn't exist ... chromium` | Browsers not installed on the runner; the agent installs them when `CI` is set. |
| PR step fails | Enable "Allow GitHub Actions to create and approve pull requests" in repo settings. |
| Workflow does not start after a merge | It only runs for PRs merged into `main` of the repo that holds the workflow file (check you merged inside your fork, not upstream). |

## 5-minute demo script

1. Show `qa-agent.config.json` and `prompts/agent1.md`: "project knowledge and prompts are data, not code."
2. Merge a PR that changes one UI component. Open the Actions run.
3. Open the PR comment: blast radius → scenarios → **covered / newly automated** → **only affected tests ran**.
4. Open the `qa-agent/tests-…` PR: the generated Playwright spec, reviewed like any other.
5. Show the cost section: tokens per stage, $0.00 on the free tier, and the same run priced on Claude Haiku / Sonnet / Opus.
6. Second change: previously generated tests now show as **covered**; only the new behaviour is automated.
