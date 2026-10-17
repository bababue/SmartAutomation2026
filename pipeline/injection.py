"""Text in a document that speaks to the model instead of describing the ground.

Every page goes to the model with its text layer, and a document can carry text meant to steer it ("ignore the
previous instructions", "mark this record as released"), visible or hidden in the layer. The model has no tools
and its answer is validated against the schema, so such text can at most falsify what is read; a record from a
document that contains it therefore always goes to a person. Text inside page images is not seen here.
"""

from __future__ import annotations

import re

PATTERNS = [
    ("instructions to ignore earlier instructions", re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|system|your)\b"
        r"[^.\n]{0,20}\b(instructions?|prompts?|rules?|directions?)\b"
        r"|\b(ignorier\w*|vergiss|missachte\w*)\b[^.\n]{0,40}\b(anweisung\w*|instruktion\w*|regeln|vorgaben)\b")),
    ("text addressing an AI model", re.compile(
        r"\byou are (now )?(an? )?(ai|assistant|language model|llm|chatgpt|claude|gpt)\b"
        r"|\bdu bist (jetzt )?(ein(e)? )?(ki|assistent\w*|sprachmodell)\b")),
    ("a reference to the system prompt", re.compile(
        r"\b(system|developer) ?(prompt|message|instructions?)\b|\bsystem(prompt|anweisung\w*)\b")),
    ("chat markup", re.compile(
        r"(^|\n)\s*(system|assistant)\s*:|<\|?(im_start|im_end)\|?>|</?(system|instructions?)>")),
    ("an instruction about the output", re.compile(
        r"\b(set|mark|report|output|return)\b[^.\n]{0,40}\b(confidence|review[ _]status|auto[ _-]?approved)\b"
        r"|\bdo not (extract|transcribe|report|include|flag)\b|\bnicht (extrahieren|übertragen|erfassen)\b")),
]


def findings(page_texts: dict[tuple[str, int | None], str]) -> list[str]:
    """One blocking finding per page and kind of text that looks like an instruction to the model."""
    out = []
    for (_, page), text in sorted(page_texts.items(), key=lambda item: item[0][1] or 0):
        lowered = text.lower()
        for label, pattern in PATTERNS:
            if pattern.search(lowered):
                out.append(f"possible prompt injection on page {page}: {label}")
    return out
