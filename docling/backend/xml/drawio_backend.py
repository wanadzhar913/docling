"""Backend to parse draw.io diagram files in XML format.

Draw.io (diagrams.net) stores diagrams as XML with a root ``<mxGraphModel>`` element
or inside an ``<mxfile>`` container with one or more ``<diagram>`` pages.
"""

from __future__ import annotations

import base64
import logging
import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Final
from urllib.parse import unquote

from bs4 import BeautifulSoup
from docling_core.types.doc import (
    DocItemLabel,
    DoclingDocument,
    DocumentOrigin,
    GraphCell,
    GraphCellLabel,
    GraphData,
    GraphLink,
    GraphLinkLabel,
    NodeItem,
)
from lxml import etree
from typing_extensions import override

from docling.backend.abstract_backend import DeclarativeDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import InputDocument

_log = logging.getLogger(__name__)

_SKIP_CELL_IDS: Final[frozenset[str]] = frozenset({"0", "1"})


@dataclass(frozen=True)
class DrawioCell:
    cell_id: str
    value: str
    style: str
    parent: str
    is_vertex: bool
    is_edge: bool
    source: str
    target: str
    x: str
    y: str
    width: str
    height: str


@dataclass(frozen=True)
class DrawioPage:
    name: str
    graph_model: etree._Element


def _strip_html(value: str) -> str:
    if not value or "<" not in value:
        return value.strip()
    return BeautifulSoup(value, "html.parser").get_text(separator=" ", strip=True)


def _decode_compressed_diagram(data: str) -> str:
    """Decode draw.io compressed diagram payload."""
    decoded = unquote(data)
    raw = base64.b64decode(decoded)
    inflated = zlib.decompress(raw, -15)
    return inflated.decode("utf-8")


def _parse_graph_model_element(element: etree._Element) -> etree._Element:
    tag = etree.QName(element).localname
    if tag == "mxGraphModel":
        return element
    graph_model = element.find(".//mxGraphModel")
    if graph_model is None:
        raise ValueError("No mxGraphModel element found")
    return graph_model


def _extract_pages(root: etree._Element) -> list[DrawioPage]:
    tag = etree.QName(root).localname
    if tag == "mxGraphModel":
        return [DrawioPage(name="Diagram", graph_model=root)]

    pages: list[DrawioPage] = []
    for diagram in root.findall("diagram"):
        page_name = diagram.get("name") or diagram.get("id") or "Diagram"
        diagram_text = (diagram.text or "").strip()
        if diagram_text and not diagram_text.lstrip().startswith("<"):
            diagram_xml = _decode_compressed_diagram(diagram_text)
            page_root = etree.fromstring(diagram_xml.encode("utf-8"))
            graph_model = _parse_graph_model_element(page_root)
        else:
            child_model = diagram.find("mxGraphModel")
            if child_model is None:
                graph_model = _parse_graph_model_element(diagram)
            else:
                graph_model = child_model
        pages.append(DrawioPage(name=page_name, graph_model=graph_model))
    return pages


def _iter_cells(graph_model: etree._Element) -> list[DrawioCell]:
    cells: list[DrawioCell] = []
    for cell in graph_model.findall(".//mxCell"):
        cell_id = cell.get("id")
        if not cell_id or cell_id in _SKIP_CELL_IDS:
            continue

        geometry = cell.find("mxGeometry")
        cells.append(
            DrawioCell(
                cell_id=cell_id,
                value=_strip_html(cell.get("value") or ""),
                style=cell.get("style") or "",
                parent=cell.get("parent") or "",
                is_vertex=cell.get("vertex") == "1",
                is_edge=cell.get("edge") == "1",
                source=cell.get("source") or "",
                target=cell.get("target") or "",
                x=geometry.get("x", "") if geometry is not None else "",
                y=geometry.get("y", "") if geometry is not None else "",
                width=geometry.get("width", "") if geometry is not None else "",
                height=geometry.get("height", "") if geometry is not None else "",
            )
        )
    return cells


