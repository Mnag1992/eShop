"""
Post-Merge Autonomous QA Pipeline (project-agnostic)
====================================================

Triggered when a PR is merged into main (see .github/workflows/post-merge-qa.yml).
The automation suite is a Playwright (TypeScript) end-to-end suite.

Everything project-specific lives in qa-agent/qa-agent.config.json (paths, journey map,
test data, app description) and the LLM prompts in qa-agent/prompts/*.md (seeded from built-in
defaults on the first run, then always read from the files so they can be tuned).
Token usage and cost are written to qa-results/qa-cost-report.{md,html}. Setup for a new
project: qa-agent/README.md.

Stage 1  Ingestion         -> git diff of the merged PR, mapped onto eShop's services (src/*).
Stage 1b Static analysis   -> deterministic blast radius (no AI):
                                * project graph from <ProjectReference> (reverse transitive closure)
                                * types declared in changed files -> code that uses them
                                * services -> user journeys (Playwright specs) that exercise them
Stage 2  Agent 1 (QA Architect)    -> LLM returns QA scenarios for the change and maps each one
                                      to the existing automated test(s) that cover it (or "missing").
Stage 3  Agent 2 (Test Automation) -> automates the MISSING scenarios with the existing Playwright
                                      framework (helpers, locator style, auth projects) in
                                      e2e/generated/, validates them with `playwright test --list`
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
Requires on PATH: git, node/npx (npm ci done) and dotnet (Playwright starts the AppHost).

Useful environment switches:
  QA_DRY_RUN=true   analysis only - no LLM calls, no test execution (prints the impact as JSON)
  QA_HEADED=1       run Playwright with a visible browser (default is headless)
  DIFF_BASE/DIFF_HEAD  commits to compare (default: HEAD~1..HEAD; the workflow sets them for the PR)

Security: LLM keys and the GitHub token are NEVER passed to the processes that execute
repository or LLM-written code (Playwright / dotnet) - see clean_env().
"""

