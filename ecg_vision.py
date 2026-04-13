#!/usr/bin/env python3
"""
ECG waveform analyzer with a two-stage pipeline:
1) CV feature extraction from ECG image.
2) Interpretation layer (optional external LLM, deterministic fallback).

Key design decisions:
- Grid calibration uses a *physiological feedback loop*: multiple calibration
  candidates are tried and the one yielding a plausible heart-rate (40-200 bpm)
  is selected.  This avoids the absurd 400+ bpm readings from naive grid
  autocorrelation.
- The deterministic fallback embeds actual numeric values throughout so that
  two different ECGs always produce visibly different summaries.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
_GROQ_API_KEY: str | None = None

# Physiologically plausible heart-rate window (bpm)
_HR_MIN_PLAUSIBLE = 40.0
_HR_MAX_PLAUSIBLE = 200.0


@dataclass
class ECGFeatures:
    estimated_heart_rate_bpm: float | None
    rhythm_regular: bool | None
    rr_interval_mean_ms: float | None
    rr_interval_std_ms: float | None
    qrs_width_proxy_ms: float | None
    lead_count_estimate: int
    quality_score: float
    confidence: str
    technical_notes: List[str]
    # Extra context for richer summaries
    peak_count: int = 0
    usable_lead_rows: int = 0
    rr_cv_percent: float | None = None        # coefficient of variation %
    calibration_method: str = "unknown"


def _is_supported_image(path: str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


def _load_image(path: str) -> np.ndarray:
    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Could not read image for ECG analysis.")
    return image


def _safe_print(text: str) -> None:
    """Print text safely on Windows terminals."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        sys.stdout.buffer.write((text + "\n").encode(encoding, errors="replace"))
    except Exception:
        print(text)


# ---------------------------------------------------------------------------
# CV helpers
# ---------------------------------------------------------------------------

