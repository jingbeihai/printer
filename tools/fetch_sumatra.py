"""Fetch the official portable SumatraPDF used by the Windows print backend."""
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import requests

URL = "https://www.sumatrapdfreader.org/dl/rel/3.6.1/SumatraPDF-3.6.1-64.zip"
TARGET = Path(__file__).with_name("SumatraPDF.exe")
LICENSE = Path(__file__).with_name("SumatraPDF-COPYING.txt")

with requests.Session() as session:
    session.trust_env = False
    response = session.get(URL, timeout=60)
    response.raise_for_status()
    with ZipFile(BytesIO(response.content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".exe")]
        if len(names) != 1 or not Path(names[0]).name.lower().startswith("sumatrapdf-"):
            raise RuntimeError(f"Unexpected portable archive contents: {names}")
        TARGET.write_bytes(archive.read(names[0]))
    notice = session.get(
        "https://raw.githubusercontent.com/sumatrapdfreader/sumatrapdf/master/COPYING",
        timeout=30,
    )
    notice.raise_for_status()
    LICENSE.write_bytes(notice.content)
print(f"Fetched official SumatraPDF portable: {TARGET.stat().st_size} bytes")
