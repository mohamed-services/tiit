#!/usr/bin/env python3
"""Image autoencoder pipeline using Gemini models.

Encodes images to text via Gemini, reconstructs via Gemini image generation,
then iteratively refines the encoding by comparing original vs reconstruction.
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

ENCODE_MODEL = "gemini-3.1-pro-preview"
DECODE_MODEL = "gemini-3-pro-image-preview"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}

REFINE_PROMPT = """\
You are the encoder half of an image autoencoder. You previously produced the encoding below, \
and a decoder model used it to reconstruct the image.

Your previous encoding:
{encoding}

You are given three images:
1. The ORIGINAL image
2. The RECONSTRUCTED image (decoder output)
3. The ERROR MAP — a heat map showing pixel-level differences. Bright/white regions have the \
highest error; dark/black regions are accurate. This tells you exactly where the reconstruction \
fails most.

Focus your corrections on the bright regions in the error map — these are the biggest sources \
of RMSE. Fix wrong colors, misplaced elements, missing or extra details, incorrect text, \
wrong spatial layout, or lighting errors in those regions.

Now produce an IMPROVED encoding that corrects these errors. Use the same encoding format. \
You may add, remove, or rewrite any field. \
Output ONLY the improved encoding, nothing else.
"""


def load_prompt(mode: str, prompt_file: Path | None = None) -> str:
    """Load the autoencoder prompt with the specified mode."""
    prompt_path = prompt_file or (Path(__file__).parent / "autoencoder_prompt.md")
    text = prompt_path.read_text(encoding="utf-8")
    return text.rsplit("MODE:", 1)[0] + f"MODE: {mode}"


def encode_image(client: genai.Client, image_path: Path, prompt: str) -> str:
    """Send an image + ENCODE prompt to Gemini, return the text encoding."""
    img = Image.open(image_path)
    response = client.models.generate_content(
        model=ENCODE_MODEL,
        contents=[prompt, img],
        config=types.GenerateContentConfig(temperature=0.0),
    )
    return response.text


def make_error_map(original_path: Path, reconstructed: Image.Image) -> Image.Image:
    """Create a heat map of per-pixel error (amplified for visibility)."""
    orig = np.array(Image.open(original_path).convert("RGB"), dtype=np.float64)
    recon = np.array(
        reconstructed.convert("RGB").resize((orig.shape[1], orig.shape[0])),
        dtype=np.float64,
    )
    # Per-pixel RMSE across channels, normalized to 0-255 and amplified
    error = np.sqrt(np.mean((orig - recon) ** 2, axis=2))
    error = np.clip(error * 3, 0, 255).astype(np.uint8)  # 3x amplification
    return Image.fromarray(error, mode="L")


def refine_encoding(
    client: genai.Client,
    original_path: Path,
    reconstructed: Image.Image,
    previous_encoding: str,
) -> str:
    """Compare original vs reconstruction with error map and produce an improved encoding."""
    original = Image.open(original_path)
    error_map = make_error_map(original_path, reconstructed)
    prompt = REFINE_PROMPT.format(encoding=previous_encoding)
    response = client.models.generate_content(
        model=ENCODE_MODEL,
        contents=[prompt, original, reconstructed, error_map],
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
    if not response.candidates or not response.candidates[0].content.parts:
        raise RuntimeError("Model did not return an image (empty response)")
    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            return Image.open(io.BytesIO(part.inline_data.data))
    raise RuntimeError("Model did not return an image")


def compute_rmse(original: Path, reconstructed: Image.Image) -> float:
    """Compute per-pixel RMSE between the original and reconstructed images."""
    orig = np.array(Image.open(original).convert("RGB"), dtype=np.float64)
    recon = np.array(
        reconstructed.convert("RGB").resize((orig.shape[1], orig.shape[0])),
        dtype=np.float64,
    )
    return float(np.sqrt(np.mean((orig - recon) ** 2)))


def make_comparison(original_path: Path, reconstructed: Image.Image) -> Image.Image:
    """Create a side-by-side comparison image."""
    original = Image.open(original_path).convert("RGB")
    recon_resized = reconstructed.convert("RGB").resize(original.size)
    comparison = Image.new("RGB", (original.width * 2, original.height))
    comparison.paste(original, (0, 0))
    comparison.paste(recon_resized, (original.width, 0))
    return comparison


def process_image(
    client: genai.Client,
    img_path: Path,
    encode_prompt: str,
    decode_prompt: str,
    output_dir: Path,
    iterations: int,
) -> dict | None:
    """Process a single image through the encode-decode-refine loop."""
    print(f"--- {img_path.name} ---")

    # Initial encode
    print("  [iter 0] Encoding...")
    encoding = encode_image(client, img_path, encode_prompt)
    word_count = len(encoding.split())
    print(f"  [iter 0] Encoding ({word_count} words):")
    for line in encoding.strip().splitlines():
        print(f"    {line}")

    (output_dir / f"{img_path.stem}_encoding_0.txt").write_text(
        encoding, encoding="utf-8"
    )

    # Initial decode
    print("  [iter 0] Decoding...")
    try:
        reconstructed = decode_encoding(client, encoding, decode_prompt)
    except RuntimeError as e:
        print(f"  SKIPPED: {e}\n")
        return None

    reconstructed.save(output_dir / f"{img_path.stem}_reconstructed_0.png")
    make_comparison(img_path, reconstructed).save(
        output_dir / f"{img_path.stem}_comparison_0.png"
    )
    make_error_map(img_path, reconstructed).save(
        output_dir / f"{img_path.stem}_errormap_0.png"
    )
    rmse = compute_rmse(img_path, reconstructed)
    print(f"  [iter 0] RMSE: {rmse:.2f}")

    best_rmse = rmse
    best_encoding = encoding
    best_reconstructed = reconstructed
    best_iter = 0

    # Iterative refinement
    for i in range(1, iterations + 1):
        print(f"  [iter {i}] Refining encoding...")
        encoding = refine_encoding(client, img_path, reconstructed, encoding)
        word_count = len(encoding.split())
        print(f"  [iter {i}] Encoding ({word_count} words):")
        for line in encoding.strip().splitlines():
            print(f"    {line}")

        (output_dir / f"{img_path.stem}_encoding_{i}.txt").write_text(
            encoding, encoding="utf-8"
        )

        print(f"  [iter {i}] Decoding...")
        try:
            reconstructed = decode_encoding(client, encoding, decode_prompt)
        except RuntimeError as e:
            print(f"  [iter {i}] Decode failed: {e}, keeping previous best")
            continue

        reconstructed.save(output_dir / f"{img_path.stem}_reconstructed_{i}.png")
        make_error_map(img_path, reconstructed).save(
            output_dir / f"{img_path.stem}_errormap_{i}.png"
        )
        make_comparison(img_path, reconstructed).save(
            output_dir / f"{img_path.stem}_comparison_{i}.png"
        )
        rmse = compute_rmse(img_path, reconstructed)
        print(f"  [iter {i}] RMSE: {rmse:.2f} (best: {best_rmse:.2f})")

        if rmse < best_rmse:
            best_rmse = rmse
            best_encoding = encoding
            best_reconstructed = reconstructed
            best_iter = i
            print(f"  [iter {i}] ** New best! **")

    # Save final best
    best_reconstructed.save(output_dir / f"{img_path.stem}_best.png")
    make_comparison(img_path, best_reconstructed).save(
        output_dir / f"{img_path.stem}_best_comparison.png"
    )
    (output_dir / f"{img_path.stem}_best_encoding.txt").write_text(
        best_encoding, encoding="utf-8"
    )
    print(f"  Best: iter {best_iter}, RMSE {best_rmse:.2f}\n")

    return {
        "image": img_path.name,
        "rmse": best_rmse,
        "words": len(best_encoding.split()),
        "best_iter": best_iter,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Image autoencoder with iterative refinement using Gemini models."
    )
    parser.add_argument("images_dir", help="Path to directory containing input images")
    parser.add_argument(
        "-o", "--output", default="output", help="Output directory (default: output)"
    )
    parser.add_argument(
        "-p",
        "--prompt",
        default=None,
        help="Path to prompt file (default: autoencoder_prompt.md)",
    )
    parser.add_argument(
        "-n",
        "--iterations",
        type=int,
        default=0,
        help="Number of refinement iterations (default: 0 = no refinement)",
    )
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit("Error: GOOGLE_API_KEY not found in environment or .env file")

    client = genai.Client(api_key=api_key)

    prompt_file = Path(args.prompt) if args.prompt else None
    encode_prompt = load_prompt("ENCODE", prompt_file)
    decode_prompt = load_prompt("DECODE", prompt_file)

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

    print(f"Found {len(image_files)} image(s), {args.iterations} refinement iteration(s)\n")

    results = []

    for img_path in image_files:
        result = process_image(
            client, img_path, encode_prompt, decode_prompt, output_dir, args.iterations
        )
        if result:
            results.append(result)

    if not results:
        print("No images were successfully processed.")
        return

    # Summary
    print("=" * 65)
    print(f"{'Image':<30} {'RMSE':>10} {'Words':>8} {'Best Iter':>10}")
    print("-" * 65)
    for r in results:
        print(
            f"{r['image']:<30} {r['rmse']:>10.2f} {r['words']:>8} {r['best_iter']:>10}"
        )
    print("-" * 65)
    avg_rmse = np.mean([r["rmse"] for r in results])
    avg_words = np.mean([r["words"] for r in results])
    print(f"{'AVERAGE':<30} {avg_rmse:>10.2f} {avg_words:>8.1f}")


if __name__ == "__main__":
    main()
