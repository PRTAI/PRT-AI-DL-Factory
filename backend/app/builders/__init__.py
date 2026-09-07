"""Job-config -> executable-command builders, one per framework."""

from __future__ import annotations

from pathlib import Path

from ..models import Framework, Job
from . import harbor as harbor_builder
from . import llamafactory as llamafactory_builder
from . import trl as trl_builder
from . import verl as verl_builder
from .base import BuildError, BuildResult

_BUILDERS = {
    Framework.LLAMAFACTORY: llamafactory_builder.build,
    Framework.VERL: verl_builder.build,
    Framework.TRL: trl_builder.build,
    Framework.HARBOR: harbor_builder.build,
}


def build_job(job: Job, work_dir: Path, output_dir: Path) -> BuildResult:
    builder = _BUILDERS.get(job.framework)
    if builder is None:
        raise BuildError(f"未知框架: {job.framework}")
    return builder(job, work_dir, output_dir)


__all__ = ["BuildError", "BuildResult", "build_job"]
