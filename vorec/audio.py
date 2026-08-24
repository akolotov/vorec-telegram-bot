"""Audio preparation and OpenAI-compatible transcription helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from openai import OpenAI


MERGE_PROMPT = """You are a professional editor of Russian speech transcripts. The two ASR transcripts below describe the same recording. Produce one complete, faithful, readable transcript in Russian.

This is transcription consolidation, not a summary, rewrite, analysis, or fact-check. Use only information in the two sources. Preserve the union of meaningful content in chronological order: ideas, qualifications, questions, examples, names, numbers, dates, commands, and closing phrases. When sources differ, choose the reading best supported by grammar and surrounding context; do not invent a third interpretation. Preserve content appearing in only one source unless it is clearly ASR garbage, an accidental duplicate, or an unrecoverable fragment.

Edit into natural Russian while retaining the speaker's register and intent. Correct clear recognition errors, grammar, word order, punctuation, capitalization, and obvious repetitions, but do not add facts or silently remove meaningful detail. Split into sensible paragraphs. Before answering, silently check that no meaningful section or concrete detail has been lost.

Return only the completed transcript: no title, summary, source labels, Markdown, or explanation."""


def resolve_converter(converter: str) -> str:
    """Return an executable path, using the bundled ffmpeg when needed."""
    if converter != "ffmpeg" or shutil.which(converter):
        return converter

    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def prepare_wav(source: Path, target: Path, converter: str, overwrite: bool) -> None:
    if target.exists() and not overwrite:
        print(f"Using existing WAV: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    converter = resolve_converter(converter)
    if Path(converter).name == "afconvert":
        command = [
            converter,
            "-f",
            "WAVE",
            "-d",
            "LEI16@16000",
            "-c",
            "1",
            "--mix",
            str(source),
            str(target),
        ]
    else:
        command = [
            converter,
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(target),
        ]
    print(f"Preparing WAV: {source} -> {target}")
    subprocess.run(command, check=True)


def response_dict(response: Any) -> dict:
    """Preserve the complete OpenAI-compatible response alongside extracted text."""
    return response.model_dump(mode="json")


def whisper_transcribe(wav_path: Path, client: OpenAI, model: str) -> dict:
    with wav_path.open("rb") as audio:
        response = client.audio.transcriptions.create(
            model=model, file=audio, language="ru", response_format="verbose_json"
        )
    return response_dict(response)


def merge_transcripts(
    primary_text: str, gigaam_text: str, client: OpenAI, model: str
) -> tuple[dict, str]:
    content = (
        f"{MERGE_PROMPT}\n\n<TRANSCRIPT_A>\n{primary_text}\n</TRANSCRIPT_A>"
        f"\n\n<TRANSCRIPT_B>\n{gigaam_text}\n</TRANSCRIPT_B>"
    )
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )
    text = response.choices[0].message.content if response.choices else None
    if not isinstance(text, str) or not text.strip():
        raise ValueError("The inference provider merge response contains empty assistant text.")
    return response_dict(response), text
