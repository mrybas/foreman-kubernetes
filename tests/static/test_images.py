"""Static assertions on image references: every image value accepts a digest."""

import pytest
import yaml

from test_render import containers

DIGEST = "sha256:" + "0123456789abcdef" * 4
OTHER_DIGEST = "sha256:" + "fedcba9876543210" * 4
EVERYTHING = {
    **{name: {"enabled": True} for name in
       ("certificates", "dynflow", "httpd", "pulp", "candlepin", "katello", "proxy")},
    "foreman": {"fqdn": "foreman.test", "externalUrl": "https://foreman.test"},
}
# Image value path -> default repository. Containers are matched by repository.
IMAGES = {
    "image": "quay.io/foreman/foreman",
    "pulp.image": "quay.io/foreman/pulp",
    "candlepin.image": "quay.io/foreman/candlepin",
    "proxy.image": "quay.io/foreman/foreman-proxy",
    "httpd.image": "registry.access.redhat.com/ubi10/httpd-24",
    "test.image": "curlimages/curl",
}


def values(path=None, **image):
    result = yaml.safe_load(yaml.safe_dump(EVERYTHING))
    if path:
        node = result
        for key in path.split("."):
            node = node.setdefault(key, {})
        node.update(image)
    return result


def workload_images(manifests):
    """(workload name, container name, image) of every container."""
    return [(workload["metadata"]["name"], container["name"], container["image"])
            for workload in manifests.of_kind("Deployment", "Job", "Pod")
            for container in containers(workload)]


def repository_of(image):
    return image.split("@")[0] if "@" in image else image.rsplit(":", 1)[0]


def images_of(manifests, repository):
    found = [image for _, _, image in workload_images(manifests)
             if repository_of(image) == repository]
    assert found, f"no container uses {repository}"
    return found


def test_defaults_use_tags_except_httpd(render):
    manifests = render(values())
    for path, repository in IMAGES.items():
        for image in images_of(manifests, repository):
            if path == "httpd.image":
                assert image.startswith(f"{repository}@sha256:"), image
            else:
                assert "@" not in image and image.startswith(f"{repository}:"), image


@pytest.mark.parametrize("path", IMAGES)
def test_digest_wins_over_tag(render, path):
    repository = IMAGES[path]
    manifests = render(values(path, tag="ignored", digest=DIGEST))
    for image in images_of(manifests, repository):
        assert image == f"{repository}@{DIGEST}"


@pytest.mark.parametrize("path", IMAGES)
def test_digest_applies_only_to_its_image(render, path):
    manifests = render(values(path, digest=OTHER_DIGEST))
    for other, repository in IMAGES.items():
        if other not in (path, "httpd.image"):
            for image in images_of(manifests, repository):
                assert OTHER_DIGEST not in image


def test_foreman_digest_pins_every_foreman_container(render):
    """Web, Dynflow, migration and Katello Job containers move together."""
    manifests = render(values("image", digest=DIGEST))
    names = {workload for workload, _, image in workload_images(manifests)
             if repository_of(image) == IMAGES["image"]}
    assert {"foreman-foreman", "foreman-foreman-migrate-1", "foreman-foreman-pulp-smart-proxy-1",
            "foreman-foreman-dynflow-orchestrator", "foreman-foreman-dynflow-worker",
            "foreman-foreman-dynflow-worker-hosts-queue"} <= names
    assert set(images_of(manifests, IMAGES["image"])) == {f"{IMAGES['image']}@{DIGEST}"}


def test_custom_repository_with_digest(render):
    manifests = render({"image": {"repository": "registry.example.test/foreman", "digest": DIGEST}})
    for _, _, image in workload_images(manifests):
        if repository_of(image) != IMAGES["test.image"]:
            assert image == f"registry.example.test/foreman@{DIGEST}"


@pytest.mark.parametrize("digest", [
    "6e3772a97de91e2aa7aa835d82446e6fec6647e9b7001f08d5d4642de8f37077",
    "sha256:6e3772a9",
    "sha512:" + "0" * 64,
    "sha256:" + "A" * 64,
])
def test_malformed_digest_fails(render, digest):
    with pytest.raises(AssertionError, match="must be sha256:<64 hex digits>"):
        render(values("pulp.image", digest=digest))
