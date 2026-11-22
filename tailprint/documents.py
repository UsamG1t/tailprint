"""Normalize documents, impose physical sheets, and build native spooler options."""

import io
import math
import os
import re
import shutil
import subprocess
from copy import copy
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree
from PIL import Image, ImageOps
from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject
from resvg_py import svg_to_bytes

FORMATS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".svg"}
DEFAULTS = dict(
    paper="A4",
    orientation="portrait",
    scale="fit",
    nup=1,
    duplex="simplex",
    booklet=False,
    pages="",
)
CHOICES = dict(
    paper=("A4", "Letter", "A3"),
    orientation=("portrait", "landscape"),
    scale=("fit", "shrink", "actual"),
    nup=(1, 2, 4, 6, 9),
    duplex=("simplex", "long", "short"),
)


def settings(raw):
    if not isinstance(raw, dict) or raw.keys() - DEFAULTS.keys():
        raise ValueError("Неизвестные параметры печати.")
    opts = {**DEFAULTS, **raw}
    if (
        any(opts[k] not in allowed for k, allowed in CHOICES.items())
        or type(opts["nup"]) is not int
    ):
        raise ValueError("Недопустимые параметры листа.")
    if type(opts["booklet"]) is not bool or not isinstance(opts["pages"], str):
        raise ValueError("Проверьте настройки печати.")
    pages = opts["pages"].replace(" ", "")
    if len(pages) > 180 or (
        pages
        and not re.fullmatch(
            r"[1-9]\d{0,3}(?:-[1-9]\d{0,3})?(?:,[1-9]\d{0,3}(?:-[1-9]\d{0,3})?)*", pages
        )
    ):
        raise ValueError("Диапазон задаётся так: 1-3,5,8.")
    opts["pages"] = pages
    if opts["booklet"]:
        opts.update(orientation="landscape", nup=2, duplex="short")
    return opts


def validate_file(suffix, data):
    if suffix not in FORMATS:
        raise ValueError("Этот формат не поддерживается.")
    if suffix == ".pdf":
        valid = data.startswith(b"%PDF-") and b"%%EOF" in data[-4096:]
    elif suffix in (".docx", ".xlsx"):
        manifest = "word/document.xml" if suffix == ".docx" else "xl/workbook.xml"
        try:
            with ZipFile(io.BytesIO(data)) as package:
                valid = manifest in package.namelist()
                if sum(p.file_size for p in package.infolist()) > 160 * 1024**2:
                    raise ValueError("Распакованный документ превышает 160 МиБ.")
        except BadZipFile:
            valid = False
    elif suffix in (".doc", ".xls"):
        valid = data.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
    elif suffix == ".png":
        valid = data.startswith(bytes.fromhex("89504E470D0A1A0A"))
    elif suffix in (".jpg", ".jpeg"):
        valid = data.startswith(b"\xff\xd8\xff")
    else:
        valid = b"<svg" in data[:65536].lower() or b":svg" in data[:65536].lower()
    if not valid:
        raise ValueError("Содержимое файла не соответствует его расширению.")


def office_program():
    candidates = [
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]) / "LibreOffice/program/soffice.exe")
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    raise ValueError("Для Word и Excel установите LibreOffice на узле принтера.")


def office_pdf(source, directory):
    profile = directory / "office-profile"
    (profile / "user").mkdir(parents=True)
    config = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
