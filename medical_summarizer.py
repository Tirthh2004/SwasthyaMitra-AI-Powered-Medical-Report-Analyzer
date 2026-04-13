#!/usr/bin/env python3
"""
MEDICAL REPORT SUMMARIZER (LLM-POWERED)

This module builds on top of `medical_extract.py`.
It does two things:
- Uses `extract_text(file_path)` to pull text from a PDF/image.
- Uses Groq's free API (Llama 3.3 70B) to generate rich, layman-friendly
  summaries with emoji formatting.

If the Groq API is unavailable (no key, network error), it falls back to
a basic heuristic summarizer.

Example (CLI):
    python medical_summarizer.py report.pdf
"""

from __future__ import annotations

import argparse
import os
import sys
import re
from pathlib import Path
from typing import Dict

from medical_extract import extract_text

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_GROQ_API_KEY = None
_MAX_REPORT_CHARS = 12000  # Truncate very long reports to fit context window


def _load_api_key() -> str | None:
    """Load the Groq API key from .env file."""
    global _GROQ_API_KEY
    if _GROQ_API_KEY:
        return _GROQ_API_KEY

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        _GROQ_API_KEY = key
        return key

    return None


# ---------------------------------------------------------------------------
# LLM-Powered Summarizer (Groq + Llama 3.3 70B)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are SwasthyaMitra, an expert medical report analyst who explains reports in simple layman language that anyone can understand — even someone with no medical background.

You MUST follow this EXACT output format for EVERY medical report:

---

🩺 **What This Report Tests**

It measures:
- [Test Name] → [What it checks in simple words]
- [Test Name] → [What it checks in simple words]
(List ALL tests found in the report)

---

📊 **What Your Report Shows (Simple Explanation)**

✅ **Normal Values**
- [Test Name] – [Value] (Normal range: [range]) — Normal
- ...

⚠️ **Slightly Elevated / Low**
- [Test Name] – [Value] (Normal: [range])
  👉 This means: [simple explanation of what this elevation/drop means]
- ...

❌ **Abnormal / Concerning Values** (if any)
- [Test Name] – [Value] (Normal: [range])
  👉 This means: [simple explanation]
- ...

(If all values are normal, just show the ✅ section and say "All values are within the normal range!")

---

🧾 **Overall Simple Summary**

[2-4 sentences in very simple language explaining the overall health picture from this report. Mention what is good, what needs attention, and possible reasons for any abnormal values like lifestyle factors, diet, stress, medications, etc.]

---

🥗 **General Suggestions**
- [Actionable health tip 1]
- [Actionable health tip 2]
- [Actionable health tip 3]
- [Actionable health tip 4]
- Follow up with your doctor if advised

---

🚨 **Important**
- This is NOT a medical diagnosis
- This is a simplified explanation for your understanding only
- If you have symptoms like [relevant symptoms based on report type], consult your doctor immediately
- Always discuss your reports with a qualified medical professional

---

🇮🇳 **सरल सारांश (Overall Summary in Hindi)**

[Translate the "Overall Simple Summary" section above into simple, easy-to-understand Hindi. Use Devanagari script. Keep it 2-4 sentences, just like the English version.]

---

🇮🇳 **सामान्य सुझाव (General Suggestions in Hindi)**
- [Translate each suggestion from the "General Suggestions" section above into simple Hindi]
- [Continue for all suggestions]

---

🇮🇳🔸 **સરળ સારાંશ (Overall Summary in Gujarati)**

[Translate the "Overall Simple Summary" section above into simple, easy-to-understand Gujarati. Use Gujarati script. Keep it 2-4 sentences, just like the English version.]

---

🇮🇳🔸 **સામાન્ય સૂચનો (General Suggestions in Gujarati)**
- [Translate each suggestion from the "General Suggestions" section above into simple Gujarati]
- [Continue for all suggestions]

---

🍎 **Customized Diet Plan**

[AI Dietitian: Generate a personalized diet plan based (Vegetarian/Vegan)purely on the Abnormal values + Slightly Elevated / Low + Normal Values. For instance/example, if HBA1c is high, automatically exclude high glycemic index foods from the suggestion list. ONLY suggest vegetarian/vegan food combinations. Include specific meals: Breakfast, Lunch, Dinner, Snacks. English Only!!]

---