def _estimate_grid_spacing_px(gray: np.ndarray) -> float:
    """
    Estimate the small-square grid spacing (in pixels) from vertical line
    periodicity using autocorrelation.
    """
    edges = cv2.Canny(gray, 30, 120)
    vertical_profile = edges.sum(axis=0).astype(np.float64)
    vertical_profile -= vertical_profile.mean()

    if np.allclose(vertical_profile.std(), 0):
        horizontal_profile = edges.sum(axis=1).astype(np.float64)
        horizontal_profile -= horizontal_profile.mean()
        if np.allclose(horizontal_profile.std(), 0):
            return 10.0
        vertical_profile = horizontal_profile

    corr = np.correlate(vertical_profile, vertical_profile, mode="full")
    corr = corr[corr.size // 2:]
    if corr.size < 20:
        return 10.0

    if corr[0] > 0:
        corr = corr / corr[0]

    search_start = 4
    search_end = min(100, corr.size)
    search = corr[search_start:search_end]
    if search.size == 0:
        return 10.0

    peaks = []
    for i in range(1, len(search) - 1):
        if search[i] > search[i - 1] and search[i] > search[i + 1] and search[i] > 0.15:
            peaks.append((i + search_start, float(search[i])))

    if not peaks:
        peak_idx = int(np.argmax(search)) + search_start
        return float(max(5, min(30, peak_idx)))

    best_peak = peaks[0][0]
    return float(max(5, min(30, best_peak)))


def _split_lead_rows(binary: np.ndarray) -> List[Tuple[int, int]]:
    """Split the image into horizontal bands, each containing one lead row."""
    row_density = binary.mean(axis=1)
    thresh = np.percentile(row_density, 65)
    mask = row_density > thresh

    segments: List[Tuple[int, int]] = []
    start = None
    for i, val in enumerate(mask):
        if val and start is None:
            start = i
        elif not val and start is not None:
            if i - start > 20:
                segments.append((start, i))
            start = None
    if start is not None and (len(mask) - start > 20):
        segments.append((start, len(mask) - 1))

    if not segments:
        h = binary.shape[0]
        band = h // 4
        return [(0, band), (band, 2 * band), (2 * band, 3 * band), (3 * band, h - 1)]
    return segments[:12]


def _signal_from_row(binary_row_img: np.ndarray) -> np.ndarray:
    """Convert lead row to 1D waveform by taking median y of dark pixels per x."""
    h, w = binary_row_img.shape
    signal = np.full((w,), np.nan, dtype=np.float32)
    for x in range(w):
        ys = np.where(binary_row_img[:, x] > 0)[0]
        if ys.size > 0:
            signal[x] = float(np.median(ys))

    if np.isnan(signal).all():
        return np.zeros((w,), dtype=np.float32)

    valid = ~np.isnan(signal)
    x = np.arange(w)
    signal = np.interp(x, x[valid], signal[valid]).astype(np.float32)
    baseline = np.median(signal)
    return (baseline - signal).astype(np.float32)


def _detect_r_peaks(signal: np.ndarray, min_distance_px: int = 20) -> List[int]:
    """Detect R-peaks using adaptive thresholding and local maxima."""
    if signal.size < 50:
        return []

    kernel_size = max(5, min(15, signal.size // 50) | 1)
    smooth = cv2.GaussianBlur(signal.reshape(-1, 1), (1, kernel_size), 0).reshape(-1)

    signal_range = float(np.max(smooth) - np.min(smooth))
    if signal_range < 2.0:
        return []

    amp_thresh = max(
        np.percentile(smooth, 85) * 0.5,
        np.mean(smooth) + 0.3 * np.std(smooth),
        1.5,
    )

    peaks: List[int] = []
    for i in range(2, len(smooth) - 2):
        if smooth[i] < amp_thresh:
            continue
        if (
            smooth[i] >= smooth[i - 1]
            and smooth[i] >= smooth[i + 1]
            and smooth[i] >= smooth[i - 2]
            and smooth[i] >= smooth[i + 2]
        ):
            if not peaks or i - peaks[-1] >= min_distance_px:
                peaks.append(i)
            elif smooth[i] > smooth[peaks[-1]]:
                peaks[-1] = i
    return peaks


def _estimate_qrs_width_px(signal: np.ndarray, peaks: List[int]) -> float | None:
    if not peaks:
        return None
    widths = []
    max_amp = max(float(np.max(signal)), 1.0)
    thr = max_amp * 0.5
    for p in peaks[:20]:
        left = p
        right = p
        while left > 0 and signal[left] > thr:
            left -= 1
        while right < len(signal) - 1 and signal[right] > thr:
            right += 1
        widths.append(max(1, right - left))
    if not widths:
        return None
    return float(np.median(widths))


# ---------------------------------------------------------------------------
# Heart-rate calibration with physiological feedback
# ---------------------------------------------------------------------------

def _compute_hr_for_ms_per_px(
    rr_px_values: List[float], ms_per_px: float
) -> Tuple[float | None, float | None, float | None, bool | None]:
    """Given raw R-R intervals in pixels, compute HR stats using a calibration."""
    rr_ms = np.array(rr_px_values, dtype=np.float64) * ms_per_px
    # Remove outliers
    q1, q3 = np.percentile(rr_ms, 25), np.percentile(rr_ms, 75)
    iqr = q3 - q1
    if iqr > 0:
        mask = (rr_ms >= q1 - 2 * iqr) & (rr_ms <= q3 + 2 * iqr)
        rr_clean = rr_ms[mask] if mask.sum() >= 2 else rr_ms
    else:
        rr_clean = rr_ms

    rr_mean = float(np.mean(rr_clean))
    rr_std = float(np.std(rr_clean))
    if rr_mean <= 0:
        return None, None, None, None

    hr = 60000.0 / rr_mean
    cv = rr_std / rr_mean
    rhythm_regular = bool(cv < 0.12)
    return hr, rr_mean, rr_std, rhythm_regular


def _find_best_calibration(
    rr_px_values: List[float],
    autocorr_grid_px: float,
    image_width_px: int,
) -> Tuple[float, str]:
    """
    Try multiple ms/px calibrations and pick the one that yields
    a physiologically plausible heart-rate (40-200 bpm).

    Returns (ms_per_px, calibration_method_name).
    """
    candidates: List[Tuple[float, str]] = []

    # Candidate 1: Autocorrelation-based grid
    px_per_sec_auto = autocorr_grid_px * 25.0
    if px_per_sec_auto > 0:
        candidates.append((1000.0 / px_per_sec_auto, "grid-autocorrelation"))

    # Candidate 2: Assume standard 10-second strip across image width
    # (most common 12-lead ECG format: 2.5s per column × 4 columns = 10s)
    px_per_sec_10s = image_width_px / 10.0
    if px_per_sec_10s > 0:
        candidates.append((1000.0 / px_per_sec_10s, "10s-strip-assumption"))

    # Candidate 3: Assume 2.5-second rhythm strip
    px_per_sec_2_5 = image_width_px / 2.5
    if px_per_sec_2_5 > 0:
        candidates.append((1000.0 / px_per_sec_2_5, "2.5s-strip-assumption"))

    # Candidate 4: Common DPI-based calibrations (150, 200, 300 DPI)
    for dpi in [150, 200, 300]:
        # At 25 mm/s: 1 mm = dpi/25.4 px, so 1s = 25mm = 25*dpi/25.4 px
        px_per_sec = 25.0 * dpi / 25.4
        candidates.append((1000.0 / px_per_sec, f"dpi-{dpi}"))

    # Score each candidate: pick the one whose HR is closest to 75 bpm
    # (center of normal range) while being within plausible bounds
    best_ms_per_px = candidates[0][0]
    best_method = candidates[0][1]
    best_score = float("inf")

    for ms_per_px, method in candidates:
        hr, _, _, _ = _compute_hr_for_ms_per_px(rr_px_values, ms_per_px)
        if hr is None:
            continue
        if _HR_MIN_PLAUSIBLE <= hr <= _HR_MAX_PLAUSIBLE:
            # Prefer HR closer to 75 bpm (center of normal)
            score = abs(hr - 75.0)
            if score < best_score:
                best_score = score
                best_ms_per_px = ms_per_px
                best_method = method

    # If no candidate gave a plausible HR, use the one closest to plausible
    if best_score == float("inf"):
        for ms_per_px, method in candidates:
            hr, _, _, _ = _compute_hr_for_ms_per_px(rr_px_values, ms_per_px)
            if hr is None:
                continue
            dist = min(abs(hr - _HR_MIN_PLAUSIBLE), abs(hr - _HR_MAX_PLAUSIBLE))
            if dist < best_score:
                best_score = dist
                best_ms_per_px = ms_per_px
                best_method = method + " (best-effort)"

    return best_ms_per_px, best_method


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def _extract_features(image: np.ndarray) -> ECGFeatures:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    denoised = cv2.GaussianBlur(gray, (5, 5), 0)
    binary = cv2.adaptiveThreshold(
        denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 8
    )

    autocorr_grid_px = _estimate_grid_spacing_px(gray)
    lead_rows = _split_lead_rows(binary)

    # ── Pass 1: Collect raw R-R intervals in *pixels* (no calibration yet) ──
    rr_px_all: List[float] = []
    qrs_px_all: List[float] = []
    usable_rows = 0
    total_peaks = 0
    notes: List[str] = []

    for y0, y1 in lead_rows:
        row = binary[max(0, y0): min(binary.shape[0], y1), :]
        signal = _signal_from_row(row)
        peaks = _detect_r_peaks(
            signal, min_distance_px=max(8, int(autocorr_grid_px * 1.5))
        )
        if len(peaks) < 2:
            continue
        usable_rows += 1
        total_peaks += len(peaks)
        rr_px = np.diff(peaks).astype(np.float64)
        rr_px_all.extend(rr_px.tolist())

        qrs_px = _estimate_qrs_width_px(signal, peaks)
        if qrs_px is not None:
            qrs_px_all.append(qrs_px)

    # ── Pass 2: Calibrate using physiological feedback ──
    hr: float | None = None
    rr_mean: float | None = None
    rr_std: float | None = None
    rhythm_regular: bool | None = None
    qrs_ms: float | None = None
    calibration_method = "none"
    rr_cv_pct: float | None = None

    if rr_px_all:
        ms_per_px, calibration_method = _find_best_calibration(
            rr_px_all, autocorr_grid_px, image.shape[1]
        )

        hr, rr_mean, rr_std, rhythm_regular = _compute_hr_for_ms_per_px(
            rr_px_all, ms_per_px
        )

        if rr_mean and rr_std:
            rr_cv_pct = round((rr_std / rr_mean) * 100.0, 1)

        if qrs_px_all:
            qrs_ms = float(np.median(np.array(qrs_px_all))) * ms_per_px

        # Final sanity guard
        if hr is not None and (hr < 25 or hr > 250):
            notes.append(
                f"Calibrated heart-rate ({hr:.0f} bpm) is still outside normal "
                f"physiological range; grid calibration may be unreliable for this image."
            )

        notes.append(f"Calibration method: {calibration_method}")
    else:
        notes.append(
            "Could not detect enough R-R intervals from the waveform image."
        )

    # ── Quality scoring ──
    quality = min(1.0, (usable_rows / max(1, len(lead_rows))) * 0.8 + 0.2)
    if quality >= 0.75:
        confidence = "moderate"
    elif quality >= 0.5:
        confidence = "low-to-moderate"
    else:
        confidence = "low"
        notes.append("Image quality/contrast may be limiting waveform precision.")

    if hr is not None and (hr < _HR_MIN_PLAUSIBLE or hr > _HR_MAX_PLAUSIBLE):
        notes.append(
            "Heart-rate estimate is outside the normal resting range; verify manually."
        )

    if not notes:
        notes.append(
            "Automated ECG CV extraction has limited precision without calibration."
        )

    return ECGFeatures(
        estimated_heart_rate_bpm=round(hr, 1) if hr else None,
        rhythm_regular=rhythm_regular,
        rr_interval_mean_ms=round(rr_mean, 1) if rr_mean else None,
        rr_interval_std_ms=round(rr_std, 1) if rr_std else None,
        qrs_width_proxy_ms=round(qrs_ms, 1) if qrs_ms else None,
        lead_count_estimate=len(lead_rows),
        quality_score=round(quality, 3),
        confidence=confidence,
        technical_notes=notes,
        peak_count=total_peaks,
        usable_lead_rows=usable_rows,
        rr_cv_percent=rr_cv_pct,
        calibration_method=calibration_method,
    )


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------

def _features_to_json(features: ECGFeatures) -> str:
    return json.dumps(
        {
            "estimated_heart_rate_bpm": features.estimated_heart_rate_bpm,
            "rhythm_regular": features.rhythm_regular,
            "rr_interval_mean_ms": features.rr_interval_mean_ms,
            "rr_interval_std_ms": features.rr_interval_std_ms,
            "qrs_width_proxy_ms": features.qrs_width_proxy_ms,
            "lead_count_estimate": features.lead_count_estimate,
            "quality_score": features.quality_score,
            "confidence": features.confidence,
            "peak_count": features.peak_count,
            "usable_lead_rows": features.usable_lead_rows,
            "rr_cv_percent": features.rr_cv_percent,
            "calibration_method": features.calibration_method,
            "technical_notes": features.technical_notes,
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Groq LLM integration
# ---------------------------------------------------------------------------

def _to_data_url(image_path: str) -> str:
    suffix = Path(image_path).suffix.lower().replace(".", "")
    mime = "jpeg" if suffix in {"jpg", "jpeg"} else "png"
    payload = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{payload}"


def _load_groq_api_key() -> str | None:
    global _GROQ_API_KEY
    if _GROQ_API_KEY:
        return _GROQ_API_KEY

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if api_key:
        _GROQ_API_KEY = api_key
        return api_key
    return None


_ECG_SYSTEM_PROMPT = """You are SwasthyaMitra, an expert cardiology report analyst who explains ECG findings in simple layman language that anyone can understand — even someone with no medical background.

You will receive:
1. An ECG waveform image
2. Computer-vision (CV) extracted metrics from that image (heart rate, rhythm regularity, QRS width, etc.)

Your job is to cross-reference the CV metrics with the visual ECG image and produce a structured, patient-friendly report.

You MUST follow this EXACT output format:

---

🩺 **ECG Waveform Extraction Snapshot**

- Lead rows detected: [number]
- Estimated heart rate: [value] bpm
- Mean R-R interval: [value] ms ± [value] ms
- QRS width estimate: [value] ms (proxy)
- Signal quality score: [value] (confidence: [level])

---

📊 **Waveform-Derived Findings**

- [Describe the rhythm — regular vs irregular, sinus vs otherwise]
- [Describe the rate — normal, tachycardia, bradycardia]
- [Describe QRS morphology if notable — narrow, wide, normal]
- [Any other notable waveform observations from the image]

---

✅ **Likely Reassuring Findings**

- [List positive/normal observations from the ECG]
- [Be specific based on what you see in the image AND the CV metrics]

---

⚠️ **Potential Concerns / Uncertain Signals**

- [List any abnormalities or borderline findings]
- [For each concern, explain in simple terms what it COULD mean without causing panic]
- [If CV metrics and visual assessment disagree, note the discrepancy]
- [If nothing is concerning, state: "No obvious high-risk patterns detected."]

---

🧾 **Overall ECG Impression (Non-Diagnostic)**

[2-3 sentences summarizing the overall ECG picture in very simple language. Be specific to THIS ECG — mention the actual heart rate, rhythm pattern, and any unique observations you see.]

---

🥗 **Next Practical Steps**

- [Actionable step 1]
- [Actionable step 2]
- [Actionable step 3]
- [Actionable step 4 — urgent care signs]

---

🚨 **Important**

- This is NOT a medical diagnosis
- ECG image-only analysis can miss nuanced conduction or ischemic changes
- A 12-lead ECG interpreted by a cardiologist is the gold standard
- Always discuss ECG findings with a qualified medical professional

---

🇮🇳 **सरल सारांश (Overall Summary in Hindi)**

[Translate the "Overall ECG Impression" into simple Hindi. Keep it 2-3 sentences.]

---

🇮🇳 **सामान्य सुझाव (Next Steps in Hindi)**

- [Translate each step into simple Hindi]

---

🇮🇳🔸 **સરળ સારાંશ (Overall Summary in Gujarati)**

[Translate the "Overall ECG Impression" into simple Gujarati. Keep it 2-3 sentences.]

---

🇮🇳🔸 **સામાન્ય સૂચનો (Next Steps in Gujarati)**

- [Translate each step into simple Gujarati]

---

RULES:
1. ALWAYS use the exact emoji headers shown above
2. Be warm, reassuring but honest
3. Use plain English — no medical jargon without explanation
4. Give SPECIFIC observations from the actual ECG image, not generic text
5. Cross-reference the CV-extracted metrics with your visual assessment
6. Include actual numeric values from the CV features
7. DO NOT fabricate measurements
8. Hindi and Gujarati sections are MANDATORY"""


def _try_groq_interpretation(features: ECGFeatures, image_path: str) -> str | None:
    """
    Send the ECG image + CV features to Groq's vision model for a rich,
    structured interpretation. Returns None on any failure.
    """
    api_key = _load_groq_api_key()
    if not api_key:
        _safe_print("[ECG] No Groq API key found -- skipping LLM interpretation.")
        return None

    try:
        from groq import Groq

        client = Groq(api_key=api_key)
        feature_json = _features_to_json(features)
        data_url = _to_data_url(image_path)
        model_name = os.environ.get(
            "GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"
        ).strip()

        user_message = (
            "Please analyze the following ECG waveform image and provide a complete "
            "patient-friendly summary in the format specified.\n\n"
            "Here are the computer-vision extracted features from this ECG:\n\n"
            "--- START OF CV FEATURES ---\n"
            f"{feature_json}\n"
            "--- END OF CV FEATURES ---\n\n"
            "Use these CV features as your primary evidence, and validate/supplement "
            "with your visual assessment of the ECG image. If the CV features seem "
            "inconsistent with what you see, note the discrepancy."
        )

        _safe_print(f"[ECG] Calling Groq model: {model_name}")
        resp = client.chat.completions.create(
            model=model_name,
            temperature=0.3,
            max_tokens=4096,
            top_p=0.9,
            messages=[
                {"role": "system", "content": _ECG_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_message},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        )
        content = resp.choices[0].message.content
        if content and len(content.strip()) > 100:
            _safe_print("[ECG] LLM interpretation received successfully.")
            return content.strip()
        _safe_print("[ECG] LLM returned an unexpectedly short response -- using fallback.")
        return None

    except Exception as e:
        _safe_print(f"\n[ECG] LLM API call failed: {e}")
        _safe_print("      Falling back to offline deterministic summarizer...\n")
        return None


# ---------------------------------------------------------------------------
# Deterministic fallback (offline, data-driven)
# ---------------------------------------------------------------------------

def _deterministic_interpretation(features: ECGFeatures) -> str:
    """
    Offline fallback: produce a structured ECG summary from CV features.
    Every section embeds the actual numeric values so that different ECGs
    always produce visibly different summaries.
    """
    f = features  # shorthand

    # ── Snapshot text ──
    hr_text = f"{f.estimated_heart_rate_bpm:.1f} bpm" if f.estimated_heart_rate_bpm else "not reliably estimated"
    rr_text = (
        f"{f.rr_interval_mean_ms:.1f} ms +/- {f.rr_interval_std_ms:.1f} ms"
        if f.rr_interval_mean_ms and f.rr_interval_std_ms
        else "insufficient R-R signal quality"
    )
    qrs_text = (
        f"{f.qrs_width_proxy_ms:.1f} ms (proxy)"
        if f.qrs_width_proxy_ms
        else "not reliably estimated"
    )

    # ── Waveform findings (data-driven) ──
    findings: List[str] = []
    if f.rhythm_regular is True:
        findings.append(
            f"R-R interval coefficient of variation is {f.rr_cv_percent:.1f}% "
            f"(below 12%), indicating a relatively regular rhythm."
            if f.rr_cv_percent is not None
            else "Rhythm appears relatively regular, suggesting a stable pacemaker origin."
        )
    elif f.rhythm_regular is False:
        findings.append(
            f"R-R interval coefficient of variation is {f.rr_cv_percent:.1f}% "
            f"(above 12%), indicating variable/irregular rhythm — "
            f"could be normal variation (respiratory sinus arrhythmia) or possible arrhythmia."
            if f.rr_cv_percent is not None
            else "Rhythm appears variable/irregular — clinical correlation recommended."
        )
    else:
        findings.append("Rhythm regularity could not be assessed from this image.")

    if f.estimated_heart_rate_bpm is not None:
        hr = f.estimated_heart_rate_bpm
        if 60 <= hr <= 100:
            findings.append(
                f"Heart rate (~{hr:.0f} bpm) is within the normal resting range (60-100 bpm)."
            )
        elif hr > 100:
            findings.append(
                f"Heart rate (~{hr:.0f} bpm) is above the normal resting range (>100 bpm) — "
                f"may indicate sinus tachycardia due to exercise, anxiety, caffeine, "
                f"fever, dehydration, or other causes."
            )
        elif hr < 60:
            findings.append(
                f"Heart rate (~{hr:.0f} bpm) is below the normal resting range (<60 bpm) — "
                f"may indicate sinus bradycardia, which can be normal in athletes or during sleep."
            )
        else:
            findings.append(f"Heart rate is approximately {hr:.0f} bpm.")
    else:
        findings.append("Heart rate could not be reliably estimated from the waveform.")

    if f.qrs_width_proxy_ms is not None:
        qrs = f.qrs_width_proxy_ms
        if qrs <= 120:
            findings.append(
                f"QRS width proxy (~{qrs:.0f} ms) is within normal limits "
                f"(<=120 ms), suggesting normal ventricular conduction."
            )
        else:
            findings.append(
                f"QRS width proxy (~{qrs:.0f} ms) appears widened (>120 ms), "
                f"which could suggest a conduction delay. Needs cardiologist review."
            )

    findings.append(
        f"Analysis detected {f.peak_count} R-peaks across "
        f"{f.usable_lead_rows} usable lead row(s) "
        f"(out of {f.lead_count_estimate} detected)."
    )

    findings_text = "\n".join(f"- {x}" for x in findings)

    # ── Reassuring findings ──
    reassuring: List[str] = []
    if f.quality_score >= 0.5:
        reassuring.append(
            f"ECG trace was detectable across {f.usable_lead_rows} "
            f"of {f.lead_count_estimate} lead rows "
            f"(quality score: {f.quality_score:.2f})."
        )
    if f.estimated_heart_rate_bpm and 50 <= f.estimated_heart_rate_bpm <= 110:
        reassuring.append(
            f"Heart rate ({f.estimated_heart_rate_bpm:.0f} bpm) is within "
            f"or near the normal resting range."
        )
    if f.rhythm_regular is True:
        reassuring.append("Regular rhythm pattern detected — a positive sign.")
    if f.qrs_width_proxy_ms and f.qrs_width_proxy_ms <= 120:
        reassuring.append(
            f"QRS complexes appear narrow (~{f.qrs_width_proxy_ms:.0f} ms), "
            f"suggesting normal ventricular conduction."
        )
    if not reassuring:
        reassuring.append("Core ECG complexes were detectable in the uploaded image.")
    reassuring_text = "\n".join(f"- {x}" for x in reassuring)

    # ── Concerns ──
    concerns: List[str] = []
    if f.estimated_heart_rate_bpm:
        if f.estimated_heart_rate_bpm > 100:
            concerns.append(
                f"Heart-rate ({f.estimated_heart_rate_bpm:.0f} bpm) trends high — "
                f"possible tachycardia. Could be due to exercise, anxiety, "
                f"dehydration, fever, or cardiac causes."
            )
        elif f.estimated_heart_rate_bpm < 50:
            concerns.append(
                f"Heart-rate ({f.estimated_heart_rate_bpm:.0f} bpm) trends low — "
                f"possible bradycardia. May be normal for athletes or during sleep, "
                f"but could also indicate conduction issues."
            )
    if f.qrs_width_proxy_ms and f.qrs_width_proxy_ms > 120:
        concerns.append(
            f"QRS proxy ({f.qrs_width_proxy_ms:.0f} ms) appears widened (>120 ms); "
            f"conduction delay cannot be excluded."
        )
    if f.rhythm_regular is False:
        concerns.append(
            f"R-R variability is elevated "
            f"(CV={f.rr_cv_percent:.1f}%); "
            f"this suggests potential rhythm irregularity."
            if f.rr_cv_percent is not None
            else "R-R variability is elevated, suggesting potential rhythm irregularity."
        )
    if f.quality_score < 0.5:
        concerns.append(
            f"Image quality score is low ({f.quality_score:.2f}) — "
            f"findings should be interpreted with extra caution."
        )
    if not concerns:
        concerns.append("No obvious high-risk waveform patterns detected by automated analysis.")
    concern_text = "\n".join(f"- {x}" for x in concerns)

    # ── Overall impression ──
    if f.estimated_heart_rate_bpm and _HR_MIN_PLAUSIBLE <= f.estimated_heart_rate_bpm <= _HR_MAX_PLAUSIBLE:
        hr_v = f.estimated_heart_rate_bpm
        if 55 <= hr_v <= 105 and f.rhythm_regular is True:
            impression = (
                f"This ECG shows a heart rate of approximately {hr_v:.0f} bpm "
                f"with a regular rhythm (R-R interval: {rr_text}). "
                f"Based on automated image analysis, no obvious high-risk patterns were detected. "
                f"However, subtle changes (like ST-segment shifts) may not be captured from images alone. "
                f"Please have a cardiologist review the original ECG for a definitive interpretation."
            )
        else:
            status_parts = []
            if hr_v > 100:
                status_parts.append(f"elevated heart rate ({hr_v:.0f} bpm)")
            elif hr_v < 60:
                status_parts.append(f"lower-than-average heart rate ({hr_v:.0f} bpm)")
            else:
                status_parts.append(f"heart rate of {hr_v:.0f} bpm")
            if f.rhythm_regular is False:
                status_parts.append("some rhythm variability")
            status_str = " and ".join(status_parts)
            impression = (
                f"This ECG shows {status_str}. "
                f"Some findings may warrant clinical attention (see concerns above). "
                f"A cardiologist should review the original ECG recording, especially if "
                f"you have symptoms like chest pain, dizziness, or palpitations."
            )
    else:
        if f.estimated_heart_rate_bpm:
            impression = (
                f"The automated system estimated a heart-rate of ~{f.estimated_heart_rate_bpm:.0f} bpm, "
                f"which is outside the expected physiological range. This likely indicates a "
                f"calibration issue with the image rather than an actual cardiac abnormality. "
                f"Please have the ECG reviewed by a cardiologist with the original recording."
            )
        else:
            impression = (
                "The automated system could not reliably extract heart rate from this ECG image. "
                "This may be due to image quality, unusual ECG format, or very faint waveforms. "
                "Please re-capture a clearer image or consult a cardiologist with the original ECG."
            )

    # ── Hindi ──
    if f.estimated_heart_rate_bpm and _HR_MIN_PLAUSIBLE <= f.estimated_heart_rate_bpm <= _HR_MAX_PLAUSIBLE:
        hindi_imp = (
            f"इस ECG में हृदय गति लगभग {f.estimated_heart_rate_bpm:.0f} bpm दिखाई गई है। "
            + (
                "यह सामान्य सीमा में है। "
                if 55 <= f.estimated_heart_rate_bpm <= 105
                else "इसे डॉक्टर से जांच करवाना चाहिए। "
            )
            + "कृपया हृदय रोग विशेषज्ञ से अपनी ECG की पुष्टि करवाएं।"
        )
    else:
        hindi_imp = (
            "स्वचालित प्रणाली इस ECG छवि से हृदय गति को ठीक से नहीं माप सकी। "
            "कृपया स्पष्ट छवि लें या हृदय रोग विशेषज्ञ से संपर्क करें।"
        )

    # ── Gujarati ──
    if f.estimated_heart_rate_bpm and _HR_MIN_PLAUSIBLE <= f.estimated_heart_rate_bpm <= _HR_MAX_PLAUSIBLE:
        guj_imp = (
            f"આ ECG માં હૃદય ગતિ લગભગ {f.estimated_heart_rate_bpm:.0f} bpm છે। "
            + (
                "આ સામાન્ય શ્રેણીમાં છે। "
                if 55 <= f.estimated_heart_rate_bpm <= 105
                else "આને ડૉક્ટર દ્વારા તપાસ કરાવવી જોઈએ। "
            )
            + "કૃપયા હૃદય રોગ નિષ્ણાત સાથે ECG ની ચકાસણી કરાવો।"
        )
    else:
        guj_imp = (
            "ઓટોમેટેડ સિસ્ટમ આ ECG ઈમેજમાંથી હૃદય ગતિ યોગ્ય રીતે માપી શક્યું નથી। "
            "કૃપયા સ્પષ્ટ ઈમેજ લો અથવા હૃદય રોગ નિષ્ણાતનો સંપર્ક કરો।"
        )

    notes = f.technical_notes or ["Automated ECG extraction has limited precision."]
    note_lines = "\n".join(f"  - {n}" for n in notes)

    return f"""🩺 **ECG Waveform Extraction Snapshot**

- Lead rows detected: {f.lead_count_estimate} ({f.usable_lead_rows} usable)
- R-peaks detected: {f.peak_count}
- Estimated heart rate: {hr_text}
- Mean R-R interval: {rr_text}
- QRS width estimate: {qrs_text}
- Signal quality score: {f.quality_score:.2f} (confidence: {f.confidence})

---

📊 **Waveform-Derived Findings**

{findings_text}

---

✅ **Likely Reassuring Findings**

{reassuring_text}

---

⚠️ **Potential Concerns / Uncertain Signals**

{concern_text}

---

🧾 **Overall ECG Impression (Non-Diagnostic)**

{impression}

---

🥗 **Next Practical Steps**

- If available, compare this ECG with a prior baseline ECG for any changes.
- Re-capture a clearer scan/photo if the trace is faint, skewed, or partially cut off.
- If you are experiencing symptoms (chest pain, palpitations, dizziness), see a cardiologist promptly.
- Seek urgent/emergency care for severe chest pain, fainting, severe breathlessness, or persistent palpitations.

---

🚨 **Important**

- This is NOT a medical diagnosis.
- ECG image-only analysis can miss nuanced conduction or ischemic changes.
- A 12-lead ECG interpreted by a cardiologist is the gold standard.
- Always discuss ECG findings with a qualified medical professional.
- Technical notes:
{note_lines}

---

🇮🇳 **सरल सारांश (Overall Summary in Hindi)**

{hindi_imp}

---

🇮🇳 **सामान्य सुझाव (Next Steps in Hindi)**

- यदि उपलब्ध हो, तो इस ECG की तुलना पिछली ECG से करें।
- यदि ट्रेस धुंधला या टेढ़ा है, तो स्पष्ट स्कैन/फोटो लें।
- यदि सीने में दर्द, धड़कन बढ़ना, या चक्कर आ रहा है, तो हृदय रोग विशेषज्ञ से मिलें।
- तीव्र सीने में दर्द, बेहोशी, या सांस की तकलीफ में तुरंत आपातकालीन सहायता लें।

---

🇮🇳🔸 **સરળ સારાંશ (Overall Summary in Gujarati)**

{guj_imp}

---

🇮🇳🔸 **સામાન્ય સૂચનો (Next Steps in Gujarati)**

- જો ઉપલબ્ધ હોય, તો આ ECG ની તુલના પહેલાની ECG સાથે કરો.
- જો ટ્રેસ ઝાંખું અથવા વાંકું હોય, તો ફરીથી સ્પષ્ટ ફોટો લો.
- જો છાતીમાં દુખાવો, ધબકારા, અથવા ચક્કર આવે, તો હૃદય રોગ નિષ્ણાતને મળો.
- તીવ્ર છાતીમાં દુખાવો, બેભાન, અથવા શ્વાસ લેવામાં તકલીફ હોય તો તાત્કાલિક સહાય લો.
"""


# ---------------------------------------------------------------------------
# Interpretation dispatcher
# ---------------------------------------------------------------------------

def _interpret(features: ECGFeatures, image_path: str) -> str:
    """Pluggable interpretation layer: try LLM first, fallback to deterministic."""
    llm_summary = _try_groq_interpretation(features, image_path)
    if llm_summary:
        return llm_summary
    return _deterministic_interpretation(features)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_ecg(file_path: str) -> Dict[str, str]:
    """
    Public API used by app.py
    Returns structure compatible with medical_Extract() output contract.
    """
    file_path = str(file_path)
    if not _is_supported_image(file_path):
        raise ValueError(
            "ECG graphical analysis currently supports PNG/JPG/JPEG images only."
        )

    image = _load_image(file_path)
    features = _extract_features(image)
    summary = _interpret(features, file_path)

    return {
        "file_path": file_path,
        "raw_text": _features_to_json(features),
        "layman_summary": summary,
    }