import fnmatch
import glob
import json
import os
import random
import re
import shutil
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
# Configuration (qa-agent/qa-agent.config.json; LLM settings via environment variables)
# --------------------------------------------------------------------------- #
def _load_config():
    """Generic defaults, overridden by qa-agent/qa-agent.config.json (path: QA_CONFIG)."""
    cfg = {
        "project_name": os.path.basename(os.getcwd()),
        "project_description": "a web application",
        "suite_description": "Playwright end-to-end tests that drive the application in a browser.",
        "src_dir": "src", "e2e_dir": "e2e", "generated_dir": "", "login_setup": "",
        "source_extensions": [".cs", ".razor"], "shared_dir": "", "excluded_projects": [],
        "global_files": ["playwright.config.ts", "package.json", "package-lock.json"],
        "generic_types": ["Program", "Extensions", "GlobalUsings", "Constants", "Startup", "App", "Routes"],
        "auth_specs": [], "all_specs": [], "spec_map": {}, "default_specs": "all", "helper_files": [],
        "ui_markup": {"dirs": [], "extensions": [".razor"], "budget_chars": 40000},
        "test_data": [], "prompts_dir": "qa-agent/prompts", "pricing_file": "qa-agent/pricing.json",
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
        return cfg
    for k, v in user.items():
        if k.startswith("_"):
            continue
        cfg[k] = {**cfg[k], **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
    return cfg


CFG = _load_config()
SRC_DIR = CFG["src_dir"]
E2E_DIR = CFG["e2e_dir"]
GENERATED_DIR = CFG["generated_dir"] or f"{E2E_DIR}/generated"   # <generated_dir>/<Owner>__<Name>.<auth|anon>.spec.ts
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


def _discover_specs():
    names = []
    for p in sorted(glob.glob(f"{E2E_DIR}/*.spec.ts")):
        names.append(os.path.basename(p)[:-len(".spec.ts")])
    return names


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
                    body = e.read().decode("utf-8", "ignore")[:300]
                    errors.append(f"{name}/{model} HTTP {e.code}: {body}")
                    if e.code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                        wait = (15 * attempt if e.code == 429 else min(30, 2 ** attempt)) + random.uniform(0, 1.5)
                        print(f"⚠️  {name}/{model} busy (HTTP {e.code}), retry {attempt}/{MAX_RETRIES - 1} in {wait:.1f}s")
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
    raise LLMUnavailable("No LLM provider available.\n   " + "\n   ".join(errors[-6:] or ["no API key/token configured"]))


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
    m = re.search(r"```(?:typescript|ts)?\s*(.*?)```", code, re.DOTALL)
    return (m.group(1) if m else code).strip()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


# Anything that executes repository code or LLM-written tests (Playwright, dotnet) must not be
# able to read the LLM keys or the GitHub token, or a malicious PR could exfiltrate them.
SECRET_ENV = re.compile(r"(TOKEN|API_KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
KEEP_ENV = {"USERNAME1", "PASSWORD"}  # seeded demo user the e2e login uses (public in the README)


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
    return f"{E2E_DIR}/{name}.spec.ts"


def is_auth_spec(path):
    name = os.path.basename(path)
    return name.endswith(".auth.spec.ts") or name.split(".")[0] in AUTH_SPECS


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
TEST_DECL = re.compile(r"^\s*test(?:\.(?:only|skip|fixme|slow))?\(\s*(['\"`])(.+?)\1")


def load_e2e_inventory():
    """Every automated test: {"id": "e2e/CartTest.spec.ts:4", "file", "line", "title", "auth"}"""
    tests = []
    for f in sorted(norm(p) for p in glob.glob(f"{E2E_DIR}/**/*.spec.ts", recursive=True)):
        for i, line in enumerate(read(f).splitlines(), start=1):
            m = TEST_DECL.match(line)
            if m:
                tests.append({"id": f"{f}:{i}", "file": f, "line": i, "title": m.group(2), "auth": is_auth_spec(f)})
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
        elif f.startswith(f"{E2E_DIR}/") and f.endswith(".ts"):
            if f.endswith(".spec.ts"):
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
    impacted_generated = [norm(f) for f in glob.glob(f"{GENERATED_DIR}/*.spec.ts")
                          if full_run or os.path.basename(f).split("__")[0] in impacted]
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
        "suite_description": CFG["suite_description"],
        "example_step": "Perform an action in the application", "example_test_id": f"{E2E_DIR}/Example.spec.ts:4",
        "example_page": "/", "example_paths": "'/', '/some-page'", "helpers_rules": "reuse the existing helpers from the framework section instead of re-implementing them.",
        "auth_rules": "Authentication is handled by the Playwright project (storage state) - never log in inside the test.",
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
   through the user interface with Playwright; otherwise automatable = false (manual check).

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
                 + "\n".join(f"### {f}\n```ts\n{read(f, 5000)}\n```" for f in spec_files))
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
# Stage 3: Agent 2 - Test Automation Engineer (Playwright, self-healing)
# --------------------------------------------------------------------------- #
DEFAULT_AGENT2_PROMPT = """
You are Agent 2, a senior test automation engineer extending {{project_name}}'s existing Playwright
(TypeScript) end-to-end suite. Automate ALL given QA scenarios in ONE spec file
(one test() per scenario) so it fits seamlessly into the existing framework.

Hard rules:
- import { test, expect } from '@playwright/test'; {{helpers_rules}}
- baseURL is configured: navigate with relative paths ({{example_paths}}).
- Follow the existing specs' style: role/label/text locators (getByRole, getByText,
  getByLabel), data-* attributes that already exist in the markup, await every expect,
  { timeout: 15_000 } for assertions that wait on a server round-trip.
- {{auth_rules}}
- Only use pages, texts and elements that exist in the provided markup and specs.
{{test_data_rules_agent2}}
{{app_rules}}
- Test titles: "<scenario id> <scenario title>".
- Output ONLY TypeScript code, no markdown, no explanations.
"""


def playwright_list(spec):
    result = run([tool("npx"), "playwright", "test", spec, "--list"], env=clean_env())  # loading a spec executes it
    out = result.stdout + result.stderr
    ok = result.returncode == 0 and re.search(r"Total: [1-9]\d* tests?", out) is not None
    return ok, out[-6000:]


def safe_name(name, fallback):
    name = re.sub(r"\W", "", name or "")
    return name if name and name[0].isalpha() else fallback


def unique_path(name, suffix):
    base, n = name, 2
    path = f"{GENERATED_DIR}/{name}{suffix}"
    while os.path.exists(path):  # never overwrite an existing test - it is the regression baseline
        name = f"{base}V{n}"
        path = f"{GENERATED_DIR}/{name}{suffix}"
        n += 1
    return path


def ui_markup(static):
    """UI markup Agent 2 can pick locators from: changed UI files first, then the configured pages."""
    ui = CFG["ui_markup"]
    exts = tuple(ui.get("extensions", [".razor"]))
    files = [f for fs in static["changed_src_files"].values() for f in fs if f.endswith(exts)]
    for d in ui.get("dirs", []):
        for ext in exts:
            files += sorted(norm(f) for f in glob.glob(f"{d}/**/*{ext}", recursive=True))
    blocks, budget = [], ui.get("budget_chars", 40000)
    for f in dict.fromkeys(files):
        if os.path.exists(f) and budget > 0:
            text = read(f, 6000)
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

    framework = "\n".join(f"### {f}\n```ts\n{read(f, 5000)}\n```"
                          for f in [*HELPER_FILES, *(spec_file(s) for s in ALL_SPECS)] if os.path.exists(f))
    markup = ui_markup(static)
    generated = []
    for (name, auth), group in groups.items():
        path = unique_path(f"{safe_name(owner, 'WebApp')}__{name}", f".{auth}.spec.ts")
        user_prompt = (
            f"Spec file: {path} ({'logged-in user' if auth == 'auth' else 'anonymous user'})\n\n"
            f"## QA scenarios to automate\n{json.dumps(group, indent=2)}\n\n"
            f"## Existing framework: helpers and specs\n{framework}\n\n"
            f"## Storefront markup\n{markup}\n\n{catalog_block()}"
        )
        print(f"🤖 Agent 2: automating {len(group)} scenario(s) -> {path}")
        try:
            written = write_with_self_healing(path, user_prompt)
        except LLMUnavailable as e:  # keep Agent 1's analysis; never leave an unvalidated spec behind
            print(f"⚠️  Agent 2 could not finish {path}: {e}")
            if os.path.exists(path):
                os.remove(path)
            written = False
        if written:
            generated.append(path)
            for s in group:
                s["automated_in"] = path
        else:
            for s in group:
                s["note"] = "automation attempt failed validation - needs manual automation"
    return generated


_BROWSERS_READY = False


def ensure_browsers():
    """CI runners start without Playwright browsers; install once, before the first test run."""
    global _BROWSERS_READY
    if _BROWSERS_READY or not os.getenv("CI"):
        return
    print("🌐 Installing Playwright Chromium...")
    subprocess.run([tool("npx"), "playwright", "install", "--with-deps", "chromium"], env=clean_env())
    _BROWSERS_READY = True


def execute_spec(path):
    """Really run a generated spec (with the login setup); returns (ok, error output)."""
    ensure_browsers()
    result = run([tool("npx"), "playwright", "test", path, *([LOGIN_SETUP] if LOGIN_SETUP else []), "--reporter=line", "--retries=0"],
                 env=clean_env({"ESHOP_USE_HTTP_ENDPOINTS": os.getenv("ESHOP_USE_HTTP_ENDPOINTS", "1")}))
    out = result.stdout + result.stderr
    return result.returncode == 0, out[-6000:]


def write_with_self_healing(path, user_prompt):
    code = strip_code_fences(call_llm(load_prompt("agent2"), user_prompt, purpose=f"Agent 2 {os.path.basename(path)}"))
    os.makedirs(GENERATED_DIR, exist_ok=True)
    for attempt in range(MAX_HEAL_ATTEMPTS + 1):
        with open(path, "w", encoding="utf-8") as f:
            f.write(code + "\n")
        ok, errors = playwright_list(path)
        if ok:  # loads fine - now prove it passes against the real app (catches invented data)
            ok, errors = execute_spec(path)
            errors = "The spec loaded but FAILED when executed against the running app:\n" + errors
        if ok:
            print(f"✅ Agent 2: generated + validated + passing -> {path}")
            return True
        if attempt == MAX_HEAL_ATTEMPTS:
            break
        print(f"🩹 Agent 2: {path} failed to load, self-healing (attempt {attempt + 1}/{MAX_HEAL_ATTEMPTS})...")
        print("----- Agent 2 error output (tail) -----\n" + errors[-2500:] + "\n---------------------------------------")
        fix_prompt = (f"{user_prompt}\n\n## Your previous code\n```ts\n{code}\n```\n\n"
                      f"## Playwright / TypeScript errors\n{errors}\n\nReturn the corrected full file.")
        code = strip_code_fences(call_llm(load_prompt("agent2"), fix_prompt, purpose=f"Agent 2 heal {os.path.basename(path)}"))
    print(f"❌ Agent 2: {path} still invalid - discarded.")
    os.remove(path)
    return False


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
        selected = [t for t in inventory if t["id"] in ids]
    else:
        selected = [t for t in inventory if t["file"] in static["candidate_specs"]]
    return selected, list(generated)


def run_e2e(selected, generated):
    targets = [t["id"] for t in selected] + generated
    if not targets:
        print("ℹ️  No affected automated tests to execute.")
        return False, []
    if LOGIN_SETUP and (any(t["auth"] for t in selected) or any(".auth." in g for g in generated)):
        targets.append(LOGIN_SETUP)  # logged-in project depends on the login setup
    print(f"🎭 Executing affected tests: {', '.join(targets)}")
    npx = tool("npx")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    junit = os.path.join(RESULTS_DIR, "e2e-junit.xml")
    if os.path.exists(junit):
        os.remove(junit)  # never parse results left over from an earlier run
    # Headless by default (playwright.config.ts). The html reporter must never open/serve a report.
    env = clean_env({"PLAYWRIGHT_JUNIT_OUTPUT_NAME": junit, "PLAYWRIGHT_HTML_OPEN": "never"})
    env.setdefault("ESHOP_USE_HTTP_ENDPOINTS", "1")
    if os.getenv("QA_HEADED"):
        env["PW_HEADED"] = "1"
    ensure_browsers()
    proc = subprocess.run([npx, "playwright", "test", *targets, "--reporter=line,junit,html"], env=env)

    rows = []
    if os.path.exists(junit):
        for tc in ET.parse(junit).getroot().iter("testcase"):
            status, msg = "PASS", ""
            for tag in ("failure", "error"):
                node = tc.find(tag)
                if node is not None:
                    status, msg = "FAIL", (node.get("message") or (node.text or ""))[:150]
            if tc.find("skipped") is not None:
                status = "SKIP"
            rows.append((tc.get("classname", ""), tc.get("name"), status, tc.get("time", ""), msg))
    if proc.returncode != 0 and not rows:
        rows = [("e2e", "(playwright run)", "FAIL", "", f"playwright exited with {proc.returncode}")]
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


def write_report(analysis, static, inventory, selected, generated, results, failed, llm_error, quarantined):
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
            lines += [f"<details><summary>{path}</summary>", "", "```ts", code.rstrip(), "```", "", "</details>", ""]
    if qa_cost:
        pricing = qa_cost.load_pricing(CFG["pricing_file"])
        cost_cfg = CFG["cost"]
        lines += [qa_cost.render_markdown(qa_cost.summarize(
            LLM_CALLS, pricing, cost_cfg.get("free_providers"), cost_cfg.get("reference_models"),
            cost_cfg.get("prs_per_month", 50)))]
    lines += ["## LLM usage", *(f"- {u}" for u in LLM_USAGE_LOG or ["none"])]
    if llm_error:
        lines += ["", f"> ⚠️ LLM fallback used: {llm_error.splitlines()[0]}"]
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

        print("🤖 Stage 3: Agent 2 automating missing scenarios with Playwright...")
        generated = agent_2_automate(analysis, static)
    except (LLMUnavailable, ValueError) as e:  # ValueError covers malformed JSON from the LLM
        llm_error = str(e)
        print(f"⚠️  {e}\n➡️  Continuing with rule-based impact analysis (existing tests of impacted journeys).")
        analysis = rule_based_scenarios(static)

    selected, new_specs = select_tests(analysis, static, inventory, generated, llm_error is None)
    print("🧪 Stage 4: running affected tests only...")
    failed, results = run_e2e(selected, new_specs)
    quarantined = {}
    if failed and generated:
        bad = [r for r in results if r[2] == "FAIL"]
        only_new = bool(bad) and all("generated" in (r[0] or "") for r in bad)
        quarantined = quarantine_generated_tests(generated)
        if only_new:
            print("ℹ️  Only the new tests failed; existing tests are green. New specs quarantined.")
            failed = False
    write_report(analysis, static, inventory, selected, generated, results, failed, llm_error, quarantined)
    if qa_cost:
        qa_cost.write_reports(LLM_CALLS, RESULTS_DIR, CFG["pricing_file"], CFG["cost"])
        print(f"💰 Cost report: {RESULTS_DIR}/qa-cost-report.md (+ .html)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