RULES:
1. ALWAYS use the exact emoji headers shown above (🩺, 📊, ✅, ⚠️, ❌, 🧾, 🥗, 🚨, 🇮🇳, 🇮🇳🔸, 🍎)
2. Be warm, reassuring but honest
3. Use plain English — no medical jargon without explanation
4. If a value is borderline, explain what it COULD mean without causing panic
5. Give SPECIFIC suggestions relevant to the report type (not generic advice)
6. If the report text is unclear or partially readable, still do your best with available data
7. Include the actual numeric values and reference ranges when available
8. If it's NOT a medical/lab report, say so clearly
9. ALWAYS include the Hindi translation sections (🇮🇳) after the Important section — these are mandatory
10. ALWAYS include the Gujarati translation sections (🇮🇳🔸) after the Hindi sections — these are mandatory"""


def _summarize_with_llm(report_text: str) -> str | None:
    """
    Send extracted report text to Groq's Llama 3.3 70B and get a rich
    layman-friendly summary.

    Returns None if the API call fails for any reason.
    """
    api_key = _load_api_key()
    if not api_key:
        return None

    try:
        from groq import Groq

        client = Groq(api_key=api_key)

        # Truncate very long reports to avoid context window issues
        trimmed_text = report_text[:_MAX_REPORT_CHARS]
        if len(report_text) > _MAX_REPORT_CHARS:
            trimmed_text += "\n\n[... report truncated for processing ...]"

        user_message = (
            "Please analyze the following medical report and provide a complete "
            "layman-friendly summary in the format specified:\n\n"
            "--- START OF REPORT ---\n"
            f"{trimmed_text}\n"
            "--- END OF REPORT ---"
        )

        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            model="llama-3.3-70b-versatile",
            temperature=0.3,
            max_tokens=4096,
            top_p=0.9,
        )

        response = chat_completion.choices[0].message.content
        if response and len(response.strip()) > 100:
            return response.strip()
        return None

    except Exception as e:
        _safe_print(f"\n⚠️  LLM API call failed: {e}")
        _safe_print("    Falling back to offline heuristic summarizer...\n")
        return None


# ---------------------------------------------------------------------------
# Heuristic Fallback Summarizer (offline, no API needed)
# ---------------------------------------------------------------------------

def _extract_float_after(label: str, text: str) -> float | None:
    """Find a floating-point number immediately following a label."""
    pattern = re.escape(label) + r"\s+([0-9]+(?:\.[0-9]+)?)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _extract_float_after_any(labels: list[str], text: str) -> float | None:
    """Try multiple label variants and return the first matching value."""
    for label in labels:
        value = _extract_float_after(label, text)
        if value is not None:
            return value
    return None


def _summarize_generic_labs_fallback(report_text: str) -> str | None:
    """
    Generic interpreter for lab-style lines.
    Checks if values are inside, below, or above the printed reference range.
    """
    lines = [ln.strip() for ln in report_text.splitlines() if ln.strip()]
    explanations: list[str] = []

    for line in lines:
        if not re.search(r"[0-9]", line) or not re.search(r"[A-Za-z]", line):
            continue

        m = re.match(
            r"^([A-Za-z][A-Za-z0-9 %()/+\-]+?)\s+([0-9]+(?:\.[0-9]+)?)\s*(.*)$", line
        )
        if not m:
            continue

        name_raw, val_str, rest = m.groups()
        name = " ".join(name_raw.split())

        try:
            value = float(val_str)
        except ValueError:
            continue

        low = high = None
        range_match = re.search(
            r"([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)", rest
        )
        if range_match:
            try:
                low = float(range_match.group(1))
                high = float(range_match.group(2))
            except ValueError:
                low = high = None

        if low is None and high is None:
            lt = re.search(r"<\s*([0-9]+(?:\.[0-9]+)?)", rest)
            if lt:
                try:
                    high = float(lt.group(1))
                except ValueError:
                    high = None

        if low is None and high is None:
            continue

        if low is not None and high is not None:
            if value < low:
                status = "⚠️ BELOW normal range"
            elif value > high:
                status = "⚠️ ABOVE normal range"
            else:
                status = "✅ Normal"
            ref_text = f"range {low:g}–{high:g}"
        elif high is not None:
            if value < high:
                status = "✅ Normal"
            else:
                status = "⚠️ ABOVE normal limit"
            ref_text = f"limit <{high:g}"
        else:
            status = "ℹ️ See doctor"
            ref_text = f"limit >{low:g}"

        explanations.append(f"- {name.strip()}: {value:g} ({ref_text}) — {status}")

        if len(explanations) >= 15:
            break

    if not explanations:
        return None

    header = "📊 **What Your Report Shows (Simple Explanation)**\n\n"
    bullets = "\n".join(explanations)
    footer = (
        "\n\n🚨 **Important**\n"
        "- This is a basic offline analysis — for a detailed summary, ensure internet connectivity.\n"
        "- Always discuss your report with your doctor."
    )
    return header + bullets + footer


def _heuristic_fallback(report_text: str) -> str:
    """
    Offline fallback summarizer using basic regex parsing.
    Used when the LLM is unavailable.
    """
    report_text = (report_text or "").strip()
    if len(report_text) < 50:
        return "The report is very short, so there is nothing substantial to summarize."

    result = _summarize_generic_labs_fallback(report_text)
    if result:
        intro = (
            "🩺 **What This Report Tests**\n\n"
            "ℹ️ _Offline mode — showing basic value analysis._\n"
            "_For a detailed AI-powered summary, check your internet connection._\n\n"
        )
        return intro + result

    # Ultra-basic fallback: just show first few lines
    lines = [l.strip() for l in report_text.splitlines() if l.strip()][:20]
    return (
        "🩺 **Report Content Preview**\n\n"
        + "\n".join(lines)
        + "\n\n🚨 **Important**: Could not generate a detailed summary offline. "
    )


# ---------------------------------------------------------------------------
# Safe printing (Windows terminal compatibility)
# ---------------------------------------------------------------------------

def _safe_print(text: str) -> None:
    """Print text safely on Windows terminals."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        sys.stdout.buffer.write((text + "\n").encode(encoding, errors="replace"))
    except Exception:
        print(text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def summarize_medical_report_layman(report_text: str, use_llm: bool = True) -> str:
    """
    Turn a raw medical report into a plain-English explanation
    suitable for a non-medical reader.

    Uses Groq's LLM by default. Falls back to heuristic logic if LLM fails.
    """
    report_text = (report_text or "").strip()
    if len(report_text) < 50:
        return "The report is very short, so there is nothing substantial to summarize."

    # Try LLM first
    if use_llm:
        llm_result = _summarize_with_llm(report_text)
        if llm_result:
            return llm_result

    # Fallback to heuristic
    return _heuristic_fallback(report_text)


def medical_Extract(file_path: str) -> Dict[str, str]:
    """
    High-level helper that:
    - extracts raw text from the given file (PDF / PNG / JPG / JPEG)
    - generates a layman-friendly explanation of the report.
    """
    file_path = str(file_path)
    raw_text = extract_text(file_path)
    layman_summary = summarize_medical_report_layman(raw_text)

    return {
        "file_path": file_path,
        "raw_text": raw_text,
        "layman_summary": layman_summary,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    """
    Command-line interface:
        python medical_summarizer.py report.pdf
    """
    parser = argparse.ArgumentParser(
        description=(
            "Extract text from a medical report (PDF/image) and summarize it "
            "in simple, layman language using AI (Groq LLM)."
        )
    )
    parser.add_argument(
        "file_path",
        help="Path to the medical report (PDF, PNG, JPG, JPEG).",
    )
    parser.add_argument(
        "--show-raw",
        action="store_true",
        help="Also print the full extracted text after the summary.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM and use offline heuristic summarizer only.",
    )
    args = parser.parse_args()

    _safe_print(f"\n📄 Processing: {args.file_path}")
    _safe_print("─" * 60)

    raw_text = extract_text(args.file_path)

    if raw_text.startswith("Error:"):
        _safe_print(f"\n❌ {raw_text}")
        sys.exit(1)

    _safe_print("✅ Text extracted successfully!")

    if not args.no_llm:
        _safe_print("🤖 Generating AI-powered summary (LLM)...\n")
    else:
        _safe_print("📋 Generating offline summary...\n")

    summary = summarize_medical_report_layman(raw_text, use_llm=not args.no_llm)

    _safe_print("═" * 60)
    _safe_print(summary)
    _safe_print("═" * 60)

    if args.show_raw:
        _safe_print("\n=== Raw extracted text ===\n")
        _safe_print(raw_text)


if __name__ == "__main__":
    _cli()
