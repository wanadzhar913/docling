import base64
import zlib
from io import BytesIO
from pathlib import Path

import pytest
from docling_core.types.doc import DocItemLabel, GraphCellLabel, GraphLinkLabel
from lxml import etree

from docling.backend.xml.drawio_backend import (
    DrawioDocumentBackend,
    _decode_compressed_diagram,
    _extract_pages,
)
from docling.datamodel.base_models import (
    DocumentStream,
    FormatToExtensions,
    InputFormat,
)
from docling.datamodel.document import (
    ConversionResult,
    InputDocument,
    _DocumentConversionInput,
)
from docling.document_converter import DocumentConverter, DrawioFormatOption

DATA_DIR = Path(__file__).parent / "data" / "drawio"


def _encode_drawio_diagram(xml_str: str) -> str:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    compressed = compressor.compress(xml_str.encode("utf-8")) + compressor.flush()
    return base64.b64encode(compressed).decode("utf-8")


def _make_input_doc(path_or_stream, filename: str | None = None) -> InputDocument:
    if isinstance(path_or_stream, Path):
        return InputDocument(
            path_or_stream=path_or_stream,
            format=InputFormat.DRAWIO,
            backend=DrawioDocumentBackend,
        )
    return InputDocument(
        path_or_stream=path_or_stream,
        format=InputFormat.DRAWIO,
        filename=filename,
        backend=DrawioDocumentBackend,
    )


def test_drawio_extension_registered():
    assert "drawio" in FormatToExtensions[InputFormat.DRAWIO]


def test_simple_graph_conversion():
    path = DATA_DIR / "simple_graph.drawio"
    backend = DrawioDocumentBackend(
        in_doc=_make_input_doc(path),
        path_or_stream=path,
    )
    assert backend.is_valid()
    doc = backend.convert()

    text_values = [item.text for item in doc.texts if item.label == DocItemLabel.TEXT]
    assert "Start" in text_values
    assert "End" in text_values
    assert len(doc.key_value_items) == 1

    graph = doc.key_value_items[0].graph
    assert graph is not None
    assert len(graph.cells) == 3
    assert all(cell.label == GraphCellLabel.KEY for cell in graph.cells)
    assert any(link.label == GraphLinkLabel.TO_CHILD for link in graph.links)


def test_mxfile_uncompressed_conversion():
    path = DATA_DIR / "mxfile_uncompressed.drawio"
    backend = DrawioDocumentBackend(
        in_doc=_make_input_doc(path),
        path_or_stream=path,
    )
    doc = backend.convert()

    headings = [
        item.text for item in doc.texts if item.label == DocItemLabel.SECTION_HEADER
    ]
    assert "Flow" in headings

    text_values = [item.text for item in doc.texts if item.label == DocItemLabel.TEXT]
    assert "Input" in text_values
    assert "Output" in text_values


def test_compressed_diagram_conversion(tmp_path: Path):
    inner_xml = (DATA_DIR / "simple_graph.drawio").read_text(encoding="utf-8")
    compressed = _encode_drawio_diagram(inner_xml)
    drawio_xml = (
        '<mxfile host="app.diagrams.net">'
        f'<diagram id="page1" name="Compressed">{compressed}</diagram>'
        "</mxfile>"
    )
    drawio_path = tmp_path / "compressed.drawio"
    drawio_path.write_text(drawio_xml, encoding="utf-8")

    backend = DrawioDocumentBackend(
        in_doc=_make_input_doc(drawio_path),
        path_or_stream=drawio_path,
    )
    assert backend.is_valid()
    doc = backend.convert()

    text_values = [item.text for item in doc.texts if item.label == DocItemLabel.TEXT]
    assert "Start" in text_values
    assert "End" in text_values


def test_decode_compressed_diagram_roundtrip():
    inner_xml = (DATA_DIR / "simple_graph.drawio").read_text(encoding="utf-8")
    encoded = _encode_drawio_diagram(inner_xml)
    decoded = _decode_compressed_diagram(encoded)
    pages = _extract_pages(
        etree.fromstring(
            f'<mxfile><diagram name="Test">{encoded}</diagram></mxfile>'.encode()
        )
    )
    assert len(pages) == 1
    assert decoded.startswith("<mxGraphModel")


@pytest.mark.parametrize("use_stream", [False, True])
def test_e2e_drawio_conversion(use_stream: bool):
    converter = DocumentConverter(allowed_formats=[InputFormat.DRAWIO])
    path = DATA_DIR / "simple_graph.drawio"

    if use_stream:
        buf = BytesIO(path.read_bytes())
        stream = DocumentStream(name=path.name, stream=buf)
        conv_result: ConversionResult = converter.convert(stream)
    else:
        conv_result = converter.convert(path)

    assert conv_result.status.name in {"SUCCESS", "PARTIAL_SUCCESS"}
    doc = conv_result.document
    text_values = [item.text for item in doc.texts if item.label == DocItemLabel.TEXT]
    assert "Start" in text_values
    assert "End" in text_values


def test_guess_format_drawio(tmp_path: Path):
    dci = _DocumentConversionInput(path_or_stream_iterator=[])

    drawio_path = DATA_DIR / "simple_graph.drawio"
    assert dci._guess_format(drawio_path) == InputFormat.DRAWIO

    xml_path = DATA_DIR / "simple_graph.xml"
    assert dci._guess_format(xml_path) == InputFormat.DRAWIO

    buf = BytesIO(drawio_path.read_bytes())
    stream = DocumentStream(name="simple_graph.drawio", stream=buf)
    assert dci._guess_format(stream) == InputFormat.DRAWIO

    buf = BytesIO(xml_path.read_bytes())
    stream = DocumentStream(name="simple_graph.xml", stream=buf)
    assert dci._guess_format(stream) == InputFormat.DRAWIO

    uspto_path = Path("./tests/data/uspto/ipa20110039701.xml")
    assert dci._guess_format(uspto_path) == InputFormat.XML_USPTO

    jats_path = Path("./tests/data/jats/elife-56337.xml")
    assert dci._guess_format(jats_path) == InputFormat.XML_JATS

    xbrl_path = Path("./tests/data/xbrl/mlac-20251231.xml")
    assert dci._guess_format(xbrl_path) == InputFormat.XML_XBRL

    generic_xml = (
        '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE docling_test SYSTEM '
        '"test.dtd"><docling>Docling parses documents</docling>'
    )
    generic_path = tmp_path / "docling_test.xml"
    generic_path.write_text(generic_xml, encoding="utf-8")
    assert dci._guess_format(generic_path) is None


def test_drawio_format_option_registered():
    converter = DocumentConverter(allowed_formats=[InputFormat.DRAWIO])
    assert InputFormat.DRAWIO in converter.allowed_formats
    assert DrawioFormatOption().backend is DrawioDocumentBackend