class DrawioDocumentBackend(DeclarativeDocumentBackend):
    """Backend to parse draw.io diagram files."""

    @override
    def __init__(
        self,
        in_doc: InputDocument,
        path_or_stream: BytesIO | Path,
    ) -> None:
        super().__init__(in_doc, path_or_stream)
        self.valid = False
        self.pages: list[DrawioPage] = []

        try:
            if isinstance(path_or_stream, BytesIO):
                path_or_stream.seek(0)
            parser = etree.XMLParser(
                resolve_entities=False,
                load_dtd=False,
                no_network=True,
                dtd_validation=False,
            )
            tree = etree.parse(path_or_stream, parser=parser)
            root = tree.getroot()
            root_tag = etree.QName(root).localname
            if root_tag not in {"mxGraphModel", "mxfile"}:
                return
            self.pages = _extract_pages(root)
            self.valid = bool(self.pages)
        except Exception as exc:
            raise RuntimeError(
                "Could not initialize draw.io backend for file with hash "
                f"{self.document_hash}."
            ) from exc

    @override
    def is_valid(self) -> bool:
        return self.valid

    @classmethod
    @override
    def supports_pagination(cls) -> bool:
        return False

    @classmethod
    @override
    def supported_formats(cls) -> set[InputFormat]:
        return {InputFormat.DRAWIO}

    @override
    def convert(self) -> DoclingDocument:
        if not self.is_valid():
            raise RuntimeError(
                f"Invalid draw.io document with hash {self.document_hash}"
            )

        origin = DocumentOrigin(
            filename=self.file.name or "file",
            mimetype="application/xml",
            binary_hash=self.document_hash,
        )
        doc = DoclingDocument(name=self.file.stem or "file", origin=origin)
        doc.add_title(text=doc.name)

        cells: list[GraphCell] = []
        links: list[GraphLink] = []
        cell_id_to_graph_id: dict[str, int] = {}
        next_id = 0
        created_links: set[tuple[int, int]] = set()

        def add_link(label: GraphLinkLabel, src: int, tgt: int) -> None:
            key = (src, tgt)
            if key in created_links:
                return
            created_links.add(key)
            links.append(GraphLink(label=label, source_cell_id=src, target_cell_id=tgt))

        for page in self.pages:
            parent: NodeItem = doc.add_heading(text=page.name, level=1)
            page_cells = _iter_cells(page.graph_model)

            for drawio_cell in page_cells:
                if drawio_cell.value:
                    doc.add_text(
                        parent=parent,
                        label=DocItemLabel.TEXT,
                        text=drawio_cell.value,
                    )

            for drawio_cell in page_cells:
                if not (drawio_cell.is_vertex or drawio_cell.is_edge):
                    continue

                label_parts = [drawio_cell.value or drawio_cell.cell_id]
                if drawio_cell.is_vertex:
                    label_parts.append("vertex")
                if drawio_cell.is_edge:
                    label_parts.append("edge")
                if drawio_cell.style:
                    label_parts.append(f"style={drawio_cell.style}")
                if any(
                    (
                        drawio_cell.x,
                        drawio_cell.y,
                        drawio_cell.width,
                        drawio_cell.height,
                    )
                ):
                    label_parts.append(
                        "geometry="
                        f"x={drawio_cell.x},y={drawio_cell.y},"
                        f"w={drawio_cell.width},h={drawio_cell.height}"
                    )

                graph_cell = GraphCell(
                    label=GraphCellLabel.KEY,
                    cell_id=next_id,
                    text=" | ".join(label_parts),
                    orig=drawio_cell.cell_id,
                )
                cells.append(graph_cell)
                cell_id_to_graph_id[drawio_cell.cell_id] = next_id
                next_id += 1

                if drawio_cell.parent and drawio_cell.parent in cell_id_to_graph_id:
                    add_link(
                        GraphLinkLabel.TO_CHILD,
                        cell_id_to_graph_id[drawio_cell.parent],
                        graph_cell.cell_id,
                    )

            for drawio_cell in page_cells:
                if not drawio_cell.is_edge:
                    continue
                if drawio_cell.source not in cell_id_to_graph_id:
                    continue
                if drawio_cell.target not in cell_id_to_graph_id:
                    continue
                add_link(
                    GraphLinkLabel.TO_CHILD,
                    cell_id_to_graph_id[drawio_cell.source],
                    cell_id_to_graph_id[drawio_cell.target],
                )

        if cells and links:
            doc.add_key_values(graph=GraphData(cells=cells, links=links))

        return doc
