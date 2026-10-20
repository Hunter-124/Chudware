"""Plain PDF prose is text, not an implicit resource/markup capability."""

import pytest

pytest.importorskip("reportlab")
pdfium = pytest.importorskip("pypdfium2")

from PIL import Image
from services.deliverables import api, project
from services.deliverables.templates._doc import Doc, render

@pytest.fixture
def image_payload(tmp_path):
    outside = tmp_path / "outside-project"
    outside.mkdir()
    image = outside / "private.png"
    Image.new("RGB", (2, 2), color="red").save(image)
    return f'<img src="{image}" width="12" height="12"/>'

def assert_text_only_pdf(path):
    assert b"/Subtype /Image" not in path.read_bytes()
    with pdfium.PdfDocument(str(path)) as pdf:
        text = "\n".join(page.get_textpage().get_text_range() for page in pdf)
    assert "<img src=" in text
    assert "A & B < C > D" in text
    assert "<b>literal</b>" in text


def test_smoke_pdf_prose_does_not_embed_local_images(tmp_path, image_payload, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("CHUDWARE_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    result = api.make_report({
        "kind": "report", "template": "smoke_report", "format": "pdf",
        "title": "Plain prose security fixture", "outline": [
            {"type": "title", "heading": image_payload, "body": [
                image_payload, "A & B < C > D", "<b>literal</b>"]}]})
    assert result["ok"], result
    assert_text_only_pdf(project.resolve_project_path(result["path"]))


def test_shared_pdf_all_plain_text_ops_do_not_embed_images(tmp_path, image_payload):
    path = tmp_path / "doc.pdf"
    doc = Doc(image_payload)
    doc.para(image_payload).para("A & B < C > D").para("<b>literal</b>")
    doc.bullets([image_payload]).kv([(image_payload, image_payload)])
    doc.table([[image_payload, image_payload]]).image(None, image_payload)
    render(doc, "pdf", path)
    assert_text_only_pdf(path)


def test_explicit_image_op_still_embeds_an_image(tmp_path, image_payload):
    image = tmp_path / "outside-project" / "private.png"
    path = tmp_path / "explicit.pdf"
    render(Doc("Authorized image").image(image, "Plain caption A & B"), "pdf", path)
    assert b"/Subtype /Image" in path.read_bytes()
