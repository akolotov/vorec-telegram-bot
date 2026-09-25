"""Probe structured title and tag generation against the configured inference server.

When the inference host resolves on the host, run with
``.venv/bin/python -m scripts.probe_structured_title_tags``. For a host name
available only on the Compose network, copy the current source files into a
temporary container directory and run the module there::

    docker compose exec -T bot mkdir -p /tmp/vorec-structured-probe
    tar -cf - vorec/__init__.py vorec/audio.py vorec/storage.py scripts/probe_structured_title_tags.py | docker compose exec -T bot tar -xf - -C /tmp/vorec-structured-probe
    docker compose exec -T -w /tmp/vorec-structured-probe bot python -m scripts.probe_structured_title_tags

This script uses synthetic transcripts and tags; it does not touch the database.
"""

from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import ValidationError

from vorec.audio import generate_transcript_title
from vorec.storage import TagDefinition

TAGS = (
    (101, "работа", "Рабочие задачи, проекты, встречи и решения."),
    (102, "здоровье", "Самочувствие, лечение, упражнения и медицинские вопросы."),
    (103, "покупки", "Выбор, заказ или приобретение товаров и услуг."),
    (104, "поездки", "Маршруты, билеты, проживание и планы путешествий."),
)

EXAMPLES = (
    (
        "one matching tag",
        "Нужно сегодня заказать новый фильтр для воды. Сравню цены в двух "
        "магазинах и оформлю доставку на субботу.",
    ),
    (
        "multiple matching tags",
        "На рабочей встрече решили провести презентацию нового приложения "
        "клиенту в Казани и согласовать время выступления с командой. "
        "Для двухдневной поездки нужно выбрать поезд, купить билеты и "
        "забронировать гостиницу рядом с офисом клиента.",
    ),
    (
        "no matching tag",
        "Вспомнил, как в детстве мы с дедушкой собирали бумажные кораблики "
        "и пускали их по ручью после дождя.",
    ),
)


def main() -> None:
    load_dotenv(".env")
    required = ("INFERENCE_API_URL", "INFERENCE_API_KEY")
    if any(not os.getenv(name) for name in required):
        raise SystemExit("Primary inference configuration is incomplete.")

    client = OpenAI(
        base_url=os.environ["INFERENCE_API_URL"],
        api_key=os.environ["INFERENCE_API_KEY"],
        timeout=60,
        max_retries=0,
    )
    model = os.getenv("TITLE_MODEL", "gemma-4-26b-a4b-it-4bit")
    tags = (
        ()
        if sys.argv[1:] == ["no-tags"]
        else tuple(TagDefinition(*tag) for tag in TAGS)
    )
    print("Using configured primary inference endpoint and title model.", flush=True)
    print("Allowed tag names:", [tag.name for tag in tags], flush=True)

    if len(sys.argv) == 1:
        examples = EXAMPLES
    elif sys.argv[1] == "no-tags":
        examples = (EXAMPLES[0],)
    else:
        examples = (EXAMPLES[int(sys.argv[1])],)
    for label, transcript in examples:
        started = time.monotonic()
        print(f"\nCase: {label}", flush=True)
        try:
            response, title, selected_tags = generate_transcript_title(
                transcript, client, model, tags
            )
            print("Validated: yes")
            print("Model JSON:", response["choices"][0]["message"]["content"])
            print(f"Title: {title}")
            print(f"Selected tags: {list(selected_tags)}")
        except Exception as error:
            # Deliberately avoid printing exception text: it can include endpoint details.
            print(f"Validated: no; error type: {type(error).__name__}")
            if isinstance(error, ValidationError):
                print("Validation issues:", error.errors(include_input=False))
        print(f"Elapsed: {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
