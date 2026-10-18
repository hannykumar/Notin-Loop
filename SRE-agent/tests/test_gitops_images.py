import pytest

from executor.gitops import _image_with_tag


@pytest.mark.parametrize("image,expected", [
    ("registry:5000/team/api", "registry:5000/team/api:stable"),
    ("registry:5000/team/api:bad", "registry:5000/team/api:stable"),
    ("registry:5000/team/api@sha256:abc", "registry:5000/team/api:stable"),
    ("api:bad", "api:stable"),
])
def test_gitops_tag_change_preserves_registry_and_repository(image, expected):
    assert _image_with_tag(image, "stable") == expected
