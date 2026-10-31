"""
Post-Merge Autonomous QA Pipeline (project-agnostic)
====================================================

Triggered when a PR is merged into main (see .github/workflows/post-merge-qa.yml).
The automation suite can be ANY test framework (Playwright, Cypress, Jest, Vitest, pytest or a custom
command): everything framework-specific is the "runner" section of the config (see RUNNER_PRESETS).

Everything project-specific lives in qa-agent/qa-agent.config.json (paths, journey map,
test data, app description) and the LLM prompts in qa-agent/prompts/*.md (seeded from built-in
defaults on the first run, then always read from the files so they can be tuned).
Token usage and cost are written to qa-results/qa-cost-report.{md,html}. Setup for a new
project: qa-agent/README.md.

Stage 1  Ingestion         -> git diff of the merged PR, mapped onto the application's services (src/*).
Stage 1b Static analysis   -> deterministic blast radius (no AI):
                                * project graph from <ProjectReference> (reverse transitive closure)
                                * types declared in changed files -> code that uses them
                                * services -> user journeys (test files) that exercise them
Stage 2  Agent 1 (QA Architect)    -> LLM returns QA scenarios for the change and maps each one
                                      to the existing automated test(s) that cover it (or "missing").
Stage 3  Agent 2 (Test Automation) -> automates the MISSING scenarios with the existing test
                                      framework (helpers, locator style, auth projects) in
                                      <tests>/generated/, validates them with the runner's list command
                                      and self-heals errors by feeding them back to the LLM.
Stage 4  Targeted execution -> runs ONLY the affected tests (file:line) + the newly automated ones,
                               writes qa-report.md (also to the GitHub job summary).

LLM providers (tried in order, each with retries + model fallback):
  1. GitHub Models  - uses GITHUB_TOKEN (free, automatic inside GitHub Actions)
  2. Google Gemini  - uses GEMINI_API_KEY
  3. Anthropic      - uses ANTHROPIC_API_KEY
If every provider is unavailable the pipeline does NOT crash: it falls back to the static
analysis and runs the specs of the impacted journeys.

No third-party Python packages are required (only the standard library).
Requires on PATH: git, node/npx (npm ci done) and whatever your test framework / app needs (dotnet, python ...).

Useful environment switches:
  QA_DRY_RUN=true   analysis only - no LLM calls, no test execution (prints the impact as JSON)
  QA_HEADED=1       run the browser tests headed (runner.headed_env) (default is headless)
  DIFF_BASE/DIFF_HEAD  commits to compare (default: HEAD~1..HEAD; the workflow sets them for the PR)

Security: LLM keys and the GitHub token are NEVER passed to the processes that execute
repository or LLM-written code (test framework / app build) - see clean_env().
"""

import fnmatch
import glob
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

# Windows consoles default to cp1252 and crash on emoji -> force UTF-8 output.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:  # cost report lives in qa-agent/qa_cost.py (optional - the agent works without it)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "qa-agent"))
    import qa_cost
except ImportError:
    qa_cost = None


# --------------------------------------------------------------------------- #
# Test-framework layer: everything that is specific to Playwright / Cypress / pytest ... lives in a
# "runner" preset (overridable key by key in qa-agent.config.json -> "runner"). The pipeline itself
# (diff -> impact -> Agent 1 -> Agent 2 -> run affected tests -> report) never mentions a framework.
#
# Command templates are argv lists. Tokens: {spec} one spec file, {targets} (expands to many args),
# {setup} (login/setup project, empty when not needed), {junit}/{json} result files. "python" = this
# interpreter. A template item that ends up empty is dropped. An empty list = "this step does not exist".
# --------------------------------------------------------------------------- #
_TS_RULES = """- import { test, expect } from '@playwright/test'; {{helpers_rules}}
- baseURL is configured: navigate with relative paths ({{example_paths}}).
- Follow the existing specs' style: role/label/text locators (getByRole, getByText,
  getByLabel), data-* attributes that already exist in the markup, await every expect,
  { timeout: 15_000 } for assertions that wait on a server round-trip.
- {{auth_rules}}
- Only use pages, texts and elements that exist in the provided markup and specs.
- Write top-level test() calls; do NOT wrap them in test.describe.
- Scope text lookups to the smallest container (a list row, a panel): the same text, such as a product name, can appear in several places on one page, and a page-wide getByText then fails in strict mode. Prefer data-* attribute locators with toHaveText / toContainText.
- A test that checks something is NOT shown must also assert something that proves the page rendered (for example an empty-state message); otherwise it can never fail."""

_PW = {
    "label": "Playwright", "code_lang": "ts", "language_name": "TypeScript",
    "automation_phrase": "through the user interface with Playwright",
    "suite_description": "Playwright end-to-end tests that drive the application in a browser.",
    "e2e_dir": "e2e", "global_files": ["playwright.config.ts", "package.json", "package-lock.json"],
    "spec_globs": ["*.spec.ts"], "spec_suffix": ".spec.ts", "support_exts": [".ts"],
    "generated_name": "{name}.{auth}.spec.ts",
    "test_regex": r"^\s*test(?:\.(?:only|skip|fixme|slow))?\(\s*(['\"`])(?P<title>.+?)\1",
    "block_style": "call", "title_key_regex": r"^\S+",
    "id_template": "{file}:{line}", "target_template": "{id}", "targets_join": "",
    "list_cmd": ["npx", "playwright", "test", "{spec}", "--list"], "list_ok_regex": r"Total: [1-9]\d* tests?",
    "exec_cmd": ["npx", "playwright", "test", "{spec}", "{setup}", "--reporter=line,json", "--retries=0"],
    "exec_results": "playwright-json",
    "run_cmd": ["npx", "playwright", "test", "{targets}", "{setup}", "--reporter=line,junit,html"],
    "env": {"PLAYWRIGHT_JUNIT_OUTPUT_NAME": "{junit}", "PLAYWRIGHT_HTML_OPEN": "never"},
    "exec_env": {"PLAYWRIGHT_JSON_OUTPUT_NAME": "{json}"},
    "headed_env": {"PW_HEADED": "1"},
    "install_cmd": ["npx", "playwright", "install", "--with-deps", "chromium"],
    "negative_markers": [".not."], "assert_regex": r"expect\(.*",
    "agent2_rules": _TS_RULES,
}
_CY = {**_PW,
    "label": "Cypress", "suite_description": "Cypress end-to-end tests that drive the application in a browser.",
    "automation_phrase": "through the user interface with Cypress",
    "e2e_dir": "cypress/e2e", "global_files": ["cypress.config.ts", "cypress.config.js", "package.json", "package-lock.json"],
    "spec_globs": ["*.cy.ts", "*.cy.js"], "spec_suffix": ".cy.ts", "generated_name": "{name}.{auth}.cy.ts",
    "test_regex": r"^\s*it(?:\.(?:only|skip))?\(\s*(['\"`])(?P<title>.+?)\1",
    "id_template": "{file}:{line}", "target_template": "{file}", "targets_join": ",",
    "list_cmd": [], "list_ok_regex": "",
    "exec_cmd": ["npx", "cypress", "run", "--spec", "{spec}", "--reporter", "junit",
                 "--reporter-options", "mochaFile={junit},toConsole=false"],
    "exec_results": "junit",
    "run_cmd": ["npx", "cypress", "run", "--spec", "{targets}", "--reporter", "junit",
                "--reporter-options", "mochaFile={junit},toConsole=false"],
    "env": {}, "exec_env": {}, "headed_env": {}, "install_cmd": [],
    "negative_markers": ["not."], "assert_regex": r"\.should\(.*",
    "agent2_rules": """- Use the Cypress API: describe is optional; write one it() per scenario. {{helpers_rules}}
- baseUrl is configured: cy.visit() with relative paths ({{example_paths}}).
- Follow the existing specs' style (cy.get with data-* attributes, cy.contains, .should assertions).
- {{auth_rules}}
- Only use pages, texts and elements that exist in the provided markup and specs.
- Scope text lookups to the smallest container; the same text can appear several times on a page.
- A test that checks something is NOT shown must also assert something that proves the page rendered.""",
}
_JEST = {**_PW,
    "label": "Jest", "code_lang": "ts", "suite_description": "Jest tests (API / unit / integration) of the application.",
    "automation_phrase": "with Jest", "e2e_dir": "tests", "global_files": ["jest.config.js", "jest.config.ts", "package.json", "package-lock.json"],
    "spec_globs": ["*.test.ts", "*.test.js", "*.spec.ts"], "spec_suffix": ".test.ts", "generated_name": "{name}.{auth}.test.ts",
    "test_regex": r"^\s*(?:it|test)(?:\.(?:only|skip))?\(\s*(['\"`])(?P<title>.+?)\1",
    "target_template": "{file}", "targets_join": "",
    "list_cmd": [], "list_ok_regex": "",
    "exec_cmd": ["npx", "jest", "{spec}", "--ci"], "exec_results": "junit",
    "run_cmd": ["npx", "jest", "{targets}", "--ci"], "env": {"JEST_JUNIT_OUTPUT_FILE": "{junit}"},
    "exec_env": {"JEST_JUNIT_OUTPUT_FILE": "{junit}"}, "headed_env": {}, "install_cmd": [],
    "negative_markers": [".not."], "assert_regex": r"expect\(.*",
    "agent2_rules": """- Use Jest (`describe` is optional; one `it()` / `test()` per scenario). {{helpers_rules}}
- Follow the existing tests' style and imports.
- {{auth_rules}}
- Only call functions, endpoints and data that exist in the provided source and tests.
- A test that checks something is absent must also assert something that proves the code ran.""",
}
_JEST["run_cmd"] = ["npx", "jest", "{targets}", "--ci", "--reporters=default", "--reporters=jest-junit"]
_JEST["exec_cmd"] = ["npx", "jest", "{spec}", "--ci", "--reporters=default", "--reporters=jest-junit"]
_VITEST = {**_JEST, "label": "Vitest", "suite_description": "Vitest tests of the application.", "automation_phrase": "with Vitest",
    "global_files": ["vitest.config.ts", "package.json", "package-lock.json"],
    "exec_cmd": ["npx", "vitest", "run", "{spec}", "--reporter=default", "--reporter=junit", "--outputFile.junit={junit}"],
    "run_cmd": ["npx", "vitest", "run", "{targets}", "--reporter=default", "--reporter=junit", "--outputFile.junit={junit}"],
    "env": {}, "exec_env": {}}
