"""Unit tests for Structured Report detection and HTML rendering."""
from __future__ import annotations

from pydicom.dataset import Dataset

from deid_app.sr_render import dataset_to_html, is_structured_report


def _content_item(value_type, concept_meaning, **kwargs):
    item = Dataset()
    item.ValueType = value_type
    concept = Dataset()
    concept.CodeMeaning = concept_meaning
    item.ConceptNameCodeSequence = [concept]
    for key, value in kwargs.items():
        setattr(item, key, value)
    return item


def test_is_structured_report_true_for_enhanced_sr():
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.88.22"
    assert is_structured_report(ds)


def test_is_structured_report_false_for_image_sop_class():
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    assert not is_structured_report(ds)


def test_is_structured_report_false_when_missing():
    ds = Dataset()
    assert not is_structured_report(ds)


def test_is_structured_report_true_for_unlisted_sop_class_via_structure():
    """A future/unlisted SR Storage SOP Class should still be detected structurally."""
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.88.999"
    ds.ContentSequence = []
    assert is_structured_report(ds)


def test_is_structured_report_false_when_pixel_data_present():
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.88.999"
    ds.ContentSequence = []
    ds.PixelData = b"\x00"
    assert not is_structured_report(ds)


def test_dataset_to_html_renders_text_and_nested_items():
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.88.22"
    child = _content_item("TEXT", "Finding", TextValue="No acute abnormality")
    root = _content_item("CONTAINER", "Impression")
    root.ContentSequence = [child]
    ds.ContentSequence = [root]

    html = dataset_to_html(ds)

    assert "Impression" in html
    assert "Finding" in html
    assert "No acute abnormality" in html
    assert "<ul>" in html and "</ul>" in html


def test_dataset_to_html_handles_missing_content_sequence():
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.88.22"

    html = dataset_to_html(ds)

    assert "No Content Sequence" in html
