from app.core.chat_attachments import (
    extract_attachments,
    extract_image_refs,
    format_attachment_status,
    render_attachments_tag,
    render_image_refs_tag,
    strip_attachments_tag,
    strip_image_refs_tag,
)


def test_attachment_tag_round_trip_and_strip():
    attachments = [{
        "path": ".SiGMA/chat_attachments/20200101-105002-1a2b3c.png",
        "mime_type": "image/png",
        "name": "pasted.png",
        "size": 123,
    }]
    content = f"<status>current_time: now</status>{render_attachments_tag(attachments)}\nLook at this"

    assert extract_attachments(content) == attachments
    assert strip_attachments_tag(content) == "<status>current_time: now</status>\nLook at this"


def test_attachment_status_points_model_to_vision_tool():
    status = format_attachment_status([{
        "path": ".SiGMA/chat_attachments/example.jpg",
        "mime_type": "image/jpeg",
    }])

    assert ".SiGMA/chat_attachments/example.jpg" in status
    assert "vision_analyze" in status


def test_image_refs_tag_round_trip_and_strip():
    refs = [{
        "path": "/tmp/example.jpg",
        "mime_type": "image/jpeg",
        "name": "example.jpg",
        "source": "read",
        "text": "Image file: /tmp/example.jpg (100x200)",
    }]
    content = f"Image file: /tmp/example.jpg{render_image_refs_tag(refs)}"

    assert extract_image_refs(content) == refs
    assert strip_image_refs_tag(content).strip() == "Image file: /tmp/example.jpg"
