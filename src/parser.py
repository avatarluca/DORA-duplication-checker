from __future__ import annotations
from src.config import SUPPLEMENTAL_VERSIONS, RDF_NS, NS

import xml.etree.ElementTree as ET



def _normalize_version(version: str | None) -> str:
    if not version:
        return ""
    return " ".join(version.lower().split())


def is_supplemental_version(version: str | None) -> bool:
    label = _normalize_version(version)
    return label in SUPPLEMENTAL_VERSIONS or label.startswith("supplemental")


def parse_rels_int_supplements(xml_bytes: bytes) -> list[str]:
    try:
        root = ET.fromstring(xml_bytes)
        descriptions = root.findall("rdf:Description", NS)
        if not descriptions:
            descriptions = [elem for elem in root.iter() if elem.tag.split("}", 1)[-1] == "Description"]

        supplements = []
        for desc in descriptions:
            about = desc.get(f"{{{RDF_NS}}}about") or desc.get("rdf:about") or ""
            dsid = about.rsplit("/", 1)[-1] if about else None
            if not dsid or dsid.endswith("_PDF-A"):
                continue

            version = ""
            for child in desc:
                tag = child.tag.split("}", 1)[-1]
                if tag == "lib4ridora-multi-embargo-document_version":
                    version = (child.text or "").strip()
                    break

            if is_supplemental_version(version):
                supplements.append(dsid)
        return supplements
    except Exception:
        return []