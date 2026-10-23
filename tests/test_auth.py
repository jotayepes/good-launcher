import pytest

from gogl.auth import AuthError, extract_code


def test_extract_code_from_redirect_url():
    url = "https://embed.gog.com/on_login_success?origin=client&code=ABC123"
    assert extract_code(url) == "ABC123"
    assert extract_code("  ABC123 ") == "ABC123"
    with pytest.raises(AuthError):
        extract_code("https://embed.gog.com/on_login_success?origin=client")
