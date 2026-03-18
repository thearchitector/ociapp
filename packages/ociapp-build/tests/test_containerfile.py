from ociapp_build.config import ManagedBuildConfig
from ociapp_build.containerfile import render_managed_containerfile


def test_render_managed_containerfile_includes_required_commands() -> None:
    rendered = render_managed_containerfile(
        ManagedBuildConfig(entrypoint="demo.main:app", system_packages=("git", "curl")),
        wheel_name="demo_app-1.2.3-py3-none-any.whl",
    )

    assert "tini git curl" in rendered
    assert "demo_app-1.2.3-py3-none-any.whl" in rendered
    assert (
        "ociapp @ git+https://github.com/thearchitector/ociapp.git@main"
        "#subdirectory=packages/ociapp"
    ) in rendered
    assert 'CMD ["ociapp", "serve", "--app", "demo.main:app"]' in rendered
