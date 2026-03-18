from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ManagedBuildConfig


BASE_IMAGE = "python:3.14-slim"
OCIAPP_GIT_REQUIREMENT = (
    "ociapp @ git+https://github.com/thearchitector/ociapp.git@main"
    "#subdirectory=packages/ociapp"
)
APP_USER = "ociapp"


def render_managed_containerfile(config: "ManagedBuildConfig", wheel_name: str) -> str:
    """Renders the built-in managed OCIApp Containerfile."""

    system_packages = " ".join(config.system_packages)
    if system_packages:
        package_install = (
            f"RUN apt-get update && apt-get install -y --no-install-recommends "
            f"tini {system_packages} && rm -rf /var/lib/apt/lists/*"
        )
    else:
        package_install = (
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "tini && rm -rf /var/lib/apt/lists/*"
        )

    return "\n".join([
        f"FROM {BASE_IMAGE}",
        package_install,
        (
            f"RUN groupadd --system {APP_USER} && "
            f"useradd --system --gid {APP_USER} --create-home --home-dir /home/{APP_USER} {APP_USER}"
        ),
        "COPY dist/ /tmp/dist/",
        f'RUN python -m pip install --no-cache-dir /tmp/dist/{wheel_name} "{OCIAPP_GIT_REQUIREMENT}"',
        f"USER {APP_USER}",
        'ENTRYPOINT ["tini", "--"]',
        f'CMD ["ociapp", "serve", "--app", "{config.entrypoint}"]',
        "",
    ])
