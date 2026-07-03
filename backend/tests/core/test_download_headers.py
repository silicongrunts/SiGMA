from fastapi.responses import Response

from app.core.downloads import content_disposition_header, download_headers


def test_download_headers_support_unicode_filename():
    headers = download_headers("中文项目.zip")
    value = headers["Content-Disposition"]

    assert value == (
        'attachment; filename="____.zip"; '
        "filename*=UTF-8''%E4%B8%AD%E6%96%87%E9%A1%B9%E7%9B%AE.zip"
    )
    value.encode("latin-1")
    Response(content=b"zip", headers=headers)


def test_download_headers_sanitize_fallback_filename():
    value = content_disposition_header('bad/name"with\r\ncontrols.zip')

    assert 'filename="bad_name_with__controls.zip"' in value
    assert "filename*=UTF-8''bad_name%22with__controls.zip" in value
    assert "\r" not in value
    assert "\n" not in value
    value.encode("latin-1")


def test_download_headers_keep_ascii_filename_readable():
    value = content_disposition_header("selected.zip")

    assert value == (
        'attachment; filename="selected.zip"; '
        "filename*=UTF-8''selected.zip"
    )
