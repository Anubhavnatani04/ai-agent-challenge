import argparse, json, os, subprocess, sys
from pathlib import Path
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

MAX_ATTEMPTS = 3
PYTEST_ARGS = ["-q", "--maxfail=1"]

def repo_context(target: str):
    root = Path(__file__).parent.resolve()
    data_pdf = root / f"data/{target}/{target} sample.pdf"
    data_csv = root / f"data/{target}/result.csv"
    return {
        "root": str(root),
        "target": target,
        "data_pdf_exists": data_pdf.exists(),
        "data_csv_exists": data_csv.exists(),
        "data_pdf": str(data_pdf),
        "data_csv": str(data_csv),
        "present_files": file_tree_snapshot(root),
        "existing_contents": read_existing_files([
            root / f"custom_parsers/{target}_parser.py",
            root / f"tests/test_{target}.py",
            root / "README.md",
        ]),
    }

def file_tree_snapshot(root: Path):
    out = []
    for p in root.rglob("*"):
        if p.is_file():
            # Skip virtualenvs / large dirs
            if any(seg in p.parts for seg in [".venv", "venv", "__pycache__", ".git"]):
                continue
            rel = str(p.relative_to(root))
            out.append(rel)
    return sorted(out)

def read_existing_files(paths):
    result = {}
    for p in paths:
        p = Path(p)
        if p.exists():
            try:
                result[str(p)] = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                result[str(p)] = ""
    return result

def apply_patches(patches: list, root: Path):
    # patches is a list of objects
    for patch in patches:
        rel = patch.get("path")
        content = patch.get("content", "")
        if not rel:
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

def run_pytest_subprocess(root: Path):
    # Run tests in a clean subprocess
    cmd = [sys.executable, "-m", "pytest", *PYTEST_ARGS]
    proc = subprocess.Popen(
        cmd, cwd=str(root),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    out, _ = proc.communicate()
    return proc.returncode, out

def llm_propose_patches(system_prompt: str, user_prompt: str, model: str = "openai/gpt-oss-120b"):
    # Expect a JSON response
    client = Groq()
    
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.2,
        max_completion_tokens=8000,
        top_p=1,
        response_format={"type": "json_object"}
    )
    
    content = completion.choices[0].message.content
    try:
        data = json.loads(content)
    except Exception:
        # Fallback
        start = content.find("{")
        end = content.rfind("}")
        data = json.loads(content[start:end+1])
    return data

SYSTEM_PROMPT = """You are an autonomous coding agent that must:
- Create or update files in this repo to implement a PDF statement parser for the given bank target.
- Ensure a test under tests/ verifies parse(pdf_path) matches the CSV exactly (columns, order, values).
- Keep each iteration idempotent; overwrite files fully (no diffs).
- Output strictly JSON with keys: patches (list of {path, content}), notes (string).
- Do not include markdown code fences in content.
Contract:
- Generated parser path: custom_parsers/{target}_parser.py
- Parser signature: def parse(pdf_path: str) -> pandas.DataFrame
- Use only stdlib + pandas + pdfplumber (already installed).
- Tests should read: data/{target}/{target} sample.pdf and result.csv.
- Tests must assert DataFrame equality to CSV after dtype/whitespace normalization if needed.
"""

def build_user_prompt(ctx, last_report):
    target = ctx["target"]
    pref = target[:4]
    instructions = f"""
Goal: Make tests pass for target '{target}' by generating:
- custom_parsers/{target}_parser.py implementing parse(pdf_path) -> pandas.DataFrame
- tests/test_{target}.py that loads DataFrame from parse(data/{target}/{target} sample.pdf) and compares to pd.read_csv(data/{target}/result.csv) with strict equality (column names + order + values).

Context:
- data_pdf_exists: {ctx['data_pdf_exists']}, path: {ctx['data_pdf']}
- data_csv_exists: {ctx['data_csv_exists']}, path: {ctx['data_csv']}
- Present files: {ctx['present_files']}
- Existing contents keys: {list(ctx['existing_contents'].keys())}

Last pytest report (can be empty on first run):
{last_report or 'N/A'}

Produce JSON with:
{{
  "patches": [
    {{
      "path": "custom_parsers/{target}_parser.py",
      "content": "<full file>"
    }},
    {{
      "path": "tests/test_{target}.py",
      "content": "<full file>"
    }},
    {{
      "path": "README.md",
      "content": "<append or full file>"
    }}
  ],
  "notes": "Short rationale of changes"
}}

Implementation tips:
- Use pdfplumber to extract the main table; try page.extract_table or page.extract_tables and concatenate.
- Normalize strings (strip), parse dates, coerce numerics; reorder columns to match CSV header exactly.
- If headers are not recognized, set them explicitly from the CSV header.
"""
    return instructions

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="Bank key, e.g., icici")
    ap.add_argument("--model", default="llama-3.3-70b-versatile")
    args = ap.parse_args()

    root = Path(__file__).parent.resolve()
    (root / "tests").mkdir(exist_ok=True)
    (root / "custom_parsers").mkdir(exist_ok=True)

    last_report = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        ctx = repo_context(args.target)
        sys_prompt = SYSTEM_PROMPT.replace("{target}", args.target)
        user_prompt = build_user_prompt(ctx, last_report)

        print(f"\n=== Attempt {attempt}/{MAX_ATTEMPTS} ===")
        plan = llm_propose_patches(sys_prompt, user_prompt, model=args.model)

        patches = plan.get("patches", [])
        notes = plan.get("notes", "")
        if notes:
            print(f"Agent notes: {notes[:500]}")

        apply_patches(patches, root)
        code, report = run_pytest_subprocess(root)
        print(report)

        if code == 0:
            print("✅ Tests passed.")
            return 0
        else:
            last_report = report
            print(f"❌ Tests failed (exit {code}), iterating...")

    print("⚠️ Reached max attempts without success.")
    return 1

if __name__ == "__main__":
    sys.exit(main())