_PYTEST = {**_PW,
    "label": "pytest", "code_lang": "python", "language_name": "Python",
    "suite_description": "pytest tests (UI with Selenium/Playwright-python, or API) of the application.",
    "automation_phrase": "with pytest", "e2e_dir": "tests",
    "global_files": ["pytest.ini", "pyproject.toml", "requirements.txt", "conftest.py", "setup.cfg", "tox.ini"],
    "spec_globs": ["test_*.py", "*_test.py"], "spec_suffix": ".py", "support_exts": [".py"],
    "generated_name": "test_{name}_{auth}.py",
    "test_regex": r"^(?:async\s+)?def\s+(?P<title>test\w*)\s*\(", "block_style": "indent",
    "title_key_regex": r"^test_(\w+?_\d+)", "id_template": "{file}::{title}", "target_template": "{id}", "targets_join": "",
    "list_cmd": ["python", "-m", "pytest", "{spec}", "--collect-only", "-q"], "list_ok_regex": r"[1-9]\d* tests? collected|::test",
    "exec_cmd": ["python", "-m", "pytest", "{spec}", "-q", "-p", "no:cacheprovider", "--junitxml={junit}"], "exec_results": "junit",
    "run_cmd": ["python", "-m", "pytest", "{targets}", "-q", "-p", "no:cacheprovider", "--junitxml={junit}"],
    "env": {}, "exec_env": {}, "headed_env": {}, "install_cmd": [],
    "negative_markers": [" not ", " is None", "== 0", "== []"], "assert_regex": r"\s*assert .*",
    "agent2_rules": """- Write plain pytest functions (def test_<SCENARIO_ID>_<short_name>(...)), one per scenario, no classes. {{helpers_rules}}
- Reuse the fixtures and helpers of the existing tests / conftest.py instead of re-implementing them.
- {{auth_rules}}
- Only use endpoints, pages, selectors and data that exist in the provided source and tests.
- Test function names start with test_ followed by the scenario id, e.g. test_TC_CART_001_adds_item.
- A test that checks something is absent must also assert something that proves the code ran.""",
}
RUNNER_PRESETS = {"playwright": _PW, "cypress": _CY, "jest": _JEST, "vitest": _VITEST, "pytest": _PYTEST}
RUNNER_PRESETS["generic"] = {**_PW, "label": "custom framework"}  # starting point for a fully custom "runner"

# --------------------------------------------------------------------------- #
# Configuration (qa-agent/qa-agent.config.json; LLM settings via environment variables)
# --------------------------------------------------------------------------- #
def _load_config():
    """Generic defaults, overridden by qa-agent/qa-agent.config.json (path: QA_CONFIG)."""
    cfg = {
        "project_name": os.path.basename(os.getcwd()),
        "project_description": "a web application",
        "suite_description": "", "runner": {"preset": "playwright"}, "keep_env": [],
        "src_dir": "src", "e2e_dir": "", "generated_dir": "", "login_setup": "",
        "source_extensions": [".cs", ".razor"], "shared_dir": "", "excluded_projects": [],
        "global_files": [],
        "generic_types": ["Program", "Extensions", "GlobalUsings", "Constants", "Startup", "App", "Routes"],
        "auth_specs": [], "all_specs": [], "spec_map": {}, "default_specs": "all", "helper_files": [],
        "ui_markup": {"dirs": [], "extensions": [".razor"], "budget_chars": 40000},
        "app_port": 0, "test_data": [], "prompts_dir": "qa-agent/prompts", "pricing_file": "qa-agent/pricing.json",
        "cost": {"free_providers": ["github", "gemini"],
                 "reference_models": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"], "prs_per_month": 50},
        "prompt_vars": {},
    }
    path = os.getenv("QA_CONFIG", "qa-agent/qa-agent.config.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            user = json.load(f)
    except OSError:
        print(f"ℹ️  No config file at {path} - using generic defaults (see qa-agent/README.md).")
        user = {}
    for k, v in user.items():
        if k.startswith("_"):
            continue
        cfg[k] = {**cfg[k], **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
    # the test framework: preset defaults, then the "runner" section of the config on top
    r = cfg.get("runner") or {}
    preset = RUNNER_PRESETS.get(r.get("preset", "playwright"))
    if preset is None:
        raise SystemExit(f"❌ Unknown runner preset '{r.get('preset')}'. Available: {', '.join(RUNNER_PRESETS)}")
    runner = {**preset, **{k: v for k, v in r.items() if k != "preset"}}
    for k in ("env", "exec_env", "headed_env"):  # environment maps merge instead of replace
        runner[k] = {**preset.get(k, {}), **(r.get(k) or {})}
    cfg["runner"] = runner
    cfg["e2e_dir"] = cfg["e2e_dir"] or runner["e2e_dir"]
    cfg["suite_description"] = cfg["suite_description"] or runner["suite_description"]
    cfg["global_files"] = cfg["global_files"] or runner["global_files"]
    return cfg


CFG = _load_config()
SRC_DIR = CFG["src_dir"]
E2E_DIR = CFG["e2e_dir"]
RUNNER = CFG["runner"]
CODE_LANG = RUNNER["code_lang"]
SPEC_SUFFIX = RUNNER["spec_suffix"]
GENERATED_DIR = CFG["generated_dir"] or f"{E2E_DIR}/generated"   # <generated_dir>/<Owner>__<Name>.<auth|anon> + spec suffix
LOGIN_SETUP = CFG["login_setup"]            # "" = the suite has no login setup project
RESULTS_DIR = "qa-results"
QUARANTINE_DIR = "qa-quarantine"
REPORT_FILE = "qa-report.md"
SOURCE_EXTS = tuple(CFG["source_extensions"])
EXCLUDED_PROJECTS = set(CFG["excluded_projects"])
SHARED_DIR = CFG["shared_dir"] or f"{SRC_DIR}/Shared"    # shared code linked by <Compile Include="..\Shared\X.cs">
GLOBAL_FILES = set(CFG["global_files"])     # repo-wide build inputs: a change here can affect every journey
GENERIC_TYPES = set(CFG["generic_types"])   # type names too generic to follow through the type graph
AUTH_SPECS = set(CFG["auth_specs"])         # specs that run as the logged-in user (playwright.config.ts project)
HELPER_FILES = list(CFG["helper_files"])
APP_PORT = int(CFG["app_port"] or 0)        # port the test framework's web server listens on (0 = do not manage it)
TEST_RE = re.compile(RUNNER["test_regex"])  # one test declaration per line; group "title"
GEN_TEMPLATE = RUNNER["generated_name"]     # e.g. "{name}.{auth}.spec.ts" or "test_{name}_{auth}.py"
_gen_parts = GEN_TEMPLATE.split("{name}")
GEN_PREFIX = _gen_parts[0]                  # text before the name ("test_" for pytest)
_GEN_RE = re.compile("^" + re.escape(GEN_TEMPLATE).replace(re.escape("{name}"), r"(?P<name>.+?)")
                     .replace(re.escape("{auth}"), r"(?P<auth>auth|anon)") + "$")


def is_spec_file(path):
    """A test file of the suite (not a helper): matches runner.spec_globs."""
    return any(fnmatch.fnmatch(os.path.basename(path), g) for g in RUNNER["spec_globs"])


def list_spec_files(pattern_dir, recursive=False):
    found = glob.glob(f"{pattern_dir}/**/*", recursive=True) if recursive else glob.glob(f"{pattern_dir}/*")
    return sorted(p.replace("\\", "/") for p in found if os.path.isfile(p) and is_spec_file(p))


def _discover_specs():
    return [os.path.basename(p)[:-len(SPEC_SUFFIX)] for p in list_spec_files(E2E_DIR)
            if p.endswith(SPEC_SUFFIX)]


ALL_SPECS = list(CFG["all_specs"]) or _discover_specs()
E2E_SPEC_MAP = CFG["spec_map"]              # service/project -> user journeys (specs) that exercise it


def specs_for(project_name):
    """Journeys for a project; unlisted projects get default_specs ('all' = every journey, [] = none)."""
    if project_name in E2E_SPEC_MAP:
        return E2E_SPEC_MAP[project_name]
    default = CFG["default_specs"]
    return ALL_SPECS if default == "all" else list(default)


PROVIDER_ORDER = [p.strip() for p in os.getenv("LLM_PROVIDERS", "github,gemini,anthropic").split(",") if p.strip()]

GITHUB_MODELS = os.getenv("GITHUB_MODELS", "openai/gpt-4.1-mini,openai/gpt-4o-mini").split(",")
GEMINI_MODELS = os.getenv("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite").split(",")
ANTHROPIC_MODELS = os.getenv("ANTHROPIC_MODELS", "claude-sonnet-5-5,claude-haiku-5-5").split(",")

# Rough per-provider input budget (characters, ~4 chars/token). GitHub Models' free tier
# caps a request at ~8k input tokens, so prompts are trimmed before they are sent.
PROVIDER_MAX_CHARS = {
    "github": int(os.getenv("GITHUB_MODELS_MAX_CHARS", "26000")),
    "gemini": int(os.getenv("GEMINI_MAX_CHARS", "400000")),
    "anthropic": int(os.getenv("ANTHROPIC_MAX_CHARS", "400000")),
}

MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "4"))
HTTP_TIMEOUT = int(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
MAX_HEAL_ATTEMPTS = int(os.getenv("MAX_HEAL_ATTEMPTS", "2"))

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}
RATE_LIMIT_MAX_WAIT = int(os.getenv("LLM_RATE_LIMIT_MAX_WAIT", "90"))  # longest single wait on a 429 (seconds)


class LLMUnavailable(Exception):
    """Raised when no configured provider/model could answer."""


# --------------------------------------------------------------------------- #
# LLM layer: provider adapters (plain REST, no SDKs)
# --------------------------------------------------------------------------- #
def _post_json(url, headers, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _github_models(model, system_prompt, user_prompt, json_mode):
    token = os.getenv("GITHUB_MODELS_TOKEN") or os.getenv("GITHUB_TOKEN")
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    data = _post_json(
        "https://models.github.ai/inference/chat/completions",
        {"Authorization": f"Bearer {token}", "Accept": "application/json"},
        payload,
    )
    u = data.get("usage") or {}
    return data["choices"][0]["message"]["content"], {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0)}


def _gemini(model, system_prompt, user_prompt, json_mode):
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    gen_cfg = {"temperature": 0.1}
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"
    data = _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key},
        {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": gen_cfg,
        },
    )
    u = data.get("usageMetadata") or {}  # Gemini bills "thinking" tokens as output
    usage = {"input": u.get("promptTokenCount", 0),
             "output": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)}
    return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]), usage


