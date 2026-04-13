from ecg_vision import _load_image, _extract_features, _try_groq_interpretation

image_path = "img1.png"
print("Extracting features...")
feature = _extract_features(_load_image(image_path))

print(f"Calling try_groq_interpretation on {image_path}...")
res = _try_groq_interpretation(feature, image_path)
print("Result snippet:")
print(str(res)[:500] if res else "None")
