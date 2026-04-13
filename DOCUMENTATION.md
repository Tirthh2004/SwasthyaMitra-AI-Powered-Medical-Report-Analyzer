# 📖 Technical Documentation: SwasthyaMitra

This document provides a deep-dive into the technical implementation, module structure, and logic of the **SwasthyaMitra** (Legal Lens: Text Extraction) project.

---

## 1. System Overview
SwasthyaMitra is a RAG (Retrieval-Augmented Generation) based system meant for medical document analysis and patient assistance. It consists of three primary layers:
1.  **Extraction Layer**: Converts PDFs and Images to machine-readable text.
2.  **Analysis Layer**: Interprets medical data using Large Language Models (LLMs).
3.  **Chatbot/Assistance Layer**: Provides semantic search over healthcare providers using FAISS vector indexing.

---

## 2. Module Reference

### `app.py`
The main entry point for the Flask web server.
- **Endpoints**:
  - `/`: Serves the SPAs (Single Page Application) frontend.
  - `/api/analyze` (POST): Accepts file uploads, triggers extraction, and returns AI-generated layman summaries.
  - `/api/chat` (POST): Processes user messages through the chatbot engine.
  - `/api/health` (GET): Simple service health check.

### `medical_extract.py`
The core OCR and PDF parsing engine.
- **`extract_text(file_path)`**: The primary function.
  - Uses `pdfplumber` for text-based PDFs.
  - Uses `pytesseract` + `OpenCV` (Grayscale) for images.
  - **Configuration**: Requires the Tesseract executable path to be set in `pytesseract.pytesseract.tesseract_cmd`.

### `medical_summarizer.py`
The AI interpreter that turns raw lab text into patient-friendly summaries.
- **LLM Engine**: Uses Groq's `llama-3.3-70b-versatile` model.
- **Prompt Logic**: Instructs the LLM to output a specific template with emoji headers.
- **Heuristic Fallback**: Contains `_heuristic_fallback()`, which uses regex to identify blood test values and reference ranges even when the LLM is inaccessible.

### `chatbot_engine.py`
A RAG-powered semantic search engine for the health assistant.
- **Indexing**: On startup, it parses files in the `Doctor's Data` folder.
- **Search Logic**: 
  - Uses `all-MiniLM-L6-v2` for sentence embeddings.
  - Uses `FAISS` (IndexFlatIP) for fast similarity matching.
  - Includes a keyword-based fallback if the `sentence-transformers` library fails to load.

---

## 3. Data Management

### Doctor's Data (`/Doctor's Data`)
The system indexes several data sources for the chatbot:
- **Ahmedabad/Mumbai Doctors**: Sourced from PDF and Excel files.
- **Gujarat Hospitals**: Sourced from PDF.
- **AMC Health Centers**: Sourced from PDF.

### Vector Storage
The vector index is built in-memory during the `init()` call in `chatbot_engine.py`. This ensures the latest data is always searchable without manual re-indexing steps.

---

## 4. API Specification

### Analysis API
- **Endpoint**: `/api/analyze`
- **Method**: `POST`
- **Payload**: `multipart/form-data` with a `file` field.
- **Response**:
  ```json
  {
    "success": true,
    "filename": "report.pdf",
    "summary": "...AI text...",
    "raw_text": "...raw text...",
    "timestamp": "25 Feb 2026, 09:41 PM"
  }
  ```

### Chat API
- **Endpoint**: `/api/chat`
- **Method**: `POST`
- **Payload**: `{"message": "Eye doctor in Ahmedabad"}`
- **Response**:
  ```json
  {
    "reply": "...HTML formatted results..."
  }
  ```

---

## 5. Troubleshooting Guide

| Issue | Potential Solution |
| :--- | :--- |
| **TesseractNotFoundError** | Ensure Tesseract is installed and the path in `medical_extract.py` matches your system. |
| **API Analysis Failed** | Check if `GROQ_API.txt` contains a valid key and ensure internet connectivity. |
| **Chatbot missing results** | Ensure files in `Doctor's Data` are correctly named and follow the mapping in `chatbot_engine.py`. |
| **Excel loading error** | Run `pip install openpyxl` to support XLSX/XLS parsing. |

---

## 6. Future Enhancements
- **Multi-lingual Support**: Translating summaries into Hindi/Gujarati.
- **Cloud Hosting**: Containerizing the Flask app for deployment on Render/AWS.
- **Persistent Vector DB**: Using Pinecone or ChromaDB for persistent storage of large-scale doctor databases.