def _anthropic(model, system_prompt, user_prompt, json_mode):
    key = os.getenv("ANTHROPIC_API_KEY")
    if json_mode:
        system_prompt += "\nRespond with ONLY the JSON object, no prose, no markdown fences."
    # Current Claude models reject non-default sampling params (temperature/top_p) -> omit them.
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
        {
            "model": model,
            "max_tokens": 16000,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
    )
    if data.get("stop_reason") == "refusal":
        raise ValueError("model declined the request (stop_reason=refusal)")
    u = data.get("usage") or {}
    return ("".join(b.get("text", "") for b in data["content"] if b.get("type") == "text"),
            {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0)})


PROVIDERS = {
    "github": {
        "fn": _github_models,
        "models": GITHUB_MODELS,
        "enabled": lambda: bool(os.getenv("GITHUB_MODELS_TOKEN") or os.getenv("GITHUB_TOKEN")),
    },
    "gemini": {
        "fn": _gemini,
        "models": GEMINI_MODELS,
        "enabled": lambda: bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
    },
    "anthropic": {
        "fn": _anthropic,
        "models": ANTHROPIC_MODELS,
        "enabled": lambda: bool(os.getenv("ANTHROPIC_API_KEY")),
    },
}

LLM_USAGE_LOG = []  # which provider/model actually answered (for the report)
LLM_CALLS = []      # per call: purpose, provider, model, input/output tokens (for the cost report)
DEAD_MODELS = set()  # circuit breaker: models that already failed in this run are skipped


def _retry_delay(headers, body):
    """Seconds the provider asks us to wait (Retry-After header, Gemini retryDelay, 'retry after N seconds'), or None."""
    try:
        v = headers.get("Retry-After") if headers else None
        if v and str(v).strip().isdigit():
            return float(v)
    except Exception:
        pass
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body) or re.search(r"retry after (\d+) seconds", body, re.I)
    return float(m.group(1)) if m else None


def _daily_quota_spent(body):
    """Per-DAY quota exhaustion will not clear within a run - do not wait for it."""
    return bool(re.search(r"PerDay|per day|daily", body, re.I))


