import asyncio
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from .errors import (
    HelmChartNotFoundError,
    HelmChartVersionNotFoundError,
    HelmRepositoryError,
)
from .models import HelmRepository, ResolvedChart


LOGGER = logging.getLogger(__name__)


class ChartResolver:
    def __init__(
        self,
        repositories: tuple[HelmRepository, ...],
        timeout: int = 10,
    ) -> None:
        self._repositories = repositories
        self._timeout = timeout
        # Parsed index per repository URL, for the life of this resolver.
        # Without it every install and upgrade re-downloads and re-parses
        # the full index: three consecutive resolutions of one chart cost
        # 50.6s, 51.8s and 46.0s against a 27 MB index.
        self._indexes: dict[str, dict] = {}

    async def resolve(
        self,
        chart_name: str,
        chart_version: str | None = None,
    ) -> ResolvedChart:
        """
        Resolve a chart name against supported Helm repositories.
        """

        unreachable: list = []
        for repository in self._repositories:
            index = self._indexes.get(repository.url)
            if index is None:
                try:
                    index = await asyncio.to_thread(
                        self._load_repository_index,
                        repository,
                    )
                except HelmRepositoryError as exc:
                    # Keep going. The list is searched in order, so a
                    # single unreachable mirror must not hide charts held
                    # in the repositories after it — and charts.longhorn.io
                    # is first in the default list.
                    LOGGER.warning("skipping repository '%s': %s",
                                   repository.name, exc)
                    unreachable.append(f"{repository.name} ({exc})")
                    continue
                self._indexes[repository.url] = index

            entries = index.get("entries", {})

            chart_entries = entries.get(chart_name)

            if not chart_entries:
                continue

            if chart_version is not None:
                version_found = any(
                    chart.get("version") == chart_version
                    for chart in chart_entries
                )

                if not version_found:
                    raise HelmChartVersionNotFoundError(
                        f"Chart '{chart_name}' exists in "
                        f"repository '{repository.name}', "
                        f"but version '{chart_version}' "
                        "was not found."
                    )

            LOGGER.info(
                "Resolved chart '%s' to repository '%s'",
                chart_name,
                repository.name,
            )

            return ResolvedChart(
                name=chart_name,
                repository=repository,
                version=chart_version,
            )

        # If every repository was unreachable, "chart not found" is a
        # misdiagnosis — say what actually happened.
        if unreachable and len(unreachable) == len(self._repositories):
            raise HelmRepositoryError(
                f"Chart '{chart_name}' could not be resolved because no "
                f"repository was reachable: {'; '.join(unreachable)}"
            )
        message = (
            f"Chart '{chart_name}' was not found in any searched Helm "
            f"repository ({', '.join(r.name for r in self._repositories)})."
        )
        if unreachable:
            message += f" Skipped as unreachable: {'; '.join(unreachable)}."
        raise HelmChartNotFoundError(message)

    def clear_cache(self) -> None:
        """Forgets the cached indexes, so the next resolve re-fetches.

        Needed after a chart is published: a long-lived process would
        otherwise never see a new version.
        """
        self._indexes.clear()

    def _load_repository_index(
        self,
        repository: HelmRepository,
    ) -> dict:
        index_url = (
            f"{repository.url.rstrip('/')}/index.yaml"
        )

        LOGGER.debug(
            "Fetching Helm repository index: %s",
            index_url,
        )

        request = Request(
            index_url,
            headers={
                "User-Agent": "helm-manager/1.0",
                "Accept": "application/x-yaml,text/yaml,*/*",
            },
        )

        try:
            with urlopen(
                request,
                timeout=self._timeout,
            ) as response:
                content = response.read()

        except (
            HTTPError,
            URLError,
            TimeoutError,
        ) as exc:
            raise HelmRepositoryError(
                f"Unable to access Helm repository "
                f"'{repository.name}': {exc}"
            ) from exc

        try:
            index = yaml.safe_load(content)

        except yaml.YAMLError as exc:
            raise HelmRepositoryError(
                f"Invalid index.yaml from repository "
                f"'{repository.name}'"
            ) from exc

        if not isinstance(index, dict):
            raise HelmRepositoryError(
                f"Invalid index.yaml returned by "
                f"repository '{repository.name}'"
            )

        return index