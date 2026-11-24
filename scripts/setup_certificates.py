"""Готовит отдельную доверенную цепочку GigaChat, не меняя системные сертификаты."""
import ssl
from pathlib import Path

import certifi
import requests

ROOT = Path(__file__).resolve().parent.parent
URL = "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt"


def main():
    response = requests.get(URL, timeout=30)
    response.raise_for_status()
    certificate = response.content
    if b"-----BEGIN CERTIFICATE-----" not in certificate:
        raise ValueError("Источник не вернул PEM-сертификат")
    directory = ROOT / "certs"
    directory.mkdir(exist_ok=True)
    bundle = directory / "gigachat-ca.pem"
    bundle.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + certificate)
    ssl.create_default_context(cafile=str(bundle))
    print(f"Сертификаты подготовлены. Укажите в .env:\nGIGACHAT_CA_BUNDLE={bundle}")


if __name__ == "__main__":
    main()
