import argparse, json, os, subprocess, sys
from pathlib import Path
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

MAX_ATTEMPTS = 3
PYTEST_ARGS = ["-q", "--maxfail=1"]

def repo_context(target: str):
    root = Path(__file__).parent.resolve()
    data_dir = root / "data" / target
    data_pdf = data_dir / f"{target} sample.pdf"
    data_csv = data_dir / "result.csv"
    # Ensure the data dir exists (for new targets)
    data_dir.mkdir(parents=True, exist_ok=True)
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
            root / f"tests/test_{target}.py"
        ]),
    }

def file_tree_snapshot(root: Path):
    out = []
    for p in root.rglob("*"):
        if p.is_file():
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
    for patch in patches:
        rel = patch.get("path")
        content = patch.get("content", "")
        if not rel:
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

def run_pytest_subprocess(root: Path):
    cmd = [sys.executable, "-m", "pytest", *PYTEST_ARGS]
    proc = subprocess.Popen(
        cmd, cwd=str(root),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    out, _ = proc.communicate()
    return proc.returncode, out

def llm_propose_patches(system_prompt: str, user_prompt: str):
    client = Groq(timeout=120)
    for attempt in range(3):
        try:
            completion = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.8,
                max_completion_tokens=8192,
                top_p=1,
                stream=False,
                stop=None
            )
            content = completion.choices[0].message.content
            if not content or content.strip() == "":
                print(f"Attempt {attempt+1}: Got empty content from API.")
                continue
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                start = content.find("{")
                end = content.rfind("}")
                if start != -1 and end != -1 and end > start:
                    try:
                        return json.loads(content[start:end+1])
                    except Exception as e:
                        print(f"JSON decode failed on substring: {e}")
                        print(f"Content substring: {content[start:end+1]}")
                print(f"Malformed JSON content received: {content}")
                continue
        except Exception as e:
            print(f"Attempt {attempt+1} failed with error: {e}")
    raise RuntimeError("All attempts to get valid JSON from Groq API failed")

SYSTEM_PROMPT = """You are an autonomous coding agent that must:
- Create or update files in this repo to implement a robust, versatile PDF statement parser for the given bank target.
- Ensure a test under tests/ verifies parse(pdf_path) matches the CSV exactly (columns, order, values), and logs differences if any.
- Each iteration should be idempotent; overwrite files fully (no diffs).
- Output strictly JSON with keys: patches (list of {path, content}), notes (string).
- Do not include markdown code fences in content.
Contract:
- Parser path: custom_parsers/{target}_parser.py
- Parser signature: def parse(pdf_path: str) -> pandas.DataFrame
- Use only stdlib + pandas + pdfplumber (already installed).
- Test must read 'data/{target}/{target} sample.pdf' and 'data/{target}/result.csv'.
- Tests must assert DataFrame equality, matching columns, order, types, and values (after cleaning).
- Prefer to infer CSV schema automatically (use pd.read_csv).
- Robust handling: Support variable tables, headers in different locations, multi-page PDFs, missing columns.
- Parsing logic should try multiple table extraction strategies (extract_table, extract_tables, bbox regions, etc).
- Always print helpful error if no table is detected.
"""

def build_user_prompt(ctx, last_report):
    target = ctx["target"]
    instructions = f"""
Goal: Pass all tests for target '{target}' by generating:
- custom_parsers/{target}_parser.py — must define def parse(pdf_path) -> pandas.DataFrame
- tests/test_{target}.py — must load PDF from 'data/{target}/{target} sample.pdf', expected CSV from 'data/{target}/result.csv', normalize, and assert complete equality.

Context:
- PDF exists: {ctx['data_pdf_exists']}  | {ctx['data_pdf']}
- CSV exists: {ctx['data_csv_exists']}  | {ctx['data_csv']}
- Present files: {ctx['present_files']}
- Existing contents keys: {list(ctx['existing_contents'].keys())}
- Last pytest report (may be empty on first run): {last_report or 'N/A'}

Extra implementation tips:
- Always infer expected columns from reading the CSV at 'data/{target}/result.csv'. Set these as column order for parser output.
- When parsing PDF: If extract_table fails, try extract_tables and concatenate rows. If there are extra header rows, skip them. Support tables spanning multiple pages.
- Coerce all string cells with .strip(), parse date columns with pd.to_datetime using the format in CSV, and coerce numerics.
- If parser dataframe or CSV dataframe can't be matched, print out first few mismatches in the test.
- Print a helpful error if no table found.
Produce strict JSON:
{{
  "patches": [
    {{
      "path": "custom_parsers/{target}_parser.py",
      "content": "<full file>"
    }},
    {{
      "path": "tests/test_{target}.py",
      "content": "<full file>"
    }}
  ],
  "notes": "Short rationale of changes"
}}
Final reminder: be robust to different table formats and headers, and try to make each fix self-explanatory based on test failures!
"""
    return instructions

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="Bank key (e.g., icici)")
    args = ap.parse_args()

    root = Path(__file__).parent.resolve()

    # Ensure required directories and target data folder
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "custom_parsers").mkdir(parents=True, exist_ok=True)
    (root / "data" / args.target).mkdir(parents=True, exist_ok=True)

    last_report = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        ctx = repo_context(args.target)
        sys_prompt = SYSTEM_PROMPT.replace("{target}", args.target)
        user_prompt = build_user_prompt(ctx, last_report)

        print(f"\n=== Attempt {attempt}/{MAX_ATTEMPTS} ===")
        plan = llm_propose_patches(sys_prompt, user_prompt)

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
