"""Render DICOM Structured Report content sequences as read-only HTML."""
from __future__ import annotations

from .render import has_pixel_data

# DICOM SR-family Storage SOP Class UIDs (PS3.6 Annex A), current as of the
# 2026c registry. Instances of these hold no pixel data, only a nested
# ContentSequence (0040,A730) of text/code/numeric items.
SR_SOP_CLASS_UIDS = {
    "1.2.840.10008.5.1.4.1.1.88.1",   # Text SR - Trial (retired)
    "1.2.840.10008.5.1.4.1.1.88.2",   # Audio SR - Trial (retired)
    "1.2.840.10008.5.1.4.1.1.88.3",   # Detail SR - Trial (retired)
    "1.2.840.10008.5.1.4.1.1.88.4",   # Comprehensive SR - Trial (retired)
    "1.2.840.10008.5.1.4.1.1.88.11",  # Basic Text SR
    "1.2.840.10008.5.1.4.1.1.88.22",  # Enhanced SR
    "1.2.840.10008.5.1.4.1.1.88.33",  # Comprehensive SR
    "1.2.840.10008.5.1.4.1.1.88.34",  # Comprehensive 3D SR
    "1.2.840.10008.5.1.4.1.1.88.35",  # Extensible SR
    "1.2.840.10008.5.1.4.1.1.88.40",  # Procedure Log
    "1.2.840.10008.5.1.4.1.1.88.50",  # Mammography CAD SR
    "1.2.840.10008.5.1.4.1.1.88.59",  # Key Object Selection Document
    "1.2.840.10008.5.1.4.1.1.88.65",  # Chest CAD SR
    "1.2.840.10008.5.1.4.1.1.88.67",  # X-Ray Radiation Dose SR
    "1.2.840.10008.5.1.4.1.1.88.68",  # Radiopharmaceutical Radiation Dose SR
    "1.2.840.10008.5.1.4.1.1.88.69",  # Colon CAD SR
    "1.2.840.10008.5.1.4.1.1.88.70",  # Implantation Plan SR
    "1.2.840.10008.5.1.4.1.1.88.71",  # Acquisition Context SR
    "1.2.840.10008.5.1.4.1.1.88.72",  # Simplified Adult Echo SR
    "1.2.840.10008.5.1.4.1.1.88.73",  # Patient Radiation Dose SR
    "1.2.840.10008.5.1.4.1.1.88.74",  # Planned Imaging Agent Administration SR
    "1.2.840.10008.5.1.4.1.1.88.75",  # Performed Imaging Agent Administration SR
    "1.2.840.10008.5.1.4.1.1.88.76",  # Enhanced X-Ray Radiation Dose SR
    "1.2.840.10008.5.1.4.1.1.88.77",  # Waveform Annotation SR
    "1.2.840.10008.5.1.4.1.1.78.6",   # Spectacle Prescription Report
    "1.2.840.10008.5.1.4.1.1.79.1",   # Macular Grid Thickness and Volume Report
    "1.2.840.10008.5.1.4.1.1.90.1",   # Content Assessment Results
}

def is_structured_report(ds) -> bool:
    """True for known SR-family SOP Classes, or any other dataset shaped like one.

    The explicit UID set covers everything in the current DICOM registry, but
    new SR Storage SOP Classes get added to the standard periodically. Datasets
    with a top-level ContentSequence and no pixel data are, in practice, always
    SR-family objects, so that combination is treated as SR too rather than
    falling through to a pixel-data crash.
    """
    if str(getattr(ds, "SOPClassUID", "")) in SR_SOP_CLASS_UIDS:
        return True
    return not has_pixel_data(ds) and hasattr(ds, "ContentSequence")


def _item_to_html(item) -> str:
    """Recursively render one content item and its children as an <li>."""
    vtype = getattr(item, "ValueType", "")

    concept_name_seq = getattr(item, "ConceptNameCodeSequence", None)
    meaning = concept_name_seq[0].CodeMeaning if concept_name_seq else "Unknown Concept"

    val_str = ""
    if vtype == "TEXT":
        val_str = getattr(item, "TextValue", "")
    elif vtype == "CODE":
        code_seq = getattr(item, "ConceptCodeSequence", None)
        val_str = code_seq[0].CodeMeaning if code_seq else ""
    elif vtype == "NUM":
        num_seq = getattr(item, "MeasuredValueSequence", None)
        if num_seq:
            val_str = str(getattr(num_seq[0], "NumericValue", ""))
            units_seq = getattr(num_seq[0], "MeasurementUnitsCodeSequence", None)
            if units_seq:
                val_str += f" {units_seq[0].CodeMeaning}"
    elif vtype == "UIDREF":
        val_str = getattr(item, "UID", "")
    elif vtype == "DATETIME":
        val_str = getattr(item, "DateTime", "")
    elif vtype == "DATE":
        val_str = getattr(item, "Date", "")
    elif vtype == "TIME":
        val_str = getattr(item, "Time", "")
    elif vtype == "PNAME":
        val_str = str(getattr(item, "PersonName", ""))

    html = f"<li><strong>{meaning}:</strong> {val_str}" if val_str else f"<li><strong>{meaning}</strong>"

    if hasattr(item, "ContentSequence") and item.ContentSequence:
        html += "<ul>"
        for sub_item in item.ContentSequence:
            html += _item_to_html(sub_item)
        html += "</ul>"

    html += "</li>"
    return html


def dataset_to_html(ds) -> str:
    """Render a Structured Report dataset's ContentSequence as a standalone HTML document."""
    html_content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>DICOM Structured Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; background-color: #f9f9f9; color: #333; }
        h1 { color: #0056b3; border-bottom: 2px solid #0056b3; padding-bottom: 10px; }
        ul { list-style-type: square; padding-left: 25px; }
        li { margin: 6px 0; }
        strong { color: #111; }
    </style>
</head>
<body>
   
    <div style="border: 2px solid #0056b3; background-color: #e7f3ff; padding: 10px; margin: 15px 0; border-radius: 5px;">
        <p style="font-size: small; font-style: italic; margin: 0; color: #0056b3;"><strong>Note:</strong> This report is not editable in this view.</p>
    </div>
     <h1>Structured Report</h1>
    <ul>
"""

    if hasattr(ds, "ContentSequence"):
        for item in ds.ContentSequence:
            html_content += _item_to_html(item)
    else:
        html_content += "<li>No Content Sequence (0040,A730) found in this file.</li>"

    html_content += """    </ul>
</body>
</html>"""

    return html_content
