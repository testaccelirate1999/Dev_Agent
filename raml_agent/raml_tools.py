# raml_agent/raml_tools.py
# ─────────────────────────────────────────────────────────────────────────────
# Core RAML logic — private to raml_agent/.
# All LLM calls use the Anthropic SDK directly.
# Static validation has no LLM dependency at all.
# ─────────────────────────────────────────────────────────────────────────────

import json
import os
import re

import anthropic

from .prompts import (
    RAML_GENERATION_PROMPT,
    FIX_ERRORS_PROMPT,
    LESSON_EXTRACTION_PROMPT,
)

CLAUDE_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# ── Anthropic client (lazy singleton) ─────────────────────────────────────────

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _llm(system: str, user: str,
         model: str = CLAUDE_MODEL, max_tokens: int = 8096) -> str:
    resp = _get_client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


# ── JSON parsing ──────────────────────────────────────────────────────────────

def parse_json_safe(text: str) -> dict:
    """Parse JSON from LLM output — never raises."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned, flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            depth += (ch == "{") - (ch == "}")
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    break
    return {"message": text, "files": [], "changed_files": [], "deleted_files": []}


def _clean_raml(path: str, content: str) -> str:
    """Strip markdown fences; ensure .raml files start with #%RAML."""
    content = re.sub(r"^```[a-z]*\n?", "", content, flags=re.MULTILINE)
    content = re.sub(r"\n?```$", "", content, flags=re.MULTILINE).strip()
    if path.endswith(".raml") and not content.startswith("#%RAML"):
        content = "#%RAML 1.0\n" + content
    return content


# ── Generate ──────────────────────────────────────────────────────────────────

def generate_raml(request: str, context: str,
                  lessons_block: str, current_files: dict) -> dict:
    """LLM call: generate or update RAML project files."""
    system = "\n\n".join(p for p in [lessons_block, RAML_GENERATION_PROMPT] if p)

    parts = []
    if context:
        parts.append(f"<retrieved_context>\n{context}\n</retrieved_context>")
    if current_files:
        summary = "\n\n".join(
            f"=== {p} ===\n{c[:1000]}{'...(truncated)' if len(c) > 1000 else ''}"
            for p, c in current_files.items()
        )
        parts.append(f"Current project files:\n{summary}")
    parts.append(f"User request: {request}")

    raw    = _llm(system=system, user="\n\n---\n\n".join(parts))
    parsed = parse_json_safe(raw)

    for f in parsed.get("files", []):
        f["content"] = _clean_raml(f["path"], f["content"])

    return parsed


# ── Validate (static, no LLM) ─────────────────────────────────────────────────