def _fit(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[... context truncated to fit the model's input limit ...]"


def call_llm(system_prompt, user_prompt, json_mode=False, purpose=""):
    """Try every enabled provider -> every model -> retries with exponential backoff."""
    errors = []
    for name in PROVIDER_ORDER:
        provider = PROVIDERS.get(name)
        if not provider or not provider["enabled"]():
            continue
        prompt = _fit(user_prompt, PROVIDER_MAX_CHARS.get(name, 100000) - len(system_prompt))
        for model in [m.strip() for m in provider["models"] if m.strip()]:
            if (name, model) in DEAD_MODELS:
                continue
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    started = time.time()
                    text, usage = provider["fn"](model, system_prompt, prompt, json_mode)
                    text = text.strip()
                    if not text:
                        raise ValueError("empty response")
                    call = {"purpose": purpose, "provider": name, "model": model,
                            "input_tokens": int(usage.get("input") or 0), "output_tokens": int(usage.get("output") or 0),
                            "seconds": round(time.time() - started, 1)}
                    if not call["input_tokens"] and not call["output_tokens"]:  # provider sent no usage block
                        call["input_tokens"] = (len(system_prompt) + len(prompt)) // 4
                        call["output_tokens"] = len(text) // 4
                        call["estimated"] = True
                    LLM_CALLS.append(call)
                    LLM_USAGE_LOG.append(f"{purpose}: {name}/{model} ({call['input_tokens']:,} in / {call['output_tokens']:,} out)")
                    print(f"   ↳ answered by {name}/{model}")
                    return text
                except urllib.error.HTTPError as e:
                    full_body = e.read().decode("utf-8", "ignore")
                    body = full_body[:300]
                    daily = e.code == 429 and _daily_quota_spent(full_body)
                    reason = "daily quota used up" if daily else "rate limited" if e.code == 429 else f"HTTP {e.code}"
                    errors.append(f"{name}/{model}: {reason} (HTTP {e.code}) {body[:160]}")
                    if daily:
                        print(f"⚠️  {name}/{model}: free-tier DAILY quota is used up - skipping it (resets once a day)")
                        DEAD_MODELS.add((name, model))
                        break
                    if e.code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                        asked = _retry_delay(e.headers, full_body)
                        base = min(asked + 2, RATE_LIMIT_MAX_WAIT) if asked else (15 * attempt if e.code == 429 else min(30, 2 ** attempt))
                        wait = base + random.uniform(0, 1.5)
                        print(f"⚠️  {name}/{model} busy (HTTP {e.code}{', provider asked for %ds' % asked if asked else ''}), "
                              f"retry {attempt}/{MAX_RETRIES - 1} in {wait:.1f}s")
                        time.sleep(wait)
                        continue
                    print(f"⚠️  {name}/{model} failed (HTTP {e.code}) -> trying next model/provider")
                    DEAD_MODELS.add((name, model))
                    break  # 400/401/403/404/413 etc. -> no point retrying this model
                except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as e:
                    errors.append(f"{name}/{model}: {e}")
                    if attempt < MAX_RETRIES:
                        wait = min(30, 2 ** attempt)
                        print(f"⚠️  {name}/{model} error ({e}), retry in {wait}s")
                        time.sleep(wait)
                        continue
                    DEAD_MODELS.add((name, model))
                    break
    reasons = list(dict.fromkeys(errors))[-6:] or ["no API key/token configured (set GEMINI_API_KEY / GITHUB_TOKEN / ANTHROPIC_API_KEY)"]
    raise LLMUnavailable("No LLM provider available.\n   " + "\n   ".join(reasons))


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def strip_code_fences(code):
    code = code.strip()
    m = re.search(r"```[A-Za-z]*\s*(.*?)```", code, re.DOTALL)
    return (m.group(1) if m else code).strip()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


# Anything that executes repository code or LLM-written tests (Playwright, dotnet) must not be
# able to read the LLM keys or the GitHub token, or a malicious PR could exfiltrate them.
SECRET_ENV = re.compile(r"(TOKEN|API_KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
KEEP_ENV = set(CFG["keep_env"])  # secret-looking variables the tests legitimately need (e.g. a public demo login)


def clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if k in KEEP_ENV or not SECRET_ENV.search(k)}
    env.update(extra or {})
    return env


def tool(name):
    path = shutil.which(name) or shutil.which(f"{name}.cmd") or shutil.which(f"{name}.exe")
    if not path:
        raise SystemExit(f"❌ '{name}' not found on PATH.")
    return path


def read(path, limit=None):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    return text if limit is None or len(text) <= limit else text[:limit] + "\n// ... truncated ..."


def norm(path):
    return path.replace("\\", "/")


def spec_file(name):
    return f"{E2E_DIR}/{name}{SPEC_SUFFIX}"


def is_auth_spec(path):
    name = os.path.basename(path)
    m = _GEN_RE.match(name)
    if m:  # a generated spec says in its name whether it runs as the logged-in user
        return m.group("auth") == "auth"
    return name.split(".")[0] in AUTH_SPECS or name[:-len(SPEC_SUFFIX)] in AUTH_SPECS


def generated_owner(path):
    """Project a generated spec belongs to (first part of Owner__Name)."""
    base = os.path.basename(path)
    if GEN_PREFIX and base.startswith(GEN_PREFIX):
        base = base[len(GEN_PREFIX):]
    return base.split("__")[0]


TYPE_DECL = re.compile(r"\b(?:class|record|interface|struct|enum)\s+([A-Z]\w*)")


def declared_types(path):
    if path.endswith(".razor"):
        return {os.path.basename(path).split(".")[0]}  # a Razor component's type is its file name
    if path.endswith(".cs"):
        return set(TYPE_DECL.findall(read(path)))
    if path.endswith(SOURCE_EXTS):  # other languages: a file's "type" is its file name
        return {os.path.basename(path).split(".")[0]}
    return set()


# --------------------------------------------------------------------------- #
# Existing automation suite (Playwright)
# --------------------------------------------------------------------------- #
def test_title(line):
    m = TEST_RE.match(line)
    return m.group("title") if m else None


def load_e2e_inventory():
    """Every automated test: {"id": "e2e/CartTest.spec.ts:4", "file", "line", "title", "auth"}"""
    tests = []
    for f in list_spec_files(E2E_DIR, recursive=True):
        for i, line in enumerate(read(f).splitlines(), start=1):
            title = test_title(line)
            if title is not None:
                tests.append({"id": RUNNER["id_template"].format(file=f, line=i, title=title), "file": f, "line": i,
                              "title": title, "auth": is_auth_spec(f)})
    return tests


# --------------------------------------------------------------------------- #
# Stage 1: Trigger & code ingestion
# --------------------------------------------------------------------------- #
def get_post_merge_diff():
    """DIFF_BASE/DIFF_HEAD are set by the workflow (merge-base..PR head). Locally: HEAD~1..HEAD."""
    head = os.getenv("DIFF_HEAD") or "HEAD"
    base = os.getenv("DIFF_BASE")
    if not base:
        base = f"{head}~1" if run(["git", "rev-parse", "--verify", f"{head}~1"]).returncode == 0 else None
    if not base:
        print("ℹ️  Repository has a single commit - nothing to compare against.")
        return "", []
    files = [norm(f) for f in run(["git", "diff", "--name-only", base, head]).stdout.splitlines() if f.strip()]
    # The diff text for the LLM only includes code that matters for impact analysis.
    diff = run(["git", "diff", base, head, "--", SRC_DIR, E2E_DIR,
                ":(exclude)*.css", ":(exclude)*.png", ":(exclude)*.jpg", ":(exclude)*.webp",
                f":(exclude){SRC_DIR}/*/*.json"]).stdout
    return diff, files


# --------------------------------------------------------------------------- #
# Stage 1b: Static (deterministic) impact analysis
# --------------------------------------------------------------------------- #
def load_projects():
    projects = {}
    for csproj in glob.glob(f"{SRC_DIR}/*/*.csproj"):
        csproj = norm(csproj)
        name = os.path.splitext(os.path.basename(csproj))[0]
        if name in EXCLUDED_PROJECTS:
            continue
        text = read(csproj)
        refs = {os.path.splitext(os.path.basename(norm(r)))[0]
                for r in re.findall(r'<ProjectReference\s+Include="([^"]+)"', text)}
        shared = {norm(s) for s in re.findall(r'<Compile\s+Include="\.\.[\\/]Shared[\\/]([^"]+)"', text)}
        projects[name] = {"name": name, "dir": os.path.dirname(csproj), "refs": refs - EXCLUDED_PROJECTS,
                          "shared": shared}
    if not projects:  # not a .NET layout: every top-level folder under src_dir is a component
        for d in sorted(glob.glob(f"{SRC_DIR}/*/")):
            d = norm(d).rstrip("/")
            name = os.path.basename(d)
            if name not in EXCLUDED_PROJECTS:
                projects[name] = {"name": name, "dir": d, "refs": set(), "shared": set()}
    return projects


def owner_project(path, projects):
    for p in projects.values():
        if path.startswith(p["dir"] + "/"):
            return p["name"]
    return None


def owners_of(path, projects):
    """Projects a changed file belongs to. A src/Shared file belongs to every service that links it."""
    owner = owner_project(path, projects)
    if owner:
        return [owner]
    if path.startswith(SHARED_DIR + "/"):
        rel = path[len(SHARED_DIR) + 1:]
        return [p["name"] for p in projects.values() if any(fnmatch.fnmatch(rel, pat) for pat in p["shared"])]
    return []


def project_files(project):
    return [norm(f) for ext in SOURCE_EXTS for f in glob.glob(f"{project['dir']}/**/*{ext}", recursive=True)
            if not any(x in norm(f) for x in ("/obj/", "/bin/", "/node_modules/"))]


def static_impact(changed_files, projects):
    full_run = any(f in GLOBAL_FILES for f in changed_files)
    changed_src, changed_e2e = {}, set()
    for f in changed_files:
        owners = owners_of(f, projects)
        if owners:
            for owner in owners:
                changed_src.setdefault(owner, []).append(f)
        elif f.startswith(SHARED_DIR + "/"):
            full_run = True  # shared code that no known project links: be safe, consider every journey
        elif f.startswith(f"{E2E_DIR}/") and f.endswith(tuple(RUNNER["support_exts"])):
            if is_spec_file(f):
                changed_e2e.add(f)
            else:  # shared helpers / login setup -> every journey may be affected
                full_run = True

    # project level: reverse transitive closure over <ProjectReference>
    impacted = set(changed_src)
    grew = True
    while grew:
        grew = False
        for n, p in projects.items():
            if n not in impacted and p["refs"] & impacted:
                impacted.add(n)
                grew = True

    # type level: types declared in changed code -> code that uses them
    changed_types = set()
    for files in changed_src.values():
        for f in files:
            if os.path.exists(f):
                changed_types |= declared_types(f)
    changed_types -= GENERIC_TYPES
    impacted_types, dependent_files = set(changed_types), set()
    changed_paths = {f for fs in changed_src.values() for f in fs}
    scan = {f: read(f) for n in impacted for f in project_files(projects[n]) if f not in changed_paths}
    grew = True
    while grew and impacted_types:
        grew = False
        pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(impacted_types))) + r")\b")
        for f, src in scan.items():
            if f not in dependent_files and pattern.search(src):
                dependent_files.add(f)
                new = declared_types(f) - impacted_types - GENERIC_TYPES
                if new:
                    impacted_types |= new
                    grew = True

    # journey level: which existing specs exercise the impacted services
    specs = set(changed_e2e)
    for n in (projects if full_run else impacted):
        specs.update(spec_file(s) for s in specs_for(n))
    impacted_generated = [f for f in list_spec_files(GENERATED_DIR)
                          if full_run or generated_owner(f) in impacted]
    specs.update(impacted_generated)

    return {
        "full_run": full_run,
        "changed_projects": sorted(changed_src),
        "indirectly_impacted_projects": sorted(impacted - set(changed_src)),
        "changed_types": sorted(changed_types),
        "indirectly_impacted_types": sorted(impacted_types - changed_types),
        "dependent_files": sorted(dependent_files),
        "candidate_specs": sorted(s for s in specs if os.path.exists(s)),
        "changed_src_files": changed_src,
    }


# --------------------------------------------------------------------------- #
# Prompts: editable files, seeded from the built-in defaults on the first run
# --------------------------------------------------------------------------- #
def prompt_vars():
    v = {
        "project_name": CFG["project_name"], "project_description": CFG["project_description"],
        "suite_description": CFG["suite_description"], "automation_phrase": RUNNER["automation_phrase"],
        "runner_label": RUNNER["label"], "code_lang_name": RUNNER.get("language_name", "code"),
        "runner_rules": RUNNER["agent2_rules"],
        "example_step": "Perform an action in the application",
        "example_test_id": RUNNER["id_template"].format(file=f"{E2E_DIR}/Example{SPEC_SUFFIX}", line=4, title="test_example"),
        "example_page": "/", "example_paths": "'/', '/some-page'", "helpers_rules": "reuse the existing helpers from the framework section instead of re-implementing them.",
        "auth_rules": "Authentication is handled by the test framework's setup (stored session) - never log in inside the test.",
        "app_rules": "",
    }
    titles = [e.get("title", "Test data") for e in CFG["test_data"]]
    if titles:
        t = " / ".join(f'"{x}"' for x in titles)
        v["test_data_rules_agent1"] = (f"Test data: scenarios may only use data from the {t} section(s), exactly as listed "
                                       "(names, prices). Never invent data. If a scenario needs a value the data cannot "
                                       "produce, do not mark it automatable.")
        v["test_data_rules_agent2"] = (f"- Test data: use ONLY exact values from the {t} section(s). Never invent or \"assume\" any. "
                                       "Compute expected totals from the listed prices. If a scenario cannot be built from "
                                       "the listed data, skip that test() instead of guessing.")
    else:
        v["test_data_rules_agent1"] = v["test_data_rules_agent2"] = ""
    v.update(CFG["prompt_vars"])
    return v


