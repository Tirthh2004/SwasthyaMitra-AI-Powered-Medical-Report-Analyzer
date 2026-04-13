# Legal Lens: Presentation Guide (Local LLMs & Challenges)

This guide provides strategic answers for faculty questions regarding the technical difficulties faced during the development of **Legal Lens** (SwasthyaMitra) using local models like BioBERT, Llama, and Flan-T5.

## 🚀 Core Argument: Why Local Models?
If asked why you didn't just use APIs (like OpenAI), your answer should focus on **Data Privacy**.
> "Medical data is extremely sensitive. By using local models like Flan-T5 and BioBERT, we ensure that patient reports are processed entirely on-device, maintaining total privacy and compliance with healthcare data standards."

---

## 🛠️ Difficulty 1: Hardware Constraints & Model Size
**Problem:** Local LLMs (Large Language Models) like Llama or BioBERT require significant VRAM and RAM, which standard college laptops often lack.
**The "Overcome" Strategy:**
*   **Quantization:** Explain that you used **4-bit or 8-bit quantization** (converting model weights to smaller formats) to fit a 7B or 8B parameter Llama model into 8GB of RAM.
*   **Model Selection:** Mention using **Flan-T5 Small/Base** for summarization tasks because it is highly efficient and performs well on specific instruction-following tasks without needing a GPU.
*   **BioBERT for NER:** Explain that instead of using a giant model for everything, you used **BioBERT** specifically for *Named Entity Recognition* (extracting medicine names, lab values) because it is specialized for medical text and smaller than general-purpose LLMs.

## 🔍 Difficulty 2: OCR "Noise" in Medical Reports
**Problem:** Medical reports are often scanned images with low quality. Tesseract OCR often misreads numbers (e.g., "1.0" as "10") or medical terms.
**The "Overcome" Strategy:**
*   **Image Pre-processing:** Used **OpenCV** to convert images to grayscale and apply **Gaussian Blur**/Thresholding to remove background noise before passing it to Tesseract.
*   **Heuristic Verification:** In `medical_summarizer.py`, we implemented a **fallback heuristic logic** that validates extracted values against known medical ranges. If the OCR gives an impossible value, the system flags it or ignores it.

## ⚡ Difficulty 3: Latency & Inference Speed
**Problem:** Local models are significantly slower than APIs. Generating a summary could take 30-60 seconds on a CPU.
**The "Overcome" Strategy:**
*   **Caching Extraction:** We cached the extracted text so that if the user asks a follow-up question in the chatbot, the system doesn't need to perform OCR again.
*   **Hybrid Search logic:** In the chatbot (`chatbot_engine.py`), we used **keyword-based retrieval** from local PDFs/Excels instead of running a full LLM search for every query. This makes the "Doctor Search" instant while keeping "Report Analysis" for the LLM.

## 🧪 Difficulty 4: Medical Context Hallucinations
**Problem:** General LLMs sometimes misunderstand lab results (e.g., thinking a high WBC count is always good because "high" sounds positive).
**The "Overcome" Strategy:**
*   **Specialized Prompting:** We used **Few-Shot Prompting** where we provided the LLM with examples of how to interpret specific lab values (Normal vs. Abnormal).
*   **BioBERT Embeddings:** Mention that you used **BioBERT** to understand the *semantics* of medical terms, ensuring that "Hypertension" and "High Blood Pressure" are treated as the same concept during search.

---

## 💡 Pro-Tips for the Presentation
*   **Demonstrate the Fallback:** If the local model is too slow during the demo, show the **Heuristic Summary** (the one using regex) and explain that it's the "Lite" version of the AI.
*   **Show the Code Structure:** Point to `medical_extract.py` for OCR and `medical_summarizer.py` for the AI logic. This proves you built the pipeline yourself.
*   **Mention "GGUF":** If they ask about Llama, mention you used the **GGUF format** with `llama-cpp-python` for CPU-optimized inference. This sounds very technical and professional.