def validate_raml(files: dict[str, str]) -> dict:
    """
    Static RAML 1.0 validation — no LLM.
    Returns {errors, error_count, warning_count, valid}.
    """
    errors = []
    raml   = {p: c for p, c in files.items() if p.endswith(".raml")}
    root   = next(
        (p for p, c in raml.items()
         if c.strip().split("\n")[0].strip() == "#%RAML 1.0"),
        None,
    )

    for path, content in raml.items():
        lines = content.split("\n")
        first = lines[0].strip() if lines else ""

        for i, line in enumerate(lines, 1):
            # V1: Invalid security scheme type
            if re.match(r"^\s*type:\s*(API Key|apiKey|api_key|ApiKey)\s*$",
                        line, re.I):
                errors.append(_err(path, i, "error",
                    "Invalid security scheme type. Use 'Pass Through', "
                    "'Basic Authentication', 'Digest Authentication', or 'OAuth 2.0'.",
                    "V1_SECURITY_TYPE"))

            # V2: Inline traits in root
            if path == root and re.match(r"^traits:\s*$", line):
                next_ln = lines[i].strip() if i < len(lines) else ""
                if next_ln and not next_ln.startswith("!include"):
                    errors.append(_err(path, i, "error",
                        "Traits defined inline in root. Move to traits/*.raml "
                        "and import via 'uses:'.", "V2_INLINE_TRAITS"))

            # V3: Trait imported with !include instead of uses:
            if re.match(r"^\s*\w+:\s*!include\s+traits/", line):
                errors.append(_err(path, i, "error",
                    "Trait files must be imported with 'uses:' not '!include'.",
                    "V3_TRAIT_INCLUDE"))

            # V4: !include pointing to missing file
            inc = re.search(r"!include\s+(\S+)", line)
            if inc:
                inc_path = inc.group(1)
                base_dir = "/".join(path.split("/")[:-1])
                segs = []
                for seg in (f"{base_dir}/{inc_path}".lstrip("/")).split("/"):
                    if seg == "..":
                        if segs: segs.pop()
                    elif seg and seg != ".":
                        segs.append(seg)
                resolved = "/".join(segs)
                if resolved not in files:
                    errors.append(_err(path, i, "error",
                        f"!include target '{inc_path}' not found "
                        f"(resolved: '{resolved}').", "V4_MISSING_INCLUDE"))

            # V5: Inline types in root
            if path == root and re.match(r"^types:\s*$", line):
                next_ln = lines[i].strip() if i < len(lines) else ""
                if next_ln and not next_ln.startswith("!include"):
                    errors.append(_err(path, i, "error",
                        "Type definitions must be in data-types/*.raml Library "
                        "files, not inline in root.", "V5_INLINE_TYPES"))

        # V6: Data-type file wrong header
        if "data-types/" in path and not first.startswith("#%RAML 1.0 Library"):
            errors.append(_err(path, 1, "error",
                f"Data-type file must start with '#%RAML 1.0 Library', "
                f"found: '{first}'.", "V6_DATATYPE_HEADER"))

        # V7: Trait file wrong header
        if "traits/" in path and not first.startswith("#%RAML 1.0 Trait"):
            errors.append(_err(path, 1, "error",
                f"Trait file must start with '#%RAML 1.0 Trait', "
                f"found: '{first}'.", "V7_TRAIT_HEADER"))

    # V8: Unreferenced files (warnings)
    EXCLUDED          = {"README.md", "exchange.json"}
    EXCLUDED_PREFIXES = ("examples/", "schemas/")
    if root:
        all_content = "\n".join(files.values())
        for path in files:
            if path in (root, *EXCLUDED):
                continue
            if any(path.startswith(p) for p in EXCLUDED_PREFIXES):
                continue
            name = path.split("/")[-1]
            if path not in all_content and name not in all_content:
                errors.append(_err(path, 1, "warning",
                    "File not referenced from any project file. "
                    "Add via !include or uses:.", "V8_UNREFERENCED"))

    # V9: exchange.json validation
    if "exchange.json" in files:
        try:
            ex = json.loads(files["exchange.json"])
            for field in ["organizationId", "assetId", "version"]:
                if not ex.get(field):
                    errors.append(_err("exchange.json", 1, "error",
                        f"Missing required field '{field}' in exchange.json.",
                        "V9_EXCHANGE_JSON"))
        except json.JSONDecodeError as e:
            errors.append(_err("exchange.json", 1, "error",
                f"exchange.json is not valid JSON: {e}", "V9_EXCHANGE_JSON"))
    else:
        errors.append(_err("exchange.json", 0, "warning",
            "exchange.json missing — will be auto-generated on push.",
            "V9_EXCHANGE_JSON"))

    ec = sum(1 for e in errors if e["severity"] == "error")
    wc = sum(1 for e in errors if e["severity"] == "warning")
    return {"errors": errors, "error_count": ec,
            "warning_count": wc, "valid": ec == 0}


def _err(file: str, line: int, severity: str,
         message: str, rule: str) -> dict:
    return {"file": file, "line": line,
            "severity": severity, "message": message, "rule": rule}


# ── Fix errors ────────────────────────────────────────────────────────────────

def fix_raml_errors(errors: list, current_files: dict,
                    lessons_block: str = "") -> dict:
    """LLM call: fix specific validation errors."""
    hard_errors = [e for e in errors if e["severity"] == "error"]
    if not hard_errors:
        return {"message": "No errors to fix.",
                "files": [], "changed_files": [], "deleted_files": []}

    error_lines = "\n".join(
        f"  [{e['severity'].upper()}] {e['file']}:{e['line']} "
        f"({e.get('rule', '')}) — {e['message']}"
        for e in hard_errors
    )
    files_context = "\n\n".join(
        f"=== {p} ===\n{c}" for p, c in current_files.items()
    )
    system = "\n\n".join(p for p in [lessons_block, FIX_ERRORS_PROMPT] if p)
    raw    = _llm(
        system = system,
        user   = (f"VALIDATION ERRORS TO FIX:\n{error_lines}\n\n"
                  f"CURRENT FILES:\n{files_context}"),
    )
    parsed = parse_json_safe(raw)
    for f in parsed.get("files", []):
        f["content"] = _clean_raml(f["path"], f["content"])
    return parsed


# ── Save lesson ───────────────────────────────────────────────────────────────

def extract_and_save_lesson(lesson_memory, last_agent_reply: str,
                             user_feedback: str, project_name: str) -> dict | None:
    """Detect if user feedback is a correction; save as a lesson if so."""
    if lesson_memory is None:
        return None
    try:
        raw = _llm(
            system     = LESSON_EXTRACTION_PROMPT,
            user       = (f"Agent's last response:\n{last_agent_reply[:500]}\n\n"
                          f"User follow-up:\n{user_feedback}"),
            max_tokens = 150,
        )
        result = parse_json_safe(raw)
        if not result.get("is_correction"):
            return None

        mistake    = result.get("mistake", "").strip()
        correction = result.get("correction", "").strip()
        if not mistake or not correction:
            return None

        lesson_id = lesson_memory.save(
            mistake      = mistake,
            correction   = correction,
            category     = result.get("category", "general"),
            project_name = project_name,
        )
        return {
            "id":         lesson_id,
            "mistake":    mistake,
            "correction": correction,
            "category":   result.get("category", "general"),
        }
    except Exception:
        return None