</oor:items>"""
    (profile / "user/registrymodifications.xcu").write_text(config, encoding="utf8")
    result = subprocess.run(
        [
            office_program(),
            "-env:UserInstallation=" + profile.resolve().as_uri(),
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(directory),
            str(source),
        ],
        capture_output=True,
        timeout=90,
    )
    pdf = source.with_suffix(".pdf")
    if result.returncode or not pdf.exists():
        raise ValueError(
            "LibreOffice не смог открыть документ. Проверьте файл и защиту паролем."
        )
    if pdf.stat().st_size > 160 * 1024**2:
        raise ValueError("PDF после конвертации превышает 160 МиБ.")
    return pdf


def svg_image(source):
    root = ElementTree.fromstring(source.read_bytes(), forbid_dtd=True)
    if root.tag.split("}")[-1] != "svg":
        raise ValueError("Неверный SVG.")
    for node in root.iter():
        if node.tag.split("}")[-1] in {"script", "foreignObject", "image", "feImage"}:
            raise ValueError(
                "Для SVG используйте векторные объекты без скриптов и вложенных изображений."
            )
        for key, val in node.attrib.items():
            if key.split("}")[-1] in {"href", "base"} and not val.startswith("#"):
                raise ValueError("Внешние ресурсы в SVG не поддерживаются.")
    # Bound each dimension before handing the file to the native rasterizer.
    view = root.get("viewBox", "").replace(",", " ").split()

    def size(attr):
        match = re.fullmatch(r"([0-9.]+)(px|pt|mm|cm|in)?", root.get(attr, ""))
        if not match:
            return None
        return (
            float(match[1])
            * {
                None: 1,
                "px": 1,
                "pt": 96 / 72,
                "mm": 96 / 25.4,
                "cm": 96 / 2.54,
                "in": 96,
            }[match[2]]
        )

    w, h = size("width"), size("height")
    if len(view) == 4:
        vw, vh = map(float, view[2:])
        if not w and not h:
            w, h = vw, vh
        elif not w and vh > 0:
            w = h * vw / vh
        elif not h and vw > 0:
            h = w * vh / vw
    w, h = w or 300, h or 150
    if not all(math.isfinite(v) and v > 0 for v in (w, h)):
        raise ValueError("Проверьте размеры SVG.")
    root.set("width", str(w))
    root.set("height", str(h))
    ratio = 2400 / max(w, h)
    png = svg_to_bytes(
        svg_string=ElementTree.tostring(root, encoding="unicode"),
        width=max(1, round(w * ratio)),
        height=max(1, round(h * ratio)),
        background="#ffffff",
    )
    return io.BytesIO(png), 96 * ratio


def image_pdf(source):
    obj, dpi = svg_image(source) if source.suffix == ".svg" else (source, None)
    with Image.open(obj) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError("Изображение превышает 40 мегапикселей.")
        dpi = dpi or image.info.get("dpi", (96, 96))[0]
        image = ImageOps.exif_transpose(image).convert("RGBA")
        white = Image.new("RGB", image.size, "white")
        white.paste(image, mask=image.getchannel("A"))
        out = io.BytesIO()
        white.save(out, format="PDF", resolution=float(dpi) if dpi and dpi > 0 else 96)
        out.seek(0)
        return out


def selected_pages(spec, count):
    if not count or count > 2000:
        raise ValueError("Поддерживаются документы от 1 до 2000 страниц.")
    if not spec:
        return list(range(count))
    indices = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        if not 1 <= lo <= hi <= count:
            raise ValueError(f"В документе {count} стр. Проверьте диапазон {part}.")
        indices.extend(range(lo - 1, hi))
        if len(indices) > 2000:
            raise ValueError("Диапазон превышает 2000 страниц.")
    return indices


def sides(pages, nup, booklet):
    if booklet:
        padded = pages + [None] * (-len(pages) % 4)
        for start in range(0, len(padded) // 2, 2):
            yield [padded[-start - 1], padded[start]]
            yield [padded[start + 1], padded[-start - 2]]
    else:
        for start in range(0, len(pages), nup):
            yield pages[start : start + nup]


def impose(reader, opts, target):
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("Снимите пароль с документа перед печатью.")
    indices = selected_pages(opts["pages"], len(reader.pages))
    w, h = {"A4": (595.276, 841.89), "A3": (841.89, 1190.551), "Letter": (612, 792)}[
        opts["paper"]
    ]
    landscape = opts["orientation"] == "landscape"
    if landscape:
        w, h = h, w
    cols, rows = {1: (1, 1), 2: (1, 2), 4: (2, 2), 6: (2, 3), 9: (3, 3)}[opts["nup"]]
    if landscape:
        cols, rows = rows, cols
    cw, ch = w / cols, h / rows
    writer = PdfWriter()
    for group in sides(indices, opts["nup"], opts["booklet"]):
        sheet = PageObject.create_blank_page(width=w, height=h)
        for pos, number in enumerate(group):
            if number is None:
                continue
            page = copy(reader.pages[number])
            if page.rotation:
                page.transfer_rotation_to_content()
            box = page.cropbox
            unit = float(page.user_unit)
            bw, bh = float(box.width), float(box.height)
            if min(bw, bh, unit) <= 0:
                raise ValueError("Некорректные размеры страницы.")
            # Five millimetres per cell leave space for ordinary printer hardware margins.
            aw, ah = cw - 28.346, ch - 28.346
            factor = min(aw / bw, ah / bh)
            if opts["scale"] == "actual":
                factor = unit
            elif opts["scale"] == "shrink":
                factor = min(unit, factor)
            cx, cy = (
                (float(box.left) + float(box.right)) / 2,
                (float(box.top) + float(box.bottom)) / 2,
            )
            visible_w, visible_h = min(bw, aw / factor), min(bh, ah / factor)
            page.cropbox = RectangleObject(
                (
                    cx - visible_w / 2,
                    cy - visible_h / 2,
                    cx + visible_w / 2,
                    cy + visible_h / 2,
                )
            )
            tx = (pos % cols + 0.5) * cw - factor * cx
            ty = h - (pos // cols + 0.5) * ch - factor * cy
            sheet.merge_transformed_page(
                page, Transformation((factor, 0, 0, factor, tx, ty))
            )
        writer.add_page(sheet)
    with target.open("wb") as out:
        writer.write(out)


def prepare(source, opts, target):
    if source.suffix in (".docx", ".doc", ".xlsx", ".xls"):
        pdf = office_pdf(source, source.parent)
    elif source.suffix == ".pdf":
        pdf = source
    else:
        pdf = image_pdf(source)
    impose(PdfReader(pdf), opts, target)
