#!/usr/bin/env python3
"""
SwasthyaMitra — Flask Web Backend
Serves the frontend and provides API endpoints for medical report analysis.
"""

import os
import sys
import uuid
import json
import math
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template, request, jsonify

# Ensure the project root is on sys.path so we can import our modules.
sys.path.insert(0, str(Path(__file__).parent))
from medical_summarizer import medical_Extract
from ecg_vision import analyze_ecg
import chatbot_engine

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB max upload

UPLOAD_FOLDER = Path(__file__).parent / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
ECG_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


def _allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the main single-page application."""
    return render_template("index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """
    Accept a medical report file upload, extract text, run LLM analysis,
    and return the summary as JSON.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not _allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type. Please upload PDF, PNG, JPG, or JPEG."}), 400

    is_ecg_graph = request.form.get("is_ecg_graph", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # Save to uploads/ with a unique name to avoid collisions
    ext = Path(file.filename).suffix.lower()
    unique_name = f"{uuid.uuid4().hex}{ext}"
    save_path = UPLOAD_FOLDER / unique_name

    try:
        file.save(str(save_path))

        if is_ecg_graph:
            if ext not in ECG_IMAGE_EXTENSIONS:
                return jsonify(
                    {
                        "error": "ECG graphical analysis currently supports image uploads only (PNG/JPG/JPEG)."
                    }
                ), 400
            result = analyze_ecg(str(save_path))
            analysis_type = "ecg_graph"
        else:
            # Existing pipeline: extract text -> LLM summary
            result = medical_Extract(str(save_path))
            analysis_type = "text_report"

        return jsonify({
            "success": True,
            "filename": file.filename,
            "summary": result["layman_summary"],
            "raw_text": result["raw_text"],
            "analysis_type": analysis_type,
            "timestamp": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        })

    except Exception as e:
        return jsonify({"error": f"Analysis failed: {str(e)}"}), 500

    finally:
        # Clean up the uploaded file
        if save_path.exists():
            try:
                save_path.unlink()
            except OSError:
                pass


@app.route("/api/chat", methods=["POST"])
def chat():
    """Chatbot endpoint — accepts a message and returns a reply."""
    data = request.get_json(silent=True)
    if not data or "message" not in data:
        return jsonify({"error": "No message provided"}), 400

    context = data.get("context", "")
    reply = chatbot_engine.get_response(data["message"], context=context)
    return jsonify({"reply": reply})


def _haversine_km(lat1, lon1, lat2, lon2):
    """Return the distance in kilometres between two lat/lng points."""
    R = 6371  # Earth radius in km
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(d_lon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@app.route("/api/recommend-doctors", methods=["POST"])
def recommend_doctors():
    """
    Accept a report summary, determine relevant specialties from it,
    and return a JSON list of recommended doctors with map coordinates.
    Optionally accepts user lat/lng to sort by proximity.
    """
    data = request.get_json(silent=True)
    if not data or "summary" not in data:
        return jsonify({"error": "No summary provided"}), 400

    summary = data["summary"].lower()

    # ── Map keywords in the report to medical specialties ──
    SPECIALTY_MAP = {
        "hematologist":    ["hemoglobin", "hb ", "rbc", "wbc", "platelet", "anemia", "blood count", "hematocrit", "mcv", "mch", "mchc", "ferritin", "iron", "leukemia", "lymphoma", "coagulation"],
        "endocrinologist":  ["thyroid", "tsh", "t3", "t4", "hba1c", "insulin", "cortisol", "hormone", "pcos", "testosterone", "estrogen", "prolactin", "gland", "pituitary"],
        "diabetologist":   ["glucose", "sugar", "diabetes", "fasting blood sugar", "hba1c", "glycated", "bsf", "ppbs", "neuropathy", "diabetic"],
        "cardiologist":    ["cholesterol", "ldl", "hdl", "triglyceride", "lipid", "cardiac", "heart", "bp ", "blood pressure", "ecg", "ekg", "chest pain", "arrhythmia", "echo", "troponin"],
        "hepatologist":    ["liver", "sgpt", "sgot", "bilirubin", "alt", "ast", "alkaline phosphatase", "alp", "albumin", "hepatitis", "cirrhosis", "jaundice", "ascites"],
        "nephrologist":    ["kidney", "creatinine", "urea", "bun", "gfr", "uric acid", "renal", "dialysis", "nephropathy", "proteinuria", "hematuria"],
        "pulmonologist":   ["lung", "respiratory", "chest", "oxygen", "spo2", "pulmonary", "asthma", "copd", "breath", "wheezing", "tuberculosis", "x-ray", "spirometry"],
        "orthopedic":      ["calcium", "vitamin d", "bone", "phosphorus", "joint", "fracture", "arthritis", "spine", "back pain", "knee", "ortho"],
        "general physician": ["fever", "cough", "infection", "general", "routine", "check-up", "viral", "weakness", "fatigue", "cold", "flu", "body ache"],
        "urologist":       ["urine", "urinary", "uti", "prostate", "psa", "bladder", "stone", "calculus", "hematuria", "dysuria"],
        "gastroenterologist": ["stomach", "digestion", "gastric", "abdomen", "gi ", "ulcer", "acid", "reflux", "gerd", "bowel", "colon", "endoscopy", "diarrhea", "constipation"],
        "oncologist":      ["cancer", "tumor", "tumour", "malignant", "oncology", "carcinoma", "biopsy", "chemo", "radiation", "marker", "cea", "psa", "ca-125"],
        "dermatologist":   ["skin", "rash", "allergy", "ige", "acne", "hair", "nail", "eczema", "psoriasis", "melanoma", "dermatitis"],
        "neurologist":     ["brain", "nerve", "stroke", "neurology", "headache", "seizure", "migraine", "eeg", "mri", "dementia", "numbness", "tingling", "tremor", "epilepsy"],
        "gynecologist":    ["women", "pregnancy", "uterus", "ovary", "menstrual", "pcos", "gynecology", "pap smear", "cervix", "fibroid", "period", "menopause", "maternal"],
        "pediatrician":    ["child", "infant", "pediatric", "baby", "kid", "toddler", "growth", "vaccination", "immunization", "newborn"],
        "psychiatrist":    ["mental", "depression", "anxiety", "psychiatry", "stress", "mood", "bipolar", "schizophrenia", "hallucination", "adhd", "ocd"],
        "psychologist":    ["mental", "therapy", "counseling", "psychology", "stress", "mood", "behavior", "cognitive", "trauma", "ptsd", "phobia"],
    }

    import re
    # Extract only the abnormal (❌) and slightly elevated/low (⚠️) sections for keyword mapping
    # This prevents normal tests mentioned in "What This Report Tests" from triggering specialists.
    parts = re.split(r'(🩺|📊|✅|⚠️|❌|🧾|🥗|🚨|🇮🇳|🍎)', summary)
    abnormal_text = ""
    current_marker = ""
    
    for part in parts:
        if part in ['🩺', '📊', '✅', '⚠️', '❌', '🧾', '🥗', '🚨', '🇮🇳', '🍎']:
            current_marker = part
        elif current_marker in ['⚠️', '❌']:
            abnormal_text += " " + part

    matched_specialties = []
    
    if abnormal_text.strip():
        for specialty, keywords in SPECIALTY_MAP.items():
            for kw in keywords:
                if kw in abnormal_text:
                    if specialty not in matched_specialties:
                        matched_specialties.append(specialty)
                    break

    # Default to general physician if no abnormalities found or no keywords matched
    if not matched_specialties:
        matched_specialties = ["general physician"]
    
    # ── Curated doctor database with Ahmedabad/Gujarat coordinates ──
    DOCTOR_DB = [
        {"name": "Dr. Tejas Patel",          "specialty": "cardiologist",         "hospital": "Apex Heart Institute",        "area": "Nehrunagar, Ahmedabad",    "lat": 23.0395, "lng": 72.5560, "phone": "079-2642-1111"},
        {"name": "Dr. Aditi Desai",           "specialty": "cardiologist",         "hospital": "Care Institute of Medical Sciences", "area": "Sola, Ahmedabad", "lat": 23.0670, "lng": 72.5188, "phone": "079-3010-1010"},
        {"name": "Dr. Manish Patel",          "specialty": "diabetologist",        "hospital": "DiaCare Hospital",            "area": "Bodakdev, Ahmedabad",      "lat": 23.0431, "lng": 72.5099, "phone": "079-4026-3300"},
        {"name": "Dr. Banshi Saboo",          "specialty": "diabetologist",        "hospital": "DiaCare - Diabetes Care",     "area": "Paldi, Ahmedabad",         "lat": 23.0147, "lng": 72.5638, "phone": "079-2657-7700"},
        {"name": "Dr. Harshad Patel",         "specialty": "endocrinologist",      "hospital": "Sterling Endocrine Clinic",   "area": "Gurukul, Ahmedabad",       "lat": 23.0387, "lng": 72.5316, "phone": "079-2740-1234"},
        {"name": "Dr. Kamlesh Patel",         "specialty": "endocrinologist",      "hospital": "Apollo Hospital",             "area": "Gandhinagar Hwy, Ahmedabad","lat": 23.0575, "lng": 72.5339, "phone": "079-6605-0505"},
        {"name": "Dr. Bipin Shah",            "specialty": "hepatologist",         "hospital": "Shalby Hospital",             "area": "SG Highway, Ahmedabad",    "lat": 23.0257, "lng": 72.5053, "phone": "079-4020-0200"},
        {"name": "Dr. Shruti Patel",          "specialty": "hepatologist",         "hospital": "HCG Hospital",                "area": "Mithakali, Ahmedabad",     "lat": 23.0379, "lng": 72.5566, "phone": "079-2656-8800"},
        {"name": "Dr. Rushi Deshpande",       "specialty": "nephrologist",         "hospital": "IKDRC – Kidney Hospital",     "area": "Civil Hospital, Ahmedabad", "lat": 23.0507, "lng": 72.5974, "phone": "079-2268-0366"},
        {"name": "Dr. Manoj Vithalani",       "specialty": "nephrologist",         "hospital": "Sterling Hospital",           "area": "Gurukul, Ahmedabad",       "lat": 23.0382, "lng": 72.5309, "phone": "079-4001-5001"},
        {"name": "Dr. Sandip Shah",           "specialty": "hematologist",         "hospital": "SAL Hospital",                "area": "Thaltej, Ahmedabad",       "lat": 23.0515, "lng": 72.5010, "phone": "079-7120-1200"},
        {"name": "Dr. Rina Patel",            "specialty": "hematologist",         "hospital": "CIMS Hospital",               "area": "Science City Rd, Ahmedabad","lat": 23.0700, "lng": 72.5147, "phone": "079-2771-2771"},
        {"name": "Dr. Mukesh Patel",          "specialty": "general physician",    "hospital": "Zydus Hospital",              "area": "Thaltej, Ahmedabad",       "lat": 23.0530, "lng": 72.4990, "phone": "079-6619-0000"},
        {"name": "Dr. Priya Mehta",           "specialty": "general physician",    "hospital": "VS Hospital",                 "area": "Ellisbridge, Ahmedabad",   "lat": 23.0305, "lng": 72.5651, "phone": "079-2657-7621"},
        {"name": "Dr. Amit Patel",            "specialty": "pulmonologist",        "hospital": "UN Mehta Institute",          "area": "Civil Hospital, Ahmedabad", "lat": 23.0500, "lng": 72.5981, "phone": "079-2226-8640"},
        {"name": "Dr. Keyur Parikh",          "specialty": "pulmonologist",        "hospital": "CIMS Hospital",               "area": "Sola, Ahmedabad",          "lat": 23.0703, "lng": 72.5150, "phone": "079-2771-2771"},
        {"name": "Dr. Vikram Shah",           "specialty": "orthopedic",           "hospital": "Shalby Hospital",             "area": "SG Highway, Ahmedabad",    "lat": 23.0260, "lng": 72.5057, "phone": "079-4020-0200"},
        {"name": "Dr. Kalpesh Desai",         "specialty": "orthopedic",           "hospital": "CIMS Hospital",               "area": "Science City, Ahmedabad",  "lat": 23.0698, "lng": 72.5152, "phone": "079-2771-2771"},
        {"name": "Dr. Nirav Shah",            "specialty": "urologist",            "hospital": "Apollo Hospital",             "area": "Gandhinagar Hwy, Ahmedabad","lat": 23.0578, "lng": 72.5342, "phone": "079-6605-0505"},
        {"name": "Dr. Rajesh Patel",          "specialty": "gastroenterologist",   "hospital": "Sterling Hospital",           "area": "Gurukul, Ahmedabad",       "lat": 23.0385, "lng": 72.5312, "phone": "079-4001-5001"},
        {"name": "Dr. Shyam Sablania",        "specialty": "oncologist",           "hospital": "HCG Cancer Centre",           "area": "Mithakali, Ahmedabad",     "lat": 23.0376, "lng": 72.5570, "phone": "079-2656-8800"},
        {"name": "Dr. Deepa Ganatra",         "specialty": "oncologist",           "hospital": "Gujarat Cancer Society",      "area": "Asarwa, Ahmedabad",        "lat": 23.0452, "lng": 72.6040, "phone": "079-2268-8008"},
        {"name": "Dr. Saurin Shah",           "specialty": "dermatologist",        "hospital": "Kutiz Skin Clinic",           "area": "CG Road, Ahmedabad",       "lat": 23.0348, "lng": 72.5610, "phone": "079-2646-4242"},

        # Neurologist → Brain, stroke, nerves
        {"name": "Dr. Sucheta Mudgerikar",    "specialty": "neurologist",           "hospital": "Apollo Hospital, Gandhinagar", "area": "Gandhinagar Hwy, Ahmedabad", "lat": 23.0573, "lng": 72.5339, "phone": "079-6605-0505"},
        {"name": "Dr. Somesh Desai",          "specialty": "neurologist",           "hospital": "Zydus Hospitals",            "area": "Thaltej, Ahmedabad",      "lat": 23.0530, "lng": 72.4990, "phone": "079-6619-0000"},
        {"name": "Dr. Praveen Saxena",        "specialty": "neurologist",           "hospital": "Apollo Hospital, Gandhinagar", "area": "Gandhinagar Hwy, Ahmedabad", "lat": 23.0573, "lng": 72.5339, "phone": "079-6605-0505"},
        {"name": "Dr. Navneet Saraiya",       "specialty": "neurologist",           "hospital": "KD Hospital",                "area": "Chandkheda, Ahmedabad",   "lat": 23.0967, "lng": 72.5756, "phone": "079-2297-1111"},
        {"name": "Dr. Devashish Vyas",        "specialty": "neurologist",           "hospital": "SGVP Holistic Hospital",     "area": "Naroda, Ahmedabad",       "lat": 23.0580, "lng": 72.5772, "phone": "079-2296-1111"},

        # Gynecologist → Women’s health
        {"name": "Dr. Anjali Patel",          "specialty": "gynecologist",          "hospital": "Zydus Hospital",             "area": "Thaltej, Ahmedabad",      "lat": 23.0530, "lng": 72.4990, "phone": "079-6619-0000"},
        {"name": "Dr. Neeta Patel",           "specialty": "gynecologist",          "hospital": "Sunflower Hospital",         "area": "Navrangpura, Ahmedabad",  "lat": 23.0372, "lng": 72.5340, "phone": "079-2757-1111"},
        {"name": "Dr. Manisha Joshi",         "specialty": "gynecologist",          "hospital": "Divine Women's Hospital",    "area": "Naroda, Ahmedabad",       "lat": 23.0578, "lng": 72.5769, "phone": "079-2296-2222"},
        {"name": "Dr. Shweta Patel",          "specialty": "gynecologist",          "hospital": "Apollo Hospital, Gandhinagar", "area": "Gandhinagar Hwy, Ahmedabad", "lat": 23.0573, "lng": 72.5339, "phone": "079-6605-0505"},
        {"name": "Dr. Pooja Mehta",           "specialty": "gynecologist",          "hospital": "CIMS Hospital",              "area": "Science City Rd, Ahmedabad","lat": 23.0700, "lng": 72.5147, "phone": "079-2771-2771"},

        # Pediatrician → Child health
        {"name": "Dr. Deepak Patel",          "specialty": "pediatrician",          "hospital": "Zydus Hospital",             "area": "Thaltej, Ahmedabad",      "lat": 23.0530, "lng": 72.4990, "phone": "079-6619-0000"},
        {"name": "Dr. Ravi Mehta",            "specialty": "pediatrician",          "hospital": "SGVP Holistic Hospital",     "area": "Naroda, Ahmedabad",       "lat": 23.0580, "lng": 72.5772, "phone": "079-2296-1111"},
        {"name": "Dr. Anjali Shah",           "specialty": "pediatrician",          "hospital": "Apollo Hospital, Gandhinagar", "area": "Gandhinagar Hwy, Ahmedabad", "lat": 23.0573, "lng": 72.5339, "phone": "079-6605-0505"},
        {"name": "Dr. Nidhi Patel",           "specialty": "pediatrician",          "hospital": "CIMS Hospital",              "area": "Science City Rd, Ahmedabad","lat": 23.0700, "lng": 72.5147, "phone": "079-2771-2771"},
        {"name": "Dr. Kaushik Prajapati",     "specialty": "pediatrician",          "hospital": "KD Hospital",                "area": "Chandkheda, Ahmedabad",   "lat": 23.0967, "lng": 72.5756, "phone": "079-2297-1111"},

        # Psychiatrist / Psychologist → Mental health
        {"name": "Dr. Rakesh Sanghadiya",     "specialty": "psychiatrist",          "hospital": "Aatman Hospital",            "area": "Gurukul, Ahmedabad",      "lat": 23.0382, "lng": 72.5309, "phone": "079-2689-1111"},
        {"name": "Dr. Anjali Shah",           "specialty": "psychiatrist",          "hospital": "Parth Hospital",             "area": "Narol, Ahmedabad",        "lat": 23.0325, "lng": 72.5540, "phone": "079-2211-2222"},
        {"name": "Dr. Prakash Patel",         "specialty": "psychiatrist",          "hospital": "Apollo Hospital, Gandhinagar", "area": "Gandhinagar Hwy, Ahmedabad", "lat": 23.0573, "lng": 72.5339, "phone": "079-6605-0505"},
        {"name": "Dr. Neha Mehta",            "specialty": "psychologist",          "hospital": "Civil Hospital Psychiatry Unit", "area": "Ellisbridge, Ahmedabad","lat": 23.0305, "lng": 72.5651, "phone": "079-2657-7621"},
        {"name": "Dr. Rohan Desai",           "specialty": "psychiatrist",          "hospital": "SGVP Holistic Hospital",     "area": "Naroda, Ahmedabad",       "lat": 23.0580, "lng": 72.5772, "phone": "079-2296-1111"},
    ]

    # Filter doctors matching the detected specialties
    recommended = [dict(d) for d in DOCTOR_DB if d["specialty"] in matched_specialties]

    # If too few results, pad with general physicians
    if len(recommended) < 2:
        for d in DOCTOR_DB:
            if d["specialty"] == "general physician" and dict(d) not in recommended:
                recommended.append(dict(d))

    # ── Dynamic location-based sorting ──
    user_lat = data.get("lat")
    user_lng = data.get("lng")
    user_location_available = (
        user_lat is not None and user_lng is not None
    )

    if user_location_available:
        try:
            user_lat = float(user_lat)
            user_lng = float(user_lng)
            for doc in recommended:
                doc["distance_km"] = round(
                    _haversine_km(user_lat, user_lng, doc["lat"], doc["lng"]), 1
                )
            recommended.sort(key=lambda d: d["distance_km"])
        except (ValueError, TypeError):
            user_location_available = False

    return jsonify({
        "success": True,
        "matched_specialties": matched_specialties,
        "doctors": recommended,
        "user_location": user_location_available,
    })



@app.route("/api/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "service": "SwasthyaMitra"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    chatbot_engine.init()  # Pre-load Doctor's Data for chatbot
    print("\n🩺 SwasthyaMitra — Medical Report Analyzer")
    print("   Open http://localhost:5000 in your browser\n")
    app.run(debug=True, host="0.0.0.0", port=5000)
