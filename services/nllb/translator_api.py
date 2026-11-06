import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

load_dotenv()  # reads .env in this file's directory (repo root), separate from bot/.env

app = FastAPI()

# facebook/nllb-200-distilled-1.3B is a drop-in upgrade for translation
# quality at roughly 2-4x the latency, if that trade is worth it later.
model_name = os.environ.get("NLLB_MODEL", "facebook/nllb-200-distilled-600M")

# torch.cuda.is_available() also reports True for AMD GPUs when a
# ROCm-enabled torch build is installed (ROCm reuses the same CUDA-shaped
# API) - see requirements.txt for the correct install command per platform.
device = "cuda" if torch.cuda.is_available() else "cpu"

# Production runs GPU-only by design - fail fast instead of silently
# degrading to a slow CPU fallback. Set REQUIRE_GPU=false for CPU-only
# dev/test machines (e.g. the Windows dev box this was smoke-tested on).
REQUIRE_GPU = os.environ.get("REQUIRE_GPU", "true").lower() != "false"
if REQUIRE_GPU and device == "cpu":
    raise RuntimeError(
        "No GPU detected (torch.cuda.is_available() is False). Install the "
        "ROCm-enabled torch build for this host (see requirements.txt), or "
        "set REQUIRE_GPU=false to explicitly allow CPU fallback."
    )

# fp16 on GPU roughly halves VRAM usage (~2.4GB -> ~1.2GB for this model)
# with negligible quality loss for inference - matters here since this GPU
# is shared with LM Studio's Qwen model. CPU matmul doesn't reliably
# support fp16, so stick to fp32 there (dev/test only, REQUIRE_GPU=false).
torch_dtype = torch.float16 if device == "cuda" else torch.float32

print(f"Loading {model_name} on {device} ({torch_dtype})...")
if device == "cuda":
    # Limit PyTorch to 30% of VRAM (~4.8GB on a 16GB card) so it doesn't starve LM Studio
    torch.cuda.set_per_process_memory_fraction(0.3, 0)
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModelForSeq2SeqLM.from_pretrained(model_name, dtype=torch_dtype).to(device)
print("Ready!")

# ISO 639-1 (what language-detection libraries like cld return) -> FLORES-200
# (what NLLB's tokenizer expects). Covers the languages most likely to show
# up in EVE Online chat; unmapped codes fall back to the tokenizer's default
# rather than failing.
ISO_TO_FLORES = {
    "en": "eng_Latn",
    "ru": "rus_Cyrl",
    "de": "deu_Latn",
    "fr": "fra_Latn",
    "es": "spa_Latn",
    "pt": "por_Latn",
    "it": "ita_Latn",
    "pl": "pol_Latn",
    "nl": "nld_Latn",
    "zh": "zho_Hans",
    "zh-Hant": "zho_Hant",
    "ja": "jpn_Jpan",
    "ko": "kor_Hang",
    "uk": "ukr_Cyrl",
    "ro": "ron_Latn",
    "cs": "ces_Latn",
    "sv": "swe_Latn",
    "fi": "fin_Latn",
    "da": "dan_Latn",
    "no": "nob_Latn",
    "tr": "tur_Latn",
    "ar": "arb_Arab",
    "el": "ell_Grek",
    "he": "heb_Hebr",
    "hu": "hun_Latn",
    "bg": "bul_Cyrl",
    "sr": "srp_Cyrl",
    "hr": "hrv_Latn",
    "sk": "slk_Latn",
    "vi": "vie_Latn",
    "id": "ind_Latn",
    "th": "tha_Thai",
}

_warned_unmapped = set()

# The tokenizer's source language is global state: it must be set on every request, or a
# request without a hint silently inherits the previous request's language.
DEFAULT_SRC_LANG = tokenizer.src_lang


def flores_token_id(code):
    """Look up a FLORES-200 language token's id, or None if it's not a real one.

    Newer transformers versions dropped the `lang_code_to_id` dict that older
    NLLB code relied on - language codes are now just ordinary added tokens,
    so an unknown code silently resolves to unk_token_id instead of raising.
    """
    token_id = tokenizer.convert_tokens_to_ids(code)
    return None if token_id == tokenizer.unk_token_id else token_id


class TranslateRequest(BaseModel):
    text: str
    source_lang: str | None = None  # ISO 639-1 code, e.g. "ru"
    target_lang: str = "eng_Latn"  # NLLB code for English


@app.post("/translate")
async def translate(req: TranslateRequest):
    target_lang_id = flores_token_id(req.target_lang)
    if target_lang_id is None:
        raise HTTPException(status_code=400, detail=f"Unknown target_lang: {req.target_lang}")

    tokenizer.src_lang = DEFAULT_SRC_LANG
    if req.source_lang:
        flores_code = ISO_TO_FLORES.get(req.source_lang)
        if flores_code and flores_token_id(flores_code) is not None:
            tokenizer.src_lang = flores_code
        elif req.source_lang not in _warned_unmapped:
            print(f"No FLORES-200 mapping for source_lang '{req.source_lang}', using tokenizer default")
            _warned_unmapped.add(req.source_lang)

    # NLLB's position embeddings stop at 1024 tokens; truncate instead of crashing on huge pastes
    inputs = tokenizer(req.text, return_tensors="pt", truncation=True, max_length=1024).to(device)

    translated_tokens = model.generate(
        **inputs,
        forced_bos_token_id=target_lang_id,
        max_length=512
    )

    translation = tokenizer.batch_decode(translated_tokens, skip_special_tokens=True)[0]
    return {"translation": translation}


if __name__ == "__main__":
    import uvicorn
    # 8000 collides with other common local services (e.g. Alliance Auth) -
    # 8001 is less likely to already be taken, but verify with
    # `ss -ltnp | grep <port>` on the host before relying on it.
    port = int(os.environ.get("TRANSLATOR_API_PORT", "8001"))
    # In Docker it must listen on 0.0.0.0 (TRANSLATOR_API_HOST) so Nova's container can reach it
    uvicorn.run(app, host=os.environ.get("TRANSLATOR_API_HOST", "127.0.0.1"), port=port)
