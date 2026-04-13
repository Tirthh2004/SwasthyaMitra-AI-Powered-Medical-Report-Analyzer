#!/usr/bin/env python3
"""
SIMPLE TEXT EXTRACTOR
Usage: python simple_extractor.py <file_path>
"""

import sys
from pathlib import Path

def extract_text(file_path):
    """Extract text from PDF or image file"""
    
    file_path = Path(file_path)
    extension = file_path.suffix.lower()
    
    # PDF extraction
    if extension == '.pdf':
        import pdfplumber
        text = ""
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text += page.extract_text() + "\n"
        return text
    
    # Image extraction (PNG, JPG, JPEG)
    # elif extension in ['.png', '.jpg', '.jpeg']:
    #     import pytesseract
    #     from PIL import Image
    #     text = pytesseract.image_to_string(Image.open(file_path))
    #     return text
    elif extension in ['.png', '.jpg', '.jpeg']:
        import pytesseract
        import cv2
        from PIL import Image
    
        pytesseract.pytesseract.tesseract_cmd = r"C:\Users\TylerShah\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"
    
        img = cv2.imread(str(file_path))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
        text = pytesseract.image_to_string(gray)
        return text

    else:
        return "Error: File must be PDF, PNG, JPG, or JPEG"

# Run if called directly
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python simple_extractor.py <file_path>")
        print("\nExample:")
        print("  python simple_extractor.py report.pdf")
        print("  python simple_extractor.py report.png")
        sys.exit(1)
    
    file_path = sys.argv[1]
    print(f"Extracting text from: {file_path}\n")
    
    text = extract_text(file_path)
    print(text)
    
    # Optionally save to file
    output = Path(file_path).stem + "_output.txt"
    with open(output, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f"\n✓ Text saved to: {output}")