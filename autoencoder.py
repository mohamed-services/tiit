#!/usr/bin/env python3
"""Image autoencoder pipeline using Gemini models.

Encodes images to text via Gemini Flash, reconstructs them via Gemini image
generation, and computes RMSE between originals and reconstructions.
"""

import argparse
import io
import os
import sys
from pathlib import Path

# Fix Windows console encoding
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image

ENCODE_MODEL = "gemini-3.1-flash-lite-preview"
DECODE_MODEL = "gemini-3.1-flash-image-preview"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}


def load_prompt(mode: str) -> str:
    """Load the autoencoder prompt with the specified mode."""
    prompt_path = Path(__file__).parent / "autoencoder_prompt.md"
    text = prompt_path.read_text(encoding="utf-8")
    return text.rsplit("MODE:", 1)[0] + f"MODE: {mode}"


def encode_image(client: genai.Client, image_path: Path, prompt: str) -> str:
    """Send an image + ENCODE prompt to Gemini Flash, return the text encoding."""
    img = Image.open(image_path)
    response = client.models.generate_content(
        model=ENCODE_MODEL,
        contents=[prompt, img],
        config=types.GenerateContentConfig(temperature=0.0),
    )
    return response.text


def decode_encoding(client: genai.Client, encoding: str, prompt: str) -> Image.Image:
    """Send a text encoding + DECODE prompt to Gemini, return the reconstructed image."""
    response = client.models.generate_content(
        model=DECODE_MODEL,
        contents=f"{prompt}\n\n{encoding}",
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            temperature=0.0,
        ),
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            return Image.open(io.BytesIO(part.inline_data.data))
    raise RuntimeError("Model did not return an image")


def compute_mse(original: Path, reconstructed: Image.Image) -> float:
    """Compute per-pixel RMSE between the original and reconstructed images."""
    orig = np.array(Image.open(original).convert("RGB"), dtype=np.float64)
    recon = np.array(
        reconstructed.convert("RGB").resize((orig.shape[1], orig.shape[0])),
        dtype=np.float64,
    )
    return float(np.sqrt(np.mean((orig - recon) ** 2)))


def main():
    parser = argparse.ArgumentParser(
        description="Image autoencoder: encode images to text via Gemini Flash, "
        "reconstruct via Gemini image generation, and measure RMSE."
    )
    parser.add_argument("images_dir", help="Path to directory containing input images")
    parser.add_argument(
        "-o", "--output", default="output", help="Output directory (default: output)"
    )
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit("Error: GOOGLE_API_KEY not found in environment or .env file")

    client = genai.Client(api_key=api_key)

    encode_prompt = load_prompt("ENCODE")
    decode_prompt = load_prompt("DECODE")

    images_dir = Path(args.images_dir)
    if not images_dir.is_dir():
        sys.exit(f"Error: not a directory: {images_dir}")

    image_files = sorted(
        f for f in images_dir.iterdir() if f.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not image_files:
        sys.exit(f"No images found in {images_dir}")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Found {len(image_files)} image(s)\n")

    results = []

    for img_path in image_files:
        print(f"--- {img_path.name} ---")

        # Encode: image -> text
        print("  Encoding...")
        encoding = encode_image(client, img_path, encode_prompt)
        word_count = len(encoding.split())
        print(f"  Encoding ({word_count} words):")
        for line in encoding.strip().splitlines():
            print(f"    {line}")

        (output_dir / f"{img_path.stem}_encoding.txt").write_text(
            encoding, encoding="utf-8"
        )

        # Decode: text -> image
        print("  Decoding...")
        reconstructed = decode_encoding(client, encoding, decode_prompt)
        recon_path = output_dir / f"{img_path.stem}_reconstructed.png"
        reconstructed.save(recon_path)
        print(f"  Saved: {recon_path}")

        # Side-by-side comparison
        original = Image.open(img_path).convert("RGB")
        recon_resized = reconstructed.convert("RGB").resize(original.size)
        comparison = Image.new("RGB", (original.width * 2, original.height))
        comparison.paste(original, (0, 0))
        comparison.paste(recon_resized, (original.width, 0))
        comp_path = output_dir / f"{img_path.stem}_comparison.png"
        comparison.save(comp_path)
        print(f"  Comparison: {comp_path}")

        # RMSE
        mse = compute_mse(img_path, reconstructed)
        print(f"  RMSE: {mse:.2f}\n")

        results.append({"image": img_path.name, "mse": mse, "words": word_count})

    # Summary
    print("=" * 55)
    print(f"{'Image':<30} {'RMSE':>12} {'Words':>10}")
    print("-" * 55)
    for r in results:
        print(f"{r['image']:<30} {r['mse']:>12.2f} {r['words']:>10}")
    print("-" * 55)
    avg_mse = np.mean([r["mse"] for r in results])
    avg_words = np.mean([r["words"] for r in results])
    print(f"{'AVERAGE':<30} {avg_mse:>12.2f} {avg_words:>10.1f}")


if __name__ == "__main__":
    main()
