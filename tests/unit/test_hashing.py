from app.services.hashing import content_hash


def test_known_sha256():
    assert content_hash("hello") == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_same_content_same_hash():
    assert content_hash("same text") == content_hash("same text")


def test_different_content_different_hash():
    assert content_hash("version one") != content_hash("version two")