def render_prompt(text):
    vars_ = prompt_vars()
    for _ in range(3):  # a variable's value may itself contain {{variables}} (e.g. runner_rules)
        text = re.sub(r"\{\{(\w+)\}\}", lambda m: str(vars_.get(m.group(1), "")), text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


_PROMPT_CACHE = {}


def load_prompt(name):
    """
    First run (or QA_REFRESH_PROMPTS=1): write the default prompt, filled in from the config, to
    <prompts_dir>/<name>.md (an existing file is kept as <name>.md.bak). Every later run reads the file,
    so the prompt can be tuned by editing it. <!-- html comments --> in the file are not sent to the LLM.
    """
    if name in _PROMPT_CACHE:
        return _PROMPT_CACHE[name]
    path = os.path.join(CFG["prompts_dir"], f"{name}.md")
    refresh = bool(os.getenv("QA_REFRESH_PROMPTS"))
    if refresh or not os.path.exists(path):
        os.makedirs(CFG["prompts_dir"], exist_ok=True)
        if os.path.exists(path):
            shutil.copyfile(path, path + ".bak")
        header = (f"<!-- {name} prompt for the post-merge QA agent. Generated from the built-in default on the first run;\n"
                  "     from now on THIS FILE is what the agent sends to the LLM - edit it freely. Comments like this one are\n"
                  "     removed before sending. Set QA_REFRESH_PROMPTS=1 to regenerate it from the default (old copy -> .bak). -->\n")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(header + render_prompt({"agent1": DEFAULT_AGENT1_PROMPT, "agent2": DEFAULT_AGENT2_PROMPT}[name]))
        print(f"📝 Prompt file {'refreshed' if refresh else 'created'}: {path}")
    text = read(path)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    _PROMPT_CACHE[name] = render_prompt(text)
    return _PROMPT_CACHE[name]


# --------------------------------------------------------------------------- #
# Stage 2: Agent 1 - QA Architect (scenarios + coverage mapping)
# --------------------------------------------------------------------------- #
def catalog_block():
    """The seeded test data (config 'test_data'): the ONLY values that exist in the test environment."""
    blocks = []
    for entry in CFG["test_data"]:
        try:
            with open(entry["file"], encoding="utf-8-sig") as f:
                items = json.load(f)
        except (OSError, ValueError, KeyError):
            continue
        if isinstance(items, dict):  # {"items": [...]} style files
            items = next((v for v in items.values() if isinstance(v, list)), [])
        fmt = entry.get("line", "- {name}")
        lines = []
        for item in items:
            try:
                lines.append(fmt.format_map(item))
            except (KeyError, ValueError, AttributeError, IndexError):
                lines.append(f"- {item}")
        note = entry.get("note", "the ONLY values that exist; names and values are exact")
        blocks.append(f"## {entry.get('title', 'Test data')} ({note})\n" + "\n".join(lines))
    return "\n\n".join(blocks)


DEFAULT_AGENT1_PROMPT = """
You are Agent 1, a senior QA Architect for {{project_name}} - {{project_description}}

The QA automation suite is {{suite_description}}

You receive the merged PR diff, a deterministic static impact analysis, the source of the
changed files and the full inventory of existing automated tests (with their source).

Tasks:
1. Blast radius: what behaviour changed, which services, pages and user journeys are affected
   (directly and through dependencies, integration events and API contracts).
2. Write QA test scenarios for the changed and impacted behaviour (positive, negative,
   boundary, regression), described as a QA engineer would: steps and expected result.
3. For each scenario search the existing automation suite. If an existing test already
   verifies it, status = "covered" and list the test ids (exactly as given in the inventory,
   e.g. "{{example_test_id}}"). Be strict - only if the test really asserts it.
4. If not covered: status = "missing", and automatable = true only if it can be verified
   {{automation_phrase}}; otherwise automatable = false (manual check).

Return ONLY this JSON (do not change its shape):
{
  "impact_summary": "2-4 sentences",
  "risk_level": "LOW|MEDIUM|HIGH",
  "blast_radius": {
    "changed_components": ["ServiceName: ClassName"],
    "affected_pages": ["{{example_page}}"],
    "affected_user_flows": ["add to cart", "checkout"]
  },
  "test_scenarios": [
    {
      "id": "TC_FEATURE_001",
      "title": "short title",
      "steps": ["Open the application", "{{example_step}}", "..."],
      "expected": "expected result",
      "type": "positive|negative|boundary|regression",
      "priority": "P1|P2|P3",
      "logged_in": true,
      "status": "covered|missing",
      "covered_by": ["{{example_test_id}}"],
      "automatable": true,
      "target_spec": "FeatureNameImpact"
    }
  ]
}
Rules: covered_by may only contain ids from the inventory ([] when missing).
target_spec (PascalCase, no extension) groups missing scenarios into one new spec file.
Write scenarios in user-visible terms: use the labels, buttons and numbers a user sees on screen. Never put code identifiers (constants, properties, class names such as MaxQuantityPerItem) in scenario titles or steps.
{{test_data_rules_agent1}}
"""


def build_agent1_prompt(diff_text, static, inventory):
    parts = [f"## Merged PR diff\n```diff\n{diff_text[:30000]}\n```"]
    parts.append(
        "## Static impact analysis (deterministic)\n"
        f"- Changed projects: {static['changed_projects']}\n"
        f"- Projects that depend on them: {static['indirectly_impacted_projects']}\n"
        f"- Changed types: {static['changed_types']}\n"
        f"- Types that use them: {static['indirectly_impacted_types'][:60]}\n"
        f"- Journeys (specs) that exercise impacted services: {static['candidate_specs']}"
    )
    cat = catalog_block()
    if cat:
        parts.append(cat)
    parts.append("## Automation inventory (id -> test title)\n"
                 + "\n".join(f"- {t['id']} -> {t['title']} ({'logged in' if t['auth'] else 'anonymous'})" for t in inventory))
    changed_code = dict.fromkeys(f for files in static["changed_src_files"].values() for f in files
                                 if f.endswith(SOURCE_EXTS + (".cshtml",)) and os.path.exists(f))
    src_block = [f"### {f}\n```\n{read(f, 12000)}\n```" for f in changed_code]  # a shared file appears once
    parts.append("## Current source of changed files\n" + "\n".join(src_block))
    spec_files = list(dict.fromkeys(static["candidate_specs"] + sorted({t["file"] for t in inventory})))
    parts.append("## Source of existing automated tests\n"
                 + "\n".join(f"### {f}\n```{CODE_LANG}\n{read(f, 5000)}\n```" for f in spec_files))
    return "\n\n".join(parts)


def agent_1_generate_scenarios(diff_text, static, inventory):
    prompt = build_agent1_prompt(diff_text, static, inventory)
    analysis = parse_json(call_llm(load_prompt("agent1"), prompt, json_mode=True, purpose="Agent 1"))
    known = {t["id"] for t in inventory}
    for s in analysis.get("test_scenarios", []):
        refs = s.get("covered_by") or []
        refs = [refs] if isinstance(refs, str) else refs
        valid = [norm(r) for r in refs if norm(r) in known]
        if s.get("status") == "covered" and not valid:
            s["status"], s["note"] = "missing", "AI cited a test that does not exist in the suite"
        s["covered_by"] = valid
    return analysis


def rule_based_scenarios(static):
    """Fallback when no LLM is reachable: use the deterministic dependency analysis only."""
    return {
        "impact_summary": "LLM unavailable - rule-based analysis from the project and type dependency graph; "
                          "running every test of the impacted journeys.",
        "risk_level": "UNKNOWN",
        "blast_radius": {"changed_components": static["changed_projects"], "affected_pages": [], "affected_user_flows": []},
        "test_scenarios": [],
    }


# --------------------------------------------------------------------------- #
# Stage 3: Agent 2 - Test Automation Engineer (self-healing)
# --------------------------------------------------------------------------- #
DEFAULT_AGENT2_PROMPT = """
You are Agent 2, a senior test automation engineer extending {{project_name}}'s existing {{runner_label}}
({{code_lang_name}}) suite. Automate ALL given QA scenarios in ONE test file
(one test per scenario) so it fits seamlessly into the existing framework.

Hard rules:
{{runner_rules}}
{{test_data_rules_agent2}}
{{app_rules}}
- Test titles / names start with the scenario id: "<scenario id> <scenario title>".
- Output ONLY {{code_lang_name}} code, no markdown, no explanations.
"""


def build_cmd(template, **tokens):
    """argv from a runner template. {targets}/{setup} may expand to several args or none; empty items are dropped."""
    argv = []
    for item in template:
        hit = [k for k in tokens if "{" + k + "}" in item]
        if item in ("{targets}", "{setup}") and isinstance(tokens.get(item[1:-1]), list):
            argv.extend(tokens[item[1:-1]])
            continue
        for k in hit:
            v = tokens[k]
            item = item.replace("{" + k + "}", ",".join(v) if isinstance(v, list) else str(v))
        if item.strip():
            argv.append(item)
    if argv and argv[0] == "python":
        argv[0] = sys.executable
    elif argv:
        argv[0] = tool(argv[0])
    return argv


def runner_env(base, tokens, headed=False):
    env = dict(RUNNER["env"] if base == "run" else {**RUNNER["env"], **RUNNER["exec_env"]})
    if headed:
        env.update(RUNNER["headed_env"])
    return {k: str(v).replace("{junit}", tokens.get("junit", "")).replace("{json}", tokens.get("json", ""))
            for k, v in env.items()}


def validate_spec(spec):
    """Does the generated file load / compile / collect at least one test? (no list command = skip, execution checks it)"""
    if not RUNNER["list_cmd"]:
        return True, ""
    result = run(build_cmd(RUNNER["list_cmd"], spec=spec), env=clean_env())  # loading a spec executes it
    out = result.stdout + result.stderr
    ok = result.returncode == 0 and (not RUNNER["list_ok_regex"] or re.search(RUNNER["list_ok_regex"], out) is not None)
    return ok, out[-6000:]


def safe_name(name, fallback):
    name = re.sub(r"\W", "", name or "")
    return name if name and name[0].isalpha() else fallback


def unique_path(name, auth):
    base, n = name, 2
    path = f"{GENERATED_DIR}/{GEN_TEMPLATE.format(name=name, auth=auth)}"
    while os.path.exists(path):  # never overwrite an existing test - it is the regression baseline
        name = f"{base}V{n}"
        path = f"{GENERATED_DIR}/{GEN_TEMPLATE.format(name=name, auth=auth)}"
        n += 1
    return path


def ui_markup(static, only_changed=False):
    """UI markup Agent 2 can pick locators from: changed UI files first, then the configured pages."""
    ui = CFG["ui_markup"]
    exts = tuple(ui.get("extensions", [".razor"]))
    changed = [f for fs in static["changed_src_files"].values() for f in fs if f.endswith(exts)]
    files = list(changed)
    for d in ([] if only_changed else ui.get("dirs", [])):
        for ext in exts:
            files += sorted(norm(f) for f in glob.glob(f"{d}/**/*{ext}", recursive=True))
    blocks, budget = [], ui.get("budget_chars", 40000) + len(changed) * ui.get("changed_file_chars", 30000)
    for f in dict.fromkeys(files):
        if os.path.exists(f) and budget > 0:
            # files changed by the PR are shown in full - the new element is often near the end of the page
            text = read(f, ui.get("changed_file_chars", 30000) if f in changed else ui.get("other_file_chars", 6000))
            budget -= len(text)
            blocks.append(f"### {f}\n```markup\n{text}\n```")
    return "\n".join(blocks)


def agent_2_automate(analysis, static):
    todo = [s for s in analysis.get("test_scenarios", []) if s.get("status") == "missing" and s.get("automatable")]
    for s in analysis.get("test_scenarios", []):
        if s.get("status") == "missing" and not s.get("automatable"):
            s.setdefault("note", "not automatable through the UI - manual check")
    if not todo:
        print("✅ Agent 2: every automatable scenario is already covered - nothing to write.")
        return []

    # owner prefix lets later runs pick generated specs by impacted project (see static_impact)
    owner = (static["changed_projects"] or ["WebApp"])[0]
    groups = {}
    for s in todo:
        key = (safe_name(s.get("target_spec"), "GeneratedImpact"), "auth" if s.get("logged_in", True) else "anon")
        groups.setdefault(key, []).append(s)

    framework = "\n".join(f"### {f}\n```{CODE_LANG}\n{read(f, 5000)}\n```"
                          for f in [*HELPER_FILES, *(spec_file(s) for s in ALL_SPECS)] if os.path.exists(f))
    markup = ui_markup(static)
    generated = []
    for (name, auth), group in groups.items():
        path = unique_path(f"{safe_name(owner, 'WebApp')}__{name}", auth)
        user_prompt = (
            f"Spec file: {path} ({'logged-in user' if auth == 'auth' else 'anonymous user'})\n\n"
            f"## QA scenarios to automate\n{json.dumps(group, indent=2)}\n\n"
            f"## Existing framework: helpers and specs\n{framework}\n\n"
            f"## Storefront markup\n{markup}\n\n{catalog_block()}"
        )
        # healing re-sends the prompt on every round: keep it small (helpers + the changed pages only)
        helpers = "\n".join(f"### {f}\n```{CODE_LANG}\n{read(f, 5000)}\n```" for f in HELPER_FILES if os.path.exists(f))
        heal_context = (
            f"Spec file: {path} ({'logged-in user' if auth == 'auth' else 'anonymous user'})\n\n"
            f"## QA scenarios to automate\n{json.dumps(group, indent=2)}\n\n"
            f"## Existing helpers\n{helpers}\n\n"
            f"## Markup of the changed pages\n{ui_markup(static, only_changed=True)}\n\n{catalog_block()}"
        )
        print(f"🤖 Agent 2: automating {len(group)} scenario(s) -> {path}")
        try:
            written, dropped = write_with_self_healing(path, user_prompt, heal_context)
        except LLMUnavailable as e:  # keep Agent 1's analysis; never leave an unvalidated spec behind
            print(f"⚠️  Agent 2 could not finish {path}: {e}")
            if os.path.exists(path):
                os.remove(path)
            written, dropped = False, []
        if written:
            generated.append(path)
            active = {title_key(t) for t in map(test_title, read(path).splitlines()) if t is not None}
            dropped_keys = {title_key(t) for t in dropped}
            for s in group:
                sid = str(s.get("id"))
                if sid in dropped_keys:
                    s["note"] = "test failed when executed (or could not fail) - needs manual automation or review"
                elif sid not in active:
                    s["note"] = "Agent 2 skipped this scenario (cannot be built from the available markup/test data) - manual check"
                else:
                    s["automated_in"] = path
        else:
            for s in group:
                s["note"] = "automation attempt failed validation - needs manual automation"
    return generated


_BROWSERS_READY = False


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def ensure_port_free(port=None, wait=90):
    """
    Playwright starts the app itself (webServer). If an earlier run left it behind, the next start fails with
    "http://localhost:<port> is already used" and NO test runs. Wait for a clean shutdown, then kill the listener.
    """
    port = port or APP_PORT
    if not port or not _port_open(port):
        return
    print(f"⏳ Port {port} is still in use by a previous run - waiting up to {wait}s for it to exit...")
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(3)
        if not _port_open(port):
            return
    print(f"🧹 Port {port} still busy - stopping whatever listens on it")
    for cmd in (["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], ["fuser", f"{port}/tcp"]):
        if shutil.which(cmd[0]):
            pids = re.findall(r"\d+", run(cmd).stdout)
            for pid in pids:
                try:
                    os.kill(int(pid), 9)
                except (OSError, ValueError):
                    pass
            break
    for _ in range(20):
        if not _port_open(port):
            return
        time.sleep(1)
    print(f"⚠️  Port {port} is still in use - the next Playwright start may fail")


def ensure_browsers():
    """CI runners start without browsers / drivers; run the framework's install command once (runner.install_cmd)."""
    global _BROWSERS_READY
    if _BROWSERS_READY or not os.getenv("CI") or not RUNNER["install_cmd"]:
        return
    print(f"🌐 Installing {RUNNER['label']} browsers/drivers...")
    subprocess.run(build_cmd(RUNNER["install_cmd"]), env=clean_env())
    _BROWSERS_READY = True


def parse_junit(path):
    """Rows (classname, name, PASS|FAIL|SKIP, seconds, message) of a JUnit XML file (empty if missing/unreadable)."""
    rows = []
    if not os.path.exists(path):
        return rows
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return rows
    for tc in root.iter("testcase"):
        status, msg = "PASS", ""
        for tag in ("failure", "error"):
            node = tc.find(tag)
            if node is not None:
                status, msg = "FAIL", (node.get("message") or (node.text or ""))[:150]
        if tc.find("skipped") is not None:
            status = "SKIP"
        rows.append((tc.get("classname", ""), tc.get("name"), status, tc.get("time", ""), msg))
    return rows


def execute_spec(path):
    """
    Really run a generated spec (with the login setup).
    Returns (ok, error output, titles of the tests that failed).
    """
    ensure_browsers()
    ensure_port_free()
    os.makedirs(RESULTS_DIR, exist_ok=True)
    report = os.path.abspath(os.path.join(RESULTS_DIR, "heal-run.json"))
    junit = os.path.abspath(os.path.join(RESULTS_DIR, "heal-run.xml"))
    for p in (report, junit):
        if os.path.exists(p):
            os.remove(p)
    tokens = {"spec": path, "setup": [LOGIN_SETUP] if LOGIN_SETUP else [], "json": report, "junit": junit}
    result = run(build_cmd(RUNNER["exec_cmd"], **tokens), env=clean_env(runner_env("exec", tokens)))
    out = result.stdout + result.stderr
    ensure_port_free()  # the app must be gone before the next start
    failed = []
    if RUNNER["exec_results"] == "junit":
        failed = [r[1] for r in parse_junit(junit) if r[2] == "FAIL"]
    else:
      try:
        def walk(suite):
            for sp in suite.get("specs", []):
                if not sp.get("ok", True):
                    failed.append(sp.get("title", ""))
            for child in suite.get("suites", []):
                walk(child)
        with open(report, encoding="utf-8") as f:
            for suite in json.load(f).get("suites", []):
                walk(suite)
      except (OSError, ValueError):
        pass
    return result.returncode == 0, out[-6000:], failed


def title_key(title):
    """Scenario id = first word of a test title (e.g. 'TC_CART_001'); robust against quote escaping."""
    m = re.search(RUNNER["title_key_regex"], title or "")
    return (m.group(1) if m and m.groups() else m.group(0)) if m else ""


def _call_end(lines, i):
    """Index of the line where the call that starts on lines[i] closes (skips strings and comments)."""
    depth, started, q, in_block = 0, False, None, False
    for n in range(i, len(lines)):
        line, k = lines[n], 0
        while k < len(line):
            c = line[k]
            if in_block:
                if line.startswith("*/", k):
                    in_block, k = False, k + 1
            elif q:
                if c == "\\":
                    k += 1
                elif c == q:
                    q = None
            elif line.startswith("//", k):
                break
            elif line.startswith("/*", k):
                in_block, k = True, k + 1
            elif c in "'\"`":
                q = c
            elif c in "([{":
                depth, started = depth + 1, True
            elif c in ")]}":
                depth -= 1
                if started and depth <= 0:
                    return n
            k += 1
    return len(lines) - 1


def split_tests(code):
    """
    Split a spec into (header, [(title, block)], footer). None if no test is found.
    block_style "call": test("..", () => {..}) calls (also wrapped in a describe: its closing line is the footer).
    block_style "indent": python-style `def test_x():` functions (decorators belong to the function below them).
    """
    lines = code.split("\n")
    starts = [i for i, l in enumerate(lines) if test_title(l) is not None]
    if not starts:
        return None
    if RUNNER["block_style"] == "indent":
        begins = []
        for i in starts:
            b = i
            while b > 0 and lines[b - 1].lstrip().startswith("@"):
                b -= 1
            begins.append(b)
        blocks = []
        for n, i in enumerate(starts):
            end = begins[n + 1] if n + 1 < len(starts) else len(lines)
            blocks.append((test_title(lines[i]), "\n".join(lines[begins[n]:end]).rstrip("\n") + "\n"))
        return "\n".join(lines[:begins[0]]), blocks, ""
    last_end = _call_end(lines, starts[-1])
    blocks = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else last_end + 1
        blocks.append((test_title(lines[i]), "\n".join(lines[i:end])))
    return "\n".join(lines[:starts[0]]), blocks, "\n".join(lines[last_end + 1:])


def salvage_passing_tests(code, failed_titles):
    """Drop the failing test() blocks and keep the passing ones. Returns (code, dropped titles) or None."""
    parts = split_tests(code)
    if not parts or not failed_titles:
        return None
    header, blocks, footer = parts
    failed_keys = {title_key(t) for t in failed_titles}
    block_keys = {title_key(t) for t, _ in blocks}
    if not failed_keys <= block_keys:  # a failure outside our tests (e.g. the login setup)
        return None
    kept = [b for t, b in blocks if title_key(t) not in failed_keys]
    if not kept:
        return None
    return header + "\n" + "\n".join(kept) + ("\n" + footer if footer.strip() else ""), \
        [t for t, _ in blocks if title_key(t) in failed_keys]


def find_vacuous_tests(code):
    """Titles of tests that only assert something is NOT shown - they pass even when the feature is broken."""
    parts = split_tests(code)
    if not parts:
        return []
    out = []
    for title, block in parts[1]:
        if not RUNNER["assert_regex"] or not RUNNER["negative_markers"]:
            return []  # this framework has no "only asserts absence" check
        expects = re.findall(RUNNER["assert_regex"], block)
        if expects and all(any(m in e or m in e.replace(" ", "") for m in RUNNER["negative_markers"]) for e in expects):
            out.append(title)
    return out


def write_with_self_healing(path, user_prompt, heal_context=None):
    """Returns (kept, dropped titles). kept=False means the whole spec was discarded."""
    code = strip_code_fences(call_llm(load_prompt("agent2"), user_prompt, purpose=f"Agent 2 {os.path.basename(path)}"))
    os.makedirs(GENERATED_DIR, exist_ok=True)
    failed_titles = []
    for attempt in range(MAX_HEAL_ATTEMPTS + 1):
        with open(path, "w", encoding="utf-8") as f:
            f.write(code + "\n")
        ok, errors = validate_spec(path)
        failed_titles = []
        if ok:  # loads fine - now prove it passes against the real app (catches invented data)
            ok, errors, failed_titles = execute_spec(path)
            errors = "The spec loaded but FAILED when executed against the running app:\n" + errors
        if ok:
            weak = find_vacuous_tests(code)
            if weak:  # passes, but could never fail: only asserts absence
                ok, failed_titles = False, weak
                errors = ("These tests only assert that something is NOT visible, so they would pass even if the feature "
                          "were broken: " + ", ".join(weak) + ". Add an assertion that proves the page rendered "
                          "(for example the empty-state message) or the exact expected value.")
        if ok:
            print(f"✅ Agent 2: generated + validated + passing -> {path}")
            return True, []
        if attempt == MAX_HEAL_ATTEMPTS:
            break
        print(f"🩹 Agent 2: {path} failed to load, self-healing (attempt {attempt + 1}/{MAX_HEAL_ATTEMPTS})...")
        print("----- Agent 2 error output (tail) -----\n" + errors[-2500:] + "\n---------------------------------------")
        keep_note = (f"\nThese tests FAILED: {failed_titles}. Tests that are not listed passed - keep them unchanged "
                     "and fix only the failing ones.\n" if failed_titles else "")
        fix_prompt = (f"{heal_context or user_prompt}\n\n## Your previous code\n```{CODE_LANG}\n{code}\n```\n\n"
                      f"## {RUNNER['label']} / {RUNNER.get('language_name', 'code')} errors\n{errors[-6000:]}\n{keep_note}\nReturn the corrected full file.")
        code = strip_code_fences(call_llm(load_prompt("agent2"), fix_prompt, purpose=f"Agent 2 heal {os.path.basename(path)}"))

    # still failing: keep the tests that pass, drop only the ones that do not
    salvaged = salvage_passing_tests(code, failed_titles)
    if salvaged:
        kept_code, dropped = salvaged
        with open(path, "w", encoding="utf-8") as f:
            f.write(kept_code + "\n")
        ok, _ = validate_spec(path)
        if ok:
            ok, _, _ = execute_spec(path)
        if ok:
            print(f"✅ Agent 2: kept the passing tests, dropped {len(dropped)} failing: {', '.join(dropped)}")
            return True, dropped
    print(f"❌ Agent 2: {path} still invalid - discarded.")
    if os.path.exists(path):
        os.remove(path)
    return False, []


# --------------------------------------------------------------------------- #
# Stage 4: Targeted execution & reporting
# --------------------------------------------------------------------------- #
def select_tests(analysis, static, inventory, generated, llm_ok):
    """
    Affected tests only:
      * LLM available -> the existing tests that cover the QA scenarios (file:line) + new specs
      * LLM down      -> every test of the statically impacted journeys
    """
    if llm_ok:
        ids = {r for s in analysis.get("test_scenarios", []) for r in s.get("covered_by", [])}
        # regression guard: also run existing tests that open a page this change affects (e.g. goto('/cart'))
        pages = set()
        for p in analysis.get("blast_radius", {}).get("affected_pages", []):
            pages.update(re.findall(r"/[A-Za-z][\w\-/]*", str(p)))
        for t in inventory:
            if t["id"] in ids or t["file"] not in static["candidate_specs"]:
                continue
            body = read(t["file"])
            if any(re.search(r"goto\(\s*['\"`]" + re.escape(pg) + r"['\"`?/]", body) for pg in pages):
                ids.add(t["id"])
        selected = [t for t in inventory if t["id"] in ids]
    else:
        selected = [t for t in inventory if t["file"] in static["candidate_specs"]]
    return selected, list(generated)


def run_e2e(selected, generated):
    ids = [RUNNER["target_template"].format(id=t["id"], file=t["file"], line=t["line"], title=t["title"]) for t in selected]
    targets = list(dict.fromkeys(ids + generated))  # file-level runners: one entry per file
    if not targets:
        print("ℹ️  No affected automated tests to execute.")
        return False, []
    need_setup = bool(LOGIN_SETUP) and (any(t["auth"] for t in selected) or any(is_auth_spec(g) for g in generated))
    print(f"🧪 Executing affected tests ({RUNNER['label']}): {', '.join(targets + ([LOGIN_SETUP] if need_setup else []))}")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    junit = os.path.abspath(os.path.join(RESULTS_DIR, "e2e-junit.xml"))
    if os.path.exists(junit):
        os.remove(junit)  # never parse results left over from an earlier run
    tokens = {"targets": [",".join(targets)] if RUNNER["targets_join"] == "," else targets,
              "setup": [LOGIN_SETUP] if need_setup else [], "junit": junit, "json": ""}
    env = clean_env(runner_env("run", tokens, headed=bool(os.getenv("QA_HEADED"))))
    argv = build_cmd(RUNNER["run_cmd"], **tokens)
    ensure_browsers()
    for attempt in (1, 2):
        ensure_port_free()
        proc = subprocess.run(argv, env=env)
        if proc.returncode == 0 or os.path.exists(junit) or attempt == 2:
            break  # passed, or tests really ran and failed
        print(f"⚠️  {RUNNER['label']} exited before producing any results (app start-up problem?) - retrying once in 30s...")
        time.sleep(30)

    rows = parse_junit(junit)
    if proc.returncode != 0 and not rows:
        rows = [("e2e", f"({RUNNER['label']} run)", "FAIL", "", f"{RUNNER['label']} exited with {proc.returncode}")]
    return proc.returncode != 0, rows


def quarantine_generated_tests(generated):
    """
    The run is red, so the merged code may contain a bug - and tests Agent 2 just wrote
    from that code could treat the bug as correct. Hold them back from the repository
    (the commit step only commits e2e/generated/) and attach them to the report for review.
    """
    held = {}
    for path in generated:
        if os.path.exists(path):
            held[path] = read(path)
            os.makedirs(QUARANTINE_DIR, exist_ok=True)
            shutil.move(path, os.path.join(QUARANTINE_DIR, os.path.basename(path)))
            print(f"🛑 Run is red - {path} held back from the repo for human review.")
    return held


GEN_MARK = os.path.basename(GENERATED_DIR.rstrip("/"))


def is_generated_row(row):
    """Is this result row from a newly generated spec? (JUnit classname is the file path or dotted module path)"""
    cls = (row[0] or "").replace("\\", "/").replace(".", "/")
    return GEN_MARK in cls.split("/")


def is_setup_row(row):
    return bool(LOGIN_SETUP) and os.path.basename(LOGIN_SETUP) == (row[0] or "")


DECISION_FILE = os.path.join(RESULTS_DIR, "decision.md")


def build_decision(results, generated, quarantined):
    """The 'Needs your decision' text: what failed, what it probably means, what the agent did NOT do."""
    bad = [r for r in results if r[2] == "FAIL"]
    if not bad:
        return ""
    new_fail = [r for r in bad if is_generated_row(r)]
    old_fail = [r for r in bad if not is_generated_row(r) and not is_setup_row(r)]
    setup_fail = [r for r in bad if is_setup_row(r)]
    first = lambda r: (r[4] or "").strip().splitlines()[0][:200] if (r[4] or "").strip() else f"see the {RUNNER['label']} report"
    out = ["## ⚠️ Needs your decision", ""]
    if setup_fail:
        out += ["- **Login setup failed**, so no browser test could run. This points to the environment "
                "(app did not start, seeded user missing, or the login page changed) rather than to a single test.", ""]
    if old_fail:
        out += ["**Existing test(s) failed after this change:**", ""]
        out += [f"- `{r[0]}` - {r[1]}: {first(r)}" for r in old_fail]
        out += ["",
                "This is one of two things, and the agent cannot tell which:",
                "1. **A regression** in the merged change - fix the application.",
                "2. **A test the change made out of date** (e.g. a locator that now matches more than one element) - "
                "update the test.",
                "",
                "The agent never edits existing tests and never decides this for you."]
    if new_fail and not old_fail:
        out += ["**Only the newly generated test(s) failed; the existing tests are green:**", ""]
        out += [f"- `{r[0]}` - {r[1]}: {first(r)}" for r in new_fail]
        out += ["", "The merged change is probably fine; the generated test is probably wrong or flaky."]
    if quarantined:
        out += ["", f"**{len(quarantined)} generated spec(s) were held back** (not added to the suite) and opened in a "
                "**draft PR** for review: " + ", ".join(f"`{os.path.basename(p)}`" for p in quarantined) + ". "
                "Do not merge it until the failure above is resolved."]
    elif generated:
        out += ["", "No generated spec was kept."]
    else:
        out += ["", "No new tests could be verified in this run, so there is no PR to review."]
    out.append("")
    return "\n".join(out)


def write_report(analysis, static, inventory, selected, generated, results, failed, llm_error, quarantined, decision=""):
    br = analysis.get("blast_radius", {})
    scen = analysis.get("test_scenarios", [])
    covered = sum(1 for s in scen if s.get("status") == "covered")
    automated = sum(1 for s in scen if s.get("automated_in"))
    manual = sum(1 for s in scen if s.get("status") == "missing" and not s.get("automated_in"))
    lines = [
        "# 🤖 Post-Merge Autonomous QA Report",
        "",
        f"**Overall:** {'❌ FAILED' if failed else '✅ PASSED'}  |  **Risk:** {analysis.get('risk_level', '-')}"
        + ("  |  ⚠️ repo-wide change - all journeys considered" if static["full_run"] else ""),
        "",
        *([decision] if decision else []),
        "## Impact summary",
        analysis.get("impact_summary", "-"),
        "",
        "## Blast radius",
        f"- Changed services/projects: {', '.join(static['changed_projects']) or '-'}",
        f"- Dependent projects (via ProjectReference): {', '.join(static['indirectly_impacted_projects']) or '-'}",
        f"- Changed types: {', '.join(static['changed_types']) or '-'}",
        f"- Dependent types: {', '.join(static['indirectly_impacted_types'][:40]) or '-'}",
        f"- Changed components (AI): {', '.join(br.get('changed_components', [])) or '-'}",
        f"- Affected pages (AI): {', '.join(br.get('affected_pages', [])) or '-'}",
        f"- Affected user flows (AI): {', '.join(br.get('affected_user_flows', [])) or '-'}",
        "",
    ]
    if scen:
        lines += [
            f"## QA scenarios ({len(scen)}: {covered} already automated, {automated} newly automated, {manual} manual/open)",
            "",
            "| ID | Scenario | Type | Priority | Status | Automated by / note |",
            "|---|---|---|---|---|---|",
        ]
        for s in scen:
            if s.get("covered_by"):
                where = ", ".join(s["covered_by"])
            else:
                where = s.get("automated_in") or s.get("note") or "-"
            status = "✅ covered" if s.get("status") == "covered" else "🆕 automated" if s.get("automated_in") else "⚠️ missing"
            lines.append(f"| {s.get('id')} | {s.get('title')} | {s.get('type', '')} | {s.get('priority', '')} | "
                         f"{status} | {str(where).replace('|', '/')} |")
        lines.append("")
        lines += ["<details><summary>Scenario steps</summary>", ""]
        for s in scen:
            lines += [f"**{s.get('id')} {s.get('title')}**", *(f"{i}. {st}" for i, st in enumerate(s.get("steps", []), 1)),
                      f"- Expected: {s.get('expected', '-')}", ""]
        lines += ["</details>", ""]

    sel_ids = {t["id"] for t in selected}
    lines += [
        "## 🎯 Targeted execution",
        f"**Ran {len(selected)} of {len(inventory)} existing automated tests + {len(generated)} new spec(s); "
        f"skipped {len(inventory) - len(selected)} unaffected tests.**",
        "",
        "| Test | Decision |",
        "|---|---|",
        *(f"| `{t['id']}` {t['title']} | {'▶️ RUN' if t['id'] in sel_ids else '⏭️ SKIPPED'} |" for t in inventory),
        *(f"| `{g}` | ▶️ RUN (🆕 new) |" for g in generated),
        "",
    ]
    if results:
        passed = sum(1 for r in results if r[2] == "PASS")
        lines += [f"## Execution results ({passed}/{len(results)} passed)", "",
                  "| Spec | Test | Result | Time (s) | Message |", "|---|---|---|---|---|"]
        for r in results:
            icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️"}[r[2]]
            lines.append(f"| {r[0]} | {r[1]} | {icon} {r[2]} | {r[3]} | {r[4].replace('|', '/').replace(chr(10), ' ')} |")
        lines.append("")
    if quarantined:
        lines += [
            "## 🛑 New automation held back",
            "The targeted run failed, so the merged code may contain a regression (or the new test is wrong). "
            "The new specs were **not committed**. Review them below and commit manually if they are correct.",
            "",
        ]
        for path, code in quarantined.items():
            lines += [f"<details><summary>{path}</summary>", "", f"```{CODE_LANG}", code.rstrip(), "```", "", "</details>", ""]
    if qa_cost:
        pricing = qa_cost.load_pricing(CFG["pricing_file"])
        cost_cfg = CFG["cost"]
        lines += [qa_cost.render_markdown(qa_cost.summarize(
            LLM_CALLS, pricing, cost_cfg.get("free_providers"), cost_cfg.get("reference_models"),
            cost_cfg.get("prs_per_month", 50)))]
    lines += ["## LLM usage", *(f"- {u}" for u in LLM_USAGE_LOG or ["none"])]
    if llm_error:
        lines += ["", "> ⚠️ LLM fallback used: " + llm_error.splitlines()[0],
                  *(f"> - {x.strip()}" for x in llm_error.splitlines()[1:])]
    report = "\n".join(lines) + "\n"

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report)
    print(f"📝 Report written to {REPORT_FILE}")


# --------------------------------------------------------------------------- #
def main():
    print("📥 Stage 1: extracting merged PR diff...")
    diff, changed_files = get_post_merge_diff()
    projects = load_projects()
    static = static_impact(changed_files, projects)
    inventory = load_e2e_inventory()
    if not static["changed_projects"] and not static["candidate_specs"]:
        print(f"No application changes detected (changed: {', '.join(changed_files) or '-'}). Nothing to do.")
        return 0
    print(f"   Changed projects: {', '.join(static['changed_projects']) or '-'}")
    print(f"   Dependent projects: {', '.join(static['indirectly_impacted_projects']) or '-'}")
    print(f"   Changed types: {', '.join(static['changed_types']) or '-'}")
    print(f"   Impacted journeys: {', '.join(static['candidate_specs']) or '-'}")
    print(f"   Automation inventory: {len(inventory)} tests")

    if os.getenv("QA_DRY_RUN") == "true":  # analysis only - no LLM, no test execution
        print(json.dumps({**static, "inventory": [t["id"] + " " + t["title"] for t in inventory]}, indent=2))
        return 0

    llm_error = None
    generated = []
    print("🤖 Stage 2: Agent 1 writing QA scenarios and checking the automation suite...")
    try:
        analysis = agent_1_generate_scenarios(diff, static, inventory)
        scen = analysis.get("test_scenarios", [])
        print(f"   {len(scen)} scenarios, {sum(1 for s in scen if s.get('status') == 'missing')} not automated yet")

        print(f"🤖 Stage 3: Agent 2 automating missing scenarios with {RUNNER['label']}...")
        generated = agent_2_automate(analysis, static)
    except (LLMUnavailable, ValueError) as e:  # ValueError covers malformed JSON from the LLM
        llm_error = str(e)
        print(f"⚠️  {e}\n➡️  Continuing with rule-based impact analysis (existing tests of impacted journeys).")
        analysis = rule_based_scenarios(static)

    selected, new_specs = select_tests(analysis, static, inventory, generated, llm_error is None)
    print("🧪 Stage 4: running affected tests only...")
    failed, results = run_e2e(selected, new_specs)
    quarantined = {}
    decision = build_decision(results, generated, {}) if failed else ""
    if failed and generated:
        bad = [r for r in results if r[2] == "FAIL"]
        only_new = bool(bad) and all(is_generated_row(r) for r in bad)
        quarantined = quarantine_generated_tests(generated)
        decision = build_decision(results, generated, quarantined)
        if only_new:
            print("ℹ️  Only the new tests failed; existing tests are green. New specs quarantined.")
            failed = False
    write_report(analysis, static, inventory, selected, generated, results, failed, llm_error, quarantined, decision)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(DECISION_FILE, "w", encoding="utf-8") as f:   # used as the draft-PR description by the workflow
        f.write(decision)
    if qa_cost:
        qa_cost.write_reports(LLM_CALLS, RESULTS_DIR, CFG["pricing_file"], CFG["cost"])
        print(f"💰 Cost report: {RESULTS_DIR}/qa-cost-report.md (+ .html)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
