"""Прямой запуск сервера: python -m app."""
import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Сервер «Прозрачная вакансия»")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес прослушивания (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Порт сервера (по умолчанию 8000)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть в диапазоне 1–65535")
    uvicorn.run("app.main:app", host=args.host, port=args.port, workers=1, loop="asyncio", http="h11")


if __name__ == "__main__":
    main()
